"""
GPU pose detection for Trackmind: RTMO (OpenMMLab, Apache-2.0) on ONNX Runtime
with DirectML, so it runs on any DirectX 12 GPU — NVIDIA, AMD or Intel — with
no CUDA install.

Why not MediaPipe on the GPU: MediaPipe's Python GPU support is Linux/macOS
only; on Windows it always runs on the CPU, where it competes with vMix and
the rest of the stream PC. RTMO also finds *every* person in the frame
(MediaPipe Pose returns one), which lets the subject lock pick the right one.

People come back as MediaPipe-style 33-landmark lists (COCO-17 keypoints
mapped onto MediaPipe indices) so the rest of the tracker is unchanged.
"""

import hashlib
import math
import os
import sys
import time
import types
import urllib.request
import zipfile

import numpy as np

MODEL_FILE = "rtmo-s-u8.onnx"      # RTMO-s with a uint8 NHWC input (see make_uint8_input)
MODEL_URL = ("https://download.openmmlab.com/mmpose/v1/projects/rtmo/onnx_sdk/"
             "rtmo-s_8xb32-600e_body7-640x640-dac2bf74_20231211.zip")
INPUT = 640
DET_THRESHOLD = 0.30         # person score to keep
DUP_IOU = 0.55               # boxes overlapping this much are the same person...
DUP_CENTER = 0.04            # ...or torso centres this close (frame fraction)

# COCO-17 (RTMO) → MediaPipe Pose landmark index
COCO_TO_MP = {0: 0, 1: 2, 2: 5, 3: 7, 4: 8, 5: 11, 6: 12, 7: 13, 8: 14,
              9: 15, 10: 16, 11: 23, 12: 24, 13: 25, 14: 26, 15: 27, 16: 28}


# ─────────────────────────────────────────────────────────────
# Model file
# ─────────────────────────────────────────────────────────────

def model_search_paths():
    paths = []
    if getattr(sys, "frozen", False):
        paths.append(os.path.join(sys._MEIPASS, "models", MODEL_FILE))
    here = os.path.dirname(os.path.abspath(__file__))
    paths.append(os.path.join(here, "models", MODEL_FILE))
    paths.append(os.path.join(os.path.expanduser("~/.trackmind"), "models", MODEL_FILE))
    return paths


def find_model():
    return next((p for p in model_search_paths() if os.path.isfile(p)), None)


def make_uint8_input(src_path, dst_path):
    """
    Give the model a uint8 NHWC input — exactly what OpenCV produces — with
    the float conversion and NHWC→NCHW transpose done inside the graph, on
    the GPU. Uploading uint8 is 4× less data than float32; on an RTX 2060
    this cut inference from ~41 ms to ~24 ms and CPU prep from ~8 ms to ~1 ms.
    Build-time only (needs the `onnx` package).
    """
    import onnx
    from onnx import TensorProto, helper
    m = onnx.load(src_path)
    g = m.graph
    old = g.input[0].name
    g.input.remove(g.input[0])
    g.input.insert(0, helper.make_tensor_value_info("image", TensorProto.UINT8, [1, INPUT, INPUT, 3]))
    for node in reversed([helper.make_node("Cast", ["image"], ["image_f"], to=TensorProto.FLOAT),
                          helper.make_node("Transpose", ["image_f"], [old], perm=[0, 3, 1, 2])]):
        g.node.insert(0, node)
    onnx.checker.check_model(m)
    onnx.save(m, dst_path)


def fetch_model(dest_dir, url=MODEL_URL, quiet=False):
    """Download RTMO-s from OpenMMLab and prepare dest_dir/rtmo-s-u8.onnx (build-time)."""
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, MODEL_FILE)
    if os.path.isfile(dest):
        return dest
    zpath = os.path.join(dest_dir, "rtmo-s.zip")
    raw = os.path.join(dest_dir, "rtmo-s.onnx")
    if not os.path.isfile(raw):
        if not quiet:
            print(f"[POSE] Downloading {url}")
        urllib.request.urlretrieve(url, zpath)
        with zipfile.ZipFile(zpath) as z:
            name = next(n for n in z.namelist() if n.endswith("end2end.onnx"))
            with z.open(name) as src, open(raw + ".part", "wb") as out:
                out.write(src.read())
        os.replace(raw + ".part", raw)
        os.remove(zpath)
    make_uint8_input(raw, dest + ".part")
    os.replace(dest + ".part", dest)
    if not quiet:
        h = hashlib.sha256(open(dest, "rb").read()).hexdigest()
        print(f"[POSE] Saved {dest} (sha256 {h[:16]}…)")
    return dest


