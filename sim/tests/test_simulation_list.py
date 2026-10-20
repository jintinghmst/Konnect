"""Self-tests for the extended simulation set (copper, stackup, parts, losses,
thermal, closed-loop converter, stress, input network, EMI, corners, budget).

Each model is checked against an independent calculation. Run from the
repository root:  python -m pytest sim/tests -q   (add -m "not slow" to skip
the closed-loop runs).
"""
import json
import math
import sys
from pathlib import Path

import numpy as np
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from konnect_sim import (budget, converter as cv, copper, corners, emi, inputfilter, layout,  # noqa: E402
                         losses, parts, report, stackup, stress, thermal)
from konnect_sim.layout import Board, Footprint, Pad, Segment, Via, Zone  # noqa: E402
from konnect_sim.report import Param  # noqa: E402

STACK = [("F.Cu", "copper", 0.035), ("core", "dielectric", 1.53), ("B.Cu", "copper", 0.035)]
FIXTURE = Path(__file__).resolve().parents[2] / "crates/konnect-sexp/tests/fixtures/pic_programmer.kicad_pcb"


def _board(footprints, segments=(), vias=(), zones=()):
    return Board(None, {f.ref: f for f in footprints}, list(segments), list(vias), list(zones),
                 {"F.Cu": 0.035, "B.Cu": 0.035}, 1.6, True, STACK)


def _fp(ref, x, y, size=(0.6, 0.6), layer="F.Cu", net="N"):
    f = Footprint(ref, "", 0, 0, 0, layer)
    f.pads["1"] = Pad(ref, "1", net, x, y, size, [layer])
    return f


# ── report provenance ─────────────────────────────────────────────────────

def test_check_status_follows_provenance():
    ok = report.check("a", "<= 1", "0.5", True, "m", {"x": Param(1, "", "datasheet p.5")})
    assert ok.status == report.PASS and ok.sources == ["datasheet p.5"]
    assumed = report.check("a", "<= 1", "0.5", True, "m", {"x": Param(1), "y": 2.0})
    assert assumed.status == report.INCOMPLETE and set(assumed.assumptions) == {"x", "y"}
    no_model = report.check("a", "<= 1", "0.5", True, "m", {"x": Param(1, "", "s")}, missing_model="vendor model")
    assert no_model.status == report.INCOMPLETE
    assert report.check("a", "<= 1", "2", False, "m", {"x": Param(1, "", "s")}).status == report.FAIL


# ── stackup ───────────────────────────────────────────────────────────────

def test_stackup_json_and_board(tmp_path):
    data = {"source": "fab stackup sheet rev 3", "layers": [
        {"name": "F.Cu", "type": "copper", "thickness_mm": 0.035},
        {"name": "pp", "type": "prepreg", "thickness_mm": 0.2, "er": 4.4},
        {"name": "In1.Cu", "type": "copper", "thickness_mm": 0.0175},
        {"name": "core", "type": "core", "thickness_mm": 1.065, "er": 4.6},
        {"name": "In2.Cu", "type": "copper", "thickness_mm": 0.0175},
        {"name": "pp2", "type": "prepreg", "thickness_mm": 0.2, "er": 4.4},
        {"name": "B.Cu", "type": "copper", "thickness_mm": 0.035}]}
    p = tmp_path / "s.json"
    p.write_text(json.dumps(data))
    s = stackup.from_json(p)
    assert s.gap_mm("F.Cu", "In1.Cu").value == pytest.approx(0.2) and s.gap_mm("F.Cu", "In1.Cu").sourced
    assert s.er("F.Cu", "In2.Cu").value == pytest.approx((0.2 * 4.4 + 1.065 * 4.6) / 1.265)
    b = _board([])
    s.apply(b)
    assert b.stackup_sourced and b.copper("In1.Cu") == 0.0175 and b.layer_gap_mm("F.Cu", "In1.Cu") == pytest.approx(0.2)
    p.write_text(json.dumps({"layers": data["layers"]}))
    with pytest.raises(ValueError):
        stackup.from_json(p)
    if FIXTURE.exists():
        fb = stackup.from_board(FIXTURE)
        assert fb is not None and fb.copper_layers == ["F.Cu", "B.Cu"] and fb.source.startswith("board stackup")


# ── copper flow ───────────────────────────────────────────────────────────

