"""Worst-case component corners: vertex enumeration and Monte Carlo.

Each varying input is a ``Spread`` (nominal, low, high) with the source of its
limits (datasheet tolerance, temperature range, frequency spread...). A sweep
runs a user function on every combination of extremes (all vertices of the
tolerance box, up to ``max_vertices``) or on random draws, and reports the
worst case of each output metric with the corner that produced it.

Vertices find the worst case of any metric that is monotonic in each input,
which covers ripple, set-point, current peaks and most stress figures. For
non-monotonic metrics (resonances, loop phase near a pole-zero pair) add
Monte Carlo draws as well.
"""
from __future__ import annotations

import itertools
import random
from dataclasses import dataclass

from .report import Param


@dataclass
class Spread:
    name: str
    nom: float
    lo: float
    hi: float
    source: str = ""            # where the limits come from; empty = assumed

    @staticmethod
    def tol(name: str, nom: float, frac: float, source: str = "") -> "Spread":
        return Spread(name, nom, nom * (1 - frac), nom * (1 + frac), source)

    def param(self, v: float) -> Param:
        return Param(v, "", self.source, self.name)


def vertices(spreads: list[Spread], max_vertices: int = 1024) -> list[dict]:
    if 2 ** len(spreads) > max_vertices:
        raise ValueError(f"{len(spreads)} spreads give {2 ** len(spreads)} vertices; "
                         f"raise max_vertices or use montecarlo()")
    cases = [{s.name: s.nom for s in spreads} | {"_corner": "nominal"}]
    for combo in itertools.product(*[(s.lo, s.hi) for s in spreads]):
        tag = ",".join(f"{s.name}={'lo' if v == s.lo else 'hi'}" for s, v in zip(spreads, combo))
        cases.append({s.name: v for s, v in zip(spreads, combo)} | {"_corner": tag})
    return cases


def montecarlo(spreads: list[Spread], n: int, seed: int = 1) -> list[dict]:
    """Uniform draws inside each spread (a uniform box is the conservative
    choice when only limits, not distributions, are published)."""
    rng = random.Random(seed)
    return [{s.name: rng.uniform(s.lo, s.hi) for s in spreads} | {"_corner": f"mc{i}"} for i in range(n)]


def sweep(fn, cases: list[dict]) -> list[tuple[dict, dict]]:
    """Run ``fn(case) -> {metric: value}`` on every case."""
    return [(c, fn(c)) for c in cases]


def worst(results: list[tuple[dict, dict]], metric: str, sense: str = "max") -> tuple[dict, float]:
    pick = max if sense == "max" else min
    case, res = pick(results, key=lambda cr: cr[1][metric])
    return case, res[metric]


def sources(spreads: list[Spread]) -> dict:
    """Provenance entries for ``report.check``: one per spread."""
    return {f"{s.name} spread": Param(s.nom, "", s.source, s.name) for s in spreads}
