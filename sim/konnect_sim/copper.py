"""DC current flow in the real copper of one net: pours, tracks, pads and vias.

The saved board's *filled* zone polygons (what KiCad actually poured, with
clearances, thermal-relief spokes and islands), the net's tracks and pads, and
its via and plated-hole barrels are rasterised onto a square grid per copper
layer. Each copper cell is a node; neighbouring cells on a layer are joined by
the sheet conductance t/rho; barrels join the layers. Solving the resistor
network gives:

- whether two pads are connected through copper at all (and through what);
- the resistance between pads or pad groups;
- the sheet current density everywhere, so neck-downs, thermal spokes and pad
  entries carrying the current are found, not guessed;
- the current in every via;
- the I^2R power per cell, for the board thermal model.

Method limits (state them with any result):
- DC only. At switching harmonics current crowds under the forward conductor;
  use ``buck.layout_parasitics`` for the switching-loop inductance.
- Grid quantisation: features narrower than ~3 cells are represented with up
  to +/- one cell of width error. ``pitch_mm`` must be set so the narrowest
  copper on the path is >= 3 cells; ``CopperGrid.resolution_note`` reports it.
- Track arcs are not read (straight segments only); a board with arc tracks on
  the path must say so.
- Unfilled zones (saved without fill) carry no copper here: refill (B) and save
  before extracting. ``CopperGrid.unfilled_zones`` lists them.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .layout import ALPHA_CU, MU0, RHO_CU_20C, DEFAULT_PLATING_MM, Board, ipc2221_width_mm

PAD_G = 1e6          # S, ties a terminal to each cell under its pad (equipotential pad)


def _rho(temp_c: float) -> float:
    return RHO_CU_20C * (1 + ALPHA_CU * (temp_c - 20))


def _seg_mask(X, Y, s) -> np.ndarray:
    dx, dy = s.x2 - s.x1, s.y2 - s.y1
    L2 = dx * dx + dy * dy
    if L2 == 0:
        d2 = (X - s.x1) ** 2 + (Y - s.y1) ** 2
    else:
        t = np.clip(((X - s.x1) * dx + (Y - s.y1) * dy) / L2, 0.0, 1.0)
        d2 = (X - (s.x1 + t * dx)) ** 2 + (Y - (s.y1 + t * dy)) ** 2
    return d2 <= (s.width / 2) ** 2


def _pad_mask(X, Y, p) -> np.ndarray:
    c, s_ = math.cos(math.radians(p.rotation)), math.sin(math.radians(p.rotation))
    # the inverse of layout._rotate brings a point into the pad's own frame
    dx, dy = X - p.x, Y - p.y
    lx, ly = dx * c - dy * s_, dx * s_ + dy * c
    hx, hy = p.size[0] / 2, p.size[1] / 2
    if p.shape == "circle":
        return lx * lx + ly * ly <= hx * hx
    if p.shape == "oval":
        r = min(hx, hy)
        cx, cy = np.maximum(np.abs(lx) - (hx - r), 0), np.maximum(np.abs(ly) - (hy - r), 0)
        return cx * cx + cy * cy <= r * r
    return (np.abs(lx) <= hx) & (np.abs(ly) <= hy)


def _poly_mask(X, Y, pts) -> np.ndarray:
    from matplotlib.path import Path as MPath
    return MPath(np.asarray(pts)).contains_points(np.column_stack([X.ravel(), Y.ravel()])).reshape(X.shape)


@dataclass
class CopperGrid:
    board: Board
    net: str
    pitch_mm: float
    x0: float
    y0: float
    nx: int
    ny: int
    layers: list[str]
    mask: dict[str, np.ndarray]          # layer -> (ny, nx) bool
    node: dict[str, np.ndarray]          # layer -> (ny, nx) int node id, -1 off copper
    n_nodes: int
    rows: list = field(default_factory=list)   # conductance edges (a, b, g, kind)
    barrels: list = field(default_factory=list)  # (label, x, y, [(node_a, node_b, edge index)])
    temp_c: float = 25.0
    unfilled_zones: list[str] = field(default_factory=list)
    narrowest_track_mm: float | None = None

    @property
    def resolution_note(self) -> str:
        if self.narrowest_track_mm is None:
            return f"grid {self.pitch_mm} mm"
        cells = self.narrowest_track_mm / self.pitch_mm
        ok = "adequate" if cells >= 3 else "TOO COARSE (< 3 cells across the narrowest track)"
        return f"grid {self.pitch_mm} mm; narrowest track {self.narrowest_track_mm} mm = {cells:.1f} cells ({ok})"

    def xy(self, layer: str, i: int, j: int) -> tuple[float, float]:
        return self.x0 + (j + 0.5) * self.pitch_mm, self.y0 + (i + 0.5) * self.pitch_mm

    def pad_nodes(self, ref: str, number: str) -> list[int]:
        p = self.board.pad(ref, number)
        ids = []
        for layer in self.layers:
            if not p.on(layer):
                continue
            X, Y = self._centres()
            m = _pad_mask(X, Y, p) & self.mask[layer]
            ids += list(self.node[layer][m])
        return ids

    def _centres(self):
        xs = self.x0 + (np.arange(self.nx) + 0.5) * self.pitch_mm
        ys = self.y0 + (np.arange(self.ny) + 0.5) * self.pitch_mm
        return np.meshgrid(xs, ys)


def build(board: Board, net: str, pitch_mm: float = 0.1, temp_c: float = 25.0,
          margin_mm: float = 1.0) -> CopperGrid:
    """Rasterise ``net``'s copper on every copper layer of ``board``."""
    layers = [n for n, kind, _ in board.stack if kind == "copper"] or ["F.Cu", "B.Cu"]
    segs = board.net_segments(net)
    vias = board.net_vias(net)
    pads = [p for f in board.footprints.values() for p in f.pads.values() if p.net == net]
    zones = board.zones_on(net)
    unfilled = [f"{net} zone on {','.join(z.layers)}" for z in zones if not z.filled]

    xs, ys = [], []
    for z in zones:
        for polys in z.fills.values():
            for poly in polys:
                xs += [x for x, _ in poly]
                ys += [y for _, y in poly]
    for s in segs:
        xs += [s.x1, s.x2]
        ys += [s.y1, s.y2]
    for p in pads:
        r = max(p.size) / 2
        xs += [p.x - r, p.x + r]
        ys += [p.y - r, p.y + r]
    for v in vias:
        xs += [v.x - v.size, v.x + v.size]
        ys += [v.y - v.size, v.y + v.size]
    if not xs:
        raise KeyError(f"net {net} has no copper on the board")
    x0, y0 = min(xs) - margin_mm, min(ys) - margin_mm
    nx = int(math.ceil((max(xs) + margin_mm - x0) / pitch_mm))
    ny = int(math.ceil((max(ys) + margin_mm - y0) / pitch_mm))
    gx = x0 + (np.arange(nx) + 0.5) * pitch_mm
    gy = y0 + (np.arange(ny) + 0.5) * pitch_mm
    X, Y = np.meshgrid(gx, gy)

    def window(xa, xb, ya, yb):
        ja, jb = max(int((xa - x0) / pitch_mm) - 1, 0), min(int((xb - x0) / pitch_mm) + 2, nx)
        ia, ib = max(int((ya - y0) / pitch_mm) - 1, 0), min(int((yb - y0) / pitch_mm) + 2, ny)
        return slice(ia, ib), slice(ja, jb)

    mask = {}
    for layer in layers:
        m = np.zeros((ny, nx), bool)
        for z in zones:
            for poly in z.fills.get(layer, []):
                px, py = [q[0] for q in poly], [q[1] for q in poly]
                w = window(min(px), max(px), min(py), max(py))
                m[w] |= _poly_mask(X[w], Y[w], poly)
        for s in segs:
            if s.layer == layer:
                w = window(min(s.x1, s.x2) - s.width, max(s.x1, s.x2) + s.width,
                           min(s.y1, s.y2) - s.width, max(s.y1, s.y2) + s.width)
                m[w] |= _seg_mask(X[w], Y[w], s)
        for p in pads:
            if p.on(layer):
                r = max(p.size)
                w = window(p.x - r, p.x + r, p.y - r, p.y + r)
                m[w] |= _pad_mask(X[w], Y[w], p)
        for v in vias:
            w = window(v.x - v.size, v.x + v.size, v.y - v.size, v.y + v.size)
            m[w] |= (X[w] - v.x) ** 2 + (Y[w] - v.y) ** 2 <= (v.size / 2) ** 2
        mask[layer] = m

    node, n = {}, 0
    for layer in layers:
        ids = -np.ones((ny, nx), int)
        k = int(mask[layer].sum())
        ids[mask[layer]] = np.arange(n, n + k)
        node[layer], n = ids, n + k

    grid = CopperGrid(board, net, pitch_mm, x0, y0, nx, ny, layers, mask, node, n,
                      temp_c=temp_c, unfilled_zones=unfilled,
                      narrowest_track_mm=min((s.width for s in segs), default=None))
    rho = _rho(temp_c)
    a_list, b_list, g_list = [], [], []
    for layer in layers:
        g = board.copper(layer) * 1e-3 / rho          # sheet conductance, S per square
        ids = node[layer]
        for di, dj in ((0, 1), (1, 0)):
            a = ids[: ny - di, : nx - dj]
            b = ids[di:, dj:]
            ok = (a >= 0) & (b >= 0)
            a_list.append(a[ok])
            b_list.append(b[ok])
            g_list.append(np.full(int(ok.sum()), g))
    # Barrels: vias and plated through-holes join the layers. Each layer's
    # cells inside the barrel's land meet at a barrel node on that layer.
    order = [n_ for n_, kind, _ in board.stack if kind == "copper"] or layers
    depth, z = {}, 0.0
    for name, kind, t in (board.stack or [("F.Cu", "copper", 0), ("B.Cu", "copper", board.thickness_mm)]):
        if kind == "copper":
            depth[name] = z
        z += t
    if "B.Cu" not in depth or depth["B.Cu"] == 0:
        depth = {"F.Cu": 0.0, "B.Cu": board.thickness_mm} | {k: v for k, v in depth.items() if k not in ("F.Cu", "B.Cu")}
    holes = [(f"via {v.x:.2f},{v.y:.2f}", v.x, v.y, v.size / 2, v.drill) for v in vias]
    holes += [(f"{p.ref}.{p.number}", p.x, p.y, min(p.size) / 2, p.drill) for p in pads
              if p.drill > 0 and "*.Cu" in p.layers]
    extra = n
    for label, hx, hy, land_r, drill in holes:
        layer_nodes = []
        for layer in order:
            if layer not in mask:
                continue
            w = window(hx - land_r, hx + land_r, hy - land_r, hy + land_r)
            sub = node[layer][w]
            inside = ((X[w] - hx) ** 2 + (Y[w] - hy) ** 2 <= max(land_r, pitch_mm) ** 2) & (sub >= 0)
            if not inside.any():
                continue
            bn = extra
            extra += 1
            cells = sub[inside]
            a_list.append(np.full(len(cells), bn))
            b_list.append(cells)
            g_list.append(np.full(len(cells), PAD_G))
            layer_nodes.append((layer, bn))
        segs_ = []
        for (la, na), (lb, nb) in zip(layer_nodes, layer_nodes[1:]):
            h = abs(depth.get(lb, board.thickness_mm) - depth.get(la, 0.0)) or board.thickness_mm
            area = math.pi * ((drill / 2 + DEFAULT_PLATING_MM) ** 2 - (drill / 2) ** 2) * 1e-6
            r = rho * h * 1e-3 / area
            a_list.append(np.array([na]))
            b_list.append(np.array([nb]))
            g_list.append(np.array([1 / r]))
            segs_.append((na, nb, 1 / r))
        grid.barrels.append((label, hx, hy, segs_, drill))
    grid.n_nodes = extra
    grid.rows = (np.concatenate(a_list), np.concatenate(b_list), np.concatenate(g_list))
    return grid


