"""Read a saved .kicad_pcb (read-only) and estimate the parasitics simulation needs.

This module never writes a board file: every board change goes through the
Konnect MCP tools. It reads the *saved* file, so save the board in KiCad before
extracting, or the numbers describe an older layout.

Estimates (each is a stated approximation, not a field solution):

- copper path resistance: Dijkstra over tracks, vias and pads of one net,
  R = rho * length / (width * copper thickness), rho at the stated temperature;
- via inductance: L[nH] = 0.2 h (ln(4h/d) + 1), h and d in mm (Johnson & Graham);
- loop inductance: the loop drawn through the given pad centres replaced by a
  circle of equal area, L = mu0 R (ln(8R/a) - 2), a = 0.2235 (w + t) for a flat
  conductor of width w and thickness t (Grover). Return currents that flow in a
  plane under the loop make the real value lower; treat it as an upper estimate.
"""
from __future__ import annotations

import heapq
import math
import re
from dataclasses import dataclass, field
from pathlib import Path

MU0 = 4e-7 * math.pi
RHO_CU_20C = 1.72e-8      # ohm*m, annealed copper at 20 °C
ALPHA_CU = 0.00393        # 1/K
DEFAULT_CU_MM = 0.035     # 1 oz, used only when the board has no stackup
DEFAULT_PLATING_MM = 0.025

# --- S-expression reader ------------------------------------------------------

_TOKEN = re.compile(r'\(|\)|"(?:\\.|[^"\\])*"|[^\s()"]+')


def _parse(text: str):
    stack, cur = [], []
    for tok in _TOKEN.findall(text):
        if tok == "(":
            stack.append(cur)
            cur = []
        elif tok == ")":
            done, cur = cur, stack.pop()
            cur.append(done)
        elif tok.startswith('"'):
            cur.append(tok[1:-1].replace('\\"', '"').replace("\\\\", "\\"))
        else:
            cur.append(tok)
    return cur[0]


def _kids(node, name):
    return [c for c in node[1:] if isinstance(c, list) and c and c[0] == name]


def _kid(node, name):
    k = _kids(node, name)
    return k[0] if k else None


def _val(node, name, i=1, default=None):
    k = _kid(node, name)
    return k[i] if k is not None and len(k) > i else default


def _xy(node, name="at"):
    k = _kid(node, name)
    if k is None:
        return None
    return float(k[1]), float(k[2]), float(k[3]) if len(k) > 3 and _isnum(k[3]) else 0.0


def _isnum(s):
    try:
        float(s)
        return True
    except (TypeError, ValueError):
        return False


# --- Board model --------------------------------------------------------------

@dataclass
class Pad:
    ref: str
    number: str
    net: str
    x: float
    y: float
    size: tuple[float, float]
    layers: list[str]
    rotation: float = 0.0     # absolute pad orientation, degrees (as KiCad stores it)

    def on(self, layer: str) -> bool:
        return layer in self.layers or "*.Cu" in self.layers

    @property
    def reach(self) -> float:
        return max(self.size) / 2

    def distance_to_segment(self, x1, y1, x2, y2) -> float:
        """Distance from the pad's rectangle to a line segment (0 if they cross)."""
        hx, hy = self.size[0] / 2, self.size[1] / 2

        def local(x, y):
            return _rotate(x - self.x, y - self.y, -self.rotation)

        (ax, ay), (bx, by) = local(x1, y1), local(x2, y2)
        # Liang-Barsky: does the segment enter the box?
        t0, t1, dx, dy = 0.0, 1.0, bx - ax, by - ay
        inside = True
        for p, q in ((-dx, ax + hx), (dx, hx - ax), (-dy, ay + hy), (dy, hy - ay)):
            if p == 0:
                if q < 0:
                    inside = False
                    break
            else:
                r = q / p
                if p < 0:
                    t0 = max(t0, r)
                else:
                    t1 = min(t1, r)
                if t0 > t1:
                    inside = False
                    break
        if inside:
            return 0.0

        def box_dist(px, py):
            return math.hypot(max(abs(px) - hx, 0.0), max(abs(py) - hy, 0.0))

        def seg_dist(px, py):
            L2 = dx * dx + dy * dy
            t = 0.0 if L2 == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / L2))
            return math.hypot(px - (ax + t * dx), py - (ay + t * dy))

        return min([box_dist(ax, ay), box_dist(bx, by)] +
                   [seg_dist(cx, cy) for cx in (-hx, hx) for cy in (-hy, hy)])


