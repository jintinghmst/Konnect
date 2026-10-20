"""Steady-state board temperature: heat spreading in copper and laminate.

A finite-difference model on a square grid over the board. Each copper layer is
a sheet whose lateral conductance follows the real copper coverage (every net's
pours, tracks and pads); the laminate between layers conducts vertically and
laterally; both faces lose heat to ambient through a film coefficient ``h``
(convection plus linearised radiation). Heat sources are component losses
spread over their pads and the I^2R maps from ``copper.solve``.

Limits: steady state only; ``h`` is a stated input (natural convection on a
horizontal board is typically quoted 5-15 W/m^2K, so an unmeasured ``h`` keeps
the result INCOMPLETE); component package-to-board resistance enters through
the ``psi`` values given to ``component_temps``; vias are not modelled as
thermal paths except as copper area.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np

from .copper import _pad_mask, _poly_mask, _seg_mask
from .layout import Board
from .report import Param, check, val

K_COPPER = Param(385.0, "W/mK", "copper thermal conductivity (physical constant)")


@dataclass
class ThermalResult:
    x0: float
    y0: float
    pitch_mm: float
    layers: list[str]
    temp_c: dict[str, np.ndarray]
    ambient_c: float

    def peak(self) -> tuple[float, str, float, float]:
        best = (-1e9, "", 0.0, 0.0)
        for layer, t in self.temp_c.items():
            i, j = np.unravel_index(np.argmax(t), t.shape)
            if t[i, j] > best[0]:
                best = (float(t[i, j]), layer, self.x0 + (j + 0.5) * self.pitch_mm,
                        self.y0 + (i + 0.5) * self.pitch_mm)
        return best

    def at(self, layer: str, x: float, y: float) -> float:
        t = self.temp_c[layer]
        j = min(max(int((x - self.x0) / self.pitch_mm), 0), t.shape[1] - 1)
        i = min(max(int((y - self.y0) / self.pitch_mm), 0), t.shape[0] - 1)
        return float(t[i, j])


def solve(board: Board, *, ambient_c: float, h_w_m2k: Param, k_laminate_inplane: Param,
          k_laminate_through: Param, footprint_watts: dict[str, float] | None = None,
          copper_flows: list | None = None, pitch_mm: float = 0.5,
          outline: tuple[float, float, float, float] | None = None) -> ThermalResult:
    """``footprint_watts``: {ref: W} spread over that footprint's pads;
    ``copper_flows``: ``copper.Flow`` objects scaled to their real current
    (their ``power_w`` maps are added); ``outline``: (x0, y0, x1, y1) of the
    board, default the extent of all copper and footprints."""
    layers = [n for n, kind, _ in board.stack if kind == "copper"] or ["F.Cu", "B.Cu"]
    if outline is None:
        xs, ys = [], []
        for z in board.zones:
            xs += [p[0] for p in z.outline]
            ys += [p[1] for p in z.outline]
        for f in board.footprints.values():
            for p in f.pads.values():
                xs.append(p.x)
                ys.append(p.y)
        outline = (min(xs) - 1, min(ys) - 1, max(xs) + 1, max(ys) + 1)
    x0, y0, x1, y1 = outline
    nx, ny = int(math.ceil((x1 - x0) / pitch_mm)), int(math.ceil((y1 - y0) / pitch_mm))
    X, Y = np.meshgrid(x0 + (np.arange(nx) + 0.5) * pitch_mm, y0 + (np.arange(ny) + 0.5) * pitch_mm)
    pads = [p for f in board.footprints.values() for p in f.pads.values()]

    # copper coverage per layer (sampled at 3x3 points per cell)
    cover = {}
    sub = (np.arange(3) + 0.5) / 3 - 0.5
    for layer in layers:
        acc = np.zeros((ny, nx))
        for du in sub:
            for dv in sub:
                Xs, Ys = X + du * pitch_mm, Y + dv * pitch_mm
                m = np.zeros((ny, nx), bool)
                for z in board.zones:
                    for poly in z.fills.get(layer, []):
                        m |= _poly_mask(Xs, Ys, poly)
                for s in board.segments:
                    if s.layer == layer:
                        m |= _seg_mask(Xs, Ys, s)
                for p in pads:
                    if p.on(layer):
                        m |= _pad_mask(Xs, Ys, p)
                acc += m
        cover[layer] = acc / 9

    a = pitch_mm * 1e-3                         # cell edge, m
    area = a * a
    nl = len(layers)
    gaps = []
    names = [n for n, _, _ in board.stack]
    for la, lb in zip(layers, layers[1:]):
        if la in names and lb in names:
            gaps.append(board.layer_gap_mm(la, lb))
        else:
            gaps.append(board.thickness_mm - 0.07)
    lam_t = sum(gaps) * 1e-3
    idx = np.arange(nl * ny * nx).reshape(nl, ny, nx)
    rows, cols, vals = [], [], []

    def link(i, j, g):
        rows.extend([i, j, i, j])
        cols.extend([j, i, i, j])
        vals.extend([-g, -g, g, g])

    diag = np.zeros(nl * ny * nx)
    for k, layer in enumerate(layers):
        t_cu = board.copper(layer) * 1e-3
        # sheet conductance per cell: copper share plus this layer's half of the laminate
        gs = val(K_COPPER) * t_cu * cover[layer] + val(k_laminate_inplane) * lam_t / nl
        gx = 2 / (1 / gs[:, :-1] + 1 / gs[:, 1:])          # series of two half-cells
        gy = 2 / (1 / gs[:-1, :] + 1 / gs[1:, :])
        for g, a_, b_ in ((gx, idx[k, :, :-1], idx[k, :, 1:]), (gy, idx[k, :-1, :], idx[k, 1:, :])):
            rows.extend(np.concatenate([a_.ravel(), b_.ravel(), a_.ravel(), b_.ravel()]))
            cols.extend(np.concatenate([b_.ravel(), a_.ravel(), a_.ravel(), b_.ravel()]))
            vals.extend(np.concatenate([-g.ravel(), -g.ravel(), g.ravel(), g.ravel()]))
        if k in (0, nl - 1):                                 # faces to ambient
            diag[idx[k].ravel()] += val(h_w_m2k) * area
    for k, gap in enumerate(gaps):
        g = val(k_laminate_through) * area / (gap * 1e-3)
        a_, b_ = idx[k].ravel(), idx[k + 1].ravel()
        rows.extend(np.concatenate([a_, b_, a_, b_]))
        cols.extend(np.concatenate([b_, a_, a_, b_]))
        vals.extend(np.concatenate([np.full(len(a_), -g)] * 2 + [np.full(len(a_), g)] * 2))

    q = np.zeros(nl * ny * nx)
    for ref, w in (footprint_watts or {}).items():
        f = board.footprints[ref]
        k = layers.index(f.layer) if f.layer in layers else 0
        m = np.zeros((ny, nx), bool)
        for p in f.pads.values():
            m |= _pad_mask(X, Y, p)
        if not m.any():
            i = min(max(int((f.y - y0) / pitch_mm), 0), ny - 1)
            j = min(max(int((f.x - x0) / pitch_mm), 0), nx - 1)
            m[i, j] = True
        q[idx[k][m]] += w / m.sum()
    for flow in copper_flows or []:
        gr = flow.grid
        for layer, pw in flow.power_w.items():
            if layer not in layers:
                continue
            k = layers.index(layer)
            ii, jj = np.nonzero(pw)
            xs_ = gr.x0 + (jj + 0.5) * gr.pitch_mm
            ys_ = gr.y0 + (ii + 0.5) * gr.pitch_mm
            ti = np.clip(((ys_ - y0) / pitch_mm).astype(int), 0, ny - 1)
            tj = np.clip(((xs_ - x0) / pitch_mm).astype(int), 0, nx - 1)
            np.add.at(q, idx[k][ti, tj], pw[ii, jj])

    from scipy.sparse import coo_matrix, diags
    from scipy.sparse.linalg import spsolve
    G = coo_matrix((np.asarray(vals, float), (np.asarray(rows), np.asarray(cols))),
                   shape=(len(q), len(q))).tocsr() + diags(diag)
    rise = spsolve(G.tocsc(), q)
    temp = {layer: ambient_c + rise[idx[k]] for k, layer in enumerate(layers)}
    return ThermalResult(x0, y0, pitch_mm, layers, temp, ambient_c)


def board_checks(res: ThermalResult, board: Board, *, h_w_m2k: Param, k_laminate_inplane: Param,
                 k_laminate_through: Param, max_board_c: Param | None = None,
                 psi: dict[str, tuple[Param, float]] | None = None,
                 limits: dict[str, Param] | None = None) -> list:
    """Hot spot and per-component temperatures. ``psi``: {ref: (psi or theta to
    board in K/W, watts)} gives a component temperature above its board spot;
    ``limits``: {ref: max °C}."""
    inputs = {"film coefficient h": h_w_m2k, "laminate k in-plane": k_laminate_inplane,
              "laminate k through": k_laminate_through, "copper k": K_COPPER}
    t, layer, x, y = res.peak()
    lim = val(max_board_c) if max_board_c is not None else None
    out = [check("Board hot spot", f"<= {lim:.0f} °C" if lim else "reported",
                 f"{t:.1f} °C on {layer} at ({x:.1f}, {y:.1f}) mm, ambient {res.ambient_c:g} °C",
                 (t <= lim) if lim else True, "finite-difference heat spreading (copper coverage, laminate, h)",
                 inputs | {"board limit": max_board_c}, value=t, limit=lim,
                 to_complete="measure with a thermal camera at full load to confirm h")]
    for ref, (r_th, watts) in (psi or {}).items():
        f = board.footprints[ref]
        tb = res.at(f.layer if f.layer in res.layers else res.layers[0], f.x, f.y)
        tc = tb + val(r_th) * watts
        lim_c = val(limits[ref]) if limits and ref in limits else None
        out.append(check(f"{ref} temperature", f"<= {lim_c:.0f} °C" if lim_c else "reported",
                         f"{tc:.1f} °C (board {tb:.1f} °C + {val(r_th):.0f} K/W x {watts * 1e3:.0f} mW)",
                         (tc <= lim_c) if lim_c else True, "board temperature + package resistance",
                         inputs | {f"{ref} package resistance": r_th,
                                   f"{ref} limit": limits.get(ref) if limits else None},
                         value=tc, limit=lim_c))
    return out
