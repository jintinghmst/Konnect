"""Closed-loop switched simulation of a synchronous buck with its controller.

The power stage is integrated as a switched state-space model (fixed step,
RK4): source and cable (R, L), input and output capacitor banks (C with DC-bias
curve, ESR), inductor (L(I) curve, DCR), high- and low-side switches with
on-resistance, body diodes and discontinuous conduction, output copper to the
load, and the feedback divider with an optional series injection source. The
controller is evaluated every step from the same measurements a real IC sees.

Controllers (``COT``, ``VoltageMode``, ``PeakCurrentMode``) are behavioural
models parameterised from datasheet values. Datasheets rarely publish every
internal value (ripple-injection gain, compensation), so a controller carries
``validated_by``: a result from an unvalidated controller is evidence of
behaviour, not proof, and checks built from it stay INCOMPLETE until the model
has been matched to a vendor waveform or a bench measurement.

What it answers: regulation across line and load (item 3), loop gain and phase
margin by injection (4), load steps (5), start-up and shut-down (6), light-load
pulse skipping (7), input steps, dips, brown-out and hot-plug (8), overload,
short circuit, hiccup and recovery (18), and source/cable interaction (19).
What it does not: switching-edge ringing (use ``buck.BuckTestbench.run_edge``)
and anything faster than the step ``dt``.
"""
from __future__ import annotations

import copy
import math
from dataclasses import dataclass, field

import numpy as np


# ── power stage ────────────────────────────────────────────────────────────

@dataclass
class Bank:
    """``count`` identical parallel capacitors. ``c_vs_v``: [(V, C/C_nom)]."""
    c_f: float
    esr_ohm: float
    count: int = 1
    c_vs_v: list[tuple[float, float]] | None = None

    def c_at(self, v: float) -> float:
        if self.c_vs_v:
            xs, ys = zip(*sorted(self.c_vs_v))
            return self.c_f * self.count * float(np.interp(abs(v), xs, ys))
        return self.c_f * self.count

    @property
    def g(self) -> float:
        return self.count / self.esr_ohm


@dataclass
class Plant:
    l_h: float
    dcr_ohm: float
    cin: list[Bank]
    cout: list[Bank]
    rds_hs: float
    rds_ls: float
    r_top: float
    r_bot: float
    vf_body: float = 0.7
    r_body: float = 0.02
    src_r_ohm: float = 0.05
    src_l_h: float = 1e-6
    r_out_path: float = 0.0         # output caps to the load point
    sense_at_load: bool = True       # divider tap at the load (else at the caps)
    l_vs_i: list[tuple[float, float]] | None = None   # (A, L/L_nom)

    def l_at(self, i: float) -> float:
        if self.l_vs_i:
            xs, ys = zip(*sorted(self.l_vs_i))
            return self.l_h * float(np.interp(abs(i), xs, ys))
        return self.l_h


# ── sources and loads ──────────────────────────────────────────────────────

def constant(v):
    return lambda t: v


def ramp(v0, v1, t0, t1):
    def f(t):
        if t <= t0:
            return v0
        if t >= t1:
            return v1
        return v0 + (v1 - v0) * (t - t0) / (t1 - t0)
    return f


def piecewise(points):
    """[(t, value), ...] linear between points, held outside."""
    ts, vs = zip(*points)
    return lambda t: float(np.interp(t, ts, vs))


@dataclass
class Load:
    """Norton load: current = i(t) + v * g(t). ``von``: an electronic load in
    CC mode draws nothing below this voltage."""
    i: callable = constant(0.0)
    g: callable = constant(0.0)
    von: float = 0.0

    @staticmethod
    def current(i_fn, von=0.0):
        return Load(i_fn if callable(i_fn) else constant(i_fn), constant(0.0), von)

    @staticmethod
    def resistor(r_fn):
        rf = r_fn if callable(r_fn) else constant(r_fn)
        return Load(constant(0.0), lambda t: 1.0 / rf(t))

    @staticmethod
    def step(i0, i1, t0, slew_a_per_s, von=0.0):
        t1 = t0 + abs(i1 - i0) / slew_a_per_s
        return Load(ramp(i0, i1, t0, t1), constant(0.0), von)


