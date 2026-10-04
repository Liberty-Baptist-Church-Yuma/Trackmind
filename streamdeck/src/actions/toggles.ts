import { action, type KeyDownEvent } from "@elgato/streamdeck";

import type { TrackmindStatus } from "../trackmind";
import { TrackmindAction, type ToggleMode, type View } from "./base";

type ToggleSettings = { mode?: ToggleMode };

/** Stream-state caption shared by keys that need a live camera. */
function streamCaption(s: TrackmindStatus): string | null {
	if (s.stream === "connecting") return "Connecting…";
	if (s.stream === "offline") return s.error ? "Stream error" : "No stream";
	return null;
}

abstract class ToggleAction extends TrackmindAction<ToggleSettings> {
	protected abstract readonly command: string;

	override async onKeyDown(ev: KeyDownEvent<ToggleSettings>): Promise<void> {
		await this.run(ev.action, this.command, { state: ev.payload.settings.mode ?? "toggle" });
	}
}

@action({ UUID: "com.coder747.trackmind.tracking" })
export class TrackingAction extends ToggleAction {
	protected readonly command = "tracking";

	protected view(_: ToggleSettings, s: TrackmindStatus | null): View {
		if (!s) return { key: { tone: "green", active: false, icon: "tracking", label: "Tracking" } };
		const waiting = streamCaption(s);
		if (s.tracking) {
			return {
				key: {
					tone: "green",
					active: true,
					icon: "tracking",
					label: "Tracking",
					sub: s.locked ? "Locked on" : s.subject ? "Subject" : "Searching",
					subInk: s.subject || s.locked ? "tone" : "warn",
					corner: s.subject ? "green" : "amber",
				},
			};
		}
		return {
			key: {
				tone: "green",
				active: false,
				icon: "tracking",
				label: "Tracking",
				sub: waiting ?? (s.manual ? "Manual" : "Paused"),
				subInk: waiting ? "warn" : "dim",
			},
		};
	}
}

@action({ UUID: "com.coder747.trackmind.lock" })
export class LockAction extends ToggleAction {
	protected readonly command = "lock";

	protected view(_: ToggleSettings, s: TrackmindStatus | null): View {
		if (s?.locked) {
			return { key: { tone: "amber", active: true, icon: "lock", label: "Locked", sub: "On subject" } };
		}
		return {
			key: {
				tone: "amber",
				active: false,
				icon: "unlock",
				label: "Lock",
				sub: s && !s.tracking ? "Needs tracking" : "Off",
			},
		};
	}
}

type AutoSettings = ToggleSettings & { style?: "keep" | "calm" | "balanced" | "responsive" };

const AUTO_SITUATION = { still: "Steady", walking: "Following", fast: "Catching up" } as const;

@action({ UUID: "com.coder747.trackmind.auto" })
export class AutoAction extends TrackmindAction<AutoSettings> {
	override async onKeyDown(ev: KeyDownEvent<AutoSettings>): Promise<void> {
		const { mode, style } = ev.payload.settings;
		const body: Record<string, string> = { state: mode ?? "toggle" };
		if (style && style !== "keep") body.style = style;
		await this.run(ev.action, "auto", body);
	}

	protected view(_: AutoSettings, s: TrackmindStatus | null): View {
		const base = { tone: "green", icon: "auto", label: "Auto" } as const;
		const a = s?.auto;
		if (s && !a) return { key: { ...base, active: false, sub: "Update Trackmind", subInk: "warn" } };
		if (!a?.enabled) return { key: { ...base, active: false, sub: "Off" } };
		if (!s?.tracking) return { key: { ...base, active: true, sub: "On · paused", subInk: "dim" } };
		if (!a.learned) return { key: { ...base, active: true, sub: "Learning", subInk: "warn", corner: "amber" } };
		const sit = a.situation ? AUTO_SITUATION[a.situation] : "On";
		const eased = (a.soften ?? 1) < 0.9;
		return { key: { ...base, active: true, sub: sit, corner: eased ? "amber" : "green" } };
	}
}

@action({ UUID: "com.coder747.trackmind.autozoom" })
export class AutoZoomAction extends ToggleAction {
	protected readonly command = "autozoom";

	protected view(_: ToggleSettings, s: TrackmindStatus | null): View {
		const on = !!s?.autozoom;
		const sub = !on ? "Off" : s?.zooming === 1 ? "Zooming in" : s?.zooming === -1 ? "Zooming out" : "On";
		return { key: { tone: "blue", active: on, icon: "autozoom", label: "Auto-Zoom", sub } };
	}
}

@action({ UUID: "com.coder747.trackmind.anchor" })
export class AnchorAction extends ToggleAction {
	protected readonly command = "anchor";

	protected view(_: ToggleSettings, s: TrackmindStatus | null): View {
		const a = s?.anchor;
		const base = { tone: "amber", icon: "pulpit", label: "Pulpit" } as const;
		if (s && !a) return { key: { ...base, active: false, sub: "Update Trackmind", subInk: "warn" } };
		if (!a?.enabled) {
			return { key: { ...base, active: false, sub: a && !a.learned ? "Not learned" : "Off", subInk: a && !a.learned ? "warn" : "dim" } };
		}
		if (!a.learned) return { key: { ...base, active: false, sub: "Not learned", subInk: "warn" } };
		if (a.state === "held") return { key: { ...base, active: true, label: "Pulpit", sub: "Holding", corner: "green" } };
		if (a.state === "snapping") return { key: { ...base, active: true, sub: a.mode === "glide" ? "Gliding…" : "Snapping…" } };
		if (a.state === "free") {
			const near = a.offset !== null && a.offset <= a.range;
			return { key: { ...base, active: true, sub: near ? "In range" : "Armed", corner: near ? "amber" : undefined } };
		}
		return { key: { ...base, active: true, sub: "On · paused", subInk: "dim" } };
	}
}

@action({ UUID: "com.coder747.trackmind.motionsync" })
export class MotionSyncAction extends ToggleAction {
	protected readonly command = "motion-sync";

	protected view(_: ToggleSettings, s: TrackmindStatus | null): View {
		const on = !!s?.motion_sync;
		return { key: { tone: "blue", active: on, icon: "motionSync", label: "Motion Sync", sub: on ? "On" : "Off" } };
	}
}
