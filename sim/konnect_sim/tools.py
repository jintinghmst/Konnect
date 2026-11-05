"""Circuit-independent simulation tools, as JSON-in / JSON-out functions.

Each function here is one ``sim_*`` tool of Konnect's ``simulation`` toolset,
served through ``cli.py``, and can also be called directly from Python. None of them
assumes a particular circuit: they work on any KiCad schematic or saved board.
Topology-specific models (``buck``, ``converter``, ``losses``, ``scenarios``,
``stress``) stay library code for now.

Every tool that produces a verdict returns it under the evidence rule of
``report``: values given as {"value": v, "source": "..."} are sourced, plain
numbers are assumptions, and a check with any assumption is INCOMPLETE.

Board tools read the *saved* .kicad_pcb (read-only): refill zones and save in
KiCad first. Coordinates are board millimetres, pads are [reference, pad].

Every simulation tool also draws its result (``konnect_sim.figures``) and returns the
graphs as ``figures`` [{"title", "path"}], attached to its checks as well, so
``write_report`` embeds them. They go to ``figures_dir`` (the project's
``sim/figures/<run>``); without one, to a ``konnect_sim/figures`` folder in the
system temporary directory.
"""
from __future__ import annotations

import ast
import json
import math
import operator
import re
import tempfile
from dataclasses import asdict
from pathlib import Path

import numpy as np

from . import (copper, corners, cycle, emi, figures, inputfilter, layout, netlist, parts, report, stackup, stress,
               thermal)
from .report import Param


# ── helpers ────────────────────────────────────────────────────────────────

def _param(x, unit: str = "", name: str = "") -> Param | None:
    """{"value": v, "source": s} -> sourced Param; a bare number -> assumed."""
    if x is None:
        return None
    if isinstance(x, dict):
        return Param(float(x["value"]), x.get("unit", unit), x.get("source", "") or "", x.get("name", name))
    return Param(float(x), unit, "", name)


def _board(board: str, stackup_json: str | None = None):
    b = layout.load(board)
    st = stackup.from_json(stackup_json) if stackup_json else stackup.from_board(board)
    if st is not None:
        st.apply(b)
    return b, st


def _pads(pairs) -> list[tuple[str, str]]:
    return [(str(r), str(p)) for r, p in pairs]


def _check_dict(c) -> dict:
    d = asdict(c)
    d["meets"], d["margin"] = c.meets, c.margin
    return d


def _num(x):
    return None if x is None else float(x)


def _fig_path(figures_dir: str | None, *parts) -> Path:
    """``<figures_dir>/<parts joined>.png``; the system temp folder when no folder is given."""
    folder = Path(figures_dir) if figures_dir else Path(tempfile.gettempdir()) / "konnect_sim" / "figures"
    name = "_".join(re.sub(r"[^A-Za-z0-9._+-]+", "-", str(x)).strip("-") for x in parts if str(x))
    return folder / f"{name[:120]}.png"


def _graphed(figs: list[dict], checks: list) -> list[dict]:
    """Attach the tool's graphs to each of its checks; return them as dicts."""
    out = []
    for c in checks:
        c.figures = list(figs)
        out.append(_check_dict(c))
    return out


def _checks_figure(figures_dir, stem: str, checks: list, title: str) -> list[dict]:
    """Fallback graph: the checks' margins to their limits."""
    path = figures.checks_vs_limits(_fig_path(figures_dir, stem), checks, title=title)
    return [{"title": title, "path": path}] if path else []


def _extent(g) -> tuple:
    return (g.x0, g.x0 + g.nx * g.pitch_mm, g.y0 + g.ny * g.pitch_mm, g.y0)


# ── schematic and stackup ──────────────────────────────────────────────────

def read_schematic(schematic: str) -> dict:
    """Components (value, footprint, fields such as MPN, pin -> net) and nets,
    from KiCad's own netlist export."""
    d = netlist.load(schematic)
    comps = {r: {"value": c.value, "footprint": c.footprint, "dnp": c.dnp, "pins": c.pins,
                 "fields": {k: v for k, v in c.fields.items() if v},
                 "parsed": {"value": c.parsed.value, "unit": c.parsed.unit,
                            "voltage_rating": c.parsed.voltage_rating,
                            "tolerance_pct": c.parsed.tolerance_pct} if c.parsed else None}
             for r, c in d.components.items()}
    return {"schematic": schematic, "components": comps,
            "nets": {n: [list(x) for x in v] for n, v in d.nets.items()}}


def board_stackup(board: str, stackup_json: str | None = None) -> dict:
    """The stackup every board tool uses: the board's own (Board Setup), or a
    fabricator stackup JSON with its source. Reports which, or that none exists."""
    st = stackup.from_json(stackup_json) if stackup_json else stackup.from_board(board)
    if st is None:
        return {"sourced": False, "source": None,
                "note": "board has no stackup: set it in Board Setup or pass a fab stackup JSON"}
    cu = st.copper_layers
    gaps = {f"{a}-{b}": st.gap_mm(a, b).value for a, b in zip(cu, cu[1:])}
    return {"sourced": True, "source": st.source, "thickness_mm": st.thickness_mm,
            "layers": [asdict(l) for l in st.layers], "copper_layers": cu, "dielectric_gaps_mm": gaps}


# ── copper ─────────────────────────────────────────────────────────────────

