"""Simulation toolset (served by Konnect through konnect_sim.cli): each tool
against an independent result, the expression sandbox, and the bridge
protocol Konnect uses (catalog, one call per process, structured errors)."""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

SIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM))

from konnect_sim import cli, layout, tools  # noqa: E402

FIXTURE = SIM.parent / "crates/konnect-sexp/tests/fixtures/pic_programmer.kicad_pcb"


def test_catalog_lists_every_tool_with_a_schema():
    cat = cli.catalog()
    assert len(cat) == len(cli.TOOLS) == 21
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
    rep = tools.write_report("t", "d", [c1], str(tmp_path), "r", documents=["md"],
                             figures=[{"title": ti, "path": p} for ti, p in figs])
    md = Path(rep["md"]).read_text(encoding="utf-8")
    assert "## Further figures" in md and "](figs/fig_ripple_summary.png)" in md
    assert "](r_figures/r_checks_vs_limits.png)" in md                 # every report opens with its checks chart
    js = json.loads(Path(rep["json"]).read_text())
    assert Path(rep["json"]).parent == tmp_path / "r_data"              # data kept out of the report's folder
    assert js["summary_figure"] == str((tmp_path / "r_figures/r_checks_vs_limits.png").resolve())
    assert js["figures"][0]["path"] == str((tmp_path / "figs/fig_ripple_summary.png").resolve())
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


def test_documents_carry_text_tables_and_figures(tmp_path):
    import docx
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from konnect_sim import document
    fig_dir = tmp_path / "sim" / "figures" / "it1"
    fig_dir.mkdir(parents=True)
    fig, ax = plt.subplots(); ax.plot([0, 1], [0, 1]); fig.savefig(fig_dir / "line.png"); plt.close(fig)
    md = tmp_path / "reports" / "r.md"
    md.parent.mkdir()
    md.write_text("# Buck report\n\nRipple is **5.7 mVpp** at 30 V, µ Ω ≤ ± °.  \nsecond line\n\n"
                  "- one\n  - nested\n1. first\n\n| Check | Status |\n|---|---|\n| ripple | **INCOMPLETE** |\n"
                  "| set-point | **PASS** |\n\n![Line chart](../sim/figures/it1/line.png)\n\n```\ncode block\n```\n",
                  encoding="utf-8")
    kinds = [b.kind for b in document.parse(md.read_text(encoding="utf-8"))]
    assert kinds == ["heading", "para", "list", "list", "list", "table", "image", "code"]
    out = tools.render_document(str(md), ["pdf", "docx"])
    assert Path(out["pdf"]).read_bytes()[:5] == b"%PDF-"
    d = docx.Document(out["docx"])
    assert len(d.tables) == 1 and d.tables[0].cell(2, 1).text == "PASS"
    assert len(d.inline_shapes) == 1 and "Figure 1. Line chart" in [p.text for p in d.paragraphs]
    fitz = pytest.importorskip("fitz")
    text = fitz.open(out["pdf"])[0].get_text()
    assert "5.7 mVpp" in text and "µ Ω ≤ ± °" in text and "INCOMPLETE" in text and "Figure 1. Line chart" in text
    md.write_text("![gone](nope.png)\n", encoding="utf-8")
    with pytest.raises(ValueError, match="nope.png"):
        tools.render_document(str(md))


def test_report_in_the_project_folders_links_figures_and_renders_pdf(tmp_path):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    from konnect_sim.project import Folders
    script = tmp_path / "proj" / "sim" / "scripts" / "eval.py"
    F = Folders.of_script(script, "it2").make()
    assert F.root == tmp_path / "proj" and F.figures == F.root / "sim/figures/it2" and F.reports.is_dir()
    fig, ax = plt.subplots(); ax.plot([0, 1]); fig.savefig(F.figures / "a.png"); plt.close(fig)
    c1 = tools.evaluate_check("a", "<= 1", 0.5, 1.0, "max", "m", {"x": {"value": 1, "source": "s"}})
    rep = tools.write_report("t", "d", [c1], str(F.reports), "r", figures=[{"title": "A", "path": str(F.figures / "a.png")}])
    assert Path(rep["pdf"]).stat().st_size > 5000 and "docx" not in rep and "md" not in rep
    assert sorted(x.name for x in F.reports.iterdir()) == ["r.pdf"]       # the reports folder holds only the PDF
    assert Path(rep["json"]) == F.root / "sim/results/r/r.json"
    fitz = pytest.importorskip("fitz")
    text = "".join(pg.get_text() for pg in fitz.open(rep["pdf"]))
    # summary first, then the tests, then figures not tied to a test
    assert text.index("Overall verdict: PASS") < text.index("Results by test") < text.index("1. a: PASS") \
        < text.index("Further figures")


