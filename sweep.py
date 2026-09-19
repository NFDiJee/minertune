#!/usr/bin/env python3
"""Auto-Sweep (CLI): Kalibrierung + Frequenz/Spannungs-Sweep + Sweet-Spot-Auswertung.

Alle Schreibzugriffe mit save=false. Am Ende IMMER (try/finally) Job-Intervall und
sicherer Punkt wiederhergestellt. Messformeln: tuner.compute_metrics (verifiziert).
Ausgabe ins Terminal, Run-JSON zusaetzlich unter runs/.
"""

import json
import os
import statistics
import sys
import time
import urllib.error
import urllib.request

import tuner

# ============================================================================
# KONFIG
# ============================================================================
MINER_BASE = "http://192.168.1.100/api/v1"
STATUS = MINER_BASE + "/status" ; TUNING = MINER_BASE + "/tuning" ; MINING = MINER_BASE + "/mining" ; JOBSCHED = MINER_BASE + "/job-schedule"
HTTP_TIMEOUT = 10
SAFE_FREQ = 300 ; SAFE_MV = 950
VERIFY_TOL_MV = 12
# Temperatur-Sicherheit
SOFT_ASIC_C = 70.0 ; SOFT_VR_C = 85.0        # Punkt verwerfen + zurueck auf SAFE
HARD_ASIC_C = 80.0 ; HARD_VR_C = 95.0        # NOTABSCHALTUNG: POST /mining {"enabled":false}, Sweep-Ende
# Gueltigkeit
ERROR_MAX_PCT = 2.0                          # darueber = instabil (Ueberlastung)
HASHRATE_MIN_FRAC = 0.90                     # Hashrate muss >= 90% der Frequenz-Erwartung sein (sonst unterversorgt)
# Adaptives Warmup / Fenster
SAMPLE_S = 5
SETTLE_WINDOW_S = 30                         # so lange muss Hashrate flach sein
SETTLE_TOL_FRAC = 0.03                       # +/-3% = flach
WARMUP_MAX_S = 90                            # Deckel; danach messen, aber "not_settled" flaggen
MEASURE_S = 90                               # Messfenster (Verifikationslauf; spaeter 180)
# Totmann
DEADMAN_MAX_CONSEC = 5                       # so viele Fehler in Folge -> Abbruch
DEADMAN_RETRY_PAUSE_S = 5
# Referenzpunkt fuer Kalibrierung (bewiesen gut versorgt, schnell eingeschwungen)
REF_FREQ = 450 ; REF_MV = 1050
# Job-Intervall-Kalibrierung
JOBCAL_ALT_MS = 250                          # Alternative gegen den Default testen
JOBCAL_GAIN_FRAC = 0.03                      # nur wechseln, wenn >3% mehr Hashrate
# Plan (Verifikationsvariante)
PLAN = [(300, 950), (300, 1000), (375, 1000), (375, 1050), (450, 1050), (450, 1100)]
CAL_MEASURE_S = 60
KNEE_FACTOR = 1.5
# ============================================================================

assert tuner.STATUS_URL == STATUS

GH_PER_MHZ = None
RUN = {"status": "starting", "points": [], "calibration": {}, "log_events": []}
RUN_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "runs",
                        time.strftime("run_%Y%m%d_%H%M%S.json"))
STATE = {"wrote_tuning": False, "job_changed": False, "original_job_interval": None,
         "stock_f": None, "stock_mv": None, "emergency": False}


class Deadman(RuntimeError):
    pass


class SoftTemp(Exception):
    pass


def log(msg=""):
    print(f"{time.strftime('%H:%M:%S')}  {msg}" if msg else "", flush=True)


def _n(v, nd=1):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


def save_run():
    os.makedirs(os.path.dirname(RUN_PATH), exist_ok=True)
    tmp = RUN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(RUN, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, RUN_PATH)


