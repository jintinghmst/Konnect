"""Voltage stress on the switch node and the IC pins, from an edge-resolved run.

``buck.BuckTestbench.run_edge`` resolves the switching edges and the hot-loop
ringing. This module measures, from those waveforms, what the datasheet's
absolute-maximum table constrains:

- SW positive overshoot and negative undershoot, and how long SW stays beyond
  each limit (datasheets often allow a transient level for a stated time,
  e.g. "-3 V for 10 ns");
- the IC's local VIN pin (input capacitors plus hot-loop ringing);
- VBST to SW (bootstrap capacitor voltage) and VBST to GND.

The ringing amplitude depends on the edge rate and on the loop's damping, so
both must be sourced: the edge time from the datasheet or a measurement, the
damping from ``skin_resistance`` (copper at the ring frequency) plus the
decoupling capacitor's ESR at that frequency. With either assumed, the result
is reported but the check stays INCOMPLETE.
"""
from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np

from .layout import ALPHA_CU, MU0, RHO_CU_20C
from .report import Param, check, val


def skin_resistance(r_dc: float, thickness_mm: float, f_hz: float, temp_c: float = 25.0,
                    one_sided: bool = True) -> float:
    """AC resistance of a flat conductor at ``f_hz``. Over a plane the current
    flows on the face towards the plane (``one_sided``), in a depth delta, so
    R_ac = R_dc * t / delta once delta < t (t / 2 delta when both faces carry it)."""
    rho = RHO_CU_20C * (1 + ALPHA_CU * (temp_c - 20))
    delta = math.sqrt(rho / (math.pi * f_hz * MU0)) * 1e3          # mm
    t = thickness_mm if one_sided else thickness_mm / 2
    return r_dc * max(t / delta, 1.0)


@dataclass
class Limit:
    """One absolute-maximum entry: ``v`` with sense "max" (must stay below) or
    "min" (must stay above); ``duration_s`` None means never, otherwise the
    level may be exceeded for at most that long per event."""
    name: str
    v: Param
    sense: str = "max"
    duration_s: Param | None = None


def excursions(t: np.ndarray, v: np.ndarray, level: float, sense: str) -> dict:
    """Peak beyond ``level`` and the longest continuous time beyond it."""
    beyond = v > level if sense == "max" else v < level
    longest, start = 0.0, None
    for i, b in enumerate(beyond):
        if b and start is None:
            start = t[i]
        if (not b or i == len(beyond) - 1) and start is not None:
            longest = max(longest, t[i] - start)
            start = None
    peak = float(v.max() if sense == "max" else v.min())
    return {"peak": peak, "longest_s": longest, "count": int(np.sum(np.diff(beyond.astype(int)) == 1))}


def stress_checks(edge: dict, limits: dict[str, list[Limit]], inputs: dict) -> list:
    """``edge``: the dict returned by ``run_edge`` (its ``waves``); ``limits``:
    {"sw": [...], "vic": [...], "vbst_sw": [...], "vbst": [...]};
    ``inputs``: the provenance of everything the waveform depends on (edge
    time, hot-loop L, damping, capacitor ESR/ESL) for the status rule."""
    w = edge["waves"]
    series = {"sw": w["sw"], "vic": w["vic"]}
    if "vbst" in w:
        series["vbst"] = w["vbst"]
        series["vbst_sw"] = w["vbst"] - w["sw"]
    out = []
    for key, lims in limits.items():
        if key not in series:
            out.append(check(f"{key} stress", "modelled", "not in the testbench", None, "SPICE edge run",
                             inputs, missing_model=f"{key} node (enable the bootstrap model)"))
            continue
        for lim in lims:
            ex = excursions(w["t"], series[key], val(lim.v), lim.sense)
            if lim.duration_s is None:
                ok = ex["longest_s"] == 0.0
                req = f"{'<' if lim.sense == 'max' else '>'} {val(lim.v):g} V ({lim.name})"
                res = f"peak {ex['peak']:.2f} V" + ("" if ok else f", beyond for {ex['longest_s'] * 1e9:.1f} ns")
            else:
                ok = ex["longest_s"] <= val(lim.duration_s)
                req = (f"beyond {val(lim.v):g} V for <= {val(lim.duration_s) * 1e9:.0f} ns ({lim.name})")
                res = f"peak {ex['peak']:.2f} V, longest {ex['longest_s'] * 1e9:.1f} ns beyond"
            out.append(check(f"{key.upper()} {lim.name}", req, res, ok, "SPICE edge-resolved transient",
                             inputs | {f"{lim.name} limit": lim.v, f"{lim.name} duration": lim.duration_s},
                             value=ex["peak"], limit=val(lim.v), sense=lim.sense, layout=True,
                             lever="layout: smaller hot loop; design: slower edge / snubber"))
    return out