# ── controllers ────────────────────────────────────────────────────────────

@dataclass
class Meas:
    t: float
    vin: float       # at the IC's VIN pin (input-cap node)
    vout: float      # output-cap node
    vload: float
    vfb: float
    il: float
    en: bool


@dataclass
class Common:
    """Supervisory behaviour shared by all controllers (datasheet values)."""
    vref: float
    t_ss: float = 1e-3               # soft-start ramp time
    uvlo_on: float = 0.0             # VIN rising threshold
    uvlo_off: float = 0.0            # VIN falling threshold
    pulse_skip: bool = True          # zero-cross: low side off at I_L = 0 (DCM)
    prebias_safe: bool = True        # no low-side turn-on before the first high-side pulse
    uvp_frac: float | None = None    # output under-voltage protection (fraction of vref)
    uvp_delay: float = 0.0
    hiccup_off: float | None = None  # off time before restart after UVP
    ovp_frac: float | None = None    # high side held off above this fraction of vref
    discharge_r: float | None = None # output discharge when disabled (ohm), if the IC has one
    validated_by: str = ""           # vendor waveform or bench data the model was matched to


EPS = 1e-12      # s, tolerance when comparing a time against a scheduled event


class _Base:
    """Controllers set ``next_t``: the absolute time of their next scheduled
    transition (end of an on-time or dead time, a PWM edge). The simulation
    shortens its step to land on it, so switching instants are exact rather
    than quantised to the step."""

    def __init__(self, common: Common):
        self.c = common
        self.next_t = math.inf
        self.t_last = None
        self.state = "off"            # off | ss | run | hiccup
        self.t_state = 0.0
        self.vref_now = 0.0
        self.hs = self.ls = False
        self.pulsed = False
        self.t_uv = None
        self.events: list[tuple[float, str]] = []

    def _supervise(self, m: Meas):
        c = self.c
        ok_in = m.vin >= (c.uvlo_off if self.state in ("ss", "run") else c.uvlo_on)
        if not m.en or not ok_in:
            if self.state != "off":
                self.events.append((m.t, "disabled" if not m.en else "UVLO"))
            self.state, self.t_state, self.vref_now, self.pulsed = "off", m.t, 0.0, False
            return False
        if self.state == "hiccup":
            if m.t - self.t_state >= (c.hiccup_off or 0.0):
                self.events.append((m.t, "restart"))
                self.state, self.t_state, self.pulsed = "ss", m.t, False
            else:
                return False
        if self.state == "off":
            self.events.append((m.t, "start"))
            self.state, self.t_state = "ss", m.t
        if self.state == "ss":
            frac = (m.t - self.t_state) / c.t_ss if c.t_ss > 0 else 1.0
            self.vref_now = c.vref * min(frac, 1.0)
            if frac >= 1.0:
                self.state = "run"
        else:
            self.vref_now = c.vref
        if c.uvp_frac is not None and self.state == "run":
            if m.vfb < c.uvp_frac * c.vref:
                self.t_uv = m.t if self.t_uv is None else self.t_uv
                if m.t - self.t_uv >= c.uvp_delay:
                    self.events.append((m.t, "UVP hiccup" if c.hiccup_off is not None else "UVP latch"))
                    self.state, self.t_state, self.t_uv, self.vref_now = "hiccup", m.t, None, 0.0
                    if c.hiccup_off is None:
                        self.c = copy.copy(c)
                        self.c.hiccup_off = math.inf
                    return False
            else:
                self.t_uv = None
        return True

    def _ls_allowed(self, il: float) -> bool:
        if self.c.prebias_safe and not self.pulsed:
            return False
        return not (self.c.pulse_skip and il <= 0.0)


