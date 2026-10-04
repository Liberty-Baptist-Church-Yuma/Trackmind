/**
 * Trackmind desktop UI.
 *
 * State flows one way: the engine pushes a full snapshot over SSE, render()
 * paints it. User actions call commands; the reply (and the next snapshot)
 * brings the new state back. Controls the user is touching are never
 * overwritten mid-gesture.
 */
import { call, previewUrl, subscribe } from "./api.js";
import { liquidGlass, refreshGlass } from "./liquid-glass.js";

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

let S = null; // latest state
let linked = false;
const busy = new Set(); // controls mid-edit

// ── Preferences (per machine, UI-only) ───────────────────────

const pref = {
	get(key, def) {
		try {
			return localStorage.getItem(`tm.${key}`) ?? def;
		} catch {
			return def;
		}
	},
	set(key, v) {
		try {
			localStorage.setItem(`tm.${key}`, v);
		} catch {
			/* private mode */
		}
	},
};

function applyPrefs() {
	const q = pref.get("glass", "full");
	document.documentElement.classList.toggle("lite", q === "lite");
	paintSeg($("#glassQuality"), q);
	const ov = pref.get("overlay", "1") === "1";
	document.body.classList.toggle("no-overlay", !ov);
	$("#overlayToggle").checked = ov;
	const hz = pref.get("holdzone", "1") === "1";
	document.body.classList.toggle("no-holdzone", !hz);
	$("#holdzoneToggle").checked = hz;
}

// ── Toasts & tooltips ────────────────────────────────────────

function toast(msg, tone = "amber") {
	const t = document.createElement("div");
	t.className = "toast glass glass--dense";
	t.dataset.tone = tone;
	t.textContent = msg;
	$("#toasts").append(t);
	setTimeout(() => {
		t.classList.add("out");
		t.addEventListener("animationend", () => t.remove(), { once: true });
	}, 3200);
}

function tooltips() {
	const tip = $("#tip");
	let timer;
	document.addEventListener("pointerover", (e) => {
		const el = e.target.closest("[data-tip]");
		clearTimeout(timer);
		if (!el) return (tip.hidden = true);
		timer = setTimeout(() => {
			tip.replaceChildren(document.createTextNode(el.dataset.tip));
			if (el.dataset.key) {
				const k = document.createElement("kbd");
				k.textContent = el.dataset.key;
				tip.append(k);
			}
			tip.hidden = false;
			const r = el.getBoundingClientRect();
			const tr = tip.getBoundingClientRect();
			const below = r.top < 120;
			tip.style.left = `${Math.max(8, Math.min(innerWidth - tr.width - 8, r.left + r.width / 2 - tr.width / 2))}px`;
			tip.style.top = `${below ? r.bottom + 10 : r.top - tr.height - 10}px`;
		}, 450);
	});
	document.addEventListener("pointerdown", () => {
		clearTimeout(timer);
		tip.hidden = true;
	});
}

// ── Commands ─────────────────────────────────────────────────

async function run(cmd, body, { quiet = false } = {}) {
	try {
		const res = await call(cmd, body);
		if (res.state) render(res.state);
		return res;
	} catch (err) {
		if (!quiet) toast(err.message, err.status === 409 ? "amber" : "red");
		if (err.state) render(err.state);
		return null;
	}
}

function flash(el) {
	el.classList.remove("flash");
	void el.offsetWidth;
	el.classList.add("flash");
}

const actions = {
	tracking: () => run("tracking", { state: "toggle" }),
	lock: () => run("lock", { state: "toggle" }),
	autozoom: () => run("autozoom", { state: "toggle" }),
	auto: () => run("auto", { state: "toggle" }),
	home: async () => {
		flash($("#tileHome"));
		if (await run("home", { tracking: "off" })) toast(`Home preset ${S?.home_preset ?? ""} recalled`, "red");
	},
	"anchor-learn": () => run("anchor-learn").then((r) => r && toast(`Recalling preset ${S?.anchor?.preset ?? ""} to learn the pulpit…`, "amber")),
	reconnect: () => run("reconnect").then((r) => r && toast("Reconnecting to the camera…", "blue")),
	"toggle-tune": () => toggleTune(),
	fullscreen: () => toggleFullscreen(),
	"open-settings": (el) => openSheet(el?.dataset.section),
	"close-settings": () => closeSheet(),
	"update-check": () => run("update-check").then((r) => r && toast("Checking for updates…", "blue")),
	reveal: () => {
		const f = $("#passField");
		f.type = f.type === "password" ? "text" : "password";
	},
	"profile-save": async () => {
		const input = $("#newProfile");
		const name = input.value.trim();
		if (!name) return input.focus();
		if (await run("profile-save", { name })) {
			input.value = "";
			toast(`Saved profile “${name}”`, "green");
		}
	},
};