def copper_connectivity(board: str, net: str, pads: list | None = None, pitch_mm: float = 0.1,
                        stackup_json: str | None = None, figures_dir: str | None = None) -> dict:
    """Which pads of ``net`` share copper (pours with their thermal reliefs,
    tracks, pads, vias). ``pads`` defaults to every pad of the net. Graph: the
    net's copper per layer, one colour per group of joined pads."""
    b, _ = _board(board, stackup_json)
    g = copper.build(b, net, pitch_mm)
    plist = _pads(pads) if pads else [(p.ref, p.number) for f in b.footprints.values()
                                      for p in f.pads.values() if p.net == net]
    con = copper.connectivity(g, plist)
    lab = copper.components(g)
    group_of = {}
    for k, grp in enumerate(con["groups"]):
        for ref, num in grp:
            for n in g.pad_nodes(ref, num):
                group_of.setdefault(int(lab[n]), min(k, 6))
    layers = {}
    for layer in g.layers:
        a = np.full(g.mask[layer].shape, np.nan)
        on = g.mask[layer]
        a[on] = [group_of.get(int(lab[n]), 7) for n in g.node[layer][on]]
        layers[layer] = a
    marks = [(b.pad(r, n).x, b.pad(r, n).y, f"{r}.{n}") for r, n in plist if (r, n) not in con["missing"]][:40]
    title = f"Copper of {net}: " + ("all pads joined" if con["connected"] else f"{len(con['groups'])} separate groups")
    path = figures.board_map(_fig_path(figures_dir, "copper_connectivity", net), layers, _extent(g), title=title,
                             label="colour = group of joined pads, grey = copper joined to no listed pad",
                             marks=marks, categorical=True)
    return {"net": net, "connected": con["connected"], "groups": con["groups"], "missing": con["missing"],
            "unfilled_zones": g.unfilled_zones, "resolution": g.resolution_note,
            "evidence": f"saved board fill ({Path(board).name})", "figures": [{"title": title, "path": path}]}


def copper_path(board: str, net: str, source_pads: list, sink_pads: list, current_a: float = 1.0,
                rms_a: float | None = None, rise_c: float | None = None, plane_gap_mm: float | None = None,
                pitch_mm: float = 0.1, stackup_json: str | None = None, figures_dir: str | None = None) -> dict:
    """DC solve of ``net`` between pad groups: resistance, voltage drop at
    ``current_a``, peak current density (1 mm averaged; raw and pad-entry
    peaks too), current in every via barrel, and with ``rms_a`` + ``rise_c``
    the IPC-2221 copper and via capacity verdict. ``plane_gap_mm`` adds a
    low-frequency partial-inductance estimate. Graph: current density per layer."""
    b, st = _board(board, stackup_json)
    g = copper.build(b, net, pitch_mm)
    f = copper.solve(g, _pads(source_pads), _pads(sink_pads), current_a)
    out = {"net": net, "connected": f.connected, "resolution": g.resolution_note,
           "unfilled_zones": g.unfilled_zones, "stackup_source": st.source if st else None}
    if not f.connected:
        return out | {"note": "source and sink pads are not joined by copper"}
    out |= {"resistance_ohm": f.resistance_ohm, "drop_v": f.resistance_ohm * current_a,
            "via_currents": [{"via": l, "current_a": i * (rms_a or current_a) / current_a, "drill_mm": d}
                             for l, i, d in f.via_currents]}
    if rms_a and rise_c:
        cap = copper.capacity(f, rms_a, rise_c)
        assumed = [] if st else ["copper thickness"]
        assumed.append("via plating thickness")
        ok = cap["copper_ok"] and cap["vias_ok"]
        out["capacity"] = cap | {"ok": ok, "status": report.INCOMPLETE if assumed else (report.PASS if ok else report.FAIL),
                                 "assumptions": assumed}
    if plane_gap_mm:
        out["plane_partial_inductance_h"] = copper.plane_inductance_estimate(f, plane_gap_mm)
    i_plot = rms_a or current_a
    layers = {layer: np.where(g.mask[layer], k * i_plot / current_a, np.nan) for layer, k in f.sheet_current.items()}
    pk = f.peak()
    marks = [(b.pad(r, n).x, b.pad(r, n).y, f"{r}.{n}") for r, n in _pads(source_pads) + _pads(sink_pads)]
    marks.append((pk[2], pk[3], f"peak {pk[0] * i_plot / current_a:.2f} A/mm", pk[1]))
    title = (f"Current density in {net} at {i_plot:g} A: R = {f.resistance_ohm * 1e3:.2f} mOhm, "
             f"drop {f.resistance_ohm * i_plot * 1e3:.1f} mV")
    path = figures.board_map(_fig_path(figures_dir, "copper_path", net), layers, _extent(g), title=title,
                             label="sheet current density (A/mm)", marks=marks)
    out["figures"] = [{"title": title, "path": path}]
    if "capacity" in out:
        out["capacity"]["figures"] = out["figures"]
    return out


