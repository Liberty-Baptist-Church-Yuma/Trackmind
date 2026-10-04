#!/usr/bin/env python3
"""
Trackmind: intelligent PTZ auto-tracking
-------------------------------------------------
Video source  : RTSP stream over camera LAN port
Camera control: VISCA over IP (TCP 5678)
Detection     : MediaPipe Pose
Interface     : HTML UI (ui/) in a native window via pywebview (WebView2),
                served by a local-only HTTP server that also hosts the
                Control API used by the Stream Deck plugin

Run: python autotrack.py            (app window)
     python autotrack.py --browser  (open the UI in a web browser)
"""

import socket
import time
import threading
import sys
import math
import os
import json
import secrets
import urllib.request
import urllib.error
import subprocess
import tempfile

try:
    import cv2
except ImportError:
    sys.exit("ERROR: pip install opencv-python")

try:
    import mediapipe as mp
except ImportError:
    sys.exit("ERROR: pip install mediapipe")

import numpy as np

from diagnostics import DiagnosticsLog, NULL_DIAG
from autopilot import AutoPilot, EgoMotion, STYLES as AUTO_STYLES


def _read_version():
    if getattr(sys, 'frozen', False):
        bases = [sys._MEIPASS]
    else:
        bases = [os.path.dirname(os.path.abspath(__file__)), os.getcwd()]
    for base in bases:
        try:
            with open(os.path.join(base, "version.txt")) as f:
                ver = f.read().strip().lstrip("v")
                if ver:
                    return ver
        except Exception:
            pass
    return "unknown"

VERSION = _read_version()
GITHUB_REPO = "coder747-8i/Trackmind"  # ← update this

# ─────────────────────────────────────────────────────────────
# Shared live settings (read by tracker thread, written by UI)
# ─────────────────────────────────────────────────────────────

class Settings:
    def __init__(self):
        self.camera_ip    = ""
        self.rtsp_user    = ""
        self.rtsp_pass    = ""
        self.rtsp_stream  = "2"
        self.visca_port   = 5678
        self.home_preset  = 0

        self.pan_dead     = 0.17
        self.pan_near     = 0.32
        self.pan_slow     = 2
        self.pan_fast     = 5
        self.tilt_dead    = 0.17
        self.tilt_near    = 0.32
        self.tilt_slow    = 2
        self.tilt_fast    = 5

        self.zoom_enabled = False
        self.zoom_target  = 0.45
        self.zoom_dead    = 0.20
        self.zoom_speed   = 1

        self.motion_sync  = False   # PTZOptics: sync pan/tilt/zoom to arrive together
        self.motion_smooth = 5      # 0=off (snap), 1..10 = gentler accel/decel ramps

        self.latency_comp = 0.4
        self.lost_timeout = 2.0

        # Auto mode (autopilot.py): picks speeds, dead zones and smoothing by
        # itself from what it learns about the camera and the situation.
        # The auto_gain/latency values are learned live and remembered for the
        # next start; they belong to the camera, not to a profile.
        self.auto_mode      = False
        self.auto_style     = "balanced"   # calm | balanced | responsive
        self.auto_gain_pan  = 0.10
        self.auto_gain_tilt = 0.10
        self.auto_latency   = 0.35

        self.track_focus  = 'upper'
        self.track_offset = 2

        # Pulpit anchor: when the camera settles near a fixed spot (the pulpit),
        # snap to that spot's preset and hold the shot until the speaker walks
        # away. Stored per profile, so it can be on for "Sunday AM" and off
        # everywhere else. anchor_pan/tilt are the camera's own absolute
        # position for the preset, learned with the "Learn pulpit" button.
        self.anchor_enabled = False
        self.anchor_preset  = 5
        self.anchor_range   = 4      # 1..10 — how near the camera must settle
        self.anchor_dwell   = 1.0    # s the camera must sit still before snapping
        self.anchor_hold    = 0.25   # half-width of the hold zone (frame fraction)
        self.anchor_hold_top = 0.50  # hold zone height, from the top of the frame
        self.anchor_mode    = "recall"   # "recall" the preset, or "glide" there slowly
        self.anchor_glide   = 6      # glide pan/tilt speed, 1..24
        self.anchor_pan     = None
        self.anchor_tilt    = None

        # Pose engine: "auto" = GPU (RTMO via DirectML, pose_gpu.py) when one
        # is usable, else CPU (MediaPipe); "gpu" / "cpu" force one. App-wide.
        self.pose_engine    = "auto"

        # Local control API (used by the Stream Deck plugin). App-wide, not
        # per-profile — loading a profile never changes these.
        self.api_enabled  = True
        self.api_port     = 8742

    # ── Persistence ─────────────────────────────────────────

    @staticmethod
    def _system_user():
        return (os.environ.get('USERNAME') or os.environ.get('USER') or 'default').lower()

    @staticmethod
    def _config_dir():
        base = os.path.expanduser("~/.trackmind")
        user_dir = os.path.join(base, Settings._system_user())
        os.makedirs(user_dir, exist_ok=True)
        return user_dir

    @staticmethod
    def _config_path():
        return os.path.join(Settings._config_dir(), "config.json")

    @staticmethod
    def is_first_run():
        return not os.path.exists(Settings._config_path())

    def save(self):
        data = {
            "camera_ip":    self.camera_ip,
            "rtsp_user":    self.rtsp_user,
            "rtsp_pass":    self.rtsp_pass,
            "rtsp_stream":  self.rtsp_stream,
            "home_preset":  self.home_preset,
            "pan_dead":     self.pan_dead,
            "pan_slow":     self.pan_slow,
            "pan_fast":     self.pan_fast,
            "tilt_dead":    self.tilt_dead,
            "tilt_slow":    self.tilt_slow,
            "tilt_fast":    self.tilt_fast,
            "zoom_enabled": self.zoom_enabled,
            "zoom_target":  self.zoom_target,
            "zoom_dead":    self.zoom_dead,
            "zoom_speed":   self.zoom_speed,
            "motion_sync":  self.motion_sync,
            "motion_smooth": self.motion_smooth,
            "latency_comp": self.latency_comp,
            "lost_timeout": self.lost_timeout,
            "track_offset": self.track_offset,
            "auto_mode":      self.auto_mode,
            "auto_style":     self.auto_style,
            "auto_gain_pan":  self.auto_gain_pan,
            "auto_gain_tilt": self.auto_gain_tilt,
            "auto_latency":   self.auto_latency,
            "pose_engine":    self.pose_engine,
            "anchor_enabled": self.anchor_enabled,
            "anchor_preset":  self.anchor_preset,
            "anchor_range":   self.anchor_range,
            "anchor_dwell":   self.anchor_dwell,
            "anchor_hold":    self.anchor_hold,
            "anchor_hold_top": self.anchor_hold_top,
            "anchor_mode":    self.anchor_mode,
            "anchor_glide":   self.anchor_glide,
            "anchor_pan":     self.anchor_pan,
            "anchor_tilt":    self.anchor_tilt,
            "api_enabled":  self.api_enabled,
            "api_port":     self.api_port,
        }
        try:
            with open(self._config_path(), "w") as f:
                json.dump(data, f, indent=2)
            print(f"[CFG] Settings saved to {self._config_path()}")
        except Exception as e:
            print(f"[CFG] Could not save settings: {e}")

    def load(self):
        path = self._config_path()
        if not os.path.exists(path):
            return
        try:
            with open(path, "r") as f:
                data = json.load(f)
            for key, val in data.items():
                if hasattr(self, key):
                    setattr(self, key, val)
            # Recompute derived values
            self.pan_near  = self.pan_dead  + 0.15
            self.tilt_near = self.tilt_dead + 0.15
            print(f"[CFG] Settings loaded from {path}")
        except Exception as e:
            print(f"[CFG] Could not load settings: {e}")

    def to_dict(self):
        return {
            "camera_ip": self.camera_ip, "rtsp_user": self.rtsp_user,
            "rtsp_pass": self.rtsp_pass, "rtsp_stream": self.rtsp_stream,
            "home_preset": self.home_preset,
            "pan_dead": self.pan_dead, "pan_slow": self.pan_slow, "pan_fast": self.pan_fast,
            "tilt_dead": self.tilt_dead, "tilt_slow": self.tilt_slow, "tilt_fast": self.tilt_fast,
            "zoom_enabled": self.zoom_enabled, "zoom_target": self.zoom_target,
            "zoom_dead": self.zoom_dead, "zoom_speed": self.zoom_speed,
            "motion_sync": self.motion_sync, "motion_smooth": self.motion_smooth,
            "latency_comp": self.latency_comp, "lost_timeout": self.lost_timeout,
            "track_offset": self.track_offset,
            "auto_mode": self.auto_mode, "auto_style": self.auto_style,
            "anchor_enabled": self.anchor_enabled, "anchor_preset": self.anchor_preset,
            "anchor_range": self.anchor_range, "anchor_dwell": self.anchor_dwell,
            "anchor_hold": self.anchor_hold, "anchor_hold_top": self.anchor_hold_top,
            "anchor_mode": self.anchor_mode, "anchor_glide": self.anchor_glide,
            "anchor_pan": self.anchor_pan, "anchor_tilt": self.anchor_tilt,
        }


SETTINGS = Settings()
SETTINGS.load()   # Load saved settings on startup


# ─────────────────────────────────────────────────────────────
# User Profiles
# ─────────────────────────────────────────────────────────────

class ProfileManager:
    def __init__(self):
        self.profiles      = {}   # name -> settings dict
        self.current       = None
        self._load()

    @staticmethod
    def _path():
        return os.path.join(Settings._config_dir(), "profiles.json")

    def _settings_dict(self):
        return SETTINGS.to_dict()

    def list_profiles(self):
        return list(self.profiles.keys())

    def save_profile(self, name):
        self.profiles[name] = self._settings_dict()
        self.current = name
        self._persist()
        print(f"[PROFILE] Saved profile: {name}")

    def load_profile(self, name):
        if name not in self.profiles:
            return False
        # Settings a profile predates (e.g. the pulpit anchor on a profile saved
        # before v1.8) fall back to defaults rather than leaking in from
        # whichever profile was loaded before.
        data = {**Settings().to_dict(), **self.profiles[name]}
        for key, val in data.items():
            if hasattr(SETTINGS, key):
                setattr(SETTINGS, key, val)
        SETTINGS.pan_near  = SETTINGS.pan_dead  + 0.15
        SETTINGS.tilt_near = SETTINGS.tilt_dead + 0.15
        self.current = name
        print(f"[PROFILE] Loaded profile: {name}")
        return True

    ANCHOR_KEYS = ("anchor_enabled", "anchor_preset", "anchor_range", "anchor_dwell",
                   "anchor_hold", "anchor_hold_top", "anchor_mode", "anchor_glide",
                   "anchor_pan", "anchor_tilt")

    def sync_anchor(self):
        """
        Copy the live pulpit-anchor settings into the active profile. Each
        profile keeps its own anchor (Sunday AM's pulpit ≠ Wednesday's), and
        learning one shouldn't need a separate "save profile" step.
        """
        if self.current not in self.profiles:
            return
        prof = self.profiles[self.current]
        for key in self.ANCHOR_KEYS:
            prof[key] = getattr(SETTINGS, key)
        self._persist()

    def delete_profile(self, name):
        if name in self.profiles:
            del self.profiles[name]
            if self.current == name:
                self.current = None
            self._persist()

    def _persist(self):
        try:
            data = {"current": self.current, "profiles": self.profiles}
            with open(self._path(), "w") as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"[PROFILE] Save failed: {e}")

    def _load(self):
        p = self._path()
        if not os.path.exists(p):
            return
        try:
            with open(p, "r") as f:
                data = json.load(f)
            self.profiles = data.get("profiles", {})
            self.current  = data.get("current", None)
            print(f"[PROFILE] Loaded {len(self.profiles)} profiles")
        except Exception as e:
            print(f"[PROFILE] Load failed: {e}")


PROFILE_MANAGER = ProfileManager()

# Flight recorder for tracking behaviour (see diagnostics.py). Day files under
# ~/.trackmind/<user>/diagnostics, kept for 30 days.
DIAG = DiagnosticsLog(os.path.join(Settings._config_dir(), "diagnostics"))


# ─────────────────────────────────────────────────────────────
# Auto-Updater
# ─────────────────────────────────────────────────────────────

