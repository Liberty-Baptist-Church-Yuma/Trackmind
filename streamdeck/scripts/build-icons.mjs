/**
 * Generates the plugin's static images from the same renderer the live keys
 * use, so the action list, default key art, and previews always match what
 * shows on the deck. Run with `npm run icons` after changing src/render.
 *
 *   --preview <file.png>   also write a contact sheet of every key state
 *
 * (The marketplace and category icons come from ../branding.)
 */
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import url from "node:url";

import { Resvg } from "@resvg/resvg-js";
import ts from "typescript";

const root = path.dirname(path.dirname(url.fileURLToPath(import.meta.url)));
const plugin = path.join(root, "com.coder747.trackmind.sdPlugin");
const out = path.join(plugin, "imgs");
const core = url.pathToFileURL(path.join(root, "..", "ui", "js", "liquid-glass-core.js")).href;

// Transpile the render modules into a temp dir as plain ESM.
const tmp = fs.mkdtempSync(path.join(os.tmpdir(), "trackmind-icons-"));
for (const name of ["palette", "icons", "glass"]) {
	const src = fs.readFileSync(path.join(root, "src", "render", `${name}.ts`), "utf8");
	const { outputText } = ts.transpileModule(src, {
		compilerOptions: { module: ts.ModuleKind.ES2022, target: ts.ScriptTarget.ES2022 },
	});
	fs.writeFileSync(
		path.join(tmp, `${name}.mjs`),
		outputText.replace(/from "\.\.\/\.\.\/\.\.\/ui\/js\/liquid-glass-core\.js"/, `from "${core}"`).replace(/from "\.\/(\w+)"/g, 'from "./$1.mjs"'),
	);
}
const { keyLayers, dialLayers } = await import(url.pathToFileURL(path.join(tmp, "glass.mjs")).href);
const { icons } = await import(url.pathToFileURL(path.join(tmp, "icons.mjs")).href);

const fontFiles = fs.readdirSync(path.join(plugin, "fonts")).filter((f) => f.endsWith(".ttf")).map((f) => path.join(plugin, "fonts", f));
const raster = (svg, width) =>
	new Resvg(svg, { fitTo: { mode: "width", value: width }, font: { loadSystemFonts: false, fontFiles, defaultFontFamily: "Inter Display" } }).render();

/** Render layered art (glass backdrop + overlay) at `width` px. */
const compose = (layers, width) => {
	const bg = raster(layers.backdrop, width).asPng().toString("base64");
	return raster(layers.overlay(`data:image/png;base64,${bg}`), width);
};

const write = (rel, data) => {
	const file = path.join(out, rel);
	fs.mkdirSync(path.dirname(file), { recursive: true });
	fs.writeFileSync(file, data);
};

/** Monochrome list icon (white on transparent), 24-unit grid. */
const listIcon = (fn, size) => `<svg xmlns="http://www.w3.org/2000/svg" width="${size}" height="${size}" viewBox="0 0 24 24">${fn("#ffffff", "#ffffff")}</svg>\n`;

const actions = {
	tracking: { icon: "tracking", key: { tone: "green", active: false, icon: "tracking", label: "Tracking", sub: "Off" } },
	lock: { icon: "lock", key: { tone: "amber", active: false, icon: "unlock", label: "Lock", sub: "Off" } },
	auto: { icon: "auto", key: { tone: "green", active: false, icon: "auto", label: "Auto", sub: "Off" } },
	autozoom: { icon: "autozoom", key: { tone: "blue", active: false, icon: "autozoom", label: "Auto-Zoom", sub: "Off" } },
	anchor: { icon: "pulpit", key: { tone: "amber", active: false, icon: "pulpit", label: "Pulpit", sub: "Off" } },
	motionsync: { icon: "motionSync", key: { tone: "blue", active: false, icon: "motionSync", label: "Motion Sync", sub: "Off" } },
	preset: { icon: "camera", key: { tone: "red", active: false, glyph: "1", label: "Preset", sub: "Recall" } },
	home: { icon: "home", key: { tone: "red", active: false, icon: "home", label: "Home", sub: "Preset" } },
	profile: { icon: "profile", key: { tone: "blue", active: false, icon: "profile", label: "Profile", sub: "Load" } },
	ptz: { icon: "arrow", key: { tone: "blue", active: false, icon: "arrow", direction: "up", label: "Tilt Up", sub: "Hold" } },
	zoom: { icon: "zoomIn", key: { tone: "blue", active: false, icon: "zoomIn", label: "Zoom In", sub: "Hold" } },
	status: { icon: "camera", key: { tone: "green", active: true, icon: "camera", label: "Trackmind", sub: "Status" } },
	dial: { icon: "tracking", key: { tone: "green", active: true, icon: "tracking", label: "Tracking", sub: "Dial" } },
};

