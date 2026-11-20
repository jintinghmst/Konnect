"""Lumped layout parasitics: extraction from a synthetic two-layer buck board and their
effect in the closed-loop and SPICE models."""
from __future__ import annotations

import math
from dataclasses import replace
from pathlib import Path

import numpy as np
import pytest

from konnect_sim import converter as cv
from konnect_sim import layout as lay
from konnect_sim import parasitics as par
from konnect_sim.buck import BuckTestbench, CapBank


def _pad(ref, num, net, x, y, w=1.0, h=1.0):
    return lay.Pad(ref, num, net, x, y, (w, h), ["F.Cu", "F.Paste", "F.Mask"])


def buck_board() -> lay.Board:
    """J1 -> C1 (C_in) -> U1 -> L1 -> C2 (C_out) -> J2 on F.Cu, solid GND pour on B.Cu,
    one GND via at each ground pad; R1 taps the feedback at x = 26 mm."""
    fps = {}

    def fp(ref, *pads):
        fps[ref] = lay.Footprint(ref, ref, pads[0].x, pads[0].y, 0.0, "F.Cu", {p.number: p for p in pads})

    fp("J1", _pad("J1", "1", "VIN", 0, 0, 2, 2), _pad("J1", "2", "GND", 0, 4, 2, 2))
    fp("C1", _pad("C1", "1", "VIN", 8, 0), _pad("C1", "2", "GND", 8, 2))
    fp("U1", _pad("U1", "1", "VIN", 10, 0, 0.6, 0.6), _pad("U1", "2", "GND", 10, 2, 0.6, 0.6),
       _pad("U1", "3", "SW", 12, 1, 0.6, 0.6))
    fp("L1", _pad("L1", "1", "SW", 16, 1, 2, 2), _pad("L1", "2", "VOUT", 21, 1, 2, 2))
    fp("C2", _pad("C2", "1", "VOUT", 24, 1), _pad("C2", "2", "GND", 24, 3))
    fp("R1", _pad("R1", "1", "VOUT", 26, 4, 0.6, 0.6), _pad("R1", "2", "FB", 27, 4, 0.6, 0.6))
    fp("J2", _pad("J2", "1", "VOUT", 32, 1, 2, 2), _pad("J2", "2", "GND", 32, 5, 2, 2))
    S = lay.Segment
    segs = [S(0, 0, 8, 0, 1.0, "F.Cu", "VIN"), S(8, 0, 10, 0, 0.6, "F.Cu", "VIN"),
            S(12, 1, 16, 1, 1.0, "F.Cu", "SW"),
            S(21, 1, 24, 1, 1.0, "F.Cu", "VOUT"), S(24, 1, 32, 1, 1.0, "F.Cu", "VOUT"),
            S(26, 1, 26, 4, 0.25, "F.Cu", "VOUT")]
    vias = []
    for x, y in ((0, 4), (8, 2), (10, 2), (24, 3), (32, 5)):
        segs.append(S(x, y, x, y + 0.9, 0.5, "F.Cu", "GND"))
        vias.append(lay.Via(x, y + 0.9, 0.6, 0.3, "GND"))
    rect = [(-3, -3), (35, -3), (35, 8), (-3, 8)]
    zones = [lay.Zone("GND", ["B.Cu"], rect, True, {"B.Cu": [rect]})]
    stack = [("F.Cu", "copper", 0.035), ("dielectric 1", "core", 1.51), ("B.Cu", "copper", 0.035)]
    return lay.Board(Path("synthetic_buck.kicad_pcb"), fps, segs, vias, zones,
                     {"F.Cu": 0.035, "B.Cu": 0.035}, 1.58, True, stack, "test stackup")


def extract():
    return par.extract_buck(buck_board(), ic="U1", ic_vin="1", ic_gnd="2", ic_sw="3", cin_refs=["C1"],
                            cout_refs=["C2"], inductor="L1", sense=("R1", "1"), load_vout=("J2", "1"),
                            load_gnd=("J2", "2"), sense_gnd=("U1", "2"), source_vin=("J1", "1"),
                            copper_pitch_mm=0.1)


@pytest.fixture(scope="module")
def lumped():
    return extract()


