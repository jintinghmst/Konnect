---
name: kicad-simulation
description: |
  Workflow skill for proving a KiCad design meets its specification by writing and running
  simulation code. Triggers on: "simulate", "does it meet spec", "verify the ripple",
  "check the buck", "SPICE", "will this work in reality", "load step", "check the filter",
  "input RMS current", "is the feedback divider right", "simulation report".
argument-hint: "[circuit or requirement to verify]"
---

# Simulation-Based Spec Verification

Use this skill to answer "does this design meet its specification?" with
evidence: numbers produced by code that can be re-run, compared with stated
limits, and reported with every assumption visible. The assistant writes the
simulation script; the `konnect_sim` Python package in the Konnect repository
(`sim/`) supplies the parts that must not be re-invented per script.

Read [`references/simulation-methods.md`](references/simulation-methods.md)
before choosing a method or writing a testbench.

---

## Workflow

### 1. Pin down the requirement

List each requirement as a measurable limit with its source: user, product
spec, or component datasheet (with document number and table). If the user
gives no limit, propose one, mark it **assumed**, and keep it assumed in the
report. A check against an assumed limit is `INCOMPLETE`, never PASS.

### 2. Extract the design, never retype it

- Components, values and nets: `export_netlist_summary`, `list_schematic_components`,
  or the package's KiCad netlist loader (uses kicad-cli's own connectivity).
- Board facts for SI/PI work: `get_board_stackup`, `get_component_pads`, `query_traces`.
- Parse values with the package's value parser. A value that is
  marked DNP is excluded and the report says so.

Record the schematic path and the date; every number in the report must be
traceable to the design file or a cited document.

### 3. Choose the cheapest method that can answer the question

| Question | Method |
|---|---|
| Set-points, ratios, simple ripple and RMS estimates | Closed-form equations |
| Steady-state switching waveforms, ripple with losses, component currents | Switched SPICE transient |
| Linear filters, dividers, op-amp stages | SPICE AC/OP, compared with closed form |
| Loop stability, load-step response | SPICE with the **vendor's controller model** |
| Impedance, crosstalk, via transitions | Field solver (not available yet) |

Prefer two independent methods for every PASS (for example closed form and
SPICE agreeing within a stated tolerance). One method alone is acceptable only
when the second is impossible, and the report says so.

### 4. Write the script

- Put it in the project folder under `sim/`, named after the question
  (`sim/buck_ripple.py`), so the user can re-run it.
- Write SPICE testbenches as plain SPICE text through the package's netlist
  runner, so an engineer can read and re-run them in any SPICE simulator.
- State every model parameter that is not from the design or a datasheet
  (ESR, ESL, DCR, source impedance, capacitor DC-bias derating) as an
  assumption in the script and the report.
- Sweep the corners that matter: minimum and maximum input voltage, load,
  and component tolerance. A nominal-only result is not evidence of margin.

### 5. Validate the simulation before trusting it

- Compare SPICE against the closed-form estimate on the same operating point.
  A disagreement larger than about 10 % is a modelling error to explain before
  any result is reported.
- Check that the transient has reached steady state (measure over the last
  cycles of a run long enough that averages stop changing).
- A simulator error, non-convergence, or a result outside physical bounds is
  a failed check, not a result.

### 6. Report

Use the package's report builder. Each check carries requirement, result,
status, method, sources, assumptions, and what would complete it. Write the
report next to the script (`sim/<name>_report.md` and `.json`).

Status rules:

- **PASS / FAIL**: every governing input is sourced and the method can answer
  the question.
- **INCOMPLETE**: any governing input is assumed, a needed model is missing, or
  the method cannot answer the question. Name the missing evidence.

The overall verdict is FAIL if any check fails, otherwise INCOMPLETE if any
check is incomplete, otherwise PASS.

---

## Limits to state plainly

- SPICE here has no model of a specific controller IC unless the vendor's model
  is supplied. Open-loop switched models cannot prove stability or load-step
  behaviour; report those `INCOMPLETE` until the controller model is available.
- Ceramic capacitors lose capacitance under DC bias. Without the manufacturer's
  derating curve for the exact part number, bound the result (nominal and a
  stated derated value) and mark the dependent check `INCOMPLETE`.
- Simulation verifies the circuit as modelled. Layout parasitics are excluded
  unless extracted; say so in the report.

## Rules

1. Never present a number without its method and source.
2. Never turn an assumed input into a PASS.
3. Never retype component values; extract them from the design.
4. Keep scripts and reports in the project so results can be reproduced.
5. Validate every SPICE result against an independent estimate.
