"""Standard verification suites: one fixed set of checks, operating points, solver
settings and charts per circuit class, so every design of that class is verified the same
way. ``SUITES`` names them; add a module with ``template`` and ``run`` for a new class."""
from __future__ import annotations

from . import buck

SUITES = {"buck": buck}


def _suite(circuit: str):
    try:
        return SUITES[circuit]
    except KeyError:
        raise ValueError(f"no standard suite for {circuit!r}; available: {', '.join(SUITES)}") from None


def template(project, circuit: str, iteration: int | None = None):
    return _suite(circuit).template(project, iteration)


def run(project, circuit: str, iteration: int | None = None, **kw) -> dict:
    return _suite(circuit).run(project, iteration, **kw)
