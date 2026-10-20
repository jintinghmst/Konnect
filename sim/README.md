# konnect_sim

Python building blocks for the `kicad-simulation` skill: the assistant writes a
short script per question ("does this buck meet its ripple spec?") and uses this
package for the parts that must be reliable every time.

## Requirements

- KiCad 10 (its bundled `ngspice.dll` is the SPICE engine; set `KICAD_DIR` if
  KiCad is not in `D:\KiCad` or `C:\Program Files\KiCad\10.0`)
- Python 3.10+: `pip install numpy scipy matplotlib scikit-rf PySpice pytest`

## Modules

| Module | Purpose |
|---|---|
| `spice` | Configure ngspice from KiCad; `run_netlist(text)` returns result vectors |
| `netlist` | `load(schematic)` → components, values, nets (via kicad-cli) |
| `units` | Parse KiCad values: `"22uF 16V"`, `"100k 0.1%"`, `"470pF DNP"` |
| `report` | `Check` / `Report` with PASS / FAIL / INCOMPLETE rules; Markdown + JSON |
| `buck` | Buck design equations, a switched open-loop SPICE testbench (optionally with layout parasitics, plus an edge-resolved run for hot-loop ringing), and `layout_parasitics()` for a buck board |
| `layout` | Read a saved `.kicad_pcb` (read-only): pads, tracks, vias, pours, stackup; copper path resistance, loop/via inductance with a plane under the loop, IPC-2221 track width |
| `cycle` | `CycleLog`: iteration history and continue / revert / met / stalled / limit decisions for the `kicad-sim-optimize` cycle |
| `stackup` | Stackup with provenance: the board's own (`from_board`) or a fabricator's published stackup (`from_json`, must carry its source); `apply(board)` |
| `copper` | DC flow in the real filled copper of a net: pours with thermal reliefs, tracks, pads, via barrels. Connectivity, resistance, current density, via currents, IPC-2221 capacity, I²R maps |
| `parts` | `Inductor` / `Capacitor` from manufacturer data and their checks: Isat, L(I), Irms, losses, temperature; C_eff (bias, tolerance, temperature), ripple share by impedance, self-heating, voltage derating |
| `losses` | Buck loss budget (conduction, switching, Coss, dead time, drive, magnetics, ESR, copper), efficiency, Tj with RDS(on)(Tj), switching-time fit to a vendor efficiency curve |
| `thermal` | Steady-state board temperature from copper coverage, laminate and a film coefficient; component temperatures |
| `converter` | Closed-loop switched buck (`Plant`, `Sim`) with `COT`, `VoltageMode` and `PeakCurrentMode` controllers; exact switching instants; loop gain by series injection, margins |
| `scenarios` | Closed-loop spec checks: regulation, stability over corners, load step, start-up/shut-down, light load, line steps and brown-out, overload and short |
| `stress` | Switch-node and pin stress (VIN pin, VBST–SW) from an edge-resolved run, against limits with durations; skin-effect resistance |
| `inputfilter` | Middlebrook margin of source/cable/filter against the converter's negative input resistance; hot-plug overshoot |
| `emi` | Conducted DM spectrum at a LISN, limit-line margin, CM estimate, SW-to-FB mutual capacitance and coupled noise |
| `corners` | Tolerance vertices and Monte Carlo over sourced spreads; worst case per metric |
| `budget` | Output-voltage window at the load, both limits, every contribution |

Every model input is a `report.Param` carrying its source; `report.check`
derives the status from the inputs' provenance, so an assumed value or an
unvalidated controller model keeps a check INCOMPLETE automatically. The
coverage map of a buck verification (which module answers which question, and
what makes it PASS/FAIL) is in the `kicad-simulation` skill's
`references/simulation-methods.md`.

## Usage

```python
import sys; sys.path.insert(0, r"D:\research\Konnect\sim")
from konnect_sim import netlist, buck, report
design = netlist.load(r"path\to\board.kicad_sch")
```

## Self-test

```
python -m pytest sim/tests -q
```

Every module is checked against an independent calculation: the switched buck
testbench against the closed-form equations (within 5 %), hot-loop ringing
frequency against 1/(2π√LC) (within 10 %), track resistance against
ρL/(w·t) on KiCad's demo board, the loop-inductance estimate against the exact
square-loop formula (within 10 %), and the cycle's decision rules on scripted
histories. The extended models (`tests/test_simulation_list.py`): the copper
solve against plate, track and via hand calculations (within 2-5 %), board
thermal against uniform flux (1 %), the closed-loop model open-loop against
the averaged switch node, injected loop gain against an analytic voltage-mode
loop (2.5 dB, 12°), COT regulation and pulse skipping, the switching-time fit,
skin depth, hot-plug and Middlebrook against the series RLC, harmonics of a
square wave, corner enumeration and the output budget arithmetic.

`python -m pytest sim/tests -q -m "not slow"` skips the closed-loop and SPICE
runs that take more than a few seconds.
