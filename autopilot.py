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
  4. Delay compensation. The camera is steered by where the subject is seen,
     corrected for moves already sent but not yet on video (a Smith
     predictor), which stops the overshoot-and-swing-back. It doesn't try to
     predict where the subject is going: the camera starts when they do, runs
     at the pace they are measured to be walking, and aims only slightly
     ahead (LEAD). When they stop it always centres them (RECENTRE_*).
  5. Situations. Still / walking / fast, with hysteresis, each with its own
     dead zone, responsiveness and speed cap; a still speaker gets a calm,
     locked-off shot, a walking one gets matched pace.
  6. Hunting guard. If the camera reverses direction repeatedly it softens
     itself, and recovers once things are calm.
  7. Zoom (ZoomPilot). Frames first, zooms once the subject settles; eases
     in proportionally, stops early for the delay, learns the zoom rate.

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
    "walking": dict(dead=0.06, settle=0.03, k=1.3, ff=1.0, vmax=0.70, accel=0.36),
    "fast":    dict(dead=0.04, settle=0.02, k=1.8, ff=1.0, vmax=1.40, accel=0.78),
}
TILT_SCALE = dict(dead=1.3, k=0.7, ff=0.5, vmax=0.5)   # people rarely move vertically

STYLES = {
    "calm":       dict(speed=0.7, dead=1.3),
    "balanced":   dict(speed=1.0, dead=1.0),
    "responsive": dict(speed=1.35, dead=0.8),
}