class Updater:
    """
    Checks GitHub releases for a newer version and installs it.

    UI-agnostic: progress lives in `self.state` (a plain dict the web UI
    renders), and `on_exit` is called once the installer has been launched so
    the app can close and let it overwrite the running .exe.

    Phases: idle → checking → uptodate | available | error
            available → downloading → installing (→ app exits)
    """

    RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"

    # ShellExecute return values <= 32 are error codes. Map the ones we
    # actually hit to plain-English causes so a failed update tells the user
    # what went wrong instead of just vanishing.
    _SHELLEXEC_ERRORS = {
        0:  "The system is out of memory or resources.",
        2:  "The installer file could not be found.",
        3:  "The installer path could not be found.",
        5:  "Windows blocked the installer, or you clicked \"No\" on the "
            "User Account Control (admin) prompt. Click Yes when it appears.",
        8:  "Not enough memory to start the installer.",
        26: "A sharing violation occurred (the file may be locked by "
            "antivirus). Try again, or install manually.",
        31: "No application is associated with the installer file.",
    }

    def __init__(self, on_exit):
        self._on_exit = on_exit
        self._lock    = threading.Lock()
        self.state    = {
            "phase":      "idle",
            "manual":     False,
            "current":    VERSION,
            "latest":     None,
            "notes":      None,
            "has_installer": False,
            "progress":   None,
            "error":      None,
            "last_check": None,
            "releases_url": self.RELEASES_URL,
        }
        self._install_url = None

    def snapshot(self):
        with self._lock:
            return dict(self.state)

    def _set(self, **kw):
        with self._lock:
            self.state.update(kw)

    def dismiss(self):
        """UI closed the update prompt — back to idle, keep what we learned."""
        if self.state["phase"] in ("uptodate", "available", "error"):
            self._set(phase="idle", error=None)

    def check_async(self, manual=False):
        if self.state["phase"] in ("checking", "downloading", "installing"):
            return
        self._set(phase="checking", manual=manual, error=None)
        threading.Thread(target=self._check, args=(manual,), daemon=True).start()

    @staticmethod
    def _ver_tuple(v):
        try:
            return tuple(int(x) for x in v.split("."))
        except Exception:
            return (0,)

    def _check(self, manual):
        try:
            url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
            req = urllib.request.Request(url, headers={"User-Agent": "TrackMind"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                data = json.loads(resp.read().decode())

            tag = data.get("tag_name", "").lstrip("v")
            self._set(last_check=time.time())
            if not tag or self._ver_tuple(tag) <= self._ver_tuple(VERSION):
                print(f"[UPDATE] Up to date (v{VERSION})")
                # Silent launch checks don't bother the user when up to date.
                self._set(phase="uptodate" if manual else "idle", latest=tag or VERSION)
                return

            install_url = None
            for asset in data.get("assets", []):
                aname = asset.get("name", "").lower()
                if aname.endswith(".exe") and "setup" in aname:
                    install_url = asset.get("browser_download_url")
                    break
            self._install_url = install_url
            print(f"[UPDATE] New version available: v{tag}")
            self._set(phase="available", latest=tag,
                      notes=(data.get("body") or "").strip()[:4000] or None,
                      has_installer=bool(install_url))
        except Exception as e:
            print(f"[UPDATE] Check failed: {e}")
            self._set(last_check=time.time())
            self._set(phase="error" if manual else "idle", error=str(e))

    def install_async(self):
        if self.state["phase"] != "available" or not self._install_url:
            return False
        self._set(phase="downloading", progress=0, error=None)
        threading.Thread(target=self._download_and_install,
                         args=(self._install_url,), daemon=True).start()
        return True

    def _strip_mark_of_the_web(self, path):
        """
        Remove the 'downloaded from the internet' tag (the Zone.Identifier
        NTFS alternate data stream). Without this, Windows SmartScreen can
        silently block an unsigned installer launched non-interactively —
        which looks exactly like 'the installer flashed and nothing happened'.
        Harmless if the stream isn't present.
        """
        try:
            os.remove(path + ":Zone.Identifier")
        except OSError:
            pass

    def _download_and_install(self, url):
        try:
            tmp = tempfile.NamedTemporaryFile(suffix="_Trackmind_Setup.exe", delete=False)
            tmp_path = tmp.name
            tmp.close()

            def reporthook(count, block_size, total_size):
                if total_size > 0:
                    self._set(progress=min(100, int(count * block_size * 100 / total_size)))

            _, headers = urllib.request.urlretrieve(url, tmp_path, reporthook)

            # Verify the download actually completed — a truncated or empty
            # file would otherwise launch and silently do nothing.
            actual = os.path.getsize(tmp_path)
            expected = headers.get("Content-Length")
            if actual < 1_000_000 or (expected and actual < int(expected)):
                raise OSError(
                    f"The download was incomplete ({actual} bytes"
                    + (f" of {expected}" if expected else "")
                    + "). Check your connection and try again."
                )

            # Clear the Mark-of-the-Web so SmartScreen doesn't silently block
            # the silent/elevated launch below.
            self._strip_mark_of_the_web(tmp_path)
            self._set(phase="installing", progress=100)

            # Launch the installer elevated and silent. It will close this
            # running instance (so the locked .exe can be overwritten) and
            # relaunch the updated app when it finishes. We then close
            # ourselves promptly so the file unlocks cleanly — the installer
            # also force-closes us as a fallback before copying.
            import ctypes
            # Own the UAC prompt with our window so it comes up in front of
            # Trackmind instead of flashing unseen in the taskbar.
            owner = ctypes.windll.user32.GetForegroundWindow() or None
            ret = ctypes.windll.shell32.ShellExecuteW(owner, "runas", tmp_path, "/S", None, 1)
            if ret <= 32:
                reason = self._SHELLEXEC_ERRORS.get(
                    int(ret), f"The installer could not be started (code {ret}).")
                raise OSError(reason)
            print("[UPDATE] Installer started — closing so it can replace Trackmind.exe")
            time.sleep(0.8)
            self._on_exit()
            # The installer waits for us to exit before force-closing us. Make
            # sure we really do go, even if a stuck thread would keep the
            # process alive after the window closes.
            guard = threading.Timer(5.0, lambda: os._exit(0))
            guard.daemon = True   # a normal exit mustn't wait for it
            guard.start()

        except Exception as e:
            print(f"[UPDATE] Install failed: {e}")
            self._set(phase="error", error=f"The update could not be installed automatically. {e}")


# ─────────────────────────────────────────────────────────────
# VISCA Controller
# ─────────────────────────────────────────────────────────────

class VISCAController:
    def __init__(self):
        self._sock       = None
        self._lock       = threading.Lock()
        self._connected  = False

    @property
    def ip(self):
        return SETTINGS.camera_ip

    @property
    def port(self):
        return SETTINGS.visca_port

    def connect(self) -> bool:
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            s.settimeout(2.0)
            s.connect((self.ip, self.port))
            self._sock      = s
            self._connected = True
            print(f"[VISCA] Connected to {self.ip}:{self.port}")
            return True
        except Exception as e:
            print(f"[VISCA] Connection failed: {e}")
            self._connected = False
            return False

    def disconnect(self):
        if self._sock:
            try:
                self._sock.close()
            except Exception:
                pass
        self._connected = False

    def _send(self, cmd: bytes) -> bool:
        with self._lock:
            for attempt in range(2):
                if not self._connected:
                    if not self.connect():
                        return False
                try:
                    self._sock.sendall(cmd)
                    return True
                except Exception as e:
                    print(f"[VISCA] Send error: {e}")
                    self._connected = False
                    try:
                        self._sock.close()
                    except Exception:
                        pass
        return False

    def pan_tilt(self, pan_speed, tilt_speed, pan_dir, tilt_dir):
        ps  = max(1, min(24, abs(pan_speed)))
        ts  = max(1, min(24, abs(tilt_speed)))
        cmd = bytes([0x81, 0x01, 0x06, 0x01, ps, ts, pan_dir, tilt_dir, 0xFF])
        return self._send(cmd)

    def stop(self):
        return self.pan_tilt(1, 1, 3, 3)

    def move(self, pan_vel: int, tilt_vel: int):
        pd = 1 if pan_vel > 0 else (2 if pan_vel < 0 else 3)
        td = 1 if tilt_vel > 0 else (2 if tilt_vel < 0 else 3)
        ps = abs(pan_vel) if pan_vel != 0 else 1
        ts = abs(tilt_vel) if tilt_vel != 0 else 1
        return self.pan_tilt(ps, ts, pd, td)

    def zoom_in(self, speed=1):
        s = max(0, min(7, speed))
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x20 | s, 0xFF]))

    def zoom_out(self, speed=1):
        s = max(0, min(7, speed))
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x30 | s, 0xFF]))

    def zoom_stop(self):
        return self._send(bytes([0x81, 0x01, 0x04, 0x07, 0x00, 0xFF]))

    def recall_preset(self, preset: int):
        p = max(0, min(89, preset))
        return self._send(bytes([0x81, 0x01, 0x04, 0x3F, 0x02, p, 0xFF]))

    def home(self):
        return self._send(bytes([0x81, 0x01, 0x06, 0x04, 0xFF]))

    def absolute_move(self, pan: int, tilt: int, pan_speed: int, tilt_speed: int):
        """
        Drive to an absolute pan/tilt position at the given speeds — a glide,
        unlike a preset recall, which moves at the camera's preset speed and
        also changes zoom. VISCA: 8x 01 06 02 VV WW 0Y 0Y 0Y 0Y 0Z 0Z 0Z 0Z FF
        """
        nib = lambda v: [((v & 0xFFFF) >> sh) & 0xF for sh in (12, 8, 4, 0)]
        vv = max(1, min(24, pan_speed))
        ww = max(1, min(20, tilt_speed))
        return self._send(bytes([0x81, 0x01, 0x06, 0x02, vv, ww, *nib(pan), *nib(tilt), 0xFF]))

    def query_pan_tilt(self, timeout=0.3):
        """
        Absolute pan/tilt position, or None if the camera didn't answer.
        VISCA Pan-tiltPosInq: 8x 09 06 12 FF → y0 50 0p 0p 0p 0p 0t 0t 0t 0t FF
        (signed 16-bit, one nibble per byte). Also drains the ACK/completion
        replies that ordinary commands leave queued on the socket.
        """
        with self._lock:
            if not self._connected and not self.connect():
                return None
            s = self._sock
            try:
                s.setblocking(False)
                try:
                    while True:
                        if not s.recv(4096):
                            raise ConnectionError("camera closed the VISCA socket")
                except (BlockingIOError, InterruptedError):
                    pass
                s.settimeout(timeout)
                s.sendall(bytes([0x81, 0x09, 0x06, 0x12, 0xFF]))
                buf = b""
                deadline = time.monotonic() + timeout
                while time.monotonic() < deadline:
                    s.settimeout(max(0.01, deadline - time.monotonic()))
                    chunk = s.recv(256)
                    if not chunk:
                        raise ConnectionError("camera closed the VISCA socket")
                    buf += chunk
                    while b"\xff" in buf:
                        msg, buf = buf.split(b"\xff", 1)
                        if len(msg) == 10 and msg[1] == 0x50:
                            n = lambda b: ((b[0] & 0xF) << 12) | ((b[1] & 0xF) << 8) | ((b[2] & 0xF) << 4) | (b[3] & 0xF)
                            signed = lambda v: v - 0x10000 if v & 0x8000 else v
                            return signed(n(msg[2:6])), signed(n(msg[6:10]))
                return None
            except socket.timeout:
                return None
            except OSError as e:
                print(f"[VISCA] Position query failed: {e}")
                self._connected = False
                try: s.close()
                except Exception: pass
                return None
            finally:
                try:
                    if self._connected: s.settimeout(2.0)
                except Exception:
                    pass

    def set_motion_sync(self, on: bool):
        """
        PTZOptics Motion Sync: when enabled, the camera scales each axis's
        speed so pan, tilt, and zoom all reach a recalled preset at the same
        moment — producing smooth, coordinated motion instead of one axis
        finishing early. Camera-side setting; persists until changed.
        VISCA: 8x 0A 11 13 02 FF (on) / 03 FF (off)
        """
        payload = 0x02 if on else 0x03
        ok = self._send(bytes([0x81, 0x0A, 0x11, 0x13, payload, 0xFF]))
        if ok:
            print(f"[VISCA] Motion Sync {'ON' if on else 'OFF'}")
        return ok


# ─────────────────────────────────────────────────────────────
# Zone speed helper
# ─────────────────────────────────────────────────────────────

def zone_speed(pos, dead, near, slow, fast) -> int:
    err = pos - 0.5
    mag = abs(err)
    if mag < dead:
        return 0
    speed = slow if mag < near else fast
    return speed if err > 0 else -speed