def test_extraction_matches_hand_formulas(lumped):
    ms = lay.microstrip(1.0, 1.51, 4.5)
    # input: 8 mm of 1 mm track over the plane (J1 -> C1)
    assert lumped.l_in_h == pytest.approx(ms["l_per_m"] * 8e-3, rel=1e-6)
    assert lumped.r_in_ohm == pytest.approx(lay.sheet_resistance(0.035) * 8 / 1.0, rel=1e-6)
    # SW: 4 mm over the plane
    assert lumped.l_sw_h == pytest.approx(ms["l_per_m"] * 4e-3, rel=1e-6)
    # output: from the sense junction (x = 26) to J2 (x = 32), 6 mm
    assert lumped.l_out_h == pytest.approx(ms["l_per_m"] * 6e-3, rel=1e-6)
    assert lumped.details["output"]["length_mm"] == pytest.approx(6.0)
    assert lumped.l_gnd_h == 0.0          # in the output microstrip loop
    assert 0 < lumped.r_gnd_ohm < 5e-3    # through the B.Cu pour
    # mounting: one GND via per capacitor, the VIN/VOUT terminal joined on F.Cu
    lv = lay.via_inductance(1.58, 0.3)
    assert lumped.l_mount_cout_h == pytest.approx(lv)
    assert lumped.l_mount_cin_h == pytest.approx(lv)
    # hot loop: a fraction of a nH to a few nH for 2 mm over a 1.5 mm plane plus vias
    assert 0.3e-9 < lumped.l_hot_h < 5e-9
    # switch node: 4 mm track + pads over 1.51 mm of FR-4, around a pF
    assert 0.3e-12 < lumped.c_sw_f < 5e-12
    assert lumped.unmodelled == []


def test_rows_figure_and_round_trip(lumped, tmp_path):
    rows = lumped.rows()
    assert {r["branch"] for r in rows} >= {"Hot loop C_in - IC - C_in", "Switch-node copper to GND"}
    assert "| Element |" in lumped.markdown_table()
    assert Path(lumped.figure(tmp_path / "par.png")).exists()
    again = par.Lumped.load(lumped.save(tmp_path / "par.json"))
    assert again.l_hot_h == lumped.l_hot_h and again.details.keys() == lumped.details.keys()
    # numpy scalars (the copper solve, loop formulas) must round-trip as numbers
    import numpy as np
    np_lumped = replace(lumped, l_hot_h=np.float64(lumped.l_hot_h), details={"x": {"v": np.float64(1.5)}})
    back = par.Lumped.load(np_lumped.save(tmp_path / "np.json"))
    assert isinstance(back.l_hot_h, float) and back.details["x"]["v"] == 1.5
    assert lumped.check_lumping(1e-9) == []          # every branch < ~16 mm
    assert lumped.check_lumping(0.1e-9)               # a 100 ps edge makes them distributed


def test_bar_inductance():
    # Grover: 10 mm x 1 mm x 35 um strip in free space, about 6.5 nH
    assert par.bar_inductance(10, 1, 0.035) == pytest.approx(6.5e-9, rel=0.1)


# ── effect in the closed-loop model ────────────────────────────────────────

L_H, C_EACH, ESR_EACH, VIN, FSW = 10e-6, 22e-6, 0.003, 24.0, 500e3


def _plant(**kw):
    return cv.Plant(L_H, 0.03, [cv.Bank(10e-6, 0.005, 2)], [cv.Bank(C_EACH, ESR_EACH, 2, esl_h=0.4e-9)],
                    0.1, 0.05, 52.5e3, 10e3, **({"src_r_ohm": 0.05, "src_l_h": 0.0} | kw))


def _ctrl():
    # type III placed on the LC double pole and the ESR zero, crossover ~30 kHz
    c_out, esr = 2 * C_EACH, ESR_EACH / 2
    w0, wp1, wp2 = 1 / math.sqrt(L_H * c_out), 1 / (esr * c_out), math.pi * FSW
    num, den = cv.type3(1.0, w0, w0, wp1, wp2)
    s = 2j * math.pi * 30e3
    zl, zc = s * L_H + 0.1, esr + 1 / (s * c_out)
    t1 = abs(np.polyval(num, s) / np.polyval(den, s) * VIN * zc / (zl + zc) * 10 / 62.5)
    c = cv.VoltageMode(cv.Common(vref=0.8, t_ss=0), fsw=FSW, comp=cv.type3(1 / t1, w0, w0, wp1, wp2), v_ramp=1.0)
    c.state, c.pulsed = "run", True
    return c


def _sim(plant, load, i0=1.0):
    s = cv.Sim(plant, _ctrl(), vin=VIN, load=load, fsw_hint=FSW, v0_out=5.0)
    s.x[0], s.idle = i0, False
    return s


def _run(plant, i_load=1.0, t_end=300e-6):
    w = _sim(plant, cv.Load.current(i_load), i_load).run(t_end)
    st = cv.steady(w, t_end - 40e-6)
    st["max_step"] = float(np.abs(np.diff(w.vload[w.window(t_end - 40e-6)])).max())
    return st


