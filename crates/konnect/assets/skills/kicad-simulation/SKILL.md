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
limits, and reported with every assumption visible.

Start with Konnect's `simulation` toolset (`load_toolset("simulation")`). Its
`sim_*` tools work on any design: `sim_read_schematic`, `sim_board_stackup`,
`sim_copper_connectivity`, `sim_copper_path`, `sim_trace_coupling`,
`sim_board_thermal`, `sim_spice_run`, `sim_capacitor_check`,
`sim_inductor_check`, `sim_input_network`, `sim_conducted_emissions`,
`sim_corner_sweep`, `sim_evaluate_check`, `sim_write_report`,
`sim_render_document`, `sim_cycle_record` and `sim_layout_calc`. Pass every sourced input as
`{"value": v, "source": "..."}`; a bare number is an assumption. Board tools
read the saved board, so refill zones and save first.

For topology-specific questions the toolset does not cover yet (the
closed-loop buck converter, switch-node stress), the assistant writes a script
on the `konnect_sim` Python package in the Konnect repository (`sim/`), which
supplies the parts that must not be re-invented per script.

Read [`references/simulation-methods.md`](references/simulation-methods.md)
before choosing a method or writing a testbench, and
[`references/spec-and-emc-estimates.md`](references/spec-and-emc-estimates.md)
when writing a specification or checking PDN target impedance, conducted or
radiated emissions, crosstalk, filter attenuation or a pre-compliance plan
(textbook relations with citations, mapped to the `sim_*` tools).

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
| Regulation, loop stability, load and line steps, start-up, light load, faults | Closed-loop switched simulation with a controller model (`converter`, `scenarios`); the **vendor's model** where it runs in ngspice |
| Switch-node and pin voltage stress | Edge-resolved SPICE run plus `stress` limits with durations |
| Copper resistance, ground connectivity, current density, via current | Filled-copper solve (`copper`) |
| Inductor, capacitor, loss, efficiency and temperature | Manufacturer data through `parts`, `losses`, `thermal` |
| Input-filter interaction, hot-plug, conducted EMI, SW-to-FB coupling | `inputfilter`, `emi` (pre-compliance estimates); Ott's common- and differential-mode envelopes as the second method |
| PDN impedance against the target impedance | SPICE AC sweep with a 1 A source at the IC node |
| Radiated emission, cable common-mode current | Ott's loop and cable estimates from extracted loop area (design direction only, never compliance) |
| Impedance, crosstalk, via transitions | Field solver (not available yet) |

[`references/simulation-methods.md`](references/simulation-methods.md) maps
every buck verification item to the module that checks it and the evidence
that makes it PASS/FAIL rather than INCOMPLETE.

Prefer two independent methods for every PASS (for example closed form and
SPICE agreeing within a stated tolerance). One method alone is acceptable only
when the second is impossible, and the report says so.

### 4. Write the script

Keep every project's simulation files in the same folders (the package's
`project.Folders` gives these paths; never mix scripts, data and charts in one
folder):

| Folder | Holds |
|---|---|
| `sim/scripts/` | the scripts, named after the question (`buck_ripple.py`), so the user can re-run them |
| `sim/inputs/` | sourced inputs: fabricator stackup JSON, spec, values read from datasheets |
| `sim/results/<run>/` | raw outputs of one run: check JSON, waveforms (CSV/NPZ), logs |
| `sim/figures/<run>/` | that run's charts (PNG) |
| `sim/results/cycle_log.json` | the optimisation cycle's history |
| `reports/` | the reports people read: `.md`, `.json` and `.pdf` |

`<run>` names one evaluation (`baseline`, `it1`, `it2`, ...). A script takes
the run name as an argument and starts with
`F = Folders.of_script(__file__, run).make()`.

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
report to `reports/` (`reports/<name>_report.md`, `.json` and `.pdf`):
`Report.write` and `sim_write_report` render the PDF, text and figures
together, by default. Add `"docx"` to `documents` when the user wants Word.

A narrative engineering report (design choices, deliverables, results summary,
recommendations) is also written in Markdown to `reports/`, with its figures
linked from `sim/figures/<run>/` by relative path, and rendered with
`sim_render_document` (or `document.render`). The PDF is the deliverable; the
Markdown is its source.

Status rules:

- **PASS / FAIL**: every governing input is sourced and the method can answer
  the question.
- **INCOMPLETE**: any governing input is assumed, a needed model is missing, or
  the method cannot answer the question. Name the missing evidence.

The overall verdict is FAIL if any check fails, otherwise INCOMPLETE if any
check is incomplete, otherwise PASS.

**Graph every checked quantity.** A report is not finished until each swept or
waveform result is also drawn against its limit and embedded in the report:

- Build charts with the package's `figures` module (`metric_vs_x`,
  `waveform_grid`, `spectrum`, `bode`). For a switching converter, save
  `converter.snapshot(w, period)` at every operating point of the steady-state
  sweep (the whole window the check measured) and call
  `figures.ripple_figures(..., period=period)`: one summary of output, input
  and inductor ripple versus load per input voltage, plus waveform grids of
  output ripple, input ripple, inductor current and switch node. Every
  peak-to-peak value in the figures is then the one the check reported.
- Draw every spec limit as a labelled reference line. Mark a point the model
  cannot represent (pulse skipping, a missing model) as such; never draw it as
  a zero.
- Save charts to `sim/figures/<run>/` and embed them:
  `Report(figures=[(title, path)])` in a script, or
  `sim_write_report(..., figures=[{"title", "path"}])` from the toolset (the
  `png` of `sim_board_thermal` included). Then open the PNGs and check them for
  overlapping labels, and check the rendered PDF pages, before reporting.

---

## When the result depends on the layout

To extract the board's parasitics into the simulation, and to iterate the layout
until the checks meet their limits, use the `kicad-sim-optimize` skill. It
reuses this skill's scripts and report rules.

## Limits to state plainly

- The closed-loop controllers (`converter.COT`, `VoltageMode`,
  `PeakCurrentMode`) are behavioural models built from datasheet values.
  Datasheets rarely publish internal values such as the ripple-injection gain,
  so a controller counts as evidence only after it has been matched to a vendor
  waveform (datasheet load-step or start-up figure) or a bench measurement,
  recorded in `Common.validated_by`. Until then every closed-loop check is
  `INCOMPLETE`, whatever its margin. Prefer the vendor's model when it runs.
- The switch-node peak depends on the edge rate and on the switch's own
  damping; copper resistance barely damps the hot-loop ring. Without a sourced
  edge time (or the vendor's switch model) the stress result stays `INCOMPLETE`.
- EMI results are harmonic amplitudes, not a CISPR 16 receiver: a margin under
  about 6 dB is a fail-risk. Limit lines come from the standard, with citation.
- Ceramic capacitors lose capacitance under DC bias. Without the manufacturer's
  derating curve for the exact part number, bound the result (nominal and a
  stated derated value) and mark the dependent check `INCOMPLETE`.
- Simulation verifies the circuit as modelled. Layout parasitics are excluded
  unless extracted; say so in the report.

## Rules

1. Never present a number without its method and source.
2. Never turn an assumed input into a PASS.
3. Never retype component values; extract them from the design.
4. Keep scripts, inputs, results, figures and reports in the project's
   standard folders so results can be reproduced.
5. Validate every SPICE result against an independent estimate.
