#!/usr/bin/env python3
"""
Trackmind diagnostics: a flight recorder for the tracker.

While tracking, Trackmind writes what it saw and what it told the camera to
daily JSON-lines files (~/.trackmind/<user>/diagnostics/diag-YYYY-MM-DD.jsonl).
Files older than RETENTION_DAYS (30) are deleted automatically.

Each line is one record: {"t": <unix time>, "k": <kind>, ...fields}
    sample   ~10/s while tracking: subject position, commanded speeds, fps
             (home=1: subject gone and camera parked at its home preset)
    cmd      a pan/tilt speed actually sent to the camera
    zoom     a zoom direction sent to the camera
    coast    detection dropped; easing off instead of stopping
    lost     subject gone past lost_timeout; camera sent home
    lock     lock acquired / released / rejected a different person
    stream   RTSP stream (re)connected or dropped
    session  tracking turned on/off (with a settings snapshot)
    settings settings changed

The analyzer turns that into a report of likely problems:

    python diagnostics.py               last 7 days
    python diagnostics.py --days 30     everything kept
    python diagnostics.py --json        machine-readable
"""

import datetime as _dt
import glob
import json
import os
import sys
import threading
import time

RETENTION_DAYS = 30
MAX_FILE_BYTES = 100 * 1024 * 1024   # per day; past this only events are kept
SAMPLE_INTERVAL = 0.1                # s between "sample" records


def _day(ts):
    return _dt.datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


class DiagnosticsLog:
    """Thread-safe, append-only JSONL recorder with day files and retention."""

    def __init__(self, directory, retention_days=RETENTION_DAYS, clock=time.time,
                 enabled=True):
        self.directory      = directory
        self.retention_days = retention_days
        self.enabled        = enabled
        self._clock         = clock
        self._lock          = threading.Lock()
        self._file          = None
        self._file_day      = None
        self._last_sample   = 0.0
        self._last_kind_t   = {}
        self._last_flush    = 0.0
        self._purged_day    = None

    # ── Writing ───────────────────────────────────────────────

    def event(self, kind, throttle=0.0, **fields):
        """Record one event. `throttle` = min seconds between events of this kind."""
        if not self.enabled:
            return
        now = self._clock()
        if throttle and now - self._last_kind_t.get(kind, -1e9) < throttle:
            return
        self._last_kind_t[kind] = now
        self._write(now, kind, fields, flush=True)

    def sample(self, **fields):
        """High-rate state snapshot; rate-limited to SAMPLE_INTERVAL."""
        if not self.enabled:
            return
        now = self._clock()
        if now - self._last_sample < SAMPLE_INTERVAL:
            return
        self._last_sample = now
        self._write(now, "sample", fields, flush=False)

    def _write(self, now, kind, fields, flush):
        rec = {"t": round(now, 3), "k": kind}
        for key, val in fields.items():
            rec[key] = round(val, 4) if isinstance(val, float) else val
        line = json.dumps(rec, separators=(",", ":")) + "\n"
        with self._lock:
            try:
                f = self._open(now)
                if kind == "sample" and f.tell() > MAX_FILE_BYTES:
                    return
                f.write(line)
                if flush or now - self._last_flush > 2.0:
                    f.flush()
                    self._last_flush = now
            except OSError:
                pass   # diagnostics must never take the tracker down

    def _open(self, now):
        day = _day(now)
        if self._file is None or day != self._file_day:
            if self._file:
                self._file.close()
            os.makedirs(self.directory, exist_ok=True)
            self._file = open(os.path.join(self.directory, f"diag-{day}.jsonl"),
                              "a", encoding="utf-8")
            self._file_day = day
            if self._purged_day != day:
                self._purged_day = day
                self._purge_locked(now)
        return self._file

    # ── Retention ─────────────────────────────────────────────

    def purge(self):
        with self._lock:
            return self._purge_locked(self._clock())

    def _purge_locked(self, now):
        """Delete day files older than the retention window. Returns names removed."""
        cutoff = _dt.date.fromtimestamp(now) - _dt.timedelta(days=self.retention_days)
        removed = []
        for path in glob.glob(os.path.join(self.directory, "diag-*.jsonl")):
            name = os.path.basename(path)
            try:
                day = _dt.datetime.strptime(name[5:15], "%Y-%m-%d").date()
            except ValueError:
                continue
            if day < cutoff:
                try:
                    os.remove(path)
                    removed.append(name)
                except OSError:
                    pass
        return removed

    def close(self):
        with self._lock:
            if self._file:
                self._file.close()
                self._file = None


