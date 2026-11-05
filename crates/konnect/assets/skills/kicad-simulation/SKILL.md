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
`sim_render_document`, `sim_cycle_record` and `sim_layout_calc`. The design
records of `kicad-design-flow` (`sim_write_spec`, `sim_design_status`, ...) are
in the separate `design` toolset. Pass every sourced input as
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

If the project has an accepted specification (`design/records/01_specification.json`,
from the `kicad-design-flow` skill), it is the list: one check per requirement,
named `R-xx <category>`, with the limit cited as
`design/records/01_specification.json R-xx rev N (accepted)`. The user accepted it, so it
is a sourced limit.

Otherwise list each requirement as a measurable limit with its source: user,
product spec, or component datasheet (with document number and table). If the
user gives no limit, propose one, mark it **assumed**, and keep it assumed in the
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

Every project uses the same layout (the package's `project.Folders` gives these
paths; never mix scripts, data and charts in one folder):

```
<project>/
  *.kicad_pro / _sch / _pcb
  design/                  approved specification and part selection (kicad-design-flow)
  sim/                     the simulation scripts, shared by all iterations
  sim/inputs/              sourced inputs: fabricator stackup JSON, datasheet values
  sim/report_sources/      Markdown sources of written reports
  sim/cycle_log.json       the optimisation cycle's history
  reports/                 final reports: PDF only
  iteration1/sim/results/  raw outputs of iteration 1: check JSON, waveforms (CSV/NPZ), logs
  iteration1/sim/figures/  its charts (PNG)
  iteration1/reports/      its report: PDF only
  iteration2/ ...          the next pass, after a design or layout change
```

| Folder | Holds |
|---|---|
| `sim/` | the scripts, named after the question (`buck_ripple.py`), so the user can re-run them |
| `iteration<N>/` | everything one evaluation produced; never overwritten by a later one |
| `reports/` | the final reports people read (the engineering report, the cycle's final report). Word only when the user asks |

An iteration is one evaluation of one state of the design. A first verification is
`iteration1`; each change to the schematic or board followed by a re-run is the next
iteration (`Folders.next(project)`). Re-running the same state overwrites its own
iteration only. A script takes the iteration number as an argument and starts with
`F = Folders.of_script(__file__, iteration).make()`; its outputs go to `F.results`,
`F.figures` and `F.iteration_reports`.

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
status, method, sources, assumptions, what would complete it, and its graphs.
`Report.write` and `sim_write_report` write one file people read,
`reports/<name>.pdf`, laid out as:

1. **Summary:** the overall verdict and counts, a table of every check, the
   chart of all checks against their limits, and general notes and assumptions.
2. **Results by test:** one numbered section per check with requirement,
   result, method, sources, assumptions, what would complete it and the lever,
   followed by its graphs. A graph shared by several checks is drawn under the
   first and referred to by figure number after that.
3. **Further figures:** charts not tied to one check.

Write an iteration's report to `iteration<N>/reports/<name>.pdf`. Nothing else goes
into a reports folder: the summary chart goes to `iteration<N>/sim/figures/` and the
checks as JSON (read by `sim_cycle_record`) to `iteration<N>/sim/results/`, which
are the defaults for a report in `iteration<N>/reports/`. Add `"docx"` to
`documents` only when the user asks for Word, and `"md"` only to debug the layout.

A narrative engineering report (design choices, deliverables, results summary,
recommendations) is written in Markdown to `sim/report_sources/`, with its
figures linked from `iteration<N>/sim/figures/` by relative path, and rendered into
`reports/` with `sim_render_document(markdown, out_dir=<project>/reports)`. The
PDF is the deliverable; the Markdown stays with the simulation files.

Status rules:

- **PASS / FAIL**: every governing input is sourced and the method can answer
  the question.
- **INCOMPLETE**: any governing input is assumed, a needed model is missing, or
  the method cannot answer the question. Name the missing evidence.

The overall verdict is FAIL if any check fails, otherwise INCOMPLETE if any
check is incomplete, otherwise PASS.

**Every simulation is graphed.** No result is reported without its graph:

- Every `sim_*` tool that simulates draws its result and returns it as
  `figures` [{"title", "path"}], attached to each check it returns:

  | Tool | Graph |
  |---|---|
  | `sim_spice_run` | The `plot` vectors (default: the measured ones) against time or frequency, with measured values marked |
  | `sim_corner_sweep` | Each metric at every case, with the nominal and the worst cases marked |
  | `sim_capacitor_check` | DC-bias curve with the working and maximum voltage and the rating |
  | `sim_inductor_check` | L(I) curve with the DC, worst-peak and saturation currents |
  | `sim_input_network` | Network output impedance against the converter's input resistance |
  | `sim_conducted_emissions` | Spectrum against the limit line |
  | `sim_copper_path` | Current-density map |
  | `sim_copper_connectivity` | Map of the joined copper groups |
  | `sim_trace_coupling` | Coupled runs |
  | `sim_board_thermal` | Temperature map |

  Parts without a manufacturer curve fall back to their checks against their limits.
- Pass `figures_dir` = `iteration<N>/sim/figures/` of the project to every tool, so the
  graphs stay with the iteration. Without it they go to the system temporary folder.
- `sim_write_report` and `Report.write` embed the checks' graphs. Every report also opens
  its figures with a chart of all numeric checks as margins to their limits, coloured by
  status. Checks with no graph of their own are listed under "Checks without a graph of
  their own" and returned as `ungraphed`. Give each of them a graph (a script chart
  passed in `figures`, or the tool that produced the number) before the report is
  final. Only checks that are not a simulation (a datasheet lookup) may stay there.
- In a script, attach a chart to its check (`Check(..., figures=[{"title", "path"}])`)
  and build it with the package's `figures` module:
  - general charts: `metric_vs_x`, `waveform_grid`, `spectrum`, `bode`, `curves`,
    `cases`, `bars`, `board_map`;
  - for a switching converter, save `converter.snapshot(w, period)` at every operating
    point of the steady-state sweep (the whole window the check measured) and call
    `figures.ripple_figures(..., period=period)`. This gives one summary of output, input
    and inductor ripple versus load per input voltage, plus waveform grids of output
    ripple, input ripple, inductor current and switch node. Every peak-to-peak value in
    the figures is then the one the check reported.
- Draw every spec limit as a labelled reference line. Mark a point the model cannot
  represent (pulse skipping, a missing model) as such; never draw it as a zero.
- Open the PNGs and check them for overlapping labels, and check the rendered PDF pages,
  before reporting.

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
6. Never report a simulation result without its graph; pass `figures_dir` to every
   `sim_*` tool.
