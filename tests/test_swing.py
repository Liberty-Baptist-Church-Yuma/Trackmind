"""
Auto mode must not swing the camera back and forth at high speed.

Field report on v1.8.4: the camera whipped one way and then the other at speed.
Reproduced in the camera simulator two ways:

  * A glitch in the aim point (an arm raise, a lean, a pose-model hiccup) of
    0.12-0.2 of the frame for under half a second. It polluted the speaker's
    velocity estimate, Auto switched to "fast", and the predicted-position
    edge catch-up then drove the camera at up to speed 16-19, with the
    reversal guard overwhelmed by the large predicted error.
  * Reversing from speed: the commanded speed slewed straight through zero to
    full speed the other way.
"""

import pytest

import autopilot
import autotrack
from autopilot import AutoPilot, _Axis
from camsim import Camera, simulate, walk_then_stop

from test_autopilot import _configure, _warm


def _spiky(tracker, clock, spikes):
    """Add (t0, t1, dx, dy) jumps to the aim point while sim time is in the window."""
    orig, base = tracker.process, {}

    def process(det):
        base.setdefault("t", clock.t)
        t = clock.t - base["t"]
        if det is not None:
            for t0, t1, dx, dy in spikes:
                if t0 <= t < t1:
                    det = (min(1, max(0, det[0] + dx)), min(1, max(0, det[1] + dy))) + tuple(det[2:])
        return orig(det)

    tracker.process = process
    return tracker


# ── Closed loop ────────────────────────────────────────────────

@pytest.mark.parametrize("fov,delay", [(25, 0.30), (25, 0.55), (12, 0.55)])
@pytest.mark.parametrize("jump", [0.12, 0.20])
def test_a_gesture_jump_does_not_make_a_still_speaker_swing(settings, clock, fov, delay, jump):
    _configure(settings, "auto")
    settings.pan_fast = settings.tilt_fast = 24
    cam = Camera(fov=fov, video_latency=delay, miss_rate=0.03)
    tr, v = _warm(cam, clock, seed=3)
    _spiky(tr, clock, [(2.0, 2.4, jump, 0.0), (4.0, 4.4, -jump, 0.0)])
    r = simulate(autotrack, clock, v, lambda t: 0.0, cam, seconds=8, seed=3, tracker=tr)
    assert max(abs(p) for p in r.pan) <= 3, "camera whipped at a speaker who hadn't moved"
    assert r.reversals() == 0


@pytest.mark.parametrize("fov", [25, 12])
@pytest.mark.parametrize("jump", [0.15, -0.15])
def test_a_jump_while_walking_does_not_whip_the_camera(settings, clock, fov, jump):
    _configure(settings, "auto")
    settings.pan_fast = settings.tilt_fast = 24
    cam = Camera(fov=fov, video_latency=0.55, miss_rate=0.03)
    tr, v = _warm(cam, clock, seed=4)
    script = walk_then_stop(dist=14.0, speed=5.0, start=1.0)
    _spiky(tr, clock, [(3.0, 3.4, jump, 0.0)])
    r = simulate(autotrack, clock, v, script, cam, seconds=9, seed=4, tracker=tr)
    walker = 5.0 / cam.fov                                    # frame widths / s
    assert max(abs(c) for c in r.cam_v) / cam.fov <= 1.8 * walker + 0.1
    assert r.reversals() <= 1


# ── The pieces ─────────────────────────────────────────────────

def test_a_lone_jump_is_not_believed_but_a_move_that_sticks_is():
    a = _Axis(0)
    t = 0.0
    for _ in range(10):                                       # steady at 0.5
        t += 0.067; a.on_ego(t, 0.0, True); assert a.gate(t, 0.5) == pytest.approx(0.5)
    t += 0.067
    a.on_ego(t, 0.0, True)
    used = a.gate(t, 0.7)                                     # suddenly 0.2 away
    assert used == pytest.approx(0.5) and a.jumping           # keeps steering on the last good position
    for _ in range(3):
        t += 0.067; a.on_ego(t, 0.0, True)
        assert a.gate(t, 0.7) == pytest.approx(0.5)           # still not sure
    for _ in range(10):                                       # ...it stays there: accept it
        t += 0.067; a.on_ego(t, 0.0, True)
        used = a.gate(t, 0.7)
    assert used == pytest.approx(0.7) and not a.jumping
    assert abs(a.w) < 0.1                                     # and the jump left no speed behind


