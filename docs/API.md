# Trackmind Control API

Trackmind runs a small JSON-over-HTTP API so other tools can drive tracking. It powers the [Stream Deck plugin](../streamdeck/README.md), and you can call it yourself from Bitfocus Companion, vMix scripts, AutoHotkey, `curl`, or anything else that can make an HTTP request.

- **Base URL:** `http://127.0.0.1:8742/api/`
- **Enable / change port:** Trackmind → **Settings → Stream Deck → Control API** (on by default, port `8742`)
- **Format:** JSON in, JSON out. Every response includes `"ok": true|false`.

---

## Security model

The API only listens on **127.0.0.1**, so nothing on your network can reach it; only programs on the same PC can. Browsers are locked out too:

- Requests must carry a `Host` header of `127.0.0.1`, `localhost`, or `::1`. This blocks DNS-rebinding attacks. Status `403` otherwise.
- `POST` bodies must be sent as `Content-Type: application/json`. A web page can't send that cross-site without a CORS preflight, and the API never answers preflights, so a malicious page can't fire commands. Status `415` otherwise.

Normal clients (curl, Companion, Node, Python, PowerShell) set both of these naturally.

**Turning the API off.** When **Control API** is off, Trackmind moves its own interface to a random local port. Requests to `8742` then fail to connect, and any other request without the app's token gets `403 "Control API is disabled"`.

**What the API can't do.** The Trackmind window talks to the same server, using a random per-launch token. The token unlocks the app-only endpoints: full settings (including the camera password), profile editing, updates, and the live preview. External clients never see the camera login or settings, and can only call the commands documented below.

---

## Reading state

### `GET /api/status`

Returns a snapshot of everything Trackmind is doing. Poll it (the Stream Deck plugin polls every 350 ms); the call is cheap.

```json
{
  "ok": true,
  "app": "trackmind",
  "version": "1.8",
  "status": "TRACKING",
  "stream": "live",
  "error": null,
  "tracking": true,
  "locked": false,
  "subject": true,
  "detection": { "cx": 0.52, "cy": 0.41, "w": 0.18, "h": 0.46 },
  "autozoom": false,
  "zooming": 0,
  "motion_sync": true,
  "visca": true,
  "manual": false,
  "camera_ip": "192.168.100.88",
  "home_preset": 0,
  "anchor": { "enabled": true, "learned": true, "preset": 5, "mode": "recall",
              "range": 4, "hold": 0.25, "state": "held", "offset": null,
              "learning": false, "error": null },
  "auto": { "enabled": true, "style": "balanced", "situation": "walking",
            "learned": true, "gain_pan": 0.042, "gain_tilt": 0.031,
            "latency": 0.46, "fit_r2": 0.95, "soften": 1.0 },
  "profile": "Sunday AM",
  "profiles": ["Sunday AM", "Wednesday", "Choir"],
  "values": {
    "track_offset": 2,
    "motion_smooth": 5,
    "zoom_target": 45,
    "zoom_speed": 1
  }
}
```

