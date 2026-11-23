"""The standard verification of a synchronous buck converter.

Every buck is checked the same way: the same checks in the same order, at the same operating
points with the same solver settings, with the same charts. Only the design's values (read
from its schematic, its board and its inputs file) and their sources differ.

Checks, always all present:

    R-01 Input range           closed-loop regulation, Vin min/nom/max x 10 % and 100 % load
    R-02 Output accuracy       worst-case window at the load (reference, divider, regulation,
                               ripple, load-step excursion)
    R-03 Output ripple         switched SPICE, Vin min/nom/max x 10/25/50/75/100 % load
    R-04 Load transient        closed loop, step between two load fractions and back
    R-05 Startup               closed loop at Vin max, no load and full load
    R-06 Loop stability        loop gain by injection at Vin min and max, full load
    R-07 Efficiency            loss budget and switched SPICE over the grid
    R-08 Thermal               IC junction, inductor, and the board when there is one
    R-09 Short-circuit protection  closed loop, overload and output short at Vin max
    S-01 Switch-node and pin stress  edge-resolved SPICE at Vin max, full load
    S-02 Inductor              saturation, L at peak, RMS, losses, temperature
    S-03 Input capacitor RMS current  SPICE over the grid against the capacitors' rating
    S-04 Input network         Middlebrook margin and hot-plug overshoot
    S-05 Layout copper and parasitics  lumped network, ground return, track width, lumping
    S-06 Model validation      SPICE against closed form

plus, exactly when the accepted specification has the category, R-xx Line transient and
R-xx Light-load behaviour. Requirement ids come from the specification
(``design/records/01_specification.json``) when it has the category, else the catalogue's.

Without a validated controller model (``ic.validated_by`` empty) the closed-loop checks run
on a generic controller of the IC's type (peak current, voltage mode or COT) designed for a
crossover at fsw/10, draw their charts, and stay INCOMPLETE.

Inputs: ``iteration<N>/sim/inputs/buck_design.json`` (``template`` writes it). Every entry
is ``{value, unit, source}``; null means unknown. A value needed only to shape a model falls
back to the suite's fixed default (assumed, so the checks that use it stay INCOMPLETE); a
value a slot cannot run without leaves that slot INCOMPLETE with "needs ...".
"""
from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, replace
from functools import cached_property
from pathlib import Path

import numpy as np

from .. import buck as bk
from .. import converter as cv
from .. import design as dsn
from .. import figures, inputfilter, layout, losses, netlist, parts, scenarios, stress, units
from ..parasitics import extract_buck
from ..project import Folders
from ..report import Check, Param, Report, check, val
from .base import Header, Missing, Slot, file_hash, load_json, need, or_default, param, run_slots

NAME, VERSION = "buck", 1
INPUTS_FILE = "buck_design.json"


@dataclass(frozen=True)
class Settings:
    """Fixed for every buck. Tests may pass a lighter copy; reports print the values used."""
    load_fractions: tuple = (0.1, 0.25, 0.5, 0.75, 1.0)    # SPICE grid, of Iout max
    cl_load_fractions: tuple = (0.1, 1.0)                  # closed-loop regulation grid
    spice_cycles: int = 400
    spice_measure_cycles: int = 20
    setpoint_iterations: int = 3
    edge_cycles: int = 6
    settle_periods: int = 300
    cl_steps_per_period: int = 100                         # closed-loop RK4 step: T / 100 (or 0.4 ESR*C)
    stability_points: int = 7                              # fsw/40 .. fsw/4, log spaced
    step_t_after_s: float = 200e-6
    fault_t_s: float = 1.5e-3
    fault_after_s: float = 3e-3
    crossover_fraction: float = 0.1                        # generic controller: fc = fsw/10
    overload_factor: float = 1.5
    short_ohm: float = 0.01


SETTINGS = Settings()

# Model-shaping values used when the inputs file has none: assumed, listed in the report.
DEFAULTS = {
    ("capacitor", "esr"): (0.005, "ohm"), ("capacitor", "esl"): (0.5e-9, "H"),
    ("inductor", "dcr"): (0.03, "ohm"), ("inductor", "tol"): (0.2, ""),
    ("ic", "rds_tempco"): (0.004, "1/K"), ("ic", "t_ss"): (1e-3, "s"), ("ic", "t_off_min"): (200e-9, "s"),
    ("ic", "t_dead"): (10e-9, "s"), ("ic", "t_edge"): (5e-9, "s"), ("ic", "c_sw"): (300e-12, "F"),
    ("source", "r"): (0.05, "ohm"), ("source", "l"): (1e-6, "H"),
    ("design", "resistor_tol_pct"): (1.0, "%"), ("design", "er"): (4.5, ""),
}

# catalogue id -> category (kicad-design-flow spec-catalogue.md)
CORE = [("R-01", "Input range"), ("R-02", "Output accuracy"), ("R-03", "Output ripple"),
        ("R-04", "Load transient"), ("R-05", "Startup"), ("R-06", "Loop stability"), ("R-07", "Efficiency"),
        ("R-08", "Thermal"), ("R-09", "Short-circuit protection")]
OPTIONAL = [("Line transient", "Line transient"), ("Light-load behaviour", "Light-load")]
SUPPLEMENTARY_SPEC = {"S-01": "Pin stress", "S-04": "Input ripple and EMI"}


def _key(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.lower())[:8]


# ── the design ────────────────────────────────────────────────────────────

