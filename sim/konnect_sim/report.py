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
                 "| Check | Requirement | Result | Status | Method |",
                 "|---|---|---|---|---|"]
        for c in self.checks:
            lines.append(f"| {c.name} | {c.requirement} | {c.result} | **{c.status}** | {c.method} |")
        lines += ["", "## Evidence and assumptions", ""]
        for c in self.checks:
            lines.append(f"### {c.name} — {c.status}")
            if c.sources:
                lines.append("Sources: " + "; ".join(c.sources))
            if c.assumptions:
                lines.append("Assumptions: " + "; ".join(c.assumptions))
            if c.to_complete:
                lines.append("To complete: " + c.to_complete)
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
                                  "checks": [asdict(c) for c in self.checks],
                                  "notes": self.notes}, indent=2), encoding="utf-8")
        return md, js
