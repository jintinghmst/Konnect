"""Parse KiCad value strings into numbers.

KiCad values are free text: ``"4.7uF 50V"``, ``"100k 0.1%"``, ``"2.2 DNP"``,
``"6.8uH"``, ``"470pF DNP"``. ``parse_value`` returns the primary quantity in
SI units plus whatever ratings and flags the string also carries.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

_PREFIX = {
    "p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "μ": 1e-6, "m": 1e-3,
    "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "meg": 1e6, "G": 1e9,
}
# "4.7uF", "100k", "2.2", "4k7", "0R1", "6.8uH", "1M"
_NUMBER = re.compile(
    r"^(?P<a>\d+(?:\.\d+)?)(?P<p>meg|[pnuµμmkKMG]?)(?P<b>\d*)(?P<unit>[FHΩRrVAW]?|ohm|Ohm)?$"
)


@dataclass
class Value:
    raw: str
    value: float | None
    unit: str | None
    voltage_rating: float | None = None
    tolerance_pct: float | None = None
    dnp: bool = False
    extra: list[str] = field(default_factory=list)


def _number(token: str) -> tuple[float, str | None] | None:
    m = _NUMBER.match(token)
    if not m:
        return None
    a, p, b, unit = m.group("a"), m.group("p"), m.group("b"), m.group("unit") or None
    if b:  # "4k7" style: the prefix is the decimal point
        if "." in a:
            return None
        a = f"{a}.{b}"
    if unit in ("R", "r") and p == "" and not b:
        unit = "Ω"
    if unit in ("ohm", "Ohm"):
        unit = "Ω"
    return float(a) * _PREFIX[p], unit


def parse_value(raw: str, default_unit: str | None = None) -> Value:
    """Parse ``raw``; ``default_unit`` names the unit when the string omits it."""
    out = Value(raw=raw, value=None, unit=None)
    for tok in raw.replace(",", " ").split():
        up = tok.upper()
        if up in ("DNP", "DNI", "NC", "NF"):
            out.dnp = True
            continue
        if tok.endswith("%"):
            try:
                out.tolerance_pct = float(tok[:-1])
                continue
            except ValueError:
                pass
        if tok[-1:] in ("V", "v") and out.value is not None:
            num = _number(tok[:-1])
            if num:
                out.voltage_rating = num[0]
                continue
        num = _number(tok)
        if num and out.value is None:
            out.value, out.unit = num[0], num[1] or default_unit
            continue
        out.extra.append(tok)
    return out


def default_unit_for(reference: str) -> str | None:
    """Unit implied by a reference prefix (R -> Ω, C -> F, L -> H)."""
    prefix = re.match(r"[A-Za-z]+", reference)
    return {"R": "Ω", "C": "F", "L": "H"}.get(prefix.group(0).upper() if prefix else "")
