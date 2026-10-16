"""Read components, values and nets from a KiCad schematic.

Uses ``kicad-cli sch export netlist --format kicadxml`` so connectivity is
KiCad's own, not re-derived. Locate kicad-cli via ``KICAD_DIR`` / the same
search as ``spice.kicad_dir``.
"""
from __future__ import annotations

import subprocess
import tempfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

from .spice import kicad_dir
from .units import Value, default_unit_for, parse_value


@dataclass
class Component:
    ref: str
    value: str
    footprint: str
    fields: dict[str, str]
    pins: dict[str, str] = field(default_factory=dict)  # pin number -> net name
    parsed: Value | None = None

    @property
    def dnp(self) -> bool:
        return bool(self.parsed and self.parsed.dnp) or self.fields.get("DNP", "").strip() not in ("", "0")


@dataclass
class Design:
    schematic: Path
    components: dict[str, Component]
    nets: dict[str, list[tuple[str, str]]]  # net -> [(ref, pin)]

    def on_net(self, net: str, prefix: str | None = None) -> list[Component]:
        refs = {ref for ref, _ in self.nets.get(net, [])}
        return [self.components[r] for r in sorted(refs)
                if r in self.components and (prefix is None or r.startswith(prefix))]

    def between(self, net_a: str, net_b: str, prefix: str | None = None) -> list[Component]:
        """Two-terminal parts with one pin on each net (e.g. caps from VOUT to GND)."""
        out = []
        for c in self.components.values():
            if prefix and not c.ref.startswith(prefix):
                continue
            nets = set(c.pins.values())
            if len(c.pins) == 2 and nets == {net_a, net_b}:
                out.append(c)
        return sorted(out, key=lambda c: c.ref)


def load(schematic: str | Path) -> Design:
    sch = Path(schematic)
    cli = kicad_dir() / "bin" / "kicad-cli.exe"
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "net.xml"
        subprocess.run(
            [str(cli), "sch", "export", "netlist", "--format", "kicadxml",
             "--output", str(out), str(sch)],
            check=True, capture_output=True, text=True,
        )
        root = ET.parse(out).getroot()

    comps: dict[str, Component] = {}
    for c in root.iter("comp"):
        ref = c.get("ref")
        fields = {f.get("name"): (f.text or "") for f in c.iter("field")}
        value = (c.findtext("value") or "").strip()
        comp = Component(ref=ref, value=value, footprint=c.findtext("footprint") or "",
                         fields=fields)
        comp.parsed = parse_value(value, default_unit_for(ref))
        comps[ref] = comp

    nets: dict[str, list[tuple[str, str]]] = {}
    for n in root.iter("net"):
        name = n.get("name")
        for node in n.iter("node"):
            ref, pin = node.get("ref"), node.get("pin")
            nets.setdefault(name, []).append((ref, pin))
            if ref in comps:
                comps[ref].pins[pin] = name
    return Design(schematic=sch, components=comps, nets=nets)
