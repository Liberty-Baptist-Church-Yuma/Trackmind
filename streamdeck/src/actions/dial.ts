import {
	action,
	type DialAction,
	type DialDownEvent,
	type DialRotateEvent,
	type TouchTapEvent,
} from "@elgato/streamdeck";

import { trackmind, type TrackmindStatus, type Values } from "../trackmind";
import { TrackmindAction, type View } from "./base";

type Param = keyof Values;

const PARAMS: Record<Param, { name: string; min: number; max: number; centred?: boolean; fmt: (v: number) => string }> = {
	track_offset: { name: "Vertical offset", min: -7, max: 7, centred: true, fmt: (v) => (v > 0 ? `+${v}` : `${v}`) },
	motion_smooth: { name: "Motion smoothing", min: 0, max: 10, fmt: (v) => `${v}` },
	zoom_target: { name: "Zoom target", min: 20, max: 90, fmt: (v) => `${v}%` },
	zoom_speed: { name: "Zoom speed", min: 0, max: 7, fmt: (v) => `${v}` },
	wide_dead: { name: "Wide tracking spacing", min: 20, max: 49, fmt: (v) => `${v}%` },
};

type DialSettings = { param?: Param; step?: number };

/**
 * Stream Deck+ dial: twist to fine-tune a tracking value live, push to toggle
 * tracking, tap the strip to lock onto the subject.
 *
 * Rotation is applied optimistically (the strip moves immediately) and ticks
 * are coalesced so a fast spin sends one request at a time, not dozens.
 */
@action({ UUID: "com.coder747.trackmind.dial" })
export class TrackingDialAction extends TrackmindAction<DialSettings> {
	#pending = new Map<string, number>();
	#inflight = new Set<string>();
	#optimistic = new Map<string, number>();

	override onDialRotate(ev: DialRotateEvent<DialSettings>): void {
		const param = ev.payload.settings.param ?? "track_offset";
		const step = Math.max(1, ev.payload.settings.step ?? 1);
		const spec = PARAMS[param];
		const base = this.#optimistic.get(ev.action.id) ?? trackmind.status?.values[param] ?? 0;
		this.#optimistic.set(ev.action.id, Math.max(spec.min, Math.min(spec.max, base + ev.payload.ticks * step)));
		this.#pending.set(ev.action.id, (this.#pending.get(ev.action.id) ?? 0) + ev.payload.ticks * step);
		void this.redraw(ev.action);
		void this.#flush(ev.action, param);
	}

	async #flush(dial: DialAction<DialSettings>, param: Param): Promise<void> {
		if (this.#inflight.has(dial.id)) return;
		const delta = this.#pending.get(dial.id) ?? 0;
		if (!delta) {
			this.#optimistic.delete(dial.id);
			void this.redraw(dial);
			return;
		}
		this.#pending.set(dial.id, 0);
		this.#inflight.add(dial.id);
		const res = await this.run(dial, "adjust", { key: param, delta });
		this.#inflight.delete(dial.id);
		if (!res.ok) {
			this.#pending.delete(dial.id);
			this.#optimistic.delete(dial.id);
			void this.redraw(dial);
			return;
		}
		void this.#flush(dial, param);
	}

	override async onDialDown(ev: DialDownEvent<DialSettings>): Promise<void> {
		await this.run(ev.action, "tracking", { state: "toggle" });
	}

	override async onTouchTap(ev: TouchTapEvent<DialSettings>): Promise<void> {
		await this.run(ev.action, "lock", { state: "toggle" });
	}

	protected view(settings: DialSettings, s: TrackmindStatus | null, id: string): View {
		const param = settings.param ?? "track_offset";
		const spec = PARAMS[param] ?? PARAMS.track_offset;
		const value = this.#optimistic.get(id) ?? s?.values[param] ?? 0;
		const status = !s
			? "OFFLINE"
			: s.stream !== "live"
				? "CONNECTING"
				: s.tracking
					? s.subject
						? "TRACKING"
						: "SEARCHING"
					: "PAUSED";
		return {
			dial: {
				tone: "green",
				active: !!s?.tracking,
				status,
				name: spec.name,
				value: spec.fmt(value),
				min: spec.min,
				max: spec.max,
				current: value,
				centred: spec.centred,
				locked: !!s?.locked,
			},
		};
	}
}
