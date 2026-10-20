"""Source, cable and input-filter interaction with the converter.

A regulating converter draws constant power, so inside its loop bandwidth its
input looks like a negative resistance, R_in = -Vin^2 / Pin. The source network
(supply output impedance, cable R and L, any filter, the input capacitors)
forms a resonant circuit in front of it. Two checks follow:

- Middlebrook: the source network's output impedance must stay well below
  |R_in| at every frequency up to the loop crossover (margin in dB);
- hot-plug: connecting a charged source through cable inductance rings the
  input capacitors up to nearly twice the supply; the peak must stay below the
  IC's VIN absolute maximum.

Impedances are evaluated analytically; ``converter.Sim`` reproduces both in the
time domain (an input step with the controller running) as a cross-check.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .report import Param, check, val


@dataclass
class Branch:
    """Series R-L-C branch to ground (a capacitor bank or a damping leg).
    ``c`` None means no capacitor (an R-L leg)."""
    r: float
    l: float = 0.0
    c: float | None = None

    def z(self, f):
        w = 2j * np.pi * f
        z = self.r + w * self.l
        return z + (1 / (w * self.c) if self.c else 0)


@dataclass
class SourceNetwork:
    r_series: float                  # supply output resistance + cable/connector R
    l_series: float                  # cable and connector inductance
    shunt: list[Branch] = field(default_factory=list)   # input caps, damping legs at the converter
    filter_stages: list[tuple[float, float, list[Branch]]] = field(default_factory=list)
    # each extra stage: (series R, series L, shunt branches), source side first

    def z_out(self, f):
        """Impedance seen from the converter's input terminals."""
        f = np.asarray(f, float)
        w = 2j * np.pi * f
        z = np.zeros_like(w)                                     # ideal source
        stages = self.filter_stages + [(self.r_series, self.l_series, self.shunt)]
        for rs, ls, sh in stages:
            z = z + rs + w * ls
            for b in sh:
                z = 1 / (1 / z + 1 / b.z(f))
        return z


def middlebrook(net: SourceNetwork, *, vin_min: float, p_in_max: float, f_max: float,
                f_min: float = 100.0, points: int = 400) -> dict:
    """Margin |R_in| / |Z_out| (dB) from ``f_min`` to ``f_max`` (the loop
    crossover). Returns the worst margin, where it occurs, and the peak of
    |Z_out| (the network resonance) with its Q against the series resistance."""
    f = np.logspace(math.log10(f_min), math.log10(f_max), points)
    zo = np.abs(net.z_out(f))
    rin = vin_min ** 2 / p_in_max
    margin = 20 * np.log10(rin / zo)
    i = int(np.argmin(margin))
    k = int(np.argmax(zo))
    f_res = float(f[k])
    return {"r_in_ohm": rin, "min_margin_db": float(margin[i]), "at_hz": float(f[i]),
            "z_peak_ohm": float(zo[k]), "f_peak_hz": f_res,
            "peak_at_edge": k in (0, len(f) - 1)}


def hot_plug(v_supply: float, r: float, l: float, c: float, esr: float = 0.0,
             t_end: float | None = None, n: int = 20000) -> dict:
    """Peak input voltage when a charged ``v_supply`` is connected through
    R-L into an uncharged capacitor C (with ESR). Exact step response of the
    series RLC (capacitor node voltage including the ESR drop)."""
    rt = r + esr
    w0 = 1 / math.sqrt(l * c)
    zeta = rt / 2 * math.sqrt(c / l)
    t_end = t_end or 10 * 2 * math.pi / w0
    t = np.linspace(0, t_end, n)
    if zeta < 1:
        wd = w0 * math.sqrt(1 - zeta ** 2)
        i = v_supply / (l * wd) * np.exp(-zeta * w0 * t) * np.sin(wd * t)
    elif zeta == 1:
        i = v_supply / l * t * np.exp(-w0 * t)
    else:
        s1 = -w0 * (zeta - math.sqrt(zeta ** 2 - 1))
        s2 = -w0 * (zeta + math.sqrt(zeta ** 2 - 1))
        i = v_supply / (l * (s1 - s2)) * (np.exp(s1 * t) - np.exp(s2 * t))
    vc = np.concatenate([[0.0], np.cumsum((i[1:] + i[:-1]) / 2 * np.diff(t))]) / c
    v_node = vc + i * esr
    return {"v_peak": float(v_node.max()), "zeta": zeta, "f_ring_hz": w0 / (2 * math.pi),
            "i_peak": float(i.max())}


def checks(net: SourceNetwork, *, vin_min: Param, vin_max: Param, p_in_max: Param, f_cross: Param,
           margin_min: Param, vin_abs_max: Param, cable_l: Param, cable_r: Param,
           c_in: Param, esr_in: Param) -> list:
    inputs = {"cable L": cable_l, "cable R": cable_r, "input capacitance": c_in, "input ESR": esr_in}
    mb = middlebrook(net, vin_min=val(vin_min), p_in_max=val(p_in_max), f_max=val(f_cross))
    out = [check("Input network vs converter input impedance (Middlebrook)",
                 f">= {val(margin_min):.0f} dB up to {val(f_cross) / 1e3:.0f} kHz",
                 f"{mb['min_margin_db']:.1f} dB at {mb['at_hz'] / 1e3:.1f} kHz "
                 f"(|R_in| {mb['r_in_ohm']:.2f} ohm, |Z_out| peak {mb['z_peak_ohm']:.3f} ohm at {mb['f_peak_hz'] / 1e3:.1f} kHz)",
                 mb["min_margin_db"] >= val(margin_min), "analytic |Z_out| of the source network vs Vin^2/Pin",
                 inputs | {"Vin min": vin_min, "Pin max": p_in_max, "loop crossover": f_cross,
                           "required margin": margin_min},
                 value=mb["min_margin_db"], limit=val(margin_min), sense="min",
                 lever="design: damping leg (R-C) across the input, or more bulk capacitance with ESR")]
    hp = hot_plug(val(vin_max), val(cable_r), val(cable_l), val(c_in), val(esr_in))
    out.append(check("Hot-plug input overshoot", f"< {val(vin_abs_max):.1f} V (VIN abs max)",
                     f"{hp['v_peak']:.2f} V peak from {val(vin_max):.1f} V (zeta {hp['zeta']:.2f}, "
                     f"{hp['f_ring_hz'] / 1e3:.0f} kHz)", hp["v_peak"] < val(vin_abs_max),
                     "series RLC step response", inputs | {"Vin max": vin_max, "VIN abs max": vin_abs_max},
                     value=hp["v_peak"], limit=val(vin_abs_max),
                     lever="design: bulk electrolytic (ESR damping) or TVS at the input"))
    return out