def test_a_pipe_in_a_check_result_stays_in_its_table_cell(tmp_path):
    from konnect_sim import document
    c1 = tools.evaluate_check("pm", ">= 45", 60, 45, "min", "m", {"x": 1}, result="PM 60°; |Vx| < ripple")
    rep = tools.write_report("t", "d", [c1], str(tmp_path), "r", documents=[])
    table = next(b for b in document.parse(Path(rep["md"]).read_text(encoding="utf-8")) if b.kind == "table")
    assert [len(r) for r in table.rows] == [6, 6] and document.plain(table.rows[1][3]) == "PM 60°; |Vx| < ripple"
    assert "pdf" not in rep


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
    assert code == 0 and len(cat) == 21
    code, out = _bridge("sim_layout_calc", stdin=json.dumps({"kind": "via_inductance",
                                                               "params": {"h_mm": 1.6, "d_mm": 0.4}}))
    assert code == 0 and out["ok"] and out["result"]["henry"] * 1e9 == pytest.approx(1.207, abs=0.01)
    code, out = _bridge("sim_layout_calc", stdin=json.dumps({"kind": "via_inductance", "bogus": 1}))
    assert code == 0 and not out["ok"] and out["error"]["kind"] == "invalid_arguments"
    code, out = _bridge("sim_no_such_tool", stdin="{}")
    assert not out["ok"] and out["error"]["kind"] == "unknown_tool"
    code, out = _bridge("sim_layout_calc", stdin="{not json")
    assert code == 2 and out["error"]["kind"] == "invalid_json"


def test_design_records_hold_the_user_gates(tmp_path):
    import docx
    req = lambda i, cat, crit: {"id": i, "category": cat, "parameter": cat, "condition": "Vin 9-36 V, Iout 0-5 A",
                                "criterion": crit, "origin": "assumed", "verification": "sim_spice_run; bench"}
    spec = [req("R-01", "Input range", "9-36 V"), req("R-02", "Output ripple", "<= 50 mVpp")]
    part = {"role": "Buck IC", "part": "LM76005", "reasons": ["36 V input, 5 A"], "requirements": ["R-01"]}
    with pytest.raises(ValueError):                     # parts before an accepted spec
        tools.write_part_selection(str(tmp_path), "Buck", [part])
    with pytest.raises(ValueError):                     # an origin outside the evidence vocabulary
        tools.write_spec(str(tmp_path), "Buck", [spec[0] | {"origin": "guess"}])
    out = tools.write_spec(str(tmp_path), "Buck", spec, brief="24 V to 5 V", documents=["pdf", "docx"])
    assert out["revision"] == 1 and out["status"] == "draft" and Path(out["pdf"]).read_bytes()[:5] == b"%PDF-"
    assert docx.Document(out["docx"]).tables[0].cell(2, 4).text == "<= 50 mVpp"
    assert sorted(x.name for x in (tmp_path / "design").iterdir()) == [
        "01_specification.docx", "01_specification.pdf", "records"]
    assert "wait for the user" in tools.design_status(str(tmp_path))["next"]
    with pytest.raises(ValueError):                     # approval needs the user's words
        tools.approve_record(str(tmp_path), "specification", "user", " ")
    assert tools.approve_record(str(tmp_path), "specification", "user", "accept", documents=[])["status"] == "accepted"
    assert tools.write_spec(str(tmp_path), "Buck", spec, brief="24 V to 5 V", documents=[])["status"] == "accepted"
    with pytest.raises(ValueError):                     # an unknown requirement id
        tools.write_part_selection(str(tmp_path), "Buck", [part | {"requirements": ["R-09"]}], documents=[])
    p = tools.write_part_selection(str(tmp_path), "Buck", [part], documents=[])
    assert p["requirements_not_cited"] == ["R-02"]
    tools.approve_record(str(tmp_path), "part_selection", "user", "parts ok", documents=[])
    # revising the spec reopens it and the parts built on it
    r = tools.write_spec(str(tmp_path), "Buck", spec + [req("R-03", "Efficiency", ">= 90 %")], change="add R-03",
                         documents=[])
    assert r["revision"] == 2 and r["status"] == "draft" and r["part_selection_reopened"]
    with pytest.raises(ValueError):                     # parts still cite spec revision 1
        tools.approve_record(str(tmp_path), "part_selection", "user", "ok", documents=[])
    assert "specification is a draft" in tools.design_status(str(tmp_path))["next"]


