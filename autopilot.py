"""
Trackmind Auto mode: a self-tuning pan/tilt controller.

The manual controller maps "how far off centre" straight to a VISCA speed.
That can't be right in every situation: the same speed 6 is a crawl on a wide
shot and a whip-pan on a tight one, and the ~0.3-0.5 s between a command and
seeing its effect means a speed that's fine for a still speaker overshoots a
walking one. Auto mode works in picture units instead and learns the rest:

  1. Ego-motion. Phase correlation between consecutive frames measures how
     fast the *background* moves, i.e. what the camera itself is doing.
  2. Loop model. Comparing that with the speeds we sent learns, live,
       gain    — frame widths per second per VISCA speed step (changes with zoom)
       latency — seconds from sending a command to seeing it on video
  3. Subject velocity. Image velocity minus ego-motion = how the person is
     really moving, independent of the camera.
  4. Prediction. The camera is steered toward where the subject will be once
     everything already commanded has shown up on video (a Smith predictor),
     which is what stops the overshoot-and-swing-back.
  5. Situations. Still / walking / fast, with hysteresis, each with its own
     dead zone, responsiveness and speed cap; a still speaker gets a calm,
     locked-off shot, a walking one gets matched pace.
  6. Hunting guard. If the camera reverses direction repeatedly it softens
     itself, and recovers once things are calm.

Pure Python + numpy (cv2 only for EgoMotion), so it is fully testable in
simulation — see tests/test_autopilot.py.
"""

import bisect
import math
from collections import deque

import numpy as np


# ─────────────────────────────────────────────────────────────
# Ego-motion: the camera's own movement, from the background
# ─────────────────────────────────────────────────────────────

class EgoMotion:
    """
    Global image shift between consecutive frames via phase correlation on a
    small greyscale copy (~1-2 ms per frame). The subject is a small part of
    the picture, so the dominant shift is the background — the camera's own
    motion. Returns background velocity in frame widths/heights per second.
    """
    SIZE = (320, 180)

    def __init__(self):
        self._prev = None
        self._t    = None
        self._win  = None

    def reset(self):
        self._prev = self._t = None

    def update(self, frame_bgr, t):
        import cv2
        g = cv2.resize(cv2.cvtColor(frame_bgr, cv2.COLOR_BGR2GRAY), self.SIZE,
                       interpolation=cv2.INTER_AREA).astype(np.float32)
        if self._win is None:
            self._win = cv2.createHanningWindow(self.SIZE, cv2.CV_32F)
        prev, pt = self._prev, self._t
        self._prev, self._t = g, t
        if prev is None or t <= pt or t - pt > 0.5 or prev.shape != g.shape:
            return None
        (dx, dy), conf = cv2.phaseCorrelate(prev, g, self._win)
        dt = t - pt
        return dx / self.SIZE[0] / dt, dy / self.SIZE[1] / dt, conf


# ─────────────────────────────────────────────────────────────
# Loop model: learns gain (per axis) and latency
# ─────────────────────────────────────────────────────────────

