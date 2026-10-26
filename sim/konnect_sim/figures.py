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
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

SURFACE, INK, INK2, MUTED, GRID, AXIS = "#fcfcfb", "#0b0b0b", "#52514e", "#898781", "#e1e0d9", "#c3c2b7"
CRITICAL = "#d03b3b"
SERIES = [("#2a78d6", "o"), ("#eb6834", "s"), ("#1baf7a", "^"), ("#6250d6", "D")]   # colour, marker


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
