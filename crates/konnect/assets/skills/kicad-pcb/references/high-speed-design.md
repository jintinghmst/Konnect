# High-Speed Digital Design

Read this before placing or routing any interface faster than ordinary GPIO, and
before choosing a stackup for one. It replaces the earlier scattered high-speed
notes. The guidance is condensed and paraphrased from two sources; consult them
for the full reasoning:

- Sheridan Technologies, "Signal Integrity: A Practical Guide for High-Speed
  Design" (sheridantech.io/2026/07/30/signal-integrity/), retrieved 2026-10-16.
- KYOCERA, "Build-up Structure FC-BGA, Organic Package"
  (global.kyocera.com/prdct/organic/prdct/package/fcbga/std/), retrieved
  2026-10-16.

Neither source gives impedance targets, trace widths, length-matching
tolerances, via sizes or termination values. Those come from the interface
specification, the component datasheets, the ordered stackup and a field solver
(see [`trace-width-table.md`](trace-width-table.md)). A missing input makes the
decision `INCOMPLETE`; do not substitute a remembered number.

## 1. Decide whether a net is a transmission line

A trace behaves as a transmission line once its edge rate is fast compared with
its propagation delay on the board. Judge by **rise time, not clock frequency**:

- a 1.0 ns edge has a critical length of roughly 3 in (about 75 mm);
- a ~75 ps edge (a 5 GHz-class signal) has a critical length of roughly 1/4 in
  (about 6 mm);
- effects start to matter above a few tens of MHz, and above about 100 MHz every
  signal needs SI-aware design.

Treat any interface faster than ordinary GPIO as a channel to be designed, not a
connection to be drawn. Record which nets qualify and why.

## 2. Treat SI as an architecture decision

- Settle the stackup, reference-plane assignment and channel budget before
  layout. Fixing SI after routing is frozen is the expensive path.
- Own the whole channel: driver, package transition, vias, connectors, cable and
  receiver, plus the access points needed to measure it.
- Account for effects from other disciplines: firmware switching behaviour,
  mechanical return paths, and manufacturing substitutions of dielectric or
  connectors.
- Tie evidence to milestones: early on, show the architecture supports the edge
  rates; after routing, show the routed design meets the channel budget; before
  release, show the tested configuration matches the released one.

## 3. The failure mechanisms and their first fixes

| Mechanism | Cause | Fix first |
|---|---|---|
| **Reflection** | Abrupt impedance change: connectors, via stubs, geometry changes, wrong termination | Termination, return-path continuity, channel modelling. Not cosmetic rerouting |
| **Crosstalk** | Long parallel runs, tight spacing, victims beside aggressors, detoured return current | Less coupling, shorter parallel runs, continuous reference plane, move victims to a quieter layer. Spacing is one knob, not the whole fix |
| **Power/ground noise** | Simultaneous switching outputs | Bypass capacitors close to the power pins they serve; planes able to deliver the transient current |
| **Loss** | Dielectric and conductor loss | Closes the eye vertically; budget it per channel |
| **Jitter** | Intersymbol interference, crosstalk and supply noise together | Consumes horizontal eye margin; treat the sources jointly |
| **Fibre-weave skew** | Glass-weave pattern under one pair member | Matters when the timing budget is tight |

## 4. Layout rules

- **Controlled impedance:** route on layers with an assigned, continuous
  reference plane. Width and gap come from the solved stackup, written into a
  project netclass (`create_netclass`, `assign_net_to_class`).
- **Return path before neatness:** never route a fast net across a plane split,
  an anti-pad void, or a connector that breaks the reference. Every layer change
  needs a nearby return path for the reference plane change as well as the
  signal.
- **Layer choice:** route sensitive signals on internal layers next to their
  reference planes.
- **Length and count:** keep fast traces short; minimise via count and
  impedance mismatches along the route.
- **Spacing (3W):** keep clocks and other sensitive nets at least three trace
  widths apart from neighbours. Apply it through netclass clearance, then confirm
  with DRC.
- **Aggressor/victim separation:** separate fast-switching nets from sensitive
  ones by distance, layer, or both.
