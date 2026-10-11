/*
 * GOAT Gauge widget — a compact usage readout injected above the Command Code
 * composer. Quota, credits and period totals come from the host's own preload
 * bridge (window.electronAPI, whose calls answer an envelope); derived rate
 * metrics may still be pushed in by the local service via __goatUpdate until
 * they are sampled here instead. No charts: parameters, numbers, and plain
 * progress bars only.
 */
(() => {
  "use strict";

  const VERSION = "8";
  const ID = "goat-gauge-widget";
  const POLL_VISIBLE_MS = 2500;
  const POLL_HIDDEN_MS = 15000;
  const STORAGE_KEY = "goat-gauge-layout-v2";
  const PERIOD_KEY = "goat-gauge-period-v1";

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
    pushedMode: "",
    serverOffset: 0,
    timer: null,
    polling: false,
    heartbeat: null,
    observer: null,
    resizer: null,
    mountedIn: null,
    // Spec A-5: each await below is checked against this generation, so a reply
    // that lands after a thread switch can never paint the previous conversation.
    generation: 0,
    threadId: "",
    stats: null,
    statsAt: 0,
    inFlight: false,
    samples: [],
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

  /* A progress bar with no semantics reads as nothing at all, and `4/6` is
   * spoken as "four slash six" unless valuetext spells it out (spec P-8). */
  function bar(share, tone, label, used, cap) {
    const now = Number(used);
    const max = Number(cap);
    const semantic =
      isFinite(now) && isFinite(max) && max > 0
        ? ` role="progressbar" aria-valuemin="0" aria-valuemax="${trim(max)}"` +
          ` aria-valuenow="${trim(now)}" aria-valuetext="${label} ${trim(now)} / ${trim(max)}"`
        : "";
    return (
      `<span class="bar${tone ? " " + tone : ""}"${semantic}>` +
      `<i style="width:${(clamp(share, 0, 1) * 100).toFixed(1)}%"></i></span>`
    );
  }

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
      bar(share, tone, label, used, cap) +
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

  /* A composer that exists but is hidden is not an anchor: mounting into a
   * collapsed pane yields a readout nobody can see and no error to explain it. */
  function visible(element) {
    if (!element || !element.isConnected) {
      return false;
    }
    if (element.closest("[hidden],[inert]")) {
      return false;
    }
    if (!element.getClientRects().length) {
      return false;
    }
    return getComputedStyle(element).visibility !== "hidden";
  }

  function hostPayload(overview) {
    const limits = (overview && overview.windowLimits) || {};
    const credits = (overview && overview.credits) || {};
    const usage = (overview && overview.usage) || {};
    const subscription = (overview && overview.subscription) || {};
    return {
      five: limits.fiveHour || null,
      week: limits.weekly || null,
      limited: Boolean(limits.limited),
      monthly: isFinite(credits.monthlyCredits)
        ? credits.monthlyCredits +
          (credits.purchasedCredits || 0) +
          (credits.freeCredits || 0)
        : null,
      // The host reports no monthly ceiling at all: subscription only carries
      // planId/status/period dates, and the "plan" bridge methods are agent plan
      // files. So the period boundary is what we have to anchor an estimate to.
      periodStart: String(subscription.currentPeriodStart || ""),
      periodEnd: Date.parse(subscription.currentPeriodEnd || "") || 0,
      requests: usage.totalCount,
      tokens: usage.totalTokens,
      outputTokens: usage.totalTokensOut,
      cost: usage.totalCost,
      average: usage.averageCost,
      success: usage.successRate,
    };
  }

  /* Ceiling for the monthly meter, in order of trustworthiness: the plan cap the
   * local service knows from the billing API, then the largest remaining balance
   * observed inside the current billing period - which is the allowance itself
   * once the period has just rolled over. Both are labelled identically to the
   * other meters; nothing is invented out of thin air. */
  function monthlyCap(host) {
    const pushed = pushedMonthly();
    if (isFinite(pushed.cap) && pushed.cap > 0) {
      return pushed.cap;
    }
    if (!host.periodStart || !isFinite(host.monthly)) {
      return NaN;
    }
    let record = null;
    try {
      record = JSON.parse(localStorage.getItem(PERIOD_KEY) || "null");
    } catch {
      record = null;
    }
    if (!record || record.start !== host.periodStart || !isFinite(Number(record.cap))) {
      record = { start: host.periodStart, cap: host.monthly };
    } else {
      record.cap = Math.max(Number(record.cap), host.monthly);
    }
    try {
      localStorage.setItem(PERIOD_KEY, JSON.stringify(record));
    } catch {
      /* nothing to do: the estimate simply restarts next poll */
    }
    return Number(record.cap);
  }

  /* Credits per hour is a wall-clock rate by definition. tok/s and throughput
   * are not: the host reports no timing at all, so they are normalised over the
   * intervals where counters actually advanced (busy time), never over idle
   * gaps that would deflate them by an order of magnitude (D-7). */
  const MIN_BURN_MS = 120000;

  function derivedRates() {
    const span = state.samples;
    if (span.length < 2) {
      return {};
    }
    const first = span[0];
    const last = span[span.length - 1];
    const hours = (last.at - first.at) / 3600e3;
    if (!(hours > 0) || last.at - first.at < MIN_BURN_MS) {
      return {};
    }
    let busyMs = 0;
    let output = 0;
    let requests = 0;
    for (let index = 1; index < span.length; index += 1) {
      const deltaOut = span[index].out - span[index - 1].out;
      const deltaReq = span[index].reqs - span[index - 1].reqs;
      const deltaMs = span[index].at - span[index - 1].at;
      if (deltaOut > 0 || deltaReq > 0) {
        busyMs += deltaMs;
        output += Math.max(0, deltaOut);
        requests += Math.max(0, deltaReq);
      }
    }
    const used = last.five - first.five;
    return {
      burn: used >= 0 ? used / hours : NaN,
      tokens_per_sec: busyMs > 0 && output > 0 ? output / (busyMs / 1000) : NaN,
      requests_per_min: busyMs > 0 && requests > 0 ? requests / (busyMs / 60000) : NaN,
    };
  }

  function recordSample(host) {
    const five = host && host.five ? Number(host.five.used) : NaN;
    const out = Number(host.outputTokens);
    const reqs = Number(host.requests);
    if (!isFinite(five)) {
      return;
    }
    const last = state.samples[state.samples.length - 1];
    if (last && (five < last.five - 1e-9 || out < last.out - 1e-9 || reqs < last.reqs - 1e-9)) {
      state.samples = [];  // window or account reset: the old span proves nothing
    }
    state.samples.push({ at: now(), five, out: isFinite(out) ? out : 0, reqs: isFinite(reqs) ? reqs : 0 });
    if (state.samples.length > 60) {
      state.samples.shift();
    }
  }

  function pushedMonthly() {
    const monthly = (state.pushed && state.pushed.monthly) || {};
    return {
      cap: number(monthly.cap),
      remaining: number(monthly.remaining),
      reset_at: number(monthly.reset_at) || 0,
    };
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
        const meter = monthlyMeter(host);
        if (meter) {
          return meterChip("month", L.month, meter.used, meter.cap, meter.reset);
        }
        const reset = pushedMonthly().reset_at || host.periodEnd || 0;
        // Still no ceiling by either route: show the remainder without a bar
        // rather than inventing a denominator to fill one.
        return (
          `<span class="chip meter" data-metric="month"><b>${L.month}</b>` +
          `<em>${trim(host.monthly)} ${L.remaining}</em>` +
          `<span class="cd">${countdown(reset)}</span>${chrome("month")}</span>`
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
    const busy = fresh.state === "empty" || fresh.state === "error" ? "true" : "false";
    if (!layout.editing) {
      return (
        `<span class="chip dot" data-state="${fresh.state}" title="${fresh.text}"` +
        ` aria-label="${fresh.text}"><i aria-hidden="true"></i></span>` +
        `<button class="tool" type="button" data-act="edit" title="${L.edit}"` +
        ` aria-label="${L.edit}" aria-busy="${busy}">⚙</button>`
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
    // Rates and burn prefer the local service's numbers (it holds per-request
    // durations) and fall back to what the host alone can supply.
    const pushed = {
      rates: rates(),
      projections: projections(),
      monthly: (state.pushed && state.pushed.monthly) || {},
    };
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

  const HOST_CONTROL = 'button,a[href],[role="button"],[role="combobox"],input,select,[contenteditable="true"]';

  function intersection(a, b) {
    const left = Math.max(a.left, b.left);
    const right = Math.min(a.right, b.right);
    const top = Math.max(a.top, b.top);
    const bottom = Math.min(a.bottom, b.bottom);
    if (right - left <= 0 || bottom - top <= 0) {
      return null;
    }
    return { left, right, top, bottom };
  }

  /* Two boxes sharing an area does not prove one paints over the other: a host
   * button can overlap the bar's rectangle while the bar is on top of it, and a
   * decorative layer can own the point instead. Ask who actually receives the
   * pointer at the overlap's centre and four corners. */
  function pointHitsHost(node, area) {
    if (typeof document.elementFromPoint !== "function") {
      return true;  // without a hit test, stay conservative: hide rather than cover
    }
    const probes = [
      [(area.left + area.right) / 2, (area.top + area.bottom) / 2],
      [area.left + 1, area.top + 1],
      [area.right - 1, area.top + 1],
      [area.left + 1, area.bottom - 1],
      [area.right - 1, area.bottom - 1],
    ];
    for (const probe of probes) {
      const hit = document.elementFromPoint(Math.round(probe[0]), Math.round(probe[1]));
      if (!hit || hit === node || node.contains(hit)) {
        continue;
      }
      let style = null;
      try {
        style = getComputedStyle(hit);
      } catch {
        style = null;
      }
      if (style && (style.visibility === "hidden" || style.pointerEvents === "none")) {
        continue;  // not something the user can actually click
      }
      return true;
    }
    return false;
  }

  /* The readout never paints over a host control: overlapping the composer, a
   * guard inside the composer's own row, or a real hit on an external control
   * all hide it. */
  function guardAgainstHost(node, row) {
    const mine = node.getBoundingClientRect();
    const composer = document.querySelector(".cc-composer");
    const surface = document.querySelector(".cc-composer-surface") || composer;
    let blocked =
      mine.width < 40 ||
      chips(row).length === 0 ||
      Boolean(surface && mine.bottom > surface.getBoundingClientRect().top + 1);
    if (!blocked) {
      for (const control of [...document.querySelectorAll(HOST_CONTROL)].filter(visible)) {
        if (node.contains(control)) {
          continue;
        }
        const area = intersection(mine, control.getBoundingClientRect());
        if (!area) {
          continue;
        }
        if (composer && composer.contains(control)) {
          blocked = true;
          break;
        }
        if (pointHitsHost(node, area)) {
          blocked = true;
          break;
        }
      }
    }
    if (blocked) {
      node.dataset.layout = "blocked";
      // visibility:hidden alone still lets a screen reader speak the bar and Tab
      // still walks into it, which is the opposite of "not present".
      node.inert = true;
      node.setAttribute("aria-hidden", "true");
    } else {
      node.inert = false;
      node.removeAttribute("aria-hidden");
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
.tool:focus-visible,.pick:focus-visible,.hide:focus-visible{outline:2px solid var(--goat-accent,#7c5cf6);
  outline-offset:1px;border-radius:6px}
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

  /* Sit directly above the composer box. The composer's four ancestors measure
   * display:block on the live host, so this is a block sibling - the earlier
   * flex-column requirement was inferred from a stale build and made every
   * real mount return false without a single error. */
  function mount() {
    if (!document.documentElement) {
      return false;
    }
    const composers = [...document.querySelectorAll(".cc-composer")].filter(visible);
    if (composers.length !== 1) {
      // Zero means the composer is not built yet; more than one means we cannot
      // tell which one owns this row. Guessing is not a fallback.
      return false;
    }
    const target = composers[0];
    const parent = target.parentElement;
    if (!parent) {
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

  /* The bridge answers an envelope ({success, overview}); reading the envelope as
   * the payload leaves every field undefined while the object check still passes
   * - a readout that shows nothing and reports no error. */
  const ENVELOPE_KEY = { accountOverview: "overview", getAppState: "state", sessionStats: "stats" };

  function unwrap(name, reply) {
    if (!reply || typeof reply !== "object" || !("success" in reply)) {
      return reply;
    }
    if (reply.success === false) {
      return null;
    }
    const key = ENVELOPE_KEY[name];
    if (key && reply[key] !== undefined) {
      return reply[key];
    }
    const rest = Object.keys(reply).filter((item) => item !== "success" && !/error/i.test(item));
    return rest.length === 1 ? reply[rest[0]] : reply;
  }

  function readThread(api) {
    if (typeof api.getAppState !== "function") {
      return Promise.resolve("");
    }
    return api.getAppState().then((reply) => {
      const loaded = unwrap("getAppState", reply);
      return loaded && typeof loaded.activeThreadId === "string" ? loaded.activeThreadId : "";
    });
  }

  function cacheHit(stats) {
    const tokens = stats && stats.tokens;
    if (!tokens) {
      return NaN;
    }
    const read = Number(tokens.cacheRead);
    const input = Number(tokens.input);
    if (!isFinite(read) || !isFinite(input) || read + input <= 0) {
      return NaN;  // absence is not zero (D-2)
    }
    return (read / (read + input)) * 100;
  }

  /* used and cap must come from the SAME pool: the service's plan quota
   * (cap/remaining) and the host's credit balance are different things, and
   * pairing one's numerator with the other's denominator is simply false data. */
  function monthlyMeter(host) {
    const pushed = pushedMonthly();
    if (isFinite(pushed.cap) && pushed.cap > 0 && isFinite(pushed.remaining)) {
      return { used: Math.max(0, pushed.cap - pushed.remaining), cap: pushed.cap,
        reset: pushed.reset_at || (host && host.periodEnd) || 0, source: "plan" };
    }
    const estimate = isFinite(host && host.monthly) ? monthlyCap(host) : NaN;
    if (isFinite(estimate) && estimate > 0) {
      return { used: Math.max(0, estimate - host.monthly), cap: estimate,
        reset: (host && host.periodEnd) || pushed.reset_at || 0, source: "est" };
    }
    return null;
  }

  /* Number(null) and Number("") are both 0, so a field the server deliberately
   * omitted must not arrive as a measured zero (spec D-2). */
  const number = (value) =>
    value === null || value === undefined || value === "" ? NaN : Number(value);

  function rates() {
    const pushed = (state.pushed && state.pushed.rates) || {};
    const derived = derivedRates();
    const pick = (name, value) => (isFinite(number(value)) ? number(value) : number(derived[name]));
    const hit = number(pushed.hit_rate_pct);
    return {
      tokens_per_sec: pick("tokens_per_sec", pushed.tokens_per_sec),
      requests_per_min: pick("requests_per_min", pushed.requests_per_min),
      hit_rate_pct: isFinite(hit) ? hit : cacheHit(state.stats),
    };
  }

  function projections() {
    const pushed = ((state.pushed && state.pushed.projections) || {}).five_hour || {};
    const burn = number(pushed.burn_per_hour);
    if (isFinite(burn)) {
      return { five_hour: { burn_per_hour: burn, verdict: pushed.verdict } };
    }
    const derived = number(derivedRates().burn);
    return { five_hour: isFinite(derived) ? { burn_per_hour: derived } : {} };
  }

  async function pullHost() {
    if (state.inFlight) {
      return;  // one flight per cycle; a heartbeat during a pull is redundant work
    }
    const api = window.electronAPI;
    if (!api || typeof api.accountOverview !== "function") {
      state.hostError = "no-bridge";
      render();
      return;
    }
    state.inFlight = true;
    const generation = state.generation;
    const stale = () => generation !== state.generation;
    try {
      const reply = await api.accountOverview();
      if (stale()) {
        return;
      }
      const overview = unwrap("accountOverview", reply);
      if (reply && typeof reply === "object" && reply.success === false) {
        state.host = null;
        state.hostError = "unsuccessful";
      } else if (overview && typeof overview === "object") {
        state.host = overview;
        state.hostAt = Date.now();
        state.hostError = "";
        recordSample(hostPayload(overview));
      } else {
        state.host = null;
        state.hostError = "empty";
      }

      const threadId = await readThread(api);
      if (stale()) {
        return;
      }
      if (threadId && threadId !== state.threadId) {
        // A new conversation owns none of the previous session's numbers, so every
        // session-scoped value is dropped and the in-flight result is orphaned by
        // the bumped generation. The credit window is account-wide, so its newest
        // sample survives.
        state.threadId = threadId;
        state.stats = null;
        state.statsAt = 0;
        state.samples = state.samples.slice(-1);
        state.generation = generation + 1;
        // Quota already arrived in this pass; without this the board would sit on
        // "waiting" for another whole poll cycle after every thread switch.
        render();
        return;
      }
      if (state.threadId && typeof api.sessionStats === "function") {
        const statsReply = await api.sessionStats(state.threadId);
        if (stale()) {
          return;
        }
        const stats = unwrap("sessionStats", statsReply);
        if (stats && typeof stats === "object") {
          state.stats = stats;
          state.statsAt = Date.now();
        } else {
          state.stats = null;
        }
      }
    } catch (error) {
      if (!stale()) {
        state.hostError = String(error && error.message ? error.message : error).slice(0, 120);
      }
    } finally {
      state.inFlight = false;
    }
    if (!stale()) {
      render();
    }
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
    state.pushedMode = payload.mode === "demo" ? "demo" : payload.mode === "live" ? "live" : "unknown";
    if (isFinite(payload.server_time)) {
      state.serverOffset = payload.server_time - Date.now();
    }
    render();
    return { ok: true, mounted: Boolean(document.getElementById(ID)) };
  };

  /* Presentation state that can be compared, not just logged: a layout or motion
   * change has to show a difference here before anyone may claim it took effect
   * (spec P-7). Numbers, ids and class names only - no account data. */
  function fingerprint() {
    const node = document.getElementById(ID);
    if (!node || !node.shadowRoot) {
      return "absent";
    }
    const row = node.shadowRoot.getElementById("row");
    const board = row
      ? chips(row).map((chipNode) => chipNode.dataset.metric || "tool").join("+")
      : "";
    const measured = row ? Math.round(row.scrollWidth) : 0;
    const height = row ? Math.round(row.clientHeight) : 0;
    const burn = Number(derivedRates().burn);
    return [
      node.dataset.state,
      node.dataset.layout,
      node.dataset.edit,
      String((document.documentElement || {}).lang || ""),
      node.dataset.dark,
      node.inert ? "inert" : "live",
      board,
      `w${measured}xh${height}`,
      `src=${state.pushed ? "push:" + state.pushedMode : "-"}+${state.host ? "host" : "-"}+${isFinite(burn) ? "sample" : "-"}` +
      `+${(() => { const host = state.host ? hostPayload(state.host) : null; const m = host && monthlyMeter(host); return m ? "m" + m.source : "m-"; })()}`,
    ].join("|");
  }

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
    fingerprint: fingerprint(),
    session: state.threadId ? "known" : "none",
    statsAt: state.statsAt,
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
    state.generation += 1;  // orphan any in-flight bridge call before the board goes away
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