# ----------------------------------------------------------------------------
# HTTP
# ----------------------------------------------------------------------------
def post_json(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode("utf-8"), method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as r:
            return r.status, r.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as e:
        try:
            text = e.read().decode("utf-8", errors="replace")
        except Exception:
            text = ""
        raise RuntimeError(f"POST {url} rejected: HTTP {e.code} {e.reason} - {text.strip()}") from e
    except Exception as e:
        raise RuntimeError(f"POST {url} failed: {type(e).__name__}: {e}") from e


def get_json(url):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, method="GET"), timeout=HTTP_TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8"))
    except Exception as e:
        raise RuntimeError(f"GET {url} failed: {type(e).__name__}: {e}") from e


def safe_get():
    """get_status mit Totmann: bis DEADMAN_MAX_CONSEC Versuche, dann Deadman."""
    last = None
    for i in range(1, DEADMAN_MAX_CONSEC + 1):
        try:
            return tuner.get_status()
        except RuntimeError as e:
            last = e
            log(f"  status error {i}/{DEADMAN_MAX_CONSEC}: {e}")
            if i < DEADMAN_MAX_CONSEC:
                time.sleep(DEADMAN_RETRY_PAUSE_S)
    raise Deadman(f"deadman: {DEADMAN_MAX_CONSEC} status errors in a row ({last})")


# ----------------------------------------------------------------------------
# Schreiben / Sicherheit
# ----------------------------------------------------------------------------
def danger_for(freq, mv, stock_f, stock_mv):
    return abs(freq - stock_f) / stock_f > 0.15 or abs(mv - stock_mv) / stock_mv > 0.15


def set_point(freq, mv, stock_f, stock_mv):
    """-> (ok, measured_mv)"""
    danger = danger_for(freq, mv, stock_f, stock_mv)
    log(f"  set {freq} MHz / {mv} mV  (Danger: f {abs(freq - stock_f) / stock_f * 100:.1f} %, "
        f"mv {abs(mv - stock_mv) / stock_mv * 100:.1f} % -> {danger})")
    payload = {"frequency_mhz": freq, "core_mv": mv, "save": False}
    if danger:
        payload["danger_acknowledged"] = True
    STATE["wrote_tuning"] = True
    code, text = post_json(TUNING, payload)
    log(f"  POST /tuning {json.dumps(payload)} -> HTTP {code} {text.strip()}")
    time.sleep(1.5)
    st = safe_get()
    cur_f, meas = st.get("current_frequency_mhz"), st.get("measured_core_mv")
    ok = cur_f == freq and isinstance(meas, (int, float)) and abs(meas - mv) <= VERIFY_TOL_MV
    log(f"  verify: f={cur_f} (Soll {freq}) mv={meas} (Soll {mv} +/-{VERIFY_TOL_MV}) -> {'OK' if ok else 'FEHLGESCHLAGEN'}")
    return ok, meas


def set_job_interval(ms):
    STATE["job_changed"] = True
    code, text = post_json(JOBSCHED, {"interval_ms": ms, "save": False})
    log(f"  POST /job-schedule {{\"interval_ms\": {ms}, \"save\": false}} -> HTTP {code} {text.strip()}")
    time.sleep(1.0)
    js = get_json(JOBSCHED)
    ok = js.get("interval_ms") == ms
    log(f"  verify job-schedule: {json.dumps(js)} -> {'OK' if ok else 'FEHLGESCHLAGEN'}")
    if not ok:
        raise RuntimeError(f"Job-Intervall {ms} ms nicht bestaetigt")
    return ok


def emergency_stop(grund):
    STATE["emergency"] = True
    try:
        code, text = post_json(MINING, {"enabled": False})
        log(f"POST /mining {{\"enabled\": false}} -> HTTP {code} {text.strip()}")
    except RuntimeError as e:
        log(f"!!! POST mining off FEHLGESCHLAGEN: {e}")
    print("!" * 76)
    print(f"!!! NOTABSCHALTUNG: {grund} - ASICs deaktiviert (mining enabled=false)")
    print("!" * 76, flush=True)
    RUN["status"] = "emergency_stop"
    RUN["emergency_reason"] = grund
    try:
        save_run()
    except Exception as e:
        log(f"Run-JSON konnte nicht gespeichert werden: {e}")
    sys.exit(1)