class COT(_Base):
    """Constant on-time with ripple injection (D-CAP-style).

    A pulse starts when V_FB + ramp falls below the reference, the off-time has
    reached ``t_off_min`` and the inductor current is below the valley limit.
    On-time adapts to keep the frequency near ``fsw``:
    t_on = (Vout_set + r_comp * I_L,avg) / (Vin fsw). ``r_comp`` models the
    loss compensation of adaptive on-time timers, which stretch the on-time as
    conduction drops grow with load (0 = pure Vout/Vin timer).
    The ramp emulates inductor ripple: k_ramp * (I_L - lowpass(I_L)).
    ``ki`` adds an integrator on V_FB error (D-CAP3-style DC correction).
    """

    def __init__(self, common: Common, *, fsw: float, vout_set: float, t_off_min: float,
                 i_valley_limit: float, k_ramp: float, tau_ramp: float, t_on_min: float = 0.0,
                 t_dead: float = 10e-9, ki: float = 0.0, r_comp: float = 0.0):
        super().__init__(common)
        self.fsw, self.vout_set, self.t_off_min, self.t_on_min = fsw, vout_set, t_off_min, t_on_min
        self.i_valley, self.k_ramp, self.tau_ramp, self.t_dead, self.ki = (
            i_valley_limit, k_ramp, tau_ramp, t_dead, ki)
        self.r_comp = r_comp
        self.il_lp = 0.0
        self.integ = 0.0
        self.t_on_start = -1.0
        self.t_off_start = -1.0
        self.t_phase = 0.0
        self.phase = "idle"           # hs | dead_ls | ls | dead_hs | idle
        self.valley_limited = 0

    def update(self, m: Meas, dt: float):
        el = 0.0 if self.t_last is None else m.t - self.t_last
        self.t_last = m.t
        self.il_lp += (m.il - self.il_lp) * min(el / self.tau_ramp, 1.0)
        self.next_t = math.inf
        if not self._supervise(m):
            self.hs = self.ls = False
            self.phase = "idle"
            self.il_lp = m.il
            self.integ = 0.0
            return
        ramp = self.k_ramp * (m.il - self.il_lp)
        if self.ki:
            self.integ += self.ki * (self.vref_now - m.vfb) * el
        ref = self.vref_now + self.integ
        ovp = self.c.ovp_frac is not None and m.vfb > self.c.ovp_frac * self.c.vref
        ph = self.phase
        t_on = max((self.vout_set + self.r_comp * max(self.il_lp, 0.0)) / (max(m.vin, 0.1) * self.fsw),
                   self.t_on_min)
        if ph == "hs":
            if m.t - self.t_on_start >= t_on - EPS or ovp:
                self.t_off_start, self.t_phase, ph = m.t, m.t, "dead_ls"
        elif ph == "dead_ls" and m.t - self.t_phase >= self.t_dead - EPS:
            ph = "ls" if self._ls_allowed(m.il) else "idle"
        elif ph == "dead_hs" and m.t - self.t_phase >= self.t_dead - EPS:
            ph, self.pulsed, self.t_on_start = "hs", True, m.t
        if ph in ("ls", "idle"):
            if ph == "ls" and not self._ls_allowed(m.il):
                ph = "idle"
            off_ok = self.t_off_start < 0 or m.t - self.t_off_start >= self.t_off_min - EPS
            if off_ok and not ovp and m.vfb + ramp < ref:
                if m.il > self.i_valley:
                    self.valley_limited += 1
                elif ph == "ls":
                    ph, self.t_phase = "dead_hs", m.t
                else:
                    ph, self.pulsed, self.t_on_start = "hs", True, m.t
        self.phase = ph
        self.hs, self.ls = ph == "hs", ph == "ls"
        if ph == "hs":
            self.next_t = self.t_on_start + t_on
        elif ph in ("dead_ls", "dead_hs"):
            self.next_t = self.t_phase + self.t_dead
        elif self.t_off_start >= 0 and m.t - self.t_off_start < self.t_off_min:
            self.next_t = self.t_off_start + self.t_off_min


class _Compensator:
    """Continuous transfer function Gc(s) = num/den (descending powers of s),
    discretised by the bilinear transform at the simulation step."""

    def __init__(self, num, den, dt):
        from scipy.signal import cont2discrete, tf2ss
        a, b, c, d = tf2ss(num, den)
        ad, bd, cd, dd, _ = cont2discrete((a, b, c, d), dt, method="bilinear")
        self.ad, self.bd, self.cd, self.dd = ad, bd.ravel(), cd.ravel(), float(np.squeeze(dd))
        self.x = np.zeros(ad.shape[0])

    def step(self, e: float) -> float:
        y = float(self.cd @ self.x) + self.dd * e
        self.x = self.ad @ self.x + self.bd * e
        return y

    def reset(self):
        self.x[:] = 0.0


