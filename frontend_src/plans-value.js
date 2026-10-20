/* Plans & Value comparison view. Backend owns arithmetic; this module formats and owns request lifetime. */
(() => {
  const PERIODS = [{key: "this_month", label: "This month"}, {key: "last_month", label: "Last month"}, {key: "this_cycle", label: "This cycle"}];
  const periodIndex = key => Math.max(0, PERIODS.findIndex(item => item.key === key));
  const esc = s => String(s ?? "").replace(/[&<>'"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[c]));
  const finite = v => (v == null || v === "" || !Number.isFinite(+v)) ? null : +v;
  const formatUsd = v => finite(v) == null ? null : "$" + (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
  const formatMultiple = v => finite(v) == null ? null : (+v).toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2}) + "×";
  const formatShortDate = d => !/^\d{4}-\d{2}-\d{2}$/.test(d || "") ? (d || "") : new Intl.DateTimeFormat("en-US", {month: "short", day: "numeric", year: "numeric", timeZone: "UTC"}).format(new Date(d + "T12:00:00Z"));
  const periodCaption = p => (p?.period === "this_cycle" ? (p.periodLabel || "Each plan's current quota window") : null) || p?.periodLabel || ((p?.localFrom || p?.localStartDate) && (p?.localTo || p?.localEndDate) ? `${formatShortDate(p.localFrom || p.localStartDate)} – ${formatShortDate(p.localTo || p.localEndDate)} (${p.timezone || ""})` : (p?.timezone ? `(${p.timezone})` : ""));

  const usageLabel = g => {
    const st = g?.usageValueStatus || g?.usageStatus;
    if (st === "no_records") return "No recorded usage";
    const m = formatUsd(g?.usageValueUsd);
    if (m == null) return st === "measured_zero" ? "$0.00" : "Unavailable";
    return (g?.usageBound === "lower_bound" || st === "lower_bound" || st === "partial") ? "≥ " + m : m;
  };

  const multipleLabel = g => {
    const m = g?.multiple ?? g?.referenceMultiple;
    const f = formatMultiple(m);
    if (f != null) return ((g?.multipleBasis) === "lower_bound") ? "≥ " + f : f;
    return g?.multipleReason || "Unavailable";
  };

  const costLabel = g => formatUsd(g?.configuredCostUsd ?? g?.accruedCostUsd) || "$0.00";

  const coverageLabel = g => {
    const c = g?.pricingCoverage || {};
    if (c.label) return String(c.label);
    const st = c.status || "none", ev = c.eventCount, un = c.unpricedModels || [];
    if (st === "complete") return `Complete (${ev ?? 0} events)`;
    if (st === "partial") return `Partial (${un.length === 1 ? "1 unpriced model" : (un.length || 0) + " unpriced models"})`;
    return st === "none" ? "No recorded usage" : "Unavailable";
  };

  const collectionLabel = g => g?.collectionEvidence?.label || "Collection status unavailable";

  const attributionLabel = g => {
    const a = g?.attribution || {};
    if (a.label) return String(a.label);
    const st = a.status || "configured";
    return st === "shared" ? "Shared tool association" : st === "unsupported" ? "Unsupported custom association" : st === "unassigned" ? "Unassigned" : "Configured tool association";
  };

  const toolBadge = g => String(g?.toolLabel || (g?.toolKeys || []).join(" / ") || "tool");
  // Provider marks come from the shared sprite (/logos.svg); only names live here.
  const MARKS = {
    codex: "Codex", "claude-code": "Claude", cursor: "Cursor", grok: "Grok", zai: "Z.AI", opencode: "OpenCode",
    antigravity: "Antigravity", muse: "Muse Code", openrouter: "OpenRouter", xai: "xAI", traycer: "Traycer",
    openai: "OpenAI", anthropic: "Anthropic", custom: "Custom"
  };
  const SPRITE_IDS = {"claude-code": "claude", muse: "meta"};
  const markKey = tool => tool === "zcode" ? "zai" : tool;
  const toolsOf = g => {
    const keys = (g?.toolKeys || []).map(key => markKey(String(key)));
    const list = (keys.length ? keys : ["custom"]).filter((key, index, all) => all.indexOf(key) === index);
    return list.slice(0, 2);
  };
  const spriteUrl = () => (typeof document !== "undefined" && document.querySelector?.('meta[name="burnrate-logos"]')?.content) || "/logos.svg";
  const svgMark = id => {
    const node = typeof document.createElementNS === "function"
      ? document.createElementNS("http://www.w3.org/2000/svg", "svg")
      : document.createElement("svg");
    node.setAttribute("viewBox", "0 0 24 24");
    node.setAttribute("aria-hidden", "true");
    node.innerHTML = '<use href="' + spriteUrl() + "#" + id + '"></use>';
    return node;
  };
  const logo = tool => {
    const key = markKey(tool);
    const id = MARKS[key] ? key : "custom";
    const wrap = h("span", {className: "plans-value-logo", "data-tool": id, title: MARKS[id]});
    wrap.append(svgMark(SPRITE_IDS[id] || id));
    return wrap;
  };
  const includedPlans = g => (g?.plans || []).map(p => p.name || ("Plan " + (p.id ?? ""))).join(", ");

  const sanitizeExplanationText = v => String(v ?? "")
    .replace(/[A-Za-z]:\\[^\s]+/g, "[path omitted]")
    .replace(/\/(?:Users|home|var|etc|tmp)\/[^\s]+/g, "[path omitted]")
    .replace(/[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi, "[redacted]")
    .replace(/\b(sk-|Bearer\s+)[A-Za-z0-9._-]{8,}/gi, "[redacted]");

  const explanationSections = (g, p) => {
    const ex = g?.explanation || {}, ref = ex.referenceValue || {};
    const join = (...values) => values.flat().filter(v => (typeof v === "string" && v) || typeof v === "number").map(sanitizeExplanationText).join(". ");
    const date = value => {
      const parsed = new Date(value || "");
      if (!Number.isFinite(parsed.getTime())) return "Unavailable";
      return new Intl.DateTimeFormat("en-US", {dateStyle:"medium",timeStyle:"short",timeZone:p?.timezone || "UTC"}).format(parsed);
    };
    const collection = g?.collectionEvidence || {};
    const sources = (collection.sources || []).slice(0,12).map(s => join(
      s.sourceLabel || s.source, s.status, s.configuredState,
      `Last attempt: ${date(s.lastAttemptAt)}`, `Last success: ${date(s.lastSuccessAt)}`,
      `Freshness: ${s.freshness?.state || "unknown"}`, s.reason, s.coverage
    ));
    const exclusions = (ref.excluded || []).slice(0,8).map(item => `Excluded: ${typeof item?.detail === "string" ? item.detail : "Reason unavailable"}`);
    return [
      ["Period", join(periodCaption(p), (ex.period?.activeIntervals || []).map(i => `${i.startUtc} – ${i.endUtc}`), ex.period?.note)],
      ["Configured cost", join(includedPlans(g), ex.configuredCost?.formula, ex.configuredCost?.note)],
      ["Tools & association", join(toolBadge(g), ex.tools?.label, ex.tools?.note)],
      ["Reference value", join(ref.note, `${ref.pricedEvents ?? "Unavailable"} priced events; ${ref.unpricedEvents ?? "Unavailable"} unpriced events`, (ref.unpricedModels || []).map(m => typeof m === "string" ? m : `${m.modelKey || "Unknown model"}: ${m.tokens ?? "Unavailable"} tokens`), exclusions)],
      ["Collection limits", join(collectionLabel(g), sources, collection.note, ex.limits?.pricingVsCollection)],
      ["Reference-value multiple", join(ex.multiple?.formula || multipleLabel(g), ex.multiple?.note)]
    ];
  };

  const h = (tag, a, ...c) => {
    const n = document.createElement(tag);
    if (a) for (const [k, v] of Object.entries(a)) {
      if (v == null || v === false) continue;
      if (k === "className") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on") && typeof v === "function") n.addEventListener(k.slice(2).toLowerCase(), v);
      else if (k === "dataset") Object.assign(n.dataset, v);
      else n.setAttribute(k, v === true ? "" : String(v));
    }
    for (const ch of c.flat()) if (ch != null && ch !== false) n.append(typeof ch === "string" ? document.createTextNode(ch) : ch);
    return n;
  };

  const btn = (cls, lbl, act, txt) => h("button", {type: "button", className: cls, "aria-label": lbl, onClick: act}, txt);

  function renderEmpty(cnt, p, ctx) {
    const r = p?.emptyReason || p?.empty_reason;
    const msg = r === "no_plans" ? "No plans are configured for this period." : r === "no_usage" ? "No recorded usage for this period." : "No plan comparisons are available for this period.";
    cnt.append(h("div", {className: "plans-value-empty", role: "status"}, h("p", {text: msg}), btn("settings-link", "Manage plans", () => ctx.openPlanManager?.(), "Manage plans")));
  }

  function renderWhy(g, p) {
    const inner = h("div", {className: "plans-value-why-inner"});
    for (const [title, text] of explanationSections(g, p)) {
      inner.append(h("section", {className: "plans-value-why-section"}, h("h4", {text: title}), h("p", {text})));
    }
    return h("details", {className: "plans-value-why"},
      h("summary", {"aria-label": `Why this number for ${g?.name || "plan"}?`}, "Why", h("span", {className: "plans-value-why-chevron", "aria-hidden": "true"})),
      h("div", {className: "plans-value-why-body"}, inner)
    );
  }

  function renderRow(g, p, ctx) {
    const name = g?.name || "Plan group", pl = includedPlans(g);
    const cycleOff = p?.period === "this_cycle" && g?.cycleAvailable === false;
    const cov = g?.pricingCoverage || {}, ev = g?.collectionEvidence || {};
    const covCls = cov.status === "complete" ? "badge-complete" : cov.status === "partial" ? "badge-partial" : "badge-warn";
    const evCls = ev.status === "healthy" ? "badge-complete" : "badge-warn";
    const note = cycleOff ? (g.cycleReason || "Cycle comparison is unavailable for this plan.") : (g?.cycleLabel || "");
    const showIncluded = pl && pl !== name;
    const metric = (l, cls, txt) => {
      const figure = /^[$≥]/.test(txt) || /×$/.test(txt);
      return h("div", {className: "plans-value-metric"},
        h("strong", {className: cls + " mono" + (figure ? " is-figure" : ""), title: txt, text: txt}),
        h("span", {className: "plans-value-metric-label", text: l})
      );
    };
    const attribution = attributionLabel(g);
    const showAttribution = attribution !== "Configured tool association";
    const tools = toolsOf(g);
    const logos = h("div", {className: tools.length > 1 ? "plans-value-logos is-pair" : "plans-value-logos"});
    for (const tool of tools) logos.append(logo(tool));
    return h("article", {className: "plans-value-row", "data-group-id": g?.groupId || "", "data-tool": tools[0], "aria-label": `Comparison for ${name}`},
      h("div", {className: "plans-value-card"},
        logos,
        h("div", {className: "plans-value-identity"},
          h("strong", {className: "plans-value-name", text: name}),
          h("span", {className: "plans-value-tool-badge", text: toolBadge(g)}),
          showIncluded ? h("p", {className: "plans-value-included", text: "Includes: " + pl}) : null,
          note ? h("p", {className: "plans-value-included", text: note}) : null
        ),
        h("div", {className: "plans-value-metrics"},
          metric("Cost", "plans-value-cost", cycleOff ? "Unavailable" : costLabel(g)),
          metric("Usage", "plans-value-usage", cycleOff ? "Unavailable" : usageLabel(g)),
          metric("Multiple", "plans-value-multiple", cycleOff ? "Unavailable" : multipleLabel(g))
        )
      ),
      h("div", {className: "plans-value-meta"},
        h("div", {className: "plans-value-badges", "aria-label": "Coverage and attribution"},
          h("span", {className: "plans-value-badge " + covCls, "data-kind": "pricing", title: coverageLabel(g), text: coverageLabel(g)}),
          h("span", {className: "plans-value-badge " + evCls, "data-kind": "collection", title: collectionLabel(g), text: collectionLabel(g)}),
          showAttribution ? h("span", {className: "plans-value-badge badge-complete", "data-kind": "attribution", title: attribution, text: attribution}) : null
        ),
        h("div", {className: "plans-value-row-actions"},
          btn("settings-link", `Manage plans for ${name}`, () => ctx.openPlanManager?.(g), "Plans"),
          btn("ghost-button", `Check connection for ${name}`, event => ctx.openHarnesses?.(g, event.currentTarget), "Connect")
        )
      ),
      renderWhy(g, p)
    );
  }

  function renderUnassigned(un, ctx) {
    if (!un) return null;
    return h("section", {className: "plans-value-unassigned", "aria-label": "Unassigned usage"},
      h("div", {className: "plans-value-unassigned-head"},
        h("strong", {text: "Unassigned / no configured plan"}),
        h("span", {className: "plans-value-usage mono", text: usageLabel(un)}),
        btn("settings-link", "Open plan manager for unassigned usage", () => ctx.openPlanManager?.(), "Plans")
      ),
      h("p", {className: "plans-value-unassigned-note", text: sanitizeExplanationText(un.explanation?.note || (typeof un.explanation === "string" ? un.explanation : "Usage without an applicable configured plan or safe tool association."))})
    );
  }

  const hasClass = (node, name) => String(node?.className || "").split(/\s+/).includes(name);
  function findClass(root, name) {
    const stack = [...(root?.children || [])];
    while (stack.length) {
      const node = stack.shift();
      if (hasClass(node, name)) return node;
      if (node?.children?.length) stack.push(...node.children);
    }
    return null;
  }

  function playShift(node, direction) {
    if (!node?.setAttribute) return;
    node.setAttribute("data-shift", direction || "in");
    const list = node.classList;
    if (!list || typeof list.toggle !== "function") return;
    list.toggle("is-entering", false);
    if (typeof node.getBoundingClientRect === "function") node.getBoundingClientRect();
    list.toggle("is-entering", true);
  }

  function ensureFrame(cnt, period, ctx, caption) {
    let frame = findClass(cnt, "plans-value");
    if (!frame) {
      frame = h("section", {className: "plans-value", "aria-label": "Plans and Value"},
        h("div", {className: "plans-value-toolbar"},
          h("div", {className: "cadence-control plans-value-period", role: "group", "aria-label": "Comparison period"},
            ...PERIODS.map(it => h("button", {
              type: "button", "data-period": it.key, "aria-pressed": "false", "aria-label": it.label,
              onClick: () => ctx.onPeriodChange?.(it.key)
            }, it.label))
          ),
          h("p", {className: "plans-value-dates", "aria-label": "Selected period dates"})
        ),
        h("div", {className: "plans-value-stage"}),
        h("div", {className: "plans-value-footer"},
          btn("settings-link", "Open plan manager", () => ctx.openPlanManager?.(), "Plans"),
          btn("ghost-button", "Open connection diagnostics", event => ctx.openHarnesses?.(null, event.currentTarget), "Connections")
        )
      );
      cnt.replaceChildren(frame);
    }
    const buttons = [];
    const pending = [...(frame.children || [])];
    while (pending.length) {
      const node = pending.shift();
      if (node?.getAttribute?.("data-period")) buttons.push(node);
      if (node?.children?.length) pending.push(...node.children);
    }
    for (const button of buttons) {
      const on = button.getAttribute("data-period") === period;
      button.setAttribute("aria-pressed", String(on));
      button.classList?.toggle?.("active", on);
    }
    const dates = findClass(frame, "plans-value-dates");
    if (dates) dates.textContent = caption || "";
    return frame;
  }

  function skelCard() {
    return h("article", {className: "plans-value-skel", "aria-hidden": "true"},
      h("span", {className: "plans-value-skel-mark"}),
      h("span", {className: "plans-value-skel-copy"}, h("i"), h("i", {className: "is-short"})),
      h("span", {className: "plans-value-skel-metrics"}, h("i"), h("i"), h("i"))
    );
  }

  function renderLoading(cnt, period, ctx = {}) {
    const label = period === "last_month" ? "last month" : period === "this_cycle" ? "this cycle" : "this month";
    const frame = ensureFrame(cnt, period, ctx, "");
    const stage = findClass(frame, "plans-value-stage");
    stage.replaceChildren(h("div", {className: "plans-value-loading", role: "status", "aria-label": "Loading Plans and Value"},
      h("p", {text: `Loading ${label}…`}),
      skelCard(), skelCard(), skelCard()
    ));
  }

  function renderError(cnt, err, onRetry, period, ctx = {}) {
    const frame = ensureFrame(cnt, period, ctx, "");
    findClass(frame, "plans-value-stage").replaceChildren(h("div", {className: "plans-value-error", role: "alert"}, h("p", {text: err?.message || "Plans & Value could not load."}), btn("ghost-button", "Retry loading Plans and Value", onRetry, "Retry")));
  }

  function renderContent(cnt, p, ctx, period, shift) {
    const frame = ensureFrame(cnt, period, ctx, periodCaption(p || {timezone: ctx.timezone}));
    const stage = findClass(frame, "plans-value-stage");
    const groups = p?.groups || [];
    stage.replaceChildren();
    if (!groups.length && !p?.unassigned) renderEmpty(stage, p || {emptyReason: "no_plans"}, ctx);
    else {
      for (const g of groups) stage.append(renderRow(g, p, ctx));
      const un = renderUnassigned(p?.unassigned, ctx);
      if (un) stage.append(un);
    }
    playShift(stage, shift);
    playShift(findClass(frame, "plans-value-dates"), shift);
  }

  function defaultFetchPeriod(period, {signal} = {}) {
    return fetch("/api/subscriptions/value?period=" + encodeURIComponent(period), {cache: "no-store", signal}).then(async res => {
      const d = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(d.error || "Plans & Value request failed.");
      return d;
    });
  }

  function createPlansValueController(container, context = {}) {
    let generation = 0, period = context.initialPeriod || "this_month", payload = null, abort = null, closed = false, shift = "in";
    const fetchPeriod = context.fetchPeriod || defaultFetchPeriod;
    const viewContext = {...context, onPeriodChange(next) { load(next); }};
    function owns(token) { return !closed && token === generation; }
    function paint() { if (!closed) renderContent(container, payload, viewContext, period, shift); shift = "in"; }

    async function load(nextPeriod = period) {
      const token = ++generation;
      if (nextPeriod !== period) shift = periodIndex(nextPeriod) >= periodIndex(period) ? "next" : "prev";
      period = nextPeriod;
      abort?.abort();
      abort = typeof AbortController !== "undefined" ? new AbortController() : null;
      payload = null;
      renderLoading(container, period, viewContext);
      try {
        const data = await fetchPeriod(period, {signal: abort?.signal});
        if (!owns(token)) return null;
        payload = data;
        period = data.period || period;
        paint();
        return data;
      } catch (err) {
        if (!owns(token) || err?.name === "AbortError") return null;
        payload = null;
        renderError(container, err, () => load(period), period, viewContext);
        return null;
      }
    }

    function apply(data) {
      const token = ++generation;
      abort?.abort();
      if (!owns(token)) return;
      payload = data;
      period = data?.period || period;
      paint();
    }

    return {
      load, apply, close() { closed = true; ++generation; abort?.abort(); }, reopen() { closed = false; }, paint,
      getPeriod: () => period, getPayload: () => payload, getGeneration: () => generation, isClosed: () => closed
    };
  }

  function renderPlansValue(container, payload, context = {}) {
    const ctrl = createPlansValueController(container, context);
    if (payload) ctrl.apply(payload);
    else ctrl.load();
    return ctrl;
  }

  if (typeof module !== "undefined" && module.exports) {
   const api = {
    renderPlansValue, createPlansValueController, renderContent, renderLoading, renderError,
    formatUsd, formatMultiple, formatShortDate, periodCaption, esc, multipleLabel, usageLabel, costLabel, coverageLabel, collectionLabel, attributionLabel,
    sanitizeExplanationText, PERIODS
  };
   module.exports = api;
   if (typeof window !== "undefined") window.PlansValue = window.plansValueApi = api;
  }
  if (typeof window !== "undefined") {
    window.renderPlansValue = renderPlansValue;
    window.createPlansValueController = createPlansValueController;
    window.BurnrateMarks = { mark: logo };
  }
})();