def check_temps(asic, vr):
    if isinstance(asic, (int, float)) and asic > HARD_ASIC_C:
        emergency_stop(f"asic_temp_c {asic:.1f} > {HARD_ASIC_C}")
    if isinstance(vr, (int, float)) and vr > HARD_VR_C:
        emergency_stop(f"vr_temp_c {vr:.1f} > {HARD_VR_C}")
    if (isinstance(asic, (int, float)) and asic > SOFT_ASIC_C) or (isinstance(vr, (int, float)) and vr > SOFT_VR_C):
        return "soft"
    return None


def expected_ths(freq):
    return freq * GH_PER_MHZ / 1000.0 if GH_PER_MHZ else None


def in_bounds(freq, mv, b):
    return (b["frequency_min"] <= freq <= b["frequency_max"] and freq % b["frequency_step"] == 0
            and b["core_min"] <= mv <= b["core_max"] and mv % b["core_step"] == 0)


# ----------------------------------------------------------------------------
# Messkern
# ----------------------------------------------------------------------------
def _schedule(duration, on_sample):
    """Ruft on_sample(st, t) alle SAMPLE_S bis duration; on_sample -> True beendet vorzeitig."""
    t_start = time.monotonic()
    n = 0
    while True:
        n += 1
        target = t_start + n * SAMPLE_S
        if target > t_start + duration + 1e-6:
            time.sleep(max(0.0, t_start + duration - time.monotonic()))
            return t_start
        time.sleep(max(0.0, target - time.monotonic()))
        st = safe_get()
        if on_sample(st, time.monotonic() - t_start):
            return t_start