def trace_coupling(board: str, aggressor_net: str, victim_net: str, er_eff: float | None = None,
                   max_sep_mm: float = 5.0, dv: dict | float | None = None, t_edge: dict | float | None = None,
                   r_victim_ohm: float | None = None, c_victim: dict | float | None = None,
                   allowed_v: dict | float | None = None, stackup_json: str | None = None,
                   figures_dir: str | None = None) -> dict:
    """Mutual capacitance between parallel track runs of two nets (wires over a
    ground plane), and with an aggressor swing ``dv`` in ``t_edge`` into a
    victim of Thevenin resistance ``r_victim_ohm`` the coupled noise check.
    Graph: mutual capacitance of every coupled run."""
    b, st = _board(board, stackup_json)
    # the reference plane is the next copper layer under F.Cu (In1 on a multilayer board)
    coppers = [n for n, typ, _ in (b.stack or []) if typ == "copper"]
    ref = coppers[1] if len(coppers) > 1 else "B.Cu"
    h = b.layer_gap_mm("F.Cu", ref)
    if er_eff is None:
        er = st.er("F.Cu", ref).value if st and st.er("F.Cu", ref).value else 4.5
        er_eff = (er + 1) / 2
    res = emi.sw_fb_coupling(b, aggressor_net, victim_net, h, er_eff, max_sep_mm)
    title = f"Coupling {aggressor_net} -> {victim_net}: {res['c_mutual_f'] * 1e12:.3g} pF in total"
    path = figures.bars(_fig_path(figures_dir, "trace_coupling", aggressor_net, victim_net),
                        [f"{r['layer']}: {r['overlap_mm']} mm parallel at {r['sep_mm']} mm" for r in res["runs"]],
                        [r["c_f"] * 1e12 for r in res["runs"]], title=title, xlabel="mutual capacitance (pF)",
                        empty_text=f"no parallel track runs closer than {max_sep_mm} mm")
    figs = [{"title": title, "path": path}]
    out = {"c_mutual_f": res["c_mutual_f"], "runs": res["runs"], "height_mm": h, "er_eff": er_eff,
           "note": "track runs only; pads and pours next to the victim are not included", "figures": figs}
    if dv is not None and t_edge is not None and r_victim_ohm and allowed_v is not None:
        c = emi.fb_noise_check(Param(res["c_mutual_f"], "F", "track geometry extraction", "mutual C"),
                               _param(dv, "V", "aggressor swing"), _param(t_edge, "s", "edge time"),
                               r_victim_ohm, 1e99, _param(allowed_v, "V", "allowed noise"),
                               c_fb=_param(c_victim, "F", "victim node C"))
        out["check"] = _graphed(figs, [c])[0]
    return out


# ── thermal ────────────────────────────────────────────────────────────────

def board_thermal(board: str, ambient_c: float, h_w_m2k: dict | float, k_inplane: dict | float,
                  k_through: dict | float, footprint_watts: dict, package_resistance: dict | None = None,
                  limits_c: dict | None = None, board_limit_c: dict | float | None = None,
                  copper_flows: list | None = None, pitch_mm: float = 0.5, png: str | None = None,
                  stackup_json: str | None = None, figures_dir: str | None = None) -> dict:
    """Steady-state board temperature from footprint losses ({ref: W}) and
    optional copper I^2R flows ([{net, source_pads, sink_pads, rms_a}]).
    ``package_resistance`` {ref: K/W or {value, source}} adds component
    temperatures above their board spot. Graph: the temperature map of F.Cu,
    saved to ``png`` when given."""
    b, st = _board(board, stackup_json)
    flows = []
    for fl in copper_flows or []:
        g = copper.build(b, fl["net"], 0.1)
        f = copper.solve(g, _pads(fl["source_pads"]), _pads(fl["sink_pads"]), 1.0)
        if f.connected:
            k = float(fl["rms_a"]) ** 2
            f.power_w = {l: p * k for l, p in f.power_w.items()}
            flows.append(f)
    h, ki, kt = _param(h_w_m2k, "W/m2K", "film coefficient"), _param(k_inplane, "W/mK", "laminate k in-plane"), \
        _param(k_through, "W/mK", "laminate k through")
    res = thermal.solve(b, ambient_c=ambient_c, h_w_m2k=h, k_laminate_inplane=ki, k_laminate_through=kt,
                        footprint_watts={k: float(v) for k, v in footprint_watts.items()},
                        copper_flows=flows, pitch_mm=pitch_mm)
    psi = {ref: (_param(r, "K/W", f"{ref} package resistance"), float(footprint_watts.get(ref, 0)))
           for ref, r in (package_resistance or {}).items()}
    lim = {ref: _param(v, "°C", f"{ref} limit") for ref, v in (limits_c or {}).items()}
    checks = thermal.board_checks(res, b, h_w_m2k=h, k_laminate_inplane=ki, k_laminate_through=kt,
                                  max_board_c=_param(board_limit_c, "°C", "board limit"), psi=psi, limits=lim)
    peak = res.peak()
    out = {"peak_c": peak[0], "peak_layer": peak[1], "peak_xy_mm": [peak[2], peak[3]],
           "footprint_board_c": {r: res.at(f.layer if f.layer in res.layers else res.layers[0], f.x, f.y)
                                 for r, f in b.footprints.items()},
           "checks": []}
    png = png or str(_fig_path(figures_dir, "board_thermal"))
    if png:
        Path(png).parent.mkdir(parents=True, exist_ok=True)
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        t = res.temp_c[res.layers[0]]
        fig, ax = plt.subplots(figsize=(7, 4.5))
        im = ax.imshow(t, origin="upper", cmap="inferno", extent=[res.x0, res.x0 + t.shape[1] * res.pitch_mm,
                                                                   res.y0 + t.shape[0] * res.pitch_mm, res.y0])
        fig.colorbar(im, label=f"°C ({res.layers[0]})")
        ax.set_xlabel("mm")
        ax.set_title(f"Board temperature at {ambient_c:g} °C ambient: peak {peak[0]:.1f} °C")
        fig.tight_layout()
        fig.savefig(png, dpi=120)
        plt.close(fig)
        out["png"] = png
    out["figures"] = [{"title": f"Board temperature ({res.layers[0]})", "path": png}]
    out["checks"] = _graphed(out["figures"], checks)
    return out


# ── SPICE ──────────────────────────────────────────────────────────────────