class Context:
    """The design as the slots see it: inputs, schematic values, board network, and every
    shared simulation result, each computed once on first use."""

    def __init__(self, project: Path, F: Folders, inputs: dict, settings: Settings):
        self.root, self.F, self.d, self.s = project, F, inputs, settings
        self.spec = dsn.load(project, dsn.SPEC) or {}
        self.fig = F.figures
        self.assumed_defaults: list[str] = []
        sch = inputs.get("schematic") or next((p.name for p in project.glob("*.kicad_sch")), None)
        pcb = inputs.get("board") or next((p.name for p in project.glob("*.kicad_pcb")), None)
        self.sch = project / sch if sch else None
        self.pcb = project / pcb if pcb and (project / pcb).is_file() else None

    # ── inputs ──
    def p(self, section: str, key: str, unit: str = "", name: str = "") -> Param | None:
        return param((self.d.get(section) or {}).get(key), unit, name or f"{section} {key}")

    def pd(self, section: str, key: str, name: str = "") -> Param:
        """An input, or the fixed default (recorded as an assumption)."""
        p = self.p(section, key, name=name)
        if p is not None:
            return p
        value, unit = DEFAULTS[(section if (section, key) in DEFAULTS else "capacitor", key)]
        label = name or f"{section} {key}"
        if label not in self.assumed_defaults:
            self.assumed_defaults.append(f"{label} = {value:g} {unit}")
        return Param(value, unit, "", f"{label} (suite default)")

    def refs(self, key: str):
        return (self.d.get("refs") or {}).get(key)

    def pad(self, key: str):
        v = self.refs(key)
        return (str(v[0]), str(v[1])) if v else None

    # ── requirement ids and limits ──
    def req(self, category: str, default_id: str | None) -> tuple[str | None, dict | None]:
        for r in self.spec.get("requirements", []):
            if _key(r["category"]) == _key(category):
                return r["id"], r
        return default_id, None

    def citation(self, category: str) -> str:
        rid, r = self.req(category, None)
        if r is not None and self.spec.get("status") == dsn.ACCEPTED:
            return f"design/records/01_specification.json {rid} rev {self.spec['revision']} (accepted)"
        return ""

    def limit(self, category: str, key: str, default: float, unit: str = "") -> Param:
        """A requirement's limit: the inputs file's number, sourced by the accepted spec;
        without a number the catalogue's proposal, assumed."""
        entry = ((self.d.get("limits") or {}).get(category) or {}).get(key)
        p = param(entry, unit, f"{category} {key}")
        cite = self.citation(category)
        if p is None:
            return Param(default, unit, "", f"{category} {key} (catalogue proposal, assumed)")
        return replace(p, source=p.source or cite)

    # ── operating point ──
    @cached_property
    def op(self) -> dict:
        o = {k: self.p("operating", k) for k in ("vin_min", "vin_nom", "vin_max", "vout", "iout_max")}
        need(**{f"operating.{k}": v for k, v in o.items()})
        return o

    @cached_property
    def vins(self) -> list[float]:
        return [val(self.op[k]) for k in ("vin_min", "vin_nom", "vin_max")]

    @cached_property
    def loads(self) -> list[float]:
        return [round(f * val(self.op["iout_max"]), 6) for f in self.s.load_fractions]

    @cached_property
    def fsw(self) -> Param:
        f = self.p("ic", "fsw", "Hz", "fSW")
        need(**{"ic.fsw": f})
        return f

    @property
    def period(self) -> float:
        return 1 / val(self.fsw)

    # ── schematic values ──
    @cached_property
    def net(self):
        need(schematic=self.sch)
        return netlist.load(self.sch)

    def value(self, ref: str) -> Param:
        c = self.net.components.get(ref)
        if c is None:
            raise Missing([f"{ref} in the schematic"])
        pv = c.parsed
        if pv is None or pv.value is None:
            pv = units.parse_value(c.value.replace("/", " "), units.default_unit_for(ref))
        if pv.value is None:
            raise Missing([f"a value for {ref} ('{c.value}')"])
        return Param(pv.value, "", f"{self.sch.name} ({ref} = {c.value})", ref)

    @cached_property
    def L(self) -> Param:
        need(**{"refs.inductor": self.refs("inductor")})
        return self.value(self.refs("inductor"))

    def caps(self, role: str) -> list[str]:
        refs = self.refs(role) or []
        need(**{f"refs.{role}": refs or None})
        return refs

    def cap_param(self, ref: str, key: str) -> Param:
        entry = (self.d.get("capacitors") or {}).get(ref) or {}
        p = param(entry.get(key), name=f"{ref} {key}")
        if p is not None:
            return p
        if ("capacitor", key) not in DEFAULTS:
            return None
        value, unit = DEFAULTS[("capacitor", key)]
        note = f"{ref} {key} = {value:g} {unit}"
        if note not in self.assumed_defaults:
            self.assumed_defaults.append(note)
        return Param(value, unit, "", f"{ref} {key} (suite default)")

    def dc_bias(self, ref: str):
        entry = ((self.d.get("capacitors") or {}).get(ref) or {}).get("dc_bias") or {}
        pts = entry.get("points")
        return (pts, entry.get("source") or "") if pts else (None, "")

    def derate(self, ref: str, v: float) -> float:
        pts, _ = self.dc_bias(ref)
        if not pts:
            return 1.0
        xs, ys = zip(*sorted(pts))
        return float(np.interp(abs(v), xs, ys))

    @cached_property
    def divider(self) -> tuple[Param, Param]:
        """(R_top, R_bot). A fixed-output IC (``ic.fixed_output``) divides internally: its FB
        pin senses V_out directly and ``vref_*`` are the output's own set-point limits."""
        if (self.d.get("ic") or {}).get("fixed_output"):
            src = "fixed-output IC: FB senses V_out (internal divider)"
            return Param(0.0, "ohm", src, "R_top"), Param(1e6, "ohm", src, "R_bot (FB input)")
        need(**{"refs.r_top": self.refs("r_top"), "refs.r_bot": self.refs("r_bot")})
        return self.value(self.refs("r_top")), self.value(self.refs("r_bot"))

    @cached_property
    def vref(self) -> Param:
        v = self.p("ic", "vref_typ", "V", "VFB typ")
        need(**{"ic.vref_typ": v})
        return v

    @cached_property
    def vout_set(self) -> float:
        rt, rb = self.divider
        return bk.divider_vout(val(self.vref), val(rt), val(rb))

    # ── layout ──
    @cached_property
    def board(self):
        if self.pcb is None:
            return None
        b = layout.load(self.pcb)
        st_file = self.d.get("stackup_file")
        from .. import stackup
        st = stackup.from_json(self.root / st_file) if st_file else stackup.from_board(self.pcb)
        if st is not None:
            st.apply(b)
        return b

    layout_error = ""

    @cached_property
    def lumped(self):
        """The board's lumped network; None without a board, or when the extraction failed
        (``layout_error`` says why). Raises ``Missing`` without the pads to extract it."""
        if self.board is None:
            return None
        try:
            return self._extract()
        except Missing:
            raise
        except Exception as e:
            self.layout_error = f"{type(e).__name__}: {e}"
            return None

    def _extract(self):
        pins = self.d.get("refs") or {}
        need(**{f"refs.{k}": pins.get(k) for k in ("ic", "ic_vin", "ic_gnd", "ic_sw", "sense", "load_vout",
                                                    "load_gnd", "sense_gnd")})
        n = extract_buck(self.board, ic=pins["ic"], ic_vin=str(pins["ic_vin"]), ic_gnd=str(pins["ic_gnd"]),
                         ic_sw=str(pins["ic_sw"]), cin_refs=self.caps("cin"), cout_refs=self.caps("cout"),
                         inductor=self.refs("inductor"), sense=self.pad("sense"), load_vout=self.pad("load_vout"),
                         load_gnd=self.pad("load_gnd"), sense_gnd=self.pad("sense_gnd"),
                         source_vin=self.pad("source_vin"), nets=self.d.get("nets") or None,
                         er=val(self.pd("design", "er", "laminate er")))
        n.save(self.F.results / "parasitics.json")
        return n

    def layout_inputs(self) -> dict:
        if self.board is None:
            return {"layout parasitics (no board yet: ideal copper)": None}
        try:
            ln = self.lumped
        except Missing:
            return {"layout parasitics (pads for the extraction not given: ideal copper)": 1.0}
        if ln is None:
            return {f"layout parasitics (extraction failed: {self.layout_error}; ideal copper)": 1.0}
        src = ln.source if self.board.stackup_source else ""
        return {"layout parasitics": Param(1.0, "", src, "lumped network"),
                "laminate er": self.pd("design", "er", "laminate er")}

    # ── model inputs, grouped by what depends on them ──
    def power_inputs(self) -> dict:
        d = {"Vin": self.op["vin_nom"], "Vout": self.op["vout"], "Iout": self.op["iout_max"], "fSW": self.fsw,
             "L": self.L, "DCR": self.pd("inductor", "dcr", "inductor DCR"),
             "RDS(on) HS": self.p("ic", "rds_hs"), "RDS(on) LS": self.p("ic", "rds_ls"),
             "source R": self.pd("source", "r", "source R"), "source L": self.pd("source", "l", "source L")}
        for ref in self.caps("cin") + self.caps("cout"):
            d[f"{ref} C"] = self.value(ref)
            d[f"{ref} ESR"] = self.cap_param(ref, "esr")
            d[f"{ref} ESL"] = self.cap_param(ref, "esl")
            pts, src = self.dc_bias(ref)
            d[f"{ref} DC-bias curve"] = Param(1.0, "", src, "DC bias") if pts else None
            if not pts:
                d[f"{ref} DC-bias curve (none: nominal C)"] = 1.0
        return d | self.layout_inputs()

    def ctrl_inputs(self) -> dict:
        return {"VFB": self.vref, "t_ss": self.pd("ic", "t_ss", "soft-start time")}

    # ── SPICE (open loop at the set-point) ──
    def banks(self, role: str, v: float) -> list[bk.CapBank]:
        out = []
        for ref in self.caps(role):
            out.append(bk.CapBank(val(self.value(ref)) * self.derate(ref, v), 1, val(self.cap_param(ref, "esr")),
                                  val(self.cap_param(ref, "esl"))))
        return out

    def testbench(self, vin: float, iout: float) -> bk.BuckTestbench:
        need(**{"ic.rds_hs": self.p("ic", "rds_hs"), "ic.rds_ls": self.p("ic", "rds_ls")})
        tb = bk.BuckTestbench(
            vin=vin, vout_target=self.vout_set, iout=iout, fsw=val(self.fsw), l_h=val(self.L),
            l_dcr_ohm=val(self.pd("inductor", "dcr", "inductor DCR")), cout=self.banks("cout", self.vout_set),
            cin=self.banks("cin", vin), rds_hs_ohm=val(self.p("ic", "rds_hs")), rds_ls_ohm=val(self.p("ic", "rds_ls")),
            source_r_ohm=val(self.pd("source", "r", "source R")), source_l_h=val(self.pd("source", "l", "source L")),
            dead_time_s=val(self.pd("ic", "t_dead", "dead time")), c_sw_f=val(self.pd("ic", "c_sw", "switch-node C")),
            edge_s=val(self.pd("ic", "t_edge", "switching edge")), cycles=self.s.spice_cycles,
            measure_cycles=self.s.spice_measure_cycles)
        return tb

    def with_layout(self, tb, edges: bool):
        try:
            ln = self.lumped
        except Missing:
            return tb
        if ln is None:
            return tb
        return ln.apply_testbench(tb, edges=edges)

    @cached_property
    def grid(self) -> dict:
        """SPICE steady state at every (Vin, load): {(vin, i): result with ``snap``}."""
        out = {}
        for v in self.vins:
            for i in self.loads:
                tb = self.with_layout(self.testbench(v, i), edges=False)
                out[(v, i)] = tb.run_at_setpoint(self.s.setpoint_iterations, snap=True)
        rows = [{"vin": v, "iout": i} | {k: x for k, x in r.items() if k not in ("snap", "solver")}
                for (v, i), r in out.items()]
        (self.F.results / "spice_grid.json").write_text(json.dumps(rows, indent=1, default=float), encoding="utf-8")
        return out

    # ── closed loop ──
    @cached_property
    def plant(self) -> cv.Plant:
        rt, rb = self.divider
        lvi = None
        curve = (self.d.get("inductor") or {}).get("l_vs_i") or {}
        if curve.get("points"):
            pts = sorted(curve["points"])
            lvi = [(a, h / pts[0][1]) for a, h in pts]

        def banks(role, v):
            # one equivalent bank per group (C summed, ESR and ESL in parallel, the DC-bias curve
            # of the largest part): a small HF capacitor's ESR*C would otherwise force a step
            # far below the switching period, and it barely affects the loop
            refs = self.caps(role)
            cs = [val(self.value(r)) for r in refs]
            big = refs[int(np.argmax(cs))]
            curve = self.dc_bias(big)[0]
            return [cv.Bank(sum(cs), 1 / sum(1 / val(self.cap_param(r, "esr")) for r in refs), 1,
                            [(x, y) for x, y in curve] if curve else None,
                            esl_h=1 / sum(1 / val(self.cap_param(r, "esl")) for r in refs))]

        p = cv.Plant(val(self.L), val(self.pd("inductor", "dcr", "inductor DCR")), banks("cin", 0), banks("cout", 0),
                     val(self.p("ic", "rds_hs")), val(self.p("ic", "rds_ls")), val(rt), val(rb),
                     src_r_ohm=val(self.pd("source", "r", "source R")), src_l_h=val(self.pd("source", "l", "source L")),
                     l_vs_i=lvi)
        try:
            ln = self.lumped
        except Missing:
            ln = None
        return ln.apply_plant(p) if ln is not None else p

    @cached_property
    def controller_kind(self) -> str:
        kind = ((self.d.get("ic") or {}).get("controller") or "").lower().replace("-", "_").replace(" ", "_")
        need(**{"ic.controller (peak_current, voltage_mode or cot)":
                kind if kind in ("peak_current", "voltage_mode", "cot") else None})
        return kind

    def common(self) -> cv.Common:
        ic = self.d.get("ic") or {}
        g = lambda k: val(self.p("ic", k)) if self.p("ic", k) is not None else None  # noqa: E731
        return cv.Common(vref=val(self.vref), t_ss=val(self.pd("ic", "t_ss", "soft-start time")),
                         uvlo_on=g("uvlo_on") or 0.0, uvlo_off=g("uvlo_off") or 0.0,
                         pulse_skip=(ic.get("light_load_mode") or "pfm").lower() != "fpwm",
                         uvp_frac=g("uvp_frac"), uvp_delay=g("uvp_delay") or 0.0, hiccup_off=g("hiccup_off"),
                         validated_by=ic.get("validated_by") or "")

    @cached_property
    def comp(self) -> dict:
        """The generic controller's design (or the datasheet's, when given): crossover at
        ``crossover_fraction`` x fsw, full load, Vin nominal."""
        fsw, vin, vout = val(self.fsw), val(self.op["vin_nom"]), self.vout_set
        L = val(self.L)
        rt, rb = (val(x) for x in self.divider)
        h = rb / (rt + rb)
        c = sum(val(self.value(r)) * self.derate(r, vout) for r in self.caps("cout"))
        esr = 1 / sum(1 / val(self.cap_param(r, "esr")) for r in self.caps("cout"))
        r_load = vout / val(self.op["iout_max"])
        fc = self.s.crossover_fraction * fsw
        s = 2j * math.pi * fc
        kind = self.controller_kind
        ilim = self.p("ic", "i_peak_limit")
        if kind == "peak_current":
            r_i = 1.0 / (val(ilim) if ilim else 2 * val(self.op["iout_max"]))
            slope = r_i * vout / L                               # = the down-slope: no subharmonic at any D
            T = 1 / fsw
            d = vout / vin
            mc = 1 + slope / (r_i * (vin - vout) / L)
            wp = 1 / (c * r_load) + T / (L * c) * (mc * (1 - d) - 0.5)
            gvc = lambda s: (r_load / r_i) / (1 + r_load * T / L * (mc * (1 - d) - 0.5)) \
                * (1 + s * c * esr) / (1 + s / wp)  # noqa: E731
            wz, wp2 = wp, min(1 / (c * esr), math.pi * fsw)
            num, den = cv.type2(1.0, wz, wp2)
            k = 1 / abs(np.polyval(num, s) / np.polyval(den, s) * gvc(s) * h)
            return {"kind": kind, "comp": cv.type2(k, wz, wp2), "r_i": r_i, "slope": slope, "fc": fc}
        if kind == "voltage_mode":
            w0, wp1, wp2 = 1 / math.sqrt(L * c), min(1 / (c * esr), math.pi * fsw), math.pi * fsw
            num, den = cv.type3(1.0, w0, w0, wp1, wp2)
            zl, zc = s * L + val(self.pd("inductor", "dcr", "inductor DCR")), esr + 1 / (s * c)
            k = 1 / abs(np.polyval(num, s) / np.polyval(den, s) * vin * zc / (zl + zc) * h)
            return {"kind": kind, "comp": cv.type3(k, w0, w0, wp1, wp2), "fc": fc}
        dil = bk.inductor_ripple_pp(vin, vout, L, fsw)
        return {"kind": kind, "k_ramp": 0.03 * val(self.vref) / dil, "tau_ramp": 10 / fsw, "fc": fc}

    def controller(self):
        cd, fsw = self.comp, val(self.fsw)
        g = lambda k, dflt: val(self.p("ic", k)) if self.p("ic", k) is not None else dflt  # noqa: E731
        if cd["kind"] == "peak_current":
            return cv.PeakCurrentMode(self.common(), fsw=fsw, comp=cd["comp"], r_i=cd["r_i"],
                                      slope_v_per_s=cd["slope"], i_peak_limit=g("i_peak_limit", math.inf),
                                      t_dead=val(self.pd("ic", "t_dead", "dead time")))
        if cd["kind"] == "voltage_mode":
            return cv.VoltageMode(self.common(), fsw=fsw, comp=cd["comp"], v_ramp=1.0,
                                  i_peak_limit=g("i_peak_limit", math.inf),
                                  t_dead=val(self.pd("ic", "t_dead", "dead time")))
        i_lim = g("i_valley_limit", g("i_peak_limit", 1.5 * val(self.op["iout_max"])))
        return cv.COT(self.common(), fsw=fsw, vout_set=self.vout_set,
                      t_off_min=val(self.pd("ic", "t_off_min", "minimum off-time")), i_valley_limit=i_lim,
                      k_ramp=cd["k_ramp"], tau_ramp=cd["tau_ramp"], t_on_min=g("t_on_min", 0.0),
                      t_dead=val(self.pd("ic", "t_dead", "dead time")))

    def make(self, vin, load, en=None, v0=None, il0=None, v0_in=None, cond=None):
        ctrl = self.controller()
        running = v0 is not None and v0 > 0
        if running:
            ctrl.state, ctrl.vref_now, ctrl.pulsed = "run", val(self.vref), True
            if isinstance(ctrl, cv.COT):
                ctrl.phase, ctrl.il_lp = "ls", il0 or 0.0
        sim = cv.Sim(self.plant, ctrl, vin=vin, load=load, en=en, fsw_hint=val(self.fsw), dt=self.cl_dt,
                     v0_out=v0 or 0.0, v0_in=v0_in)
        if running and il0:
            sim.x[0], sim.idle = il0, False
        return sim

    @cached_property
    def cl_dt(self) -> float:
        taus = [b.esr_ohm / b.count * b.c_at(0) for b in self.plant.cin + self.plant.cout]
        return min(self.period / self.s.cl_steps_per_period, 0.4 * min(taus))

    @property
    def model_note(self) -> str:
        ic = self.d.get("ic") or {}
        if ic.get("validated_by"):
            return f"controller model validated by {ic['validated_by']}"
        return (f"generic {self.comp['kind'].replace('_', ' ')} controller (crossover designed at "
                f"{self.comp['fc'] / 1e3:.0f} kHz), not the IC's own model")

    @cached_property
    def regulation(self) -> tuple[list, list]:
        tol = self.output_tol
        table = []
        chk = scenarios.regulation(self.make, self.vins, [f * val(self.op["iout_max"]) for f in self.s.cl_load_fractions],
                                   v_target=Param(self.vout_set, "V", self.vref.source, "set-point"), tol=tol,
                                   period=self.period, inputs=self.power_inputs() | self.ctrl_inputs(), table=table)
        return chk, table

    @property
    def output_tol(self) -> Param:
        lo = self.limit_window[0]
        return Param((val(self.op["vout"]) - val(lo)) / val(self.op["vout"]), "", lo.source, "regulation band")

    @cached_property
    def limit_window(self) -> tuple[Param, Param]:
        vo = val(self.op["vout"])
        return (self.limit("Output accuracy", "lo", 0.98 * vo, "V"), self.limit("Output accuracy", "hi", 1.02 * vo, "V"))

    @cached_property
    def load_step(self) -> tuple[list, dict]:
        io = val(self.op["iout_max"])
        i0 = val(self.limit("Load transient", "step_from", 0.25)) * io
        i1 = val(self.limit("Load transient", "step_to", 0.75)) * io
        waves = {}
        settle = self.limit("Load transient", "settle", 100e-6, "s")
        chk = scenarios.load_step(self.make, val(self.op["vin_nom"]), i0, i1,
                                  self.limit("Load transient", "slew", 1e6, "A/s"),
                                  v_target=Param(self.vout_set, "V", self.vref.source, "set-point"),
                                  max_dev=self.limit("Load transient", "max_dev", 0.05),
                                  settle_band=self.output_tol, max_settle=settle, period=self.period,
                                  inputs=self.power_inputs() | self.ctrl_inputs(),
                                  t_after=max(self.s.step_t_after_s, 2 * val(settle)), waves=waves)
        return chk, waves

    @cached_property
    def stability(self) -> tuple[list, list]:
        fsw, io, vo = val(self.fsw), val(self.op["iout_max"]), self.vout_set
        conds = [{"vin": val(self.op["vin_min"]), "iout": io, "v0": vo, "label": f"Vin {val(self.op['vin_min']):g} V"},
                 {"vin": val(self.op["vin_max"]), "iout": io, "v0": vo, "label": f"Vin {val(self.op['vin_max']):g} V"}]
        freqs = list(np.geomspace(fsw / 40, fsw / 4, self.s.stability_points))
        amp = lambda f: (0.02 if f < self.comp["fc"] / 2 else 0.008) * vo  # noqa: E731
        return scenarios.stability(self.make, conds, freqs, amp, pm_min=self.limit("Loop stability", "pm", 45.0, "deg"),
                                   gm_min=self.limit("Loop stability", "gm", 10.0, "dB"), period=self.period,
                                   inputs=self.power_inputs() | self.ctrl_inputs())

    # ── losses ──
    def stage(self) -> losses.PowerStage:
        ic = lambda k: self.p("ic", k)  # noqa: E731
        return losses.PowerStage(rds_hs=ic("rds_hs"), rds_ls=ic("rds_ls"),
                                 rds_tempco=self.pd("ic", "rds_tempco", "RDS(on) tempco"),
                                 t_rise=ic("t_rise"), t_fall=ic("t_fall"), coss_hs=ic("coss_hs"), coss_ls=ic("coss_ls"),
                                 t_dead=ic("t_dead"), vf_body=ic("vf_body"), qg_total=ic("qg_total"), iq=ic("iq"),
                                 theta_ja=ic("theta_ja"), tj_max=ic("tj_max"))

    def operating(self, vin: float, iout: float, ambient: float) -> losses.Operating:
        try:
            ln = self.lumped
        except Missing:
            ln = None
        r_cu = (ln.r_sw_ohm + ln.r_out_ohm + ln.r_gnd_ohm) if ln else 0.0
        return losses.Operating(vin=vin, vout=self.vout_set, iout=iout, fsw=val(self.fsw), l_h=val(self.L),
                                ambient_c=ambient, r_dcr=val(self.pd("inductor", "dcr", "inductor DCR")),
                                r_copper=r_cu, r_copper_in=(ln.r_in_ohm + ln.r_hot_ohm) if ln else 0.0)

    @property
    def ambient(self) -> Param:
        return self.limit("Thermal", "ambient", 25.0, "°C")


