import { EventEmitter } from "node:events";
import http from "node:http";

import streamDeck from "@elgato/streamdeck";

/** Values the dial can adjust live — mirrors App._ADJUSTABLE in autotrack.py. */
export type Values = {
	track_offset: number;
	motion_smooth: number;
	zoom_target: number;
	zoom_speed: number;
};

/** Pulpit anchor — see Controller.anchor_view in autotrack.py. */
export type Anchor = {
	enabled: boolean;
	learned: boolean;
	preset: number;
	state: "off" | "free" | "snapping" | "held";
	mode: "recall" | "glide";
	offset: number | null;
	range: number;
	learning: boolean;
	error: string | null;
};

/** Auto mode — see Controller.status / AutoPilot.view in autotrack.py. */
export type Auto = {
	enabled: boolean;
	style: "calm" | "balanced" | "responsive";
	/** The rest is only present while tracking with Auto on. */
	situation?: "still" | "walking" | "fast";
	learned?: boolean;
	gain_pan?: number;
	latency?: number;
	soften?: number;
};

/** GET /api/status — see App.api_status in autotrack.py. */
export type TrackmindStatus = {
	ok: boolean;
	version: string;
	status: "TRACKING" | "PAUSED" | "OFFLINE";
	stream: "live" | "connecting" | "offline";
	error: string | null;
	tracking: boolean;
	locked: boolean;
	subject: boolean;
	autozoom: boolean;
	zooming: -1 | 0 | 1;
	motion_sync: boolean;
	visca: boolean;
	manual: boolean;
	camera_ip: string;
	home_preset: number;
	/** Missing on Trackmind older than v1.8. */
	anchor?: Anchor;
	/** Missing on Trackmind older than v1.8.2. */
	auto?: Auto;
	profile: string | null;
	profiles: string[];
	values: Values;
};

export type CommandResult = { ok: boolean; error?: string; state?: TrackmindStatus };

export type Connection = { host: string; port: number };

export const DEFAULT_CONNECTION: Connection = { host: "127.0.0.1", port: 8742 };

const POLL_ONLINE_MS = 350;
const POLL_OFFLINE_MS = 2000;
const TIMEOUT_MS = 1500;

/**
 * Single shared connection to the Trackmind desktop app.
 *
 * Polls /api/status and emits `change` only when something actually changed,
 * so keys are redrawn on state transitions rather than every poll. Command
 * responses carry the fresh state too, which makes presses feel instant.
 */
class TrackmindClient extends EventEmitter<{ change: [] }> {
	status: TrackmindStatus | null = null;
	online = false;
	lastError: string | null = null;
	/** Preset most recently recalled from the deck — shown as "on air" until tracking takes over. */
	onAirPreset: number | null = null;

	#conn: Connection = { ...DEFAULT_CONNECTION };
	#agent = new http.Agent({ keepAlive: true, maxSockets: 4 });
	#timer: NodeJS.Timeout | undefined;
	#fingerprint = "";

	constructor() {
		super();
		this.setMaxListeners(50); // one listener per action class, plus the inspector bridge
	}

	get connection(): Connection {
		return { ...this.#conn };
	}

	configure(conn: Partial<Connection>): void {
		const host = (conn.host ?? "").trim() || DEFAULT_CONNECTION.host;
		const port = Number(conn.port) > 0 && Number(conn.port) < 65536 ? Number(conn.port) : DEFAULT_CONNECTION.port;
		if (host === this.#conn.host && port === this.#conn.port) return;
		this.#conn = { host, port };
		streamDeck.logger.info(`Trackmind endpoint → http://${host}:${port}`);
		this.refresh();
	}

	start(): void {
		if (!this.#timer) this.#schedule(0);
	}

	/** Poll right now (e.g. after a settings change or a status-key press). */
	refresh(): void {
		this.#schedule(0);
	}

	async send(command: string, body: Record<string, unknown> = {}): Promise<CommandResult> {
		try {
			const res = await this.#request<CommandResult>("POST", `/api/${command}`, body);
			if (res.state) this.#accept(res.state);
			if (res.ok) {
				if (command === "preset") this.onAirPreset = Number(body.preset);
				else if (command === "home") this.onAirPreset = this.status?.home_preset ?? null;
				else if (command === "tracking" || command === "move") this.onAirPreset = null;
				this.#emitIfChanged(true);
			}
			return res;
		} catch (err) {
			this.#fail(err);
			return { ok: false, error: this.lastError ?? "Trackmind unreachable" };
		}
	}

	#schedule(ms: number): void {
		clearTimeout(this.#timer);
		this.#timer = setTimeout(() => void this.#poll(), ms);
	}

	async #poll(): Promise<void> {
		try {
			this.#accept(await this.#request<TrackmindStatus>("GET", "/api/status"));
			this.#emitIfChanged();
		} catch (err) {
			this.#fail(err);
		}
		this.#schedule(this.online ? POLL_ONLINE_MS : POLL_OFFLINE_MS);
	}

	#accept(status: TrackmindStatus): void {
		this.status = status;
		this.online = true;
		this.lastError = null;
		if (status.tracking || status.manual) this.onAirPreset = null;
	}

	#fail(err: unknown): void {
		const msg = err instanceof Error ? err.message : String(err);
		if (this.online) streamDeck.logger.warn(`Trackmind went offline: ${msg}`);
		this.online = false;
		this.lastError = msg;
		this.#emitIfChanged();
	}

	#emitIfChanged(force = false): void {
		const fp = JSON.stringify([this.online, this.status, this.onAirPreset]);
		if (!force && fp === this.#fingerprint) return;
		this.#fingerprint = fp;
		this.emit("change");
	}

	#request<T>(method: "GET" | "POST", path: string, body?: unknown): Promise<T> {
		const payload = body === undefined ? undefined : JSON.stringify(body);
		return new Promise<T>((resolve, reject) => {
			const req = http.request(
				{
					host: this.#conn.host,
					port: this.#conn.port,
					path,
					method,
					agent: this.#agent,
					timeout: TIMEOUT_MS,
					headers: payload
						? { "Content-Type": "application/json", "Content-Length": Buffer.byteLength(payload) }
						: undefined,
				},
				(res) => {
					let raw = "";
					res.setEncoding("utf8");
					res.on("data", (chunk) => (raw += chunk));
					res.on("end", () => {
						try {
							const json = JSON.parse(raw) as T & { app?: string; state?: unknown };
							// 4xx/5xx still carry a JSON body with {ok:false,error}
							if (method === "GET" && (res.statusCode !== 200 || json.app !== "trackmind")) {
								reject(new Error(`Not Trackmind (HTTP ${res.statusCode})`));
							} else {
								resolve(json);
							}
						} catch {
							reject(new Error(`Unexpected reply (HTTP ${res.statusCode})`));
						}
					});
				},
			);
			req.on("timeout", () => req.destroy(new Error("timed out")));
			req.on("error", (err: NodeJS.ErrnoException) =>
				reject(new Error(err.code === "ECONNREFUSED" ? "Trackmind isn't running" : err.message)),
			);
			req.end(payload);
		});
	}
}

export const trackmind = new TrackmindClient();