def measure_point(freq, mv, stock_f, stock_mv, label, measure_s=MEASURE_S):
    base = {"label": label, "freq": freq, "mv": mv}
    log(f"--- {label}: {freq} MHz / {mv} mV ---")
    ok, meas = set_point(freq, mv, stock_f, stock_mv)
    if not ok:
        r = dict(base, status="verify_failed", valid=False, reason="verify_failed", measured_mv=meas)
        log(f"  => {label} {freq}/{mv}: VERIFY FEHLGESCHLAGEN - nicht gemessen")
        return r

    # adaptives Warmup
    warm = []  # (t, local)
    info = {"settle_s": None}
    need = int(round(SETTLE_WINDOW_S / SAMPLE_S)) + 1

    def on_warm(st, t):
        if check_temps(st.get("asic_temp_c"), st.get("vr_temp_c")) == "soft":
            raise SoftTemp(f"asic {_n(st.get('asic_temp_c'))} / vr {_n(st.get('vr_temp_c'))}")
        warm.append((t, st.get("local_hashrate_ghs")))
        recent = [v for tt, v in warm if tt >= t - SETTLE_WINDOW_S - 0.5 and isinstance(v, (int, float))]
        flat = False
        if t >= SETTLE_WINDOW_S and len(recent) >= need:
            m = statistics.mean(recent)
            flat = all(abs(v - m) <= SETTLE_TOL_FRAC * m for v in recent)
        log(f"  [warmup {t:4.0f}s] local={_n(st.get('local_hashrate_ghs'))} GH/s  wall={_n(st.get('wall_power_w'))} W  "
            f"asic={_n(st.get('asic_temp_c'))} vr={_n(st.get('vr_temp_c'))}  mv={st.get('measured_core_mv')}"
            f"{'  -> eingeschwungen' if flat else ''}")
        if flat:
            info["settle_s"] = round(t, 1)
            return True
        return False

    try:
        _schedule(WARMUP_MAX_S, on_warm)
    except SoftTemp as e:
        log(f"  => {label} {freq}/{mv}: SOFT-TEMP ({e}) - Punkt verworfen, zurueck auf SAFE")
        set_point(SAFE_FREQ, SAFE_MV, stock_f, stock_mv)
        return dict(base, status="soft_temp", valid=False, reason=f"soft_temp: {e}")
    not_settled = info["settle_s"] is None

    # Messfenster
    s0 = safe_get()
    t0 = time.monotonic()
    difficulty = s0.get("local_hashrate_difficulty")
    c0 = dict(valid0=s0.get("local_valid_candidates"), invalid0=s0.get("local_invalid_candidates"),
              accepted0=s0.get("accepted_shares"), rejected0=s0.get("rejected_shares"))
    series = {k: [] for k in tuner.SERIES_FIELDS}
    mvs = []

    def on_win(st, t):
        if check_temps(st.get("asic_temp_c"), st.get("vr_temp_c")) == "soft":
            raise SoftTemp(f"asic {_n(st.get('asic_temp_c'))} / vr {_n(st.get('vr_temp_c'))}")
        for k in series:
            series[k].append(st.get(k))
        mvs.append(st.get("measured_core_mv"))
        return False

    try:
        _schedule(measure_s, on_win)
    except SoftTemp as e:
        log(f"  => {label} {freq}/{mv}: SOFT-TEMP im Fenster ({e}) - Punkt verworfen, zurueck auf SAFE")
        set_point(SAFE_FREQ, SAFE_MV, stock_f, stock_mv)
        return dict(base, status="soft_temp", valid=False, reason=f"soft_temp: {e}")
    s1 = safe_get()
    t1 = time.monotonic()
    counters = dict(c0, valid1=s1.get("local_valid_candidates"), invalid1=s1.get("local_invalid_candidates"),
                    accepted1=s1.get("accepted_shares"), rejected1=s1.get("rejected_shares"))
    m = tuner.compute_metrics(series, counters, t1 - t0, difficulty)

    exp = expected_ths(freq)
    frac = m["hashrate_local_ths"] / exp if exp else None
    hashrate_ok = frac is None or frac >= HASHRATE_MIN_FRAC
    error_ok = m["error_pct"] < ERROR_MAX_PCT
    reasons = []
    if not hashrate_ok:
        reasons.append(f"unterversorgt: {frac * 100:.0f}% der Erwartung")
    if not error_ok:
        reasons.append(f"instabil: error {m['error_pct']:.2f}%")
    if not_settled:
        reasons.append("not_settled")
    valid = hashrate_ok and error_ok and not not_settled
    mv_vals = [v for v in mvs if isinstance(v, (int, float))]
    r = dict(base, status="measured",
             measured_mv=round(statistics.mean(mv_vals), 1) if mv_vals else None,
             error_pct=m["error_pct"], hashrate_local_ths=m["hashrate_local_ths"],
             hashrate_valid_ths=m["hashrate_valid_ths"], expected_ths=exp, frac_of_expected=frac,
             wall_avg=m["wall_avg"], rail_avg=m["rail_avg"], jth_local=m["jth_local"], jth_valid=m["jth_valid"],
             asic_temp_max=m["asic_temp_max"], vr_temp_max=m["vr_temp_max"],
             settle_s=info["settle_s"], not_settled=not_settled, valid=valid,
             reason="ok" if valid else "; ".join(reasons), http_errors=0, dur=m["dur"],
             dvalid=m["dvalid"], dinvalid=m["dinvalid"])
    log(f"  => {label} {freq:>3}/{mv:>4}  mv={_n(r['measured_mv'])}  settle={_n(r['settle_s'], 0)}s  "
        f"local={_n(r['hashrate_local_ths'], 3)} TH/s ({'%.0f%%' % (frac * 100) if frac else 'n/a'} erw.)  "
        f"valid={_n(r['hashrate_valid_ths'], 3)}  wall={_n(r['wall_avg'], 1)} W  "
        f"J/TH={_n(r['jth_local'], 2)}  err={_n(r['error_pct'], 2)}%  "
        f"asic/vr max={_n(r['asic_temp_max'])}/{_n(r['vr_temp_max'])}  -> {'GUELTIG' if valid else 'UNGUELTIG'} ({r['reason']})")
    return r