# ── the slots ─────────────────────────────────────────────────────────────

def _ser(ctx, key, scale=1.0):
    return {f"{i:g} A": [ctx.grid[(v, i)][key] * scale for v in ctx.vins] for i in ctx.loads}


def r01(ctx):
    chk, table = ctx.regulation
    lo, hi = ctx.limit_window
    series = {}
    for vin, i, v in table:
        series.setdefault(f"{i:g} A", ([], []))
        series[f"{i:g} A"][0].append(vin)
        series[f"{i:g} A"][1].append(v)
    fig = figures.curves(ctx.fig / "r01_vout_vs_vin.png",
                         [{"ylabel": "V at the load (V)", "series": series,
                           "limits": [(val(lo), f"lower {val(lo):.3f} V"), (val(hi), f"upper {val(hi):.3f} V", 0.6)]}],
                         title="Output voltage vs input voltage (closed loop), one line per load current",
                         xlabel="input voltage (V)", note=ctx.model_note)
    return chk, {"Output voltage vs input voltage": fig}


def r02(ctx):
    rt, rb = ctx.divider
    tol = ctx.pd("design", "resistor_tol_pct", "divider resistor tolerance")
    vmin_ref, vmax_ref = ctx.p("ic", "vref_min"), ctx.p("ic", "vref_max")
    need(**{"ic.vref_min": vmin_ref, "ic.vref_max": vmax_ref})
    lo_set, hi_set = bk.divider_vout_range(val(vmin_ref), val(vmax_ref), val(rt), val(rb), val(tol), val(tol))
    src = "; ".join(s for s in (vmin_ref.source, rt.source, rb.source) if s)
    _, table = ctx.regulation
    vs = [v for _, _, v in table]
    reg_down = max(0.0, ctx.vout_set - min(vs))
    reg_up = max(0.0, max(vs) - ctx.vout_set)
    ripple = max(r["vload_pp"] for r in ctx.grid.values())
    steps, _ = ctx.load_step
    dip = max(ctx.vout_set - float(re.search(r"min ([\d.]+) V", c.result).group(1)) for c in steps)
    over = max(float(re.search(r"max ([\d.]+) V", c.result).group(1)) - ctx.vout_set for c in steps)
    cl = ctx.model_note
    cl_src = cl if (ctx.d.get("ic") or {}).get("validated_by") else ""
    from .. import budget
    lo_lim, hi_lim = ctx.limit_window
    terms = dict(v_set_min=Param(lo_set, "V", src, "set-point min"), v_set_max=Param(hi_set, "V", src, "set-point max"),
                 drop_power=Param(0.0, "V", "in the closed-loop regulation (copper modelled)", "copper drop"),
                 drop_ground=Param(0.0, "V", "in the closed-loop regulation (copper modelled)", "ground drop"),
                 reg_down=Param(reg_down, "V", cl_src, f"regulation, {cl}"),
                 reg_up=Param(reg_up, "V", cl_src, f"regulation, {cl}"),
                 ripple_pp=Param(ripple, "V", "SPICE grid (R-03)", "ripple"),
                 dip=Param(max(dip, 0.0), "V", cl_src, f"load-step dip, {cl}"),
                 overshoot=Param(max(over, 0.0), "V", cl_src, f"load-step overshoot, {cl}"))
    subs = budget.checks(lo_lim, hi_lim, **terms)
    w = budget.window(**terms)
    fig = figures.window(ctx.fig / "r02_window.png", val(ctx.op["vout"]), w["v_min"], w["v_max"], val(lo_lim),
                         val(hi_lim), title="Output voltage window at the load (worst case)",
                         terms_lo=[("set-point min", ctx.vout_set - lo_set), ("regulation", reg_down),
                                   ("ripple / 2", ripple / 2), ("load-step dip", max(dip, 0.0))],
                         terms_hi=[("set-point max", hi_set - ctx.vout_set), ("regulation", reg_up),
                                   ("ripple / 2", ripple / 2), ("overshoot", max(over, 0.0))])
    return subs, {"Output voltage window at the load": fig}