def type3(gain, wz1, wz2, wp1, wp2):
    """Type-III compensator Gc = gain (1+s/wz1)(1+s/wz2) / (s (1+s/wp1)(1+s/wp2))
    as (num, den)."""
    num = np.polymul([1 / wz1, 1], [1 / wz2, 1]) * gain
    den = np.polymul([1, 0], np.polymul([1 / wp1, 1], [1 / wp2, 1]))
    return num, den


def type2(gain, wz, wp):
    return np.array([gain / wz, gain]), np.polymul([1, 0], [1 / wp, 1])


class _PWM(_Base):
    def __init__(self, common, fsw, num, den, t_dead, d_max):
        super().__init__(common)
        self.fsw, self.num, self.den, self.t_dead, self.d_max = fsw, num, den, t_dead, d_max
        self.comp = None
        self.t_comp = None
        self.dt_comp = None
        self.t_cycle = 0.0
        self.vc = 0.0

    def _cycle(self, m, dt):
        if self.comp is None:
            self.comp, self.dt_comp = _Compensator(self.num, self.den, dt), dt
        self.next_t = math.inf
        if not self._supervise(m):
            self.hs = self.ls = False
            self.comp.reset()
            self.t_cycle, self.t_comp = m.t, m.t
            return None
        # the compensator runs on its own regular clock (it was discretised at
        # dt), whatever extra steps the event scheduling inserts
        if self.t_comp is None:
            self.t_comp = m.t
        while m.t - self.t_comp >= self.dt_comp - EPS:
            self.vc = self.comp.step(self.vref_now - m.vfb)
            self.t_comp += self.dt_comp
        phase = (m.t - self.t_cycle) * self.fsw
        if phase >= 1.0 - EPS * self.fsw:
            self.t_cycle += max(math.floor(phase + EPS * self.fsw), 1) / self.fsw
            return 0.0, True
        return phase, False

    def _schedule(self, m, edges):
        """Next of the given within-cycle phases (0..1), or the next cycle start."""
        t_next = self.t_cycle + 1 / self.fsw
        for e in edges:
            te = self.t_cycle + e / self.fsw
            if te > m.t + EPS:
                t_next = min(t_next, te)
        self.next_t = t_next


class VoltageMode(_PWM):
    """Fixed-frequency voltage-mode PWM: duty = Vc / V_ramp (optionally with
    input feed-forward, V_ramp proportional to Vin), cycle-by-cycle peak
    current limit."""

    def __init__(self, common: Common, *, fsw: float, comp: tuple, v_ramp: float,
                 i_peak_limit: float = math.inf, feedforward_vin: float | None = None,
                 t_dead: float = 10e-9, d_max: float = 0.95):
        super().__init__(common, fsw, comp[0], comp[1], t_dead, d_max)
        self.v_ramp, self.i_peak, self.ff = v_ramp, i_peak_limit, feedforward_vin
        self.off_at = None

    def update(self, m: Meas, dt: float):
        r = self._cycle(m, dt)
        if r is None:
            return
        phase, new = r
        vr = self.v_ramp * (m.vin / self.ff if self.ff else 1.0)
        tol = EPS * self.fsw
        if new:
            self.off_at = None                    # trailing edge not reached yet this cycle
        # analog trailing-edge PWM: the high side turns off when the ramp
        # crosses the compensator output (or at the peak-current limit) and
        # stays off for the rest of the cycle
        duty_now = min(max(self.vc / vr, 0.0), self.d_max)
        if self.off_at is None and (phase >= duty_now - tol or m.il >= self.i_peak):
            self.off_at = phase
        on = self.off_at is None
        if on and not self.hs:
            self.pulsed = True
        self.hs = on
        off = self.off_at if self.off_at is not None else duty_now
        self.ls = ((not on) and phase >= off + self.t_dead * self.fsw - tol
                   and phase < 1 - self.t_dead * self.fsw - tol and self._ls_allowed(m.il))
        self._schedule(m, [duty_now, off + self.t_dead * self.fsw, 1 - self.t_dead * self.fsw])