def spice_run(netlist_text: str, measures: list | None = None, csv: str | None = None,
              plot: list | None = None, figures_dir: str | None = None) -> dict:
    """Run a complete SPICE netlist in KiCad's ngspice and summarise vectors.
    ``measures``: [{vector, kind: max|min|avg|pp|rms|final|at, t_from?, t_to?, at?}]
    (``t`` means the sweep variable: time, frequency or the DC source). AC
    vectors are reported as magnitude. ``csv`` saves every vector. Graph: the
    ``plot`` vectors (default: the measured ones, else up to six) against the
    sweep, one panel each, with the measured values marked."""
    from .spice import run_netlist
    res = run_netlist(netlist_text)
    axis = next((k for k in ("time", "frequency", "v-sweep") if k in res), None)
    x = np.asarray(res[axis]) if axis else None
    summary = {}
    for k, v in res.items():
        a = np.abs(v) if np.iscomplexobj(v) else np.asarray(v, float)
        if a.size:
            summary[k] = {"min": float(a.min()), "max": float(a.max()), "final": float(a[-1]), "points": int(a.size)}
    out = {"axis": axis, "vectors": summary}
    if measures:
        vals = []
        for mspec in measures:
            v = res[mspec["vector"]]
            a = np.abs(v) if np.iscomplexobj(v) else np.asarray(v, float)
            sel = np.ones_like(a, bool) if x is None else (x >= mspec.get("t_from", -np.inf)) & (x <= mspec.get("t_to", np.inf))
            kind, w = mspec["kind"], a[sel]
            if kind == "at":
                r = float(np.interp(mspec["at"], x, a))
            elif kind == "avg":
                r = float(np.trapezoid(w, x[sel]) / (x[sel][-1] - x[sel][0])) if x is not None else float(w.mean())
            elif kind == "rms":
                r = float(math.sqrt(np.trapezoid(w ** 2, x[sel]) / (x[sel][-1] - x[sel][0])))
            else:
                r = float({"max": np.max, "min": np.min, "pp": np.ptp, "final": lambda q: q[-1]}[kind](w))
            vals.append(mspec | {"value": r})
        out["measures"] = vals
    if csv:
        keys = [k for k in res if np.asarray(res[k]).size == (x.size if x is not None else 0)]
        data = np.column_stack([np.abs(res[k]) if np.iscomplexobj(res[k]) else res[k] for k in keys])
        np.savetxt(csv, data, delimiter=",", header=",".join(keys), comments="")
        out["csv"] = csv
    out["figures"] = _spice_figure(netlist_text, res, axis, x, plot, out.get("measures", []), figures_dir)
    return out


def _spice_figure(netlist_text, res, axis, x, plot, measures, figures_dir) -> list[dict]:
    first = next((ln.strip().lstrip("*").strip() for ln in netlist_text.splitlines() if ln.strip()), "spice")
    stem = ("spice", first[:40])
    names = list(plot or dict.fromkeys(m["vector"] for m in measures) or
                 [k for k in res if k != axis and np.asarray(res[k]).size == (x.size if x is not None else 1)][:6])
    names = [n for n in names if n in res]
    if not names:
        return []

    def mag(v):
        return np.abs(v) if np.iscomplexobj(v) else np.asarray(v, float)

    if x is None:                                          # operating point: one value per vector
        path = figures.bars(_fig_path(figures_dir, *stem), names, [float(mag(res[n])[0]) for n in names],
                            title=f"Operating point: {first}", xlabel="value (V or A)")
        return [{"title": f"Operating point: {first}", "path": path}]
    scale, xlabel = {"time": (1e6, "time (µs)"), "frequency": (1.0, "frequency (Hz)")}.get(axis, (1.0, axis))
    panels = []
    for n in names:
        y = mag(res[n])
        marks, notes = [], []
        for m in measures:
            if m["vector"] != n:
                continue
            sel = (x >= m.get("t_from", -np.inf)) & (x <= m.get("t_to", np.inf))
            if m["kind"] in ("max", "min") and sel.any():
                k = np.flatnonzero(sel)[int(np.argmax(y[sel]) if m["kind"] == "max" else np.argmin(y[sel]))]
                marks.append((x[k] * scale, y[k], f"{m['kind']} {m['value']:.4g}"))
            elif m["kind"] == "at":
                marks.append((m["at"] * scale, m["value"], f"at {m['at']:.3g}: {m['value']:.4g}"))
            elif m["kind"] == "final":
                marks.append((x[-1] * scale, m["value"], f"final {m['value']:.4g}"))
            else:
                notes.append(f"{m['kind']} {m['value']:.4g}")
        label = n + (f"  ({', '.join(notes)})" if notes else "")
        panels.append({"ylabel": label if len(label) < 28 else n, "series": {label: (x * scale, y)}, "marks": marks})
    ac = axis == "frequency"
    title = f"{'AC magnitude' if ac else 'SPICE ' + axis}: {first}"
    path = figures.curves(_fig_path(figures_dir, *stem), panels, title=title, xlabel=xlabel, logx=ac,
                          logy=ac and all(np.all(mag(res[n]) > 0) for n in names))
    return [{"title": title, "path": path}]


# ── components ─────────────────────────────────────────────────────────────