SETTLE_FRAC = 0.35   # a move started at the dead-zone edge carries on until the
                     # subject is this fraction of the dead zone from centre


def smooth_speed(pos, dead, slow, fast, active=0):
    """
    Continuous proportional speed (float) with hysteresis.

    Returns (speed, active). `active` is the direction the axis is currently
    re-centring in (+1/-1) or 0 when idle; pass the previous value back in.

    Outside the dead zone the speed eases from `slow` up to `fast` (frame
    edge) along a smoothstep curve. Once a move has started it does NOT stop
    at the dead-zone edge — it keeps going, easing down to a crawl, until the
    subject is close to centre. Stopping right at the edge left the subject
    parked on the boundary, where every small step restarted the camera:
    the constant stop-start "hunting" look. A move ends early if the subject
    crosses centre, so an overshoot never turns into a reversal.
    """
    err  = pos - 0.5
    mag  = abs(err)
    side = 1 if err > 0 else -1
    settle = dead * SETTLE_FRAC
    if mag >= dead:
        span  = max(1e-6, 0.5 - dead)
        norm  = min(1.0, (mag - dead) / span)
        eased = norm * norm * (3.0 - 2.0 * norm)   # smoothstep, 0..1
        speed = slow + (fast - slow) * eased
        active = side
    elif active == side and mag > settle:
        # Coming home: ease from `slow` at the edge down to the minimum speed
        frac  = (mag - settle) / max(1e-6, dead - settle)
        speed = 1.0 + (max(1.0, slow) - 1.0) * frac
    else:
        return 0.0, 0
    return speed * side, active


# ─────────────────────────────────────────────────────────────
# Person Detector (MediaPipe Pose + lock-on)
# ─────────────────────────────────────────────────────────────

class PersonDetector:
    LOCK_DIST     = 0.25   # frame fraction a locked subject may move between detections
    LOCK_GROW     = 0.30   # search radius growth per second while the subject is unseen
    LOCK_MAX_DIST = 0.75   # ≈ whole frame: a lock never gets stuck searching one spot
    LOCK_CROP     = 0.32   # min half-width of the region searched while locked
    LOCK_PREDICT  = 1.0    # s — max time the subject's last velocity is extrapolated

    FOLLOW_STICK  = 0.15   # unlocked (GPU): keep following whoever is this close to the last spot
    SIZE_WEIGHT   = 0.15   # locked (GPU): how much a size mismatch counts against a candidate

    def __init__(self, diag=None, engine=None, gpu_factory=None):
        self.diag        = diag or NULL_DIAG
        self.pose = self.pose_locked = None       # MediaPipe, created when needed
        self.gpu         = None
        want = engine or SETTINGS.pose_engine
        self.engine = {"engine": "cpu", "device": "CPU · MediaPipe", "ms": None,
                       "requested": want, "error": None}
        if want in ("auto", "gpu"):
            try:
                self.gpu = (gpu_factory or _make_gpu_pose)()
                self.engine.update(engine="gpu", device=f"GPU · {self.gpu.device_name}")
                print(f"[POSE] Using the GPU: {self.gpu.device_name} (RTMO, DirectML)")
            except Exception as e:
                self.engine["error"] = f"GPU unavailable: {e}"
                print(f"[POSE] GPU unavailable ({e}) — using MediaPipe on the CPU")
                self.diag.event("engine", ev="gpu_unavailable", error=str(e)[:300])
        if self.gpu is None:
            self._ensure_cpu()
        self._locked_cx  = None
        self._locked_cy  = None
        self._locked_h   = None
        self._lock_seen  = 0.0
        self._lock_vx    = 0.0    # subject velocity in frame fractions / s,
        self._lock_vy    = 0.0    # so a fast exit is searched where it went

    def _ensure_cpu(self):
        if self.pose is None:
            self.mp_pose     = mp.solutions.pose
            self.pose        = self._make_pose()
            # Locked mode runs on a crop around the subject. A separate instance
            # keeps MediaPipe's frame-to-frame tracking state consistent with the
            # crop, instead of mixing crops and full frames in one tracker.
            self.pose_locked = self._make_pose()

    def _gpu_failed(self, err):
        """The GPU stopped working mid-service (driver reset, device lost): carry on on the CPU."""
        print(f"[POSE] GPU inference failed ({err}) — switching to the CPU")
        self.diag.event("engine", ev="gpu_failed", error=str(err)[:300])
        self.gpu = None
        self.engine.update(engine="cpu", device="CPU · MediaPipe", ms=None,
                           error=f"GPU failed, switched to CPU: {err}")
        self._ensure_cpu()

    def _lock_update(self, cx, cy, now, h=None):
        dt = now - self._lock_seen
        if self._locked_cx is not None and 0.0 < dt < 0.5:
            a = 0.3
            self._lock_vx = (1 - a) * self._lock_vx + a * (cx - self._locked_cx) / dt
            self._lock_vy = (1 - a) * self._lock_vy + a * (cy - self._locked_cy) / dt
        elif self._locked_cx is None:
            self._lock_vx = self._lock_vy = 0.0
        self._locked_cx, self._locked_cy, self._lock_seen = cx, cy, now
        if h is not None:
            self._locked_h = h if self._locked_h is None else 0.8 * self._locked_h + 0.2 * h

    def _make_pose(self):
        return self.mp_pose.Pose(
            static_image_mode=False,
            model_complexity=1,
            smooth_landmarks=True,
            min_detection_confidence=0.55,
            min_tracking_confidence=0.5,
        )

    # Landmark sets. Elbows/wrists are left out on purpose: a speaker's
    # gestures would drag the aim point (and the zoom fill) around with them.
    UPPER_LANDMARKS = [0, 11, 12, 23, 24]          # nose, shoulders, hips
    FULL_LANDMARKS  = [0, 11, 12, 23, 24, 25, 26]  # upper + knees
    LOWER_LANDMARKS = [23, 24, 25, 26, 27, 28]     # hips, knees, ankles

    def _landmarks_to_bbox(self, lms):
        focus  = SETTINGS.track_focus
        offset = SETTINGS.track_offset  # -5 to +5, negative = higher, positive = lower

        if focus == 'lower':
            indices = self.LOWER_LANDMARKS
        elif focus == 'upper':
            indices = self.UPPER_LANDMARKS
        else:  # full
            indices = self.FULL_LANDMARKS

        visible = [lms[i] for i in indices if lms[i].visibility > 0.4]
        if len(visible) < 2:
            visible = [lms[i] for i in self.UPPER_LANDMARKS if lms[i].visibility > 0.4]
        if len(visible) < 3:
            return None

        xs = [l.x for l in visible]
        ys = [l.y for l in visible]
        x0, x1 = min(xs), max(xs)
        y0, y1 = min(ys), max(ys)
        pad_x = (x1 - x0) * 0.15
        pad_y = (y1 - y0) * 0.15
        x0 = max(0.0, x0 - pad_x)
        x1 = min(1.0, x1 + pad_x)
        y0 = max(0.0, y0 - pad_y)
        y1 = min(1.0, y1 + pad_y)

        # Horizontal aim follows the torso centre line (shoulder/hip
        # midpoints) when visible — steadier than the bbox middle, which
        # shifts whenever a landmark flickers in or out of visibility.
        torso = [lms[i] for i in (11, 12, 23, 24) if lms[i].visibility > 0.4]
        cx = (sum(l.x for l in torso) / len(torso)) if len(torso) >= 2 else (x0 + x1) / 2
        cy = (y0 + y1) / 2

        # Base vertical adjustment per mode
        h = y1 - y0
        if focus == 'upper':
            cy -= h * 0.15   # shift up toward chest
        # full and lower stay centered naturally

        # Apply manual offset: each step = 5% of bbox height
        # negative offset = move aim point up (camera tilts up)
        # positive offset = move aim point down (camera tilts down)
        cy += (offset / 7.0) * h * 0.7
        cy = max(0.0, min(1.0, cy))

        return cx, cy, x1 - x0, y1 - y0

    def detect(self, frame_rgb=None, hard_lock=False, frame_bgr=None):
        """
        hard_lock=False (default): follows most prominent person,
                                   updates lock position each frame freely.
        hard_lock=True:  strictly ignores anyone too far from locked position.

        Pass the frame as RGB or BGR (BGR avoids a conversion on the GPU path).
        On the GPU every person in the frame is detected and _choose picks
        one; the MediaPipe (CPU) path below sees only one person per image.

        MediaPipe Pose only ever returns ONE person — whoever it judges most
        prominent. So a lock can't just filter full-frame results: once a
        second person steals the detection, the locked subject is invisible
        until they leave. Instead, while locked, pose runs on a crop around
        the subject's last position, so other people are mostly outside the
        image the model sees.
        """
        now = time.monotonic()
        if self.gpu is not None:
            bgr = frame_bgr if frame_bgr is not None else cv2.cvtColor(frame_rgb, cv2.COLOR_RGB2BGR)
            try:
                people = self.gpu.infer(bgr)
            except Exception as e:
                self._gpu_failed(e)
            else:
                self.engine["ms"] = round(self.gpu.ms, 1) if self.gpu.ms else None
                return self._choose(people, hard_lock, now)
        if frame_rgb is None:
            frame_rgb = cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2RGB)
        t0 = time.perf_counter()
        bbox = self._detect_mediapipe(frame_rgb, hard_lock, now)
        ms = (time.perf_counter() - t0) * 1000
        self.engine["ms"] = round(ms if self.engine["ms"] is None else 0.9 * self.engine["ms"] + 0.1 * ms, 1)
        return bbox

    def _choose(self, people, hard_lock, now):
        """Pick the subject among everyone the GPU model found."""
        cands = []
        for p in people:
            b = self._landmarks_to_bbox(p["landmarks"])
            if b is not None:
                cands.append((b, p["score"]))
        if not cands:
            return None
        dist = lambda b, x, y: math.hypot(b[0] - x, b[1] - y)

        if not hard_lock or self._locked_cx is None:
            pick = None
            if self._locked_cx is not None and now - self._lock_seen < 1.0:
                # Keep following the same person rather than hopping to
                # whoever scores highest this frame.
                near = min(cands, key=lambda c: dist(c[0], self._locked_cx, self._locked_cy))
                if dist(near[0], self._locked_cx, self._locked_cy) < self.FOLLOW_STICK:
                    pick = near
            if pick is None:
                pick = max(cands, key=lambda c: c[1] * c[0][3])   # confident × tall = prominent
            bbox = pick[0]
            if hard_lock:
                print(f"[LOCK] Acquired at ({bbox[0]:.2f}, {bbox[1]:.2f})")
                self.diag.event("lock", ev="acquired", cx=bbox[0], cy=bbox[1], people=len(cands))
            self._lock_update(bbox[0], bbox[1], now, bbox[3])
            return bbox

        unseen = max(0.0, now - self._lock_seen)
        ahead  = min(unseen, self.LOCK_PREDICT)
        px = min(1.0, max(0.0, self._locked_cx + self._lock_vx * ahead))
        py = min(1.0, max(0.0, self._locked_cy + self._lock_vy * ahead))
        radius = min(self.LOCK_MAX_DIST, self.LOCK_DIST + self.LOCK_GROW * unseen)
        inside = [c for c in cands if dist(c[0], px, py) < radius]
        if not inside:
            self.diag.event("lock", throttle=0.5, ev="reject", people=len(cands), radius=radius,
                            dist=min(dist(c[0], px, py) for c in cands))
            return None
        lh = self._locked_h or 0.0

        def cost(c):
            size = abs(c[0][3] - lh) / lh if lh > 0 else 0.0
            return dist(c[0], px, py) + self.SIZE_WEIGHT * size
        bbox = min(inside, key=cost)[0]
        if unseen > 0.5:
            print(f"[LOCK] Reacquired after {unseen:.1f}s at ({bbox[0]:.2f}, {bbox[1]:.2f})")
            self.diag.event("lock", ev="reacquired", unseen=unseen, cx=bbox[0], cy=bbox[1])
        self._lock_update(bbox[0], bbox[1], now, bbox[3])
        return bbox

    def _detect_mediapipe(self, frame_rgb, hard_lock, now):
        if not hard_lock or self._locked_cx is None:
            bbox = self._detect_in(self.pose, frame_rgb, 0.0, 1.0)
            if bbox is None:
                return None
            if hard_lock:
                print(f"[LOCK] Acquired at ({bbox[0]:.2f}, {bbox[1]:.2f})")
                self.diag.event("lock", ev="acquired", cx=bbox[0], cy=bbox[1])
            # Unlocked — follow whoever MediaPipe sees, update position freely
            self._lock_update(bbox[0], bbox[1], now)
            return bbox

        # Locked: search where the subject is heading, not where they were
        # last seen, and widen the search the longer they're unseen. The old
        # fixed 0.25 radius around the last position meant a subject who left
        # quickly was never found again until the lock was toggled off.
        unseen = max(0.0, now - self._lock_seen)
        ahead  = min(unseen, self.LOCK_PREDICT)
        px = min(1.0, max(0.0, self._locked_cx + self._lock_vx * ahead))
        py = min(1.0, max(0.0, self._locked_cy + self._lock_vy * ahead))
        radius = min(self.LOCK_MAX_DIST, self.LOCK_DIST + self.LOCK_GROW * unseen)
        half   = max(self.LOCK_CROP, radius + 0.05)
        bbox   = self._detect_in(self.pose_locked, frame_rgb, px - half, px + half)
        if bbox is None:
            return None
        cx, cy = bbox[0], bbox[1]
        dist = math.hypot(cx - px, cy - py)
        if dist < radius:
            if unseen > 0.5:
                print(f"[LOCK] Reacquired after {unseen:.1f}s at ({cx:.2f}, {cy:.2f})")
                self.diag.event("lock", ev="reacquired", unseen=unseen, cx=cx, cy=cy)
            self._lock_update(cx, cy, now)
            return bbox
        self.diag.event("lock", throttle=0.5, ev="reject", dist=dist, radius=radius)
        return None

    def _detect_in(self, pose, frame_rgb, x_lo, x_hi):
        """Run pose on the vertical strip x_lo..x_hi (frame fractions); bbox in full-frame coords."""
        W = frame_rgb.shape[1]
        a = max(0, int(x_lo * W))
        b = min(W, int(math.ceil(x_hi * W)))
        if b - a < W // 8:
            return None
        crop = frame_rgb if (a == 0 and b == W) else np.ascontiguousarray(frame_rgb[:, a:b])
        results = pose.process(crop)
        if not results.pose_landmarks:
            return None
        bbox = self._landmarks_to_bbox(results.pose_landmarks.landmark)
        if bbox is None:
            return None
        cx, cy, w, h = bbox
        k = (b - a) / W
        return a / W + cx * k, cy, w * k, h

    def release_lock(self):
        self._locked_cx = None
        self._locked_cy = None
        self._locked_h  = None
        self._lock_vx = self._lock_vy = 0.0
        self.diag.event("lock", ev="released")
        print("[LOCK] Released — will reacquire")

    def close(self):
        for p in (self.pose, self.pose_locked):
            if p is not None:
                p.close()


