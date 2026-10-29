# Specifications, PI/EMC Estimates and Pre-Compliance

Read this when writing a design specification, setting a power-integrity target,
or estimating emissions, crosstalk or filter attenuation before hardware. It
turns the textbook relations into checks for this skill's evidence rule: every
estimate below is a model, so its check is `INCOMPLETE` until its inputs are
sourced and, for EMC, until a measurement confirms it.

Sources (SI/PI Assistant corpus; pages are the corpus PDF pages): **Ott**,
*Electromagnetic Compatibility Engineering* (2009); **DSI**, *Digital Signal
Integrity: Modeling and Simulation with Interconnects and Packages*; **Hall &
Heck**, *Advanced Signal Integrity for High-Speed Digital Designs*; **Paul**,
*Analysis of Multiconductor Transmission Lines* (2e).

## 1. What a specification records

Write it before the schematic, because early fixes are the cheap ones (Ott
ch. 1, p. 29), and keep every limit with its source:

| Section | Contents |
|---|---|
| Functional | Rails with tolerance and ripple limits; signal levels and interfaces; operating ranges |
| Emissions | Standard and class (e.g. CISPR 32 class B), conducted and radiated limit lines, measuring distance |
| Immunity | ESD, radiated and conducted immunity, EFT, surge levels, where they apply |
| Power integrity | Target impedance per rail over frequency; allowed ripple at each IC |
| Signal integrity | Impedance targets with tolerance, skew, crosstalk limits per critical pair, reflection limits |
| Margins | The project's design margin to each regulatory limit. The books give no figure; this skill treats a modelled EMI margin under about 6 dB as a fail risk |
| Verification | The simulation and pre-compliance test for each item, with pass/fail criteria |

A limit the user did not give is **assumed** (`SKILL.md` step 1).

## 2. PDN target impedance

- Z_t = k·dV/dI, dV the allowed transient deviation and dI the transient current;
  k = 2 up to 1/(π·tr) because about half the current spectrum lies below that
  frequency, then Z_t may rise at 20 dB/decade (Ott §11.4.5, Eq. 11-8,
  Fig. 11-17, p. 469; example: 5 V ± 5 %, 2.5 A, 2 ns → 200 mΩ to 159 MHz).
- Or Z_t = x·V_DD / I_avg with x the allowed ripple fraction, typically 5–10 %
  (DSI §11.4.1, p. 422).
- The impedance seen by the IC must stay below Z_t over the band. Parallel equal
  capacitors lower it; widely different values add anti-resonance peaks (Ott
  §11.4.3–11.4.4). For the transient, the capacitance must also meet
  C ≥ dI·dt/dV (Ott Eq. 11-12).

**Check with `sim_spice_run`:** drive the rail node with a 1 A AC current so its
voltage magnitude equals the impedance. Every R, L and C comes from the
capacitor models, `sim_layout_calc` (mounting loop) and the regulator datasheet;
the values below are placeholders.

```spice
* PDN impedance at the IC: |V(ic)| in volts = |Z| in ohms
I1 0 ic AC 1
* 4 x 100 nF decoupling in parallel (C, ESR, ESL + mounting from sources)
Cd ic d1 400n
Rd d1 d2 2.5m
Ld d2 0 0.2n
* 10 uF bulk behind plane inductance
Lp ic b0 0.5n
Cb b0 b1 10u
Rb b1 b2 5m
Lb b2 0 1n
* regulator output (from its datasheet output impedance)
Lr ic r1 20n
Rr r1 0 5m
.ac dec 50 1k 1g
.end
```

Measure `max` of vector `ic` over the band and compare with Z_t in
`sim_evaluate_check`.

## 3. Conducted emissions of a switching converter

Ott's envelope model (§13.2, pp. 523–528), V in volts, F0 the switching
frequency in Hz:

| Mode | Level at the fundamental (flat to 1/(π·tr)) | Above 1/(π·tr) | Set by |
|---|---|---|---|
| Common mode | V_CM = 100·V_P·F0·C_P (Eq. 13-2) | −20 dB/decade | Switched voltage V_P and the parasitic capacitance C_P from the switching node to chassis or earth |
| Differential mode | V_DM = 2·F0·L_F·I_P (Fig. 13-12) | −20 dB/decade | Peak switch current I_P and the input capacitor's ESL L_F; below about 500 kHz its ESR governs instead |

- Common mode dominates when V_P > L_F·I_P / (50·C_P) (Eq. 13-7): high-voltage,
  low-current supplies are common-mode limited; low-voltage, high-current ones
  differential-mode limited.
- Slowing the edge moves the breakpoint down but does not lower the flat level
  (p. 523).
- Required filter attenuation = estimated level − limit + margin (Ott's examples:
  26 dB over the limit → ≥ 30 dB of filter).

**In Konnect:** `sim_conducted_emissions` computes the differential-mode
spectrum at the LISN from a simulated input-current waveform, which is better
than the envelope; use the envelope as the independent second method.
Common-mode emission needs C_P, which no board tool extracts: state it as
unmodelled. The filter's output impedance must stay below the converter's
negative input impedance (Ott ch. 13, p. 547): `sim_input_network`.