class NullDiagnostics:
    """Drop-in that records nothing (tests, or diagnostics turned off)."""
    def event(self, *a, **kw): pass
    def sample(self, **kw): pass
    def purge(self): return []
    def close(self): pass


NULL_DIAG = NullDiagnostics()


# ─────────────────────────────────────────────────────────────
# Analyzer
# ─────────────────────────────────────────────────────────────

def load_records(directory, days=7, now=None):
    now = now or time.time()
    since = now - days * 86400
    out = []
    for path in sorted(glob.glob(os.path.join(directory, "diag-*.jsonl"))):
        with open(path, encoding="utf-8") as f:
            for line in f:
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue   # a line cut short by a crash
                if rec.get("t", 0) >= since:
                    out.append(rec)
    out.sort(key=lambda r: r["t"])
    return out


# Thresholds for the "issues" list. Tuned to call out what a viewer notices.
REVERSAL_WINDOW   = 1.5    # s — a direction flip this soon after moving = hunting
HUNT_PER_MIN      = 2.0
STOPSTART_PER_MIN = 12.0
DROPOUT_PCT       = 15.0
HIGH_SPEED        = 9      # VISCA pan/tilt speed considered fast for live video
HIGH_SPEED_PCT    = 10.0
LOW_FPS           = 15.0


