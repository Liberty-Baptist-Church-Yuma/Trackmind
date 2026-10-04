"""
Auto mode: the loop model, velocity estimate, quantizer, situations and the
hunting guard in isolation, then closed-loop against the camera simulator,
including the comparison with manual mode that the tuning was based on.
"""

import random

import numpy as np
import pytest

import autopilot
import autotrack
from autopilot import AutoPilot, LoopModel, _Axis
from camsim import Camera, fast_exit, pacing, simulate, still_with_sway, walk_then_stop


# ── Loop model ─────────────────────────────────────────────────

def _feed(model, gain, latency, seconds=12.0, fps=15, noise=0.01, seed=0, t0=100.0):
    """Drive a model with a known camera: random speed changes every ~0.6 s."""
    rnd = random.Random(seed)
    cmds, t, next_cmd = [], t0, t0
    while t < t0 + seconds:
        if t >= next_cmd:
            u = rnd.choice([-6, -4, -2, 0, 0, 2, 4, 6])
            model.on_command(t, u, 0)
            cmds.append((t, u))
            next_cmd = t + rnd.uniform(0.4, 0.9)
        applied = next((u for (ct, u) in reversed(cmds) if ct <= t - latency), 0)
        model.on_ego(t, gain * applied + rnd.gauss(0, noise), rnd.gauss(0, noise), 0.9)
        t += 1.0 / fps


@pytest.mark.parametrize("gain,latency", [(0.02, 0.3), (0.05, 0.45), (0.12, 0.6)])
def test_loop_model_learns_gain_and_latency(gain, latency):
    m = LoopModel(gain_pan=0.10, latency=0.35)
    _feed(m, gain, latency)
    assert m.fits > 0
    assert m.gain[0] == pytest.approx(gain, rel=0.2)
    assert m.latency == pytest.approx(latency, abs=0.1)


def test_loop_model_keeps_priors_without_motion():
    m = LoopModel(gain_pan=0.08, latency=0.4)
    for i in range(300):
        m.on_ego(100 + i / 15, 0.0, 0.0, 0.9)
    assert m.fits == 0 and m.gain[0] == 0.08 and m.latency == 0.4


def test_loop_model_ignores_uncommanded_motion_during_blackout():
    m = LoopModel(gain_pan=0.05)
    m.blackout(200.0)                 # e.g. a preset recall until t=200
    for i in range(300):              # big motion, no commands
        m.on_ego(100 + i / 15, 0.5, 0.0, 0.9)
    assert m.fits == 0 and m.gain[0] == 0.05


def test_low_confidence_ego_is_ignored():
    m = LoopModel()
    _feed(m, 0.05, 0.3)
    fits = m.fits
    for i in range(200):
        m.on_ego(200 + i / 15, 5.0, 0.0, 0.05)
    assert m.fits == fits


def test_commanded_shift_integrates_pending_motion():
    m = LoopModel(gain_pan=0.1)
    m.on_command(10.0, 5, 0)
    # 5 steps * 0.1 gain = 0.5 frame/s, for the last 0.4 s
    assert m.commanded_shift(10.4, 0, 0.4) == pytest.approx(0.2)
    assert m.commanded_shift(10.4, 0, 1.0) == pytest.approx(0.2)   # nothing before 10.0
    m.on_command(10.4, 0, 0)
    assert m.commanded_shift(10.6, 0, 0.4) == pytest.approx(0.1)


def test_rescale_on_zoom():
    m = LoopModel(gain_pan=0.05, gain_tilt=0.04)
    m.rescale(2.0)
    assert m.gain == pytest.approx([0.10, 0.08])


# ── Subject velocity independent of camera motion ─────────────

def test_velocity_is_zero_for_still_subject_while_camera_pans():
    a = _Axis(0)
    x, t = 0.7, 0.0
    for _ in range(30):              # background (and subject) slide left at 0.3/s
        t += 1 / 15
        x -= 0.3 / 15
        a.on_ego(t, -0.3, True)
        a.observe(t, x)
    assert abs(a.w) < 0.02


def test_velocity_of_walking_subject_while_camera_follows():
    a = _Axis(0)
    t = 0.0
    for _ in range(30):              # camera matches a 0.25/s walker: subject fixed in frame
        t += 1 / 15
        a.on_ego(t, -0.25, True)
        a.observe(t, 0.5)
    assert a.w == pytest.approx(0.25, abs=0.03)


def test_ego_loss_restarts_velocity_fit():
    a = _Axis(0)
    for i in range(10):
        a.on_ego(i / 15, 0.0, True); a.observe(i / 15, 0.5)
    a.on_ego(1.0, 0.0, False)
    assert len(a.hist) == 0


