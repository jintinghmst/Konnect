# EMC Layout: Stackup, Return Paths, Decoupling and Crosstalk

Read this before choosing a stackup, placing decoupling or filter parts, pouring
planes, or routing any board with switching regulators, clocks, cables or mixed
analog and digital circuits. It complements
[`high-speed-design.md`](high-speed-design.md) (channel design for fast
interfaces) with the textbook physics of return current, radiation and
crosstalk. Schematic-stage decisions are in
`kicad-schematic/references/emc-schematic.md`.

Sources (SI/PI Assistant corpus; pages are the corpus PDF pages):

- **Ott**: *Electromagnetic Compatibility Engineering*, Wiley 2009.
- **Hall & Heck**: *Advanced Signal Integrity for High-Speed Digital Designs*.
- **Paul**: *Analysis of Multiconductor Transmission Lines*, 2nd ed.

Dimensions and dB figures below are the books' worked examples. They show the
direction and rough size of an effect; the numbers for this board come from its
ordered stackup, `sim_layout_calc`, `sim_trace_coupling` and the other `sim_*`
tools.

## 0. The question that decides every layout choice

**Where does the return current flow?** (Ott ch. 16 summary, p. 678.) High-frequency
return current flows in the plane directly under its trace, because that path
has the least inductance (Ott ch. 10 summary, p. 443). Two rules follow:

1. Keep every current loop small: a loop is a loop antenna (Ott §12.1).
2. Give each signal one continuous reference plane.

## 1. Stackup

Ott's six multilayer objectives (§16.4.2.1, p. 660):

1. A signal layer always adjacent to a plane.
2. Signal layers tightly coupled (close) to their planes.
3. Power and ground planes closely coupled.
4. High-speed signals buried between planes, which shield them.
5. Several ground planes, which lower the reference impedance and common-mode
   radiation.
6. A critical signal routed on more than one layer stays on two layers adjacent
   to the same plane.

Below eight layers, objectives 3 and 4 cannot both be met (footnote, p. 660).

**Four-layer boards** (signal, plane, plane, signal): put the signal layers as
close to the planes as possible (≤ 0.010 in) and use a thick core between the
planes (Ott Fig. 16-13, pp. 662–663). Ott's example, 0.005 in instead of
0.020 in, gives:

- about 12 dB less differential-mode (loop) radiation, because the loop area
  falls by four;
- about 35 % less plane inductance (0.13 → 0.085 nH/in), so slightly under 4 dB
  less cable common-mode radiation;
- crosstalk lower by 16 (24 dB), since at fixed spacing it scales with the
  square of trace height (Ott Eq. 10-15).

With normal construction the inter-plane capacitance does not decouple
effectively below about 500 MHz, so trading it for tight signal-to-plane
coupling costs little (p. 662).

A four-layer board radiates 20 dB or more less than a two-layer board, all else
equal; above about 10 MHz consider multilayer boards seriously (Ott §16.4.2,
p. 660).

KiCad holds the stackup in Board Setup: read it with `get_board_stackup` or
`sim_board_stackup` and record the fabricator's stackup (see
`trace-width-table.md`). Konnect cannot write it.

## 2. Return-path discontinuities

The three common ones are slots or splits in a plane, a signal changing layers
(its return changes planes), and plane cutouts at connectors or under ICs
(Ott §16.3, p. 649).

| Discontinuity | Effect | Layout response |
|---|---|---|
| Slot in a plane | Return current detours round it; a 1.5 in slot raised plane voltage five-fold (14 dB) in Ott's measurement, and slots can raise radiation by more than 20 dB (Table 16-1, pp. 649–651) | **Do not have slots.** A row of non-overlapping holes is harmless (Table 16-1) |
| Split plane (separate voltage areas) | Return current must find a decoupling capacitor to cross (Ott §16.3.2) | No traces across the split on adjacent layers; if unavoidable, stitching capacitors at the crossing (p. 663) |
| Trace crossing a gap | The gap acts as a slotline and couples to every other trace crossing it: about 20 % coupling measured for lines 1.4 in apart over a 25 mil gap (Hall & Heck §10.2.2) | Same as above; differential pairs are far less affected |
| Layer change | Return moves to another plane | A ground via (or stitching capacitor between unlike planes) next to the signal via |

Every pour must be tied to its net with vias; stitch ground pours to the ground
plane (`add_zone`, `add_via`). Check slots and splits with
`sim_copper_connectivity` on the ground net.