@dataclass
class Footprint:
    ref: str
    value: str
    x: float
    y: float
    rotation: float
    layer: str
    pads: dict[str, Pad] = field(default_factory=dict)


@dataclass
class Segment:
    x1: float
    y1: float
    x2: float
    y2: float
    width: float
    layer: str
    net: str

    @property
    def length(self) -> float:
        return math.hypot(self.x2 - self.x1, self.y2 - self.y1)


@dataclass
class Via:
    x: float
    y: float
    size: float
    drill: float
    net: str


@dataclass
class Zone:
    net: str
    layers: list[str]
    outline: list[tuple[float, float]]
    filled: bool


@dataclass
class Board:
    path: Path
    footprints: dict[str, Footprint]
    segments: list[Segment]
    vias: list[Via]
    zones: list[Zone]
    copper_mm: dict[str, float]      # copper thickness per layer
    thickness_mm: float              # board thickness
    stackup_sourced: bool            # False when defaults were used
    stack: list[tuple[str, str, float]] = field(default_factory=list)  # (name, type, mm), top down

    def pad(self, ref: str, number: str) -> Pad:
        return self.footprints[ref].pads[number]

    def net_segments(self, net: str) -> list[Segment]:
        return [s for s in self.segments if s.net == net]

    def net_vias(self, net: str) -> list[Via]:
        return [v for v in self.vias if v.net == net]

    def zones_on(self, net: str) -> list[Zone]:
        return [z for z in self.zones if z.net == net]

    def copper(self, layer: str) -> float:
        return self.copper_mm.get(layer, DEFAULT_CU_MM)

    def layer_gap_mm(self, a: str, b: str) -> float:
        """Dielectric distance between two copper layers. Without a stackup a
        two-layer board is assumed (board thickness minus copper)."""
        names = [n for n, _, _ in self.stack]
        if a in names and b in names:
            i, j = sorted((names.index(a), names.index(b)))
            return sum(t for _, typ, t in self.stack[i + 1:j] if typ != "copper")
        return self.thickness_mm - 2 * DEFAULT_CU_MM

    def plane_under(self, net: str, x: float, y: float, not_layer: str) -> str | None:
        """Copper layer (other than ``not_layer``) whose pour of ``net`` covers
        the point. Uses the zone outline; cut-outs in the fill are ignored."""
        for z in self.zones_on(net):
            for layer in z.layers:
                if layer != not_layer and layer.endswith(".Cu") and point_in_polygon(x, y, z.outline):
                    return layer
        return None


def _rotate(x, y, deg):
    # KiCad: y points down, positive angles turn counter-clockwise on screen.
    r = math.radians(deg)
    return x * math.cos(r) + y * math.sin(r), -x * math.sin(r) + y * math.cos(r)


