"""Behaviour tests for the injected readout, driven through a DOM stub in Node.

``app/web/widget.js`` only runs inside the Command Code renderer, so these
assertions exercise it against a minimal DOM implementation instead of a live
app. Skipped when Node is unavailable.
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

WIDGET = Path(__file__).resolve().parent.parent / "app" / "web" / "widget.js"

NODE_DRIVER = r"""
const fs = require("fs");
const vm = require("vm");
const source = fs.readFileSync(process.argv[2], "utf8");

let VERSION = null;
const OVERVIEW = {
  user: { name: "tester" },
  windowLimits: {
    limited: true,
    fiveHour: { used: 0.223064208, cap: 3, exceeded: false, resetAt: Date.now() + 2 * 3600e3 },
    weekly: { used: 5.6, cap: 6, exceeded: false, resetAt: Date.now() + 3 * 86400e3 },
  },
  credits: { monthlyCredits: 7.2, purchasedCredits: 0, freeCredits: 0 },
  usage: {
    totalCount: 834,
    totalTokens: 351471408,
    totalCost: 2.77562987,
    averageCost: 0.0033280933,
    successRate: 100,
  },
};

class El {
  constructor(tag) {
    this.tagName = String(tag).toUpperCase();
    this.children = [];
    this.parentElement = null;
    this.attrs = {};
    this.dataset = {};
    this.style = { setProperty: (k, v) => { this.style[k] = String(v); } };
    this._class = new Set();
    this.shadow = null;
    this._html = "";
    this._text = "";
    this.id = "";
    this.rect = { top: 0, bottom: 40, left: 0, right: 600, width: 600, height: 40 };
  }
  get classList() {
    const set = this._class;
    return {
      add: (c) => set.add(c),
      remove: (c) => set.delete(c),
      contains: (c) => set.has(c),
      toggle: (c, on) => (on ? set.add(c) : set.delete(c)),
    };
  }
  set className(v) { this._class = new Set(String(v).split(/\s+/).filter(Boolean)); }
  get className() { return [...this._class].join(" "); }
  addEventListener() {}
  removeEventListener() {}
  setAttribute(k, v) { this.attrs[k] = String(v); if (k === "id") this.id = String(v); }
  getAttribute(k) { return this.attrs[k]; }
  append(...nodes) { for (const n of nodes) { n.parentElement = this; this.children.push(n); } }
  insertBefore(node, ref) {
    const index = ref ? this.children.indexOf(ref) : this.children.length;
    node.parentElement = this;
    this.children.splice(index < 0 ? this.children.length : index, 0, node);
    return node;
  }
  remove() {
    if (!this.parentElement) return;
    const i = this.parentElement.children.indexOf(this);
    if (i >= 0) this.parentElement.children.splice(i, 1);
    this.parentElement = null;
  }
  attachShadow() { this.shadow = new El("#shadow-root"); this.shadow.host = this; return this.shadow; }
  get shadowRoot() { return this.shadow; }
  // Width has to react to child count, otherwise shedding chips changes
  // nothing and the degradation path can never be observed.
  // Line-count model so the wrap/trim tiers are observable: ~50px per chip,
  // 24px per line, and a two-line ceiling while wrapping.
  get _naturalWidth() {
    // Bars and countdowns cost real width, otherwise the tier that sheds them
    // could never be observed.
    return 30 + this.children.length * 50 +
      this.querySelectorAll(".bar").length * 20 + this.querySelectorAll(".cd").length * 30;
  }
  get _wrappedLike() {
    const host = this.parentElement && this.parentElement.host;
    return Boolean(host && host.dataset &&
      ["wrapped", "tall", "editing"].indexOf(host.dataset.layout) >= 0);
  }
  get _heightCapped() {
    const host = this.parentElement && this.parentElement.host;
    return Boolean(host && host.dataset && host.dataset.layout === "wrapped");
  }
  get scrollWidth() {
    if (this._naturalWidthOverride !== undefined) return this._naturalWidthOverride;
    return this._wrappedLike ? this.clientWidth : this._naturalWidth;
  }
  set scrollWidth(v) { this._naturalWidthOverride = v; }
  get clientWidth() { return this._clientWidth === undefined ? 600 : this._clientWidth; }
  set clientWidth(v) { this._clientWidth = v; }
  get scrollHeight() {
    const lines = Math.max(1, Math.ceil(this._naturalWidth / Math.max(1, this.clientWidth)));
    return lines * 24;
  }
  get clientHeight() { return this._heightCapped ? 72 : this.scrollHeight; }
  set innerHTML(v) {
    this._html = String(v);
    const parts = [...String(v).matchAll(/<span class="chip([^"]*)"([^>]*)>([\s\S]*?)(?=<span class="chip|<span class="tools|$)/g)];
    this.children = parts.map((m) => {
      const node = new El("span");
      node.className = "chip" + m[1];
      for (const attr of (m[2] || "").matchAll(/([a-z-]+)="([^"]*)"/g)) {
        node.attrs[attr[1]] = attr[2];
        if (attr[1].startsWith("data-")) {
          node.dataset[attr[1].slice(5).replace(/-([a-z])/g, (_x, c) => c.toUpperCase())] = attr[2];
        }
      }
      const label = /^\s*<b>([^<]*)<\/b>/.exec(m[3] || "");
      node.label = label ? label[1] : "";
      node.parentElement = this;   // without this, remove() is a no-op
      const inner = m[3] || "";
      for (const kind of ["bar", "cd", "hide"]) {
        const hits = inner.match(new RegExp('class="' + kind + '[ "]', "g"));
        for (let i = 0; i < (hits ? hits.length : 0); i += 1) {
          const child = new El("span");
          child.className = kind;
          child.parentElement = node;
          node.children.push(child);
        }
      }
      return node;
    });
  }
  get innerHTML() { return this._html; }
  set textContent(v) { this._text = String(v); }
  get textContent() { return this._text; }
  getBoundingClientRect() { return this.rect; }
  querySelector(sel) { return this.querySelectorAll(sel)[0] || null; }
  querySelectorAll(sel) {
    const out = [];
    const walk = (node) => {
      for (const child of node.children) {
        if (matches(child, sel)) out.push(child);
        walk(child);
      }
    };
    walk(this);
    return out;
  }
  getElementById(id) { return findById(this, id); }
}

