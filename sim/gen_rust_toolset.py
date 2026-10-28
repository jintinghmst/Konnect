"""Generate crates/konnect-core/src/tools/simulation.rs from the bridge catalogue.

    python sim/gen_rust_toolset.py

The Python functions in konnect_sim.tools are the source of truth; this script
turns their signatures (via konnect_sim.cli.catalog) into Konnect `tool!`
registrations with closed JSON schemas, tightening the loose Python hints
below (pad pairs, point tables, caller-keyed maps). Re-run it after changing a
tool's signature; tests compare the generated file with the catalogue.
"""
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from konnect_sim import cli  # noqa: E402

OUT = HERE.parent / "crates/konnect-core/src/tools/simulation.rs"

NUM_OR_SOURCED = {"anyOf": [{"type": "number"}, {"type": "object", "properties": {
    "value": {"type": "number"}, "source": {"type": "string"}, "unit": {"type": "string"},
    "name": {"type": "string"}}, "required": ["value"]}]}
PAD = {"type": "array", "prefixItems": [{"type": "string"}, {"type": "string"}], "minItems": 2, "maxItems": 2,
       "description": "[reference, pad number]"}
PADS = {"type": "array", "items": PAD, "minItems": 1}
POINTS = {"type": "array", "items": {"type": "array", "items": {"type": "number"}, "minItems": 2}}
NUM_MAP = {"type": "object", "additionalProperties": {"type": "number"}}
SOURCED_MAP = {"type": "object", "additionalProperties": NUM_OR_SOURCED}

# tool -> {param: schema} overriding what the Python hint alone can say
OVERRIDES = {
    "sim_copper_connectivity": {"pads": PADS},
    "sim_copper_path": {"source_pads": PADS, "sink_pads": PADS},
    "sim_board_thermal": {
        "footprint_watts": NUM_MAP | {"description": "{reference: watts}"},
        "package_resistance": SOURCED_MAP | {"description": "{reference: K/W to board}"},
        "limits_c": SOURCED_MAP | {"description": "{reference: max °C}"},
        "copper_flows": {"type": "array", "items": {"type": "object", "properties": {
            "net": {"type": "string"}, "source_pads": PADS, "sink_pads": PADS, "rms_a": {"type": "number"}},
            "required": ["net", "source_pads", "sink_pads", "rms_a"]}},
    },
    "sim_spice_run": {"measures": {"type": "array", "items": {"type": "object", "properties": {
        "vector": {"type": "string"}, "kind": {"enum": ["max", "min", "avg", "pp", "rms", "final", "at"]},
        "t_from": {"type": "number"}, "t_to": {"type": "number"}, "at": {"type": "number"}},
        "required": ["vector", "kind"]}}},
    "sim_capacitor_check": {"dc_bias": POINTS | {"description": "[[V, C/C_nom], ...] manufacturer curve"}},
    "sim_inductor_check": {"l_vs_i": POINTS | {"description": "[[A, H], ...] manufacturer curve"},
                           "core_loss_table": POINTS | {"description": "[[f_hz, dI_pp_A, W], ...]"}},
    "sim_input_network": {"branches": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
        "r": {"type": "number"}, "l": {"type": "number"}, "c": {"type": "number"}}, "required": ["r"]}}},
    "sim_conducted_emissions": {"limit_points": POINTS | {"description": "[[f_hz, dBuV], ...] from the standard"}},
    "sim_corner_sweep": {
        "spreads": {"type": "array", "minItems": 1, "items": {"type": "object", "properties": {
            "name": {"type": "string"}, "nom": {"type": "number"}, "lo": {"type": "number"},
            "hi": {"type": "number"}, "tol": {"type": "number"}, "source": {"type": "string"}},
            "required": ["name", "nom"]}},
        "metrics": {"type": "object", "additionalProperties": {"type": "string"},
                    "description": "{metric: arithmetic expression of the spread names}"},
        "mode": {"enum": ["vertices", "montecarlo"], "default": "vertices"},
    },
    "sim_evaluate_check": {"inputs": SOURCED_MAP | {"description": "{input name: number or {value, source}}"},
                           "sense": {"enum": ["max", "min"]}},
    "sim_write_report": {"checks": {"type": "array", "items": {"type": "object", "additionalProperties": True,
                                                               "description": "a check record returned by another sim_* tool"}},
                         "notes": {"type": "array", "items": {"type": "string"}},
                         "figures": {"type": "array", "items": {
                             "type": "object", "additionalProperties": False,
                             "properties": {"title": {"type": "string"},
                                            "path": {"type": "string", "description": "PNG file to embed"}},
                             "required": ["title", "path"]}},
                         "documents": {"type": "array", "items": {"enum": ["pdf", "docx"]},
                                       "description": "rendered with the figures; default [\"pdf\"]"}},
    "sim_render_document": {"formats": {"type": "array", "items": {"enum": ["pdf", "docx"]},
                                        "description": "default [\"pdf\"]"}},
    "sim_cycle_record": {"files": {"type": "array", "items": {"type": "string"}},
                         "objectives": NUM_MAP | {"description": "{objective: value} (lower is better)"}},
    "sim_layout_calc": {"kind": {"enum": ["microstrip", "via_inductance", "via_resistance", "ipc2221_width",
                                          "ipc2221_current", "skin_resistance", "loop_inductance",
                                          "sheet_resistance"]},
                        "params": {"type": "object", "additionalProperties": {"anyOf": [
                            {"type": "number"}, {"type": "boolean"}, POINTS]},
                            "description": "the kind's parameters, see the description"}},
}