def capacitor_check(mpn: str, c_f: dict | float, tol: dict | float, v_rating: dict | float, count: int,
                    role: str, v_max: dict | float, v_dc: float, c_needed: dict | float | None = None,
                    derating: dict | float | None = None, dc_bias: list | None = None, dc_bias_source: str = "",
                    temp_coef: dict | float | None = None, esr_ohm: dict | float | None = None,
                    ripple_rating_a: dict | float | None = None, ripple_rise_k: dict | float | None = None,
                    irms_part_a: float = 0.0, loss_part_w: float = 0.0, max_rise_k: dict | float | None = None,
                    figures_dir: str | None = None) -> dict:
    """Voltage rating vs applied maximum and derating, effective capacitance
    (tolerance, DC bias, temperature), ripple current and self-heating.
    Graph: the DC-bias curve with the working and maximum voltages and the
    rating; without a curve, the checks against their limits."""
    cap = parts.Capacitor(mpn, _param(c_f, "F", "C"), _param(tol, "", "tolerance"), _param(v_rating, "V", "rating"),
                          count, [tuple(x) for x in dc_bias] if dc_bias else None, dc_bias_source,
                          _param(temp_coef, "", "temperature coefficient"), _param(esr_ohm, "ohm", "ESR"), "",
                          None, _param(ripple_rating_a, "A", "ripple rating"), _param(ripple_rise_k, "K", "ripple rise"))
    cs = parts.capacitor_checks(cap, role, v_max=_param(v_max, "V", "applied max"), irms_part=irms_part_a,
                                loss_part_w=loss_part_w, c_needed=_param(c_needed, "F", "required C"), v_dc=v_dc,
                                derating=_param(derating, "", "derating"), max_rise=_param(max_rise_k, "K", "max rise"))
    if dc_bias:
        pts = sorted(tuple(x) for x in dc_bias)
        r_dc = parts._interp(pts, v_dc)
        title = f"{mpn}: capacitance vs DC bias ({role})"
        path = figures.curves(_fig_path(figures_dir, "capacitor", mpn, role), [{
            "ylabel": "C / C_nom", "series": {"manufacturer curve": ([p[0] for p in pts], [p[1] for p in pts])},
            "marks": [(v_dc, r_dc, f"working {v_dc:g} V: {r_dc:.2f}")],
            "vlines": [(_param(v_max).value, "applied max"), (_param(v_rating).value, "rating")]}],
            title=title, xlabel="DC bias (V)", note=f"curve: {dc_bias_source or 'unsourced'}")
        figs = [{"title": title, "path": path}]
    else:
        figs = _checks_figure(figures_dir, f"capacitor_{mpn}_{role}", cs, f"{mpn} ({role}): checks against limits")
    return {"checks": _graphed(figs, cs), "c_eff_part_f": cap.c_eff(v_dc).value, "figures": figs}


def inductor_check(mpn: str, l_h: dict | float, tol: dict | float, dcr_typ_ohm: dict | float,
                   dcr_max_ohm: dict | float | None, i_dc_a: float, di_pp_a: float, i_peak_worst_a: dict | float,
                   fsw_hz: float, ambient_c: dict | float, isat_a: dict | float | None = None,
                   isat_definition: str = "", irms_a: dict | float | None = None,
                   irms_rise_k: dict | float | None = None, l_vs_i: list | None = None, l_vs_i_source: str = "",
                   core_loss_table: list | None = None, core_loss_source: str = "",
                   max_temp_c: dict | float | None = None, l_min_needed_h: dict | float | None = None,
                   dcr_ref_c: float = 25.0, figures_dir: str | None = None) -> dict:
    """Saturation margin, L at peak current, RMS current, copper and core loss,
    temperature rise scaled from the rating. Graph: the L(I) curve with the DC,
    worst peak and saturation currents; without a curve, the checks."""
    isat = _param(isat_a, "A", isat_definition or "Isat")
    ind = parts.Inductor(mpn, _param(l_h, "H", "L"), _param(tol, "", "tolerance"), _param(dcr_typ_ohm, "ohm", "DCR typ"),
                         _param(dcr_max_ohm, "ohm", "DCR max"), dcr_ref_c, isat, _param(irms_a, "A", "Irms"),
                         _param(irms_rise_k, "K", "Irms rise"), [tuple(x) for x in l_vs_i] if l_vs_i else None,
                         l_vs_i_source, None, [tuple(x) for x in core_loss_table] if core_loss_table else None,
                         core_loss_source)
    cs = parts.inductor_checks(ind, i_dc=i_dc_a, di_pp=di_pp_a, i_peak_worst=_param(i_peak_worst_a, "A", "peak current"),
                               fsw=fsw_hz, ambient_c=_param(ambient_c, "°C", "ambient"),
                               max_temp_c=_param(max_temp_c, "°C", "max temperature"),
                               l_min_needed=_param(l_min_needed_h, "H", "required L"))
    if l_vs_i:
        pts = sorted(tuple(x) for x in l_vs_i)
        ipk = _param(i_peak_worst_a).value
        vl = [(i_dc_a, "I_dc"), (ipk, "worst peak")] + ([(isat.value, "Isat")] if isat else [])
        title = f"{mpn}: inductance vs current"
        path = figures.curves(_fig_path(figures_dir, "inductor", mpn), [{
            "ylabel": "L (µH)", "series": {"manufacturer curve": ([p[0] for p in pts], [p[1] * 1e6 for p in pts])},
            "marks": [(ipk, parts._interp(pts, ipk) * 1e6, f"{parts._interp(pts, ipk) * 1e6:.3g} µH at peak")],
            "vlines": vl}], title=title, xlabel="current (A)", note=f"curve: {l_vs_i_source or 'unsourced'}")
        figs = [{"title": title, "path": path}]
    else:
        figs = _checks_figure(figures_dir, f"inductor_{mpn}", cs, f"{mpn}: checks against limits")
    return {"checks": _graphed(figs, cs), "figures": figs}


# ── power input ────────────────────────────────────────────────────────────