def _png(fig) -> bool:
    return Path(fig["path"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n" and fig["title"]


def test_every_simulation_tool_draws_its_result(tmp_path):
    d = str(tmp_path)
    runs = {
        "spice ac": tools.spice_run("rc\nV1 a 0 DC 0 AC 1\nR1 a out 1k\nC1 out 0 10n\n.ac dec 20 10 1meg\n.end",
                                    [{"vector": "out", "kind": "at", "at": 15.9e3}], figures_dir=d),
        "spice tran": tools.spice_run("step\nV1 a 0 PULSE(0 1 0 1n 1n 1 2)\nR1 a out 1k\nC1 out 0 1n\n"
                                      ".tran 10n 5u\n.end", [{"vector": "out", "kind": "max"},
                                                             {"vector": "out", "kind": "avg"}], figures_dir=d),
        "corners": tools.corner_sweep([{"name": "L", "nom": 3.3e-6, "tol": 0.2}, {"name": "f", "nom": 6e5, "tol": 0.1}],
                                      {"di": "7 * 0.42 / (L * f)"}, figures_dir=d),
        "cap curve": tools.capacitor_check("C", 22e-6, 0.1, 16, 2, "out", 5.5, 5, dc_bias=[[0, 1], [5, 0.6], [16, 0.3]],
                                           dc_bias_source="vendor curve", figures_dir=d),
        "cap no curve": tools.capacitor_check("C", 4.7e-6, 0.1, 100, 3, "in", 30, 24, figures_dir=d),
        "inductor": tools.inductor_check("L", 4.7e-6, 0.2, 0.01, 0.012, 5, 1.5, 6.5, 4e5, 25, isat_a=8,
                                         l_vs_i=[[0, 4.7e-6], [6, 4.2e-6], [9, 3.3e-6]], figures_dir=d),
        "input": tools.input_network(0.05, 1e-6, [{"r": 0.002, "c": 20e-6}], 10.8, 13.2, 16.7, 1e5, 6, 19,
                                     figures_dir=d),
    }
    csv = tmp_path / "iin.csv"
    t = np.linspace(0, 10 / 4e5, 2000, endpoint=False)
    np.savetxt(csv, np.column_stack([t, 2 + np.sign(np.sin(2 * np.pi * 4e5 * t))]), delimiter=",",
               header="time,iin", comments="")
    runs["emi"] = tools.conducted_emissions(str(csv), "time", "iin", [[150e3, 66], [5e5, 56], [3e7, 60]],
                                            "CISPR 32 class B (test)", figures_dir=d)
    if FIXTURE.exists():
        b = layout.load(FIXTURE)
        pads = [(p.ref, p.number) for f in b.footprints.values() for p in f.pads.values() if p.net == "VCC"]
        runs["connectivity"] = tools.copper_connectivity(str(FIXTURE), "VCC", pitch_mm=0.3, figures_dir=d)
        runs["path"] = tools.copper_path(str(FIXTURE), "VCC", [pads[0]], [pads[-1]], current_a=0.5, pitch_mm=0.3,
                                         figures_dir=d)
        runs["coupling"] = tools.trace_coupling(str(FIXTURE), "VCC", "GND", figures_dir=d)
    for name, out in runs.items():
        if name == "path" and not out["connected"]:
            continue
        assert out["figures"] and all(_png(f) for f in out["figures"]), name
        for c in out.get("checks", []) + ([out["check"]] if "check" in out else []):
            assert c["figures"] == out["figures"], name               # each check carries its graph
    # the report embeds the checks' graphs and the summary chart, and names what has no graph
    plain = tools.evaluate_check("ripple", "<= 50 mV", 0.02, 0.05, "max", "closed form", {"x": 1})
    rep = tools.write_report("t", "d", runs["inductor"]["checks"] + [plain], str(tmp_path / "reports"), "r",
                             documents=["md"])
    titles = [f["title"] for f in rep["figures"]]
    assert titles[0] == "Checks against their limits" and "L: inductance vs current" in titles
    assert rep["ungraphed"] == ["ripple"]
    assert (tmp_path / "sim/figures/r/r_checks_vs_limits.png").is_file()   # beside the reports folder
    md = Path(rep["md"]).read_text(encoding="utf-8")
    assert "Checks without a graph of their own: ripple." in md
    # the inductor's checks share one graph: drawn under the first, referred to by number after
    assert md.count("inductor_L.png)") == 1
    assert "Graph: Figure 2, shown under test 1." in md