# the open maps the router test must accept (path strings as that test builds them)
OPEN_MAPS = sorted(
    {f"{t}/properties/{p}" for t, o in OVERRIDES.items() for p, s in o.items()
     if isinstance(s, dict) and isinstance(s.get("additionalProperties"), dict)}
    | {f"{t}/properties/{p}/items" for t, o in OVERRIDES.items() for p, s in o.items()
       if isinstance(s.get("items"), dict) and s["items"].get("additionalProperties") is True})


def rustfmt(code: str) -> str:
    """Format as `cargo fmt` would (CI enforces it); unformatted if rustfmt is absent."""
    import shutil
    import subprocess
    if not shutil.which("rustfmt"):
        return code
    p = subprocess.run(["rustfmt", "--edition", "2021", "--emit", "stdout"], input=code, capture_output=True,
                       text=True, encoding="utf-8")
    if p.returncode != 0:
        raise SystemExit(f"rustfmt failed:\n{p.stderr}")
    return p.stdout


def rust_str(s: str) -> str:
    return 'r#"' + s.replace('"#', '" #') + '"#'


def main():
    tools = []
    for t in cli.catalog():
        sch = t["input_schema"]
        for p, s in OVERRIDES.get(t["name"], {}).items():
            assert p in sch["properties"], (t["name"], p)
            default = sch["properties"][p].get("default")
            sch["properties"][p] = dict(s) | ({"default": default} if default is not None else {})
        tools.append((t["name"], t["description"], sch))

    lines = [
        "//! `simulation` toolset — circuit-independent simulation of a KiCad design.",
        "//!",
        "//! GENERATED by `sim/gen_rust_toolset.py` from the `konnect_sim` tool catalogue;",
        "//! edit the Python tools and re-run the generator rather than this file.",
        "//!",
        "//! Each tool runs `python -m konnect_sim.cli <tool>` with its arguments on stdin",
        "//! (see `sim/konnect_sim/cli.py`). The interpreter is `KONNECT_PYTHON` (default",
        "//! `python`) and the package directory `KONNECT_SIM_DIR` (default: this",
        "//! repository's `sim` folder). Board tools read the saved board, read-only.",
        "",
        "use crate::mcp::{error::ToolErrorKind, protocol::CallToolResult};",
        "use crate::tool;",
        "use crate::tools::{ToolContext, ToolDef};",
        "use serde_json::{json, Map, Value};",
        "use std::process::Stdio;",
        "use std::time::Duration;",
        "use tokio::io::AsyncWriteExt;",
        "",
        "/// Default upper bound for one simulation call; `KONNECT_SIM_TIMEOUT_S` overrides it.",
        "const DEFAULT_TIMEOUT_S: u64 = 1800;",
        "",
        "pub fn tools() -> Vec<ToolDef> {",
        "    vec![",
    ]
    for name, desc, sch in tools:
        lines += [
            "        tool!(",
            f'            "{name}",',
            f"            {rust_str(desc)},",
            f"            json!({json.dumps(sch, ensure_ascii=False)}),",
            f"            |args, ctx| async move {{ handle_{name}(args, ctx).await }}",
            "        ),",
        ]
    lines += ["    ]", "}", ""]
    for name, _, sch in tools:
        fields = ", ".join(f'"{p}"' for p in sch["properties"])
        lines += [
            f"async fn handle_{name}(args: &Value, _ctx: &ToolContext) -> anyhow::Result<CallToolResult> {{",
            f'    run_sim("{name}", args, &[{fields}]).await',
            "}",
            "",
        ]
    lines.append(RUNNER)
    text = rustfmt("\n".join(lines))
    if "--check" in sys.argv:
        current = OUT.read_text(encoding="utf-8") if OUT.exists() else ""
        if current != text:
            print(f"{OUT} is stale: run python sim/gen_rust_toolset.py")
            sys.exit(1)
        print("up to date")
        return
    OUT.write_text(text, encoding="utf-8")
    print(f"wrote {OUT} ({len(tools)} tools)")
    print("open maps for the router test:")
    for m in OPEN_MAPS:
        print("  ", m)