# ── Quantizer, situations, guard ──────────────────────────────

def _quant(seq, gain=0.1):
    p = AutoPilot(gain_pan=gain)
    a = p.axes[0]
    out = []
    for u in seq:
        a.b_cmd = u * gain
        out.append(p._quantize(a))
    return out


def test_quantizer_starts_and_stops_with_hysteresis():
    assert _quant([0.5, 0.6, 0.75, 0.5, 0.35, 0.25]) == [0, 0, 1, 1, 1, 0]


def test_quantizer_does_not_dither_between_steps():
    assert _quant([5.0, 5.6, 5.4, 5.7, 5.3, 5.75]) == [5] * 6
    assert _quant([5.0, 6.0])[-1] == 6


def test_situation_hysteresis():
    p = AutoPilot()
    p._classify(0.0, 0.10)
    assert p.situation == "walking"
    p._classify(0.1, 0.05)                    # between leave and enter: stays
    assert p.situation == "walking"
    p._classify(0.2, 0.02)
    p._classify(0.5, 0.02)
    assert p.situation == "walking"           # not yet LEAVE_AFTER
    p._classify(1.1, 0.02)
    assert p.situation == "still"
    p._classify(1.2, 0.6)
    assert p.situation == "fast"


def test_hunting_guard_ignores_following_a_turnaround():
    p = AutoPilot()
    p.axes[0].w = 0.2                         # subject walking right
    p.on_command(0.0, 4, 0)                   # camera was going left
    p.on_command(0.5, -4, 0)                  # follows the subject right
    assert len(p._reversals) == 0


def test_hunting_guard_softens_on_bounces():
    p = AutoPilot()
    t = 0.0
    for d in (4, -4, 4, -4):                  # back and forth, subject still
        p.on_command(t, d, 0); t += 0.5
    p._guard(t, 0.05)
    assert p.soften < 1.0


# ── Closed loop ────────────────────────────────────────────────

def _configure(settings, mode):
    settings.auto_mode = mode == "auto"
    settings.auto_style = "balanced"
    settings.pan_fast = settings.tilt_fast = {"manual10": 10, "manual5": 5}.get(mode, 5)


def _warm(cam, clock, seed=9):
    from conftest import FakeVisca
    v = FakeVisca()
    r = simulate(autotrack, clock, v, pacing(), cam, seconds=30, seed=seed)
    r.tracker.reset()
    v.pan = 0
    return r.tracker, v


@pytest.mark.parametrize("fov", [40, 25, 10])
def test_auto_learns_the_camera_from_cold(settings, clock, fov):
    from conftest import FakeVisca
    _configure(settings, "auto")
    cam = Camera(fov=fov, video_latency=0.3)
    r = simulate(autotrack, clock, FakeVisca(), pacing(), cam, seconds=30, seed=2)
    true_gain = cam.deg_per_step / fov
    true_latency = cam.video_latency + cam.cmd_latency + cam.ramp
    assert r.learned["learned"]
    assert r.learned["gain_pan"] == pytest.approx(true_gain, rel=0.3)
    assert r.learned["latency"] == pytest.approx(true_latency, abs=0.15)


def test_learned_values_are_remembered(settings, clock):
    from conftest import FakeVisca
    _configure(settings, "auto")
    simulate(autotrack, clock, FakeVisca(), pacing(), Camera(fov=25), seconds=30, seed=2)
    assert settings.auto_gain_pan == pytest.approx(1 / 25, rel=0.3)
    assert 0.2 < settings.auto_latency < 0.8


@pytest.mark.parametrize("fov", [40, 10])
def test_auto_holds_a_still_speaker(settings, clock, fov):
    _configure(settings, "auto")
    cam = Camera(fov=fov, video_latency=0.4, miss_rate=0.05)
    tr, v = _warm(cam, clock)
    n0 = len(v.moves)
    r = simulate(autotrack, clock, v, still_with_sway(), cam, seconds=12, seed=5, tracker=tr)
    assert r.reversals() == 0
    assert len(v.moves) - n0 <= 2            # at most one gentle re-centre


@pytest.mark.parametrize("fov", [40, 25, 10])
def test_auto_walk_then_stop_no_bounce_and_centres(settings, clock, fov):
    _configure(settings, "auto")
    cam = Camera(fov=fov, video_latency=0.4, miss_rate=0.05)
    tr, v = _warm(cam, clock)
    r = simulate(autotrack, clock, v, walk_then_stop(), cam, seconds=12, seed=5, tracker=tr)
    moves = [p for p in r.pan if p]
    big_back = [p for p in moves if p > 1]   # walking right needs pan < 0
    assert not big_back, "camera swung back after the speaker stopped"
    assert r.max_error(9, 12) < 0.2


