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


@dataclass
class Report:
    title: str
    design: str
    checks: list[Check] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

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

    def to_markdown(self) -> str:
        lines = [f"# {self.title}", "", f"Design: `{self.design}`  ", f"Date: {date.today()}  ",
                 f"**Overall verdict: {self.verdict}**", "",
                 "| Check | Requirement | Result | Meets | Status | Method |",
                 "|---|---|---|---|---|---|"]
        for c in self.checks:
            meets = {True: "yes", False: "**no**", None: "–"}[c.meets]
            tag = " (layout)" if c.layout else ""
            lines.append(f"| {c.name}{tag} | {c.requirement} | {c.result} | {meets} | "
                         f"**{c.status}** | {c.method} |")
        lines += ["", "## Evidence and assumptions", ""]
        for c in self.checks:
            lines.append(f"### {c.name} — {c.status}")
            if c.sources:
                lines.append("Sources: " + "; ".join(c.sources))
            if c.assumptions:
                lines.append("Assumptions: " + "; ".join(c.assumptions))
            if c.to_complete:
                lines.append("To complete: " + c.to_complete)
            if c.lever:
                lines.append("Lever: " + c.lever)
            lines.append("")
        if self.notes:
            lines += ["## Notes", ""] + [f"- {n}" for n in self.notes]
        return "\n".join(lines) + "\n"

    def write(self, folder: str | Path, stem: str) -> tuple[Path, Path]:
        folder = Path(folder)
        folder.mkdir(parents=True, exist_ok=True)
        md, js = folder / f"{stem}.md", folder / f"{stem}.json"
        md.write_text(self.to_markdown(), encoding="utf-8")
        js.write_text(json.dumps({"title": self.title, "design": self.design,
                                  "verdict": self.verdict,
                                  "checks": [asdict(c) | {"meets": c.meets, "margin": c.margin}
                                             for c in self.checks],
                                  "notes": self.notes}, indent=2), encoding="utf-8")
        return md, js