for (const [name, { icon, key }] of Object.entries(actions)) {
	const dir = `actions/${name}`;
	for (const f of fs.existsSync(path.join(out, dir)) ? fs.readdirSync(path.join(out, dir)) : []) fs.rmSync(path.join(out, dir, f));
	write(`${dir}/icon.svg`, listIcon(icons[icon], 20));
	const layers = keyLayers(key);
	write(`${dir}/key.png`, compose(layers, 72).asPng());
	write(`${dir}/key@2x.png`, compose(layers, 144).asPng());
}

const i = process.argv.indexOf("--preview");
if (i > 0) {
	const keys = [
		{ tone: "green", active: true, icon: "tracking", label: "Tracking", sub: "Subject", corner: "green" },
		{ tone: "green", active: true, icon: "tracking", label: "Tracking", sub: "Searching", subInk: "warn", corner: "amber" },
		{ tone: "green", active: false, icon: "tracking", label: "Tracking", sub: "Off" },
		{ tone: "amber", active: true, icon: "lock", label: "Locked", sub: "On subject" },
		{ tone: "amber", active: false, icon: "unlock", label: "Lock", sub: "Off" },
		{ tone: "green", active: true, icon: "auto", label: "Auto", sub: "Following", corner: "green" },
		{ tone: "blue", active: true, icon: "autozoom", label: "Auto-Zoom", sub: "On" },
		{ tone: "red", active: true, glyph: "4", label: "Pulpit", sub: "On air" },
		{ tone: "red", active: false, glyph: "12", label: "Choir Loft", sub: "Preset 12" },
		{ tone: "red", active: false, icon: "home", label: "Home", sub: "Preset 0" },
		{ tone: "blue", active: true, icon: "profile", label: "Sunday AM", sub: "Active" },
		{ tone: "blue", active: false, icon: "motionSync", label: "Motion Sync", sub: "Off" },
		{ tone: "amber", active: true, icon: "pulpit", label: "Pulpit", sub: "Holding", corner: "green" },
		{ tone: "blue", active: true, icon: "arrow", direction: "up-right", label: "Up-Right", sub: "Speed 9" },
		{ tone: "blue", active: false, icon: "zoomIn", label: "Zoom In", sub: "Speed 3" },
		{ tone: "green", active: true, icon: "camera", label: "Tracking", sub: "192.168.100.88", corner: "green" },
		{ tone: "green", active: false, icon: "tracking", label: "Tracking", offline: true },
	];
	const dials = [
		{ tone: "green", active: true, status: "TRACKING", name: "Vertical offset", value: "+2", min: -7, max: 7, current: 2, centred: true, locked: true },
		{ tone: "green", active: false, status: "PAUSED", name: "Motion smoothing", value: "5", min: 0, max: 10, current: 5 },
		{ tone: "green", active: false, offline: true, status: "", name: "Zoom target", value: "45%", min: 20, max: 90, current: 45 },
	];
	const cols = 5;
	const cell = 162;
	const rows = Math.ceil(keys.length / cols);
	const W = 36 + cols * cell;
	const H = 36 + rows * cell + 130;
	let body = "";
	keys.forEach((k, n) => {
		const png = compose(keyLayers(k), 288).asPng().toString("base64");
		body += `<image x="${18 + (n % cols) * cell}" y="${18 + Math.floor(n / cols) * cell}" width="144" height="144" href="data:image/png;base64,${png}"/>`;
	});
	dials.forEach((d, n) => {
		const png = compose(dialLayers(d), 400).asPng().toString("base64");
		body += `<image x="${18 + n * 216}" y="${28 + rows * cell}" width="200" height="100" href="data:image/png;base64,${png}"/>`;
	});
	const sheet = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}"><rect width="100%" height="100%" rx="24" fill="#111215"/>${body}</svg>`;
	fs.writeFileSync(process.argv[i + 1], new Resvg(sheet, { fitTo: { mode: "width", value: W * 2 } }).render().asPng());
}

fs.rmSync(tmp, { recursive: true, force: true });
console.log(`Wrote icons to ${path.relative(root, out)}`);