def load(path: str | Path) -> Board:
    """Parse a saved KiCad board (read-only)."""
    path = Path(path)
    root = _parse(path.read_text(encoding="utf-8"))

    footprints = {}
    for fp in _kids(root, "footprint"):
        fx, fy, frot = _xy(fp)
        ref = value = ""
        for prop in _kids(fp, "property"):
            if prop[1] == "Reference":
                ref = prop[2]
            elif prop[1] == "Value":
                value = prop[2]
        f = Footprint(ref, value, fx, fy, frot, _val(fp, "layer", default="F.Cu"))
        for p in _kids(fp, "pad"):
            px, py, prot = _xy(p)
            dx, dy = _rotate(px, py, frot)
            size = _kid(p, "size")
            layers = _kid(p, "layers")
            f.pads[str(p[1])] = Pad(ref, str(p[1]), _val(p, "net", default="") or "",
                                    fx + dx, fy + dy,
                                    (float(size[1]), float(size[2])) if size else (0.0, 0.0),
                                    list(layers[1:]) if layers else [], prot)
        if ref:
            footprints[ref] = f

    segments = []
    for s in _kids(root, "segment"):
        a, b = _kid(s, "start"), _kid(s, "end")
        segments.append(Segment(float(a[1]), float(a[2]), float(b[1]), float(b[2]),
                                float(_val(s, "width")), _val(s, "layer"), _val(s, "net", default="")))

    vias = []
    for v in _kids(root, "via"):
        x, y, _ = _xy(v)
        vias.append(Via(x, y, float(_val(v, "size")), float(_val(v, "drill")), _val(v, "net", default="")))

    zones = []
    for z in _kids(root, "zone"):
        layers = _kid(z, "layers") or _kid(z, "layer")
        poly = _kid(z, "polygon")
        pts = []
        if poly is not None and _kid(poly, "pts") is not None:
            pts = [(float(p[1]), float(p[2])) for p in _kids(_kid(poly, "pts"), "xy")]
        zones.append(Zone(_val(z, "net_name", default=None) or _val(z, "net", default=""),
                          list(layers[1:]) if layers else [], pts,
                          bool(_kids(z, "filled_polygon"))))

    copper, total, sourced, stack_list = {}, 0.0, False, []
    setup = _kid(root, "setup")
    stack = _kid(setup, "stackup") if setup is not None else None
    if stack is not None:
        for layer in _kids(stack, "layer"):
            t = _val(layer, "thickness")
            if t is None or not _isnum(t):
                continue
            total += float(t)
            stack_list.append((layer[1], _val(layer, "type", default=""), float(t)))
            if _val(layer, "type") == "copper":
                copper[layer[1]] = float(t)
                sourced = True
    general = _kid(root, "general")
    thickness = float(_val(general, "thickness", default=total or 1.6)) if general else (total or 1.6)
    return Board(path, footprints, segments, vias, zones, copper, thickness, sourced, stack_list)


# --- Physics helpers ----------------------------------------------------------

def sheet_resistance(copper_mm: float, temp_c: float = 25.0) -> float:
    """Ohms per square of a copper layer."""
    rho = RHO_CU_20C * (1 + ALPHA_CU * (temp_c - 20))
    return rho / (copper_mm * 1e-3)


def via_inductance(h_mm: float, d_mm: float) -> float:
    """Henries. L[nH] = 0.2 h (ln(4h/d) + 1), h = barrel length, d = drill."""
    return 0.2 * h_mm * (math.log(4 * h_mm / d_mm) + 1) * 1e-9


def via_resistance(h_mm: float, drill_mm: float, plating_mm: float = DEFAULT_PLATING_MM,
                   temp_c: float = 25.0) -> float:
    rho = RHO_CU_20C * (1 + ALPHA_CU * (temp_c - 20))
    area = math.pi * ((drill_mm / 2 + plating_mm) ** 2 - (drill_mm / 2) ** 2) * 1e-6
    return rho * h_mm * 1e-3 / area


def polygon_area(points) -> float:
    """Shoelace area, mm^2 (absolute)."""
    n = len(points)
    return abs(sum(points[i][0] * points[(i + 1) % n][1] - points[(i + 1) % n][0] * points[i][1]
                   for i in range(n))) / 2


def microstrip(width_mm: float, height_mm: float, er: float) -> dict:
    """Hammerstad microstrip: Z0 (ohm), effective permittivity and inductance
    per metre of a track of the given width at the given height over a plane."""
    u = width_mm / height_mm
    if u <= 1:
        eeff = (er + 1) / 2 + (er - 1) / 2 * ((1 + 12 / u) ** -0.5 + 0.04 * (1 - u) ** 2)
        z0 = 60 / math.sqrt(eeff) * math.log(8 / u + u / 4)
    else:
        eeff = (er + 1) / 2 + (er - 1) / 2 * (1 + 12 / u) ** -0.5
        z0 = 120 * math.pi / (math.sqrt(eeff) * (u + 1.393 + 0.667 * math.log(u + 1.444)))
    return {"z0": z0, "eeff": eeff, "l_per_m": z0 * math.sqrt(eeff) / 299_792_458}