class PeakCurrentMode(_PWM):
    """Fixed-frequency peak current mode: the high side turns on each cycle and
    off when R_i * I_L + slope compensation reaches the compensator output."""

    def __init__(self, common: Common, *, fsw: float, comp: tuple, r_i: float, slope_v_per_s: float,
                 i_peak_limit: float = math.inf, t_dead: float = 10e-9, d_max: float = 0.95):
        super().__init__(common, fsw, comp[0], comp[1], t_dead, d_max)
        self.r_i, self.slope, self.i_peak = r_i, slope_v_per_s, i_peak_limit
        self.tripped = False

    def update(self, m: Meas, dt: float):
        r = self._cycle(m, dt)
        if r is None:
            return
        phase, new = r
        if new:
            self.tripped = False
        t_in = phase / self.fsw
        if self.hs and (self.r_i * m.il + self.slope * t_in >= self.vc or m.il >= self.i_peak
                        or phase >= self.d_max):
            self.tripped = True
        on = not self.tripped and phase < self.d_max
        if on and not self.hs:
            self.pulsed = True
        self.hs = on
        self.ls = (not on) and self._ls_allowed(m.il)
        self._schedule(m, [self.d_max])


# ── simulation ─────────────────────────────────────────────────────────────

@dataclass
class Waves:
    t: np.ndarray
    vin: np.ndarray
    vout: np.ndarray
    vload: np.ndarray
    vfb: np.ndarray
    il: np.ndarray
    isrc: np.ndarray
    iload: np.ndarray
    vsw: np.ndarray
    hs_on_times: np.ndarray
    events: list
    dt: float

    def window(self, t0, t1=None):
        t1 = self.t[-1] if t1 is None else t1
        return (self.t >= t0) & (self.t <= t1)