def test_a_glitch_does_not_leave_a_speed_in_the_velocity_estimate():
    a = _Axis(0)
    t = 0.0
    for i in range(12):
        t += 0.067; a.on_ego(t, 0.0, True); a.observe(t, a.gate(t, 0.5))
    for i in range(6):                                        # 0.4 s glitch, then back
        t += 0.067; a.on_ego(t, 0.0, True); a.observe(t, a.gate(t, 0.72))
    for i in range(10):
        t += 0.067; a.on_ego(t, 0.0, True); a.observe(t, a.gate(t, 0.5))
    assert abs(a.w) < 0.05


def test_a_real_walk_is_never_mistaken_for_a_glitch():
    """From rest up to 0.7 frame widths / s (a brisk walk in a tight shot) at 10 fps, as a person speeds up."""
    a = _Axis(0)
    t, x, v = 0.0, 0.2, 0.0
    for i in range(30):
        t += 0.1
        v = min(0.7, v + 1.5 * 0.1)                           # about 1.5 frame widths / s^2
        x += v * 0.1
        a.on_ego(t, 0.0, True)
        a.observe(t, a.gate(t, x))
        assert not a.jumping, f"mistook a real walk for a glitch at frame {i}"
    assert a.w == pytest.approx(0.7, abs=0.1)


def test_reversing_from_speed_stops_first_and_waits():
    p = AutoPilot(gain_pan=0.05, latency=0.5)
    p.situation = "fast"
    a = p.axes[0]
    style = autopilot.STYLES["balanced"]
    a.b_cmd, a.active = 0.6, -1                               # running hard to the right...
    a.w = 0.4                                                 # ...and the subject has turned round and is walking left to right
    t, trace = 10.0, []
    for _ in range(25):                                       # subject now far on the other side
        t += 0.067
        p._axis(a, 0.95, 0.5, 0.067, style, t, 24)
        trace.append(a.b_cmd)
    assert all(b >= -1e-9 for b in trace[:4]), "went straight through zero at speed"
    assert min(trace) < -0.05, "should still turn round eventually"
    first_reversed = next(i for i, b in enumerate(trace) if b < -0.02)
    first_stopped = next(i for i, b in enumerate(trace) if b < 0.12)
    assert (first_reversed - first_stopped) * 0.067 >= 0.2, "no pause between the two directions"


def test_slow_corrections_can_reverse_at_once():
    p = AutoPilot(gain_pan=0.05, latency=0.5)
    a = p.axes[0]
    a.b_cmd, a.active = 0.05, 1                               # barely creeping
    t = 10.0
    for _ in range(4):
        t += 0.067
        p._axis(a, 0.9, 0.5, 0.067, autopilot.STYLES["balanced"], t, 24)
    assert a.b_cmd < 0.05                                     # not held up by the reversal wait


def test_changing_vertical_aim_is_not_waited_out_as_a_glitch(settings, clock, visca):
    """Moving the Vertical aim slider steps the aim point on purpose; the camera should follow at once."""
    settings.auto_mode, settings.track_offset = True, 0
    tr = autotrack.AutoTracker(visca)
    for _ in range(12):
        clock.advance(0.067)
        tr.process((0.5, 0.50, 0.15, 0.5))
    settings.track_offset = 4                                  # a few notches: the aim point drops by ~0.1
    clock.advance(0.067)
    tr.process((0.5, 0.60, 0.15, 0.5))
    assert not tr.pilot.axes[1].jumping
