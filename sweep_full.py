#!/usr/bin/env python3
"""Voll-Sweep mit adaptiver Vmin-Suche je Frequenz - modellunabhaengig.

Alle Grenzen werden aus dem Profil (GET /status) abgeleitet; die KONFIG enthaelt nur
relative/dimensionslose Faktoren, Zeiten und physikalische Temperaturgrenzen.

  --plan-only (Standard): Profil lesen, Parameter ableiten, Matrix anzeigen. NUR GET.
  --run                  : echter Sweep (Kalibrierung, Vmin-Suche, drei Sweet Spots, Run-JSON).

Messformeln: tuner.compute_metrics. HTTP-/Totmann-Helfer: sweep.py (verifiziert).
Alle Schreibzugriffe mit save=false; am Ende IMMER (try/finally) Job-Intervall und
Ausgangspunkt wiederhergestellt.
"""

import argparse
import collections
import signal
import glob
import json
import math
import os
import statistics
import sys
import threading
import time

import tuner
import sweep as base  # post_json, get_json, safe_get, danger_for, Deadman, log, _n

# ============================================================================
# KONFIG - nur relative Faktoren (dimensionslos, modellunabhaengig)
# ============================================================================
MINER_BASE = "http://192.168.1.100/api/v1"
STATUS = MINER_BASE + "/status" ; TUNING = MINER_BASE + "/tuning" ; MINING = MINER_BASE + "/mining" ; JOBSCHED = MINER_BASE + "/job-schedule"

FREQ_LOW_PCT   = 0.52    # Untergrenze = stock_freq * (1-0.52)
FREQ_HIGH_PCT  = 0.00    # Obergrenze ueber Stock; nur mit ALLOW_ABOVE_STOCK
ALLOW_ABOVE_STOCK = False
FREQ_STEP_FACTOR = 25    # Frequenzschritt = FREQ_STEP_FACTOR * frequency_step (Profil), min. 1x
MV_STEP_FACTOR   = 5     # Spannungsschritt = MV_STEP_FACTOR * core_step (Profil)
FLOOR_PCT   = 0.75       # Spannungsboden der Vmin-Suche = stock_core_mv * 0.75
START_PCT   = 1.00       # Startspannung je Frequenz = stock_core_mv * 1.00 (dann heruntertasten)
MIN_MV_SPAN_PCT = 0.05   # Spannungs-Suchspanne (core_min .. mv_start) mind. 5 % von stock_core_mv, sonst range_too_narrow
START_MARGIN_STEPS = 2   # ab 2. Frequenz: Start = Vmin(hoehere Frequenz) + 2 Spannungsschritte (<= mv_start)
WINDOW_MIN_S = 600 ; TARGET_HITS = 600 ; WINDOW_MAX_S = 900
ERROR_MAX_PCT = 2.0 ; HASHRATE_MIN_FRAC = 0.90
SOFT_ASIC_C = 70 ; SOFT_VR_C = 85 ; HARD_ASIC_C = 80 ; HARD_VR_C = 95
INPUT_V_MIN_FRAC = 0.95  # Input-Voltage-Waechter = gemessene Start-input_voltage_v * 0.95
EARLY_ABORT = True       # unterversorgten Punkt nach Einschwingen sofort verwerfen
VERIFY_TOL_FRAC = 0.012  # Verify: |measured - soll| <= max(2 * core_step, soll * 0.012)
DANGER_PCT = 0.15        # Firmware-Regel: >15 % Abstand zu Stock -> danger_acknowledged
JOBCAL_ALT_FACTOR = 0.5  # Job-Intervall-Test: Alternative = Original * 0.5
JOBCAL_GAIN_FRAC = 0.03
KNEE_FACTOR = 1.5 ; COMPROMISE_FRAC = 1.05
REALISTIC_STEPS = 4      # realistische Stufenzahl je Frequenz (nur Schaetzung)
# Zeiten
SAMPLE_S = 5 ; SETTLE_WINDOW_S = 30 ; SETTLE_TOL_FRAC = 0.03 ; WARMUP_MAX_S = 90
# Dreistufige Punkt-Bewertung
SETTLE_PRECHECK_S = 25   # Stufe 1: Schnell-Vorabcheck nach 25 s
PRECHECK_AVG_S = 10      # ... Mittel der letzten ~10 s
PRECHECK_FRAC = 0.75     # ... >= 75 % der Erwartung (bewusst grosszuegiger als HASHRATE_MIN_FRAC 0.90)
INWIN_CHECK_S = 60       # Stufe 2 im Fenster: ab 60 s gleitendes 60-s-Mittel pruefen
INWIN_MIN_HITS = 30      # ... Fehlerrate erst ab 30 Treffern bewerten
RESERVE_STEPS = 1        # efficiency/target: Reservestufen ueber dem ersten voll versorgten Punkt
KNEE_MIN_FRAC = 0.97     # Knie-Kurve: je Frequenz niedrigster gueltiger Punkt mit >= 97 % der Erwartung
FULL_MEASURE = False     # True: jeder getestete Punkt bekommt das volle Fenster (kein 30-s-Vorabcheck-Skip);
                         #       Stufe-2-Fruehabbruch und alle Sicherungen bleiben aktiv
CAL_WINDOW_S = 120 ; WARMUP_EST_S = 45 ; OVERHEAD_EST_S = 15
MIN_SAMPLE_FRAC = 0.8
# ============================================================================

assert tuner.STATUS_URL == STATUS
assert (base.TUNING, base.MINING, base.JOBSCHED) == (TUNING, MINING, JOBSCHED)

_n = base._n
HERE = os.path.dirname(os.path.abspath(__file__))
LIVE_PATH = os.path.join(HERE, "runs", "live_state.json")
WINDOW_LOG_S = 30        # Live-Zeile im Messfenster alle 30 s
_LOGBUF = collections.deque(maxlen=50)
LIVE = {"miner_url": MINER_BASE, "dry_run": False, "badge": "LIVE SWEEP - sweep_full.py writes (save=false)",
        "best_key": "jth_local", "status": "starting", "stock": {}, "bounds": {}, "config": {}, "plan": [],
        "current": {"idx": None, "frequency_mhz": None, "core_mv": None, "phase": "idle", "elapsed_s": 0,
                    "remaining_s": 0, "http_errors": 0, "live": {}, "avg": {}},
        "results": [], "log": []}


def log(msg=""):
    base.log(msg)
    if msg:
        _LOGBUF.append(f"{time.strftime('%H:%M:%S')}  {msg}")


