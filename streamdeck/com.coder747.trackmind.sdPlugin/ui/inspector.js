/**
 * Trackmind property inspector.
 *
 * One page serves every action: sections carry data-for="<action suffix …>"
 * and anything not meant for the current action is removed at startup.
 * Controls bind to settings through data-setting; the plugin pushes live
 * Trackmind state here via sendToPropertyInspector.
 */
(() => {
	"use strict";

	const ACTIONS = {
		tracking: { name: "Tracking", accent: "green" },
		lock: { name: "Lock Subject", accent: "amber" },
		auto: { name: "Auto Mode", accent: "green" },
		autozoom: { name: "Auto-Zoom", accent: "blue" },
		motionsync: { name: "Motion Sync", accent: "blue" },
		anchor: { name: "Pulpit Anchor", accent: "amber" },
		preset: { name: "Recall Preset", accent: "red" },
		home: { name: "Home Preset", accent: "red" },
		profile: { name: "Load Profile", accent: "blue" },
		ptz: { name: "Pan / Tilt", accent: "blue" },
		zoom: { name: "Zoom", accent: "blue" },
		status: { name: "Status", accent: "green" },
		dial: { name: "Tracking Dial", accent: "green" },
	};

	const $ = (sel, root = document) => root.querySelector(sel);
	const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));

	let socket = null;
	let uuid = "";
	let actionUUID = "";
	let kind = "";
	let settings = {};
	let global = {};
	let lastStatus = null;

	// ── Stream Deck wire protocol ──────────────────────────────

	function send(event, payload) {
		if (!socket || socket.readyState !== WebSocket.OPEN) return;
		const msg = { event, context: uuid };
		if (event === "sendToPlugin") msg.action = actionUUID;
		if (payload !== undefined) msg.payload = payload;
		socket.send(JSON.stringify(msg));
	}

	function saveSettings() {
		send("setSettings", settings);
	}

	function saveGlobal() {
		send("setGlobalSettings", global);
	}

	// Stream Deck calls this global once the inspector is loaded.
	window.connectElgatoStreamDeckSocket = (port, inUUID, registerEvent, _info, actionInfo) => {
		uuid = inUUID;
		const info = JSON.parse(actionInfo);
		actionUUID = info.action;
		kind = actionUUID.split(".").pop();
		settings = info.payload?.settings ?? {};

		setup();

		socket = new WebSocket(`ws://127.0.0.1:${port}`);
		socket.onopen = () => {
			socket.send(JSON.stringify({ event: registerEvent, uuid }));
			send("getGlobalSettings");
			send("sendToPlugin", { type: "hello" });
		};
		socket.onmessage = (e) => {
			const msg = JSON.parse(e.data);
			switch (msg.event) {
				case "didReceiveSettings":
					settings = msg.payload?.settings ?? {};
					hydrate();
					break;
				case "didReceiveGlobalSettings":
					global = msg.payload?.settings ?? {};
					$("#host").value = global.host ?? "";
					$("#port").value = global.port ?? "";
					break;
				case "sendToPropertyInspector":
					if (msg.payload?.type === "status") renderStatus(msg.payload);
					break;
			}
		};
	};

	// ── Layout ─────────────────────────────────────────────────

	function setup() {
		const meta = ACTIONS[kind] ?? { name: "Trackmind", accent: "green" };
		document.body.dataset.tone = meta.accent;
		$("#actionName").textContent = meta.name;

		// Drop everything that isn't for this action (so duplicate setting
		// names — e.g. two "speed" sliders — never collide).
		for (const el of $$("[data-for]")) {
			if (!el.dataset.for.split(/\s+/).includes(kind)) el.remove();
		}
		for (const el of $$("[data-hint-for]")) {
			if (el.dataset.hintFor !== kind) el.remove();
		}

		bindControls();
		hydrate();
		window.TrackmindGlass?.liquidGlass();
	}

	/** Value of a setting, falling back to the control's data-default. */
	function valueOf(el) {
		const key = el.dataset.setting;
		if (settings[key] !== undefined) return settings[key];
		const d = el.dataset.default;
		if (d === undefined) return undefined;
		if (d === "true" || d === "false") return d === "true";
		return /^-?\d+$/.test(d) ? Number(d) : d;
	}

	function set(key, value) {
		settings[key] = value;
		saveSettings();
	}

	function bindControls() {
		// Segmented, d-pad, chips: one choice among buttons
		for (const group of $$(".seg, .dpad, .chips")) {
			const buttons = $$("button[data-value]", group);
			buttons.forEach((b) => {
				b.setAttribute("role", "radio");
				b.type = "button";
				b.addEventListener("click", () => {
					set(group.dataset.setting, b.dataset.value);
					paintChoice(group);
				});
			});
			if (group.classList.contains("seg")) group.style.setProperty("--n", buttons.length);
		}

		// Steppers
		for (const st of $$(".stepper")) {
			const input = $("input", st);
			const min = Number(st.dataset.min);
			const max = Number(st.dataset.max);
			const commit = (v) => {
				const n = Math.max(min, Math.min(max, Math.round(Number(v) || 0)));
				input.value = n;
				set(st.dataset.setting, n);
			};
			$$("button", st).forEach((b) => {
				b.type = "button";
				b.addEventListener("click", () => commit(Number(input.value) + Number(b.dataset.step)));
			});
			input.addEventListener("change", () => commit(input.value));
			input.addEventListener("keydown", (e) => {
				if (e.key === "ArrowUp" || e.key === "ArrowDown") {
					e.preventDefault();
					commit(Number(input.value) + (e.key === "ArrowUp" ? 1 : -1));
				}
			});
		}

		// Ranges
		for (const r of $$('input[type="range"][data-setting]')) {
			r.addEventListener("input", () => {
				paintRange(r);
				set(r.dataset.setting, Number(r.value));
			});
		}

		// Switches
		for (const c of $$('input[type="checkbox"][data-setting]')) {
			c.addEventListener("change", () => set(c.dataset.setting, c.checked));
		}

		// Text (debounced)
		for (const t of $$('input[type="text"][data-setting]')) {
			let timer;
			t.addEventListener("input", () => {
				clearTimeout(timer);
				timer = setTimeout(() => set(t.dataset.setting, t.value.trim()), 250);
			});
		}

		// Profile select
		const sel = $("#profileSelect");
		if (sel) sel.addEventListener("change", () => set("profile", sel.value));

		// Connection
		const commitConn = () => {
			const host = $("#host").value.trim();
			const port = Number($("#port").value);
			global = { ...global, host: host || undefined, port: port || undefined };
			saveGlobal();
			send("sendToPlugin", { type: "test" });
		};
		$("#host").addEventListener("change", commitConn);
		$("#port").addEventListener("change", commitConn);
		$("#testBtn").addEventListener("click", () => {
			const btn = $("#testBtn");
			btn.classList.add("busy");
			$("span", btn).textContent = "Testing…";
			commitConn();
			setTimeout(() => {
				btn.classList.remove("busy");
				$("span", btn).textContent = "Test connection";
			}, 900);
		});
	}

	function paintChoice(group) {
		const current = String(valueOf(group));
		const buttons = $$("button[data-value]", group);
		buttons.forEach((b, i) => {
			const on = b.dataset.value === current;
			b.setAttribute("aria-checked", String(on));
			if (on) group.style.setProperty("--i", i);
		});
	}

	function paintRange(r) {
		const min = Number(r.min);
		const max = Number(r.max);
		const pct = ((Number(r.value) - min) / (max - min)) * 100;
		r.style.setProperty("--pct", `${pct}%`);
		const out = $(".value", r.parentElement);
		if (out) out.textContent = r.value;
	}

	/** Push current settings into every control. */
	function hydrate() {
		$$(".seg, .dpad, .chips").forEach(paintChoice);
		for (const st of $$(".stepper")) $("input", st).value = valueOf(st);
		for (const r of $$('input[type="range"][data-setting]')) {
			r.value = valueOf(r);
			paintRange(r);
		}
		for (const c of $$('input[type="checkbox"][data-setting]')) c.checked = !!valueOf(c);
		for (const t of $$('input[type="text"][data-setting]')) {
			if (document.activeElement !== t) t.value = settings[t.dataset.setting] ?? "";
		}
		fillProfiles();
	}

	// ── Live status ────────────────────────────────────────────

	function fillProfiles() {
		const sel = $("#profileSelect");
		if (!sel) return;
		const names = lastStatus?.profiles ?? [];
		const chosen = settings.profile ?? "";
		const opts = [...names];
		if (chosen && !opts.includes(chosen)) opts.unshift(chosen);

		const want = JSON.stringify([opts, chosen, !!lastStatus]);
		if (sel.dataset.state === want) return;
		sel.dataset.state = want;

		sel.replaceChildren();
		const placeholder = new Option(
			lastStatus ? (names.length ? "Choose a profile…" : "No profiles saved yet") : "Connect to Trackmind to list profiles",
			"",
		);
		placeholder.disabled = true;
		sel.add(placeholder);
		for (const n of opts) {
			const label = !lastStatus || names.includes(n) ? n : `${n} (missing)`;
			sel.add(new Option(n === lastStatus?.profile ? `${label}  ·  active` : label, n));
		}
		sel.value = chosen;
		if (!chosen) placeholder.selected = true;
	}

	function renderStatus({ online, error, connection, status }) {
		lastStatus = online ? status : null;

		const tally = $("#tally");
		let tone = "off";
		let label = "OFFLINE";
		if (online && status) {
			if (status.stream !== "live") {
				tone = status.stream === "offline" && status.error ? "red" : "amber";
				label = status.stream === "offline" ? "NO STREAM" : "CONNECTING";
			} else if (status.tracking) {
				tone = "green";
				label = status.locked ? "LOCKED" : "TRACKING";
			} else {
				tone = "amber";
				label = status.manual ? "MANUAL" : "PAUSED";
			}
		}
		tally.dataset.tone = tone;
		tally.textContent = label;

		$("#statCamera").textContent = status?.camera_ip || "—";
		const subj = $("#statSubject");
		subj.className = "stat-value";
		if (!online || !status) subj.textContent = "—";
		else if (!status.tracking) subj.textContent = "Idle";
		else if (status.subject) {
			subj.textContent = status.locked ? "Locked" : "In frame";
			subj.classList.add("good");
		} else {
			subj.textContent = "Searching";
			subj.classList.add("warn");
		}
		$("#statProfile").textContent = (online && status?.profile) || "—";

		const line = $("#connLine");
		const conn = $("#connection");
		if (online && status) {
			line.textContent = `Connected · v${status.version}`;
			line.className = "conn-line ok";
		} else {
			line.textContent = error || "Trackmind not found";
			line.className = "conn-line bad";
			if (!conn.dataset.touched) conn.open = true;
		}
		if (connection) {
			$("#host").placeholder = connection.host;
			$("#port").placeholder = String(connection.port);
		}

		const homeHint = $("#homePresetHint");
		if (homeHint) homeHint.textContent = status ? ` (currently preset ${status.home_preset})` : "";

		fillProfiles();
	}

	document.addEventListener("DOMContentLoaded", () => {
		const conn = $("#connection");
		conn.addEventListener("toggle", () => (conn.dataset.touched = "1"));
	});
})();
