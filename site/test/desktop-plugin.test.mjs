// The desktop half of the Hermes plugin (plugin/desktop/plugin.js) loads uncompiled in the Hermes
// desktop app, which resolves only '@hermes/plugin-sdk', 'react' and 'react/jsx-runtime'. These
// tests load it the same way, with those three specifiers swapped for small stand-ins, register it
// against a fake plugin context, and render its page to text.

import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

const SOURCE = new URL("../../plugin/desktop/plugin.js", import.meta.url);
const ALLOWED = new Set(["@hermes/plugin-sdk", "react", "react/jsx-runtime"]);

const JSX = `
export const jsx = (type, props, key) => ({ type, props: props || {}, key });
export const jsxs = jsx;
export const Fragment = "fragment";
`;

// Each SDK component renders as an element named after it; hooks answer from globalThis.__sdk.
const SDK = `
const el = (name) => (props) => ({ type: name, props });
export const Badge = el("Badge");
export const Button = el("Button");
export const EmptyState = (p) => ({ type: "EmptyState", props: { children: [p.title, " ", p.description] } });
export const ErrorState = (p) => ({ type: "ErrorState", props: { children: [p.title, " ", p.description] } });
export const host = { navigate: (path) => globalThis.__sdk.navigated.push(path) };
export const PALETTE_AREA = "palette";
export const ROUTES_AREA = "routes";
export const SIDEBAR_NAV_AREA = "sidebar.nav";
export const relativeTime = (ms) => "ago:" + ms;
export const useQuery = (options) => globalThis.__sdk.useQuery(options);
`;

const dataUrl = (code) => "data:text/javascript;base64," + Buffer.from(code).toString("base64");

function importSpecifiers(source) {
  return [...source.matchAll(/\bfrom\s+['"]([^'"]+)['"]/g)].map((m) => m[1]);
}

async function loadPlugin() {
  const source = await readFile(SOURCE, "utf8");
  const stubs = { "@hermes/plugin-sdk": SDK, "react/jsx-runtime": JSX };
  const rewritten = source.replace(/\bfrom\s+['"]([^'"]+)['"]/g, (_, spec) => `from '${dataUrl(stubs[spec])}'`);
  return import(dataUrl(rewritten));
}

// Render an element tree to text, calling function components on the way.
function text(node) {
  if (node === null || node === undefined || node === false || node === true) return "";
  if (typeof node === "string" || typeof node === "number") return String(node);
  if (Array.isArray(node)) return node.map(text).join(" ");
  if (typeof node.type === "function") return text(node.type(node.props));
  return text(node.props.children);
}

function find(node, predicate) {
  if (!node || typeof node !== "object") return null;
  if (Array.isArray(node)) {
    for (const child of node) {
      const hit = find(child, predicate);
      if (hit) return hit;
    }
    return null;
  }
  if (predicate(node)) return node;
  if (typeof node.type === "function") return find(node.type(node.props), predicate);
  return find(node.props.children, predicate);
}

const DEVICES = {
  gateway: { running: true, updated_at: 1000, stale_after_s: 90 },
  devices: [
    { id: "hg-desk", name: "Desk", board: "esp32s3-touch-amoled-206", firmware: "0.4.1", online: true,
      connected_at: 990, last_seen: 900, pending: false,
      battery: { battery_percent: 100, charging: false, external_power: true } },
    { id: "hg-attic", name: "Attic", board: "sim-320x240", firmware: "0.4.1", online: false,
      last_seen: 500, pending: false, battery: null },
    { id: "hg-new", name: "", board: "", firmware: "", online: true, connected_at: 995, last_seen: null,
      pending: true, battery: null },
  ],
};

function register(plugin) {
  const registered = [];
  const calls = [];
  const ctx = {
    rest: (path, opts) => {
      calls.push([path, opts]);
      return Promise.resolve(DEVICES);
    },
    registerMany: (items) => registered.push(...items),
    register: (item) => registered.push(item),
  };
  plugin.register(ctx);
  return { registered, calls };
}