class LoopModel:
    """
    Model: background velocity b(t) = gain * speed(t - latency), per axis,
    where speed is the VISCA pan/tilt speed in effect (pan + = left, tilt + =
    up; with those conventions the background moves + in image x/y, so gains
    are positive). Fitted by weighted least squares over a sliding window,
    scanning candidate latencies for the best fit.
    """
    WINDOW     = 20.0     # s of ego samples kept
    HALF_LIFE  = 6.0      # s — recent samples count more (zoom changes the gain)
    FIT_EVERY  = 1.0      # s
    MIN_MOVING = 12       # samples with the camera moving needed for a fit
    MIN_R2     = 0.55     # fit quality needed to accept an estimate
    LAGS       = np.arange(0.05, 1.01, 0.025)
    GAIN_RANGE = (0.003, 1.5)

    def __init__(self, gain_pan=0.10, gain_tilt=0.10, latency=0.35):
        self.gain    = [gain_pan, gain_tilt]
        self.latency = latency
        self.fits    = 0          # accepted fits so far (0 = still using priors)
        self.r2      = None
        self._cmd_t  = [-1e9]
        self._cmd_u  = [(0, 0)]
        self._ego    = deque()
        self._next_fit = 0.0
        self._blackout = -1e9

    # Commands
    def on_command(self, t, pan, tilt):
        self._cmd_t.append(t)
        self._cmd_u.append((pan, tilt))
        while len(self._cmd_t) > 2 and self._cmd_t[1] < t - self.WINDOW - 2.0:
            self._cmd_t.pop(0); self._cmd_u.pop(0)

    def speed_at(self, t):
        return self._cmd_u[bisect.bisect_right(self._cmd_t, t) - 1]

    def commanded_shift(self, now, axis, horizon):
        """∫ gain·speed dτ over the last `horizon` s — motion sent but not yet seen."""
        g, t0 = self.gain[axis], now - horizon
        i = max(0, bisect.bisect_right(self._cmd_t, t0) - 1)
        total = 0.0
        for j in range(i, len(self._cmd_t)):
            a = max(t0, self._cmd_t[j])
            b = self._cmd_t[j + 1] if j + 1 < len(self._cmd_t) else now
            if b > a:
                total += self._cmd_u[j][axis] * (b - a)
        return g * total

    def blackout(self, until):
        """Motion we didn't command (preset recall, manual move): don't learn from it."""
        self._blackout = max(self._blackout, until)
        self._ego.clear()

    # Ego samples
    def on_ego(self, t, bx, by, conf):
        if t < self._blackout or conf < 0.25:
            return
        self._ego.append((t, bx, by))
        while self._ego and self._ego[0][0] < t - self.WINDOW:
            self._ego.popleft()
        if t >= self._next_fit:
            self._next_fit = t + self.FIT_EVERY
            self.fit(t)

    def fit(self, now):
        if len(self._ego) < 20:
            return False
        t = np.array([e[0] for e in self._ego])
        b = np.array([[e[1], e[2]] for e in self._ego])
        w = np.power(0.5, (now - t) / self.HALF_LIFE)
        ct = np.array(self._cmd_t)
        cu = np.array(self._cmd_u, dtype=float)

        # Which axis moved more decides the latency; both get a gain.
        best = None
        for lag in self.LAGS:
            idx = np.searchsorted(ct, t - lag, side="right") - 1
            u = cu[np.clip(idx, 0, len(cu) - 1)]
            res = []
            for ax in (0, 1):
                uu, bb = u[:, ax], b[:, ax]
                moving = np.count_nonzero(uu)
                suu = np.sum(w * uu * uu)
                if moving < self.MIN_MOVING or suu <= 0:
                    res.append(None)
                    continue
                g = np.sum(w * uu * bb) / suu
                sse = np.sum(w * (bb - g * uu) ** 2)
                sst = np.sum(w * bb * bb) + 1e-12
                res.append((g, 1 - sse / sst, sst))
            scored = [r for r in res if r]
            if not scored:
                continue
            score = sum(r[1] * r[2] for r in scored) / sum(r[2] for r in scored)
            if best is None or score > best[0]:
                best = (score, lag, res)
        if best is None:
            return False
        score, lag, res = best
        self.r2 = round(float(score), 3)
        if score < self.MIN_R2:
            return False
        a = 0.5 if self.fits < 3 else 0.25     # settle quickly, then smooth
        self.latency = (1 - a) * self.latency + a * float(lag)
        for ax in (0, 1):
            if res[ax] and res[ax][1] >= self.MIN_R2 and res[ax][0] > 0:
                g = min(max(float(res[ax][0]), self.GAIN_RANGE[0]), self.GAIN_RANGE[1])
                self.gain[ax] = (1 - a) * self.gain[ax] + a * g
        self.fits += 1
        return True

    def rescale(self, ratio):
        """Zoom changed the picture scale by `ratio` — gains scale with it."""
        ratio = min(2.5, max(0.4, ratio))
        self.gain = [g * ratio for g in self.gain]


# ─────────────────────────────────────────────────────────────
# Situations and styles
# ─────────────────────────────────────────────────────────────