def input_network(series_r_ohm: dict | float, series_l_h: dict | float, branches: list, vin_min: dict | float,
                  vin_max: dict | float, p_in_max: dict | float, f_max_hz: dict | float,
                  margin_min_db: dict | float, vin_abs_max: dict | float, figures_dir: str | None = None) -> dict:
    """Source/cable/filter output impedance against a constant-power load's
    negative input resistance (Middlebrook, up to ``f_max_hz``), and the
    hot-plug overshoot into the input capacitors. ``branches``: [{r, l, c}].
    Graph: |Z_out| over frequency against |R_in| and the required margin."""
    br = [inputfilter.Branch(float(b["r"]), float(b.get("l", 0.0)), b.get("c")) for b in branches]
    net = inputfilter.SourceNetwork(_param(series_r_ohm).value, _param(series_l_h).value, br)
    c_tot = sum(b.c or 0 for b in br)
    esr = min((b.r for b in br if b.c), default=0.0)
    cs = inputfilter.checks(net, vin_min=_param(vin_min, "V", "Vin min"), vin_max=_param(vin_max, "V", "Vin max"),
                            p_in_max=_param(p_in_max, "W", "Pin max"), f_cross=_param(f_max_hz, "Hz", "loop crossover"),
                            margin_min=_param(margin_min_db, "dB", "required margin"),
                            vin_abs_max=_param(vin_abs_max, "V", "VIN abs max"),
                            cable_l=_param(series_l_h, "H", "cable L"), cable_r=_param(series_r_ohm, "ohm", "cable R"),
                            c_in=Param(c_tot, "F", "branch list", "input capacitance"),
                            esr_in=Param(esr, "ohm", "branch list", "input ESR"))
    fmax = _param(f_max_hz).value
    f = np.logspace(2, math.log10(fmax * 10), 400)
    rin = _param(vin_min).value ** 2 / _param(p_in_max).value
    m = _param(margin_min_db).value
    title = "Input network: |Z_out| against the converter's |R_in|"
    path = figures.curves(_fig_path(figures_dir, "input_network"), [{
        "ylabel": "ohm", "series": {"|Z_out| of source, cable and filter": (f, np.abs(net.z_out(f)))},
        "limits": [(rin, f"|R_in| = Vin_min²/P_in = {rin:.3g} ohm"),
                   (rin / 10 ** (m / 20), f"{m:g} dB below |R_in|: |Z_out| must stay under this")],
        "vlines": [(fmax, "loop crossover")]}], title=title, xlabel="frequency (Hz)", logx=True, logy=True)
    figs = [{"title": title, "path": path}]
    return {"checks": _graphed(figs, cs), "figures": figs}


def conducted_emissions(csv: str, time_col: str, current_col: str, limit_points: list, limit_source: str,
                        l_lisn_h: float = 50e-6, f_lo: float = 150e3, f_hi: float = 30e6,
                        figures_dir: str | None = None) -> dict:
    """Differential-mode conducted spectrum at a LISN port from any simulated
    input-current waveform (CSV of whole periods) and its worst margin to a
    limit line [[f_hz, dBuV], ...] cited in ``limit_source``. Graph: the
    spectrum against the limit line."""
    import csv as _csv
    with open(csv, newline="", encoding="utf-8") as fh:
        rows = list(_csv.DictReader(fh))
    t = np.array([float(r[time_col]) for r in rows])
    i = np.array([float(r[current_col]) for r in rows])
    f, a = emi.harmonics(t, i)
    keep = (f >= f_lo) & (f <= f_hi)
    spec = {"f": f[keep], "dbuv": emi.dbuv(np.abs(emi.lisn_port_voltage(a[keep], f[keep], l_lisn_h)))}
    lm = emi.limit_margin(spec, [tuple(p) for p in limit_points])
    c = report.check("Conducted emissions (DM, LISN)", ">= 6 dB margin", f"{lm['margin_db']:+.1f} dB at "
                     f"{lm['at_hz'] / 1e3:.0f} kHz", lm["margin_db"] >= 6, "harmonics x LISN port impedance",
                     {"limit line": Param(0, "", limit_source, "limit"), "waveform": Param(0, "", csv, "waveform")},
                     value=lm["margin_db"], limit=6.0, sense="min")
    title = "Conducted emissions (DM) at the LISN"
    path = figures.spectrum(_fig_path(figures_dir, "conducted_emissions", Path(csv).stem), spec["f"], spec["dbuv"],
                            [tuple(p) for p in limit_points], title=title, limit_label=limit_source[:50])
    figs = [{"title": title, "path": path}]
    return {"margin": lm, "check": _graphed(figs, [c])[0], "figures": figs,
            "spectrum_top": sorted(zip(spec["f"].tolist(), spec["dbuv"].tolist()), key=lambda p: -p[1])[:10]}


# ── margins ────────────────────────────────────────────────────────────────

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}
_FUNCS = {"sqrt": math.sqrt, "log": math.log, "log10": math.log10, "exp": math.exp, "sin": math.sin,
          "cos": math.cos, "tan": math.tan, "atan": math.atan, "abs": abs, "min": min, "max": max, "hypot": math.hypot}


def _eval(expr: str, env: dict) -> float:
    """Arithmetic only: numbers, the case's variables, pi, and a few math
    functions. Anything else (attributes, imports, names not in the case) is refused."""
    def ev(n):
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, (int, float)):
            return n.value
        if isinstance(n, ast.Name):
            if n.id in env:
                return env[n.id]
            if n.id == "pi":
                return math.pi
            raise ValueError(f"unknown name {n.id}")
        if isinstance(n, ast.BinOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.left), ev(n.right))
        if isinstance(n, ast.UnaryOp) and type(n.op) in _OPS:
            return _OPS[type(n.op)](ev(n.operand))
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id in _FUNCS and not n.keywords:
            return _FUNCS[n.func.id](*[ev(a) for a in n.args])
        raise ValueError(f"unsupported expression element: {ast.dump(n)[:60]}")
    return float(ev(ast.parse(expr, mode="eval")))


