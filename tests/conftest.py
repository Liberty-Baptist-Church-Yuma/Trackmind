"""
Test harness for Trackmind.

autotrack.py reads/writes ~/.trackmind on import, so HOME is pointed at a
throwaway directory first. cv2/mediapipe are stubbed when not installed — the
tests drive the detector with fake pose models and never decode video.
"""

import os
import sys
import tempfile
import types

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_HOME = tempfile.mkdtemp(prefix="trackmind-test-")
os.environ["USERPROFILE"] = _HOME
os.environ["HOME"] = _HOME
os.environ["USERNAME"] = "test"

try:
    import cv2  # noqa: F401
except ImportError:
    sys.modules["cv2"] = types.ModuleType("cv2")

try:
    import mediapipe  # noqa: F401
except ImportError:
    mp = types.ModuleType("mediapipe")
    mp.solutions = types.SimpleNamespace(pose=types.SimpleNamespace(Pose=None))
    sys.modules["mediapipe"] = mp

import pytest  # noqa: E402


class FakeClock:
    """Stands in for time.monotonic so tests control the passage of time."""
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock(monkeypatch):
    import autotrack
    c = FakeClock()
    monkeypatch.setattr(autotrack.time, "monotonic", c)
    return c


@pytest.fixture
def settings():
    """Live SETTINGS reset to a known profile (the user's current one), restored after."""
    import autotrack
    s = autotrack.SETTINGS
    saved = dict(vars(s))
    for k, v in vars(autotrack.Settings()).items():
        setattr(s, k, v)
    s.pan_dead, s.pan_slow, s.pan_fast = 0.14, 2, 10
    s.tilt_dead, s.tilt_slow, s.tilt_fast = 0.17, 2, 10
    s.motion_smooth, s.latency_comp, s.lost_timeout = 10, 0.3, 2.0
    s.zoom_enabled, s.anchor_enabled = False, False
    s.pose_engine = "cpu"        # tests drive fake pose models; never touch a real GPU
    yield s
    for k, v in saved.items():
        setattr(s, k, v)


class FakeVisca:
    """Records every command instead of talking to a camera."""
    def __init__(self):
        self.moves  = []     # (pan_vel, tilt_vel) as sent
        self.zooms  = []
        self.presets = []
        self.pan, self.tilt = 0, 0

    def move(self, pan_vel, tilt_vel):
        self.moves.append((pan_vel, tilt_vel))
        self.pan, self.tilt = pan_vel, tilt_vel
        return True

    def stop(self):
        return self.move(0, 0)

    def zoom_in(self, speed=1):  self.zooms.append(1);  return True
    def zoom_out(self, speed=1): self.zooms.append(-1); return True
    def zoom_stop(self):         self.zooms.append(0);  return True

    def recall_preset(self, p):
        self.presets.append(p)
        return True


@pytest.fixture
def visca():
    return FakeVisca()
