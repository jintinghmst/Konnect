"""Building blocks for simulations an AI assistant writes against KiCad designs.

The assistant writes a short script per question ("does the buck meet its
ripple spec?"); this package supplies the parts that must not be re-invented
in every script:

- ``spice``   : run ngspice through PySpice using KiCad's bundled ngspice.dll
- ``netlist`` : read a KiCad schematic's components, values and nets
- ``units``   : parse KiCad value strings such as ``"22uF 16V"`` or ``"470pF DNP"``
- ``report``  : spec checks with explicit status, method and assumptions
- ``buck``    : buck-converter design equations, a switched SPICE testbench,
                and extraction of a buck layout's parasitics
- ``layout``  : read a saved board (read-only) and estimate copper resistance,
                loop and via inductance, and IPC-2221 track width
- ``cycle``   : iteration log and stop rules for the simulate -> change cycle
- ``stackup`` : the board's or the fabricator's stackup, with its source
- ``copper``  : DC flow in the real filled copper of a net (connectivity,
                resistance, current density, via currents, I^2R maps)
- ``parts``   : inductor and capacitor suitability from manufacturer data
- ``losses``  : buck loss budget, efficiency and junction temperature
- ``thermal`` : steady-state board temperature (heat spreading)
- ``converter``: closed-loop switched buck with controller models; loop gain
                by injection
- ``scenarios``: closed-loop checks (regulation, stability, load and line
                steps, start-up, light load, faults)
- ``stress``  : switch-node and pin voltage stress against limits with durations
- ``inputfilter``: source/cable/filter interaction and hot-plug
- ``emi``     : conducted-emission and coupling estimates
- ``corners`` : tolerance vertices and Monte Carlo
- ``budget``  : output-voltage window at the load
- ``figures`` : report charts: every checked quantity against its limit
                (ripple summary and waveform grids, spectrum, Bode)
- ``document``: render a Markdown report as PDF or Word, text and figures together
- ``project`` : the standard folders of a project's scripts, inputs, results,
                figures and reports

Every result that a report presents as evidence carries the method that
produced it and the assumptions it rests on. A check whose inputs are assumed
rather than sourced is reported as INCOMPLETE, never PASS.
"""

__all__ = ["spice", "netlist", "units", "report", "buck", "layout", "cycle", "stackup", "copper",
           "parts", "losses", "thermal", "converter", "scenarios", "stress", "inputfilter", "emi",
           "corners", "budget", "figures", "document", "project"]
__version__ = "0.3.0"