def corner_sweep(spreads: list, metrics: dict, mode: str = "vertices", n: int = 200, seed: int = 1,
                 figures_dir: str | None = None) -> dict:
    """Worst case of each metric over tolerance spreads. Graph: every metric's
    value at every case, with the nominal and the worst cases marked.
    ``spreads``: [{name, nom, lo, hi, source}] (or {name, nom, tol, source});
    ``metrics``: {name: arithmetic expression of the spread names}."""
    sp = []
    for s in spreads:
        if "tol" in s:
            sp.append(corners.Spread.tol(s["name"], float(s["nom"]), float(s["tol"]), s.get("source", "")))
        else:
            sp.append(corners.Spread(s["name"], float(s["nom"]), float(s["lo"]), float(s["hi"]), s.get("source", "")))
    cases = corners.vertices(sp) if mode == "vertices" else corners.montecarlo(sp, n, seed)
    res = corners.sweep(lambda c: {k: _eval(e, c) for k, e in metrics.items()}, cases)
    out = {"cases": len(cases), "assumed_spreads": [s.name for s in sp if not s.source], "metrics": {}}
    for k in metrics:
        hi_case, hi = corners.worst(res, k, "max")
        lo_case, lo = corners.worst(res, k, "min")
        nominal = next(r for c, r in res if c["_corner"] == "nominal")[k] if mode == "vertices" else None
        out["metrics"][k] = {"max": hi, "max_corner": hi_case["_corner"], "min": lo,
                             "min_corner": lo_case["_corner"], "nominal": nominal}
    title = f"Tolerance {'corners' if mode == 'vertices' else 'Monte Carlo'}: {len(cases)} cases"
    path = figures.cases(_fig_path(figures_dir, "corner_sweep", *metrics),
                         {k: out["metrics"][k] | {"values": [r[k] for _, r in res]} for k in metrics}, title=title)
    out["figures"] = [{"title": title, "path": path}]
    return out


# ── evidence, reports and the optimisation cycle ───────────────────────────

def evaluate_check(name: str, requirement: str, value: float, limit: float, sense: str, method: str,
                   inputs: dict, result: str | None = None, layout_dependent: bool = False,
                   to_complete: str = "", lever: str = "") -> dict:
    """Apply the evidence rule to any comparison: ``inputs`` {name: number or
    {value, source}}; PASS/FAIL only when every input is sourced."""
    ok = value <= limit if sense == "max" else value >= limit
    c = report.check(name, requirement, result or f"{value:g}", ok, method,
                     {k: _param(v, "", k) for k, v in inputs.items()}, value=value, limit=limit, sense=sense,
                     layout=layout_dependent, to_complete=to_complete, lever=lever)
    return _check_dict(c)


def write_report(title: str, design: str, checks: list, out_dir: str, stem: str, notes: list | None = None,
                 figures: list | None = None, documents: list | None = None, figures_dir: str | None = None,
                 data_dir: str | None = None) -> dict:
    """Write the report of check dicts (as returned by the other tools) as one
    PDF, ``<out_dir>/<stem>.pdf``: the overall verdict, a table of the checks and
    the chart of all checks against their limits first, then one section per
    check with its method, sources, assumptions and its own graphs (the
    ``figures`` the sim_* tools attach), then ``figures``: [{"title", "path"}]
    further PNGs. The verdict follows the report rules; ``ungraphed`` lists
    checks with no graph of their own. The chart goes to ``figures_dir`` and the
    checks as JSON (for sim_cycle_record) to ``data_dir``: by default
    ``<project>/sim/figures/<stem>`` and ``<project>/sim/results/<stem>`` for a
    report in ``<project>/reports``. ``documents``: ["pdf"] (default); add
    "docx" for Word or "md" to keep the Markdown source."""
    fields = set(report.Check.__dataclass_fields__)
    figs = [(f["title"], f["path"]) for f in (figures or [])]
    figs_all = figs + [(f["title"], f["path"]) for c in checks for f in c.get("figures", [])]
    missing = [p for _, p in figs_all if not Path(p).is_file()]
    if missing:
        raise ValueError(f"figure files not found: {missing}")
    rep = report.Report(title, design, notes=list(notes or []), figures=figs)
    for c in checks:
        rep.add(report.Check(**{k: v for k, v in c.items() if k in fields}))
    docs = ["pdf"] if documents is None else list(documents)
    doc, js = rep.write(out_dir, stem, documents=docs, figures_dir=figures_dir, data_dir=data_dir)
    counts = {s: sum(c.status == s for c in rep.checks) for s in (report.PASS, report.FAIL, report.INCOMPLETE)}
    return {"verdict": rep.verdict, "counts": counts, "json": str(js),
            "figures": [{"title": t, "path": p} for t, p in rep.all_figures()], "ungraphed": rep.ungraphed,
            **{f: str(doc.with_suffix("." + f)) for f in (docs or ["md"])}}


def render_document(markdown: str, formats: list | None = None, out_dir: str | None = None) -> dict:
    """Render a Markdown write-up (e.g. the engineering report, kept in
    ``<project>/sim/report_sources/``) as a PDF in ``out_dir`` (the project's
    ``reports``; default: next to the Markdown), text and images together.
    Image paths are relative to the Markdown file. ``formats``: ["pdf"]
    (default) and/or "docx"."""
    from . import document
    md = Path(markdown)
    if not md.is_file():
        raise ValueError(f"Markdown file not found: {md}")
    missing = [b.path for b in document.parse(md.read_text(encoding="utf-8")) if b.kind == "image"
               and not (Path(b.path) if Path(b.path).is_absolute() else md.parent / b.path).is_file()]
    if missing:
        raise ValueError(f"images not found (paths are relative to {md.parent}): {missing}")
    return document.render(md, formats or ["pdf"], out_dir)


