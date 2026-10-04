/**
 * Key icons on a 24-unit grid — the same set as the desktop app's sprite
 * (ui/index.html), so the deck and the app speak one visual language.
 * Each icon takes an ink colour and an accent for its "lit" detail.
 */
export type IconFn = (ink: string, accent: string) => string;

const stroke = (d: string, ink: string, w = 2): string =>
	`<path d="${d}" fill="none" stroke="${ink}" stroke-width="${w}" stroke-linecap="round" stroke-linejoin="round"/>`;

export const icons = {
	/** Tracking frame + subject with motion echo — the Trackmind mark. */
	tracking: (ink, accent) =>
		stroke("M3 8V6a3 3 0 0 1 3-3h2M16 3h2a3 3 0 0 1 3 3v2M21 16v2a3 3 0 0 1-3 3h-2M8 21H6a3 3 0 0 1-3-3v-2", ink) +
		`<circle cx="7.3" cy="12" r="1.05" fill="${accent}" opacity=".35"/><circle cx="9.6" cy="12" r="1.7" fill="${accent}" opacity=".6"/><circle cx="13.6" cy="12" r="3.2" fill="${accent}"/>`,

	lock: (ink, accent) =>
		stroke("M8 11V8a4 4 0 0 1 8 0v3", ink) +
		`<rect x="5" y="11" width="14" height="10" rx="3" fill="none" stroke="${ink}" stroke-width="2"/><circle cx="12" cy="16" r="1.8" fill="${accent}"/>`,

	unlock: (ink, accent) =>
		stroke("M8 11V8a4 4 0 0 1 7.7-1.5", ink) +
		`<rect x="5" y="11" width="14" height="10" rx="3" fill="none" stroke="${ink}" stroke-width="2"/><circle cx="12" cy="16" r="1.8" fill="${accent}"/>`,

	autozoom: (ink, accent) =>
		stroke("M3 7.5V5.5a2.5 2.5 0 0 1 2.5-2.5h2M16.5 3h2A2.5 2.5 0 0 1 21 5.5v2M21 16.5v2a2.5 2.5 0 0 1-2.5 2.5h-2M7.5 21h-2A2.5 2.5 0 0 1 3 18.5v-2", ink) +
		`<circle cx="11" cy="11" r="4" fill="none" stroke="${accent}" stroke-width="2"/>` +
		stroke("M14 14l3 3", accent, 2.3),

	/** Auto mode — the app's i-sparkle: a big spark (ink) and a small one (accent). */
	auto: (ink, accent) =>
		`<path d="M11 3l1.9 5.6L18.5 10.5l-5.6 1.9L11 18l-1.9-5.6L3.5 10.5l5.6-1.9z" fill="none" stroke="${ink}" stroke-width="2" stroke-linejoin="round"/>` +
		`<path d="M18.5 14.5l.9 2.6 2.6.9-2.6.9-.9 2.6-.9-2.6-2.6-.9 2.6-.9z" fill="${accent}"/>`,

	motionSync: (ink, accent) =>
		stroke("M3 7h11M3 12h11M3 17h11", ink) +
		stroke("M12 4.5L14.5 7 12 9.5M12 9.5l2.5 2.5-2.5 2.5M12 14.5l2.5 2.5-2.5 2.5", ink) +
		stroke("M19.5 3.5v17", accent, 2.5),

	/** Lectern with the speaker's head above it — same as the app's i-pulpit. */
	pulpit: (ink, accent) =>
		stroke("M4.5 9.5L19.5 7v3.2L4.5 12.7z", ink) + stroke("M12 12v8M8 20.5h8", ink) + `<circle cx="12" cy="4" r="1.8" fill="${accent}"/>`,

	home: (ink, accent) =>
		stroke("M3.5 11L12 4l8.5 7M6 9.5V20h12V9.5", ink) + `<rect x="10" y="14" width="4" height="6" rx="1" fill="${accent}"/>`,

	profile: (ink, accent) =>
		`<path d="M12 3l9 4.5-9 4.5-9-4.5z" fill="${accent}" stroke="${accent}" stroke-width="1.6" stroke-linejoin="round"/>` +
		stroke("M3 12l9 4.5 9-4.5M3 16.5L12 21l9-4.5", ink),

	camera: (ink, accent) =>
		`<rect x="3" y="4" width="18" height="11" rx="4" fill="none" stroke="${ink}" stroke-width="2"/><circle cx="12" cy="9.5" r="2.5" fill="${accent}"/>` +
		stroke("M12 15v3M7.5 20.5h9M9 20.5l1.2-2.5h3.6l1.2 2.5", ink, 1.8),

	/** Arrow pointing up; rotated for other directions. */
	arrow: (ink, accent) => stroke("M12 19V5.5M6 11.5l6-6 6 6", accent, 2.4),

	zoomIn: (ink, accent) =>
		`<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="${ink}" stroke-width="2"/>` + stroke("M15.5 15.5L20 20", ink, 2.4) + stroke("M10.5 7.5v6M7.5 10.5h6", accent, 2),

	zoomOut: (ink, accent) =>
		`<circle cx="10.5" cy="10.5" r="6.5" fill="none" stroke="${ink}" stroke-width="2"/>` + stroke("M15.5 15.5L20 20", ink, 2.4) + stroke("M7.5 10.5h6", accent, 2),

	offline: (ink, accent) =>
		`<circle cx="12" cy="12" r="8.5" fill="none" stroke="${ink}" stroke-width="1.8" stroke-dasharray="2.6 2.6"/>` + stroke("M9 9l6 6M15 9l-6 6", accent, 2),
} satisfies Record<string, IconFn>;

export type IconName = keyof typeof icons;

/** Degrees of rotation for {@link icons.arrow}, keyed by direction. */
export const ARROW_ANGLE: Record<string, number> = {
	up: 0,
	"up-right": 45,
	right: 90,
	"down-right": 135,
	down: 180,
	"down-left": 225,
	left: 270,
	"up-left": 315,
};