def r03(ctx):
    lim = ctx.limit("Output ripple", "max", 0.01 * val(ctx.op["vout"]), "V")
    snaps = {f"{v:g}V_{i:g}A": r["snap"] for (v, i), r in ctx.grid.items()}
    figs = figures.ripple_figures(ctx.fig, snaps, ctx.vins, ctx.loads, vout_pp_max=val(lim), period=ctx.period)
    worst = max(ctx.grid.items(), key=lambda kv: kv[1]["vload_pp"])
    (v, i), r = worst
    ok = r["vload_pp"] <= val(lim)
    c = check("Output ripple", f"<= {val(lim) * 1e3:.1f} mVpp", f"{r['vload_pp'] * 1e3:.2f} mVpp worst, at Vin {v:g} V, "
              f"{i:g} A (switched SPICE, {len(ctx.grid)} operating points)", ok,
              "switched SPICE at the set-point over Vin x load", ctx.power_inputs() | {"limit": lim},
              value=r["vload_pp"], limit=val(lim), lever="more or lower-ESR output capacitance; larger L")
    return [c], dict(figs)


def _time_panels(items, t0_key, scale_t, title, path, xlabel, panels_spec, note=""):
    panels = []
    for ylabel, attr, limits in panels_spec:
        series = {}
        for lab, (w, t0) in items.items():
            series[lab] = ((w.t - t0) * scale_t, getattr(w, attr))
        panels.append({"ylabel": ylabel, "series": series, "limits": limits})
    return figures.curves(path, panels, title=title, xlabel=xlabel, note=note)


def r04(ctx):
    chk, waves = ctx.load_step
    lo, hi = ctx.limit_window
    fig = _time_panels({f"step {k}": v for k, v in waves.items()}, None, 1e6, "Load step response",
                       ctx.fig / "r04_load_step.png", "time from the step (µs)",
                       [("V at the load (V)", "vload", [(val(lo), "lower"), (val(hi), "upper", 0.6)]),
                        ("inductor current (A)", "il", [])], note=ctx.model_note)
    return chk, {"Load step response": fig}


def r05(ctx):
    vin = val(ctx.op["vin_max"])
    t_ss = val(ctx.pd("ic", "t_ss", "soft-start time"))
    vo = val(ctx.op["vout"])
    waves = {}
    chk = scenarios.startup_shutdown(ctx.make, vin, r_full=ctx.vout_set / val(ctx.op["iout_max"]),
                                     v_target=Param(ctx.vout_set, "V", ctx.vref.source, "set-point"),
                                     max_overshoot=ctx.limit("Startup", "overshoot", 0.05 * vo, "V"),
                                     max_inrush=ctx.limit("Startup", "inrush", 2 * val(ctx.op["iout_max"]), "A"),
                                     t_run=1.5 * t_ss + 200e-6, inputs=ctx.power_inputs() | ctx.ctrl_inputs(),
                                     waves=waves)
    items = {k: (w, 0.0) for k, (w, _, _) in waves.items()}
    fig = _time_panels(items, None, 1e3, f"Start-up and restart at Vin {vin:g} V", ctx.fig / "r05_startup.png",
                       "time (ms)", [("V at the load (V)", "vload", []), ("input current (A)", "isrc", [])],
                       note=ctx.model_note)
    return chk, {"Start-up and restart": fig}


