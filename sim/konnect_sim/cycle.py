"""Bookkeeping and stop rules for the simulate -> review -> change -> re-simulate cycle.

Each iteration records what changed, fingerprints of the design files it
simulated, every check (value, limit, whether it meets, evidence status), the
objective metrics being minimised, and the DRC/ERC counts. ``decide()`` turns
that history into one of:

- ``met``      every constraint meets its limit, checks are clean, and the
               objectives stopped improving (or ``stop_when_met`` is set);
- ``continue`` keep going: something misses, or objectives still improve;
- ``stalled``  the worst constraint margin and the objectives have not
               improved for ``patience`` iterations: stop and report the best;
- ``limit``    the iteration budget is spent;
- ``revert``   the latest change made things worse (new DRC/ERC errors, a
               constraint that met now misses, or a worse worst-margin):
               undo it before trying something else.

The log lives next to the script as ``<stem>_cycle.json`` so the history
survives between runs and can be reviewed.
"""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


def fingerprint(*paths: str | Path) -> dict[str, str]:
    """Short SHA-256 of each file, so a report names the exact design it checked."""
    out = {}
    for p in paths:
        p = Path(p)
        out[p.name] = hashlib.sha256(p.read_bytes()).hexdigest()[:12] if p.exists() else "missing"
    return out


@dataclass
class Iteration:
    index: int
    change: str
    files: dict[str, str]
    checks: list[dict]
    objectives: dict[str, float]
    drc_errors: int | None
    erc_errors: int | None
    verdict: str
    time: str = field(default_factory=lambda: datetime.now().isoformat(timespec="seconds"))

    @property
    def constraints(self) -> list[dict]:
        return [c for c in self.checks if c.get("meets") is not None]

    @property
    def misses(self) -> list[str]:
        return [c["name"] for c in self.constraints if not c["meets"]]

    @property
    def worst_margin(self) -> float:
        m = [c["margin"] for c in self.constraints if c.get("margin") is not None]
        return min(m) if m else 0.0

    @property
    def clean(self) -> bool:
        return not self.drc_errors and not self.erc_errors


@dataclass
class CycleLog:
    path: Path
    max_iterations: int = 6
    patience: int = 2
    min_gain: float = 0.05            # objective improvement that counts (5 %)
    stop_when_met: bool = False
    iterations: list[Iteration] = field(default_factory=list)

    @classmethod
    def open(cls, path: str | Path, **kw) -> "CycleLog":
        path = Path(path)
        log = cls(path, **kw)
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            log.iterations = [Iteration(**it) for it in data.get("iterations", [])]
        return log

    def record(self, report, change: str, files: dict[str, str], objectives: dict[str, float],
               drc_errors: int | None = None, erc_errors: int | None = None) -> Iteration:
        checks = [{"name": c.name, "value": c.value, "limit": c.limit, "sense": c.sense,
                   "meets": c.meets, "margin": c.margin, "status": c.status,
                   "layout": c.layout, "lever": c.lever} for c in report.checks]
        it = Iteration(len(self.iterations), change, files, checks, objectives,
                       drc_errors, erc_errors, report.verdict)
        self.iterations.append(it)
        self.save()
        return it

    def save(self):
        self.path.write_text(json.dumps({
            "max_iterations": self.max_iterations, "patience": self.patience,
            "min_gain": self.min_gain,
            "iterations": [it.__dict__ for it in self.iterations]}, indent=2), encoding="utf-8")

    # ── decisions ──────────────────────────────────────────────────────────

    @staticmethod
    def _gain(old: dict, new: dict) -> float:
        """Best relative reduction across shared objectives (all are minimised)."""
        gains = [(old[k] - new[k]) / abs(old[k]) for k in new if k in old and old[k]]
        return max(gains, default=0.0)

    def best(self) -> Iteration | None:
        good = [it for it in self.iterations if it.clean] or self.iterations
        if not good:
            return None
        return max(good, key=lambda it: (not it.misses, it.worst_margin,
                                         -sum(it.objectives.values())))

    def decide(self) -> tuple[str, str]:
        its = self.iterations
        if not its:
            return "continue", "no iterations recorded yet"
        cur = its[-1]
        if len(its) >= 2:
            prev = its[-2]
            newly_missed = [n for n in cur.misses if n not in prev.misses]
            if (cur.drc_errors or 0) > (prev.drc_errors or 0) or (cur.erc_errors or 0) > (prev.erc_errors or 0):
                return "revert", "the last change added DRC/ERC errors"
            if newly_missed:
                return "revert", "the last change broke: " + ", ".join(newly_missed)
            if cur.worst_margin < prev.worst_margin - 1e-9 and cur.misses:
                return "revert", "the last change reduced the worst margin"
        if not cur.misses and cur.clean:
            if self.stop_when_met:
                return "met", "all constraints meet"
            # Objectives only count as exhausted after a change made *while*
            # constraints already met fails to gain; the iteration that first
            # met the constraints may have targeted something else entirely.
            first_met = next(i for i, it in enumerate(its)
                             if all(not x.misses and x.clean for x in its[i:]))
            if len(its) - 1 > first_met and \
                    self._gain(its[-2].objectives, cur.objectives) < self.min_gain:
                return "met", "constraints meet and objectives stopped improving"
            if len(its) - 1 == first_met and len(its) < self.max_iterations:
                return "continue", "constraints meet; try an objective improvement"
        if len(its) >= self.max_iterations:
            return "limit", f"iteration budget of {self.max_iterations} spent"
        if len(its) > self.patience:
            window = its[-(self.patience + 1):]
            base = window[0]
            if all(it.worst_margin <= base.worst_margin + 1e-9 and
                   self._gain(base.objectives, it.objectives) < self.min_gain for it in window[1:]):
                return "stalled", f"no improvement in {self.patience} iterations"
        what = ", ".join(cur.misses) if cur.misses else "objectives still improving"
        return "continue", what

    def summary_markdown(self) -> str:
        names = sorted({k for it in self.iterations for k in it.objectives})
        head = "".join(f" {n} |" for n in names)
        lines = ["| # | Change | Misses | Worst margin |" + head + " DRC | Verdict |",
                 "|---|---|---|---|" + "---|" * len(names) + "---|---|"]
        for it in self.iterations:
            objs = "".join(f" {it.objectives.get(n, float('nan')):.4g} |" for n in names)
            lines.append(f"| {it.index} | {it.change} | {', '.join(it.misses) or 'none'} | "
                         f"{it.worst_margin:+.1%} |{objs} {it.drc_errors} | {it.verdict} |")
        state, why = self.decide()
        b = self.best()
        lines += ["", f"**Decision: {state}** — {why}."]
        if b is not None:
            lines.append(f"Best iteration so far: #{b.index} ({b.change}).")
        return "\n".join(lines) + "\n"
