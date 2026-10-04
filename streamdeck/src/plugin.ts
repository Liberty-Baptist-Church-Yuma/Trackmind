import streamDeck from "@elgato/streamdeck";

import { HomeAction, MoveAction, PresetAction, ProfileAction, StatusAction, ZoomAction } from "./actions/camera";
import { TrackingDialAction } from "./actions/dial";
import { AnchorAction, AutoAction, AutoZoomAction, LockAction, MotionSyncAction, TrackingAction } from "./actions/toggles";
import { initRaster } from "./render/raster";
import { trackmind, type Connection } from "./trackmind";

streamDeck.logger.setLevel("info");

streamDeck.actions.registerAction(new TrackingAction());
streamDeck.actions.registerAction(new LockAction());
streamDeck.actions.registerAction(new AutoAction());
streamDeck.actions.registerAction(new AutoZoomAction());
streamDeck.actions.registerAction(new MotionSyncAction());
streamDeck.actions.registerAction(new AnchorAction());
streamDeck.actions.registerAction(new PresetAction());
streamDeck.actions.registerAction(new HomeAction());
streamDeck.actions.registerAction(new ProfileAction());
streamDeck.actions.registerAction(new MoveAction());
streamDeck.actions.registerAction(new ZoomAction());
streamDeck.actions.registerAction(new StatusAction());
streamDeck.actions.registerAction(new TrackingDialAction());

// ── Property inspector bridge ────────────────────────────────
// The inspector never talks to Trackmind directly; it asks the plugin, which
// already holds the live state (connection health, profile list, …).

type InspectorMessage = { type: "hello" } | { type: "test" };

function pushToInspector(): void {
	if (!streamDeck.ui.action) return;
	void streamDeck.ui.sendToPropertyInspector({
		type: "status",
		online: trackmind.online,
		error: trackmind.lastError,
		connection: trackmind.connection,
		status: trackmind.status,
	});
}

trackmind.on("change", pushToInspector);

streamDeck.ui.onSendToPlugin<InspectorMessage>((ev) => {
	if (ev.payload.type === "test") trackmind.refresh();
	pushToInspector();
});

streamDeck.ui.onDidAppear(() => pushToInspector());

// ── Connection settings (shared by every key) ────────────────

streamDeck.settings.onDidReceiveGlobalSettings<Connection>((ev) => trackmind.configure(ev.settings));

// Warm up the key renderer (WASM + fonts) while Stream Deck connects.
void initRaster().catch((err) => streamDeck.logger.error(`Renderer failed to start: ${err}`));

await streamDeck.connect();
trackmind.configure(await streamDeck.settings.getGlobalSettings<Connection>());
trackmind.start();