function findById(node, id) {
  if (node.id === id) return node;
  for (const child of node.children) {
    const hit = findById(child, id);
    if (hit) return hit;
  }
  return null;
}

function matches(node, sel) {
  if (sel.startsWith(".")) return node._class.has(sel.slice(1));
  const bare = /^\[([a-z-]+)\]$/.exec(sel);
  if (bare) return node.attrs[bare[1]] !== undefined || node.dataset[bare[1].slice(5)] !== undefined;
  const attr = /\[([a-z-]+)="([^"]*)"\]/.exec(sel);
  if (attr) {
    if (attr[1] === "rows") return node.attrs.rows === attr[2];
    return String(node.dataset[attr[1]] || node.attrs[attr[1]] || "") === attr[2];
  }
  if (sel.includes(".")) {
    const [tag, cls] = sel.split(".");
    return (!tag || node.tagName === tag.toUpperCase()) && node._class.has(cls);
  }
  return node.tagName === sel.toUpperCase();
}

function makeDoc(lang) {
  const root = new El("html");
  root.lang = lang || "zh-Hans";   // the host sets <html lang> on startup
  return {
    documentElement: root,
    hidden: false,
    createElement: (tag) => new El(tag),
    getElementById: (id) => findById(root, id),
    querySelector: (sel) => {
      const walk = (node) => {
        for (const child of node.children) {
          if (matches(child, sel)) return child;
          const hit = walk(child);
          if (hit) return hit;
        }
        return null;
      };
      return walk(root);
    },
    addEventListener: () => {},
  };
}

function composerDoc(lang) {
  const doc = makeDoc(lang);
  const feed = new El("div");
  feed.className = "cc-feed-body";
  const composer = new El("div");
  composer.className = "cc-composer";
  const surface = new El("div");
  surface.className = "cc-composer-surface";
  const area = new El("textarea");
  area.setAttribute("rows", "1");
  // The composer box starts below where the readout sits; without this the
  // stub's default rect(0..40) overlaps every widget.
  surface.rect = { top: 44, bottom: 200, left: 0, right: 600, width: 600, height: 156 };
  surface.append(area);
  composer.append(surface);
  feed.append(composer);
  doc.documentElement.append(feed);
  return { doc, feed, composer, surface, area };
}

const resizeObservers = [];
class FakeResizeObserver {
  constructor(cb) { this.cb = cb; this.targets = []; this.disconnected = false; resizeObservers.push(this); }
  observe(node) { this.targets.push(node); }
  disconnect() { this.disconnected = true; }
  fire() { this.cb([]); }
}