# ----------------------------------------------------------------------------
# Auswertung
# ----------------------------------------------------------------------------
def sweet_spots(points):
    valid = [p for p in points if p.get("valid")]
    out = {"efficiency": None, "knee": None}
    print()
    print("=" * 76)
    print("SWEET-SPOT-AUSWERTUNG (nur gueltige Punkte)")
    print("=" * 76)
    if not valid:
        print("  Keine gueltigen Punkte.")
        return out

    eff = min(valid, key=lambda p: p["jth_local"])
    out["efficiency"] = {k: eff[k] for k in ("freq", "mv", "jth_local", "hashrate_local_ths", "wall_avg")}
    print("1) Effizienz-Sweet-Spot (min jth_local):")
    print(f"   {eff['freq']} MHz / {eff['mv']} mV  ->  {eff['jth_local']:.2f} J/TH  |  "
          f"{eff['hashrate_local_ths']:.3f} TH/s  |  {eff['wall_avg']:.1f} W")

    # Knie: je Frequenz nur der gueltige Punkt mit geringster Leistung
    best_per_f = {}
    for p in valid:
        if p["freq"] not in best_per_f or p["wall_avg"] < best_per_f[p["freq"]]["wall_avg"]:
            best_per_f[p["freq"]] = p
    dropped = [p for p in valid if best_per_f[p["freq"]] is not p]
    curve = sorted(best_per_f.values(), key=lambda p: p["hashrate_local_ths"])
    print()
    print("2) Performance-Knie:")
    if dropped:
        print("   Je Frequenz nur Punkt mit geringster Leistung (gleiche Frequenz = gleiche Hashrate, dT waere Rauschen);")
        print("   ausgelassen: " + ", ".join(f"{p['freq']}/{p['mv']} ({p['wall_avg']:.1f} W)" for p in dropped))
    margs = []
    for i in range(1, len(curve)):
        dT = curve[i]["hashrate_local_ths"] - curve[i - 1]["hashrate_local_ths"]
        dW = curve[i]["wall_avg"] - curve[i - 1]["wall_avg"]
        margs.append((i, dW / dT if dT > 0 else None, dW, dT))
    knee = curve[-1]
    ref = next((m for _, m, _, _ in margs if m is not None), None)
    lines = []
    if ref is None:
        reason = "keine Segmente mit dT>0 -> hoechster gueltiger Punkt"
    else:
        knee = curve[0]
        reason = f"alle Segmente <= {KNEE_FACTOR} x Referenz -> hoechster Punkt"
        for i, m, dW, dT in margs:
            a, b = curve[i - 1], curve[i]
            ok = m is not None and m <= KNEE_FACTOR * ref
            lines.append(f"   {a['freq']}/{a['mv']} -> {b['freq']}/{b['mv']}: dW={dW:+.1f} W, dT={dT:+.3f} TH/s, "
                         f"marg={_n(m, 1)} W/(TH/s)  {'<=' if ok else '>'} {KNEE_FACTOR} x {ref:.1f} = {KNEE_FACTOR * ref:.1f}")
            if not ok:
                reason = f"Segment zu {b['freq']}/{b['mv']} kostet > {KNEE_FACTOR} x Referenz -> Knie davor"
                break
            knee = b
    out["knee"] = {k: knee[k] for k in ("freq", "mv", "hashrate_local_ths", "wall_avg", "jth_local")}
    out["knee"]["marg"] = [{"to": f"{curve[i]['freq']}/{curve[i]['mv']}", "marg_w_per_ths": m} for i, m, _, _ in margs]
    out["knee"]["reason"] = reason
    for ln in lines:
        print(ln)
    print(f"   Knie: {knee['freq']} MHz / {knee['mv']} mV  ->  {knee['hashrate_local_ths']:.3f} TH/s  |  "
          f"{knee['wall_avg']:.1f} W  |  {knee['jth_local']:.2f} J/TH")
    print(f"   Begruendung: {reason}")
    print()
    print("   HINWEIS: Die 6-Punkte-Variante ist fuer belastbare Sweet Spots zu klein -")
    print("   sie verifiziert nur die Mechanik (Kalibrierung, Messkern, Gueltigkeit, Auswertung).")
    print("=" * 76)
    return out