document.addEventListener("click", (e) => {
	const el = e.target.closest("[data-action]");
	if (!el) return;
	const fn = actions[el.dataset.action];
	if (fn) {
		e.preventDefault();
		fn(el);
	}
});

function toggleFullscreen() {
	if (S?.window === "app") {
		document.body.dataset.fullscreen = document.body.dataset.fullscreen === "1" ? "0" : "1";
		return run("window", { action: "fullscreen" });
	}
	if (document.fullscreenElement) document.exitFullscreen();
	else document.documentElement.requestFullscreen?.();
}

// ── Keyboard ─────────────────────────────────────────────────

document.addEventListener("keydown", (e) => {
	if (e.target.closest("input, select, textarea") || e.ctrlKey || e.metaKey || e.altKey) {
		if (e.key === "Escape") e.target.blur();
		return;
	}
	const k = e.key.toLowerCase();
	const map = { t: "tracking", l: "lock", a: "auto", z: "autozoom", h: "home", u: "toggle-tune", f: "fullscreen", ",": "open-settings" };
	if (e.key === "F11") {
		e.preventDefault();
		return toggleFullscreen();
	}
	if (e.key === "Escape") {
		if (!$("#sheet").hidden) return closeSheet();
		if (!$("#tune").hidden) return toggleTune(false);
		if (S?.window === "app" && document.body.dataset.fullscreen === "1") return toggleFullscreen();
		if (document.fullscreenElement) return document.exitFullscreen();
		return;
	}
	if (map[k] && $("#wizard").hidden) {
		e.preventDefault();
		actions[map[k]]();
	}
});

// ── Video & overlay ──────────────────────────────────────────

const video = $("#video");
const ambient = $("#ambient");
let streaming = false;

function setStreaming(on) {
	if (on === streaming) return;
	streaming = on;
	if (on) {
		const url = previewUrl();
		video.src = url;
		ambient.src = url;
	} else {
		video.removeAttribute("src");
		ambient.removeAttribute("src");
	}
}

/** Letterboxed rect of the video inside the window (object-fit: contain). */
function videoRect() {
	const vw = video.naturalWidth || 16;
	const vh = video.naturalHeight || 9;
	const W = innerWidth;
	const H = innerHeight;
	const s = Math.min(W / vw, H / vh);
	const w = vw * s;
	const h = vh * s;
	return { x: (W - w) / 2, y: (H - h) / 2, w, h };
}

function paintOverlay() {
	const ov = $("#overlay");
	const r = videoRect();
	Object.assign(ov.style, { left: `${r.x}px`, top: `${r.y}px`, width: `${r.w}px`, height: `${r.h}px` });
	if (!S) return;

	const dz = $("#deadzone");
	const pd = S.settings?.pan_dead ?? 0.17;
	const td = S.settings?.tilt_dead ?? 0.17;
	Object.assign(dz.style, {
		left: `${(0.5 - pd) * r.w}px`,
		top: `${(0.5 - td) * r.h}px`,
		width: `${2 * pd * r.w}px`,
		height: `${2 * td * r.h}px`,
	});

	const ret = $("#reticle");
	const d = S.detection;
	ret.classList.toggle("on", !!d);
	ret.classList.toggle("locked", !!S.locked);
	if (d) {
		Object.assign(ret.style, {
			left: `${(d.cx - d.w / 2) * r.w}px`,
			top: `${(d.cy - d.h / 2) * r.h}px`,
			width: `${d.w * r.w}px`,
			height: `${d.h * r.h}px`,
		});
		$("#reticleTag").textContent = S.locked ? "Locked" : "Subject";
	}

	const anchorState = S.anchor?.state;
	$("#holdChip").hidden = !["snapping", "held"].includes(anchorState);
	text("#holdChipText", anchorState !== "snapping" ? "Holding on pulpit" : S.anchor.mode === "glide" ? "Gliding to pulpit" : "Snapping to pulpit");

	// While held, the hold zone replaces the dead zone as the band the speaker can roam
	const held = anchorState === "held";
	ov.classList.toggle("holding", held);
	const hz = $("#holdzone");
	const hw = S.anchor?.hold ?? 0.25;
	Object.assign(hz.style, { left: `${(0.5 - hw) * r.w}px`, width: `${2 * hw * r.w}px` });

	const z = $("#zoomChip");
	z.hidden = !S.zooming;
	$("#zoomChipText").textContent = S.zooming === 1 ? "Zooming in" : "Zooming out";
}

