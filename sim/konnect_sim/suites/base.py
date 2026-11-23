"""Shared machinery of the standard verification suites.

A suite is a fixed list of ``Slot``s. Every slot always produces exactly one check, named
``<id> <name>``, and always the same charts, in the same order: a slot that cannot run (an
input is missing, a simulation fails) still appears, as INCOMPLETE with the reason, and each
chart it could not draw is replaced by a placeholder that says why. Two runs of one suite on
the same circuit class therefore list the same checks and the same charts; only the design's
values, and how well they are sourced, differ.
"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from .. import figures
from ..report import FAIL, INCOMPLETE, PASS, Check, Param


class Missing(Exception):
    """A slot cannot run without these inputs."""

    def __init__(self, names: list[str]):
        super().__init__(", ".join(names))
        self.names = names


def need(**values):
    """Raise ``Missing`` naming every argument that is None."""
    gone = [k for k, v in values.items() if v is None]
    if gone:
        raise Missing(gone)


def param(entry, unit: str = "", name: str = "") -> Param | None:
    """A ``Param`` from an inputs-file entry ``{value, unit, source}`` (or a bare number, which
    counts as assumed); None when the entry is null or has no value."""
    if entry is None:
        return None
    if isinstance(entry, (int, float)):
        return Param(float(entry), unit, "", name)
    if entry.get("value") is None:
        return None
    return Param(float(entry["value"]), entry.get("unit") or unit, entry.get("source") or "", name)


def or_default(p: Param | None, value: float, unit: str, name: str) -> Param:
    """``p``, or the suite's fixed default as an assumed ``Param`` (empty source)."""
    return p if p is not None else Param(value, unit, "", f"{name} (suite default, assumed)")


@dataclass
class Slot:
    id: str                         # "R-03" (a requirement) or "S-01" (supplementary)
    name: str                       # "Output ripple"
    method: str                     # how it is answered, shown when the slot cannot run
    charts: list[str]               # the fixed chart titles, in order
    run: Callable                   # ctx -> (checks, {chart title: png path})
    requirement: str = ""           # shown when the slot cannot run
    layout: bool = False

    @property
    def label(self) -> str:
        return f"{self.id} {self.name}"


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", text.lower()).strip("_")


def combine(label: str, subs: list[Check], *, requirement: str = "", method: str = "",
            layout: bool = False, lever: str = "") -> Check:
    """One check from a slot's sub-checks: FAIL if any fails, INCOMPLETE if any is
    INCOMPLETE, else PASS; the value and limit are those of the sub-check with the worst
    margin, so the summary chart and the optimisation cycle see the binding case."""
    states = {c.status for c in subs}
    status = FAIL if FAIL in states else INCOMPLETE if (INCOMPLETE in states or not subs) else PASS
    srcs, assumed, todo, methods = [], [], [], []
    for c in subs:
        srcs += [s for s in c.sources if s not in srcs]
        assumed += [a for a in c.assumptions if a not in assumed]
        if c.to_complete and c.to_complete not in todo:
            todo.append(c.to_complete)
        if c.method and c.method not in methods:
            methods.append(c.method)
    ranked = [c for c in subs if c.margin is not None]
    worst = min(ranked, key=lambda c: c.margin) if ranked else None
    single = len(subs) == 1
    return Check(
        name=label,
        requirement=requirement or "; ".join(dict.fromkeys(c.requirement for c in subs)),
        result=(subs[0].result if single else "; ".join(f"{c.name}: {c.result}" for c in subs)),
        status=status, method=method or "; ".join(methods), sources=srcs, assumptions=assumed,
        to_complete=" ".join(todo), value=worst.value if worst else (subs[0].value if single else None),
        limit=worst.limit if worst else (subs[0].limit if single else None),
        sense=worst.sense if worst else "max", layout=layout or any(c.layout for c in subs),
        lever=lever or next((c.lever for c in subs if c.lever), ""))


def run_slots(slots: list[Slot], ctx, fig_dir: Path) -> list[Check]:
    """Run every slot in order; see the module docstring for what never changes."""
    out = []
    for slot in slots:
        reason, subs, charts = "", [], {}
        try:
            subs, charts = slot.run(ctx)
        except Missing as m:
            reason = "needs " + ", ".join(m.names)
        except Exception as e:                           # a failed simulation is reported, never dropped
            reason = f"simulation failed: {type(e).__name__}: {e}"
        if reason:
            check = Check(slot.label, slot.requirement, "not simulated", INCOMPLETE, slot.method,
                          to_complete=reason, layout=slot.layout)
        else:
            check = combine(slot.label, subs, layout=slot.layout)
        check.figures = []
        for title in slot.charts:
            path = charts.get(title)
            if not path or not Path(path).is_file():
                path = figures.placeholder(fig_dir / f"{_slug(slot.id)}_{_slug(title)}.png", title,
                                           reason or "this case did not produce the chart")
            check.figures.append({"title": title, "path": str(path)})
        out.append(check)
    return out


def file_hash(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()[:12] if Path(path).is_file() else "none"


def load_json(path: Path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8")) if Path(path).is_file() else {}


@dataclass
class Header:
    """What identifies a run, for comparing two runs: suite, version, inputs hash."""
    suite: str
    version: int
    inputs_file: str
    inputs_hash: str
    settings: dict = field(default_factory=dict)

    def notes(self) -> list[str]:
        return [f"Standard {self.suite} suite v{self.version}; inputs {Path(self.inputs_file).name} "
                f"(sha256 {self.inputs_hash}). Every {self.suite} design is checked with the same checks, "
                f"operating points, solver settings and charts; a check that cannot be completed stays "
                f"in the report as INCOMPLETE.",
                "Suite settings: " + ", ".join(f"{k} {v}" for k, v in self.settings.items())]