class Sim:
    """Switched simulation. ``vin``: source voltage function of time; ``en``:
    enable function of time (bool); ``inject``: series voltage between the
    sense point and the divider top, function of time."""

    def __init__(self, plant: Plant, ctrl, *, vin, load: Load, en=None, inject=None,
                 dt: float | None = None, fsw_hint: float = 500e3, v0_out: float = 0.0,
                 v0_in: float | None = None):
        self.p, self.c, self.load = plant, ctrl, load
        self.vin_fn = vin if callable(vin) else constant(vin)
        self.en_fn = en or (lambda t: True)
        self.inj_fn = inject or (lambda t: 0.0)
        taus = [b.esr_ohm / b.count * b.c_at(0) for b in plant.cin + plant.cout]
        self.dt = dt or min(1 / fsw_hint / 300, 0.4 * min(taus))
        vi = self.vin_fn(0.0) if v0_in is None else v0_in
        self.t = 0.0
        # state: il, isrc, vcin..., vcout...
        self.x = [0.0, 0.0] + [vi] * len(plant.cin) + [v0_out] * len(plant.cout)
        self.idle = True       # inductor current clamped at zero (DCM)
        self.hs_times: list[float] = []

    def snapshot(self):
        return copy.deepcopy(self)

    # node voltages from states and switch state
    def _nodes(self, x, t, hs, ls):
        p = self.p
        il, isrc = x[0], x[1]
        nin = len(p.cin)
        vci, vco = x[2:2 + nin], x[2 + nin:]
        load_i, load_g = self.load.i(t), self.load.g(t)
        # source: inductive (state) or, with no inductance, a Norton source
        gin = sum(b.g for b in p.cin)
        conduct_hs = hs or (not hs and not ls and il < 0 and not self.idle)
        i_draw = il if conduct_hs else 0.0
        if p.src_l_h > 0:
            vin = (isrc - i_draw + sum(b.g * v for b, v in zip(p.cin, vci))) / gin
        else:
            gs = 1 / p.src_r_ohm
            vin = (self.vin_fn(t) * gs - i_draw + sum(b.g * v for b, v in zip(p.cin, vci))) / (gin + gs)
        gout = sum(b.g for b in p.cout)
        gl = load_g / (1 + load_g * p.r_out_path) if load_g else 0.0
        gl += 1 / (p.r_top + p.r_bot)              # the feedback divider loads the output too
        vout = (il - load_i + sum(b.g * v for b, v in zip(p.cout, vco))) / (gout + gl)
        if load_i > 0 and vout < self.load.von:
            # a current-sinking load cannot pull its terminals below von (>= 0 V)
            load_i = 0.0
            vout = (il + sum(b.g * v for b, v in zip(p.cout, vco))) / (gout + gl)
        if self.c.c.discharge_r and self.c.state == "off":
            gd = 1 / self.c.c.discharge_r
            vout = (il - load_i + sum(b.g * v for b, v in zip(p.cout, vco))) / (gout + gl + gd)
            load_i += vout * gd                       # the discharge path, counted as load
        iload = load_i + vout * gl
        vload = vout - iload * p.r_out_path
        if hs:
            vsw = vin - il * p.rds_hs
        elif ls:
            vsw = -il * p.rds_ls
        elif self.idle:
            vsw = vout
        elif il > 0:
            vsw = -p.vf_body - il * p.r_body
        else:
            vsw = vin + p.vf_body - il * p.r_body
        return vin, vout, vload, vsw, iload, vci, vco

    def _deriv(self, x, t, hs, ls):
        p = self.p
        vin, vout, vload, vsw, iload, vci, vco = self._nodes(x, t, hs, ls)
        il = x[0]
        dil = 0.0 if (self.idle and not hs and not ls) else (vsw - il * p.dcr_ohm - vout) / p.l_at(il)
        disrc = (self.vin_fn(t) - x[1] * p.src_r_ohm - vin) / p.src_l_h if p.src_l_h > 0 else 0.0
        d = [dil, disrc]
        d += [b.g * (vin - v) / b.c_at(v) for b, v in zip(p.cin, vci)]
        d += [b.g * (vout - v) / b.c_at(v) for b, v in zip(p.cout, vco)]
        return d

    def _meas(self):
        hs, ls = self.c.hs, self.c.ls
        vin, vout, vload, vsw, iload, _, _ = self._nodes(self.x, self.t, hs, ls)
        sense = vload if self.p.sense_at_load else vout
        vfb = (sense + self.inj_fn(self.t)) * self.p.r_bot / (self.p.r_top + self.p.r_bot)
        return Meas(self.t, vin, vout, vload, vfb, self.x[0], bool(self.en_fn(self.t))), vsw, iload

    def run(self, t_end: float, record_every: int = 1) -> Waves:
        rec = {k: [] for k in ("t", "vin", "vout", "vload", "vfb", "il", "isrc", "iload", "vsw")}
        dt, n = self.dt, 0
        hs_prev = self.c.hs
        while self.t < t_end - dt / 2:
            m, vsw, iload = self._meas()
            self.c.update(m, dt)
            hs, ls = self.c.hs, self.c.ls
            if hs and not hs_prev:
                self.hs_times.append(self.t)
            hs_prev = hs
            if hs or ls:
                self.idle = False
            elif not self.idle and abs(self.x[0]) < 1e-12:
                self.idle = True
            if n % record_every == 0:
                for k, v in (("t", self.t), ("vin", m.vin), ("vout", m.vout), ("vload", m.vload),
                             ("vfb", m.vfb), ("il", self.x[0]), ("isrc", self.x[1]),
                             ("iload", iload), ("vsw", vsw)):
                    rec[k].append(v)
            x, t = self.x, self.t
            h = dt
            te = getattr(self.c, "next_t", math.inf)
            if t + 1e-3 * dt < te < t + dt:
                h = te - t                       # land exactly on the controller's next edge
            k1 = self._deriv(x, t, hs, ls)
            k2 = self._deriv([a + h / 2 * b for a, b in zip(x, k1)], t + h / 2, hs, ls)
            k3 = self._deriv([a + h / 2 * b for a, b in zip(x, k2)], t + h / 2, hs, ls)
            k4 = self._deriv([a + h * b for a, b in zip(x, k3)], t + h, hs, ls)
            xn = [a + h / 6 * (b1 + 2 * b2 + 2 * b3 + b4) for a, b1, b2, b3, b4 in zip(x, k1, k2, k3, k4)]
            # diode commutation: with both switches off the current cannot reverse
            if not hs and not ls and not self.idle and x[0] * xn[0] <= 0:
                xn[0] = 0.0
                self.idle = True
            if self.p.src_l_h <= 0:
                xn[1] = 0.0
            self.x, self.t = xn, t + h
            n += 1
        a = {k: np.asarray(v) for k, v in rec.items()}
        if self.p.src_l_h <= 0:
            a["isrc"] = (np.array([self.vin_fn(t) for t in a["t"]]) - a["vin"]) / self.p.src_r_ohm
        return Waves(a["t"], a["vin"], a["vout"], a["vload"], a["vfb"], a["il"], a["isrc"], a["iload"],
                     a["vsw"], np.asarray(self.hs_times), list(self.c.events), dt)


