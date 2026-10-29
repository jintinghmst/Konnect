"""Design records: the specification and the part selection, each approved by the user
before the design moves on.

A PCB design runs through stages, and the user decides at the end of each one (the
``kicad-design-flow`` skill):

    1. specification   requirements with test condition and pass criterion  -> user accepts
    2. part selection  the chosen parts, why, and which requirements they serve -> user accepts
    3. schematic, layout, verification (the other skills)

Each record is kept in ``<project>/design/`` as JSON (the source of truth) and rendered as
Markdown, PDF and Word for people to read:

    design/01_specification.json / .md / .pdf / .docx
    design/02_part_selection.json / .md / .pdf / .docx

Rules the functions enforce, so the gates hold whatever the assistant does:

- A record is written as a *draft*. Only ``approve`` makes it *accepted*, and it needs the
  name of the approver and their statement.
- Changing an accepted record starts a new draft revision; approval does not carry over.
- The part selection can be written only against an accepted specification, and every
  requirement it cites must exist there. Revising the specification turns an accepted
  part selection back into a draft that names the revision it was based on.
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import date
from pathlib import Path

from . import document
from .project import Folders

DRAFT, ACCEPTED = "draft", "accepted"
SPEC, PARTS = "specification", "part_selection"
STEMS = {SPEC: "01_specification", PARTS: "02_part_selection"}
ORIGINS = ("user", "assumed", "standard", "datasheet")     # who set a requirement's limit


@dataclass
class Requirement:
    id: str                   # "R-01"
    category: str             # "Output ripple"
    parameter: str            # what is measured: "Vout peak-to-peak ripple"
    condition: str            # test condition: "Vin 9-36 V, Iout 0-5 A, 20 MHz bandwidth"
    criterion: str            # proposed pass criterion: "<= 50 mVpp"
    origin: str = "assumed"   # user / assumed (proposed by the assistant) / standard: ... / datasheet: ...
    rationale: str = ""       # why this limit
    verification: str = ""    # simulation (tool or script) and bench test that will check it


@dataclass
class PartChoice:
    role: str                 # "Buck converter IC", "Output inductor"
    part: str                 # manufacturer part number
    reasons: list[str]        # why this part, each reason tied to a requirement or constraint
    requirements: list[str]   # ids of the requirements it serves
    manufacturer: str = ""
    key_values: str = ""      # "4.7 uH, Isat 10 A, DCR 9 mOhm" with the datasheet as source
    alternatives: list[str] = field(default_factory=list)   # "TPS54560: rejected because ..."
    datasheet: str = ""       # document number / URL
    sourcing: str = ""        # "LCSC C12345, basic part, 3k in stock (date)"
    references: str = ""      # planned designators, "U1" or "C10-C13"
    risks: str = ""           # open points to settle before or during the schematic


def _check_origin(r: Requirement) -> None:
    head = r.origin.split(":", 1)[0].strip().lower()
    if head not in ORIGINS:
        raise ValueError(f"{r.id}: origin must be one of {list(ORIGINS)} (standard/datasheet as "
                         f"'standard: <document>'), not {r.origin!r}")


def _path(project: str | Path, kind: str, suffix: str = ".json") -> Path:
    return Folders(Path(project)).design / f"{STEMS[kind]}{suffix}"


def load(project: str | Path, kind: str) -> dict | None:
    p = _path(project, kind)
    return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None


def _cell(text) -> str:
    return str(text).replace("|", "\\|").replace("\n", " ") or "–"


def _header(rec: dict, heading: str) -> list[str]:
    status = rec["status"]
    if status == ACCEPTED:
        a = rec["approvals"][-1]
        state = f"**ACCEPTED** by {a['by']} on {a['date']}"
    else:
        state = "**DRAFT, awaiting the user's approval**"
    return [f"# {rec['title']}: {heading}", "", f"Project: `{rec['project']}`  ",
            f"Revision {rec['revision']}, {rec['date']}  ", f"Status: {state}", ""]


def _footer(rec: dict, ask: str) -> list[str]:
    lines = []
    if rec.get("notes"):
        lines += ["## Notes", ""] + [f"- {n}" for n in rec["notes"]] + [""]
    lines += ["## Revision history", "", "| Rev | Date | Change |", "|---|---|---|"]
    lines += [f"| {h['revision']} | {h['date']} | {_cell(h['change'])} |" for h in rec["history"]]
    lines += ["", "## Approval", ""]
    if rec["approvals"]:
        lines += ["| Rev | Date | By | Statement |", "|---|---|---|---|"]
        lines += [f"| {a['revision']} | {a['date']} | {_cell(a['by'])} | {_cell(a['statement'])} |"
                  for a in rec["approvals"]]
        lines.append("")
    if rec["status"] != ACCEPTED:
        lines.append(ask)
    return lines


def spec_markdown(rec: dict) -> str:
    lines = _header(rec, "Design Specification")
    if rec.get("brief"):
        lines += ["## Design brief", "", rec["brief"], ""]
    lines += ["## Requirements and test criteria", "",
              "Origin: **user** = given by the user; **assumed** = proposed by the assistant, the user "
              "accepts or changes it; **standard** / **datasheet** = taken from the named document.", "",
              "| ID | Category | Parameter | Test condition | Pass criterion | Origin |",
              "|---|---|---|---|---|---|"]
    for r in rec["requirements"]:
        lines.append(f"| {r['id']} | {_cell(r['category'])} | {_cell(r['parameter'])} | {_cell(r['condition'])} | "
                     f"**{_cell(r['criterion'])}** | {_cell(r['origin'])} |")
    lines += ["", "## Verification plan and rationale", "",
              "| ID | Verification (simulation, bench) | Rationale |", "|---|---|---|"]
    for r in rec["requirements"]:
        lines.append(f"| {r['id']} | {_cell(r['verification'])} | {_cell(r['rationale'])} |")
    lines.append("")
    lines += _footer(rec, "To continue, the user accepts this specification as it stands, or lists requirements "
                          "to add, change or remove; a change produces the next revision.")
    return "\n".join(lines) + "\n"


def parts_markdown(rec: dict, spec: dict) -> str:
    lines = _header(rec, "Part Selection")
    lines += [f"Based on specification revision {rec['spec_revision']}.", ""]
    lines += ["## Selected parts", "", "| Role | Part | Manufacturer | Key values | Serves |", "|---|---|---|---|---|"]
    for p in rec["parts"]:
        lines.append(f"| {_cell(p['role'])} | {_cell(p['part'])} | {_cell(p['manufacturer'])} | "
                     f"{_cell(p['key_values'])} | {_cell(', '.join(p['requirements']))} |")
    lines += ["", "## Reasons for each choice", ""]
    for p in rec["parts"]:
        lines += [f"### {p['role']}: {p['part']}" + (f" ({p['references']})" if p.get("references") else ""), ""]
        lines += [f"- {r}" for r in p["reasons"]]
        for label, key in (("Alternatives considered", "alternatives"), ("Datasheet", "datasheet"),
                           ("Sourcing", "sourcing"), ("Risks and open points", "risks")):
            v = p.get(key)
            if isinstance(v, list):
                v = "; ".join(v)
            if v:
                lines.append(f"- **{label}:** {v}")
        lines.append("")
    lines += ["## Requirement coverage", "", "| ID | Category | Addressed by |", "|---|---|---|"]
    by = {r["id"]: [p["role"] for p in rec["parts"] if r["id"] in p["requirements"]] for r in spec["requirements"]}
    for r in spec["requirements"]:
        lines.append(f"| {r['id']} | {_cell(r['category'])} | "
                     f"{_cell(', '.join(by[r['id']]) or 'not by a part choice (layout or verification)')} |")
    lines.append("")
    lines += _footer(rec, "To continue, the user accepts this part selection, or names the parts to change and why; "
                          "a change produces the next revision.")
    return "\n".join(lines) + "\n"


def _render(project: str | Path, kind: str, rec: dict, documents) -> dict:
    md = _path(project, kind, ".md")
    md.parent.mkdir(parents=True, exist_ok=True)
    _path(project, kind).write_text(json.dumps(rec, indent=2), encoding="utf-8")
    text = spec_markdown(rec) if kind == SPEC else parts_markdown(rec, load(project, SPEC))
    md.write_text(text, encoding="utf-8")
    out = {"json": str(_path(project, kind)), "markdown": str(md)}
    if documents:
        out |= document.render(md, documents)
    return out


def _content(rec: dict, keys) -> dict:
    return {k: rec.get(k) for k in keys}


def _revise(old: dict | None, new: dict, keys, change: str) -> dict:
    """Keep the old revision when nothing changed; otherwise the next draft revision."""
    today = str(date.today())
    if old and _content(old, keys) == _content(new, keys):
        return old
    rev = old["revision"] + 1 if old else 1
    history = (old["history"] if old else []) + [{"revision": rev, "date": today,
                                                  "change": change or ("first issue" if rev == 1 else "revised")}]
    return new | {"revision": rev, "date": today, "status": DRAFT, "history": history,
                  "approvals": old["approvals"] if old else []}


SPEC_KEYS = ("title", "brief", "requirements", "notes")
PARTS_KEYS = ("title", "parts", "notes", "spec_revision")


def write_spec(project: str | Path, title: str, requirements: list[Requirement], brief: str = "",
               notes: list[str] | None = None, change: str = "", documents=("pdf", "docx")) -> dict:
    ids = [r.id for r in requirements]
    if not requirements:
        raise ValueError("a specification needs at least one requirement")
    if len(set(ids)) != len(ids):
        raise ValueError(f"requirement ids must be unique: {ids}")
    for r in requirements:
        _check_origin(r)
        if not (r.condition.strip() and r.criterion.strip()):
            raise ValueError(f"{r.id}: every requirement needs a test condition and a pass criterion")
    old = load(project, SPEC)
    rec = _revise(old, {"kind": SPEC, "project": str(Path(project)), "title": title, "brief": brief,
                        "requirements": [asdict(r) for r in requirements], "notes": list(notes or [])},
                  SPEC_KEYS, change)
    out = _render(project, SPEC, rec, documents)
    parts = load(project, PARTS)
    if parts and parts["spec_revision"] != rec["revision"] and parts["status"] == ACCEPTED:
        parts["status"] = DRAFT
        parts["history"].append({"revision": parts["revision"], "date": str(date.today()),
                                 "change": f"specification revised to revision {rec['revision']}; "
                                           f"re-check the parts against it"})
        _render(project, PARTS, parts, documents)
        out["part_selection_reopened"] = True
    return out | {"revision": rec["revision"], "status": rec["status"]}


def write_parts(project: str | Path, title: str, parts: list[PartChoice], notes: list[str] | None = None,
                change: str = "", documents=("pdf", "docx")) -> dict:
    spec = load(project, SPEC)
    if not spec or spec["status"] != ACCEPTED:
        raise ValueError("the specification must be accepted by the user before parts are selected "
                         "(design/01_specification.json)")
    if not parts:
        raise ValueError("a part selection needs at least one part")
    known = {r["id"] for r in spec["requirements"]}
    for p in parts:
        if not p.reasons:
            raise ValueError(f"{p.role} ({p.part}): give the reasons for choosing it")
        unknown = sorted(set(p.requirements) - known)
        if unknown:
            raise ValueError(f"{p.role} ({p.part}) cites requirements not in the specification: {unknown}")
    old = load(project, PARTS)
    rec = _revise(old, {"kind": PARTS, "project": str(Path(project)), "title": title,
                        "spec_revision": spec["revision"], "parts": [asdict(p) for p in parts],
                        "notes": list(notes or [])}, PARTS_KEYS, change)
    out = _render(project, PARTS, rec, documents)
    uncovered = [r["id"] for r in spec["requirements"] if not any(r["id"] in p.requirements for p in parts)]
    return out | {"revision": rec["revision"], "status": rec["status"], "requirements_not_cited": uncovered}


def approve(project: str | Path, kind: str, approved_by: str, statement: str, documents=("pdf", "docx")) -> dict:
    """Record the user's acceptance of the current revision of a record."""
    if kind not in STEMS:
        raise ValueError(f"record must be one of {list(STEMS)}")
    if not approved_by.strip() or not statement.strip():
        raise ValueError("approval needs the approver and their statement (the user's own words)")
    rec = load(project, kind)
    if rec is None:
        raise ValueError(f"no {kind} has been written yet")
    if kind == PARTS:
        spec = load(project, SPEC)
        if not spec or spec["status"] != ACCEPTED or spec["revision"] != rec["spec_revision"]:
            raise ValueError("the part selection was written against specification revision "
                             f"{rec['spec_revision']}, which is not the accepted current one; rewrite it first")
    if rec["status"] == ACCEPTED:
        return {"status": ACCEPTED, "revision": rec["revision"], "already": True}
    rec["status"] = ACCEPTED
    rec["approvals"].append({"revision": rec["revision"], "date": str(date.today()), "by": approved_by,
                             "statement": statement})
    return _render(project, kind, rec, documents) | {"status": ACCEPTED, "revision": rec["revision"]}


def status(project: str | Path) -> dict:
    """Where the design stands and what the next step is."""
    spec, parts = load(project, SPEC), load(project, PARTS)

    def brief(r):
        return None if r is None else {"revision": r["revision"], "status": r["status"],
                                       "json": str(_path(project, r["kind"]))}

    if spec is None:
        nxt = "write the specification (stage 1), then wait for the user to accept it"
    elif spec["status"] != ACCEPTED:
        nxt = "the specification is a draft: wait for the user to accept it or change it"
    elif parts is None:
        nxt = "select the parts (stage 2), then wait for the user to accept them"
    elif parts["spec_revision"] != spec["revision"]:
        nxt = (f"the part selection is based on specification revision {parts['spec_revision']}, now "
               f"{spec['revision']}: revise it, then wait for the user")
    elif parts["status"] != ACCEPTED:
        nxt = "the part selection is a draft: wait for the user to accept it or change it"
    else:
        nxt = "specification and parts accepted: the schematic stage may start"
    return {"specification": brief(spec), "part_selection": brief(parts), "next": nxt}
