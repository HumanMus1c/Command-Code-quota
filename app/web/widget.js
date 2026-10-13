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

  const VERSION = "19";
  const ID = "goat-gauge-widget";
  const POLL_VISIBLE_MS = 2500;
  const POLL_HIDDEN_MS = 15000;
  const STORAGE_KEY = "goat-gauge-layout-v2";
  const PERIOD_KEY = "goat-gauge-period-v1";

  if (window.__goatWidgetVersion === VERSION && document.getElementById(ID)) {
    return;
  }
  /* One painter per row. A page can end up with two closures of the same version
   * because the guard above only asks whether a node exists: when the host's React
   * drops our node the next injection builds a second one while the first closure's
   * heartbeat and poll are still running - and each paints the board it remembers
   * into whichever node answers to the id. That is how a metric gets drawn twice,
   * its neighbour vanishes, and the status dot turns into a chip. Taking over means
   * disposing the previous owner first, whatever its version said it was. */
  if (typeof window.__goatDispose === "function") {
    try {
      window.__goatDispose();
    } catch {
      // A closure too broken to dispose must not be able to block the new one.
    }
  }

  const LOCALES = {
    zh: {
      week: "周",
      month: "月",
      requests: "请求",
      tokens: "TOKEN",
      cost: "费用",
      average: "均价",
      context: "上下文",
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
      context: "Ctx",
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
    visibility: null,
    mountedIn: null,
    // The element this closure built. Painting is only ever allowed into it:
    // a closure that lost the node has lost the board, not inherited it.
    node: null,
    slots: [],
    shapeChanges: [],
    reorders: 0,
    // A drag that crossed a chip ends on a click over its new neighbour; that click
    // is the release, not an intent to hide. Timed, so a keyboard click an action
    // button takes later is never swallowed.
    suppressUntil: 0,
    shedLevel: -1,
    shedChanges: [],
    levelChanges: 0,
    fitWidth: 0,
    climb: true,
    boardLog: [],
    boardChanges: 0,
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
    { id: "ctx", label: () => locale().context },
    { id: "burn", label: () => locale().consumption },
    { id: "cache", label: () => locale().cacheHit },
    { id: "rate", label: () => locale().throughput },
    { id: "requests", label: () => locale().requests },
    { id: "tokens", label: () => locale().tokens },
    { id: "cost", label: () => locale().cost },
    { id: "avg", label: () => locale().average },
  ];
  const DEFAULT_ORDER = METRICS.map((metric) => metric.id);

  function placeholderHtml(id) {
    const metric = METRICS.filter((item) => item.id === id)[0];
    // An em dash, not a zero: D-2 forbids inventing a reading. "idle" rather than
    // "muted" so the editor still offers the hide control - a placeholder is a real
    // metric the user may want gone.
    return chip(id, metric ? metric.label() : id, "—", "idle");
  }

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
        Array.isArray(list)
          ? list.filter((id, index) => DEFAULT_ORDER.indexOf(id) >= 0 && list.indexOf(id) === index)
          : [];
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
   * events; the drag handler only ever computes a target index. A pointer moves
   * many times between two crossings, so a wanted sequence that is already the
   * current one must not cost a render. */
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
    if (order.join(" ") === layout.order.join(" ")) {
      return;
    }
    layout.order = order;
    render();
  }

  function commitDomOrder(ids) {
    // The DOM is only evidence of intent when it holds exactly the chips this
    // layout renders. Anything else - a repeated id, an id we are hiding, an id
    // we never had - belongs to somebody else's write, and baking it in is how a
    // duplicate of one chip silently displaces its neighbour for good.
    const keeps = layout.order.filter((id) => layout.hidden.indexOf(id) < 0);
    const seen = [];
    for (const id of ids) {
      if (keeps.indexOf(id) >= 0 && seen.indexOf(id) < 0) {
        seen.push(id);
      }
    }
    if (seen.length !== keeps.length) {
      return false;
    }
    const result = layout.order.slice();
    // Rewrite only the slots the rendered metrics occupied. Metrics that happen
    // to have no data right now keep their position instead of being pushed to
    // the tail by an absence that is not the user's doing.
    const slots = [];
    for (let index = 0; index < result.length; index += 1) {
      if (keeps.indexOf(result[index]) >= 0) {
        slots.push(index);
      }
    }
    slots.forEach((slot, position) => {
      result[slot] = seen[position];
    });
    layout.order = result;
    return true;
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

  /* The affordance has to be discoverable before the first failed drag: a dashed
   * border alone does not say the chip may be moved. Only `title` - an aria-label
   * here would replace the reading of the label and value instead of adding to it. */
  function grab() {
    return layout.editing ? ` title="${locale().dragHint}"` : "";
  }

  const chip = (id, label, value, extraClass) =>
    `<span class="chip${extraClass ? " " + extraClass : ""}" data-metric="${id}"${grab()}>` +
    `<b>${label}</b><em>${value}</em>${chrome(id)}</span>`;

  function meterChip(id, label, used, cap, resetAt, extraClass) {
    if (!isFinite(cap) || cap <= 0) {
      return "";
    }
    const share = used / cap;
    const tone = share >= 0.95 ? "danger" : share >= 0.85 ? "warn" : "";
    return (
      `<span class="chip meter${tone ? " " + tone : ""}" data-metric="${id}"${grab()}>` +
      `<b>${label}</b><em>${trim(used)}/${trim(cap)}</em>` +
      bar(share, tone, label, used, cap) +
      `<span class="cd">${countdown(resetAt)}</span>${chrome(id)}</span>`
    );
  }

  /* Every write is guarded: an unchanged poll must not touch the DOM at all.
   * Unconditional dataset/attribute writes each queue a mutation record even when
   * the value is identical, and that steady drip is what reads as a flicker. */
  function setData(element, name, value) {
    if (element.dataset[name] !== value) {
      element.dataset[name] = value;
    }
  }

  function setAttr(element, name, value) {
    if (element.getAttribute(name) !== value) {
      element.setAttribute(name, value);
    }
  }

  function removeAttr(element, name) {
    if (element.hasAttribute(name)) {
      element.removeAttribute(name);
    }
  }

  function setFlag(element, name, on) {
    if (element.classList.contains(name) !== on) {
      element.classList.toggle(name, on);
    }
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
          `<span class="chip meter" data-metric="month"${grab()}><b>${L.month}</b>` +
          `<em>${trim(host.monthly)} ${L.remaining}</em>` +
          `<span class="cd">${countdown(reset)}</span>${chrome("month")}</span>`
        );
      }
      case "ctx": {
        // The per-thread context window is live. The account-level token counters
        // are not - they are cached period aggregates, which is why throughput in
        // tok/s could never yield a difference and was replaced by this (D-12).
        const usage = (state.stats && state.stats.contextUsage) || {};
        const current = number(usage.current);
        const limit = number(usage.limit);
        if (!isFinite(current) || !isFinite(limit) || limit <= 0) {
          return "";
        }
        const share = current / limit;
        const tone = share >= 0.95 ? "danger" : share >= 0.85 ? "warn" : "";
        // Same shape as every other meter; the numbers go through compact() because
        // a token window is five or six digits and trim() would print them raw.
        return `<span class="chip meter${tone ? " " + tone : ""}" data-metric="ctx"${grab()}>` +
          `<b>${L.context}</b><em>${compact(current)}/${compact(limit)}</em>` +
          bar(share, tone, L.context, current, limit) +
          // No countdown: a context window has no reset moment, and an empty .cd
          // would be a shed target with nothing to shed.
          chrome("ctx") + "</span>";
      }
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
      return [
        `<span class="chip dot" data-state="${fresh.state}" title="${fresh.text}"` +
        ` aria-label="${fresh.text}"><i aria-hidden="true"></i></span>`,
        `<button class="tool" type="button" data-act="edit" title="${L.edit}"` +
        ` aria-label="${L.edit}" aria-busy="${busy}">⚙</button>`,
      ];
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
    return [
      `<span class="chip dot" data-state="${fresh.state}" title="${fresh.text}"` +
      ` aria-label="${fresh.text}"><i></i></span>`,
      `<span class="tools"><span class="picker-wrap">${picker}` +
      `<button class="tool" type="button" data-act="add" title="${L.add}" aria-label="${L.add}"` +
      ` aria-haspopup="true" aria-expanded="${layout.picker ? "true" : "false"}">${L.add} ▾</button></span>` +
      `<button class="tool" type="button" data-act="cancel">${L.cancel}</button>` +
      `<button class="tool primary" type="button" data-act="save">${L.save}</button></span>`,
    ];
  }

  const partKey = (markup) => {
    const metric = /data-metric="([^"]+)"/.exec(markup);
    if (metric) {
      return metric[1];
    }
    const act = /data-act="([^"]+)"/.exec(markup);
    if (act) {
      return `act:${act[1]}`;
    }
    const cls = /class="([^"]*)"/.exec(markup);
    return cls ? `.${cls[1].split(" ")[0]}` : markup.slice(0, 16);
  };

  /* Patch the existing element instead of replacing it: the node survives, so the
   * bar's width transition continues from its previous value instead of restarting
   * from zero - which is the flicker a whole-chip swap still causes. */
  function patchNode(target, source) {
    for (const name of source.getAttributeNames()) {
      if (name === "class" || name === "style") {
        continue;
      }
      setAttr(target, name, source.getAttribute(name));
    }
    for (const name of target.getAttributeNames()) {
      if (name === "class" || name === "style" || source.hasAttribute(name)) {
        continue;
      }
      target.removeAttribute(name);
    }
    for (const cls of source.classList) {
      setFlag(target, cls, true);
    }
    for (const cls of [...target.classList]) {
      // `shed` is the row's width decision and `dragging` is the pointer's, so a
      // content patch must never undo either one.
      if (cls === "shed" || cls === "dragging") {
        continue;
      }
      setFlag(target, cls, source.classList.contains(cls));
    }
    const style = source.getAttribute("style");
    if (style === null) {
      removeAttr(target, "style");
    } else {
      setAttr(target, "style", style);
    }

    const wanted = [...source.childNodes];
    const current = [...target.childNodes];
    if (wanted.length === 1 && wanted[0].nodeType === 3) {
      const text = wanted[0].nodeValue;
      if (target.textContent !== text) {
        target.textContent = text;
      }
      return;
    }
    if (wanted.length !== current.length) {
      target.innerHTML = source.innerHTML;
      return;
    }
    wanted.forEach((node, index) => {
      const twin = current[index];
      if (node.nodeType === 3 && twin.nodeType === 3) {
        if (twin.nodeValue !== node.nodeValue) {
          twin.nodeValue = node.nodeValue;
        }
      } else if (node.nodeType === 1 && twin.nodeType === 1) {
        patchNode(twin, node);
      } else {
        target.innerHTML = source.innerHTML;
      }
    });
  }

  /* One slot per top-level element, keyed by metric. Shape changes (mount, edit
   * mode, layout save) still take the full write; a data change only touches the
   * slots whose generated markup differs, and even then patches in place.
   * Generated strings are compared against generated strings - never against
   * outerHTML - because shedding adds classes to descendants afterwards.
   *
   * A reorder is not a shape change: when the same elements are wanted in a new
   * sequence they are moved, not regenerated. Rebuilding the row was what made a
   * drag die after one crossing - the element under the pointer became a stranger,
   * its `dragging` class went with it, and every bar restarted its transition. */
  function paint(row, parts) {
    const keys = parts.map(partKey);
    const previous = state.slots;
    const sameShape =
      previous.length === parts.length && previous.every((slot, index) => slot.key === keys[index]);
    if (sameShape) {
      patchSlots(previous, parts);
      return;
    }
    const held = new Map();
    for (const slot of previous) {
      if (slot.node && slot.node.isConnected && !held.has(slot.key)) {
        held.set(slot.key, slot.node);
      }
    }
    const movable =
      new Set(keys).size === keys.length && keys.length === held.size &&
      keys.every((key) => held.has(key));
    if (movable) {
      const wanted = keys.map((key) => held.get(key));
      const remembered = new Map(previous.map((slot) => [slot.key, slot]));
      reorder(row, wanted);
      state.reorders += 1;
      state.slots = keys.map((key, index) => {
        const slot = remembered.get(key) || { markup: "" };
        return { key, markup: slot.markup, node: wanted[index] };
      });
      patchSlots(state.slots, parts);
      return;
    }
    // Bounded diagnostic: a shape change is the one case that still rewrites the
    // whole row, so it has to name itself instead of being inferred from a
    // MutationObserver count.
    if (state.shapeChanges.length < 4) {
      state.shapeChanges.push({
        had: previous.map((slot) => slot.key).join(","),
        now: keys.join(","),
      });
    }
    row.innerHTML = parts.join("");
    // Slots are taken from what the DOM actually holds, so a substrate that
    // models fewer elements than we generated (the string-level shim) keeps
    // taking the full-write path instead of patching the wrong node.
    state.slots = [...row.children].map((child, index) => {
      const outer = typeof child.outerHTML === "string" ? child.outerHTML : "";
      return { key: partKey(outer || parts[index] || ""), markup: outer, node: child };
    });
  }

  function patchSlots(slots, parts) {
    slots.forEach((slot, index) => {
      const markup = parts[index];
      if (slot.markup === markup) {
        return;
      }
      slot.markup = markup;
      const holder = document.createElement("div");
      holder.innerHTML = markup;
      const source = holder.firstElementChild;
      if (source && slot.node) {
        patchNode(slot.node, source);
      }
    });
  }

  /* Move only the elements that are out of place, then slide them into their new
   * places instead of teleporting: an order that changes silently is exactly the
   * "no visual feedback" complaint. Web Animations leaves no attribute behind, so
   * an idle board still writes nothing at all. */
  function reorder(row, wanted) {
    const slides =
      typeof Element === "function" && typeof wanted[0].animate === "function" && !reducedMotion();
    const first = wanted.map((node) => (slides ? node.getBoundingClientRect().left : 0));
    wanted.forEach((node, index) => {
      const at = row.children[index];
      if (at !== node) {
        row.insertBefore(node, at || null);
      }
    });
    if (!slides) {
      return;
    }
    wanted.forEach((node, index) => {
      if (drag && drag.node === node) {
        return;  // the lifted chip sits where the pointer put it
      }
      const shift = first[index] - node.getBoundingClientRect().left;
      if (Math.abs(shift) < 1) {
        return;
      }
      node.animate(
        [{ transform: `translateX(${shift}px)` }, { transform: "translateX(0)" }],
        { duration: 160, easing: "ease-out" }
      );
    });
  }

  function reducedMotion() {
    return (
      typeof window.matchMedia === "function" &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches
    );
  }

  function render() {
    const node = document.getElementById(ID);
    if (!node || node !== state.node) {
      // Not the row this closure built: whatever answers to the id now is another
      // painter's board, and writing into it is the double-draw that eats metrics.
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
    const fresh = freshness();

    if (!host && !state.pushed) {
      // Before the first answer there is nothing to show but the waiting state -
      // the full board arrives with initialization, not as a wall of dashes.
      parts.push(`<span class="chip muted" data-metric="waiting"><em>${L.waiting}</em></span>`);
    } else {
      for (const id of layout.order) {
        if (layout.hidden.indexOf(id) >= 0) {
          continue;
        }
        // The board's membership is constant: every metric the user keeps is always
        // there, and one that has no reading right now shows an em dash. A chip
        // arriving or leaving is 60-90px of re-flow, which is what made the readout
        // jump between 28px and 49px while the user was typing, and in the editor it
        // pushed 取消 / 保存 / 添加 out of view. An em dash is not a zero (D-2).
        parts.push(metricHtml(id, host, pushed, L) || placeholderHtml(id));
      }
    }

    parts.push(...toolsHtml(fresh));

    const root = node.shadowRoot;
    const row = root.getElementById("row");
    // Only touch the DOM when the board actually changed: rewriting the row every
    // poll destroyed and rebuilt every chip, restarted each bar's width transition
    // from zero, and is what read as ghosting on the host.
    paint(row, parts);
    setData(node, "state", fresh.state);
    setData(node, "edit", layout.editing ? "on" : "off");
    setAttr(node, "aria-label", L.aria);
    state.langTag = String((document.documentElement || {}).lang || "");
    state.themeTag = themeSignature();
    applyTheme(node);
    if (layout.editing) {
      setData(node, "layout", "editing");
    } else {
      fit(root, node);
    }
    observeBoard(row, node);
  }

  /* The height jitter cannot be reproduced in the fixture (its data is constant by
   * construction) and an idle window on the host never showed it, so it has to be
   * caught in the act by the board itself: every time the rendered height or the
   * membership moves, the transition is logged with its timestamp. The log is a
   * ring of the last moves plus a total, so a fast flicker is still measurable
   * after the buffer wrapped. */
  function observeBoard(row, node) {
    const height = Math.round(row.clientHeight);
    const board = chips(row).map((chipNode) => chipNode.dataset.metric || "tool").join("+");
    const last = state.boardLog[state.boardLog.length - 1];
    if (last && last.h === height && last.board === board) {
      return;
    }
    state.boardChanges += 1;
    if (state.boardLog.length >= 24) {
      state.boardLog.shift();
    }
    state.boardLog.push({
      t: Math.round(now() / 1000),
      h: height,
      chips: row.childElementCount,
      level: state.shedLevel,
      tier: node.dataset.layout,
      board,
    });
  }

  /* The host ships Tailwind-style channel values ("134 61% 67%", sometimes with
   * " / alpha"), which are only usable once wrapped in a color function. Dropping
   * one straight into `background:` is an invalid declaration the browser throws
   * away, so the dot and the progress fill render transparent and nothing errors. */
  const CHANNELS = /^[-0-9.]+\s+[-0-9.]+%\s+[-0-9.]+%(\s*\/\s*[-0-9.]+)?$/;
  const USABLE = /^(#[0-9a-f]{3,8}|rgba?\(|hsla?\(|oklch\(|color-mix\(|currentcolor|transparent|white|black|inherit)/i;

  function usableColor(value, fallback) {
    const raw = String(value === null || value === undefined ? "" : value).trim();
    if (!raw) {
      return fallback;
    }
    if (USABLE.test(raw)) {
      return raw;
    }
    if (CHANNELS.test(raw)) {
      return `hsl(${raw.replace(/\s*\/\s*/, " / ")})`;
    }
    if (/^[-0-9.]+$/.test(raw)) {
      return `hsl(${raw} 60% 55%)`;  // a bare hue channel
    }
    return fallback;
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
      node.style.setProperty(entry[0], usableColor(value, fallback));
    }
    setData(node, "dark", dark ? "1" : "0");
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
   * and the editor always show the same set.
   *
   * Presentation pressure is one number: the shed level. -1 is the single line,
   * 0..N means N decorations are hidden in priority order, and once every
   * decoration is gone the height budget goes instead. A poll only reads the
   * measurement and steps the level down when the row proves it is overflowing,
   * so a board whose width did not change writes no class at all. Climbing back up
   * costs the width a lower level would need, which can only be measured by
   * un-shedding first - so the *only* event allowed to re-open the decision is the
   * available width changing. A rebuilt row re-applies the level it already has. */
  const TIER_ATTRS = ["full", "wrapped", "tall"];

  function fit(root, node) {
    const row = root.getElementById("row");
    let level = state.shedLevel;
    if (TIER_ATTRS.indexOf(node.dataset.layout || "") < 0) {
      // The attribute is not naming a tier we can produce: "editing" is left behind
      // when the editor closes, and "blocked" when the occlusion guard last hid us.
      // Neither matches a rule, so the row silently falls back to nowrap + overflow
      // hidden and clips whatever does not fit - or stays invisible forever. Trusting
      // the recorded level here is what made the board collapse to one line.
      state.climb = true;
    }
    // The width this tier was judged against. A resize callback reporting the same
    // width is our own height change coming back around, and re-judging for it is
    // what made the board alternate between one line and two on a stable layout.
    state.fitWidth = row.clientWidth;
    if (state.climb || level < 0) {
      // Force the no-wrap state before measuring: scrollWidth is only an honest
      // "does it fit on one line" reading while the row is not already wrapping.
      setData(node, "layout", "full");
      const spare = row.clientWidth - row.scrollWidth;
      // Staying on one line costs nothing; returning to it needs 12px of slack, so
      // a width sitting on the boundary cannot oscillate between tiers.
      if (spare >= (level < 0 ? -1 : 12)) {
        applyLevel(row, 0);
        setLevel(-1);
        state.climb = false;
        guardAgainstHost(node, row);
        return;
      }
      level = 0;
      applyLevel(row, 0);
      state.climb = false;
      setData(node, "layout", "wrapped");
    } else {
      applyLevel(row, level);
    }
    while (row.scrollHeight > row.clientHeight + 1) {
      if (!applyLevel(row, level + 1)) {
        // Nothing left to shed but the budget; grow instead of dropping data.
        setData(node, "layout", "tall");
        break;
      }
      level += 1;
    }
    setLevel(level);
    guardAgainstHost(node, row);
  }

  function chips(row) {
    return [...row.querySelectorAll(".chip")];
  }

  function decorations(row) {
    const items = [];
    for (const selector of [".cd", ".bar"]) {
      items.push(...row.querySelectorAll(selector));
    }
    return items;
  }

  /* One guarded write per decoration whose shed state really differs, so asking
   * for the level the row already holds changes nothing. False means the row
   * cannot be degraded any further: every decoration is already shed. */
  function applyLevel(row, level) {
    const items = decorations(row);
    if (level > items.length) {
      return false;
    }
    items.forEach((decoration, index) => setFlag(decoration, "shed", index < level));
    return true;
  }

  /* A level change is the only thing that may cost classes on the board, so it
   * names itself instead of being inferred from a MutationObserver count. The
   * list is bounded, the counter is not: an idle window has to be provable as
   * "the level never moved" even after the ring buffer filled up. */
  function setLevel(level) {
    if (level === state.shedLevel) {
      return;
    }
    state.levelChanges += 1;
    if (state.shedChanges.length < 4) {
      state.shedChanges.push({ from: state.shedLevel, to: level });
    }
    state.shedLevel = level;
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
      setData(node, "layout", "blocked");
      // visibility:hidden alone still lets a screen reader speak the bar and Tab
      // still walks into it, which is the opposite of "not present".
      if (node.inert !== true) {
        node.inert = true;
      }
      setAttr(node, "aria-hidden", "true");
    } else if (node.inert !== false) {
      node.inert = false;
      removeAttr(node, "aria-hidden");
    }
  }

  const STYLE = `
/* cursor:default goes on the host, not on the chips: the I-beam appears over any
 * text box whose cursor is auto, and a readout is nothing but text boxes.
 * Selection itself stays available outside the editor - only the caret shape is
 * normalised, so selecting a number never looks like a drag handle. */
:host{display:block;font-family:var(--goat-font,inherit);font-size:11.5px;line-height:1.4;
  color:inherit;position:relative;cursor:default}
:host([data-layout="blocked"]){visibility:hidden;pointer-events:none}
#row{display:flex;align-items:center;gap:7px;flex-wrap:nowrap;overflow:hidden;
  margin:0 auto;max-width:100%;padding:3px 8px;border-radius:8px;
  background:color-mix(in srgb,currentColor 6%,transparent);
  border:1px solid color-mix(in srgb,currentColor 14%,transparent);
  font-variant-numeric:tabular-nums}
:host([data-layout="wrapped"]) #row{flex-wrap:wrap;max-height:calc(4.2em + 16px);row-gap:2px}
:host([data-layout="tall"]) #row{flex-wrap:wrap;max-height:none;row-gap:2px}
/* The editor is all pointer: a press-drag across chips is a reorder, and the
 * browser reading it as a text selection paints the board purple. */
:host([data-edit="on"]) #row{flex-wrap:wrap;max-height:none;overflow:visible;row-gap:5px;
  user-select:none;-webkit-user-select:none}
.chip{position:relative;display:inline-flex;align-items:center;gap:3px;white-space:nowrap;
  opacity:.9;padding:2px 5px;border:1px solid transparent;border-radius:7px;
  transition:background .12s ease,border-color .12s ease,box-shadow .12s ease}
.chip:hover{background:color-mix(in srgb,currentColor 10%,transparent);
  border-color:color-mix(in srgb,currentColor 24%,transparent)}
.chip b{font-weight:600;opacity:.6;font-size:10px;letter-spacing:.02em}
.chip em{font-style:normal;font-family:var(--goat-mono,ui-monospace,Consolas,monospace)}
.chip.muted{opacity:.45}
.chip.idle{opacity:.62}
.chip.bad em{color:var(--goat-warn,#d97706)}
.chip .cd{opacity:.62;font-size:10.5px}
.cd.shed,.bar.shed{display:none}
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
:host([data-edit="on"]) .chip.dragging{opacity:1;cursor:grabbing;z-index:2;
  border:1px solid var(--goat-accent,#7c5cf6);
  background:color-mix(in srgb,var(--goat-accent,#7c5cf6) 16%,transparent);
  box-shadow:0 5px 16px rgba(0,0,0,.25)}
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
      const node = document.getElementById(ID);
      const row = node && node === state.node && node.shadowRoot
        ? node.shadowRoot.getElementById("row")
        : null;
      // Only the width the tier was judged against can buy presentation back. The
      // observer also fires for our own height changes, and re-opening the decision
      // for those is a two-state oscillator: wrap, grow, observe, re-judge, unwrap.
      if (row && row.clientWidth !== state.fitWidth) {
        state.climb = true;
      }
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

  /* Row-level delegation: a render can replace any chip at any moment, so the
   * listeners that must never be lost live on the row - and the ones that have to
   * outlive the row entirely (the release) live on the window. */
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
      if (!chipNode || !chipNode.getAttribute("data-metric")) {
        return;
      }
      if (chipNode.classList.contains("muted")) {
        return;
      }
      beginDrag(event, chipNode);
    });
    row.addEventListener("click", (event) => {
      event.stopPropagation();
      const target = event.target;
      if (now() < state.suppressUntil) {
        // A drag releases over the chip it just jumped over, and every hide button
        // overhangs 5px into its neighbour: without this the drag itself hides a
        // metric, which is a very convincing "the board ate my data".
        state.suppressUntil = 0;
        return;
      }
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

  function beginDrag(event, chipNode) {
    // A second press while one is live must close the first: its class would stay
    // on a chip nobody is holding, and its listeners would never be withdrawn.
    stopDrag();
    drag = {
      id: chipNode.getAttribute("data-metric"),
      node: chipNode,
      pointerId: event.pointerId,
      x: event.clientX || 0,
      from: event.clientX || 0,
      frame: 0,
      moved: false,
    };
    setFlag(chipNode, "dragging", true);
    window.addEventListener("pointermove", onDragMove);
    window.addEventListener("pointerup", onDragEnd);
    window.addEventListener("pointercancel", onDragEnd);
    if (typeof chipNode.setPointerCapture === "function" && event.pointerId !== undefined) {
      try {
        chipNode.setPointerCapture(event.pointerId);
      } catch {
        // The window listeners already cover a release outside the board.
      }
    }
  }

  function stopDrag() {
    if (!drag) {
      return false;
    }
    const moved = drag.moved;
    const node = drag.node;
    if (drag.frame) {
      if (typeof cancelAnimationFrame === "function") {
        cancelAnimationFrame(drag.frame);
      } else {
        clearTimeout(drag.frame);
      }
    }
    if (typeof node.releasePointerCapture === "function" && typeof node.hasPointerCapture === "function") {
      try {
        if (node.hasPointerCapture(drag.pointerId)) {
          node.releasePointerCapture(drag.pointerId);
        }
      } catch {
        // Already gone with the node.
      }
    }
    setFlag(node, "dragging", false);
    window.removeEventListener("pointermove", onDragMove);
    window.removeEventListener("pointerup", onDragEnd);
    window.removeEventListener("pointercancel", onDragEnd);
    drag = null;
    return moved;
  }

  function onDragMove(event) {
    if (!drag) {
      return;
    }
    if (event.buttons === 0) {
      // Nothing is held down any more; a stray move must not keep the board
      // rearranging itself under the cursor.
      onDragEnd();
      return;
    }
    if (event.clientX !== undefined) {
      drag.x = event.clientX;
    }
    if (drag.frame) {
      return;  // one crossing test per frame, however many moves arrived
    }
    drag.frame = typeof requestAnimationFrame === "function"
      ? requestAnimationFrame(stepDrag)
      : setTimeout(stepDrag, 16);
  }

  function stepDrag() {
    if (!drag) {
      return;
    }
    drag.frame = 0;
    if (Math.abs(drag.x - drag.from) > 3) {
      drag.moved = true;
    }
    const node = document.getElementById(ID);
    const row = node && node === state.node && node.shadowRoot
      ? node.shadowRoot.getElementById("row")
      : null;
    if (!row || !drag.node.isConnected) {
      stopDrag();
      return;
    }
    const siblings = chips(row).filter(
      (chipNode) => chipNode !== drag.node && chipNode.getAttribute("data-metric")
    );
    if (!siblings.length) {
      return;
    }
    let before = null;
    for (const sibling of siblings) {
      const rect = sibling.getBoundingClientRect();
      if (drag.x < rect.left + rect.width / 2) {
        before = sibling;
        break;
      }
    }
    if (before) {
      moveMetric(drag.id, before.getAttribute("data-metric"), true);
      return;
    }
    moveMetric(drag.id, siblings[siblings.length - 1].getAttribute("data-metric"), false);
  }

  function onDragEnd() {
    if (stopDrag()) {
      state.suppressUntil = now() + 500;
    }
    const node = document.getElementById(ID);
    if (node && node === state.node && node.shadowRoot) {
      commitDomOrder(
        [...node.shadowRoot.querySelectorAll("[data-metric]")].map((c) => c.getAttribute("data-metric"))
      );
    }
    render();
  }

  function build() {
    const node = document.createElement("div");
    node.id = ID;
    node.setAttribute("role", "group");
    setAttr(node, "aria-label", locale().aria);
    const root = node.attachShadow({ mode: "open" });
    const style = document.createElement("style");
    style.textContent = STYLE;
    const row = document.createElement("div");
    row.id = "row";
    root.append(style, row);
    bindEvents(root, node);
    state.node = node;
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
    if (existing && existing === state.node && existing.parentElement === parent) {
      state.mountedIn = parent;
      return true;
    }
    if (existing) {
      // Somebody else's row - a closure that outlived its own dispose, or the host
      // re-rendering around a node we no longer own. Two rows with this id in one
      // document is undefined behaviour, and ours is the only one we may paint.
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
      // Named and remembered: a dispose that cannot undo this listener leaves the
      // "disposed" closure alive, and it starts polling again on the next tab
      // switch - which is a second painter on a board that looks single-owner.
      state.visibility = () => {
        if (!document.hidden) {
          schedule(0);
        }
      };
      state.heartbeat = setInterval(tick, 1000);
      // Host re-renders replace the composer subtree; re-anchor on any change.
      state.observer = new MutationObserver(tick);
      state.observer.observe(document.documentElement, {
        childList: true,
        subtree: true,
        attributes: true,
        attributeFilter: ["data-theme", "class", "lang"],
      });
      document.addEventListener("visibilitychange", state.visibility);
    }
    if (mount()) {
      onMounted();
    }
  }

  function tick() {
    const node = document.getElementById(ID);
    if (!node || node !== state.node || node.parentElement !== state.mountedIn) {
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
      `L${state.shedLevel}`,
      board,
      `w${measured}xh${height}`,
      `src=${state.pushed ? "push:" + state.pushedMode : "-"}+${state.host ? "host" : "-"}+${isFinite(burn) ? "sample" : "-"}` +
      `+${(() => { const host = state.host ? hostPayload(state.host) : null; const m = host && monthlyMeter(host); return m ? "m" + m.source : "m-"; })()}`,
    ].join("|");
  }

  /* The editor's action row lives on a wrapped line, so "the user cannot see 取消
   * / 保存 / 添加" has several possible causes - our own clip, an ancestor clip, or
   * host chrome painted over us - and they need different fixes. This reports who
   * actually owns the point at the centre of that row and which ancestors clip. */
  function editorGeometry(row, node) {
    const tools = row.querySelector(".tools");
    if (!tools) {
      return null;
    }
    const box = tools.getBoundingClientRect();
    let owner = null;
    if (typeof document.elementFromPoint === "function") {
      const hit = document.elementFromPoint(Math.round(box.left + box.width / 2), Math.round(box.top + box.height / 2));
      owner = hit === node || (hit && node.contains && node.contains(hit)) ? "ours"
        : hit ? `${hit.nodeName}#${hit.id || ""}.${String(hit.className || "").slice(0, 28)}` : "none";
    }
    const clipped = [];
    for (let ancestor = node.parentElement; ancestor; ancestor = ancestor.parentElement) {
      let style = null;
      try {
        style = getComputedStyle(ancestor);
      } catch {
        break;
      }
      if (style && /hidden|clip|auto|scroll/.test(`${style.overflowY} ${style.overflowX}`)) {
        clipped.push(`${ancestor.nodeName}#${ancestor.id || ""}.${String(ancestor.className || "").slice(0, 28)}` +
          `@${Math.round(ancestor.getBoundingClientRect().bottom)}`);
      }
    }
    return {
      top: Math.round(box.top),
      bottom: Math.round(box.bottom),
      rowBottom: Math.round(row.getBoundingClientRect().bottom),
      hostBottom: Math.round(node.getBoundingClientRect().bottom),
      owner,
      clipped,
    };
  }

  window.__goatProbe = () => ({
    mounted: Boolean(document.getElementById(ID)),
    version: window.__goatWidgetVersion || "",
    host: Boolean(state.host),
    hostError: state.hostError,
    pushed: Boolean(state.pushed),
    layout: (document.getElementById(ID) || {}).dataset ? document.getElementById(ID).dataset.layout : "",
    editing: layout.editing,
    fitWidth: state.fitWidth,
    editor: (() => {
      const node = document.getElementById(ID);
      const row = node && node.shadowRoot ? node.shadowRoot.getElementById("row") : null;
      return row ? editorGeometry(row, node) : null;
    })(),
    metrics: currentLayout(),
    // Single-writer self-proof: does the node answering to the id belong to the
    // closure being asked? A false here is the double-draw that eats metrics.
    ownsNode: document.getElementById(ID) === state.node,
    dragging: Boolean(drag),
    reorders: state.reorders,
    dark: (document.getElementById(ID) || {}).dataset ? document.getElementById(ID).dataset.dark : "",
    fingerprint: fingerprint(),
    session: state.threadId ? "known" : "none",
    statsAt: state.statsAt,
    shapeChanges: state.shapeChanges,
    shedLevel: state.shedLevel,
    shedChanges: state.shedChanges,
    levelChanges: state.levelChanges,
    boardChanges: state.boardChanges,
    boardLog: state.boardLog,
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
    stopDrag();
    if (state.timer) clearTimeout(state.timer);
    if (state.heartbeat) clearInterval(state.heartbeat);
    if (state.observer) state.observer.disconnect();
    if (state.resizer) state.resizer.disconnect();
    if (state.visibility && typeof document.removeEventListener === "function") {
      document.removeEventListener("visibilitychange", state.visibility);
    }
    window.removeEventListener("resize", refit);
    const node = state.node || document.getElementById(ID);
    if (node) node.remove();
    state.timer = state.heartbeat = state.observer = state.visibility = null;
    // Ownership ends here as well: a disposed closure that still holds a node can
    // answer to the id and start painting over its replacement.
    state.node = null;
    state.slots = [];
    window.__goatWidgetVersion = "";
    return { ok: true };
  };

  window.__goatWidgetVersion = VERSION;
  loadLayout();
  start();
})();