WALK_ENTER, WALK_LEAVE = 0.06, 0.03   # subject speed, frame widths / s
FAST_ENTER, FAST_LEAVE = 0.40, 0.25
LEAVE_AFTER            = 0.8           # s below a threshold before stepping down
BRAKE                  = 2.5           # braking may be this much harder than accelerating
EDGE                   = 0.32          # |error| past this: catch up at "fast" limits...
EDGE_ACCEL             = 1.5           # ...accelerating up to this hard
CATCH_UP, CATCH_UP_REL = 0.004, 0.03   # fw/s (+ fraction of his pace) the camera may exceed his speed by to close a gap
LAT_REF, LAT_SCALE_MAX = 0.40, 2.0     # delay the limits were tuned at; most they scale up for more
W_MAX                  = 0.75          # fastest believable subject, frame widths / s
W_FRESH, FRESH_SPAN    = 0.35, 0.5     # until VEL fit spans FRESH_SPAN s after a reset, |w| <= W_FRESH
LEAD                   = 0.35          # fraction of (his speed x video delay) aimed ahead of him: a slight look-ahead
LEAD_MAX               = 0.05          # ...and never more than this far (frame fraction)
RECENTRE_DEAD          = 0.035         # once he has stopped, centre him if he is this far off...
RECENTRE_SETTLE        = 0.015         # ...and stop when within this (frame fraction)
RECENTRE_TIME          = 8.0           # s to keep trying after he stops before giving up (camera at its limit)
REVERSE_HOLD           = 2.0           # s after a move during which...
REVERSE_DEAD           = 0.20          # ...reversing needs this much error
STEP_HOLD              = 0.8           # VISCA steps of change needed to switch step
COAST_HOLD             = 0.2           # s a missed detection keeps the camera at its speed


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
        self.first_t = None   # first observation since the last reset
        self.w      = 0.0     # subject's own velocity, picture units / s
        self.b_ego  = 0.0     # latest background velocity (for diagnostics)
        self.last_side   = 0
        self.last_side_t = -1e9
        self.recentre    = False  # he has been on the move: centre him once he stops
        self.still_t     = None   # when he stopped

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

        The result is capped at a believable pace, and held to walking pace
        for the first half second after a reset: a fit over a few frames of a
        subject just re-found at the edge of the picture (half the body cut
        off, filters restarting) once read 2.4 frame widths/s and whip-panned
        the camera at speed 16 past the speaker.
        """
        if self.first_t is None:
            self.first_t = t
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
            lim = W_FRESH if t - self.first_t < FRESH_SPAN else W_MAX
            self.w = max(-lim, min(lim, 0.5 * self.w + 0.5 * (num / den)))


# ─────────────────────────────────────────────────────────────
# Zoom
# ─────────────────────────────────────────────────────────────

ZOOM_RATE_PRIOR  = 0.06    # ln(fill) per second per (VISCA zoom speed + 1); learned live
ZOOM_RATE_RANGE  = (0.01, 0.4)
ZOOM_SETTLE      = 0.35    # stop within this fraction of the dead band from the target
ZOOM_TIME        = 2.0     # s a zoom aims to take to close the gap (it slows as it nears)
ZOOM_IN_STILL    = 1.0     # s the subject must be settled and centred before zooming in
ZOOM_IN_CENTRE   = 0.12    # ...within this of centre (frame fraction)
ZOOM_OUT_AFTER   = 0.3     # s too big before zooming out
ZOOM_COOLDOWN    = 1.5     # s after a zoom before the next one
ZOOM_SPEED_HOLD  = 0.4     # s between zoom speed changes
ZOOM_FAST_WIDEN  = 0.8     # a fast-moving subject gets a wider target
ZOOM_LIMIT_AFTER = 1.2     # s (after latency) with little change in size = camera at its zoom limit
ZOOM_PROGRESS    = 1.2     # s window the zoom's progress is measured over
ZOOM_LIMIT_HOLD  = 15.0    # s before trying that direction again
FILL_SMOOTH      = 0.4     # s time constant of the size reading


class ZoomPilot:
    """
    Auto mode's zoom. The manual zoom runs at one fixed speed until the size
    crosses a band edge; with ~0.4 s of video delay that always overshoots,
    and it zooms whenever the size reading twitches. Here, in log units of
    the subject's size (so zooming 2× is the same step at any focal length):

      * Zoom in only once the subject has settled, centred, with the camera
        still — never chase a walking subject with zoom. Zoom out promptly
        when they're too big, and aim wider while they move fast.
      * Speed proportional to the distance from target, so it eases in.
      * Stop early by what the delay will still add (measured rate × latency).
      * Learn how fast this camera zooms per speed step, live.
      * Notice the end of the zoom range and stop asking for more.
    """

    def __init__(self, rate=ZOOM_RATE_PRIOR):
        self.rate = rate
        self._learned = 0        # rate observations so far (learn fast at first)
        self.reset()

    def reset(self):
        """Forget the subject (keeps the learned zoom rate)."""
        self.dir = 0
        self.speed = 0
        self.f = None            # smoothed ln(fill)
        self._f_t = None
        self._hist = deque()     # (t, ln fill) for the rate of change
        self._started = None
        self._speed_t = -1e9
        self._stopped = -1e9
        self._in_since = None
        self._out_since = None
        self._limit = {1: -1e9, -1: -1e9}

    def _observe(self, now, fill):
        lf = math.log(max(1e-3, fill))
        if self.f is None or now - self._f_t > 1.0:
            self.f = lf
        else:
            a = 1.0 - math.exp(-(now - self._f_t) / FILL_SMOOTH)
            self.f += a * (lf - self.f)
        self._f_t = now
        self._hist.append((now, lf))
        while self._hist and self._hist[0][0] < now - ZOOM_PROGRESS:
            self._hist.popleft()

    def measured_rate(self, since=None):
        """d ln(fill) / dt over the last ZOOM_PROGRESS s, or since `since` (+ = growing = zooming in)."""
        h = [x for x in self._hist if since is None or x[0] >= since]
        if len(h) < 6 or h[-1][0] - h[0][0] < 0.5 * ZOOM_PROGRESS:
            return None
        n = len(h)
        mt = sum(x[0] for x in h) / n
        mf = sum(x[1] for x in h) / n
        den = sum((x[0] - mt) ** 2 for x in h)
        return sum((x[0] - mt) * (x[1] - mf) for x in h) / den if den > 0 else None

    def _speed_for(self, gap, max_speed):
        want = gap / ZOOM_TIME                     # ln units / s
        return int(min(max_speed, max(0, round(want / self.rate) - 1)))

    def step(self, now, fill, cx, situation, camera_moving, target, dead, max_speed,
             latency, style=None):
        """
        One detection. fill = subject size (frame fraction); returns
        (direction, speed): +1 zoom in, -1 zoom out, 0 stop; VISCA speed 0-7.
        """
        self._observe(now, fill)
        dead_k = (style or {}).get("dead", 1.0)
        if situation == "fast":
            target *= ZOOM_FAST_WIDEN
        e = self.f - math.log(target)
        dead_ln = math.log(1.0 + dead / target) * dead_k
        settle = ZOOM_SETTLE * dead_ln
        L = latency
        centred = abs(cx - 0.5) < ZOOM_IN_CENTRE
        calm = situation == "still" and not camera_moving

        if self.dir:
            running = now - self._started
            expected = self.rate * (self.speed + 1)      # ln units / s at this speed
            # Where the size ends up if we stop now: what's in the pipe (the
            # learned rate × latency and the size smoothing's lag; the
            # measured rate is too noisy to stop on)
            e_pred = e + self.dir * expected * (L + FILL_SMOOTH)
            if self.dir > 0:
                done = e_pred >= -settle
                # Small pan corrections while zooming in are fine (zooming
                # magnifies any offset); the subject moving off isn't.
                abort = situation != "still" or abs(cx - 0.5) > 1.5 * ZOOM_IN_CENTRE
            else:
                done = e_pred <= settle
                abort = False
            # Progress since the zoom (or this speed) has had time to show on video
            r = self.measured_rate(since=self._started + L + 0.2)
            stuck = (running > L + ZOOM_LIMIT_AFTER and r is not None
                     and r * self.dir < 0.25 * self.rate)
            if stuck:
                self._limit[self.dir] = now
            if done or abort or stuck:
                return self.stop(now)
            r = self.measured_rate(since=self._speed_t + L + 0.2)
            if r is not None and r * self.dir > 0:          # learn this camera's zoom rate
                obs = min(ZOOM_RATE_RANGE[1], max(ZOOM_RATE_RANGE[0], abs(r) / (self.speed + 1)))
                a = max(0.05, 0.3 / (1 + self._learned / 10))
                self.rate += a * (obs - self.rate)
                self._learned += 1
            sp = self._speed_for(max(0.0, abs(e_pred) - settle), max_speed)
            if sp != self.speed and now - self._speed_t >= ZOOM_SPEED_HOLD:
                self.speed, self._speed_t = sp, now
            return self.dir, self.speed

        # Idle
        self._out_since = (self._out_since or now) if e > dead_ln else None
        self._in_since = (self._in_since or now) if (e < -dead_ln and calm and centred) else None
        if now - self._stopped < ZOOM_COOLDOWN:
            return 0, 0
        if self._out_since and now - self._out_since >= ZOOM_OUT_AFTER \
                and now - self._limit[-1] > ZOOM_LIMIT_HOLD:
            return self._start(now, -1, abs(e) - settle, max_speed)
        if self._in_since and now - self._in_since >= ZOOM_IN_STILL \
                and now - self._limit[1] > ZOOM_LIMIT_HOLD:
            return self._start(now, 1, abs(e) - settle, max_speed)
        return 0, 0

    def _start(self, now, d, gap, max_speed):
        self.dir, self.speed = d, self._speed_for(gap, max_speed)
        self._started = self._speed_t = now
        self._in_since = self._out_since = None
        return self.dir, self.speed

    def stop(self, now):
        """Zoom stopped (by us or by something else: preset, lost subject)."""
        self.dir, self.speed = 0, 0
        self._stopped = now
        return 0, 0

    def view(self):
        return {"zoom_rate": round(self.rate, 4), "zoom_dir": self.dir, "zoom_speed": self.speed,
                "fill": round(math.exp(self.f), 3) if self.f is not None else None}


class AutoPilot:
    def __init__(self, gain_pan=0.10, gain_tilt=0.10, latency=0.35, zoom_rate=ZOOM_RATE_PRIOR):
        self.model     = LoopModel(gain_pan, gain_tilt, latency)
        self.axes      = [_Axis(0), _Axis(1)]
        self.zoom      = ZoomPilot(zoom_rate)
        self.situation = "still"
        self._below_t  = None
        self._last_t   = None
        self.soften    = 1.0         # hunting guard multiplier (0.4 .. 1)
        self._reversals = deque()
        self._last_dir = 0
        self._zoom_h0  = None
        self._zoom_check = None
        self._gap_t    = None        # when the current run of missed detections began

    # ── Inputs ────────────────────────────────────────────────

    def reset(self):
        for a in self.axes:
            a.reset()
        self.situation, self._below_t, self._last_t = "still", None, None
        self._gap_t = None
        self._reversals.clear()
        self.zoom.reset()

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

    def step(self, now, cx, cy, h, style="balanced", max_step=(24, 24), adapt=True, recentre=True):
        """
        One detection. Returns (pan, tilt) VISCA speeds (pan + = left), never
        faster than max_step (the profile's Fast speeds) per axis. adapt=False
        turns off following harder for a longer measured delay and the
        predicted-position edge check (Settings → "Adapt to video delay").
        recentre=False leaves a speaker who has stopped wherever they are
        inside the calm dead zone (Settings → "Centre after moving").
        """
        dt = min(0.25, max(1e-3, now - self._last_t)) if self._last_t else None
        self._last_t = now
        self._gap_t = None
        st = STYLES.get(style, STYLES["balanced"])

        if self._zoom_check and now >= self._zoom_check and self._zoom_h0:
            self.model.rescale(h / self._zoom_h0)
            self._zoom_h0 = self._zoom_check = None

        for a, x in zip(self.axes, (cx, cy)):
            a.observe(now, x)
        self._classify(now, abs(self.axes[0].w))
        for a in self.axes:
            if self.situation != "still" and abs(a.w) >= WALK_LEAVE:
                a.recentre, a.still_t = True, None
            elif a.recentre and a.still_t is None:
                a.still_t = now          # he has stopped
        self._guard(now, dt or 0.0)
        if dt is None:
            return self.output()

        L = min(0.9, max(0.08, self.model.latency))
        return tuple(self._axis(a, x, L, dt, st, now, m, adapt, recentre)
                     for a, x, m in zip(self.axes, (cx, cy), max_step))

    def coast(self, now):
        """
        No detection this frame. A dropped frame or two (a turn, a gesture
        across the torso) doesn't mean the speaker stopped, so the camera
        keeps its speed for COAST_HOLD s, then eases off.
        """
        dt = min(0.25, max(1e-3, now - self._last_t)) if self._last_t else 0.04
        self._last_t = now
        if self._gap_t is None:
            self._gap_t = now
        if now - self._gap_t < COAST_HOLD:
            return self.output()
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

    def _axis(self, a, x, L, dt, style, now, max_step=24, adapt=True, recentre=True):
        p = self._params(a, style)
        gate = min(1.0, abs(a.w) / WALK_ENTER)            # no lead for jitter
        shift = self.model.commanded_shift(now, a.axis, L)
        # Where the camera aims: only slightly ahead of him
        x_pred = x + max(-LEAD_MAX, min(LEAD_MAX, LEAD * a.w * L * gate)) + shift
        e = x_pred - 0.5
        # Where he will really be once the video delay has played out: only
        # used to see that he is about to leave the picture, never to aim
        e_edge = x + a.w * L * gate + shift - 0.5
        # The longer the measured video delay, the further behind the picture
        # the camera is, so a moving subject is followed with proportionally
        # harder acceleration and catch-up (1x at LAT_REF, up to LAT_SCALE_MAX).
        lat = min(LAT_SCALE_MAX, max(1.0, L / LAT_REF)) if adapt else 1.0
        edge_k = 1.0 if a.axis == 0 else TILT_SCALE["dead"]
        side = (e > 0) - (e < 0)

        # He has stopped after being on the move: centre him properly. A
        # speaker who only sways keeps the wide, calm dead zone; one who just
        # walked somewhere doesn't get left off to the side of it.
        tight = recentre and a.recentre and a.still_t is not None
        if tight:
            if now - a.still_t > RECENTRE_TIME:
                a.recentre = tight = False
            else:
                sc = style["dead"] * (TILT_SCALE["dead"] if a.axis else 1.0)
                p["dead"] = min(p["dead"], RECENTRE_DEAD * sc)
                p["settle"] = min(p["settle"], RECENTRE_SETTLE * sc)

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
        elif tight and abs(e) < p["settle"]:
            a.recentre = False                            # centred: back to the calm hold

        vmax, accel = p["vmax"], p["accel"]
        if self.situation != "still":
            accel *= lat
        # Edge check on where the subject will be once the delay has played
        # out, not where the delayed picture shows them: with ~0.65 s of delay,
        # by the time the picture showed a fast walker near the edge they were
        # already half out of it.
        if max(abs(x - 0.5), abs(e_edge) if adapt else 0.0) > EDGE * edge_k:
            # About to lose them off the edge: catch up at "fast" limits
            f = SITUATIONS["fast"]
            tilt = TILT_SCALE["vmax"] if a.axis else 1.0
            vmax  = max(vmax,  f["vmax"] * style["speed"] * tilt)
            accel = max(accel, EDGE_ACCEL * style["speed"] * tilt * lat)
        fb = -p["k"] * e if a.active else 0.0
        if tight and a.active:
            # A small error asks for less than one speed step; give it one so the move happens
            fb = -a.active * max(abs(fb), 0.8 * self.model.gain[a.axis])
        if self.situation != "still" and not tight:
            # Match his pace and nothing more. Closing the gap faster than that
            # ran the camera ahead of the walker and carried it past them when
            # they stopped; the gap closes when he slows or stops. (A longer
            # measured video delay allows a proportionally bigger gap-closing.)
            catch = (CATCH_UP + CATCH_UP_REL * abs(a.w)) * lat
            fb = max(-catch, min(catch, fb))
        ff = -p["ff"] * a.w if self.situation != "still" else 0.0
        if a.w * e < 0:
            # Camera is already ahead of the subject: let them catch up
            ff *= max(0.0, 1.0 - abs(e) / max(1e-6, p["dead"]))
        g = self.model.gain[a.axis]
        if g > 0:
            vmax = min(vmax, max_step * g)               # the profile's Fast speed is a hard limit
        b_des = max(-vmax, min(vmax, fb + ff))

        braking = abs(b_des) < abs(a.b_cmd) or b_des * a.b_cmd < 0
        a.b_cmd = _approach(a.b_cmd, b_des, accel * (BRAKE if braking else 1.0) * dt)
        q = self._quantize(a)
        if abs(q) > max_step:
            a.q = q = max_step if q > 0 else -max_step
        return float(q)

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
            **self.zoom.view(),
        }


def _approach(cur, target, d):
    if target > cur:
        return min(target, cur + d)
    return max(target, cur - d)