def print_table(points):
    print()
    print("=" * 76)
    print("UEBERSICHT")
    print("=" * 76)
    print(f"  {'Punkt':<8}{'f':>5}{'mv':>6}{'gem.':>8}{'settle':>8}{'TH/s':>8}{'erw%':>6}{'W':>7}{'J/TH':>7}{'err%':>7}  Status")
    for p in points:
        frac = p.get("frac_of_expected")
        print(f"  {p['label']:<8}{p['freq']:>5}{p['mv']:>6}{_n(p.get('measured_mv')):>8}{_n(p.get('settle_s'), 0):>8}"
              f"{_n(p.get('hashrate_local_ths'), 3):>8}{('%.0f' % (frac * 100)) if frac else '-':>6}"
              f"{_n(p.get('wall_avg'), 1):>7}{_n(p.get('jth_local'), 2):>7}{_n(p.get('error_pct'), 2):>7}  "
              f"{'GUELTIG' if p.get('valid') else 'ungueltig'} ({p.get('reason')})")


# ----------------------------------------------------------------------------
# Ablauf
# ----------------------------------------------------------------------------
def run():
    global GH_PER_MHZ
    # A. Startstatus
    st = safe_get()
    stock_f, stock_mv = st.get("stock_frequency_mhz"), st.get("stock_core_mv")
    STATE["stock_f"], STATE["stock_mv"] = stock_f, stock_mv
    STATE["original_job_interval"] = st.get("job_interval_ms")
    bounds = {k: st.get(k) for k in ["frequency_min", "frequency_max", "frequency_step",
                                     "core_min", "core_max", "core_step"]}
    RUN.update(miner_url=MINER_BASE, profile=st.get("profile"), profile_id=st.get("profile_id"),
               stock={"frequency_mhz": stock_f, "core_mv": stock_mv}, bounds=bounds,
               original_job_interval_ms=STATE["original_job_interval"],
               start_point={"frequency_mhz": st.get("current_frequency_mhz"), "core_mv": st.get("core_mv")},
               plan=[{"freq": f, "mv": m} for f, m in PLAN],
               config={k: globals()[k] for k in ["SAMPLE_S", "SETTLE_WINDOW_S", "SETTLE_TOL_FRAC", "WARMUP_MAX_S",
                                                 "MEASURE_S", "CAL_MEASURE_S", "ERROR_MAX_PCT", "HASHRATE_MIN_FRAC",
                                                 "REF_FREQ", "REF_MV", "JOBCAL_ALT_MS", "JOBCAL_GAIN_FRAC"]})
    log(f"Startstatus: profile={st.get('profile')} ({st.get('profile_id')})  stock {stock_f}/{stock_mv}  "
        f"current {st.get('current_frequency_mhz')}/{st.get('core_mv')}  job_interval={STATE['original_job_interval']} ms  "
        f"diff={st.get('local_hashrate_difficulty')}  mining_enabled={st.get('mining_enabled')}  "
        f"asic={_n(st.get('asic_temp_c'))} vr={_n(st.get('vr_temp_c'))}")
    log(f"Bounds: f {bounds['frequency_min']}..{bounds['frequency_max']} step {bounds['frequency_step']} | "
        f"mv {bounds['core_min']}..{bounds['core_max']} step {bounds['core_step']}")
    if st.get("mining_enabled") is not True:
        raise RuntimeError("mining_enabled ist nicht True - Abbruch ohne Schreibzugriff")
    if check_temps(st.get("asic_temp_c"), st.get("vr_temp_c")) == "soft":
        raise RuntimeError("Temperatur bereits ueber SOFT-Grenze - Abbruch ohne Schreibzugriff")
    for f, m in PLAN + [(REF_FREQ, REF_MV), (SAFE_FREQ, SAFE_MV)]:
        if not in_bounds(f, m, bounds):
            log(f"Hinweis: {f}/{m} ausserhalb der Grenzen/Schrittweite")
    if not in_bounds(REF_FREQ, REF_MV, bounds) or not in_bounds(SAFE_FREQ, SAFE_MV, bounds):
        raise RuntimeError("REF- oder SAFE-Punkt ausserhalb der Grenzen - Abbruch ohne Schreibzugriff")

    # B. Job-Intervall-Kalibrierung
    print()
    print("=" * 76)
    print(f"KALIBRIERUNG  Referenz {REF_FREQ} MHz / {REF_MV} mV, Fenster {CAL_MEASURE_S}s")
    print("=" * 76)
    orig = STATE["original_job_interval"]
    log(f"B) Job-Intervall: Default {orig} ms vs. Alternative {JOBCAL_ALT_MS} ms")
    c1 = measure_point(REF_FREQ, REF_MV, stock_f, stock_mv, f"cal-{orig}", CAL_MEASURE_S)
    if c1.get("status") != "measured":
        raise RuntimeError(f"Kalibrierung Referenzpunkt fehlgeschlagen ({c1.get('reason')})")
    h_def = c1["hashrate_local_ths"]
    set_job_interval(JOBCAL_ALT_MS)
    c2 = measure_point(REF_FREQ, REF_MV, stock_f, stock_mv, f"cal-{JOBCAL_ALT_MS}", CAL_MEASURE_S)
    if c2.get("status") != "measured":
        raise RuntimeError(f"Kalibrierung Alternative fehlgeschlagen ({c2.get('reason')})")
    h_alt = c2["hashrate_local_ths"]
    gain = h_alt / h_def - 1
    if h_alt >= h_def * (1 + JOBCAL_GAIN_FRAC):
        job_interval, h_ref, ref_pt = JOBCAL_ALT_MS, h_alt, c2
        decision = f"WECHSEL auf {JOBCAL_ALT_MS} ms (Gewinn {gain * 100:+.1f} % >= {JOBCAL_GAIN_FRAC * 100:.0f} %)"
    else:
        job_interval, h_ref, ref_pt = orig, h_def, c1
        decision = f"BLEIBT {orig} ms (Gewinn {gain * 100:+.1f} % < {JOBCAL_GAIN_FRAC * 100:.0f} %)"
        set_job_interval(orig)
        STATE["job_changed"] = False
    print()
    log(f"Job-Intervall: {orig} ms -> {h_def:.3f} TH/s ({c1['wall_avg']:.1f} W) | "
        f"{JOBCAL_ALT_MS} ms -> {h_alt:.3f} TH/s ({c2['wall_avg']:.1f} W)")
    log(f"Entscheidung Job-Intervall: {decision}")

    # C. Hashrate-Kalibrierung
    GH_PER_MHZ = h_ref * 1000 / REF_FREQ
    log(f"C) GH_PER_MHZ = {h_ref * 1000:.1f} GH/s / {REF_FREQ} MHz = {GH_PER_MHZ:.2f} GH/s pro MHz "
        f"(erwartet ~13-14)  -> Erwartung bei 300 MHz: {expected_ths(300):.3f} TH/s")
    RUN["calibration"] = {"job_default_ms": orig, "h_default_ths": h_def, "job_alt_ms": JOBCAL_ALT_MS,
                          "h_alt_ths": h_alt, "gain_frac": gain, "decision": decision,
                          "job_interval_ms": job_interval, "gh_per_mhz": GH_PER_MHZ,
                          "points": [c1, c2], "ref_point_used": ref_pt["label"]}
    RUN["gh_per_mhz"], RUN["job_interval_ms"] = GH_PER_MHZ, job_interval
    save_run()

    # Sweep
    print()
    print("=" * 76)
    print(f"SWEEP  {len(PLAN)} Punkte, Warmup adaptiv <= {WARMUP_MAX_S}s, Fenster {MEASURE_S}s")
    print("=" * 76)
    skip_hotter = None
    for i, (f, m) in enumerate(PLAN, 1):
        label = f"P{i}"
        if not in_bounds(f, m, bounds):
            log(f"--- {label}: {f}/{m} UEBERSPRUNGEN (ausserhalb Grenzen/Schrittweite) ---")
            RUN["points"].append({"label": label, "freq": f, "mv": m, "status": "skipped", "valid": False,
                                  "reason": "ausserhalb Grenzen/Schrittweite"})
            continue
        if skip_hotter and f >= skip_hotter[0] and m >= skip_hotter[1]:
            log(f"--- {label}: {f}/{m} UEBERSPRUNGEN (nach SOFT-Temp bei {skip_hotter[0]}/{skip_hotter[1]}) ---")
            RUN["points"].append({"label": label, "freq": f, "mv": m, "status": "skipped", "valid": False,
                                  "reason": "nach soft_temp uebersprungen"})
            continue
        r = measure_point(f, m, stock_f, stock_mv, label)
        RUN["points"].append(r)
        if r.get("status") == "soft_temp":
            skip_hotter = (f, m)
        save_run()

    print_table(RUN["points"])
    RUN["sweet_spots"] = sweet_spots(RUN["points"])
    RUN["status"] = "finished"


