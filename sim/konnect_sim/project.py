"""Where a KiCad project keeps its design records, simulation scripts and results, so every
project reads the same way:

    <project>/
      *.kicad_pro / _sch / _pcb
      design/                         the user-approved specification and part selection (PDF;
                                      their JSON, and the optimisation cycle's log, in design/records/)
      iteration1/sim/scripts/         the scripts that produced this iteration (re-runnable)
      iteration1/sim/inputs/          sourced inputs: fabricator stackup, values read from datasheets
      iteration1/sim/results/         raw outputs: check JSON, waveforms (CSV/NPZ), logs
      iteration1/sim/figures/         the charts (PNG)
      iteration1/sim/report_sources/  Markdown sources of written reports
      iteration1/reports/             the iteration's reports (PDF only); the last iteration's
                                      reports are the final ones
      iteration2/...                  the next simulate -> change pass, and so on

There is no project-level ``sim/`` or ``reports/``: everything a pass used or produced is in
its own iteration folder. A design that is simulated once has only ``iteration1``. A script
lives in ``iteration<N>/sim/scripts/`` and finds its project and iteration from there:

    from konnect_sim.project import Folders
    F = Folders.of_script(__file__).make()
    json.dump(rows, open(F.results / "steady_table.json", "w"))

The next pass starts with ``Folders.next(project).make().carry_over()``, which copies the
previous iteration's scripts and inputs so it can be changed without touching the old one.
"""
from __future__ import annotations

import re
import shutil
from dataclasses import dataclass
from pathlib import Path

_ITERATION = re.compile(r"(?:iteration|it)?(\d+)$")
_ITERATION_DIR = re.compile(r"iteration(\d+)")


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
    def of_script(cls, script: str | Path, iteration: int | str | None = None) -> "Folders":
        """For a script in ``<project>/iteration<N>/sim/scripts/``; the iteration is that
        folder's unless given. The older ``<project>/sim/`` and ``<project>/sim/scripts/``
        still resolve to the project (iteration 1 unless given)."""
        here = Path(script).resolve().parent
        if (here.name == "scripts" and here.parent.name == "sim"
                and _ITERATION_DIR.fullmatch(here.parents[1].name)):
            folder = int(_ITERATION_DIR.fullmatch(here.parents[1].name).group(1))
            return cls(here.parents[2], folder if iteration is None else iteration)
        root = here.parents[1] if here.name == "scripts" and here.parent.name == "sim" else here.parent
        return cls(root, 1 if iteration is None else iteration)

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

    @property
    def design(self) -> Path:
        return self.root / "design"

    @property
    def cycle_log(self) -> Path:
        """The optimisation cycle's history, across iterations."""
        return self.design / "records" / "cycle_log.json"

    # this iteration
    @property
    def iteration_dir(self) -> Path:
        return self.root / self.run

    @property
    def sim(self) -> Path:
        return self.iteration_dir / "sim"

    @property
    def scripts(self) -> Path:
        return self.sim / "scripts"

    @property
    def inputs(self) -> Path:
        return self.sim / "inputs"

    @property
    def results(self) -> Path:
        return self.sim / "results"

    @property
    def figures(self) -> Path:
        return self.sim / "figures"

    @property
    def report_sources(self) -> Path:
        return self.sim / "report_sources"

    @property
    def iteration_reports(self) -> Path:
        return self.iteration_dir / "reports"

    @property
    def reports(self) -> Path:
        """Where this iteration's reports go; the last iteration's are the final ones."""
        return self.iteration_reports

    def make(self) -> "Folders":
        for p in (self.scripts, self.inputs, self.results, self.figures, self.iteration_reports):
            p.mkdir(parents=True, exist_ok=True)
        return self

    def carry_over(self) -> "Folders":
        """Copy the previous iteration's scripts and inputs into this one (files already here
        are kept), so the new pass starts from what the last one ran."""
        if self.iteration > 1:
            prev = Folders(self.root, self.iteration - 1)
            for src, dst in ((prev.scripts, self.scripts), (prev.inputs, self.inputs)):
                if not src.is_dir():
                    continue
                for f in src.rglob("*"):
                    out = dst / f.relative_to(src)
                    if f.is_file() and "__pycache__" not in f.parts and not out.exists():
                        out.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(f, out)
        return self


def report_dirs(folder: Path, stem: str) -> tuple[Path, Path]:
    """Where a report written to ``folder`` keeps its charts and its data (JSON):
    ``iteration<N>/sim/figures`` and ``/results`` for ``iteration<N>/reports``;
    ``<stem>_figures`` and ``<stem>_data`` inside any other folder."""
    folder = Path(folder)
    if folder.name == "reports" and re.fullmatch(r"iteration\d+", folder.parent.name):
        return folder.parent / "sim" / "figures", folder.parent / "sim" / "results"
    return folder / f"{stem}_figures", folder / f"{stem}_data"