test("the desktop plugin imports only what the desktop app can resolve", async () => {
  const specifiers = importSpecifiers(await readFile(SOURCE, "utf8"));
  assert.ok(specifiers.length > 0);
  for (const spec of specifiers) assert.ok(ALLOWED.has(spec), `plugin/desktop/plugin.js imports ${spec}`);
});

test("the desktop plugin adds a Gadgets page with a sidebar entry and a palette command", async () => {
  const plugin = (await loadPlugin()).default;
  // The id is the backend namespace: ctx.rest('/devices') reaches /api/plugins/<id>/devices.
  assert.equal(plugin.id, "gadget");
  const { registered } = register(plugin);
  const byArea = Object.fromEntries(registered.map((item) => [item.area, item]));
  assert.deepEqual(byArea.routes.data, { path: "/gadget" });
  assert.deepEqual(byArea["sidebar.nav"].data, { codicon: "watch", label: "Gadgets", path: "/gadget" });
  globalThis.__sdk = { navigated: [] };
  byArea.palette.data.run();
  assert.deepEqual(globalThis.__sdk.navigated, ["/gadget"]);
});

test("the Gadgets page reads /devices and shows each device", async () => {
  const plugin = (await loadPlugin()).default;
  const { registered, calls } = register(plugin);
  const page = registered.find((item) => item.area === "routes");
  let options;
  globalThis.__sdk = {
    navigated: [],
    useQuery: (o) => {
      options = o;
      return { data: DEVICES, error: null, isFetching: false, refetch: () => {} };
    },
  };
  const shown = text(page.render());
  await options.queryFn();
  assert.deepEqual(calls, [["/devices", { timeoutMs: 15000 }]]);
  assert.ok(options.refetchInterval >= 5000, "polls no faster than every few seconds");
  assert.match(shown, /3 known, 2 online\./);
  assert.match(shown, /Desk/);
  assert.match(shown, /100%, on USB/);
  assert.match(shown, /esp32s3-touch-amoled-206/);
  assert.match(shown, /Online/);
  assert.match(shown, /Offline/);
  assert.match(shown, /Online Waiting to pair/); // connected, not yet approved
  assert.match(shown, /ago:500000/); // Attic's last seen, in milliseconds
  assert.doesNotMatch(shown, /not running/);
});

test("the Gadgets page says when the gateway is stopped, and when the route fails", async () => {
  const { GadgetsView } = await loadPlugin();
  const stopped = { ...DEVICES, gateway: { running: false, updated_at: 700 } };
  assert.match(text(GadgetsView({ data: stopped, error: null, loading: false })), /gateway is not running.*ago:700000/s);
  const failed = text(GadgetsView({ data: undefined, error: new Error("401 Unauthorized"), loading: false }));
  assert.match(failed, /Could not load the devices/);
  assert.match(failed, /401 Unauthorized/);
  assert.match(text(GadgetsView({ data: { gateway: {}, devices: [] }, loading: false })), /No gadgets yet/);
  const refresh = find(GadgetsView({ data: DEVICES, loading: true, onRefresh: () => {} }),
    (n) => n.type === "Button");
  assert.equal(refresh.props.disabled, true);
});

test("battery text matches the web tab's", async () => {
  const { batteryText } = await loadPlugin();
  assert.equal(batteryText(null), "-");
  assert.equal(batteryText({ battery_percent: 76.4 }), "76%");
  assert.equal(batteryText({ battery_percent: 94, charging: true }), "94%, charging");
  assert.equal(batteryText({ battery_mv: 3950 }), "3.95 V");
  assert.equal(batteryText({ battery_present: false }), "no battery");
  // The route keeps only finite readings; the page does not trust that either.
  assert.equal(batteryText({ battery_percent: NaN, battery_mv: Infinity }), "-");
});