def restore():
    print()
    log("=== ABSCHLUSS ===")
    orig = STATE["original_job_interval"]
    if STATE["job_changed"] and orig is not None:
        try:
            set_job_interval(orig)
            STATE["job_changed"] = False
            log(f"Job-Intervall zurueck auf {orig} ms")
        except Exception as e:
            log(f"!!! Job-Intervall zuruecksetzen FEHLGESCHLAGEN: {e}")
    if STATE["wrote_tuning"]:
        try:
            ok, _ = set_point(SAFE_FREQ, SAFE_MV, STATE["stock_f"], STATE["stock_mv"])
            if STATE["emergency"]:
                log("Nach Notabschaltung: Mining bleibt AUS (Tuning auf SAFE gesetzt).")
            elif ok:
                log("Zurueckgesetzt auf sicheren Punkt.")
            else:
                log("!!! Ruecksetzen gesendet, aber Verify NICHT bestaetigt - bitte manuell pruefen.")
            RUN["restored"] = ok
        except Exception as e:
            RUN["restored"] = False
            log(f"!!! RUECKSETZEN FEHLGESCHLAGEN: {e}")
    log("Hinweis: save=false wurde nie ueberschrieben - ein Neustart des Miners stellt den gespeicherten Zustand her.")


def main():
    print("---- BEGINN BERICHT ----")
    RUN["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    log(f"sweep.py | {MINER_BASE} | SAFE {SAFE_FREQ}/{SAFE_MV} | REF {REF_FREQ}/{REF_MV} | "
        f"SOFT {SOFT_ASIC_C}/{SOFT_VR_C} HARD {HARD_ASIC_C}/{HARD_VR_C} | err<{ERROR_MAX_PCT}% "
        f"hashrate>={HASHRATE_MIN_FRAC * 100:.0f}% | Totmann {DEADMAN_MAX_CONSEC}x")
    code = 0
    try:
        run()
    except SystemExit as e:  # emergency_stop
        code = e.code if isinstance(e.code, int) else 1
    except Deadman as e:
        log(f"ABBRUCH: {e}")
        RUN["status"] = "deadman"
        code = 2
    except KeyboardInterrupt:
        log("Abgebrochen (Strg+C).")
        RUN["status"] = "interrupted"
        code = 130
    except Exception as e:
        log(f"FEHLER: {type(e).__name__}: {e}")
        RUN["status"] = f"error: {e}"
        code = 1
    finally:
        restore()
        RUN["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        try:
            save_run()
            log(f"Run-JSON gespeichert: {RUN_PATH}")
        except Exception as e:
            log(f"!!! Run-JSON konnte nicht gespeichert werden: {e}")
    print()
    print("---- ENDE BERICHT ----", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