addEventListener("resize", paintOverlay);
video.addEventListener("load", paintOverlay);

// ── Rendering ────────────────────────────────────────────────

function text(sel, v) {
	const el = $(sel);
	if (el && el.textContent !== v) el.textContent = v;
}

function tile(id, on, sub, { disabled = false, warn = false } = {}) {
	const el = $(id);
	el.classList.toggle("on", on);
	el.classList.toggle("disabled", disabled);
	el.classList.toggle("warn", warn);
	text(`${id}Sub`, sub);
}

function describe(s) {
	if (!linked) return { tone: "red", label: "Engine offline", detail: "Trackmind isn't responding" };
	if (s.stream === "offline") {
		return s.error === "No camera configured"
			? { tone: "off", label: "No camera", detail: "Add your camera to get started" }
			: { tone: "red", label: "No signal", detail: s.error || "Camera stream unavailable" };
	}
	if (s.stream === "connecting") return { tone: "amber", label: "Connecting", detail: s.camera_ip };
	if (s.manual) return { tone: "blue", label: "Manual", detail: "Stream Deck is driving the camera" };
	if (s.tracking) {
		if (s.locked) return { tone: "amber", label: "Locked", detail: "Following the locked subject" };
		if (s.anchor?.state === "held") return { tone: "green", label: "Tracking", detail: "Holding on the pulpit" };
		return s.subject
			? { tone: "green", label: "Tracking", detail: s.auto?.enabled ? `Auto · ${autoLabel(s.auto)}` : "Subject in frame" }
			: { tone: "green", label: "Tracking", detail: "Searching for a subject…" };
	}
	return { tone: "off", label: "Paused", detail: s.profile ? `Profile · ${s.profile}` : "Tracking is off" };
}

// Auto mode — what it's doing, in words
const AUTO_SITUATION = { still: "Steady", walking: "Following", fast: "Catching up" };
function autoLabel(a) {
	if (!a?.situation) return "Ready";
	if (!a.learned) return "Learning the camera";
	return AUTO_SITUATION[a.situation] + (a.soften < 0.9 ? " · eased off" : "");
}

function renderAuto(s) {
	const a = s.auto;
	if (!a) return;
	let line = "Off — using the manual speeds below";
	let color = "";
	if (a.enabled && !s.tracking) line = "On — starts when tracking does";
	else if (a.enabled && !a.learned)
		(line = "Learning the camera — it measures how this camera moves the first few times it pans"), (color = "var(--amber)");
	else if (a.enabled) {
		const pct = Math.round(a.gain_pan * 100);
		line = `${autoLabel(a)} · 1 speed step ≈ ${pct}% of the frame per second · video delay ${a.latency.toFixed(2)} s`;
		color = "var(--green)";
	}
	text("#autoStatus", line);
	$("#autoStatus").style.color = color;
}

