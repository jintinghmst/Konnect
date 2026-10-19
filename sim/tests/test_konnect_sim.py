"""Self-tests: the simulation package must agree with hand calculation.

Run from the repository root:  python -m pytest sim/tests -q
Requires KiCad 10 (for ngspice.dll), PySpice and numpy.
"""
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from konnect_sim import buck, report, spice, units  # noqa: E402


def test_value_parsing():
    v = units.parse_value("22uF 16V", "F")
    assert math.isclose(v.value, 22e-6) and v.voltage_rating == 16 and not v.dnp
    v = units.parse_value("470pF DNP", "F")
    assert math.isclose(v.value, 470e-12) and v.dnp
    v = units.parse_value("100k 0.1%", "Ω")
    assert math.isclose(v.value, 100e3) and v.tolerance_pct == 0.1
    assert math.isclose(units.parse_value("4k7").value, 4700)
    assert math.isclose(units.parse_value("2.2", "Ω").value, 2.2)


def test_rc_lowpass_matches_hand_calculation():
    res = spice.run_netlist("""rc
V1 a 0 DC 0 AC 1
R1 a out 1k
C1 out 0 10n
.ac dec 200 10 1meg
.end""")
    f = res["frequency"]
    mag = 20 * np.log10(np.abs(res["out"]))
    fc = np.interp(-3.0103, mag[::-1], f[::-1])
    assert abs(fc - 1 / (2 * math.pi * 1e3 * 10e-9)) / fc < 0.01


def test_divider_setpoint():
    assert math.isclose(buck.divider_vout(1.0, 100e3, 25e3), 5.0)
    lo, hi = buck.divider_vout_range(0.99, 1.01, 100e3, 25e3, 1, 1)
    assert lo < 5.0 < hi


@pytest.mark.slow
def test_switched_buck_agrees_with_closed_form():
    vin, vout, io, fsw, l_h = 24, 5, 5, 500e3, 6.8e-6
    tb = buck.BuckTestbench(vin, vout, io, fsw, l_h, 0.005,
                            [buck.CapBank(22e-6, 6, 0.003)], [buck.CapBank(4.7e-6, 4, 0.005)],
                            0.053, 0.031, source_r_ohm=0.05, source_l_h=10e-6, cycles=600)
    s = tb.run_at_setpoint()
    assert abs(s["vout_avg"] - vout) < 0.01
    assert abs(s["il_avg"] - io) / io < 0.02
    # ripple from the loss-adjusted duty the simulation converged to
    d = s["duty"]
    di = (vin - vout) * d / (l_h * fsw)
    assert abs(s["il_pp"] - di) / di < 0.05
    assert abs(s["icin_rms"] - buck.input_rms_current(io, d, di)) / s["icin_rms"] < 0.05


def test_report_status_rules():
    r = report.Report("t", "x")
    r.add(report.Check("a", "<= 1", "0.5", report.judge(True, assumed=False), "closed-form"))
    assert r.verdict == report.PASS
    r.add(report.Check("b", "<= 1", "0.5", report.judge(True, assumed=True), "closed-form"))
    assert r.verdict == report.INCOMPLETE
    r.add(report.Check("c", "<= 1", "2", report.judge(False, assumed=False), "closed-form"))
    assert r.verdict == report.FAIL


# ── layout extraction and the optimisation cycle ──────────────────────────

from konnect_sim import cycle, layout  # noqa: E402

FIXTURE = Path(__file__).resolve().parents[2] / "crates/konnect-sexp/tests/fixtures/pic_programmer.kicad_pcb"


@pytest.fixture(scope="module")
def pic_board():
    if not FIXTURE.exists():
        pytest.skip("pic_programmer fixture not present")
    return layout.load(FIXTURE)


def test_board_parse_and_rotation(pic_board):
    # Track ends land on pad centres of rotated footprints only with KiCad's
    # rotation convention; the mirrored convention misses most of them.
    pads = [p for f in pic_board.footprints.values() if f.rotation in (90.0, -90.0)
            for p in f.pads.values()]
    ends = [(s.net, x, y) for s in pic_board.segments for x, y in ((s.x1, s.y1), (s.x2, s.y2))]
    hits = sum(any(n == p.net and math.hypot(p.x - x, p.y - y) < 0.01 for n, x, y in ends) for p in pads)
    assert len(pic_board.footprints) == 63 and len(pic_board.segments) == 370
    assert hits >= 25


