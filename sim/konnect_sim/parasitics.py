"""Lumped model of a switching converter's copper parasitics, read from the saved board.

Each power branch of the layout becomes one lumped element, a series R and L (and, for
the switch node, a shunt C), and the simulations insert those elements in place of
ideal wires:

    source --[R_in, L_in]-- C_in --[L_hot, R_hot]-- IC --SW--[R_sw, L_sw]-- L --+-- C_out
                                                         (C_sw to ground)       |
                       load <--[R_out, L_out]-- sense junction ----------------+
                       load GND --[R_gnd, L_gnd]-- IC / divider ground
    each capacitor: ESL (datasheet) + mounting inductance (its pads' vias to the plane)

``Lumped.apply_plant`` puts them into ``converter.Plant`` (closed loop: regulation,
ripple, load steps, efficiency) and ``Lumped.apply_testbench`` into
``buck.BuckTestbench`` (switched SPICE, edge-resolved ringing).

Per-element models (each a stated approximation, not a field solution):

- R: track path resistance (``layout.path_resistance``), the filled-copper solve for a
  ground return through a pour (``copper``), the loop copper of the hot loop.
- L of a track over a plane: Hammerstad microstrip L' x length, which is the loop
  inductance of the track and its image return in the plane (go and return together).
- L of a track with no plane under it: partial self-inductance of a flat bar (Rosa,
  Grover), go conductor only; its return is counted separately when routed.
- L of a via: 0.2 h (ln(4h/d) + 1) nH (Johnson & Graham), parallel vias as L/n
  (mutual coupling ignored, so a lower bound).
- Hot loop: ``buck.layout_parasitics`` (routed loop polygon minus the plane image, or
  VIN track over the plane plus the ground vias).
- C of the switch node: microstrip C' x length for its tracks, parallel plate for its pads
  and pours over a ground plane (fringing on pads and pours ignored, a lower bound).

Lumping is valid while a branch is short against the fastest edge: segment length <
t_r c / (10 sqrt(er_eff)) (Hall & Heck), about 16 mm for a 1 ns edge on FR-4. Longer
paths need a distributed model; ``check_lumping`` says which branches exceed it.
"""
from __future__ import annotations

import json
import math
from dataclasses import asdict, dataclass, field, fields, replace
from pathlib import Path

from . import layout as lay

EPS0 = 8.854e-12
C0 = 299_792_458.0


# ── element formulas ───────────────────────────────────────────────────────

def bar_inductance(length_mm: float, width_mm: float, thickness_mm: float) -> float:
    """Partial self-inductance (H) of a straight flat conductor:
    L = mu0 l / (2 pi) [ln(2l / (w + t)) + 0.5 + 0.2235 (w + t) / l] (Rosa; Grover)."""
    if length_mm <= 0:
        return 0.0
    l, wt = length_mm * 1e-3, (width_mm + thickness_mm) * 1e-3
    return lay.MU0 * l / (2 * math.pi) * (math.log(2 * l / wt) + 0.5 + 0.2235 * wt / l)


def _copper_layers(board) -> list[str]:
    names = [n for n, typ, _ in board.stack if typ == "copper"]
    return names or ["F.Cu", "B.Cu"]


def _pad_layers(board, pad) -> list[str]:
    if "*.Cu" in pad.layers:
        return _copper_layers(board)
    return [l for l in pad.layers if l.endswith(".Cu")]


def _edge_layer(board, net: str, path, j: int) -> str:
    """Copper layer of hop ``j`` of a track path."""
    segs = board.net_segments(net)
    for n in (path.nodes[j], path.nodes[j + 1]):
        if n[0] == "end":
            return segs[n[1]].layer
    (x0, y0), (x1, y1) = path.points[j], path.points[j + 1]
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    best = min(segs, key=lambda s: lay._project(mx, my, s)[1], default=None)
    return best.layer if best else "F.Cu"