def publish_live(**current):
    """Zustand fuer das Dashboard (--follow runs/live_state.json) atomar schreiben."""
    try:
        LIVE["current"].update(current)
        LIVE["log"] = list(_LOGBUF)
        LIVE["status"] = RUN.get("status")
        LIVE["generated_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        os.makedirs(os.path.dirname(LIVE_PATH), exist_ok=True)
        tmp = LIVE_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(LIVE, f, ensure_ascii=False, default=str)
        os.replace(tmp, LIVE_PATH)
    except Exception as e:  # Dashboard darf den Sweep nie stoppen
        base.log(f"(live_state not written: {e})")


def live_from(st):
    ths = lambda x: x / 1000 if isinstance(x, (int, float)) else None
    return {"local_ths": ths(st.get("local_hashrate_ghs")), "pool_ths": ths(st.get("accepted_pool_hashrate_ghs")),
            "wall_w": effective_wall_power(st)[0], "wall_src": effective_wall_power(st)[1],
            "rail_w": st.get("rail_power_w"), "asic_c": st.get("asic_temp_c"),
            "vr_c": st.get("vr_temp_c"), "valid": st.get("local_valid_candidates"),
            "invalid": st.get("local_invalid_candidates"), "vin": st.get("input_voltage_v"),
            "iin": st.get("input_current_a")}


def live_line(label, phase, t, st, extra=""):
    log(f"    [{label} {phase} {t:4.0f}s] local={_n(st.get('local_hashrate_ghs'))} GH/s  "
        f"wall={_n(effective_wall_power(st)[0])} W ({effective_wall_power(st)[1]})  vin={_n(st.get('input_voltage_v'), 2)} V  "
        f"iin={_n(st.get('input_current_a'), 2)} A  asic={_n(st.get('asic_temp_c'))} vr={_n(st.get('vr_temp_c'))} C  "
        f"mv={st.get('measured_core_mv')}{extra}")


def add_result(r):
    """Punkt-Ergebnis ins Dashboard-Format uebernehmen."""
    idx = len(LIVE["results"])
    LIVE["plan"].append({"frequency_mhz": r["freq"], "core_mv": r["mv"]})
    LIVE["results"].append(dict(r, idx=idx, frequency_mhz=r["freq"], core_mv=r["mv"],
                                measured_core_mv=r.get("measured_mv")))
    publish_live(idx=None, phase="done")
GH_PER_MHZ = None
D = {}   # abgeleitete Parameter
RUN = {"status": "starting", "points": [], "calibration": {}, "vmin": {}}
RUN_PATH = os.path.join(HERE, "runs", time.strftime("full_%Y%m%d_%H%M%S.json"))
STATE = {"wrote_tuning": False, "job_changed": False, "emergency": False}


# ----------------------------------------------------------------------------
# Sprachneutrale Meldungen: {"code": ..., "params": {...}}. control.py uebersetzt sie ueber
# lang/<sprache>.json (Schluessel "code.<code>"); MSG_EN ist nur der englische Klartext fuer Log/CLI/Export.
# ----------------------------------------------------------------------------
MSG_EN = {
    # Punkt-Bewertung
    "precheck_underpowered": "underpowered (precheck): {pct}% < {thresh}% of expected after {secs}s",
    "precheck_ok": "precheck ok {pct}%",
    "early_abort_warmup": "early abort: underpowered {pct}% of expected after warmup (settled {settle_s}s)",
    "early_abort_warmup_unsettled": "early abort: underpowered {pct}% of expected after warmup (not settled)",
    "early_abort_window_hashrate": "early abort in window: {pct}% of expected (60 s average) after {secs}s",
    "early_abort_window_error": "early abort in window: error {err}% after {secs}s",
    "underpowered": "underpowered: {pct}% of expected",
    "unstable": "unstable: error {err}%",
    "too_few_samples": "too few samples ({n} < {min})",
    "not_settled": "not settled",
    "hits_capped": "hits {hits} < {target} (cap)",
    "ok": "ok",
    "verify_failed": "verify failed",
    "soft_temp": "soft temperature limit: {sensor} {value} > {limit} C",
    "input_voltage_low": "input voltage {value} V < {limit} V",
    "hard_temp": "hard temperature limit: {sensor} {value} > {limit} C",
    # Plan
    "band_efficiency": "f_low {lo} .. band middle {hi} (stock * {mid_frac})",
    "band_performance": "band middle {lo} (stock * {mid_frac}) .. f_high {hi}",
    "band_full": "f_low {lo} .. f_high {hi}",
    "band_target": "f_target {f_target} * {lo_frac} .. * {hi_frac}, clamped [{min}..{max}]",
    "gate_above_stock": "Leaves the factory range (above stock). Confirmation required at start.",
    "early_stop_rule": "Test ascending; stop as soon as the best J/TH per frequency rises {rises}x in a row (valley captured)",
    "early_stop_after": "after {freq} MHz",
    "note_freq_capped": "frequency upper limit {raw} MHz capped at wall frequency_max {max}",
    "note_mv_capped": "voltage upper limit {raw} mV capped at wall core_max {max}",
    "note_target_needs_above": "target {target} TH/s needs ~{f} MHz > f_high {f_high} - only reachable with allow_above_stock",
    "note_target_over_wall": "target {target} TH/s needs ~{f} MHz > f_high {f_high} - beyond the wall",
    "est_per_point": "warmup ~{warmup}s + window (min {min}s, until {hits} hits, max {max}s) + {overhead}s",
    "est_fast_perf": "full window per frequency: {n} (Vmin); precheck ~{secs}s",
    "est_fast_reserve": "full window per frequency: {n} (Vmin + reserve); precheck ~{secs}s",
    "derive_start_below_floor": "mv_start {start} < mv_floor {floor} (START_PCT < FLOOR_PCT?)",
    "derive_no_freqs": "no frequencies in band",
    "range_too_narrow": "Voltage range too narrow for a sweep (core_min {min} .. core_max {max}, stock {stock} mV) - only manual point setting is useful here.",
    # Fehler / Abbrueche
    "unknown_mode": "sweep_mode {value} unknown ({allowed})",
    "unknown_resolution": "resolution {value} unknown ({allowed})",
    "target_required": "target_hashrate requires target_ths > 0",
    "no_estimate": "no hashrate estimate possible (local_hashrate_ghs/current_frequency_mhz missing)",
    "wall_frequency": "frequency {value} violates profile [{min}..{max}] step {step}",
    "wall_voltage": "voltage {value} violates profile [{min}..{max}] step {step}",
    # Sweet Spots
    "knee_highest_vmin": "highest Vmin point",
    "knee_segment_exceeds": "segment to {freq}/{mv} > {factor} x reference -> knee before it",
    "knee_all_segments_ok": "all segments <= {factor} x reference -> highest Vmin point",
    # Kalibrierung
    "job_switch": "SWITCH to {ms} ms ({gain_pct:+.1f} %)",
    "job_keep": "KEEP {ms} ms ({gain_pct:+.1f} % < {thresh} %)",
}


def msg(code, **params):
    """Sprachneutrale Meldung."""
    return {"code": code, "params": params}


def msg_text(m):
    """Englischer Klartext einer Meldung (auch Listen); alte Freitexte (Altlaeufe) unveraendert."""
    if m is None:
        return ""
    if isinstance(m, list):
        return "; ".join(msg_text(x) for x in m)
    if isinstance(m, dict) and "code" in m:
        tpl = MSG_EN.get(m["code"])
        try:
            return tpl.format(**(m.get("params") or {})) if tpl else m["code"]
        except (KeyError, ValueError, IndexError):
            return m["code"]
    return str(m)


class CodedError(ValueError):
    """Fehler mit Code + Parametern (control.py uebersetzt); str() = englischer Klartext."""

    def __init__(self, code, **params):
        self.code, self.params = code, params
        super().__init__(msg_text(msg(code, **params)))


class PointAbort(Exception):
    """Punkt abbrechen (Soft-Temp / Eingangsspannung) -> Ausgangspunkt, Suche fuer diese Frequenz beenden."""

    def __init__(self, code, **params):
        self.code, self.params = code, params
        super().__init__(msg_text(msg(code, **params)))


class WallViolation(RuntimeError):
    def __init__(self, code, **params):
        self.code, self.params = code, params
        super().__init__(msg_text(msg(code, **params)))


class Stopped(Exception):
    """Sweep von aussen gestoppt (Web-Steuerung / Signal) -> Ruecksetzen im finally."""


STOP = threading.Event()


def _sleep(s):
    """Unterbrechbares Warten in den Messschleifen."""
    if STOP.wait(max(0.0, s)):
        raise Stopped("sweep stopped")


def effective_wall_power(st):
    """-> (watt, quelle): 'measured' nur wenn der Miner wall_power_measured==True meldet, sonst 'estimated'."""
    return st.get("wall_power_w"), ("measured" if st.get("wall_power_measured") is True else "estimated")


CONFIG_KEYS = {  # config.json-Schluessel -> Modulkonstante
    "window_min_s": "WINDOW_MIN_S", "target_hits": "TARGET_HITS", "window_max_s": "WINDOW_MAX_S",
    "freq_low_pct": "FREQ_LOW_PCT", "freq_high_pct": "FREQ_HIGH_PCT", "allow_above_stock": "ALLOW_ABOVE_STOCK",
    "freq_step_factor": "FREQ_STEP_FACTOR", "mv_step_factor": "MV_STEP_FACTOR", "floor_pct": "FLOOR_PCT",
    "error_max_pct": "ERROR_MAX_PCT", "hashrate_min_frac": "HASHRATE_MIN_FRAC", "soft_asic_c": "SOFT_ASIC_C",
    "soft_vr_c": "SOFT_VR_C", "hard_asic_c": "HARD_ASIC_C", "hard_vr_c": "HARD_VR_C",
    "input_v_min_frac": "INPUT_V_MIN_FRAC",
}


def configure(miner_url=None, cfg=None):
    """Miner-URL und relative Parameter setzen (fuer control.py). Wirkt auf sweep_full, sweep und tuner."""
    g = globals()
    if miner_url:
        base_url = miner_url.rstrip("/")
        g.update(MINER_BASE=base_url, STATUS=base_url + "/status", TUNING=base_url + "/tuning",
                 MINING=base_url + "/mining", JOBSCHED=base_url + "/job-schedule")
        tuner.STATUS_URL = g["STATUS"]
        base.STATUS, base.TUNING, base.MINING, base.JOBSCHED = g["STATUS"], g["TUNING"], g["MINING"], g["JOBSCHED"]
        LIVE["miner_url"] = base_url
    for k, v in (cfg or {}).items():
        if k in CONFIG_KEYS:
            g[CONFIG_KEYS[k]] = v


def reset_run():
    """Frischen Laufzustand anlegen (vor jedem Sweep aus der Web-Steuerung)."""
    global GH_PER_MHZ, RUN_PATH
    GH_PER_MHZ = None
    STOP.clear()
    _LOGBUF.clear()
    D.clear()
    RUN.clear(); RUN.update({"status": "starting", "points": [], "calibration": {}, "vmin": {}})
    STATE.clear(); STATE.update({"wrote_tuning": False, "job_changed": False, "emergency": False})
    LIVE.update(status="starting", plan=[], results=[], log=[],
                current={"idx": None, "frequency_mhz": None, "core_mv": None, "phase": "idle", "elapsed_s": 0,
                         "remaining_s": 0, "http_errors": 0, "live": {}, "avg": {}})
    RUN_PATH = os.path.join(HERE, "runs", time.strftime("full_%Y%m%d_%H%M%S.json"))


# ----------------------------------------------------------------------------
# Ableitung aus dem Profil
# ----------------------------------------------------------------------------
def round_to(v, step):
    return int(round(v / step) * step)


def clamp(v, lo, hi):
    return max(lo, min(hi, v))


def snap(v, step, lo, hi):
    """Auf step runden und HART in [lo..hi] klemmen; das Ergebnis liegt immer auf dem Schrittraster."""
    lo_a, hi_a = int(-(-lo // step) * step), int(hi // step * step)
    return clamp(round_to(v, step), lo_a, hi_a)


def mv_range(smv, floor_pct, cs, cmin, cmax, step):
    """Spannungs-Suchspanne -> (mv_start, mv_floor, fehler oder None). Beide Werte auf core_step und in
    [core_min..core_max]. Schmaler Bereich (Spanne < step): Boden auf den Standard-FLOOR_PCT zurueck, der
    ebenfalls an core_min geklemmt wird (bei Minern wie dem Thor P2 = core_min). Bleibt insgesamt
    weniger als max(step, MIN_MV_SPAN_PCT * stock) zwischen core_min und mv_start -> range_too_narrow."""
    start = snap(smv * START_PCT, cs, cmin, cmax)
    floor = snap(smv * floor_pct, cs, cmin, cmax)
    if start - floor < step:
        floor = min(floor, snap(smv * FLOOR_PCT, cs, cmin, cmax))
    avail = start - snap(cmin, cs, cmin, cmax)
    err = None
    if avail < max(step, MIN_MV_SPAN_PCT * smv) or start - floor < step:
        err = msg("range_too_narrow", min=cmin, max=cmax, stock=smv)
    return start, floor, err


def derive(p):
    """Alle Grenzen aus dem Profil; liefert dict mit Werten und Formeltexten."""
    fs, cs = p["frequency_step"], p["core_step"]
    sf, smv = p["stock_frequency_mhz"], p["stock_core_mv"]
    fmin, fmax, cmin, cmax = p["frequency_min"], p["frequency_max"], p["core_min"], p["core_max"]
    d = {"freq_step": fs, "mv_step": cs}
    d["freq_sweep_step"] = max(fs, FREQ_STEP_FACTOR * fs)
    d["mv_sweep_step"] = max(cs, MV_STEP_FACTOR * cs)
    d["f_low"] = snap(sf * (1 - FREQ_LOW_PCT), fs, fmin, fmax)
    if ALLOW_ABOVE_STOCK:
        d["f_high"] = snap(sf * (1 + FREQ_HIGH_PCT), fs, fmin, fmax)
    else:
        d["f_high"] = snap(sf, fs, fmin, fmax)
    freqs, f = [], d["f_low"]
    while f <= d["f_high"]:
        freqs.append(snap(f, fs, fmin, fmax))
        f += d["freq_sweep_step"]
    if freqs and freqs[-1] != d["f_high"]:
        freqs.append(d["f_high"])
    d["freqs"] = sorted(set(freqs))
    d["mv_start"], d["mv_floor"], mv_err = mv_range(smv, FLOOR_PCT, cs, cmin, cmax, d["mv_sweep_step"])
    vin = p["input_voltage_v"]
    d["input_v_min"] = vin * INPUT_V_MIN_FRAC if isinstance(vin, (int, float)) else None
    d["safe"] = (p["current_frequency_mhz"], p["core_mv"])
    d["job_alt_ms"] = (max(1, round(p["job_interval_ms"] * JOBCAL_ALT_FACTOR))
                       if isinstance(p["job_interval_ms"], (int, float)) else None)
    d["mv_steps"] = list(range(d["mv_start"], d["mv_floor"] - 1, -d["mv_sweep_step"])) \
        if d["mv_start"] >= d["mv_floor"] else []
    d["errors"] = [mv_err] if mv_err else []
    if d["mv_start"] < d["mv_floor"]:
        d["errors"].append(msg("derive_start_below_floor", start=d["mv_start"], floor=d["mv_floor"]))
    if not d["freqs"]:
        d["errors"].append(msg("derive_no_freqs"))
    d["formula"] = {
        "freq_sweep_step": f"max(frequency_step, {FREQ_STEP_FACTOR} * frequency_step) = max({fs}, {FREQ_STEP_FACTOR * fs})",
        "mv_sweep_step": f"max(core_step, {MV_STEP_FACTOR} * core_step) = max({cs}, {MV_STEP_FACTOR * cs})",
        "f_low": f"clamp(round_to({sf} * (1 - {FREQ_LOW_PCT}) = {sf * (1 - FREQ_LOW_PCT):g}, {fs}), {fmin}, {fmax}) = {d['f_low']}",
        "f_high": (f"clamp(round_to({sf} * (1 + {FREQ_HIGH_PCT}), {fs}), {fmin}, {fmax}) = {d['f_high']}" if ALLOW_ABOVE_STOCK
                   else f"clamp(stock_frequency_mhz {sf}, {fmin}, {fmax}) = {d['f_high']} (ALLOW_ABOVE_STOCK=False)"),
        "mv_start": f"clamp(round_to({smv} * {START_PCT} = {smv * START_PCT:g}, {cs}), {cmin}, {cmax}) = {d['mv_start']}",
        "mv_floor": f"clamp(round_to({smv} * {FLOOR_PCT} = {smv * FLOOR_PCT:g}, {cs}), {cmin}, {cmax}) = {d['mv_floor']}",
        "input_v_min": f"start input_voltage_v {_n(vin, 2)} * {INPUT_V_MIN_FRAC}",
        "safe": "operating point at start (current_frequency_mhz / core_mv)",
        "job_alt_ms": f"job_interval_ms {p['job_interval_ms']} * {JOBCAL_ALT_FACTOR}",
        "verify_tol": f"max(2 * core_step = {2 * cs}, target * {VERIFY_TOL_FRAC})",
    }
    return d


def check_wall(freq, mv, p):
    """HARTE Mauer: nichts ausserhalb der Profilgrenzen oder abseits der Schrittweiten setzen."""
    if not (p["frequency_min"] <= freq <= p["frequency_max"]) or freq % p["frequency_step"]:
        raise WallViolation("wall_frequency", value=freq, min=p["frequency_min"], max=p["frequency_max"], step=p["frequency_step"])
    if not (p["core_min"] <= mv <= p["core_max"]) or mv % p["core_step"]:
        raise WallViolation("wall_voltage", value=mv, min=p["core_min"], max=p["core_max"], step=p["core_step"])


# ----------------------------------------------------------------------------
# Zielmodi / Aufloesung / Gate (reine Plan-Generierung, kein Miner-Zugriff)
# ----------------------------------------------------------------------------
SWEEP_MODES = ("efficiency", "performance", "full", "target_hashrate")
RESOLUTIONS = ("fein", "grob")
MID_FRAC = 0.75              # Bandmitte = stock * 0.75 (Grenze Effizienz/Performance)
TARGET_BAND = (0.85, 1.15)   # target_hashrate: f_ziel * 0.85 .. f_ziel * 1.15
ANCHOR_STEPS = (1, 2)        # performance: Ankerpunkte = Bandmitte - 1 bzw. 2 Frequenzschritte
EFF_STOP_RISES = 2           # efficiency: Stopp, wenn bester J/TH je Frequenz >= 2x in Folge steigt
GATE_TEXT = msg("gate_above_stock")


def _grid(lo, hi, step, fs):
    """lo..hi in step, jeder Wert auf fs gerundet; hi immer enthalten."""
    out, f = [], lo
    while f <= hi:
        out.append(round_to(f, fs))
        f += step
    if not out or out[-1] != hi:
        out.append(hi)
    return sorted(set(out))


def plan_matrix(p, mode="full", resolution="fein", target_ths=None, allow_above_stock=None,
                freq_high_pct=None, mv_high_pct=None, gh_per_mhz=None, overrides=None):
    """Testplan aus Profil + Zielmodus + Aufloesung. Alle Grenzen relativ zu Stock bzw. Profil.

    overrides: optionale relative Parameter (freq_low_pct, floor_pct, freq_step_factor, mv_step_factor)
    nur fuer DIESEN Plan - aendert keine globalen Einstellungen.
    gh_per_mhz: kalibrierter Wert; fehlt er, grobe Schaetzung aus dem aktuellen Betriebspunkt ('vorlaeufig').
    """
    if mode not in SWEEP_MODES:
        raise CodedError("unknown_mode", value=repr(mode), allowed=", ".join(SWEEP_MODES))
    if resolution not in RESOLUTIONS:
        raise CodedError("unknown_resolution", value=repr(resolution), allowed=", ".join(RESOLUTIONS))
    o = overrides or {}
    fl_pct = float(o.get("freq_low_pct", FREQ_LOW_PCT))
    floor_pct = float(o.get("floor_pct", FLOOR_PCT))
    fsf = int(o.get("freq_step_factor", FREQ_STEP_FACTOR))
    msf = int(o.get("mv_step_factor", MV_STEP_FACTOR))
    allow = ALLOW_ABOVE_STOCK if allow_above_stock is None else bool(allow_above_stock)
    fh_pct = float(FREQ_HIGH_PCT if freq_high_pct is None else freq_high_pct) if allow else 0.0
    mh_pct = float(mv_high_pct or 0.0) if allow else 0.0
    fs, cs = p["frequency_step"], p["core_step"]
    S, SMV = p["stock_frequency_mhz"], p["stock_core_mv"]
    fmin, fmax, cmin, cmax = p["frequency_min"], p["frequency_max"], p["core_min"], p["core_max"]
    notes, formula = [], {}

    # Aufloesung
    mult = 1 if resolution == "fein" else 2
    f_step = max(fs, fsf * fs) * mult
    mv_step = max(cs, msf * cs) * mult
    formula["freq_step"] = f"max({fs}, {fsf} * {fs}) * {mult} = {f_step} MHz"
    formula["mv_step"] = f"max({cs}, {msf} * {cs}) * {mult} = {mv_step} mV"

    # stock-relative Grenzen + harte Mauer
    f_low = snap(S * (1 - fl_pct), fs, fmin, fmax)
    formula["f_low"] = f"round_to({S} * (1 - {fl_pct}) = {S * (1 - fl_pct):g}, {fs}), clamped [{fmin}..{fmax}] = {f_low}"
    f_high_raw = round_to(S * (1 + fh_pct), fs) if allow else S
    f_high = snap(f_high_raw, fs, fmin, fmax)
    f_high_capped = f_high_raw > fmax
    if f_high < f_low:
        raise CodedError("derive_no_freqs")
    formula["f_high"] = (f"min(round_to({S} * (1 + {fh_pct}), {fs}) = {f_high_raw}, frequency_max {fmax}) = {f_high}"
                         if allow else f"stock_frequency_mhz {S} (allow_above_stock=False)")
    f_mid = clamp(round_to(S * MID_FRAC, fs), f_low, f_high)
    formula["f_mid"] = f"round_to({S} * {MID_FRAC}, {fs}) = {f_mid}"
    # Spannung HART in [core_min..core_max]; zu schmaler Bereich -> klare Meldung statt ungueltiger Werte
    mv_stock, mv_floor, mv_err = mv_range(SMV, floor_pct, cs, cmin, cmax, mv_step)
    if mv_err:
        raise CodedError(mv_err["code"], **mv_err["params"])
    mv_top_raw = round_to(SMV * (1 + mh_pct), cs) if allow and mh_pct > 0 else mv_stock
    mv_top = max(mv_stock, snap(mv_top_raw, cs, cmin, cmax))
    formula["mv_start"] = (f"<= stock: {mv_stock} mV; above stock: min(round_to({SMV} * (1 + {mh_pct}), {cs}) = "
                           f"{mv_top_raw}, core_max {cmax}) = {mv_top} mV" if allow and mh_pct > 0
                           else f"round_to({SMV} * {START_PCT}, {cs}), clamped [{cmin}..{cmax}] = {mv_stock} mV")
    formula["mv_floor"] = f"round_to({SMV} * {floor_pct} = {SMV * floor_pct:g}, {cs}), clamped [{cmin}..{cmax}] = {mv_floor} mV"
    if f_high_capped:
        notes.append(msg("note_freq_capped", raw=f_high_raw, max=fmax))
    if mv_top_raw > cmax:
        notes.append(msg("note_mv_capped", raw=mv_top_raw, max=cmax))

    # Hashrate-Schaetzung
    gh_prelim = gh_per_mhz is None
    if gh_prelim:
        h, cf = p.get("local_hashrate_ghs"), p.get("current_frequency_mhz")
        gh_per_mhz = h / cf if isinstance(h, (int, float)) and cf else None

    # Band je Modus
    anchors, target = [], None
    if mode == "efficiency":
        lo, hi = f_low, max(f_low, f_mid)
        band_reason = msg("band_efficiency", lo=f_low, hi=hi, mid_frac=MID_FRAC)
    elif mode == "performance":
        lo, hi = f_mid, f_high
        band_reason = msg("band_performance", lo=f_mid, hi=f_high, mid_frac=MID_FRAC)
        anchors = sorted({clamp(round_to(f_mid - k * f_step, fs), f_low, f_high) for k in ANCHOR_STEPS} - {f_mid})
    elif mode == "full":
        lo, hi = f_low, f_high
        band_reason = msg("band_full", lo=f_low, hi=f_high)
    else:
        if not isinstance(target_ths, (int, float)) or target_ths <= 0:
            raise CodedError("target_required")
        if not gh_per_mhz:
            raise CodedError("no_estimate")
        f_t = target_ths * 1000 / gh_per_mhz
        lo = snap(f_t * TARGET_BAND[0], fs, fmin, f_high)
        hi = snap(f_t * TARGET_BAND[1], fs, fmin, f_high)
        target = {"target_ths": target_ths, "f_target_mhz": round(f_t, 1), "gh_per_mhz": round(gh_per_mhz, 3),
                  "preliminary": gh_prelim, "reachable": f_t <= f_high,
                  "formula": f"{target_ths} * 1000 / {gh_per_mhz:.2f} GH/s/MHz = {f_t:.1f} MHz"}
        band_reason = msg("band_target", f_target=round(f_t), lo_frac=TARGET_BAND[0], hi_frac=TARGET_BAND[1],
                          min=fmin, max=f_high)
        if f_t > f_high:
            notes.append(msg("note_target_needs_above" if not allow else "note_target_over_wall",
                             target=target_ths, f=round(f_t), f_high=f_high))
    freqs = _grid(lo, hi, f_step, fs)

    # Punkte + harte Mauer + Gate
    n = len(range(mv_stock, mv_floor - 1, -mv_step)) if mv_stock >= mv_floor else 0
    points = []
    for f in sorted(set(freqs) | set(anchors)):
        f_c = min(f, fmax)
        above = f_c > S
        mv_s = mv_top if above else mv_stock
        mv_c = min(mv_s, cmax)
        steps = len(range(mv_c, mv_floor - 1, -mv_step)) if mv_c >= mv_floor else 0
        points.append({"freq": f_c, "mv_start": mv_c, "mv_floor": mv_floor, "steps": steps,
                       "role": "anchor" if f in anchors and f not in freqs else "band",
                       "above_stock": above or mv_c > SMV,
                       "capped_to_wall": (f > fmax or mv_s > cmax
                                          or (f_high_capped and f_c == f_high)          # Band-Obergrenze gekappt
                                          or (above and mv_top_raw > cmax)),            # Spannungs-Obergrenze gekappt
                       "expected_ths": round(f_c * gh_per_mhz / 1000, 3) if gh_per_mhz else None})
    for i, q in enumerate(points):
        q["index"] = i
        # harte Mauer: gar nicht erst ungueltige Werte planen (Grenzen + Schrittraster)
        assert fmin <= q["freq"] <= fmax and not q["freq"] % fs, "wall violated (frequency)"
        assert cmin <= q["mv_floor"] <= q["mv_start"] <= cmax, "wall violated (voltage)"
        assert not q["mv_floor"] % cs and not q["mv_start"] % cs, "wall violated (core_step)"
    requires_gate = any(q["above_stock"] for q in points)
    order = "asc" if mode == "efficiency" else "desc"
    early_stop = msg("early_stop_rule", rises=EFF_STOP_RISES) if mode == "efficiency" else None
    return {"sweep_mode": mode, "resolution": resolution, "freq_step": f_step, "mv_step": mv_step,
            "band": {"from_mhz": lo, "to_mhz": hi, "reason": band_reason}, "anchors": anchors,
            "f_low": f_low, "f_mid": f_mid, "f_high": f_high, "mv_stock": mv_stock, "mv_top": mv_top,
            "mv_floor": mv_floor, "allow_above_stock": allow, "freq_high_pct": fh_pct, "mv_high_pct": mh_pct,
            "order": order, "early_stop_rule": early_stop, "target": target,
            "gh_per_mhz": gh_per_mhz, "gh_per_mhz_preliminary": gh_prelim,
            "requires_gate": requires_gate, "gate_text": GATE_TEXT if requires_gate else None,
            "wall": {"frequency_max": fmax, "core_max": cmax, "violations": 0}, "notes": notes,
            "formula": formula, "points": points, "steps_per_freq_stock": n}


PRECHECK_COST_S = SETTLE_PRECHECK_S + 5     # Vorabcheck inkl. Set/Verify


def plan_estimate(pl, diff):
    """Punktzahl + Dauer fuer Worst Case und realistisch, jeweils voll (jeder Punkt volles Fenster)
    und schnell (Vorabcheck-Suche; volles Fenster nur fuer Ergebnis-/Reserve-/Ankerpunkte)."""
    fast = _fast_estimate(pl, diff)
    worst_n = real_n = 0
    worst_s = real_s = 0.0
    for q in pl["points"]:
        win, _ = window_estimate(q["freq"], pl["gh_per_mhz"], diff)
        per = WARMUP_EST_S + win + OVERHEAD_EST_S
        rn = min(q["steps"], REALISTIC_STEPS)
        worst_n += q["steps"]; real_n += rn
        worst_s += q["steps"] * per; real_s += rn * per
        q["window_s"] = round(win)
    cal = 2 * (WARMUP_EST_S + CAL_WINDOW_S + OVERHEAD_EST_S)
    return {"points_worst_case": worst_n, "points_realistic": real_n, "calibration_points": 2,
            "duration_worst_s": round(worst_s + cal), "duration_realistic_s": round(real_s + cal),
            "full_measure": ({"duration_worst_s": fast["full_measure_duration_worst_s"],
                              "duration_realistic_s": fast["full_measure_duration_realistic_s"]} if fast else
                             {"duration_worst_s": round(worst_s + cal), "duration_realistic_s": round(real_s + cal)}),
            "fast": fast,
            "per_point": msg("est_per_point", warmup=WARMUP_EST_S, min=WINDOW_MIN_S, hits=TARGET_HITS,
                             max=WINDOW_MAX_S, overhead=OVERHEAD_EST_S)}


def _fast_estimate(pl, diff):
    mode = pl["sweep_mode"]
    if mode == "full":   # bisheriger Ablauf: kein Vorabcheck-Abstieg -> wie full_measure
        return None
    full_per_f = 1 if mode == "performance" else 1 + RESERVE_STEPS
    pts = pl["points"]
    if mode == "target_hashrate":   # gerichtet: typ. 2-3 Frequenzen um die Zielfrequenz
        ft = (pl.get("target") or {}).get("f_target_mhz") or pts[len(pts) // 2]["freq"]
        pts = sorted(pts, key=lambda q: abs(q["freq"] - ft))[:3]
    worst = real = fm_worst = fm_real = 0.0
    n_full = n_pre_w = n_pre_r = 0
    abort_cost = WARMUP_EST_S + OVERHEAD_EST_S          # Stufe-2-Fruehabbruch nach dem Warmup
    for q in pts:
        win, _ = window_estimate(q["freq"], pl["gh_per_mhz"], diff)
        full = WARMUP_EST_S + win + OVERHEAD_EST_S
        nf = min(full_per_f, q["steps"])
        pw = max(q["steps"] - nf, 0)
        pr = max(min(q["steps"], REALISTIC_STEPS) - nf, 0)
        worst += nf * full + pw * PRECHECK_COST_S
        real += nf * full + pr * PRECHECK_COST_S
        n_full += nf; n_pre_w += pw; n_pre_r += pr
        if mode == "performance":   # voll: jede gueltige Stufe oberhalb Vmin volles Fenster + 1 Abbruch
            fm_worst += q["steps"] * full
            fm_real += min(q["steps"], REALISTIC_STEPS) * full + abort_cost
        else:                        # Aufwaertssuche: Stufen unter Vmin enden per Stufe 2 statt Vorabcheck
            fm_worst += nf * full + pw * abort_cost
            fm_real += nf * full + pr * abort_cost
    cal = 2 * (WARMUP_EST_S + CAL_WINDOW_S + OVERHEAD_EST_S)
    return {"duration_worst_s": round(worst + cal), "duration_realistic_s": round(real + cal),
            "full_measure_duration_worst_s": round(fm_worst + cal),
            "full_measure_duration_realistic_s": round(fm_real + cal),
            "full_window_points": n_full, "precheck_points_worst": n_pre_w, "precheck_points_realistic": n_pre_r,
            "frequencies": len(pts), "note": msg("est_fast_perf" if mode == "performance" else "est_fast_reserve",
                                                  n=full_per_f, secs=PRECHECK_COST_S)}


def verify_tol(mv, p):
    return max(2 * p["core_step"], mv * VERIFY_TOL_FRAC)


# ----------------------------------------------------------------------------
# Plan
# ----------------------------------------------------------------------------
def read_profile():
    st = base.safe_get()
    keys = ["profile", "profile_id", "stock_frequency_mhz", "stock_core_mv", "frequency_min", "frequency_max",
            "frequency_step", "core_min", "core_max", "core_step", "local_hashrate_difficulty", "input_voltage_v",
            "current_frequency_mhz", "core_mv", "local_hashrate_ghs", "job_interval_ms", "mining_enabled",
            "asic_temp_c", "vr_temp_c"]
    return st, {k: st.get(k) for k in keys}


def last_efficiency_point():
    """Effizienz-Sweet-Spot aus dem juengsten Run-JSON (falls vorhanden)."""
    for path in sorted(glob.glob(os.path.join(HERE, "runs", "*.json")), key=os.path.getmtime, reverse=True):
        try:
            with open(path, encoding="utf-8") as f:
                d = json.load(f)
            ss = d.get("sweet_spots") or {}
            e = ss.get("efficiency")
            if e and "freq" in e:
                return e["freq"], e["mv"], e.get("jth_local"), os.path.basename(path)
        except Exception:
            continue
    return None


def window_estimate(freq, gh_per_mhz, diff):
    if not gh_per_mhz or not diff:
        return WINDOW_MIN_S, None
    t_hits = TARGET_HITS / (freq * gh_per_mhz * 1e9 / (diff * 2 ** 32))
    return min(max(WINDOW_MIN_S, t_hits), WINDOW_MAX_S), t_hits


def _fmt_dur(s):
    h, rem = divmod(int(round(s)), 3600)
    return f"{h} h {rem // 60:02d} min" if h else f"{rem // 60} min"


def print_plan(p, d):
    print("=" * 80)
    print("PROFILE")
    print("=" * 80)
    for k in ["profile", "profile_id", "stock_frequency_mhz", "stock_core_mv", "frequency_min", "frequency_max",
              "frequency_step", "core_min", "core_max", "core_step", "local_hashrate_difficulty", "input_voltage_v"]:
        v = p[k]
        print(f"  {k:<28} {_n(v, 2) if isinstance(v, float) else v}")
    print(f"  {'current operating point':<28} {p['current_frequency_mhz']} MHz / {p['core_mv']} mV, "
          f"{_n(p['local_hashrate_ghs'])} GH/s, job_interval {p['job_interval_ms']} ms")

    print()
    print("=" * 80)
    print("RELATIVE CONFIG")
    print("=" * 80)
    print(f"  FREQ_LOW_PCT={FREQ_LOW_PCT}  FREQ_HIGH_PCT={FREQ_HIGH_PCT}  ALLOW_ABOVE_STOCK={ALLOW_ABOVE_STOCK}  "
          f"FREQ_STEP_FACTOR={FREQ_STEP_FACTOR}  MV_STEP_FACTOR={MV_STEP_FACTOR}")
    print(f"  FLOOR_PCT={FLOOR_PCT}  START_PCT={START_PCT}  START_MARGIN_STEPS={START_MARGIN_STEPS}  "
          f"INPUT_V_MIN_FRAC={INPUT_V_MIN_FRAC}  EARLY_ABORT={EARLY_ABORT}")
    print(f"  WINDOW_MIN_S={WINDOW_MIN_S}  TARGET_HITS={TARGET_HITS}  WINDOW_MAX_S={WINDOW_MAX_S}  "
          f"ERROR_MAX_PCT={ERROR_MAX_PCT}  HASHRATE_MIN_FRAC={HASHRATE_MIN_FRAC}")
    print(f"  SOFT ASIC/VR={SOFT_ASIC_C}/{SOFT_VR_C} C  HARD ASIC/VR={HARD_ASIC_C}/{HARD_VR_C} C  "
          f"VERIFY_TOL_FRAC={VERIFY_TOL_FRAC}  JOBCAL_ALT_FACTOR={JOBCAL_ALT_FACTOR}")

    print()
    print("=" * 80)
    print("DERIVED PARAMETERS (from profile)")
    print("=" * 80)
    rows = [("freq_sweep_step", f"{d['freq_sweep_step']} MHz"), ("mv_sweep_step", f"{d['mv_sweep_step']} mV"),
            ("f_low", f"{d['f_low']} MHz"), ("f_high", f"{d['f_high']} MHz"),
            ("mv_start", f"{d['mv_start']} mV"), ("mv_floor", f"{d['mv_floor']} mV"),
            ("input_v_min", f"{_n(d['input_v_min'], 2)} V"),
            ("safe", f"{d['safe'][0]} MHz / {d['safe'][1]} mV"),
            ("job_alt_ms", f"{d['job_alt_ms']} ms"),
            ("verify_tol", f"{verify_tol(d['mv_start'], p):.1f} mV at mv_start, {verify_tol(d['mv_floor'], p):.1f} mV at mv_floor")]
    for k, v in rows:
        print(f"  {k:<16} = {v:<26} <- {d['formula'][k]}")
    print(f"  HARD wall: f <= frequency_max {p['frequency_max']} MHz, mv <= core_max {p['core_max']} mV "
          f"(and >= min, on step size) - checked before EVERY set")
    for e in d["errors"]:
        print(f"  !!! {e}")

    gh_est = (p["local_hashrate_ghs"] / p["current_frequency_mhz"]
              if isinstance(p["local_hashrate_ghs"], (int, float)) and p["current_frequency_mhz"] else None)
    diff = p["local_hashrate_difficulty"]
    n_steps = len(d["mv_steps"])
    n_real = min(n_steps, REALISTIC_STEPS)

    print()
    print("=" * 80)
    print(f"MATRIX  {len(d['freqs'])} frequencies x Vmin search {d['mv_start']} -> {d['mv_floor']} mV "
          f"in {d['mv_sweep_step']} mV steps")
    print("=" * 80)
    print(f"  steps per frequency = ({d['mv_start']} - {d['mv_floor']}) / {d['mv_sweep_step']} + 1 = {n_steps}")
    print(f"  {'f MHz':>6} {'% stock':>8} {'search mV':>15} {'steps':>7} {'exp. TH/s':>10} "
          f"{'hits/600s':>13} {'window eff.':>13}")
    tot_worst_nom = tot_real_nom = tot_worst_hit = tot_real_hit = 0.0
    capped = []
    for f in d["freqs"]:
        win, t_hits = window_estimate(f, gh_est, diff)
        exp = f * gh_est / 1000 if gh_est else None
        hits600 = WINDOW_MIN_S / t_hits * TARGET_HITS if t_hits else None
        if t_hits and t_hits > WINDOW_MAX_S:
            capped.append(f)
        nom = WARMUP_EST_S + WINDOW_MIN_S + OVERHEAD_EST_S
        hit = WARMUP_EST_S + win + OVERHEAD_EST_S
        tot_worst_nom += n_steps * nom ; tot_real_nom += n_real * nom
        tot_worst_hit += n_steps * hit ; tot_real_hit += n_real * hit
        print(f"  {f:>6} {f / p['stock_frequency_mhz'] * 100:>7.1f}% {d['mv_start']:>6} -> {d['mv_floor']:<6} "
              f"{n_steps:>7} {_n(exp, 2):>10} {_n(hits600, 0):>13} {win:>12.0f}s")
    cal = 2 * (WARMUP_EST_S + CAL_WINDOW_S + OVERHEAD_EST_S)
    n_f = len(d["freqs"])
    print("  " + "-" * 78)
    print(f"  TOTAL points worst case   : {n_f} x {n_steps} = {n_f * n_steps}  (+2 calibration points)")
    print(f"  TOTAL points realistic    : {n_f} x {n_real} = {n_f * n_real}  (assuming {REALISTIC_STEPS} steps/frequency, +2 calibration points)")
    print()
    print(f"  duration per point nominal: warmup ~{WARMUP_EST_S}s + {WINDOW_MIN_S}s + {OVERHEAD_EST_S}s = "
          f"{WARMUP_EST_S + WINDOW_MIN_S + OVERHEAD_EST_S}s")
    print(f"  TOTAL duration worst case : nominal {_fmt_dur(tot_worst_nom + cal):>10}   hit-corrected {_fmt_dur(tot_worst_hit + cal):>10}")
    print(f"  TOTAL duration realistic  : nominal {_fmt_dur(tot_real_nom + cal):>10}   hit-corrected {_fmt_dur(tot_real_hit + cal):>10}")
    if gh_est:
        print(f"  (hit-corrected: window = max({WINDOW_MIN_S}s, time for {TARGET_HITS} hits), cap {WINDOW_MAX_S}s; "
              f"hashrate estimate {gh_est:.2f} GH/s per MHz from current operation)")
    if capped:
        print(f"  Note: at {', '.join(map(str, capped))} MHz, {TARGET_HITS} hits are not reachable in {WINDOW_MAX_S}s "
              f"-> window ends at the cap (point stays valid, 'hits < {TARGET_HITS}' is noted).")
    if EARLY_ABORT:
        print("  EARLY_ABORT: underpowered points are discarded right after warmup (no measurement window),")
        print(f"  i.e. the last (invalid) point per frequency costs only ~{WARMUP_MAX_S}s instead of a full window.")

    # Effizienzpunkt im Band?
    print()
    print("=" * 80)
    print("CHECK: previous efficiency point in band?")
    print("=" * 80)
    ep = last_efficiency_point()
    if ep:
        ef, emv, ej, src = ep
        print(f"  source: {src} -> efficiency sweet spot {ef} MHz / {emv} mV ({_n(ej, 2)} J/TH)")
    else:
        ef, emv = p["current_frequency_mhz"], p["core_mv"]
        print(f"  no run JSON with sweet spot found - checking current operating point {ef} MHz / {emv} mV")
    f_in = d["f_low"] <= ef <= d["f_high"]
    mv_in = d["mv_floor"] <= emv <= d["mv_start"]
    near = min(d["freqs"], key=lambda f: abs(f - ef)) if d["freqs"] else None
    print(f"  frequency {ef} MHz in band [{d['f_low']} .. {d['f_high']}]: {'YES' if f_in else 'NO'}"
          + ("" if f_in else f"  (lower limit is {d['f_low'] - ef:+d} MHz off)"))
    print(f"  voltage {emv} mV in search range [{d['mv_floor']} .. {d['mv_start']}]: {'YES' if mv_in else 'NO'}")
    print(f"  nearest grid frequency: {near} MHz")
    if not f_in and ef < d["f_low"]:
        need = 1 - ef / p["stock_frequency_mhz"]
        print(f"  -> for {ef} MHz in band, FREQ_LOW_PCT >= {need:.3f} would be needed "
              f"({p['stock_frequency_mhz']} * (1 - {need:.3f}) = {ef}).")
    return {"points_worst_case": n_f * n_steps, "points_realistic": n_f * n_real,
            "duration_worst_s": tot_worst_hit + cal, "duration_realistic_s": tot_real_hit + cal,
            "efficiency_point_in_band": f_in and mv_in}


# ----------------------------------------------------------------------------
# Schreiben / Sicherheit (nur --run)
# ----------------------------------------------------------------------------
def save_run():
    os.makedirs(os.path.dirname(RUN_PATH), exist_ok=True)
    tmp = RUN_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(RUN, f, indent=2, ensure_ascii=False, default=str)
    os.replace(tmp, RUN_PATH)


def set_point(freq, mv, p):
    check_wall(freq, mv, p)
    sf, smv = p["stock_frequency_mhz"], p["stock_core_mv"]
    df, dm = abs(freq - sf) / sf, abs(mv - smv) / smv
    danger = df > DANGER_PCT or dm > DANGER_PCT
    payload = {"frequency_mhz": freq, "core_mv": mv, "save": False}
    if danger:
        payload["danger_acknowledged"] = True
    STATE["wrote_tuning"] = True
    code, text = base.post_json(TUNING, payload)
    time.sleep(1.5)
    st = base.safe_get()
    cur_f, meas = st.get("current_frequency_mhz"), st.get("measured_core_mv")
    tol = verify_tol(mv, p)
    ok = cur_f == freq and isinstance(meas, (int, float)) and abs(meas - mv) <= tol
    log(f"  set {freq}/{mv} (Danger f {df * 100:.1f}% mv {dm * 100:.1f}% -> {danger}) HTTP {code} {text.strip()} "
        f"| verify f={cur_f} mv={meas} (tol {tol:.0f}) -> {'OK' if ok else 'FAILED'}")
    return ok, meas


def set_job_interval(ms):
    STATE["job_changed"] = True
    code, text = base.post_json(JOBSCHED, {"interval_ms": ms, "save": False})
    time.sleep(1.0)
    js = base.get_json(JOBSCHED)
    ok = js.get("interval_ms") == ms
    log(f"  job-schedule {ms} ms: HTTP {code} {text.strip()} | verify {json.dumps(js)} -> {'OK' if ok else 'FAILED'}")
    if not ok:
        raise RuntimeError(f"job interval {ms} ms not confirmed")


def emergency_stop(grund):
    STATE["emergency"] = True
    try:
        code, text = base.post_json(MINING, {"enabled": False})
        log(f"POST /mining {{\"enabled\": false}} -> HTTP {code} {text.strip()}")
    except RuntimeError as e:
        log(f"!!! POST mining off FAILED: {e}")
    print("!" * 80)
    print(f"!!! EMERGENCY SHUTDOWN: {msg_text(grund)} - ASICs disabled (mining enabled=false)")
    print("!" * 80, flush=True)
    RUN["status"], RUN["emergency_reason"] = "emergency_stop", grund
    try:
        save_run()
    except Exception as e:
        log(f"Run JSON could not be saved: {e}")
    sys.exit(1)


def guard(st):
    a, v, vin = st.get("asic_temp_c"), st.get("vr_temp_c"), st.get("input_voltage_v")
    if isinstance(a, (int, float)) and a > HARD_ASIC_C:
        emergency_stop(msg("hard_temp", sensor="asic", value=round(a, 1), limit=HARD_ASIC_C))
    if isinstance(v, (int, float)) and v > HARD_VR_C:
        emergency_stop(msg("hard_temp", sensor="vr", value=round(v, 1), limit=HARD_VR_C))
    if isinstance(a, (int, float)) and a > SOFT_ASIC_C:
        raise PointAbort("soft_temp", sensor="asic", value=round(a, 1), limit=SOFT_ASIC_C)
    if isinstance(v, (int, float)) and v > SOFT_VR_C:
        raise PointAbort("soft_temp", sensor="vr", value=round(v, 1), limit=SOFT_VR_C)
    if D.get("input_v_min") and isinstance(vin, (int, float)) and vin < D["input_v_min"]:
        raise PointAbort("input_voltage_low", value=round(vin, 2), limit=round(D["input_v_min"], 2))


def expected_ths(freq):
    return freq * GH_PER_MHZ / 1000.0 if GH_PER_MHZ else None


# ----------------------------------------------------------------------------
# Messkern
# ----------------------------------------------------------------------------
def measure_point(freq, mv, label, p, window_s=None, target_hits=None, window_max_s=None,
                  precheck_only=False):
    """precheck_only=True: nur Stufe 1 (Vorabcheck ~25 s), Ergebnis status 'precheck_pass'/'precheck_fail'.
    FULL_MEASURE=True deaktiviert den Vorabcheck-Skip (nicht aber den Stufe-2-Fruehabbruch).
    Fenster-Parameter None -> aktuelle Modulwerte ZUR LAUFZEIT (config.json / Laufparameter wirken so wirklich)."""
    window_s = WINDOW_MIN_S if window_s is None else window_s
    target_hits = TARGET_HITS if target_hits is None else target_hits
    window_max_s = WINDOW_MAX_S if window_max_s is None else window_max_s
    r = {"label": label, "freq": freq, "mv": mv, "window_params": {"min_s": window_s, "max_s": window_max_s, "hits": target_hits}}
    publish_live(idx=len(LIVE["results"]), frequency_mhz=freq, core_mv=mv, phase="setting", elapsed_s=0,
                 remaining_s=0, avg={"samples": 0})
    ok, meas = set_point(freq, mv, p)
    if not ok:
        return dict(r, status="verify_failed", valid=False, reason=msg("verify_failed"), measured_mv=meas)

    # adaptives Warmup
    warm, need, settle_s = [], int(round(SETTLE_WINDOW_S / SAMPLE_S)) + 1, None
    exp_pre, prechecked = expected_ths(freq), False
    t_w, n = time.monotonic(), 0
    while True:
        n += 1
        target = t_w + n * SAMPLE_S
        if target > t_w + WARMUP_MAX_S + 1e-6:
            break
        _sleep(target - time.monotonic())
        st = base.safe_get()
        t = time.monotonic() - t_w
        live_line(label, "warmup", t, st)
        publish_live(phase="warmup", elapsed_s=round(t, 1), remaining_s=round(WARMUP_MAX_S - t, 1), live=live_from(st))
        guard(st)
        warm.append((t, st.get("local_hashrate_ghs")))
        # Stufe 1: Schnell-Vorabcheck
        if exp_pre and (precheck_only or not FULL_MEASURE) and not prechecked and t >= SETTLE_PRECHECK_S - 0.5:
            prechecked = True
            last10 = [x for tt, x in warm if tt >= t - PRECHECK_AVG_S - 0.5 and isinstance(x, (int, float))]
            pf = statistics.mean(last10) / 1000 / exp_pre if last10 else 0.0
            if pf < PRECHECK_FRAC:
                reason = msg("precheck_underpowered", pct=round(pf * 100), thresh=round(PRECHECK_FRAC * 100), secs=round(t))
                log(f"  => {label:<10} STAGE 1 {msg_text(reason)} -> INVALID, no measurement window")
                return dict(r, status="precheck_fail", stage=1, valid=False, reason=reason, measured_mv=meas,
                            frac_of_expected=pf, expected_ths=exp_pre, point_s=round(t, 1))
            if precheck_only:
                log(f"  => {label:<10} STAGE 1 passed: {pf * 100:.0f}% >= {PRECHECK_FRAC * 100:.0f}% (precheck only, no window)")
                return dict(r, status="precheck_pass", stage=1, valid=None, reason=msg("precheck_ok", pct=round(pf * 100)),
                            measured_mv=meas, frac_of_expected=pf, expected_ths=exp_pre, point_s=round(t, 1))
            log(f"    [{label} precheck] {pf * 100:.0f}% of expected >= {PRECHECK_FRAC * 100:.0f}% -> continue")
        recent = [x for tt, x in warm if tt >= t - SETTLE_WINDOW_S - 0.5 and isinstance(x, (int, float))]
        if t >= SETTLE_WINDOW_S and len(recent) >= need:
            m = statistics.mean(recent)
            if all(abs(x - m) <= SETTLE_TOL_FRAC * m for x in recent):
                settle_s = round(t, 1)
                break
    not_settled = settle_s is None

    # EARLY_ABORT: Hashrate der letzten SETTLE_WINDOW_S gegen Erwartung
    exp = expected_ths(freq)
    last = [x for tt, x in warm if tt >= warm[-1][0] - SETTLE_WINDOW_S - 0.5 and isinstance(x, (int, float))] if warm else []
    warm_frac = (statistics.mean(last) / 1000 / exp) if (exp and last) else None
    if EARLY_ABORT and warm_frac is not None and warm_frac < HASHRATE_MIN_FRAC:
        reason = (msg("early_abort_warmup", pct=round(warm_frac * 100), settle_s=settle_s) if settle_s
                  else msg("early_abort_warmup_unsettled", pct=round(warm_frac * 100)))
        log(f"  => {label:<10} STAGE 2 {msg_text(reason)} -> INVALID, no measurement window")
        return dict(r, status="early_abort", stage=2, valid=False, reason=reason, measured_mv=meas,
                    frac_of_expected=warm_frac, expected_ths=exp, settle_s=settle_s, not_settled=not_settled,
                    point_s=round(warm[-1][0], 1) if warm else None)

    # Messfenster: bis >= window_s UND >= target_hits, Deckel window_max_s
    s0 = base.safe_get()
    guard(s0)
    t0 = time.monotonic()
    diff = s0.get("local_hashrate_difficulty")
    c0 = dict(valid0=s0.get("local_valid_candidates"), invalid0=s0.get("local_invalid_candidates"),
              accepted0=s0.get("accepted_shares"), rejected0=s0.get("rejected_shares"))
    series = {k: [] for k in tuner.SERIES_FIELDS}
    mvs, hits, n, last_report = [], 0, 0, 0.0
    wall_srcs = set()
    while True:
        n += 1
        _sleep(t0 + n * SAMPLE_S - time.monotonic())
        st = base.safe_get()
        for k in series:
            series[k].append(effective_wall_power(st)[0] if k == "wall_power_w" else st.get(k))
        wall_srcs.add(effective_wall_power(st)[1])
        mvs.append(st.get("measured_core_mv"))
        v = st.get("local_valid_candidates")
        if isinstance(v, int) and isinstance(c0["valid0"], int):
            hits = v - c0["valid0"] if v >= c0["valid0"] else v
        el = time.monotonic() - t0
        if el - last_report >= WINDOW_LOG_S:
            last_report = el
            live_line(label, "window", el, st, f"  hits={hits}")
        mm = tuner.compute_metrics(series, dict(c0, valid1=v, invalid1=st.get("local_invalid_candidates"),
                                                accepted1=st.get("accepted_shares"), rejected1=st.get("rejected_shares")),
                                   el, diff) if isinstance(v, int) else None
        publish_live(phase="measuring", elapsed_s=round(el, 1), remaining_s=round(max(window_s - el, 0), 1),
                     live=live_from(st), avg=({k: mm[k] for k in ["error_pct", "hashrate_local_ths", "hashrate_valid_ths",
                                                                 "wall_avg", "jth_local", "jth_valid", "samples"]} if mm else {}))
        guard(st)
        # Stufe 2 im Fenster: gleitendes Mittel faellt unter 0.90 oder Fehlerrate steigt
        if exp and el >= INWIN_CHECK_S:
            recent = [x for x in series["local_hashrate_ghs"][-int(INWIN_CHECK_S / SAMPLE_S):] if isinstance(x, (int, float))]
            rf = statistics.mean(recent) / 1000 / exp if recent else None
            bad = None
            if rf is not None and rf < HASHRATE_MIN_FRAC:
                bad = msg("early_abort_window_hashrate", pct=round(rf * 100), secs=round(el))
            elif mm and (mm["dvalid"] + mm["dinvalid"]) >= INWIN_MIN_HITS and mm["error_pct"] >= ERROR_MAX_PCT:
                bad = msg("early_abort_window_error", err=round(mm["error_pct"], 2), secs=round(el))
            if bad:
                log(f"  => {label:<10} STAGE 2 {msg_text(bad)} -> INVALID")
                return dict(r, status="early_abort", stage=2, valid=False, reason=bad, measured_mv=meas,
                            frac_of_expected=rf, expected_ths=exp, settle_s=settle_s,
                            point_s=round((settle_s or WARMUP_MAX_S) + el, 1))
        if (el >= window_s and hits >= target_hits) or el >= window_max_s:
            break
    s1 = base.safe_get()
    t1 = time.monotonic()
    counters = dict(c0, valid1=s1.get("local_valid_candidates"), invalid1=s1.get("local_invalid_candidates"),
                    accepted1=s1.get("accepted_shares"), rejected1=s1.get("rejected_shares"))
    m = tuner.compute_metrics(series, counters, t1 - t0, diff)

    frac = m["hashrate_local_ths"] / exp if exp else None
    min_samples = int(window_s / SAMPLE_S * MIN_SAMPLE_FRAC)
    hard, notes = [], []
    if frac is not None and frac < HASHRATE_MIN_FRAC:
        hard.append(msg("underpowered", pct=round(frac * 100)))
    if m["error_pct"] >= ERROR_MAX_PCT:
        hard.append(msg("unstable", err=round(m["error_pct"], 2)))
    if m["samples"] < min_samples:
        hard.append(msg("too_few_samples", n=m["samples"], min=min_samples))
    if not_settled:
        hard.append(msg("not_settled"))
    if target_hits and m["dvalid"] < target_hits:
        notes.append(msg("hits_capped", hits=m["dvalid"], target=target_hits))
    valid = not hard
    mv_vals = [x for x in mvs if isinstance(x, (int, float))]
    r.update(status="measured", measured_mv=round(statistics.mean(mv_vals), 1) if mv_vals else None,
             error_pct=m["error_pct"], hashrate_local_ths=m["hashrate_local_ths"],
             hashrate_valid_ths=m["hashrate_valid_ths"], expected_ths=exp, frac_of_expected=frac,
             wall_avg=m["wall_avg"], rail_avg=m["rail_avg"], jth_local=m["jth_local"], jth_valid=m["jth_valid"],
             asic_temp_max=m["asic_temp_max"], vr_temp_max=m["vr_temp_max"], settle_s=settle_s,
             not_settled=not_settled, dvalid=m["dvalid"], dinvalid=m["dinvalid"], samples=m["samples"],
             dur=m["dur"], valid=valid, reason=(hard + notes) or [msg("ok")], stage=3,
             point_s=round((settle_s or WARMUP_MAX_S) + m["dur"], 1),
             wall_power_source=(wall_srcs.pop() if len(wall_srcs) == 1 else "mixed") if wall_srcs else None)
    log(f"  => {label:<10} mv={_n(r['measured_mv'])} settle={_n(settle_s, 0)}s dur={m['dur']:.0f}s hits={m['dvalid']}  "
        f"local={_n(m['hashrate_local_ths'], 3)} TH/s ({'%.0f%%' % (frac * 100) if frac else 'n/a'} exp.)  "
        f"wall={_n(m['wall_avg'], 1)} W ({r['wall_power_source']})  J/TH={_n(m['jth_local'], 2)}  err={_n(m['error_pct'], 2)}%  "
        f"-> {'VALID' if valid else 'INVALID'} ({msg_text(r['reason'])})")
    return r


# ----------------------------------------------------------------------------
# Sweet Spots
# ----------------------------------------------------------------------------
def sweet_spots(points, vmin):
    valid = [q for q in points if q.get("valid")]
    out = {"efficiency": None, "knee": None, "compromise": None}
    keys = ("freq", "mv", "hashrate_local_ths", "wall_avg", "jth_local")
    print()
    print("=" * 80)
    print("SWEET SPOTS (valid points only)")
    print("=" * 80)
    if not valid:
        print("  No valid points.")
        return out
    fmt = lambda q: (f"{q['freq']} MHz / {q['mv']} mV -> {q['hashrate_local_ths']:.3f} TH/s | "
                     f"{q['wall_avg']:.1f} W | {q['jth_local']:.2f} J/TH")
    eff = min(valid, key=lambda q: q["jth_local"])
    out["efficiency"] = {k: eff[k] for k in keys}
    print(f"a) Efficiency (min J/TH):       {fmt(eff)}")

    curve = sorted([q for q in valid if vmin.get(q["freq"]) == q["mv"]], key=lambda q: q["hashrate_local_ths"])
    knee, reason, lines, ref = (curve[-1] if curve else None), msg("knee_highest_vmin"), [], None
    for i in range(1, len(curve)):
        dT = curve[i]["hashrate_local_ths"] - curve[i - 1]["hashrate_local_ths"]
        dW = curve[i]["wall_avg"] - curve[i - 1]["wall_avg"]
        marg = dW / dT if dT > 0 else None
        if ref is None and marg is not None:
            ref, knee = marg, curve[0]
        ok = marg is not None and ref is not None and marg <= KNEE_FACTOR * ref
        lines.append(f"     {curve[i - 1]['freq']}/{curve[i - 1]['mv']} -> {curve[i]['freq']}/{curve[i]['mv']}: "
                     f"dW={dW:+.1f} W dT={dT:+.3f} TH/s marg={_n(marg, 1)} W/(TH/s)"
                     + (f" {'<=' if ok else '>'} {KNEE_FACTOR * ref:.1f}" if ref else ""))
        if ref is not None and not ok:
            reason = msg("knee_segment_exceeds", freq=curve[i]["freq"], mv=curve[i]["mv"], factor=KNEE_FACTOR)
            break
        if ok:
            knee = curve[i]
            reason = msg("knee_all_segments_ok", factor=KNEE_FACTOR)
    if knee:
        out["knee"] = dict({k: knee[k] for k in keys}, reason=reason)
        print(f"b) Performance knee:            {fmt(knee)}")
        for ln in lines:
            print(ln)
        print(f"     Reason: {msg_text(reason)}")
    best = eff["jth_local"]
    cands = [q for q in valid if q["jth_local"] <= COMPROMISE_FRAC * best]
    comp = max(cands, key=lambda q: (q["hashrate_local_ths"], -q["jth_local"]))
    out["compromise"] = dict({k: comp[k] for k in keys}, jth_limit=COMPROMISE_FRAC * best)
    print(f"c) Best compromise (J/TH <= {COMPROMISE_FRAC:.2f} x {best:.2f} = {COMPROMISE_FRAC * best:.2f}): {fmt(comp)}")
    print("=" * 80)
    return out


# ----------------------------------------------------------------------------
# Sicherungen: Banner + Selbsttest (ohne Netzzugriff)
# ----------------------------------------------------------------------------
class _SelftestEmergency(Exception):
    pass


def safety_banner_and_selftest(p):
    print()
    print("=" * 80)
    print("SAFEGUARDS (active for the whole run, checked on every sample)")
    print("=" * 80)
    log(f"  HARD  ASIC > {HARD_ASIC_C} C or VR > {HARD_VR_C} C  -> EMERGENCY SHUTDOWN: POST /mining {{\"enabled\": false}}, sweep ends")
    log(f"  SOFT  ASIC > {SOFT_ASIC_C} C or VR > {SOFT_VR_C} C  -> discard point, start point, next frequency")
    log(f"  INPUT input_voltage_v < {D['input_v_min']:.2f} V  -> discard point, start point, next frequency")
    log(f"  DEADMAN {base.DEADMAN_MAX_CONSEC} status errors in a row (pause {base.DEADMAN_RETRY_PAUSE_S}s) -> sweep abort + reset")
    log(f"  WALL  f <= {p['frequency_max']} MHz, mv <= {p['core_max']} mV (profile) before every set; SIGTERM/Ctrl+C -> reset")
    # Selbsttest: Waechter mit kuenstlichen Werten ausloesen - emergency_stop und Netz dabei ersetzt
    real_es, real_get, real_pause = globals()["emergency_stop"], tuner.get_status, base.DEADMAN_RETRY_PAUSE_S
    fired = []

    def fake_es(grund):
        fired.append(grund)
        raise _SelftestEmergency(grund)

    def fake_get():
        raise RuntimeError("self-test: simulated status error")

    results = []
    try:
        globals()["emergency_stop"] = fake_es
        ok_t = {"asic_temp_c": 40.0, "vr_temp_c": 45.0, "input_voltage_v": D["input_v_min"] + 0.5}
        for name, st, expect in [
            ("HARD ASIC", dict(ok_t, asic_temp_c=HARD_ASIC_C + 1), "emergency"),
            ("HARD VR", dict(ok_t, vr_temp_c=HARD_VR_C + 1), "emergency"),
            ("SOFT ASIC", dict(ok_t, asic_temp_c=SOFT_ASIC_C + 1), "abort"),
            ("SOFT VR", dict(ok_t, vr_temp_c=SOFT_VR_C + 1), "abort"),
            ("INPUT", dict(ok_t, input_voltage_v=D["input_v_min"] - 0.01), "abort"),
            ("normal", ok_t, "none"),
        ]:
            try:
                guard(st)
                got = "none"
            except _SelftestEmergency:
                got = "emergency"
            except PointAbort:
                got = "abort"
            results.append((name, expect, got))
        tuner.get_status = fake_get
        base.DEADMAN_RETRY_PAUSE_S = 0
        try:
            base.safe_get()
            got = "none"
        except base.Deadman:
            got = "deadman"
        results.append(("DEADMAN", "deadman", got))
    finally:
        globals()["emergency_stop"] = real_es
        tuner.get_status = real_get
        base.DEADMAN_RETRY_PAUSE_S = real_pause
    all_ok = all(e == g for _, e, g in results)
    log("  SELF-TEST: " + "  ".join(f"{n}:{'OK' if e == g else 'ERROR(' + g + ')'}" for n, e, g in results)
        + f"  -> {'ALL SAFEGUARDS ARMED' if all_ok else 'FAILED'}")
    RUN["safety_selftest"] = {n: g for n, _, g in results}
    if not all_ok:
        raise RuntimeError("safeguard self-test failed - abort without write access")


# ----------------------------------------------------------------------------
# --run
# ----------------------------------------------------------------------------
def setup_and_calibrate(st, p, d):
    """Vorbedingungen, Sicherungs-Selbsttest, Job-Intervall- und Hashrate-Kalibrierung (gemeinsam fuer alle Modi)."""
    global GH_PER_MHZ
    RUN.update(miner_url=MINER_BASE, profile=p["profile"], profile_id=p["profile_id"],
               stock={"frequency_mhz": p["stock_frequency_mhz"], "core_mv": p["stock_core_mv"]},
               bounds={k: p[k] for k in ["frequency_min", "frequency_max", "frequency_step",
                                         "core_min", "core_max", "core_step"]},
               derived={k: v for k, v in d.items() if k != "formula"}, derived_formula=d["formula"],
               relative_config={k: globals()[k] for k in [
                   "FREQ_LOW_PCT", "FREQ_HIGH_PCT", "ALLOW_ABOVE_STOCK", "FREQ_STEP_FACTOR", "MV_STEP_FACTOR",
                   "FLOOR_PCT", "START_PCT", "START_MARGIN_STEPS", "WINDOW_MIN_S", "TARGET_HITS", "WINDOW_MAX_S",
                   "ERROR_MAX_PCT", "HASHRATE_MIN_FRAC", "INPUT_V_MIN_FRAC", "EARLY_ABORT", "VERIFY_TOL_FRAC",
                   "JOBCAL_ALT_FACTOR"]},
               original_job_interval_ms=p["job_interval_ms"])
    if p["mining_enabled"] is not True:
        raise RuntimeError("mining_enabled is not True - abort without write access")
    if d["errors"]:
        raise RuntimeError("derived parameters invalid: " + msg_text(d["errors"]))
    check_wall(*d["safe"], p)
    guard(st)
    LIVE.update(stock=RUN["stock"], bounds=RUN["bounds"],
                config={"warmup_s": WARMUP_MAX_S, "window_s": WINDOW_MIN_S, "sample_s": SAMPLE_S})
    RUN["status"] = "running"
    safety_banner_and_selftest(p)
    publish_live(phase="calibration")

    # Kalibrierung: Stock-Frequenz bei mv_start
    ref_f, ref_mv = d["f_high"] if not ALLOW_ABOVE_STOCK else p["stock_frequency_mhz"], d["mv_start"]
    orig, alt = p["job_interval_ms"], d["job_alt_ms"]
    print()
    print("=" * 80)
    print(f"CALIBRATION  reference {ref_f} MHz / {ref_mv} mV, window {CAL_WINDOW_S}s, job {orig} vs {alt} ms")
    print("=" * 80)
    c1 = measure_point(ref_f, ref_mv, f"cal-{orig}", p, CAL_WINDOW_S, 0, CAL_WINDOW_S)
    add_result(c1)
    if c1.get("status") != "measured" or c1.get("not_settled"):
        raise RuntimeError(f"calibration failed ({msg_text(c1.get('reason'))})")
    set_job_interval(alt)
    c2 = measure_point(ref_f, ref_mv, f"cal-{alt}", p, CAL_WINDOW_S, 0, CAL_WINDOW_S)
    add_result(c2)
    if c2.get("status") != "measured" or c2.get("not_settled"):
        raise RuntimeError(f"calibration of alternative failed ({msg_text(c2.get('reason'))})")
    h1, h2 = c1["hashrate_local_ths"], c2["hashrate_local_ths"]
    if h2 >= h1 * (1 + JOBCAL_GAIN_FRAC):
        job, h_ref = alt, h2
        decision = msg("job_switch", ms=alt, gain_pct=round((h2 / h1 - 1) * 100, 1))
    else:
        job, h_ref = orig, h1
        decision = msg("job_keep", ms=orig, gain_pct=round((h2 / h1 - 1) * 100, 1), thresh=round(JOBCAL_GAIN_FRAC * 100))
        set_job_interval(orig)
        STATE["job_changed"] = False
    GH_PER_MHZ = h_ref * 1000 / ref_f
    log(f"Job interval: {orig} ms -> {h1:.3f} TH/s | {alt} ms -> {h2:.3f} TH/s => {msg_text(decision)}")
    log(f"GH_PER_MHZ = {h_ref * 1000:.1f} / {ref_f} = {GH_PER_MHZ:.2f} GH/s per MHz")
    RUN["calibration"] = {"ref": {"freq": ref_f, "mv": ref_mv}, "h_default_ths": h1, "h_alt_ths": h2,
                          "decision": decision, "points": [c1, c2]}
    RUN["gh_per_mhz"], RUN["job_interval_ms"] = GH_PER_MHZ, job
    save_run()


def run(st, p, d, freqs=None):
    """freqs: optionale Auswahl (Teilmenge von d['freqs']); None = alle."""
    if freqs:
        chosen = sorted({int(f) for f in freqs} & set(d["freqs"]))
        if not chosen:
            raise RuntimeError(f"none of the selected frequencies {freqs} is in the plan {d['freqs']}")
        d = dict(d, freqs=chosen)
    setup_and_calibrate(st, p, d)

    # Vmin-Suche: Frequenzen absteigend
    print()
    print("=" * 80)
    print(f"VMIN SEARCH  {len(d['freqs'])} frequencies, step {d['mv_sweep_step']} mV, floor {d['mv_floor']} mV")
    print("=" * 80)
    prev_vmin = None
    for f in sorted(d["freqs"], reverse=True):
        start = d["mv_start"] if prev_vmin is None else min(d["mv_start"], prev_vmin + START_MARGIN_STEPS * d["mv_sweep_step"])
        log(f"--- {f} MHz: start {start} mV, down in {d['mv_sweep_step']} mV steps to {d['mv_floor']} mV ---")
        vmin, mv, hit_floor = None, start, False
        while mv >= d["mv_floor"]:
            try:
                r = measure_point(f, mv, f"{f}/{mv}", p)
            except PointAbort as e:
                log(f"  => {f}/{mv}: ABORT ({e}) -> start point, search for {f} MHz ended")
                RUN["points"].append({"label": f"{f}/{mv}", "freq": f, "mv": mv, "status": "aborted",
                                      "valid": False, "reason": msg(e.code, **e.params)})
                add_result(RUN["points"][-1])
                set_point(*d["safe"], p)
                save_run()
                break
            RUN["points"].append(r)
            add_result(r)
            save_run()
            if not r["valid"]:
                break
            vmin = mv
            if mv - d["mv_sweep_step"] < d["mv_floor"]:
                hit_floor = True
            mv -= d["mv_sweep_step"]
        RUN["vmin"][str(f)] = {"vmin": vmin, "floor_reached": hit_floor}
        log(f"*** Vmin({f} MHz) = {vmin if vmin is not None else 'none found'} mV"
            f"{'  (next step would be below mv_floor ' + str(d['mv_floor']) + ' - true Vmin may be lower)' if hit_floor else ''} ***")
        if vmin is not None:
            prev_vmin = vmin

    print()
    print("=" * 80)
    print("OVERVIEW")
    print("=" * 80)
    for q in RUN["points"]:
        print(f"  {q['freq']:>5}/{q['mv']:<5} {_n(q.get('hashrate_local_ths'), 3):>7} TH/s "
              f"{_n(q.get('wall_avg'), 1):>6} W {_n(q.get('jth_local'), 2):>6} J/TH  err {_n(q.get('error_pct'), 2):>5}%  "
              f"{'VALID' if q.get('valid') else 'invalid'} ({msg_text(q.get('reason'))})")
    vmin_map = {int(k): v["vmin"] for k, v in RUN["vmin"].items() if v["vmin"] is not None}
    RUN["sweet_spots"] = sweet_spots(RUN["points"], vmin_map)
    RUN["status"] = "finished"
    publish_live(idx=None, phase="idle")


# ----------------------------------------------------------------------------
# Gerichtete Suche je Zielmodus
# ----------------------------------------------------------------------------
def _measure(f, mv, p, tag="", precheck_only=False):
    """Punkt messen + protokollieren. PointAbort -> Ausgangspunkt, Ergebnis mit status 'aborted'.
    tag: sprachneutraler Punkt-Tag (reserve, probe_down, knee_reserve, precheck) - Label + Feld 'tag'."""
    label = f"{f}/{mv}" + (f" ({tag})" if tag else "")
    try:
        r = measure_point(f, mv, label, p, precheck_only=precheck_only)
    except PointAbort as e:
        log(f"  => {label}: ABORT ({e}) -> start point")
        r = {"label": label, "freq": f, "mv": mv, "status": "aborted", "valid": False,
             "reason": msg(e.code, **e.params), "stage": 0}
        set_point(*D["safe"], p)
    if tag:
        r["tag"] = tag
    RUN["points"].append(r)
    add_result(r)
    save_run()
    return r


def search_up(f, start_mv, floor_mv, top_mv, step, p):
    """Von unten nach oben bis zum ersten VOLL versorgten Punkt; dann + RESERVE_STEPS.
    Ist schon der Startpunkt versorgt (und liegt ueber dem Boden), wird nach unten nachgetastet."""
    seen = {}
    mv = max(start_mv, floor_mv)
    found = None
    while mv <= top_mv:
        r = _measure(f, mv, p)
        seen[mv] = r
        if r.get("status") == "aborted":
            return {"found": None, "vmin": None, "rec": None, "aborted": True}
        if r.get("valid"):
            found = mv
            break
        mv += step
    if found is None:
        return {"found": None, "vmin": None, "rec": None, "aborted": False}
    if found == max(start_mv, floor_mv) and found > floor_mv:       # Start schon versorgt -> nachtasten
        probe = found - step
        while probe >= floor_mv:
            r = _measure(f, probe, p, "probe_down")
            seen[probe] = r
            if r.get("status") == "aborted" or not r.get("valid"):
                break
            found = probe
            probe -= step
    vmin = min(found + RESERVE_STEPS * step, top_mv)
    rec = seen.get(vmin)
    if rec is None or not rec.get("valid"):
        rec = _measure(f, vmin, p, "reserve") if vmin not in seen else rec
    if not rec.get("valid"):
        rec, vmin = seen[found], found
    seen[found]["result"] = True     # Ergebnispunkte (fuer Sweet Spots): erster versorgter Punkt + Reserve
    rec["result"] = True
    log(f"*** {f} MHz: first fully supplied point {found} mV -> Vmin with reserve {vmin} mV "
        f"({_n(rec.get('hashrate_local_ths'), 3)} TH/s, {_n(rec.get('jth_local'), 2)} J/TH) ***")
    return {"found": found, "vmin": vmin, "rec": rec, "aborted": False}


def _knee_reserve(f, vmin, rec, start_mv, step, p, seen):
    """Grenzwertiges Vmin (< KNEE_MIN_FRAC der Erwartung): Vmin + 1 Schritt als stabilen Punkt fuer die
    Knie-Kurve mitfuehren - im vollen Modus schon gemessen, im schnellen Modus jetzt voll vermessen."""
    if (rec.get("frac_of_expected") or 1) >= KNEE_MIN_FRAC or vmin + step > start_mv:
        return
    r2 = seen.get(vmin + step) or _measure(f, vmin + step, p, "knee_reserve")
    if r2.get("valid"):
        r2["result"] = True


def search_down(f, start_mv, floor_mv, step, p):
    """Von sicher versorgt nach unten; Vmin = niedrigster gueltiger Punkt.
    Schnell (FULL_MEASURE=False): abwaerts NUR per Vorabcheck, volles Fenster erst am niedrigsten bestandenen
    Punkt; ist der ungueltig (75..90 %), mit vollen Fenstern stufenweise zurueck nach oben bis gueltig.
    Voll (FULL_MEASURE=True): jede Stufe volles Fenster bis zum ersten ungueltigen Punkt."""
    if not FULL_MEASURE:
        last_pass, mv = None, start_mv
        while mv >= floor_mv:
            r = _measure(f, mv, p, "precheck", precheck_only=True)
            if r.get("status") == "aborted":
                return {"found": None, "vmin": None, "rec": None, "aborted": True}
            if r.get("status") != "precheck_pass":
                break
            last_pass = mv
            mv -= step
        vmin, rec, mv = None, None, last_pass
        while mv is not None and mv <= start_mv:
            r = _measure(f, mv, p)
            if r.get("status") == "aborted":
                break
            if r.get("valid"):
                vmin, rec = mv, r
                break
            mv += step
        if rec is not None:
            rec["result"] = True
            _knee_reserve(f, vmin, rec, start_mv, step, p, {})
        log(f"*** {f} MHz: Vmin {vmin if vmin is not None else 'none'} mV (fast: precheck descent to "
            f"{last_pass}, full window from there)"
            + (f" ({_n(rec.get('hashrate_local_ths'), 3)} TH/s, {_n(rec.get('jth_local'), 2)} J/TH)" if rec else "") + " ***")
        return {"found": vmin, "vmin": vmin, "rec": rec, "aborted": False}
    vmin, rec, mv, seen = None, None, start_mv, {}
    while mv >= floor_mv:
        r = _measure(f, mv, p)
        seen[mv] = r
        if r.get("status") == "aborted" or not r.get("valid"):
            break
        vmin, rec = mv, r
        mv -= step
    if rec is not None:
        rec["result"] = True
        _knee_reserve(f, vmin, rec, start_mv, step, p, seen)
    log(f"*** {f} MHz: Vmin {vmin if vmin is not None else 'none'} mV"
        + (f" ({_n(rec.get('hashrate_local_ths'), 3)} TH/s, {_n(rec.get('jth_local'), 2)} J/TH)" if rec else "") + " ***")
    return {"found": vmin, "vmin": vmin, "rec": rec, "aborted": False}


def _best_jth(f):
    v = [q["jth_local"] for q in RUN["points"] if q.get("freq") == f and q.get("valid") and q.get("jth_local")]
    return min(v) if v else None


def run_efficiency(p, pl, freqs):
    step = pl["mv_step"]
    prev_vmin, rises, last_best = None, 0, None
    for f in sorted(freqs):
        q = next(x for x in pl["points"] if x["freq"] == f)
        start = pl["mv_floor"] if prev_vmin is None else max(pl["mv_floor"], prev_vmin - step)
        log(f"--- {f} MHz (ascending): start {start} mV {'(floor)' if prev_vmin is None else f'(Vmin of previous frequency {prev_vmin} - {step})'}"
            f", up to {q['mv_start']} mV ---")
        res = search_up(f, start, pl["mv_floor"], q["mv_start"], step, p)
        RUN["vmin"][str(f)] = {"vmin": res["vmin"], "found": res["found"], "start_mv": start}
        if res["vmin"] is None:
            log(f"*** {f} MHz not fully supplied up to {q['mv_start']} mV{' (abort)' if res['aborted'] else ''} "
                f"-> higher frequencies are not tested ***")
            break
        prev_vmin = res["vmin"]
        best = _best_jth(f)
        if last_best is not None and best is not None:
            rises = rises + 1 if best > last_best else 0
            log(f"    best J/TH {f} MHz = {best:.2f} (before {last_best:.2f}) -> rises in a row: {rises}")
        last_best = best if best is not None else last_best
        if rises >= EFF_STOP_RISES:
            log(f"*** Stop rule: best J/TH rose {EFF_STOP_RISES}x in a row -> efficiency valley captured ***")
            RUN["early_stop"] = msg("early_stop_after", freq=f)
            break


def run_performance(p, pl, freqs):
    step = pl["mv_step"]
    prev_vmin = None
    for f in sorted(freqs, reverse=True):
        q = next(x for x in pl["points"] if x["freq"] == f)
        start = q["mv_start"] if prev_vmin is None else min(q["mv_start"], prev_vmin + START_MARGIN_STEPS * step)
        log(f"--- {f} MHz (descending{', ANCHOR' if q['role'] == 'anchor' else ''}): start {start} mV, down to {pl['mv_floor']} mV ---")
        res = search_down(f, start, pl["mv_floor"], step, p)
        RUN["vmin"][str(f)] = {"vmin": res["vmin"], "found": res["found"], "start_mv": start, "role": q["role"]}
        if res["vmin"] is not None:
            prev_vmin = res["vmin"]


def run_target(p, pl, freqs):
    target = pl["target"]["target_ths"]
    fs, step, fstep = p["frequency_step"], pl["mv_step"], pl["freq_step"]
    lo, hi = min(freqs), max(freqs)
    f_t = target * 1000 / GH_PER_MHZ
    f = clamp(round_to(f_t, fs), lo, hi)
    log(f"Target {target} TH/s -> f_target = {target} * 1000 / {GH_PER_MHZ:.2f} = {f_t:.1f} MHz (calibrated) -> start at {f} MHz, "
        f"band {lo}..{hi}, step {fstep} MHz")
    best, direction, prev_found, tried = None, None, None, []
    while lo <= f <= hi and f not in tried:
        tried.append(f)
        q_top = next((x["mv_start"] for x in pl["points"] if x["freq"] == f), pl["mv_stock"])
        if prev_found is None:
            start = pl["mv_floor"]
        else:
            start = max(pl["mv_floor"], prev_found - (step if direction == "hoch" else 2 * step))
        log(f"--- {f} MHz ({({'hoch': 'up', 'runter': 'down'}.get(direction) or 'start')}): Vmin from below from {start} mV ---")
        res = search_up(f, start, pl["mv_floor"], q_top, step, p)
        RUN["vmin"][str(f)] = {"vmin": res["vmin"], "found": res["found"], "start_mv": start}
        if res["rec"] is None:
            log(f"*** {f} MHz cannot be supplied - search ended ***")
            break
        prev_found = res["found"]
        h = res["rec"]["hashrate_local_ths"]
        if h >= target:
            best = res["rec"]
            nf = clamp(round_to(f - fstep, fs), lo, hi)
            if direction == "hoch" or nf == f or nf * GH_PER_MHZ / 1000 < target:
                log(f"    {h:.3f} TH/s >= {target} and next lower frequency {nf} MHz expects "
                    f"{nf * GH_PER_MHZ / 1000:.3f} TH/s {'< target' if nf * GH_PER_MHZ / 1000 < target else ''} -> hit")
                break
            log(f"    {h:.3f} TH/s >= {target} clearly -> frequency DOWN to {nf} MHz")
            direction, f = "runter", nf
        else:
            if direction == "runter":
                log(f"    {h:.3f} TH/s < {target} after lowering -> previous point is the closest >= target")
                break
            nf = clamp(round_to(f + fstep, fs), lo, hi)
            if nf == f:
                log(f"    {h:.3f} TH/s < {target}, band end {hi} MHz reached -> target not reachable in band")
                break
            log(f"    {h:.3f} TH/s < {target} -> frequency UP to {nf} MHz")
            direction, f = "hoch", nf
    RUN["target_result"] = ({k: best.get(k) for k in ("freq", "mv", "hashrate_local_ths", "wall_avg", "jth_local")}
                            if best else None)
    RUN["target_tried_freqs"] = tried
    if best:
        log(f"*** TARGET {target} TH/s: {best['freq']} MHz / {best['mv']} mV -> {best['hashrate_local_ths']:.3f} TH/s, "
            f"{best['wall_avg']:.1f} W, {best['jth_local']:.2f} J/TH (tested frequencies: {tried}) ***")


def run_plan(st, p, d, pl, selected=None, full_measure=False):
    """Sweep nach Zielmodus-Plan (plan_matrix). 'full' nutzt den bisherigen run().
    full_measure: jeder getestete Punkt volles Fenster (gilt fuer efficiency, performance, target_hashrate)."""
    global FULL_MEASURE
    FULL_MEASURE = bool(full_measure)
    RUN["full_measure"] = FULL_MEASURE
    log(f"Measurement depth: {'full_measure - every point gets the full window' if FULL_MEASURE else 'fast - precheck search, full window only for result/anchor/reserve points'}")
    freqs = sorted({q["freq"] for q in pl["points"]})
    if selected:
        freqs = sorted({int(x) for x in selected} & set(freqs))
        if not freqs:
            raise RuntimeError("none of the selected frequencies is in the plan")
    RUN["plan"] = {k: v for k, v in pl.items() if k not in ("points", "formula")}
    RUN["plan_points"] = pl["points"]
    if pl["sweep_mode"] == "full":
        d = dict(d, freqs=freqs, mv_sweep_step=pl["mv_step"])
        return run(st, p, d)
    setup_and_calibrate(st, p, d)
    print()
    print("=" * 80)
    print(f"DIRECTED SEARCH  mode {pl['sweep_mode']}, {len(freqs)} frequencies, step {pl['mv_step']} mV, "
          f"floor {pl['mv_floor']} mV, precheck {PRECHECK_FRAC * 100:.0f}% after {SETTLE_PRECHECK_S}s")
    print("=" * 80)
    {"efficiency": run_efficiency, "performance": run_performance, "target_hashrate": run_target}[pl["sweep_mode"]](p, pl, freqs)
    print()
    print("=" * 80)
    print("OVERVIEW")
    print("=" * 80)
    for q in RUN["points"]:
        print(f"  {q['label']:<22} St.{q.get('stage', '-')}  {_n(q.get('hashrate_local_ths'), 3):>7} TH/s "
              f"{_n(q.get('wall_avg'), 1):>6} W {_n(q.get('jth_local'), 2):>6} J/TH  {_n(q.get('point_s'), 0):>5}s  "
              f"{'VALID' if q.get('valid') else ('precheck ok' if q.get('status') == 'precheck_pass' else 'invalid')} ({msg_text(q.get('reason'))})")
    # Knie-Kurve: grenzwertige Vmin-Punkte (90..97 %) verzerren dW/dT -> je Frequenz der niedrigste
    # gueltige Punkt mit >= KNEE_MIN_FRAC der Erwartung (sonst Vmin)
    vmin_map = {}
    for k, v in RUN["vmin"].items():
        f = int(k)
        solid = sorted(q["mv"] for q in RUN["points"] if q.get("freq") == f and q.get("valid") and q.get("result")
                       and (q.get("frac_of_expected") or 0) >= KNEE_MIN_FRAC)
        if solid:
            vmin_map[f] = solid[0]
        elif v.get("vmin") is not None:
            vmin_map[f] = v["vmin"]
    RUN["knee_curve_mv"] = vmin_map
    # Sweet Spots nur aus den Ergebnispunkten (Vmin/Reserve je Frequenz) - so haengen sie nicht von der
    # Messtiefe (full_measure) der Zwischenpunkte ab
    RUN["sweet_spots"] = sweet_spots([q for q in RUN["points"] if q.get("result")], vmin_map)
    if RUN.get("target_result"):
        RUN["sweet_spots"]["target"] = RUN["target_result"]
    RUN["status"] = "finished"
    publish_live(idx=None, phase="idle")


def restore(p):
    print()
    log("=== WRAP-UP ===")
    orig = p.get("job_interval_ms")
    if STATE["job_changed"] and orig is not None:
        try:
            set_job_interval(orig)
            STATE["job_changed"] = False
        except Exception as e:
            log(f"!!! resetting job interval FAILED: {e}")
    if STATE["wrote_tuning"]:
        try:
            ok, _ = set_point(*D["safe"], p)
            RUN["restored"] = ok
            if STATE["emergency"]:
                log("After emergency shutdown: mining stays OFF (tuning set to start point).")
            else:
                log(f"Reset to start point {D['safe'][0]} MHz / {D['safe'][1]} mV." if ok
                    else "!!! verify after reset NOT confirmed")
        except Exception as e:
            RUN["restored"] = False
            log(f"!!! RESET FAILED: {e}")
    log("Note: save=false was never overridden - a reboot restores the saved state.")


def main():
    ap = argparse.ArgumentParser(description="Full sweep with Vmin search (model independent)")
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--plan-only", action="store_true", help="plan only (default, no write access)")
    g.add_argument("--run", action="store_true", help="run the real sweep (writes to the miner)")
    g.add_argument("--plan", action="store_true", help="goal-mode plan as JSON (read-only)")
    ap.add_argument("--mode", default="full", choices=SWEEP_MODES)
    ap.add_argument("--resolution", default="fein", choices=RESOLUTIONS)
    ap.add_argument("--target-ths", type=float)
    ap.add_argument("--allow-above-stock", action="store_true")
    ap.add_argument("--freq-high-pct", type=float)
    ap.add_argument("--mv-high-pct", type=float)
    ap.add_argument("--full-measure", action="store_true", help="measure every point with the full window")
    args = ap.parse_args()
    if args.plan:
        st, p = read_profile()   # nur GET
        pl = plan_matrix(p, args.mode, args.resolution, args.target_ths, args.allow_above_stock or None,
                         args.freq_high_pct, args.mv_high_pct)
        pl["estimate"] = plan_estimate(pl, p["local_hashrate_difficulty"])
        print(json.dumps(pl, indent=2, ensure_ascii=False))
        return
    mode = "run" if args.run else "plan-only"

    print("---- BEGINN BERICHT ----")
    log(f"sweep_full.py  mode: {mode.upper()}{'  (read-only, no write access)' if mode == 'plan-only' else ''}")
    try:
        st, p = read_profile()
        missing = [k for k in ["stock_frequency_mhz", "stock_core_mv", "frequency_min", "frequency_max",
                               "frequency_step", "core_min", "core_max", "core_step"] if not isinstance(p[k], (int, float))]
        if missing:
            raise RuntimeError(f"profile incomplete: {missing}")
    except Exception as e:
        log(f"ERROR reading the profile: {e}")
        print("---- ENDE BERICHT ----")
        sys.exit(1)
    D.update(derive(p))
    est = print_plan(p, D)

    if mode == "plan-only":
        print()
        log("PLAN-ONLY: done. Nothing was written to the miner.")
        print("---- ENDE BERICHT ----", flush=True)
        return

    RUN["timestamp"] = time.strftime("%Y-%m-%dT%H:%M:%S")
    RUN["plan_estimate"] = est

    def _sigterm(signum, frame):
        raise KeyboardInterrupt(f"Signal {signum}")
    signal.signal(signal.SIGTERM, _sigterm)   # SIGHUP bewusst NICHT: nohup ignoriert es (Terminal-Trennung)
    code = 0
    try:
        if args.mode != "full" or args.resolution != "fein":
            pl = plan_matrix(p, args.mode, args.resolution, args.target_ths, args.allow_above_stock or None,
                             args.freq_high_pct, args.mv_high_pct)
            run_plan(st, p, D, pl, full_measure=args.full_measure)
        else:
            run(st, p, D)
    except SystemExit as e:
        code = e.code if isinstance(e.code, int) else 1
    except Stopped:
        log("Sweep stopped.")
        RUN["status"], code = "stopped", 130
    except base.Deadman as e:
        log(f"ABORT: {e}")
        RUN["status"], code = "deadman", 2
    except KeyboardInterrupt:
        log("Interrupted (Ctrl+C).")
        RUN["status"], code = "interrupted", 130
    except Exception as e:
        log(f"ERROR: {type(e).__name__}: {e}")
        RUN["status"], code = f"error: {e}", 1
    finally:
        restore(p)
        RUN["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S")
        publish_live(idx=None, phase="idle")
        try:
            save_run()
            log(f"Run JSON saved: {RUN_PATH}")
        except Exception as e:
            log(f"!!! Run JSON could not be saved: {e}")
    print("---- ENDE BERICHT ----", flush=True)
    sys.exit(code)


if __name__ == "__main__":
    main()
