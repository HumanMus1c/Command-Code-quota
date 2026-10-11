/*
 * GOAT Gauge widget — a compact usage readout injected above the Command Code
 * composer. Data comes from the host's own preload bridge (window.electronAPI)
 * and from derived metrics pushed in by the local injector (__goatUpdate).
 * No charts: parameters, numbers, and plain progress bars only.
 */
(() => {
  "use strict";

  const VERSION = "2";
  const ID = "goat-gauge-widget";
  const POLL_VISIBLE_MS = 2500;
  const POLL_HIDDEN_MS = 15000;
  const STORAGE_KEY = "goat-gauge-layout-v2";

  if (window.__goatWidgetVersion === VERSION && document.getElementById(ID)) {
    return;
  }

  const LOCALES = {
    zh: {
      week: "周",
      month: "月",
      requests: "请求",
      tokens: "TOKEN",
      cost: "费用",
      average: "均价",
      generation: "生成",
      consumption: "消耗",
      cacheHit: "命中",
      throughput: "吞吐",
      perMinute: "次/分",
      remaining: "cr 剩余",
      pendingReset: "待重置",
      waiting: "等待宿主额度数据…",
      aria: "Command Code 额度与用量",
      statusError: "宿主接口不可用",
      statusEmpty: "未连接",
      statusStale: "数据 {n}s 前",
      statusLive: "同步 {n}s",
      edit: "编辑布局",
      cancel: "取消",
      save: "保存",
      add: "添加",
      hide: "隐藏此信息",
      noHidden: "没有已隐藏的信息",
      dragHint: "拖动可调整顺序",
    },
    // Every non-Chinese host locale lands here; the host itself only ships
    // en/zh and falls back to en.
    en: {
      week: "Week",
      month: "Month",
      requests: "Reqs",
      tokens: "Tok",
      cost: "Cost",
      average: "Avg",
      generation: "Gen",
      consumption: "Burn",
      cacheHit: "Cache",
      throughput: "Rate",
      perMinute: "/min",
      remaining: "cr left",
      pendingReset: "reset pending",
      waiting: "Waiting for host quota data…",
      aria: "Command Code quota and usage",
      statusError: "No host bridge",
      statusEmpty: "Disconnected",
      statusStale: "stale {n}s",
      statusLive: "synced {n}s",
      edit: "Edit layout",
      cancel: "Cancel",
      save: "Save",
      add: "Add",
      hide: "Hide this metric",
      noHidden: "Nothing hidden",
      dragHint: "Drag to reorder",
    },
  };

  /* The host writes <html lang> on startup and on locale change; zh-* gets the
   * Chinese strings and everything else falls back to English. */
  function locale() {
    const root = document.documentElement;
    const tag = String((root && root.lang) || "").toLowerCase() ||
      String(navigatorLanguage()).toLowerCase();
    return tag.indexOf("zh") === 0 ? LOCALES.zh : LOCALES.en;
  }

  function navigatorLanguage() {
    if (typeof navigator === "undefined" || !navigator) {
      return "";
    }
    return navigator.language || (navigator.languages && navigator.languages[0]) || "";
  }

  function phrase(template, value) {
    return template.replace("{n}", String(value));
  }

  /* Host CSS custom properties, most specific candidate first. Reading them
   * keeps the readout on the same palette as the app instead of a lookalike. */
  const THEME_TOKENS = [
    ["--goat-accent", ["--primary", "--accent", "--brand"], "#7c5cf6"],
    ["--goat-good", ["--success", "--green"], "#22c55e"],
    ["--goat-warn", ["--warning", "--amber"], "#d97706"],
    ["--goat-danger", ["--destructive", "--danger", "--red"], "#ef4444"],
    ["--goat-line", ["--border", "--input"], ""],
    ["--goat-muted", ["--ink-subtle", "--muted-foreground", "--muted"], ""],
    ["--goat-ink", ["--foreground", "--ink", "--card-foreground"], ""],
    ["--goat-surface", ["--card", "--popover", "--background", "--secondary"], "#ffffff", "#221d2e"],
  ];

  function themeSignature() {
    const root = document.documentElement;
    if (!root) {
      return "";
    }
    return `${root.getAttribute("data-theme") || ""}|${root.className || ""}|${root.lang || ""}`;
  }

  function isDarkTheme() {
    const root = document.documentElement;
    if (!root) {
      return false;
    }
    const named = String(root.getAttribute("data-theme") || "").toLowerCase();
    return named.indexOf("dark") >= 0 || root.classList.contains("dark");
  }

  const state = {
    langTag: null,
    themeTag: null,
    host: null,
    hostAt: 0,
    hostError: "",
    pushed: null,
    pushedAt: 0,
    serverOffset: 0,
    timer: null,
    polling: false,
    heartbeat: null,
    observer: null,
    resizer: null,
    mountedIn: null,
  };

  const layout = {
    order: [],
    hidden: [],
    editing: false,
    picker: false,
    draft: null,
  };

  const METRICS = [
    { id: "five", label: () => "5h", meter: true },
    { id: "week", label: () => locale().week, meter: true },
    { id: "month", label: () => locale().month, meter: true },
    { id: "gen", label: () => locale().generation },
    { id: "burn", label: () => locale().consumption },
    { id: "cache", label: () => locale().cacheHit },
    { id: "rate", label: () => locale().throughput },
    { id: "requests", label: () => locale().requests },
    { id: "tokens", label: () => locale().tokens },
    { id: "cost", label: () => locale().cost },
    { id: "avg", label: () => locale().average },
  ];
  const DEFAULT_ORDER = METRICS.map((metric) => metric.id);

  function loadLayout() {
    layout.order = DEFAULT_ORDER.slice();
    layout.hidden = [];
    let raw = "";
    try {
      raw = (typeof localStorage !== "undefined" && localStorage.getItem(STORAGE_KEY)) || "";
    } catch {
      return;
    }
    if (!raw) {
      return;
    }
    try {
      const saved = JSON.parse(raw);
      const known = (list) =>
        Array.isArray(list) ? list.filter((id) => DEFAULT_ORDER.indexOf(id) >= 0) : [];
      const order = known(saved.order);
      const hidden = known(saved.hidden);
      const rest = DEFAULT_ORDER.filter((id) => order.indexOf(id) === -1);
      layout.order = order.concat(rest);
      layout.hidden = hidden;
    } catch {
      // A corrupt entry must not leave the readout blank; defaults win.
    }
  }

  function persistLayout() {
    try {
      localStorage.setItem(
        STORAGE_KEY,
        JSON.stringify({ order: layout.order, hidden: layout.hidden })
      );
    } catch {
      // Storage can be unavailable; the session layout still works.
    }
  }

  function currentLayout() {
    return {
      order: layout.order.slice(),
      hidden: layout.hidden.slice(),
      editing: layout.editing,
    };
  }

  function enterEdit() {
    layout.draft = { order: layout.order.slice(), hidden: layout.hidden.slice() };
    layout.editing = true;
    layout.picker = false;
    render();
  }

  function cancelEdit() {
    if (layout.draft) {
      layout.order = layout.draft.order.slice();
      layout.hidden = layout.draft.hidden.slice();
    }
    closeEdit();
  }

  function saveEdit() {
    persistLayout();
    closeEdit();
  }

  function closeEdit() {
    layout.editing = false;
    layout.picker = false;
    layout.draft = null;
    render();
  }

  function hideMetric(id) {
    if (layout.hidden.indexOf(id) === -1 && DEFAULT_ORDER.indexOf(id) >= 0) {
      layout.hidden.push(id);
      render();
    }
  }

  function addMetric(id) {
    const index = layout.hidden.indexOf(id);
    if (index >= 0) {
      layout.hidden.splice(index, 1);
    }
    if (layout.order.indexOf(id) === -1) {
      layout.order.push(id);
    }
    layout.picker = false;
    render();
  }

  /* Reorder by identifier so the rule is testable without synthetic pointer
   * events; the drag handler only ever computes a target index. */
  function moveMetric(fromId, toId, before) {
    if (fromId === toId) {
      return;
    }
    const order = layout.order.slice();
    const from = order.indexOf(fromId);
    if (from < 0) {
      return;
    }
    order.splice(from, 1);
    let target = order.indexOf(toId);
    if (target < 0) {
      order.push(fromId);
    } else {
      order.splice(before ? target : target + 1, 0, fromId);
    }
    layout.order = order;
    render();
  }

  function commitDomOrder(ids) {
    const visible = ids.filter((id) => layout.order.indexOf(id) >= 0);
    const result = layout.order.slice();
    // Rewrite only the slots the rendered metrics occupied. Metrics that happen
    // to have no data right now keep their position instead of being pushed to
    // the tail by an absence that is not the user's doing.
    const slots = [];
    for (let index = 0; index < result.length; index += 1) {
      if (visible.indexOf(result[index]) >= 0) {
        slots.push(index);
      }
    }
    slots.forEach((slot, position) => {
      result[slot] = visible[position];
    });
    layout.order = result;
  }

  const bar = (share, tone) =>
    `<span class="bar${tone ? " " + tone : ""}"><i style="width:${(clamp(share, 0, 1) * 100).toFixed(1)}%"></i></span>`;

  function chrome(id) {
    if (!layout.editing) {
      return "";
    }
    const L = locale();
    return `<button class="hide" type="button" data-hide="${id}" title="${L.hide}" aria-label="${L.hide}">×</button>`;
  }

  const chip = (id, label, value, extraClass) =>
    `<span class="chip${extraClass ? " " + extraClass : ""}" data-metric="${id}">` +
    `<b>${label}</b><em>${value}</em>${chrome(id)}</span>`;

  function meterChip(id, label, used, cap, resetAt, extraClass) {
    if (!isFinite(cap) || cap <= 0) {
      return "";
    }
    const share = used / cap;
    const tone = share >= 0.95 ? "danger" : share >= 0.85 ? "warn" : "";
    return (
      `<span class="chip meter${tone ? " " + tone : ""}" data-metric="${id}">` +
      `<b>${label}</b><em>${trim(used)}/${trim(cap)}</em>` +
      bar(share, tone) +
      `<span class="cd">${countdown(resetAt)}</span>${chrome(id)}</span>`
    );
  }

  function clamp(value, min, max) {
    return Math.min(max, Math.max(min, value));
  }

  function isFinite(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function trim(value) {
    if (!isFinite(value)) {
      return "--";
    }
    if (Number.isInteger(value)) return String(value);
    const abs = Math.abs(value);
    if (abs >= 100) return value.toFixed(0);
    if (abs >= 10) return value.toFixed(1);
    return value.toFixed(2);
  }

  function compact(value) {
    if (!isFinite(value)) {
      return "--";
    }
    const abs = Math.abs(value);
    if (abs >= 1e9) return `${(value / 1e9).toFixed(2)}B`;
    if (abs >= 1e6) return `${(value / 1e6).toFixed(2)}M`;
    if (abs >= 1e3) return `${(value / 1e3).toFixed(1)}K`;
    return String(Math.round(value));
  }

  function money(value) {
    if (!isFinite(value)) {
      return "--";
    }
    return Math.abs(value) >= 1 ? `$${value.toFixed(2)}` : `$${value.toFixed(4)}`;
  }

  function countdown(resetAt) {
    if (!isFinite(resetAt) || resetAt <= now()) {
      return resetAt ? locale().pendingReset : "";
    }
    const seconds = Math.floor((resetAt - now()) / 1000);
    if (seconds < 60) return `${seconds}s`;
    const minutes = Math.floor(seconds / 60);
    if (minutes < 60) return `${minutes}m`;
    const hours = Math.floor(minutes / 60);
    if (hours < 48) return `${hours}h${minutes % 60 ? " " + (minutes % 60) + "m" : ""}`;
    return `${Math.floor(hours / 24)}d${hours % 24}h`;
  }

  function now() {
    return Date.now() + state.serverOffset;
  }

  function hostPayload(overview) {
    const limits = (overview && overview.windowLimits) || {};
    const credits = (overview && overview.credits) || {};
    const usage = (overview && overview.usage) || {};
    return {
      five: limits.fiveHour || null,
      week: limits.weekly || null,
      limited: Boolean(limits.limited),
      monthly: isFinite(credits.monthlyCredits)
        ? credits.monthlyCredits +
          (credits.purchasedCredits || 0) +
          (credits.freeCredits || 0)
        : null,
      requests: usage.totalCount,
      tokens: usage.totalTokens,
      cost: usage.totalCost,
      average: usage.averageCost,
      success: usage.successRate,
    };
  }

  function pushedMonthly() {
    const monthly = (state.pushed && state.pushed.monthly) || {};
    return { cap: Number(monthly.cap), remaining: Number(monthly.remaining), reset_at: Number(monthly.reset_at) || 0 };
  }

  function metricHtml(id, host, pushed, L) {
    const rates = pushed.rates || {};
    const burn = (pushed.projections || {}).five_hour || {};
    switch (id) {
      case "five":
        return host && host.five ? meterChip("five", "5h", host.five.used, host.five.cap, host.five.resetAt) : "";
      case "week":
        return host && host.week ? meterChip("week", L.week, host.week.used, host.week.cap, host.week.resetAt) : "";
      case "month": {
        if (!host || !isFinite(host.monthly)) {
          return "";
        }
        const monthly = pushedMonthly();
        if (isFinite(monthly.cap) && monthly.cap > 0) {
          return meterChip("month", L.month, Math.max(0, monthly.cap - host.monthly), monthly.cap, monthly.reset_at);
        }
        // No ceiling from the host overview, so show the remainder without a
        // bar rather than inventing a denominator to fill one.
        return (
          `<span class="chip meter" data-metric="month"><b>${L.month}</b>` +
          `<em>${trim(host.monthly)} ${L.remaining}</em>` +
          `<span class="cd">${countdown(monthly.reset_at)}</span>${chrome("month")}</span>`
        );
      }
      case "gen":
        return isFinite(rates.tokens_per_sec) ? chip("gen", L.generation, `${rates.tokens_per_sec.toFixed(0)} tok/s`) : "";
      case "burn":
        return isFinite(burn.burn_per_hour)
          ? chip("burn", L.consumption, `${burn.burn_per_hour.toFixed(2)} cr/h`,
              burn.verdict === "before_reset" ? "bad" : "")
          : "";
      case "cache":
        return isFinite(rates.hit_rate_pct) ? chip("cache", L.cacheHit, `${rates.hit_rate_pct.toFixed(1)}%`) : "";
      case "rate":
        return isFinite(rates.requests_per_min)
          ? chip("rate", L.throughput, `${rates.requests_per_min.toFixed(1)} ${L.perMinute}`)
          : "";
      case "requests":
        return host && isFinite(host.requests) ? chip("requests", L.requests, compact(host.requests)) : "";
      case "tokens":
        return host && isFinite(host.tokens) ? chip("tokens", L.tokens, compact(host.tokens)) : "";
      case "cost":
        return host && isFinite(host.cost) ? chip("cost", L.cost, money(host.cost)) : "";
      case "avg":
        return host && isFinite(host.average) ? chip("avg", L.average, money(host.average)) : "";
      default:
        return "";
    }
  }

  function toolsHtml(fresh) {
    const L = locale();
    if (!layout.editing) {
      return (
        `<span class="chip dot" data-state="${fresh.state}" title="${fresh.text}"` +
        ` aria-label="${fresh.text}"><i></i></span>` +
        `<button class="tool" type="button" data-act="edit" title="${L.edit}" aria-label="${L.edit}">⚙</button>`
      );
    }
    const hidden = layout.hidden.filter((id) => DEFAULT_ORDER.indexOf(id) >= 0);
    const picker = layout.picker
      ? `<span class="picker">${
          hidden.length
            ? hidden
                .map((id) => {
                  const metric = METRICS.filter((item) => item.id === id)[0];
                  return `<button type="button" class="pick" data-pick="${id}">${metric.label()}</button>`;
                })
                .join("")
            : `<span class="pick empty">${L.noHidden}</span>`
        }</span>`
      : "";
    return (
      `<span class="chip dot" data-state="${fresh.state}" title="${fresh.text}"` +
      ` aria-label="${fresh.text}"><i></i></span>` +
      `<span class="tools"><span class="picker-wrap">${picker}` +
      `<button class="tool" type="button" data-act="add" title="${L.add}" aria-label="${L.add}"` +
      ` aria-haspopup="true" aria-expanded="${layout.picker ? "true" : "false"}">${L.add} ▾</button></span>` +
      `<button class="tool" type="button" data-act="cancel">${L.cancel}</button>` +
      `<button class="tool primary" type="button" data-act="save">${L.save}</button></span>`
    );
  }

  function render() {
    const node = document.getElementById(ID);
    if (!node) {
      return;
    }
    const host = state.host ? hostPayload(state.host) : null;
    const pushed = state.pushed || {};
    const L = locale();
    const parts = [];

    for (const id of layout.order) {
      if (layout.hidden.indexOf(id) >= 0) {
        continue;
      }
      const markup = metricHtml(id, host, pushed, L);
      if (markup) {
        parts.push(markup);
      }
    }

    const fresh = freshness();
    parts.push(toolsHtml(fresh));

    if (!host && !state.pushed) {
      parts.unshift(`<span class="chip muted" data-metric="waiting"><em>${L.waiting}</em></span>`);
    }

    const root = node.shadowRoot;
    const row = root.getElementById("row");
    row.innerHTML = parts.join("");
    node.dataset.state = fresh.state;
    node.dataset.edit = layout.editing ? "on" : "off";
    node.setAttribute("aria-label", L.aria);
    state.langTag = String((document.documentElement || {}).lang || "");
    state.themeTag = themeSignature();
    applyTheme(node);
    if (layout.editing) {
      node.dataset.layout = "editing";
    } else {
      fit(root, node);
    }
  }

  function applyTheme(node) {
    let computed = null;
    try {
      computed = getComputedStyle(document.documentElement);
    } catch {
      computed = null;
    }
    const dark = isDarkTheme();
    for (const entry of THEME_TOKENS) {
      let value = "";
      for (const name of entry[1]) {
        const candidate = computed && computed.getPropertyValue
          ? String(computed.getPropertyValue(name) || "").trim()
          : "";
        if (candidate) {
          value = candidate;
          break;
        }
      }
      const fallback = entry[3] && dark ? entry[3] : entry[2];
      node.style.setProperty(entry[0], value || fallback);
    }
    node.dataset.dark = dark ? "1" : "0";
  }

  function freshness() {
    const L = locale();
    const hostAge = state.hostAt ? now() - state.hostAt : null;
    const pushedAge = state.pushedAt ? now() - state.pushedAt : null;
    if (state.hostError && !state.hostAt) {
      return { state: "error", text: L.statusError };
    }
    if (hostAge === null && pushedAge === null) {
      return { state: "empty", text: L.statusEmpty };
    }
    const best = Math.min(
      hostAge === null ? Infinity : hostAge,
      pushedAge === null ? Infinity : pushedAge
    );
    if (best > 60000) {
      return { state: "stale", text: phrase(L.statusStale, Math.round(best / 1000)) };
    }
    return { state: "live", text: phrase(L.statusLive, Math.round(best / 1000)) };
  }

  /* Width pressure may only ever cost presentation: one line, then wrapped
   * lines within a height budget, then the countdowns and bars inside the
   * meters, then the height budget itself. Which metrics are on the board is
   * decided by the saved layout alone, never by the viewport, so the readout
   * and the editor always show the same set. */
  function fit(root, node) {
    const row = root.getElementById("row");
    // Force the no-wrap state before measuring: scrollWidth is only an honest
    // "does it fit on one line" reading while the row is not already wrapping.
    node.dataset.layout = "full";
    if (row.scrollWidth <= row.clientWidth + 1) {
      guardAgainstHost(node, row);
      return;
    }
    node.dataset.layout = "wrapped";
    while (row.scrollHeight > row.clientHeight + 1) {
      if (!shedOne(row)) {
        // Nothing left to shed but the budget; grow instead of dropping data.
        node.dataset.layout = "tall";
        break;
      }
    }
    guardAgainstHost(node, row);
  }

  function chips(row) {
    return [...row.querySelectorAll(".chip")];
  }

  /* Only presentation degrades: countdowns first, then bars, always keeping
   * every metric. Dropping whole chips here is what made the board show a
   * different set of metrics than the editor offers at the same width. */
  function shedOne(row) {
    for (const decoration of [".cd", ".bar"]) {
      const node = row.querySelector(decoration);
      if (node) {
        node.remove();
        return true;
      }
    }
    return false;
  }

  function isProtected(chipNode) {
    return chipNode.classList.contains("meter") || chipNode.classList.contains("dot");
  }

  /* The readout is a flex sibling of the composer; never let it paint over it. */
  function guardAgainstHost(node, row) {
    const host =
      document.querySelector(".cc-composer-surface") || document.querySelector(".cc-composer");
    if (!host) {
      return;
    }
    const mine = node.getBoundingClientRect();
    const theirs = host.getBoundingClientRect();
    if (mine.bottom > theirs.top + 1 || mine.width < 40 || chips(row).length === 0) {
      node.dataset.layout = "blocked";
    }
  }

  const STYLE = `
:host{display:block;font-family:var(--goat-font,inherit);font-size:11.5px;line-height:1.4;
  color:inherit;position:relative}
:host([data-layout="blocked"]){visibility:hidden;pointer-events:none}
#row{display:flex;align-items:center;gap:7px;flex-wrap:nowrap;overflow:hidden;
  margin:0 auto;max-width:100%;padding:3px 8px;border-radius:8px;
  background:color-mix(in srgb,currentColor 6%,transparent);
  border:1px solid color-mix(in srgb,currentColor 14%,transparent);
  font-variant-numeric:tabular-nums}
:host([data-layout="wrapped"]) #row{flex-wrap:wrap;max-height:calc(4.2em + 16px);row-gap:2px}
:host([data-layout="tall"]) #row{flex-wrap:wrap;max-height:none;row-gap:2px}
:host([data-edit="on"]) #row{flex-wrap:wrap;max-height:none;overflow:visible;row-gap:5px}
.chip{position:relative;display:inline-flex;align-items:center;gap:3px;white-space:nowrap;
  opacity:.9;padding:2px 5px;border:1px solid transparent;border-radius:7px;
  transition:background .12s ease,border-color .12s ease}
.chip:hover{background:color-mix(in srgb,currentColor 10%,transparent);
  border-color:color-mix(in srgb,currentColor 24%,transparent)}
.chip b{font-weight:600;opacity:.6;font-size:10px;letter-spacing:.02em}
.chip em{font-style:normal;font-family:var(--goat-mono,ui-monospace,Consolas,monospace)}
.chip.muted{opacity:.45}
.chip.bad em{color:var(--goat-warn,#d97706)}
.chip .cd{opacity:.62;font-size:10.5px}
.bar{display:inline-block;width:30px;height:4px;border-radius:2px;overflow:hidden;
  background:color-mix(in srgb,currentColor 18%,transparent)}
.bar>i{display:block;height:100%;border-radius:2px;background:var(--goat-accent,#7c5cf6);
  transition:width .35s ease}
.chip.warn .bar>i{background:var(--goat-warn,#d97706)}
.chip.danger .bar>i{background:var(--goat-danger,#ef4444)}
.chip.warn em,.chip.danger em{opacity:1}
.dot{padding:2px 4px;cursor:default}
.dot i{width:7px;height:7px;border-radius:50%;background:currentColor;opacity:.4}
:host([data-state="live"]) .dot i{background:var(--goat-good,#22c55e);opacity:1}
:host([data-state="stale"]) .dot i{background:var(--goat-warn,#d97706);opacity:1}
:host([data-state="error"]) .dot i{background:var(--goat-danger,#ef4444);opacity:1}
.tool{border:1px solid color-mix(in srgb,currentColor 22%,transparent);border-radius:7px;
  background:color-mix(in srgb,currentColor 6%,transparent);color:inherit;cursor:pointer;
  font:inherit;font-size:10.5px;line-height:1;padding:3px 7px;
  transition:background .12s ease,border-color .12s ease}
.tool:hover{background:color-mix(in srgb,currentColor 14%,transparent);
  border-color:color-mix(in srgb,currentColor 34%,transparent)}
.tool.primary{border-color:var(--goat-accent,#7c5cf6);color:var(--goat-accent,#7c5cf6)}
.tools{display:inline-flex;align-items:center;gap:5px}
.picker-wrap{position:relative;display:inline-flex}
.picker{position:absolute;right:0;bottom:calc(100% + 6px);z-index:30;display:flex;
  flex-direction:column;gap:2px;min-width:104px;padding:5px;border-radius:9px;
  background:var(--goat-surface,#fff);
  border:1px solid color-mix(in srgb,currentColor 24%,transparent);
  box-shadow:0 6px 22px rgba(0,0,0,.18)}
.picker .pick{border:0;background:transparent;color:inherit;font:inherit;font-size:11px;
  text-align:left;padding:4px 7px;border-radius:6px;cursor:pointer}
.picker .pick:hover{background:color-mix(in srgb,currentColor 12%,transparent)}
.picker .empty{opacity:.5;font-size:11px;padding:4px 7px}
:host([data-edit="on"]) .chip{border:1px dashed color-mix(in srgb,currentColor 38%,transparent);
  cursor:grab}
:host([data-edit="on"]) .chip.muted{cursor:default;border-style:dotted}
:host([data-edit="on"]) .chip.dragging{opacity:.55;cursor:grabbing}
.chip .hide{display:none;position:absolute;right:-5px;bottom:-5px;width:15px;height:15px;
  place-items:center;padding:0;border-radius:50%;border:1px solid color-mix(in srgb,currentColor 30%,transparent);
  background:var(--goat-surface,#fff);color:inherit;font:inherit;font-size:11px;line-height:1;
  cursor:pointer}
:host([data-edit="on"]) .chip:not(.muted) .hide{display:grid}
.chip .hide:hover{border-color:var(--goat-danger,#ef4444);color:var(--goat-danger,#ef4444)}
@media (prefers-reduced-motion:reduce){*{transition:none!important}}
`;

  /* Re-measure when the host window or composer changes size. */
  let fitQueued = false;
  function refit() {
    if (fitQueued) {
      return;
    }
    fitQueued = true;
    const step = () => {
      fitQueued = false;
      render();
    };
    if (typeof requestAnimationFrame === "function") {
      requestAnimationFrame(step);
    } else {
      setTimeout(step, 16);
    }
  }

  function watchSize(node) {
    if (!state.resizer && typeof ResizeObserver === "function") {
      state.resizer = new ResizeObserver(refit);
    }
    if (state.resizer) {
      // A remount replaces the node, so the surviving observer must track it.
      state.resizer.observe(node);
      const composer = document.querySelector(".cc-composer-surface");
      if (composer) {
        state.resizer.observe(composer);
      }
    }
    window.addEventListener("resize", refit);
  }

  /* Row-level delegation: chips are rebuilt on every render, so listeners must
   * not live on them. */
  function bindEvents(root, node) {
    const row = root.getElementById("row");
    row.addEventListener("pointerdown", (event) => {
      if (event.button !== undefined && event.button !== 0) {
        return;
      }
      event.stopPropagation();
      if (!layout.editing) {
        return;
      }
      if (event.target.getAttribute("data-hide") !== null) {
        return;
      }
      const chipNode = ownerChip(event.target);
      if (!chipNode || chipNode.classList.contains("muted")) {
        return;
      }
      drag = { id: chipNode.getAttribute("data-metric"), node: chipNode };
      chipNode.classList.add("dragging");
      row.addEventListener("pointermove", onDragMove);
      row.addEventListener("pointerup", onDragEnd);
    });
    row.addEventListener("pointermove", onDragMove);
    row.addEventListener("click", (event) => {
      event.stopPropagation();
      const target = event.target;
      const hidden = target.getAttribute("data-hide");
      if (hidden !== null) {
        hideMetric(hidden);
        return;
      }
      const picked = target.getAttribute("data-pick");
      if (picked !== null) {
        addMetric(picked);
        return;
      }
      const action = target.getAttribute("data-act");
      if (action === "edit") {
        enterEdit();
      } else if (action === "cancel") {
        cancelEdit();
      } else if (action === "save") {
        saveEdit();
      } else if (action === "add") {
        layout.picker = !layout.picker;
        render();
      }
    });
  }

  let drag = null;

  function ownerChip(target) {
    let node = target;
    while (node && !(node.classList && node.classList.contains("chip"))) {
      node = node.parentElement;
    }
    return node;
  }

  function onDragMove(event) {
    if (!drag) {
      return;
    }
    const row = drag.node.parentElement;
    if (!row) {
      return;
    }
    const siblings = chips(row).filter((c) => c !== drag.node);
    const x = event.clientX === undefined ? drag.x || 0 : event.clientX;
    let before = null;
    for (const sibling of siblings) {
      const rect = sibling.getBoundingClientRect();
      if (x < rect.left + rect.width / 2) {
        before = sibling;
        break;
      }
    }
    const anchorId = before ? before.getAttribute("data-metric") : null;
    if (!before && siblings.length) {
      moveMetric(drag.id, siblings[siblings.length - 1].getAttribute("data-metric"), false);
      return;
    }
    if (anchorId) {
      moveMetric(drag.id, anchorId, true);
    }
  }

  function onDragEnd() {
    if (!drag) {
      return;
    }
    drag.node.classList.remove("dragging");
    const row = document.getElementById(ID);
    if (row && row.shadowRoot) {
      commitDomOrder(
        [...row.shadowRoot.querySelectorAll("[data-metric]")].map((c) => c.getAttribute("data-metric"))
      );
    }
    drag = null;
    render();
  }

  function build() {
    const node = document.createElement("div");
    node.id = ID;
    node.setAttribute("role", "group");
    node.setAttribute("aria-label", locale().aria);
    const root = node.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    const row = document.createElement("div");
    row.id = "row";
    root.append(style, row);
    bindEvents(root, node);
    return node;
  }

  /* Sit directly above the composer box, as a real sibling in its flex column. */
  function mount() {
    if (!document.documentElement) {
      return false;
    }
    const composer = document.querySelector(".cc-composer") || null;
    const editor = composer ? null : document.querySelector('textarea[rows="1"]');
    const target = composer || nearestColumnChild(editor);
    if (!target || !target.parentElement) {
      return false;
    }
    const parent = target.parentElement;
    if (getComputedStyle(parent).display.indexOf("flex") === -1) {
      return false;
    }
    const existing = document.getElementById(ID);
    if (existing && existing.parentElement === parent) {
      state.mountedIn = parent;
      return true;
    }
    if (existing) {
      existing.remove();
    }
    const node = build();
    parent.insertBefore(node, target);
    state.mountedIn = parent;
    render();
    return true;
  }

  function nearestColumnChild(editor) {
    let node = editor;
    while (node && node.parentElement) {
      const parent = node.parentElement;
      if (getComputedStyle(parent).display.indexOf("flex") !== -1 &&
          getComputedStyle(parent).flexDirection === "column") {
        return node;
      }
      node = parent;
    }
    return null;
  }

  async function pullHost() {
    const api = window.electronAPI;
    if (!api || typeof api.accountOverview !== "function") {
      state.hostError = "no-bridge";
      render();
      return;
    }
    try {
      const overview = await api.accountOverview();
      if (overview && typeof overview === "object") {
        state.host = overview;
        state.hostAt = Date.now();
        state.hostError = "";
      } else {
        state.hostError = "empty";
      }
    } catch (error) {
      state.hostError = String(error && error.message ? error.message : error).slice(0, 120);
    }
    render();
  }

  function schedule(delay) {
    if (state.timer) {
      clearTimeout(state.timer);
    }
    state.timer = setTimeout(() => {
      pullHost();
      schedule(document.hidden ? POLL_HIDDEN_MS : POLL_VISIBLE_MS);
    }, delay);
  }

  function start() {
    // The injection runs before the host has rendered its composer, so a failed
    // first mount is the normal case and must leave the retry machinery behind.
    if (!state.heartbeat) {
      state.heartbeat = setInterval(tick, 1000);
      // Host re-renders replace the composer subtree; re-anchor on any change.
      state.observer = new MutationObserver(tick);
      state.observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["data-theme", "class", "lang"],
      });
      document.addEventListener("visibilitychange", () => {
        if (!document.hidden) {
          schedule(0);
        }
      });
    }
    if (mount()) {
      onMounted();
    }
  }

  function tick() {
    const node = document.getElementById(ID);
    if (!node || node.parentElement !== state.mountedIn) {
      if (mount()) {
        onMounted();
      }
      return;
    }
    const restyled = themeSignature() !== state.themeTag;
    if (restyled || String((document.documentElement || {}).lang || "") !== state.langTag ||
        (!state.host && !state.pushed)) {
      render();
    }
  }

  function onMounted() {
    const node = document.getElementById(ID);
    if (!node) {
      return;
    }
    watchSize(node);
    if (state.polling) {
      return;
    }
    state.polling = true;
    schedule(0);
  }

  window.__goatUpdate = (payload) => {
    if (!payload || typeof payload !== "object") {
      return { ok: false };
    }
    if (payload.version !== VERSION) {
      return { ok: false, reason: "version" };
    }
    state.pushed = payload;
    state.pushedAt = Date.now();
    if (isFinite(payload.server_time)) {
      state.serverOffset = payload.server_time - Date.now();
    }
    render();
    return { ok: true, mounted: Boolean(document.getElementById(ID)) };
  };

  window.__goatProbe = () => ({
    mounted: Boolean(document.getElementById(ID)),
    version: window.__goatWidgetVersion || "",
    host: Boolean(state.host),
    hostError: state.hostError,
    pushed: Boolean(state.pushed),
    layout: (document.getElementById(ID) || {}).dataset ? document.getElementById(ID).dataset.layout : "",
    editing: layout.editing,
    metrics: currentLayout(),
    dark: (document.getElementById(ID) || {}).dataset ? document.getElementById(ID).dataset.dark : "",
  });

  // Scriptable surface for the injector and for tests; also the only way to
  // drive the editor without synthetic pointer events.
  window.__goatLayout = (command, arg) => {
    switch (command) {
      case "edit":
        enterEdit();
        break;
      case "cancel":
        cancelEdit();
        break;
      case "save":
        saveEdit();
        break;
      case "hide":
        hideMetric(arg);
        break;
      case "add":
        addMetric(arg);
        break;
      case "move":
        moveMetric(arg.from, arg.to, arg.before !== false);
        break;
      case "commit":
        commitDomOrder(Array.isArray(arg) ? arg : []);
        render();
        break;
      case "picker":
        layout.picker = !layout.picker;
        render();
        break;
      default:
        break;
    }
    return currentLayout();
  };

  window.__goatDispose = () => {
    if (state.timer) clearTimeout(state.timer);
    if (state.heartbeat) clearInterval(state.heartbeat);
    if (state.observer) state.observer.disconnect();
    if (state.resizer) state.resizer.disconnect();
    window.removeEventListener("resize", refit);
    const node = document.getElementById(ID);
    if (node) node.remove();
    state.timer = state.heartbeat = state.observer = null;
    window.__goatWidgetVersion = "";
    return { ok: true };
  };

  window.__goatWidgetVersion = VERSION;
  loadLayout();
  start();
})();
