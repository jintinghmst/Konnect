"""PCB stackup with provenance: copper thickness, dielectric spacing, materials.

Two sources are accepted, never a remembered default:

- the stackup saved in the board (KiCad Board Setup > Physical Stackup), which
  is the design's own declaration;
- a JSON file transcribed from the fabricator's published stackup, which must
  carry the URL or document it came from::

    {"source": "JLCPCB JLC04161H-7628, https://jlcpcb.com/... retrieved 2026-10-19",
     "layers": [
       {"name": "F.Cu", "type": "copper", "thickness_mm": 0.035},
       {"name": "prepreg 1", "type": "prepreg", "thickness_mm": 0.2104,
        "material": "7628", "er": 4.4, "loss_tangent": 0.02},
       ...]}

``apply(board)`` writes the stackup into a ``layout.Board`` so every
extraction uses it, and marks the board's stackup as sourced.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from .report import Param


@dataclass
class StackLayer:
    name: str
    kind: str                     # "copper", "core", "prepreg", "mask", ...
    thickness_mm: float
    material: str = ""
    er: float | None = None
    loss_tangent: float | None = None


@dataclass
class Stackup:
    layers: list[StackLayer]
    source: str
    notes: list[str] = field(default_factory=list)

    def _param(self, value, unit, what):
        return Param(value, unit, self.source if value is not None else "", what)

    @property
    def copper_layers(self) -> list[str]:
        return [l.name for l in self.layers if l.kind == "copper"]

    @property
    def thickness_mm(self) -> float:
        """Copper plus dielectric thickness (mask and silkscreen excluded)."""
        return sum(l.thickness_mm for l in self.layers
                   if l.kind == "copper" or l.kind in ("core", "prepreg", "dielectric"))

    def copper_mm(self, layer: str) -> Param:
        for l in self.layers:
            if l.name == layer and l.kind == "copper":
                return self._param(l.thickness_mm, "mm", f"{layer} copper")
        return Param(0.0, "mm", "", f"{layer} copper (not in stackup)")

    def _between(self, a: str, b: str) -> list[StackLayer]:
        names = [l.name for l in self.layers]
        i, j = sorted((names.index(a), names.index(b)))
        return [l for l in self.layers[i + 1:j] if l.kind != "copper"]

    def gap_mm(self, a: str, b: str) -> Param:
        """Dielectric distance between copper layers ``a`` and ``b``."""
        between = self._between(a, b)
        return self._param(sum(l.thickness_mm for l in between), "mm", f"{a}-{b} gap")

    def er(self, a: str, b: str) -> Param:
        """Thickness-weighted relative permittivity between two copper layers."""
        between = self._between(a, b)
        if not between or any(l.er is None for l in between):
            return Param(0.0, "", "", f"{a}-{b} er (not given)")
        t = sum(l.thickness_mm for l in between)
        return self._param(sum(l.er * l.thickness_mm for l in between) / t, "", f"{a}-{b} er")

    def apply(self, board) -> None:
        """Use this stackup for every extraction on ``board`` (a layout.Board)."""
        board.copper_mm = {l.name: l.thickness_mm for l in self.layers if l.kind == "copper"}
        board.stack = [(l.name, l.kind if l.kind == "copper" else "dielectric", l.thickness_mm)
                       for l in self.layers if l.kind in ("copper", "core", "prepreg", "dielectric")]
        board.thickness_mm = self.thickness_mm
        board.stackup_sourced = True
        board.stackup_source = self.source


def from_json(path: str | Path) -> Stackup:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if not data.get("source"):
        raise ValueError(f"{path}: a fabricator stackup needs a 'source' (URL or document)")
    layers = [StackLayer(l["name"], l["type"], float(l["thickness_mm"]), l.get("material", ""),
                         l.get("er"), l.get("loss_tangent")) for l in data["layers"]]
    return Stackup(layers, data["source"], data.get("notes", []))


def from_board(path: str | Path) -> Stackup | None:
    """The stackup saved in a .kicad_pcb, or None when the board has none."""
    from .layout import _kid, _kids, _parse, _val, _isnum

    root = _parse(Path(path).read_text(encoding="utf-8"))
    setup = _kid(root, "setup")
    stack = _kid(setup, "stackup") if setup is not None else None
    if stack is None:
        return None
    layers = []
    for l in _kids(stack, "layer"):
        t = _val(l, "thickness")
        if t is None or not _isnum(t):
            continue
        er, tan = _val(l, "epsilon_r"), _val(l, "loss_tangent")
        layers.append(StackLayer(str(l[1]), _val(l, "type", default=""), float(t),
                                 _val(l, "material", default="") or "",
                                 float(er) if er is not None and _isnum(er) else None,
                                 float(tan) if tan is not None and _isnum(tan) else None))
    if not any(l.kind == "copper" for l in layers):
        return None
    return Stackup(layers, f"board stackup ({Path(path).name})")
