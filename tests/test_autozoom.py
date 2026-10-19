"""
Auto mode zoom (autopilot.ZoomPilot) in a closed loop with a simulated zoom
lens: the subject's size changes at `true_rate` ln-units/s per (speed + 1)
after a command delay, and the pilot sees it a video delay late, with noise.
"""

import math
import random
import types

import pytest

import autopilot
from autopilot import ZoomPilot


class Lens:
    def __init__(self, fill=0.2, true_rate=0.06, cmd_latency=0.1, video_latency=0.3,
                 noise=0.01, lo=0.05, hi=0.95, seed=0):
        self.lnf = math.log(fill)
        self.rate, self.cmd_l, self.vid_l = true_rate, cmd_latency, video_latency
        self.noise, self.lo, self.hi = noise, math.log(lo), math.log(hi)
        self.rnd = random.Random(seed)
        self.cmds = [(-1e9, 0, 0)]
        self.hist = []

    def command(self, t, d, speed):
        if (d, speed) != self.cmds[-1][1:]:
            self.cmds.append((t, d, speed))

    def advance(self, t, dt):
        _, d, sp = next(c for c in reversed(self.cmds) if c[0] <= t - self.cmd_l)
        self.lnf = min(self.hi, max(self.lo, self.lnf + d * self.rate * (sp + 1) * dt))
        self.hist.append((t, self.lnf))

    def seen(self, t):
        past = [h for h in self.hist if h[0] <= t - self.vid_l]
        lnf = past[-1][1] if past else self.hist[0][1]
        return math.exp(lnf) * (1 + self.rnd.gauss(0, self.noise))


def simulate(z, lens, seconds=12.0, fps=15, situation="still", cx=0.5, moving=False,
             target=0.45, dead=0.20, max_speed=5, latency=0.4, start=0.0):
    """Returns [(t, dir, speed, true fill)]."""
    out, t, phys, next_f = [], start, 1 / 240, start
    d = sp = 0
    while t < start + seconds:
        lens.advance(t, phys)
        if t >= next_f:
            next_f += 1 / fps
            sit = situation(t) if callable(situation) else situation
            d, sp = z.step(t, lens.seen(t), cx, sit, moving, target, dead, max_speed, latency)
            lens.command(t, d, sp)
            out.append((t, d, sp, math.exp(lens.lnf)))
        t += phys
    return out


def directions(run):
    seq = []
    for _, d, _, _ in run:
        if d and (not seq or seq[-1] != d):
            seq.append(d)
    return seq


def test_zooms_in_to_target_without_overshoot_or_hunting():
    z, lens = ZoomPilot(), Lens(fill=0.2)
    run = simulate(z, lens)
    assert directions(run) == [1]                       # one zoom in, never back out
    final = run[-1][3]
    assert abs(math.log(final / 0.45)) < math.log(1 + 0.2 / 0.45)      # inside the band
    assert final <= 0.45 * 1.15                          # didn't sail past the target
    assert run[-1][1] == 0


def test_eases_in_rather_than_running_at_one_speed():
    z, lens = ZoomPilot(), Lens(fill=0.12)
    run = simulate(z, lens, max_speed=7)
    speeds = [sp for _, d, sp, _ in run if d]
    assert max(speeds) > min(speeds)                     # slowed down approaching target
    assert speeds[-1] < max(speeds)


def test_waits_for_the_subject_to_settle_before_zooming_in():
    z, lens = ZoomPilot(), Lens(fill=0.2)
    run = simulate(z, lens, seconds=0.8)
    assert all(d == 0 for _, d, _, _ in run)             # < ZOOM_IN_STILL: still waiting


def test_never_zooms_in_on_a_walking_subject_or_off_centre():
    for kw in (dict(situation="walking"), dict(cx=0.75), dict(moving=True)):
        z, lens = ZoomPilot(), Lens(fill=0.2)
        assert directions(simulate(z, lens, seconds=6, **kw)) == [], kw


def test_zooms_out_when_too_big_even_while_walking():
    z, lens = ZoomPilot(), Lens(fill=0.85)
    run = simulate(z, lens, situation="walking")
    assert directions(run) == [-1]
    assert run[-1][3] < 0.45 + 0.2


def test_fast_subject_gets_a_wider_shot():
    z, lens = ZoomPilot(), Lens(fill=0.55)
    run = simulate(z, lens, situation="fast", dead=0.08)
    assert directions(run) == [-1]
    assert run[-1][3] < 0.45


def test_zoom_in_stops_when_subject_starts_walking():
    z, lens = ZoomPilot(), Lens(fill=0.15, true_rate=0.03)
    run = simulate(z, lens, max_speed=1, situation=lambda t: "still" if t < 3.0 else "walking")
    zooming_after = [d for t, d, _, _ in run if t > 3.2]
    assert any(d == 1 for t, d, _, _ in run if t < 3.0)
    assert all(d != 1 for d in zooming_after)


