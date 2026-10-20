"""Closed-loop test scenarios as spec checks (items 3-8 and 18).

Each function takes a factory ``make(vin, load, en=None, v0=None, il0=None,
v0_in=None, cond=None)`` returning a fresh ``converter.Sim`` for the design
(plant + controller; ``vin`` a number or a function of time; ``cond`` the
stability condition dict, for factories that vary the plant), runs
one scenario, and returns ``report.Check`` objects. The controller model's
``Common.validated_by`` decides the evidence level: with it empty the checks
are INCOMPLETE ("controller model not validated"), whatever the margin.

Scenarios:
- ``regulation``: DC output across the line and load grid (item 3)
- ``stability``: loop gain by injection over line, load and plant corners (4)
- ``load_step``: dip, overshoot, settling and ringing for a stated step (5)
- ``startup_shutdown``: start-up at no load and full load (overshoot, inrush,
  monotonic rise), shutdown and restart (6)
- ``light_load``: pulse skipping, ripple and regulation at light and no load (7)
- ``input_transients``: line steps, a dip to brown-out and recovery (8)
- ``faults``: overload, output short, hiccup and recovery (18)
"""
from __future__ import annotations

import math

import numpy as np

from . import converter as cv
from .report import Param, check, val


def _model(sim) -> dict:
    c = sim.c.c
    return {"controller model": Param(1.0, "", c.validated_by, "controller model")}


def _settle(sim, period, n=150):
    sim.run(sim.t + n * period)
    return sim


def regulation(make, vins: list[float], loads: list[float], *, v_target: Param, tol: Param,
               period: float, inputs: dict) -> list:
    rows, worst = [], 0.0
    sim = None
    for vin in vins:
        for i in loads:
            sim = make(vin, cv.Load.current(i), v0=val(v_target), il0=i)
            _settle(sim, period, 300)
            w = sim.run(sim.t + 60 * period)
            st = cv.steady(w, w.t[0])
            err = st["vload_avg"] / val(v_target) - 1
            rows.append((vin, i, st["vload_avg"]))
            worst = max(worst, abs(err))
    vals = [r[2] for r in rows]
    detail = f"{min(vals):.4f}-{max(vals):.4f} V over Vin {min(vins)}-{max(vins)} V, I {min(loads)}-{max(loads)} A"
    return [check("Closed-loop regulation (line and load)", f"within ±{val(tol):.1%} of {val(v_target):.3f} V",
                  f"{detail}; worst {worst:.2%}", worst <= val(tol), "closed-loop switched simulation",
                  inputs | _model(sim) | {"target": v_target, "tolerance": tol},
                  value=worst, limit=val(tol),
                  missing_model="" if sim.c.c.validated_by else "controller model not validated")]


