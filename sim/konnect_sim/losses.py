"""Synchronous-buck loss budget, efficiency and component temperatures.

Losses at one operating point (all inputs ``report.Param`` with sources):

- conduction:  I_hs_rms^2 R_hs(Tj) + I_ls_rms^2 R_ls(Tj)
- switching:   0.5 Vin (I_valley t_rise + I_peak t_fall) fsw   (hard-switched high side)
- output charge: 0.5 (Coss_hs + Coss_ls) Vin^2 fsw
- dead time:   Vf (I_valley + I_peak) t_dead fsw   (low-side body diode, both edges)
- gate drive and quiescent: (Qg_total fsw + Iq) Vin   (drive supplied from VIN)
- inductor (DCR at temperature + core), capacitor ESR, board copper I^2 R

The IC's junction temperature follows from its losses and the thermal
resistance; on-resistance depends on that temperature, so the two are solved
together. Integrated converters rarely publish switching times: they can be
fitted to the vendor's efficiency curve (``fit_switching_time``), which then
becomes their source, and the fit residual is reported.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field, replace

from .report import Check, Param, check, val


@dataclass
class PowerStage:
    """The switches and their drive, from the converter or MOSFET datasheets."""
    rds_hs: Param                         # ohm at 25 °C
    rds_ls: Param
    rds_tempco: Param                     # fractional change per K (e.g. 0.004)
    t_rise: Param | None = None           # s, SW rising edge (high-side turn-on)
    t_fall: Param | None = None           # s
    coss_hs: Param | None = None          # F
    coss_ls: Param | None = None
    t_dead: Param | None = None           # s per edge
    vf_body: Param | None = None          # V
    qg_total: Param | None = None         # C, both gates
    iq: Param | None = None               # A from VIN while switching (excluding gate drive)
    theta_ja: Param | None = None         # K/W, IC junction to ambient on the stated board
    tj_max: Param | None = None           # °C, limit to check against

    def rds(self, which: str, tj: float) -> float:
        r = val(self.rds_hs if which == "hs" else self.rds_ls)
        return r * (1 + val(self.rds_tempco) * (tj - 25))


@dataclass
class Operating:
    vin: float
    vout: float
    iout: float
    fsw: float
    l_h: float
    ambient_c: float
    r_dcr: float = 0.0            # ohm at temperature (from parts.Inductor.dcr_at)
    p_core: float = 0.0           # W
    p_caps: float = 0.0           # W, capacitor ESR total
    r_copper: float = 0.0         # ohm, board copper in series with the load current
    r_copper_in: float = 0.0      # ohm, board copper carrying the input current


@dataclass
class LossBudget:
    duty: float
    di_pp: float
    tj_c: float
    parts: dict[str, float] = field(default_factory=dict)   # W
    missing: list[str] = field(default_factory=list)

    @property
    def p_loss(self) -> float:
        return sum(self.parts.values())

    @property
    def p_ic(self) -> float:
        return sum(v for k, v in self.parts.items() if k.startswith("ic:"))


def budget(ps: PowerStage, op: Operating, iterations: int = 30) -> tuple[LossBudget, float]:
    """Loss budget and efficiency, solving Tj and duty self-consistently."""
    tj = op.ambient_c + 20
    for _ in range(iterations):
        r_hs, r_ls = ps.rds("hs", tj), ps.rds("ls", tj)
        r_series = op.r_dcr + op.r_copper
        d = (op.vout + op.iout * (r_ls + r_series)) / (op.vin + op.iout * (r_ls - r_hs))
        d = min(max(d, 0.0), 1.0)
        di = (op.vin - op.vout) * d / (op.l_h * op.fsw)
        i_v, i_p = op.iout - di / 2, op.iout + di / 2
        sq = op.iout ** 2 + di ** 2 / 12
        b = LossBudget(d, di, tj)
        b.parts["ic:conduction high side"] = d * sq * r_hs
        b.parts["ic:conduction low side"] = (1 - d) * sq * r_ls
        if ps.t_rise is not None and ps.t_fall is not None:
            b.parts["ic:switching"] = 0.5 * op.vin * (max(i_v, 0) * val(ps.t_rise) + i_p * val(ps.t_fall)) * op.fsw
        else:
            b.missing.append("switching times")
        if ps.coss_hs is not None and ps.coss_ls is not None:
            b.parts["ic:output charge"] = 0.5 * (val(ps.coss_hs) + val(ps.coss_ls)) * op.vin ** 2 * op.fsw
        else:
            b.missing.append("Coss")
        if ps.t_dead is not None and ps.vf_body is not None:
            b.parts["ic:dead time"] = val(ps.vf_body) * (max(i_v, 0) + i_p) * val(ps.t_dead) * op.fsw
        else:
            b.missing.append("dead time / body diode")
        if ps.qg_total is not None:
            b.parts["ic:gate drive"] = val(ps.qg_total) * op.fsw * op.vin
        else:
            b.missing.append("gate charge")
        if ps.iq is not None:
            b.parts["ic:quiescent"] = val(ps.iq) * op.vin
        b.parts["inductor copper"] = sq * op.r_dcr
        b.parts["inductor core"] = op.p_core
        b.parts["capacitor ESR"] = op.p_caps
        b.parts["board copper (output path)"] = op.iout ** 2 * op.r_copper
        pin_est = op.vout * op.iout + sum(b.parts.values())
        b.parts["board copper (input path)"] = (pin_est / op.vin) ** 2 * op.r_copper_in
        new_tj = op.ambient_c + b.p_ic * (val(ps.theta_ja) if ps.theta_ja is not None else 0.0)
        if abs(new_tj - tj) < 0.01:
            tj = new_tj
            break
        tj = new_tj
    b.tj_c = tj
    pout = op.vout * op.iout
    return b, pout / (pout + b.p_loss)


def fit_switching_time(ps: PowerStage, points: list[tuple[Operating, float]], curve_source: str,
                       lo: float = 0.1e-9, hi: float = 50e-9) -> tuple[PowerStage, float]:
    """Fit one switching time (t_rise = t_fall) so the budget reproduces the
    vendor efficiency points ``[(operating point, efficiency), ...]``.
    Returns the fitted stage (switching times sourced to ``curve_source``) and
    the worst absolute efficiency residual."""
    def err(t):
        tp = Param(t, "s", f"fitted to {curve_source}", "switching time")
        s = replace(ps, t_rise=tp, t_fall=tp)
        return sum((budget(s, op)[1] - eta) ** 2 for op, eta in points), s

    a, b_ = math.log(lo), math.log(hi)
    for _ in range(60):                    # golden-section search on log(t)
        m1, m2 = a + (b_ - a) * 0.382, a + (b_ - a) * 0.618
        if err(math.exp(m1))[0] < err(math.exp(m2))[0]:
            b_ = m2
        else:
            a = m1
    _, fitted = err(math.exp((a + b_) / 2))
    resid = max(abs(budget(fitted, op)[1] - eta) for op, eta in points)
    return fitted, resid


def efficiency_checks(ps: PowerStage, op: Operating, *, eta_min: Param | None = None,
                      inputs: dict | None = None) -> tuple[list[Check], LossBudget, float]:
    b, eta = budget(ps, op)
    srcs = {"RDS(on) high side": ps.rds_hs, "RDS(on) low side": ps.rds_ls, "RDS tempco": ps.rds_tempco,
            "switching time": ps.t_rise, "Coss": ps.coss_hs, "dead time": ps.t_dead,
            "body diode Vf": ps.vf_body, "gate charge": ps.qg_total} | (inputs or {})
    detail = ", ".join(f"{k.replace('ic:', 'IC ')} {v * 1e3:.0f} mW" for k, v in b.parts.items() if v > 0)
    lim = val(eta_min) if eta_min is not None else None
    out = [check(f"Efficiency at {op.vin:g} V, {op.iout:g} A", f">= {lim:.1%}" if lim else "reported",
                 f"{eta:.1%} (loss {b.p_loss * 1e3:.0f} mW: {detail})", (eta >= lim) if lim else True,
                 "loss budget (conduction, switching, Coss, dead time, drive, magnetics, ESR, copper)",
                 srcs | {"efficiency limit": eta_min},
                 missing_model=("missing: " + ", ".join(b.missing)) if b.missing else "",
                 value=eta, limit=lim, sense="min")]
    if ps.theta_ja is not None:
        lim_t = val(ps.tj_max) if ps.tj_max is not None else None
        out.append(check(f"IC junction temperature at {op.ambient_c:g} °C ambient",
                         f"<= {lim_t:.0f} °C" if lim_t else "reported",
                         f"{b.tj_c:.0f} °C ({b.p_ic * 1e3:.0f} mW in the IC)", (b.tj_c <= lim_t) if lim_t else True,
                         "Tj = Ta + P_ic theta_JA, RDS(on) at Tj", srcs | {"theta_JA": ps.theta_ja, "Tj max": ps.tj_max},
                         missing_model=("missing: " + ", ".join(b.missing)) if b.missing else "",
                         to_complete="theta_JA is for the datasheet's test board; confirm with a thermal "
                                     "measurement on this board", value=b.tj_c, limit=lim_t))
    return out, b, eta
