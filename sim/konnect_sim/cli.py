"""Command-line bridge to the circuit-independent simulation tools.

Konnect's MCP server runs the ``simulation`` toolset through this module, so
there is one MCP server and the Python package stays an implementation detail:

    python -m konnect_sim.cli --catalog            tool names, descriptions, JSON schemas
    python -m konnect_sim.cli <tool> < args.json   run one tool; JSON result on stdout

The result is always one JSON object on stdout: {"ok": true, "result": ...}
or {"ok": false, "error": {"kind": ..., "message": ...}}. The exit code is 0
for both, so the caller decides how to surface a tool error; a non-zero exit
means the bridge itself failed (bad JSON, unknown tool, crash).
"""
from __future__ import annotations

import inspect
import json
import math
import sys
import traceback
import types
import typing

from . import tools

# tool name -> function; read_only says whether the tool writes files
TOOLS = {
    "sim_read_schematic": (tools.read_schematic, True),
    "sim_board_stackup": (tools.board_stackup, True),
    "sim_copper_connectivity": (tools.copper_connectivity, True),
    "sim_copper_path": (tools.copper_path, True),
    "sim_trace_coupling": (tools.trace_coupling, True),
    "sim_board_thermal": (tools.board_thermal, False),
    "sim_spice_run": (tools.spice_run, False),
    "sim_capacitor_check": (tools.capacitor_check, True),
    "sim_inductor_check": (tools.inductor_check, True),
    "sim_input_network": (tools.input_network, True),
    "sim_conducted_emissions": (tools.conducted_emissions, True),
    "sim_corner_sweep": (tools.corner_sweep, True),
    "sim_evaluate_check": (tools.evaluate_check, True),
    "sim_write_report": (tools.write_report, False),
    "sim_render_document": (tools.render_document, False),
    "sim_cycle_record": (tools.cycle_record, False),
    "sim_layout_calc": (tools.layout_calc, True),
}

SOURCED = {"type": "object", "properties": {"value": {"type": "number"}, "source": {"type": "string"},
                                            "unit": {"type": "string"}}, "required": ["value"],
           "description": "a sourced value; a bare number is an assumption"}


def _schema(tp) -> dict:
    """JSON schema of a type hint (the small set the tools use)."""
    origin, args = typing.get_origin(tp), typing.get_args(tp)
    if origin is typing.Union or origin is types.UnionType:
        opts = [a for a in args if a is not type(None)]
        if dict in opts and float in opts:
            return {"anyOf": [{"type": "number"}, SOURCED]}
        return _schema(opts[0]) if len(opts) == 1 else {"anyOf": [_schema(a) for a in opts]}
    if tp is str:
        return {"type": "string"}
    if tp is bool:
        return {"type": "boolean"}
    if tp is int:
        return {"type": "integer"}
    if tp is float:
        return {"type": "number"}
    if tp is list or origin is list:
        return {"type": "array"}
    if tp is dict or origin is dict:
        return {"type": "object"}
    return {}


def catalog() -> list[dict]:
    out = []
    for name, (fn, read_only) in TOOLS.items():
        hints = typing.get_type_hints(fn)
        props, required = {}, []
        for p in inspect.signature(fn).parameters.values():
            props[p.name] = _schema(hints.get(p.name, str))
            if p.default is inspect.Parameter.empty:
                required.append(p.name)
            elif p.default is not None:
                props[p.name]["default"] = p.default
        out.append({"name": name, "description": inspect.cleandoc(fn.__doc__ or ""), "read_only": read_only,
                    "input_schema": {"type": "object", "properties": props, "required": required}})
    return out


def _clean(x):
    """Make results JSON-safe: numpy scalars/arrays, NaN/inf, tuples."""
    import numpy as np
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, np.ndarray):
        return _clean(x.tolist())
    if isinstance(x, (np.bool_,)):
        return bool(x)
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        f = float(x)
        return None if math.isnan(f) or math.isinf(f) else f
    return x


def run(name: str, args: dict) -> dict:
    if name not in TOOLS:
        return {"ok": False, "error": {"kind": "unknown_tool", "message": f"no simulation tool {name!r}"}}
    fn, _ = TOOLS[name]
    params = inspect.signature(fn).parameters
    unknown = sorted(set(args) - set(params))
    if unknown:
        return {"ok": False, "error": {"kind": "invalid_arguments", "message": f"unknown arguments {unknown}"}}
    try:
        return {"ok": True, "result": _clean(fn(**args))}
    except (TypeError, ValueError, KeyError) as e:
        return {"ok": False, "error": {"kind": "invalid_arguments", "message": f"{type(e).__name__}: {e}"}}
    except FileNotFoundError as e:
        return {"ok": False, "error": {"kind": "file_not_found", "message": str(e)}}
    except Exception as e:  # report, never hang the caller
        return {"ok": False, "error": {"kind": "simulation_failed", "message": f"{type(e).__name__}: {e}",
                                       "trace": traceback.format_exc(limit=5)}}


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv == ["--catalog"]:
        print(json.dumps(catalog()))
        return 0
    if len(argv) != 1:
        print(json.dumps({"ok": False, "error": {"kind": "usage",
                                                 "message": "usage: konnect_sim.cli --catalog | <tool> < args.json"}}))
        return 2
    raw = sys.stdin.read()
    try:
        args = json.loads(raw) if raw.strip() else {}
    except json.JSONDecodeError as e:
        print(json.dumps({"ok": False, "error": {"kind": "invalid_json", "message": str(e)}}))
        return 2
    print(json.dumps(run(argv[0], args)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