@dataclass
class Flow:
    grid: CopperGrid
    current_a: float
    resistance_ohm: float | None
    connected: bool
    voltage: np.ndarray | None = None                     # per node, V
    sheet_current: dict[str, np.ndarray] = field(default_factory=dict)  # layer -> A/mm
    power_w: dict[str, np.ndarray] = field(default_factory=dict)        # layer -> W per cell
    via_currents: list[tuple[str, float, float]] = field(default_factory=list)  # (label, A, drill)

    def peak(self) -> tuple[float, str, float, float]:
        """(A/mm, layer, x, y) of the highest sheet current density."""
        best = (0.0, "", 0.0, 0.0)
        for layer, k in self.sheet_current.items():
            if k.size and np.nanmax(k) > best[0]:
                i, j = np.unravel_index(np.nanargmax(k), k.shape)
                x, y = self.grid.xy(layer, i, j)
                best = (float(k[i, j]), layer, x, y)
        return best


def _laplacian(grid: CopperGrid, extra: int = 0):
    from scipy.sparse import coo_matrix
    a, b, g = grid.rows
    n = grid.n_nodes + extra
    rows = np.concatenate([a, b, a, b])
    cols = np.concatenate([b, a, a, b])
    vals = np.concatenate([-g, -g, g, g])
    return coo_matrix((vals, (rows, cols)), shape=(n, n)).tocsr()