# ─────────────────────────────────────────────────────────────
# GPU selection (DXGI adapter list, same order DirectML uses)
# ─────────────────────────────────────────────────────────────

def list_gpus():
    """[(device_id, name, dedicated_vram_bytes)] for hardware adapters; [] if unavailable."""
    if sys.platform != "win32":
        return []
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("a", wintypes.DWORD), ("b", wintypes.WORD), ("c", wintypes.WORD),
                        ("d", ctypes.c_ubyte * 8)]

        class LUID(ctypes.Structure):
            _fields_ = [("lo", wintypes.DWORD), ("hi", wintypes.LONG)]

        class DESC1(ctypes.Structure):
            _fields_ = [("Description", wintypes.WCHAR * 128), ("VendorId", wintypes.UINT),
                        ("DeviceId", wintypes.UINT), ("SubSysId", wintypes.UINT),
                        ("Revision", wintypes.UINT), ("DedicatedVideoMemory", ctypes.c_size_t),
                        ("DedicatedSystemMemory", ctypes.c_size_t),
                        ("SharedSystemMemory", ctypes.c_size_t), ("AdapterLuid", LUID),
                        ("Flags", wintypes.UINT)]

        iid = GUID(0x770aae78, 0xf26f, 0x4dba, (ctypes.c_ubyte * 8)(0xa8, 0x29, 0x25, 0x3c, 0x83, 0xd1, 0xb3, 0x87))
        factory = ctypes.c_void_p()
        if ctypes.windll.dxgi.CreateDXGIFactory1(ctypes.byref(iid), ctypes.byref(factory)) != 0:
            return []

        def method(obj, index, *argtypes):
            vtbl = ctypes.cast(obj, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
            return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtbl[index])

        out, i = [], 0
        enum = method(factory, 12, wintypes.UINT, ctypes.POINTER(ctypes.c_void_p))   # EnumAdapters1
        while True:
            adapter = ctypes.c_void_p()
            if enum(factory, i, ctypes.byref(adapter)) != 0:
                break
            desc = DESC1()
            method(adapter, 10, ctypes.POINTER(DESC1))(adapter, ctypes.byref(desc))     # GetDesc1
            method(adapter, 2)(adapter)                                                  # Release
            if not desc.Flags & 2:                                                       # skip software
                out.append((i, desc.Description, int(desc.DedicatedVideoMemory)))
            i += 1
        method(factory, 2)(factory)
        return out
    except Exception:
        return []


def best_gpu():
    """The adapter with the most dedicated VRAM (the discrete GPU over integrated)."""
    gpus = list_gpus()
    return max(gpus, key=lambda g: g[2]) if gpus else (0, "Default GPU", 0)


# ─────────────────────────────────────────────────────────────
# Pre/post-processing (pure numpy — unit-tested without a GPU)
# ─────────────────────────────────────────────────────────────

def letterbox(frame_bgr, size=INPUT, canvas=None):
    """
    RTMO input: aspect-preserving resize into a size×size BGR canvas padded
    with 114, as uint8 NHWC (1, size, size, 3). Pass `canvas` to reuse a buffer.
    """
    import cv2
    h, w = frame_bgr.shape[:2]
    r = min(size / h, size / w)
    nh, nw = int(round(h * r)), int(round(w * r))
    if canvas is None or canvas.shape != (size, size, 3):
        canvas = np.full((size, size, 3), 114, np.uint8)
    else:
        canvas[nh:, :] = 114
        canvas[:nh, nw:] = 114
    canvas[:nh, :nw] = cv2.resize(frame_bgr, (nw, nh), interpolation=cv2.INTER_LINEAR)
    return canvas[None], r


def _iou(a, b):
    ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
    inter = ix * iy
    union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
    return inter / union if union > 0 else 0.0


def _torso_center(k):
    pts = k[[5, 6, 11, 12]]
    good = pts[pts[:, 2] > 0.3]
    return (good[:, 0].mean(), good[:, 1].mean()) if len(good) >= 2 else None


