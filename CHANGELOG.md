# Changelog

## v1.8.3 — 2026-10-04

### Pulpit anchor
- **The hold zone is now a box in the upper part of the picture**, not a full-height band. While the pulpit shot is held, the speaker's head has to stay in it. **Hold zone height** (Settings → Pulpit, per profile) sets how far down it reaches; it defaults to the top 50%. Leaning and gesturing keep the shot; walking down off the platform or sitting down releases it. The amber outline on the video shows the box.

### GPU processing
- **Pose detection runs on the GPU.** It uses RTMO (OpenMMLab) on ONNX Runtime + DirectML, so any DirectX 12 GPU works (NVIDIA, AMD or Intel) with no CUDA to install. The GPU with the most video memory is chosen automatically. MediaPipe can't use the GPU on Windows, so it used to compete with vMix for the CPU.
- **Faster, lighter.** On an RTX 2060 that vMix was also using, live tracking went from 6.4 to 13.7 frames per second, with half the CPU time per frame. When the GPU isn't busy, the model takes about 25 ms per frame (MediaPipe on the CPU took about 95 ms). Image conversion was moved into the model so it runs on the GPU too.
- **The lock sees everyone.** The GPU model detects every person in the frame. Unlocked tracking stays on the person it's following instead of hopping to whoever looks most prominent, and the lock picks the person nearest where the subject is heading, of similar size.
- **Hardware video decoding** of the camera stream when the driver supports it.
- **Always a fallback.** With no usable GPU, or if the GPU fails mid-service, Trackmind switches to MediaPipe on the CPU and keeps tracking.
- **Settings → Advanced → Processing:** Auto / GPU / CPU, plus the engine, device and time per frame in use.

### Stream Deck
- **New Auto Mode key** (plugin v1.2.0). It toggles Auto mode on or off, can set a style (Calm, Balanced or Responsive) on every press, and shows what Auto is doing: Steady, Following, Catching up, or Learning. A green corner light shows while it's active, amber while it's learning or has eased off. Needs Trackmind v1.8.2 or newer.

## v1.8.2 — 2026-09-30

