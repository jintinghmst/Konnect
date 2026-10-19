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
histories.