function renderSignal(s) {
	const card = $("#signal");
	let tone = "amber";
	let title = "Connecting to camera";
	let body = `Opening the RTSP stream from ${s.camera_ip} — this takes a few seconds.`;
	if (!linked) {
		tone = "red";
		title = "Trackmind engine offline";
		body = "The tracking engine stopped responding. Restart Trackmind if this doesn't clear in a moment.";
	} else if (s.error === "No camera configured") {
		tone = "blue";
		title = "Add your camera";
		body = "Enter your PTZOptics camera's IP address and login to start tracking.";
	} else if (s.stream === "offline") {
		tone = "red";
		title = "Can't reach the camera";
		body = `${s.error || "The stream didn't open."} Check the IP address (${s.camera_ip || "not set"}), login, and that the camera is on this network.`;
	}
	card.dataset.tone = tone;
	text("#signalTitle", title);
	text("#signalText", body);
}

function render(s) {
	S = s;
	const b = document.body;
	b.dataset.stream = linked ? s.stream : "offline";
	b.dataset.tracking = String(!!s.tracking);
	setStreaming(linked && s.stream === "live");

	const d = describe(s);
	const tally = $("#tally");
	tally.dataset.tone = d.tone;
	text("#tally", d.label);
	text("#statusDetail", d.detail);
	text("#chipCamera", s.camera_ip || "No camera");
	text("#chipFps", s.stream === "live" ? `${Math.round(s.fps)} fps` : s.stream);

	const live = s.stream === "live";
	tile("#tileTracking", s.tracking, s.tracking ? (s.subject ? "Subject" : "Searching…") : live ? "Off" : "No stream", {
		disabled: !live,
		warn: s.tracking && !s.subject,
	});
	tile("#tileLock", s.locked, s.locked ? "On subject" : s.tracking ? "Off" : "Track first", { disabled: !s.tracking });
	tile("#tileAuto", !!s.auto?.enabled, s.auto?.enabled ? (s.tracking ? autoLabel(s.auto) : "On") : "Off");
	b.dataset.auto = String(!!s.auto?.enabled);
	tile("#tileZoom", s.autozoom, s.autozoom ? (s.zooming === 1 ? "Zooming in" : s.zooming === -1 ? "Zooming out" : "On") : "Off");
	text("#tileHomeSub", `Preset ${s.home_preset}`);

	renderSignal(s);
	b.classList.toggle("first-run", !!s.first_run);
	paintOverlay();
	renderTune(s);
	renderSettings(s);
	renderProfiles(s);
	renderUpdate(s.update);
	maybeWizard(s);
}

// ── Live tune ────────────────────────────────────────────────

function toggleTune(force) {
	const t = $("#tune");
	const show = force ?? t.hidden;
	t.hidden = !show;
	$("#tuneBtn").classList.toggle("active", show);
	if (show) requestAnimationFrame(() => refreshGlass(t));
}

function paintSlider(r) {
	const min = Number(r.min);
	const max = Number(r.max);
	r.style.setProperty("--pct", `${((Number(r.value) - min) / (max - min)) * 100}%`);
	const out = r.parentElement.querySelector(".value");
	if (out) {
		const fixed = r.dataset.fixed;
		const v = fixed ? Number(r.value).toFixed(Number(fixed)) : r.value;
		const sign = r.classList.contains("centred") && Number(r.value) > 0 ? "+" : "";
		out.textContent = `${sign}${v}${r.dataset.suffix ?? ""}`;
	}
}

function renderTune(s) {
	for (const r of $$("[data-adjust]")) {
		if (busy.has(r)) continue;
		const v = s.values?.[r.dataset.adjust];
		if (v !== undefined && Number(r.value) !== v) r.value = v;
		paintSlider(r);
	}
	const sel = $("#tuneProfile");
	if (busy.has(sel)) return;
	const want = JSON.stringify([s.profiles, s.profile]);
	if (sel.dataset.v !== want) {
		sel.dataset.v = want;
		sel.replaceChildren(
			new Option(s.profiles.length ? "Choose profile…" : "No profiles yet", ""),
			...s.profiles.map((n) => new Option(n, n)),
		);
		sel.options[0].disabled = true;
		sel.value = s.profile && s.profiles.includes(s.profile) ? s.profile : "";
	}
}

