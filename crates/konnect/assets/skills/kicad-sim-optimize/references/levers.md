# Levers: What Change Moves Which Check

Use this table to turn a failing or improvable check into one change, and the
formulas below to predict the effect before touching the board. All formulas
are implemented in the simulation package's layout module; quote the predicted
and the measured value in the iteration record.

## Buck converter

| Check or objective | Kind | Layout lever (first) | Design lever (if layout cannot reach it) |
|---|---|---|---|
| Hot-loop inductance | objective | Move the input capacitor nearest the IC so its VIN and GND pads sit next to the IC's VIN and GND pins, same layer, no vias in the loop. Add or extend a GND pour on the adjacent layer under the loop | Add a small high-frequency capacitor (100 nF, 0402) right at the pins |
| Switch-node ringing frequency, peak | evidence | Same as hot loop. The peak needs the vendor model or a measurement; never claim it from the estimate | Snubber or gate resistor, if the IC supports it |
| Load-point voltage | constraint | Widen VOUT and GND tracks between the output capacitors and the load; move the feedback tap to the load point (route the divider's top from the load pad); add GND return vias | Trim the feedback divider |
| Track current capacity (IPC-2221) | constraint | Widen the narrowest long track on the net; add a parallel track or pour on the other layer with vias | None. Layout only |
| Output ripple at the load | constraint | Shorten output capacitor to load path; place output capacitors at the inductor's output pad | More or larger output capacitance; lower-ESR parts |
| Inductor peak current, ripple | constraint | None. Set by values | Larger inductance, different part |
| Set-point tolerance | constraint | None | Tighter-tolerance divider resistors |
| Input capacitor RMS current | constraint | None | More input capacitors, higher ripple rating |
| SW pin to inductor resistance | objective | Shorten and widen the SW track, but keep the SW copper area small (EMI); do not pour it | None |
| Output-capacitor mounting inductance (ESL step in the ripple) | objective | Put the capacitor's GND via (two if room) at its pad, not after a track; keep VOUT and GND pads on the layer next to the plane | Lower-ESL package (0805 to 0603, reverse-geometry) or more capacitors in parallel |
| Switch-node copper capacitance | objective | Shrink the SW copper; no plane pour under the SW pad beyond what the return needs | None |
| Output-path inductance (load-step dip at the load) | objective | Shorten the junction-to-load track; keep it over the ground plane | Local capacitance at the load |

## Formulas for predicting a change

Use these before applying a change. They are the same ones the extraction uses.

- **Track resistance:** R = ρ · L / (w · t), with ρ = 1.72e-8 Ω·m at 20 °C
  (+0.393 %/K). One square of 35 µm copper is about 0.50 mΩ.
- **Track width for a current (IPC-2221):** I = k · ΔT^0.44 · A^0.725, with
  A in mil², k = 0.048 external and 0.024 internal. Use the copper thickness
  from the stackup.
- **Hot-loop inductance, GND returned through a plane** (the usual layout):
  the VIN track from the capacitor to the IC is a microstrip over the plane,
  L = L' · length, with L' = Z0 · √εeff / c from the Hammerstad formulas (about
  0.5 nH/mm for a 1 mm track 1.5 mm above the plane on FR-4). Add the GND vias
  the return current passes through at each end; parallel vias at one end
  divide that end's via inductance. Each via is L = 0.2 · h · (ln(4h/d) + 1) nH
  (h, d in mm; about 1.2 nH for a 1.6 mm board and 0.4 mm drill).
- **Hot-loop inductance, GND routed back with tracks:** the polygon of the
  actual VIN and GND track paths, replaced by a circle of equal area,
  L = μ0 · R · (ln(8R / a) − 2), with a = 0.2235 · (w + t); a plane at distance
  h under it subtracts the mutual inductance of an image loop at 2h.
- **Several input capacitors:** the one with the lowest loop dominates the
  switching edge; the extraction reports that one as L_hot.
- **Ringing frequency:** f = 1 / (2π √(L_hot · C)), where C is the switch-node
  plus VIN-pin capacitance. SPICE reproduces this within 10 %, which validates
  the lumped model, not the peak.

### Predicting a change before applying it

Recompute the quantity with the planned geometry. For a capacitor moved so its
VIN pad sits on the IC's VIN track, with one GND via at each end:

```python
from konnect_sim import layout
b = layout.load("board.kicad_pcb")              # saved board, read-only
gap = b.layer_gap_mm("F.Cu", "B.Cu")
length = 1.2                                    # planned cap-pad-to-IC-pin track, mm
track = layout.microstrip(1.0, gap, 4.5)["l_per_m"] * length * 1e-3
vias = 2 * layout.via_inductance(b.thickness_mm, 0.4)
print((track + vias) * 1e9, "nH")
```

Compare with the current value from the report. Apply the change only if the
gain is at least 5 % and the new position fits (courtyards, outline). In the
reference test the prediction for each change was within 5 % of the value
extracted afterwards.

- **Capacitor mounting:** the vias at each pad in parallel, L_via / n per terminal,
  added to the part's ESL. The ESL step in the output ripple is
  (ESL + L_mount) / N · Vin / L_ind for N capacitors in parallel.
- **Switch-node copper:** C = ε0 εr A / h for pads and pours over the plane, plus
  C' = √εeff / (c Z0) per length for tracks.

## What the extraction does not model

| Item | Effect | What to do |
|---|---|---|
| Current through copper pours | Series paths through a pour report "no track path" and are listed in `unmodelled` (the ground return is solved through the fill) | Route a track in parallel if the resistance matters, or treat the check as unmodelled |
| Mutual inductance between branches, current crowding | Lumped L is a closed-form estimate per branch | Bench measurement; field solver when available |
| Branches longer than tr·c / (10 √εr,eff) | The lumped element misrepresents the edge | Distributed model; keep the check `INCOMPLETE` |
| Plane cut-outs and fill clearance | The plane under a loop is taken from the zone outline | Inspect the fill; a slot under the loop invalidates the plane benefit |
| Absolute switch-node peak | Set by the IC's switching speed and high-frequency losses | Vendor model or measurement with a short ground spring |
| Radiated emissions | Not simulated | Minimise hot-loop area and SW copper as objectives; pre-compliance scan |
| Thermal coupling between parts | Not simulated | Thermal simulation or measurement |

A miss caused by an unmodelled item is reported, not "fixed".
