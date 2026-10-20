"""EMI pre-compliance estimates: conducted emissions and switch-node coupling.

Conducted (differential mode): the converter's input-current waveform from a
``converter.Sim`` run (steady state, source impedance set to the LISN's) is
transformed to harmonics, and the voltage across the LISN's 50 ohm port is
I(f) * (50 || j w L_lisn). The levels are harmonic amplitudes, not a CISPR 16
receiver (no 9 kHz RBW, no quasi-peak or average detector): treat a margin of
less than ~6 dB as a fail-risk, not a pass. The switched model's edges are
instantaneous, so levels above ~1/(pi t_rise) are over-estimated.

Common mode: switch-node dV/dt into the capacitance from SW copper to the
chassis/reference plane; that capacitance is a measured or extracted input
(typically pF), so without it the check is INCOMPLETE.

Switch-node to feedback coupling (capacitive): mutual capacitance per unit
length of two conductors over a ground plane from the inductance matrix of
wires above ground (C = mu eps L^-1), integrated over every parallel run of SW
and FB copper on the same layer; the coupled noise at the FB pin is
C_m * dV/dt * R_th(FB), compared with an allowed fraction of the reference.

Limit lines (dBuV vs frequency) must be supplied from the standard with its
citation, never typed from memory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .layout import MU0, Board
from .report import Param, check, val

EPS0 = 8.8541878128e-12


def lisn_port_voltage(i_spectrum: np.ndarray, f: np.ndarray, l_lisn: float = 50e-6,
                      r_port: float = 50.0) -> np.ndarray:
    """Voltage across the LISN measurement port for current harmonics."""
    zl = 2j * np.pi * f * l_lisn
    return i_spectrum * (zl * r_port / (zl + r_port))


def harmonics(t: np.ndarray, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Single-sided harmonic amplitudes (peak) of ``x`` over a window of whole
    periods (resampled uniformly)."""
    n = len(t)
    tu = np.linspace(t[0], t[-1], n, endpoint=False)
    xu = np.interp(tu, t, x)
    spec = np.fft.rfft(xu - xu.mean()) / n * 2
    f = np.fft.rfftfreq(n, tu[1] - tu[0])
    return f[1:], np.abs(spec[1:])


def dbuv(v: np.ndarray) -> np.ndarray:
    return 20 * np.log10(np.maximum(v / math.sqrt(2), 1e-12) / 1e-6)   # rms


def conducted(w, t0: float, t1: float, l_lisn: float = 50e-6, f_lo: float = 150e3,
              f_hi: float = 30e6) -> dict:
    """Differential-mode conducted spectrum at the LISN port from a Waves run
    whose source impedance is the LISN's."""
    m = w.window(t0, t1)
    f, a = harmonics(w.t[m], w.isrc[m])
    keep = (f >= f_lo) & (f <= f_hi)
    v = lisn_port_voltage(a[keep], f[keep], l_lisn)
    return {"f": f[keep], "dbuv": dbuv(np.abs(v))}


def limit_margin(spec: dict, limit_points: list[tuple[float, float]]) -> dict:
    """Worst margin (dB) of a spectrum against a limit line given as
    [(f_hz, dBuV), ...], interpolated linearly in log f (steps given as two
    points at the same frequency are honoured)."""
    lf = np.log10([p[0] for p in limit_points])
    lv = np.array([p[1] for p in limit_points])
    f, v = spec["f"], spec["dbuv"]
    inside = (f >= 10 ** lf[0]) & (f <= 10 ** lf[-1])
    lim = np.interp(np.log10(f[inside]), lf, lv)
    margin = lim - v[inside]
    i = int(np.argmin(margin))
    return {"margin_db": float(margin[i]), "at_hz": float(f[inside][i]), "level_dbuv": float(v[inside][i]),
            "limit_dbuv": float(lim[i])}


def common_mode(t: np.ndarray, vsw: np.ndarray, c_cm: float, f_lo=150e3, f_hi=30e6) -> dict:
    """CM current through ``c_cm`` driven by SW, measured on two LISNs in
    parallel (25 ohm)."""
    f, a = harmonics(t, vsw)
    keep = (f >= f_lo) & (f <= f_hi)
    i_cm = 2 * np.pi * f[keep] * c_cm * a[keep]
    return {"f": f[keep], "dbuv": dbuv(i_cm * 25.0)}