def test_copper_plate_track_and_via_match_hand():
    rs = layout.sheet_resistance(0.035)
    plate = Zone("N", ["F.Cu"], [], True, {"F.Cu": [[(0, 0), (20, 0), (20, 5), (0, 5)]]})
    b = _board([_fp("A", 0.5, 2.5, (1, 5)), _fp("B", 19.5, 2.5, (1, 5))], zones=[plate])
    f = copper.solve(copper.build(b, "N", 0.1), [("A", "1")], [("B", "1")])
    assert abs(f.resistance_ohm - rs * 18 / 5) / (rs * 18 / 5) < 0.02     # between the pads' inner edges
    b = _board([_fp("A", 0, 0), _fp("B", 10, 0)], [Segment(0, 0, 10, 0, 0.3, "F.Cu", "N")])
    f = copper.solve(copper.build(b, "N", 0.05), [("A", "1")], [("B", "1")])
    hand = rs * 9.4 / 0.3
    assert abs(f.resistance_ohm - hand) / hand < 0.03
    b = _board([_fp("A", 0, 0), _fp("B", 10, 0, layer="B.Cu")],
               [Segment(0, 0, 5, 0, 1.0, "F.Cu", "N"), Segment(5, 0, 10, 0, 1.0, "B.Cu", "N")],
               [Via(5, 0, 0.8, 0.4, "N")])
    f = copper.solve(copper.build(b, "N", 0.05), [("A", "1")], [("B", "1")])
    hand = rs * 4.3 * 2 + layout.via_resistance(1.6, 0.4)
    assert abs(f.resistance_ohm - hand) / hand < 0.05
    assert len(f.via_currents) == 1 and f.via_currents[0][1] == pytest.approx(1.0, rel=1e-6)
    cap = copper.capacity(f, 3.0, 10)
    assert not cap["vias_ok"]                        # one 0.4 mm via cannot carry 3 A at 10 K rise


def test_copper_capacity_is_grid_independent_at_a_bend():
    # an L-shaped 1 mm track: the raw density at the inner corner is singular
    # (it grows as the grid is refined); the 1 mm-averaged peak must not
    segs = [Segment(0, 0, 10, 0, 1.0, "F.Cu", "N"), Segment(10, 0, 10, 10, 1.0, "F.Cu", "N")]
    b = _board([_fp("A", 0, 0), _fp("B", 10, 10)], segs)
    caps = [copper.capacity(copper.solve(copper.build(b, "N", p), [("A", "1")], [("B", "1")]), 2.0, 10)
            for p in (0.1, 0.05)]
    assert caps[1]["raw_peak_a_per_mm"] > caps[0]["raw_peak_a_per_mm"] * 1.1
    assert abs(caps[1]["peak_a_per_mm"] / caps[0]["peak_a_per_mm"] - 1) < 0.08
    assert 0.85 < caps[1]["equivalent_width_mm"] < 1.0       # close to the 1 mm track width


def test_copper_split_plane_is_not_connected():
    halves = Zone("N", ["F.Cu"], [], True, {"F.Cu": [[(0, 0), (9, 0), (9, 5), (0, 5)],
                                                      [(11, 0), (20, 0), (20, 5), (11, 5)]]})
    b = _board([_fp("A", 1, 2.5), _fp("B", 19, 2.5)], zones=[halves])
    g = copper.build(b, "N", 0.2)
    con = copper.connectivity(g, [("A", "1"), ("B", "1")])
    assert not con["connected"] and len(con["groups"]) == 2
    assert not copper.solve(g, [("A", "1")], [("B", "1")]).connected


# ── parts ─────────────────────────────────────────────────────────────────

def test_capacitor_effective_c_and_share():
    c = parts.Capacitor("X", Param(22e-6, "F", "ds"), Param(0.1, "", "ds"), Param(10, "V", "ds"), 3,
                        dc_bias=[(0, 1.0), (5, 0.5), (10, 0.3)], dc_bias_source="vendor curve",
                        temp_coef=Param(0.15, "", "ds"), esr=Param(0.002, "ohm", "ds"), esl=Param(0.5e-9, "H", "ds"))
    ce = c.c_eff(5.0)
    assert ce.value == pytest.approx(22e-6 * 0.5 * 0.9 * 0.85) and ce.sourced
    # two identical banks share any waveform equally
    t = np.linspace(0, 1 / 650e3, 400, endpoint=False)
    tri = np.interp(t, [0, 0.4 / 650e3, 1 / 650e3], [-0.7, 0.7, -0.7])
    sh = parts.harmonic_share(tri, t[1] - t[0], [c, c], 5.0)
    assert sh[0]["irms_part"] == pytest.approx(sh[1]["irms_part"])
    total = sum(s["irms_part"] * 3 for s in sh)          # identical parts: in-phase currents add
    assert total == pytest.approx(np.sqrt(np.mean((tri - tri.mean()) ** 2)), rel=0.02)
    chk = parts.capacitor_checks(c, "Cout", v_max=Param(5.1, "V", "sim"), irms_part=0.1, loss_part_w=1e-4,
                                 c_needed=Param(20e-6, "F", "spec"), v_dc=5.0, derating=Param(0.8, "", "spec"))
    assert chk[0].status == report.PASS and chk[1].status == report.PASS
    assert chk[-1].status == report.INCOMPLETE              # no heating curve given


