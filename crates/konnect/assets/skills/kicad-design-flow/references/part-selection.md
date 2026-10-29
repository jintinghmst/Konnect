# Part Selection

How the assistant chooses parts in stage 2 and what it records for each. A choice is
only as good as the datasheet values behind it. Every key value names its document and
table, so the `kicad-simulation` checks can use it as sourced input later.

## Order of choices

1. **Topology and controlling IC.** Choose this first; it fixes most of the other parts.
2. **Power stage.** For a switching converter: inductor, output capacitors, input
   capacitors, and external switches or diodes.
3. **Setting parts.** Feedback divider, compensation, soft-start, frequency, enable and
   UVLO resistors, bootstrap capacitor.
4. **Protection and EMC.** Input filter, TVS, fuse, reverse-polarity protection,
   ferrites, ESD (`kicad-schematic/references/component-selection-emc.md`).
5. **Connectors and mechanical parts.**

## Controlling IC: criteria

| Criterion | Check against |
|---|---|
| Input range incl. transients, with margin to the absolute maximum | R-01, the pin-stress requirement |
| Output current and current limit (its minimum must cover Iout,max plus half the ripple; its maximum must stay within the inductor saturation current) | R-09, Iout,max |
| Reference accuracy over temperature | R-02 |
| Switching frequency (size versus loss; EMI bands; spread spectrum) | R-03, R-07, EMI |
| Control mode and compensation (internal compensation limits the output filter) | R-04, R-06 |
| Soft-start, pre-bias, power-good, enable | R-05 |
| Light-load mode (PFM or forced PWM) | Light-load requirement, R-03 |
| Protection: hiccup or latch, thermal shutdown, OVP | R-09 |
| Package thermal resistance on a real board (θJA from the datasheet's EVM, not JEDEC alone) | R-08 |
| Vendor model (SPICE or SIMPLIS) available for verification | Stage 5 evidence |
| Availability, price, assembler class (JLCPCB basic or extended), second source | Cost and assembly |

Compare at least two candidates. Record the losers with the reason they lost; that is
what lets the user overrule a choice.

## Power stage

| Part | Size from | Datasheet values to record |
|---|---|---|
| Inductor | Ripple ratio 20–40 % of Iout,max at Vin,max (as the IC's datasheet advises) | L and tolerance, Isat (state its definition, e.g. 20 % or 30 % drop), Irms for a 40 °C rise, DCR typical and maximum, core-loss data, shielding |
| Output capacitors | Ripple (R-03), transient (R-04), the loop's limits on ESR and C (R-06) | C, voltage rating, **effective C under DC bias** at Vout, ESR and ESL, ripple current |
| Input capacitors | RMS input current, input ripple, the hot loop | Same as above at Vin,max; ripple-current rating ≥ I_in,rms |
| Switches and diodes (external) | Voltage and current stress (R-09, pin stress) | V rating, RDS(on) or VF, Qg, Qrr, thermal resistance |

The `sim_inductor_check` and `sim_capacitor_check` tools take these values directly; a
value without a datasheet source leaves the dependent checks `INCOMPLETE`.

## Passives and general rules

- Use E-series values; state the tolerance needed for the R-02 budget (for example
  0.1 % feedback resistors when the reference already uses 1 %).
- MLCC dielectric X5R or X7R for power. Use C0G for timing and compensation. Apply
  voltage derating, and keep the count of distinct values low for assembly.
- Prefer one package size per function (0402 or 0603 passives, as the assembler and
  hand rework allow).
- Check the symbol and footprint exist (`search_symbols`, `search_footprints`) or note
  that the part needs a library entry (`kicad-library`).

## Sourcing record

For each part, record:

- the LCSC number and JLCPCB class (basic or extended), from `search_jlcpcb_parts` /
  `get_jlcpcb_part`, or the distributor and its stock;
- the date the stock was checked;
- for parts with a single source, a named alternative, or the reason none exists.

## The record (`sim_write_part_selection`)

| Field | Content |
|---|---|
| `role` | What the part does: "Buck converter IC", "Output capacitors (4×)" |
| `part`, `manufacturer` | The exact orderable part number |
| `key_values` | The values the design depends on, with units, from the datasheet |
| `requirements` | IDs the part serves |
| `reasons` | One reason per line, each tied to a requirement or constraint: "Isat 9.4 A > 6.6 A peak at the current limit (R-09)" |
| `alternatives` | "TPS54560: lower cost, but 5 A limit leaves no margin for R-09" |
| `datasheet` | Document number and revision, or URL |
| `sourcing` | As above |
| `references` | Planned designators |
| `risks` | What could still change the choice: DC-bias derating not yet confirmed, a long lead time |

Put the sizing equations and their results in `notes` (switching frequency, L, C_out,
C_in, divider, compensation), so the user can follow how the values were reached.
