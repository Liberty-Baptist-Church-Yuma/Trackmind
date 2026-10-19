"""
The pulpit hold zone belongs to the pulpit, not to the picture.

Field report (2026-10-18, sketch): the speaker steps down in front of the pulpit,
the camera tilts down to follow them, and then the tracker decides it needs the
pulpit shot again, because with the camera tilted down their head sits in the
upper part of the picture, where the hold zone is. The zone is defined for the
picture the camera has at the pulpit. Once the camera has moved, the zone has to
move with the pulpit in the picture, so the speaker is judged against where the
zone really is.
"""

import pytest

import autotrack
from autotrack import AutoTracker
from camsim import Camera, simulate

UNITS_PER_DEG = 16          # PTZOptics: AutoTracker.ANCHOR_UNITS position units are about 1 degree


@pytest.fixture
def anchor(settings, clock, visca):
    settings.anchor_enabled = True
    settings.anchor_pan = settings.anchor_tilt = 0
    settings.anchor_range, settings.anchor_dwell = 10, 1.0
    settings.anchor_hold, settings.anchor_hold_top = 0.25, 0.5
    tr = AutoTracker(visca)
    return tr


def _at_pulpit_but_idle(tr, clock):
    """The camera has come to rest 3 steps from the pulpit position, not moving."""
    tr.cam_pos = (3 * tr.ANCHOR_UNITS, 0, clock())
    tr._prev_pan = tr._prev_tilt = 0
    tr._vx = 0.0


def _try_snap(tr, clock, cx, top, seconds=3.0):
    for _ in range(int(seconds / 0.1)):
        clock.advance(0.1)
        tr.cam_pos = (tr.cam_pos[0], tr.cam_pos[1], clock())
        tr._anchor(clock(), cx, top)
    return tr.anchor_state


# ── The decision ───────────────────────────────────────────────

def test_a_speaker_in_the_pulpit_zone_gets_the_snap(anchor, clock, visca):
    _at_pulpit_but_idle(anchor, clock)
    assert _try_snap(anchor, clock, cx=0.5, top=0.2) == "snapping"
    assert visca.presets


def test_a_speaker_below_the_zone_does_not(anchor, clock, visca):
    _at_pulpit_but_idle(anchor, clock)
    assert _try_snap(anchor, clock, cx=0.5, top=0.7) == "free"
    assert not visca.presets


def test_the_camera_tilting_down_moves_the_zone_up_with_the_pulpit(anchor, clock, visca):
    """The drawing: speaker below the pulpit, camera tilted down onto them, head now high in the picture."""
    _at_pulpit_but_idle(anchor, clock)
    anchor._world, anchor._world_ok = [0.0, -0.25], True          # the picture has shifted up by a quarter of its height
    assert _try_snap(anchor, clock, cx=0.5, top=0.3) == "free"   # in the picture's upper half, but not in the pulpit's
    assert not visca.presets


def test_the_same_picture_position_is_fine_once_the_camera_is_back_on_the_pulpit(anchor, clock, visca):
    _at_pulpit_but_idle(anchor, clock)
    anchor._world, anchor._world_ok = [0.0, 0.0], True
    assert _try_snap(anchor, clock, cx=0.5, top=0.3) == "snapping"


def test_sideways_works_the_same_way(anchor, clock, visca):
    _at_pulpit_but_idle(anchor, clock)
    anchor._world, anchor._world_ok = [-0.3, 0.0], True           # camera panned right: the pulpit is 0.3 left in the picture
    assert _try_snap(anchor, clock, cx=0.5, top=0.2) == "free"    # centred in the picture, 0.3 right of the pulpit: outside +/-0.25


def test_without_a_measured_shift_it_falls_back_to_the_picture(anchor, clock, visca):
    _at_pulpit_but_idle(anchor, clock)
    assert not anchor._world_ok
    assert _try_snap(anchor, clock, cx=0.5, top=0.2) == "snapping"


# ── Measuring the shift ────────────────────────────────────────

def test_the_picture_shift_is_measured_from_the_cameras_own_motion(anchor, clock):
    anchor.cam_pos = (0, 0, clock())                              # at the pulpit position: calibrate
    anchor._anchor(clock(), 0.5, 0.2)
    assert anchor._world_ok and anchor._world == [0.0, 0.0]
    for _ in range(10):                                           # tilting down: the background moves up the picture
        clock.advance(0.1)
        anchor.observe_ego(clock(), 0.0, -0.1, 0.9)               # -0.1 picture heights / s for 1 s
    assert anchor._world[1] == pytest.approx(-0.09, abs=0.02)     # (first sample has no interval to integrate)
    assert anchor._world[0] == pytest.approx(0.0, abs=1e-9)


def test_zooming_makes_the_shift_unknown_until_the_camera_is_back_on_the_pulpit(anchor, clock):
    anchor.cam_pos = (0, 0, clock())
    anchor._anchor(clock(), 0.5, 0.2)
    anchor._prev_zoom = 1
    clock.advance(0.1); anchor.observe_ego(clock(), 0.0, 0.0, 0.9)
    assert not anchor._world_ok
    anchor._prev_zoom = 0
    anchor.cam_pos = (0, 0, clock())
    anchor._anchor(clock(), 0.5, 0.2)
    assert anchor._world_ok


# ── Closed loop: the speaker steps off the pulpit and stays close ──

@pytest.mark.parametrize("auto", [False, True])
def test_a_speaker_standing_near_the_pulpit_but_off_it_does_not_pull_the_camera_back(settings, clock, visca, auto):
    """
    Behind the pulpit for a few seconds (the camera snaps and holds), then 4 degrees to the side for a long
    time. The camera follows, and rests within the snap range of the pulpit with the speaker centred in the
    picture, which is inside the picture's hold zone but not the pulpit's. It must not snap back.
    """
    settings.auto_mode = auto
    settings.anchor_enabled = True
    settings.anchor_pan = settings.anchor_tilt = 0
    settings.anchor_range, settings.anchor_dwell = 10, 1.0
    settings.anchor_hold, settings.anchor_hold_top = 0.10, 0.5         # a zone 2.5 degrees either side at this zoom
    cam = Camera(fov=25, video_latency=0.3)
    script = lambda t: 0.0 if t < 5.0 else 4.0

    def feed(tr, cam_angle):
        tr.cam_pos = (cam_angle * UNITS_PER_DEG, 0, clock())

    r = simulate(autotrack, clock, visca, script, cam, seconds=25, seed=1, on_frame=feed)
    assert len(visca.presets) == 1, f"snapped to the pulpit {len(visca.presets)} times; once, behind the pulpit, is right"
    assert r.tracker.anchor_state == "free"
    assert r.max_error(15, 25) < 0.08            # and the camera is on them


def test_walking_back_to_the_pulpit_still_snaps(settings, clock, visca):
    settings.auto_mode = False
    settings.anchor_enabled = True
    settings.anchor_pan = settings.anchor_tilt = 0
    settings.anchor_range, settings.anchor_dwell = 10, 1.0
    settings.anchor_hold, settings.anchor_hold_top = 0.10, 0.5
    cam = Camera(fov=25, video_latency=0.3)
    script = lambda t: 0.0 if t < 4.0 else (4.0 if t < 14.0 else 0.0)  # pulpit, away, back

    def feed(tr, cam_angle):
        tr.cam_pos = (cam_angle * UNITS_PER_DEG, 0, clock())

    simulate(autotrack, clock, visca, script, cam, seconds=30, seed=1, on_frame=feed)
    assert len(visca.presets) == 2               # once on arrival, once on return