def stability(make, conditions: list[dict], freqs, amplitude, *, pm_min: Param, gm_min: Param,
              period: float, inputs: dict) -> tuple[list, list]:
    """``conditions``: [{"vin":..., "iout":..., "label":...}, ...]; the factory
    may read extra keys (e.g. a capacitance corner). Returns (checks, per-condition margins)."""
    table = []
    sim = None
    for cond in conditions:
        sim = make(cond["vin"], cv.Load.current(cond["iout"]), v0=cond.get("v0"), il0=cond["iout"], cond=cond)
        _settle(sim, period, 400)
        st = cv.steady(sim.run(sim.t + 20 * period), sim.t - 20 * period)
        if st["il_min"] <= 0.0:
            # pulse skipping: no fixed operating point, small-signal loop gain is not defined
            table.append((cond.get("label", str(cond)) + " [DCM, excluded]",
                          {"fc_hz": None, "phase_margin_deg": None, "gain_margin_db": None, "dcm": True}, []))
            continue
        pts = cv.loop_gain(sim, freqs, amplitude)
        mg = cv.margins(pts)
        table.append((cond.get("label", str(cond)), mg, pts))
    ccm = [(lab, m, p) for lab, m, p in table if not m.get("dcm")]
    pms = [m["phase_margin_deg"] for _, m, _ in ccm]
    gms = [m["gain_margin_db"] for _, m, _ in ccm if m["gain_margin_db"] is not None]
    worst_pm = min((p for p in pms if p is not None), default=None)
    det = "; ".join(lab if m.get("dcm") else
                    f"{lab}: fc {m['fc_hz'] / 1e3 if m['fc_hz'] else float('nan'):.1f} kHz, PM "
                    f"{m['phase_margin_deg'] if m['phase_margin_deg'] is not None else float('nan'):.0f}°"
                    for lab, m, _ in table)
    low = [(lab, p["f"]) for lab, _, pts in ccm for p in pts if not p["snr_ok"]]
    if low:
        det += f"; low-SNR points (|Vx| < ripple): {low}"
    ok_pm = worst_pm is not None and None not in pms and worst_pm >= val(pm_min)
    out = [check("Loop phase margin over corners", f">= {val(pm_min):.0f}°", det, ok_pm if worst_pm is not None else None,
                 "loop gain by series injection in the switched simulation",
                 inputs | _model(sim) | {"PM limit": pm_min}, value=worst_pm, limit=val(pm_min), sense="min",
                 missing_model="" if sim.c.c.validated_by else "controller model not validated",
                 to_complete="" if None not in pms else "a condition had no 0 dB crossing in the swept range")]
    gm_txt = (f"{min(gms):.1f} dB worst" if gms else "phase did not reach -180° in the swept range")
    out.append(check("Loop gain margin over corners", f">= {val(gm_min):.0f} dB", gm_txt,
                     (min(gms) >= val(gm_min)) if gms else None, "loop gain by series injection",
                     inputs | _model(sim) | {"GM limit": gm_min}, value=min(gms) if gms else None,
                     limit=val(gm_min), sense="min",
                     missing_model="" if sim.c.c.validated_by else "controller model not validated",
                     to_complete="" if gms else "extend the sweep towards fsw/2 to find the -180° point"))
    return out, table


def load_step(make, vin: float, i0: float, i1: float, slew: Param, *, v_target: Param, max_dev: Param,
              settle_band: Param, max_settle: Param, period: float, inputs: dict, t_after: float = 200e-6) -> list:
    out = []
    for a, b, tag in ((i0, i1, "up"), (i1, i0, "down")):
        sim = make(vin, cv.Load.step(a, b, 0.0, val(slew)), v0=val(v_target), il0=a)
        # hold the start level while settling: re-arm the step after settling
        sim.load = cv.Load.current(a)
        _settle(sim, period, 300)
        t_ev = sim.t
        sim.load = cv.Load.step(a, b, t_ev, val(slew))
        w = sim.run(t_ev + t_after)
        tr = cv.transient(w, t_ev, None, val(settle_band) * val(v_target), period)
        dev_frac = max(abs(val(v_target) - tr["v_min"]), abs(tr["v_max"] - val(v_target))) / val(v_target)
        zeta = f", ζ≈{tr['zeta']:.2f}" if tr["zeta"] else ""
        out.append(check(f"Load step {a:g}→{b:g} A ({val(slew) / 1e6:g} A/µs)",
                         f"deviation <= {val(max_dev):.1%}, settle <= {val(max_settle) * 1e6:.0f} µs",
                         f"min {tr['v_min']:.3f} V, max {tr['v_max']:.3f} V ({dev_frac:.2%}), settles in "
                         f"{tr['t_settle'] * 1e6:.0f} µs, {tr['ringing_peaks']} peaks{zeta}",
                         dev_frac <= val(max_dev) and tr["t_settle"] <= val(max_settle) and tr["settled"],
                         "closed-loop switched simulation", inputs | _model(sim) |
                         {"deviation limit": max_dev, "settling limit": max_settle, "slew": slew},
                         value=dev_frac, limit=val(max_dev),
                         missing_model="" if sim.c.c.validated_by else "controller model not validated"))
    return out