function bindTune() {
	for (const r of $$("[data-adjust]")) {
		let timer;
		r.addEventListener("pointerdown", () => busy.add(r));
		r.addEventListener("input", () => {
			busy.add(r);
			paintSlider(r);
			clearTimeout(timer);
			timer = setTimeout(() => run("adjust", { key: r.dataset.adjust, value: Number(r.value) }, { quiet: true }), 60);
		});
		r.addEventListener("change", () => setTimeout(() => busy.delete(r), 500));
	}
	const sel = $("#tuneProfile");
	sel.addEventListener("focus", () => busy.add(sel));
	sel.addEventListener("blur", () => busy.delete(sel));
	sel.addEventListener("change", async () => {
		if (sel.value && (await run("profile", { name: sel.value }))) toast(`Loaded profile “${sel.value}”`, "green");
		busy.delete(sel);
	});
}

// ── Settings sheet ───────────────────────────────────────────

function openSheet(section) {
	const sheet = $("#sheet");
	if (sheet.hidden) {
		sheet.hidden = false;
		sheet.classList.remove("closing");
		$("#scrim").hidden = false;
	}
	if (section) {
		requestAnimationFrame(() => {
			const el = $(`#sec-${section}`);
			$("#sheetBody").scrollTo({ top: el.offsetTop - 8, behavior: "smooth" });
			markNav(section);
		});
	}
}

function closeSheet() {
	const sheet = $("#sheet");
	if (sheet.hidden) return;
	document.activeElement?.blur();
	sheet.classList.add("closing");
	$("#scrim").hidden = true;
	sheet.addEventListener(
		"animationend",
		() => {
			sheet.hidden = true;
			sheet.classList.remove("closing");
		},
		{ once: true },
	);
}

function markNav(id) {
	for (const b of $$("#sheetNav button")) {
		const on = b.dataset.section === id;
		if (on && !b.classList.contains("active")) b.scrollIntoView({ block: "nearest", inline: "nearest", behavior: "smooth" });
		b.classList.toggle("active", on);
	}
}

function bindSheetNav() {
	$("#sheetNav").addEventListener("click", (e) => {
		const b = e.target.closest("button[data-section]");
		if (b) openSheet(b.dataset.section);
	});
	const body = $("#sheetBody");
	body.addEventListener("scroll", () => {
		let current = "camera";
		for (const sec of $$(".group", body)) if (sec.offsetTop - 60 <= body.scrollTop) current = sec.id.slice(4);
		markNav(current);
	});
	markNav("camera");
}

/** Convert between the UI's display value and the stored setting. */
const toSetting = (el, v) => (el.dataset.scale ? Number(v) / Number(el.dataset.scale) : v);
const fromSetting = (el, v) => (el.dataset.scale ? Math.round(Number(v) * Number(el.dataset.scale)) : v);

let pending = {};
let flushTimer;
function queueSetting(key, value, delay = 120) {
	pending[key] = value;
	clearTimeout(flushTimer);
	flushTimer = setTimeout(async () => {
		const changes = pending;
		pending = {};
		const res = await run("settings", { changes });
		if (res && ("camera_ip" in changes || "rtsp_user" in changes || "rtsp_pass" in changes || "rtsp_stream" in changes)) {
			toast("Reconnecting with the new camera settings…", "blue");
		}
	}, delay);
}

function paintSeg(group, value) {
	if (!group) return;
	const buttons = $$("button[data-value]", group);
	group.style.setProperty("--n", buttons.length);
	buttons.forEach((b, i) => {
		const on = String(b.dataset.value) === String(value);
		b.setAttribute("aria-checked", String(on));
		b.setAttribute("role", "radio");
		if (on) group.style.setProperty("--i", i);
	});
}

function bindStepper(st, onCommit) {
	const input = $("input", st);
	const min = Number(st.dataset.min);
	const max = Number(st.dataset.max);
	const commit = (v) => {
		const n = Math.max(min, Math.min(max, Math.round(Number(v) || 0)));
		input.value = n;
		onCommit(n);
	};
	for (const b of $$("button", st)) {
		b.type = "button";
		b.addEventListener("click", () => commit(Number(input.value) + Number(b.dataset.step)));
	}
	input.addEventListener("change", () => commit(input.value));
	input.addEventListener("focus", () => busy.add(st));
	input.addEventListener("blur", () => setTimeout(() => busy.delete(st), 400));
	return { set: (v) => !busy.has(st) && document.activeElement !== input && (input.value = v) };
}

