# EMC at Schematic Stage

Read this before drawing power distribution, decoupling, power-entry or
cable-interface circuitry, and before any mixed-signal (ADC/DAC) design. It
condenses the textbook guidance that decides the schematic; layout follows in
`kicad-pcb/references/emc-layout.md` and part choice in
[`component-selection-emc.md`](component-selection-emc.md).

Sources, from the SI/PI Assistant corpus (cite them when the guidance decides a
design question; pages are the corpus PDF pages):

- **Ott**: H. W. Ott, *Electromagnetic Compatibility Engineering*, Wiley 2009.
- **DSI**: *Digital Signal Integrity: Modeling and Simulation with Interconnects
  and Packages*.

The numbers below are design starting points from those books. A value that
governs a check still comes from the part's datasheet or the design's spec; a
textbook rule of thumb is not a sourced input, so a check that rests on one stays
`INCOMPLETE`.

## 1. Design EMC in, not on

Mitigation options shrink and cost rises as a product moves from design to test
to production; considered stage by stage at design time, the fixes are usually
simple, and Ott reports that a design handled this way can have 90 % or more of
its potential problems removed before initial testing (Ott ch. 1, Fig. 1-1,
p. 29). So decide filtering, grounding and decoupling in the schematic, where
they cost a part, not in the lab.

Two layout rules drive schematic decisions (Ott ch. 16–17):

1. Return current must flow in the smallest possible loop.
2. A system should have one reference (ground) plane.

## 2. Grounding and mixed-signal partitioning

- **One ground.** Use a single ground plane, partitioned into analog and digital
  regions rather than split into separate planes (Ott ch. 17; answer to
  Problem 17.4: "almost always better to have only one single reference plane").
- **AGND/DGND pins.** On A/D and D/A converters and most mixed-signal ICs, the
  labels say where the pins connect *inside* the IC, not outside. Connect both to
  the analog ground and decouple to it (Ott §17.3, pp. 688 and 693). Exception: large
  DSPs and codecs with much digital circuitry: AGND to analog ground, DGND to
  digital ground, unless the datasheet says otherwise (same page).
- **Digital supply of a mixed-signal IC.** Decouple it to analog ground with the
  capacitor connected directly to the DGND pin, and isolate the supply with a
  small series impedance (ferrite bead or resistor) whether it is fed from the
  analog or the digital rail (Ott §17.9.2, Fig. 17-21, p. 705).
- **Power entry and regulators.** Bring power in on the digital side, where the
  large currents are, and place the analog regulator or filter so it straddles
  the partition, like the converters (Ott §17.9, Fig. 17-20, p. 705).
- **Net names** carry voltage and domain (`+3V3_A`, `+3V3_D`), so the layout
  partition can be read from the netlist.

## 3. Decoupling networks

Decoupling places an L–C network, not a capacitor, next to the IC; above its
series resonance it is inductive (Ott Eq. 11-5, p. 456).

- **Value (low frequencies).** The total capacitance must (1) stay below the
  target impedance at the lowest frequency of interest and (2) supply the
  switching transient: C ≥ dI·dt/dV (Ott Eq. 11-12, p. 480; 500 mA for 2 ns
  within 0.1 V needs ≥ 0.01 µF).
- **Package (high frequencies).** Above resonance only the inductance matters.
  Use the smallest practical package and the largest value available in it; a
  1206 MLCC is about 1.2 nH, an 0603 about 0.6 nH (Ott, text after Eq. 11-12, p. 480).
- **Many equal values beat a spread of values.** Equal-value capacitors in
  parallel lower the inductance over a wide band (Ott §11.4, Fig. 11-13, p. 463).
  Values an order of magnitude or more apart create anti-resonance peaks between
  their resonances; Archambeault's measurements showed up to 25 dB more noise in
  50–200 MHz (Ott §11.4.3–11.4.4, pp. 463–467). Values within about 2:1, or the
  same nominal value with tolerance spread, are not a problem (p. 466).