def startup_shutdown(make, vin: float, *, r_full: float, v_target: Param, max_overshoot: Param,
                     max_inrush: Param, t_run: float, inputs: dict, t_off: float = 300e-6,
                     record_every: int = 4) -> list:
    out = []
    for label, load in (("no load", cv.Load.current(0.0)), ("full load", cv.Load.resistor(r_full))):
        t_restart = t_run + t_off
        en = lambda t, a=t_run, b=t_restart: not (a <= t < b)
        sim = make(vin, load, en=en, v0=0.0, il0=0.0)
        w = sim.run(t_restart + t_run, record_every=record_every)
        m1 = w.window(0, t_run)
        # Monotonic means the output follows the soft-start ramp without dips.
        # Early in soft-start the converter pulse-skips and the output is a
        # staircase between widely spaced pulses, so judge the envelope averaged
        # over 1/20 of the soft-start time, not the switching ripple.
        va = cv.cycle_average(w, w.vload, max(3e-6, sim.c.c.t_ss / 20))
        over = float(w.vload[m1].max() - val(v_target))
        inrush = float(w.isrc[m1].max())
        # monotonic: the averaged rise never falls by more than 1 % of target before regulation
        rise = va[m1]
        reach = np.argmax(rise >= 0.95 * val(v_target)) if (rise >= 0.95 * val(v_target)).any() else len(rise)
        drops = np.maximum.accumulate(rise[:reach]) - rise[:reach]
        mono = bool(len(drops) == 0 or drops.max() < 0.01 * val(v_target))
        m_off = w.window(t_run, t_restart)
        v_end_off = float(w.vload[m_off][-1]) if m_off.any() else float("nan")
        m2 = w.window(t_restart)
        restarted = bool(m2.any() and w.vload[m2][-1] > 0.95 * val(v_target))
        out.append(check(f"Start-up ({label})", f"overshoot <= {val(max_overshoot) * 1e3:.0f} mV, inrush <= "
                         f"{val(max_inrush):.1f} A, monotonic",
                         f"overshoot {over * 1e3:.0f} mV, input peak {inrush:.2f} A, "
                         f"{'monotonic' if mono else 'NON-monotonic'}, reached 95 % in "
                         f"{(w.t[m1][reach] if reach < len(rise) else float('nan')) * 1e3:.2f} ms",
                         over <= val(max_overshoot) and inrush <= val(max_inrush) and mono,
                         "closed-loop switched simulation (soft-start)", inputs | _model(sim) |
                         {"overshoot limit": max_overshoot, "inrush limit": max_inrush},
                         value=over, limit=val(max_overshoot),
                         missing_model="" if sim.c.c.validated_by else "controller model not validated"))
        out.append(check(f"Shutdown and restart ({label})", "output decays when disabled; restarts on EN",
                         f"V_load {v_end_off:.2f} V after {t_off * 1e6:.0f} µs off; "
                         f"{'restarted' if restarted else 'did NOT restart'}; events {[e[1] for e in w.events]}",
                         restarted, "closed-loop switched simulation (EN toggled)", inputs | _model(sim),
                         missing_model="" if sim.c.c.validated_by else "controller model not validated"))
    return out


def light_load(make, vin: float, loads: list[float], *, v_target: Param, tol: Param, ripple_max: Param,
               period: float, inputs: dict, f_audio: float = 20e3, t_run: float = 1.5e-3) -> list:
    rows = []
    sim = None
    for i in loads:
        sim = make(vin, cv.Load.current(i), v0=val(v_target), il0=i)
        sim.run(sim.t + t_run / 2)
        w = sim.run(sim.t + t_run / 2)
        st = cv.steady(w, w.t[0])
        rows.append((i, st))
    worst_err = max(abs(st["vload_avg"] / val(v_target) - 1) for _, st in rows)
    worst_rip = max(st["vload_pp"] for _, st in rows)
    audio = [i for i, st in rows if 0 < st["pulse_hz"] < f_audio]
    det = "; ".join(f"{i * 1e3:g} mA: {st['vload_avg']:.3f} V, {st['vload_pp'] * 1e3:.1f} mVpp, "
                    f"{st['pulse_hz'] / 1e3:.1f} kHz pulses" for i, st in rows)
    miss = "" if sim.c.c.validated_by else "controller model not validated"
    return [check("Light-load regulation", f"within ±{val(tol):.1%}", det + f"; worst {worst_err:.2%}",
                  worst_err <= val(tol), "closed-loop switched simulation", inputs | _model(sim) | {"tolerance": tol},
                  value=worst_err, limit=val(tol), missing_model=miss),
            check("Light-load ripple", f"<= {val(ripple_max) * 1e3:.0f} mVpp", f"{worst_rip * 1e3:.1f} mVpp worst"
                  + (f"; pulse rate in the audio band at {audio} A" if audio else ""),
                  worst_rip <= val(ripple_max), "closed-loop switched simulation",
                  inputs | _model(sim) | {"ripple limit": ripple_max}, value=worst_rip, limit=val(ripple_max),
                  missing_model=miss)]