def analyze(records):
    """Summarize records into metrics plus a list of human-readable issues."""
    samples = [r for r in records if r["k"] == "sample"]
    cmds    = [r for r in records if r["k"] == "cmd"]
    kinds   = {}
    for r in records:
        kinds[r["k"]] = kinds.get(r["k"], 0) + 1

    # Tracking time: sum of gaps between consecutive samples, ignoring pauses
    tracked_s = 0.0
    for a, b in zip(samples, samples[1:]):
        gap = b["t"] - a["t"]
        if gap < 2.0:
            tracked_s += gap
    minutes = tracked_s / 60.0

    # Parked at home with nobody on stage isn't a detection problem
    looking  = [r for r in samples if not r.get("home")]
    detected = sum(1 for r in looking if r.get("det"))
    dropout_pct = 100.0 * (1 - detected / len(looking)) if looking else 0.0

    # Hunting: a command reverses an axis shortly after moving the other way
    def reversals(axis):
        n, last_dir, last_t = 0, 0, None
        for r in cmds:
            v = r.get(axis, 0)
            d = (v > 0) - (v < 0)
            if d == 0:
                continue
            if last_dir and d != last_dir and r["t"] - last_t < REVERSAL_WINDOW:
                n += 1
            last_dir, last_t = d, r["t"]
        return n
    pan_rev, tilt_rev = reversals("pan"), reversals("tilt")

    # Stop/start cycles: moving → stopped → moving again within 1s
    stopstart, prev_moving, stop_t = 0, False, None
    for r in cmds:
        moving = bool(r.get("pan") or r.get("tilt"))
        if prev_moving and not moving:
            stop_t = r["t"]
        elif moving and not prev_moving and stop_t is not None and r["t"] - stop_t < 1.0:
            stopstart += 1
        prev_moving = moving

    speeds = [abs(r.get("pc", 0)) for r in samples if r.get("det")]
    fast_pct = 100.0 * sum(1 for v in speeds if v >= HIGH_SPEED) / len(speeds) if speeds else 0.0
    fps = [r["fps"] for r in samples if r.get("fps")]
    errs = [abs(r["cx"] - 0.5) for r in samples if r.get("det") and "cx" in r]

    m = {
        "records": len(records),
        "tracked_minutes": round(minutes, 1),
        "detection_dropout_pct": round(dropout_pct, 1),
        "coasts": kinds.get("coast", 0),
        "lost_to_home": kinds.get("lost", 0),
        "lock_rejects": sum(1 for r in records if r["k"] == "lock" and r.get("ev") == "reject"),
        "lock_acquired": sum(1 for r in records if r["k"] == "lock" and r.get("ev") == "acquired"),
        "stream_drops": sum(1 for r in records if r["k"] == "stream" and r.get("ev") == "drop"),
        "pan_reversals": pan_rev,
        "tilt_reversals": tilt_rev,
        "stop_start_cycles": stopstart,
        "fast_pan_pct": round(fast_pct, 1),
        "max_pan_speed": max((abs(r.get("pan", 0)) for r in cmds), default=0),
        "mean_center_error": round(sum(errs) / len(errs), 3) if errs else None,
        "fps_avg": round(sum(fps) / len(fps), 1) if fps else None,
        "fps_min": round(min(fps), 1) if fps else None,
    }

    # Auto mode: how it spent its time and what it learned about the camera
    auto = [r for r in samples if r.get("auto")]
    if auto:
        sits = {}
        for r in auto:
            sits[r.get("sit", "?")] = sits.get(r.get("sit", "?"), 0) + 1
        last = auto[-1]
        eased = sum(1 for a, b in zip(auto, auto[1:])
                    if b.get("soft", 1) < a.get("soft", 1) - 0.05)
        m.update({
            "auto_pct_of_tracking": round(100.0 * len(auto) / len(samples), 1),
            "auto_situations_pct": {k: round(100.0 * v / len(auto), 1) for k, v in sorted(sits.items())},
            "auto_gain_pan": last.get("g"),
            "auto_latency_s": last.get("L"),
            "auto_eased_off": eased,
        })

    issues = []
    per_min = lambda n: n / minutes if minutes > 0.5 else 0.0
    if per_min(pan_rev + tilt_rev) > HUNT_PER_MIN:
        issues.append(f"Hunting: camera reversed direction {pan_rev + tilt_rev} times "
                      f"({per_min(pan_rev + tilt_rev):.1f}/min). Lower the fast speeds, raise "
                      f"Smoothing, or lower latency compensation.")
    if per_min(stopstart) > STOPSTART_PER_MIN:
        issues.append(f"Stop-start stutter: {stopstart} stop→start cycles "
                      f"({per_min(stopstart):.1f}/min). Widen the dead zones a little.")
    if fast_pct > HIGH_SPEED_PCT:
        issues.append(f"Running fast: pan speed >={HIGH_SPEED} for {fast_pct:.0f}% of tracked "
                      f"frames. Lower pan/tilt fast speed (4–6 suits most zoomed-in shots).")
    if looking and dropout_pct > DROPOUT_PCT:
        issues.append(f"Detection dropping out on {dropout_pct:.0f}% of frames. Check lighting, "
                      f"zoom (the whole torso should be in frame), or use the sub stream.")
    if m["lock_rejects"] > 20:
        issues.append(f"Lock rejected another person {m['lock_rejects']} times — people "
                      f"crossing near the locked subject.")
    if m["lost_to_home"] and minutes and per_min(m["lost_to_home"]) > 0.5:
        issues.append(f"Subject lost and camera sent home {m['lost_to_home']} times. "
                      f"Consider a longer lost timeout.")
    if fps and m["fps_avg"] < LOW_FPS:
        issues.append(f"Low frame rate ({m['fps_avg']} fps avg). Use the sub stream or a "
                      f"faster PC; the tracker reacts late at low fps.")
    if auto and per_min(m["auto_eased_off"]) > 0.3:
        issues.append(f"Auto mode eased off {m['auto_eased_off']} times because the camera "
                      f"was bouncing. Try the Calm style, or check the video delay "
                      f"(auto_latency_s) - over 0.6 s usually means the main stream is selected.")
    if m["stream_drops"]:
        issues.append(f"Video stream dropped {m['stream_drops']} times (network/camera).")
    return {"metrics": m, "issues": issues}


def default_dir():
    user = (os.environ.get("USERNAME") or os.environ.get("USER") or "default").lower()
    return os.path.join(os.path.expanduser("~/.trackmind"), user, "diagnostics")


def main(argv=None):
    import argparse
    ap = argparse.ArgumentParser(description="Summarize Trackmind diagnostics")
    ap.add_argument("--dir", default=default_dir())
    ap.add_argument("--days", type=float, default=7)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    report = analyze(load_records(args.dir, args.days))
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"Trackmind diagnostics - last {args.days:g} days ({args.dir})\n")
    for k, v in report["metrics"].items():
        print(f"  {k:<24} {v}")
    print()
    if report["issues"]:
        print("Issues:")
        for i in report["issues"]:
            print(f"  * {i}")
    else:
        print("No issues found.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