- **BGA escape:** extra vias or a layer change are acceptable when the return
  path is preserved.
- **Bends:** 90° corners cause a small discontinuity that is usually negligible
  at common GHz rates; spend the effort on stackup and return path instead.
  Revisit only for the fastest links (the Sheridan guide flags 56G NRZ).
- **Test points:** pads, vias and stubs change impedance. Keep them few and
  plan them during architecture, not after routing.

## 5. Termination

Choose series, parallel or AC termination from driver behaviour, receiver
sensitivity and edge rate. Review termination at schematic stage, before layout.

## 6. Konnect tool support today

| Need | Tool | Limit |
|---|---|---|
| Read the stackup (thickness, εr, loss tangent, copper, finish) | `get_board_stackup` | Live board only; read-only. Change the stackup in KiCad Board Setup |
| Encode widths, gaps, clearances | `create_netclass`, `assign_net_to_class`, `set_layer_constraints` | No differential-pair gap field in netclasses |
| Route a pair | `route_differential_pair` | Two parallel segments at a fixed gap; no coupled routing or length tuning |
| Inspect lengths | `query_traces` | Raw segments; sum them per net yourself |
| Vias | `add_via` | Through vias only (drill, pad). No blind/buried/micro vias or backdrill |
| Check rules | `run_drc` | Enforces only the values written to netclasses and the custom DRC rules file |
| Hand off to an external solver | `export_ipc2581`, `export_odb` | |

Konnect has no impedance solver, S-parameter or eye analysis. Report any SI
verdict that would need one as `INCOMPLETE` and name the missing analysis.

## 7. Verification workflow

| Stage | Evidence |
|---|---|
| Architecture / pre-layout | Stackup review, channel budget, impedance targets |
| Architecture / schematic | Termination review, pre-layout simulation (IBIS, IBIS-AMI) |
| Layout | Routed-rules check, post-layout extraction |
| Layout | Crosstalk analysis, eye-diagram margin |
| Schematic and layout | PDN review, lab waveform stability |
| Architecture / test | Bring-up plan and debug-access map |

Measurement and simulation methods:

- **TDR** locates impedance discontinuities.
- **VNA S-parameters**, including mixed-mode, show insertion loss, return loss
  and coupling.
- **Eye diagrams and single-pulse response** show usable timing and amplitude
  margin at the receiver.
- **IBIS / IBIS-AMI models** let the channel be simulated before hardware. A
  marginal prediction is a design problem, not a lab problem.
- **Probing:** measure into a terminated load where possible; otherwise use an
  active high-impedance probe as close to the transmitter as possible.

## 8. Change control

The verified channel belongs to one configuration. A different laminate,
stackup, connector or supplier is a technical change that may need re-analysis
or retest. Keep an evidence package (channel requirements, simulation results,
measurements from the tested build, and the traceability between them) and
check that production matches the qualified build.

## 9. Package transitions: FC-BGA substrate capability snapshot

The package substrate is part of the channel. When a high-speed device uses a
flip-chip BGA, its substrate geometry is far finer than the PCB's, so the
package-to-board transition (bump, substrate via, ball, PCB pad and escape via)
is a likely discontinuity to model.

Kyocera's published capability for its build-up FC-BGA organic substrates
(retrieved 2026-10-16; the vendor states specifications may change without
notice):

| Item | Published capability |
|---|---|
| Layer structure | up to 10-n-10 |
| Build-up line width / space | 9 µm / 12 µm minimum |
| Via land diameter | 85 µm minimum |
| Core line width (subtractive) | 30 µm minimum |
| Core space (subtractive) | 45 µm minimum |
| Flip-chip pitch | 100 µm minimum |
| I/O count | more than 3,000 |
| Other | laser-formed vias; thermosetting epoxy; Ni/Au or SAC finishes; halogen- and lead-free options |

Use these figures only to understand what lies on the package side of the
transition. They are **not PCB design rules**: never write them into netclasses
or DRC. The page gives no dielectric constant, loss tangent, thickness or
impedance data, so a package model must come from the device vendor (package
S-parameters or IBIS package data). Missing package data makes the channel
analysis `INCOMPLETE`.
