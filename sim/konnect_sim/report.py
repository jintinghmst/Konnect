"""Spec checks with an explicit evidence trail.

A ``Check`` records what was compared, how the number was obtained, and what it
assumed. Status rules:

- PASS / FAIL only when every input the result depends on is sourced
  (design file, datasheet, user spec) and the method is stated.
- INCOMPLETE when any governing input is assumed, a model is missing, or the
  method cannot answer the question (e.g. loop stability without a controller
  model). The report says what would complete it.
"""
from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

PASS, FAIL, INCOMPLETE = "PASS", "FAIL", "INCOMPLETE"


@dataclass
class Check:
    name: str
    requirement: str          # human-readable limit, e.g. "<= 50 mVpp"
    result: str               # human-readable observed value
    status: str               # PASS / FAIL / INCOMPLETE
    method: str               # "closed-form", "SPICE transient", ...
    sources: list[str] = field(default_factory=list)      # where inputs came from
    assumptions: list[str] = field(default_factory=list)  # anything not sourced
    to_complete: str = ""     # what evidence would turn INCOMPLETE into PASS/FAIL
    # Machine-readable comparison, used by the optimisation cycle. ``status``
    # says how trustworthy the evidence is; ``meets`` says whether the number,
    # as modelled, is inside the limit. An INCOMPLETE check can still miss.
    value: float | None = None
    limit: float | None = None
    sense: str = "max"        # "max": value <= limit; "min": value >= limit
    layout: bool = False      # result depends on the board, not only the schematic
    lever: str = ""           # the design or layout change that moves the value
    # graphs of this result: [{"title", "path"}] (PNG), drawn by the tool or script that made it
    figures: list[dict] = field(default_factory=list)

    @property
    def meets(self) -> bool | None:
        if self.value is None or self.limit is None:
            return None
        return self.value <= self.limit if self.sense == "max" else self.value >= self.limit

    @property
    def margin(self) -> float | None:
        """Signed fractional margin to the limit; negative means it misses."""
        if self.value is None or self.limit is None or self.limit == 0:
            return None
        m = (self.limit - self.value) / abs(self.limit)
        return m if self.sense == "max" else -m


def judge(ok: bool, assumed: bool) -> str:
    """Status for a numeric comparison; an assumed input caps it at INCOMPLETE."""
    if assumed:
        return INCOMPLETE
    return PASS if ok else FAIL


@dataclass(frozen=True)
class Param:
    """A model input with its provenance. ``source`` names the datasheet page,
    measurement, spec file or design file the value came from; an empty source
    means the value is assumed."""
    value: float
    unit: str = ""
    source: str = ""
    name: str = ""

    @property
    def sourced(self) -> bool:
        return bool(self.source)

    def __float__(self) -> float:
        return float(self.value)


def val(x) -> float:
    """Plain number from a ``Param`` or a number."""
    return float(x.value) if isinstance(x, Param) else float(x)


def provenance(params: dict) -> tuple[list[str], list[str]]:
    """Split named inputs into (sources, assumptions) for a ``Check``.
    Plain numbers and unsourced ``Param`` values count as assumptions; ``None``
    entries are skipped."""
    sources, assumed = [], []
    for name, p in params.items():
        if p is None:
            continue
        if isinstance(p, Param) and p.sourced:
            if p.source not in sources:
                sources.append(p.source)
        else:
            assumed.append(name)
    return sources, assumed


def check(name: str, requirement: str, result: str, ok: bool | None, method: str,
          inputs: dict, *, to_complete: str = "", missing_model: str = "", **kw) -> Check:
    """Build a ``Check`` whose status follows from its inputs' provenance.

    ``ok`` None (the method cannot answer) or a ``missing_model`` gives
    INCOMPLETE; so does any assumed input. Otherwise PASS/FAIL from ``ok``.
    """
    sources, assumed = provenance(inputs)
    if missing_model:
        assumed = assumed + [missing_model]
    status = INCOMPLETE if ok is None or assumed else (PASS if ok else FAIL)
    return Check(name, requirement, result, status, method, sources, assumed,
                 to_complete, **kw)


