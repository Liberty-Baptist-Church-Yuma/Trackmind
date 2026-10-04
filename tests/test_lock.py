"""
Subject lock. A fake pose model stands in for MediaPipe: like the real one it
returns only ONE person per image — the first visible one in the scene list
(the "most prominent") — so these tests exercise the real failure modes.
"""

import types

import numpy as np
import pytest

import autotrack
from autotrack import PersonDetector

W, H = 640, 360


class Scene:
    def __init__(self):
        self.people = []      # [(cx, cy)] in full-frame fractions, most prominent first

    def frame(self):
        # Encode each column's x index in the pixels, so the fake model can
        # tell which part of the frame a crop came from.
        f = np.zeros((H, W, 3), dtype=np.uint8)
        xs = np.arange(W)
        f[:, :, 0] = xs % 256
        f[:, :, 1] = xs // 256
        return f


def _person_landmarks(cx, cy):
    lm = [types.SimpleNamespace(x=cx, y=cy, visibility=0.0) for _ in range(33)]
    pts = {0: (cx, cy - 0.15), 11: (cx - 0.05, cy - 0.08), 12: (cx + 0.05, cy - 0.08),
           23: (cx - 0.04, cy + 0.12), 24: (cx + 0.04, cy + 0.12)}
    for i, (x, y) in pts.items():
        lm[i] = types.SimpleNamespace(x=x, y=y, visibility=0.95)
    return lm


class FakePose:
    def __init__(self, scene):
        self.scene = scene

    def process(self, img):
        a = int(img[0, 0, 0]) + 256 * int(img[0, 0, 1])
        w = img.shape[1]
        for (cx, cy) in self.scene.people:
            px = cx * W
            if a <= px < a + w:
                lm = _person_landmarks((px - a) / w, cy)
                return types.SimpleNamespace(pose_landmarks=types.SimpleNamespace(landmark=lm))
        return types.SimpleNamespace(pose_landmarks=None)

    def close(self):
        pass


@pytest.fixture
def scene(monkeypatch, settings):
    sc = Scene()
    monkeypatch.setattr(PersonDetector, "_make_pose", lambda self: FakePose(sc))
    monkeypatch.setattr(autotrack.mp, "solutions",
                        types.SimpleNamespace(pose=None), raising=False)
    settings.track_focus, settings.track_offset = "upper", 0
    return sc


def step(det, scene, clock, lock=True, dt=0.04):
    clock.advance(dt)
    return det.detect(scene.frame(), hard_lock=lock)


def test_crop_coordinates_map_back_to_full_frame(scene, clock):
    d = PersonDetector()
    scene.people = [(0.7, 0.5)]
    first = step(d, scene, clock)                  # full-frame acquire
    again = step(d, scene, clock)                  # cropped search
    assert first[0] == pytest.approx(0.7, abs=0.01)
    assert again[0] == pytest.approx(0.7, abs=0.01)


def test_lock_holds_when_a_more_prominent_person_appears(scene, clock):
    d = PersonDetector()
    scene.people = [(0.65, 0.5)]
    step(d, scene, clock)
    # Someone more prominent walks in on the far left. Unlocked MediaPipe
    # would switch to them; the locked crop shouldn't even see them.
    scene.people = [(0.12, 0.5), (0.65, 0.5)]
    for _ in range(10):
        got = step(d, scene, clock)
        assert got is not None and got[0] == pytest.approx(0.65, abs=0.01)


def test_unlocked_follows_most_prominent(scene, clock):
    d = PersonDetector()
    scene.people = [(0.12, 0.5), (0.65, 0.5)]
    assert step(d, scene, clock, lock=False)[0] == pytest.approx(0.12, abs=0.01)


def test_fast_exit_up_and_right_is_reacquired(scene, clock):
    """
    Reported in the field: the subject walked away quickly to the upper right,
    the lock lost them and kept searching the old spot until it was toggled off.
    """
    d = PersonDetector()
    x, y = 0.5, 0.55
    scene.people = [(x, y)]
    step(d, scene, clock)
    for _ in range(8):                             # moving fast up and right
        x, y = x + 0.03, y - 0.015
        scene.people = [(x, y)]
        assert step(d, scene, clock) is not None
    scene.people = []                              # blurred / not detected
    for _ in range(12):
        assert step(d, scene, clock) is None
    scene.people = [(0.95, 0.25)]                  # found again, far from the old spot
    found = None
    for _ in range(40):                            # within ~1.6 s
        found = step(d, scene, clock)
        if found:
            break
    assert found is not None and found[0] == pytest.approx(0.95, abs=0.01)


def test_lock_rejects_other_person_right_after_losing_subject(scene, clock):
    d = PersonDetector()
    scene.people = [(0.5, 0.5)]
    step(d, scene, clock)
    scene.people = [(0.85, 0.5)]                   # a different person, subject gone
    assert step(d, scene, clock) is None


def test_lock_can_reacquire_anywhere_after_long_absence(scene, clock):
    d = PersonDetector()
    scene.people = [(0.2, 0.5)]
    step(d, scene, clock)
    scene.people = []
    for _ in range(50):                            # 2 s unseen
        step(d, scene, clock)
    scene.people = [(0.9, 0.5)]
    assert step(d, scene, clock) is not None


def test_release_lock_reacquires_most_prominent(scene, clock):
    d = PersonDetector()
    scene.people = [(0.2, 0.5)]
    step(d, scene, clock)
    d.release_lock()
    scene.people = [(0.8, 0.5)]
    assert step(d, scene, clock)[0] == pytest.approx(0.8, abs=0.01)


def test_gesturing_arms_do_not_move_aim_point(scene, clock):
    d = PersonDetector()
    still = _person_landmarks(0.5, 0.5)
    waving = _person_landmarks(0.5, 0.5)
    waving[13] = types.SimpleNamespace(x=0.75, y=0.30, visibility=0.95)   # elbow out
    waving[14] = types.SimpleNamespace(x=0.70, y=0.25, visibility=0.95)
    a, b = d._landmarks_to_bbox(still), d._landmarks_to_bbox(waving)
    assert a[0] == pytest.approx(b[0]) and a[1] == pytest.approx(b[1])
    assert a[3] == pytest.approx(b[3])     # zoom fill unchanged too
