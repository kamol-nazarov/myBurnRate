const WINDOWS = [
  {key:"15m",label:"15m"},{key:"30m",label:"30m"},{key:"1h",label:"1h"},{key:"3h",label:"3h"},
  {key:"6h",label:"6h"},{key:"12h",label:"12h"},{key:"1d",label:"1d"},{key:"1w",label:"1w"},
  {key:"1mo",label:"1mo"},{key:"mtd",label:"MTD"},{key:"ytd",label:"YTD"},{key:"all",label:"All"}
];
const WINDOW_KEYS = new Set(WINDOWS.map(item => item.key));
const SHORT_WINDOWS = new Set(["15m","30m","1h","3h","6h","12h","1d"]);
const MIX_COLORS = {cached_input:"var(--mix-cached)",fresh_input:"var(--mix-fresh)",output:"var(--mix-output)",cache_write:"var(--mix-write)"};
const MIX_LABELS = {cached_input:"Cached input",fresh_input:"Fresh input",output:"Output",cache_write:"Cache write"};
const DAYS = ["Mon","Tue","Wed","Thu","Fri","Sat","Sun"];
// Categorical tool colors resolve per theme (graphite or paper) in spend.css.
const TOOL_COLORS = {
  codex:"var(--t-codex)",claude:"var(--t-claude)","claude-code":"var(--t-claude)",cursor:"var(--t-cursor)",
  opencode:"var(--t-opencode)",zai:"var(--t-opencode)",zcode:"var(--t-zcode)",grok:"var(--t-grok)",supergrok:"var(--t-grok)","grok-build":"var(--t-grok)",
  openrouter:"var(--t-openrouter)",xai:"var(--t-grok)",antigravity:"var(--t-antigravity)",muse:"var(--t-muse)"
};
const EASE_RATE = 0.13;
const REDUCE_MOTION = typeof matchMedia === "function" && matchMedia("(prefers-reduced-motion: reduce)").matches;
const POLL_MS = 15000;
const HEADROOM = 8;
const PLOT = 92;
const PROBE = new URLSearchParams(location.search).get("probe") === "1";
// Last successful payload per window, kept in this origin's localStorage so a
// repeat visit paints real numbers before the first fetch returns. Bump the
// version whenever the payload contract changes; older keys are pruned on boot.
const SNAPSHOT_PREFIX = "burnrate:snapshot:v1:";
const SNAPSHOT_MAX_AGE_MS = 15 * 1000;

const params = new URLSearchParams(location.search);
const initialWindow = String(params.get("window") || "1d").toLowerCase();
const state = {
  window: WINDOW_KEYS.has(initialWindow) ? initialWindow : "1d",
  machine: String(params.get("machine") || "all").trim() || "all",
  tool: String(params.get("tool") || "all").trim() || "all",
  knownTools: [],
  mode:"stacked",
  hover:null,
  pinned:null,
  view:"overview",
  entity:null,
  summary:null,
  entityData:null,
  entityDataIdentity:null,
  health:null,
  navigation:null,
  sortKey:"value",
  sortDir:-1,
  mixOpen:true,
  subsOpen:false,
  heatCell:null,
  heatFocus:0,
  displayTz:null,
  dHover:null,
  dPinned:null,
  request:0,
  disp:Object.create(null),
  targets:Object.create(null),
  chartScales:Object.create(null),
  detailScales:Object.create(null),
  easeNodes:null,
  easeFrames:0,
  probeBar:null,
  probeBarStable:null
};

if (PROBE) document.documentElement.classList.add("frozen");
// Paint timing for the probe and for the bench: largest contentful paint is
// only available through an observer, and the last refresh render duration
// is measured around the in-place render.
const timing = {lcp: 0, lastRenderMs: null};
try {
  new PerformanceObserver(list => {
    list.getEntries().forEach(entry => { timing.lcp = entry.startTime; });
  }).observe({type: "largest-contentful-paint", buffered: true});
} catch {}