def components(grid: CopperGrid) -> np.ndarray:
    """Connected-component label of every node."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.csgraph import connected_components
    a, b, _ = grid.rows
    adj = coo_matrix((np.ones(len(a)), (a, b)), shape=(grid.n_nodes, grid.n_nodes))
    return connected_components(adj, directed=False)[1]


def connectivity(grid: CopperGrid, pads: list[tuple[str, str]]) -> dict:
    """Which of ``pads`` share copper. Returns {"groups": [[pads...], ...],
    "connected": bool, "missing": [pads with no copper cell]}."""
    lab = components(grid)
    groups: dict[int, list] = {}
    missing = []
    for ref, num in pads:
        ids = grid.pad_nodes(ref, num)
        if not ids:
            missing.append((ref, num))
            continue
        for c in sorted({int(lab[i]) for i in ids}):
            groups.setdefault(c, []).append((ref, num))
    gl = list(groups.values())
    return {"groups": gl, "connected": len(gl) == 1 and not missing, "missing": missing}


def solve(grid: CopperGrid, source: list[tuple[str, str]], sink: list[tuple[str, str]],
          current_a: float = 1.0) -> Flow:
    """Drive ``current_a`` into the ``source`` pads (tied together) and out of
    the ``sink`` pads (tied together, the 0 V reference)."""
    from scipy.sparse import coo_matrix
    from scipy.sparse.linalg import spsolve

    src = [i for p in source for i in grid.pad_nodes(*p)]
    snk = [i for p in sink for i in grid.pad_nodes(*p)]
    if not src or not snk:
        return Flow(grid, current_a, None, False)
    lab = components(grid)
    if not ({int(lab[i]) for i in src} & {int(lab[i]) for i in snk}):
        return Flow(grid, current_a, None, False)
    n = grid.n_nodes
    ts, tk = n, n + 1                                    # terminal nodes
    a = np.concatenate([np.full(len(src), ts), np.full(len(snk), tk)])
    b = np.array(src + snk)
    G = _laplacian(grid, 2) + coo_matrix(
        (np.concatenate([-np.full(len(a), PAD_G)] * 2 + [np.full(len(a), PAD_G)] * 2),
         (np.concatenate([a, b, a, b]), np.concatenate([b, a, a, b]))), shape=(n + 2, n + 2)).tocsr()
    # keep only the component that carries the current; ground the sink terminal
    comp = int(lab[src[0]])
    keep = np.concatenate([np.where(lab == comp)[0], [ts]])
    G = G[keep][:, keep]
    I = np.zeros(len(keep))
    I[-1] = current_a
    # The sink terminal is the 0 V reference: dropping its row and column
    # leaves its PAD_G ties on the sink cells' diagonals, which grounds them.
    V = spsolve(G.tocsc(), I)
    v_full = np.zeros(n)
    v_full[keep[:-1]] = V[:-1]
    r = float(V[-1] / current_a)

    flow = Flow(grid, current_a, r, True, v_full)
    p = grid.pitch_mm
    rho = _rho(grid.temp_c)
    for layer in grid.layers:
        ids = grid.node[layer]
        on = ids >= 0
        vv = np.where(on, v_full[np.clip(ids, 0, None)], np.nan)
        g = grid.board.copper(layer) * 1e-3 / rho
        # edge currents (A) between horizontal and vertical neighbours
        ix = np.where(on[:, 1:] & on[:, :-1], (vv[:, :-1] - vv[:, 1:]) * g, 0.0)
        iy = np.where(on[1:, :] & on[:-1, :], (vv[:-1, :] - vv[1:, :]) * g, 0.0)
        jx = np.zeros_like(vv)
        jy = np.zeros_like(vv)
        jx[:, :-1] += ix / 2
        jx[:, 1:] += ix / 2
        jy[:-1, :] += iy / 2
        jy[1:, :] += iy / 2
        k = np.hypot(jx, jy) / p                       # A per mm of width
        flow.sheet_current[layer] = np.where(on, k, 0.0)
        pw = np.zeros_like(vv)
        pw[:, :-1] += ix ** 2 / g / 2
        pw[:, 1:] += ix ** 2 / g / 2
        pw[:-1, :] += iy ** 2 / g / 2
        pw[1:, :] += iy ** 2 / g / 2
        flow.power_w[layer] = np.where(on, pw, 0.0)
    # barrel currents: the largest segment current of each barrel
    vt = np.zeros(n + 2)
    vt[keep] = V
    for label, hx, hy, segs_, drill in grid.barrels:
        if segs_:
            i_b = max(abs(vt[na] - vt[nb]) * gb for na, nb, gb in segs_)
            flow.via_currents.append((label, float(i_b), drill))
    return flow


def ipc2221_current_a(area_mm2: float, rise_c: float, external: bool = True) -> float:
    """Current for a conductor cross-section from IPC-2221: I = k dT^0.44 A^0.725."""
    k = 0.048 if external else 0.024
    return k * rise_c ** 0.44 * (area_mm2 / 0.0254 ** 2) ** 0.725


def _pad_cells(flow: Flow, layer: str) -> np.ndarray:
    g = flow.grid
    X, Y = g._centres()
    m = np.zeros((g.ny, g.nx), bool)
    for f in g.board.footprints.values():
        for p in f.pads.values():
            if p.net == g.net and p.on(layer):
                m |= _pad_mask(X, Y, p)
    return m


def _smoothed(k: np.ndarray, on: np.ndarray, cells: int) -> np.ndarray:
    """Mean of ``k`` over copper cells in a (cells x cells) window."""
    from scipy.ndimage import uniform_filter
    if cells <= 1:
        return k
    num = uniform_filter(np.where(on, k, 0.0), size=cells, mode="constant")
    den = uniform_filter(on.astype(float), size=cells, mode="constant")
    return np.where(on, num / np.maximum(den, 1e-12), 0.0)


def capacity(flow: Flow, rms_a: float, rise_c: float, exclude_pads: bool = True,
             smooth_mm: float = 1.0) -> dict:
    """Copper and barrel capacity for ``rms_a`` flowing as solved (scaled).

    Copper: the narrowest effective section is where the sheet current density
    peaks; an equivalent conductor of width I/K_peak must be at least the
    IPC-2221 width for ``rms_a`` (conservative: it assumes the whole current
    crowds at the peak density). Barrels: each via's share of ``rms_a`` against
    IPC-2221 internal-conductor capacity of its plating cross-section.

    IPC-2221 sizes conductors, not the entry into a component pin, so with
    ``exclude_pads`` the copper check uses the peak outside pad copper and the
    pad-entry peak is reported separately (``pad_peak_a_per_mm``).

    Current density is singular at the inner corner of a bend and grows as the
    grid is refined, while copper spreads heat over about a millimetre; the
    density is therefore averaged over ``smooth_mm`` before its peak is taken.
    A real neck longer than ``smooth_mm`` survives the averaging; a corner
    spike does not. The unsmoothed grid peak is reported as ``raw_peak_a_per_mm``.
    """
    scale = rms_a / flow.current_a
    raw = flow.peak()[0]
    cells = max(int(round(smooth_mm / flow.grid.pitch_mm)), 1)
    sm = {lyr: _smoothed(k, flow.grid.mask[lyr], cells) for lyr, k in flow.sheet_current.items()}
    k_pad = max((float(np.max(np.where(_pad_cells(flow, lyr), k, 0.0))) for lyr, k in sm.items()), default=0.0)
    if exclude_pads:
        best = (0.0, "", 0.0, 0.0)
        for lyr, k in sm.items():
            kk = np.where(_pad_cells(flow, lyr), 0.0, k)
            if kk.size and kk.max() > best[0]:
                i, j = np.unravel_index(np.argmax(kk), kk.shape)
                best = (float(kk[i, j]), lyr, *flow.grid.xy(lyr, i, j))
        k_peak, layer, x, y = best
    else:
        best = (0.0, "", 0.0, 0.0)
        for lyr, k in sm.items():
            if k.size and k.max() > best[0]:
                i, j = np.unravel_index(np.argmax(k), k.shape)
                best = (float(k[i, j]), lyr, *flow.grid.xy(lyr, i, j))
        k_peak, layer, x, y = best
    k_peak *= scale
    t = flow.grid.board.copper(layer) if layer else 0.035
    w_need = ipc2221_width_mm(rms_a, rise_c, t)
    w_eq = rms_a / k_peak if k_peak else math.inf
    vias = []
    for label, i_v, drill in flow.via_currents:
        area = math.pi * ((drill / 2 + DEFAULT_PLATING_MM) ** 2 - (drill / 2) ** 2)
        allow = ipc2221_current_a(area, rise_c, external=False)
        vias.append({"via": label, "current_a": i_v * scale, "allow_a": allow,
                     "ok": i_v * scale <= allow})
    return {"peak_a_per_mm": k_peak, "at": (layer, round(float(x), 2), round(float(y), 2)),
            "pad_peak_a_per_mm": k_pad * scale, "raw_peak_a_per_mm": raw * scale,
            "smooth_mm": smooth_mm,
            "equivalent_width_mm": w_eq, "ipc_width_mm": w_need, "copper_ok": w_eq >= w_need,
            "vias": vias, "vias_ok": all(v["ok"] for v in vias),
            "plating_mm_assumed": DEFAULT_PLATING_MM}


def plane_inductance_estimate(flow: Flow, gap_mm: float) -> float:
    """Low-frequency partial inductance (H) of the solved current distribution
    returning against a parallel conductor ``gap_mm`` away, by field energy
    between two sheets carrying equal and opposite currents:
    L = mu0 * gap * sum(K^2 * A) / I^2. Uses the DC distribution; at switching
    harmonics current crowds and the true value is higher."""
    p = flow.grid.pitch_mm * 1e-3
    tot = 0.0
    for k in flow.sheet_current.values():
        tot += float(np.nansum((k * 1e3) ** 2)) * p * p   # (A/m)^2 * m^2
    return MU0 * gap_mm * 1e-3 * tot / flow.current_a ** 2
