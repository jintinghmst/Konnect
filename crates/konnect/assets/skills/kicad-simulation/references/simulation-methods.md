# Simulation Methods and the Simulation Package

## Environment

- **SPICE engine:** KiCad's bundled ngspice library (`bin/ngspice.dll`, code
  models in `lib/ngspice`), driven from Python through PySpice. No separate
  simulator install. Set the `KICAD_DIR` environment variable if KiCad is not
  in a standard location.
- **Python packages:** numpy, scipy, matplotlib, scikit-rf, PySpice.
- **Package:** the Konnect repository's `sim` folder. Add it to the import
  path, then use its modules:

| Module | Use |
|---|---|
| spice | configure ngspice; run a plain-SPICE netlist and get vectors back |
| netlist | load a schematic's components, values and nets via kicad-cli |
| units | parse KiCad value text such as "22uF 16V" or "470pF DNP" |
| report | checks with status, method, sources and assumptions; Markdown and JSON output |
| buck | buck design equations, a switched open-loop SPICE testbench (optionally with layout parasitics and an edge-resolved run), and extraction of a buck layout's parasitics |
| layout | read a saved board (read-only): pads, tracks, vias, pours and their fills, stackup; copper path resistance, loop and via inductance, IPC-2221 track width |
| cycle | iteration log and continue / revert / met / stalled / limit decisions for the optimisation cycle |
| stackup | stackup with provenance: the board's own, or a fabricator's published stackup as JSON with its source; applied to a board for every extraction |
| copper | DC flow in the real copper of a net (filled pours with thermal reliefs, tracks, pads, via and plated-hole barrels): connectivity, resistance, current density, via currents, I²R maps |
| parts | inductor and capacitor suitability from manufacturer data: saturation, L(I), RMS current, losses, temperature; effective capacitance, ripple share by impedance, self-heating, voltage derating |
| losses | synchronous-buck loss budget and efficiency, junction temperature solved with RDS(on)(Tj), switching time fitted to a vendor efficiency curve |
| thermal | steady-state board temperature: copper-coverage heat spreading, laminate, film coefficient, component and copper heat sources |
| converter | closed-loop switched simulation (source and cable, capacitor DC bias, L(I), DCM, body diodes) with COT, voltage-mode and peak-current-mode controllers; loop gain by series injection |
| scenarios | closed-loop checks: regulation, stability over corners, load step, start-up/shut-down, light load, line steps and brown-out, overload and short |
| stress | switch-node and pin stress from an edge-resolved run, against limits with durations; skin-effect resistance |
| inputfilter | Middlebrook margin of the source network against the converter's negative input resistance; hot-plug overshoot |
| emi | conducted DM spectrum at a LISN, limit-line margin, CM estimate, SW-to-FB mutual capacitance and coupled noise |
| corners | tolerance vertices and Monte Carlo over sourced spreads |
| budget | output-voltage window at the load: both limits, every contribution |
| figures | report charts: ripple summary and waveform grids from `converter.snapshot`, metric vs sweep, spectrum vs limit, Bode; embedded through `Report(figures=...)` |
| document | the report as PDF (default) or Word, text and figures together: `Report.write(..., documents=...)`, `sim_write_report`, `sim_render_document` |
| project | `Folders`: `sim/scripts`, `sim/inputs`, `sim/results/<run>`, `sim/figures/<run>`, `reports/` |

## Coverage of a buck verification