def test_plant_parasitics_change_load_voltage_and_ripple(lumped):
    base = _plant()
    with_cu = lumped.apply_plant(base)
    assert with_cu.r_out_path == lumped.r_out_ohm and with_cu.cout[0].l_mount_h == lumped.l_mount_cout_h
    a, b = _run(base), _run(with_cu)
    # the loop holds the sense junction; the load loses I * (R_out + R_gnd)
    drop = a["vload_avg"] - b["vload_avg"]
    assert drop == pytest.approx(1.0 * (lumped.r_out_ohm + lumped.r_gnd_ohm), rel=0.25, abs=2e-4)
    # ESL + mounting inductance: at each switching edge di_L/dt changes by Vin / L, so the
    # output steps by L_cout Vin / L (the ESL step of the ripple)
    for st, l_mount in ((a, 0.0), (b, lumped.l_mount_cout_h)):
        l_cout = (0.4e-9 + l_mount) / 2
        assert st["max_step"] == pytest.approx(l_cout * VIN / L_H, rel=0.2)


def test_output_inductance_in_a_load_step():
    base = _plant()
    p = replace(base, l_out_path=20e-9)
    load = cv.Load.step(0.2, 1.0, 300e-6, 1e6)      # 1 A/us
    out = []
    for pl in (base, p):
        w = _sim(pl, load, 0.2).run(301e-6)
        m = w.window(300.2e-6, 300.7e-6)
        out.append(float(np.mean(w.vload[m])))
    # during the ramp the load sees L di/dt = 20 nH * 1 A/us = 20 mV less
    assert out[0] - out[1] == pytest.approx(0.020, rel=0.2)


# ── effect in the SPICE testbench ──────────────────────────────────────────

def test_testbench_netlist_has_every_element(lumped):
    tb = BuckTestbench(vin=24, vout_target=5, iout=1, fsw=500e3, l_h=10e-6, l_dcr_ohm=0.03,
                       cout=[CapBank(22e-6, 2, 0.003)], cin=[CapBank(10e-6, 2, 0.005)],
                       rds_hs_ohm=0.1, rds_ls_ohm=0.05, cycles=60, measure_cycles=5)
    t2 = lumped.apply_testbench(tb)
    net = t2.netlist(5 / 24)
    for name in ("LHOT", "LSWP", "LOUTP", "RGNDP", "RSRC"):
        assert name in net
    assert t2.c_sw_f == pytest.approx(tb.c_sw_f + lumped.c_sw_f)
    assert t2.cout[0].l_mount_h == lumped.l_mount_cout_h
    r0, r1 = tb.run(5 / 24), t2.run(5 / 24)
    # the load sees the copper drop (open loop, same duty)
    assert r0["vload_avg"] - r1["vload_avg"] > 0.5 * (lumped.r_out_ohm + lumped.r_gnd_ohm + lumped.r_sw_ohm)


def test_layout_parasitics_tool(monkeypatch, tmp_path):
    from konnect_sim import tools
    monkeypatch.setattr(tools, "_board", lambda board, stackup_json=None: (buck_board(), None))
    out = tools.layout_parasitics(
        "synthetic_buck.kicad_pcb", ic="U1", ic_vin="1", ic_gnd="2", ic_sw="3", cin_refs=["C1"], cout_refs=["C2"],
        inductor="L1", sense=["R1", "1"], load_vout=["J2", "1"], load_gnd=["J2", "2"], sense_gnd=["U1", "2"],
        source_vin=["J1", "1"], er={"value": 4.5, "source": "fab stackup"}, t_rise_s=1e-9,
        out_json=str(tmp_path / "par.json"), figures_dir=str(tmp_path))
    assert out["lumped"]["l_hot_h"] > 0 and out["too_long_to_lump"] == []
    assert out["assumptions"] == ["stackup (copper thickness, dielectric height)"]
    assert Path(out["figures"][0]["path"]).exists()
    assert par.Lumped.load(out["json"]).l_out_h == out["lumped"]["l_out_h"]
    import json
    json.dumps(out)                          # the bridge returns it as JSON


def test_steady_state_testbench_leaves_out_only_the_input_loop_inductance(lumped):
    tb = BuckTestbench(vin=24, vout_target=5, iout=1, fsw=500e3, l_h=10e-6, l_dcr_ohm=0.03,
                       cout=[CapBank(22e-6, 2, 0.003)], cin=[CapBank(10e-6, 2, 0.005)],
                       rds_hs_ohm=0.1, rds_ls_ohm=0.05)
    ss, full = lumped.apply_testbench(tb, edges=False), lumped.apply_testbench(tb)
    assert ss.l_hot_h == 0.0 and ss.cin[0].l_mount_h == 0.0
    assert full.l_hot_h == lumped.l_hot_h and full.cin[0].l_mount_h == lumped.l_mount_cin_h
    for f in ("r_hot_ohm", "r_sw_path_ohm", "l_sw_path_h", "r_out_path_ohm", "l_out_path_h",
              "r_gnd_path_ohm", "source_r_ohm", "source_l_h", "c_sw_f"):
        assert getattr(ss, f) == getattr(full, f)
    assert ss.cout[0].l_mount_h == full.cout[0].l_mount_h == lumped.l_mount_cout_h
