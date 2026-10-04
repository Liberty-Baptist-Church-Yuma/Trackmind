"""
GPU pose engine: RTMO output decoding, the multi-person subject choice, and
falling back to MediaPipe on the CPU. A fake GPU stands in for ONNX Runtime;
the last test runs the real model when it and a DirectML GPU are available.
"""

import types

import numpy as np
import pytest

import autotrack
import pose_gpu
from autotrack import PersonDetector


def _coco(cx, cy, h=0.5, score=0.9):
    """COCO-17 keypoints (normalized) for a standing person centred at cx, cy, height h."""
    k = np.zeros((17, 3), np.float32)
    top = cy - h / 2
    rel = {0: (0, 0.06), 1: (-0.01, 0.05), 2: (0.01, 0.05), 3: (-0.02, 0.06), 4: (0.02, 0.06),
           5: (-0.05, 0.19), 6: (0.05, 0.19), 7: (-0.07, 0.34), 8: (0.07, 0.34),
           9: (-0.08, 0.46), 10: (0.08, 0.46), 11: (-0.04, 0.50), 12: (0.04, 0.50),
           13: (-0.04, 0.75), 14: (0.04, 0.75), 15: (-0.04, 1.0), 16: (0.04, 1.0)}
    for i, (dx, fy) in rel.items():
        k[i] = (cx + dx, top + fy * h, score)
    return k


# ── Output decoding ────────────────────────────────────────────

def _raw(people, W=1280, H=720, ratio=0.5):
    """Model-pixel dets/kps for (cx, cy, h, score) people given in frame fractions."""
    dets, kps = [], []
    for cx, cy, h, s in people:
        k = _coco(cx, cy, h)
        k[:, 0] *= W * ratio
        k[:, 1] *= H * ratio
        dets.append([k[:, 0].min(), k[:, 1].min(), k[:, 0].max(), k[:, 1].max(), s])
        kps.append(k)
    return np.array(dets, np.float32), np.array(kps, np.float32)


def test_decode_scales_back_to_frame_fractions():
    dets, kps = _raw([(0.4, 0.5, 0.6, 0.9)])
    (p,) = pose_gpu.decode(dets, kps, 0.5, 1280, 720)
    assert p["center"][0] == pytest.approx(0.4, abs=0.01)
    assert 0 <= p["box"][0] < p["box"][2] <= 1


def test_decode_merges_duplicates_and_drops_low_scores():
    dets, kps = _raw([(0.40, 0.5, 0.6, 0.92), (0.405, 0.5, 0.62, 0.80),   # same person twice
                      (0.75, 0.5, 0.5, 0.70), (0.2, 0.5, 0.5, 0.10)])     # another + noise
    people = pose_gpu.decode(dets, kps, 0.5, 1280, 720)
    assert [round(p["center"][0], 2) for p in people] == [0.40, 0.75]
    assert people[0]["score"] == pytest.approx(0.92)


def test_landmarks_follow_mediapipe_indices():
    lms = pose_gpu.to_landmarks(_coco(0.5, 0.5))
    assert len(lms) == 33
    assert lms[11].x < lms[12].x                 # shoulders
    assert lms[23].visibility > 0.5              # hips
    assert lms[17].visibility == 0               # no COCO equivalent


def test_letterbox_is_uint8_nhwc_and_padded():
    cv2 = pytest.importorskip("cv2")
    if not hasattr(cv2, "resize"):
        pytest.skip("OpenCV stubbed")
    x, r = pose_gpu.letterbox(np.zeros((720, 1280, 3), np.uint8))
    assert x.shape == (1, 640, 640, 3) and x.dtype == np.uint8
    assert r == pytest.approx(0.5)
    assert x[0, 400, 10, 0] == 114 and x[0, 100, 10, 0] == 0


# ── Choosing the subject among several people ─────────────────

class FakeGpu:
    device_name = "Fake RTX"

    def __init__(self):
        self.people = []            # [(cx, cy, h, score)]
        self.ms = 20.0
        self.fail = False

    def infer(self, frame):
        if self.fail:
            raise RuntimeError("device removed")
        return [{"box": (cx - 0.1, cy - h / 2, cx + 0.1, cy + h / 2), "score": s,
                 "center": (cx, cy), "landmarks": pose_gpu.to_landmarks(_coco(cx, cy, h, 0.9))}
                for cx, cy, h, s in self.people]


@pytest.fixture
def gpu(settings):
    settings.track_focus, settings.track_offset = "upper", 0
    return FakeGpu()


FRAME = np.zeros((72, 128, 3), np.uint8)


def step(d, clock, lock=False, dt=0.04):
    clock.advance(dt)
    return d.detect(frame_bgr=FRAME, hard_lock=lock)


