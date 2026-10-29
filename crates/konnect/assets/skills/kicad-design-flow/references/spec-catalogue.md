# Specification Catalogue

The requirement lists the assistant starts from in stage 1, per circuit class. Each row
gives a default **test condition** and a **proposed pass criterion**.

## How to use the proposals

- They are starting points only. They follow common power-electronics practice (vendor
  application notes, typical datasheet test conditions), not a standard or a textbook.
  In the record they carry origin `assumed`, and the rationale says why the value fits
  this design.
- A number the user gave replaces the proposal, with origin `user`.
- A number from a standard (e.g. CISPR 32 class B) or from a datasheet uses origin
  `standard: <doc>` or `datasheet: <doc>`.
- Fill every placeholder from the request: `Vin` range, `Vout`, `Iout,max`, ambient
  temperature `Ta,max`.
- Mark the condition corners: minimum, nominal and maximum input; zero, typical and
  maximum load; minimum and maximum ambient.

The verification column names how stage 5 will check each item: the `kicad-simulation`
method and the bench test.

## 1. Power converters (DC/DC, LDO, regulators)

### Core requirements

These are always written.

| ID | Category | Parameter | Default test condition | Proposed pass criterion | Verification |
|---|---|---|---|---|---|
| R-01 | Input range | Input voltage over which every other requirement holds | Steady state, Vin,min to Vin,max, Iout 0 to Iout,max | Regulates and meets R-02 to R-09 over the whole range; survives the absolute maximum input (state it, e.g. Vin,max + 20 % for 100 ms) without damage | Closed-loop sweep (`converter`, `scenarios`); bench sweep with a programmable supply |
| R-02 | Output accuracy | Vout DC set-point, total error | All Vin, Iout 0 to Iout,max, Ta,min to Ta,max; includes reference tolerance, divider tolerance, line and load regulation | Within ±2 % of nominal Vout (±3 % if the load allows; ±1 % for precision loads) | `budget` worst case plus `sim_corner_sweep`; DMM at the output connector over the corners |
| R-03 | Output ripple | Vout peak-to-peak at the switching frequency | Vin,min/nom/max, Iout,max (and light load); measured at the output capacitor with 20 MHz bandwidth, tip-and-barrel | ≤ 1 % of Vout pp (e.g. ≤ 50 mVpp at 5 V), or the load's own limit | Switched SPICE or `converter` steady state; `figures.ripple_figures`; scope |
| R-04 | Load transient | Vout deviation and recovery for a load step | Step 25 % to 75 % of Iout,max and back, at 1 A/µs (or the load's real slew), Vin nominal and extremes | Deviation ≤ ±5 % of Vout; back within the R-02 band in ≤ 100 µs (or 10 switching cycles at low frequency); no ringing beyond two overshoots | `scenarios` load step (closed loop, matched controller); electronic load step |
| R-05 | Startup | Soft-start time, overshoot, monotonicity, inrush | Enable or Vin applied with a 1 ms ramp at Vin,min and Vin,max; Iout 0 and Iout,max; also into a pre-biased output (50 % Vout) if the load can pre-bias | Monotonic rise; soft-start 1–10 ms; overshoot ≤ 5 % of Vout; input inrush ≤ the source's current limit; no reverse sinking of a pre-biased output | `scenarios` start-up; scope on Vout, EN, Iin |
| R-06 | Loop stability | Loop-gain crossover, phase and gain margin | Vin,min and Vin,max; Iout light and maximum; Ta extremes for the capacitor ESR and C spread | Phase margin ≥ 45°; gain margin ≥ 10 dB; crossover ≤ fsw/10 (≤ fsw/5 for fixed-frequency current mode, if the vendor allows); for internally compensated parts, inside the vendor's stated output-filter range | Loop gain by injection (`converter`); network analyser with an injection transformer |
| R-07 | Efficiency | Pout/Pin | Vin nominal and maximum; Iout 10 %, 50 %, 100 %; Ta 25 °C | ≥ the value the thermal budget needs (e.g. ≥ 90 % at full load for a 5 V/5 A buck from 24 V); at light load per the application (standby budget) | `losses` budget from datasheet values; power analyser or four-wire DMMs |
| R-08 | Thermal | Temperatures of the IC junction, inductor, capacitors and board | Iout,max continuous, worst Vin for losses, Ta,max, stated airflow (still air if unknown), the real board | Tj ≤ Tj,max − 25 °C (e.g. ≤ 125 °C for a 150 °C part); inductor and capacitors ≤ their rated temperature minus 10 °C; board ≤ 105 °C (FR-4 Tg margin) | `losses` + `sim_board_thermal`; thermal camera after a 30 min soak |
| R-09 | Short-circuit protection | Behaviour with the output shorted | Hard short (< 10 mΩ) at the output, applied in operation and at start-up; Vin,max; Ta,max; duration ≥ 1 min or continuous | No damage and no part beyond its ratings; peak switch and inductor current ≤ the inductor saturation and IC limits; average input power bounded (hiccup or fold-back); automatic recovery within one soft-start after the short is removed | `scenarios` fault cases; `sim_inductor_check` at the current limit; bench short with a MOSFET switch |

### Recommended additional requirements

Propose these; the user keeps or removes them.

| Category | Parameter | Default test condition | Proposed pass criterion |
|---|---|---|---|
| Line transient | Vout deviation for an input step | Vin,min to Vin,max in 100 µs, Iout,max | Deviation ≤ ±3 % of Vout |
| Undervoltage lockout | Turn-on and turn-off input thresholds | Slow Vin ramp up and down | On below Vin,min with hysteresis ≥ 5 % of Vin; no chattering at the thresholds |
| Light-load behaviour | Mode (PFM/pulse skipping/FPWM), ripple, quiescent current | Iout 0 to 10 % | Ripple within R-03 or a stated light-load limit; input current ≤ the standby budget |
| Input ripple and EMI | Input ripple current and conducted emission | Iout,max, Vin nominal; LISN per the standard | Below the standard's limit (e.g. CISPR 32 class B) with the project margin (`kicad-simulation/references/spec-and-emc-estimates.md`) |
| Overvoltage and reverse input | Output OVP, input reverse polarity, surge | Per the application | No damage; output ≤ the load's absolute maximum |
| Pin stress | Switch-node, BOOT and VIN pin voltage | Vin,max, Iout,max, real edge rate | Every pin within its absolute maximum with ≥ 10 % margin, durations per the datasheet (`stress`) |
| Environment | Ambient range, altitude, humidity | — | Stated range, e.g. −20 to +60 °C |
| Size, cost, assembly | Board area, height, BOM cost, assembler | — | As the user states, e.g. JLCPCB assembly with basic parts preferred |

## 2. Analog signal chain (sensor front end, amplifier, filter, ADC driver)

| Category | Default test condition | Proposed criterion (assumed) |
|---|---|---|
| Gain and gain accuracy | Full temperature range, resistor tolerances | Gain error within the system error budget, e.g. ≤ 0.5 % |
| Offset and drift | Inputs shorted, Ta range | Referred to the input, below 1/4 of the LSB or the resolution needed |
| Bandwidth and filter response | Small signal, −3 dB point and stop-band at the ADC Nyquist frequency | −3 dB ≥ the signal band; aliasing attenuation ≥ the ADC's dynamic range needs |
| Noise | Integrated over the band | Below 1/2 LSB rms, or the stated SNR |
| Input range and protection | Overvoltage on inputs, ESD | No damage at the stated overvoltage; IEC 61000-4-2 level if the input is external |
| Supply rejection | Ripple from R-03 of the supply | Output error from supply ripple below the error budget |

## 3. Digital and MCU boards

| Category | Default test condition | Proposed criterion (assumed) |
|---|---|---|
| Power rails | Each rail: tolerance, ripple, target impedance | Per the IC datasheets; PDN target impedance per `spec-and-emc-estimates.md` §2 |
| Clocks | Crystal load, accuracy, start-up | Frequency error within the interface's need (e.g. USB full speed allows ±0.25 %) |
| Interfaces | Each bus: impedance, length, termination | Interface standard limits; controlled impedance ±10 % |
| Reset and brown-out | Supply ramp and dips | Clean reset; no lock-up |
| Programming and debug | Connector, access | Programmable on the assembled board |
| ESD and EMC | Connectors exposed to the user | IEC 61000-4-2 level; emission class |

## Writing a good requirement

- **Measurable:** a quantity, a limit and a unit. "Low ripple" is not a requirement.
- **Conditioned:** every corner that changes the answer is in the test condition.
- **Verifiable twice:** name the simulation and the bench test now, so stage 5 knows what
  to run.
- **Justified:** the rationale of an assumed limit says what drives it, such as the
  load's tolerance, a downstream ADC or the thermal budget.
