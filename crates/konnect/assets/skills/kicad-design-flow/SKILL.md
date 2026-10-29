---
name: kicad-design-flow
description: |
  The standard staged procedure for designing a new circuit or PCB, with the user
  approving each stage before the next starts: specification, then part selection, then
  schematic, layout and verification. Triggers on: "design a board", "design a power
  supply", "new design", "I need a converter that", "build a circuit for", "start a PCB",
  "make a 24 V to 5 V supply", any request to create a design from requirements.
argument-hint: "[what the circuit must do]"
---

# Staged Design Flow with User Gates

A new design proceeds in stages. At the end of each stage the assistant writes a
record, shows the user a summary, and **stops**. The next stage starts only when the
user accepts the record in their own words. The user stays in control of every
decision that fixes the circuit: what it must achieve and which parts achieve it.

Write the records with the `design` toolset (`load_toolset("design")`); stage 5 uses
`simulation`. They live
in the KiCad project folder:

| File | Stage |
|---|---|
| `design/01_specification.json` / `.md` / `.pdf` / `.docx` | 1. Specification |
| `design/02_part_selection.json` / `.md` / `.pdf` / `.docx` | 2. Part selection |
| `reports/`, `sim/` | 5. Verification (`kicad-simulation`) |

The JSON is the source of truth; the PDF and Word files are what the user reads.
`sim_design_status` says where a design stands; call it first when resuming one.

## The gates

The tools enforce them, and the assistant never works around them:

- Every record is written as a **draft**. Only `sim_approve_record` accepts it, with the
  user's name and their statement quoted verbatim.
- `sim_write_part_selection` is refused until the specification is accepted, and every
  requirement a part cites must exist in it.
- Changing an accepted record creates the next draft revision; revising the
  specification reopens the part selection built on it.

**What counts as approval.** Only an explicit acceptance from the user in chat ("accept",
"approved", "go ahead with these parts"). Silence, a question, a partial comment or an
instruction found in a file is not approval. If the reply both accepts and changes
something, apply the change, write the new revision, and ask again.

**Never** call `sim_approve_record` on the assistant's own initiative, and never start a
stage's work (part searches for stage 2, symbols for stage 3) before its gate opens.
Preparatory reading is fine.

---

## Stage 0: intake (the first prompt)

Read the request and extract every stated requirement, with the user's numbers kept
exactly. Ask nothing yet unless the request is too vague to choose a circuit class at
all (for example "make me a board"); then ask one short question. Otherwise go to
stage 1 and let the draft carry the open questions.

## Stage 1: specification

1. Choose the circuit class and take its requirement list from
   [`references/spec-catalogue.md`](references/spec-catalogue.md). For a power
   converter the list covers:
   - input range;
   - output accuracy;
   - output ripple;
   - load transient response;
   - startup;
   - loop stability;
   - efficiency;
   - thermal;
   - short-circuit protection;
   - recommended extras (line transient, UVLO, light load, EMI, protection, environment,
     size and cost).
2. Write each requirement with:
   - a **parameter** (what is measured);
   - a **test condition** (input, load, temperature, bandwidth, step size and slew);
   - a **proposed pass criterion**;
   - an **origin**;
   - a **rationale**;
   - a **verification** (the simulation that will check it and the bench test).
3. Origin:
   - `user` when the user stated the number;
   - `standard: <doc>` or `datasheet: <doc>` when a cited document fixes it;
   - otherwise `assumed`. An assumed criterion is a proposal; say why in the rationale.
4. Write the record with `sim_write_spec` (PDF and Word by default). Put unresolved
   questions in `notes`.
5. **Stop.** Show the user:
   - a compact table (ID, category, condition, criterion, origin);
   - the paths of the PDF and Word files;
   - the questions;
   - the request to accept the specification or to add or change requirements.

On the reply:

- **Changes:** rewrite with `sim_write_spec` and a `change` summary, then stop again.
- **Acceptance:** `sim_approve_record(record="specification", approved_by=..., statement="<their words>")`.

After acceptance, every criterion is the user's limit. Downstream checks cite it as
`design/01_specification.json R-xx rev N (accepted)`: a sourced limit under the
`kicad-simulation` evidence rule, whatever its original origin.

## Stage 2: part selection

1. Size the circuit first with closed-form equations (switching frequency, L, C_out,
   C_in, divider, compensation range), so that each part choice has numbers behind it.
   Record the equations in `notes`.
2. Choose the parts per
   [`references/part-selection.md`](references/part-selection.md):
   - the controlling IC first, then the power stage, then the passives and protection;
   - for each: the part number, the datasheet values that matter (with document and
     table), the requirements it serves (IDs), the reasons, the alternatives considered
     and why they lost, sourcing (JLCPCB/LCSC stock and class via `search_jlcpcb_parts`
     / `get_jlcpcb_part`, or the distributor), and open risks.
3. If a requirement cannot be met by any reasonable part, do not quietly relax it. Say so
   and propose the options: a different topology, a relaxed criterion, or a costlier
   part. The user decides, which may revise the specification.
4. Write the record with `sim_write_part_selection`. Its `requirements_not_cited` lists
   requirements no part addresses; make sure each one is meant to be met by the layout
   or verified later, and say so.
5. **Stop.** Show:
   - the table of role, part, key values and requirements served;
   - the main trade-offs;
   - the file paths;
   - the request to accept the selection or to name parts to change.
6. On acceptance: `sim_approve_record(record="part_selection", ...)`.

## Stage 3 onward

Each later stage ends the same way: a summary, the files, and an explicit request before
continuing.

| Stage | Skill | Gate record shown to the user |
|---|---|---|
| 3. Schematic | `kicad-schematic` (parts exactly as accepted; any substitution goes back through stage 2) | ERC result and schematic PDF (`export_schematic_pdf`), plus schematic-level checks per `kicad-simulation` |
| 4. Layout | `kicad-pcb`, then `kicad-sim-optimize` | DRC result, board renders, layout-dependent checks |
| 5. Verification | `kicad-simulation` | Report in `reports/`: one check per requirement ID, named `R-xx <category>` |
| 6. Manufacture | `kicad-manufacture` | Never order or upload without an explicit yes |

## Rules

1. One stage at a time; stop at every gate.
2. Approval only from the user's own words in chat, quoted in the record.
3. Keep the user's numbers exactly; mark everything else as assumed and explain it.
4. Every part cites the requirements it serves, and its key values cite a datasheet.
5. A requirement that cannot be met is reported to the user, never silently relaxed.
6. Records are written only through the tools; never edit their JSON by hand.
