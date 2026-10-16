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
| `buck` | Buck design equations and a switched open-loop SPICE testbench |

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

The switched buck testbench is checked against the closed-form equations
(inductor ripple and input RMS current within 5 %).