def test_inductor_checks_need_curves():
    ind = parts.Inductor("L", Param(3.3e-6, "H", "ds"), Param(0.2, "", "ds"), Param(0.02, "ohm", "ds"),
                         Param(0.024, "ohm", "ds"), isat=Param(6.0, "A", "ds", "30 % drop"),
                         irms=Param(5.0, "A", "ds"), irms_rise=Param(40, "K", "ds"))
    out = parts.inductor_checks(ind, i_dc=3, di_pp=1.4, i_peak_worst=Param(5.0, "A", "datasheet limit"),
                                fsw=650e3, ambient_c=Param(25, "C", "spec"))
    by = {c.name: c for c in out}
    assert by["Inductor saturation margin"].status == report.PASS
    assert by["Inductance at peak current"].status == report.INCOMPLETE   # no L(I) curve
    assert by["Inductor losses"].status == report.INCOMPLETE              # no core-loss data


# ── losses and thermal ────────────────────────────────────────────────────

def test_loss_budget_conduction_and_fit():
    ps = losses.PowerStage(Param(0.068), Param(0.039), Param(0.0))
    op = losses.Operating(12, 5, 3, 650e3, 3.3e-6, 25)
    b, eta = losses.budget(ps, op)
    d = b.duty
    sq = 9 + b.di_pp ** 2 / 12
    assert b.parts["ic:conduction high side"] == pytest.approx(d * sq * 0.068)
    assert eta == pytest.approx(15 / (15 + b.p_loss))
    full = losses.PowerStage(Param(0.068), Param(0.039), Param(0.004), Param(6e-9), Param(6e-9),
                             Param(3e-10), Param(3e-10), Param(1e-8), Param(0.7), Param(1e-8), Param(5e-4))
    pts = [(losses.Operating(12, 5, i, 650e3, 3.3e-6, 25), losses.budget(full, losses.Operating(12, 5, i, 650e3, 3.3e-6, 25))[1])
           for i in (0.5, 1.5, 3.0)]
    start = losses.PowerStage(**{**full.__dict__, "t_rise": None, "t_fall": None})
    fit, resid = losses.fit_switching_time(start, pts, "vendor curve")
    assert fit.t_rise.value == pytest.approx(6e-9, rel=1e-3) and resid < 1e-6 and fit.t_rise.sourced


def test_thermal_uniform_flux_matches_hand():
    f = Footprint("H", "", 10, 10, 0, "F.Cu")
    f.pads["1"] = Pad("H", "1", "", 10, 10, (20, 20), ["F.Cu"])
    b = _board([f])
    h, kin, kth = Param(10.0), Param(0.8), Param(0.3)
    r = thermal.solve(b, ambient_c=25, h_w_m2k=h, k_laminate_inplane=kin, k_laminate_through=kth,
                      footprint_watts={"H": 1.0}, outline=(0, 0, 20, 20), pitch_mm=1.0)
    area = 0.02 * 0.02
    # both faces at h, plus half the flux crossing the laminate to the far face
    hand = 1.0 / (2 * 10 * area) + 0.5 * 1.53e-3 / (0.3 * area) / 2
    assert r.peak()[0] - 25 == pytest.approx(hand, rel=0.01)


# ── closed-loop converter ─────────────────────────────────────────────────

FSW = 650e3


def _plant(**kw):
    return cv.Plant(3.3e-6, 0.02, [cv.Bank(10e-6, 0.003, 2)], [cv.Bank(22e-6, 0.002, 3)], 0.068, 0.039,
                    54.9e3, 10e3, **({"src_r_ohm": 0.05, "src_l_h": 1e-6} | kw))


class _Fixed(cv._Base):
    def __init__(self, d):
        super().__init__(cv.Common(vref=1, t_ss=0))
        self.d = d

    def update(self, m, dt):
        n, ph = divmod(m.t * FSW + 1e-9, 1.0)
        self.hs = ph < self.d - 10e-9 * FSW
        self.ls = self.d <= ph < 1 - 10e-9 * FSW
        edges = [self.d - 10e-9 * FSW, self.d, 1 - 10e-9 * FSW, 1.0]
        self.next_t = min((n + e) / FSW for e in edges if (n + e) / FSW > m.t + 1e-12)


