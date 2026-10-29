# Component Selection for EMC, SI and PI

Read this when choosing or substituting decoupling and bulk capacitors, ferrite
beads, common-mode chokes, power-line filter parts, ESD/TVS parts or termination
resistors. It gives the selection criteria from the textbooks; the governing
numbers (capacitance under bias, impedance curve, saturation, ratings) still come
from the exact part's datasheet. A selection checked only against these rules of
thumb is `INCOMPLETE` until the datasheet values are recorded.

Sources (SI/PI Assistant corpus; pages are the corpus PDF pages): **Ott**,
*Electromagnetic Compatibility Engineering* (2009); **DSI**, *Digital Signal
Integrity: Modeling and Simulation with Interconnects and Packages*.

## 1. Capacitors

### Dielectric and type

| Type | Ott's characterisation | Use |
|---|---|---|
| Mica and stable ceramics | Low ESR and ESL; useful to about 500 MHz with short leads, surface-mount to GHz; stable with time, temperature and voltage (§5.1.3, p. 222) | RF filters, timing, high-frequency bypass and digital decoupling |
| High-K ceramics | Medium-frequency only; unstable with time, temperature and frequency; can be damaged by voltage transients, so **not recommended as bypass capacitors directly across a low-impedance supply** (§5.1.3, p. 223) | Non-critical bypass, coupling, blocking |
| Film (polyester, polypropylene, polystyrene) | Medium-frequency, below about 1 MHz; polystyrene the most stable (Ott §5.1, p. 222) | Precision filters and timing |
| Aluminium electrolytic | Large capacitance; inductance an order of magnitude above tantalum (§11.8, p. 483) | Low-frequency bulk and damping; not as board-level HF decoupling |
| Solid tantalum | Fails short in normal use and on overvoltage (Table 5-1, p. 223) | Bulk, with voltage derating |

Ott does not map "high-K" to EIA codes. Treat it as the least stable class
(Y5V/Z5U-type dielectrics); X5R and X7R MLCCs are the normal choice for
decoupling and converter capacitors, but their capacitance falls with DC bias,
so take the effective value at the working voltage from the manufacturer's curve
(`sim_capacitor_check`).

### Self-resonance and value

- Every capacitor resonates with its series inductance and is inductive above
  that frequency. For a filter capacitor, the smallest value that does the job
  keeps the resonance highest (Ott §5.1.4, Table 5-2, p. 223); lead and mounting
  length lower it further.
- For decoupling, the opposite trade applies: the high-frequency impedance is set
  only by inductance, so take the smallest practical package and the largest
  value available in it (Ott, p. 480; 1206 ≈ 1.2 nH, 0603 ≈ 0.6 nH).
- Parallel two types to cover a wide band (e.g. a large-value capacitor with a
  small low-inductance ceramic; Ott §5.1.5, p. 225), but keep values within about
  2:1 or use many equal values, because values a decade apart produce
  anti-resonance peaks (Ott §11.4.3–11.4.4).

### Selection steps for decoupling

1. Minimum value from C ≥ dI·dt/dV (Ott Eq. 11-12) and from the target
   impedance at the lowest frequency of interest (Ott Eq. 11-8).
2. Smallest practical package; largest value in it; voltage rating with derating.
3. Few distinct values; many equal parts for a large IC.
4. Effective capacitance at DC bias and temperature from the datasheet curve.

## 2. Ferrite beads

- A ferrite is a high-frequency resistor: near zero impedance at DC, mostly
  resistive in its intended band; |Z| = √(R² + (2πfL)²), both frequency-dependent
  (Ott §5.7, Eq. 5-32, pp. 250–251). Use it where it is resistive.
- Effective mainly above 10 MHz, sometimes down to 1 MHz (p. 250); for RFI
  filters beads work best above about 30 MHz (§14.2 footnote, p. 573).
- **Material sets the band.** Choose from the manufacturer's recommended
  frequency range per material (Ott Fig. 5-25 shows Fair-Rite types 73, 43, 31
  and 61 spanning 1 MHz–2 GHz). Ott gives no MnZn/NiZn rule; read the
  manufacturer's chart.
- **It must add more impedance than the source plus load impedance** at the
  frequency of interest; most beads are a few hundred ohms at most, so they work
  best in low-impedance circuits (p. 252).
- **DC current.** Impedance falls as DC bias saturates the core; check the
  impedance curve at the operating current and the rated current with derating
  (Ott §11.9, p. 484: avoid saturating the power-entry bead).
- Multiple turns raise impedance as N² but add winding capacitance that lowers
  the high-frequency impedance; seldom more than two or three turns (p. 251–252).
- Check the DCR drop against the rail's regulation budget.
- Ferrite compositions differ between manufacturers, so multiple sourcing is
  difficult (p. 250): record the exact part.

## 3. Power-line filter parts

Ott's summary for switched-mode supplies (ch. 13, p. 563):

- **Common-mode attenuation** comes from the Y-capacitors and the common-mode
  choke; **differential-mode** from the X-capacitor and the choke's leakage
  inductance.
- Y-capacitor value is limited by the leakage-current requirement (Problems
  13.2, 13.9); X and Y parts need the safety rating for line connection.
- A filter works only as well as it is mounted: lead routing, magnetic coupling
  and ground inductance in series with the Y-capacitors can defeat it.
- For a board-level DC entry filter see `emc-schematic.md` §4 (0.01–0.1 µF,
  50–100 Ω bead or 0.5–5 µH; Ott §11.9).

## 4. ESD and transient parts

- TVS or capacitor at the cable entry, returned to the chassis or a separate I/O
  ground, not the circuit ground (Ott §15.5, Fig. 15-16).
- Filters for ESD need ≥ 40 dB in 100–500 MHz: shunt 100–1000 pF, series
  50–100 Ω (Ott, p. 625).
- Clamp voltage below the protected pin's absolute maximum at the expected surge
  current, standoff voltage above the normal working maximum (datasheet values;
  e.g. `sim_evaluate_check` with both sourced). Capacitance must suit the signal
  bandwidth (interface specification).

## 5. Termination resistors

| Termination | Value | Trade-off (DSI) |
|---|---|---|
| Series (source) | R_term = Z0 − R_driver; driver impedance must be below Z0 | Launches half the swing, which doubles at an open load; no static power; the reflection returns to the source, so multidrop and fast bus turnaround suffer (§2.2.3, §11.2.5) |
| Parallel (load) | R = Z0 | Clean signal, but the driver supplies static current (§2.2.2) |
| AC (R + series C) | R = Z0 | Removes the static power of parallel termination (§11.2, Fig. 11.5) |

The driver's output impedance comes from its IBIS model or datasheet; the line
impedance from the stackup solution (`kicad-pcb/references/trace-width-table.md`).

## 6. RFI series elements

Series resistor where the DC drop is acceptable; ferrite above about 30 MHz;
inductor below about 10 MHz with f·L ≈ 10 µH·MHz for 62.8 Ω (Ott §14.2,
Eq. 14-4). Shunt capacitor with f·C ≈ 0.1 µF·MHz (Eq. 14-6), mounted with
near-zero lead length.