### Auto mode
- **New Auto button** (key `A`, Stream Deck/API `auto`). Trackmind sets the speed, dead zones and smoothing itself, to suit the shot and what the speaker is doing. There are no speeds to tune per zoom or per room.
- **Learns your camera.** It measures the camera's own motion from the video (the background shifting between frames) and compares it with the speeds it sent. From that it learns how far each speed step moves the picture at the current zoom, and the real video delay. It keeps learning, re-adjusts after zooming, and remembers what it learned.
- **No overshoot from video delay.** It steers toward where the speaker will be once the moves already sent show up on video.
- **Situations.** *Steady* for a still speaker (wide dead zone, gentle re-centring), *Following* for walking (matches the speaker's pace and closes the gap gently), and *Catching up* only when the speaker is near the edge of the frame or moving fast.
- **No bounce.** After a move it holds rather than swinging back for a small overshoot. If the camera ever starts going back and forth, it eases itself off, and turnarounds by a pacing speaker don't count.
- **Styles:** Calm, Balanced, Responsive.
- In simulation across wide, medium and tight shots with 0.3–0.5 s video delay, Auto framed slightly better than a well-tuned manual profile, with about 5× fewer stop-starts. `tests/test_autopilot.py` checks this on every build.

## v1.8.1 — 2026-09-30

### Smoother tracking
- **No more parking on the dead-zone edge.** The camera used to stop the moment the subject crossed into the dead zone, leaving them on its edge where every small step restarted it: stop-start hunting. A move now carries on, easing down to a crawl, until the subject is near center. It also ends if the subject crosses center, so an overshoot doesn't turn into a swing back.
- **Brakes faster than it accelerates.** The smoothing ramp used to apply equally to slowing down. Combined with video delay, that carried the camera past the subject.
- **Missed detections don't jolt the camera.** A single dropped frame (a turn, an occlusion, motion blur) used to send a stop and restart the speed ramp from zero. The camera now eases off for up to 0.45 s while the subject is re-found.
- **Gestures don't steer the camera.** The aim point and auto-zoom fill now come from the torso (shoulders and hips) instead of a box that included the elbows, and an adaptive filter removes pose jitter.
- **Auto-zoom zooms to the target** instead of stopping at the edge of the zoom dead band.

### Lock-on
- **Another person can't steal the lock.** MediaPipe Pose only reports one person. Previously a more prominent person took the detection and the locked subject became invisible. While locked, pose now runs on a crop around the subject.
- **A fast exit no longer leaves the lock stuck.** It used to search only within 25% of the frame around where the subject was last seen, forever, until LOCK was toggled off. It now searches where the subject was heading and widens the search over time to the whole frame. After the camera returns home, the lock re-acquires.

### Diagnostics and tests
- **Tracking flight recorder** (`diagnostics.py`). Records detections and camera commands to daily files with 30-day retention. `python diagnostics.py` reports hunting, stop-start stutter, high speed, detection dropouts, lock rejections, stream drops and low fps, each with a suggested fix.
- **Test suite** (`tests/`, run with `python -m pytest tests`). Covers the speed curve, smoothing, dropouts, a closed-loop simulation with video latency, lock behaviour (including a fast exit), and diagnostics retention. Runs in CI on every push.

## v1.8 — 2026-09-24

### Pulpit anchor
- **Snap to the pulpit.** When the speaker steps behind the pulpit, the dead zone can leave the camera resting slightly off-centre. Turn on **Settings → Pulpit**, choose the pulpit preset (e.g. 5) and click **Learn pulpit**. Trackmind recalls the preset and asks the camera where it landed. From then on, whenever the camera comes to rest near that spot, Trackmind recalls the preset and holds the shot. Gestures and leaning won't move it. Tracking resumes as soon as the speaker walks out of the hold zone.
- **Per profile.** Each profile keeps its own anchor (on/off, preset, learned position and tuning), so Sunday AM and Wednesday can use different pulpits, or none. Anchor changes save straight into the active profile. Profiles saved before this version load with the anchor off.
- **Tuning.** *Snap range* sets how close the camera must be, and Settings shows the live distance while tracking. *Settle time* sets how long it must sit still, so walking past the pulpit won't trigger a snap. *Hold zone* sets how far the speaker can move before tracking takes over again.
- **Snap style.** *Recall preset* jumps to the preset (zoom included). *Glide* pans to the learned position at an adjustable speed and keeps the current zoom, for a snap that's barely noticeable on air.
- **On screen.** A **Holding on pulpit** chip shows over the preview while the shot is held, and an amber band outlines the hold zone. You can turn the band off in Settings → Advanced → Pulpit hold zone.
- **Stream Deck.** New **Pulpit Anchor** key: toggle / on / off, showing off, not learned, armed, in range, or holding.
- **Control API.** New `anchor` toggle, and an `anchor` block in `/api/status`.
- Requires a camera that answers the VISCA pan/tilt position inquiry (PTZOptics does).

### Auto-update fixed
- **Updates install again.** Since v1.4, clicking *Install & restart* closed Trackmind and nothing else happened. The installer closed the running app with `taskkill /T`, which also kills that app's child processes. An installer launched through the admin (UAC) prompt counts as a child of the app that started it, so the installer was killing itself before it copied anything. It now waits up to 6 s for Trackmind to close by itself (saving your settings), then force-closes only Trackmind.
- **Survives antivirus locks.** The old `Trackmind.exe` is renamed out of the way instead of deleted, which works even while antivirus is scanning it. If the update still can't be applied, the old version is restored and reopened, with a message saying why.
- **Reopens as you.** After an update, Trackmind now starts as the signed-in user instead of inheriting the installer's admin rights. The same applies to "Launch Trackmind now" at the end of a manual install.
- **The admin prompt comes to the front** instead of sometimes flashing unseen in the taskbar. Trackmind also always exits after starting the installer, so a stuck background task can't hold up the update.
- Updating from v1.4–v1.7 uses the new installer, so the fix applies to this update too.

### Stream Deck
- **Keys fill the whole button.** The glass pane used to sit inset in the key, leaving a visible black border and a smaller picture. It now runs edge to edge, with larger icons and text. Stream Deck + touch-strip segments are full-bleed too, so there are no black gaps along the strip.

### Branding
- **App icon edges fixed.** The large icon sizes had a half-transparent rim that showed as a white fringe on light backgrounds (Explorer, taskbar, installer). The edge is now fully opaque. The Stream Deck marketplace icon and the in-app icon are fixed too.
- The generated logo, icon and installer art moved from the repo root to `logos/`.

---

## v1.7 — 2026-09-23

### A brand-new Trackmind
- **Rebuilt interface.** The live camera now fills the window, and every control floats over the picture as **liquid glass**: real refraction, specular rims, and tally-colored tints, in the vMix language of green = tracking, amber = lock, red = on air. The window is a native WebView2 window. Run with `--browser` to use it in a web browser instead.
- **Tracking overlay.** A reticle follows the detected subject (green, turning amber when locked), with a dead-zone guide and a "zooming" indicator.
- **Control dock.** Large Tracking, Lock, Auto-Zoom and Home buttons, with live status under each.
- **Live tune.** A floating panel for adjusting vertical aim, smoothing and zoom target, or switching profile, mid-service.
- **Settings sheet.** Camera, Profiles, Tracking, Zoom, Advanced, Stream Deck and About sections. Every change applies and saves immediately; there's no Apply button. You can now also choose the RTSP stream (Main 1080p or Sub 720p).
- **New setup wizard, update prompts, and connection states.** Covers "connecting", "can't reach the camera" (with Reconnect), and "add your camera". Updates show release notes and live download progress.
- **Keyboard shortcuts:** `T` tracking, `L` lock, `Z` auto-zoom, `H` home, `U` live tune, `F` full screen, `,` settings.
- **New logo and app icon.** A tracking frame holding a tally-green subject, whose green dot is also the dot of the "i" in the wordmark. The icon, installer art and README banner are all generated from `branding/`.

### Stream Deck
- **New plugin.** It covers Tracking, Lock Subject, Recall Preset (glows red while on air), Home Preset, Auto-Zoom, Pan/Tilt and Zoom hold keys, Load Profile, Motion Sync, a live Status tile, and a Stream Deck + **Tracking Dial** for live tuning. Keys are rendered as liquid glass, with the same optics as the app. Install by downloading `Trackmind-StreamDeck-v1.7.streamDeckPlugin` from this release and double-clicking it. See `streamdeck/README.md`.

### Control API
- **Local JSON HTTP API** on `127.0.0.1:8742`. It drives tracking, lock, presets, profiles, manual PTZ and live values. The Stream Deck plugin uses it, and so can Companion, vMix scripts and anything else that speaks HTTP. Manual moves have a built-in dead-man switch, so they stop by themselves if the controller goes quiet. External clients can never read the camera login or settings. Full reference in `docs/API.md`.

### Under the hood
- Tkinter and Pillow are gone. The tracking engine is unchanged, and now also serves the interface, the live preview (MJPEG) and state updates (Server-Sent Events) from one local-only server.
- Loading a profile with a different camera now reconnects the stream automatically.

---

## v1.6 — 2026-06-21

### Fixes
- **Auto-update file-overwrite race condition fixed.** The NSIS installer previously relied on a fixed 2-second sleep after `taskkill`, which was not always enough time for Windows Defender or other AV software to release its lock on the killed process's EXE. NSIS would then silently skip the file copy, relaunch the *old* binary, and the update appeared to do nothing. The installer now confirms the lock is released by repeatedly attempting to delete the old EXE (up to 10 retries × 1 second each = 10 s max) before copying the new one, and explicitly aborts with an error message if the copy still fails.

---

## v1.5 — 2026-06-21

### Fixes
- **Hardened the auto-updater.** The downloaded installer now has its Mark-of-the-Web (the "downloaded from the internet" tag) stripped before launch, so Windows SmartScreen no longer silently blocks the silent/elevated install. The updater also verifies the download finished (catching truncated/empty files) and now reports a clear reason when an update can't start — including when the User Account Control prompt is declined — instead of failing silently.

---

## v1.4 — 2026-06-21

### Fixes
- **Auto-update now actually applies.** The updater launched the silent installer while the old app was still running, so Windows refused to overwrite the locked `.exe` — the install failed quietly and reopening still showed the old version. The installer now force-closes the running app before copying files, then relaunches the updated app when it finishes a silent (auto-update) install. The app also shows an "updating — app will restart" status and exits cleanly so the file unlocks.
- Fixed a Start Menu shortcut that pointed at a folder the installer never created.

---

## v1.3 — 2026-06-21

### Changes
- **Smoother tracking motion.** The pan/tilt controller now uses continuous proportional speed instead of snapping between discrete slow/fast steps, and ramps acceleration and deceleration so the camera eases into and out of moves rather than jerking. The result is noticeably more cinematic following.
  - New **Motion Smoothing** setting (0–10, default 5) under MOVEMENT tunes how gentle the accel/decel ramps are. 0 restores the old instant behavior; 10 is silkiest (slightly slower to react).
- Added **Motion Sync** setting — when enabled, the PTZOptics camera scales each axis's speed so pan, tilt, and zoom all reach a recalled preset at the same moment, producing smooth, coordinated motion instead of one axis finishing early. Toggle it in the settings panel under the new **Motion Sync** section. The state is saved per profile and applied to the camera on connect.

---

## v1.2 — 2026-06-03

### Fixes
- Settings panel: pressing Enter or clicking away from any field now immediately applies and saves values — no more needing to click Apply
- Version display: app no longer shows "unknown" when run from certain working directories
- Installer: version is now read automatically from `version.txt` — no more hardcoded version strings to forget to update

### Changes
- GitHub Actions workflow added — releases are now built automatically on Windows when a version tag is pushed

---

## v1.1

### Changes
- Redesigned settings panel with scrollable single-column layout
- Added user profiles — save and load sets of tracking settings by name
- Added auto-updater — checks GitHub releases on launch and can download/install updates
- Dynamic versioning via `version.txt`
- App icon and NSIS installer

---

## v1.0

Initial release.
- MediaPipe Pose detection for PTZOptics cameras over RTSP
- VISCA over IP pan/tilt/zoom control
- Auto-tracking with dead zone, slow/fast speed zones, and velocity prediction
- Lock-on mode to ignore other people in frame
- Auto-zoom to keep subject filling the frame
- First-run setup wizard
