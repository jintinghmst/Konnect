"""Simulation toolset (served by Konnect through konnect_sim.cli): each tool
against an independent result, the expression sandbox, and the bridge
protocol Konnect uses (catalog, one call per process, structured errors)."""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import pytest

SIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM))

from konnect_sim import cli, layout, tools  # noqa: E402

FIXTURE = SIM.parent / "crates/konnect-sexp/tests/fixtures/pic_programmer.kicad_pcb"


def test_catalog_lists_every_tool_with_a_schema():
    cat = cli.catalog()
    assert len(cat) == len(cli.TOOLS) == 16
    for t in cat:
        assert t["name"].startswith("sim_") and t["description"]
        sch = t["input_schema"]
        assert sch["type"] == "object" and set(sch["required"]) <= set(sch["properties"])
    sourced = next(t for t in cat if t["name"] == "sim_capacitor_check")["input_schema"]["properties"]["c_f"]
    assert any(o.get("properties", {}).get("source") for o in sourced["anyOf"])   # {value, source} accepted


def test_layout_calc_matches_library():
    r = tools.layout_calc("via_inductance", {"h_mm": 1.6, "d_mm": 0.4})
    assert r["henry"] * 1e9 == pytest.approx(1.207, abs=0.01)
    w = tools.layout_calc("ipc2221_width", {"current_a": 3, "rise_c": 10, "copper_mm": 0.035})
    assert w["width_mm"] == pytest.approx(1.37, abs=0.02)
    with pytest.raises(ValueError):
        tools.layout_calc("nonsense", {})


def test_corner_sweep_and_expression_sandbox():
    out = tools.corner_sweep(
        [{"name": "L", "nom": 3.3e-6, "tol": 0.2, "source": "datasheet"},
         {"name": "fsw", "nom": 650e3, "tol": 0.15}],
        {"di": "7 * 0.42 / (L * fsw)"})
    m = out["metrics"]["di"]
    assert m["max"] == pytest.approx(7 * 0.42 / (3.3e-6 * 0.8 * 650e3 * 0.85))
    assert m["max_corner"] == "L=lo,fsw=lo" and out["assumed_spreads"] == ["fsw"]
    for bad in ("__import__('os').system('x')", "L.__class__", "open('f')"):
        with pytest.raises(ValueError):
            tools.corner_sweep([{"name": "L", "nom": 1, "tol": 0.1}], {"x": bad})


def test_evaluate_check_follows_evidence_rule():
    ok = tools.evaluate_check("ripple", "<= 50 mV", 0.02, 0.05, "max", "SPICE",
                              {"ESR": {"value": 0.002, "source": "Murata curve"}})
    assert ok["status"] == "PASS" and ok["meets"]
    assumed = tools.evaluate_check("ripple", "<= 50 mV", 0.02, 0.05, "max", "SPICE", {"ESR": 0.002})
    assert assumed["status"] == "INCOMPLETE" and assumed["assumptions"] == ["ESR"]


def test_capacitor_self_heating_uses_ripple_and_esr():
    out = tools.capacitor_check("C", 4.7e-6, 0.1, 100, 3, "in", 30, 24, esr_ohm=0.005, irms_part_a=0.75)
    heat = next(c for c in out["checks"] if c["name"].endswith("self-heating"))
    assert heat["result"].startswith("2.8 mW")                    # 0.75 A^2 x 5 mOhm


def test_spice_run_rc_corner():
    net = "rc\nV1 a 0 DC 0 AC 1\nR1 a out 1k\nC1 out 0 10n\n.ac dec 100 10 1meg\n.end"
    fc = 1 / (2 * math.pi * 1e3 * 10e-9)
    out = tools.spice_run(net, [{"vector": "out", "kind": "at", "at": fc}])
    assert out["axis"] == "frequency"
    assert out["measures"][0]["value"] == pytest.approx(1 / math.sqrt(2), rel=0.01)


def test_input_network_hot_plug_branches():
    out = tools.input_network({"value": 0.05, "source": "spec"}, {"value": 1e-6, "source": "spec"},
                              [{"r": 0.002, "c": 20e-6}, {"r": 0.85, "c": 47e-6}], 10.8, 13.2, 16.7, 100e3, 6, 19)
    names = [c["name"] for c in out["checks"]]
    assert "Hot-plug input overshoot" in names
    hp = next(c for c in out["checks"] if c["name"].startswith("Hot-plug"))
    assert hp["value"] < 2 * 13.2 and hp["status"] == "INCOMPLETE"   # bare numbers are assumptions


@pytest.mark.skipif(not FIXTURE.exists(), reason="KiCad demo board fixture not present")
def test_copper_tools_on_demo_board():
    b = layout.load(FIXTURE)
    seg = next(s for s in b.segments if s.net == "VCC")
    pads = [(p.ref, p.number) for f in b.footprints.values() for p in f.pads.values() if p.net == "VCC"]
    con = tools.copper_connectivity(str(FIXTURE), "VCC", pitch_mm=0.2)
    assert con["net"] == "VCC" and con["groups"]
    path = tools.copper_path(str(FIXTURE), "VCC", [pads[0]], [pads[-1]], current_a=0.5, rms_a=0.5, rise_c=10,
                             pitch_mm=0.2)
    if path["connected"]:
        assert path["resistance_ohm"] > 0 and path["drop_v"] == pytest.approx(path["resistance_ohm"] * 0.5)
        assert path["capacity"]["status"] == "INCOMPLETE"          # via plating is assumed
    st = tools.board_stackup(str(FIXTURE))
    assert st["sourced"] and st["copper_layers"] == ["F.Cu", "B.Cu"]