def test_switched_model_open_loop_matches_hand():
    s = cv.Sim(_plant(), _Fixed(0.4385), vin=12, load=cv.Load.current(3.0), fsw_hint=FSW, v0_out=4.95)
    s.x[0], s.idle = 3.0, False
    w = s.run(300 / FSW)
    st = cv.steady(w, 280 / FSW)
    # averaged switch node: Ton (Vin during the on-time - I Rhs) - Tls I Rls - dead-time
    # diode, minus the DCR drop. The input caps sag while the high side conducts, so the
    # relevant input voltage is its average over the on-time, not over the period.
    T, td = 1 / FSW, 10e-9
    on = w.window(280 / FSW) & (w.vsw > 6)
    vin_c = float(w.vin[on].mean()) 
    ton, tls = 0.4385 * T - td, (1 - 0.4385) * T - td
    v_hand = (ton * (vin_c - 3 * 0.068) - tls * 3 * 0.039 - 2 * td * (0.7 + 3 * 0.02)) / T - 3 * 0.02
    assert abs(st["vout_avg"] - v_hand) < 0.02
    di = (vin_c - 3 * 0.068 - st["vout_avg"] - 0.06) * ton / 3.3e-6
    assert abs(st["il_pp"] - di) / di < 0.03


def _cot(**kw):
    com = cv.Common(vref=0.765, t_ss=0.0, **kw)
    c = cv.COT(com, fsw=FSW, vout_set=4.965, t_off_min=220e-9, i_valley_limit=3.5, k_ramp=0.015, tau_ramp=20e-6)
    c.state, c.vref_now, c.pulsed, c.phase = "run", 0.765, True, "ls"
    return c


def test_cot_regulates_and_skips_pulses():
    for i_load, cont in ((3.0, True), (0.05, False)):
        s = cv.Sim(_plant(), _cot(), vin=12, load=cv.Load.current(i_load), fsw_hint=FSW, v0_out=5.0)
        s.x[0], s.idle = i_load, False
        s.c.il_lp = i_load
        st = cv.steady(s.run(400e-6), 300e-6)
        assert abs(st["vout_avg"] - 4.965) / 4.965 < 0.015           # ripple-regulated COT sits a little high
        if cont:
            assert 0.9 * FSW < st["pulse_hz"] < 1.15 * FSW and st["il_min"] > 0
        else:
            assert st["pulse_hz"] < 0.2 * FSW and st["il_min"] > -0.05   # DCM: pulse skipping


@pytest.mark.slow
def test_injected_loop_gain_matches_analytic_voltage_mode():
    L, C, esr, dcr, vin = 3.3e-6, 66e-6, 0.002 / 3, 0.02, 12.0
    w0, wp1, wp2 = 1 / math.sqrt(L * C), 1 / (esr * C), math.pi * FSW
    hdiv = 10 / 64.9

    def t_an(f, g):
        s = 2j * math.pi * f
        num, den = cv.type3(g, w0, w0, wp1, wp2)
        zl, zc = s * L + dcr + 0.42 * 0.068 + 0.58 * 0.039, esr + 1 / (s * C)
        return np.polyval(num, s) / np.polyval(den, s) * vin * zc / (zl + zc) * hdiv

    g = 1 / abs(t_an(40e3, 1.0))
    c = cv.VoltageMode(cv.Common(vref=0.765, t_ss=0), fsw=FSW, comp=cv.type3(g, w0, w0, wp1, wp2), v_ramp=1.0)
    c.state, c.pulsed = "run", True
    s = cv.Sim(_plant(src_r_ohm=1e-3, src_l_h=0.0), c, vin=vin, load=cv.Load.current(2.0), fsw_hint=FSW, v0_out=4.965)
    s.x[0], s.idle = 2.0, False
    s.run(400e-6)
    pts = cv.loop_gain(s, [5e3, 20e3, 40e3], lambda f: 0.15 if f < 10e3 else 0.03)
    for p in pts:
        assert p["snr_ok"]
        ta = t_an(p["f"], g)
        assert abs(p["mag_db"] - 20 * math.log10(abs(ta))) < 2.5
        assert abs(p["phase_deg"] - math.degrees(np.angle(ta))) < 12
    m = cv.margins(pts)
    assert 35e3 < m["fc_hz"] < 45e3 and 50 < m["phase_margin_deg"] < 75