def _make_gpu_pose():
    from pose_gpu import GpuPose      # imported lazily: onnxruntime is optional
    return GpuPose()


# ─────────────────────────────────────────────────────────────
# Auto-Tracker
# ─────────────────────────────────────────────────────────────

class OneEuroFilter:
    """
    Adaptive low-pass filter (Casiez et al., "1€ filter"). Heavy smoothing
    while the value is nearly still — kills pose jitter that would otherwise
    nudge the camera — and light smoothing when it moves fast, so a walking
    subject isn't followed late.
    """
    def __init__(self, min_cutoff=1.0, beta=4.0, d_cutoff=1.0):
        self.min_cutoff, self.beta, self.d_cutoff = min_cutoff, beta, d_cutoff
        self.reset()

    def reset(self):
        self._x = self._dx = None

    @staticmethod
    def _alpha(cutoff, dt):
        tau = 1.0 / (2 * math.pi * cutoff)
        return 1.0 / (1.0 + tau / dt)

    def __call__(self, x, dt):
        if self._x is None:
            self._x, self._dx = x, 0.0
            return x
        dt = max(1e-3, dt)
        dx = (x - self._x) / dt
        a_d = self._alpha(self.d_cutoff, dt)
        self._dx = a_d * dx + (1 - a_d) * self._dx
        a = self._alpha(self.min_cutoff + self.beta * abs(self._dx), dt)
        self._x = a * x + (1 - a) * self._x
        return self._x