| Item | Check | Module | PASS/FAIL needs |
|---|---|---|---|
| Ground connectivity and extraction | load ground connected through fill; return R; plane partial L | copper, buck.layout_parasitics | zones filled and saved |
| Stackup | copper, gaps, εr from a source | stackup | board stackup or fab document |
| Closed-loop regulation | DC output over line and load | scenarios.regulation | validated controller model |
| Stability | PM/GM by injection over line, load, Cout/ESR corners | converter.loop_gain, scenarios.stability | validated controller model; injection with `snr_ok` |
| Load step | dip, overshoot, settling, ringing | scenarios.load_step | validated controller model; spec step and slew |
| Start-up and shut-down | overshoot, inrush, monotonic rise, restart | scenarios.startup_shutdown | validated controller model |
| Light load | pulse skipping, ripple, regulation, audio-band pulses | scenarios.light_load | validated controller model |
| Input transients | line steps, dip and brown-out, hot-plug | scenarios.input_transients, inputfilter.hot_plug | validated model; cable R/L |
| Switch-node stress | peak, undershoot, time beyond each limit | buck.run_edge, stress | sourced edge time and damping |
| Other pin stress | VIN pin, VBST–SW, VBST | buck (bootstrap model), stress | as above; datasheet limits |
| Inductor | Isat margin, L(I), Irms, copper and core loss, temperature | parts | datasheet ratings, L(I) curve, core-loss data |
| Capacitors | C_eff, ripple current per part, heating, voltage | parts | DC-bias curve, ESR(f), heating curve, derating policy |
| Losses and efficiency | full loss budget, η | losses | RDS(on), tempco, switching times (or fit to vendor η curve) |
| Thermal | Tj, component temperatures, board hot spot | losses, parts, thermal | θJA / package resistance; measured film coefficient |
| Copper and via capacity | peak current density, via currents vs IPC-2221 | copper.capacity | RMS currents from the simulation |
| Output-voltage limits | min and max at the load | budget | every term sourced |
| Worst-case corners | L, C, ESR, R, Vref, temperature, fsw | corners | tolerance limits from datasheets |
| Protection | overload, short, hiccup, recovery | scenarios.faults | validated model; datasheet limits |
| Input-network interaction | Middlebrook margin, resonance, Q | inputfilter | cable/supply impedance |
| EMI and coupling | DM spectrum vs limit, CM estimate, SW-to-FB noise | emi | limit line from the standard; C to chassis |

## Choosing a method

1. **Closed form** first. It is fast, shows the governing physics, and gives
   the yardstick for validating any simulation.
2. **SPICE operating point and AC** for linear networks (dividers, filters,
   amplifiers). Agreement with the closed form within numerical tolerance is
   expected.
3. **SPICE transient** for switching converters and anything with timing.
   Start the run near steady state (inductor and capacitor initial conditions)
   and measure over the final cycles.
4. **Vendor models** for controller behaviour (stability, load step, soft
   start). If the model does not load in ngspice, say so; do not substitute a
   guessed controller.

## Writing SPICE testbenches that converge

- Model power switches as gate-controlled conductances rather than ngspice's
  ideal switch element, which fails to converge on hard-switched nodes.
- In behavioural sources, clamp with the min and max functions; the
  limit function did not behave as expected in KiCad 10's ngspice.
- Give every switching node a realistic capacitance (MOSFET output
  capacitance, a few hundred pF) and the low-side switch a body diode.
- Give sources a series resistance; zero-ohm loops stall the solver.
- Use the gear integration method and finite gate edges (a few ns).
- Do not name a node `in`: ngspice's AC analysis fails on it. Use `vin`, `a`, or similar.

## Common assumptions to declare

| Parameter | Typical assumed range | Better source |
|---|---|---|
| Ceramic ESR | 2–5 mΩ per part | Manufacturer impedance curve |
| Ceramic ESL | 0.3–1 nH per part | Manufacturer data, layout extraction |
| Ceramic DC-bias derating | 20–60 % loss at rated-voltage fraction | Manufacturer curve for the exact part number |
| Electrolytic ESR | 0.05–0.5 Ω | Datasheet |
| Inductor DCR, saturation | none | Inductor datasheet for the exact part number |
| Source impedance | 50 mΩ, 0.1–10 µH | Supply and cable specification |

Every row used without the better source makes the dependent check
`INCOMPLETE`.