def path_inductance(board, net: str, path, gnd_net: str, er: float, start: int = 0) -> dict:
    """Series inductance of a track path from node ``start`` to its end: microstrip
    loop inductance where a ground pour lies under the hop (return included), partial
    bar inductance where none does (return not included), plus its vias."""
    over, partial, vias, bare_mm = 0.0, 0.0, 0.0, 0.0
    net_vias = board.net_vias(net)
    for j in range(start, len(path.edges)):
        e = path.edges[j]
        if e["length"] <= 0 or e["width"] == math.inf:
            continue
        layer = _edge_layer(board, net, path, j)
        (x0, y0), (x1, y1) = path.points[j], path.points[j + 1]
        plane = board.plane_under(gnd_net, (x0 + x1) / 2, (y0 + y1) / 2, layer)
        if plane:
            h = board.layer_gap_mm(layer, plane)
            over += lay.microstrip(e["width"], h, er)["l_per_m"] * e["length"] * 1e-3
        else:
            partial += bar_inductance(e["length"], e["width"], board.copper(layer))
            bare_mm += e["length"]
    for n in path.nodes[max(start, 1):-1]:
        if n[0] == "via":
            vias += lay.via_inductance(board.thickness_mm, net_vias[n[1]].drill)
    return {"l_h": over + partial + vias, "over_plane_h": over, "partial_h": partial, "vias_h": vias,
            "length_mm": sum(e["length"] for e in path.edges[start:]), "no_plane_mm": bare_mm}


def mounting_inductance(board, ref: str, nets: tuple[str, str], reach_mm: float = 1.5) -> dict:
    """Inductance a capacitor's connection adds to its ESL: for each terminal, the vias
    of that net within ``reach_mm`` of the pad edge, in parallel (L_via / n). A terminal
    joined on its own layer (no via) adds nothing here: its copper is in the path terms."""
    out = {"l_h": 0.0, "vias": {}}
    for net in nets:
        pad = next((p for p in board.footprints[ref].pads.values() if p.net == net), None)
        if pad is None:
            continue
        near = [v for v in board.net_vias(net) if math.hypot(v.x - pad.x, v.y - pad.y) <= pad.reach + reach_mm]
        out["vias"][net] = len(near)
        if near:
            out["l_h"] += min(lay.via_inductance(board.thickness_mm, v.drill) for v in near) / len(near)
    return out


def net_capacitance(board, net: str, gnd_net: str, er: float) -> dict:
    """Capacitance of a net's copper to a ground plane on another layer: microstrip C'
    for tracks, parallel plate for pads and filled pours. Copper with no ground plane
    under it is reported as ``no_plane_mm2`` and not counted."""
    tracks = pads = pours = 0.0
    bare = 0.0
    for s in board.net_segments(net):
        plane = board.plane_under(gnd_net, (s.x1 + s.x2) / 2, (s.y1 + s.y2) / 2, s.layer)
        if plane:
            ms = lay.microstrip(s.width, board.layer_gap_mm(s.layer, plane), er)
            tracks += math.sqrt(ms["eeff"]) / (C0 * ms["z0"]) * s.length * 1e-3
        else:
            bare += s.length * s.width

    def plate(area_mm2, x, y, layer):
        nonlocal bare
        plane = board.plane_under(gnd_net, x, y, layer)
        if not plane:
            bare += area_mm2
            return 0.0
        return EPS0 * er * area_mm2 * 1e-6 / (board.layer_gap_mm(layer, plane) * 1e-3)

    for f in board.footprints.values():
        for p in f.pads.values():
            if p.net == net:
                for layer in _pad_layers(board, p):
                    pads += plate(p.size[0] * p.size[1], p.x, p.y, layer)
    for z in board.zones_on(net):
        for layer, polys in z.fills.items():
            for pts in polys:
                cx, cy = sum(x for x, _ in pts) / len(pts), sum(y for _, y in pts) / len(pts)
                pours += plate(lay.polygon_area(pts), cx, cy, layer)
    return {"c_f": tracks + pads + pours, "tracks_f": tracks, "pads_f": pads, "pours_f": pours,
            "no_plane_mm2": bare}