def r06(ctx):
    chk, table = ctx.stability
    curves = [(lab, m, pts) for lab, m, pts in table if pts]
    generic = "" if (ctx.d.get("ic") or {}).get("validated_by") else ", generic controller"
    fig = figures.bode(ctx.fig / "r06_loop_gain.png", curves, title=f"Loop gain by injection, full load{generic}",
                       pm_min=val(ctx.limit("Loop stability", "pm", 45.0, "deg")))
    return chk, {"Loop gain": fig}


def r07(ctx):
    eta_min = ctx.limit("Efficiency", "min", 0.85)
    ps, amb = ctx.stage(), val(ctx.ambient)
    need(**{"ic.rds_hs": ps.rds_hs, "ic.rds_ls": ps.rds_ls})
    budget_eta = {(v, i): losses.budget(ps, ctx.operating(v, i, amb))[1] for v in ctx.vins for i in ctx.loads}
    io = val(ctx.op["iout_max"])
    subs = []
    for v in (val(ctx.op["vin_nom"]), val(ctx.op["vin_max"])):
        c, _, _ = losses.efficiency_checks(ps, ctx.operating(v, io, amb), eta_min=eta_min,
                                           inputs=ctx.power_inputs())
        sp = ctx.grid[(v, ctx.loads[-1])]["eff"]
        c[0].result += f"; switched SPICE {sp:.1%}"
        subs.append(c[0])
    lo = min(min(budget_eta.values()), min(r["eff"] for r in ctx.grid.values()), val(eta_min)) * 100
    ymin = max(0.0, 10 * math.floor((lo - 5) / 10))
    panels = [{"title": "Loss budget", "ylabel": "efficiency (%)", "legend_title": "load current", "ymin": ymin,
               "series": {f"{i:g} A": [budget_eta[(v, i)] * 100 for v in ctx.vins] for i in ctx.loads},
               "limit": val(eta_min) * 100, "limit_label": f"limit {val(eta_min):.0%}", "limit_below": True,
               "ymax": 100},
              {"title": "Switched SPICE (conduction and edge losses only)", "ylabel": "efficiency (%)",
               "legend_title": "load current", "series": _ser(ctx, "eff", 100), "ymax": 100, "ymin": ymin}]
    fig = figures.metric_vs_x(ctx.fig / "r07_efficiency.png", ctx.vins, panels, xlabel="input voltage (V)",
                              title="Efficiency vs input voltage, one line per load current", panel_width=4.6)
    return subs, {"Efficiency vs input voltage": fig}


def r08(ctx):
    ps = ctx.stage()
    amb = ctx.ambient
    vin, io = val(ctx.op["vin_max"]), val(ctx.op["iout_max"])
    ps = replace(ps, tj_max=ctx.limit("Thermal", "tj_max", val(ps.tj_max) - 25 if ps.tj_max else 125.0, "°C"))
    subs, b, _ = losses.efficiency_checks(ps, ctx.operating(vin, io, val(amb)), inputs=ctx.power_inputs() | {"ambient": amb})
    subs = [c for c in subs if "junction" in c.name]
    if ps.theta_ja is None:
        subs = [check("IC junction temperature", f"<= {val(ps.tj_max):.0f} °C", f"{b.p_ic * 1e3:.0f} mW in the IC; "
                      "no theta_JA to turn it into a temperature", None, "Tj = Ta + P_ic theta_JA",
                      {"ambient": amb}, to_complete="the IC's theta_JA (ic.theta_ja) from its datasheet")]
    charts = {"Losses at Vin max, full load": figures.bars(
        ctx.fig / "r08_losses.png", [k.replace("ic:", "IC ") for k, v in b.parts.items() if v > 0],
        [v * 1e3 for v in b.parts.values() if v > 0], title=f"Losses at {vin:g} V, {io:g} A", xlabel="mW")}
    if ctx.board is not None:
        from ..tools import board_thermal
        p_l = b.parts.get("inductor copper", 0.0) + b.parts.get("inductor core", 0.0)
        watts = {ctx.refs("ic"): b.p_ic} | ({ctx.refs("inductor"): p_l} if p_l else {})
        res = board_thermal(str(ctx.pcb), val(amb), 10.0, 0.3, 0.3, watts,
                            png=str(ctx.fig / "r08_board_temperature.png"))
        charts["Board temperature"] = res["png"]
        for c in res["checks"]:
            subs.append(Check(**{k: v for k, v in c.items() if k in Check.__dataclass_fields__}))
    return subs, charts


def r09(ctx):
    vin = val(ctx.op["vin_max"])
    isat = ctx.p("inductor", "isat", "A", "inductor Isat") or Param(float("inf"), "A", "", "Isat (not given)")
    waves = {}
    chk = scenarios.faults(ctx.make, vin, i_overload=ctx.s.overload_factor * val(ctx.op["iout_max"]),
                           r_short=ctx.s.short_ohm, i_sat=isat,
                           v_target=Param(ctx.vout_set, "V", ctx.vref.source, "set-point"), period=ctx.period,
                           inputs=ctx.power_inputs() | ctx.ctrl_inputs(), t_fault=ctx.s.fault_t_s,
                           t_after=ctx.s.fault_after_s, waves=waves)
    for c in chk:
        c.requirement = c.requirement.replace("Isat inf A", "Isat (not given)")
    panels = []
    for ylabel, attr in (("inductor current (A)", "il"), ("V at the load (V)", "vload")):
        series = {}
        for lab, (w1, w2, t0) in waves.items():
            t = np.concatenate([w1.t, w2.t]) - t0
            series[lab] = (t * 1e3, np.concatenate([getattr(w1, attr), getattr(w2, attr)]))
        lim = [(val(isat), f"Isat {val(isat):.2f} A")] if attr == "il" and math.isfinite(val(isat)) else []
        panels.append({"ylabel": ylabel, "series": series, "limits": lim})
    fig = figures.curves(ctx.fig / "r09_faults.png", panels, title=f"Overload and output short at Vin {vin:g} V",
                         xlabel="time from the fault (ms)", note=ctx.model_note)
    return chk, {"Overload and output short": fig}


def r_line(ctx):
    rid, _ = ctx.req("Line transient", None)
    vmin, vnom, vmax = ctx.vins
    waves = {}
    chk = scenarios.input_transients(ctx.make, v_nom=vnom, v_lo=vmin, v_hi=vmax, iout=val(ctx.op["iout_max"]),
                                     v_target=Param(ctx.vout_set, "V", ctx.vref.source, "set-point"),
                                     max_dev=ctx.limit("Line transient", "max_dev", 0.03),
                                     slew_v_per_s=(vmax - vmin) / 100e-6, dip_to=max(0.8 * vmin, 0.1),
                                     dip_len=200e-6, period=ctx.period, inputs=ctx.power_inputs() | ctx.ctrl_inputs(),
                                     waves=waves)
    fig = _time_panels(waves, None, 1e6, "Line steps", ctx.fig / "r_line_steps.png", "time from the step (µs)",
                       [("V at the load (V)", "vload", []), ("V at the IC VIN pin (V)", "vin", [])],
                       note=ctx.model_note)
    return chk, {"Line steps": fig}


def r_light(ctx):
    io, vo = val(ctx.op["iout_max"]), val(ctx.op["vout"])
    loads = [0.0, 0.01 * io, 0.05 * io, 0.1 * io]
    chk = scenarios.light_load(ctx.make, val(ctx.op["vin_nom"]), loads,
                               v_target=Param(ctx.vout_set, "V", ctx.vref.source, "set-point"), tol=ctx.output_tol,
                               ripple_max=ctx.limit("Light-load behaviour", "ripple", 0.01 * vo, "V"),
                               period=ctx.period, inputs=ctx.power_inputs() | ctx.ctrl_inputs())
    vals = re.findall(r"([\d.]+) mA: ([\d.]+) V, ([\d.]+) mVpp", chk[0].result)
    fig = figures.curves(ctx.fig / "r_light_load.png",
                         [{"ylabel": "V at the load (V)", "series": {"output": ([float(a) for a, _, _ in vals],
                                                                                 [float(b) for _, b, _ in vals])}},
                          {"ylabel": "ripple (mVpp)", "series": {"ripple": ([float(a) for a, _, _ in vals],
                                                                            [float(c) for _, _, c in vals])}}],
                         title=f"Light load at Vin {val(ctx.op['vin_nom']):g} V", xlabel="load current (mA)",
                         note=ctx.model_note)
    return chk, {"Light-load output": fig}