class AutoTracker:
    CMD_INTERVAL = 0.10   # min seconds between VISCA move commands (anti-flood)
    COAST_TIME   = 0.45   # s a missed detection is bridged before the camera stops
    DECEL_GAIN   = 2.5    # slowing down may ramp this much faster than speeding up
    VEL_SMOOTH   = 0.15   # EMA weight of each new image-velocity measurement
    VEL_MAX      = 0.3    # frame widths / s cap on the velocity used for prediction

    def __init__(self, visca: VISCAController, diag=None):
        self.visca         = visca
        self.diag          = diag or NULL_DIAG
        self._last_seen    = None
        self._at_home      = False
        self._prev_pan     = 0
        self._prev_tilt    = 0
        self._prev_zoom    = 0
        self._pan_active   = 0       # smooth_speed hysteresis state per axis
        self._tilt_active  = 0
        self._f_cx         = OneEuroFilter()
        self._f_cy         = OneEuroFilter()
        self._f_h          = OneEuroFilter(min_cutoff=0.5, beta=1.0)
        self._last_det_t   = None
        self.fps           = 0.0     # set by the tracker thread, for diagnostics
        s = SETTINGS
        self.pilot         = AutoPilot(s.auto_gain_pan, s.auto_gain_tilt, s.auto_latency)
        self._pilot_fits   = 0
        self._pending_reset = None   # "full" / "filters", set from other threads
        self._last_cmd_t   = 0.0
        self._last_cx      = None
        self._last_cy      = None
        self._vx           = 0.0
        self._vy           = 0.0
        # Slew-rate-limited commanded speeds (float). These ease toward the
        # proportional target instead of snapping, which is what removes the
        # jerk from starts, stops, and speed changes.
        self._pan_cmd      = 0.0
        self._tilt_cmd     = 0.0
        self._last_motion_t = time.monotonic()
        # Pulpit anchor
        self.cam_pos        = None     # (pan, tilt, t) from the position poller
        self.anchor_state   = "free"   # free → snapping → held → free
        self._anchor_t      = 0.0      # when the current state began
        self._still_since   = None     # camera idle near the pulpit since…
        self._outside_since = None     # subject outside the hold zone since…
        self._anchor_cool   = 0.0      # no re-snap before this time
        self._anchor_mode   = "recall" # how the current snap is moving

    ANCHOR_UNITS  = 16     # camera position units per "Snap range" step (~1° on PTZOptics)
    ANCHOR_ARRIVE = 3      # units — close enough to call the snap finished
    ANCHOR_MIN    = {"recall": 1.2, "glide": 0.5}   # s before arrival can count (recall also zooms)
    ANCHOR_MAX    = {"recall": 3.0, "glide": 10.0}  # s — hold anyway if the camera never reports arrival
    ANCHOR_LEAVE  = 0.4    # s the subject must stay outside the hold zone to release

    def anchor_offset(self):
        """How far the camera is from the learned pulpit, in Snap-range steps."""
        s, pos = SETTINGS, self.cam_pos
        if s.anchor_pan is None or not pos or time.monotonic() - pos[2] > 1.0:
            return None
        return max(abs(pos[0] - s.anchor_pan), abs(pos[1] - s.anchor_tilt)) / self.ANCHOR_UNITS

    def _anchor(self, now, cx, top=0.0):
        """
        Advance the pulpit-anchor state. True = hold the camera still this frame.
        cx is the subject's aim point, top the top of their box (their head).
        """
        s = SETTINGS
        if not s.anchor_enabled or s.anchor_pan is None:
            self.anchor_state = "free"
            return False

        if self.anchor_state == "snapping":
            mode, elapsed, pos = self._anchor_mode, now - self._anchor_t, self.cam_pos
            arrived = (pos is not None and pos[2] > self._anchor_t and
                       max(abs(pos[0] - s.anchor_pan), abs(pos[1] - s.anchor_tilt)) <= self.ANCHOR_ARRIVE)
            if elapsed < self.ANCHOR_MIN[mode] or (not arrived and elapsed < self.ANCHOR_MAX[mode]):
                return True
            self.anchor_state, self._anchor_t = "held", now
            self._outside_since = None
            # Velocity from frames where the camera itself was moving is junk
            self._vx = self._vy = 0.0
            self._last_cx = self._last_cy = None
            return True

        if self.anchor_state == "held":
            # The hold zone is a box: anchor_hold either side of centre, and
            # from the top of the frame down to anchor_hold_top. The head has
            # to stay in it — leaning and gestures don't, walking down off the
            # platform or sitting down does.
            if abs(cx - 0.5) <= s.anchor_hold and top <= s.anchor_hold_top:
                self._outside_since = None
                return True
            self._outside_since = self._outside_since or now
            if now - self._outside_since < self.ANCHOR_LEAVE:
                return True
            print("[ANCHOR] Speaker left the pulpit — tracking resumed")
            self.anchor_state  = "free"
            self._anchor_cool  = now + 1.5
            self._still_since  = None
            return False

        # free: snap once the camera has come to rest near the pulpit
        off  = self.anchor_offset()
        idle = self._prev_pan == 0 and self._prev_tilt == 0 and abs(self._vx) < 0.05
        if off is None or off > s.anchor_range or not idle or now < self._anchor_cool:
            self._still_since = None
            return False
        self._still_since = self._still_since or now
        if now - self._still_since < s.anchor_dwell:
            return False
        if self._prev_zoom != 0:
            self.visca.zoom_stop()
            self._prev_zoom = 0
        self._anchor_mode = "glide" if s.anchor_mode == "glide" else "recall"
        if self._anchor_mode == "glide":
            print(f"[ANCHOR] Settled at the pulpit — gliding to it at speed {s.anchor_glide}")
            self.visca.absolute_move(s.anchor_pan, s.anchor_tilt, s.anchor_glide, s.anchor_glide)
        else:
            print(f"[ANCHOR] Settled at the pulpit — recalling preset {s.anchor_preset}")
            self.visca.recall_preset(s.anchor_preset)
        self._pan_cmd = self._tilt_cmd = 0.0
        self._prev_pan = self._prev_tilt = 0
        self.pilot.on_command(now, 0, 0)
        self.pilot.blackout(now, self.ANCHOR_MAX[self._anchor_mode] + 1.0)
        self.anchor_state, self._anchor_t = "snapping", now
        return True

    @staticmethod
    def _approach(current, target, max_delta):
        """Step `current` toward `target` by at most `max_delta`."""
        if target > current:
            return min(target, current + max_delta)
        return max(target, current - max_delta)

    def _slew(self, current, target, dt):
        """
        Ease the commanded speed toward target. motion_smooth sets how gentle
        the ramp is (0 = snap). Braking is allowed DECEL_GAIN× quicker than
        speeding up: with video latency on top, a slow brake is what carried
        the camera past the subject and made it swing back.
        """
        s = SETTINGS
        if s.motion_smooth <= 0:
            return target
        accel = max(8.0, 120.0 / s.motion_smooth)   # speed units / second
        if abs(target) < abs(current) or target * current < 0:
            accel *= self.DECEL_GAIN
        return self._approach(current, target, accel * dt)

    def _send_move(self, now, force=False):
        pan_vel  = int(round(self._pan_cmd))
        tilt_vel = int(round(self._tilt_cmd))
        # Rate-limited send: only when the integer speed changes and the
        # minimum interval has elapsed — keeps the VISCA socket from flooding
        # while still updating often enough to look continuous. A stop always
        # goes out immediately.
        stopping = pan_vel == 0 and tilt_vel == 0
        if (pan_vel != self._prev_pan or tilt_vel != self._prev_tilt) and \
           (force or stopping or (now - self._last_cmd_t) >= self.CMD_INTERVAL):
            self.visca.move(pan_vel, tilt_vel)
            self.pilot.on_command(now, pan_vel, tilt_vel)
            self.diag.event("cmd", pan=pan_vel, tilt=tilt_vel)
            self._prev_pan   = pan_vel
            self._prev_tilt  = tilt_vel
            self._last_cmd_t = now

    def _coast(self, now):
        """
        Detection dropped for a frame or two (a turn, an occlusion, motion
        blur). Ease off instead of slamming to a stop and restarting from zero
        on the next frame — that stop/start was a big source of jerkiness.
        """
        dt = max(0.001, min(0.25, now - self._last_motion_t))
        self._last_motion_t = now
        if SETTINGS.auto_mode:
            self._pan_cmd, self._tilt_cmd = self.pilot.coast(now)
        else:
            self._pan_cmd  = self._slew(self._pan_cmd,  0.0, dt)
            self._tilt_cmd = self._slew(self._tilt_cmd, 0.0, dt)
        self._send_move(now)

    def request_reset(self, full=True):
        """
        Reset from another thread (UI / API). The tracker thread may be midway
        through a frame, and the pilot's buffers can't be cleared under it,
        so the reset is applied at the start of the next frame instead.
        """
        if full or self._pending_reset is None:
            self._pending_reset = "full" if full else "filters"

    def _apply_pending_reset(self):
        pending, self._pending_reset = self._pending_reset, None
        if pending == "full":
            self.reset()
        elif pending == "filters":
            self._reset_filters()

    def observe_ego(self, now, bx, by, conf):
        """Camera's own motion measured from the video (auto mode only)."""
        self._apply_pending_reset()
        if self._prev_zoom != 0:
            conf = 0.0          # zooming scales the picture; not a pan/tilt shift
        self.pilot.on_ego(now, bx, by, conf)
        if self.pilot.model.fits != self._pilot_fits:
            self._pilot_fits = self.pilot.model.fits
            m = self.pilot.model
            SETTINGS.auto_gain_pan, SETTINGS.auto_gain_tilt = (round(g, 5) for g in m.gain)
            SETTINGS.auto_latency = round(m.latency, 3)

    def process(self, detection):
        self._apply_pending_reset()
        now = time.monotonic()
        if detection is None:
            if (self._last_seen is not None and not self._at_home
                    and now - self._last_seen < self.COAST_TIME
                    and self.anchor_state == "free"):
                self.diag.event("coast", throttle=0.5)
                self._coast(now)
                self._sample(None, now)
            else:
                self._handle_lost()
                self._sample(None, now)
            return

        s   = SETTINGS
        self._at_home = False

        cx, cy, bbox_w, bbox_h = detection
        raw_cx, raw_cy = cx, cy
        fdt = (now - self._last_det_t) if self._last_det_t else 0.04
        self._last_det_t = now
        cx     = self._f_cx(cx, fdt)
        cy     = self._f_cy(cy, fdt)
        bbox_h = self._f_h(bbox_h, fdt)

        if self._anchor(now, cx, cy - bbox_h / 2):
            # Holding the pulpit shot: the preset owns pan, tilt and zoom
            self._last_seen     = now
            self._last_motion_t = now
            return

        if s.auto_mode:
            self._last_seen = self._last_motion_t = now
            self._pan_cmd, self._tilt_cmd = self.pilot.step(now, cx, cy, bbox_h, s.auto_style)
            self._vx = self.pilot.axes[0].w      # the anchor's "settled" check reads this
            self._send_move(now)
            self._sample((raw_cx, raw_cy, cx, cy, bbox_h, cx), now)
            self._zoom(s, bbox_h, now)
            return

        # Velocity prediction
        dt = max(0.01, min(0.5, (now - self._last_seen) if self._last_seen else 0.1))
        if self._last_cx is not None:
            vx = (cx - self._last_cx) / dt
            vy = (cy - self._last_cy) / dt
            a = self.VEL_SMOOTH
            self._vx = (1 - a) * self._vx + a * vx
            self._vy = (1 - a) * self._vy + a * vy
        self._last_cx   = cx
        self._last_cy   = cy
        self._last_seen = now

        max_v  = self.VEL_MAX
        self._vx = max(-max_v, min(max_v, self._vx))
        self._vy = max(-max_v, min(max_v, self._vy))

        pred_cx = max(0.0, min(1.0, cx + self._vx * s.latency_comp))
        pred_cy = max(0.0, min(1.0, cy + self._vy * s.latency_comp))

        # Continuous proportional targets (float). Negative sign keeps the
        # existing image->camera direction convention.
        pan_speed,  self._pan_active  = smooth_speed(pred_cx, s.pan_dead,  s.pan_slow,
                                                     s.pan_fast,  self._pan_active)
        tilt_speed, self._tilt_active = smooth_speed(pred_cy, s.tilt_dead, s.tilt_slow,
                                                     s.tilt_fast, self._tilt_active)
        pan_target, tilt_target = -pan_speed, -tilt_speed

        # Slew-rate limit: ease the commanded speed toward the target so the
        # camera accelerates and decelerates gradually.
        dt_motion = max(0.001, min(0.25, now - self._last_motion_t))
        self._last_motion_t = now
        self._pan_cmd  = self._slew(self._pan_cmd,  pan_target,  dt_motion)
        self._tilt_cmd = self._slew(self._tilt_cmd, tilt_target, dt_motion)
        self._send_move(now)
        self._sample((raw_cx, raw_cy, cx, cy, bbox_h, pred_cx), now)
        self._zoom(s, bbox_h, now)

    def _zoom(self, s, bbox_h, now):
        # Zoom — once started, keep going until the fill is near the target
        # rather than stopping at the band edge (same hunting fix as pan/tilt).
        if s.zoom_enabled:
            fill   = bbox_h
            settle = s.zoom_dead * 0.4
            if self._prev_zoom == 1 and fill < s.zoom_target - settle:
                zoom_dir = 1
            elif self._prev_zoom == -1 and fill > s.zoom_target + settle:
                zoom_dir = -1
            elif fill < (s.zoom_target - s.zoom_dead):
                zoom_dir = 1
            elif fill > (s.zoom_target + s.zoom_dead):
                zoom_dir = -1
            else:
                zoom_dir = 0
            if zoom_dir != self._prev_zoom:
                self.diag.event("zoom", dir=zoom_dir, fill=fill)
                self.pilot.on_zoom(now, zoom_dir, fill)
                if zoom_dir == 1:
                    self.visca.zoom_in(speed=s.zoom_speed)
                elif zoom_dir == -1:
                    self.visca.zoom_out(speed=s.zoom_speed)
                else:
                    self.visca.zoom_stop()
                self._prev_zoom = zoom_dir
        else:
            if self._prev_zoom != 0:
                self.visca.zoom_stop()
                self._prev_zoom = 0

    def _handle_lost(self):
        if self._prev_pan != 0 or self._prev_tilt != 0:
            self.visca.stop()
            self.pilot.on_command(time.monotonic(), 0, 0)
            self._prev_pan  = 0
            self._prev_tilt = 0
        if self._prev_zoom != 0:
            self.visca.zoom_stop()
            self._prev_zoom = 0
        self._vx = 0.0
        self._vy = 0.0
        self._pan_cmd  = 0.0
        self._tilt_cmd = 0.0
        self._last_cx = None
        self._last_cy = None
        self._reset_filters()
        if self._at_home:
            return
        elapsed = (time.monotonic() - self._last_seen) if self._last_seen else 999
        if elapsed >= SETTINGS.lost_timeout:
            print(f"[TRACKER] Lost — recalling preset {SETTINGS.home_preset}")
            self.diag.event("lost", preset=SETTINGS.home_preset, unseen=elapsed)
            self.visca.recall_preset(SETTINGS.home_preset)
            self.pilot.blackout(time.monotonic(), 6.0)
            self._at_home = True
            self.anchor_state = "free"
            self._still_since = None

    def reset(self):
        self._vx = self._vy = 0.0
        self._pan_cmd = self._tilt_cmd = 0.0
        self._last_motion_t = time.monotonic()
        self._last_cx = self._last_cy = None
        self._prev_pan = self._prev_tilt = self._prev_zoom = 0
        self.anchor_state = "free"
        self._still_since = self._outside_since = None
        self._reset_filters()
        # Manual moves while paused weren't logged as commands: don't learn
        # from the first moment of motion after resuming.
        self.pilot.on_command(time.monotonic(), 0, 0)
        self.pilot.blackout(time.monotonic(), 1.0)

    def _sample(self, det, now):
        """~10/s snapshot for the diagnostics recorder."""
        if det is None:
            self.diag.sample(det=0, home=int(self._at_home), pc=self._pan_cmd,
                             tc=self._tilt_cmd, anchor=self.anchor_state, fps=self.fps)
            return
        raw_cx, raw_cy, cx, cy, h, pred_cx = det
        rec = dict(det=1, rx=raw_cx, ry=raw_cy, cx=cx, cy=cy, h=h, px=pred_cx,
                   vx=self._vx, pc=self._pan_cmd, tc=self._tilt_cmd,
                   pa=self._pan_active, ta=self._tilt_active,
                   z=self._prev_zoom, anchor=self.anchor_state, fps=self.fps)
        if SETTINGS.auto_mode:
            p, m = self.pilot, self.pilot.model
            rec.update(auto=1, sit=p.situation, w=p.axes[0].w, b=p.axes[0].b_ego,
                       g=m.gain[0], gt=m.gain[1], L=m.latency, soft=p.soften,
                       pa=p.axes[0].active, ta=p.axes[1].active)
        self.diag.sample(**rec)

    def _reset_filters(self):
        self.pilot.reset()
        self._pan_active = self._tilt_active = 0
        self._f_cx.reset(); self._f_cy.reset(); self._f_h.reset()
        self._last_det_t = None


# ─────────────────────────────────────────────────────────────
# Tracker Thread
# ─────────────────────────────────────────────────────────────

class TrackerThread(threading.Thread):
    def __init__(self, app):
        super().__init__(daemon=True)
        self.app              = app
        self.running          = False
        self.tracking         = False
        self._stop_event      = threading.Event()
        self.latest_frame     = None
        self.latest_detection = None
        self.status           = "STOPPED"
        self._frame_lock      = threading.Lock()
        self.frame_id         = 0      # bumps on every new frame (preview encoder keys off it)
        self.fps              = 0.0
        # Buffer thread state
        self._buf_frame       = None
        self._buf_lock        = threading.Lock()
        self._buf_ready       = threading.Event()

    def stop(self):
        self._stop_event.set()

    def _track_frame(self, frame, frame_t, detector, tracker, ego):
        tracker.fps = self.fps
        if SETTINGS.auto_mode:
            m = ego.update(frame, frame_t)
            if m:
                tracker.observe_ego(frame_t, *m)
        else:
            ego.reset()
        detection = detector.detect(frame_bgr=frame, hard_lock=self.app.lock_active)
        was_home  = tracker._at_home
        tracker.process(detection)
        if tracker._at_home and not was_home:
            # The home preset reframes everything, so the lock's last
            # position means nothing now. Without this the lock waited
            # forever for someone to reappear at the old spot.
            detector.release_lock()
        return detection

    def _position_poller(self, visca, tracker):
        """Keeps tracker.cam_pos fresh while a learned pulpit anchor is armed."""
        while not self._stop_event.wait(0.25):
            s = SETTINGS
            if not (self.tracking and s.anchor_enabled and s.anchor_pan is not None):
                continue
            if tracker.anchor_state == "held":
                continue   # holding: the camera is still, keep the socket quiet
            pos = visca.query_pan_tilt()
            if pos:
                tracker.cam_pos = (pos[0], pos[1], time.monotonic())

    def _buffer_reader(self, cap, stop_event):
        """
        Dedicated thread that reads frames as fast as possible and
        keeps only the LATEST one. This drains the RTSP buffer
        continuously so the tracker always gets a fresh frame
        instead of a frame that is seconds old.
        """
        while not stop_event.is_set():
            ret, frame = cap.read()
            if ret and frame is not None:
                with self._buf_lock:
                    self._buf_frame = (frame, time.monotonic())
                self._buf_ready.set()
            else:
                time.sleep(0.005)

    def run(self):
        s   = SETTINGS
        url = f"rtsp://{s.rtsp_user}:{s.rtsp_pass}@{s.camera_ip}/{s.rtsp_stream}"
        print(f"[CAP] Connecting: rtsp://{s.rtsp_user}:***@{s.camera_ip}/{s.rtsp_stream}")

        os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"

        def open_rtsp():
            os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|fflags;nobuffer|flags;low_delay"
            # Decode H.264 on the GPU (D3D11 video decoder) when available;
            # falls back to software decoding if the driver/stream refuses.
            try:
                c = cv2.VideoCapture(url, cv2.CAP_FFMPEG,
                                     [cv2.CAP_PROP_HW_ACCELERATION, cv2.VIDEO_ACCELERATION_ANY])
                if not c.isOpened():
                    raise RuntimeError("hardware decode unavailable")
                accel = int(c.get(cv2.CAP_PROP_HW_ACCELERATION))
                print(f"[CAP] Video decode: {'GPU' if accel else 'CPU'}")
            except Exception:
                c = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
                print("[CAP] Video decode: CPU")
            c.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            return c

        cap = open_rtsp()

        if not cap.isOpened():
            self.status = "ERROR: Cannot open stream"
            print(f"[CAP] {self.status}")
            return

        visca    = VISCAController()
        visca.connect()
        visca.set_motion_sync(SETTINGS.motion_sync)
        detector = PersonDetector(diag=DIAG)
        tracker  = AutoTracker(visca, diag=DIAG)
        ego      = EgoMotion()
        DIAG.event("stream", ev="connected", stream=s.rtsp_stream)

        self.app.visca    = visca
        self.app.detector = detector
        self.app.tracker  = tracker
        threading.Thread(target=self._position_poller, args=(visca, tracker),
                         name="PositionPoller", daemon=True).start()

        self.running = True
        self.status  = "PAUSED"
        frame_count  = 0
        errors       = 0

        # Start the dedicated buffer-draining reader thread
        buf_stop = threading.Event()
        buf_thread = threading.Thread(
            target=self._buffer_reader,
            args=(cap, buf_stop),
            daemon=True
        )
        buf_thread.start()

        while not self._stop_event.is_set():
            # Wait up to 3s for a fresh frame
            if not self._buf_ready.wait(timeout=3.0):
                print("[CAP] No frame received — reconnecting...")
                DIAG.event("stream", ev="drop")
                buf_stop.set()
                buf_thread.join(timeout=2.0)
                cap.release()
                time.sleep(1.0)
                cap = open_rtsp()
                buf_stop.clear()
                self._buf_ready.clear()
                buf_thread = threading.Thread(
                    target=self._buffer_reader,
                    args=(cap, buf_stop),
                    daemon=True
                )
                buf_thread.start()
                continue

            # Grab latest frame and immediately clear ready flag
            self._buf_ready.clear()
            with self._buf_lock:
                item = self._buf_frame
                self._buf_frame = None

            if item is None:
                continue
            frame, frame_t = item

            frame_count += 1
            if frame_count == 1:
                print("[CAP] Stream live!")
                self.status = "PAUSED"
                last_t = time.monotonic()
            else:
                now_t  = time.monotonic()
                dt     = max(1e-3, now_t - last_t)
                last_t = now_t
                self.fps = 1.0 / dt if self.fps == 0 else 0.9 * self.fps + 0.1 / dt

            if self.tracking:
                try:
                    detection = self._track_frame(frame, frame_t, detector, tracker, ego)
                except Exception as e:
                    # One bad frame must not end tracking for the rest of the
                    # service: stop the camera, log it, and carry on.
                    detection = None
                    errors += 1
                    if errors <= 3 or errors % 100 == 0:
                        import traceback
                        print(f"[TRACKER] Frame failed ({errors}): {e}")
                        traceback.print_exc()
                    DIAG.event("error", throttle=5.0, where="frame", msg=f"{type(e).__name__}: {e}")
                    try:
                        visca.stop()
                        tracker.request_reset()
                    except Exception:
                        pass
                self.latest_detection = detection
                self.status = "TRACKING"
            else:
                ego.reset()
                detection = None
                self.latest_detection = None
                if self.status == "TRACKING":
                    visca.stop()
                    visca.zoom_stop()
                self.status = "PAUSED"

            with self._frame_lock:
                self.latest_frame = frame   # buffer thread hands us a fresh array each read
                self.frame_id    += 1

        # Cleanup
        buf_stop.set()
        buf_thread.join(timeout=2.0)
        visca.stop()
        visca.zoom_stop()
        visca.disconnect()
        detector.close()
        cap.release()
        self.running = False
        print("[INFO] Tracker thread stopped")