## 3. How far return current spreads

Return current concentrates under the trace (Ott §10.6, Figs. 10-10 and 10-13,
pp. 417–420; summary p. 443):

| Line | Return current within ± x of the trace centre |
|---|---|
| Microstrip | 50 % within ±1h, 80 % within ±3h, 97 % within ±20h |
| Stripline | 74 % within ±1h, 94 % within ±2h, 99 % within ±3h (Table 17-3) |

h is the trace height above its plane. Stripline confines the return best,
then asymmetric stripline, then microstrip. Keep fast digital traces 5–10 h from
an analog–digital partition and route them as stripline next to high-resolution
converters (Ott p. 697).

## 4. Decoupling and filter placement

- **Loop inductance, not distance, is the target.** Place each decoupling
  capacitor to give the smallest loop between it and the IC. Mounting alone
  spans 2.8 nH (thin traces, end vias) to 0.4 nH (side vias, several vias) for
  an 0805 (Ott §11.7, Fig. 11-25, pp. 481–482). Side vias beat end vias because
  opposite currents sit close; extra vias help but take space.
- Connect capacitor and IC directly to the planes, or to each other by short
  traces; either works when the loop is small (p. 481).
- One or two capacitors per IC: placement is critical. Many capacitors: spread
  them evenly round the IC. Global decoupling (many equal capacitors across the
  board straight to the planes) suits boards of similar ICs (p. 482).
- Bulk capacitor where power enters, others around the board (§11.8).
- Power-entry filter at the connector, before the power reaches the planes
  (§11.9).
- Cable filter and ESD parts at the connector, returned to the chassis or I/O
  ground. Keep the power plane out of the I/O ground area (Ott §12.4.3, p. 510).

Estimate a mounting loop with `sim_layout_calc` (kind loop_inductance) before
moving parts.

## 5. Switching regulators

- The critical loops are those with large di/dt; lay them out with minimum area
  (Ott §13.7, Fig. 13-28, p. 548). For a buck this is the input-capacitor to
  switch loop; `kicad-sim-optimize` reference `levers.md` and
  `sim_layout_calc` quantify it.
- Magnetic fields at these frequencies are hard to shield, so control them at
  the source (p. 548).

## 6. Mixed-signal boards

- One ground plane, partitioned into analog and digital regions; converters
  straddle the partition, with the analog regulator or filter (Ott ch. 17,
  Fig. 17-20).
- No digital trace in the analog region and no analog trace in the digital
  region, except the converter connections. One misrouted trace defeats the
  partition; autorouting a mixed-signal board usually fails (Ott §17.2, p. 688).

## 7. Crosstalk

- For weakly coupled, electrically short lines, crosstalk is the sum of an
  inductive part (mutual inductance) and a capacitive part (mutual capacitance),
  each proportional to frequency, so proportional to the aggressor's slew rate in
  the time domain (Paul §10.1–10.2, Eqs. 10.29, 10.58).
- Inductive coupling dominates with low-impedance terminations, capacitive with
  high-impedance ones (Paul Fig. 10.3). At low frequency, a shared return
  conductor adds common-impedance coupling (Paul Fig. 10.4).
- In a homogeneous dielectric (stripline) far-end crosstalk ideally cancels; in
  microstrip it does not (Paul Eq. 10.55).
- Levers: lower trace height (crosstalk ∝ h², Ott), more spacing, shorter
  parallel runs, stripline instead of microstrip, slower edges where the
  interface allows. Differential pairs reduce crosstalk only when pair spacing is
  generous; at equal board area they may not (Hall & Heck §7.2).
- Estimate a specific pair with `sim_trace_coupling`.

## 8. Layout EMC checklist

- [ ] Stackup meets Ott's objectives 1–2 at least; signal-to-plane spacing
      recorded from the fabricator's stackup
- [ ] No slots in ground planes; no trace crossing a split on an adjacent
      layer; stitching capacitors where a crossing is unavoidable
- [ ] A return via beside every layer change of a fast or high-current net
- [ ] Every pour connected and stitched; no isolated copper
- [ ] Decoupling loops minimal (side vias, short connections); loop inductance
      estimated for the critical ones
- [ ] Power-entry filter, bulk capacitor, cable filters and ESD parts at their
      connectors
- [ ] Switching-regulator high-di/dt loops minimal
- [ ] Mixed-signal partition respected; converters straddle it
- [ ] Crosstalk-critical pairs estimated or the check is `INCOMPLETE`
