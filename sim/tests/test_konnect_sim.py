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
