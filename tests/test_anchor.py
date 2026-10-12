"""Pulpit anchor hold zone: a box from the top of the frame the speaker's head must stay in."""

import pytest

import autotrack
from autotrack import AutoTracker


@pytest.fixture
def held(settings, clock, visca):
    settings.anchor_enabled = True
    settings.anchor_pan, settings.anchor_tilt = 0, 0
    settings.anchor_hold, settings.anchor_hold_top = 0.25, 0.5
    tr = AutoTracker(visca)
    tr.anchor_state = "held"
    return tr


def frames(tr, clock, cx, cy, h, n=20):
    """Detections with aim point (cx, cy) and box height h; top of box = cy - h/2."""
    for _ in range(n):
        clock.advance(0.04)
        tr.process((cx, cy, 0.15, h))


def test_head_in_upper_zone_keeps_the_hold(held, clock, visca):
    frames(held, clock, 0.55, 0.45, 0.5)            # head at 0.20
    assert held.anchor_state == "held" and visca.moves == []


def test_leaning_and_gestures_keep_the_hold(held, clock, visca):
    frames(held, clock, 0.6, 0.60, 0.5)             # aim point low, head still at 0.35
    assert held.anchor_state == "held"


def test_head_below_the_zone_releases(held, clock):
    frames(held, clock, 0.5, 0.85, 0.4)             # head at 0.65 — walked down the steps
    assert held.anchor_state == "free"


def test_brief_dip_does_not_release(held, clock):
    frames(held, clock, 0.5, 0.85, 0.4, n=5)        # 0.2 s < ANCHOR_LEAVE
    frames(held, clock, 0.5, 0.45, 0.5, n=5)
    assert held.anchor_state == "held"


def test_sideways_exit_still_releases(held, clock):
    frames(held, clock, 0.9, 0.45, 0.5)
    assert held.anchor_state == "free"


def test_height_is_adjustable(held, clock, settings):
    settings.anchor_hold_top = 0.8
    frames(held, clock, 0.5, 0.85, 0.4)             # head at 0.65: inside an 80% zone
    assert held.anchor_state == "held"


def test_hold_top_setting_validates_and_is_per_profile():
    v = autotrack.SETTINGS_SCHEMA["anchor_hold_top"]
    assert v(0.05) == 0.2 and v(1.5) == 1.0 and v(0.55) == 0.55
    assert "anchor_hold_top" in autotrack.Settings().to_dict()
    assert "anchor_hold_top" in autotrack.ProfileManager.ANCHOR_KEYS
