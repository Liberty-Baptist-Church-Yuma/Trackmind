"""
Closed-loop PTZ camera simulator for testing the tracker end to end.

World units are degrees. The camera pans at `deg_per_step` °/s per VISCA
speed step (after `cmd_latency`, with a first-order ramp), the picture spans
`fov` degrees, the tracker sees each frame `video_latency` late at `fps`, with
pose jitter and missed detections. Ego-motion is measured from the same
delayed frames, as EgoMotion would.

Drives the real AutoTracker (manual or auto mode) through a FakeVisca, with
time.monotonic patched to the simulation clock.
"""

import math
import random
from dataclasses import dataclass, field


@dataclass
class Camera:
    fov: float = 25.0              # horizontal field of view, degrees
    aspect: float = 16 / 9
    deg_per_step: float = 1.0      # °/s per VISCA speed step
    video_latency: float = 0.30
    cmd_latency: float = 0.10
    ramp: float = 0.10             # s, camera's own acceleration time constant
    fps: float = 15.0              # frames the tracker processes per second
    pose_noise: float = 0.006      # frame fractions (sd)
    ego_noise: float = 0.01        # frame widths / s (sd)
    miss_rate: float = 0.0


@dataclass
class Run:
    t: list = field(default_factory=list)       # processing times
    x: list = field(default_factory=list)       # true subject image x (now, not delayed)
    pan: list = field(default_factory=list)     # VISCA pan speed in effect
    cam_v: list = field(default_factory=list)   # camera angular velocity °/s
    learned: dict = None

    # ── Metrics ───────────────────────────────────────────────
    def reversals(self, after=0.0):
        n, last = 0, 0
        for t, p in zip(self.t, self.pan):
            if t < after:
                continue
            d = (p > 0) - (p < 0)
            if d and last and d != last:
                n += 1
            if d:
                last = d
        return n

    def stop_starts(self, after=0.0):
        n, moving, stop_t = 0, False, None
        for t, p in zip(self.t, self.pan):
            if t < after:
                continue
            if moving and p == 0:
                stop_t = t
            elif not moving and p != 0 and stop_t is not None and t - stop_t < 1.0:
                n += 1
            moving = p != 0
        return n

    def mean_error(self, start=0.0, end=1e9):
        e = [abs(x - 0.5) for t, x in zip(self.t, self.x) if start <= t <= end]
        return sum(e) / len(e) if e else 0.0

    def max_error(self, start=0.0, end=1e9):
        return max(abs(x - 0.5) for t, x in zip(self.t, self.x) if start <= t <= end)

    def lost_frames(self):
        return sum(1 for x in self.x if not 0.0 <= x <= 1.0)

    def jerk(self, after=0.0):
        """RMS camera acceleration in frame widths/s² (what a viewer sees) — lower is smoother."""
        acc = []
        for i in range(1, len(self.t)):
            if self.t[i] < after:
                continue
            dt = self.t[i] - self.t[i - 1]
            acc.append((self.cam_v[i] - self.cam_v[i - 1]) / dt)
        return math.sqrt(sum(a * a for a in acc) / len(acc)) / self.fov if acc else 0.0


def simulate(autotrack, clock, visca, script, cam=Camera(), seconds=20.0, seed=0,
             tracker=None, start_x=0.0):
    """
    script(t) -> subject angle in degrees (relative to the camera's start).
    Returns a Run. Pass `tracker` to continue a run with a warmed-up tracker.
    """
    rnd = random.Random(seed)
    tracker = tracker or autotrack.AutoTracker(visca)
    phys = 1 / 240
    cam_a, cam_v = 0.0, 0.0
    hist = []                 # (t, cam_angle, subject_angle)
    cmds = [(-1e9, 0)]
    run = Run()
    run.fov = cam.fov
    t, next_frame, prev_frame = 0.0, 0.0, None
    base = clock.t + 1.0          # time only moves forward across chained runs
    period = 1.0 / cam.fps
    last_pan = None
    while t < seconds:
        # Camera physics
        if visca.pan != last_pan:
            cmds.append((t, visca.pan)); last_pan = visca.pan
        applied = next(p for (ct, p) in reversed(cmds) if ct <= t - cam.cmd_latency)
        target_v = -applied * cam.deg_per_step           # VISCA pan + = left
        cam_v += (target_v - cam_v) * min(1.0, phys / cam.ramp)
        cam_a += cam_v * phys
        hist.append((t, cam_a, script(t)))

        if t >= next_frame:
            next_frame += period
            clock.t = base + t
            seen_t = t - cam.video_latency
            j = _at(hist, seen_t)
            if j is not None:
                _, ca, sa = hist[j]
                x_seen = 0.5 + (sa - ca) / cam.fov
                if prev_frame is not None:
                    pt, pca = prev_frame
                    dt = t - pt
                    b = -(ca - pca) / cam.fov / dt + rnd.gauss(0, cam.ego_noise)
                    if autotrack.SETTINGS.auto_mode:
                        tracker.observe_ego(clock.t, b, 0.0, 0.9)
                prev_frame = (t, ca)
                visible = 0.0 <= x_seen <= 1.0 and rnd.random() >= cam.miss_rate
                det = ((x_seen + rnd.gauss(0, cam.pose_noise), 0.5, 0.15, 0.4)
                       if visible else None)
                tracker.process(det)
            run.t.append(t)
            run.x.append(0.5 + (script(t) - cam_a) / cam.fov)
            run.pan.append(visca.pan)
            run.cam_v.append(cam_v)
        t += phys
    run.learned = tracker.pilot.view()
    run.tracker = tracker
    return run


def _at(hist, t):
    # hist is time-ordered at a fixed step: index directly
    if not hist or t < hist[0][0]:
        return None
    step = hist[1][0] - hist[0][0] if len(hist) > 1 else 1.0
    return min(len(hist) - 1, int((t - hist[0][0]) / step))


# ── Scenarios (degrees) ────────────────────────────────────────

def walk_then_stop(dist=6.0, speed=4.0, start=2.0):
    return lambda t: 0.0 if t < start else min(dist, (t - start) * speed)


def pacing(amp=5.0, speed=3.0, pause=2.0, start=1.0):
    """Walks amp° one way, pauses, walks back, pauses, ... (a preacher pacing the stage)."""
    leg = amp / speed
    cycle = 2 * (leg + pause)

    def f(t):
        if t < start:
            return 0.0
        u = (t - start) % cycle
        if u < leg:
            return u * speed
        if u < leg + pause:
            return amp
        if u < 2 * leg + pause:
            return amp - (u - leg - pause) * speed
        return 0.0
    return f


def still_with_sway(sway=0.25, seed=1):
    rnd = random.Random(seed)
    phase = rnd.random() * 6.28
    return lambda t: 1.0 + sway * math.sin(0.9 * t + phase)


def fast_exit(speed=8.0, start=2.0, dur=1.5):
    return lambda t: 0.0 if t < start else min(dur, t - start) * speed