# ── analysis ───────────────────────────────────────────────────────────────

def snapshot(w: Waves, period: float, cycles: int | None = None) -> np.ndarray:
    """A steady run as rows (t, vin, vload, il, vsw), time from zero: the input
    ``figures.ripple_figures`` draws. Keep the whole window that ``steady`` measured (the
    default) so the figures report the same peak-to-peak as the check; ``cycles`` keeps only
    the last few switching periods."""
    m = w.t >= (w.t[-1] - cycles * period if cycles else w.t[0])
    return np.vstack([w.t[m] - w.t[m][0], w.vin[m], w.vload[m], w.il[m], w.vsw[m]])


def cycle_average(w: Waves, x: np.ndarray, period: float) -> np.ndarray:
    """Moving average over one switching period (removes switching ripple);
    normalised at the ends so the edges are not pulled towards zero."""
    n = max(int(round(period / (w.t[1] - w.t[0]))), 1)
    k = np.ones(n)
    return np.convolve(x, k, mode="same") / np.convolve(np.ones_like(x), k, mode="same")


def steady(w: Waves, t0: float, t1: float | None = None) -> dict:
    m = w.window(t0, t1)
    hs = w.hs_on_times[(w.hs_on_times >= t0) & (w.hs_on_times <= (t1 or w.t[-1]))]
    span = (t1 or w.t[-1]) - t0
    return {"vout_avg": float(w.vout[m].mean()), "vload_avg": float(w.vload[m].mean()),
            "vload_pp": float(np.ptp(w.vload[m])), "vout_pp": float(np.ptp(w.vout[m])),
            "il_pp": float(np.ptp(w.il[m])), "il_max": float(w.il[m].max()),
            "il_min": float(w.il[m].min()), "pulse_hz": len(hs) / span if span > 0 else 0.0,
            "iin_avg": float(w.isrc[m].mean())}


def transient(w: Waves, t_event: float, v_target: float | None, band: float, period: float,
              t_end: float | None = None) -> dict:
    """Excursion after an event: min and max of V_load (ripple included),
    settling time of the cycle-averaged V_load into ``v_target`` +/- ``band``
    (``v_target`` None: the average of the last 20 % of the window), and the
    damping ratio from the log decrement of successive peaks of the averaged
    error (peaks closer than two switching periods are merged)."""
    m = w.window(t_event, t_end)
    t, v = w.t[m], w.vload[m]
    va = cycle_average(w, w.vload, 3 * period)[m]
    if v_target is None:
        v_target = float(va[int(len(va) * 0.8):].mean())
    e = va - v_target
    idx = np.nonzero(np.abs(e) > band)[0]
    t_settle = float(t[idx[-1]] - t_event) if len(idx) else 0.0
    peaks, last = [], -math.inf
    for i in range(1, len(e) - 1):
        if (e[i] - e[i - 1]) * (e[i + 1] - e[i]) < 0 and abs(e[i]) > band / 4:
            if t[i] - last < 2 * period and peaks:
                if abs(e[i]) > abs(e[peaks[-1]]):
                    peaks[-1] = i
            else:
                peaks.append(i)
            last = t[i]
    # keep alternating-sign peaks only
    alt = [p for k_, p in enumerate(peaks) if k_ == 0 or np.sign(e[p]) != np.sign(e[peaks[k_ - 1]])]
    zeta = None
    if len(alt) >= 3:
        a1, a3 = abs(e[alt[0]]), abs(e[alt[2]])
        if a3 > 0 and a1 > a3:
            dec = math.log(a1 / a3)
            zeta = dec / math.sqrt(4 * math.pi ** 2 + dec ** 2)
    return {"v_target": v_target, "v_min": float(v.min()), "v_max": float(v.max()),
            "dip": float(v_target - v.min()), "overshoot": float(v.max() - v_target),
            "t_settle": t_settle, "settled": t_settle < (t[-1] - t_event) * 0.8,
            "zeta": zeta, "ringing_peaks": len(alt)}


