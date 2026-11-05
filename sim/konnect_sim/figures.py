"""Report figures: every checked quantity drawn against its limit.

Circuit-independent chart builders that write PNG files for ``report.Report``
(``figures``) and ``tools.write_report``. One style for every chart: a light
surface, recessive grid, thin 2 px lines, the first three categorical slots of
the reference palette (validated all-pairs for up to three series) with a
distinct marker per series and a direct label, so identity never rests on
colour alone. Spec limits are dashed red reference lines with their label.

- ``metric_vs_x``    one panel per metric (e.g. ripple vs load, one line per Vin)
- ``waveform_grid``  small multiples of time waveforms over a 2-D sweep
- ``spectrum``       harmonic levels against a limit line (conducted emissions)
- ``bode``           loop-gain magnitude and phase from ``converter.loop_gain``
- ``ripple_figures`` the standard switching-converter set from snapshot windows
                     (``converter.snapshot``): summary plus output, input,
                     inductor-current and switch-node grids
- ``checks_vs_limits`` every numeric check as its margin to its limit, coloured by status
- ``curves``         y against x in stacked panels (SPICE vectors, impedance, manufacturer
                     curves) with limit lines and marked operating points
- ``cases``          a metric's value at every tolerance case, nominal and worst marked
- ``bars``           one bar per item (e.g. coupled track runs)
- ``board_map``      a per-cell board quantity (current density, copper groups) per layer
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
CRITICAL = "#d03b3b"
SERIES = [("#2a78d6", "o"), ("#eb6834", "s"), ("#1baf7a", "^"), ("#6250d6", "D")]   # colour, marker
STATUS = {"PASS": "#1b7f4c", "FAIL": CRITICAL, "INCOMPLETE": "#a66a00"}


def _plt():
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    plt.rcParams.update({
        "figure.facecolor": SURFACE, "axes.facecolor": SURFACE, "savefig.facecolor": SURFACE,
        "axes.edgecolor": AXIS, "axes.labelcolor": INK2, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.grid": True, "grid.color": GRID, "grid.linewidth": 0.6, "axes.spines.top": False,
        "axes.spines.right": False, "font.size": 9, "axes.titlesize": 10, "axes.titlecolor": INK,
        "lines.linewidth": 2.0, "lines.markersize": 6, "legend.frameon": False, "text.color": INK})
    return plt


def _save(fig, path) -> str:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=140)
    import matplotlib.pyplot as plt
    plt.close(fig)
    return str(path)


def limit_line(ax, y: float, text: str, *, x: float = 0.0, below: bool = False):
    """Dashed spec line; its label sits at axes fraction ``x`` (choose where the plot is empty)."""
    ax.axhline(y, color=CRITICAL, lw=1.2, ls="--")
    ax.annotate(text, xy=(x, y), xycoords=("axes fraction", "data"), xytext=(4, -3 if below else 3),
                textcoords="offset points", color=CRITICAL, va="top" if below else "bottom", fontsize=8)


def _direct_labels(ax, x_end, ends, span):
    """End-of-line labels nudged apart (min gap 6 % of the axis span)."""
    placed = []
    for y, text in sorted(ends):
        y = max(y, placed[-1] + 0.06 * span) if placed else y
        placed.append(y)
        ax.annotate(text, xy=(x_end, y), xytext=(6, 0), textcoords="offset points", color=INK2,
                    va="center", fontsize=8, annotation_clip=False)


def metric_vs_x(path, x, panels: list[dict], *, xlabel: str, title: str = "", note: str = "") -> str:
    """One panel per metric. ``panels``: [{"title", "series": {label: ys}, "limit": y | None,
    "limit_label": str, "limit_below": bool, "limit_x": 0..1, "ymax": float | None}]."""
    plt = _plt()
    fig, axs = plt.subplots(1, len(panels), figsize=(3.9 * len(panels), 3.8), squeeze=False)
    for ax, p in zip(axs[0], panels):
        for (lab, ys), (c, mk) in zip(p["series"].items(), SERIES):
            ax.plot(x, ys, color=c, marker=mk, label=lab)
        top = p.get("ymax") or max(max(max(ys) for ys in p["series"].values()), p.get("limit") or 0) * 1.1
        ax.set_ylim(0, top)
        if p.get("limit") is not None:
            limit_line(ax, p["limit"], p.get("limit_label", "limit"), x=p.get("limit_x", 0.0),
                       below=p.get("limit_below", False))
        _direct_labels(ax, x[-1], [(ys[-1], lab) for lab, ys in p["series"].items()], top)
        ax.set_title(p["title"]); ax.set_xlabel(xlabel)
    if len(next(iter(panels))["series"]) > 1:
        axs[0][0].legend(loc="center right")
    if title:
        fig.suptitle(title, color=INK)
    if note:
        fig.text(0.01, 0.01, note, color=MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.03 if note else 0, 1, 1))
    return _save(fig, path)


def waveform_grid(path, rows: list, cols: list, get, *, ylabel: str, title: str, scale: float = 1.0,
                  unit: str = "", ac: bool = False, band: float | None = None, row_label=str, col_label=str,
                  empty_text: str = "") -> str:
    """Small multiples, shared axes. ``get(row, col)`` -> (t_s, y), (t_s, y, pp) or None (draws
    ``empty_text``). ``ac`` removes the mean; ``band`` draws ±band (scaled units) as the spec
    limit; each panel states its peak-to-peak value: ``pp`` when given (measured over a longer
    window than the one drawn), else that of the drawn trace."""
    plt = _plt()
    fig, axs = plt.subplots(len(rows), len(cols), figsize=(2.6 * len(cols), 2.3 * len(rows)),
                            sharex=True, sharey=True, squeeze=False)
    for r, rv in enumerate(rows):
        c = SERIES[r % len(SERIES)][0]
        for k, cv in enumerate(cols):
            ax = axs[r][k]
            d = get(rv, cv)
            if r == 0:
                ax.set_title(col_label(cv))
            if k == 0:
                ax.set_ylabel(f"{row_label(rv)}\n{ylabel}")
            if r == len(rows) - 1:
                ax.set_xlabel("time (µs)")
            if d is None:
                import textwrap
                ax.text(0.5, 0.5, "\n".join(textwrap.wrap(empty_text.replace("\n", " "), 20)),
                        transform=ax.transAxes, ha="center", va="center", fontsize=8, color=MUTED)
                continue
            t, y, *pp = d
            y = np.asarray(y)
            yy = (y - y.mean()) if ac else y
            ax.plot((np.asarray(t) - t[0]) * 1e6, yy * scale, color=c, lw=1.4)
            if band is not None:
                for s in (1, -1):
                    ax.axhline(s * band, color=CRITICAL, lw=1.0, ls="--")
            ax.text(0.97, 0.94, f"{(pp[0] if pp else np.ptp(y)) * scale:.3g} {unit}pp", transform=ax.transAxes, ha="right",
                    va="top", fontsize=8, color=INK2,
                    bbox={"boxstyle": "round,pad=0.2", "facecolor": SURFACE, "edgecolor": "none", "alpha": 0.9})
    sub = f"  (dashed: limit ±{band:g} {unit} = {2 * band:g} {unit}pp)" if band is not None else ""
    fig.suptitle(title + sub, color=INK)
    fig.tight_layout()
    return _save(fig, path)


def spectrum(path, f, level_dbuv, limit_points, *, title: str, limit_label: str) -> str:
    """Harmonic levels (stems) against a stepped limit line [[f_hz, dBuV], ...]; the worst
    margin is labelled at its harmonic."""
    plt = _plt()
    f, lv = np.asarray(f), np.asarray(level_dbuv)
    lf, ll = np.array([p[0] for p in limit_points]), np.array([p[1] for p in limit_points])
    keep = (f >= lf.min()) & (f <= lf.max()) & (lv > 0)
    fig, ax = plt.subplots(figsize=(6.5, 3.8))
    ax.vlines(f[keep], 0, lv[keep], color=SERIES[0][0], lw=1.2)
    ax.plot(lf, ll, color=CRITICAL, lw=1.4, ls="--")
    ax.annotate(limit_label, xy=(lf[-1], ll[-1]), xytext=(-4, 4), textcoords="offset points", ha="right",
                color=CRITICAL, fontsize=8)
    if keep.any():
        lim_at = np.interp(np.log(f[keep]), np.log(lf), ll)
        j = int(np.argmin(lim_at - lv[keep]))
        ax.annotate(f"{f[keep][j] / 1e3:.0f} kHz: {lv[keep][j]:.1f} dBµV, margin {lim_at[j] - lv[keep][j]:.1f} dB",
                    xy=(f[keep][j], lv[keep][j]), xytext=(8, 6), textcoords="offset points", color=INK2, fontsize=8)
    ax.set_xscale("log"); ax.set_xlabel("frequency (Hz)"); ax.set_ylabel("dBµV")
    ax.set_ylim(0, max(ll.max(), lv[keep].max() if keep.any() else 0) + 15)
    ax.set_title(title)
    fig.tight_layout()
    return _save(fig, path)


def bode(path, curves: list[tuple[str, dict, list]], *, title: str, pm_min: float | None = None) -> str:
    """``curves``: [(label, margins dict, loop_gain points)] as from ``converter.loop_gain``/``margins``."""
    plt = _plt()
    fig, axs = plt.subplots(2, 1, figsize=(6.5, 5.5), sharex=True)
    for (lab, mg, pts), (c, mk) in zip(curves, SERIES):
        f = [p["f"] for p in pts]
        tag = (f": fc {mg['fc_hz'] / 1e3:.0f} kHz, PM {mg['phase_margin_deg']:.0f}°"
               if mg.get("fc_hz") and mg.get("phase_margin_deg") is not None else "")
        axs[0].semilogx(f, [p["mag_db"] for p in pts], color=c, marker=mk, ms=4, label=lab + tag)
        axs[1].semilogx(f, [p["phase_deg"] for p in pts], color=c, marker=mk, ms=4)
    axs[0].axhline(0, color=AXIS, lw=1.0); axs[0].set_ylabel("|T| (dB)"); axs[0].legend(fontsize=7)
    if pm_min is not None:
        axs[1].axhline(pm_min - 180, color=CRITICAL, lw=1.0, ls="--")
        axs[1].annotate(f"{pm_min - 180:g}° (PM {pm_min:g}° limit)", xy=(0.35, pm_min - 180),
                        xycoords=("axes fraction", "data"), xytext=(4, -3), textcoords="offset points",
                        color=CRITICAL, fontsize=8, va="top")
    axs[1].set_ylabel("phase (deg)"); axs[1].set_xlabel("frequency (Hz)")
    fig.suptitle(title, color=INK)
    fig.tight_layout()
    return _save(fig, path)


def ripple_figures(out_dir, snaps: dict, vins: list[float], loads: list[float], *, vout_pp_max: float | None = None,
                   vin_pp_max: float | None = None, il_pp_target: float | None = None,
                   not_modelled: dict | None = None, period: float | None = None, draw_cycles: int = 3,
                   key=lambda vin, i: f"{vin:g}V_{i:g}A") -> list[tuple[str, str]]:
    """The standard ripple set for a switching converter, from ``converter.snapshot`` arrays
    (rows t, vin, vload, il, vsw) keyed by ``key(vin, load)``. Limits in volts / amperes.
    Returns [(title, path)] for ``Report.figures``.

    Every peak-to-peak value is taken over the whole snapshot, the window the checks measure;
    with ``period`` the waveform grids draw only its last ``draw_cycles`` switching periods.

    A point is drawn as text, never as a zero, when the switch never toggled in the window
    or when ``not_modelled`` names its load ({load: reason}, e.g. {0.0: "PFM bursts not
    modelled"}): a model that cannot represent an operating mode must not report its ripple."""
    out = Path(out_dir)
    not_modelled = not_modelled or {}

    def get(vin, i, row):
        s = snaps.get(key(vin, i))
        if s is None or i in not_modelled or np.ptp(s[4]) < 1.0:
            return None
        m = s[0] >= s[0][-1] - draw_cycles * period if period else slice(None)
        return s[0][m], s[row][m], float(np.ptp(s[row]))

    live = [i for i in loads if all(get(v, i, 2) is not None for v in vins)]
    figs = []
    if live:
        def ser(row, scale):
            return {f"Vin {v:g} V": [get(v, i, row)[2] * scale for i in live] for v in vins}
        panels = [{"title": "Output ripple (mVpp)", "series": ser(2, 1e3),
                   "limit": vout_pp_max * 1e3 if vout_pp_max else None,
                   "limit_label": f"limit {vout_pp_max * 1e3:g} mVpp" if vout_pp_max else ""},
                  {"title": "Input ripple at the IC (mVpp)", "series": ser(1, 1e3),
                   "limit": vin_pp_max * 1e3 if vin_pp_max else None,
                   "limit_label": f"limit {vin_pp_max * 1e3:g} mVpp" if vin_pp_max else ""},
                  {"title": "Inductor current ripple (App)", "series": ser(3, 1.0), "limit": il_pp_target,
                   "limit_label": f"target {il_pp_target:g} App" if il_pp_target else "", "limit_below": True,
                   "limit_x": 0.45}]
        skipped = [i for i in loads if i not in live]
        note = "; ".join(f"{i:g} A not plotted: " + not_modelled.get(i, "no switching cycle in the window")
                         for i in skipped)
        figs.append(("Ripple summary", metric_vs_x(out / "fig_ripple_summary.png", live, panels,
                                                   xlabel="load current (A)", note=note,
                                                   title="Ripple at every operating point")))
    empty = "\n".join(sorted(set(not_modelled.values()))) or "pulse skipping:\nno switching cycle\nin the window"
    common = dict(rows=vins, cols=loads, row_label=lambda v: f"Vin {v:g} V", col_label=lambda i: f"{i:g} A",
                  empty_text=empty)
    for title, name, row, scale, unit, ylab, band, ac in (
            ("Output voltage ripple (AC part)", "fig_ripple_vout.png", 2, 1e3, "mV", "V_OUT − mean (mV)",
             vout_pp_max * 500 if vout_pp_max else None, True),
            ("Input voltage ripple at the IC (AC part)", "fig_ripple_vin.png", 1, 1e3, "mV", "V_IN − mean (mV)",
             vin_pp_max * 500 if vin_pp_max else None, True),
            ("Inductor current", "fig_ripple_il.png", 3, 1.0, "A", "I_L (A)", None, False),
            ("Switch-node voltage", "fig_ripple_sw.png", 4, 1.0, "V", "V_SW (V)", None, False)):
        figs.append((title, waveform_grid(out / name, get=lambda v, i, r=row: get(v, i, r), ylabel=ylab,
                                          title=f"{title} — rows: input voltage, columns: load", scale=scale,
                                          unit=unit, ac=ac, band=band, **common)))
    return figs


def _get(c, k, default=None):
    return c.get(k, default) if isinstance(c, dict) else getattr(c, k, default)


def checks_vs_limits(path, checks, *, title: str = "Checks against their limits") -> str | None:
    """Each check with a numeric value and limit as a bar of its margin to the limit, in % of
    the limit: right of zero meets it, left misses. Bars take the status colour and carry the
    reported result. Returns None when no check is numeric."""
    rows = []
    for c in checks:
        v, lim, sense = _get(c, "value"), _get(c, "limit"), _get(c, "sense", "max")
        if v is None or lim is None or lim == 0:
            continue
        m = (lim - v) / abs(lim) * 100
        rows.append((_get(c, "name"), m if sense == "max" else -m, _get(c, "status"), _get(c, "result", "")))
    if not rows:
        return None
    import textwrap
    plt = _plt()
    names = [textwrap.fill(str(r[0]), 26) for r in rows]
    fig, ax = plt.subplots(figsize=(8.0, 0.9 + 0.2 * sum(n.count("\n") + 2 for n in names)))
    shown = [max(-100.0, min(100.0, m)) for _, m, _, _ in rows]
    ax.barh(range(len(rows)), shown, color=[STATUS.get(s, MUTED) for _, _, s, _ in rows], height=0.6)
    for i, ((_, m, _, result), x) in enumerate(zip(rows, shown)):
        text = str(result) + ("" if x == m else f" ({m:+.0f} %)")
        text = text if len(text) <= 30 else text[:29] + "…"       # the full result is in the report table
        ax.annotate(text, xy=(x, i), xytext=(4 if x >= 0 else -4, 0), textcoords="offset points",
                    ha="left" if x >= 0 else "right", va="center", fontsize=7.5, color=INK2)
    ax.axvline(0, color=CRITICAL, lw=1.2, ls="--")
    ax.annotate("limit", xy=(0, 0), xycoords=("data", "axes fraction"), xytext=(-3, 3),
                textcoords="offset points", color=CRITICAL, fontsize=8, ha="right")
    ax.set_yticks(range(len(rows)), names, fontsize=8)
    ax.invert_yaxis()                                          # first check on top
    ax.set_xlim(-190, 190)                                     # room for the result labels beside ±100 %
    ax.set_xticks([-100, -50, 0, 50, 100])
    ax.set_xlabel("margin to the limit (% of the limit; > 0 meets it; clipped at ±100 %)")
    ax.grid(axis="y", visible=False)
    ax.set_title(f"{title}  (PASS green, FAIL red, INCOMPLETE amber)", fontsize=9)
    fig.tight_layout()
    return _save(fig, path)


def curves(path, panels: list[dict], *, title: str, xlabel: str, logx: bool = False, logy: bool = False,
           note: str = "") -> str:
    """Stacked panels sharing x. ``panels``: [{"ylabel", "series": {label: (x, y)}, "limits":
    [(y, label)], "marks": [(x, y, label)], "vlines": [(x, label)]}]."""
    plt = _plt()
    fig, axs = plt.subplots(len(panels), 1, figsize=(6.8, 1.0 + 2.3 * len(panels)), sharex=True, squeeze=False)
    for ax, p in zip(axs[:, 0], panels):
        series = p.get("series", {})
        for (lab, (x, y)), (c, _) in zip(series.items(), SERIES * 4):
            ax.plot(x, y, color=c, lw=1.6, label=lab)
        for y, lab in p.get("limits", []):
            limit_line(ax, y, lab, x=0.02)
        for x, lab in p.get("vlines", []):
            ax.axvline(x, color=MUTED, lw=1.0, ls=":")
            ax.annotate(lab, xy=(x, 0), xycoords=("data", "axes fraction"), xytext=(3, 3),
                        textcoords="offset points", color=INK2, fontsize=7.5, va="bottom")
        for x, y, lab in p.get("marks", []):
            ax.plot([x], [y], "o", color=INK, ms=5)
            ax.annotate(lab, xy=(x, y), xytext=(6, 4), textcoords="offset points", color=INK2, fontsize=7.5)
        if logx:
            ax.set_xscale("log")
        if logy:
            ax.set_yscale("log")
        ax.set_ylabel(p.get("ylabel", ""))
        if len(series) > 1:
            ax.legend(fontsize=7.5)
    axs[-1, 0].set_xlabel(xlabel)
    fig.suptitle(title, color=INK)
    if note:
        fig.text(0.01, 0.01, note, color=MUTED, fontsize=7.5)
    fig.tight_layout(rect=(0, 0.03 if note else 0, 1, 1))
    return _save(fig, path)


def cases(path, metrics: dict, *, title: str) -> str:
    """One row per metric: its value at every case (dots), the nominal (line) and the worst
    low and high cases labelled. ``metrics``: {name: {"values", "nominal", "max", "max_corner",
    "min", "min_corner"}}."""
    plt = _plt()
    fig, axs = plt.subplots(len(metrics), 1, figsize=(6.8, 0.6 + 1.35 * len(metrics)), squeeze=False)
    rng = np.random.default_rng(0)
    for ax, (name, m) in zip(axs[:, 0], metrics.items()):
        v = np.asarray(m["values"], float)
        ax.plot(v, rng.uniform(-0.25, 0.25, v.size), "o", color=SERIES[0][0], ms=3.5, alpha=0.7)
        if m.get("nominal") is not None:
            ax.axvline(m["nominal"], color=INK, lw=1.4)
            ax.annotate("nominal", xy=(m["nominal"], 0.42), xytext=(3, 0), textcoords="offset points",
                        fontsize=7.5, color=INK2, va="center")
        for k, ha in (("min", "left"), ("max", "right")):
            ax.annotate(f"{k} {m[k]:.4g}\n{m[k + '_corner']}", xy=(m[k], -0.3), xytext=(0, -2),
                        textcoords="offset points", ha=ha, va="top", fontsize=7, color=INK2)
        ax.set_ylim(-1.0, 0.6)
        ax.set_yticks([])
        ax.set_ylabel(name, rotation=0, ha="right", va="center")
        span = (v.max() - v.min()) or abs(v.max()) or 1.0
        ax.set_xlim(v.min() - 0.15 * span, v.max() + 0.15 * span)
    fig.suptitle(title, color=INK)
    fig.tight_layout()
    return _save(fig, path)


def bars(path, labels: list[str], values: list[float], *, title: str, xlabel: str, empty_text: str = "",
         limit: float | None = None, limit_label: str = "limit") -> str:
    """Horizontal bars, one per item, first on top; ``empty_text`` is drawn when there are none."""
    plt = _plt()
    fig, ax = plt.subplots(figsize=(6.8, 1.2 + 0.38 * max(1, len(labels))))
    if labels:
        ax.barh(range(len(labels)), values, color=SERIES[0][0], height=0.6)
        ax.set_yticks(range(len(labels)), labels, fontsize=8)
        ax.invert_yaxis()
        for i, v in enumerate(values):
            ax.annotate(f"{v:.3g}", xy=(v, i), xytext=(4, 0), textcoords="offset points", va="center",
                        fontsize=7.5, color=INK2)
        if limit is not None:
            ax.axvline(limit, color=CRITICAL, lw=1.2, ls="--")
            ax.annotate(limit_label, xy=(limit, 0), xycoords=("data", "axes fraction"), xytext=(3, 3),
                        textcoords="offset points", color=CRITICAL, fontsize=8)
    else:
        ax.text(0.5, 0.5, empty_text, transform=ax.transAxes, ha="center", va="center", color=MUTED)
        ax.set_yticks([])
    ax.grid(axis="y", visible=False)
    ax.set_xlabel(xlabel)
    ax.set_title(title)
    fig.tight_layout()
    return _save(fig, path)


def board_map(path, layers: dict, extent: tuple, *, title: str, label: str, marks: list | None = None,
              categorical: bool = False) -> str:
    """``layers``: {layer: (ny, nx) array, NaN off copper}; ``extent``: (x0, x1, y1, y0) mm.
    ``marks``: [(x, y, text)] on every layer, or (x, y, text, layer) on one (pads, peaks).
    ``categorical`` colours integer groups."""
    plt = _plt()
    n = len(layers)
    fig, axs = plt.subplots(1, n, figsize=(min(13.0, 4.6 * n + 1), 4.2), squeeze=False)
    finite = [a[np.isfinite(a)] for a in layers.values()]
    vmax = max((float(a.max()) for a in finite if a.size), default=1.0) or 1.0
    cmap = plt.get_cmap("tab10" if categorical else "viridis").copy()
    cmap.set_bad(SURFACE)
    im = None
    for ax, (layer, a) in zip(axs[0], layers.items()):
        im = ax.imshow(np.ma.masked_invalid(a), origin="upper", extent=extent, cmap=cmap,
                       vmin=0, vmax=9 if categorical else vmax, interpolation="nearest")
        for x, y, text, *on in marks or []:
            if on and on[0] != layer:
                continue
            ax.plot([x], [y], "+", color=INK, ms=7)
            ax.annotate(text, xy=(x, y), xytext=(3, 3), textcoords="offset points", fontsize=6.5, color=INK)
        ax.set_title(layer)
        ax.set_xlabel("mm")
        ax.grid(False)
    if not categorical and im is not None:
        fig.colorbar(im, ax=axs[0].tolist(), label=label, shrink=0.85)
    fig.suptitle(title + (f"  ({label})" if categorical else ""), color=INK)
    return _save(fig, path)