## 4. Radiated emission estimates

**Differential mode (loop):** E = 263·10⁻¹⁶ · f² · A · I / r with E in V/m, f in
Hz, A in m², I in A, r in m, over a reflecting ground (Ott Eq. 12-2, p. 489).
Solved for the largest allowed loop in practical units:
A = 380·E·r / (f²·I) with E in µV/m, f in MHz, I in mA, A in cm² (Eq. 12-4,
p. 491). Example: 25 mA at 30 MHz in 10 cm² gives 197 µV/m at 3 m, about twice
the class B limit of 100 µV/m; the allowed loop is 5 cm². For non-sinusoidal
currents, apply it to each harmonic of the Fourier series (§12.1.3).
Radiation grows with f², so harmonics matter.

**Common mode (cables):** I_CM = 0.8·E·r / (f·l) with E in µV/m, I_CM in µA, f in
MHz, r and l in m (Eq. 12-8, p. 502). A 1 m cable at 50 MHz may carry only about
5 µA for FCC class B at 3 m (15 µA class A at 10 m; Table 12-1). Most
common-mode cable emission falls in 30–300 MHz for 1–10 ns edges (p. 502).

Loop area A comes from the board (`sim_layout_calc` with the loop's points);
the current from simulation or measurement. These are estimates for design
direction, never compliance evidence.

## 5. Crosstalk

- Weakly coupled, electrically short lines: near- and far-end crosstalk are
  each an inductive plus a capacitive contribution proportional to frequency
  (time domain: to dV/dt of the aggressor) (Paul §10.1–10.2, Eqs. 10.29, 10.58).
  Inductive coupling dominates low-impedance terminations, capacitive coupling
  high-impedance ones (Fig. 10.3).
- Far-end crosstalk ideally cancels in a homogeneous dielectric (stripline), not
  in microstrip (Paul Eq. 10.55).
- Crosstalk ∝ h² at fixed spacing (Ott Eq. 10-15).
- A trace crossing a plane gap couples through the slot far more strongly than
  ordinary crosstalk (Hall & Heck §10.2.2).

Specify limits per aggressor/victim pair in mV or as a fraction of the swing.
Estimate with `sim_trace_coupling`; a full coupled-line analysis needs a field
solver, which Konnect lacks, so a tight result is `INCOMPLETE`.

## 6. Other simulation set-ups

- **Filter insertion loss** (`sim_spice_run`, AC): source and load impedances of
  the real circuit (a 50 Ω LISN per line for conducted emissions), the filter
  parts with their parasitics (capacitor ESL/ESR, the bead's R and L from its
  impedance curve at the DC current). Insertion loss = 20·log(V_out without
  filter / V_out with filter). A ferrite helps only if it adds more than the
  source plus load impedance (Ott p. 252).
- **Transmission lines** (`sim_spice_run`, transient): ngspice's lossless
  `T` element with Z0 and TD, or a lossy line model, from the solved stackup.
  A lumped LC model needs segments no longer than tr·c/(10·√εr) (Hall & Heck
  Eq. 3-22a). Bandwidth of an edge: about 0.35/tr (DSI §1.4.1). Terminations
  per `kicad-schematic/references/component-selection-emc.md` §5.
- **RFI filter at a sensitive input:** series 50–100 Ω, shunt a few ohms at the
  frequency of interest; 62.8 Ω with 1.6 Ω gives about 32 dB into a high-
  impedance load (Ott §14.2).

## 7. Pre-compliance plan

Workbench measurements catch problems early; EMC labs report 85 % of products
fail final compliance the first time, and early workbench testing can reverse
that (Ott ch. 18, pp. 711–712). A full set-up including a spectrum analyser cost
under USD 30 000 in 2008 (p. 711).

| Test | Method | Pass criterion |
|---|---|---|
| Conducted emission | LISN and spectrum analyser | Below the spec limit with the project margin |
| Cable common-mode current | Clamp current probe on every cable, ≥ 1 m long | < 5 µA class B (15 µA class A) per Table 12-1; shorter cables allow proportionally more (Ott §18, p. 713). With a 5 Ω-transfer-impedance probe, 5 µA reads 28 dBµV (Eq. 18-1) |
| Board hot spots | Near-field probes over the board, clocks and switching loops (Ott §18.2: probes, not antennas, on the bench) | Qualitative; compare before and after each change |
| Loop stability, ripple, thermal | Bode injection, scope, thermal camera | Spec limits |

Do not measure radiated emission in an unlined shielded room: wall reflections
cause errors as large as 40 dB (Ott §18.1, p. 712).

Schedule: simulate PDN, filters and crosstalk at schematic freeze; near-field
scan, LISN and cable currents on the first prototype; repeat after each layout
revision; formal pre-compliance on the pre-production build.

## 8. Specification checklist

- [ ] Every rail has tolerance, ripple limit and target impedance with its source
- [ ] Emission standard, class and limits cited; project margin stated
- [ ] Immunity levels stated where they apply
- [ ] Crosstalk and impedance limits for each critical net or pair
- [ ] Each requirement mapped to a simulation (tool and method) and a bench test
- [ ] Estimates labelled as such; their checks `INCOMPLETE` until measured