def coupled_c_per_m(w1_mm, w2_mm, s_mm, h_mm, er_eff) -> float:
    """Mutual capacitance per metre of two conductors (widths w1, w2,
    centre spacing s) at height h over a ground plane (wires-above-ground
    model, equivalent radius w/4)."""
    r1, r2 = w1_mm / 4, w2_mm / 4
    l11 = MU0 / (2 * math.pi) * math.log(2 * h_mm / r1)
    l22 = MU0 / (2 * math.pi) * math.log(2 * h_mm / r2)
    l12 = MU0 / (4 * math.pi) * math.log(1 + (2 * h_mm / s_mm) ** 2)
    det = l11 * l22 - l12 ** 2
    return MU0 * EPS0 * er_eff * l12 / det


def sw_fb_coupling(board: Board, sw_net: str, fb_net: str, h_mm: float, er_eff: float,
                   max_sep_mm: float = 5.0) -> dict:
    """Total mutual capacitance between ``sw_net`` and ``fb_net`` tracks on the
    same layer (parallel overlap of each segment pair, closer than
    ``max_sep_mm``). Pads and pours of SW are not included: add them as an
    explicit input when they sit next to the FB trace."""
    sw = board.net_segments(sw_net)
    fb = board.net_segments(fb_net)
    total, runs = 0.0, []
    for a in sw:
        for b in fb:
            if a.layer != b.layer or a.length == 0 or b.length == 0:
                continue
            ua = np.array([a.x2 - a.x1, a.y2 - a.y1]) / a.length
            ub = np.array([b.x2 - b.x1, b.y2 - b.y1]) / b.length
            if abs(abs(ua @ ub) - 1) > 0.02:              # not parallel
                continue
            # project b onto a's axis
            pa0 = 0.0
            pb = sorted([(np.array([b.x1 - a.x1, b.y1 - a.y1]) @ ua), (np.array([b.x2 - a.x1, b.y2 - a.y1]) @ ua)])
            ov = min(a.length, pb[1]) - max(pa0, pb[0])
            if ov <= 0:
                continue
            nrm = np.array([-ua[1], ua[0]])
            sep = abs(np.array([b.x1 - a.x1, b.y1 - a.y1]) @ nrm)
            if sep > max_sep_mm or sep <= (a.width + b.width) / 2:
                continue
            c = coupled_c_per_m(a.width, b.width, sep, h_mm, er_eff) * ov * 1e-3
            total += c
            runs.append({"layer": a.layer, "overlap_mm": round(ov, 2), "sep_mm": round(sep, 2), "c_f": c})
    return {"c_mutual_f": total, "runs": runs}


def fb_noise_check(c_m: Param, dv: Param, t_edge: Param, r_top: float, r_bot: float,
                   allowed_v: Param, c_fb: Param | None = None) -> object:
    """Peak noise injected at FB by an SW edge through ``c_m``. The coupled
    current C_m dV/dt charges the FB node (its pin and trace capacitance
    ``c_fb``, plus C_m) while the divider's Thevenin resistance discharges it:
    V = C_m dV/dt R_th (1 - exp(-t_edge / tau)), tau = R_th (C_m + C_fb). For a
    fast edge this tends to the charge-divider limit dV C_m / (C_m + C_fb)."""
    r_th = r_top * r_bot / (r_top + r_bot)
    cfb = val(c_fb) if c_fb is not None else 0.0
    tau = (val(c_m) + cfb) * r_th
    v = val(c_m) * val(dv) / val(t_edge) * r_th * (1 - math.exp(-val(t_edge) / tau)) if tau > 0 else 0.0
    return check("SW to FB capacitive coupling", f"<= {val(allowed_v) * 1e3:.1f} mV at FB",
                 f"{v * 1e3:.2f} mV ({val(c_m) * 1e15:.1f} fF into {cfb * 1e12:.1f} pF FB node, "
                 f"{val(dv):.1f} V in {val(t_edge) * 1e9:.1f} ns, R_th {r_th / 1e3:.1f} kohm)", v <= val(allowed_v),
                 "C_m dV/dt R_th, FB node capacitance and edge duration",
                 {"mutual capacitance": c_m, "SW swing": dv, "edge time": t_edge, "allowed noise": allowed_v,
                  "FB node capacitance": c_fb if c_fb is not None else Param(0.0, "F", "", "FB node C (not given)")},
                 value=v, limit=val(allowed_v), layout=True,
                 lever="layout: move FB away from SW, guard with ground, lower divider impedance")