def s01(ctx):
    vin, io = val(ctx.op["vin_max"]), val(ctx.op["iout_max"])
    d = ctx.grid[(vin, ctx.loads[-1])]["duty"]
    tb = ctx.with_layout(ctx.testbench(vin, io), edges=True)
    edge = tb.run_edge(d, cycles=ctx.s.edge_cycles)
    lims = {}
    vin_abs, sw_max, sw_min = ctx.p("ic", "vin_abs_max", "V", "VIN abs max"), ctx.p("ic", "sw_abs_max", "V", "SW abs max"), \
        ctx.p("ic", "sw_abs_min", "V", "SW abs min")
    dur_min = ctx.p("ic", "sw_abs_min_duration", "s", "SW negative duration")
    if sw_max or vin_abs:
        lims["sw"] = [stress.Limit("abs max", sw_max or vin_abs)]
    if sw_min:
        lims.setdefault("sw", []).append(stress.Limit("abs min", sw_min, "min", dur_min))
    if vin_abs:
        lims["vic"] = [stress.Limit("abs max", vin_abs)]
    inputs = ctx.power_inputs() | {"switching edge": ctx.pd("ic", "t_edge", "switching edge"),
                                   "switch-node C": ctx.pd("ic", "c_sw", "switch-node C")}
    if lims:
        subs = stress.stress_checks(edge, lims, inputs)
    else:
        subs = [check("Switch-node peak", "VIN and SW absolute maximum ratings", f"SW peak {edge['vsw_max']:.2f} V, "
                      f"min {edge['vsw_min']:.2f} V", None, "SPICE edge-resolved transient", inputs,
                      to_complete="the IC's VIN and SW absolute maximum ratings", value=edge["vsw_max"], layout=True)]
    w = edge["waves"]
    t = (w["t"] - w["t"][0]) * 1e9
    limits = [(val(x), f"{n} {val(x):g} V") for n, x in (("SW abs max", sw_max), ("VIN abs max", vin_abs),
                                                         ("SW abs min", sw_min)) if x is not None]
    fig = figures.curves(ctx.fig / "s01_sw_edge.png",
                         [{"ylabel": "V", "series": {"SW node": (t, w["sw"]), "IC VIN pin": (t, w["vic"])},
                           "limits": limits}],
                         title=f"Switch node and VIN pin, edge-resolved, Vin {vin:g} V, {io:g} A",
                         xlabel="time in the last cycle (ns)")
    return subs, {"Switch node and VIN pin": fig}


def s02(ctx):
    ind = ctx.d.get("inductor") or {}
    curve = ind.get("l_vs_i") or {}
    io = val(ctx.op["iout_max"])
    il_peak = max(r["il_peak"] for r in ctx.grid.values())
    lim = ctx.p("ic", "i_peak_limit", "A", "IC peak current limit")
    peak = Param(val(lim), "A", lim.source, "IC current limit") if lim else \
        Param(il_peak, "A", "", "SPICE peak (no IC current limit given)")
    part = parts.Inductor(ind.get("mpn") or ctx.refs("inductor"), ctx.L, ctx.pd("inductor", "tol", "inductor tolerance"),
                          ctx.pd("inductor", "dcr", "inductor DCR"), ctx.p("inductor", "dcr_max"),
                          isat=ctx.p("inductor", "isat", "A", "Isat"), irms=ctx.p("inductor", "irms", "A", "Irms"),
                          irms_rise=ctx.p("inductor", "irms_rise", "K", "Irms rise"),
                          l_vs_i=curve.get("points"), l_vs_i_source=curve.get("source") or "")
    dil = max(r["il_pp"] for r in ctx.grid.values())
    subs = parts.inductor_checks(part, i_dc=io, di_pp=dil, i_peak_worst=peak, fsw=val(ctx.fsw), ambient_c=ctx.ambient)
    labels = ["peak in operation (SPICE)", "peak at the IC current limit" if lim else "worst peak used",
              "Isat", "RMS in operation", "Irms rating"]
    values = [il_peak, val(peak), val(part.isat) if part.isat else 0.0, math.sqrt(io ** 2 + dil ** 2 / 12),
              val(part.irms) if part.irms else 0.0]
    fig = figures.bars(ctx.fig / "s02_inductor.png", labels, values, title=f"Inductor {part.mpn}: currents against ratings",
                       xlabel="A (0 = rating not given)")
    return subs, {"Inductor currents against ratings": fig}


def s03(ctx):
    ratings = [ctx.cap_param(r, "irms_rating") for r in ctx.caps("cin")]
    known = [p for p in ratings if p is not None]
    total = Param(sum(val(p) for p in known), "A", "; ".join(p.source for p in known if p.source), "Cin RMS rating") \
        if known and len(known) == len(ratings) else None
    (v, i), r = max(ctx.grid.items(), key=lambda kv: kv[1]["icin_rms"])
    d = ctx.vout_set / v
    cf = bk.input_rms_current(i, d, bk.inductor_ripple_pp(v, ctx.vout_set, val(ctx.L), val(ctx.fsw)))
    c = check("Input capacitor RMS current", f"<= {val(total):.2f} A (sum of ratings)" if total else "<= the ratings",
              f"{r['icin_rms']:.3f} A worst at Vin {v:g} V, {i:g} A (closed form {cf:.3f} A)",
              (r["icin_rms"] <= val(total)) if total else None, "switched SPICE over the grid vs closed form",
              ctx.power_inputs() | {"rating": total}, to_complete="" if total else "each input capacitor's RMS current rating",
              value=r["icin_rms"], limit=val(total) if total else None)
    panel = {"title": "Input capacitor RMS current (A)", "ylabel": "RMS current (A)", "legend_title": "load current",
             "series": _ser(ctx, "icin_rms")}
    if total:
        panel |= {"limit": val(total), "limit_label": f"rating {val(total):.2f} A"}
    fig = figures.metric_vs_x(ctx.fig / "s03_cin_rms.png", ctx.vins, [panel], xlabel="input voltage (V)",
                              title="Input capacitor RMS current vs input voltage, one line per load current",
                              panel_width=6.5)
    return [c], {"Input capacitor RMS current vs input voltage": fig}


def s04(ctx):
    shunt = [inputfilter.Branch(val(ctx.cap_param(r, "esr")), val(ctx.cap_param(r, "esl")),
                                val(ctx.value(r)) * ctx.derate(r, ctx.vins[0])) for r in ctx.caps("cin")]
    src_r, src_l = ctx.pd("source", "r", "source R"), ctx.pd("source", "l", "source L")
    net = inputfilter.SourceNetwork(val(src_r), val(src_l), shunt)
    p_in = Param(val(ctx.op["vout"]) * val(ctx.op["iout_max"]) / 0.85, "W", "", "input power (85 % efficiency)")
    fc = Param(ctx.comp["fc"], "Hz", "", "loop crossover (generic controller design)")
    vabs = ctx.p("ic", "vin_abs_max", "V", "VIN abs max") or Param(1.5 * ctx.vins[2], "V", "", "VIN abs max (not given)")
    c_tot = sum(b.c for b in shunt)
    subs = inputfilter.checks(net, vin_min=ctx.op["vin_min"], vin_max=ctx.op["vin_max"], p_in_max=p_in, f_cross=fc,
                              margin_min=Param(6.0, "dB", "", "Middlebrook margin (6 dB, assumed)"),
                              vin_abs_max=vabs, cable_l=src_l, cable_r=src_r, c_in=Param(c_tot, "F", "", "Cin"),
                              esr_in=Param(1 / sum(1 / b.r for b in shunt), "ohm", "", "Cin ESR"))
    f = np.geomspace(100, 10 * val(ctx.fsw), 300)
    z = np.abs(net.z_out(f))
    rin = ctx.vins[0] ** 2 / val(p_in)
    fig = figures.curves(ctx.fig / "s04_input_network.png",
                         [{"ylabel": "ohm", "series": {"|Z_out| of the source network": (f, z),
                                                       "|R_in| of the converter": (f, np.full_like(f, rin))},
                           "vlines": [(val(fc), "loop crossover")]}],
                         title="Input network against the converter's input impedance", xlabel="frequency (Hz)",
                         logx=True, logy=True)
    return subs, {"Input network impedance": fig}


def s05(ctx):
    if ctx.board is None:
        raise Missing(["a board (no .kicad_pcb yet)"])
    ln = ctx.lumped
    base = ln.details["layout_parasitics"]
    io = val(ctx.op["iout_max"])
    st_src = ctx.board.stackup_source or ""
    stack = Param(base["copper_mm"], "mm", st_src, "copper thickness")
    subs = [check("Load ground return connected", "load GND reaches the IC GND through copper",
                  f"{base['gnd_return']['method']}: {'connected' if base['gnd_return']['connected'] else 'OPEN'}, "
                  f"{base['r_gnd_return_ohm'] * 1e3:.2f} mOhm", base["gnd_return"]["connected"],
                  "filled-copper solve of the saved board", {"board": Param(1, "", ln.source)}, layout=True)]
    w_req = layout.ipc2221_width_mm(io, 10.0, base["copper_mm"])
    widths = {k: v for k, v in base["min_long_width_mm"].items() if v}
    if widths:
        net_, w_min = min(widths.items(), key=lambda kv: kv[1])
        subs.append(check("Power track width (IPC-2221, 10 K rise)", f">= {w_req:.3f} mm for {io:g} A",
                          f"narrowest power track {w_min:.2f} mm ({net_})", w_min >= w_req,
                          "IPC-2221 external-layer formula on the extracted tracks", {"copper": stack},
                          value=w_min, limit=w_req, sense="min", layout=True))
    long = ln.check_lumping(val(ctx.pd("ic", "t_edge", "switching edge")))
    subs.append(check("Branches short enough to lump", "every branch below the lumping limit",
                      "; ".join(long) or "all branches below the limit", not long,
                      "length vs t_r c / (10 sqrt(er_eff))", {"edge": ctx.pd("ic", "t_edge", "switching edge")},
                      layout=True))
    if ln.unmodelled:
        subs[-1].assumptions += [f"not modelled: {u}" for u in ln.unmodelled]
        subs[-1].status = "INCOMPLETE" if subs[-1].status == "PASS" else subs[-1].status
    fig = ln.figure(ctx.fig / "s05_parasitics.png")
    return subs, {"Lumped layout parasitics": fig}