def decode(dets, kps, ratio, width, height, threshold=DET_THRESHOLD):
    """
    Model outputs → people, most confident first, duplicates merged.
    dets: (N, 5) x1,y1,x2,y2,score in model pixels; kps: (N, 17, 3) x,y,score.
    Returns [{"box": (x0,y0,x1,y1) frame fractions, "score": s, "landmarks": [33]}].
    """
    people = []
    order = np.argsort(-dets[:, 4])
    for i in order:
        if dets[i, 4] < threshold:
            break
        box = dets[i, :4] / ratio
        k = kps[i].copy()
        k[:, :2] /= ratio
        box_n = (box[0] / width, box[1] / height, box[2] / width, box[3] / height)
        kn = k.copy()
        kn[:, 0] /= width
        kn[:, 1] /= height
        c = _torso_center(kn)
        dup = False
        for p in people:
            if _iou(box_n, p["box"]) > DUP_IOU:
                dup = True
            elif c and p["center"] and math.hypot(c[0] - p["center"][0], c[1] - p["center"][1]) < DUP_CENTER:
                dup = True
            if dup:
                break
        if dup:
            continue
        people.append({"box": box_n, "score": float(dets[i, 4]), "center": c,
                       "landmarks": to_landmarks(kn)})
    return people


def to_landmarks(kn):
    """COCO-17 normalized keypoints → MediaPipe-style list of 33 (x, y, visibility)."""
    lms = [types.SimpleNamespace(x=0.0, y=0.0, visibility=0.0) for _ in range(33)]
    for c, m in COCO_TO_MP.items():
        lms[m] = types.SimpleNamespace(x=float(kn[c, 0]), y=float(kn[c, 1]),
                                       visibility=float(kn[c, 2]))
    return lms


# ─────────────────────────────────────────────────────────────
# Runtime
# ─────────────────────────────────────────────────────────────

class GpuPose:
    """RTMO on ONNX Runtime + DirectML. Raises on construction if it can't run on a GPU."""

    def __init__(self, model_path=None, device_id=None):
        import onnxruntime as ort
        if "DmlExecutionProvider" not in ort.get_available_providers():
            raise RuntimeError("ONNX Runtime has no DirectML support (install onnxruntime-directml)")
        model_path = model_path or find_model()
        if not model_path:
            raise RuntimeError(f"Pose model {MODEL_FILE} not found")
        if device_id is None:
            device_id, self.device_name, _ = best_gpu()
        else:
            names = {g[0]: g[1] for g in list_gpus()}
            self.device_name = names.get(device_id, f"GPU {device_id}")
        self.device_id = device_id
        so = ort.SessionOptions()
        so.log_severity_level = 3
        so.enable_mem_pattern = False           # required by DirectML
        so.execution_mode = ort.ExecutionMode.ORT_SEQUENTIAL
        so.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
        self.session = ort.InferenceSession(
            model_path, so,
            providers=[("DmlExecutionProvider", {"device_id": device_id})])
        if self.session.get_providers()[0] != "DmlExecutionProvider":
            raise RuntimeError("DirectML provider did not load")
        inp = self.session.get_inputs()[0]
        if inp.type != "tensor(uint8)":
            raise RuntimeError(f"{os.path.basename(model_path)} isn't the uint8 build of the model")
        self.input = inp.name
        self.ms = None                          # inference time, EMA
        self._canvas = np.full((INPUT, INPUT, 3), 114, np.uint8)
        # Warm-up: DirectML compiles shaders on the first runs
        for _ in range(2):
            self.session.run(None, {self.input: self._canvas[None]})

    def infer(self, frame_bgr):
        h, w = frame_bgr.shape[:2]
        x, ratio = letterbox(frame_bgr, canvas=self._canvas)
        t = time.perf_counter()
        dets, kps = self.session.run(None, {self.input: x})
        ms = (time.perf_counter() - t) * 1000
        self.ms = ms if self.ms is None else 0.9 * self.ms + 0.1 * ms
        return decode(dets[0], kps[0], ratio, w, h)


if __name__ == "__main__":
    # python pose_gpu.py fetch   → download the model next to this file
    # python pose_gpu.py gpus    → list GPUs DirectML can use
    if len(sys.argv) > 1 and sys.argv[1] == "fetch":
        fetch_model(os.path.join(os.path.dirname(os.path.abspath(__file__)), "models"))
    else:
        for g in list_gpus():
            print(f"device {g[0]}: {g[1]} ({g[2] / 2**30:.1f} GB)")
        print("best:", best_gpu())