def loop_gain(base: Sim, freqs, amplitude, *, settle_cycles: int = 12, measure_cycles: int = 8,
              min_time: float = 50e-6) -> list[dict]:
    """Loop gain by injection, as a bench analyser measures it: a series source
    between the sense point (y) and the divider top (x), V_x = V_y + v_inj.
    Returns T = -V_y/V_x per frequency (magnitude dB, phase deg), |V_x| and
    |V_y| in volts, the residual ripple and ``snr_ok`` (|V_x| >= residual ripple p-p). ``amplitude`` (V) may be a function of frequency: where |T|
    is large, |V_x| ~ v_inj/|T| must stay well above the switching ripple, so
    inject more at low frequency, exactly as on the bench. ``base`` must be at
    steady state; each frequency runs on a copy of it."""
    out = []
    for f in freqs:
        amp = amplitude(f) if callable(amplitude) else amplitude
        s = base.snapshot()
        t0 = s.t
        s.inj_fn = lambda t, f=f, t0=t0, a=amp: a * math.sin(2 * math.pi * f * (t - t0))
        t_settle = max(settle_cycles / f, min_time)
        t_meas = math.ceil(max(measure_cycles / f, min_time) * f) / f   # whole injection periods
        w = s.run(t0 + t_settle + t_meas)
        m = w.window(t0 + t_settle)
        tt = w.t[m]
        sense = w.vload[m] if s.p.sense_at_load else w.vout[m]
        vy, vx = sense, sense + amp * np.sin(2 * math.pi * f * (tt - t0))
        ph = np.exp(-2j * math.pi * f * (tt - t0))
        span = tt[-1] - tt[0]
        Y, X = 2 * np.trapezoid(vy * ph, tt) / span, 2 * np.trapezoid(vx * ph, tt) / span
        T = -Y / X
        ripple = float(np.ptp(vy - np.real(Y * np.conj(ph))))     # what is left after the injected tone
        out.append({"f": f, "mag_db": 20 * math.log10(abs(T)), "phase_deg": math.degrees(np.angle(T)),
                    "vx_amp": abs(X), "vy_amp": abs(Y), "inject_v": amp, "ripple_pp": ripple,
                    # validated against an analytic loop: with |V_x| below the residual
                    # ripple (p-p) errors reach several dB / ~20 deg; raise the injection there
                    "snr_ok": abs(X) >= ripple})
    return out


def margins(points: list[dict]) -> dict:
    """Crossover, phase margin and gain margin interpolated (log f) from
    loop_gain points. Phase is unwrapped upward from the lowest frequency, so
    PM = 180 + phase at the 0 dB crossing; GM = -|T| where the phase reaches -180."""
    pts = sorted(points, key=lambda p: p["f"])
    lf = np.log([p["f"] for p in pts])
    mag = np.array([p["mag_db"] for p in pts])
    ph = np.degrees(np.unwrap(np.radians([p["phase_deg"] for p in pts])))
    if ph[0] > 180:
        ph -= 360
    fc = pm = gm = f180 = None
    for i in range(len(pts) - 1):
        if fc is None and mag[i] >= 0 > mag[i + 1]:
            r = mag[i] / (mag[i] - mag[i + 1])
            fc = float(np.exp(lf[i] + r * (lf[i + 1] - lf[i])))
            pm = float(180 + ph[i] + r * (ph[i + 1] - ph[i]))
            pm = (pm + 180) % 360 - 180          # a margin is defined modulo 360 degrees
        if f180 is None and ph[i] > -180 >= ph[i + 1]:
            r = (ph[i] + 180) / (ph[i] - ph[i + 1])
            f180 = float(np.exp(lf[i] + r * (lf[i + 1] - lf[i])))
            gm = float(-(mag[i] + r * (mag[i + 1] - mag[i])))
    return {"fc_hz": fc, "phase_margin_deg": pm, "gain_margin_db": gm, "f180_hz": f180}