| Field | Type | Meaning |
|---|---|---|
| `app` | string | Always `"trackmind"`. Use it to confirm you've reached Trackmind and not something else on that port. |
| `version` | string | Trackmind version, without the leading `v`. |
| `status` | `"TRACKING"` \| `"PAUSED"` \| `"OFFLINE"` | High-level state. `PAUSED` means the stream is up but tracking is off. |
| `stream` | `"live"` \| `"connecting"` \| `"offline"` | RTSP video state. `connecting` covers the first 2–5 s after launch or after changing the camera IP. |
| `error` | string \| null | Why the stream isn't running: `"No camera configured"`, or the capture error (e.g. `"Cannot open stream"`). |
| `tracking` | bool | Auto-tracking is on. |
| `locked` | bool | Lock-on is active (only ever `true` while tracking). |
| `subject` | bool | A person is currently detected (only while tracking). |
| `detection` | object \| null | The detected subject's box, as fractions of the frame (0–1): center `cx`/`cy`, size `w`/`h`. It's `null` when nobody is detected or tracking is off. |
| `autozoom` | bool | Auto-zoom is enabled. |
| `zooming` | `-1` \| `0` \| `1` | Auto-zoom is currently zooming out, idle, or zooming in. |
| `motion_sync` | bool | PTZOptics Motion Sync is enabled. |
| `visca` | bool | The VISCA control socket to the camera is connected. |
| `manual` | bool | A manual `move` or `zoom` is in progress (a Stream Deck key is being held). |
| `camera_ip` | string | Camera IP from Trackmind's settings. |
| `home_preset` | int | Preset used by `home` and by lost-subject recovery. |
| `anchor` | object | Pulpit anchor for the active profile. `state` is `"off"` (disabled or not tracking), `"free"` (tracking normally), `"snapping"` (recalling the preset or gliding to the pulpit) or `"held"` (holding the pulpit shot). `offset` is how far the camera is from the learned pulpit, in Snap-range steps (`null` when unknown or while holding). `learned` is `false` until **Learn pulpit** has been run for this profile. `mode` is `"recall"` or `"glide"`. `range` (Snap range, compare with `offset`) and `hold` (hold-zone half-width, fraction of the frame) are the profile's settings. `learning`/`error` report the Learn pulpit run. |
| `auto` | object | Auto mode. `enabled` and `style` (`"calm"`, `"balanced"`, `"responsive"`) are always present. While tracking with Auto on it also reports `situation` (`"still"`, `"walking"`, `"fast"`), whether the camera model is `learned` yet, the learned `gain_pan`/`gain_tilt` (frame widths per second per VISCA speed step), `latency` (seconds from command to video), `fit_r2` (how well the model fits, 0–1) and `soften` (1 = normal, lower = the hunting guard has eased off). |
| `profile` | string \| null | Last loaded profile. |
| `profiles` | string[] | Names of all saved profiles. |
| `values` | object | Live values you can change with [`adjust`](#post-apiadjust). |

---

## Commands

Every command is a `POST` with a JSON object body (use `{}` when there are no parameters).

**Success** returns `200` along with the fresh `state` (same shape as `/api/status`), so you don't need a follow-up poll:

```json
{ "ok": true, "state": { "...": "..." } }
```

**Failure** returns an error status with a readable `error` string, plus the current `state`:

```json
{ "ok": false, "error": "Turn tracking on before locking", "state": { "...": "..." } }
```

| Status | When |
|---|---|
| `400` | Bad JSON, missing or non-numeric parameter, unknown `adjust` key |
| `403` | `Host` header isn't localhost, or the Control API is turned off |
| `404` | Unknown command or unknown profile name |
| `409` | Camera stream isn't connected yet, or lock was requested while tracking is off |
| `415` | Body isn't `application/json` |
| `502` | Camera didn't accept the VISCA command |
| `503` | Trackmind's UI thread was busy for >3 s (rare; retry) |

### Toggles: `tracking`, `lock`, `auto`, `autozoom`, `motion-sync`, `anchor`

```http
POST /api/tracking      {"state": "toggle"}
POST /api/lock          {"state": "on"}
POST /api/auto          {"state": "on", "style": "calm"}
POST /api/autozoom      {"state": "off"}
POST /api/motion-sync   {"state": "toggle"}
POST /api/anchor        {"state": "toggle"}
```

| Param | Values | Default |
|---|---|---|
| `state` | `"toggle"`, `"on"`, `"off"` (also accepts `true`/`false`, `1`/`0`) | `"toggle"` |

These commands work exactly like the buttons in Trackmind's main window:

- Turning **tracking** off also releases the lock and stops the camera.
- **tracking** returns `409` until the video stream is live.
- **lock** `"on"` returns `409` while tracking is off. **lock** `"off"` always succeeds.
- **auto** turns Auto mode on or off for the active settings. The optional `style` (`"calm"`, `"balanced"`, `"responsive"`) changes how lively it is. Both are saved.
- **autozoom** and **motion-sync** are saved to Trackmind's settings. Motion Sync is pushed to the camera immediately.
- **anchor** turns the pulpit anchor on or off and saves that into the active profile. `"on"` returns `409` if the pulpit hasn't been learned for this profile yet (that's done in the app's Settings → Pulpit).

### `POST /api/preset`

Recall a camera preset.

```json
{ "preset": 4, "tracking": "off" }
```

| Param | Values | Default |
|---|---|---|
| `preset` | `0`–`89` (required) | — |
| `tracking` | `"off"` pauses tracking so the preset shot holds. `"keep"` leaves tracking on, so it takes over from the preset position. | `"off"` |

The success response also includes `"preset": 4`.

### `POST /api/home`

Recall Trackmind's **Home Preset** (from the main panel). Takes the same `tracking` param as `preset`.

```json
{ "tracking": "off" }
```

### `POST /api/profile`

Load a saved profile, exactly like **Settings → Profiles → LOAD**.

```json
{ "name": "Sunday AM" }
```

Returns `404` if no profile has that name. Names are case-sensitive; use `profiles` from `/api/status` to get the exact names.

### `POST /api/move` (manual pan / tilt)

```json
{ "pan": 6, "tilt": -3, "pause_tracking": true }
```

| Param | Values | Default |
|---|---|---|
| `pan` | `-24`–`24`. Positive = **right**, negative = left, `0` = no pan. | `0` |
| `tilt` | `-24`–`24`. Positive = **up**, negative = down, `0` = no tilt. | `0` |
| `pause_tracking` | If tracking is on, pause it for the move and **resume automatically** after the stop. | `true` |

Send `{"pan": 0, "tilt": 0}` to stop.

> **Keep-alive / dead-man switch:** a manual move **stops by itself after 1.2 s** unless you send the same command again. While a button is held, re-send the move every ~400 ms; the Stream Deck plugin does this. That way, if a client crashes or a key-up is lost, the camera can never be left spinning.

### `POST /api/zoom` (manual zoom)

```json
{ "dir": "in", "speed": 3, "pause_tracking": true }
```

| Param | Values | Default |
|---|---|---|
| `dir` | `"in"`, `"out"`, `"stop"` | `"stop"` |
| `speed` | `0`–`7` | `3` |
| `pause_tracking` | Same as `move` | `true` |

Same 1.2 s keep-alive rule as `move`. Tracking resumes only once **both** the manual move and the manual zoom have stopped.

### `POST /api/adjust`

Change a tracking value live. The change shows in Trackmind's settings panel and is saved (debounced by ~1 s, so spinning a dial doesn't hammer the disk).