def point_in_polygon(x, y, pts) -> bool:
    inside = False
    n = len(pts)
    for i in range(n):
        (x1, y1), (x2, y2) = pts[i], pts[(i + 1) % n]
        if (y1 > y) != (y2 > y) and x < x1 + (y - y1) * (x2 - x1) / (y2 - y1):
            inside = not inside
    return inside


def coaxial_mutual(r_m: float, d_m: float) -> float:
    """Mutual inductance (H) of two coaxial circular loops of radius r, d apart."""
    from scipy.special import ellipe, ellipk
    m = 4 * r_m * r_m / (4 * r_m * r_m + d_m * d_m)
    k = math.sqrt(m)
    return MU0 * r_m * ((2 / k - k) * ellipk(m) - (2 / k) * ellipe(m))


def loop_inductance(points, width_mm: float, thickness_mm: float = DEFAULT_CU_MM,
                    plane_gap_mm: float | None = None) -> dict:
    """Inductance of the loop through ``points`` (mm), circle of equal area.

    With ``plane_gap_mm`` (distance to a solid plane under the loop) the plane
    is modelled by its image loop at twice that distance carrying the opposite
    current: L = L_self - M(image). Returns henries plus the geometry used.
    ``valid`` is False when the loop is too small for the thin-wire formula
    (radius under 4x the conductor radius); the value is then rough.
    """
    area = polygon_area(points)
    r = math.sqrt(area / math.pi) * 1e-3
    a = 0.2235 * (width_mm + thickness_mm) * 1e-3
    out = {"area_mm2": area, "radius_mm": r * 1e3, "plane_gap_mm": plane_gap_mm}
    if r <= a:
        return out | {"henry": 0.0, "self_henry": 0.0, "valid": False}
    l_self = MU0 * r * (math.log(8 * r / a) - 2)
    m = coaxial_mutual(r, 2 * plane_gap_mm * 1e-3) if plane_gap_mm else 0.0
    return out | {"henry": max(l_self - m, 0.0), "self_henry": l_self, "valid": r > 4 * a}


def ipc2221_width_mm(current_a: float, rise_c: float, copper_mm: float, external: bool = True) -> float:
    """Minimum trace width from IPC-2221: I = k dT^0.44 A^0.725, A in mil^2."""
    k = 0.048 if external else 0.024
    area_mil2 = (current_a / (k * rise_c ** 0.44)) ** (1 / 0.725)
    thickness_mil = copper_mm / 0.0254
    return area_mil2 / thickness_mil * 0.0254


# --- Copper path search -------------------------------------------------------

@dataclass
class Path_:
    resistance: float
    length_mm: float
    vias: int
    min_width_mm: float
    hops: list[str]
    nodes: list = field(default_factory=list)    # graph nodes, start first
    edges: list = field(default_factory=list)    # per hop: {r, length, width, via}
    points: list = field(default_factory=list)   # (x, y) of each node, mm

    def min_width_over(self, min_length_mm: float) -> float | None:
        """Narrowest track among hops at least ``min_length_mm`` long; short
        necks at fine-pitch pads are excluded."""
        w = [e["width"] for e in self.edges if e["length"] >= min_length_mm and e["width"] != math.inf]
        return min(w) if w else None

    def resistance_from(self, node) -> float:
        """Resistance from ``node`` (on this path) to the path's end."""
        i = self.nodes.index(node)
        return sum(e["r"] for e in self.edges[i:])


def _project(px, py, s: Segment):
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        return 0.0, math.hypot(px - s.x1, py - s.y1)
    t = max(0.0, min(1.0, ((px - s.x1) * dx + (py - s.y1) * dy) / L2))
    return t, math.hypot(px - (s.x1 + t * dx), py - (s.y1 + t * dy))


