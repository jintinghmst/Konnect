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
| buck | buck design equations and a switched open-loop SPICE testbench |

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

## Validation evidence (buck testbench, 2026-10-16)

On a 24 V to 5 V, 5 A, 500 kHz, 6.8 µH test case the switched testbench
agreed with the closed-form equations:

| Quantity | Closed form | SPICE |
|---|---|---|
| Inductor ripple | 1.16 A pp (ideal duty) | 1.20 A pp (loss-adjusted duty) |
| Input capacitor RMS current | 2.04 A | 2.09 A |
| Average inductor current | 5.00 A | 5.00 A |

Output ripple was higher in SPICE (4.5 mV against 2.8 mV) because it includes
capacitor ESL, which the closed form omits. The input RMS check only matches
when the source has realistic impedance; an ideal source supplies the ripple
current itself and hides the capacitor stress.

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