const settingWriters = [];

function bindSettings() {
	for (const el of $$("#sheet [data-setting]")) {
		const key = el.dataset.setting;

		if (el.classList.contains("seg")) {
			el.addEventListener("click", (e) => {
				const b = e.target.closest("button[data-value]");
				if (!b) return;
				paintSeg(el, b.dataset.value);
				queueSetting(key, b.dataset.value, 0);
			});
			settingWriters.push((st) => paintSeg(el, st[key]));
		} else if (el.classList.contains("stepper")) {
			const w = bindStepper(el, (n) => queueSetting(key, n, 250));
			settingWriters.push((st) => w.set(st[key]));
		} else if (el.type === "checkbox") {
			el.addEventListener("change", () => queueSetting(key, el.checked, 0));
			settingWriters.push((st) => (el.checked = !!st[key]));
		} else if (el.type === "range") {
			el.addEventListener("pointerdown", () => busy.add(el));
			el.addEventListener("input", () => {
				busy.add(el);
				paintSlider(el);
				queueSetting(key, toSetting(el, el.value), 150);
			});
			el.addEventListener("change", () => setTimeout(() => busy.delete(el), 600));
			settingWriters.push((st) => {
				if (busy.has(el)) return;
				el.value = fromSetting(el, st[key]);
				paintSlider(el);
			});
		} else {
			// Text/number fields commit on Enter or blur (camera changes restart the stream)
			el.addEventListener("focus", () => busy.add(el));
			el.addEventListener("keydown", (e) => e.key === "Enter" && el.blur());
			el.addEventListener("blur", () => {
				busy.delete(el);
				const v = el.type === "number" ? Number(el.value) : el.value.trim();
				if (S && String(S.settings[key]) !== String(v)) queueSetting(key, v, 0);
			});
			settingWriters.push((st) => {
				if (!busy.has(el) && document.activeElement !== el) el.value = st[key] ?? "";
			});
		}
	}

	// Appearance (local)
	$("#glassQuality").addEventListener("click", (e) => {
		const b = e.target.closest("button[data-value]");
		if (!b) return;
		pref.set("glass", b.dataset.value);
		applyPrefs();
	});
	$("#overlayToggle").addEventListener("change", (e) => {
		pref.set("overlay", e.target.checked ? "1" : "0");
		applyPrefs();
	});
	$("#holdzoneToggle").addEventListener("change", (e) => {
		pref.set("holdzone", e.target.checked ? "1" : "0");
		applyPrefs();
	});
}

function renderSettings(s) {
	if (!s.settings) return;
	for (const w of settingWriters) w(s.settings);

	const api = s.api;
	if (api) {
		text("#apiStatus", !api.enabled ? "Off" : api.listening ? `Listening on 127.0.0.1:${api.port}` : api.error || "Not listening");
		$("#apiStatus").style.color = api.enabled ? (api.listening ? "var(--green)" : "var(--red)") : "";
	}
	renderAnchor(s);
	renderAuto(s);
	text("#aboutVersion", `v${s.version}`);
	const u = s.update;
	if (u) {
		const when = u.last_check ? new Date(u.last_check * 1000).toLocaleString([], { dateStyle: "medium", timeStyle: "short" }) : null;
		const line =
			u.phase === "checking"
				? "Checking…"
				: u.latest && u.latest !== s.version && u.has_installer
					? `v${u.latest} is available`
					: when
						? `Up to date · checked ${when}`
						: "Never checked";
		text("#updateLine", line);
	}
}