const $ = id => document.getElementById(id);
const esc = value => String(value ?? "").replace(/[&<>'"]/g, ch => ({"&":"&amp;","<":"&lt;",">":"&gt;","'":"&#39;",'"':"&quot;"}[ch]));
// Extra Claude account lanes (claude-code-<name>) wear Claude's color and mark.
const colorFor = key => TOOL_COLORS[key] || (String(key || "").startsWith("claude-code-") ? TOOL_COLORS.claude : "var(--t-other)");
window.colorFor = colorFor;
// Provider marks live in one cached sprite (/logos.svg) referenced with <use>.
const LOGO_URL = document.querySelector('meta[name="burnrate-logos"]')?.content || "/logos.svg";
const TOOL_LOGOS = {
  codex:"codex","claude-code":"claude",claude:"claude",cursor:"cursor",opencode:"opencode",zai:"zai",zcode:"zai",
  grok:"grok",supergrok:"grok","grok-build":"grok",xai:"xai",antigravity:"antigravity",muse:"meta",openrouter:"openrouter",
  traycer:"traycer",anthropic:"anthropic",openai:"openai",gemini:"gemini"
};
const LOGO_HINTS = [
  [/claude|anthropic|opus|sonnet|haiku/i, "claude"], [/codex|gpt|chatgpt/i, "codex"], [/grok|xai/i, "grok"],
  [/glm|z\.ai|zhipu/i, "zai"], [/muse|meta/i, "meta"], [/antigravity/i, "antigravity"], [/gemini/i, "gemini"],
  [/cursor/i, "cursor"], [/openrouter/i, "openrouter"], [/traycer/i, "traycer"], [/opencode/i, "opencode"]
];
// A name hint wins when the key is generic ("custom") or when the caller prefers it.
function logoKey(key, hint, preferHint) {
  const hinted = LOGO_HINTS.find(([pattern]) => pattern.test(String(hint || "")))?.[1];
  const direct = TOOL_LOGOS[String(key || "").toLowerCase()];
  return (preferHint && hinted) || direct || hinted || "custom";
}
function logoSvg(id) {
  return `<svg class="logo" viewBox="0 0 24 24" aria-hidden="true"><use href="${esc(LOGO_URL)}#${esc(id)}"></use></svg>`;
}
function setLogo(slot, id) {
  if (!slot || slot.dataset.logo === id) return;
  slot.dataset.logo = id;
  slot.querySelector(":scope > svg.logo")?.remove();
  slot.insertAdjacentHTML("afterbegin", logoSvg(id));
}
window.openHarnessUsage = key => loadEntity("tool", key);
const finite = value => {
  if (value == null || value === "") return null;
  const n = Number(value);
  return Number.isFinite(n) ? n : null;
};
const finiteDate = value => {
  if (!value) return null;
  const ms = new Date(value).getTime();
  return Number.isFinite(ms) ? ms : null;
};
const RESET_ALERTS_SEEN = "burnrate:reset-alerts:seen";
const unknown = "—";
const mixItem = (rows, key) => (rows || []).find(item => item.key === key);

function usd(value) {
  const n = finite(value);
  if (n == null) return unknown;
  return n >= 1000 ? "$" + Math.round(n).toLocaleString("en-US") : "$" + n.toFixed(2);
}
function markedUsd(value, exact) {
  const text = usd(value);
  if (text === unknown) return unknown;
  return exact === false ? "≈" + text : text;
}
function tokens(value) {
  const n = finite(value);
  if (n == null) return unknown;
  if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
  if (n >= 1e6) return (n / 1e6).toFixed(1) + "M";
  if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
  return String(Math.round(n));
}
function pct(value) {
  const n = finite(value);
  return n == null ? unknown : n.toFixed(1) + "%";
}
function number(value) {
  const n = finite(value);
  return n == null ? unknown : Math.round(n).toLocaleString("en-US");
}
function niceMax(value) {
  // Four grid steps from a 1-2-2.5-5 ladder, so every tick label is a
  // round figure (8M / 6M / 4M / 2M) rather than fractions of the peak.
  const v = Math.max(value || 0, 0.01);
  const mag = 10 ** Math.floor(Math.log10(v / 4));
  const step = [1, 2, 2.5, 5, 10].map(m => m * mag).find(candidate => candidate * 4 >= v);
  return step * 4;
}
function tickLabel(value) {
  return tokens(value).replace(/\.0([KMB])$/, "$1");
}
function gridTop(fr) {
  return HEADROOM + (1 - fr) * PLOT;
}
function quotaColor(value) {
  const n = finite(value);
  if (n == null) return "var(--dim)";
  if (n >= 85) return "var(--danger)";
  if (n >= 60) return "var(--accent)";
  return "var(--meter)";
}
function formatUnit(value, unit) {
  const n = finite(value);
  if (n == null) return unknown;
  const label = unit || "";
  if (label === "usd" || label === "$") return usd(n);
  return number(n) + (label ? " " + label : "");
}
function formatReset(iso) {
  if (!iso) return "";
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return "";
  // Labels render in the payload's configured zone, not the browser's (C13).
  return date.toLocaleString([], {timeZone: state.displayTz || undefined, month:"short", day:"numeric", hour:"numeric", minute:"2-digit"});
}
function etaFromReset(iso) {
  if (!iso) return {value:unknown, label:"to reset"};
  const ms = new Date(iso).getTime() - Date.now();
  if (!Number.isFinite(ms)) return {value:unknown, label:"to reset"};
  if (ms <= 0) return {value:"now", label:"to reset"};
  const hours = ms / 3600000;
  if (hours < 1) return {value:Math.max(1, Math.round(ms / 60000)) + "m", label:"to reset"};
  if (hours < 48) return {value:Math.round(hours) + "h", label:"to reset"};
  return {value:Math.round(hours / 24) + "d", label:"to reset"};
}
function quotaElapsedPct(row) {
  if (!row || !row.resetsAt || row.isPayg) return null;
  const resetMs = Date.parse(row.resetsAt);
  if (!Number.isFinite(resetMs)) return null;
  const remMs = resetMs - Date.now();
  if (remMs <= 0) return null;
  const label = String((row.limitKey || "") + " " + (row.label || "")).toLowerCase();
  const durMs = label.includes("5h") || label.includes("5-hour") || label.includes("five-hour")
    ? 5 * 3600000
    : label.includes("week") || label.includes("7d")
      ? 7 * 86400000
      : label.includes("day") || label.includes("24h") || label.includes("daily")
        ? 86400000
        : 0;
  if (!durMs || remMs > durMs) return null;
  return Math.max(0, Math.min(100, 100 - (remMs / durMs) * 100));
}
function quotaFreshnessAge(iso) {
  const ms = finiteDate(iso);
  if (ms == null) return "";
  const ageSec = Math.max(0, Math.round((Date.now() - ms) / 1000));
  if (ageSec < 45) return "just now";
  if (ageSec < 3600) return Math.round(ageSec / 60) + "m ago";
  if (ageSec < 86400) return Math.round(ageSec / 3600) + "h ago";
  return Math.round(ageSec / 86400) + "d ago";
}
function windowLabel(key) {
  return (WINDOWS.find(item => item.key === key) || {}).label || key;
}
function isLiveRow(row) {
  const flag = String(row?.state || "").toLowerCase().replace(/\s+/g, "_");
  return flag === "live" || flag === "running";
}
function isCacheOpportunity(opp) {
  const kind = String(opp?.kind || "").toLowerCase();
  const title = String(opp?.title || "").toLowerCase();
  return kind.includes("cache") || title.includes("cache");
}
function heatmapIsFallback(data) {
  if (data.heatmapFallback != null) return Boolean(data.heatmapFallback);
  if (data.heatmapTrailing != null) return Boolean(data.heatmapTrailing);
  return SHORT_WINDOWS.has(String(data.window?.key || state.window).toLowerCase());
}

function ease(key, target) {
  const n = finite(target);
  if (n == null) {
    delete state.targets[key];
    delete state.disp[key];
    return null;
  }
  state.targets[key] = n;
  if (state.disp[key] !== n) scheduleEase();
  return state.disp[key] === undefined ? n : state.disp[key];
}
// The easing loop is demand-driven: a frame is requested only while some
// displayed value still differs from its target, and the loop stops as soon
// as everything has settled, so an idle dashboard does no per-frame work.
let easeFrame = null;
function scheduleEase() {
  if (typeof REDUCE_MOTION !== "undefined" && REDUCE_MOTION) {
    for (const key of Object.keys(state.targets)) state.disp[key] = state.targets[key];
    paintEase();
    return;
  }
  if (easeFrame == null) easeFrame = requestAnimationFrame(tickEase);
}
function easeNodes() {
  // Bound elements only change when a render creates, binds or removes
  // nodes; each of those invalidates this registry instead of paying a
  // document-wide query on every animated frame.
  if (!state.easeNodes) state.easeNodes = [...document.querySelectorAll("[data-ease]")];
  return state.easeNodes;
}
function invalidateEaseNodes() {
  state.easeNodes = null;
}
function live(key) {
  return key in state.targets ? state.disp[key] : undefined;
}
function formatDisp(fmt, value, el) {
  if (finite(value) == null) return unknown;
  if (fmt === "usd") return usd(value);
  if (fmt === "usdDay") return usd(value) + "/day";
  if (fmt === "approxUsd") return "≈" + usd(value);
  if (fmt === "approxUsdDay") return "≈" + usd(value) + "/day";
  if (fmt === "tokens") return tokens(value);
  if (fmt === "pct") return pct(value);
  if (fmt === "number") return number(value);
  if (fmt === "peak") return "peak " + pct(value);
  if (fmt === "unit") return formatUnit(value, el?.dataset.unit);
  return String(value);
}
function paintEase() {
  easeNodes().forEach(el => {
    const key = el.dataset.ease;
    if (!key || !(key in state.targets)) return;
    const value = state.disp[key];
    if (value === undefined) return;
    const fmt = el.dataset.fmt || "text";
    if (fmt === "width") {
      el.style.width = Math.max(0, Math.min(100, value)).toFixed(2) + "%";
      if (el.dataset.quotaColor === "1" && !el.closest(".capacity-lane")) {
        const color = quotaColor(value);
        el.style.background = color;
        const label = el.closest(".capacity-row, .capacity-provider")?.querySelector("[data-ease='" + key + "'][data-fmt='pct'], [data-ease='" + key + "'][data-fmt='peak']");
        if (label) label.style.color = color;
      }
      return;
    }
    el.textContent = formatDisp(fmt, value, el);
  });
  paintComposite();
}
function paintComposite() {
  const tracked = live("tracked");
  const priced = live("priced");
  const published = live("published");
  const records = live("records");
  const effectiveRate = live("effectiveRate");
  const cachedTok = live("cachedTok");
  const inputTok = live("inputTok");
  const meanSession = live("meanSession");
  const wasteDay = live("wasteDay");
  const wasteMonth = live("wasteMonth");
  const projected = live("projected");
  const planCost = live("planCost");
  const tokenTotal = live("tokens");
  const eShare = live("eShare");
  const eVal = live("eVal");
  const hoverTotal = live("hoverTotal");
  const trackedNote = $("tracked-note");
  // renderKpis owns the note while the window is PARTIAL (it names the
  // unpriced models); the easing loop only refreshes the plain form.
  if (trackedNote && tracked != null && !trackedNote.dataset.partial) trackedNote.textContent = `${usd(priced)} priced + ${usd(published)} published-rate`;
  const tokensNote = $("tokens-note");
  if (tokensNote && records != null) {
    const outputTok = mixItem(state.summary?.mix, "output")?.tokens ?? state.summary?.totals?.outputTokens;
    tokensNote.textContent = `${number(records)} records · ${tokens(outputTok)} output`;
  }
  if ($("effective-rate") && effectiveRate != null) $("effective-rate").textContent = usd(effectiveRate);
  const cacheNote = $("cache-note");
  if (cacheNote && cachedTok != null && inputTok != null) cacheNote.textContent = `${tokens(cachedTok)} cached of ${tokens(inputTok)} input`;
  const sessionsNote = $("sessions-note");
  if (sessionsNote && meanSession != null) sessionsNote.textContent = `≈${usd(meanSession)} average tracked value`;
  const wasteNote = $("waste-headline-note");
  if (wasteNote && "wasteDay" in state.targets) wasteNote.textContent = wasteDay == null ? "unavailable" : `per day · ${usd(wasteMonth)}/mo`;
  if ($("pace-bar") && projected != null) {
    const denom = (projected || 0) + (planCost || 0);
    $("pace-bar").style.width = denom ? `${(projected / denom) * 100}%` : "0%";
  }
  const forecastBasis = state.summary?.projected?.multipleBasis;
  if ($("projected-marker")) $("projected-marker").textContent = projected == null ? "" : (forecastBasis === "lower_bound" ? "≥" : "≈");
  if ($("pace-label") && projected != null) $("pace-label").textContent = `${forecastBasis === "lower_bound" ? "usage ≥ " : "usage "}${usd(projected)}`;
  if ($("run-rate") && planCost != null) $("run-rate").textContent = `plans ${usd(planCost)}`;
  if ($("forecast-delta") && projected != null && planCost) $("forecast-delta").textContent = `${(projected / planCost).toFixed(2)}× accrued (same period)${forecastBasis === "lower_bound" ? " · lower bound" : ""}`;
  if ($("forecast-note") && planCost != null) $("forecast-note").textContent = forecastNote(state.summary);
  if ($("range-label") && state.summary && tokenTotal != null) {
    $("range-label").textContent = `${windowLabel(state.summary.window?.key || state.window)} window · ${tokens(tokenTotal)} tokens · ${number(records)} records`;
  }
  if ($("detail-share-bar") && eShare != null) $("detail-share-bar").style.width = `${Math.min(100, eShare)}%`;
  if ($("detail-share-label") && eVal != null) {
    $("detail-share-label").textContent = `${markedUsd(eVal, state.entityData?.isExact)} of tracked value · ${pct(eShare)}`;
  }
  if (state.view === "overview" && state.pinned == null && state.hover == null && hoverTotal != null && $("hover-value")) {
    $("hover-value").textContent = tokens(hoverTotal);
  }
}
function tickEase() {
  easeFrame = null;
  state.easeFrames += 1;
  let changed = false;
  let pending = false;
  for (const key of Object.keys(state.targets)) {
    const target = state.targets[key];
    const current = state.disp[key];
    if (current === undefined) {
      state.disp[key] = target;
      changed = true;
      continue;
    }
    const delta = target - current;
    if (Math.abs(delta) > Math.max(0.0004, Math.abs(target) * 0.00035)) {
      state.disp[key] = current + delta * EASE_RATE;
      changed = true;
      pending = true;
    } else if (current !== target) {
      state.disp[key] = target;
      changed = true;
    }
  }
  if (changed) paintEase();
  if (pending) easeFrame = requestAnimationFrame(tickEase);
}
function stableScale(registry, key, rawScale, anchorKey, presentKeys) {
  // The Y axis never shrinks while the bucket that set it is still on the
  // chart; it only recomputes once that bucket has left the window. Growth
  // is immediate so a new peak is never clipped.
  const entry = registry[key];
  const previous = entry ? finite(entry.scale) : null;
  const anchorPresent = Boolean(entry && presentKeys && presentKeys.has(entry.anchor));
  if (previous == null || rawScale > previous || !anchorPresent) {
    registry[key] = {scale: rawScale, anchor: anchorKey};
    return rawScale;
  }
  return previous;
}

function renderPreservingScroll(background, render) {
  if (!background) {
    render();
    return;
  }
  const left = window.scrollX;
  const top = window.scrollY;
  const generation = state.request;
  const restore = () => {
    if (generation !== state.request) return;
    if (Math.abs(window.scrollX - left) > 0.5 || Math.abs(window.scrollY - top) > 0.5) {
      window.scrollTo({left, top, behavior:"auto"});
    }
  };
  const started = performance.now();
  render();
  timing.lastRenderMs = performance.now() - started;
  restore();
  requestAnimationFrame(restore);
}

function bucketKeyOf(bucket, index) {
  // Server keys are stable grid cells; label is only a last resort because
  // calendar windows repeat it across several bars.
  return String(bucket.bucketKey || bucket.bucketStart || bucket.label || index);
}

function reconcileSegmentColumns(root, series, segmentRows, activeIndex) {
  const existing = new Map([...root.querySelectorAll(":scope > .day-column")].map(column => [column.dataset.bucketKey, column]));
  const keep = new Set();
  series.forEach((bucket, index) => {
    const bucketKey = bucketKeyOf(bucket, index);
    keep.add(bucketKey);
    let column = existing.get(bucketKey);
    if (!column) {
      column = document.createElement("div");
      column.className = "day-column";
      column.dataset.bucketKey = bucketKey;
    }
    column.classList.toggle("dim", activeIndex != null && activeIndex !== index);
    const segments = new Map([...column.querySelectorAll(":scope > .segment")].map(segment => [segment.dataset.segmentKey, segment]));
    const segmentKeep = new Set();
    segmentRows(bucket, index).forEach((item, segmentIndex) => {
      if (finite(item.height) == null) return;
      const segmentKey = String(item.key);
      segmentKeep.add(segmentKey);
      let segment = segments.get(segmentKey);
      const isNewSegment = !segment;
      if (!segment) {
        segment = document.createElement("i");
        segment.className = "segment";
        segment.dataset.segmentKey = segmentKey;
        column.appendChild(segment);
      }
      setStyle(segment, "background", item.color);
      setStyle(segment, "animationDelay", `${segmentIndex * 46 + index * 12}ms`);
      const height = `${Math.max(0, item.height).toFixed(2)}%`;
      if (isNewSegment && document.body.classList.contains("settled") && !PROBE) {
        segment.style.height = "0%";
        requestAnimationFrame(() => { segment.style.height = height; });
      } else {
        setStyle(segment, "height", height);
      }
    });
    segments.forEach((segment, key) => {
      if (!segmentKeep.has(key)) segment.remove();
    });
    let capped = false;
    column.querySelectorAll(":scope > .segment").forEach(segment => {
      const visible = !capped && parseFloat(segment.style.height) > 0;
      if (visible) capped = true;
      segment.classList.toggle("top", visible);
    });
    const at = root.children[index];
    if (at !== column) root.insertBefore(column, at || null);
  });
  existing.forEach((column, key) => {
    if (!keep.has(key)) column.remove();
  });
}

function setQuery() {
  const url = new URL(location.href);
  url.searchParams.set("window", state.window);
  if (state.machine && state.machine !== "all") url.searchParams.set("machine", state.machine);
  else url.searchParams.delete("machine");
  if (state.tool && state.tool !== "all") url.searchParams.set("tool", state.tool);
  else url.searchParams.delete("tool");
  if (PROBE) url.searchParams.set("probe", "1");
  history.replaceState(null, "", url);
}
function setLoading(on) {
  document.body.classList.toggle("loading", on && !activeDataMatches());
  $("main").setAttribute("aria-busy", String(on));
  ["overview", "detail", "diagnostics"].forEach(view => {
    $(view + "-view").setAttribute("aria-busy", String(on && state.view === view));
  });
}
function showError(error) {
  $("error-message").textContent = " " + (error?.message || String(error));
  $("error-banner").hidden = false;
}
function clearError() {
  $("error-banner").hidden = true;
}
function setView(view) {
  const changed = state.view !== view;
  state.view = view;
  $("overview-view").hidden = view !== "overview";
  $("detail-view").hidden = view !== "detail";
  $("diagnostics-view").hidden = view !== "diagnostics";
  if (changed && view !== "overview") window.scrollTo({top:0, behavior:"instant" in window ? "instant" : "auto"});
}
function readSnapshot(windowKey) {
  try {
    const raw = localStorage.getItem(SNAPSHOT_PREFIX + windowKey);
    if (!raw) return null;
    const snapshot = JSON.parse(raw);
    const age = Date.now() - Date.parse(snapshot?.storedAt || "");
    const endAge = Date.now() - Date.parse(snapshot?.summary?.window?.to || "");
    if (snapshot?.summary?.window?.key !== windowKey || !Number.isFinite(age) || age < 0 || age > SNAPSHOT_MAX_AGE_MS || !Number.isFinite(endAge) || endAge < 0 || endAge > SNAPSHOT_MAX_AGE_MS) return null;
    return snapshot;
  } catch {
    return null;
  }
}
function writeSnapshot(windowKey, summary, health) {
  try {
    localStorage.setItem(SNAPSHOT_PREFIX + windowKey, JSON.stringify({storedAt: new Date().toISOString(), summary, health: health || null}));
  } catch {}
}
function pruneSnapshots() {
  try {
    const stale = [];
    for (let index = 0; index < localStorage.length; index++) {
      const key = localStorage.key(index);
      if (key && key.startsWith("burnrate:snapshot:") && !key.startsWith(SNAPSHOT_PREFIX)) stale.push(key);
    }
    stale.forEach(key => localStorage.removeItem(key));
  } catch {}
}
function paintSnapshot(windowKey) {
  // A stored payload paints instantly but is never presented as live: the
  // navbar reads "Stale · as of HH:MM" until a real fetch succeeds.
  const snapshot = readSnapshot(windowKey);
  if (!snapshot) return false;
  state.summary = {...snapshot.summary, status: "stale", snapshot: true};
  if (snapshot.health) state.health = snapshot.health;
  if (snapshot.summary.navigation) state.navigation = snapshot.summary.navigation;
  setLoading(false);
  renderOverview();
  return true;
}
async function jsonFetch(url, prefetched, signal) {
  const response = await (prefetched || fetch(url, {cache:"no-store", signal}));
  if (!response.ok) {
    let detail = "";
    try {
      const body = await response.json();
      detail = body.detail || body.error || "";
    } catch {}
    throw new Error(`${response.status} ${detail || url}`);
  }
  return response.json();
}

function reconcileChildren(root, items, keyOf, create, update) {
  // Keyed in-place reconciliation: existing nodes keep their identity (and
  // their focus, hover, transitions and eased values); only nodes whose key
  // disappeared are removed and only new keys are created.
  delete root.dataset.emptyCopy;
  invalidateEaseNodes();
  const existing = new Map();
  [...root.children].forEach(child => {
    const key = child.dataset ? child.dataset.rkey : undefined;
    if (key == null) child.remove();
    else existing.set(key, child);
  });
  const keep = new Set();
  items.forEach((item, index) => {
    const key = String(keyOf(item, index));
    keep.add(key);
    let node = existing.get(key);
    const fresh = !node;
    if (fresh) {
      node = create(item, index);
      node.dataset.rkey = key;
    }
    update(node, item, index, fresh);
    const at = root.children[index];
    if (at !== node) root.insertBefore(node, at || null);
  });
  existing.forEach((node, key) => {
    if (!keep.has(key)) node.remove();
  });
}
function nodeFrom(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}
function setEmpty(root, html) {
  if (root.dataset.emptyCopy === html) return;
  root.innerHTML = html;
  root.dataset.emptyCopy = html;
}
function setText(el, text) {
  if (el && el.textContent !== text) el.textContent = text;
}
function setStyle(el, prop, value) {
  if (el && el.style[prop] !== value) el.style[prop] = value;
}
function setAttr(el, name, value) {
  if (!el) return;
  if (value == null) {
    if (el.hasAttribute(name)) el.removeAttribute(name);
  } else if (el.getAttribute(name) !== String(value)) {
    el.setAttribute(name, String(value));
  }
}
function setTextAfter(anchor, text) {
  let node = anchor.nextSibling;
  if (!node || node.nodeType !== Node.TEXT_NODE) {
    node = document.createTextNode("");
    anchor.after(node);
  }
  if (node.nodeValue !== text) node.nodeValue = text;
}
function bindEase(el, key, fmt, text) {
  if (!el) return;
  if (el.dataset.ease !== (key || undefined)) invalidateEaseNodes();
  if (key) {
    el.dataset.ease = key;
    el.dataset.fmt = fmt;
  } else {
    delete el.dataset.ease;
    delete el.dataset.fmt;
  }
  setText(el, text);
}
function bindWidth(el, key, value, fresh) {
  // Width bars are painted by the easing loop; only a new node needs its
  // starting width so it continues from the current eased value.
  if (!el) return;
  if (value == null) {
    delete el.dataset.ease;
    setStyle(el, "width", "0%");
    return;
  }
  el.dataset.ease = key;
  el.dataset.fmt = "width";
  if (fresh || !el.style.width) setStyle(el, "width", `${Math.max(0, Math.min(100, value)).toFixed(2)}%`);
}
function renderGutter(root, scale) {
  const stamp = String(scale);
  if (root.dataset.scale === stamp) return;
  root.dataset.scale = stamp;
  root.innerHTML = [1, 0.75, 0.5, 0.25, 0].map(fr => {
    const top = gridTop(fr).toFixed(2);
    return `<div class="grid-line${fr === 0 ? " base" : ""}" style="top:${top}%"></div><span class="grid-label" style="top:${top}%">${fr === 0 ? "0" : tickLabel(scale * fr)}</span>`;
  }).join("");
}
function renderAxis(root, series) {
  reconcileChildren(root, [0, 1, 2, 3, 4], slot => slot, () => document.createElement("span"), (node, slot) => {
    setText(node, series[Math.round(slot * (series.length - 1) / 4)]?.label || "");
  });
}
function renderHitTargets(root, series, valueOf, activeIndex) {
  reconcileChildren(root, series, (bucket, index) => index, () => nodeFrom(`<button type="button" class="hit-target"></button>`), (node, bucket, index) => {
    node.dataset.index = String(index);
    node.classList.toggle("active", activeIndex === index);
    setAttr(node, "aria-label", `${bucket.label} ${tokens(valueOf(bucket))}`);
  });
}

function capacityNote(row) {
  if (row.isPayg) return row.source && row.source !== "payg" ? row.source : "per-token rates · no quota to exhaust";
  if (row.pct == null && row.used == null && row.allowance == null) {
    return row.source || row.reason || "Quota unavailable";
  }
  const parts = [];
  if (row.used != null && row.allowance != null) parts.push(`${formatUnit(row.used, row.unit)} of ${formatUnit(row.allowance, row.unit)}`);
  else if (row.pct != null && row.allowance != null) {
    const used = Number(row.allowance) * Number(row.pct) / 100;
    parts.push(`${formatUnit(used, row.unit)} of ${formatUnit(row.allowance, row.unit)}`);
  }
  if (row.source) parts.push(row.source);
  const reset = formatReset(row.resetsAt);
  if (reset) parts.push("resets " + reset);
  const elapsed = quotaElapsedPct(row);
  if (elapsed != null && finite(row.pct) != null) {
    const diff = Math.round(Number(row.pct) - elapsed);
    parts.push(diff > 2 ? `+${diff}% vs even pace` : diff < -2 ? `${Math.abs(diff)}% under even pace` : "on even pace");
  }
  const age = quotaFreshnessAge(row.polledAt || row.observedAt);
  if (age) parts.push("polled " + age);
  return parts.join(" · ") || "Quota unavailable";
}
function sortQuotaRows(rows) {
  return [...(rows || [])].sort((a, b) => {
    if (a.isPayg && !b.isPayg) return 1;
    if (b.isPayg && !a.isPayg) return -1;
    if (Boolean(a.isPrimary) !== Boolean(b.isPrimary)) return a.isPrimary ? -1 : 1;
    const ao = finite(a.order);
    const bo = finite(b.order);
    if (ao != null || bo != null) return (ao ?? 999) - (bo ?? 999);
    const ap = finite(a.pct);
    const bp = finite(b.pct);
    if (ap == null && bp == null) return 0;
    if (ap == null) return 1;
    if (bp == null) return -1;
    return bp - ap;
  });
}
function renderQuotaRows(root, rows, idPrefix) {
  const models = quotaRowModels(rows, idPrefix);
  reconcileChildren(root, models, model => "limit:" + model.rowKey, () => {
    const node = nodeFrom(`<div class="capacity-row">
      <div>
        <div class="capacity-row-top"><span></span><b></b></div>
        <div class="track"></div>
        <span class="capacity-note"><span class="capacity-note-eta"></span></span>
      </div>
      <div class="capacity-eta"><strong></strong><span></span></div>
    </div>`);
    const note = node.querySelector(".capacity-note");
    note.insertBefore(document.createTextNode(""), note.firstChild);
    return node;
  }, (node, model, index, fresh) => {
    const top = node.querySelector(".capacity-row-top");
    setText(top.querySelector("span"), model.label);
    const pctEl = top.querySelector("b");
    bindEase(pctEl, model.skipPct ? "" : model.easeKey, "pct", model.eased == null ? unknown : pct(model.eased));
    setStyle(pctEl, "color", model.color);
    const track = node.querySelector(".track");
    track.classList.toggle("payg", model.payg);
    let bar = track.querySelector("i");
    if (model.eased == null || model.payg) {
      if (bar) bar.remove();
    } else {
      const freshBar = !bar;
      if (!bar) {
        bar = document.createElement("i");
        bar.dataset.quotaColor = "1";
        track.appendChild(bar);
      }
      bindWidth(bar, model.easeKey, model.eased, freshBar || fresh);
      setStyle(bar, "background", model.color);
      setStyle(bar, "animationDelay", `${index * 90}ms`);
    }
    const note = node.querySelector(".capacity-note");
    if (!note.firstChild || note.firstChild.nodeType !== Node.TEXT_NODE) note.insertBefore(document.createTextNode(""), note.firstChild);
    if (note.firstChild.nodeValue !== model.noteCore) note.firstChild.nodeValue = model.noteCore;
    setText(note.querySelector(".capacity-note-eta"), ` · ${model.eta.value} ${model.eta.label}`);
    const eta = node.querySelector(".capacity-eta");
    const etaStrong = eta.querySelector("strong");
    setText(etaStrong, model.eta.value);
    setStyle(etaStrong, "color", model.etaColor);
    setText(eta.querySelector("span"), model.eta.label);
  });
}
function quotaRowModels(rows, idPrefix) {
  return sortQuotaRows(rows).map((row, index) => {
    const easeKey = idPrefix + (row.limitKey || index);
    const payg = Boolean(row.isPayg);
    const pctRaw = finite(row.pct);
    const skipPct = payg || pctRaw == null;
    const eased = skipPct ? ease(easeKey, null) : ease(easeKey, Math.max(0, Math.min(100, pctRaw)));
    const color = payg ? "var(--dim)" : quotaColor(eased);
    const eta = payg
      ? {value: markedUsd(row.monthToDateUsd ?? row.used, false), label: row.etaLabel || "this month"}
      : etaFromReset(row.resetsAt);
    const etaColor = payg ? "var(--muted)" : eased == null ? "var(--text)" : eased >= 85 ? "var(--danger)" : eased >= 60 ? "var(--accent)" : "var(--text)";
    const usedKey = easeKey + "-used";
    let usedNow = finite(row.used);
    if (usedNow == null && pctRaw != null && finite(row.allowance) != null) usedNow = finite(row.allowance) * pctRaw / 100;
    if (!payg && usedNow != null) ease(usedKey, usedNow);
    const noteCore = capacityNote(row);
    const paceNote = row.forecast && row.forecast.line ? String(row.forecast.line).replace(/^pace → /, "") : "";
    const paceTargetPct = quotaElapsedPct(row);
    return {
      rowKey: row.limitKey || index,
      label: row.label || "Limit",
      easeKey, payg, skipPct, eased, color, eta, etaColor, noteCore, paceNote, paceTargetPct
    };
  });
}

function renderNavbar(payload) {
  const navigation = payload?.navigation || state.navigation;
  if (navigation) state.navigation = navigation;
  const rate = $("nav-run-rate");
  const today = $("nav-today");
  const status = $("diagnostics-button");
  const label = $("nav-status-label");
  const detail = $("nav-status-detail");
  const burn = navigation ? ease("navRate", navigation.burnRatePerDay) : null;
  const todayUsd = navigation ? ease("navToday", navigation.todayUsd) : null;
  if (burn != null) {
    rate.classList.remove("burnrate-skeleton");
    rate.dataset.ease = "navRate";
    rate.dataset.fmt = "usdDay";
    rate.textContent = usd(burn) + "/day";
    rate.removeAttribute("aria-label");
  } else if (navigation) {
    rate.classList.remove("burnrate-skeleton");
    rate.textContent = unknown;
  }
  if (todayUsd != null) {
    today.classList.remove("burnrate-skeleton");
    today.dataset.ease = "navToday";
    today.dataset.fmt = "usd";
    today.textContent = usd(todayUsd);
    today.removeAttribute("aria-label");
  } else if (navigation) {
    today.classList.remove("burnrate-skeleton");
    today.textContent = unknown;
  }
  const mode = payload?.status || state.summary?.status || "loading";
  const cadenceSeconds = payload?.cadenceSeconds ?? state.summary?.cadenceSeconds;
  const cadenceMinutes = payload?.cadenceMinutes ?? state.summary?.cadenceMinutes;
  const generated = payload?.generatedAt || state.summary?.generatedAt;
  const stamp = generated ? new Date(generated).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"}) : unknown;
  const cadenceText = cadenceSeconds != null
    ? cadenceSeconds < 60 ? `${cadenceSeconds}s cadence` : `${cadenceSeconds / 60}m cadence`
    : cadenceMinutes == null ? unknown : `${cadenceMinutes}m cadence`;
  const visualState = mode === "success" ? "live" : mode;
  status.dataset.state = visualState;
  let announce = null;
  if (mode === "error") {
    const failed = (state.health?.ingest || []).find(item => item.status === "failed" || item.status === "error");
    const source = payload?.failingSource || state.summary?.failingSource || failed?.source || "ingest";
    label.textContent = "Ingest failed";
    detail.textContent = source;
    status.setAttribute("aria-label", `Ingest failed: ${source}. Open diagnostics.`);
    announce = `Ingest failed: ${source}`;
  } else if (mode === "stale") {
    label.textContent = "Stale";
    detail.textContent = payload?.snapshot ? `as of ${stamp}` : stamp === unknown ? "no successful ingest" : `last success ${stamp}`;
    status.setAttribute("aria-label", `Stale metering. ${detail.textContent}`);
    announce = "Metering is stale";
  } else if (mode === "loading") {
    label.textContent = "Metering";
    detail.textContent = "Waiting for telemetry";
    status.setAttribute("aria-label", "Metering status loading");
    announce = "Metering status loading";
  } else if (freshIssues().length) {
    const issues = freshIssues();
    const lead = issues[0];
    status.dataset.state = state.freshness.status === "alarm" ? "error" : "stale";
    label.textContent = `${lead.short} ${lead.state}`;
    detail.textContent = issues.length > 1 ? `+${issues.length - 1} more · diagnostics` : lead.ageSeconds != null ? `${agoText(lead.ageSeconds)} old · diagnostics` : "open diagnostics";
    status.setAttribute("aria-label", `${lead.short} ${lead.state}. ${lead.detail} Open diagnostics.`);
    announce = `${lead.short} ${lead.state}`;
  } else {
    label.textContent = "Metering";
    detail.textContent = [stamp, cadenceText].filter(part => part && part !== unknown).join(" · ") || unknown;
    status.setAttribute("aria-label", `Metering live. Last refresh ${stamp}. ${cadenceText === unknown ? "Cadence unknown" : cadenceText}.`);
    announce = "Metering is live";
  }
  // Announce only transitions, never per-poll timestamps (C14).
  const signature = `${announce}`;
  if ($("status-live") && state.lastStatusAnnounce !== signature) {
    state.lastStatusAnnounce = signature;
    $("status-live").textContent = announce;
  }
}

// This computer's own freshness (local only, never proxied to the hub).
function freshIssues() {
  return (state.freshness?.issues || []).filter(item => item.severity !== "info");
}
function agoText(seconds) {
  const n = Number(seconds);
  if (seconds == null || !Number.isFinite(n)) return unknown;
  if (n < 90) return `${Math.round(n)}s`;
  if (n < 5400) return `${Math.round(n / 60)}m`;
  if (n < 172800) return `${Math.round(n / 3600)}h`;
  return `${Math.round(n / 86400)}d`;
}
async function loadFreshness() {
  if (document.hidden || Date.now() - (state.freshnessAt || 0) < 10000) return;
  state.freshnessAt = Date.now();
  try {
    state.freshness = await jsonFetch("/api/spend/freshness");
  } catch {
    state.freshness = null;
  }
  renderNavbar();
  renderFreshness();
}
function renderFreshness() {
  const data = state.freshness;
  const list = $("freshness-list");
  if (!list || state.view !== "diagnostics") return;
  const beat = data?.ingest || {};
  const parts = [];
  if (beat.lastPassSeconds != null) parts.push(`Last collection pass ${beat.lastPassSeconds}s, ${agoText(beat.lastPassAgeSeconds)} ago`);
  if (data?.sync) parts.push(data.sync.failing ? "Hub sync failing" : `Hub sync ${agoText(data.sync.ageSeconds)} ago`);
  if (data?.hub?.version) parts.push(`Hub ${data.hub.version}${data.hub.matches ? "" : `, this computer ${data.version}`}`);
  setText($("freshness-note"), data ? parts.join(" · ") || "Waiting for the first collection pass." : "Freshness unavailable.");
  const rows = [...(data?.issues || []), ...(data?.machines || []).filter(item => item.state !== "online").map(item => ({
    key: "machine:" + item.key, short: item.key, state: "quiet", severity: "info", ageSeconds: item.ageSeconds,
    detail: item.lastSeenAt ? "Last synced " + new Date(item.lastSeenAt).toLocaleString() : "Never synced"
  }))];
  if (!rows.length) {
    setEmpty(list, `<p class="panel-empty">Every source on this computer is current.</p>`);
    return;
  }
  reconcileChildren(list, rows, item => item.key, () => nodeFrom(`<div class="waste-item"><div><strong></strong><small></small></div><span></span></div>`), (node, item) => {
    setText(node.querySelector("strong"), item.short);
    setText(node.querySelector("small"), item.detail);
    const tag = node.querySelector(":scope > span");
    setText(tag, item.state + (item.ageSeconds != null ? " · " + agoText(item.ageSeconds) : ""));
    setStyle(tag, "color", item.severity === "alarm" ? "var(--danger)" : item.severity === "warn" ? "var(--accent)" : "var(--dim)");
  });
}

function renderCoverage(data) {
  const coverage = data.coverage || {};
  const exact = coverage.exactProviders || [];
  const derived = coverage.derivedProviders || [];
  const total = exact.length + derived.length;
  const unpriced = (data.models || []).some(model => model.value == null) || (state.health?.pricingGaps || []).length;
  $("coverage-title").textContent = unpriced && !exact.length && !derived.length
    ? "Pricing coverage"
    : derived.length ? "Partial pricing coverage" : exact.length ? "Exact pricing coverage" : "Pricing coverage";
  const banner = $("coverage-banner");
  if (banner) banner.dataset.state = derived.length ? "partial" : exact.length ? "exact" : "quiet";
  if (coverage.note) {
    $("coverage-note").innerHTML = esc(coverage.note).replace("≈", "<b>≈</b>");
  } else if (total > 0) {
    $("coverage-note").innerHTML = `${exact.length} of ${total} providers publish exact per-token rates. Every figure derived from published rates instead of a billed amount is marked <b>≈</b>. Unpriced usage is counted in tokens and never estimated in dollars.`;
  } else {
    $("coverage-note").innerHTML = `Every figure derived from published rates instead of a billed amount is marked <b>≈</b>. Unpriced usage is counted in tokens and never estimated in dollars.`;
  }
  $("coverage-exact").textContent = exact.length ? "exact: " + exact.join(", ") : "exact: —";
}

function renderRanges(scrollActive) {
  const html = WINDOWS.map(item => `<button type="button" role="radio" data-window="${item.key}" class="${item.key === state.window ? "active" : ""}" aria-checked="${item.key === state.window}">${item.label}</button>`).join("");
  const roots = [$("range-switch"), $("detail-ranges")];
  roots.forEach(root => {
    if (!root) return;
    if (root.dataset.ready !== "1") {
      root.innerHTML = html;
      root.dataset.ready = "1";
    } else {
      root.querySelectorAll("[data-window]").forEach(button => {
        const on = button.dataset.window === state.window;
        button.classList.toggle("active", on);
        button.setAttribute("aria-checked", String(on));
      });
    }
  });
  if (scrollActive) {
    const active = document.querySelector("#range-switch button.active, #detail-ranges button.active");
    const reduceMotion = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    // Center the active window inside its own strip only; never scroll the page.
    const strip = active?.parentElement;
    if (strip && strip.scrollWidth > strip.clientWidth) {
      const left = active.getBoundingClientRect().left - strip.getBoundingClientRect().left + strip.scrollLeft;
      strip.scrollTo({left: left - (strip.clientWidth - active.offsetWidth) / 2, behavior: reduceMotion ? "auto" : "smooth"});
    }
  }
}
function changeRange(key) {
  if (!WINDOW_KEYS.has(key) || key === state.window) return;
  state.window = key;
  state.hover = null;
  state.pinned = null;
  state.dHover = null;
  state.dPinned = null;
  state.chartScales = Object.create(null);
  state.detailScales = Object.create(null);
  setQuery();
  renderRanges(true);
  if (state.view === "detail" && state.entity) loadEntity(state.entity.kind, state.entity.key);
  else loadSummary(false);
}

function shortLimitLabel(label, providerName) {
  let text = String(label || "Limit").trim();
  const name = String(providerName || "").trim();
  if (name && text.toLowerCase().startsWith(name.toLowerCase())) text = text.slice(name.length).trim();
  text = text.replace(/^(claude|codex|z\.ai|cursor|grok build|openrouter|muse code)\s+/i, "")
    .replace(/\s+(window|credits|limit|quota)$/i, "")
    .replace(/Gemini models\s*·\s*Five-hour remaining/i, "Gemini · 5h")
    .replace(/Gemini models\s*·\s*Weekly remaining/i, "Gemini · weekly")
    .replace(/Claude\/GPT models\s*·\s*Five-hour remaining/i, "Claude/GPT · 5h")
    .replace(/Claude\/GPT models\s*·\s*Weekly remaining/i, "Claude/GPT · weekly")
    .trim();
  return text || String(label || "Limit");
}
function laneTone(value) {
  const n = finite(value);
  if (n == null) return "none";
  if (n >= 85) return "hot";
  if (n >= 60) return "warm";
  return "quiet";
}
function providerHasQuota(provider) {
  return !provider.isPayg && (provider.rows || []).some(row => finite(row.pct) != null);
}
function renderCapacity(data) {
  const checked = data.generatedAt ? "checked " + new Date(data.generatedAt).toLocaleTimeString([], {hour:"numeric", minute:"2-digit", second:"2-digit"}) : "checked —";
  setText($("capacity-checked"), checked);
  const all = (data.capacity || []).filter(item => item.providerKey !== "cursor").sort((a, b) => {
    if (a.isPayg && !b.isPayg) return 1;
    if (b.isPayg && !a.isPayg) return -1;
    const ad = finiteDate(a.forecast && a.forecast.projectedRunDry);
    const bd = finiteDate(b.forecast && b.forecast.projectedRunDry);
    if (ad != null || bd != null) {
      if (ad == null) return 1;
      if (bd == null) return -1;
      if (ad !== bd) return ad - bd;
    }
    const ap = finite(a.peakPct);
    const bp = finite(b.peakPct);
    if (ap == null && bp == null) return 0;
    if (ap == null) return 1;
    if (bp == null) return -1;
    return bp - ap;
  });
  const headline = $("capacity-headline");
  const shell = $("capacity-body");
  if (!all.length) {
    delete headline.dataset.shape;
    setText(headline, "No capacity rows");
    setEmpty(shell, `<p class="capacity-empty">Provider quotas will appear here when pollers report used, allowance, and reset times.</p>`);
    return;
  }
  // Lanes carry a measurable limit; everything else (pay as you go, unavailable
  // pollers) is folded into one footer sentence per provider.
  const providers = all.filter(providerHasQuota);
  const withoutQuota = all.filter(provider => !providerHasQuota(provider) || (window.productSourceGuidance || []).some(source => source.state === "detected_without_history" && window.ProductLogic?.capacitySourceKey(source.source) === provider.providerKey));
  const lead = providers[0] || all[0];
  const peakRaw = lead.isPayg ? null : finite(lead.peakPct);
  const peak = peakRaw == null ? ease("capPeak", null) : ease("capPeak", peakRaw);
  if (!providers.length) {
    delete headline.dataset.shape;
    setText(headline, lead.isPayg ? `${lead.providerName} no quota` : `${lead.providerName} peak ${unknown}`);
  } else {
    const leadRow = sortQuotaRows(lead.rows)[0];
    const leadEta = etaFromReset(leadRow?.resetsAt);
    const shape = `lead:${lead.providerKey}|row:${leadRow?.limitKey || ""}`;
    if (headline.dataset.shape !== shape) {
      headline.dataset.shape = shape;
      headline.innerHTML = `<span class="capacity-lead-name">${esc(lead.providerName)} <span class="capacity-lead-limit">${esc(shortLimitLabel(leadRow?.label, lead.providerName))}</span></span><span class="capacity-lead-fig" data-ease="capPeak" data-fmt="pct"></span><span class="capacity-headline-eta"></span>`;
    }
    const pressure = lead.forecast && lead.forecast.state || "";
    headline.dataset.pressure = pressure === "dry" || pressure === "tight" || pressure === "safe" ? pressure : "";
    setText(headline.querySelector("[data-ease='capPeak']"), peak == null ? unknown : pct(peak));
    const bits = [];
    if (leadEta.value !== unknown) bits.push(`${leadEta.value} ${leadEta.label}`);
    if (pressure === "dry" && lead.forecast.line) bits.push(String(lead.forecast.line).replace(/^pace → /, ""));
    setText(headline.querySelector(".capacity-headline-eta"), bits.join(" · "));
  }
  if (!shell.querySelector(".capacity-lanes")) {
    setEmpty(shell, `<div class="capacity-lanes"></div><div class="capacity-foot" hidden></div>`);
  }
  const body = shell.querySelector(".capacity-lanes");
  const foot = shell.querySelector(".capacity-foot");
  reconcileChildren(body, providers, provider => "provider:" + provider.providerKey, () => nodeFrom(`<div class="capacity-lane">
      <div class="lane-who"><span class="logo-tile"></span><div><b></b><em><span class="lane-sub"></span><span class="lane-sub-eta"></span></em></div></div>
      <div class="lane-bars"><div class="lane-tracks"></div><p class="lane-pace" hidden></p><div class="lane-win"></div></div>
      <b class="lane-pct"></b>
      <div class="lane-eta"><strong></strong><span></span></div>
    </div>`), (node, provider) => {
    const rows = sortQuotaRows((provider.rows || []).map(row => ({...row, monthToDateUsd: row.monthToDateUsd ?? provider.monthToDateUsd, isPayg: row.isPayg ?? provider.isPayg})));
    const models = quotaRowModels(rows, "cap-" + provider.providerKey + "-");
    const leadModel = models[0];
    const peakValue = finite(provider.peakPct);
    const easedPeak = peakValue == null ? ease("peak-" + provider.providerKey, null) : ease("peak-" + provider.providerKey, peakValue);
    const forecastState = provider.forecast && provider.forecast.state || "";
    let tone = laneTone(easedPeak);
    if (forecastState === "safe") tone = "quiet";
    else if (forecastState === "tight") tone = "warm";
    else if (forecastState === "dry") tone = "hot";
    setAttr(node, "data-tone", tone);
    setAttr(node, "data-forecast", forecastState === "tight" || forecastState === "dry" ? forecastState : null);
    // The full note (used of allowance, source, reset) stays available on hover
    // and in Data health; the lane surface shows only what changes a decision.
    setAttr(node, "title", rows.map(row => `${row.label || "Limit"}: ${capacityNote(row)}`).join("\n") + (provider.activityNote ? `\n${provider.activityNote}` : ""));
    node.onclick = event => {
      if (!event.target.closest("button, a")) loadEntity("tool", provider.toolKey || provider.providerKey);
    };
    const polledAge = quotaFreshnessAge(provider.polledAt);
    setLogo(node.querySelector(".lane-who > .logo-tile"), provider.providerKey === "opencode" ? "zai" : logoKey(provider.providerKey, provider.providerName));
    setText(node.querySelector(".lane-who b"), provider.providerName || "");
    setText(node.querySelector(".lane-sub"), [provider.plan, leadModel ? shortLimitLabel(leadModel.label, provider.providerName) : "", polledAge, provider.activityNote ? "OpenCode Grok activity observed" : ""].filter(Boolean).join(" · "));
    setText(node.querySelector(".lane-sub-eta"), leadModel && leadModel.eta.value !== unknown ? ` · ${leadModel.eta.value} ${leadModel.eta.label}` : "");
    const tracks = node.querySelector(".lane-tracks");
    reconcileChildren(tracks, models, model => "bar:" + model.rowKey, () => nodeFrom(`<div class="track"><i data-quota-color="1"></i><b class="pace-tick" hidden></b></div>`), (bar, model, index, fresh) => {
      bar.classList.toggle("thin", index > 0);
      const fill = bar.querySelector("i");
      bindWidth(fill, model.easeKey, model.eased, fresh);
      // Each meter carries its own pressure; a dry or tight forecast also marks the lead window.
      let barColor = quotaColor(model.eased);
      if (index === 0 && forecastState === "dry") barColor = "var(--danger)";
      else if (index === 0 && forecastState === "tight" && barColor === "var(--meter)") barColor = "var(--accent)";
      setStyle(fill, "background", barColor);
      setStyle(fill, "animationDelay", `${index * 90}ms`);
      const paceMarker = bar.querySelector(".pace-tick");
      if (paceMarker) {
        paceMarker.hidden = model.paceTargetPct == null || model.payg;
        if (model.paceTargetPct != null) setStyle(paceMarker, "left", `${model.paceTargetPct.toFixed(1)}%`);
      }
    });
    const win = node.querySelector(".lane-win");
    if (models.length > 1) {
      reconcileChildren(win, models, model => "win:" + model.rowKey, () => nodeFrom(`<span><span></span> <b></b><em></em></span>`), (span, model) => {
        setText(span.firstElementChild, shortLimitLabel(model.label, provider.providerName));
        bindEase(span.querySelector("b"), model.skipPct ? "" : model.easeKey, "pct", model.eased == null ? unknown : pct(model.eased));
        setStyle(span.querySelector("b"), "color", model.eased == null ? "" : quotaColor(model.eased));
        const etaText = model.eta.value === unknown ? "" : ` · ${model.eta.value}`;
        setText(span.querySelector("em"), etaText + (model.paceNote ? ` · ${model.paceNote}` : ""));
      });
    } else {
      setEmpty(win, "");
    }
    const pace = node.querySelector(".lane-pace");
    const paceLine = provider.forecast && provider.forecast.line || "";
    pace.hidden = !paceLine;
    setText(pace, paceLine);
    setAttr(pace, "data-state", paceLine ? forecastState : null);
    bindEase(node.querySelector(".lane-pct"), peakValue == null ? "" : "peak-" + provider.providerKey, "pct", easedPeak == null ? unknown : pct(easedPeak));
    const eta = node.querySelector(".lane-eta");
    setText(eta.querySelector("strong"), leadModel ? leadModel.eta.value : unknown);
    setText(eta.querySelector("span"), leadModel ? leadModel.eta.label : "to reset");
  });
  reconcileChildren(foot, withoutQuota, provider => "foot:" + provider.providerKey, () => nodeFrom(`<span class="capacity-foot-item"><i></i><b></b><span></span><button type="button" class="settings-link row-secondary" hidden>Connect →</button></span>`), (node, provider) => {
    node.dataset.provider = provider.providerKey;
    const rows = provider.rows || [];
    setLogo(node.querySelector("i"), logoKey(provider.providerKey, provider.providerName));
    setText(node.querySelector("b"), provider.providerName || "");
    // Pollers put the human reason after an em dash in the row label.
    const labelReason = rows.map(row => String(row.label || "").split(" — ").slice(1).join(" — ").trim()).find(Boolean);
    let text;
    if (provider.isPayg) {
      const remaining = finite(provider.fundsRemainingUsd);
      text = remaining == null
        ? `funds remaining · ${labelReason || "management key required"}`
        : `funds remaining · ${usd(remaining)}`;
    } else {
      text = provider.reason || rows.map(row => row.reason).find(Boolean) || labelReason || "quota unavailable";
    }
    if (provider.activityNote) text += ` · ${provider.activityNote}`;
    const detected = (window.productSourceGuidance || []).some(source => source.state === "detected_without_history" && window.ProductLogic?.capacitySourceKey(source.source) === provider.providerKey);
    const detail = detected ? "detected, no history yet" : text;
    const detailNode = node.querySelector("span");
    setText(detailNode, detail);
    if (detailNode) detailNode.title = detail;
    setStyle(node.querySelector("i"), "color", detected ? "var(--amber)" : "");
    const connect = node.querySelector("button"); connect.hidden = !detected;
    connect.onclick = event => window.openHarnessManager?.(event.currentTarget);
  });
  foot.hidden = !withoutQuota.length;
}

window.refreshCapacitySourceHints = () => { if (state.summary) renderCapacity(state.summary); };

function renderActivity(data) {
  const rows = data.activity || [];
  const liveRows = rows.filter(isLiveRow);
  const body = $("activity-body");
  const pill = $("live-pill");
  const count = $("live-count");
  count.classList.remove("burnrate-skeleton");
  count.removeAttribute("aria-label");
  count.textContent = `${liveRows.length} live`;
  if (liveRows.length) {
    const figure = document.createElement("span");
    figure.className = "live-figure";
    figure.textContent = String(liveRows.length);
    const word = document.createElement("span");
    word.className = "live-word";
    word.textContent = "live";
    count.replaceChildren(figure, word);
    count.setAttribute("aria-label", `${liveRows.length} live`);
  }
  pill.classList.toggle("idle", liveRows.length === 0);
  body.querySelector(":scope > .skel-stack")?.remove();
  if (!liveRows.length) {
    if (!body.querySelector(":scope > .panel-empty")) {
      body.innerHTML = `<p class="panel-empty">No agents are running right now.</p>`;
    }
    return;
  }
  body.querySelector(":scope > .panel-empty")?.remove();
  const clock = liveRows.map(row => {
    const started = Date.parse(row.startedAt || "");
    return Number.isFinite(started) ? Math.max(0, Math.round((Date.now() - started) / 60000)) : null;
  });
  const longest = Math.max(1, ...clock.filter(value => value != null));
  const existing = new Map([...body.querySelectorAll(":scope > .activity-row")].map(item => [item.dataset.activityId, item]));
  const keep = new Set();
  liveRows.forEach((row, index) => {
    const activityId = String(row.id || `${row.name || "agent"}:${row.startedAt || index}`);
    keep.add(activityId);
    const liveNow = true;
    const delay = index * 260;
    let item = existing.get(activityId);
    if (!item) {
      item = document.createElement("div");
      item.className = "activity-row";
      item.dataset.activityId = activityId;
      item.innerHTML = `<span class="logo-tile"><i class="dot"></i></span><div><strong></strong><small></small><span class="activity-share"></span></div><em></em>`;
    }
    item.classList.toggle("live", liveNow);
    item.classList.toggle("nodata", !liveNow);
    let sweep = item.querySelector(":scope > .sweep");
    if (liveNow && !sweep) {
      sweep = document.createElement("div");
      sweep.className = "sweep";
      item.insertBefore(sweep, item.firstChild);
    } else if (!liveNow && sweep) {
      sweep.remove();
      sweep = null;
    }
    if (sweep) sweep.style.animationDelay = `${delay}ms`;
    item.querySelector(".dot").style.animationDelay = `${delay}ms`;
    setLogo(item.querySelector(".logo-tile"), logoKey(null, row.modelKey));
    const rawAgentName = String(row.name || "Untitled agent").replace(/\s+/g, " ").replace(/^[#*>-]+\s*/, "").trim() || "Untitled agent";
    const shortAgentName = rawAgentName.length > 68 ? rawAgentName.slice(0, 67).trimEnd() + "…" : rawAgentName;
    const strongEl = item.querySelector("strong");
    strongEl.textContent = shortAgentName;
    strongEl.title = rawAgentName.length > 68 ? rawAgentName.slice(0, 220) : "";
    const mins = clock[index];
    const age = mins == null ? "" : mins < 1 ? "just now" : mins < 60 ? `${mins}m` : `${Math.round(mins / 60)}h`;
    const multiMachine = (data.machines && data.machines.length > 1) || (row.machineId && row.machineId !== "local");
    const machinePrefix = multiMachine && row.machineName ? `${row.machineName} · ` : "";
    item.querySelector("small").textContent = liveNow
      ? `${machinePrefix}${row.modelKey || "model"}${age ? ` · ${age}` : ""}`
      : "stopped before telemetry was emitted";
    let share = item.querySelector(".activity-share");
    if (!share) {
      share = document.createElement("span");
      share.className = "activity-share";
      item.querySelector("div").appendChild(share);
    }
    const filled = mins == null ? 8 : Math.max(8, Math.round((mins / longest) * 100));
    share.style.setProperty("--fill", `${filled}%`);
    const status = item.querySelector("em");
    status.style.animationDelay = `${delay}ms`;
    status.textContent = liveNow ? "LIVE" : "NO DATA";
    const at = body.children[index];
    if (at !== item) body.insertBefore(item, at || null);
  });
  existing.forEach((item, key) => {
    if (!keep.has(key)) item.remove();
  });
}

function renderKpis(data) {
  const totals = data.totals || {};
  const mix = data.mix || [];
  // trackedValue is withheld whenever any row in the window has no pricing
  // card. Show the known sum as an explicit PARTIAL state in that case and
  // name the unpriced models, rather than blanking the tile.
  const known = finite(totals.knownValue);
  const unpricedModels = Array.isArray(totals.unpricedModels) ? totals.unpricedModels : [];
  const partial = finite(totals.trackedValue) == null && known != null && unpricedModels.length > 0;
  const tracked = ease("tracked", partial ? known : totals.trackedValue);
  const priced = ease("priced", totals.priced);
  const published = ease("published", totals.publishedRate);
  const tokenCount = ease("tokens", totals.tokens);
  const fullEffectiveRate = finite(totals.effectiveCostPerMillionTokens);
  const knownEffectiveRate = finite(totals.knownEffectiveCostPerMillionTokens);
  const effectiveRateComplete = totals.effectiveCostComplete === true;
  const effectiveRatePartial = !effectiveRateComplete && fullEffectiveRate == null && knownEffectiveRate != null;
  const effectiveRate = ease("effectiveRate", effectiveRatePartial ? knownEffectiveRate : fullEffectiveRate);
  const cache = ease("cacheReuse", totals.cacheReusePct);
  const sessions = ease("sessions", totals.sessions);
  const records = ease("records", totals.records);
  const mean = ease("meanSession", totals.meanSessionValue);
  const mixed = (data.coverage?.derivedProviders || []).length > 0 || (finite(published) || 0) > 0;
  const marker = partial ? "≈" : (totals.trackedValueMarker ?? (tracked == null ? "" : mixed ? "≈" : ""));
  $("tracked-marker").textContent = marker;
  $("tracked-value").classList.remove("burnrate-skeleton");
  $("tracked-value").dataset.ease = "tracked";
  $("tracked-value").dataset.fmt = "usd";
  $("tracked-value").textContent = usd(tracked);
  $("tracked-chip").textContent = partial ? "partial" : mixed ? "mixed" : tracked == null ? "unknown" : "exact";
  $("tracked-chip").className = partial || mixed || tracked == null ? "" : "good";
  const trackedCard = $("tracked-chip").closest("article");
  if (trackedCard) trackedCard.dataset.tone = partial ? "partial" : mixed ? "mixed" : tracked == null ? "unknown" : "exact";
  const unpricedRows = unpricedModels.reduce((sum, item) => sum + (finite(item.records) || 0), 0);
  const unpricedNames = unpricedModels.map((item) => item.modelKey).join(", ");
  $("tracked-note").dataset.partial = partial ? "1" : "";
  $("tracked-note").textContent = partial
    ? `${usd(priced)} priced + ${usd(published)} published-rate · ${number(unpricedRows)} rows unpriced: ${unpricedNames}`
    : tracked == null
      ? "Tracked value unavailable: no priced or published-rate rows in this window"
      : `${usd(priced)} priced + ${usd(published)} published-rate`;
  $("measured-tokens").classList.remove("burnrate-skeleton");
  $("measured-tokens").dataset.ease = "tokens";
  $("measured-tokens").dataset.fmt = "tokens";
  $("measured-tokens").textContent = tokens(tokenCount);
  const outputTok = finite(totals.outputTokens) ?? finite(mixItem(mix, "output")?.tokens);
  $("tokens-note").textContent = `${number(records)} records · ${tokens(outputTok)} output`;
  $("effective-rate").classList.remove("burnrate-skeleton");
  $("effective-rate").dataset.ease = "effectiveRate";
  $("effective-rate").dataset.fmt = "usd";
  $("effective-rate").textContent = usd(effectiveRate);
  $("effective-rate-marker").textContent = effectiveRate == null ? "" : "≈";
  $("effective-rate-chip").textContent = effectiveRatePartial ? "partial" : effectiveRate == null ? "unavailable" : "API rates";
  $("effective-rate-chip").className = effectiveRatePartial || effectiveRate == null ? "" : "good";
  const rateTokens = finite(totals.effectiveCostPricedTokens);
  const rateCoverage = finite(totals.effectiveCostCoveragePct);
  $("effective-rate-note").textContent = effectiveRate == null
    ? "No reliably priced tokens in this window"
    : effectiveRatePartial
      ? `Blended across ${tokens(rateTokens)} priced tokens · ${pct(rateCoverage)} coverage`
      : `Blended from this window’s measured input, output and cache token mix`;
  $("cache-reuse").classList.remove("burnrate-skeleton");
  $("cache-reuse").dataset.ease = "cacheReuse";
  $("cache-reuse").dataset.fmt = "pct";
  $("cache-reuse").textContent = pct(cache);
  $("cache-reuse").style.color = "";
  const cachedTok = finite(totals.cachedInputTokens) ?? finite(mixItem(mix, "cached_input")?.tokens);
  const freshTok = finite(totals.freshInputTokens) ?? finite(mixItem(mix, "fresh_input")?.tokens);
  if (cachedTok == null || freshTok == null) {
    ease("cachedTok", null);
    ease("inputTok", null);
    $("cache-note").textContent = "Cache reuse withheld until cached and fresh input are both present";
  } else {
    $("cache-note").textContent = `${tokens(ease("cachedTok", cachedTok))} cached of ${tokens(ease("inputTok", cachedTok + freshTok))} input`;
  }
  $("sessions-value").classList.remove("burnrate-skeleton");
  $("sessions-value").dataset.ease = "sessions";
  $("sessions-value").dataset.fmt = "number";
  $("sessions-value").textContent = number(sessions);
  $("sessions-note").textContent = mean == null ? "Mean session value unknown" : `≈${usd(mean)} average tracked value`;
}

function renderChart(data) {
  const series = data.series || [];
  const tools = data.tools || [];
  const totals = data.totals || {};
  if (!series.length) {
    $("value-gutter").innerHTML = "";
    delete $("value-gutter").dataset.scale;
    $("stacked-plot").innerHTML = "";
    $("chart-hit-targets").innerHTML = "";
    $("chart-axis").innerHTML = "";
    setEmpty($("legend-cards"), `<p class="panel-empty">Token activity will appear here once usage events exist in this window.</p>`);
    $("hover-label").textContent = "Window total";
    $("hover-value").textContent = unknown;
    $("chart-table-body").innerHTML = "";
    $("chart-tooltip").hidden = true;
    return;
  }
  let maxBucket = 0.01;
  let anchorKey = null;
  series.forEach((item, i) => {
    const total = finite(item.total);
    if (total != null && total > maxBucket) {
      maxBucket = total;
      anchorKey = bucketKeyOf(item, i);
    }
  });
  const presentKeys = new Set(series.map(bucketKeyOf));
  const scale = stableScale(state.chartScales, state.window, niceMax(maxBucket), anchorKey, presentKeys);
  renderGutter($("value-gutter"), scale);
  const index = state.pinned ?? state.hover;
  const plot = $("stacked-plot");
  plot.hidden = state.mode !== "stacked";
  reconcileSegmentColumns(plot, series, bucket => (
    tools.map(tool => {
      const value = finite(bucket.byTool?.[tool.key]);
      if (value == null) return {key:tool.key, height:null, color:colorFor(tool.key)};
      const h = Math.max(value ? 0.35 : 0, (value / scale) * PLOT);
      return {key:tool.key, height:h, color:colorFor(tool.key)};
    })
  ), index);
  let cumulative = 0;
  const cum = series.map(item => {
    const n = finite(item.total);
    if (n == null) return cumulative;
    cumulative += n;
    return cumulative;
  });
  const cumMax = cum[cum.length - 1] || 1;
  const points = cum.map((value, i) => {
    const x = (i / Math.max(1, series.length - 1)) * 600;
    const y = 2.22 * (HEADROOM + (1 - value / cumMax) * PLOT);
    return [x, y];
  });
  const line = points.map((point, i) => `${i ? "L" : "M"}${point[0].toFixed(1)} ${point[1].toFixed(1)}`).join(" ");
  const svg = $("cumulative-plot");
  svg.toggleAttribute("hidden", state.mode !== "cumulative");
  setAttr($("cumulative-line"), "d", line);
  setAttr($("cumulative-area"), "d", line + " L600 222 L0 222 Z");
  const last = points[points.length - 1];
  setAttr($("cumulative-end"), "cx", last[0].toFixed(1));
  setAttr($("cumulative-end"), "cy", last[1].toFixed(1));
  renderHitTargets($("chart-hit-targets"), series, bucket => bucket.total, state.pinned);
  renderAxis($("chart-axis"), series);
  const legend = $("legend-cards");
  if (!tools.length) {
    setEmpty(legend, `<p class="panel-empty">No tools in this window.</p>`);
  } else {
    reconcileChildren(legend, tools, tool => tool.key, () => {
      const node = nodeFrom(`<button type="button" class="legend-card">
        <span><i class="logo-slot"></i></span>
        <strong></strong>
        <em><span class="legend-cache"></span> cache · <span class="legend-value"></span></em>
      </button>`);
      node.addEventListener("click", () => loadEntity("tool", node.dataset.key));
      return node;
    }, (node, tool) => {
      node.dataset.key = tool.key;
      const value = ease("toolTok-" + tool.key, tool.tokens);
      const windowTokens = tools.reduce((sum, item) => sum + (finite(item.tokens) || 0), 0);
      const rawShare = windowTokens ? ((finite(tool.tokens) || 0) / windowTokens) * 100 : 0;
      const share = rawShare > 0 ? Math.max(8, Math.round(rawShare)) : 0;
      node.style.setProperty("--share", share + "%");
      node.style.setProperty("--tool", colorFor(tool.key));
      const spend = ease("toolVal-" + tool.key, tool.value);
      const cache = ease("toolCache-" + tool.key, tool.cachePct);
      const marker = tool.valueMarker ?? (tool.isExact === false ? "≈" : "");
      const swatch = node.querySelector("span > i");
      setLogo(swatch, logoKey(tool.key, tool.name));
      setStyle(swatch, "color", colorFor(tool.key));
      setTextAfter(swatch, tool.name || tool.key);
      bindEase(node.querySelector("strong"), "toolTok-" + tool.key, "tokens", tokens(value));
      bindEase(node.querySelector(".legend-cache"), "toolCache-" + tool.key, "pct", pct(cache));
      bindEase(node.querySelector(".legend-value"), "toolVal-" + tool.key, tool.isExact === false ? "approxUsd" : "usd",
        `${marker && spend != null && tool.isExact !== false ? marker : ""}${markedUsd(spend, tool.isExact)}`);
    });
  }
  reconcileChildren($("chart-table-body"), series, bucketKeyOf, () => nodeFrom(`<tr><td></td><td></td><td></td></tr>`), (node, bucket) => {
    setText(node.children[0], bucket.label);
    setText(node.children[1], bucket.total == null ? unknown : String(bucket.total));
    setText(node.children[2], JSON.stringify(bucket.byTool || {}));
  });
  const totalTokens = ease("hoverTotal", totals.tokens);
  $("hover-label").textContent = index == null ? `${windowLabel(data.window?.key || state.window)} total` : series[index].label;
  $("hover-value").textContent = index == null ? tokens(totalTokens) : tokens(series[index].total);
  $("hover-value").style.color = index == null ? "var(--text)" : "var(--accent)";
  renderChartHover(data);
}

function renderChartHover(data) {
  const series = data.series || [];
  const tools = data.tools || [];
  const index = state.pinned ?? state.hover;
  const tooltip = $("chart-tooltip");
  const bucket = index == null ? null : series[index];
  document.querySelectorAll("#stacked-plot .day-column").forEach((column, i) => column.classList.toggle("dim", index != null && index !== i));
  document.querySelectorAll("#chart-hit-targets .hit-target").forEach((button, i) => button.classList.toggle("active", state.pinned === i || state.hover === i && state.pinned == null));
  if (!bucket) {
    tooltip.hidden = true;
    $("chart-live").textContent = "";
    return;
  }
  tooltip.hidden = false;
  const left = ((index + 0.5) / series.length) * 100;
  tooltip.style.left = `${left}%`;
  tooltip.style.transform = index < series.length * 0.2 ? "translateX(-12%)" : index > series.length * 0.8 ? "translateX(-88%)" : "translateX(-50%)";
  const rows = tools.map(tool => {
    const value = finite(bucket.byTool?.[tool.key]);
    return value == null ? "" : `<div class="tooltip-row"><span><i class="logo-slot" style="color:${colorFor(tool.key)}">${logoSvg(logoKey(tool.key, tool.name))}</i>${esc(tool.name)}</span><strong>${tokens(value)}</strong></div>`;
  }).join("");
  tooltip.innerHTML = `<div class="tooltip-head"><span>${esc(bucket.label)}</span><strong>${tokens(bucket.total)}</strong></div><div class="tooltip-rows">${rows}</div><div class="tooltip-hint">${state.pinned != null ? "Pinned · tap again or press Escape to release" : "Tap to pin"}</div>`;
  $("chart-live").textContent = `${bucket.label}, ${tokens(bucket.total)}`;
  $("hover-label").textContent = bucket.label;
  $("hover-value").textContent = tokens(bucket.total);
  $("hover-value").style.color = "var(--accent)";
}

function renderWaste(data) {
  const waste = data.waste || {};
  const perDay = ease("wasteDay", waste.perDay);
  const perMonth = waste.perMonth == null ? null : ease("wasteMonth", waste.perMonth);
  $("waste-headline").dataset.ease = "wasteDay";
  $("waste-headline").dataset.fmt = "usd";
  setText($("waste-headline"), usd(perDay));
  setText($("waste-headline-note"), perDay == null ? "unavailable" : `per day · ${usd(perMonth)}/mo`);
  setText($("waste-marker"), perDay == null ? "" : "≈");
  const items = waste.items || [];
  const root = $("waste-items");
  if (!items.length) {
    setEmpty(root, `<p class="panel-empty">No recoverable leaks in this window.</p>`);
  } else {
    reconcileChildren(root, items, (item, i) => item.key || i, () => nodeFrom(`<div class="waste-item"><div><strong></strong><small></small><span class="fix-chip"><i></i></span></div><span class="leak"></span></div>`), (node, item, i) => {
      const leak = ease("leak" + i, item.perDay);
      const text = leak == null ? unknown : "≈" + usd(leak) + "/day";
      setText(node.querySelector("strong"), item.title || "");
      setText(node.querySelector("small"), item.detail || "");
      setTextAfter(node.querySelector(".fix-chip > i"), item.fix || "");
      bindEase(node.querySelector(".leak"), leak == null ? "" : "leak" + i, "approxUsdDay", text);
    });
  }
  const saved = ease("cacheSaved", data.cacheSavings);
  $("cache-savings").dataset.ease = "cacheSaved";
  $("cache-savings").dataset.fmt = "approxUsd";
  setText($("cache-savings"), saved == null ? unknown : "≈" + usd(saved));
}

function unpricedCause(models) {
  const names = (Array.isArray(models) ? models : []).map(item => item && (item.name || item.modelKey)).filter(Boolean);
  if (!names.length) return "";
  const shown = names.slice(0, 3);
  const extra = names.length - shown.length;
  const list = names.length === 1
    ? shown[0]
    : extra
      ? `${shown.join(", ")}, and ${extra} more`
      : `${shown.slice(0, -1).join(", ")}${shown.length > 2 ? "," : ""} and ${shown[shown.length - 1]}`;
  return `${list} ${names.length === 1 ? "has" : "have"} no published rate, so this is a lower bound.`;
}

function forecastNote(data) {
  const projected = data?.projected || {};
  const method = projected.method || "Month-to-date reference usage versus configured accrual over the same elapsed days. A comparison at published rates, not a bill.";
  if (projected.multipleBasis !== "lower_bound") return method;
  const cause = unpricedCause(data?.totals?.unpricedModels);
  return cause ? `${method} ${cause}` : method;
}

function renderForecast(data) {
  const projected = data.projected || {};
  const value = ease("projected", projected.value);
  const plan = ease("planCost", projected.planCost);
  $("forecast-value").dataset.ease = "projected";
  $("forecast-value").dataset.fmt = "usd";
  $("forecast-value").textContent = usd(value);
  const multiple = finite(projected.multiple);
  const multipleBasis = projected.multipleBasis || (multiple == null ? "unavailable" : "ratio");
  $("projected-marker").textContent = value == null ? "" : (multipleBasis === "lower_bound" ? "≥" : "≈");
  $("forecast-delta").textContent = multiple == null ? "" : `${multiple.toFixed(2)}× accrued (same period)${multipleBasis === "lower_bound" ? " · lower bound" : ""}`;
  $("forecast-note").textContent = forecastNote(data);
  const denom = (finite(value) || 0) + (finite(plan) || 0);
  $("pace-bar").style.width = denom ? `${((finite(value) || 0) / denom) * 100}%` : "0%";
  $("pace-label").textContent = `${multipleBasis === "lower_bound" ? "usage ≥ " : "usage "}${usd(value)}`;
  $("run-rate").textContent = `plans ${usd(plan)}`;
}

function renderModels(data) {
  const columns = [
    {key:"name", label:"Model", numeric:false},
    {key:"toolKey", label:"Tool", numeric:false},
    {key:"value", label:"Value", numeric:true},
    {key:"tokens", label:"Tokens", numeric:true},
    {key:"cachePct", label:"Cache", numeric:true},
    {key:"runs", label:"Runs", numeric:true}
  ];
  const head = $("model-table-head");
  const sortStamp = `${state.sortKey}:${state.sortDir}`;
  if (head.dataset.sort !== sortStamp) {
    head.dataset.sort = sortStamp;
    head.innerHTML = columns.map(column => `<button type="button" class="${column.numeric ? "numeric" : ""} ${state.sortKey === column.key ? "active" : ""}" data-key="${column.key}">${column.label}<span>${state.sortKey === column.key ? (state.sortDir < 0 ? "▼" : "▲") : ""}</span></button>`).join("");
    head.querySelectorAll("button").forEach(button => button.addEventListener("click", () => {
      const key = button.dataset.key;
      state.sortDir = state.sortKey === key ? -state.sortDir : -1;
      state.sortKey = key;
      renderModels(state.summary || data);
    }));
  }
  const tools = new Map((data.tools || []).map(tool => [tool.key, tool]));
  const rows = [...(data.models || [])].sort((a, b) => {
    const av = state.sortKey === "name" || state.sortKey === "toolKey" ? String(a[state.sortKey] || "") : finite(a[state.sortKey]) ?? -Infinity;
    const bv = state.sortKey === "name" || state.sortKey === "toolKey" ? String(b[state.sortKey] || "") : finite(b[state.sortKey]) ?? -Infinity;
    return av > bv ? state.sortDir : av < bv ? -state.sortDir : 0;
  });
  const leader = Math.max(1, ...rows.map(model => finite(model.value) || 0));
  const body = $("model-table-body");
  if (!rows.length) {
    setEmpty(body, `<p class="panel-empty">No models in this window.</p>`);
    return;
  }
  const entries = rows.flatMap(model => [{model, part:"row"}, {model, part:"card"}]);
  reconcileChildren(body, entries, entry => `${entry.part}:${entry.model.key}`, entry => {
    const key = entry.model.key;
    const node = entry.part === "row"
      ? nodeFrom(`<div class="model-row" tabindex="0" role="button">
          <strong><i class="logo-slot"></i></strong>
          <span class="model-tool"></span>
          <span class="numeric"><span class="model-value"></span></span>
          <span class="numeric model-tokens"></span>
          <span class="numeric model-cache"></span>
          <span class="numeric col-runs"></span>
          <span class="model-share"></span>
        </div>`)
      : nodeFrom(`<button type="button" class="model-card">
          <div class="model-card-top"><strong><i class="logo-slot"></i></strong><span class="model-tool"></span></div>
          <div class="model-figures">
            <span>Value<b class="card-value"></b></span>
            <span>Tokens<b class="card-tokens"></b></span>
            <span>Cache<b class="card-cache"></b></span>
          </div>
        </button>`);
    node.dataset.key = key;
    const open = () => loadEntity("model", node.dataset.key);
    node.addEventListener("click", open);
    if (entry.part === "row") {
      node.addEventListener("keydown", event => {
        if (event.key === "Enter" || event.key === " ") { event.preventDefault(); open(); }
      });
    }
    return node;
  }, (node, entry) => {
    const model = entry.model;
    const tool = tools.get(model.toolKey);
    const spend = ease("mv-" + model.key, model.value);
    const tok = ease("mt-" + model.key, model.tokens);
    const cache = model.cachePct;
    const unpriced = model.value == null;
    const valueText = unpriced ? unknown : markedUsd(spend, model.isExact);
    const cacheColor = cache == null ? "var(--dim)" : "var(--text)";
    const approx = model.valueMarker === "≈" || (model.isExact === false && !unpriced);
    node.dataset.key = model.key;
    const swatch = node.querySelector("strong > i");
    setLogo(swatch, logoKey(model.toolKey, model.name));
    setStyle(swatch, "color", colorFor(model.toolKey));
    setTextAfter(swatch, model.name || model.key);
    setText(node.querySelector(".model-tool"), tool?.name || model.toolKey);
    if (entry.part === "row") {
      const valueCell = node.querySelector(".model-value").parentElement;
      let marker = valueCell.querySelector(".marker");
      if (approx && !marker) {
        marker = nodeFrom(`<span class="marker">≈</span>`);
        valueCell.insertBefore(marker, valueCell.firstChild);
      } else if (!approx && marker) {
        marker.remove();
      }
      bindEase(node.querySelector(".model-value"), unpriced ? "" : "mv-" + model.key, "usd", unpriced ? unknown : usd(spend));
      bindEase(node.querySelector(".model-tokens"), "mt-" + model.key, "tokens", tokens(tok));
      const cacheCell = node.querySelector(".model-cache");
      setText(cacheCell, pct(cache));
      setStyle(cacheCell, "color", cacheColor);
      setText(node.querySelector(".col-runs"), number(model.runs));
      const amount = finite(model.value) || 0;
      const share = amount > 0 ? (amount >= leader ? 100 : Math.max(3, Math.round(amount / leader * 100))) : 0;
      node.style.setProperty("--share", share + "%");
      node.style.setProperty("--tool", colorFor(model.toolKey));
    } else {
      setText(node.querySelector(".card-value"), valueText);
      setText(node.querySelector(".card-tokens"), tokens(tok));
      setText(node.querySelector(".card-cache"), pct(cache));
    }
  });
}

function renderMix(data) {
  const rows = data.mix || [];
  setText($("mix-summary"), rows.length ? pct(rows.find(item => item.key === "cached_input")?.share) + " cached" : unknown);
  setText($("mix-caret"), state.mixOpen ? "▲" : "▼");
  setAttr($("mix-toggle"), "aria-expanded", String(state.mixOpen));
  $("mix-details").hidden = !state.mixOpen;
  reconcileChildren($("mix-bar"), rows, item => item.key, () => document.createElement("i"), (node, item, index, fresh) => {
    const share = finite(item.share);
    const eased = share == null ? ease("mixShare-" + item.key, null) : ease("mixShare-" + item.key, share);
    bindWidth(node, "mixShare-" + item.key, eased == null ? null : Math.max(eased, 0), fresh);
    setStyle(node, "background", MIX_COLORS[item.key] || "var(--t-other)");
  });
  reconcileChildren($("mix-rows"), rows, item => item.key, () => nodeFrom(`<div class="mix-row"><span><i></i></span><em></em><strong></strong></div>`), (node, item) => {
    const swatch = node.querySelector("span > i");
    setStyle(swatch, "background", MIX_COLORS[item.key] || "var(--t-other)");
    setTextAfter(swatch, item.label || MIX_LABELS[item.key] || item.key);
    bindEase(node.querySelector("em"), "mix-" + item.key, "tokens", tokens(ease("mix-" + item.key, item.tokens)));
    bindEase(node.querySelector("strong"), "mixShare-" + item.key, "pct", pct(item.share));
  });
  const output = rows.find(item => item.key === "output");
  const cached = rows.find(item => item.key === "cached_input");
  setText($("mix-note"), output
    ? `Output is ${pct(output.share)} of measured volume and the most expensive class per token. Cached input carries ${pct(cached?.share)} of volume at a fraction of the fresh rate.`
    : "Token structure unavailable until component shares are present.");
}

function renderSubs(data) {
  const rows = data.subscriptions || [];
  const supplied = rows.map(row => finite(row.monthlyEquivalent ?? row.amountUsd)).filter(value => value != null);
  const monthly = supplied.length ? supplied.reduce((sum, value) => sum + value, 0) : null;
  setText($("subs-summary"), `${rows.length} plan${rows.length === 1 ? "" : "s"} · ${monthly == null ? unknown : usd(monthly) + "/mo"}`);
  $("subs-details").hidden = false;
  setText($("subscription-total"), usd(monthly));
  setText($("nav-plans-total"), $("subscription-total").textContent);
  const root = $("subscription-rows");
  if (!rows.length) {
    setEmpty(root, `<p class="panel-empty">No configured subscriptions.</p>`);
    return;
  }
  reconcileChildren(root, rows, (row, i) => `${row.toolKey || ""}:${row.name || i}`, () => nodeFrom(`<div class="sub-row"><div><span><i class="logo-slot"></i></span><small></small></div><b></b></div>`), (node, row) => {
    const swatch = node.querySelector("span > i");
    setLogo(swatch, logoKey(row.toolKey, row.name, true));
    setStyle(swatch, "color", colorFor(row.toolKey));
    setTextAfter(swatch, row.name || "");
    setText(node.querySelector("small"), row.note || row.cadence || "");
    // Cadence-honest labels (C12): an annual plan states its annual amount;
    // the monthly equivalent stays in the note.
    const cadence = String(row.cadence || "");
    const suffix = cadence === "quarterly" ? "/3mo" : cadence === "annual" ? "/yr" : "/mo";
    setText(node.querySelector("b"), row.cadence && row.amountUsd != null
      ? usd(row.amountUsd) + suffix
      : (row.amountUsd == null ? "Free" : usd(row.amountUsd)));
    const equiv = finite(row.monthlyEquivalent ?? row.amountUsd) ?? 0;
    const share = monthly ? (equiv > 0 ? Math.max(4, Math.round(equiv / monthly * 100)) : 0) : 0;
    node.style.setProperty("--share", share + "%");
    node.style.setProperty("--tool", colorFor(row.toolKey));
  });
}

function heatValueLabel(value) {
  // N03: cells carry reference usage USD, formatted as money, not tokens.
  const n = finite(value);
  if (n == null) return unknown;
  return usd(n) + " reference usage";
}
function moveHeatFocus(cells, currentIndex, delta) {
  const dayStep = 24;
  let day = Math.floor(currentIndex / dayStep);
  let hour = currentIndex % dayStep;
  hour = Math.min(23, Math.max(0, hour + (delta === -1 || delta === 1 ? delta : 0)));
  if (delta === -dayStep || delta === dayStep) day = Math.min(6, Math.max(0, day + delta / dayStep));
  const next = Math.min(cells.length - 1, Math.max(0, day * dayStep + hour));
  const target = cells[next];
  if (target) {
    state.heatFocus = next;
    cells.forEach(cell => cell.tabIndex = -1);
    target.tabIndex = 0;
    target.focus();
    applyHeat(target);
  }
}
function rateLabel(value) {
  const n = finite(value);
  if (n == null) return unknown;
  if (n >= 100) return "$" + n.toFixed(0);
  if (n >= 10) return "$" + n.toFixed(2);
  return "$" + n.toFixed(3);
}

function renderUnitPrice(data) {
  const board = $("price-board");
  if (!board) return;
  const tools = new Map((data.tools || []).map(tool => [tool.key, tool]));
  const rows = (data.models || []).flatMap(model => {
    const value = finite(model.value);
    const count = finite(model.tokens);
    if (value == null || !count) return [];
    const exact = model.isExact !== false && model.valueMarker !== "≈";
    return [{
      key: model.key,
      name: model.name || model.key,
      tool: tools.get(model.toolKey)?.name || model.toolKey || "",
      rate: value / count * 1e6,
      tokens: count,
      exact,
      color: colorFor(model.toolKey),
      logo: logoKey(model.toolKey, model.name)
    }];
  }).sort((a, b) => b.rate - a.rate);
  const solid = rows.filter(row => row.tokens >= 10e6);
  const ranked = solid.length >= 2 ? solid : rows;
  board.replaceChildren();
  if (!ranked.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No priced tokens in this window.";
    board.appendChild(empty);
    return;
  }
  const leader = ranked[0];
  const floor = ranked[ranked.length - 1];
  const times = floor.rate > 0 && rows.length > 1 ? leader.rate / floor.rate : null;
  const lead = document.createElement("div");
  lead.className = "price-lead";
  const kicker = document.createElement("span");
  kicker.className = "price-kicker";
  kicker.textContent = "Steepest model";
  const fig = document.createElement("strong");
  fig.className = "price-fig";
  if (!leader.exact) {
    const marker = document.createElement("span");
    marker.className = "marker";
    marker.textContent = "≈";
    fig.appendChild(marker);
  }
  fig.appendChild(document.createTextNode(rateLabel(leader.rate)));
  const name = document.createElement("span");
  name.className = "price-name";
  name.textContent = leader.name;
  const meta = document.createElement("span");
  meta.className = "price-meta";
  meta.textContent = `per million tokens · ${tokens(leader.tokens)} · ${leader.tool}`;
  lead.append(kicker, fig, name, meta);
  if (times != null && times > 1.05) {
    const compare = document.createElement("span");
    compare.className = "price-times";
    compare.textContent = `${times.toFixed(1)}× ${floor.name}`;
    lead.appendChild(compare);
  }
  if (ranked.length !== rows.length) {
    const omitted = document.createElement("span");
    omitted.className = "price-meta";
    omitted.textContent = "Models under 10M tokens are left out.";
    lead.appendChild(omitted);
  }
  const ladder = document.createElement("div");
  ladder.className = "price-ladder";
  ranked.forEach(row => {
    const rung = document.createElement("div");
    rung.className = "price-rung";
    const who = document.createElement("span");
    who.className = "price-who";
    const swatch = document.createElement("i");
    swatch.className = "logo-slot";
    swatch.style.color = row.color;
    setLogo(swatch, row.logo);
    const label = document.createElement("b");
    label.textContent = row.name;
    const tool = document.createElement("small");
    tool.textContent = row.tool;
    who.append(swatch, label, tool);
    const amount = document.createElement("strong");
    amount.textContent = (row.exact ? "" : "≈") + rateLabel(row.rate);
    const bar = document.createElement("span");
    bar.className = "price-bar";
    bar.style.setProperty("--share", `${Math.max(4, Math.round(row.rate / leader.rate * 100))}%`);
    bar.style.setProperty("--tool", row.color);
    rung.append(who, amount, bar);
    ladder.appendChild(rung);
  });
  board.append(lead, ladder);
}

function renderHeat(data) {
  const cells = data.heatmap || [];
  const short = heatmapIsFallback(data);
  $("heat-warning").hidden = !short;
  $("heatmap").classList.toggle("dim", short);
  const map = new Map();
  let max = 0.01;
  cells.forEach(cell => {
    const day = typeof cell.weekday === "string"
      ? Math.max(0, DAYS.findIndex(label => label.toLowerCase() === String(cell.weekday).slice(0, 3).toLowerCase()))
      : ((Number(cell.weekday) % 7) + 7) % 7;
    map.set(`${day}:${Number(cell.hour)}`, cell);
    const heat = finite(cell.value);
    if (heat != null) max = Math.max(max, heat);
  });
  const byHour = Array.from({length: 24}, () => 0);
  let total = 0;
  map.forEach(cell => {
    const amount = finite(cell.value);
    const hour = Number(cell.hour);
    if (amount == null || hour < 0 || hour > 23) return;
    total += amount;
    byHour[hour] += amount;
  });
  let peakHour = 0;
  byHour.forEach((amount, hour) => { if (amount > byHour[peakHour]) peakHour = hour; });
  const peakShare = total > 0 ? (byHour[peakHour] / total) * 100 : null;
  const clock = hour => String(hour).padStart(2, "0") + ":00";
  let band = null;
  if (total > 0) {
    const target = total * 0.5;
    for (let start = 0; start < 24; start++) {
      let running = 0;
      for (let len = 1; len <= 24; len++) {
        running += byHour[(start + len - 1) % 24];
        if (running + 1e-9 >= target) {
          if (!band || len < band.len) band = {start, len};
          break;
        }
      }
    }
  }
  const shareEl = $("heat-share");
  const spanEl = $("heat-span");
  if (shareEl) setText(shareEl, peakShare == null ? unknown : pct(peakShare));
  if (spanEl) {
    if (peakShare == null || !band) setText(spanEl, "No reference usage in this pattern.");
    else if (band.len >= 24) setText(spanEl, `Spread across the day. ${clock(peakHour)} is the peak hour.`);
    else if (band.len <= 1) setText(spanEl, `Half of this pattern sits at ${clock(band.start)}. Peak hour ${clock(peakHour)}.`);
    else setText(spanEl, `Half of this pattern sits in ${clock(band.start)}–${clock((band.start + band.len - 1) % 24)}. Peak hour ${clock(peakHour)}.`);
  }
  const applyHeat = button => {
    state.heatCell = button;
    setText($("heat-label"), button.dataset.label);
    // Heat cells are reference usage USD (N03) with the unit spoken and shown.
    setText($("heat-value"), button.dataset.value === "" ? unknown : usd(button.dataset.value));
    $("heat-value").style.color = short ? "var(--muted)" : "var(--text)";
  };
  const hours = Array.from({length:24}, (_, hour) => hour);
  reconcileChildren($("heatmap"), DAYS, (label, day) => day, label => nodeFrom(`<div class="heat-row"><span>${esc(label)}</span><div class="heat-cells"></div></div>`), (rowNode, label, day) => {
    reconcileChildren(rowNode.querySelector(".heat-cells"), hours, hour => hour, hour => {
      const button = nodeFrom(`<button type="button" class="heat-cell" tabindex="-1"></button>`);
      button.dataset.label = `${label} ${String(hour).padStart(2, "0")}:00`;
      button.addEventListener("pointerenter", () => applyHeat(button));
      button.addEventListener("click", () => applyHeat(button));
      return button;
    }, (button, hour) => {
      const cell = map.get(`${day}:${hour}`);
      const value = cell ? finite(cell.value) : null;
      // Ember ramp: the share of the accent mixed into the sunken cell colour.
      // A square-root curve keeps quiet hours distinguishable from empty ones.
      const alpha = value == null ? "0" : (0.14 + Math.sqrt(Math.max(0, value) / max) * 0.86).toFixed(3);
      const shown = value == null ? unknown : heatValueLabel(value);
      const stored = value == null ? "" : String(value);
      if (button.dataset.value !== stored) button.dataset.value = stored;
      setAttr(button, "aria-label", `${button.dataset.label} ${shown}`);
      setAttr(button, "title", `${button.dataset.label} · ${shown}`);
      if (button.style.getPropertyValue("--heat-a") !== alpha) button.style.setProperty("--heat-a", alpha);
      button.classList.toggle("is-peak", Number(hour) === peakHour && total > 0);
    });
  });
  reconcileChildren($("heat-table-body"), cells, cell => `${cell.weekday}:${cell.hour}`, () => nodeFrom(`<tr><td></td><td></td><td></td></tr>`), (node, cell) => {
    setText(node.children[0], String(cell.weekday));
    setText(node.children[1], String(cell.hour));
    setText(node.children[2], cell.value == null ? unknown : String(cell.value));
  });
  // Roving tabindex (C14): the grid is one tab stop; arrows move within it.
  const gridCells = [...$("heatmap").querySelectorAll("button.heat-cell")];
  gridCells.forEach((cell, index) => {
    cell.tabIndex = index === state.heatFocus ? 0 : -1;
    cell.addEventListener("keydown", event => {
      const step = {ArrowLeft:-1, ArrowRight:1, ArrowUp:-24, ArrowDown:24}[event.key];
      if (!step) return;
      event.preventDefault();
      moveHeatFocus(gridCells, state.heatFocus, step);
    });
  });
  const focused = gridCells[state.heatFocus];
  if (focused && state.heatCell && document.activeElement === state.heatCell) {
    applyHeat(state.heatCell);
  } else if (state.heatCell && state.heatCell.isConnected) {
    applyHeat(state.heatCell);
  } else {
    let peak = null;
    gridCells.forEach((button, index) => {
      if (button.dataset.value === "") return;
      if (!peak || Number(button.dataset.value) > Number(peak.dataset.value)) {
        peak = button;
        state.heatFocus = index;
      }
    });
    if (peak) gridCells[state.heatFocus].tabIndex = 0;
    setText($("heat-label"), peak?.dataset.label || "Peak window");
    setText($("heat-value"), peak ? usd(peak.dataset.value) : unknown);
    $("heat-value").style.color = short ? "var(--muted)" : "var(--text)";
  }
  setText($("heat-unit"), data.heatmapUnit || "reference usage (USD at documented rates)");
}

function seenResetAlerts() {
  try { return JSON.parse(localStorage.getItem(RESET_ALERTS_SEEN) || "{}"); }
  catch { return {}; }
}
function notifyResetAlerts(alerts) {
  if (typeof Notification === "undefined" || Notification.permission !== "granted") return;
  const seen = seenResetAlerts();
  let changed = false;
  for (const alert of alerts) {
    if (!alert || !alert.id || seen[alert.id]) continue;
    try {
      const note = new Notification(alert.title || "Capacity alert", {body: alert.body || "", tag: alert.id});
      note.onclick = () => { window.focus(); note.close(); };
    } catch {}
    seen[alert.id] = Date.now();
    changed = true;
  }
  if (!changed) return;
  const ids = Object.keys(seen);
  for (const id of ids.slice(0, Math.max(0, ids.length - 100))) delete seen[id];
  localStorage.setItem(RESET_ALERTS_SEEN, JSON.stringify(seen));
}
function renderResetAlerts(data) {
  const banner = $("reset-alert-banner");
  const list = $("reset-alert-list");
  if (!banner || !list) return;
  const alerts = (data.capacityForecast && data.capacityForecast.alerts) || [];
  banner.hidden = !alerts.length;
  const allow = $("reset-alert-notify");
  if (allow) allow.hidden = typeof Notification === "undefined" || Notification.permission !== "default";
  reconcileChildren(list, alerts, alert => alert.id, () => nodeFrom(`<article class="reset-alert-item"><div><strong></strong><p class="reset-alert-detail"></p><p class="reset-alert-suggestion"></p></div><button type="button" data-dismiss-alert>Dismiss</button></article>`), (node, alert) => {
    setText(node.querySelector("strong"), alert.title || "");
    const eta = etaFromReset(alert.resetsAt);
    const until = eta.value === unknown ? "" : `${eta.value} ${eta.label}`;
    setText(node.querySelector(".reset-alert-detail"), [alert.limitLabel, finite(alert.pct) == null ? "" : `${pct(alert.pct)} used`, until, alert.line].filter(Boolean).join(" · "));
    setText(node.querySelector(".reset-alert-suggestion"), alert.suggestion || "");
    const button = node.querySelector("[data-dismiss-alert]");
    button.dataset.dismissAlert = alert.id;
  });
  notifyResetAlerts(alerts);
  const form = $("capacity-alert-form");
  const preferences = data.capacityForecast && data.capacityForecast.preferences;
  if (!form || !preferences || form.contains(document.activeElement)) return;
  $("capacity-alert-threshold").value = preferences.threshold;
  $("capacity-alert-escalate").checked = Boolean(preferences.escalate);
  $("capacity-alert-quiet-start").value = preferences.quietStart || "22:00";
  $("capacity-alert-quiet-end").value = preferences.quietEnd || "07:00";
  const providers = (data.capacity || []).filter(provider => !provider.isPayg && provider.providerKey);
  const overrides = $("capacity-alert-overrides");
  reconcileChildren(overrides, providers, provider => provider.providerKey, () => nodeFrom(`<label><span></span><input type="number" min="1" max="99" step="1" placeholder="Default"></label>`), (node, provider) => {
    setText(node.querySelector("span"), provider.providerName || provider.providerKey);
    const input = node.querySelector("input");
    input.dataset.provider = provider.providerKey;
    const value = preferences.overrides && preferences.overrides[provider.providerKey];
    if (document.activeElement !== input) input.value = value == null ? "" : value;
  });
}
async function postJson(url, body) {
  const response = await fetch(url, {
    method: "POST",
    cache: "no-store",
    headers: {"Content-Type": "application/json"},
    body: JSON.stringify(body),
  });
  if (!response.ok) {
    let detail = "";
    try { detail = (await response.json()).error || ""; } catch {}
    throw new Error(detail || `${response.status}`);
  }
  return response.json();
}
function bindResetAlerts() {
  const form = $("capacity-alert-form");
  const banner = $("reset-alert-banner");
  if (form) form.addEventListener("submit", async event => {
    event.preventDefault();
    const status = $("capacity-alert-status");
    const overrides = {};
    form.querySelectorAll("#capacity-alert-overrides input").forEach(input => {
      if (input.value !== "") overrides[input.dataset.provider] = Number(input.value);
    });
    try {
      const saved = await postJson("/api/capacity/forecast-preferences", {
        threshold: Number($("capacity-alert-threshold").value),
        escalate: $("capacity-alert-escalate").checked,
        quietStart: $("capacity-alert-quiet-start").value,
        quietEnd: $("capacity-alert-quiet-end").value,
        overrides,
      });
      if (state.summary) state.summary.capacityForecast = saved;
      if (status) status.textContent = "Saved";
    } catch (error) {
      if (status) status.textContent = error.message || "Could not save";
    }
  });
  if (!banner) return;
  banner.addEventListener("click", async event => {
    const button = event.target.closest("[data-dismiss-alert]");
    if (button && button.dataset.dismissAlert) {
      try {
        const saved = await postJson("/api/capacity/forecast-alerts/dismiss", {id: button.dataset.dismissAlert});
        if (state.summary) {
          state.summary.capacityForecast = {...(state.summary.capacityForecast || {}), ...saved};
          renderResetAlerts(state.summary);
        }
      } catch (error) {
        const status = $("capacity-alert-status");
        if (status) status.textContent = error.message || "Could not dismiss";
      }
      return;
    }
    if (event.target.id !== "reset-alert-notify" || typeof Notification === "undefined") return;
    const permission = await Notification.requestPermission();
    if (permission === "granted" && state.summary) renderResetAlerts(state.summary);
    else if ($("reset-alert-notify")) $("reset-alert-notify").hidden = permission !== "default";
  });
}

function renderMachines(data) {
  const machines = (data && data.machines) || (state.summary && state.summary.machines) || [];
  const row = $("range-switch")?.parentElement;
  if (!row) return;
  let root = $("machine-switch");
  if (machines.length <= 1) {
    if (root) root.remove();
    return;
  }
  if (!root) {
    root = document.createElement("div");
    root.id = "machine-switch";
    root.className = "mode-switch";
    root.setAttribute("role", "radiogroup");
    root.setAttribute("aria-label", "Computer filter");
    root.addEventListener("click", event => {
      const button = event.target.closest("[data-machine]");
      if (button) changeMachine(button.dataset.machine);
    });
    row.insertBefore(root, $("range-switch"));
  }
  const items = [{id: "all", name: "All computers"}, ...machines];
  reconcileChildren(root, items, item => item.id, () => nodeFrom(`<button type="button" role="radio" data-machine=""></button>`), (node, item) => {
    const on = item.id === (state.machine || "all");
    node.dataset.machine = item.id;
    node.classList.toggle("active", on);
    node.setAttribute("aria-checked", String(on));
    setText(node, item.name || item.id);
  });
}
function changeMachine(machineId) {
  const next = String(machineId || "all").trim() || "all";
  if (next === state.machine) return;
  state.machine = next;
  state.hover = null;
  state.pinned = null;
  state.dHover = null;
  state.dPinned = null;
  state.chartScales = Object.create(null);
  state.detailScales = Object.create(null);
  setQuery();
  renderMachines(state.summary);
  if (state.view === "detail" && state.entity) loadEntity(state.entity.kind, state.entity.key);
  else loadSummary(false);
}
function renderToolsFilter(data) {
  const row = $("range-switch")?.parentElement;
  if (!row) return;
  if ((!state.tool || state.tool === "all") && Array.isArray(data?.tools) && data.tools.length) {
    state.knownTools = data.tools.map(t => ({key: t.key, name: t.name || t.key}));
  }
  const byKey = new Map();
  for (const item of state.knownTools || []) if (item.key) byKey.set(item.key, item.name || item.key);
  for (const item of data?.tools || []) if (item.key) byKey.set(item.key, item.name || item.key);
  for (const cap of data?.capacity || []) {
    if (cap.providerKey && cap.providerKey !== "cursor" && !byKey.has(cap.providerKey)) {
      byKey.set(cap.providerKey, cap.providerName || cap.providerKey);
    }
  }
  let root = $("tool-switch");
  if (byKey.size <= 1 && (!state.tool || state.tool === "all")) {
    if (root) root.remove();
    return;
  }
  if (!root) {
    root = document.createElement("div");
    root.id = "tool-switch";
    root.className = "mode-switch";
    root.setAttribute("role", "radiogroup");
    root.setAttribute("aria-label", "Tool filter");
    root.addEventListener("click", event => {
      const drill = event.target.closest("[data-drill-tool]");
      if (drill) { loadEntity("tool", drill.dataset.drillTool); return; }
      const button = event.target.closest("[data-tool]");
      if (button) changeTool(button.dataset.tool);
    });
    row.insertBefore(root, $("range-switch"));
  }
  const items = [{key: "all", name: "All tools"}, ...[...byKey.entries()].map(([key, name]) => ({key, name}))];
  if (state.tool && state.tool !== "all") {
    items.push({key: "__drill__:" + state.tool, name: "Detail →", drill: state.tool});
  }
  reconcileChildren(root, items, item => item.key, () => nodeFrom(`<button type="button" role="radio" data-tool=""></button>`), (node, item) => {
    if (item.drill) {
      node.removeAttribute("role");
      node.removeAttribute("aria-checked");
      delete node.dataset.tool;
      node.dataset.drillTool = item.drill;
      node.classList.remove("active");
      setText(node, item.name);
      return;
    }
    delete node.dataset.drillTool;
    node.setAttribute("role", "radio");
    const on = item.key === (state.tool || "all");
    node.dataset.tool = item.key;
    node.classList.toggle("active", on);
    node.setAttribute("aria-checked", String(on));
    if (item.key === "all") {
      setText(node, item.name);
      delete node.dataset.logo;
    } else {
      if (!node.querySelector(":scope > span")) {
        node.replaceChildren(document.createElement("span"));
        delete node.dataset.logo;
      }
      setLogo(node, logoKey(item.key, item.name));
      setText(node.querySelector(":scope > span"), item.name || item.key);
    }
  });
}
function changeTool(toolKey) {
  const next = String(toolKey || "all").trim() || "all";
  if (next === state.tool) return;
  state.tool = next;
  state.hover = null;
  state.pinned = null;
  state.chartScales = Object.create(null);
  setQuery();
  renderToolsFilter(state.summary);
  loadSummary(false);
}
function connectLiveStream() {
  if (PROBE || typeof EventSource !== "function") return;
  let timer = null;
  try {
    const stream = new EventSource("/api/spend/stream");
    stream.addEventListener("update", () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => { timer = null; refreshCurrent(); }, 150);
    });
  } catch {}
}

window.openSidecarHud = async () => {
  const internals = window.__TAURI_INTERNALS__;
  if (internals && typeof internals.invoke === "function") {
    internals.invoke("toggle_hud");
    return;
  }
  try {
    const res = await fetch("/api/hud/native", {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify({action: "open"})
    });
    const data = await res.json();
    if (data && data.ok) return;
  } catch {}
  window.open("/hud", "burnrate-hud", "popup=yes,width=380,height=640,left=36,top=72");
};

function initTopbarChrome() {
  const settings = document.querySelector(".nav-settings");
  if (settings && !document.getElementById("nav-hud")) {
    const hudBtn = nodeFrom(`<button id="nav-hud" class="nav-chip" type="button" title="Toggle Always-on-Top Sidecar HUD"><i class="settings-dot good-dot" aria-hidden="true"></i><span>HUD</span></button>`);
    hudBtn.addEventListener("click", () => window.openSidecarHud());
    settings.insertBefore(hudBtn, settings.firstChild);
  }
  const themeBtn = $("nav-theme");
  if (themeBtn) {
    // The <head> script already applied the saved or OS theme; this only flips it.
    const label = () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      themeBtn.setAttribute("aria-label", `Switch to ${next} theme`);
      themeBtn.title = `Switch to ${next} theme`;
    };
    label();
    themeBtn.addEventListener("click", () => {
      const next = document.documentElement.dataset.theme === "dark" ? "light" : "dark";
      document.documentElement.dataset.theme = next;
      try { localStorage.setItem("burnrate:theme", next); } catch {}
      label();
    });
  }
}
initTopbarChrome();

window.openSetupWizard = () => {
  const internals = window.__TAURI_INTERNALS__;
  if (internals && typeof internals.invoke === "function") {
    internals.invoke("open_setup_wizard");
    return;
  }
  window.location.href = "/setup";
};

function renderMachineSettings(data) {
  const host = $("harness-content");
  if (!host) return;
  const machines = (data && data.machines) || (state.summary && state.summary.machines) || [];
  let section = $("machine-settings-section");
  if (!section) {
    section = nodeFrom(`<section class="harness-section" id="machine-settings-section"><header><p class="eyebrow">Connected computers (Account)</p><div class="harness-actions"><button id="open-setup-wizard-button" class="harness-action" type="button">Account &amp; Setup</button><button id="open-hud-button" class="harness-action" type="button">Sidecar HUD</button></div></header><p id="machine-pair-note" class="settings-helper mono">Log in to your account via the Setup Wizard on any computer to sync usage automatically through tk-server.</p><div id="machine-rows"></div></section>`);
    host.appendChild(section);
    section.querySelector("#open-setup-wizard-button").addEventListener("click", () => window.openSetupWizard());
    section.querySelector("#open-hud-button").addEventListener("click", () => window.openSidecarHud());
  }
  const rows = $("machine-rows");
  if (!rows) return;
  reconcileChildren(rows, machines, item => item.id, () => nodeFrom(`<article class="source-setting-row"><span class="harness-mark"><i class="settings-dot"></i></span><div class="source-setting-name"><strong></strong><small class="mono"></small></div><div class="source-setting-status"><b></b><small></small></div><button type="button" class="ghost-button row-secondary"></button></article>`), (node, item) => {
    const isLocal = Boolean(item.isLocal || item.id === "local");
    const seenMs = finiteDate(item.lastSeenAt);
    const fresh = isLocal || (seenMs != null && (Date.now() - seenMs) < 120000);
    node.dataset.tone = fresh ? "good" : "quiet";
    setText(node.querySelector("strong"), item.name || item.id);
    setText(node.querySelector(".mono"), isLocal ? "Hub (this computer)" : `${item.id}${item.hostname ? " · " + item.hostname : ""}`);
    setText(node.querySelector(".source-setting-status b"), isLocal ? "Local" : fresh ? "Live" : item.lastSeenAt ? "Idle" : "Paired");
    setText(node.querySelector(".source-setting-status small"), isLocal ? "Primary SQLite hub" : item.lastSeenAt ? `Last push ${new Date(item.lastSeenAt).toLocaleTimeString([], {hour:"numeric", minute:"2-digit"})}` : "Waiting for first push");
    const btn = node.querySelector("button");
    btn.hidden = isLocal;
    setText(btn, "Remove");
    btn.onclick = isLocal ? null : async () => {
      try {
        const res = await postJson("/api/machines", {action: "revoke", id: item.id});
        if (state.summary && res.machines) {
          state.summary.machines = res.machines;
          if (state.machine === item.id) changeMachine("all");
          else { renderMachines(state.summary); renderMachineSettings(state.summary); }
        }
      } catch {}
    };
  });
}

function renderOverview() {
  const data = state.summary;
  if (state.view !== "overview" || !summaryMatches()) return;
  updateDataValidity();
  if (data.displayTz) state.displayTz = data.displayTz;
  renderNavbar(data);
  renderCoverage(data);
  renderRanges(false);
  renderMachines(data);
  renderToolsFilter(data);
  renderMachineSettings(data);
  renderCapacity(data);
  renderResetAlerts(data);
  renderActivity(data);
  renderKpis(data);
  renderChart(data);
  renderWaste(data);
  renderForecast(data);
  renderModels(data);
  renderMix(data);
  renderSubs(data);
  renderUnitPrice(data);
  renderHeat(data);
  const totals = data.totals || {};
  $("window-note").textContent = `Usage and value across ${(data.tools || []).length} tools · ${number(totals.records)} records`;
  $("range-label").textContent = `${windowLabel(data.window?.key || state.window)} window · ${tokens(totals.tokens)} tokens · ${number(totals.records)} records`;
  const empty = !(data.models || []).length && !(data.tools || []).length;
  $("empty-state").hidden = !empty;
  invalidateEaseNodes();
  requestAnimationFrame(() => document.body.classList.add("settled"));
  if (PROBE) writeProbe("overview");
}

function renderDetail() {
  const data = state.entityData;
  if (state.view !== "detail" || !entityMatches()) return;
  updateDataValidity();
  renderNavbar(data);
  renderRanges(false);
  const exact = data.isExact !== false;
  setText($("detail-kind"), data.kind === "model" ? "Model detail" : "Tool detail");
  setText($("detail-name"), data.name || unknown);
  setLogo($("detail-swatch"), logoKey(data.providerKey || data.toolKey, data.name));
  setText($("detail-plan"), data.plan || unknown);
  setText($("detail-cov"), exact ? "exact rates" : "published rates");
  $("detail-cov").className = "chip " + (exact ? "good" : "warn");
  setText($("detail-sub"), data.kind === "model"
    ? `Priced from ${data.providerName || "provider"} telemetry. ${pct(data.shareOfTrackedValue)} of tracked value in this window.${exact ? "" : " Value is published-rate equivalent, not a billed amount."}`
    : `All models routed through ${data.name}, including any amortised plan cost.${exact ? "" : " Value is published-rate equivalent."}`);
  const value = ease("eVal", data.value);
  const share = ease("eShare", data.shareOfTrackedValue);
  $("detail-share-bar").dataset.ease = "eShare";
  $("detail-share-bar").dataset.fmt = "width";
  if (share == null) setStyle($("detail-share-bar"), "width", "0%");
  else if (!$("detail-share-bar").style.width) setStyle($("detail-share-bar"), "width", `${Math.min(100, share)}%`);
  setStyle($("detail-share-bar"), "background", data.color || colorFor(data.providerKey));
  setText($("detail-share-label"), `${markedUsd(value, data.isExact)} of tracked value · ${pct(share)}`);
  const tokenCount = ease("eTok", data.tokens);
  const cache = data.cachePct;
  const runs = data.runs;
  const kpis = [
    {label:"Tracked value", value: usd(value), marker: exact || value == null ? "" : "≈", chip: exact ? "exact" : "derived", good: exact, note: pct(share) + " of tracked value"},
    {label:"Tokens", value: tokens(tokenCount), marker:"", chip:"exact", good:true, note: `${tokens(data.outputTokens)} output · ${pct(data.shareOfTokens)} of volume`},
    {label:"Cache reuse", value: pct(cache), marker:"", chip: isCacheOpportunity(data.opportunity) && data.opportunity?.kind !== "healthy" ? "below target" : cache == null ? "unknown" : "exact", good: !isCacheOpportunity(data.opportunity) || data.opportunity?.kind === "healthy" ? cache != null : false, note: cache == null ? "Reuse unknown" : `${tokens(data.cachedInputTokens ?? mixItem(data.mix, "cached_input")?.tokens)} of input reused`},
    {label:"Value per run", value: usd(ease("ePerRun", data.valuePerRun)), marker: exact || data.valuePerRun == null ? "" : "≈", chip: number(runs) + " runs", good:false, note: `${tokens(data.tokensPerRun)} tokens per run`}
  ];
  const kpiKeys = ["eVal", "eTok", "cachePct", "ePerRun"];
  const kpiFmts = ["usd", "tokens", "pct", "usd"];
  reconcileChildren($("detail-kpis"), kpis, item => item.label, () => nodeFrom(`<article class="kpi-card"><div><span></span><b></b></div><strong class="kpi-value"><span class="marker"></span><span class="kpi-figure"></span></strong><p></p></article>`), (node, item, i) => {
    setText(node.querySelector("div > span"), item.label);
    const chip = node.querySelector("div > b");
    setText(chip, item.chip);
    chip.className = item.good ? "good" : item.chip.includes("below") ? "" : "neutral";
    setText(node.querySelector(".marker"), item.marker);
    bindEase(node.querySelector(".kpi-figure"), kpiKeys[i] === "cachePct" ? "" : kpiKeys[i], kpiFmts[i], item.value);
    setText(node.querySelector("p"), item.note);
  });
  const series = data.series || [];
  let maxRaw = 0.001;
  let anchorKey = null;
  series.forEach((item, i) => {
    const tok = finite(item.tokens);
    if (tok != null && tok > maxRaw) {
      maxRaw = tok;
      anchorKey = bucketKeyOf(item, i);
    }
  });
  const detailScaleKey = `${state.window}:${state.entity?.kind || "entity"}:${state.entity?.key || "all"}`;
  const scale = stableScale(state.detailScales, detailScaleKey, niceMax(maxRaw), anchorKey, new Set(series.map(bucketKeyOf)));
  renderGutter($("detail-gutter"), scale);
  const active = state.dPinned ?? state.dHover;
  const color = data.color || colorFor(data.providerKey);
  reconcileSegmentColumns($("detail-bars"), series, bucket => {
    const tok = finite(bucket.tokens);
    return [{
      key:"tokens",
      height:tok == null ? null : Math.max(tok ? 0.5 : 0, (tok / scale) * PLOT),
      color
    }];
  }, active);
  renderHitTargets($("detail-hits"), series, bucket => bucket.tokens, active);
  renderAxis($("detail-axis"), series);
  renderDetailHover(data);
  reconcileChildren($("detail-mix-cards"), data.mix || [], item => item.key, () => nodeFrom(`<div class="mix-card"><span><i></i></span><strong></strong><div class="mini-track"><i></i></div><em></em></div>`), (node, item, index, fresh) => {
    const share = finite(item.share);
    const eased = share == null ? ease("detailMixShare-" + item.key, null) : ease("detailMixShare-" + item.key, share);
    const swatch = node.querySelector("span > i");
    setStyle(swatch, "background", MIX_COLORS[item.key] || "var(--t-other)");
    setTextAfter(swatch, item.label || MIX_LABELS[item.key] || item.key);
    setText(node.querySelector("strong"), tokens(item.tokens));
    const bar = node.querySelector(".mini-track > i");
    bindWidth(bar, "detailMixShare-" + item.key, eased == null ? null : Math.max(eased, 0), fresh);
    setStyle(bar, "background", MIX_COLORS[item.key] || "var(--t-other)");
    setText(node.querySelector("em"), `${pct(item.share)} of volume`);
  });
  const opp = data.opportunity || {};
  const panel = $("opportunity-panel");
  const missing = opp.amount == null;
  panel.className = "panel opportunity-panel " + (isCacheOpportunity(opp) && opp.kind !== "healthy" ? "warn" : "good");
  setText($("opp-title"), opp.title || (opp.kind === "cache_gap" ? "Close the cache gap" : opp.kind === "healthy" ? "Cache reuse is healthy" : opp.kind || "Opportunity"));
  $("opp-value").dataset.ease = "opp";
  $("opp-value").dataset.fmt = "usd";
  setText($("opp-value"), usd(ease("opp", opp.amount)));
  setText($("opp-marker"), missing ? "" : "≈");
  setText($("opp-unit"), "in this window");
  setText($("opp-detail"), missing ? "Opportunity withheld until priced token components cover this entity." : (opp.detail || ""));
  const fixChip = $("opp-fix");
  if (!fixChip.querySelector("i")) fixChip.innerHTML = "<i></i>";
  setTextAfter(fixChip.querySelector("i"), opp.fix || unknown);
  $("opp-saved").dataset.ease = "oppSaved";
  $("opp-saved").dataset.fmt = "approxUsd";
  setText($("opp-saved"), opp.alreadySaved == null ? unknown : "≈" + usd(ease("oppSaved", opp.alreadySaved)));
  setText($("entity-limit-title"), (data.providerName || "Provider") + " limits");
  setText($("entity-limit-plan"), data.plan || unknown);
  const limits = data.providerLimits || [];
  if (limits.length) renderQuotaRows($("entity-limits"), limits, "ent-");
  else setEmpty($("entity-limits"), `<p class="panel-empty">No live quota rows for this provider.</p>`);
  const sessions = data.sessions || {};
  const rows = sessions.rows || [];
  setText($("sessions-foot"), rows.length
    ? `${rows.length === runs ? "All " + number(runs) + " runs" : "Top " + rows.length + " of " + number(runs) + " runs"} · ${usd(sessions.shownTotal)} of ${usd(data.value)} · ${windowLabel(state.window)} window`
    : "No sessions in this window");
  const shownTotal = finite(sessions.shownTotal) ?? rows.reduce((sum, row) => sum + (finite(row.value) || 0), 0);
  const body = $("sessions-body");
  if (!rows.length) {
    setEmpty(body, `<p class="panel-empty">No distinct sessions to attribute.</p>`);
  } else {
    const entries = rows.flatMap((row, i) => [{row, i, part:"row"}, {row, i, part:"card"}]);
    reconcileChildren(body, entries, entry => `${entry.part}:${entry.row.id || entry.i}`, entry => entry.part === "row"
      ? nodeFrom(`<div class="session-row">
          <span class="sid"></span>
          <div><strong></strong><div class="session-share"><i></i></div></div>
          <span class="numeric session-duration"></span>
          <span class="numeric session-tokens"></span>
          <span class="numeric session-cache"></span>
          <span class="numeric session-value"><span class="session-figure"></span></span>
        </div>`)
      : nodeFrom(`<article class="session-card">
          <div class="session-card-top"><strong></strong><span class="sid"></span></div>
          <div class="session-figures">
            <span>Duration<b class="card-duration"></b></span>
            <span>Tokens<b class="card-tokens"></b></span>
            <span>Value<b class="card-value"></b></span>
          </div>
        </article>`), (node, entry, index, fresh) => {
      const row = entry.row;
      const i = entry.i;
      const shareOfShown = shownTotal > 0 ? (finite(row.value) || 0) / shownTotal * 100 : 0;
      const share = ease("ss-" + i, finite(row.sharePct) ?? shareOfShown);
      const cacheColor = row.cachePct == null ? "var(--dim)" : "var(--text)";
      const sid = String(row.id || "").slice(0, 10);
      const durationText = row.durationMin == null ? unknown : row.durationMin + "m";
      if (entry.part === "row") {
        setText(node.querySelector(".sid"), sid);
        setText(node.querySelector("div > strong"), row.project || "Unassigned");
        const bar = node.querySelector(".session-share > i");
        bindWidth(bar, "ss-" + i, share == null ? null : Math.min(100, share || 0), fresh);
        setStyle(bar, "background", color);
        setText(node.querySelector(".session-duration"), durationText);
        setText(node.querySelector(".session-tokens"), tokens(row.tokens));
        const cacheCell = node.querySelector(".session-cache");
        setText(cacheCell, pct(row.cachePct));
        setStyle(cacheCell, "color", cacheColor);
        const valueCell = node.querySelector(".session-value");
        const approx = row.isExact === false || data.isExact === false;
        let marker = valueCell.querySelector(".marker");
        if (approx && !marker) {
          marker = nodeFrom(`<span class="marker">≈</span>`);
          valueCell.insertBefore(marker, valueCell.firstChild);
        } else if (!approx && marker) {
          marker.remove();
        }
        setText(valueCell.querySelector(".session-figure"), usd(row.value));
      } else {
        setText(node.querySelector(".session-card-top > strong"), row.project || "Unassigned");
        setText(node.querySelector(".session-card-top > .sid"), sid);
        setText(node.querySelector(".card-duration"), durationText);
        setText(node.querySelector(".card-tokens"), tokens(row.tokens));
        setText(node.querySelector(".card-value"), markedUsd(row.value, data.isExact));
      }
    });
  }
  invalidateEaseNodes();
  requestAnimationFrame(() => document.body.classList.add("settled"));
  if (PROBE) writeProbe("detail");
}

function renderDetailHover(data) {
  const series = data.series || [];
  const index = state.dPinned ?? state.dHover;
  const bucket = index == null ? null : series[index];
  document.querySelectorAll("#detail-bars .day-column").forEach((column, i) => column.classList.toggle("dim", index != null && index !== i));
  document.querySelectorAll("#detail-hits .hit-target").forEach((button, i) => button.classList.toggle("active", index === i));
  $("detail-hover-label").textContent = bucket ? bucket.label : `${windowLabel(state.window)} total`;
  $("detail-hover-value").textContent = bucket ? tokens(bucket.tokens) : tokens(data.tokens);
  $("detail-hover-value").style.color = bucket ? "var(--accent)" : "var(--text)";
}

function renderDiagnostics() {
  const data = state.health;
  if (!data || state.view !== "diagnostics") return;
  window.renderSourceGuidance?.(data);
  renderNavbar({...data, navigation: state.navigation, status: state.summary?.status, cadenceSeconds: state.summary?.cadenceSeconds, cadenceMinutes: state.summary?.cadenceMinutes, generatedAt: data.generatedAt, failingSource: state.summary?.failingSource});
  renderFreshness();
  const grid = $("diagnostic-grid");
  const ingest = data.ingest || [];
  if (!ingest.length) {
    setEmpty(grid, `<p class="panel-empty">No ingest runs recorded.</p>`);
  } else {
    reconcileChildren(grid, ingest, item => item.source, () => nodeFrom(`<article class="diagnostic-card"><i class="logo-slot"></i><span></span><strong></strong><small><span class="diag-count"></span><br><span class="diag-detail"></span></small></article>`), (node, item) => {
      const color = item.status === "success" || item.status === "partial" ? "var(--green)" : item.status === "skipped" || item.status === "unavailable" ? "var(--accent)" : "var(--danger)";
      setText(node.querySelector("span"), item.source);
      const sourceKey = String(item.source || "").split(/[_:]/)[0];
      const sourceLogo = node.querySelector(".logo-slot");
      sourceLogo.hidden = sourceKey === "sync";
      if (sourceKey !== "sync") setLogo(sourceLogo, logoKey(sourceKey, sourceKey));
      const status = node.querySelector("strong");
      setText(status, item.status);
      setStyle(status, "color", color);
      setText(node.querySelector(".diag-count"), `${number(item.eventsLast24h)} events in 24h`);
      setText(node.querySelector(".diag-detail"), item.lastSuccess ? "Last success " + new Date(item.lastSuccess).toLocaleString() : (item.error || "No successful run"));
    });
  }
  const polls = $("quota-polls");
  const quotas = data.quotas || [];
  if (!quotas.length) {
    setEmpty(polls, `<p class="panel-empty">No quota pollers reported.</p>`);
  } else {
    reconcileChildren(polls, quotas, item => item.providerKey, () => nodeFrom(`<div class="waste-item"><div><strong></strong><small></small></div><span></span></div>`), (node, item) => {
      setText(node.querySelector("strong"), item.providerKey);
      setText(node.querySelector("small"), `${item.status} · ${item.reason || (item.lastPoll ? "polled " + item.lastPoll : "no poll")}`);
      setText(node.querySelector(":scope > span"), item.status);
    });
  }
  const gaps = $("pricing-gaps");
  if ((data.pricingGaps || []).length) {
    reconcileChildren(gaps, data.pricingGaps, gap => gap, () => nodeFrom(`<div class="waste-item"><strong></strong><span>${unknown}</span></div>`), (node, gap) => {
      setText(node.querySelector("strong"), gap);
    });
  } else {
    setEmpty(gaps, "<p>No pricing gaps.</p>");
  }
  const variance = data.providerVsComputedVariancePct;
  setText($("variance-note"), variance == null ? "Variance unavailable." : `Provider-reported versus computed cost differs by ${pct(variance)}.`);
  invalidateEaseNodes();
  if (PROBE) writeProbe("diagnostics");
}

async function loadSummary(background = false) {
  if (!background) navigateView("overview");
  if (state.view !== "overview" || pendingViews.overview) return;
  const windowKey = state.window;
  const toolKey = state.tool && state.tool !== "all" ? state.tool : "all";
  const machineSuffix = state.machine && state.machine !== "all" ? '&machine=' + encodeURIComponent(state.machine) : '';
  const unfiltered = !machineSuffix && toolKey === "all";
  const painted = unfiltered && (!state.summary || state.summary.window?.key !== state.window) && paintSnapshot(windowKey);
  const request = startViewRequest("overview");
  if (!request) return;
  // Consume startup prefetch once, even when a matching snapshot was painted.
  const startup = window.__prefetch;
  const prefetch = unfiltered && startup?.window === windowKey ? startup : null;
  window.__prefetch = null;
  // A discarded startup request may still reject after navigation.
  startup?.summary.catch(() => {});
  startup?.health.catch(() => {});
  refreshOverviewHealth(prefetch?.health);
  try {
    const data = await jsonFetch('/api/spend/summary?window=' + encodeURIComponent(windowKey) + '&tool=' + encodeURIComponent(toolKey) + machineSuffix,
      unfiltered ? prefetch?.summary : null, request.controller.signal);
    if (!ownsRender(request)) return;
    if (data.window?.key !== windowKey) throw new Error("Response range did not match the selection");
    state.summary = data;
    if (data.navigation) state.navigation = data.navigation;
    clearError();
    renderPreservingScroll(background || painted, renderOverview);
    if (unfiltered) writeSnapshot(windowKey, data, state.health);
  } catch (error) {
    failViewRequest(request, error);
  } finally {
    finishViewRequest(request);
  }
}
async function loadEntity(kind, key, background = false) {
  if (!background) navigateView("detail", {kind, key});
  if (state.view !== "detail" || state.entity?.kind !== kind || state.entity?.key !== key) return;
  const request = startViewRequest("detail");
  if (!request) return;
  const machineSuffix = state.machine && state.machine !== "all" ? '&machine=' + encodeURIComponent(state.machine) : '';
  try {
    const data = await jsonFetch('/api/spend/entity?kind=' + encodeURIComponent(request.kind)
      + '&key=' + encodeURIComponent(request.key) + '&window=' + encodeURIComponent(request.window) + machineSuffix,
      null, request.controller.signal);
    if (!ownsRender(request)) return;
    if (data.kind !== request.kind || data.window?.key !== request.window
      || (data.key != null && data.key !== request.key)) throw new Error("Response identity did not match the selection");
    state.entityData = data;
    state.entityDataIdentity = request.identity;
    if (data.navigation) state.navigation = data.navigation;
    clearError();
    renderPreservingScroll(background, renderDetail);
  } catch (error) {
    failViewRequest(request, error);
  } finally {
    finishViewRequest(request);
  }
}
async function loadDiagnostics(background = false) {
  if (!background) navigateView("diagnostics");
  const request = startViewRequest("diagnostics");
  if (!request) return;
  try {
    const data = await jsonFetch("/api/spend/health", null, request.controller.signal);
    if (!ownsRender(request)) return;
    state.health = data;
    clearError();
    renderPreservingScroll(background, renderDiagnostics);
  } catch (error) {
    failViewRequest(request, error);
  } finally {
    finishViewRequest(request);
  }
}
function refreshCurrent() {
  if (document.hidden || pendingViews[state.view]) return;
  loadFreshness();
  window.BurnrateAttention?.refresh();
  if (state.view === "overview") loadSummary(true);
  else if (state.view === "detail" && state.entity) loadEntity(state.entity.kind, state.entity.key, true);
  else if (state.view === "diagnostics") loadDiagnostics(true);
}

window.openAttentionView = async (type, model) => {
  const task = type === "capacity" ? returnOverview() : loadDiagnostics(), owned = state.request;
  await task;
  if (owned !== state.request) return;
  const root = $(type === "capacity" ? "capacity-body" : "pricing-gaps");
  const target = [...root.querySelectorAll("strong")].find(node=>node.textContent===model) || root;
  target.tabIndex = -1; target.focus(); target.scrollIntoView({block:"center"});
};

function minTargetSize() {
  // A12: heat cells are separate from the 44px check (spacing exception).
  let min = Infinity;
  let heatMin = Infinity;
  let secondaryMin = Infinity;
  document.querySelectorAll("button, [role='button'], .legend-card, .model-row, .live-pill").forEach(el => {
    if (!el.getClientRects().length) return;
    if (el.classList.contains("hit-target")) return;
    if (el.closest(".burnrate-nav, .model-table-head, .value-gutter, .chart-axis")) return;
    const box = el.getBoundingClientRect();
    if (box.width < 1 || box.height < 1) return;
    if (el.classList.contains("heat-cell")) {
      heatMin = Math.min(heatMin, box.width, box.height);
      return;
    }
    if (el.classList.contains("row-secondary")) {
      secondaryMin = Math.min(secondaryMin, box.height);
      return;
    }
    const size = Math.min(box.width, box.height);
    min = Math.min(min, size);
  });
  state.heatMinTarget = heatMin === Infinity ? 0 : heatMin;
  state.secondaryMinTarget = secondaryMin === Infinity ? 0 : secondaryMin;
  return min === Infinity ? 0 : min;
}
function writeProbe(view) {
  const report = $("probe-report");
  if (!report) return;
  const root = document.documentElement;
  const currentBar = document.querySelector(".segment");
  if (currentBar && state.probeBar == null) state.probeBar = currentBar;
  else if (currentBar && state.probeBar) state.probeBarStable = currentBar === state.probeBar;
  const payload = {
    view,
    overflow: root.scrollWidth > root.clientWidth + 1,
    scrollWidth: root.scrollWidth,
    clientWidth: root.clientWidth,
    innerWidth: window.innerWidth,
    tracked: $("tracked-value")?.textContent || "",
    liveCount: $("live-count")?.textContent || "",
    status: $("diagnostics-button")?.dataset.state || "",
    unpriced: [...document.querySelectorAll(".model-row .numeric")].some(el => el.textContent.trim() === unknown),
    paygPct: [...document.querySelectorAll(".capacity-row .track.payg")].map(track => track.previousElementSibling?.querySelector("b")?.textContent?.trim() || ""),
    minTarget: minTargetSize(),
    minSecondaryTarget: state.secondaryMinTarget,
    minHeatCell: state.heatMinTarget ?? 0,
    mq1199: window.matchMedia("(max-width:1199px)").matches,
    mq1023: window.matchMedia("(max-width:1023px)").matches,
    mq767: window.matchMedia("(max-width:767px)").matches,
    barHeight: document.querySelector(".segment")?.getBoundingClientRect().height || 0,
    barStable: state.probeBarStable,
    meterHeight: document.querySelector(".burnrate-mark")?.getBoundingClientRect().height || 0,
    easeFrames: state.easeFrames,
    easing: easeFrame != null,
    ttfb: Math.round(performance.getEntriesByType("navigation")[0]?.responseStart || 0),
    firstPaint: Math.round(performance.getEntriesByType("paint").find(entry => entry.name === "first-contentful-paint")?.startTime || 0),
    lcp: Math.round(timing.lcp || 0),
    refreshRenderMs: timing.lastRenderMs == null ? null : Math.round(timing.lastRenderMs * 100) / 100,
    loading: document.body.classList.contains("loading")
  };
  report.textContent = JSON.stringify(payload);
  report.hidden = false;
  document.title = "PROBE:" + report.textContent;
}

$("mix-toggle").addEventListener("click", () => { state.mixOpen = !state.mixOpen; if (state.summary) renderMix(state.summary); });
document.querySelectorAll("[data-mode]").forEach(button => button.addEventListener("click", () => {
  state.mode = button.dataset.mode;
  document.querySelectorAll("[data-mode]").forEach(item => {
    const on = item.dataset.mode === state.mode;
    item.classList.toggle("active", on);
    item.setAttribute("aria-pressed", on ? "true" : "false");
  });
  if (state.summary) renderChart(state.summary);
}));
$("detail-back").addEventListener("click", () => returnOverview());
$("diagnostics-back").addEventListener("click", () => returnOverview());
$("home-button").addEventListener("click", () => returnOverview(true));
$("diagnostics-button").addEventListener("click", () => loadDiagnostics());
$("retry-button").addEventListener("click", () => {
  if (state.view === "diagnostics") loadDiagnostics();
  else if (state.view === "detail" && state.entity) loadEntity(state.entity.kind, state.entity.key);
  else loadSummary();
});
function bindRangeKeys(root) {
  root.addEventListener("click", event => {
    const button = event.target.closest("[data-window]");
    if (button) changeRange(button.dataset.window);
  });
  root.addEventListener("keydown", event => {
    if (event.key !== "ArrowLeft" && event.key !== "ArrowRight") return;
    const keys = WINDOWS.map(item => item.key);
    const index = keys.indexOf(state.window);
    if (index < 0) return;
    event.preventDefault();
    const next = event.key === "ArrowLeft" ? Math.max(0, index - 1) : Math.min(keys.length - 1, index + 1);
    changeRange(keys[next]);
    // Roving focus (C14): arrows move real focus to the active radio.
    const active = root.querySelector(`[data-window="${state.window}"]`);
    if (active) {
      root.querySelectorAll("[data-window]").forEach(button => button.tabIndex = -1);
      active.tabIndex = 0;
      active.focus();
    }
  });
}
function bindChartHits() {
  const overviewHits = $("chart-hit-targets");
  overviewHits.addEventListener("pointerover", event => {
    const button = event.target.closest(".hit-target");
    if (!button || state.pinned != null || !state.summary) return;
    const index = Number(button.dataset.index);
    if (state.hover === index) return;
    state.hover = index;
    renderChartHover(state.summary);
  });
  overviewHits.addEventListener("pointerleave", () => {
    if (state.pinned != null || !state.summary) return;
    state.hover = null;
    renderChartHover(state.summary);
  });
  overviewHits.addEventListener("click", event => {
    const button = event.target.closest(".hit-target");
    if (!button || !state.summary) return;
    event.stopPropagation();
    const index = Number(button.dataset.index);
    state.pinned = state.pinned === index ? null : index;
    state.hover = state.pinned;
    renderChartHover(state.summary);
  });
  overviewHits.addEventListener("focusin", event => {
    const button = event.target.closest(".hit-target");
    if (!button || state.pinned != null || !state.summary) return;
    state.hover = Number(button.dataset.index);
    renderChartHover(state.summary);
  });
  const detailHits = $("detail-hits");
  detailHits.addEventListener("pointerover", event => {
    const button = event.target.closest(".hit-target");
    if (!button || state.dPinned != null || !state.entityData) return;
    const index = Number(button.dataset.index);
    if (state.dHover === index) return;
    state.dHover = index;
    renderDetailHover(state.entityData);
  });
  detailHits.addEventListener("pointerleave", () => {
    if (state.dPinned != null || !state.entityData) return;
    state.dHover = null;
    renderDetailHover(state.entityData);
  });
  detailHits.addEventListener("click", event => {
    const button = event.target.closest(".hit-target");
    if (!button || !state.entityData) return;
    event.stopPropagation();
    const index = Number(button.dataset.index);
    state.dPinned = state.dPinned === index ? null : index;
    state.dHover = state.dPinned;
    renderDetailHover(state.entityData);
  });
}
function clearPins() {
  state.pinned = null;
  state.hover = null;
  state.dPinned = null;
  state.dHover = null;
  if (state.view === "detail" && state.entityData) renderDetailHover(state.entityData);
  else if (state.summary) renderChartHover(state.summary);
}
document.addEventListener("pointerdown", event => {
  if (state.pinned == null && state.dPinned == null) return;
  if (event.target.closest(".hit-target, .chart-tooltip")) return;
  clearPins();
});
window.addEventListener("keydown", event => {
  if (event.target?.closest?.("input, textarea, select, dialog, [contenteditable='true']")) return;
  if (event.key === "Escape") clearPins();
  const series = state.view === "detail" ? state.entityData?.series : state.summary?.series;
  const pinnedKey = state.view === "detail" ? "dPinned" : "pinned";
  if ((event.key === "ArrowLeft" || event.key === "ArrowRight") && state[pinnedKey] != null && series?.length) {
    event.preventDefault();
    const delta = event.key === "ArrowLeft" ? -1 : 1;
    state[pinnedKey] = Math.max(0, Math.min(series.length - 1, state[pinnedKey] + delta));
    if (state.view === "detail") {
      state.dHover = state.dPinned;
      renderDetailHover(state.entityData);
    } else {
      state.hover = state.pinned;
      renderChartHover(state.summary);
    }
  }
});
document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshCurrent(); });
bindChartHits();
bindResetAlerts();
bindRangeKeys($("range-switch"));
bindRangeKeys($("detail-ranges"));
renderRanges(true);
$("capacity-poll")?.addEventListener("click", async () => {
  const btn = $("capacity-poll");
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.textContent = "↻ Polling…";
  try {
    await postJson("/api/spend/quotas/refresh", {});
    await loadSummary(true);
  } catch {} finally {
    btn.disabled = false;
    btn.textContent = "↻ Poll";
  }
});
function consumeFocusTool() {
  try {
    const url = new URL(location.href);
    const fromQuery = url.searchParams.get("focusTool");
    if (fromQuery) {
      url.searchParams.delete("focusTool");
      history.replaceState(null, "", url);
      loadEntity("tool", fromQuery);
      return true;
    }
    const raw = localStorage.getItem("burnrate:focus-tool");
    if (raw) {
      localStorage.removeItem("burnrate:focus-tool");
      const parsed = JSON.parse(raw);
      if (parsed?.key && Date.now() - Number(parsed.ts || 0) < 25000) {
        loadEntity("tool", parsed.key);
        return true;
      }
    }
  } catch {}
  return false;
}
window.addEventListener("focus", () => { consumeFocusTool(); });
window.addEventListener("storage", event => { if (event.key === "burnrate:focus-tool") consumeFocusTool(); });
pruneSnapshots();
if (!consumeFocusTool()) loadSummary();
loadFreshness();
scheduleEase();
setInterval(refreshCurrent, POLL_MS);
connectLiveStream();
