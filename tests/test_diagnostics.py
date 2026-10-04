"""Diagnostics recorder: retention, throttling, privacy, and the problem analyzer."""

import datetime as dt
import json
import os
import time

import pytest

import autotrack
import diagnostics
from diagnostics import DiagnosticsLog, analyze, load_records


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def _ts(day):
    return time.mktime(dt.datetime.strptime(day, "%Y-%m-%d").timetuple()) + 12 * 3600


def test_writes_daily_jsonl(tmp_path):
    c = Clock(_ts("2026-09-30"))
    log = DiagnosticsLog(str(tmp_path), clock=c)
    log.event("lost", preset=5)
    log.close()
    lines = (tmp_path / "diag-2026-09-30.jsonl").read_text().splitlines()
    assert json.loads(lines[0])["k"] == "lost"


def test_purges_files_older_than_30_days(tmp_path):
    for day in ("2026-08-20", "2026-08-31", "2026-09-01", "2026-09-29"):
        (tmp_path / f"diag-{day}.jsonl").write_text("")
    (tmp_path / "notes.txt").write_text("keep")
    c = Clock(_ts("2026-09-30"))
    log = DiagnosticsLog(str(tmp_path), clock=c)
    log.event("session", ev="on")          # first write of the day triggers purge
    log.close()
    names = sorted(os.listdir(tmp_path))
    assert "diag-2026-08-20.jsonl" not in names
    assert "diag-2026-08-31.jsonl" in names      # exactly 30 days old: kept
    assert "diag-2026-09-01.jsonl" in names
    assert "notes.txt" in names


def test_purge_runs_again_at_midnight_rollover(tmp_path):
    (tmp_path / "diag-2026-08-31.jsonl").write_text("")
    c = Clock(_ts("2026-09-30"))
    log = DiagnosticsLog(str(tmp_path), clock=c)
    log.event("x")
    assert (tmp_path / "diag-2026-08-31.jsonl").exists()
    c.t = _ts("2026-10-01")
    log.event("x")
    log.close()
    assert not (tmp_path / "diag-2026-08-31.jsonl").exists()


def test_samples_are_rate_limited(tmp_path):
    c = Clock(_ts("2026-09-30"))
    log = DiagnosticsLog(str(tmp_path), clock=c)
    for _ in range(100):
        c.t += 0.01                          # 100 fps for 1 s
        log.sample(det=1)
    log.close()
    n = len(load_records(str(tmp_path), days=1, now=c.t))
    assert 9 <= n <= 11


def test_event_throttle(tmp_path):
    c = Clock(_ts("2026-09-30"))
    log = DiagnosticsLog(str(tmp_path), clock=c)
    for _ in range(10):
        c.t += 0.1
        log.event("lock", throttle=0.5, ev="reject")
    log.close()
    assert len(load_records(str(tmp_path), days=1, now=c.t)) == 2


def test_disabled_writes_nothing(tmp_path):
    log = DiagnosticsLog(str(tmp_path), enabled=False)
    log.event("x"); log.sample(det=1); log.close()
    assert os.listdir(tmp_path) == []


def test_corrupt_lines_are_skipped(tmp_path):
    t = _ts("2026-09-30")
    (tmp_path / "diag-2026-09-30.jsonl").write_text(
        json.dumps({"t": t, "k": "lost"}) + "\n{\"t\": 12, \"k\n")
    assert len(load_records(str(tmp_path), days=1, now=t + 1)) == 1


def test_settings_snapshot_has_no_credentials():
    snap = autotrack._diag_settings()
    assert not {"rtsp_pass", "rtsp_user", "camera_ip"} & set(snap)


# ── Analyzer ───────────────────────────────────────────────────

def _session(seconds, cmd_pattern, det=1, fps=25.0):
    """Synthetic minute of samples plus a repeating cmd pattern [(dt, pan), ...]."""
    t0, recs = 1_000_000.0, []
    for i in range(int(seconds * 10)):
        recs.append({"t": t0 + i * 0.1, "k": "sample", "det": det, "cx": 0.5,
                     "pc": 3, "fps": fps})
    t, i = t0, 0
    while t < t0 + seconds:
        d, pan = cmd_pattern[i % len(cmd_pattern)]
        t += d
        recs.append({"t": t, "k": "cmd", "pan": pan, "tilt": 0})
        i += 1
    return sorted(recs, key=lambda r: r["t"])


def test_analyzer_flags_hunting():
    rep = analyze(_session(60, [(0.5, 6), (0.5, -6)]))
    assert rep["metrics"]["pan_reversals"] > 50
    assert any(i.startswith("Hunting") for i in rep["issues"])


def test_analyzer_flags_stop_start():
    rep = analyze(_session(60, [(0.3, 3), (0.3, 0)]))
    assert any(i.startswith("Stop-start") for i in rep["issues"])


def test_analyzer_flags_dropouts_and_low_fps():
    rep = analyze(_session(60, [(1.0, 0)], det=0, fps=9.0))
    text = " ".join(rep["issues"])
    assert "Detection dropping out" in text and "Low frame rate" in text


def test_analyzer_quiet_on_clean_session():
    rep = analyze(_session(60, [(3.0, 3), (3.0, 0)]))
    assert rep["issues"] == []


def test_end_to_end_tracker_writes_analyzable_log(tmp_path, settings, clock, visca):
    log = DiagnosticsLog(str(tmp_path))
    tr = autotrack.AutoTracker(visca, diag=log)
    for i in range(100):
        clock.advance(0.04)
        tr.process((0.9, 0.5, 0.2, 0.4) if i % 10 else None)
    log.close()
    recs = load_records(str(tmp_path), days=1)
    kinds = {r["k"] for r in recs}
    assert {"sample", "cmd", "coast"} <= kinds
    assert analyze(recs)["metrics"]["coasts"] >= 1


def test_cli_runs(tmp_path, capsys):
    assert diagnostics.main(["--dir", str(tmp_path), "--days", "1"]) == 0
    assert "No issues found" in capsys.readouterr().out


def test_empty_stage_at_home_is_not_a_dropout():
    recs = _session(60, [(3.0, 0)], det=0)
    for r in recs:
        if r["k"] == "sample":
            r["home"] = 1
    rep = analyze(recs)
    assert not any("Detection dropping out" in i for i in rep["issues"])


def test_analyzer_reports_auto_mode():
    recs = _session(60, [(3.0, 3), (3.0, 0)])
    for i, r in enumerate(r for r in recs if r["k"] == "sample"):
        r.update(auto=1, sit="walking" if i % 4 == 0 else "still", g=0.04, L=0.45,
                 soft=1.0 if i % 50 else 0.7)
    m = analyze(recs)["metrics"]
    assert m["auto_pct_of_tracking"] == 100.0
    assert m["auto_situations_pct"]["still"] == pytest.approx(75, abs=1)
    assert m["auto_gain_pan"] == 0.04 and m["auto_latency_s"] == 0.45
    assert m["auto_eased_off"] >= 10
    assert any("eased off" in i for i in analyze(recs)["issues"])