# ─────────────────────────────────────────────────────────────
# Controller — all live state and every action, UI-agnostic
# ─────────────────────────────────────────────────────────────

def _clamp_int(lo, hi):
    return lambda v: max(lo, min(hi, int(round(float(v)))))

def _clamp_float(lo, hi, nd=2):
    return lambda v: round(max(lo, min(hi, float(v))), nd)

def _as_bool(v):
    if isinstance(v, str):
        return v.strip().lower() in ("1", "true", "on", "yes")
    return bool(v)

def _as_str(n):
    return lambda v: str(v).strip()[:n]

# Every setting the UI may change, with its validator. Anything not listed
# here is rejected, so a client can't poke arbitrary attributes.
SETTINGS_SCHEMA = {
    "camera_ip":     _as_str(253),
    "rtsp_user":     _as_str(128),
    "rtsp_pass":     _as_str(128),
    "rtsp_stream":   lambda v: "1" if str(v).strip() == "1" else "2",
    "home_preset":   _clamp_int(0, 89),
    "track_offset":  _clamp_int(-7, 7),
    "pan_dead":      _clamp_float(0.02, 0.30),
    "tilt_dead":     _clamp_float(0.02, 0.30),
    "pan_slow":      _clamp_int(1, 24),
    "pan_fast":      _clamp_int(1, 24),
    "tilt_slow":     _clamp_int(1, 24),
    "tilt_fast":     _clamp_int(1, 24),
    "motion_smooth": _clamp_int(0, 10),
    "motion_sync":   _as_bool,
    "zoom_enabled":  _as_bool,
    "zoom_target":   _clamp_float(0.20, 0.90),
    "zoom_dead":     _clamp_float(0.05, 0.40),
    "zoom_speed":    _clamp_int(0, 7),
    "latency_comp":  _clamp_float(0.0, 2.0),
    "lost_timeout":  _clamp_float(1.0, 10.0, 1),
    "auto_mode":     _as_bool,
    "pose_engine":   lambda v: str(v).strip().lower() if str(v).strip().lower() in ("auto", "gpu", "cpu") else "auto",
    "auto_style":    lambda v: str(v).strip().lower() if str(v).strip().lower() in AUTO_STYLES else "balanced",
    "anchor_enabled": _as_bool,
    "anchor_preset":  _clamp_int(0, 89),
    "anchor_range":   _clamp_int(1, 10),
    "anchor_dwell":   _clamp_float(0.3, 4.0, 1),
    "anchor_hold":    _clamp_float(0.10, 0.45),
    "anchor_hold_top": _clamp_float(0.20, 1.0),
    "anchor_mode":    lambda v: "glide" if str(v).strip().lower() == "glide" else "recall",
    "anchor_glide":   _clamp_int(1, 24),
    "api_enabled":   _as_bool,
    "api_port":      _clamp_int(1024, 65535),
}

# Commands the external Control API (Stream Deck, Companion…) may call.
# Everything else needs the UI token.
PUBLIC_COMMANDS = {"tracking", "lock", "autozoom", "auto", "motion-sync", "preset",
                   "home", "profile", "move", "zoom", "adjust", "anchor"}


_DIAG_PRIVATE = {"rtsp_user", "rtsp_pass", "camera_ip"}

def _diag_settings():
    """Settings snapshot for diagnostics — no credentials."""
    return {k: v for k, v in SETTINGS.to_dict().items() if k not in _DIAG_PRIVATE}


