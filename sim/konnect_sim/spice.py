"""Run ngspice through PySpice, using the ngspice.dll that KiCad ships.

KiCad 10 on Windows bundles ``bin/ngspice.dll`` and its code models in
``lib/ngspice``. Pointing PySpice there means no separate simulator install.
Set ``KICAD_DIR`` to override the KiCad location (default ``D:\\KiCad``, then
``C:\\Program Files\\KiCad\\10.0``).
"""
from __future__ import annotations

import os
from pathlib import Path

_CANDIDATES = [
    os.environ.get("KICAD_DIR"),
    r"D:\KiCad",
    r"C:\Program Files\KiCad\10.0",
]
_configured = False


def kicad_dir() -> Path:
    for c in _CANDIDATES:
        if c and (Path(c) / "bin" / "ngspice.dll").exists():
            return Path(c)
    raise FileNotFoundError(
        "KiCad's ngspice.dll not found; set KICAD_DIR to the KiCad install folder"
    )


def configure() -> Path:
    """Point PySpice at KiCad's ngspice. Safe to call more than once."""
    global _configured
    root = kicad_dir()
    if _configured:
        return root
    os.add_dll_directory(str(root / "bin"))
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared

    NgSpiceShared.LIBRARY_PATH = str(root / "bin" / "ngspice.dll")
    NgSpiceShared.NGSPICE_PATH = str(root)
    _configured = True
    return root


def circuit(title: str):
    """Return a new PySpice ``Circuit`` after configuring ngspice."""
    configure()
    from PySpice.Spice.Netlist import Circuit

    return Circuit(title)


def simulator(circ, temperature: float = 25.0):
    configure()
    return circ.simulator(temperature=temperature, nominal_temperature=temperature)


def run_netlist(netlist: str) -> dict:
    """Run a complete SPICE netlist (title line through ``.end``).

    Returns ``{vector_name: numpy.ndarray}`` from the last plot, e.g.
    ``time``, node voltages by node name, and ``<source>#branch`` currents.
    Writing the netlist as plain SPICE keeps the testbench reviewable by an
    engineer and lets it be re-run in any SPICE simulator.
    """
    import numpy as np

    configure()
    from PySpice.Spice.NgSpice.Shared import NgSpiceShared

    ng = NgSpiceShared.new_instance()
    ng.load_circuit(netlist)
    ng.run()
    plot = ng.plot(None, ng.last_plot)
    out = {}
    for name, vec in plot.items():
        data = np.asarray(vec.to_waveform())
        # Transient/OP data is real; AC data is complex and must stay complex.
        out[name] = np.real(data) if not np.iscomplexobj(data) or not np.any(np.imag(data)) else data
    ng.remove_circuit()
    return out