def s06(ctx):
    # quantities both methods model the same way: the inductor's triangle (the input
    # capacitors' RMS also carries the switching-edge current the closed form leaves out)
    v, i = val(ctx.op["vin_nom"]), ctx.loads[-1]
    r = ctx.grid[(v, i)]
    dil = bk.inductor_ripple_pp(v, ctx.vout_set, val(ctx.L), val(ctx.fsw))
    rows = [("inductor ripple", r["il_pp"], dil), ("inductor peak", r["il_peak"], i + dil / 2)]
    errs = [abs(s - c) / c for _, s, c in rows]
    worst = max(errs)
    c = check("SPICE against closed form", "agree within 10 %",
              "; ".join(f"{n}: SPICE {s:.3f} A, closed form {cf:.3f} A ({e:.1%})" for (n, s, cf), e in zip(rows, errs)),
              worst <= 0.10, "two independent methods at Vin nominal, full load",
              {"L": ctx.L, "fSW": ctx.fsw, "Vin": ctx.op["vin_nom"]}, value=worst, limit=0.10)
    fig = figures.bars(ctx.fig / "s06_validation.png", [n for n, _, _ in rows], [e * 100 for e in errs],
                       title=f"SPICE vs closed form at Vin {v:g} V, {i:g} A", xlabel="disagreement (%)",
                       limit=10.0, limit_label="10 %")
    return [c], {"SPICE against closed form": fig}


def slots(ctx) -> list[Slot]:
    """The fixed list. Ids follow the specification where it has the category."""
    def rid(cat, dflt):
        return ctx.req(cat, dflt)[0]

    out = [
        Slot(rid("Input range", "R-01"), "Input range", "closed-loop regulation over Vin x load",
             ["Output voltage vs input voltage"], r01, "regulates over the input range"),
        Slot(rid("Output accuracy", "R-02"), "Output accuracy", "worst-case window at the load",
             ["Output voltage window at the load"], r02, "within the output window", layout=True),
        Slot(rid("Output ripple", "R-03"), "Output ripple", "switched SPICE over Vin x load",
             ["Output ripple vs input voltage", "Input voltage and inductor current ripple vs input voltage",
              "Output voltage ripple (AC part)", "Input voltage ripple at the IC (AC part)", "Inductor current",
              "Switch-node voltage"], r03, "ripple limit"),
        Slot(rid("Load transient", "R-04"), "Load transient", "closed-loop load step", ["Load step response"], r04,
             "deviation and settling"),
        Slot(rid("Startup", "R-05"), "Startup", "closed-loop start-up", ["Start-up and restart"], r05,
             "overshoot, inrush, monotonic"),
        Slot(rid("Loop stability", "R-06"), "Loop stability", "loop gain by injection", ["Loop gain"], r06,
             "phase and gain margin"),
        Slot(rid("Efficiency", "R-07"), "Efficiency", "loss budget and switched SPICE",
             ["Efficiency vs input voltage"], r07, "efficiency limit"),
        Slot(rid("Thermal", "R-08"), "Thermal", "loss budget, theta_JA and board thermal solve",
             ["Losses at Vin max, full load", "Board temperature"], r08, "temperature limits"),
        Slot(rid("Short-circuit protection", "R-09"), "Short-circuit protection", "closed-loop overload and short",
             ["Overload and output short"], r09, "peak current within Isat; recovers"),
    ]
    for cat, short in OPTIONAL:
        r_, rec = ctx.req(cat, None)
        if rec is not None:
            fn, chart = (r_line, "Line steps") if short == "Line transient" else (r_light, "Light-load output")
            out.append(Slot(r_, cat, "closed-loop simulation", [chart], fn, rec.get("criterion", "")))
    sup = [("S-01", "Switch-node and pin stress", "edge-resolved SPICE", ["Switch node and VIN pin"], s01, True),
           ("S-02", "Inductor", "worst-case currents against the inductor's ratings",
            ["Inductor currents against ratings"], s02, False),
           ("S-03", "Input capacitor RMS current", "switched SPICE over the grid",
            ["Input capacitor RMS current vs input voltage"], s03, False),
           ("S-04", "Input network", "Middlebrook margin and hot-plug", ["Input network impedance"], s04, False),
           ("S-05", "Layout copper and parasitics", "extraction from the saved board", ["Lumped layout parasitics"],
            s05, True),
           ("S-06", "Model validation", "SPICE against closed form", ["SPICE against closed form"], s06, False)]
    for sid, name, method, charts, fn, lay in sup:
        cat = SUPPLEMENTARY_SPEC.get(sid)
        out.append(Slot(rid(cat, sid) if cat else sid, name, method, charts, fn, layout=lay))
    return out


# ── inputs template ───────────────────────────────────────────────────────

def _entry(value=None, unit="", source=""):
    return {"value": value, "unit": unit, "source": source}


def _num(text: str, pattern: str):
    m = re.search(pattern, text, re.I)
    return float(m.group(1)) if m else None


def _guess_refs(net) -> dict:
    comps = net.components
    ind = next((r for r in comps if r.startswith("L") and not comps[r].dnp), None)
    ic = max((r for r in comps if r.startswith("U")), key=lambda r: len(comps[r].pins), default=None)
    gnd = next((n for n in net.nets if re.search(r"gnd", n, re.I)), None)
    out = {"ic": ic, "inductor": ind}
    if ind and ic:
        # VOUT is the inductor net with the output capacitors; SW the other (a fixed-output
        # IC's FB pin also sits on VOUT, so an IC pin alone does not identify SW)
        l_nets = sorted(set(comps[ind].pins.values()),
                        key=lambda n: len(net.between(n, gnd, "C")) if gnd else 0)
        sw, vout = (l_nets[0], l_nets[-1]) if len(l_nets) == 2 else (None, None)
        vin = None
        for pin, n in comps[ic].pins.items():
            if n not in (sw, vout, gnd) and gnd and any(c.ref.startswith("C") for c in net.between(n, gnd, "C")):
                if vin is None or len(net.between(n, gnd, "C")) > len(net.between(vin, gnd, "C")):
                    vin = n
        if gnd:
            out["cin"] = [c.ref for c in net.between(vin, gnd, "C")] if vin else []
            out["cout"] = [c.ref for c in net.between(vout, gnd, "C")] if vout else []
        pin_of = {n: p for p, n in comps[ic].pins.items()}
        out.update({"ic_vin": pin_of.get(vin), "ic_sw": pin_of.get(sw), "ic_gnd": pin_of.get(gnd)})
        # feedback divider: R_top from VOUT to a net on an IC pin, R_bot from that net to GND
        for top in (r for r in comps if r.startswith("R") and vout in comps[r].pins.values()):
            mid = next((n for n in comps[top].pins.values() if n != vout), None)
            bot = next((r for r in comps if r.startswith("R") and r != top and mid in comps[r].pins.values()
                        and gnd in comps[r].pins.values()), None)
            if bot and mid in pin_of:
                out["r_top"], out["r_bot"] = top, bot
                out["sense"] = [top, next(p for p, n in comps[top].pins.items() if n == vout)]
                break
        else:
            # no divider: a fixed-output IC senses VOUT on one of its own pins
            if vout in pin_of:
                out["sense"] = [ic, pin_of[vout]]
        out["sense_gnd"] = [ic, pin_of[gnd]] if gnd in pin_of else None
        out["nets"] = {"VIN": vin, "SW": sw, "VOUT": vout, "GND": gnd}
    return out