def path_resistance(board: Board, net: str, a: tuple[str, str], b: tuple[str, str],
                    temp_c: float = 25.0) -> Path_ | None:
    """Lowest-resistance track path between two pads of ``net``.

    Copper zones are not modelled: when the only connection is through a pour
    this returns None, and the caller must say the path is unmodelled.
    """
    segs = board.net_segments(net)
    vias = board.net_vias(net)
    pads = [p for f in board.footprints.values() for p in f.pads.values() if p.net == net]
    h = board.thickness_mm

    graph: dict = {}

    def edge(u, v, r, length=0.0, width=math.inf, via=0):
        graph.setdefault(u, []).append((v, r, length, width, via))
        graph.setdefault(v, []).append((u, r, length, width, via))

    for i, s in enumerate(segs):
        stops = [(0.0, ("end", i, 0)), (1.0, ("end", i, 1))]
        for j, o in enumerate(segs):
            if j == i or o.layer != s.layer:
                continue
            for k, (ex, ey) in enumerate(((o.x1, o.y1), (o.x2, o.y2))):
                t, d = _project(ex, ey, s)
                if d <= max(s.width, o.width) / 2:
                    stops.append((t, ("end", j, k)))
        for vi, v in enumerate(vias):
            t, d = _project(v.x, v.y, s)
            if d <= (v.size + s.width) / 2:
                stops.append((t, ("via", vi)))
        for p in pads:
            if not p.on(s.layer):
                continue
            # contact: the track's copper (centreline +/- half width) touches
            # the pad's actual rectangle; the current enters at the projection
            # of the pad centre
            if p.distance_to_segment(s.x1, s.y1, s.x2, s.y2) <= s.width / 2 + 1e-6:
                t, _ = _project(p.x, p.y, s)
                stops.append((t, ("pad", p.ref, p.number)))
        stops.sort(key=lambda x: x[0])
        rs = sheet_resistance(board.copper(s.layer), temp_c)
        for (t0, n0), (t1, n1) in zip(stops, stops[1:]):
            L = (t1 - t0) * s.length
            edge(n0, n1, rs * L / s.width, L, s.width)
    # A via is one node shared by every layer it joins. Each edge into or out
    # of it carries half the barrel resistance and half a via count, so a path
    # that passes through a via pays for it exactly once.
    via_r = {vi: via_resistance(h, v.drill, temp_c=temp_c) for vi, v in enumerate(vias)}

    def via_cost(node):
        return (via_r[node[1]] / 2, 0.5) if node[0] == "via" else (0.0, 0.0)

    def xy(node):
        if node[0] == "end":
            sg = segs[node[1]]
            return (sg.x1, sg.y1) if node[2] == 0 else (sg.x2, sg.y2)
        if node[0] == "via":
            return vias[node[1]].x, vias[node[1]].y
        return board.pad(node[1], node[2]).x, board.pad(node[1], node[2]).y

    start, goal = ("pad",) + a, ("pad",) + b
    best = {start: (0.0, 0.0, 0, math.inf, [start], [])}
    queue = [(0.0, 0, start)]
    count = 1
    while queue:
        r, _, u = heapq.heappop(queue)
        if u == goal:
            _, length, nv, wmin, nodes, edges = best[u]
            return Path_(r, length, round(nv), wmin, [str(n) for n in nodes[1:]],
                         nodes, edges, [xy(n) for n in nodes])
        if r > best[u][0]:
            continue
        _, length, nv, wmin, nodes, edges = best[u]
        for v, dr, dl, w, dv in graph.get(u, []):
            (ru, nu), (rv, nvv) = via_cost(u), via_cost(v)
            dr, dv = dr + ru + rv, dv + nu + nvv
            nr = r + dr
            if v not in best or nr < best[v][0]:
                best[v] = (nr, length + dl, nv + dv, min(wmin, w), nodes + [v],
                           edges + [{"r": dr, "length": dl, "width": w, "via": dv}])
                count += 1
                heapq.heappush(queue, (nr, count, v))
    return None