def lumping_limit_mm(t_rise_s: float, er_eff: float) -> float:
    """Longest segment a lumped model represents for an edge t_r: t_r c / (10 sqrt(er_eff))."""
    return t_rise_s * C0 / (10 * math.sqrt(er_eff)) * 1e3


def _json_default(o):
    """numpy scalars (from the copper solve and loop formulas) as numbers; anything else as text."""
    try:
        return float(o)
    except (TypeError, ValueError):
        return str(o)


# ── the lumped network ─────────────────────────────────────────────────────

BRANCHES = [  # (key, label, R field, L field)
    ("input", "Input connector to C_in", "r_in_ohm", "l_in_h"),
    ("hot_loop", "Hot loop C_in - IC - C_in", "r_hot_ohm", "l_hot_h"),
    ("sw", "SW pin to inductor", "r_sw_ohm", "l_sw_h"),
    ("output", "Sense junction to load VOUT", "r_out_ohm", "l_out_h"),
    ("gnd", "Load GND to IC GND", "r_gnd_ohm", "l_gnd_h"),
    ("cin_mount", "C_in mounting (each)", None, "l_mount_cin_h"),
    ("cout_mount", "C_out mounting (each)", None, "l_mount_cout_h"),
]


@dataclass
class Lumped:
    """One converter layout as lumped elements. Zero means "not present or not
    extracted"; ``unmodelled`` says which."""
    r_in_ohm: float = 0.0
    l_in_h: float = 0.0
    r_hot_ohm: float = 0.0
    l_hot_h: float = 0.0
    r_sw_ohm: float = 0.0
    l_sw_h: float = 0.0
    c_sw_f: float = 0.0
    r_out_ohm: float = 0.0
    l_out_h: float = 0.0
    r_gnd_ohm: float = 0.0
    l_gnd_h: float = 0.0
    l_mount_cin_h: float = 0.0
    l_mount_cout_h: float = 0.0
    source: str = ""
    details: dict = field(default_factory=dict)
    unmodelled: list[str] = field(default_factory=list)

    # ── into the simulations ──
    def apply_plant(self, plant):
        """``converter.Plant`` with the board's copper, sensed at the junction where the
        feedback tap meets the output path (``sense_at_load=False``). The source path joins the source
        impedance; the hot-loop copper carries the high-side current and is added to it
        (its inductance acts only at the edges: use ``apply_testbench`` and
        ``BuckTestbench.run_edge`` for that)."""
        return replace(
            plant, src_r_ohm=plant.src_r_ohm + self.r_in_ohm, src_l_h=plant.src_l_h + self.l_in_h,
            rds_hs=plant.rds_hs + self.r_hot_ohm, r_sw_path=self.r_sw_ohm,
            r_out_path=self.r_out_ohm, l_out_path=self.l_out_h,
            r_gnd_path=self.r_gnd_ohm, l_gnd_path=self.l_gnd_h,
            # the extracted output path starts at the sense junction, so the loop
            # regulates the capacitor node and the load sees the copper after it
            sense_at_load=False,
            cin=[replace(b, l_mount_h=self.l_mount_cin_h) for b in plant.cin],
            cout=[replace(b, l_mount_h=self.l_mount_cout_h) for b in plant.cout])

    def apply_testbench(self, tb, edges: bool = True):
        """``buck.BuckTestbench`` with the board's copper, every element in place.

        ``edges=False`` leaves out the input loop's inductances (hot loop, C_in mounting) and
        keeps every resistance and the rest of the network: for steady-state runs (ripple,
        efficiency, DC drop), where those nH only ring at hundreds of MHz after each edge,
        lengthening the run tenfold and defeating ngspice's step control at some points.
        Their effect is the edge itself: run ``run_edge`` on the ``edges=True`` testbench."""
        return replace(
            tb, source_r_ohm=tb.source_r_ohm + self.r_in_ohm, source_l_h=tb.source_l_h + self.l_in_h,
            l_hot_h=self.l_hot_h if edges else 0.0, r_hot_ohm=self.r_hot_ohm,
            r_sw_path_ohm=self.r_sw_ohm, l_sw_path_h=self.l_sw_h, c_sw_f=tb.c_sw_f + self.c_sw_f,
            r_out_path_ohm=self.r_out_ohm, l_out_path_h=self.l_out_h,
            r_gnd_path_ohm=self.r_gnd_ohm, l_gnd_path_h=self.l_gnd_h,
            cin=[replace(b, l_mount_h=self.l_mount_cin_h) for b in tb.cin] if edges else tb.cin,
            cout=[replace(b, l_mount_h=self.l_mount_cout_h) for b in tb.cout])

    # ── reporting ──
    def rows(self) -> list[dict]:
        """One row per element: branch, R (mOhm), L (nH), method."""
        out = []
        for key, label, rf, lf in BRANCHES:
            d = self.details.get(key, {})
            out.append({"branch": label, "r_mohm": getattr(self, rf) * 1e3 if rf else None,
                        "l_nh": getattr(self, lf) * 1e9, "method": d.get("method", "")})
        out.append({"branch": "Switch-node copper to GND", "c_pf": self.c_sw_f * 1e12,
                    "method": self.details.get("sw_node", {}).get("method", "")})
        return out

    def markdown_table(self) -> str:
        lines = ["| Element | R (mOhm) | L (nH) | C (pF) | Method |", "|---|---|---|---|---|"]
        for r in self.rows():
            rv = f"{r['r_mohm']:.2f}" if r.get("r_mohm") is not None else "-"
            lv = f"{r['l_nh']:.2f}" if r.get("l_nh") is not None else "-"
            cv = f"{r['c_pf']:.1f}" if r.get("c_pf") is not None else "-"
            lines.append(f"| {r['branch']} | {rv} | {lv} | {cv} | {r['method']} |")
        return "\n".join(lines)

    def figure(self, path, title: str = "Lumped layout parasitics") -> str:
        """Two panels: series inductance and resistance of every branch."""
        from . import figures as fg
        plt = fg._plt()
        labels = [b[1] for b in BRANCHES]
        ls = [getattr(self, b[3]) * 1e9 for b in BRANCHES]
        rs = [getattr(self, b[2]) * 1e3 if b[2] else float("nan") for b in BRANCHES]
        # a ground return under a microstrip output path has its L inside that loop
        in_loop = {i for i, b in enumerate(BRANCHES) if b[0] == "gnd" and self.l_gnd_h == 0
                   and "microstrip loop" in self.details.get("gnd", {}).get("method", "")}
        fig, axs = plt.subplots(1, 2, figsize=(10.5, 3.6), sharey=True)
        for ax, vals, xl in ((axs[0], ls, "series inductance (nH)"), (axs[1], rs, "series resistance (mOhm)")):
            ys = range(len(labels))
            ax.barh(ys, [0 if math.isnan(v) else v for v in vals], color=fg.SERIES[0][0], height=0.6)
            for i, v in enumerate(vals):
                text = ("in the output path's L" if ax is axs[0] and i in in_loop else
                        "n/a" if math.isnan(v) else f"{v:.3g}")
                ax.annotate(text, xy=(0 if math.isnan(v) else v, i),
                            xytext=(4, 0), textcoords="offset points", va="center", fontsize=7.5, color=fg.INK2)
            ax.set_xlabel(xl)
            ax.grid(axis="y", visible=False)
            ax.margins(x=0.18)
        axs[0].set_yticks(range(len(labels)), labels, fontsize=8)
        axs[0].invert_yaxis()
        fig.suptitle(f"{title}; switch-node copper C = {self.c_sw_f * 1e12:.1f} pF", color=fg.INK)
        if self.unmodelled:
            fig.text(0.01, 0.01, f"{len(self.unmodelled)} item(s) not modelled: see the report", color=fg.MUTED,
                     fontsize=7.5)
        fig.tight_layout(rect=(0, 0.04 if self.unmodelled else 0, 1, 1))
        return fg._save(fig, path)

    def check_lumping(self, t_rise_s: float, er_eff: float = 3.3) -> list[str]:
        """Branches longer than a lumped element can represent for edge ``t_rise_s``."""
        limit = lumping_limit_mm(t_rise_s, er_eff)
        return [f"{k}: {d['length_mm']:.1f} mm > {limit:.1f} mm lumped limit for a {t_rise_s * 1e9:g} ns edge"
                for k, d in self.details.items() if isinstance(d, dict) and d.get("length_mm", 0) > limit]

    def to_dict(self) -> dict:
        return asdict(self)

    def save(self, path) -> str:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, default=_json_default), encoding="utf-8")
        return str(path)

    @classmethod
    def load(cls, path) -> "Lumped":
        return cls(**json.loads(Path(path).read_text(encoding="utf-8")))