function fakeStorage() {
  const map = new Map();
  return {
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k),
    map,
  };
}

const observers = [];
class FakeObserver {
  constructor(cb) { this.cb = cb; observers.push(this); }
  observe() {}
  disconnect() { this.disconnected = true; }
}

function run(env) {
  const timers = [];
  const win = {
    location: { origin: "https://app.commandcode.ai", protocol: "https:" },
    electronAPI: env.electronAPI,
    document: env.doc,
    MutationObserver: FakeObserver,
    ResizeObserver: FakeResizeObserver,
    console,
    setInterval: (fn, ms) => { timers.push(["interval", fn, ms]); return timers.length; },
    clearInterval: () => {},
    setTimeout: (fn, ms) => { timers.push(["timeout", fn, ms]); return timers.length; },
    clearTimeout: () => {},
    getComputedStyle: (element) => ({
      display: "flex",
      flexDirection: "column",
      getPropertyValue: (name) => (env && env.tokens ? env.tokens[name] || "" : ""),
      _el: element,
    }),
    addEventListener: (type, fn) => { (win._listeners || (win._listeners = {}))[type] = fn; },
    removeEventListener: (type) => { if (win._listeners) delete win._listeners[type]; },
  };
  win.window = win;
  const context = {
    window: win,
    document: env.doc,
    location: win.location,
    console,
    setInterval: win.setInterval,
    clearInterval: win.clearInterval,
    setTimeout: win.setTimeout,
    clearTimeout: win.clearTimeout,
    getComputedStyle: win.getComputedStyle,
    localStorage: env.storage || fakeStorage(),
    MutationObserver: win.MutationObserver,
    ResizeObserver: win.ResizeObserver,
    requestAnimationFrame: (fn) => setTimeout(fn, 0),
    Date, Math, JSON, Number, String, Object, Array, Boolean, RegExp, Error, TypeError,
    Promise, Infinity, isFinite: (v) => Number.isFinite(v),
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context, { filename: "widget.js" });
  const fire = async (kind) => {
    const entry = timers.find((t) => t[0] === kind && !t[2]);
    if (!entry) return false;
    entry[2] = true;
    await entry[1]();
    return true;
  };
  const fireHeartbeat = () => {
    const entry = timers.find((t) => t[0] === "interval");
    return entry ? entry[1]() : false;
  };
  return { win, timers, fire, fireHeartbeat };
}

const results = [];
function check(name, condition, detail) {
  results.push({ name, ok: Boolean(condition), detail: detail === undefined ? "" : String(detail) });
}
const tick = () => new Promise((r) => setTimeout(r, 5));

