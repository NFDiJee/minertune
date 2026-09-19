#!/usr/bin/env python3
"""Tuner fuer den Miner - Stufe 1: nur lesend (ausschliesslich HTTP GET).

Schreibt keine Dateien, alle Ausgaben gehen ins Terminal.
"""

import argparse
import json
import statistics
import time
import urllib.error
import urllib.request

# ============================================================================
# KONFIG
# ============================================================================
MINER_BASE = "http://192.168.1.100/api/v1"
STATUS_URL = MINER_BASE + "/status"
HTTP_TIMEOUT = 10
TEMP_ABORT_ASIC_C = 75.0
TEMP_ABORT_VR_C   = 90.0
# ============================================================================

START_END_RETRIES = 3  # Versuche fuer Start-/Endmessung, bevor abgebrochen wird


class StatusError(RuntimeError):
    """GET /status fehlgeschlagen: code 'get_failed' + params (url, why) fuer die Uebersetzung in control.py."""

    def __init__(self, url, why):
        self.code, self.params = "get_failed", {"url": url, "why": why}
        super().__init__(f"GET {url} failed: {why}")


def get_status() -> dict:
    """GET auf STATUS_URL und JSON zurueckgeben. Wirft RuntimeError bei jedem Fehler."""
    try:
        req = urllib.request.Request(STATUS_URL, method="GET",
                                     headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT) as resp:
            body = resp.read()
    except urllib.error.HTTPError as e:
        raise StatusError(STATUS_URL, f"HTTP {e.code} {e.reason}") from e
    except Exception as e:
        raise StatusError(STATUS_URL, f"{type(e).__name__}: {e}") from e
    try:
        data = json.loads(body.decode("utf-8"))
    except Exception as e:
        raise StatusError(STATUS_URL, f"response is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise RuntimeError(f"Antwort von {STATUS_URL} ist kein JSON-Objekt")
    return data


def _get_status_retry(what):
    """Status mit einigen Wiederholungen holen (fuer Start-/Endmessung)."""
    last = None
    for attempt in range(1, START_END_RETRIES + 1):
        try:
            return get_status()
        except RuntimeError as e:
            last = e
            print(f"  [{what}] Versuch {attempt}/{START_END_RETRIES} fehlgeschlagen: {e}", flush=True)
            if attempt < START_END_RETRIES:
                time.sleep(2)
    raise RuntimeError(f"{what}: kein Status nach {START_END_RETRIES} Versuchen ({last})")


def _overtemp(status):
    """Liefert Grund-String bei Uebertemperatur, sonst None."""
    asic = status.get("asic_temp_c")
    vr = status.get("vr_temp_c")
    if isinstance(asic, (int, float)) and asic > TEMP_ABORT_ASIC_C:
        return f"asic_temp_c {asic:.1f} > {TEMP_ABORT_ASIC_C}"
    if isinstance(vr, (int, float)) and vr > TEMP_ABORT_VR_C:
        return f"vr_temp_c {vr:.1f} > {TEMP_ABORT_VR_C}"
    return None


def _num(v, nd=1):
    return f"{v:.{nd}f}" if isinstance(v, (int, float)) else "n/a"


def _mean(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return statistics.mean(vals) if vals else None


def _max(values):
    vals = [v for v in values if isinstance(v, (int, float))]
    return max(vals) if vals else None


def _sample_loop(duration_s, sample_s, t_start, label, on_sample):
    """Alle sample_s einen Status holen, bis duration_s ab t_start verstrichen ist.

    on_sample(status, elapsed) wird pro erfolgreichem Sample aufgerufen.
    Liefert (http_errors, overtemp_reason).
    """
    errors = 0
    n = 0
    while True:
        n += 1
        next_t = t_start + n * sample_s
        end_t = t_start + duration_s
        if next_t > end_t:
            # letzter Rest bis zum Fensterende ohne weiteres Sample
            rest = end_t - time.monotonic()
            if rest > 0:
                time.sleep(rest)
            return errors, None
        wait = next_t - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        elapsed = time.monotonic() - t_start
        try:
            st = get_status()
        except RuntimeError as e:
            errors += 1
            print(f"  [{label} {elapsed:6.1f}s] HTTP-Fehler #{errors}: {e}", flush=True)
            continue
        on_sample(st, elapsed)
        reason = _overtemp(st)
        if reason:
            print(f"  [{label} {elapsed:6.1f}s] UEBERTEMPERATUR: {reason} -> Abbruch", flush=True)
            return errors, reason


def measure_window(warmup_s, window_s, sample_s) -> dict:
    result = {"aborted_overtemp": False, "abort_reason": None,
              "counter_reset": False, "http_errors": 0}

    # --- Betriebspunkt ---
    st = _get_status_retry("Betriebspunkt")
    op_fields = ["current_frequency_mhz", "core_mv", "measured_core_mv",
                 "stock_frequency_mhz", "stock_core_mv", "local_hashrate_difficulty"]
    print("Aktueller Betriebspunkt:")
    for f in op_fields:
        result[f] = st.get(f)
        print(f"  {f:<28} {st.get(f)}")
    print(flush=True)

    reason = _overtemp(st)
    if reason:
        print(f"UEBERTEMPERATUR bereits beim Start: {reason} -> Abbruch")
        result.update(aborted_overtemp=True, abort_reason=reason)
        return result

    # --- Aufwaermphase ---
    print(f"Aufwaermphase: {warmup_s}s, Sample alle {sample_s}s", flush=True)

    def warm_sample(s, elapsed):
        print(f"  [warmup {elapsed:6.1f}s] asic_temp_c={_num(s.get('asic_temp_c'))}  "
              f"vr_temp_c={_num(s.get('vr_temp_c'))}", flush=True)

    errs, reason = _sample_loop(warmup_s, sample_s, time.monotonic(), "warmup", warm_sample)
    result["http_errors"] += errs
    if reason:
        result.update(aborted_overtemp=True, abort_reason=reason)
        return result
    print(flush=True)

    # --- Startmessung ---
    t0 = time.monotonic()
    s0 = _get_status_retry("Startmessung")
    valid0 = s0.get("local_valid_candidates")
    invalid0 = s0.get("local_invalid_candidates")
    accepted0 = s0.get("accepted_shares")
    rejected0 = s0.get("rejected_shares")
    diff = st.get("local_hashrate_difficulty")
    print(f"Startmessung: valid0={valid0} invalid0={invalid0} "
          f"accepted0={accepted0} rejected0={rejected0}", flush=True)
    print()

    # --- Messfenster ---
    series = {k: [] for k in SERIES_FIELDS}
    print(f"Messfenster: {window_s}s, Sample alle {sample_s}s", flush=True)

    def win_sample(s, elapsed):
        for k in series:
            series[k].append(s.get(k))
        print(f"  [window {elapsed:6.1f}s] local={_num(s.get('local_hashrate_ghs'))} GH/s  "
              f"pool={_num(s.get('accepted_pool_hashrate_ghs'))} GH/s  "
              f"wall={_num(s.get('wall_power_w'))} W  rail={_num(s.get('rail_power_w'))} W  "
              f"asic={_num(s.get('asic_temp_c'))} C  vr={_num(s.get('vr_temp_c'))} C  "
              f"valid={s.get('local_valid_candidates')} invalid={s.get('local_invalid_candidates')}",
              flush=True)

    errs, reason = _sample_loop(window_s, sample_s, t0, "window", win_sample)
    result["http_errors"] += errs
    if reason:
        result.update(aborted_overtemp=True, abort_reason=reason)

    # --- Endmessung ---
    t1 = time.monotonic()
    s1 = _get_status_retry("Endmessung")
    valid1 = s1.get("local_valid_candidates")
    invalid1 = s1.get("local_invalid_candidates")
    accepted1 = s1.get("accepted_shares")
    rejected1 = s1.get("rejected_shares")
    print(f"\nEndmessung:   valid1={valid1} invalid1={invalid1} "
          f"accepted1={accepted1} rejected1={rejected1}", flush=True)

    # --- Berechnung ---
    counters = dict(valid0=valid0, valid1=valid1, invalid0=invalid0, invalid1=invalid1,
                    accepted0=accepted0, accepted1=accepted1,
                    rejected0=rejected0, rejected1=rejected1)
    metrics = compute_metrics(series, counters, t1 - t0, diff)
    result["counter_reset"] = metrics.pop("counter_reset")
    result.update(metrics)
    return result


SERIES_FIELDS = ["local_hashrate_ghs", "accepted_pool_hashrate_ghs",
                 "wall_power_w", "rail_power_w", "asic_temp_c", "vr_temp_c"]


def compute_metrics(samples, counters, dur, difficulty) -> dict:
    """Kennzahlen eines Messfensters berechnen.

    samples:  dict Feldname -> Liste der Sample-Werte (SERIES_FIELDS)
    counters: dict mit valid0/valid1/invalid0/invalid1/accepted0/accepted1/rejected0/rejected1
    dur:      Fensterdauer in s (t1 - t0)
    difficulty: local_hashrate_difficulty aus dem Startstatus
    """
    c = counters
    counter_reset = False
    dvalid = c["valid1"] - c["valid0"]
    dinvalid = c["invalid1"] - c["invalid0"]
    if dvalid < 0 or dinvalid < 0:
        counter_reset = True
        if dvalid < 0:
            dvalid = c["valid1"]
        if dinvalid < 0:
            dinvalid = c["invalid1"]
    total = dvalid + dinvalid
    error_pct = 100.0 * dinvalid / total if total > 0 else 0.0
    daccepted = c["accepted1"] - c["accepted0"]
    drejected = c["rejected1"] - c["rejected0"]

    local_mean = _mean(samples.get("local_hashrate_ghs", []))
    hashrate_local_ths = local_mean / 1000 if local_mean is not None else 0.0
    pool_vals = [v for v in samples.get("accepted_pool_hashrate_ghs", [])
                 if isinstance(v, (int, float))]
    hashrate_pool_end_ths = pool_vals[-1] / 1000 if pool_vals else None
    hashrate_valid_ths = (dvalid * difficulty * (2 ** 32) / dur / 1e12
                          if isinstance(difficulty, (int, float)) and dur > 0 else 0.0)
    wall_avg = _mean(samples.get("wall_power_w", []))
    rail_avg = _mean(samples.get("rail_power_w", []))
    jth_local = wall_avg / hashrate_local_ths if wall_avg is not None and hashrate_local_ths > 0 else None
    jth_valid = wall_avg / hashrate_valid_ths if wall_avg is not None and hashrate_valid_ths > 0 else None

    return dict(
        dur=dur, samples=len(samples.get("asic_temp_c", [])),
        valid0=c["valid0"], valid1=c["valid1"], invalid0=c["invalid0"], invalid1=c["invalid1"],
        accepted0=c["accepted0"], accepted1=c["accepted1"],
        rejected0=c["rejected0"], rejected1=c["rejected1"],
        dvalid=dvalid, dinvalid=dinvalid, error_pct=error_pct,
        daccepted=daccepted, drejected=drejected,
        counter_reset=counter_reset,
        diff=difficulty,
        hashrate_local_ths=hashrate_local_ths,
        hashrate_valid_ths=hashrate_valid_ths,
        hashrate_pool_end_ths=hashrate_pool_end_ths,
        wall_avg=wall_avg, rail_avg=rail_avg,
        jth_local=jth_local, jth_valid=jth_valid,
        asic_temp_max=_max(samples.get("asic_temp_c", [])),
        vr_temp_max=_max(samples.get("vr_temp_c", [])),
    )


def print_result(r):
    def row(label, value, unit=""):
        print(f"  {label:<34} {value}{(' ' + unit) if unit else ''}")

    print()
    print("=" * 60)
    print("ERGEBNIS")
    print("=" * 60)
    print("Status")
    row("aborted_overtemp", r.get("aborted_overtemp"))
    if r.get("abort_reason"):
        row("abort_reason", r["abort_reason"])
    row("counter_reset", r.get("counter_reset"))
    row("http_errors", r.get("http_errors"))
    if "dur" not in r:
        print("  (Abbruch vor dem Messfenster - keine Kennzahlen)")
        print("=" * 60)
        return
    row("dur", _num(r["dur"]), "s")
    row("samples", r["samples"])
    print("Betriebspunkt")
    row("current_frequency_mhz", r.get("current_frequency_mhz"), "MHz")
    row("core_mv (Soll)", r.get("core_mv"), "mV")
    row("measured_core_mv", r.get("measured_core_mv"), "mV")
    row("stock_frequency_mhz", r.get("stock_frequency_mhz"), "MHz")
    row("stock_core_mv", r.get("stock_core_mv"), "mV")
    row("local_hashrate_difficulty", r.get("diff"))
    print("Fehlerrate")
    row("error_pct", _num(r["error_pct"], 3), "%")
    print("Hashrate")
    row("hashrate_local_ths (Mittel)", _num(r["hashrate_local_ths"], 3), "TH/s")
    row("hashrate_valid_ths (aus Countern)", _num(r["hashrate_valid_ths"], 3), "TH/s")
    row("hashrate_pool_end_ths", _num(r["hashrate_pool_end_ths"], 3), "TH/s")
    print("Effizienz")
    row("jth_local", _num(r["jth_local"], 2), "J/TH")
    row("jth_valid", _num(r["jth_valid"], 2), "J/TH")
    print("Leistung")
    row("wall_avg", _num(r["wall_avg"], 2), "W")
    row("rail_avg", _num(r["rail_avg"], 2), "W")
    print("Temperaturen")
    row("asic_temp_max", _num(r["asic_temp_max"]), "C")
    row("vr_temp_max", _num(r["vr_temp_max"]), "C")
    print("Counter (roh)")
    row("valid0 -> valid1", f"{r['valid0']} -> {r['valid1']}  (delta {r['dvalid']})")
    row("invalid0 -> invalid1", f"{r['invalid0']} -> {r['invalid1']}  (delta {r['dinvalid']})")
    row("accepted0 -> accepted1", f"{r['accepted0']} -> {r['accepted1']}  (delta {r['daccepted']})")
    row("rejected0 -> rejected1", f"{r['rejected0']} -> {r['rejected1']}  (delta {r['drejected']})")
    print("=" * 60)


def cmd_measure(args):
    print("---- BEGINN BERICHT ----")
    print(f"Zeit: {time.strftime('%Y-%m-%d %H:%M:%S')}  |  {STATUS_URL}")
    print(f"warmup={args.warmup}s  window={args.window}s  sample={args.sample}s  "
          f"Abbruch bei ASIC>{TEMP_ABORT_ASIC_C} C / VR>{TEMP_ABORT_VR_C} C")
    print()
    try:
        result = measure_window(args.warmup, args.window, args.sample)
        print_result(result)
    except RuntimeError as e:
        print(f"\nFEHLER: {e}")
    except KeyboardInterrupt:
        print("\nAbgebrochen.")
    print()
    print("---- ENDE BERICHT ----")


def main():
    p = argparse.ArgumentParser(description="Miner-Tuner (Stufe 1: nur lesend)")
    sub = p.add_subparsers(dest="cmd", required=True)
    m = sub.add_parser("measure", help="Messfenster aufnehmen und Kennzahlen berechnen")
    m.add_argument("--warmup", type=float, default=15, help="Aufwaermzeit in s (default 15)")
    m.add_argument("--window", type=float, default=180, help="Messfenster in s (default 180)")
    m.add_argument("--sample", type=float, default=10, help="Sample-Intervall in s (default 10)")
    m.set_defaults(func=cmd_measure)
    args = p.parse_args()
    if args.sample <= 0:
        p.error("--sample muss > 0 sein")
    args.func(args)


if __name__ == "__main__":
    main()