```json
{ "key": "track_offset", "delta": -1 }
{ "key": "zoom_target",  "value": 60 }
```

| `key` | Range | Meaning |
|---|---|---|
| `track_offset` | `-7`–`7` | Vertical aim point: −7 = top of head, 0 = centre, +7 = feet |
| `motion_smooth` | `0`–`10` | Accel/decel smoothing: 0 = snappy, 10 = silkiest |
| `zoom_target` | `20`–`90` | Auto-zoom target frame fill, in % |
| `zoom_speed` | `0`–`7` | Auto-zoom motor speed |

Send either `delta` (relative) or `value` (absolute). Results are clamped to the range. The success response includes `"key"` and the new `"value"`.

---

## Examples

**curl** (Git Bash / macOS / Linux):

```bash
curl -s http://127.0.0.1:8742/api/status
curl -s -X POST http://127.0.0.1:8742/api/tracking -H "Content-Type: application/json" -d '{"state":"on"}'
curl -s -X POST http://127.0.0.1:8742/api/preset   -H "Content-Type: application/json" -d '{"preset":2}'
```

**PowerShell:**

```powershell
Invoke-RestMethod http://127.0.0.1:8742/api/status
Invoke-RestMethod -Method Post -Uri http://127.0.0.1:8742/api/lock `
  -ContentType 'application/json' -Body '{"state":"toggle"}'
```

**Bitfocus Companion:** use the *Generic: HTTP Requests* module. Choose a **POST** action, set the URL to `http://127.0.0.1:8742/api/tracking`, the body to `{"state":"toggle"}`, and the content type to `application/json`.

**vMix:** in a vMix script (Settings → Scripting), send the request with `System.Net.WebClient`, setting the `Content-Type` header to `application/json` before `UploadString`. Pair it with a vMix shortcut to start tracking when an input goes live.

---

## Versioning

The API was added in Trackmind **v1.7**. New fields and commands may be added in later versions, but existing ones won't change meaning. Clients should ignore fields they don't recognise. The `anchor` command and status block were added in **v1.8**.
