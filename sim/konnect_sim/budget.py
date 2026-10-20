"""Full output-voltage window at the load: both limits, every contribution.

V_load,min = V_set,min - copper drop - ground drop - regulation (down)
             - ripple/2 - transient dip
V_load,max = V_set,max + regulation (up) + light-load offset + ripple/2
             + transient overshoot

Each term is a ``report.Param`` carrying where it came from (divider and
reference tolerance, copper extraction, closed-loop simulation or bench, load
step). Copper drop is only the part of the path the regulator does not sense:
from the point where the feedback tap joins the power path to the load pins.
"""
from __future__ import annotations

from .report import Param, check, val


def window(*, v_set_min: Param, v_set_max: Param, drop_power: Param, drop_ground: Param,
           reg_down: Param, reg_up: Param, ripple_pp: Param, dip: Param, overshoot: Param,
           light_load_offset: Param | None = None) -> dict:
    lo = (val(v_set_min) - val(drop_power) - val(drop_ground) - val(reg_down)
          - val(ripple_pp) / 2 - val(dip))
    hi = (val(v_set_max) + val(reg_up) + (val(light_load_offset) if light_load_offset else 0.0)
          + val(ripple_pp) / 2 + val(overshoot))
    return {"v_min": lo, "v_max": hi}


def checks(v_lo_limit: Param, v_hi_limit: Param, **terms) -> list:
    w = window(**terms)
    inputs = dict(terms)
    parts_lo = (f"set {val(terms['v_set_min']):.3f} - copper {val(terms['drop_power']) * 1e3:.1f} mV"
                f" - gnd {val(terms['drop_ground']) * 1e3:.1f} mV - reg {val(terms['reg_down']) * 1e3:.1f} mV"
                f" - ripple/2 {val(terms['ripple_pp']) * 5e2:.1f} mV - dip {val(terms['dip']) * 1e3:.1f} mV")
    parts_hi = (f"set {val(terms['v_set_max']):.3f} + reg {val(terms['reg_up']) * 1e3:.1f} mV"
                + (f" + light-load {val(terms['light_load_offset']) * 1e3:.1f} mV" if terms.get("light_load_offset") else "")
                + f" + ripple/2 {val(terms['ripple_pp']) * 5e2:.1f} mV + overshoot {val(terms['overshoot']) * 1e3:.1f} mV")
    return [
        check("Output voltage at the load, minimum", f">= {val(v_lo_limit):.3f} V",
              f"{w['v_min']:.3f} V ({parts_lo})", w["v_min"] >= val(v_lo_limit), "worst-case stack-up",
              inputs | {"lower limit": v_lo_limit}, value=w["v_min"], limit=val(v_lo_limit), sense="min",
              layout=True),
        check("Output voltage at the load, maximum", f"<= {val(v_hi_limit):.3f} V",
              f"{w['v_max']:.3f} V ({parts_hi})", w["v_max"] <= val(v_hi_limit), "worst-case stack-up",
              inputs | {"upper limit": v_hi_limit}, value=w["v_max"], limit=val(v_hi_limit), layout=True),
    ]
