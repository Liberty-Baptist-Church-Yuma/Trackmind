"""Pan/tilt control: speed curve, smoothing, dropout handling, closed-loop behaviour."""

import random

import autotrack
from autotrack import AutoTracker, OneEuroFilter, smooth_speed


# ── Speed curve ────────────────────────────────────────────────

def test_idle_inside_dead_zone():
    assert smooth_speed(0.5 + 0.10, 0.14, 2, 10) == (0.0, 0)


def test_starts_outside_dead_zone_and_scales_with_distance():
    near, a = smooth_speed(0.5 + 0.16, 0.14, 2, 10)
    far, _  = smooth_speed(0.5 + 0.45, 0.14, 2, 10)
    assert a == 1 and 2 <= near < far <= 10


def test_keeps_centering_inside_dead_zone_once_moving():
    # Hysteresis: a move started outside the dead zone carries on toward centre
    speed, active = smooth_speed(0.5 + 0.10, 0.14, 2, 10, active=1)
    assert active == 1 and 1.0 <= speed < 2


def test_move_ends_near_centre_and_never_reverses():
    assert smooth_speed(0.5 + 0.02, 0.14, 2, 10, active=1) == (0.0, 0)
    # Subject crossed centre (overshoot) — stop, don't swing back
    assert smooth_speed(0.5 - 0.08, 0.14, 2, 10, active=1) == (0.0, 0)


def test_never_exceeds_fast_speed():
    for i in range(101):
        speed, _ = smooth_speed(i / 100, 0.14, 2, 10)
        assert abs(speed) <= 10


# ── Jitter filter ──────────────────────────────────────────────

def test_one_euro_suppresses_jitter_on_still_subject():
    rnd = random.Random(1)
    f = OneEuroFilter()
    raw = [0.6 + rnd.uniform(-0.02, 0.02) for _ in range(200)]
    out = [f(x, 0.04) for x in raw][50:]
    assert max(out) - min(out) < 0.5 * (max(raw) - min(raw))


def test_one_euro_follows_walking_subject_closely():
    f = OneEuroFilter()
    x = 0.2
    for _ in range(100):
        x += 0.4 * 0.04          # 0.4 frame widths / s
        y = f(x, 0.04)
    assert abs(y - x) < 0.03


# ── Tracker reactions ──────────────────────────────────────────

def det(cx, cy=0.5, h=0.4):
    return (cx, cy, 0.2, h)


def run(tracker, clock, frames, cx, dt=0.04):
    for _ in range(frames):
        clock.advance(dt)
        tracker.process(det(cx) if cx is not None else None)


def test_single_missed_frame_does_not_stop_camera(settings, clock, visca):
    t = AutoTracker(visca)
    run(t, clock, 40, 0.9)                      # subject far right: panning
    assert visca.pan != 0
    sent = len(visca.moves)
    run(t, clock, 2, None)                      # two dropped detections
    assert visca.pan != 0, "a blip in detection must not slam the camera to a stop"
    run(t, clock, 5, 0.9)
    assert all(m != (0, 0) for m in visca.moves[sent:])


def test_long_loss_stops_then_goes_home(settings, clock, visca):
    t = AutoTracker(visca)
    run(t, clock, 40, 0.9)
    run(t, clock, 15, None)                     # 0.6 s
    assert visca.pan == 0
    assert visca.presets == []
    run(t, clock, 50, None)                     # past lost_timeout
    assert visca.presets == [settings.home_preset]


def test_braking_is_quicker_than_accelerating(settings, clock, visca):
    t = AutoTracker(visca)
    n = 0
    while visca.pan > -10 and n < 200:
        run(t, clock, 1, 0.99); n += 1
    accel_frames = n
    n = 0
    while visca.pan != 0 and n < 200:
        run(t, clock, 1, 0.5); n += 1
    assert n < accel_frames / 2


def test_command_rate_is_limited(settings, clock, visca):
    t = AutoTracker(visca)
    run(t, clock, 250, 0.95, dt=0.01)           # 100 fps for 2.5 s
    assert len(visca.moves) <= 2.5 / AutoTracker.CMD_INTERVAL + 2


def test_zoom_hysteresis_reaches_target_instead_of_band_edge(settings, clock, visca):
    settings.zoom_enabled, settings.zoom_target, settings.zoom_dead = True, 0.5, 0.2
    t = AutoTracker(visca)
    for h in (0.25, 0.28, 0.35, 0.42):          # fill growing as the camera zooms in
        clock.advance(0.04); t.process(det(0.5, h=h))
    # 0.35 and 0.42 are inside the ±0.2 band; old logic stopped at 0.30
    assert visca.zooms == [1]
    for _ in range(30):
        clock.advance(0.04); t.process(det(0.5, h=0.47))
    assert visca.zooms[-1] == 0


# ── Closed loop with video latency ─────────────────────────────

def simulate(settings, clock, script, seconds=8.0, latency=0.3, cmd_latency=0.1,
             k=0.05, fps=25, miss_rate=0.0, seed=0):
    """
    Camera + scene simulation. `script(t)` gives the subject's world position
    (frame widths from the camera's start). The tracker sees each frame
    `latency` s late, and the camera responds to commands `cmd_latency` s
    late, like an RTSP feed and VISCA over IP. k = frame widths/s per speed unit.
    Returns the per-frame (t, image_x, pan_speed) log.
    """
    rnd = random.Random(seed)
    visca = autotrack_visca()
    tracker = AutoTracker(visca)
    dt = 1.0 / fps
    cam, t = 0.0, 0.0
    history, cmds, log = [], [], []
    while t < seconds:
        cmds.append((t, visca.pan))
        applied = next((p for (ct, p) in reversed(cmds) if ct <= t - cmd_latency), 0)
        cam += -applied * k * dt                  # VISCA: negative pan = right
        img_x = 0.5 + script(t) - cam
        history.append((t, img_x))
        seen = next((x for (ht, x) in reversed(history) if ht <= t - latency), None)
        clock.advance(dt)
        visible = seen is not None and 0.0 <= seen <= 1.0 and rnd.random() >= miss_rate
        tracker.process(det(seen) if visible else None)
        log.append((t, img_x, visca.pan))
        t += dt
    return log


def autotrack_visca():
    from conftest import FakeVisca
    return FakeVisca()


def reversals(log):
    n, last = 0, 0
    for _, _, p in log:
        d = (p > 0) - (p < 0)
        if d and last and d != last:
            n += 1
        if d:
            last = d
    return n


def walk_then_stop(t):
    # Stands at centre, walks right 0.35 frame widths at 0.5/s, then stops
    return 0.0 if t < 1.0 else min(0.35, (t - 1.0) * 0.5)


def test_walk_then_stop_no_hunting(settings, clock):
    log = simulate(settings, clock, walk_then_stop)
    assert reversals(log) == 0
    final = [abs(x - 0.5) for (t, x, _) in log if t > 6.0]
    assert max(final) < settings.pan_dead


def test_walk_then_stop_with_detection_dropouts(settings, clock):
    log = simulate(settings, clock, walk_then_stop, miss_rate=0.15, seed=3)
    assert reversals(log) == 0
    stops = sum(1 for a, b in zip(log, log[1:]) if a[2] != 0 and b[2] == 0)
    assert stops <= 2, f"camera stop-started {stops} times on a single walk"


def test_still_subject_with_pose_jitter_keeps_camera_still(settings, clock):
    rnd = random.Random(7)
    log = simulate(settings, clock, lambda t: 0.12 + rnd.uniform(-0.03, 0.03))
    moving = [p for (t, _, p) in log if t > 3.0]
    assert all(p == 0 for p in moving)