- **Bulk capacitance.** Larger than the sum of the IC decoupling capacitors it
  recharges; one where power enters the board and others around it; typically
  5–100 µF (10 µF typical), MLCC or tantalum, small ESL. Ott advises against
  aluminium electrolytics for this board-level bulk decoupling role (inductance
  an order of magnitude above tantalum), and notes a little ESR helps damp
  resonances (Ott §11.8, p. 483).
- **Target impedance.** Size the network against Z_t = k·dV/dI (Ott §11.4.5,
  Eq. 11-8; k = 2 below 1/(π·tr)), or Z_t = x·V_DD/I_avg with x the allowed
  ripple fraction, typically 5–10 % (DSI §11.4.1). Verify with an impedance
  sweep (`kicad-simulation`, reference `spec-and-emc-estimates.md`).

Switching regulators are different: their input and output capacitors are set
by the regulator datasheet's ripple, loop and RMS-current requirements. Use the
rules above for digital and analog IC decoupling, and the datasheet for the
converter.

## 4. Power-entry filters

Every power input should carry a filter so the board's high-frequency transient
currents stay on the board and outside noise stays off it (Ott §11.9,
Fig. 11-26, p. 483):

- **Differential mode:** a π-filter, capacitors 0.01–0.1 µF with a ferrite bead
  of 50–100 Ω across the band of interest, or an inductor of 0.5–5 µH. The bead
  must not saturate at the DC current.
- **Common mode:** a common-mode choke on the board or a ferrite core on the
  power cable.
- A converter behind the filter has a negative input impedance within its loop
  bandwidth; the filter's output impedance must stay below it in magnitude or
  the combination can oscillate (Ott ch. 13, p. 547). Check it with
  `sim_input_network`.

## 5. RFI filters on sensitive inputs

Protect low-level and cable-connected inputs with a low-pass filter at the
device pin (Ott §14.2, Fig. 14-2, p. 573):

- series element 50–100 Ω at the frequency of interest: a resistor where the DC
  drop is acceptable, a ferrite bead above about 30 MHz, an inductor below about
  10 MHz with f·L ≈ 10 (µH·MHz) for 62.8 Ω;
- shunt capacitor a few ohms or less: f·C ≈ 0.1 (µF·MHz) for 1.6 Ω, e.g.
  1000 pF at 100 MHz (Eq. 14-6, Table 14-1); 62.8 Ω with 1.6 Ω gives about
  32 dB;
- the shunt capacitor works only with near-zero lead and trace inductance.

## 6. ESD entry protection

- A transient suppressor (TVS) or filter capacitor on a cable input diverts the
  ESD current to the chassis, away from the circuit; the filter at the sensitive
  device itself goes to circuit ground (Ott §15.5, Fig. 15-16, pp. 625–632).
- ESD filters need at least 40 dB in 100–500 MHz: shunt 100–1000 pF, series
  50–100 Ω resistor or ferrite (Ott §15.5, p. 625).
- In an insulated enclosure connect suppressors, filters and cable shields to a
  separate I/O ground plane (better) or a metal ESD plate under the board (best),
  not to the circuit ground plane (poorest) (Ott §15.5.3, p. 626).
- A **soft ground** is 100–1000 Ω between the ESD entry point and ground,
  limiting the discharge current and its magnetic field (Ott, p. 632).

## 7. Annotate for layout

Write each layout constraint into the schematic as a text note, so it reaches
the layout step: which capacitors belong to which pins and must sit beside them,
where the partition line runs and which nets may cross it, which filter parts
sit at which connector, and which ground (chassis, I/O, circuit) each protection
part returns to.

## 8. Schematic EMC checklist

- [ ] One ground reference; mixed-signal AGND/DGND connected per §2 (datasheet
      exceptions noted)
- [ ] Every IC supply pin has decoupling; values from the datasheet or sized by
      C ≥ dI·dt/dV, smallest practical package, few distinct values
- [ ] Bulk capacitance at the power entry and larger than the decoupling it serves
- [ ] Power-entry filter with differential- and common-mode sections; bead
      saturation checked at DC current; filter vs converter input impedance checked
- [ ] RFI filters on low-level and cable-connected inputs
- [ ] ESD/TVS parts at the connectors, returned to chassis or I/O ground
- [ ] Nets named with voltage and domain; layout constraints annotated
- [ ] ERC clean
