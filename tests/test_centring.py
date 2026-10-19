"""
Gentle centring: in Auto mode, a speaker who is standing still but off-centre
is eased back toward the middle, slowly. It is a slight tendency, not a chase:
it only starts once they have been still for a while, only for the offset the
calm zone would otherwise leave alone, and never faster than speed 1-2.
"""

import pytest

import autopilot
import autotrack
from autopilot import AutoPilot
from camsim import Camera, simulate, walk_then_stop

from test_autopilot import _configure, _warm


def _run(settings, clock, offset_deg, centre, seconds=20, fov=25):
    _configure(settings, "auto")
    settings.auto_centre = centre
    cam = Camera(fov=fov, video_latency=0.4, miss_rate=0.03)
    tr, v = _warm(cam, clock)
    r = simulate(autotrack, clock, v, lambda t: offset_deg, cam, seconds=seconds, seed=5, tracker=tr)
    return r, cam


def test_a_still_speaker_a_little_off_centre_is_eased_to_the_middle(settings, clock):
    r, cam = _run(settings, clock, 1.8, centre=True)             # 7% of the frame off centre
    assert r.max_error(15, 20) < 0.035
    assert max(abs(p) for p in r.pan) <= 2, "should creep, not chase"
    assert r.reversals() == 0


def test_with_it_off_the_calm_zone_is_left_alone_as_before(settings, clock):
    r, cam = _run(settings, clock, 1.8, centre=False)
    assert r.max_error(15, 20) > 0.06
    assert not any(r.pan)


def test_it_is_slow_in_the_picture_whatever_the_zoom(settings, clock):
    for fov in (40, 10):
        r, cam = _run(settings, clock, 0.07 * fov, centre=True, fov=fov)
        assert max(abs(c) for c in r.cam_v) / cam.fov <= 0.06, f"too quick at {fov} degrees"


def test_a_tiny_offset_is_not_chased(settings, clock):
    r, cam = _run(settings, clock, 0.6, centre=True)             # 2.4% off: inside the dot
    assert not any(r.pan)


def test_following_a_walking_speaker_is_untouched(settings, clock):
    out = {}
    for centre in (False, True):
        _configure(settings, "auto")
        settings.auto_centre = False                             # warm both up the same way...
        cam = Camera(fov=25, video_latency=0.4, miss_rate=0.03)
        tr, v = _warm(cam, clock)
        settings.auto_centre = centre                            # ...and only then switch it
        r = simulate(autotrack, clock, v, walk_then_stop(dist=8.0, speed=4.0, start=2.0), cam, seconds=3.9, seed=5, tracker=tr)
        out[centre] = list(r.pan)
    assert out[True] == out[False]                               # not one command differs until they stop


def test_it_waits_for_a_calm_speaker():
    p = AutoPilot(gain_pan=0.04, latency=0.4)
    st = autopilot.STYLES["balanced"]
    t, cmds = 100.0, []
    for _ in range(60):                                          # 4 s at 15 fps, 7% off centre, not moving
        t += 1 / 15
        p.step(t, 0.57, 0.5, 0.5, "balanced", (24, 24), centre=True)
        cmds.append((t, p.output()[0]))
    first = next(t for t, c in cmds if c)
    assert first - 100.0 >= autopilot.CENTRE_AFTER - 0.2


def test_the_setting_is_per_profile_and_validates():
    d = autotrack.Settings().to_dict()
    assert d["auto_centre"] is True
    assert autotrack.SETTINGS_SCHEMA["auto_centre"]("off") is False
