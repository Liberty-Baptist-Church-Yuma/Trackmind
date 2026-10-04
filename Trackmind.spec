# -*- mode: python ; coding: utf-8 -*-
import os
from PyInstaller.utils.hooks import collect_all

# ── Locate mediapipe and cv2 dynamically ──────────────────────
import mediapipe as _mp
import cv2 as _cv2

MP_ROOT  = os.path.dirname(_mp.__file__)
CV2_ROOT = os.path.dirname(_cv2.__file__)

datas = [
    ('logos/trackmind_icon.ico', '.'),
    ('version.txt', '.'),
    ('ui', 'ui'),   # the HTML interface (served locally, shown via WebView2)
    (os.path.join(MP_ROOT,  'modules'),          'mediapipe/modules'),
    (os.path.join(MP_ROOT,  'python/solutions'), 'mediapipe/python/solutions'),
    (os.path.join(CV2_ROOT, 'data'),             'cv2/data'),
]
binaries = []
hiddenimports = [
    'mediapipe', 'mediapipe.python', 'mediapipe.python.solutions',
    'mediapipe.python.solutions.pose',
    'cv2', 'numpy', 'webview',
]

tmp_ret = collect_all('mediapipe')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

tmp_ret = collect_all('cv2')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]

# GPU pose engine: ONNX Runtime + DirectML (DirectML.dll, provider DLLs) and
# the RTMO model. Optional at runtime — without them Trackmind uses MediaPipe.
try:
    tmp_ret = collect_all('onnxruntime')
    datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]
    hiddenimports += ['pose_gpu']
except Exception as e:
    print(f"WARNING: onnxruntime not bundled ({e}); the exe will be CPU-only")
if os.path.isfile('models/rtmo-s-u8.onnx'):
    datas += [('models/rtmo-s-u8.onnx', 'models'), ('models/LICENSE-RTMO.txt', 'models')]
else:
    print("WARNING: models/rtmo-s-u8.onnx missing; the exe will be CPU-only")

# pywebview + its WebView2 interop DLLs (Microsoft.Web.WebView2.*.dll, pythonnet)
tmp_ret = collect_all('webview')
datas += tmp_ret[0]; binaries += tmp_ret[1]; hiddenimports += tmp_ret[2]


a = Analysis(
    ['autotrack.py'],
    pathex=[],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='Trackmind',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=['logos/trackmind_icon.ico'],
)