RUNNER = r'''// ─── Bridge to the Python package ──────────────────────────────────────────────

fn sim_dir() -> std::path::PathBuf {
    std::env::var_os("KONNECT_SIM_DIR")
        .map(std::path::PathBuf::from)
        .unwrap_or_else(|| std::path::Path::new(env!("CARGO_MANIFEST_DIR")).join("../../sim"))
}

fn python() -> String {
    std::env::var("KONNECT_PYTHON").unwrap_or_else(|_| "python".to_string())
}

fn sim_timeout() -> Duration {
    let secs = std::env::var("KONNECT_SIM_TIMEOUT_S")
        .ok()
        .and_then(|s| s.parse().ok())
        .unwrap_or(DEFAULT_TIMEOUT_S);
    Duration::from_secs(secs)
}

/// Forward exactly the declared `fields` that the caller supplied, run the
/// tool in the Python package and map its answer to a tool result.
async fn run_sim(
    tool: &str,
    args: &Value,
    fields: &[&str],
) -> anyhow::Result<CallToolResult> {
    let mut forwarded = Map::new();
    for field in fields {
        if let Some(value) = args.get(*field) {
            if !value.is_null() {
                forwarded.insert((*field).to_string(), value.clone());
            }
        }
    }
    let input = serde_json::to_vec(&Value::Object(forwarded))?;
    let dir = sim_dir();
    let interpreter = python();
    let mut child = match tokio::process::Command::new(&interpreter)
        .args(["-m", "konnect_sim.cli", tool])
        .current_dir(&dir)
        .env("PYTHONPATH", &dir)
        .env("PYTHONIOENCODING", "utf-8")
        .stdin(Stdio::piped())
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .kill_on_drop(true)
        .spawn()
    {
        Ok(child) => child,
        Err(error) => {
            let reason = format!(
                "could not start the simulation runtime '{interpreter}' ({error}); install Python \
                 3.10+ with numpy, scipy, matplotlib and PySpice, or point KONNECT_PYTHON at it \
                 (package directory: {})",
                dir.display()
            );
            return Ok(CallToolResult::error_kind(
                ToolErrorKind::HandlerError { reason: reason.clone() },
                reason,
            ));
        }
    };
    if let Some(mut stdin) = child.stdin.take() {
        stdin.write_all(&input).await?;
    }
    let output = match tokio::time::timeout(sim_timeout(), child.wait_with_output()).await {
        Ok(result) => result?,
        Err(_) => {
            let reason = format!(
                "{tool} did not finish within {} s (KONNECT_SIM_TIMEOUT_S)",
                sim_timeout().as_secs()
            );
            return Ok(CallToolResult::error_kind(
                ToolErrorKind::HandlerError { reason: reason.clone() },
                reason,
            ));
        }
    };
    let stdout = String::from_utf8_lossy(&output.stdout);
    let answer: Value = match serde_json::from_str(stdout.trim()) {
        Ok(value) => value,
        Err(_) => {
            let stderr = String::from_utf8_lossy(&output.stderr);
            let tail: String = stderr.chars().rev().take(1500).collect::<Vec<_>>().into_iter().rev().collect();
            let reason = format!(
                "{tool}: the simulation runtime returned no result (exit {:?}); stderr: {tail}",
                output.status.code()
            );
            return Ok(CallToolResult::error_kind(
                ToolErrorKind::HandlerError { reason: reason.clone() },
                reason,
            ));
        }
    };
    if answer["ok"] == json!(true) {
        return Ok(CallToolResult::json(&answer["result"]));
    }
    let kind = answer["error"]["kind"].as_str().unwrap_or("simulation_failed");
    let message = answer["error"]["message"].as_str().unwrap_or("simulation failed").to_string();
    let error = match kind {
        "invalid_arguments" => ToolErrorKind::InvalidArgument {
            field: "arguments".to_string(),
            reason: message.clone(),
        },
        "file_not_found" => ToolErrorKind::FileNotFound { path: message.clone() },
        _ => ToolErrorKind::HandlerError { reason: message.clone() },
    };
    Ok(CallToolResult::error_kind(error, format!("{tool}: {message}")))
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::mcp::protocol::ToolContent;

    fn text(result: &CallToolResult) -> String {
        match result.content.first() {
            Some(ToolContent::Text { text }) => text.clone(),
            other => panic!("expected text content, got {other:?}"),
        }
    }

    /// Runs the real bridge end to end; skipped where no Python runtime exists.
    #[tokio::test]
    async fn layout_calc_round_trips_through_the_python_bridge() {
        let args = json!({"kind": "via_inductance", "params": {"h_mm": 1.6, "d_mm": 0.4}});
        let result = run_sim("sim_layout_calc", &args, &["kind", "params"]).await.unwrap();
        let body = text(&result);
        if result.is_error && body.contains("could not start the simulation runtime") {
            eprintln!("skipped: {body}");
            return;
        }
        assert!(!result.is_error, "{body}");
        let value: Value = serde_json::from_str(&body).unwrap();
        let nh = value["henry"].as_f64().unwrap() * 1e9;
        assert!((nh - 1.207).abs() < 0.01, "via inductance {nh} nH");
    }

    #[tokio::test]
    async fn only_declared_fields_are_forwarded() {
        let args = json!({"kind": "via_inductance", "params": {"h_mm": 1.6, "d_mm": 0.4}, "smuggled": 1});
        let result = run_sim("sim_layout_calc", &args, &["kind", "params"]).await.unwrap();
        let body = text(&result);
        if result.is_error && body.contains("could not start the simulation runtime") {
            return;
        }
        assert!(!result.is_error, "an undeclared field must be dropped, not passed on: {body}");
    }
}
'''

if __name__ == "__main__":
    main()
