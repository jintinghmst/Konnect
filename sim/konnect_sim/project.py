"""Where a KiCad project keeps its design records, simulation scripts and results, so every
project reads the same way:

    <project>/
      *.kicad_pro / _sch / _pcb
      design/                    the user-approved specification and part selection (PDF;
                                 their JSON in design/records/)
      sim/                       the simulation scripts (re-runnable), shared by all iterations
      sim/inputs/                sourced inputs: fabricator stackup, values read from datasheets
      sim/report_sources/        Markdown sources of written reports
      sim/cycle_log.json         the optimisation cycle's history, across iterations
      reports/                   the final reports people read (PDF only)
      iteration1/sim/results/    raw outputs of iteration 1: check JSON, waveforms (CSV/NPZ), logs
      iteration1/sim/figures/    the charts of iteration 1 (PNG)
      iteration1/reports/        the report of iteration 1 (PDF only)
      iteration2/...             the next simulate -> change pass, and so on

A design that is simulated once has only ``iteration1``. A script finds its project from
its own location and takes the iteration number as an argument:

    from konnect_sim.project import Folders
    F = Folders.of_script(__file__, iteration=2).make()
    json.dump(rows, open(F.results / "steady_table.json", "w"))
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

_ITERATION = re.compile(r"(?:iteration|it)?(\d+)$")


def iteration_number(x: int | str) -> int:
    """1 from 1, "1", "it1" or "iteration1"."""
    m = _ITERATION.match(str(x).strip().lower())
    if not m or int(m.group(1)) < 1:
        raise ValueError(f"an iteration is a number from 1 (or 'iteration<N>'), not {x!r}")
    return int(m.group(1))


@dataclass(frozen=True)
class Folders:
    root: Path                 # the KiCad project folder
    iteration: int = 1         # one simulate -> change pass, numbered from 1

    def __post_init__(self):
        object.__setattr__(self, "root", Path(self.root))
        object.__setattr__(self, "iteration", iteration_number(self.iteration))

    @classmethod
    def of_script(cls, script: str | Path, iteration: int | str = 1) -> "Folders":
        """For a script in ``<project>/sim/`` (or the older ``<project>/sim/scripts/``)."""
        here = Path(script).resolve().parent
        root = here.parents[1] if here.name == "scripts" and here.parent.name == "sim" else here.parent
        return cls(root, iteration)

    @staticmethod
    def iterations(root: str | Path) -> list[int]:
        """The iteration numbers that exist in the project, in order."""
        found = []
        for p in Path(root).glob("iteration*"):
            m = re.fullmatch(r"iteration(\d+)", p.name)
            if p.is_dir() and m:
                found.append(int(m.group(1)))
        return sorted(found)

    @classmethod
    def latest(cls, root: str | Path) -> "Folders":
        """The newest iteration (iteration 1 if there is none yet)."""
        its = cls.iterations(root)
        return cls(root, its[-1] if its else 1)

    @classmethod
    def next(cls, root: str | Path) -> "Folders":
        """The iteration after the newest one (iteration 1 in a new project)."""
        its = cls.iterations(root)
        return cls(root, its[-1] + 1 if its else 1)

    @property
    def run(self) -> str:
        """The iteration's name, ``iteration<N>``; also its folder name."""
        return f"iteration{self.iteration}"

    # shared by all iterations
    @property
    def sim(self) -> Path:
        return self.root / "sim"

    @property
    def scripts(self) -> Path:
        return self.sim

    @property
    def inputs(self) -> Path:
        return self.sim / "inputs"

    @property
    def report_sources(self) -> Path:
        return self.sim / "report_sources"

    @property
    def cycle_log(self) -> Path:
        return self.sim / "cycle_log.json"

    @property
    def reports(self) -> Path:
        """Final reports (the engineering report, the cycle's final report)."""
        return self.root / "reports"

    @property
    def design(self) -> Path:
        return self.root / "design"

    # this iteration
    @property
    def iteration_dir(self) -> Path:
        return self.root / self.run

    @property
    def results(self) -> Path:
        return self.iteration_dir / "sim" / "results"

    @property
    def figures(self) -> Path:
        return self.iteration_dir / "sim" / "figures"

    @property
    def iteration_reports(self) -> Path:
        return self.iteration_dir / "reports"

    def make(self) -> "Folders":
        for p in (self.sim, self.reports, self.results, self.figures, self.iteration_reports):
            p.mkdir(parents=True, exist_ok=True)
        return self


def report_dirs(folder: Path, stem: str) -> tuple[Path, Path]:
    """Where a report written to ``folder`` keeps its charts and its data (JSON):
    ``iteration<N>/sim/figures`` and ``/results`` for ``iteration<N>/reports``;
    ``sim/figures/<stem>`` and ``sim/results/<stem>`` for the project's ``reports``;
    ``<stem>_figures`` and ``<stem>_data`` beside any other folder."""
    folder = Path(folder)
    if folder.name == "reports" and re.fullmatch(r"iteration\d+", folder.parent.name):
        return folder.parent / "sim" / "figures", folder.parent / "sim" / "results"
    if folder.name == "reports":
        return folder.parent / "sim" / "figures" / stem, folder.parent / "sim" / "results" / stem
    return folder / f"{stem}_figures", folder / f"{stem}_data"