# Per situation, in picture units:
#   dead    re-centre once the subject is this far off centre (frame fraction)
#   settle  ...and stop once back within this
#   k       responsiveness: fraction of the error removed per second
#   ff      how much of the subject's own velocity the camera matches
#   vmax    top camera speed, frame widths per second
#   accel   max change of camera speed, frame widths per second²
# Tuned in tests/camsim.py across wide/medium/tight shots and 0.3-0.5 s video
# delay (see tests/test_autopilot.py for the comparison against manual).
SITUATIONS = {
    "still":   dict(dead=0.12, settle=0.04, k=0.9, ff=0.0, vmax=0.25, accel=0.18),
    "walking": dict(dead=0.06, settle=0.03, k=1.3, ff=0.9, vmax=0.70, accel=0.36),
    "fast":    dict(dead=0.04, settle=0.02, k=1.8, ff=1.0, vmax=1.40, accel=0.78),
}
TILT_SCALE = dict(dead=1.3, k=0.7, ff=0.5, vmax=0.5)   # people rarely move vertically

STYLES = {
    "calm":       dict(speed=0.7, dead=1.3),
    "balanced":   dict(speed=1.0, dead=1.0),
    "responsive": dict(speed=1.35, dead=0.8),
}

WALK_ENTER, WALK_LEAVE = 0.07, 0.035   # subject speed, frame widths / s
FAST_ENTER, FAST_LEAVE = 0.40, 0.25
LEAVE_AFTER            = 0.8           # s below a threshold before stepping down
BRAKE                  = 2.5           # braking may be this much harder than accelerating
EDGE                   = 0.32          # |error| past this: catch up at "fast" limits...
EDGE_ACCEL             = 1.5           # ...accelerating up to this hard
CATCH_UP, CATCH_UP_REL  = 0.03, 0.20    # max gap-closing speed while following
REVERSE_HOLD           = 2.0           # s after a move during which...
REVERSE_DEAD           = 0.20          # ...reversing needs this much error
STEP_HOLD              = 0.8           # VISCA steps of change needed to switch step


class _Axis:
    VEL_WINDOW = 0.6      # s of positions the subject's velocity is fitted over

    def __init__(self, axis):
        self.axis   = axis
        self.active = 0       # re-centring direction, 0 = holding
        self.b_cmd  = 0.0     # commanded background velocity (picture units / s)
        self.q      = 0       # VISCA speed last output (integer, with hysteresis)
        self.ego_E  = 0.0     # accumulated background displacement
        self.ego_t  = None
        self.hist   = deque() # (t, x - ego_E): the subject in camera-independent terms
        self.w      = 0.0     # subject's own velocity, picture units / s
        self.b_ego  = 0.0     # latest background velocity (for diagnostics)
        self.last_side   = 0
        self.last_side_t = -1e9

    def reset(self):
        self.__init__(self.axis)

    def on_ego(self, t, b, ok):
        if not ok:
            # Can't follow the background (zooming, low confidence): restart
            # the velocity fit rather than mix two coordinate frames.
            self.hist.clear()
            self.ego_t = t
            return
        if self.ego_t is not None and t > self.ego_t:
            self.ego_E += b * min(0.5, t - self.ego_t)
        self.ego_t, self.b_ego = t, b

    def observe(self, t, x):
        """
        Subject velocity, independent of what the camera is doing: the image
        position minus how far the background has moved is the subject's
        position in a frame that doesn't pan with the camera. A straight-line
        fit over the last VEL_WINDOW s gives its velocity with no lag mismatch
        between subject and camera motion (which made the earlier estimate
        overshoot every time the camera sped up or slowed down).
        """
        self.hist.append((t, x - self.ego_E))
        while self.hist and self.hist[0][0] < t - self.VEL_WINDOW:
            self.hist.popleft()
        if len(self.hist) < 4 or self.hist[-1][0] - self.hist[0][0] < 0.25:
            return
        n = len(self.hist)
        mt = sum(h[0] for h in self.hist) / n
        ms = sum(h[1] for h in self.hist) / n
        num = sum((h[0] - mt) * (h[1] - ms) for h in self.hist)
        den = sum((h[0] - mt) ** 2 for h in self.hist)
        if den > 0:
            self.w = 0.5 * self.w + 0.5 * (num / den)