def test_learns_the_cameras_zoom_rate():
    z, lens = ZoomPilot(rate=0.03), Lens(fill=0.1, true_rate=0.09)
    run = simulate(z, lens, seconds=10, max_speed=2)
    assert z.rate > 0.05                                 # moved toward the real 0.09
    assert abs(math.log(run[-1][3] / 0.45)) < math.log(1 + 0.2 / 0.45)


def test_stops_at_the_end_of_the_zoom_range_and_does_not_keep_asking():
    z, lens = ZoomPilot(), Lens(fill=0.1, hi=0.18)      # lens maxes out well short of target
    run = simulate(z, lens, seconds=10)
    assert run[-1][1] == 0
    starts = sum(1 for a, b in zip(run, run[1:]) if a[1] == 0 and b[1] == 1)
    assert starts <= 1


def test_holds_inside_the_band():
    z = ZoomPilot()
    for i in range(40):
        assert z.step(i * 0.066, 0.40, 0.5, "still", False, 0.45, 0.2, 5, 0.4) == (0, 0)


# ── In the tracker ─────────────────────────────────────────────

def test_tracker_auto_zoom_sends_speeds_and_stops_on_lost(settings, clock, visca):
    import autotrack
    settings.auto_mode, settings.zoom_enabled = True, True
    settings.zoom_target, settings.zoom_dead, settings.zoom_speed = 0.45, 0.2, 5
    speeds = []
    visca.zoom_in = lambda speed=1: (visca.zooms.append(1), speeds.append(speed))[0] or True
    t = autotrack.AutoTracker(visca)
    for _ in range(40):                                  # small, centred, still
        clock.advance(0.066)
        t.process((0.5, 0.5, 0.1, 0.2, 0.2))
        if t._prev_zoom:
            break
    assert visca.zooms == [1] and 0 <= speeds[0] <= 5
    clock.advance(5.0)                                   # subject gone past lost_timeout
    t.process(None)
    assert visca.zooms[-1] == 0 and t._prev_zoom == 0
    t.process((0.5, 0.5, 0.1, 0.2, 0.2))
    assert t.pilot.zoom.dir == 0                          # re-settles before zooming again


def test_zoom_rate_is_remembered(settings, clock, visca):
    import autotrack
    settings.auto_zoom_rate = 0.11
    assert autotrack.AutoTracker(visca).pilot.zoom.rate == pytest.approx(0.11)
    settings.auto_zoom_rate = 99
    assert autotrack.AutoTracker(visca).pilot.zoom.rate == autopilot.ZOOM_RATE_RANGE[1]


# ── Size reading ───────────────────────────────────────────────

def _lms(cx=0.5, top=0.2, h=0.6, aspect=16 / 9):
    lms = [types.SimpleNamespace(x=0.0, y=0.0, visibility=0.0) for _ in range(33)]
    rel = {0: (0, 0.06), 11: (-0.05, 0.19), 12: (0.05, 0.19), 23: (-0.04, 0.50),
           24: (0.04, 0.50), 25: (-0.04, 0.75), 26: (0.04, 0.75), 27: (-0.04, 1.0), 28: (0.04, 1.0)}
    for i, (dx, fy) in rel.items():
        lms[i] = types.SimpleNamespace(x=cx + dx * h / aspect, y=top + fy * h, visibility=0.95)
    return lms


def test_zoom_size_does_not_jump_when_knees_leave_the_picture(settings):
    import autotrack
    settings.track_focus = "full"
    d = autotrack.PersonDetector.__new__(autotrack.PersonDetector)
    d._aspect = 16 / 9
    whole = d._landmarks_to_bbox(_lms())
    cut = _lms()
    for i in (25, 26, 27, 28):
        cut[i].visibility = 0.1                           # zoomed in: legs out of frame
    tight = d._landmarks_to_bbox(cut)
    assert tight[3] < 0.75 * whole[3]                     # the box shrinks a lot...
    assert tight[4] == pytest.approx(whole[4], rel=0.02)  # ...the zoom size doesn't
    assert whole[4] == pytest.approx(whole[3], rel=0.25)  # same scale as the old fill


def test_zoom_size_falls_back_to_head_and_shoulders(settings):
    import autotrack
    settings.track_focus = "upper"
    d = autotrack.PersonDetector.__new__(autotrack.PersonDetector)
    d._aspect = 16 / 9
    full = d._landmarks_to_bbox(_lms())
    cut = _lms()
    for i in (23, 24, 25, 26, 27, 28):
        cut[i].visibility = 0.1
    cut[0].visibility = 0.95
    assert d._torso_fill(cut, 16 / 9) == pytest.approx(full[4], rel=0.25)