def test_report_and_cycle_roundtrip(tmp_path):
    c1 = tools.evaluate_check("a", "<= 1", 0.5, 1.0, "max", "m", {"x": {"value": 1, "source": "s"}})
    rep = tools.write_report("t", "d", [c1], str(tmp_path), "r")
    assert rep["verdict"] == "PASS" and Path(rep["json"]).exists()
    d = tools.cycle_record(str(tmp_path / "cycle.json"), rep["json"], "baseline", [], {"l_hot_nH": 2.0}, 0, 0)
    assert d["decision"] in ("continue", "met") and d["iterations"] == 1


def test_report_embeds_figures_relative_to_the_report(tmp_path):
    import numpy as np
    from konnect_sim import figures
    period = 1 / 400e3
    t = np.linspace(0, 3 * period, 300)
    on = (t % period) < 0.2 * period
    snap = np.vstack([t, 24 - 0.1 * np.sin(2 * np.pi * t / period), 5 + 0.002 * np.sin(2 * np.pi * t / period),
                      5 + 0.7 * np.sin(2 * np.pi * t / period), np.where(on, 24.0, 0.0)])
    idle = snap.copy(); idle[4] = 5.0                       # never switches: pulse skipping
    snaps = {"24V_5A": snap, "24V_0A": idle}
    figs = figures.ripple_figures(tmp_path / "figs", snaps, [24.0], [0.0, 5.0], vout_pp_max=0.03,
                                  vin_pp_max=0.5, il_pp_target=1.5)
    assert [t for t, _ in figs][0] == "Ripple summary" and all(Path(p).stat().st_size > 1000 for _, p in figs)
    c1 = tools.evaluate_check("a", "<= 1", 0.5, 1.0, "max", "m", {"x": {"value": 1, "source": "s"}})
    rep = tools.write_report("t", "d", [c1], str(tmp_path), "r",
                             figures=[{"title": ti, "path": p} for ti, p in figs])
    md = Path(rep["markdown"]).read_text(encoding="utf-8")
    assert "## Figures" in md and "](figs/fig_ripple_summary.png)" in md
    assert json.loads(Path(rep["json"]).read_text())["figures"][0]["path"] == "figs/fig_ripple_summary.png"
    with pytest.raises(ValueError):
        tools.write_report("t", "d", [c1], str(tmp_path), "r2", figures=[{"title": "x", "path": "nope.png"}])


def test_ripple_figures_report_the_checked_window_not_the_drawn_cycles(tmp_path, monkeypatch):
    import numpy as np
    from konnect_sim import figures
    period = 1 / 400e3
    t = np.linspace(0, 20 * period, 4000)
    vout = 5 + 0.002 * np.sin(2 * np.pi * t / period) + np.where(t < 2 * period, 0.004, 0.0)  # early excursion
    snap = np.vstack([t, 24 + 0 * t, vout, 5 + 0 * t, np.where((t % period) < 0.2 * period, 24.0, 0.0)])
    seen = {}
    monkeypatch.setattr(figures, "metric_vs_x", lambda path, x, panels, **kw: seen.setdefault("p", panels) and str(path))
    monkeypatch.setattr(figures, "waveform_grid", lambda path, get, **kw: seen.setdefault(Path(path).name, get) and str(path))
    figures.ripple_figures(tmp_path, {"24V_5A": snap}, [24.0], [5.0], period=period, draw_cycles=3)
    full = float(np.ptp(vout))
    assert seen["p"][0]["series"]["Vin 24 V"][0] == pytest.approx(full * 1e3)
    t_drawn, _, pp = seen["fig_ripple_vout.png"](24.0, 5.0)
    assert pp == pytest.approx(full) and t_drawn[-1] - t_drawn[0] == pytest.approx(3 * period, rel=0.01)


def test_rust_toolset_is_generated_from_the_current_catalog():
    p = subprocess.run([sys.executable, str(SIM / "gen_rust_toolset.py"), "--check"], capture_output=True,
                       text=True, cwd=str(SIM.parent), timeout=120)
    assert p.returncode == 0, p.stdout + p.stderr


def _bridge(*args, stdin=""):
    p = subprocess.run([sys.executable, "-m", "konnect_sim.cli", *args], input=stdin, capture_output=True,
                       text=True, cwd=str(SIM), env=dict(os.environ, PYTHONPATH=str(SIM)), timeout=120)
    return p.returncode, json.loads(p.stdout)


def test_bridge_protocol_as_konnect_calls_it():
    code, cat = _bridge("--catalog")
    assert code == 0 and len(cat) == 16
    code, out = _bridge("sim_layout_calc", stdin=json.dumps({"kind": "via_inductance",
                                                               "params": {"h_mm": 1.6, "d_mm": 0.4}}))
    assert code == 0 and out["ok"] and out["result"]["henry"] * 1e9 == pytest.approx(1.207, abs=0.01)
    code, out = _bridge("sim_layout_calc", stdin=json.dumps({"kind": "via_inductance", "bogus": 1}))
    assert code == 0 and not out["ok"] and out["error"]["kind"] == "invalid_arguments"
    code, out = _bridge("sim_no_such_tool", stdin="{}")
    assert not out["ok"] and out["error"]["kind"] == "unknown_tool"
    code, out = _bridge("sim_layout_calc", stdin="{not json")
    assert code == 2 and out["error"]["kind"] == "invalid_json"