(async () => {
  // ---- 1. mounts directly above the composer, inside its flex column ----
  const world = composerDoc();
  const first = run({ doc: world.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  VERSION = first.win.window.__goatWidgetVersion;
  check("contract: widget announces a protocol version", Boolean(VERSION), String(VERSION));
  const widget = findById(world.doc.documentElement, "goat-gauge-widget");
  check("mount: widget exists", widget);
  check("mount: sits before .cc-composer in the same column",
    widget && world.feed.children[0] === widget && world.feed.children[1] === world.composer,
    world.feed.children.map((c) => c.className || c.id).join(">"));
  check("mount: open shadow root with a row", Boolean(widget && widget.shadow && widget.shadow.getElementById("row")));

  // ---- 2. a second injection must not duplicate ----
  run({ doc: world.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  let copies = 0;
  const walk = (node) => { for (const c of node.children) { if (c.id === "goat-gauge-widget") copies += 1; walk(c); } };
  walk(world.doc.documentElement);
  check("mount: exactly one copy", copies === 1, `found ${copies}`);

  // ---- 3. the widget's own poll pulls host data ----
  check("poll: timer armed", await first.fire("timeout"));
  await tick();
  const html = widget.shadow.getElementById("row").innerHTML;
  check("render: 5h meter shows used/cap", /5h/.test(html) && /0\.22\/3/.test(html), html.slice(0, 120));
  check("render: weekly meter shows used/cap", /周/.test(html) && /5\.60\/6/.test(html));
  check("render: monthly without a known ceiling shows the remainder",
    /月<\/b><em>7\.20 cr 剩余/.test(html), html.slice(0, 260));
  check("render: request count shown", /<em>834</.test(html));
  check("render: token volume compacted", /351\.47M/.test(html), html.slice(0, 200));
  check("render: cost and average shown", /\$2\.78/.test(html) && /\$0\.0033/.test(html));
  check("render: bars are clamped percentages",
    [...html.matchAll(/width:([\d.]+)%/g)].length >= 2 &&
      [...html.matchAll(/width:([\d.]+)%/g)].every((m) => Number(m[1]) >= 0 && Number(m[1]) <= 100),
    [...html.matchAll(/width:([\d.]+)%/g)].map((m) => m[1]).join(","));
  check("render: window over 85% escalates tone", /chip meter warn/.test(html));
  check("render: reset countdown shown on the meter", /class="cd">\d+h/.test(html));
  check("status: dot text is hover-only", /class="chip dot"[^>]*title="/.test(html) && !/>同步[^<]*</.test(html));
  check("render: countdown is not link-styled", !/<u>/.test(html));

  // ---- 4. derived metrics are pushed in, not fetched ----
  const pushed = first.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { requests_per_min: 2.5, tokens_per_sec: 62.4, hit_rate_pct: 91.24 },
    projections: { five_hour: { burn_per_hour: 4.7612, verdict: "before_reset" } },
  });
  check("push: acknowledged", Boolean(pushed && pushed.ok === true), JSON.stringify(pushed));
  const afterPush = widget.shadow.getElementById("row").innerHTML;
  check("push: throughput rendered", /吞吐<\/b><em>2\.5 次\/分/.test(afterPush));
  check("push: generation speed rendered", /生成<\/b><em>62 tok\/s/.test(afterPush));
  check("push: consumption rate rendered", /消耗<\/b><em>4\.76 cr\/h/.test(afterPush));
  check("push: cache hit rendered", /命中<\/b><em>91\.2%/.test(afterPush));
  check("push: monthly ceiling turns it into a used/cap meter",
    /月<\/b><em>12\.8\/20<\/em>/.test(afterPush), afterPush.slice(0, 260));
  const wrong = first.win.window.__goatUpdate({ version: "9", rates: {} });
  check("push: wrong protocol version rejected", wrong.ok === false && wrong.reason === "version");

  // ---- 5. hostile strings never reach the DOM ----
  first.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now(),
    rates: { requests_per_min: "<img src=x onerror=alert(1)>", hit_rate_pct: "1;alert(2)" },
    projections: { five_hour: { burn_per_hour: "x", verdict: "before_reset" } },
  });
  const dirty = widget.shadow.getElementById("row").innerHTML;
  check("safety: non-numeric values are dropped", !/onerror|alert|1;/.test(dirty), dirty.slice(0, 100));

  // ---- 6. probe reflects reality ----
  const probe = first.win.window.__goatProbe();
  check("probe: mounted + host + pushed",
    probe.mounted === true && probe.host === true && probe.pushed === true, JSON.stringify(probe));

  // ---- 7. narrow layout blocks instead of covering the host ----
  const tight = composerDoc();
  const narrow = run({ doc: tight.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await narrow.fire("timeout");
  await tick();
  const tightWidget = findById(tight.doc.documentElement, "goat-gauge-widget");
  const tightRow = tightWidget.shadow.getElementById("row");
  tightRow.clientWidth = 120;
  tightWidget.rect = { top: 0, bottom: 30, left: 0, right: 120, width: 120, height: 30 };
  tight.surface.rect = { top: 20, bottom: 60, left: 0, right: 120, width: 120, height: 40 };
  narrow.win.window.__goatUpdate({ version: VERSION, server_time: Date.now(), rates: {}, projections: {} });
  check("fit: blocked instead of overlapping", tightWidget.dataset.layout === "blocked", tightWidget.dataset.layout);

  // ---- 8. the four width tiers, driven only by clientWidth ----
  const midWorld = composerDoc();
  const mid = run({ doc: midWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await mid.fire("timeout");
  await tick();
  mid.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { requests_per_min: 1, tokens_per_sec: 40, hit_rate_pct: 90 },
    projections: { five_hour: { burn_per_hour: 2, verdict: "after_reset" } },
  });
  await tick();
  const midWidget = findById(midWorld.doc.documentElement, "goat-gauge-widget");
  const midRow = midWidget.shadow.getElementById("row");
  const metricCount = () => midRow.children.filter((c) => c.attrs["data-metric"]).length;
  const decorations = () => midRow.querySelectorAll(".cd").length + midRow.querySelectorAll(".bar").length;

  // Width must never change which metrics are on the board.
  const PUSH = () => mid.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now() + Math.random(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { requests_per_min: 1, tokens_per_sec: 40, hit_rate_pct: 90 },
    projections: { five_hour: { burn_per_hour: 2, verdict: "after_reset" } },
  });

  const baseline = metricCount();
  check("tier: all eleven metrics render when there is room", baseline === 11, `got ${baseline}`);

  midRow.clientWidth = 400;
  PUSH();
  await tick();
  check("tier: 400px wraps onto a second line", midWidget.dataset.layout === "wrapped",
    midWidget.dataset.layout);
  check("tier: wrapping drops no metric and no decoration",
    metricCount() === baseline && decorations() === 6,
    `metrics=${metricCount()} deco=${decorations()}`);

  midRow.clientWidth = 180;
  PUSH();
  await tick();
  check("tier: 180px exceeds the height budget, so decoration goes",
    decorations() === 0, `deco=${decorations()}`);
  check("tier: 180px gives up the height budget rather than a metric",
    midWidget.dataset.layout === "tall" && metricCount() === baseline,
    `layout=${midWidget.dataset.layout} metrics=${metricCount()}`);
  const tallHtml = midRow.innerHTML;
  check("tier: every meter keeps its numbers when tall",
    /5h<\/b><em>0\.22\/3/.test(tallHtml) && /月<\/b><em>12\.8\/20/.test(tallHtml),
    tallHtml.slice(0, 140));

  midRow.clientWidth = 800;
  const resizer = resizeObservers.find((o) => o.targets.includes(midWidget));
  check("refit: widget registered a ResizeObserver", Boolean(resizer));
  resizer.fire();
  await tick();
  await tick();
  check("refit: widening returns to one line with every metric",
    midWidget.dataset.layout === "full" && metricCount() === baseline && decorations() === 6,
    `layout=${midWidget.dataset.layout} metrics=${metricCount()} deco=${decorations()}`);

  // ---- 8c. the editor and the board must agree at a narrow width ----
  const agreeWorld = composerDoc("zh-Hans");
  const agree = run({ doc: agreeWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await agree.fire("timeout");
  await tick();
  const agreeWidget = findById(agreeWorld.doc.documentElement, "goat-gauge-widget");
  const agreeRow = agreeWidget.shadowRoot.getElementById("row");
  const AGREE = (extra) => agree.win.window.__goatUpdate(Object.assign({
    version: VERSION,
    server_time: Date.now() + Math.random(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { tokens_per_sec: 61, requests_per_min: 2.5, hit_rate_pct: 91.2 },
    projections: { five_hour: { burn_per_hour: 4.76, verdict: "before_reset" } },
  }, extra || {}));
  AGREE();
  await tick();
  agreeRow.clientWidth = 180;
  AGREE();
  await tick();
  const boardMetrics = agreeRow.children.map((c) => c.attrs["data-metric"]).filter(Boolean).join(",");
  agree.win.window.__goatLayout("edit");
  await tick();
  const editorMetrics = agreeRow.children.map((c) => c.attrs["data-metric"]).filter(Boolean).join(",");
  agree.win.window.__goatLayout("cancel");
  await tick();
  check("consistency: editor shows exactly what the board shows at 180px",
    boardMetrics === editorMetrics && boardMetrics.split(",").length === 11,
    `board=${boardMetrics} editor=${editorMetrics}`);

  // ---- 9. dispose leaves nothing behind ----
  const disposed = first.win.window.__goatDispose();
  check("dispose: ok", disposed.ok === true);
  check("dispose: node removed", findById(world.doc.documentElement, "goat-gauge-widget") === null);
  check("dispose: observer disconnected", observers.some((o) => o.disconnected === true));
  check("dispose: resize observer disconnected", resizeObservers.some((o) => o.disconnected === true));

  // ---- 10. missing host bridge is stated, not silently blank ----
  const silent = composerDoc();
  const nb = run({ doc: silent.doc, electronAPI: undefined });
  await nb.fire("timeout");
  await tick();
  check("degrade: no bridge reported", nb.win.window.__goatProbe().hostError === "no-bridge",
    JSON.stringify(nb.win.window.__goatProbe()));
  const nbWidget = findById(silent.doc.documentElement, "goat-gauge-widget");
  check("degrade: empty hint visible", /等待宿主额度数据/.test(nbWidget.shadow.getElementById("row").innerHTML));

  // ---- 11. composer arrives late, which is the normal injection case ----
  const bare = makeDoc();
  const late = run({ doc: bare, electronAPI: { accountOverview: async () => OVERVIEW } });
  check("late host: nothing mounts before the composer exists",
    findById(bare.documentElement, "goat-gauge-widget") === null);
  check("late host: retry machinery is installed anyway",
    late.timers.filter((t) => t[0] === "interval").length === 1);
  check("late host: no polling before mount",
    late.timers.filter((t) => t[0] === "timeout").length === 0);

  const feed = new El("div");
  feed.className = "cc-feed-body";
  const composer = new El("div");
  composer.className = "cc-composer";
  const surface = new El("div");
  surface.className = "cc-composer-surface";
  surface.rect = { top: 44, bottom: 200, left: 0, right: 600, width: 600, height: 156 };
  composer.append(surface);
  feed.append(composer);
  bare.documentElement.append(feed);
  late.fireHeartbeat();
  await tick();
  const lateWidget = findById(bare.documentElement, "goat-gauge-widget");
  check("late host: heartbeat mounts once the composer appears", Boolean(lateWidget));
  check("late host: polling starts only after mounting",
    late.timers.filter((t) => t[0] === "timeout").length === 1);
  await late.fire("timeout");
  await tick();
  check("late host: data renders after the late mount",
    /5h/.test(lateWidget.shadow.getElementById("row").innerHTML));
  bare.documentElement.lang = "en";
  late.fireHeartbeat();
  await tick();
  check("late host: language switch still honoured after a late mount",
    /<b>Week<\/b>/.test(lateWidget.shadow.getElementById("row").innerHTML));

  // ---- 12. English host locale ----
  const enWorld = composerDoc("en");
  const enRun = run({ doc: enWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await enRun.fire("timeout");
  await tick();
  const enWidget = findById(enWorld.doc.documentElement, "goat-gauge-widget");
  const enHtml = enWidget.shadow.getElementById("row").innerHTML;
  check("i18n: english labels used", /<b>Week<\/b>/.test(enHtml) && /<b>Month<\/b>/.test(enHtml), enHtml.slice(0, 120));
  check("i18n: no chinese glyphs in english mode", !/[一-鿿]/.test(enHtml));
  check("i18n: english aria label", enWidget.getAttribute("aria-label") === "Command Code quota and usage",
    enWidget.getAttribute("aria-label"));

  // ---- 13. any non-Chinese locale falls back to english ----
  const frWorld = composerDoc("fr-FR");
  const frRun = run({ doc: frWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await frRun.fire("timeout");
  await tick();
  check("i18n: fr falls back to english",
    /<b>Week<\/b>/.test(findById(frWorld.doc.documentElement, "goat-gauge-widget").shadow.getElementById("row").innerHTML));

  // ---- 14. switching the host language re-renders without re-injection ----
  const live = composerDoc("zh-Hans");
  const liveRun = run({ doc: live.doc, electronAPI: { accountOverview: async () => OVERVIEW } });
  await liveRun.fire("timeout");
  await tick();
  const liveWidget = findById(live.doc.documentElement, "goat-gauge-widget");
  check("i18n: starts chinese", /<b>周<\/b>/.test(liveWidget.shadow.getElementById("row").innerHTML));
  live.doc.documentElement.lang = "en";
  liveRun.fireHeartbeat();
  await tick();
  check("i18n: heartbeat picks up the language switch",
    /<b>Week<\/b>/.test(liveWidget.shadow.getElementById("row").innerHTML) &&
      !/<b>周<\/b>/.test(liveWidget.shadow.getElementById("row").innerHTML),
    liveWidget.shadow.getElementById("row").innerHTML.slice(0, 90));

  // ---- 15. layout editor ----
  const edWorld = composerDoc("zh-Hans");
  const edStore = fakeStorage();
  const ed = run({ doc: edWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW }, storage: edStore });
  await ed.fire("timeout");
  await tick();
  const edW = findById(edWorld.doc.documentElement, "goat-gauge-widget");
  // Without a push the derived metrics have no data at all, which would make
  // the hide/add assertions pass vacuously.
  ed.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { tokens_per_sec: 61, requests_per_min: 2.5, hit_rate_pct: 91.2 },
    projections: { five_hour: { burn_per_hour: 4.76, verdict: "before_reset" } },
  });
  await tick();
  const edRow = () => edW.shadow.getElementById("row").innerHTML;
  const labels0 = edW.shadow.getElementById("row").children.map((c) => c.label).join(",");
  check("fixture: all eleven metrics are live before editing",
    labels0.split(",").filter(Boolean).length === 11, labels0);
  check("edit: closed by default, gear offered",
    edW.dataset.edit === "off" && /data-act="edit"/.test(edRow()) && !/data-act="save"/.test(edRow()));
  ed.win.window.__goatLayout("edit");
  await tick();
  const editing = edRow();
  check("edit: the three buttons appear only in edit mode",
    edW.dataset.edit === "on" && /data-act="add"/.test(editing) &&
      /data-act="cancel"/.test(editing) && /data-act="save"/.test(editing));
  check("edit: every chip gains a hide control", /class="hide"/.test(editing));
  check("edit: never blocked or trimmed while editing", edW.dataset.layout === "editing");
  ed.win.window.__goatLayout("picker");
  await tick();
  check("edit: add opens a picker listing nothing hidden yet",
    /没有已隐藏的信息/.test(edRow()) || /Nothing hidden/.test(edRow()), edRow().slice(0, 80));

  const labels = () => edW.shadow.getElementById("row").children.map((c) => c.label).join(",");
  ed.win.window.__goatLayout("picker");   // close it again
  ed.win.window.__goatLayout("hide", "cache");
  await tick();
  check("hide: metric leaves the board", !labels().includes("命中"), labels());
  check("hide: metric is tracked as hidden",
    ed.win.window.__goatLayout().hidden.indexOf("cache") >= 0,
    JSON.stringify(ed.win.window.__goatLayout().hidden));
  ed.win.window.__goatLayout("add", "cache");
  await tick();
  check("add: metric comes back from the picker", labels().includes("命中"), labels());

  ed.win.window.__goatLayout("move", { from: "gen", to: "five", before: true });
  await tick();
  const moved = ed.win.window.__goatLayout().order;
  check("move: drag reorder changes the sequence",
    moved.indexOf("gen") < moved.indexOf("week"), moved.join(","));

  ed.win.window.__goatLayout("hide", "tokens");
  ed.win.window.__goatLayout("cancel");
  await tick();
  const afterCancel = ed.win.window.__goatLayout();
  check("cancel: discards the draft and exits",
    afterCancel.hidden.indexOf("tokens") === -1 && afterCancel.editing === false,
    JSON.stringify(afterCancel.hidden));
  check("cancel: nothing was written to storage", edStore.map.size === 0);

  ed.win.window.__goatLayout("edit");
  ed.win.window.__goatLayout("hide", "tokens");
  ed.win.window.__goatLayout("save");
  await tick();
  const saved = JSON.parse([...edStore.map.values()][0]);
  check("save: persists order and hidden set", saved.hidden.indexOf("tokens") >= 0, JSON.stringify(saved));
  check("save: exits edit mode and re-renders without the hidden metric",
    edW.dataset.edit === "off" && !labels().includes("TOKEN"), labels());

  const reloadWorld = composerDoc("zh-Hans");
  const reload = run({ doc: reloadWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW }, storage: edStore });
  await reload.fire("timeout");
  await tick();
  check("reload: stored layout is restored on next injection",
    reload.win.window.__goatLayout().hidden.indexOf("tokens") >= 0,
    JSON.stringify(reload.win.window.__goatLayout().hidden));

  // ---- 16. host theme inheritance ----
  const lightWorld = composerDoc("zh-Hans");
  lightWorld.doc.documentElement.attrs["data-theme"] = "light";
  const light = run({
    doc: lightWorld.doc,
    electronAPI: { accountOverview: async () => OVERVIEW },
    tokens: { "--primary": "#2f6fed", "--background": "#f7f8fa" },
  });
  await light.fire("timeout");
  await tick();
  const lightWidget = findById(lightWorld.doc.documentElement, "goat-gauge-widget");
  check("theme: accent inherits the host --primary",
    lightWidget.style["--goat-accent"] === "#2f6fed", String(lightWidget.style["--goat-accent"]));
  check("theme: light host reports light", lightWidget.dataset.dark === "0", lightWidget.dataset.dark);

  const darkWorld = composerDoc("zh-Hans");
  darkWorld.doc.documentElement.attrs["data-theme"] = "purple-dark";
  const dark = run({
    doc: darkWorld.doc,
    electronAPI: { accountOverview: async () => OVERVIEW },
    tokens: { "--primary": "#8b5cf6" },
  });
  await dark.fire("timeout");
  await tick();
  const darkWidget = findById(darkWorld.doc.documentElement, "goat-gauge-widget");
  check("theme: dark host reports dark", darkWidget.dataset.dark === "1", darkWidget.dataset.dark);
  darkWorld.doc.documentElement.attrs["data-theme"] = "light";
  dark.fireHeartbeat();
  await tick();
  check("theme: host theme switch is picked up without re-injection",
    findById(darkWorld.doc.documentElement, "goat-gauge-widget").dataset.dark === "0");

  // ---- 17. a metric with no data must not be shoved to the tail ----
  const gapWorld = composerDoc("zh-Hans");
  const gapStore = fakeStorage();
  const gap = run({ doc: gapWorld.doc, electronAPI: { accountOverview: async () => OVERVIEW }, storage: gapStore });
  await gap.fire("timeout");
  await tick();
  gap.win.window.__goatUpdate({
    version: VERSION,
    server_time: Date.now(),
    monthly: { cap: 20, remaining: 7.2, reset_at: Date.now() + 18 * 86400e3 },
    rates: { hit_rate_pct: 91.2 },          // no tokens_per_sec, no requests_per_min
    projections: { five_hour: { burn_per_hour: 4.76, verdict: "before_reset" } },
  });
  await tick();
  const gapWidget = findById(gapWorld.doc.documentElement, "goat-gauge-widget");
  // The stub only understands simple selectors, so filter in JS.
  const rendered = [...gapWidget.shadowRoot.getElementById("row").querySelectorAll(".chip")]
    .map((c) => c.attrs["data-metric"]).filter(Boolean);
  check("commit: only rendered metrics are on the board",
    rendered.indexOf("gen") === -1 && rendered.includes("cache"), rendered.join(","));
  gap.win.window.__goatLayout("edit");
  // Reorder the rendered chips and commit exactly what is on screen; the two
  // metrics with no data right now must keep their original slots.
  gap.win.window.__goatLayout("commit", ["cache", "five", "week", "month", "burn", "requests", "tokens", "cost", "avg"]);
  await tick();
  const afterCommit = gap.win.window.__goatLayout().order;
  check("commit: absent metrics keep their configured position",
    afterCommit.indexOf("gen") < afterCommit.indexOf("requests") &&
      afterCommit.indexOf("rate") < afterCommit.indexOf("requests"),
    afterCommit.join(","));
  check("commit: rendered metrics took the dragged order",
    afterCommit.indexOf("cache") < afterCommit.indexOf("five"), afterCommit.join(","));

  const failed = results.filter((r) => !r.ok);
  for (const r of results) {
    console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}${r.detail ? `  -> ${r.detail}` : ""}`);
  }
  console.log(failed.length ? `${failed.length} FAILED` : "ALL PASSED");
  process.exitCode = failed.length ? 1 : 0;
})();
"""


def node_available() -> bool:
    return shutil.which("node") is not None


@unittest.skipUnless(node_available(), "node is not installed")
class WidgetBehaviourTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.driver = Path(self._tmp.name) / "driver.js"
        self.driver.write_text(NODE_DRIVER, encoding="utf-8")

    def run_driver(self) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            ["node", str(self.driver), str(WIDGET)],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=120,
        )

    def test_widget_behaviour(self) -> None:
        result = self.run_driver()
        output = (result.stdout or "") + (result.stderr or "")
        self.assertEqual(result.returncode, 0, f"node driver failed:\n{output}")
        self.assertIn("ALL PASSED", output)

    def test_widget_declares_its_contract(self) -> None:
        source = WIDGET.read_text(encoding="utf-8")
        for name in ("__goatUpdate", "__goatProbe", "__goatDispose", "__goatWidgetVersion"):
            self.assertIn(name, source, name)

    def test_widget_anchors_on_stable_semantic_classes(self) -> None:
        # The app ships an en/zh catalog, so aria-label and placeholder text
        # differ by locale and must never be used as an anchor.
        source = WIDGET.read_text(encoding="utf-8")
        self.assertIn("cc-composer", source)
        self.assertNotIn("Message Command Code", source)


if __name__ == "__main__":
    unittest.main()