@dataclass
class Report:
    title: str
    design: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    # (title, image path) pairs not tied to one check; drawn after the tests, paths
    # written relative to the report so the folder can be moved as a whole
    figures: list[tuple[str, str]] = field(default_factory=list)
    summary_figure: str = ""      # the chart of all checks against their limits (set by ``write``)

    def add(self, check: Check) -> Check:
        self.checks.append(check)
        return check

    @property
    def verdict(self) -> str:
        states = {c.status for c in self.checks}
        if FAIL in states:
            return FAIL
        if INCOMPLETE in states or not self.checks:
            return INCOMPLETE
        return PASS

    def all_figures(self) -> list[tuple[str, str]]:
        """Every figure once, in report order: the summary chart, each check's, then the rest."""
        out, seen = [], set()
        summary = [(SUMMARY_TITLE, self.summary_figure)] if self.summary_figure else []
        for title, path in summary + [(f["title"], f["path"]) for c in self.checks for f in c.figures] + self.figures:
            if path not in seen:
                seen.add(path)
                out.append((title, path))
        return out

    @property
    def ungraphed(self) -> list[str]:
        """Checks with no graph of their own (the summary chart still shows the numeric ones)."""
        return [c.name for c in self.checks if not c.figures]

    def to_markdown(self) -> str:
        """Summary first (verdict, table of checks, the chart of checks against their limits,
        general notes), then one section per test with its method, inputs and graphs."""
        def cell(text) -> str:                      # a "|" in a value must not split the row
            return str(text).replace("|", "\\|").replace("\n", " ")

        counts = {s: sum(c.status == s for c in self.checks) for s in (PASS, FAIL, INCOMPLETE)}
        lines = [f"# {self.title}", "", f"Design: `{self.design}`  ", f"Date: {date.today()}", "",
                 "## Summary", "", f"**Overall verdict: {self.verdict}**  ",
                 f"{len(self.checks)} checks: {counts[PASS]} **PASS**, {counts[FAIL]} **FAIL**, "
                 f"{counts[INCOMPLETE]} **INCOMPLETE**", "",
                 "| # | Check | Requirement | Result | Meets | Status |", "|---|---|---|---|---|---|"]
        for i, c in enumerate(self.checks, 1):
            meets = {True: "yes", False: "**no**", None: "–"}[c.meets]
            tag = " (layout)" if c.layout else ""
            lines.append(f"| {i} | {cell(c.name)}{tag} | {cell(c.requirement)} | {cell(c.result)} | {meets} | "
                         f"**{c.status}** |")
        lines.append("")
        shown: dict[str, tuple[int, int]] = {}          # figure path -> (figure number, test number)
        if self.summary_figure:
            lines += [f"![{SUMMARY_TITLE}]({self.summary_figure})", ""]
            shown[self.summary_figure] = (1, 0)
        if self.notes:
            lines += ["### General notes and assumptions", ""] + [f"- {n}" for n in self.notes] + [""]
        if self.ungraphed:
            lines += [f"Checks without a graph of their own: {'; '.join(self.ungraphed)}.", ""]

        lines += ["## Results by test", ""]
        for i, c in enumerate(self.checks, 1):
            lines += [f"### {i}. {c.name}: **{c.status}**", ""]
            meets = {True: "meets the limit", False: "**misses the limit**", None: "no numeric comparison"}[c.meets]
            for label, text in (("Requirement", c.requirement), ("Result", f"{c.result} ({meets})"),
                                ("Method", c.method), ("Sources", "; ".join(c.sources)),
                                ("Assumptions", "; ".join(c.assumptions) or "none"),
                                ("To complete", c.to_complete), ("Lever", c.lever),
                                ("Depends on the layout", "yes" if c.layout else "")):
                if text:
                    lines.append(f"- **{label}:** {text}")
            lines.append("")
            for f in c.figures:
                if f["path"] in shown:
                    n, t = shown[f["path"]]
                    where = f"shown under test {t}" if t else "in the summary"
                    lines += [f"Graph: Figure {n}, {where}.", ""]
                else:
                    shown[f["path"]] = (len(shown) + 1, i)
                    lines += [f"![{f['title']}]({f['path']})", ""]
        rest = [(t, p) for t, p in self.figures if p not in shown]
        if rest:
            lines += ["## Further figures", ""]
            for title, path in rest:
                shown[path] = (len(shown) + 1, 0)
                lines += [f"![{title}]({path})", ""]
        return "\n".join(lines) + "\n"

    def _relative_figures(self, folder: Path) -> None:
        def rel(path) -> str:
            p = Path(path)
            try:
                p = Path(os.path.relpath(p.resolve(), folder.resolve()))
            except ValueError:
                pass                              # another drive: keep as given
            return p.as_posix()

        self.figures = [(title, rel(path)) for title, path in self.figures]
        self.summary_figure = rel(self.summary_figure) if self.summary_figure else ""
        for c in self.checks:
            c.figures = [f | {"path": rel(f["path"])} for f in c.figures]

    @staticmethod
    def default_figures_dir(folder: Path, stem: str) -> Path:
        """``<project>/sim/figures/<stem>`` for a report in ``<project>/reports``, else
        ``<folder>/<stem>_figures``."""
        return folder.parent / "sim" / "figures" / stem if folder.name == "reports" else folder / f"{stem}_figures"

    @staticmethod
    def default_data_dir(folder: Path, stem: str) -> Path:
        """``<project>/sim/results/<stem>`` for a report in ``<project>/reports``, else
        ``<folder>/<stem>_data``."""
        return folder.parent / "sim" / "results" / stem if folder.name == "reports" else folder / f"{stem}_data"

    def to_json(self) -> dict:
        """The checks as data (figure paths absolute), for the optimisation cycle and scripts."""
        def absolute(path) -> str:
            return str(Path(path).resolve())

        return {"title": self.title, "design": self.design, "verdict": self.verdict,
                "checks": [asdict(c) | {"meets": c.meets, "margin": c.margin,
                                        "figures": [f | {"path": absolute(f["path"])} for f in c.figures]}
                           for c in self.checks],
                "notes": self.notes, "summary_figure": absolute(self.summary_figure) if self.summary_figure else "",
                "figures": [{"title": t, "path": absolute(p)} for t, p in self.figures],
                "ungraphed": self.ungraphed}

    def write(self, folder: str | Path, stem: str, documents=("pdf",), figures_dir: str | Path | None = None,
              data_dir: str | Path | None = None) -> tuple[Path, Path]:
        """Write the report as ``<folder>/<stem>.pdf``, the one document people read. ``documents``
        may add "docx" (Word) or "md" (keep the Markdown source); with none, the Markdown is kept.
        The chart of all checks against their limits is drawn into ``figures_dir`` and the checks
        as data go to ``<data_dir>/<stem>.json`` (defaults: ``default_figures_dir``,
        ``default_data_dir``), so the reports folder holds only the documents.
        Returns (the first document, the JSON)."""
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        documents = list(documents) or ["md"]
        from . import document, figures
        fdir = Path(figures_dir) if figures_dir else self.default_figures_dir(folder, stem)
        self.summary_figure = figures.checks_vs_limits(fdir / f"{stem}_checks_vs_limits.png", self.checks) or ""
        self.figures = [(t, p) for t, p in self.figures if t != SUMMARY_TITLE]
        ddir = Path(data_dir) if data_dir else self.default_data_dir(folder, stem)
        ddir.mkdir(parents=True, exist_ok=True)
        js = ddir / f"{stem}.json"
        js.write_text(json.dumps(self.to_json(), indent=2), encoding="utf-8")
        self._relative_figures(folder)
        md = folder / f"{stem}.md"
        md.write_text(self.to_markdown(), encoding="utf-8")
        out = document.render(md, [f for f in documents if f != "md"])
        if "md" in documents:
            out = {"md": str(md)} | out
        else:
            md.unlink()
        return Path(out[documents[0]]), js


SUMMARY_TITLE = "Checks against their limits"