def test_margins_interpolation():
    pts = [{"f": 1e3, "mag_db": 20, "phase_deg": -90}, {"f": 1e4, "mag_db": 0.0001, "phase_deg": -120},
           {"f": 1e5, "mag_db": -20, "phase_deg": 170}]          # +170 is -190 unwrapped
    m = cv.margins(pts)
    assert m["phase_margin_deg"] == pytest.approx(60, abs=0.5)
    assert m["f180_hz"] is not None and m["gain_margin_db"] > 0


# ── stress, input network, EMI, corners, budget ───────────────────────────

def test_excursions_and_skin_depth():
    t = np.linspace(0, 100e-9, 1001)
    v = np.where((t > 20e-9) & (t < 25e-9), 21.0, 12.0)
    ex = stress.excursions(t, v, 19.0, "max")
    assert ex["peak"] == 21.0 and ex["longest_s"] == pytest.approx(5e-9, abs=0.2e-9) and ex["count"] == 1
    # 35 um copper at 100 MHz: delta = 6.6 um -> R_ac / R_dc = t / delta
    delta = math.sqrt(1.72e-8 * (1 + 0.00393 * 5) / (math.pi * 1e8 * layout.MU0))
    assert stress.skin_resistance(1.0, 0.035, 1e8) == pytest.approx(35e-6 / delta, rel=1e-6)


def test_hot_plug_and_middlebrook():
    hp = inputfilter.hot_plug(12.0, 1e-4, 1e-6, 20e-6)
    assert 23.0 < hp["v_peak"] <= 24.0                     # lightly damped: nearly twice the supply
    net = inputfilter.SourceNetwork(0.01, 1e-6, [inputfilter.Branch(0.003 / 2, 0, 20e-6)])
    mb = inputfilter.middlebrook(net, vin_min=10.8, p_in_max=16.7, f_max=100e3)
    f_res = 1 / (2 * math.pi * math.sqrt(1e-6 * 20e-6))
    assert abs(mb["f_peak_hz"] - f_res) / f_res < 0.03
    z0 = math.sqrt(1e-6 / 20e-6)
    assert mb["z_peak_ohm"] == pytest.approx(z0 ** 2 / (0.01 + 0.0015), rel=0.1)   # Q-limited peak


def test_emi_harmonics_and_limit():
    f0 = 650e3
    t = np.linspace(0, 20 / f0, 20000, endpoint=False)
    sq = np.where((t * f0) % 1 < 0.5, 1.0, -1.0)
    f, a = emi.harmonics(t, sq)
    k = np.argmin(abs(f - f0))
    assert a[k] == pytest.approx(4 / math.pi, rel=0.01)
    spec = {"f": np.array([1e6, 2e6]), "dbuv": np.array([50.0, 40.0])}
    lm = emi.limit_margin(spec, [(150e3, 66.0), (5e6, 56.0)])
    assert lm["at_hz"] == 1e6 and lm["margin_db"] < 16
    # mutual capacitance falls with separation
    assert emi.coupled_c_per_m(0.3, 0.3, 1.0, 1.5, 4.0) > emi.coupled_c_per_m(0.3, 0.3, 3.0, 1.5, 4.0) > 0


def test_corners_and_budget():
    sp = [corners.Spread.tol("L", 3.3e-6, 0.2, "ds"), corners.Spread.tol("fsw", 650e3, 0.15, "ds")]
    cases = corners.vertices(sp)
    assert len(cases) == 5
    res = corners.sweep(lambda c: {"di": 7 * 0.42 / (c["L"] * c["fsw"])}, cases)
    case, di = corners.worst(res, "di")
    assert case["_corner"] == "L=lo,fsw=lo" and di == pytest.approx(7 * 0.42 / (3.3e-6 * 0.8 * 650e3 * 0.85))
    terms = dict(v_set_min=Param(4.95, "V", "s"), v_set_max=Param(5.05, "V", "s"), drop_power=Param(0.006, "V", "s"),
                 drop_ground=Param(0.004, "V", "s"), reg_down=Param(0.01, "V", "s"), reg_up=Param(0.01, "V", "s"),
                 ripple_pp=Param(0.01, "V", "s"), dip=Param(0.08, "V", "s"), overshoot=Param(0.1, "V", "s"))
    w = budget.window(**terms)
    assert w["v_min"] == pytest.approx(4.95 - 0.006 - 0.004 - 0.01 - 0.005 - 0.08)
    assert w["v_max"] == pytest.approx(5.05 + 0.01 + 0.005 + 0.1)
    out = budget.checks(Param(4.84, "V", "spec"), Param(5.15, "V", "spec"), **terms)
    assert out[0].status == report.PASS and out[1].status == report.FAIL