let anchorWasLearning = false;
function renderAnchor(s) {
	const a = s.anchor;
	if (!a) return;
	let line;
	let color = "";
	if (a.learning) line = "Learning — waiting for the camera to stop…";
	else if (a.error) (line = a.error), (color = "var(--red)");
	else if (!a.learned) line = "Not learned yet — the anchor stays off until you learn it";
	else if (a.state === "held") (line = "Holding on the pulpit"), (color = "var(--green)");
	else if (a.offset != null) {
		const near = a.offset <= s.settings.anchor_range;
		line = `Learned · camera is ${a.offset.toFixed(1)} from the pulpit${near ? " — in range" : ""}`;
		if (near) color = "var(--green)";
	} else line = a.enabled ? "Learned · live distance shows while tracking" : "Learned";
	text("#anchorStatus", line);
	$("#anchorStatus").style.color = color;
	const btn = $("#anchorLearn");
	btn.disabled = a.learning || s.stream !== "live";
	$("#anchorGlideRow").hidden = a.mode !== "glide";
	btn.lastChild.textContent = a.learning ? "Learning…" : a.learned ? "Re-learn" : "Learn pulpit";
	text(
		"#anchorProfileHint",
		s.profile
			? `Each profile keeps its own pulpit. Changes here save straight into “${s.profile}”.`
			: "Each profile keeps its own pulpit. Load or save a profile to give it its own anchor.",
	);
	if (anchorWasLearning && !a.learning) {
		if (a.error) toast(`Couldn't learn the pulpit: ${a.error}`, "red");
		else toast(`Pulpit learned${s.profile ? ` for “${s.profile}”` : ""}`, "green");
	}
	anchorWasLearning = a.learning;
}

// ── Profiles ─────────────────────────────────────────────────

function renderProfiles(s) {
	const list = $("#profileList");
	const want = JSON.stringify([s.profiles, s.profile]);
	if (list.dataset.v === want) return;
	list.dataset.v = want;
	list.replaceChildren();
	if (!s.profiles.length) {
		const p = document.createElement("div");
		p.className = "profile-empty";
		p.textContent = "No profiles yet — tune your settings, then save them below.";
		list.append(p);
		return;
	}
	for (const name of s.profiles) {
		const row = document.createElement("div");
		row.className = "profile";
		const n = document.createElement("span");
		n.className = "profile-name";
		n.textContent = name;
		row.append(n);
		if (name === s.profile) {
			const badge = document.createElement("span");
			badge.className = "badge";
			badge.textContent = "Active";
			row.append(badge);
		}
		const load = document.createElement("button");
		load.className = "btn btn--sm";
		load.textContent = "Load";
		load.addEventListener("click", async () => (await run("profile", { name })) && toast(`Loaded profile “${name}”`, "green"));
		const del = document.createElement("button");
		del.className = "btn btn--sm btn--icon btn--ghost btn--danger";
		del.setAttribute("aria-label", `Delete ${name}`);
		del.innerHTML = '<svg><use href="#i-trash"/></svg>';
		del.addEventListener("click", async () => {
			if (del.dataset.armed) {
				(await run("profile-delete", { name })) && toast(`Deleted “${name}”`, "red");
			} else {
				del.dataset.armed = "1";
				del.setAttribute("data-tip", "Click again to delete");
				del.style.color = "var(--red)";
				setTimeout(() => {
					delete del.dataset.armed;
					del.style.color = "";
				}, 2500);
			}
		});
		row.append(load, del);
		list.append(row);
	}
}

// ── Updates ──────────────────────────────────────────────────

let updateShown = null;
function renderUpdate(u) {
	if (!u) return;
	const modal = $("#updateModal");
	const show = ["available", "uptodate", "error", "downloading", "installing"].includes(u.phase);
	if (!show) {
		modal.hidden = true;
		updateShown = null;
		return;
	}
	if (updateShown !== u.phase) {
		updateShown = u.phase;
		modal.hidden = false;
		requestAnimationFrame(() => liquidGlass(modal));
	}
	const icon = $("#updateIcon");
	const go = $("#updateGo");
	const later = $("#updateLater");
	const prog = $("#updateProgress");
	const notes = $("#updateNotes");
	prog.hidden = !["downloading", "installing"].includes(u.phase);
	prog.style.setProperty("--p", `${u.progress ?? 0}%`);
	notes.hidden = !(u.phase === "available" && u.notes);
	if (u.notes && notes.textContent !== u.notes) notes.textContent = u.notes;
	go.hidden = u.phase !== "available" || !u.has_installer;
	later.hidden = ["downloading", "installing"].includes(u.phase);

	const set = (tone, title, body, laterLabel = "Later") => {
		icon.dataset.tone = tone;
		go.dataset.tone = tone;
		text("#updateTitle", title);
		text("#updateText", body);
		later.textContent = laterLabel;
	};
	if (u.phase === "available")
		set("green", `Trackmind ${u.latest} is here`, u.has_installer ? `You have v${u.current}. Trackmind will close, update, and reopen by itself.` : `You have v${u.current}. Download it from the releases page.`);
	else if (u.phase === "uptodate") set("green", "You're up to date", `Trackmind v${u.current} is the latest version.`, "Done");
	else if (u.phase === "error") set("red", "Update didn't work", `${u.error || "Couldn't reach GitHub."} You can always download it from ${u.releases_url}.`, "Close");
	else if (u.phase === "downloading") set("blue", "Downloading update", `${u.progress ?? 0}% — keep Trackmind open.`);
	else if (u.phase === "installing") set("blue", "Installing…", "Approve the Windows prompt. Trackmind will restart when it's done.");
}