def cycle_record(log_path: str, report_json: str, change: str, files: list, objectives: dict,
                 drc_errors: int | None = None, erc_errors: int | None = None,
                 max_iterations: int = 6, patience: int = 2) -> dict:
    """Record one simulate -> change iteration (a report written by
    write_report or an evaluation script) and return the cycle's decision:
    continue / revert / met / stalled / limit."""
    data = json.loads(Path(report_json).read_text(encoding="utf-8"))
    fields = set(report.Check.__dataclass_fields__)
    rep = report.Report(data["title"], data["design"], notes=data.get("notes", []))
    for c in data["checks"]:
        rep.add(report.Check(**{k: v for k, v in c.items() if k in fields}))
    log = cycle.CycleLog.open(log_path, max_iterations=max_iterations, patience=patience)
    log.record(rep, change, cycle.fingerprint(*files) if files else {}, {k: float(v) for k, v in objectives.items()},
               drc_errors, erc_errors)
    state, why = log.decide()
    return {"decision": state, "reason": why, "iterations": len(log.iterations), "summary": log.summary_markdown()}


# ── calculators ────────────────────────────────────────────────────────────

def layout_calc(kind: str, params: dict) -> dict:
    """Closed-form layout physics, with the formula named in the answer.
    kinds: microstrip {width_mm, height_mm, er}; via_inductance {h_mm, d_mm};
    via_resistance {h_mm, drill_mm, plating_mm?}; ipc2221_width {current_a, rise_c, copper_mm, external?};
    ipc2221_current {area_mm2, rise_c, external?}; skin_resistance {r_dc, thickness_mm, f_hz};
    loop_inductance {points [[x,y]...], width_mm, thickness_mm?, plane_gap_mm?};
    sheet_resistance {copper_mm, temp_c?}."""
    p = params
    if kind == "microstrip":
        return layout.microstrip(p["width_mm"], p["height_mm"], p["er"]) | {"formula": "Hammerstad"}
    if kind == "via_inductance":
        return {"henry": layout.via_inductance(p["h_mm"], p["d_mm"]), "formula": "0.2 h (ln(4h/d) + 1) nH"}
    if kind == "via_resistance":
        return {"ohm": layout.via_resistance(p["h_mm"], p["drill_mm"], p.get("plating_mm", layout.DEFAULT_PLATING_MM)),
                "formula": "rho h / barrel area"}
    if kind == "ipc2221_width":
        return {"width_mm": layout.ipc2221_width_mm(p["current_a"], p["rise_c"], p["copper_mm"], p.get("external", True)),
                "formula": "IPC-2221 I = k dT^0.44 A^0.725"}
    if kind == "ipc2221_current":
        return {"current_a": copper.ipc2221_current_a(p["area_mm2"], p["rise_c"], p.get("external", True)),
                "formula": "IPC-2221"}
    if kind == "skin_resistance":
        return {"ohm": stress.skin_resistance(p["r_dc"], p["thickness_mm"], p["f_hz"]), "formula": "R_dc t / delta"}
    if kind == "loop_inductance":
        r = layout.loop_inductance([tuple(x) for x in p["points"]], p["width_mm"],
                                   p.get("thickness_mm", layout.DEFAULT_CU_MM), p.get("plane_gap_mm"))
        return r | {"formula": "circle of equal area (Grover) minus plane image"}
    if kind == "sheet_resistance":
        return {"ohm_per_square": layout.sheet_resistance(p["copper_mm"], p.get("temp_c", 25.0))}
    raise ValueError(f"unknown kind {kind!r}")


# ── design records: the user-approved specification and part selection ────

def write_spec(project_dir: str, title: str, requirements: list, brief: str = "", notes: list | None = None,
               change: str = "", documents: list | None = None) -> dict:
    """Write the design specification (stage 1) as a draft for the user to
    accept: design/01_specification.pdf in the KiCad project folder (the record
    itself in design/records/; "docx" in ``documents`` adds Word). Each requirement: {id, category, parameter, condition (test
    condition), criterion (pass criterion), origin (user / assumed / "standard:
    <doc>" / "datasheet: <doc>"), rationale, verification}. Rewriting it with
    any change starts the next draft revision; only sim_approve_record accepts it."""
    from . import design
    reqs = [design.Requirement(**r) for r in requirements]
    return design.write_spec(project_dir, title, reqs, brief, notes, change,
                             ["pdf"] if documents is None else documents)


def write_part_selection(project_dir: str, title: str, parts: list, notes: list | None = None,
                         change: str = "", documents: list | None = None) -> dict:
    """Write the part selection (stage 2) as a draft for the user to accept:
    design/02_part_selection.pdf. Refused until the specification is accepted.
    Each part: {role, part (MPN), reasons [..], requirements [ids it serves],
    manufacturer, key_values, alternatives [..], datasheet, sourcing,
    references, risks}; every cited id must exist in the specification."""
    from . import design
    choices = [design.PartChoice(**p) for p in parts]
    return design.write_parts(project_dir, title, choices, notes, change,
                              ["pdf"] if documents is None else documents)


def approve_record(project_dir: str, record: str, approved_by: str, statement: str,
                   documents: list | None = None) -> dict:
    """Record the user's acceptance of the current revision of the
    specification or part selection. Call only after the user has accepted it
    in their own words, and pass those words as ``statement``."""
    from . import design
    return design.approve(project_dir, record, approved_by, statement,
                          ["pdf"] if documents is None else documents)


def design_status(project_dir: str) -> dict:
    """Where the staged design stands (specification and part selection:
    revision, draft or accepted) and the next step it allows."""
    from . import design
    return design.status(project_dir)
