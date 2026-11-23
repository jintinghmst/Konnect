"""Standard verification suites: every design of a circuit class gets the same checks,
in the same order, with the same charts, whatever its inputs and whatever fails."""
import json
import sys
from pathlib import Path

import pytest

SIM = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SIM))

from konnect_sim import design, suites, tools  # noqa: E402
from konnect_sim.report import FAIL, INCOMPLETE, PASS, Check  # noqa: E402
from konnect_sim.suites import base, buck  # noqa: E402

BUCK_CHECKS = ["R-01 Input range", "R-02 Output accuracy", "R-03 Output ripple", "R-04 Load transient",
               "R-05 Startup", "R-06 Loop stability", "R-07 Efficiency", "R-08 Thermal",
               "R-09 Short-circuit protection", "S-01 Switch-node and pin stress", "S-02 Inductor",
               "S-03 Input capacitor RMS current", "S-04 Input network", "S-05 Layout copper and parasitics",
               "S-06 Model validation"]
BUCK_CHARTS = ["Output voltage vs input voltage", "Output voltage window at the load",
               "Output ripple vs input voltage", "Input voltage and inductor current ripple vs input voltage",
               "Output voltage ripple (AC part)", "Input voltage ripple at the IC (AC part)", "Inductor current",
               "Switch-node voltage", "Load step response", "Start-up and restart", "Loop gain",
               "Efficiency vs input voltage", "Losses at Vin max, full load", "Board temperature",
               "Overload and output short", "Switch node and VIN pin", "Inductor currents against ratings",
               "Input capacitor RMS current vs input voltage", "Input network impedance", "Lumped layout parasitics",
               "SPICE against closed form"]


def test_an_empty_project_still_gets_every_check_and_chart(tmp_path):
    out = buck.run(tmp_path, 1)
    assert out["checks"] == BUCK_CHECKS
    assert out["charts"] == BUCK_CHARTS
    assert out["counts"] == {"PASS": 0, "FAIL": 0, "INCOMPLETE": len(BUCK_CHECKS)}
    data = json.loads(Path(out["json"]).read_text(encoding="utf-8"))
    assert all(c["to_complete"].startswith("needs ") for c in data["checks"])
    assert all(Path(f["path"]).is_file() for c in data["checks"] for f in c["figures"])
    assert data["suite"]["name"] == "buck" and data["suite"]["inputs_hash"] != "none"
    assert Path(out["pdf"]).read_bytes()[:5] == b"%PDF-"
    assert Path(out["inputs"]) == tmp_path / "iteration1/sim/inputs/buck_design.json"
    again = buck.run(tmp_path, 1)                      # same inputs: same list, same charts
    assert again["checks"] == out["checks"] and again["charts"] == out["charts"]


def test_a_failing_slot_is_reported_never_dropped(tmp_path):
    def boom(ctx):
        raise RuntimeError("ngspice gave up")

    def ok(ctx):
        c = Check("x", "<= 1", "0.5", PASS, "m", value=0.5, limit=1.0)
        return [c], {}

    slots = [base.Slot("R-01", "A", "m", ["Chart A"], boom), base.Slot("R-02", "B", "m", ["Chart B"], ok)]
    out = base.run_slots(slots, None, tmp_path)
    assert [c.name for c in out] == ["R-01 A", "R-02 B"]
    assert out[0].status == INCOMPLETE and "ngspice gave up" in out[0].to_complete
    assert out[1].status == PASS and [f["title"] for f in out[1].figures] == ["Chart B"]   # placeholder kept
    assert all(Path(f["path"]).is_file() for c in out for f in c.figures)


def test_combine_takes_the_worst_status_and_the_binding_margin():
    a = Check("a", "<= 1", "0.5", PASS, "m", sources=["s1"], value=0.5, limit=1.0)
    b = Check("b", "<= 1", "0.9", INCOMPLETE, "m", assumptions=["x"], value=0.9, limit=1.0)
    c = Check("c", "<= 1", "1.2", FAIL, "m", value=1.2, limit=1.0)
    assert base.combine("R", [a, b]).status == INCOMPLETE
    assert base.combine("R", [a, b]).value == 0.9                  # the smaller margin
    assert base.combine("R", [a, b, c]).status == FAIL
    assert base.combine("R", []).status == INCOMPLETE


def _spec(tmp_path, accept=True):
    R = design.Requirement
    reqs = [R("R-01", "Input range", "Vin", "Vin 18-32 V, Iout 0-1 A", "regulates"),
            R("R-02", "Output accuracy", "Vout", "all", "Within ±3 % of nominal"),
            R("R-03", "Output ripple", "Vpp", "Vin 18-32 V", "≤ 40 mVpp"),
            R("R-04", "Load transient", "dev", "Step 25 % to 75 % of Iout,max", "Deviation ≤ ±5 %; ≤ 100 µs"),
            R("R-06", "Loop stability", "PM", "corners", "Phase margin ≥ 50°; gain margin ≥ 8 dB"),
            R("R-07", "Light-load behaviour", "mode", "0-10 %", "ripple within R-03"),
            R("R-08", "Pin stress", "SW", "Vin max", "within abs max")]
    design.write_spec(tmp_path, "24 V to 5 V, 1 A buck", reqs, brief="24 V to 5 V output, 1 A max load")
    if accept:
        design.approve(tmp_path, design.SPEC, "user", "I accept the design")


def test_ids_and_limits_follow_the_accepted_specification(tmp_path):
    _spec(tmp_path)
    d = json.loads(buck.template(tmp_path, 1).read_text(encoding="utf-8"))
    lim = d["limits"]
    assert lim["Output ripple"]["max"]["value"] == pytest.approx(0.040)
    assert "R-03 rev 1 (accepted)" in lim["Output ripple"]["max"]["source"]
    assert lim["Output accuracy"]["pct"]["value"] == pytest.approx(0.03)
    assert lim["Loop stability"]["pm"]["value"] == 50 and lim["Loop stability"]["gm"]["value"] == 8
    assert lim["Load transient"]["settle"]["value"] == pytest.approx(100e-6)
    assert d["operating"]["vin_min"]["value"] == 18 and d["operating"]["vin_max"]["value"] == 32
    out = buck.run(tmp_path, 1)
    names = out["checks"]
    # the spec's own ids; light load added because the spec has it; pin stress takes its id
    assert "R-07 Light-load behaviour" in names and "R-08 Pin stress" not in names
    assert "R-08 Switch-node and pin stress" in names
    assert names.index("R-07 Light-load behaviour") == names.index("R-09 Short-circuit protection") + 1


def test_template_keeps_values_already_filled(tmp_path):
    path = buck.template(tmp_path, 1)
    d = json.loads(path.read_text(encoding="utf-8"))
    d["ic"]["fsw"] = {"value": 1.1e6, "unit": "Hz", "source": "datasheet p.6"}
    path.write_text(json.dumps(d), encoding="utf-8")
    d2 = json.loads(buck.template(tmp_path, 1).read_text(encoding="utf-8"))
    assert d2["ic"]["fsw"]["value"] == 1.1e6 and d2["ic"]["fsw"]["source"] == "datasheet p.6"


def test_the_tool_and_the_registry():
    assert suites.SUITES == {"buck": buck}
    with pytest.raises(ValueError):
        suites.run(".", "flyback")


def test_tool_writes_the_template_only_when_asked(tmp_path):
    out = tools.verify(str(tmp_path), write_template=True)
    assert Path(out["inputs"]).is_file()
    assert not list((tmp_path / "iteration1/reports").glob("*.pdf"))