def input_transients(make, *, v_nom: float, v_lo: float, v_hi: float, iout: float, v_target: Param,
                     max_dev: Param, slew_v_per_s: float, dip_to: float, dip_len: float, period: float,
                     inputs: dict, uvlo_off: float | None = None) -> list:
    out = []
    miss = None
    for a, b in ((v_nom, v_hi), (v_hi, v_lo), (v_lo, v_nom)):
        t_ev = 300 * period
        fn = cv.ramp(a, b, t_ev, t_ev + abs(b - a) / slew_v_per_s)
        sim = make(fn, cv.Load.current(iout), v0=val(v_target), il0=iout, v0_in=a)
        w = sim.run(t_ev + 250e-6)
        tr = cv.transient(w, t_ev, val(v_target), 0.01 * val(v_target), period)
        dev = max(abs(tr["v_min"] - val(v_target)), abs(tr["v_max"] - val(v_target))) / val(v_target)
        miss = "" if sim.c.c.validated_by else "controller model not validated"
        out.append(check(f"Line step {a:g}→{b:g} V ({slew_v_per_s / 1e6:g} V/µs)", f"deviation <= {val(max_dev):.1%}",
                         f"min {tr['v_min']:.3f} V, max {tr['v_max']:.3f} V ({dev:.2%})", dev <= val(max_dev),
                         "closed-loop switched simulation", inputs | _model(sim) | {"deviation limit": max_dev},
                         value=dev, limit=val(max_dev), missing_model=miss))
    # dip / brown-out and recovery
    t0 = 300 * period
    fn = cv.piecewise([(0, v_nom), (t0, v_nom), (t0 + 20e-6, dip_to), (t0 + 20e-6 + dip_len, dip_to),
                       (t0 + 40e-6 + dip_len, v_nom)])
    sim = make(fn, cv.Load.current(iout), v0=val(v_target), il0=iout, v0_in=v_nom)
    w = sim.run(t0 + dip_len + 2e-3, record_every=4)
    m = w.window(t0 + dip_len + 1.9e-3)
    recovered = bool(abs(w.vload[m].mean() / val(v_target) - 1) < 0.01)
    ev = [e[1] for e in w.events]
    out.append(check(f"Input dip to {dip_to:g} V for {dip_len * 1e6:.0f} µs and recovery",
                     "recovers to regulation (restart through soft-start if UVLO trips)",
                     f"{'recovered' if recovered else 'did NOT recover'}; min V_load {w.vload.min():.2f} V; events {ev}",
                     recovered, "closed-loop switched simulation", inputs | _model(sim),
                     missing_model=miss))
    return out


def faults(make, vin: float, *, i_overload: float, r_short: float, i_sat: Param, v_target: Param,
           period: float, inputs: dict, t_fault: float = 1.5e-3, t_after: float = 3e-3) -> list:
    out = []
    miss = None
    for label, fault in (("overload", cv.Load.current(i_overload)), ("output short", cv.Load.resistor(r_short))):
        sim = make(vin, cv.Load.current(0.5), v0=val(v_target), il0=0.5)
        _settle(sim, period, 200)
        t_ev = sim.t
        sim.load = fault
        w1 = sim.run(t_ev + t_fault, record_every=2)
        i_pk = float(w1.il.max())
        sim.load = cv.Load.current(0.5)
        w2 = sim.run(sim.t + t_after, record_every=4)
        m = w2.window(w2.t[-1] - 100e-6)
        rec = bool(abs(w2.vload[m].mean() / val(v_target) - 1) < 0.01)
        ev = [e[1] for e in w1.events if e[0] >= t_ev] + [e[1] for e in w2.events if e[0] > w1.t[-1]]
        miss = "" if sim.c.c.validated_by else "controller model not validated"
        out.append(check(f"{label.capitalize()} response", f"inductor peak <= Isat {val(i_sat):.1f} A; recovers",
                         f"peak I_L {i_pk:.2f} A; protection events {ev[:6]}; "
                         f"{'recovered' if rec else 'did NOT recover'} after removal",
                         i_pk <= val(i_sat) and rec, "closed-loop switched simulation",
                         inputs | _model(sim) | {"Isat": i_sat}, value=i_pk, limit=val(i_sat),
                         missing_model=miss))
    return out
