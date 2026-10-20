"""Inductor and capacitor suitability from manufacturer data.

Every number comes in as a ``report.Param`` with its source (datasheet page,
manufacturer curve or characterisation tool). A missing curve is not filled
in: the check that needs it comes back INCOMPLETE and says which curve.

Inductor checks: saturation margin at the worst-case peak current, RMS current
against the rating, copper (DCR at temperature) and core loss, temperature rise
scaled from the rating's own definition, and inductance reduction with current.

Capacitor checks: effective capacitance (tolerance, DC bias, temperature),
RMS ripple current per part (harmonic share among parallel parts by
impedance), self-heating, and applied voltage against the rating and the
derating policy.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .layout import ALPHA_CU
from .report import Check, Param, check, val

COPPER_TEMPCO = Param(ALPHA_CU, "1/K", "copper resistivity temperature coefficient (physical constant)")


def _interp(table, x):
    xs, ys = zip(*sorted(table))
    return float(np.interp(x, xs, ys))


# ── Inductors ──────────────────────────────────────────────────────────────

@dataclass
class Inductor:
    mpn: str
    l_nom: Param                       # H
    tol: Param                         # fraction, e.g. 0.2
    dcr_typ: Param                     # ohm at the datasheet reference temperature
    dcr_max: Param | None = None       # ohm
    dcr_ref_c: float = 20.0
    isat: Param | None = None          # A; what the rating means goes in ``isat.name``
    irms: Param | None = None          # A, the current that gives ``irms_rise``
    irms_rise: Param | None = None     # K temperature rise defining ``irms``
    l_vs_i: list[tuple[float, float]] | None = None   # (A, H) manufacturer curve
    l_vs_i_source: str = ""
    # core loss: either a Steinmetz fit P = k * f^a * B^b scaled through dI,
    # P = k * f^a * dIpp^b (vendor fits are often given this way), or a table
    # of (f_hz, dI_pp_a, watts) points from the vendor's loss calculator.
    core_steinmetz: tuple[float, float, float] | None = None
    core_table: list[tuple[float, float, float]] | None = None
    core_source: str = ""

    def dcr_at(self, temp_c: float, worst: bool = True) -> Param:
        base = self.dcr_max if worst and self.dcr_max is not None else self.dcr_typ
        r = val(base) * (1 + ALPHA_CU * (temp_c - self.dcr_ref_c))
        return Param(r, "ohm", base.source if isinstance(base, Param) else "", "DCR at temperature")

    def l_at(self, current_a: float) -> Param:
        """Inductance at ``current_a`` from the manufacturer's L(I) curve,
        nominal times (1 - tolerance) for the worst unit."""
        lo = 1 - val(self.tol)
        if self.l_vs_i:
            ratio = _interp(self.l_vs_i, current_a) / _interp(self.l_vs_i, 0.0)
            return Param(val(self.l_nom) * lo * ratio, "H",
                         self.l_vs_i_source if self.l_nom.sourced else "", "L(I) worst")
        return Param(val(self.l_nom) * lo, "H", "", "L(I) worst (no L vs I curve)")

    def core_loss(self, fsw: float, di_pp: float) -> Param | None:
        if self.core_steinmetz:
            k, a, b = self.core_steinmetz
            return Param(k * fsw ** a * di_pp ** b, "W", self.core_source, "core loss")
        if self.core_table:
            pts = np.array(self.core_table)
            # nearest frequency row, then interpolate in dI on a log-log scale
            f_near = pts[np.argmin(np.abs(np.log(pts[:, 0] / fsw))), 0]
            row = pts[pts[:, 0] == f_near]
            row = row[np.argsort(row[:, 1])]
            p = float(np.exp(np.interp(np.log(di_pp), np.log(row[:, 1]), np.log(row[:, 2]))))
            src = self.core_source if abs(math.log(f_near / fsw)) < 0.2 else ""
            return Param(p, "W", src, f"core loss (table row {f_near:.0f} Hz)")
        return None


def inductor_checks(ind: Inductor, *, i_dc: float, di_pp: float, i_peak_worst: Param,
                    fsw: float, ambient_c: Param, max_temp_c: Param | None = None,
                    hot_c: float | None = None, l_min_needed: Param | None = None) -> list[Check]:
    """``i_peak_worst`` is the highest current the inductor can see in normal
    operation and at the current limit (e.g. valley limit max + ripple max),
    with its own provenance."""
    out = []
    t_hot = hot_c if hot_c is not None else val(ambient_c) + (val(ind.irms_rise) if ind.irms_rise else 40)
    # saturation
    if ind.isat is None:
        out.append(check("Inductor saturation margin", "I_peak <= Isat", "no Isat rating", None,
                         "datasheet", {"Isat": None}, missing_model="inductor Isat rating",
                         to_complete=f"Isat of {ind.mpn} from the datasheet"))
    else:
        ok = val(i_peak_worst) <= val(ind.isat)
        out.append(check("Inductor saturation margin", f"I_peak <= Isat {val(ind.isat):.2f} A ({ind.isat.name})",
                         f"{val(i_peak_worst):.2f} A worst peak ({(1 - val(i_peak_worst) / val(ind.isat)):+.0%} margin)",
                         ok, "worst-case peak vs rating", {"Isat": ind.isat, "peak current": i_peak_worst},
                         value=val(i_peak_worst), limit=val(ind.isat), lever="design: higher-Isat inductor or larger L"))
    # inductance reduction with current
    l_pk = ind.l_at(val(i_peak_worst))
    need = val(l_min_needed) if l_min_needed is not None else None
    out.append(check("Inductance at peak current", f">= {need * 1e6:.2f} uH" if need else "reported",
                     f"{val(l_pk) * 1e6:.2f} uH at {val(i_peak_worst):.2f} A (worst unit)",
                     (val(l_pk) >= need) if need else True,
                     "manufacturer L(I) curve x (1 - tolerance)",
                     {"L(I) curve": l_pk, "tolerance": ind.tol, "required L": l_min_needed},
                     value=val(l_pk), limit=need, sense="min",
                     to_complete="" if ind.l_vs_i else f"L vs I curve of {ind.mpn}"))
    # RMS current
    irms = math.sqrt(i_dc ** 2 + di_pp ** 2 / 12)
    if ind.irms is None:
        out.append(check("Inductor RMS current", "I_rms <= rating", f"{irms:.2f} A", None, "datasheet",
                         {}, missing_model="inductor Irms rating"))
    else:
        out.append(check("Inductor RMS current", f"<= {val(ind.irms):.2f} A ({val(ind.irms_rise) if ind.irms_rise else '?'} K rise rating)",
                         f"{irms:.2f} A", irms <= val(ind.irms), "I_rms = sqrt(Idc^2 + dI^2/12)",
                         {"Irms rating": ind.irms}, value=irms, limit=val(ind.irms)))
    # losses and temperature
    dcr = ind.dcr_at(t_hot)
    p_cu = irms ** 2 * val(dcr)
    core = ind.core_loss(fsw, di_pp)
    p_tot = p_cu + (val(core) if core else 0.0)
    out.append(check("Inductor losses", "reported", f"copper {p_cu * 1e3:.0f} mW + core "
                     + (f"{val(core) * 1e3:.0f} mW" if core else "not modelled"), True if core else None,
                     "I_rms^2 DCR(T) + vendor core-loss model", {"DCR": dcr, "core loss": core,
                                                                  "DCR tempco": COPPER_TEMPCO},
                     missing_model="" if core else f"core-loss data for {ind.mpn}", value=p_tot))
    if ind.irms and ind.irms_rise:
        p_rated = val(ind.irms) ** 2 * val(ind.dcr_typ) * (1 + ALPHA_CU * (val(ambient_c) + val(ind.irms_rise) / 2 - ind.dcr_ref_c))
        rise = val(ind.irms_rise) * p_tot / p_rated
        t = val(ambient_c) + rise
        lim = val(max_temp_c) if max_temp_c is not None else None
        out.append(check("Inductor temperature", f"<= {lim:.0f} °C" if lim else "reported",
                         f"{t:.0f} °C ({rise:.0f} K rise at {val(ambient_c):.0f} °C ambient)",
                         (t <= lim) if lim else True, "rise scaled from the Irms rating's loss (Ploss / Ploss_rated)",
                         {"Irms rating": ind.irms, "rating rise": ind.irms_rise, "ambient": ambient_c,
                          "max temperature": max_temp_c, "core loss": core},
                         missing_model="" if core else "core loss (rise excludes it)",
                         value=t, limit=lim))
    return out


# ── Capacitors ─────────────────────────────────────────────────────────────

@dataclass
class Capacitor:
    mpn: str
    c_nom: Param                     # F
    tol: Param                       # fraction
    v_rating: Param                  # V
    count: int = 1
    dc_bias: list[tuple[float, float]] | None = None   # (V, C/C_nom) manufacturer curve
    dc_bias_source: str = ""
    temp_coef: Param | None = None   # worst fractional change over the use range (X5R: 0.15)
    esr: Param | list[tuple[float, float]] | None = None   # ohm, or (Hz, ohm) curve
    esr_source: str = ""
    esl: Param | None = None         # H
    ripple_rating: Param | None = None        # A rms for ``ripple_rise`` (vendor)
    ripple_rise: Param | None = None          # K
    heat_table: list[tuple[float, float]] | None = None    # (A rms, K rise) vendor curve
    heat_source: str = ""

    def esr_at(self, f: float) -> Param | None:
        if isinstance(self.esr, list):
            fs, rs = zip(*sorted(self.esr))
            r = float(np.exp(np.interp(np.log(f), np.log(fs), np.log(rs))))
            return Param(r, "ohm", self.esr_source, f"ESR at {f:.3g} Hz")
        return self.esr

    def c_eff(self, v_dc: float, worst: bool = True) -> Param:
        """Per-part effective capacitance at ``v_dc``: tolerance, DC bias and
        temperature (worst unit)."""
        ratio = _interp(self.dc_bias, v_dc) if self.dc_bias else 1.0
        c = val(self.c_nom) * ratio
        if worst:
            c *= (1 - val(self.tol)) * (1 - (val(self.temp_coef) if self.temp_coef else 0))
        sourced = bool(self.dc_bias) and self.c_nom.sourced and (self.temp_coef is None or self.temp_coef.sourced or not worst)
        src = self.dc_bias_source if sourced else ""
        return Param(c, "F", src, f"C_eff at {v_dc:g} V" + ("" if self.dc_bias else " (no DC-bias curve)"))

    def impedance(self, f: float, v_dc: float) -> complex:
        c = val(self.c_eff(v_dc, worst=False))
        r = val(self.esr_at(f)) if self.esr_at(f) is not None else 0.0
        l = val(self.esl) if self.esl else 0.0
        w = 2 * math.pi * f
        return complex(r, w * l - 1 / (w * c))


def harmonic_share(i_wave: np.ndarray, dt: float, banks: list[Capacitor], v_dc: float,
                   n_harm: int = 50) -> list[dict]:
    """Split one period of a capacitor-network current ``i_wave`` (A, sampled
    every ``dt``) among parallel banks by admittance, harmonic by harmonic.
    Returns per bank: RMS current per part, I^2*ESR loss per part."""
    n = len(i_wave)
    spec = np.fft.rfft(i_wave - np.mean(i_wave)) / n
    f0 = 1 / (n * dt)
    out = [{"irms_part": 0.0, "loss_part_w": 0.0} for _ in banks]
    acc = [[0.0, 0.0] for _ in banks]
    for h in range(1, min(n_harm, len(spec) - 1) + 1):
        f = h * f0
        amp = 2 * abs(spec[h])                     # peak amplitude of this harmonic
        ys = [b.count / b.impedance(f, v_dc) for b in banks]
        ytot = sum(ys)
        for k, (b, y) in enumerate(zip(banks, ys)):
            i_part = amp * abs(y / ytot) / b.count
            esr = b.esr_at(f)
            acc[k][0] += i_part ** 2 / 2
            acc[k][1] += i_part ** 2 / 2 * (val(esr) if esr is not None else 0.0)
    for k in range(len(banks)):
        out[k] = {"irms_part": math.sqrt(acc[k][0]), "loss_part_w": acc[k][1]}
    return out


def capacitor_checks(cap: Capacitor, role: str, *, v_max: Param, irms_part: float, loss_part_w: float,
                     c_needed: Param | None = None, v_dc: float | None = None,
                     derating: Param | None = None, max_rise: Param | None = None) -> list[Check]:
    """``v_max``: highest applied voltage (DC + ripple + transient) with its
    provenance; ``derating``: allowed fraction of the rating (policy, from the
    spec); ``c_needed``: total effective capacitance the design needs."""
    out = []
    frac = val(derating) if derating is not None else 1.0
    lim = val(cap.v_rating) * frac
    out.append(check(f"{role} voltage rating", f"<= {lim:.1f} V ({frac:.0%} of {val(cap.v_rating):.0f} V)",
                     f"{val(v_max):.2f} V max applied", val(v_max) <= lim, "applied max vs rating x derating",
                     {"rating": cap.v_rating, "applied max": v_max, "derating policy": derating},
                     value=val(v_max), limit=lim))
    if c_needed is not None and v_dc is not None:
        ce = cap.c_eff(v_dc)
        tot = val(ce) * cap.count
        out.append(check(f"{role} effective capacitance", f">= {val(c_needed) * 1e6:.1f} uF",
                         f"{tot * 1e6:.1f} uF ({cap.count} x {val(ce) * 1e6:.2f} uF at {v_dc:g} V, worst unit)",
                         tot >= val(c_needed), "nominal x DC bias x (1-tol) x (1-temp)",
                         {"DC-bias curve": ce, "tolerance": cap.tol, "temperature coefficient": cap.temp_coef,
                          "required C": c_needed}, value=tot, limit=val(c_needed), sense="min",
                         to_complete="" if cap.dc_bias else f"DC-bias curve of {cap.mpn}"))
    if cap.ripple_rating is not None:
        out.append(check(f"{role} ripple current", f"<= {val(cap.ripple_rating):.2f} A per part",
                         f"{irms_part:.2f} A per part", irms_part <= val(cap.ripple_rating),
                         "harmonic share by admittance", {"ripple rating": cap.ripple_rating,
                                                          "ESR curve": cap.esr_at(1e5) if cap.esr else None},
                         value=irms_part, limit=val(cap.ripple_rating)))
    rise = None
    inputs = {"ESR": cap.esr_at(1e5) if cap.esr is not None else None}
    if not loss_part_w and irms_part and cap.esr is not None:
        loss_part_w = irms_part ** 2 * val(cap.esr_at(1e5))     # the I^2 ESR the method names
    if cap.heat_table:
        rise = Param(_interp(cap.heat_table, irms_part), "K", cap.heat_source, "self-heating")
    elif cap.ripple_rating is not None and cap.ripple_rise is not None:
        rise = Param(val(cap.ripple_rise) * (irms_part / val(cap.ripple_rating)) ** 2, "K",
                     cap.ripple_rating.source if cap.ripple_rise.sourced else "", "self-heating")
    if rise is None:
        out.append(check(f"{role} self-heating", "reported", f"{loss_part_w * 1e3:.1f} mW per part",
                         None, "I^2 ESR", inputs, missing_model=f"heating curve of {cap.mpn}"))
    else:
        lim_r = val(max_rise) if max_rise is not None else None
        out.append(check(f"{role} self-heating", f"<= {lim_r:.0f} K" if lim_r else "reported",
                         f"{val(rise):.1f} K ({loss_part_w * 1e3:.1f} mW per part)",
                         (val(rise) <= lim_r) if lim_r else True, "vendor heating curve / rating scaling",
                         inputs | {"heating": rise, "max rise": max_rise}, value=val(rise), limit=lim_r))
    return out
