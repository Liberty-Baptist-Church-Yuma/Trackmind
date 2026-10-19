"""
The aim dot. It marks the point the camera is really aiming at on the speaker,
so it must come from the same number the tracker steers by, and Vertical aim
must move it on the speaker (not the speaker's box with it).
"""

import types

import pytest

import autotrack
from autotrack import PersonDetector, Controller

from test_lock import FakePose, Scene, step, _person_landmarks


def _bare_detector():
    d = PersonDetector.__new__(PersonDetector)
    d._aspect = 16 / 9
    return d


@pytest.fixture
def detector(settings):
    settings.track_focus, settings.track_offset = "upper", 0
    return _bare_detector()


def _aim(detector, offset, lms):
    autotrack.SETTINGS.track_offset = offset
    return detector._landmarks_to_bbox(lms, aspect=16 / 9)


def test_detection_carries_the_aim_point_and_the_speakers_box(detector):
    b = _aim(detector, 0, _person_landmarks(0.5, 0.5))
    assert len(b) == 7
    cx, cy, w, h, fill, bx, by = b
    assert 0.0 < bx < 1.0 and 0.0 < by < 1.0 and w > 0 and h > 0


def test_vertical_aim_moves_the_aim_point_but_not_the_speakers_box(detector):
    lms = _person_landmarks(0.5, 0.5)
    high, mid, low = (_aim(detector, o, lms) for o in (-7, 0, 7))
    # the speaker did not move...
    assert high[5:] == pytest.approx(low[5:]) and high[2:4] == pytest.approx(low[2:4])
    # ...but the aim point slides down their body as Vertical aim goes up
    assert high[1] < mid[1] < low[1]
    # each step is 5-10% of the box height, so the whole range is clearly visible
    assert (low[1] - high[1]) == pytest.approx(1.4 * low[3], rel=0.05)


def test_horizontal_aim_is_the_torso_line_even_when_the_head_turns(detector):
    lms = _person_landmarks(0.5, 0.5)
    lms[0] = types.SimpleNamespace(x=0.62, y=lms[0].y, visibility=0.95)    # nose well off to one side
    cx, _, _, _, _, bx, _ = _aim(detector, 0, lms)
    assert cx == pytest.approx(0.5, abs=1e-6)          # aim stays on the torso line
    assert bx > cx + 0.02                              # the box middle is dragged toward the nose


def test_box_middle_maps_back_to_the_full_frame_from_a_locked_crop(monkeypatch, settings, clock):
    scene = Scene()
    monkeypatch.setattr(PersonDetector, "_make_pose", lambda self: FakePose(scene))
    monkeypatch.setattr(autotrack.mp, "solutions", types.SimpleNamespace(pose=None), raising=False)
    settings.track_focus, settings.track_offset = "upper", 0
    d = PersonDetector()
    scene.people = [(0.7, 0.5)]
    step(d, scene, clock)                              # full-frame acquire
    got = step(d, scene, clock)                        # cropped search around the subject
    assert got[5] == pytest.approx(0.7, abs=0.02) and got[6] == pytest.approx(0.5, abs=0.15)


def test_status_reports_the_aim_point_and_the_box_middle(settings):
    class FakeThread:
        running = tracking = True
        status = "TRACKING"
        latest_frame = object()
        frame_id = 1
        fps = 30.0
        latest_detection = (0.61, 0.52, 0.2, 0.6, 0.5, 0.60, 0.45)
        def is_alive(self): return True

    c = Controller()
    c._thread = FakeThread()
    det = c.status(full=True)["detection"]
    assert (det["cx"], det["cy"], det["bx"], det["by"]) == (0.61, 0.52, 0.6, 0.45)
    # an older 4/5-value detection (no separate box middle) falls back to the aim point
    FakeThread.latest_detection = (0.4, 0.5, 0.2, 0.6)
    det = c.status(full=True)["detection"]
    assert (det["bx"], det["by"]) == (0.4, 0.5)
