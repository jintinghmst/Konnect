"""Where a KiCad project keeps its simulation files, so every project reads the same way:

    <project>/
      sim/scripts/          the per-project scripts (re-runnable)
      sim/inputs/           sourced inputs: fabricator stackup, spec, values read from datasheets
      sim/results/<run>/    raw outputs of one run: check JSON, waveforms (CSV/NPZ), logs
      sim/figures/<run>/    the charts of that run (PNG)
      sim/results/cycle_log.json   the optimisation cycle's history, across runs
      reports/              the reports people read: <stem>.md / .json / .pdf (+ .docx)
      design/               the user-approved design records: specification, part selection

A script finds its project from its own location:

    from konnect_sim.project import Folders
    F = Folders.of_script(__file__, run="it2").make()
    json.dump(rows, open(F.results / "steady_table.json", "w"))
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Folders:
    root: Path                 # the KiCad project folder
    run: str = "latest"        # one evaluation: "baseline", "it1", ...

    @classmethod
    def of_script(cls, script: str | Path, run: str = "latest") -> "Folders":
        """For a script in ``<project>/sim/scripts/``."""
        return cls(Path(script).resolve().parents[2], run)

    @property
    def sim(self) -> Path:
        return self.root / "sim"

    @property
    def scripts(self) -> Path:
        return self.sim / "scripts"

    @property
    def inputs(self) -> Path:
        return self.sim / "inputs"

    @property
    def results(self) -> Path:
        return self.sim / "results" / self.run

    @property
    def figures(self) -> Path:
        return self.sim / "figures" / self.run

    @property
    def cycle_log(self) -> Path:
        return self.sim / "results" / "cycle_log.json"

    @property
    def reports(self) -> Path:
        return self.root / "reports"

    @property
    def design(self) -> Path:
        return self.root / "design"

    def make(self) -> "Folders":
        for p in (self.scripts, self.inputs, self.results, self.figures, self.reports, self.design):
            p.mkdir(parents=True, exist_ok=True)
        return self