class AutoPilot:
    def __init__(self, gain_pan=0.10, gain_tilt=0.10, latency=0.35):
        self.model     = LoopModel(gain_pan, gain_tilt, latency)
        self.axes      = [_Axis(0), _Axis(1)]
        self.situation = "still"
        self._below_t  = None
        self._last_t   = None
        self.soften    = 1.0         # hunting guard multiplier (0.4 .. 1)
        self._reversals = deque()
        self._last_dir = 0
        self._zoom_h0  = None
        self._zoom_check = None

    # ── Inputs ────────────────────────────────────────────────

    def reset(self):
        for a in self.axes:
            a.reset()
        self.situation, self._below_t, self._last_t = "still", None, None
        self._reversals.clear()

    def on_command(self, now, pan, tilt):
        self.model.on_command(now, pan, tilt)
        d = (pan > 0) - (pan < 0)
        if d:
            if self._last_dir and d != self._last_dir:
                # Following a subject who turned around is not hunting. (Pan
                # + = left, so a subject moving right, w > 0, needs d = -1.)
                w = self.axes[0].w
                if not (abs(w) > WALK_LEAVE and d == -(1 if w > 0 else -1)):
                    self._reversals.append(now)
            self._last_dir = d

    def on_ego(self, now, bx, by, conf):
        self.model.on_ego(now, bx, by, conf)
        for a, b in zip(self.axes, (bx, by)):
            a.on_ego(now, b, conf >= 0.25)

    def blackout(self, now, seconds):
        self.model.blackout(now + seconds)

    def on_zoom(self, now, direction, h):
        """Zoom started (direction ±1) or stopped (0); h = subject height now."""
        if direction:
            if self._zoom_h0 is None:
                self._zoom_h0 = h
            self._zoom_check = None
            self.model.blackout(now + 0.6)
        elif self._zoom_h0:
            self._zoom_check = now + 0.6
            self.model.blackout(now + 0.6)

    # ── Control ───────────────────────────────────────────────

    def step(self, now, cx, cy, h, style="balanced"):
        """One detection. Returns (pan, tilt) VISCA speeds (pan + = left)."""
        dt = min(0.25, max(1e-3, now - self._last_t)) if self._last_t else None
        self._last_t = now
        st = STYLES.get(style, STYLES["balanced"])

        if self._zoom_check and now >= self._zoom_check and self._zoom_h0:
            self.model.rescale(h / self._zoom_h0)
            self._zoom_h0 = self._zoom_check = None

        for a, x in zip(self.axes, (cx, cy)):
            a.observe(now, x)
        self._classify(now, abs(self.axes[0].w))
        self._guard(now, dt or 0.0)
        if dt is None:
            return self.output()

        L = min(0.9, max(0.08, self.model.latency))
        return tuple(self._axis(a, x, L, dt, st, now) for a, x in zip(self.axes, (cx, cy)))

    def coast(self, now):
        """No detection this frame: ease the camera off."""
        dt = min(0.25, max(1e-3, now - self._last_t)) if self._last_t else 0.04
        self._last_t = now
        for a in self.axes:
            p = self._params(a)
            a.b_cmd = _approach(a.b_cmd, 0.0, p["accel"] * BRAKE * dt)
            a.active = 0
        return self.output()

    def output(self):
        return tuple(float(self._quantize(a)) for a in self.axes)

    # ── Internals ─────────────────────────────────────────────

    def _params(self, a, style=None):
        p = dict(SITUATIONS[self.situation])
        if a.axis == 1:
            p["dead"] *= TILT_SCALE["dead"]; p["settle"] *= TILT_SCALE["dead"]
            p["k"] *= TILT_SCALE["k"]; p["vmax"] *= TILT_SCALE["vmax"]
            p["ff"] *= TILT_SCALE["ff"]
        if style:
            p["dead"] *= style["dead"]; p["settle"] *= style["dead"]
            p["vmax"] *= style["speed"]; p["accel"] *= style["speed"]; p["k"] *= style["speed"]
        p["k"] *= self.soften; p["accel"] *= self.soften
        return p

    def _axis(self, a, x, L, dt, style, now):
        p = self._params(a, style)
        lead = min(1.0, abs(a.w) / WALK_ENTER)            # no lead for jitter
        x_pred = x + a.w * L * lead + self.model.commanded_shift(now, a.axis, L)
        e = x_pred - 0.5
        side = (e > 0) - (e < 0)

        # Swinging back the other way right after a move needs a much bigger
        # error: when the subject stops, ~0.5 s of motion is already in the
        # pipe (video delay + ramp-down), so a small overshoot is unavoidable.
        # Bouncing straight back is what looks like hunting; holding doesn't.
        dead = p["dead"]
        if side != a.last_side and now - a.last_side_t < REVERSE_HOLD:
            dead = max(dead, REVERSE_DEAD * style["dead"] * (TILT_SCALE["dead"] if a.axis else 1.0))

        if a.active == 0 and abs(e) > dead:
            a.active = side
        elif a.active and (side != a.active or abs(e) < p["settle"]):
            a.active = 0
        if a.active:
            a.last_side, a.last_side_t = a.active, now

        vmax, accel = p["vmax"], p["accel"]
        if abs(x - 0.5) > EDGE:
            # About to lose them off the edge: catch up at "fast" limits
            f = SITUATIONS["fast"]
            tilt = TILT_SCALE["vmax"] if a.axis else 1.0
            vmax  = max(vmax,  f["vmax"] * style["speed"] * tilt)
            accel = max(accel, EDGE_ACCEL * style["speed"] * tilt)
        fb = -p["k"] * e if a.active else 0.0
        if self.situation != "still":
            # Close the gap gently on top of matching the subject's pace.
            # Unlimited catch-up ran the camera at twice a walker's speed and
            # carried it past them when they stopped.
            catch = CATCH_UP + CATCH_UP_REL * abs(a.w)
            fb = max(-catch, min(catch, fb))
        ff = -p["ff"] * a.w if self.situation != "still" else 0.0
        if a.w * e < 0:
            # Camera is already ahead of the subject: let them catch up
            ff *= max(0.0, 1.0 - abs(e) / max(1e-6, p["dead"]))
        b_des = max(-vmax, min(vmax, fb + ff))

        braking = abs(b_des) < abs(a.b_cmd) or b_des * a.b_cmd < 0
        a.b_cmd = _approach(a.b_cmd, b_des, accel * (BRAKE if braking else 1.0) * dt)
        return float(self._quantize(a))

    def _quantize(self, a):
        """
        Float speed → VISCA step with hysteresis: start at 0.7, stop below
        0.3, and only change step when the wanted speed is STEP_HOLD away.
        Plain rounding flickered between neighbouring steps, which the camera
        shows as stop-start creeping or a pulsing pan.
        """
        g = self.model.gain[a.axis]
        u = a.b_cmd / g if g > 0 else 0.0
        mag = abs(u)
        cur = abs(a.q)
        if a.q == 0:
            q = int(round(mag)) if mag >= 0.7 else 0
        elif mag < 0.3:
            q = 0
        elif (u > 0) == (a.q > 0) and abs(mag - cur) < STEP_HOLD:
            q = cur                    # close enough: don't dither 5-6-5-6
        else:
            q = max(1, int(round(mag)))
        q = min(24, q)
        a.q = q if u >= 0 else -q
        return a.q

    def _classify(self, now, speed):
        s = self.situation
        up = "fast" if speed > FAST_ENTER else ("walking" if speed > WALK_ENTER else None)
        order = {"still": 0, "walking": 1, "fast": 2}
        if up and order[up] > order[s]:
            self.situation, self._below_t = up, None
            return
        floor = FAST_LEAVE if s == "fast" else WALK_LEAVE
        if s != "still" and speed < floor:
            self._below_t = self._below_t or now
            if now - self._below_t >= LEAVE_AFTER:
                self.situation = "walking" if s == "fast" and speed > WALK_LEAVE else "still"
                self._below_t = None
        else:
            self._below_t = None

    def _guard(self, now, dt):
        while self._reversals and self._reversals[0] < now - 8.0:
            self._reversals.popleft()
        if len(self._reversals) >= 2:
            self.soften = max(0.4, self.soften * 0.75)
            self._reversals.clear()
        else:
            self.soften = min(1.0, self.soften + 0.03 * dt)   # back to full in ~20 s

    def view(self):
        m = self.model
        return {
            "situation": self.situation,
            "learned":   m.fits > 0,
            "gain_pan":  round(m.gain[0], 4),
            "gain_tilt": round(m.gain[1], 4),
            "latency":   round(m.latency, 3),
            "fit_r2":    m.r2,
            "soften":    round(self.soften, 2),
        }


def _approach(cur, target, d):
    if target > cur:
        return min(target, cur + d)
    return max(target, cur - d)