function bindUpdate() {
	$("#updateGo").addEventListener("click", () => run("update-install"));
	$("#updateLater").addEventListener("click", () => run("update-dismiss", {}, { quiet: true }));
}

// ── First-run wizard ─────────────────────────────────────────

let wizStep = 0;
let wizDone = false;
const WIZ_STEPS = 5;

function maybeWizard(s) {
	if (wizDone || !s.first_run || !$("#wizard").hidden) return;
	$("#wizIp").value = s.settings?.camera_ip || "";
	$("#wizUser").value = s.settings?.rtsp_user || "admin";
	$("#wizPass").value = s.settings?.rtsp_pass || "admin";
	$("#wizPreset input").value = s.settings?.home_preset ?? 0;
	$("#wizard").hidden = false;
	requestAnimationFrame(() => liquidGlass($("#wizard")));
	wizGo(0);
}

function wizGo(n) {
	wizStep = Math.max(0, Math.min(WIZ_STEPS - 1, n));
	$$(".wiz-step").forEach((el) => el.classList.toggle("active", Number(el.dataset.step) === wizStep));
	$("#wizDots").replaceChildren(
		...Array.from({ length: WIZ_STEPS }, (_, i) => {
			const d = document.createElement("i");
			if (i === wizStep) d.className = "on";
			return d;
		}),
	);
	$("#wizBack").style.visibility = wizStep === 0 ? "hidden" : "visible";
	$("#wizNext").textContent = wizStep === 0 ? "Get started" : wizStep === WIZ_STEPS - 1 ? "Finish" : "Continue";
	const focus = $(".wiz-step.active input");
	if (focus) setTimeout(() => focus.focus(), 60);
}

function bindWizard() {
	bindStepper($("#wizPreset"), () => {});
	const dead = $("#wizDead");
	dead.addEventListener("input", () => paintSlider(dead));
	paintSlider(dead);
	$("#wizBack").addEventListener("click", () => wizGo(wizStep - 1));
	$("#wizNext").addEventListener("click", async () => {
		if (wizStep === 1 && !$("#wizIp").value.trim()) {
			$("#wizIp").focus();
			return toast("Enter the camera's IP address", "amber");
		}
		if (wizStep < WIZ_STEPS - 1) return wizGo(wizStep + 1);
		const ok = await run("setup", {
			camera_ip: $("#wizIp").value.trim(),
			rtsp_user: $("#wizUser").value.trim(),
			rtsp_pass: $("#wizPass").value,
			home_preset: Number($("#wizPreset input").value) || 0,
			dead_zone: Number(dead.value),
		});
		if (ok) {
			wizDone = true;
			$("#wizard").hidden = true;
			toast("You're all set — connecting to your camera", "green");
		}
	});
	$("#wizard").addEventListener("keydown", (e) => e.key === "Enter" && $("#wizNext").click());
}

// ── Boot ─────────────────────────────────────────────────────

applyPrefs();
tooltips();
bindTune();
bindSheetNav();
bindSettings();
bindUpdate();
bindWizard();
liquidGlass();
document.addEventListener("fullscreenchange", () => (document.body.dataset.fullscreen = document.fullscreenElement ? "1" : "0"));

subscribe(
	(state) => {
		linked = true;
		render(state);
	},
	(ok) => {
		linked = ok;
		if (!ok && S) render(S);
	},
);