# ── extraction of a buck layout ────────────────────────────────────────────

def extract_buck(board, *, ic: str, ic_vin: str, ic_gnd: str, ic_sw: str, cin_refs: list[str],
                 cout_refs: list[str], inductor: str, sense: tuple[str, str], load_vout: tuple[str, str],
                 load_gnd: tuple[str, str], sense_gnd: tuple[str, str],
                 source_vin: tuple[str, str] | None = None, nets: dict[str, str] | None = None,
                 er: float = 4.5, temp_c: float = 25.0, copper_pitch_mm: float = 0.1) -> Lumped:
    """The lumped network of a synchronous buck layout (see the module docstring).
    Arguments as ``buck.layout_parasitics``, plus ``cout_refs``."""
    from .buck import _pad_on, layout_parasitics

    base = layout_parasitics(board, ic=ic, ic_vin=ic_vin, ic_gnd=ic_gnd, ic_sw=ic_sw, cin_refs=cin_refs,
                             inductor=inductor, sense=sense, load_vout=load_vout, load_gnd=load_gnd,
                             sense_gnd=sense_gnd, source_vin=source_vin, nets=nets, er=er, temp_c=temp_c,
                             copper_pitch_mm=copper_pitch_mm)
    n = base["nets"]
    gnd = n["GND"]
    out = Lumped(source=f"{Path(board.path).name}, stackup: "
                        f"{board.stackup_source or 'not in the board (2-layer defaults assumed)'}, er {er:g}",
                 unmodelled=list(base["unmodelled"]))
    det = {"layout_parasitics": base}

    # hot loop
    out.l_hot_h, out.r_hot_ohm = base["l_hot_h"], base["r_hot_ohm"]
    best = next((lp for lp in base["loops"] if lp["ref"] == base["hot_loop_cap"]), {})
    det["hot_loop"] = {"method": best.get("model", "not modelled"), "cap": base["hot_loop_cap"],
                       "length_mm": best.get("vin_path_mm") or 0.0}

    def trace(net, a, b, label, start_node=None):
        p = lay.path_resistance(board, net, a, b, temp_c)
        if p is None:
            return None, None
        i0 = p.nodes.index(start_node) if start_node in p.nodes else 0
        li = path_inductance(board, net, p, gnd, er, start=i0)
        r = sum(e["r"] for e in p.edges[i0:])
        m = ("track over GND plane (microstrip loop L)" if li["partial_h"] == 0 else
             "track partly without a plane (bar partial L, return not included)" if li["over_plane_h"] else
             "track without a plane (bar partial L, return not included)")
        return r, li | {"method": m, "path": label, "r_ohm": r}

    # input path: source connector -> the hot-loop capacitor (falls back to the IC VIN pin)
    if source_vin:
        cap = base["hot_loop_cap"]
        dest = (cap, _pad_on(board, cap, n["VIN"]).number) if cap else (ic, ic_vin)
        r, li = trace(n["VIN"], source_vin, dest, "input connector to C_in")
        if li:
            out.r_in_ohm, out.l_in_h, det["input"] = r, li["l_h"], li
        else:
            out.unmodelled.append("input path: no track path from the connector (pour or unrouted)")

    # SW pin -> inductor
    l_sw = _pad_on(board, inductor, n["SW"])
    r, li = trace(n["SW"], (ic, ic_sw), (inductor, l_sw.number), "SW pin to inductor")
    if li:
        out.r_sw_ohm, out.l_sw_h, det["sw"] = r, li["l_h"], li
    sw_c = net_capacitance(board, n["SW"], gnd, er)
    out.c_sw_f = sw_c["c_f"]
    det["sw_node"] = sw_c | {"method": "microstrip C' (tracks) + parallel plate (pads, pours) over GND"}
    if sw_c["no_plane_mm2"] > 0:
        out.unmodelled.append(f"switch node: {sw_c['no_plane_mm2']:.1f} mm^2 of copper with no GND plane "
                              f"under it, its capacitance not counted")

    # output: the sense junction (what the loop regulates) -> load VOUT pad
    l_out = _pad_on(board, inductor, n["VOUT"])
    p_pow = lay.path_resistance(board, n["VOUT"], (inductor, l_out.number), load_vout, temp_c)
    p_sense = lay.path_resistance(board, n["VOUT"], sense, load_vout, temp_c)
    junction = None
    if p_pow and p_sense:
        on_power = set(p_pow.nodes)
        junction = next((x for x in p_sense.nodes if x in on_power), None)
    if p_pow is not None:
        r, li = trace(n["VOUT"], (inductor, l_out.number), load_vout, "sense junction to load VOUT", junction)
        out.r_out_ohm, out.l_out_h, det["output"] = base["r_out_path_ohm"], li["l_h"], li
        out_over_plane = li["partial_h"] == 0
    else:
        out_over_plane = False

    # ground return: R from the copper solve; its L is already in the output path's
    # microstrip loop when VOUT runs over the plane
    out.r_gnd_ohm = base["r_gnd_return_ohm"]
    det["gnd"] = {"method": base["gnd_return"]["method"] or "not modelled", "r_ohm": out.r_gnd_ohm}
    p_gnd = lay.path_resistance(board, gnd, load_gnd, sense_gnd, temp_c)
    if out_over_plane:
        det["gnd"]["method"] += "; L in the output path's microstrip loop"
    elif p_gnd is not None:
        li = path_inductance(board, gnd, p_gnd, gnd, er)
        out.l_gnd_h = li["partial_h"] + li["vias_h"]
        det["gnd"] |= li | {"method": det["gnd"]["method"] + "; bar partial L of the routed return"}
    elif p_pow is not None:
        out.unmodelled.append("ground return through a pour with no plane under VOUT: its inductance not estimated")

    # capacitor mounting
    cin_m = {r_: mounting_inductance(board, r_, (n["VIN"], gnd)) for r_ in cin_refs}
    cout_m = {r_: mounting_inductance(board, r_, (n["VOUT"], gnd)) for r_ in cout_refs}
    out.l_mount_cin_h = sum(m["l_h"] for m in cin_m.values()) / max(len(cin_m), 1)
    out.l_mount_cout_h = sum(m["l_h"] for m in cout_m.values()) / max(len(cout_m), 1)
    det["cin_mount"] = {"method": "vias at the pads, L_via / n per terminal", "each": cin_m}
    det["cout_mount"] = {"method": "vias at the pads, L_via / n per terminal", "each": cout_m}
    out.details = det
    for f in fields(Lumped):                 # numpy scalars from the solvers -> plain floats
        if f.type == "float":
            setattr(out, f.name, float(getattr(out, f.name)))
    return out
