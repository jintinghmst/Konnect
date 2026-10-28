---
name: kicad-sim-optimize
description: |
  Autonomous simulate → review → change → re-simulate cycle that improves a KiCad
  layout (and, when needed, the schematic) until simulation shows the design meets
  its specification, or the cycle proves it cannot. Triggers on: "optimise the
  layout", "iterate until it meets spec", "fix the layout from the simulation",
  "closed-loop design", "keep adjusting until it passes", "autonomous layout cycle",
  "reduce the hot loop", "improve the board until the simulation passes".
argument-hint: "[project folder] [spec or 'use the existing report']"
---

# Simulation-Driven Layout Optimisation Cycle

This skill closes the loop between the `kicad-simulation` skill (evidence) and the
`kicad-pcb` / `kicad-schematic` skills (changes). Each iteration reads the
simulation report, picks **one** change, applies it through Konnect MCP tools,
verifies the board, re-runs the simulation against the saved board, and lets
the package's cycle log decide whether to continue, revert, or stop.

Read [`references/levers.md`](references/levers.md) before the first change: it
maps each kind of failing check to the change that moves it, with the physics
to predict the effect before touching the board.

All board and schematic changes go through Konnect MCP tools. The simulation
reads the **saved** files, so every iteration saves before it simulates.

---

## Preconditions

Stop and report `BLOCKED` naming the missing item if any is absent:

1. A complete schematic, ERC clean, and a placed and routed board with a DRC
   result: build these with the `kicad-schematic` and `kicad-pcb` skills first.
2. A specification. Limits the user did not give stay **assumed**; the cycle
   may optimise against them but the final report keeps them assumed.
3. A per-project simulation script in the project's `sim/scripts/` folder that
   follows the script contract below (write it with the `kicad-simulation`
   skill, which defines the project's simulation folders).
4. KiCad running with the board open in the PCB Editor, reachable over IPC.

## Constraints and objectives

- **Constraints** are checks with a numeric value and a limit. A check *meets*
  when its value is inside the limit, whatever its evidence status. The cycle's
  goal is that every constraint meets and DRC and ERC are clean.
- **Objectives** are quantities to minimise that have no hard limit, such as
  hot-loop inductance or output-path resistance. Once every constraint meets,
  the cycle keeps improving objectives until a change gains less than 5 %.
- **Evidence status is separate.** A check that meets but rests on an assumed
  input stays `INCOMPLETE` in the report. The cycle never converts it to PASS.

## Script contract

The project script runs one full evaluation and records it in the cycle log.

- Arguments: `--run <name>` (`baseline`, `it1`, ...), `--change "<what changed>"`,
  `--drc <error count>`, `--erc <error count>`.
- It reads the schematic through the package's netlist loader and the saved board
  through its layout loader, extracts the layout parasitics, runs the closed-form
  and SPICE checks, writes raw outputs to `sim/results/<run>/` and charts to
  `sim/figures/<run>/`, and writes `reports/<stem>_<run>.md` / `.json` / `.pdf`
  with the charts embedded.
- Every constraint carries value, limit, sense, whether it depends on the layout,
  and its lever.
- It appends the iteration to `sim/results/cycle_log.json` through the package's
  cycle log, prints the iteration table, and ends with one line:
  `DECISION: <continue|revert|met|stalled|limit> — <reason>`.

## The cycle

### 0. Baseline
1. Save the board with `save_project`.
2. Run DRC with `run_drc` and `sync_live_board: true`, then ERC with `run_erc`.
3. Run the script with `--change "baseline"`. Read the report and the decision.

### 1. Review
- List every constraint that misses, worst margin first, and every objective.
- Classify each miss by its lever:
  - **layout lever**: placement, track width, vias, pours, sense point;
  - **design lever**: component value or part choice (schematic change).
- Read `unmodelled` in the report. A miss caused only by something unmodelled is
  an evidence gap, not a design fault: report it instead of changing the board.

### 2. Choose one change
- Pick the single change with the largest predicted gain on the worst margin.
  When all constraints meet, pick the one with the largest predicted gain on an
  objective.
- Predict the effect first. Recompute the affected quantity with the package's
  layout functions using the planned coordinates or width, as shown in the
  reference.
- Skip a change predicted to gain less than 5 %.
- **Record the undo before changing anything:** current positions and rotations
  from `get_component_list`, and UUIDs of tracks you will delete from
  `query_traces`.
- Prefer layout levers. Use a design lever only when no layout change can reach
  the limit, or the lever table says the check is set by values.

### 3. Apply
- **Layout:**
  - move parts with `set_component_placements` or `move_component`;
  - reroute with `delete_trace` then `route_trace`;
  - add vias with `add_via` and pours with `add_zone`.
- **Design:**
  - change values with `batch_edit_schematic_components`;
  - run `update_pcb_from_schematic` as a dry run, then apply;
  - place and route new parts;
  - run `run_erc`.
- **Read back:** confirm every moved reference with `get_component_list`.

### 4. Verify
- Run `run_drc` with `sync_live_board: true` and `refill_zones: true`. This
  saves the board and refills pours, so the next step simulates what is on screen.
- New DRC errors are not fixed by more changes in the same iteration; the
  decision step reverts them.

### 5. Re-simulate and decide
Run the script with `--change "<one-line description>" --drc N --erc M`, then
act on the decision:

| Decision | Action |
|---|---|
| `continue` | Go to step 1. |
| `revert` | Undo the change from the recorded state, verify (step 4), run the script with `--change "revert: …"`. Mark that lever as tried; do not repeat it unchanged. |
| `met` | Stop. Report success. |
| `stalled` | Stop. Restore the best iteration if it is not the current one; report what limits further gain. |
| `limit` | Stop. Restore the best iteration; report the remaining misses. |

## Guardrails

1. **One change per iteration**, so each result has one cause.
2. **Never move a limit to make a check meet.** Only the user changes the spec.
   Never delete, waive or loosen DRC rules or netclass minimums to clear errors.
3. **Netclasses live in the project file**, which Konnect refuses to write while
   the board is open. A netclass change pauses the cycle: ask the user to close
   the board, change it, then reopen. Widening specific tracks needs no pause.
4. **Stay inside the outline.** Read the outline before moving parts and keep
   connectors at the edges.
5. **Stop on lost authority.** If `unsafe_file_fallback` appears or IPC is lost,
   stop and hand back to the user. Do not continue from the saved file.
6. **Budget:** at most 6 iterations unless the user sets another limit.
7. **Do not trade a hard limit for an objective.** A change that makes a
   constraint miss is reverted, even if an objective improves.

## Final report

Write it in Markdown to `reports/`, embed the final run's charts from
`sim/figures/<run>/`, and render the PDF with `sim_render_document`. It holds:

- The iteration table from the cycle log (change, misses, worst margin,
  objectives, DRC, verdict) and the final decision.
- Before/after values of every constraint and objective.
- The constraints that still miss and why. Separately, the checks that meet but
  stay `INCOMPLETE`, with the evidence that would close them (part numbers,
  vendor model, measurement).
- What the parasitic extraction does not model (pours in path resistance,
  absolute switch-node peak, EMI). Say that bench measurement is the final
  arbiter for those.