def test_path_resistance_matches_hand_calculation(pic_board):
    b = pic_board
    pads = [p for f in b.footprints.values() for p in f.pads.values()]
    for s in b.segments:
        a = [p for p in pads if p.net == s.net and p.on(s.layer) and math.hypot(p.x - s.x1, p.y - s.y1) < 0.01]
        c = [p for p in pads if p.net == s.net and p.on(s.layer) and math.hypot(p.x - s.x2, p.y - s.y2) < 0.01]
        if a and c and a[0].ref != c[0].ref:
            r = layout.path_resistance(b, s.net, (a[0].ref, a[0].number), (c[0].ref, c[0].number))
            hand = layout.sheet_resistance(b.copper(s.layer)) * s.length / s.width
            assert abs(r.resistance - hand) / hand < 1e-6
            return
    pytest.fail("no single-segment pad-to-pad track found")


def test_inductance_formulas():
    # circle-of-equal-area vs the exact square-loop formula, within 10 %
    side, w = 20.0, 1.0
    a = 0.2235 * (w + 0.035)
    exact = 2 * layout.MU0 * side * 1e-3 / math.pi * (math.log(side / a) - 0.774)
    est = layout.loop_inductance([(0, 0), (side, 0), (side, side), (0, side)], w)["henry"]
    assert abs(est - exact) / exact < 0.10
    # coaxial mutual inductance tends to the dipole limit far apart
    r, d = 1e-3, 20e-3
    assert abs(layout.coaxial_mutual(r, d) / (layout.MU0 * math.pi * r**4 / (2 * d**3)) - 1) < 0.02
    # a plane under the loop lowers its inductance; closer lowers it more
    sq = [(0, 0), (4, 0), (4, 3), (0, 3)]
    free, far, near = (layout.loop_inductance(sq, 0.6, plane_gap_mm=g)["henry"] for g in (None, 1.5, 0.2))
    assert free > far > near > 0
    assert abs(layout.via_inductance(1.6, 0.4) * 1e9 - 1.207) < 0.01
    assert abs(layout.ipc2221_width_mm(3, 10, 0.035) - 1.37) < 0.02


@pytest.mark.slow
def test_hot_loop_ring_frequency_matches_closed_form():
    tb = buck.BuckTestbench(13.2, 5, 3, 650e3, 3.3e-6, 0.02, [buck.CapBank(22e-6, 3, 0.002)],
                            [buck.CapBank(10e-6, 2, 0.003)], 0.068, 0.039, source_l_h=1e-6,
                            l_hot_h=3e-9, r_hot_ohm=0.005)
    r = tb.run_edge(0.397)
    assert abs(r["ring_hz"] - r["ring_hz_cf"]) / r["ring_hz_cf"] < 0.10


def _rep(values):
    r = report.Report("t", "x")
    for name, v in values.items():
        r.add(report.Check(name, "<= 1", str(v), report.INCOMPLETE, "test", value=v, limit=1.0))
    return r


def test_cycle_decisions(tmp_path):
    log = cycle.CycleLog.open(tmp_path / "c.json", max_iterations=6, patience=2)
    log.record(_rep({"a": 1.5, "b": 0.5}), "baseline", {}, {"l_hot": 5.0}, drc_errors=0)
    assert log.decide()[0] == "continue"
    log.record(_rep({"a": 0.9, "b": 1.2}), "move C1", {}, {"l_hot": 4.0}, drc_errors=0)
    assert log.decide() == ("revert", "the last change broke: b")
    log.iterations.pop()
    log.record(_rep({"a": 1.2, "b": 0.5}), "widen", {}, {"l_hot": 5.0}, drc_errors=1)
    assert log.decide()[0] == "revert"
    log.iterations.pop()
    log.record(_rep({"a": 0.8, "b": 0.5}), "move C1", {}, {"l_hot": 4.0}, drc_errors=0)
    assert log.decide()[0] == "continue"          # met, objective still improving
    # a design-only change that first meets the constraints must not end the
    # cycle just because it left the layout objectives unchanged
    first = cycle.CycleLog.open(tmp_path / "f.json")
    first.record(_rep({"a": 1.2}), "baseline", {}, {"l_hot": 5.0}, drc_errors=0)
    first.record(_rep({"a": 0.9}), "design fix", {}, {"l_hot": 5.0}, drc_errors=0)
    assert first.decide() == ("continue", "constraints meet; try an objective improvement")
    log.record(_rep({"a": 0.8, "b": 0.5}), "move C2", {}, {"l_hot": 3.9}, drc_errors=0)
    assert log.decide()[0] == "met"               # met, objective gain < 5 %
    assert cycle.CycleLog.open(tmp_path / "c.json").iterations[-1].change == "move C2"
    stuck = cycle.CycleLog.open(tmp_path / "s.json", patience=2)
    for i in range(3):
        stuck.record(_rep({"a": 1.5}), f"try {i}", {}, {"l_hot": 5.0}, drc_errors=0)
    assert stuck.decide()[0] == "stalled"