def template(project: str | Path, iteration: int | None = None) -> Path:
    """Write (or complete) ``iteration<N>/sim/inputs/buck_design.json``: references and nets
    guessed from the schematic, limits parsed from the accepted specification, every other
    value null for the assistant to fill with a source. Values already in the file are kept."""
    project = Path(project)
    F = (Folders(project, iteration) if iteration else Folders.latest(project)).make()
    path = F.inputs / INPUTS_FILE
    old = load_json(path)
    spec = dsn.load(project, dsn.SPEC) or {}
    sch = next(project.glob("*.kicad_sch"), None)
    pcb = next(project.glob("*.kicad_pcb"), None)
    refs = {}
    if sch:
        try:
            refs = _guess_refs(netlist.load(sch))
        except Exception:
            refs = {}
    nets = refs.pop("nets", {})
    cite = lambda rid: (f"design/records/01_specification.json {rid} rev {spec.get('revision')} (accepted)"  # noqa: E731
                        if spec.get("status") == dsn.ACCEPTED else "")
    reqs = {_key(r["category"]): r for r in spec.get("requirements", [])}

    def lim(cat, pattern, scale=1.0, unit=""):
        r = reqs.get(_key(cat))
        if not r:
            return _entry(unit=unit)
        x = _num(r["criterion"], pattern)
        return _entry(x * scale if x is not None else None, unit, cite(r["id"]) if x is not None else "")

    text = " ".join(f"{r['parameter']} {r['condition']} {r['criterion']}" for r in spec.get("requirements", [])) \
        + " " + spec.get("title", "") + " " + spec.get("brief", "")
    rng = re.search(r"(\d+(?:\.\d+)?)\s*(?:-|–|to)\s*(\d+(?:\.\d+)?)\s*V\b", text)
    vout = _num(text, r"(\d+(?:\.\d+)?)\s*V\s*(?:out|output)") or _num(text, r"to\s*(\d+(?:\.\d+)?)\s*V")
    iout = _num(text, r"(\d+(?:\.\d+)?)\s*A\s*(?:max|maximum|load|out)")
    src_spec = (f"design/records/01_specification.json rev {spec.get('revision')} (accepted)"
                if spec.get("status") == dsn.ACCEPTED else "")
    tr = reqs.get(_key("Load transient"))
    tr_src = cite(tr["id"]) if tr and re.search(r"25\s*%\s*to\s*75\s*%", tr["condition"]) else ""
    doc = {
        "suite": NAME, "version": VERSION,
        "schematic": sch.name if sch else None, "board": pcb.name if pcb else None, "stackup_file": None,
        "operating": {"vin_min": _entry(float(rng.group(1)) if rng else None, "V", src_spec if rng else ""),
                      "vin_nom": _entry(None, "V"),
                      "vin_max": _entry(float(rng.group(2)) if rng else None, "V", src_spec if rng else ""),
                      "vout": _entry(vout, "V", src_spec if vout else ""),
                      "iout_max": _entry(iout, "A", src_spec if iout else "")},
        "limits": {
            "Output accuracy": {"lo": _entry(unit="V"), "hi": _entry(unit="V"),
                                "pct": lim("Output accuracy", r"±\s*(\d+(?:\.\d+)?)\s*%", 0.01)},
            "Output ripple": {"max": lim("Output ripple", r"(\d+(?:\.\d+)?)\s*mV", 1e-3, "V")},
            "Load transient": {"max_dev": lim("Load transient", r"±\s*(\d+(?:\.\d+)?)\s*%", 0.01),
                               "settle": lim("Load transient", r"(\d+(?:\.\d+)?)\s*µs", 1e-6, "s"),
                               "step_from": _entry(0.25, "", tr_src), "step_to": _entry(0.75, "", tr_src),
                               "slew": _entry(1e6, "A/s", tr_src)},
            "Startup": {"overshoot": _entry(unit="V"), "inrush": _entry(unit="A")},
            "Loop stability": {"pm": lim("Loop stability", r"phase margin\s*[≥>=]+\s*(\d+)", 1, "deg"),
                               "gm": lim("Loop stability", r"gain margin\s*[≥>=]+\s*(\d+)", 1, "dB")},
            "Efficiency": {"min": lim("Efficiency", r"[≥>=]+\s*(\d+(?:\.\d+)?)\s*%", 0.01)},
            "Thermal": {"tj_max": lim("Thermal", r"Tj\s*[≤<=]+\s*(?:Tj,max\s*[−-]\s*\d+\s*°C\s*\(e\.g\.\s*[≤<=]+\s*)?(\d+)",
                                      1, "°C"), "ambient": _entry(unit="°C")},
            "Line transient": {"max_dev": lim("Line transient", r"±\s*(\d+(?:\.\d+)?)\s*%", 0.01)},
            "Light-load behaviour": {"ripple": _entry(unit="V")},
        },
        "refs": {k: refs.get(k) for k in ("ic", "ic_vin", "ic_gnd", "ic_sw", "inductor", "cin", "cout", "r_top",
                                          "r_bot", "sense", "sense_gnd")}
        | {"load_vout": None, "load_gnd": None, "source_vin": None},
        "nets": nets or None,
        "ic": {"mpn": None, "controller": None, "fixed_output": False, "validated_by": "", "light_load_mode": None,
               **{k: _entry(unit=u) for k, u in (
                   ("fsw", "Hz"), ("vref_min", "V"), ("vref_typ", "V"), ("vref_max", "V"), ("rds_hs", "ohm"),
                   ("rds_ls", "ohm"), ("rds_tempco", "1/K"), ("i_peak_limit", "A"), ("i_valley_limit", "A"),
                   ("t_ss", "s"), ("uvlo_on", "V"), ("uvlo_off", "V"), ("t_on_min", "s"), ("t_off_min", "s"),
                   ("t_dead", "s"), ("t_edge", "s"), ("c_sw", "F"), ("t_rise", "s"), ("t_fall", "s"),
                   ("vin_abs_max", "V"), ("sw_abs_max", "V"), ("sw_abs_min", "V"), ("sw_abs_min_duration", "s"),
                   ("theta_ja", "K/W"), ("tj_max", "°C"), ("iq", "A"), ("qg_total", "C"))}},
        "inductor": {"mpn": None, "l_vs_i": {"points": None, "source": ""},
                     **{k: _entry(unit=u) for k, u in (("dcr", "ohm"), ("dcr_max", "ohm"), ("tol", ""),
                                                       ("isat", "A"), ("irms", "A"), ("irms_rise", "K"))}},
        "capacitors": {r: {"mpn": None, "esr": _entry(unit="ohm"), "esl": _entry(unit="H"),
                           "v_rating": _entry(unit="V"), "irms_rating": _entry(unit="A"),
                           "dc_bias": {"points": None, "source": ""}}
                       for r in (refs.get("cin") or []) + (refs.get("cout") or [])},
        "source": {"r": _entry(unit="ohm"), "l": _entry(unit="H")},
        "design": {"resistor_tol_pct": _entry(unit="%"), "er": _entry(unit="")},
    }

    def merge(new, have):
        if isinstance(new, dict) and isinstance(have, dict):
            return {k: merge(new.get(k), have[k]) if k in have else v for k, v in new.items()} | \
                {k: v for k, v in have.items() if k not in new}
        if isinstance(have, dict) and have.get("value") is None and isinstance(new, dict):
            return new
        return have if have not in (None, "", []) else new

    doc = merge(doc, old) if old else doc
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False), encoding="utf-8")
    return path


# ── run ───────────────────────────────────────────────────────────────────

def _normalise(d: dict) -> dict:
    """Fill derived entries: the output window from a percentage, Vin nominal from the range."""
    d = json.loads(json.dumps(d))
    op = d.setdefault("operating", {})
    if (op.get("vin_nom") or {}).get("value") is None and (op.get("vin_min") or {}).get("value") is not None \
            and (op.get("vin_max") or {}).get("value") is not None:
        op["vin_nom"] = {"value": (op["vin_min"]["value"] + op["vin_max"]["value"]) / 2, "unit": "V",
                         "source": "midpoint of the input range"}
    acc = (d.setdefault("limits", {}).setdefault("Output accuracy", {}))
    pct = (acc.get("pct") or {}).get("value")
    vo = (op.get("vout") or {}).get("value")
    if pct is not None and vo is not None:
        for k, sgn in (("lo", -1), ("hi", 1)):
            if (acc.get(k) or {}).get("value") is None:
                acc[k] = {"value": vo * (1 + sgn * pct), "unit": "V", "source": acc["pct"].get("source", "")}
    return d


def run(project: str | Path, iteration: int | None = None, settings: Settings = SETTINGS, extra=None,
        documents=("pdf",)) -> dict:
    """Run the standard buck verification on ``project`` and write
    ``iteration<N>/reports/buck_verification.pdf`` (charts in ``sim/figures``, the checks and
    the SPICE grid as JSON in ``sim/results``). ``extra``: a function ctx -> [Check] whose
    checks follow the standard ones; it never replaces or reorders them."""
    project = Path(project)
    F = (Folders(project, iteration) if iteration else Folders.latest(project)).make()
    path = F.inputs / INPUTS_FILE
    if not path.is_file():
        template(project, F.iteration)
    inputs = _normalise(load_json(path))
    ctx = Context(project, F, inputs, settings)
    checks = run_slots(slots(ctx), ctx, F.figures)
    if extra is not None:
        for c in extra(ctx):
            c.name = c.name if c.name.startswith("X-") else f"X- {c.name}"
            checks.append(c)
    header = Header(NAME, VERSION, str(path), file_hash(path), asdict(settings))
    rep = Report(f"Buck converter verification (standard suite v{VERSION})",
                 f"{ctx.sch.name if ctx.sch else 'no schematic'} / {ctx.pcb.name if ctx.pcb else 'no board'} ({F.run})",
                 checks=checks, notes=header.notes())
    try:
        rep.notes.append("Closed-loop checks: " + ctx.model_note + ".")
    except Exception:
        rep.notes.append("Closed-loop checks: no controller model could be built (see R-01).")
    if ctx.assumed_defaults:
        rep.notes.append("Suite defaults used where the inputs file has no value (assumed): "
                         + ", ".join(sorted(set(ctx.assumed_defaults))) + ".")
    if ctx.board is None:
        rep.notes.append("No board yet: every check uses ideal copper.")
    doc, js = rep.write(F.iteration_reports, "buck_verification", documents=documents)
    data = json.loads(Path(js).read_text(encoding="utf-8"))
    data["suite"] = {"name": NAME, "version": VERSION, "inputs": str(path), "inputs_hash": header.inputs_hash,
                     "settings": header.settings}
    Path(js).write_text(json.dumps(data, indent=1), encoding="utf-8")
    counts = {s: sum(c.status == s for c in checks) for s in ("PASS", "FAIL", "INCOMPLETE")}
    return {"verdict": rep.verdict, "counts": counts, "checks": [c.name for c in checks],
            "charts": [f["title"] for c in checks for f in c.figures], "pdf": str(doc), "json": str(js),
            "inputs": str(path)}