def test_uses_gpu_when_available(gpu, clock):
    d = PersonDetector(engine="auto", gpu_factory=lambda: gpu)
    assert d.engine["engine"] == "gpu" and "Fake RTX" in d.engine["device"]
    assert d.pose is None                        # MediaPipe never loaded


def test_unlocked_starts_on_the_most_prominent_person(gpu, clock):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    gpu.people = [(0.2, 0.5, 0.3, 0.9), (0.7, 0.5, 0.6, 0.9)]
    assert step(d, clock)[0] == pytest.approx(0.7, abs=0.02)


def test_unlocked_keeps_following_the_same_person(gpu, clock):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    gpu.people = [(0.5, 0.5, 0.4, 0.8)]
    step(d, clock)
    gpu.people = [(0.5, 0.5, 0.4, 0.8), (0.2, 0.5, 0.7, 0.99)]   # bigger person walks in
    for _ in range(5):
        assert step(d, clock)[0] == pytest.approx(0.5, abs=0.02)


def test_lock_picks_the_locked_person_among_several(gpu, clock):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    gpu.people = [(0.6, 0.5, 0.5, 0.9)]
    step(d, clock, lock=True)
    x = 0.6
    for _ in range(10):                          # walks left past someone standing at 0.45
        x -= 0.02
        gpu.people = [(0.45, 0.5, 0.3, 0.99), (x, 0.5, 0.5, 0.9)]
        assert step(d, clock, lock=True)[0] == pytest.approx(x, abs=0.02)


def test_lock_rejects_others_when_subject_leaves(gpu, clock):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    gpu.people = [(0.5, 0.5, 0.5, 0.9)]
    step(d, clock, lock=True)
    gpu.people = [(0.9, 0.5, 0.5, 0.95)]
    assert step(d, clock, lock=True) is None


def test_lock_reacquires_after_fast_exit(gpu, clock):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    x, y = 0.5, 0.55
    gpu.people = [(x, y, 0.5, 0.9)]
    step(d, clock, lock=True)
    for _ in range(8):
        x, y = x + 0.03, y - 0.015
        gpu.people = [(x, y, 0.5, 0.9)]
        step(d, clock, lock=True)
    gpu.people = []
    for _ in range(12):
        step(d, clock, lock=True)
    gpu.people = [(0.95, 0.3, 0.5, 0.9)]
    found = next((b for b in (step(d, clock, lock=True) for _ in range(40)) if b), None)
    assert found is not None and found[0] == pytest.approx(0.95, abs=0.02)


# ── Falling back to the CPU ────────────────────────────────────

class NoPose:
    def process(self, img):
        return types.SimpleNamespace(pose_landmarks=None)

    def close(self):
        pass


@pytest.fixture
def no_mediapipe(monkeypatch):
    monkeypatch.setattr(PersonDetector, "_make_pose", lambda self: NoPose())
    monkeypatch.setattr(autotrack.mp, "solutions", types.SimpleNamespace(pose=None), raising=False)


def test_falls_back_to_cpu_when_gpu_cannot_start(settings, no_mediapipe):
    def broken():
        raise RuntimeError("no DirectML")
    d = PersonDetector(engine="auto", gpu_factory=broken)
    assert d.engine["engine"] == "cpu" and "no DirectML" in d.engine["error"]
    assert d.pose is not None


def test_switches_to_cpu_if_gpu_fails_mid_service(gpu, clock, no_mediapipe):
    d = PersonDetector(engine="gpu", gpu_factory=lambda: gpu)
    gpu.people = [(0.5, 0.5, 0.5, 0.9)]
    assert step(d, clock) is not None
    gpu.fail = True
    assert step(d, clock) is None                # this frame goes to MediaPipe (sees nobody here)
    assert d.engine["engine"] == "cpu" and "device removed" in d.engine["error"]
    assert d.gpu is None


def test_cpu_engine_never_touches_the_gpu(settings, no_mediapipe):
    d = PersonDetector(engine="cpu", gpu_factory=lambda: pytest.fail("GPU requested"))
    assert d.engine["engine"] == "cpu"


def test_pose_engine_setting_validates():
    v = autotrack.SETTINGS_SCHEMA["pose_engine"]
    assert v("GPU") == "gpu" and v("cpu") == "cpu" and v("quantum") == "auto"


# ── The real thing, when available ────────────────────────────

def test_real_model_on_directml():
    ort = pytest.importorskip("onnxruntime")
    if "DmlExecutionProvider" not in ort.get_available_providers() or not pose_gpu.find_model():
        pytest.skip("needs onnxruntime-directml and models/rtmo-s-u8.onnx (python pose_gpu.py fetch)")
    g = pose_gpu.GpuPose()
    assert g.infer(np.zeros((720, 1280, 3), np.uint8)) == []
    assert g.ms is not None