def _score(settings, clock, mode, seeds=(21, 22)):
    from conftest import FakeVisca
    tot = dict(err=0.0, ss=0, jerk=0.0)
    scen = [(walk_then_stop(), 10), (pacing(), 24), (still_with_sway(), 12), (fast_exit(), 8)]
    for fov in (40, 25, 10):
        cam = Camera(fov=fov, video_latency=0.4, miss_rate=0.05)
        for seed in seeds:
            _configure(settings, mode)
            tr, v = _warm(cam, clock) if mode == "auto" else (None, FakeVisca())
            for script, secs in scen:
                if tr:
                    tr.reset(); v.pan = 0
                r = simulate(autotrack, clock, v, script, cam, seconds=secs, seed=seed, tracker=tr)
                tot["err"] += r.mean_error(); tot["ss"] += r.stop_starts(); tot["jerk"] += r.jerk()
    return tot


def test_auto_beats_manual_on_unseen_runs(settings, clock):
    """
    The comparison the tuning was based on, on seeds the tuning never saw:
    auto must frame at least as well as a well-tuned manual profile, with far
    fewer stop-starts and no rougher motion than the speed-10 setting it
    replaces.
    """
    auto = _score(settings, clock, "auto")
    m5   = _score(settings, clock, "manual5")
    m10  = _score(settings, clock, "manual10")
    assert auto["err"] <= m5["err"] * 1.05
    assert auto["ss"] * 3 < m5["ss"]
    assert auto["jerk"] < m10["jerk"]


# ── Wiring into the tracker ────────────────────────────────────

def test_manual_mode_never_uses_the_pilot(settings, clock, visca, monkeypatch):
    settings.auto_mode = False
    tr = autotrack.AutoTracker(visca)
    monkeypatch.setattr(tr.pilot, "step", lambda *a, **k: pytest.fail("pilot used in manual"))
    for _ in range(20):
        clock.advance(0.04); tr.process((0.9, 0.5, 0.2, 0.4))
    assert visca.pan != 0


def test_ego_ignored_while_zooming(settings, clock, visca):
    settings.auto_mode = True
    tr = autotrack.AutoTracker(visca)
    tr._prev_zoom = 1
    tr.observe_ego(clock(), 0.3, 0.0, 0.9)
    assert len(tr.pilot.model._ego) == 0


def test_preset_recall_blacks_out_learning(settings, clock, visca):
    settings.auto_mode = True
    tr = autotrack.AutoTracker(visca)
    for _ in range(10):
        clock.advance(0.04); tr.process((0.5, 0.5, 0.2, 0.4))
    for _ in range(80):               # lost → home preset recall
        clock.advance(0.04); tr.process(None)
    assert visca.presets
    tr.observe_ego(clock(), 0.4, 0.0, 0.9)
    assert len(tr.pilot.model._ego) == 0


def test_auto_settings_validate():
    assert autotrack.SETTINGS_SCHEMA["auto_style"]("Calm") == "calm"
    assert autotrack.SETTINGS_SCHEMA["auto_style"]("warp") == "balanced"
    assert autotrack.SETTINGS_SCHEMA["auto_mode"]("on") is True


def test_auto_mode_is_per_profile_but_learned_values_are_not():
    d = autotrack.Settings().to_dict()
    assert "auto_mode" in d and "auto_style" in d
    assert "auto_gain_pan" not in d and "auto_latency" not in d


# ── Ego-motion on real images (needs OpenCV) ──────────────────

def test_ego_motion_measures_a_known_shift():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "phaseCorrelate"):
        pytest.skip("OpenCV stubbed")
    rnd = np.random.default_rng(0)
    base = cv2.GaussianBlur((rnd.random((720, 1280)) * 255).astype(np.uint8), (9, 9), 0)
    frame = lambda dx: cv2.cvtColor(
        cv2.warpAffine(base, np.float32([[1, 0, dx], [0, 1, 0]]), (1280, 720),
                       borderMode=cv2.BORDER_REFLECT), cv2.COLOR_GRAY2BGR)
    ego = autopilot.EgoMotion()
    assert ego.update(frame(0), 1.0) is None
    bx, by, conf = ego.update(frame(-32), 1.1)      # 32 px left in 0.1 s
    assert bx == pytest.approx(-32 / 1280 / 0.1, rel=0.05)
    assert abs(by) < 0.02 and conf > 0.5
