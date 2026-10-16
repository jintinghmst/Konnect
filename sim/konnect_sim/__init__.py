"""Building blocks for simulations an AI assistant writes against KiCad designs.

The assistant writes a short script per question ("does the buck meet its
ripple spec?"); this package supplies the parts that must not be re-invented
in every script:

- ``spice``   : run ngspice through PySpice using KiCad's bundled ngspice.dll
- ``netlist`` : read a KiCad schematic's components, values and nets
- ``units``   : parse KiCad value strings such as ``"22uF 16V"`` or ``"470pF DNP"``
- ``report``  : spec checks with explicit status, method and assumptions
- ``buck``    : buck-converter design equations and a switched SPICE testbench

Every result that a report presents as evidence carries the method that
produced it and the assumptions it rests on. A check whose inputs are assumed
rather than sourced is reported as INCOMPLETE, never PASS.
"""

__all__ = ["spice", "netlist", "units", "report", "buck"]
__version__ = "0.1.0"