class Controller:
    MANUAL_TTL = 1.2   # s — a held move/zoom stops unless the client re-sends

    # Live-adjustable values: key -> (min, max, getter, setter)
    ADJUSTABLE = {
        "track_offset":  (-7, 7,  lambda: SETTINGS.track_offset,
                          lambda v: setattr(SETTINGS, "track_offset", v)),
        "motion_smooth": (0, 10,  lambda: SETTINGS.motion_smooth,
                          lambda v: setattr(SETTINGS, "motion_smooth", v)),
        "zoom_target":   (20, 90, lambda: int(round(SETTINGS.zoom_target * 100)),
                          lambda v: setattr(SETTINGS, "zoom_target", v / 100.0)),
        "zoom_speed":    (0, 7,   lambda: SETTINGS.zoom_speed,
                          lambda v: setattr(SETTINGS, "zoom_speed", v)),
    }

    def __init__(self):
        self._lock        = threading.RLock()
        self.visca        = None
        self.detector     = None
        self.tracker      = None
        self._thread      = None
        self.lock_active  = False
        self._learn       = {"busy": False, "error": None}
        self._manual      = {"move": None, "zoom": None, "resume": False}
        self._save_timer  = None
        self._jpeg        = (-1, None)
        self._jpeg_lock   = threading.Lock()
        self.exit_event   = threading.Event()
        self.server       = None
        self.window       = None     # pywebview window, when running as an app
        self.updater      = Updater(on_exit=self.request_exit)
        threading.Thread(target=self._watchdog, name="ManualWatchdog", daemon=True).start()

    # ── Lifecycle ─────────────────────────────────────────────

    @property
    def tracking_on(self):
        t = self._thread
        return bool(t and t.running and t.tracking)

    def start_stream(self):
        with self._lock:
            if self._thread and self._thread.is_alive():
                return
            if not SETTINGS.camera_ip:
                return
            self._thread = TrackerThread(self)
            self._thread.start()

    def restart_stream(self):
        """Stop the tracker and reconnect with the current camera settings."""
        with self._lock:
            old, self._thread = self._thread, None
            self.lock_active = False
            if old:
                old.tracking = False
                old.stop()

        def _later():
            if old:
                old.join(timeout=4.0)
            self.start_stream()
        threading.Thread(target=_later, daemon=True).start()

    def request_exit(self):
        """Close the app (window or browser mode)."""
        self.exit_event.set()
        if self.window:
            try:
                self.window.destroy()
            except Exception:
                pass

    def shutdown(self):
        if self._save_timer:
            self._save_timer.cancel()
        SETTINGS.save()
        DIAG.close()
        t = self._thread
        if t:
            t.tracking = False
            t.stop()
            t.join(timeout=2.0)

    def _stream_url(self):
        return (f"rtsp://{SETTINGS.rtsp_user}:{SETTINGS.rtsp_pass}"
                f"@{SETTINGS.camera_ip}/{SETTINGS.rtsp_stream}")

    def schedule_save(self, delay=0.8):
        """Debounced SETTINGS.save() — a dial spin shouldn't write 30×/s."""
        with self._lock:
            if self._save_timer:
                self._save_timer.cancel()
            self._save_timer = threading.Timer(delay, SETTINGS.save)
            self._save_timer.daemon = True
            self._save_timer.start()

    # ── Live toggles ──────────────────────────────────────────

    def set_tracking(self, on):
        """Returns False when the stream isn't up yet (nothing to track)."""
        with self._lock:
            t = self._thread
            if not (t and t.running):
                return False
            t.tracking = bool(on)
            DIAG.event("session", ev="on" if on else "off",
                       settings=_diag_settings() if on else None)
            if on:
                if self.tracker: self.tracker.request_reset()
            else:
                if self.visca: self.visca.stop(); self.visca.zoom_stop()
                self.lock_active = False
            return True

    def set_lock(self, on):
        """Lock only makes sense while tracking. Returns False otherwise."""
        with self._lock:
            if not self.tracking_on:
                return False
            self.lock_active = bool(on)
            DIAG.event("lock", ev="on" if on else "off")
            if not on and self.detector:
                self.detector.release_lock()
            return True

    def set_autozoom(self, on):
        with self._lock:
            SETTINGS.zoom_enabled = bool(on)
            if not on and self.visca:
                try: self.visca.zoom_stop()
                except Exception: pass

    def set_auto(self, on):
        with self._lock:
            if SETTINGS.auto_mode != bool(on):
                SETTINGS.auto_mode = bool(on)
                if self.tracker:
                    self.tracker.request_reset(full=False)
                DIAG.event("auto", on=SETTINGS.auto_mode, style=SETTINGS.auto_style)

    def set_motion_sync(self, on):
        with self._lock:
            SETTINGS.motion_sync = bool(on)
            if self.visca:
                try: self.visca.set_motion_sync(SETTINGS.motion_sync)
                except Exception as e: print(f"[VISCA] Motion Sync toggle failed: {e}")

    # ── Settings & profiles ───────────────────────────────────

    def settings_view(self):
        d = SETTINGS.to_dict()
        d["api_enabled"] = SETTINGS.api_enabled
        d["api_port"]    = SETTINGS.api_port
        d["pose_engine"] = SETTINGS.pose_engine
        return d

    def apply_settings(self, changes):
        """Validate + apply a partial settings dict. Returns the keys that changed."""
        with self._lock:
            before_url = self._stream_url()
            before_api = (SETTINGS.api_enabled, SETTINGS.api_port)
            changed = []
            for key, raw in changes.items():
                if key not in SETTINGS_SCHEMA:
                    raise ValueError(f"Unknown setting '{key}'")
                val = SETTINGS_SCHEMA[key](raw)
                if getattr(SETTINGS, key) != val:
                    setattr(SETTINGS, key, val)
                    changed.append(key)
            SETTINGS.pan_near  = SETTINGS.pan_dead  + 0.15
            SETTINGS.tilt_near = SETTINGS.tilt_dead + 0.15

            if changed:
                DIAG.event("settings", changed={k: getattr(SETTINGS, k) for k in changed
                                                 if k not in _DIAG_PRIVATE})
            if "motion_sync" in changed:
                self.set_motion_sync(SETTINGS.motion_sync)
            if "zoom_enabled" in changed:
                self.set_autozoom(SETTINGS.zoom_enabled)
            if "auto_mode" in changed and self.tracker:
                self.tracker.request_reset(full=False)
            if any(k.startswith("anchor_") for k in changed):
                PROFILE_MANAGER.sync_anchor()
            if self._stream_url() != before_url or "pose_engine" in changed:
                self.restart_stream()
            if changed:
                self.schedule_save(0.3)
            reload_url = None
            if (SETTINGS.api_enabled, SETTINGS.api_port) != before_api and self.server:
                reload_url = self.server.reconfigure()
            return changed, reload_url

    def load_profile(self, name):
        with self._lock:
            before_url = self._stream_url()
            if not PROFILE_MANAGER.load_profile(name):
                return False
            if self.visca:
                try: self.visca.set_motion_sync(SETTINGS.motion_sync)
                except Exception as e: print(f"[VISCA] Motion Sync apply failed: {e}")
            if self._stream_url() != before_url:
                self.restart_stream()
            self.schedule_save(0.3)
            return True

    def complete_setup(self, data):
        """First-run wizard: camera, credentials, home preset, sensitivity."""
        dead = SETTINGS_SCHEMA["pan_dead"](data.get("dead_zone", 0.17))
        self.apply_settings({
            "camera_ip":   data.get("camera_ip", ""),
            "rtsp_user":   data.get("rtsp_user", "admin"),
            "rtsp_pass":   data.get("rtsp_pass", "admin"),
            "home_preset": data.get("home_preset", 0),
            "pan_dead":    dead,
            "tilt_dead":   dead,
        })
        SETTINGS.save()
        self.start_stream()

    # ── Pulpit anchor ─────────────────────────────────────────

    def learn_anchor(self):
        """
        Recall the anchor preset, wait for the camera to stop, and record its
        absolute pan/tilt as this profile's pulpit. Runs in the background —
        a preset move takes a few seconds. Returns False if already running.
        """
        if self._learn["busy"]:
            return False
        self._learn = {"busy": True, "error": None}
        was_tracking = self.tracking_on
        self.set_tracking(False)
        visca = self.visca

        def _run():
            pos, err = None, None
            try:
                if not visca.recall_preset(SETTINGS.anchor_preset):
                    raise RuntimeError("Camera didn't accept the preset recall")
                time.sleep(0.8)
                last, deadline = None, time.monotonic() + 10.0
                while time.monotonic() < deadline:
                    cur = visca.query_pan_tilt(timeout=0.5)
                    if cur and cur == last:
                        pos = cur
                        break
                    last = cur
                    time.sleep(0.3)
                if not pos:
                    raise RuntimeError("Camera didn't report its position — "
                                       "it may not support VISCA position inquiry")
            except Exception as e:
                err = str(e)
            with self._lock:
                if pos:
                    SETTINGS.anchor_pan, SETTINGS.anchor_tilt = pos
                    PROFILE_MANAGER.sync_anchor()
                    self.schedule_save(0.1)
                    print(f"[ANCHOR] Learned preset {SETTINGS.anchor_preset} at pan {pos[0]}, tilt {pos[1]}")
                else:
                    print(f"[ANCHOR] Learn failed: {err}")
                self._learn = {"busy": False, "error": err}
                if was_tracking:
                    self.set_tracking(True)
        threading.Thread(target=_run, name="AnchorLearn", daemon=True).start()
        return True

    def anchor_view(self, tracking):
        s, tr = SETTINGS, self.tracker
        off = tr.anchor_offset() if tr else None
        return {
            "enabled":  bool(s.anchor_enabled),
            "learned":  s.anchor_pan is not None,
            "preset":   s.anchor_preset,
            "mode":     s.anchor_mode,
            "range":    s.anchor_range,
            "hold":     s.anchor_hold,
            "hold_top": s.anchor_hold_top,
            "state":    (tr.anchor_state if (tracking and tr and s.anchor_enabled) else "off"),
            "offset":   round(off, 1) if (off is not None and tracking) else None,
            "learning": self._learn["busy"],
            "error":    self._learn["error"],
        }

    # ── Manual PTZ (held keys) ────────────────────────────────

    def _watchdog(self):
        while not self.exit_event.wait(0.05):
            now = time.monotonic()
            with self._lock:
                for kind in ("move", "zoom"):
                    if self._manual[kind] and now > self._manual[kind]:
                        print(f"[API] Manual {kind} timed out — stopping")
                        self._end_manual(kind)

    def _begin_manual(self, kind, pause_tracking):
        if pause_tracking and self.tracking_on and not self._manual["resume"]:
            self.set_tracking(False)
            self._manual["resume"] = True
        self._manual[kind] = time.monotonic() + self.MANUAL_TTL

    def _end_manual(self, kind):
        self._manual[kind] = None
        if self.visca:
            if kind == "move": self.visca.stop()
            else:              self.visca.zoom_stop()
        if self._manual["resume"] and not (self._manual["move"] or self._manual["zoom"]):
            self._manual["resume"] = False
            self.set_tracking(True)

    # ── Preview ───────────────────────────────────────────────

    def preview_jpeg(self, max_w=1280, quality=78):
        """Latest frame as JPEG, encoded once per frame and shared by all viewers."""
        t = self._thread
        if not t or t.latest_frame is None:
            return None, None
        with self._jpeg_lock:
            if self._jpeg[0] == t.frame_id:
                return self._jpeg
            with t._frame_lock:
                frame, fid = t.latest_frame, t.frame_id
            h, w = frame.shape[:2]
            if w > max_w:
                frame = cv2.resize(frame, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
            ok, buf = cv2.imencode(".jpg", frame, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
            if not ok:
                return None, None
            self._jpeg = (fid, buf.tobytes())
            return self._jpeg

    # ── State ─────────────────────────────────────────────────

    def status(self, full=False):
        t        = self._thread
        running  = bool(t and t.running)
        alive    = bool(t and t.is_alive())
        live     = running and t.latest_frame is not None
        tracking = running and t.tracking
        status   = "TRACKING" if tracking else ("PAUSED" if running else "OFFLINE")
        error    = None
        if not SETTINGS.camera_ip:
            error = "No camera configured"
        elif t and not alive and str(t.status).startswith("ERROR"):
            error = t.status[7:] if t.status.startswith("ERROR: ") else t.status
        det = t.latest_detection if (tracking and t) else None
        out = {
            "ok":            True,
            "app":           "trackmind",
            "version":       VERSION,
            "status":        status,
            "stream":        "live" if live else ("connecting" if alive else "offline"),
            "error":         error,
            "tracking":      tracking,
            "locked":        tracking and self.lock_active,
            "subject":       bool(det),
            "detection":     ({"cx": round(det[0], 4), "cy": round(det[1], 4),
                               "w": round(det[2], 4), "h": round(det[3], 4)} if det else None),
            "autozoom":      bool(SETTINGS.zoom_enabled),
            "zooming":       (self.tracker._prev_zoom if (tracking and self.tracker) else 0),
            "motion_sync":   bool(SETTINGS.motion_sync),
            "visca":         bool(self.visca and self.visca._connected),
            "manual":        bool(self._manual["move"] or self._manual["zoom"]),
            "camera_ip":     SETTINGS.camera_ip,
            "home_preset":   SETTINGS.home_preset,
            "anchor":        self.anchor_view(tracking),
            "auto":          {"enabled": bool(SETTINGS.auto_mode), "style": SETTINGS.auto_style,
                              **(self.tracker.pilot.view() if (self.tracker and tracking
                                                               and SETTINGS.auto_mode) else {})},
            "profile":       PROFILE_MANAGER.current,
            "profiles":      PROFILE_MANAGER.list_profiles(),
            "values":        {k: spec[2]() for k, spec in self.ADJUSTABLE.items()},
        }
        if full:
            out.update({
                "fps":       round(t.fps, 1) if live else 0,
                "frame":     t.frame_id if live else 0,
                "first_run": Settings.is_first_run() or not SETTINGS.camera_ip,
                "settings":  self.settings_view(),
                "update":    self.updater.snapshot(),
                "api":       self.server.api_state() if self.server else None,
                "engine":    (dict(self.detector.engine) if self.detector else None),
                "window":    "app" if self.window else "browser",
            })
        return out

    # ── Command dispatch ──────────────────────────────────────

    @staticmethod
    def _want(body, current):
        st = str(body.get("state", "toggle")).strip().lower()
        if st in ("on", "true", "1"):  return True
        if st in ("off", "false", "0"): return False
        return not current

    @staticmethod
    def _int(body, key, lo, hi, default=None):
        v = body.get(key, default)
        if v is None:
            raise ValueError(f"'{key}' is required")
        return max(lo, min(hi, int(round(float(v)))))

    def command(self, name, body, ui=False):
        """Returns (http_status, json_body). `ui` unlocks the app-only commands."""
        with self._lock:
            try:
                code, extra = self._command(name, body, ui)
            except (TypeError, ValueError) as e:
                code, extra = 400, {"ok": False, "error": str(e)}
            out = {"ok": code == 200}
            out.update(extra)
            out["state"] = self.status(full=ui)
            return code, out

    def _command(self, name, body, ui):
        ok   = lambda **kw: (200, kw)
        fail = lambda code, msg: (code, {"ok": False, "error": msg})
        not_live = "Camera stream isn't connected yet"

        if name == "tracking":
            if not self.set_tracking(self._want(body, self.tracking_on)):
                return fail(409, not_live)
            self._manual["resume"] = False
            return ok()

        if name == "lock":
            want = self._want(body, self.lock_active)
            if not self.tracking_on:
                return ok() if not want else fail(409, "Turn tracking on before locking")
            self.set_lock(want)
            return ok()

        if name == "autozoom":
            self.set_autozoom(self._want(body, SETTINGS.zoom_enabled))
            self.schedule_save()
            return ok()

        if name == "auto":
            if "style" in body:
                SETTINGS.auto_style = SETTINGS_SCHEMA["auto_style"](body["style"])
            self.set_auto(self._want(body, SETTINGS.auto_mode))
            self.schedule_save()
            return ok(auto=SETTINGS.auto_mode, style=SETTINGS.auto_style)

        if name == "motion-sync":
            self.set_motion_sync(self._want(body, SETTINGS.motion_sync))
            self.schedule_save()
            return ok()

        if name in ("preset", "home"):
            preset = (SETTINGS.home_preset if name == "home"
                      else self._int(body, "preset", 0, 89))
            if not self.visca:
                return fail(409, not_live)
            if str(body.get("tracking", "off")).lower() == "off" and self.tracking_on:
                self.set_tracking(False)
            self._manual["resume"] = False
            if not self.visca.recall_preset(preset):
                return fail(502, "Camera didn't accept the VISCA command")
            return ok(preset=preset)

        if name == "anchor":
            SETTINGS.anchor_enabled = self._want(body, SETTINGS.anchor_enabled)
            if SETTINGS.anchor_enabled and SETTINGS.anchor_pan is None:
                SETTINGS.anchor_enabled = False
                return fail(409, "Learn the pulpit position in Settings first")
            if not SETTINGS.anchor_enabled and self.tracker:
                self.tracker.anchor_state = "free"
            PROFILE_MANAGER.sync_anchor()
            self.schedule_save()
            return ok()

        if name == "profile":
            pname = str(body.get("name", ""))
            if not self.load_profile(pname):
                return fail(404, f"No profile named '{pname}'")
            return ok(profile=pname)

        if name == "move":
            if not self.visca:
                return fail(409, not_live)
            pan  = self._int(body, "pan",  -24, 24, 0)   # + = right
            tilt = self._int(body, "tilt", -24, 24, 0)   # + = up
            if pan == 0 and tilt == 0:
                self._end_manual("move")
                return ok()
            self._begin_manual("move", _as_bool(body.get("pause_tracking", True)))
            self.visca.move(-pan, tilt)   # VISCA: positive pan_vel = left
            return ok()

        if name == "zoom":
            if not self.visca:
                return fail(409, not_live)
            direction = str(body.get("dir", "stop")).lower()
            if direction == "stop":
                self._end_manual("zoom")
                return ok()
            speed = self._int(body, "speed", 0, 7, 3)
            self._begin_manual("zoom", _as_bool(body.get("pause_tracking", True)))
            if direction == "in": self.visca.zoom_in(speed)
            else:                 self.visca.zoom_out(speed)
            return ok()

        if name == "adjust":
            key = str(body.get("key", ""))
            if key not in self.ADJUSTABLE:
                return fail(400, f"Unknown setting '{key}'")
            lo, hi, get, put = self.ADJUSTABLE[key]
            if "value" in body:
                val = self._int(body, "value", lo, hi)
            else:
                val = max(lo, min(hi, get() + self._int(body, "delta", -100, 100, 0)))
            put(val)
            self.schedule_save()
            return ok(key=key, value=val)

        # ── App-only commands (need the UI token) ──
        if not ui:
            return fail(404, f"Unknown command '{name}'")

        if name == "settings":
            changes = body.get("changes")
            if not isinstance(changes, dict):
                raise ValueError("'changes' must be an object")
            changed, reload_url = self.apply_settings(changes)
            return ok(changed=changed, reload=reload_url)

        if name == "profile-save":
            pname = str(body.get("name", "")).strip()[:40]
            if not pname:
                raise ValueError("Profile name is required")
            SETTINGS.save()
            PROFILE_MANAGER.save_profile(pname)
            return ok(profile=pname)

        if name == "profile-delete":
            pname = str(body.get("name", ""))
            if pname not in PROFILE_MANAGER.profiles:
                return fail(404, f"No profile named '{pname}'")
            PROFILE_MANAGER.delete_profile(pname)
            return ok()

        if name == "setup":
            self.complete_setup(body)
            return ok()

        if name == "anchor-learn":
            if not (self.visca and self._thread and self._thread.running):
                return fail(409, not_live)
            if not self.learn_anchor():
                return fail(409, "Already learning the pulpit position")
            return ok()

        if name == "reconnect":
            self.restart_stream()
            return ok()

        if name == "update-check":
            self.updater.check_async(manual=True)
            return ok()

        if name == "update-install":
            if not self.updater.install_async():
                return fail(409, "No installable update is available")
            return ok()

        if name == "update-dismiss":
            self.updater.dismiss()
            return ok()

        if name == "window":
            action = str(body.get("action", ""))
            if not self.window:
                return fail(409, "Not running in an app window")
            if action == "fullscreen":
                self.window.toggle_fullscreen()
                return ok()
            return fail(400, f"Unknown window action '{action}'")

        return fail(404, f"Unknown command '{name}'")


# ─────────────────────────────────────────────────────────────
# HTTP server — hosts the UI, live events, preview, Control API
# ─────────────────────────────────────────────────────────────

def _resource_dir(*parts):
    base = sys._MEIPASS if getattr(sys, 'frozen', False) else os.path.dirname(os.path.abspath(__file__))
    return os.path.join(base, *parts)

UI_DIR = _resource_dir("ui")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8", ".json": "application/json",
    ".svg": "image/svg+xml", ".png": "image/png", ".ico": "image/x-icon",
    ".woff2": "font/woff2", ".txt": "text/plain; charset=utf-8",
}


class Server:
    """
    One local HTTP server for everything, bound to 127.0.0.1 only.

      /                    the app UI (static files from ui/)
      /api/status          public state (Stream Deck, Companion…)
      /api/<command>       public commands; app-only ones need the UI token
      /api/state           full state incl. settings        (token)
      /api/events          Server-Sent Events of full state  (token)
      /api/preview.mjpg    live camera preview as MJPEG      (token)

    The UI gets a random per-launch token in its URL and sends it back on
    every call, so the settings (including the camera password) and the
    app-only commands are unreachable for anything else on the PC — even
    when the external Control API is switched on.

    Browser hardening for the public API: requests must carry a localhost
    Host header (defeats DNS rebinding) and POSTs must be application/json
    (forces a CORS preflight, which is never answered).
    """

    def __init__(self, controller):
        self.controller = controller
        self.token      = secrets.token_urlsafe(24)
        self._httpd     = None
        self.port       = None
        self.api_error  = None

    # ── Binding ───────────────────────────────────────────────

    def _bind(self):
        """Bind the configured API port, or a free port if the API is off/busy."""
        want = SETTINGS.api_port if SETTINGS.api_enabled else 0
        try:
            httpd = self._make(want)
            self.api_error = None
        except OSError as e:
            self.api_error = f"Port {want} is in use by another program"
            print(f"[API] {self.api_error} ({e})")
            httpd = self._make(0)
        return httpd

    def start(self):
        self._httpd = self._bind()
        self.port   = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="Server", daemon=True).start()
        print(f"[UI] Serving on http://127.0.0.1:{self.port}/"
              + (f"  ·  Control API on :{self.port}" if self.api_listening else "  ·  Control API off"))

    def stop(self):
        if self._httpd:
            try:
                self._httpd.shutdown()
                self._httpd.server_close()
            except Exception:
                pass
            self._httpd = None

    def reconfigure(self):
        """API port/enable changed: rebind, keep the old server briefly so the
        UI gets its reply, and hand back the new UI address."""
        old = self._httpd
        self._httpd = self._bind()
        self.port   = self._httpd.server_address[1]
        threading.Thread(target=self._httpd.serve_forever, name="Server", daemon=True).start()

        def _retire():
            time.sleep(4.0)
            try:
                old.shutdown(); old.server_close()
            except Exception:
                pass
        threading.Thread(target=_retire, daemon=True).start()
        print(f"[UI] Rebound to :{self.port}")
        return self.ui_url

    @property
    def api_listening(self):
        return bool(self._httpd and SETTINGS.api_enabled and not self.api_error)

    def api_state(self):
        return {"enabled": SETTINGS.api_enabled, "port": SETTINGS.api_port,
                "listening": self.api_listening, "error": self.api_error}

    @property
    def ui_url(self):
        return f"http://127.0.0.1:{self.port}/?token={self.token}"

    # ── Request handling ──────────────────────────────────────

    def _make(self, port):
        import http.server
        import urllib.parse
        server = self
        ctl    = self.controller

        class Handler(http.server.BaseHTTPRequestHandler):
            server_version = f"Trackmind/{VERSION}"
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass   # status is polled constantly — keep the console quiet

            def _send(self, code, data, ctype, extra=None):
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                self.send_header("Cache-Control", "no-store")
                for k, v in (extra or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _json(self, code, body):
                self._send(code, json.dumps(body).encode("utf-8"), "application/json")

            def _host_ok(self):
                host = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]")
                return host in ("127.0.0.1", "localhost", "::1")

            def _parse(self):
                u = urllib.parse.urlsplit(self.path)
                return u.path, urllib.parse.parse_qs(u.query)

            def _authed(self, query):
                tok = self.headers.get("X-Trackmind-Token") or (query.get("token") or [""])[0]
                return secrets.compare_digest(tok, server.token)

            # ── GET ──

            def do_GET(self):
                if not self._host_ok():
                    return self._json(403, {"ok": False, "error": "forbidden host"})
                path, query = self._parse()

                if path == "/api/status":
                    if not (self._authed(query) or server.api_listening):
                        return self._json(403, {"ok": False, "error": "Control API is disabled"})
                    return self._json(200, ctl.status())

                if path.startswith("/api/"):
                    if not self._authed(query):
                        return self._json(403, {"ok": False, "error": "missing UI token"})
                    if path == "/api/state":
                        return self._json(200, ctl.status(full=True))
                    if path == "/api/events":
                        return self._events()
                    if path == "/api/preview.mjpg":
                        return self._mjpeg()
                    return self._json(404, {"ok": False, "error": "not found"})

                return self._static(path)

            def _static(self, path):
                rel = "index.html" if path in ("", "/") else path.lstrip("/")
                full = os.path.normpath(os.path.join(UI_DIR, rel))
                if not full.startswith(os.path.normpath(UI_DIR) + os.sep) or not os.path.isfile(full):
                    return self._send(404, b"Not found", "text/plain")
                with open(full, "rb") as f:
                    data = f.read()
                ctype = _CONTENT_TYPES.get(os.path.splitext(full)[1].lower(), "application/octet-stream")
                self._send(200, data, ctype)

            def _stream_headers(self, ctype):
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Connection", "close")
                self.end_headers()
                self.close_connection = True

            def _events(self):
                """Push full state whenever it changes (≤ 20 Hz), heartbeat otherwise."""
                self._stream_headers("text/event-stream")
                last, last_sent = None, 0.0
                try:
                    while not ctl.exit_event.is_set() and server._httpd:
                        snap = json.dumps(ctl.status(full=True))
                        now  = time.monotonic()
                        if snap != last:
                            self.wfile.write(b"data: " + snap.encode("utf-8") + b"\n\n")
                            self.wfile.flush()
                            last, last_sent = snap, now
                        elif now - last_sent > 10:
                            self.wfile.write(b": ping\n\n")
                            self.wfile.flush()
                            last_sent = now
                        time.sleep(0.05)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            def _mjpeg(self):
                boundary = "trackmindframe"
                self._stream_headers(f"multipart/x-mixed-replace; boundary={boundary}")
                last = -1
                try:
                    while not ctl.exit_event.is_set() and server._httpd:
                        fid, jpg = ctl.preview_jpeg()
                        if jpg is None or fid == last:
                            time.sleep(0.015)
                            continue
                        last = fid
                        self.wfile.write(
                            f"--{boundary}\r\nContent-Type: image/jpeg\r\n"
                            f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
                        self.wfile.flush()
                        time.sleep(1 / 40)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError, OSError):
                    pass

            # ── POST ──

            def do_POST(self):
                if not self._host_ok():
                    return self._json(403, {"ok": False, "error": "forbidden host"})
                path, query = self._parse()
                ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
                if ctype != "application/json":
                    return self._json(415, {"ok": False, "error": "Content-Type must be application/json"})
                try:
                    length = min(int(self.headers.get("Content-Length") or 0), 65536)
                    body   = json.loads(self.rfile.read(length) or b"{}")
                    if not isinstance(body, dict):
                        raise ValueError("body must be a JSON object")
                except Exception as e:
                    return self._json(400, {"ok": False, "error": f"bad request: {e}"})
                if not path.startswith("/api/"):
                    return self._json(404, {"ok": False, "error": "not found"})
                name = path[5:].rstrip("/")
                ui   = self._authed(query)
                if not ui:
                    if not server.api_listening:
                        return self._json(403, {"ok": False, "error": "Control API is disabled"})
                    if name not in PUBLIC_COMMANDS:
                        return self._json(404, {"ok": False, "error": f"Unknown command '{name}'"})
                code, result = ctl.command(name, body, ui=ui)
                self._json(code, result)

        class HTTPServer(http.server.ThreadingHTTPServer):
            daemon_threads      = True
            allow_reuse_address = False   # never share the port with a second instance

        return HTTPServer(("127.0.0.1", port), Handler)


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    import argparse
    ap = argparse.ArgumentParser(description="Trackmind — intelligent PTZ auto-tracking")
    ap.add_argument("--browser", action="store_true",
                    help="open the UI in your web browser instead of an app window")
    ap.add_argument("--debug", action="store_true", help="enable web inspector (app window)")
    args = ap.parse_args()

    controller = Controller()
    server     = Server(controller)
    controller.server = server
    server.start()
    controller.start_stream()
    threading.Timer(3.0, controller.updater.check_async).start()

    webview = None
    if not args.browser:
        try:
            import webview  # pywebview — WebView2 on Windows
        except ImportError:
            print("[UI] pywebview not installed — opening in your browser instead")

    try:
        if webview:
            try:
                controller.window = webview.create_window(
                    "Trackmind", server.ui_url,
                    width=1360, height=860, min_size=(1000, 640),
                    background_color="#07090c", text_select=False)
                webview.start(debug=args.debug, private_mode=False,
                              storage_path=os.path.join(Settings._config_dir(), "webview"))
            except Exception as e:
                # e.g. the WebView2 runtime is missing — the browser works just as well
                print(f"[UI] App window unavailable ({e}) — opening in your browser")
                controller.window = None
                webview = None
        if not webview and not controller.exit_event.is_set():
            import webbrowser
            webbrowser.open(server.ui_url)
            print(f"[UI] Open {server.ui_url}  —  press Ctrl+C to quit")
            while not controller.exit_event.wait(0.5):
                pass
    except KeyboardInterrupt:
        pass
    finally:
        controller.exit_event.set()
        controller.shutdown()
        server.stop()


if __name__ == "__main__":
    main()
