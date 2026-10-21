"""Always-on-Top Sidecar HUD for BURNRATE (Tauri native window & browser sidecar)."""

from __future__ import annotations

HUD_HTML = r"""<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="color-scheme" content="dark light">
    <meta name="theme-color" content="#08080a">
    <title>BURNRATE · Sidecar HUD</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <script>
      // Resolve the theme before first paint: the HUD's own choice, then the
      // dashboard's, then the OS.
      (() => {
        let theme = null;
        try { theme = localStorage.getItem("burnrate:hud-theme") || localStorage.getItem("burnrate:theme"); } catch {}
        if (theme !== "dark" && theme !== "light") theme = matchMedia("(prefers-color-scheme: light)").matches ? "light" : "dark";
        document.documentElement.dataset.theme = theme;
        try { if (localStorage.getItem("burnrate:hud-mini") === "1") document.documentElement.classList.add("is-mini"); } catch {}
      })();
    </script>
    <style>
      /* data-tauri-drag-region toggle_hud_pin */
      /* "Instrument" HUD: one glance number, then every plan as a segmented meter row. */
      :root {
        color-scheme: light;
        --bg: #f4f2ee;
        --surface: #fbfaf8;
        --sunken: #e7e4de;
        --text: #141311;
        --secondary: #4a4741;
        --muted: #5f5b55;
        --dim: #74706a;
        --line: rgba(20, 18, 15, 0.1);
        --hair: rgba(20, 18, 15, 0.07);
        --accent: #cf4509;
        --accent-soft: rgba(207, 69, 9, 0.1);
        --green: #13804c;
        --danger: #c42b24;
        --meter: #141311;
        --track: rgba(20, 18, 15, 0.13);
        --tip-bg: #ffffff;
        --tip-shadow: 0 14px 34px -10px rgba(20, 18, 15, 0.35);
        --sans: "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
        --fd: Bahnschrift, "DIN Alternate", "DIN 2014", Barlow, "Segoe UI", system-ui, sans-serif;
        --mono: "Cascadia Mono", ui-monospace, "Segoe UI Mono", Consolas, monospace;
      }
      html[data-theme="dark"] {
        color-scheme: dark;
        --bg: #0a0a0c;
        --surface: #121215;
        --sunken: #0d0d0f;
        --text: #f2f0eb;
        --secondary: #b8b4ac;
        --muted: #9a968f;
        --dim: #807d76;
        --line: rgba(255, 255, 255, 0.08);
        --hair: rgba(255, 255, 255, 0.06);
        --accent: #ff6b22;
        --accent-soft: rgba(255, 107, 34, 0.14);
        --green: #3dd68c;
        --danger: #ff5247;
        --meter: #f2f0eb;
        --track: rgba(255, 255, 255, 0.11);
        --tip-bg: #1b1b1f;
        --tip-shadow: 0 16px 40px -8px #000;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      html, body {
        background: var(--bg);
        color: var(--text);
        font: 13px/1.3 var(--sans);
        font-variant-numeric: tabular-nums;
        overflow-x: hidden;
        overflow-y: auto;
        user-select: none;
        cursor: grab;
        scrollbar-width: none;
        -webkit-font-smoothing: antialiased;
      }
      html::-webkit-scrollbar, body::-webkit-scrollbar { display: none; width: 0; height: 0; }
      body { min-height: 100vh; border: 1px solid var(--line); border-radius: 10px; }
      button { font: inherit; color: inherit; }
      .page { padding: 10px 14px 12px; }

      /* Chrome row: brand on the left, icon controls on the right. */
      .top { display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 30px; }
      .brand { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
      .brand img { width: 20px; height: 20px; border-radius: 5px; display: block; }
      .brand b { font: 700 12px/1 var(--fd); letter-spacing: 0.08em; }
      .brand b span { color: var(--accent); }
      .hud-controls { display: inline-flex; align-items: center; gap: 1px; flex: none; }
      .hud-btn {
        display: inline-grid;
        place-items: center;
        width: 28px;
        height: 28px;
        padding: 0;
        border: 0;
        border-radius: 7px;
        background: none;
        color: var(--dim);
        cursor: pointer;
        transition: background 0.12s ease, color 0.12s ease;
      }
      .hud-btn svg { width: 15px; height: 15px; display: block; fill: none; stroke: currentColor; stroke-width: 1.9; stroke-linecap: round; stroke-linejoin: round; }
      .hud-btn:hover { background: var(--hair); color: var(--text); }
      .hud-btn.active { color: var(--accent); }
      .hud-btn.active .pin-head { fill: currentColor; }
      .hud-btn:focus-visible, .hud-poll-btn:focus-visible, .hud-machine-chip:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
      .icon-expand { display: none; }
      html.is-mini .icon-expand { display: block; }
      html.is-mini .icon-shrink { display: none; }

      /* Readout. */
      .date-row { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-top: 12px; }
      .label, .date, .stat header span, .plans-head, .win-label {
        font: 600 9.5px/1.1 var(--fd);
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--dim);
      }
      .hud-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        font: 600 9.5px/1 var(--fd);
        letter-spacing: 0.14em;
        color: var(--green);
      }
      .hud-status-pill i { width: 6px; height: 6px; border-radius: 50%; background: currentColor; animation: hudPing 2.2s ease-out infinite; }
      .hud-status-pill[data-state="stale"] { color: var(--accent); }
      .hud-status-pill[data-state="error"] { color: var(--danger); }
      .hud-status-pill[data-state="stale"] i, .hud-status-pill[data-state="error"] i { animation: none; }
      .hud-status-pill > span { max-width: 210px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      @keyframes hudPing {
        0% { box-shadow: 0 0 0 0 color-mix(in srgb, currentColor 55%, transparent); }
        80%, 100% { box-shadow: 0 0 0 6px transparent; }
      }
      .hero { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; margin-top: 6px; }
      .hero strong {
        font: 300 46px/0.95 var(--fd);
        letter-spacing: -0.02em;
        border-radius: 4px;
      }
      .hero small { color: var(--muted); font-size: 11.5px; text-align: right; }
      .tick { animation: stat-tick 1.1s ease-out; }
      @keyframes stat-tick { 0% { color: var(--accent); } 100% { color: inherit; } }
      .stats {
        display: grid;
        grid-template-columns: repeat(3, minmax(0, 1fr));
        margin-top: 12px;
        border-top: 1px solid var(--line);
        border-bottom: 1px solid var(--line);
      }
      .stat { min-width: 0; padding: 9px 0 9px 10px; border-left: 1px solid var(--line); }
      .stat:first-child { padding-left: 0; border-left: 0; }
      .stat header span { display: block; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
      .stat strong { display: block; margin-top: 5px; font: 400 18px/1 var(--fd); white-space: nowrap; }
      .stat small { display: block; margin-top: 3px; color: var(--dim); font-size: 10.5px; white-space: nowrap; }

      #hud-machines { display: flex; gap: 2px; margin-top: 10px; padding: 2px; border: 1px solid var(--line); border-radius: 8px; background: var(--sunken); }
      #hud-machines:empty { display: none; }
      .hud-machine-chip {
        flex: 1 1 0;
        min-width: 0;
        display: inline-flex;
        align-items: center;
        justify-content: center;
        gap: 6px;
        height: 26px;
        padding: 0 8px;
        border: 0;
        border-radius: 6px;
        background: none;
        color: var(--muted);
        font-size: 11.5px;
        font-weight: 600;
        cursor: pointer;
      }
      .hud-machine-chip span { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .hud-machine-chip:hover { color: var(--text); }
      .hud-machine-chip.active { background: var(--text); color: var(--bg); }
      .hud-machine-chip i { flex: none; width: 5px; height: 5px; border-radius: 50%; background: var(--dim); }
      .hud-machine-chip i.live { background: var(--green); }

      .live-list { margin-top: 10px; }
      .live-list[hidden] { display: none; }
      #hud-agents { display: grid; gap: 0; }
      .hud-agent { display: flex; align-items: center; justify-content: space-between; gap: 10px; min-width: 0; padding: 7px 0; cursor: pointer; }
      .hud-agent + .hud-agent { border-top: 1px solid var(--hair); }
      .hud-agent-main { display: flex; align-items: center; gap: 9px; min-width: 0; }
      .hud-agent-mark { position: relative; flex: none; display: grid; place-items: center; width: 26px; height: 26px; padding: 6px; border: 1px solid var(--line); border-radius: 7px; color: var(--text); }
      .hud-agent-mark svg { width: 100%; height: 100%; display: block; }
      .hud-pulse { position: absolute; right: -3px; bottom: -3px; width: 9px; height: 9px; border: 2px solid var(--bg); border-radius: 50%; background: var(--green); animation: hudPulse 2.2s ease-in-out infinite; }
      @keyframes hudPulse { 50% { transform: scale(1.3); opacity: 0.6; } }
      .hud-agent-text { min-width: 0; }
      .hud-agent-text strong, .hud-agent-text small { display: block; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .hud-agent-text strong { font-size: 12.5px; font-weight: 600; }
      .hud-agent-text small { margin-top: 1px; color: var(--dim); font: 10.5px var(--mono); }
      .hud-agent:hover strong { color: var(--accent); }
      .hud-machine-tag { flex: none; color: var(--dim); font: 600 9.5px/1 var(--fd); letter-spacing: 0.1em; text-transform: uppercase; }

      /* Plans: aligned meter rows. */
      .plans { margin-top: 12px; }
      .plans-head { display: flex; align-items: center; justify-content: space-between; padding-bottom: 8px; border-bottom: 1px solid var(--line); }
      #hud-quota-badge { margin-left: 6px; color: var(--accent); }
      .hud-poll-btn {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        height: 22px;
        padding: 0 8px;
        border: 1px solid var(--line);
        border-radius: 6px;
        background: none;
        color: var(--secondary);
        font: 600 9.5px/1 var(--fd);
        letter-spacing: 0.12em;
        text-transform: uppercase;
        cursor: pointer;
      }
      .hud-poll-btn svg { width: 11px; height: 11px; fill: none; stroke: currentColor; stroke-width: 2.2; stroke-linecap: round; stroke-linejoin: round; }
      .hud-poll-btn:hover { border-color: var(--text); color: var(--text); }
      .hud-poll-btn:disabled { opacity: 0.55; cursor: wait; }
      .hud-poll-btn.spinning svg { animation: hud-spin 0.75s linear infinite; }
      @keyframes hud-spin { to { transform: rotate(360deg); } }
      .plan { padding: 9px 0 8px; border-bottom: 1px solid var(--hair); cursor: pointer; }
      .plan:last-child { border-bottom: 0; }
      .plan-head { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 7px; }
      .mark { flex: none; width: 16px; height: 16px; display: grid; place-items: center; color: var(--text); }
      .mark svg { width: 15px; height: 15px; display: block; }
      .plan-head b { flex: none; font: 600 13.5px/1 var(--fd); letter-spacing: 0.01em; }
      .plan-head em { min-width: 0; margin-left: auto; overflow: hidden; color: var(--dim); font-size: 11px; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
      .plan:hover .plan-head b { color: var(--accent); }
      .windows { display: grid; gap: 4px; }
      .win { position: relative; display: grid; grid-template-columns: 62px minmax(0, 1fr) 46px 32px; align-items: center; gap: 9px; min-height: 18px; }
      .win-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .win-label .short { display: none; }
      html.is-mini .win-label .long { display: none; }
      html.is-mini .win-label .short { display: inline; }
      .plan-head b .short { display: none; }
      html.is-mini .plan-head b .long { display: none; }
      html.is-mini .plan-head b .short { display: inline; }
      .meter { position: relative; height: 9px; }
      .meter::before { content: ""; position: absolute; inset: 0; background: repeating-linear-gradient(90deg, var(--track) 0 2px, transparent 2px 4px); }
      .meter.payg::before { background: repeating-linear-gradient(135deg, var(--track) 0 2px, transparent 2px 6px); }
      .meter .fill {
        position: absolute;
        inset: 0 auto 0 0;
        width: var(--fill, 0%);
        background: var(--meter);
        -webkit-mask: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px);
        mask: repeating-linear-gradient(90deg, #000 0 2px, transparent 2px 4px);
        transition: width 0.45s cubic-bezier(.2, .7, .25, 1);
      }
      .win.warn .fill { background: var(--accent); }
      .win.crit .fill { background: var(--danger); }
      .win-pace-tick { position: absolute; top: -3px; bottom: -3px; z-index: 2; width: 2px; margin-left: -1px; border-radius: 1px; background: var(--text); opacity: 0.85; pointer-events: none; }
      .win b { font: 400 16px/1 var(--fd); text-align: right; }
      .win b.payg { color: var(--dim); font: 600 9.5px/1 var(--fd); letter-spacing: 0.12em; }
      .win.warn b { color: var(--accent); }
      .win.crit b { color: var(--danger); }
      .win .win-reset { color: var(--dim); font: 500 11px/1 var(--fd); font-style: normal; text-align: right; }
      .win.warn .win-reset, .win.crit .win-reset { color: var(--secondary); }
      .hud-empty { padding: 12px 0 4px; color: var(--muted); font-size: 12px; }
      .foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line); color: var(--dim); font-size: 10.5px; }
      .hud-mini-bar, .mini-head { display: none !important; }

      /* Mini: a glance strip. One line per plan, its tightest window only. */
      html.is-mini .page { padding: 6px 10px 8px; }
      html.is-mini .brand, html.is-mini .date-row, html.is-mini .hero, html.is-mini .stats,
      html.is-mini #hud-machines, html.is-mini .live-list, html.is-mini .plans-head, html.is-mini .foot,
      html.is-mini #hud-theme-btn, html.is-mini #hud-expand-btn { display: none !important; }
      html.is-mini .mini-head { display: flex !important; align-items: baseline; gap: 7px; min-width: 0; }
      .mini-head strong { font: 400 21px/1 var(--fd); letter-spacing: -0.01em; }
      .mini-head span { color: var(--dim); font-size: 10.5px; white-space: nowrap; }
      .mini-head .mini-live { color: var(--green); font: 600 9.5px/1 var(--fd); letter-spacing: 0.12em; text-transform: uppercase; }
      .mini-head .mini-alert { min-width: 0; overflow: hidden; text-overflow: ellipsis; color: var(--accent); font: 600 9.5px/1 var(--fd); letter-spacing: 0.12em; text-transform: uppercase; }
      .mini-head .mini-alert[data-state="error"] { color: var(--danger); }
      .mini-head:has(.mini-alert:not([hidden])) .mini-today-label { display: none; }
      html.is-mini .top { min-height: 28px; }
      html.is-mini .plans { margin-top: 4px; border-top: 1px solid var(--line); }
      html.is-mini .plan { display: grid; grid-template-columns: 74px minmax(0, 1fr); align-items: center; gap: 8px; padding: 6px 0; }
      html.is-mini .plan-head { margin: 0; }
      html.is-mini .plan-head { gap: 6px; }
      html.is-mini .plan-head em { display: none; }
      html.is-mini .mark { width: 13px; height: 13px; }
      html.is-mini .mark svg { width: 13px; height: 13px; }
      html.is-mini .plan-head b { flex: 1 1 auto; min-width: 0; overflow: hidden; font-size: 12px; text-overflow: ellipsis; white-space: nowrap; }
      html.is-mini .win:not(.is-peak) { display: none; }
      html.is-mini .win { grid-template-columns: 34px minmax(0, 1fr) 36px 26px; gap: 6px; }
      html.is-mini .win b { font-size: 14px; }
      html.is-mini .win b.payg { font-size: 9.5px; }
      html.is-mini .meter { height: 7px; }

      #hud-tip {
        position: fixed;
        z-index: 40;
        max-width: 250px;
        padding: 9px 11px;
        border-radius: 8px;
        background: var(--tip-bg);
        border: 1px solid var(--line);
        box-shadow: var(--tip-shadow);
        font-size: 11.5px;
        line-height: 1.45;
        color: var(--muted);
        pointer-events: none;
        opacity: 0;
        transform: translateY(2px);
        transition: opacity 0.12s ease, transform 0.12s ease;
      }
      #hud-tip.on { opacity: 1; transform: none; }
      #hud-tip .tip-title {
        display: -webkit-box;
        -webkit-line-clamp: 2;
        -webkit-box-orient: vertical;
        overflow: hidden;
        word-break: break-word;
        margin-bottom: 3px;
        color: var(--text);
        font: 600 12.5px/1.25 var(--fd);
      }
      #hud-tip .row { display: block; }
      #hud-tip .row.accent { color: var(--text); font-weight: 600; }
      #hud-tip .sep { display: block; height: 1px; margin: 6px 0; background: var(--line); }
      #hud-tip .row.dry { color: var(--danger); font-weight: 600; }
      #hud-tip .row.tight { color: var(--accent); font-weight: 600; }
      #hud-tip .row.safe { color: var(--green); font-weight: 600; }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { animation: none !important; transition: none !important; }
      }
    </style>
  </head>
  <body>
    <main class="page" id="hud-shell">
      <header class="top">
        <span class="brand"><img src="/favicon.svg" alt=""><b>BURN<span>RATE</span></b></span>
        <div class="mini-head" aria-live="polite">
          <strong id="hud-mini-today">—</strong><span class="mini-today-label">today</span><span class="mini-live" id="hud-mini-live" hidden></span><span class="mini-alert" id="hud-mini-alert" hidden></span>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn" id="hud-mini-btn" aria-label="Mini HUD" aria-pressed="false" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Mini HUD&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Collapse into a glance strip: one line per plan, showing its tightest window.&lt;/span&gt;">
            <svg class="icon-shrink" viewBox="0 0 24 24" aria-hidden="true"><path d="M4 14h6v6M20 10h-6V4M14 10l7-7M3 21l7-7"/></svg>
            <svg class="icon-expand" viewBox="0 0 24 24" aria-hidden="true"><path d="M15 3h6v6M9 21H3v-6M21 3l-7 7M3 21l7-7"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-theme-btn" aria-label="Switch theme" aria-pressed="false" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Theme&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Switch between the graphite and paper palettes.&lt;/span&gt;">
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>
          </button>
          <button type="button" class="hud-btn active" id="hud-pin-btn" aria-label="Pin always on top" aria-pressed="true" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Pin Always-on-Top&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Keep the Sidecar HUD floating above all other windows.&lt;/span&gt;">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16v5"/><path class="pin-head" d="M9 4h6l-1 5 3 3v3H7v-3l3-3z"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-expand-btn" aria-label="Open dashboard" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Open dashboard&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Forecasts, unit economics, and plans in the full window.&lt;/span&gt;">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-hide-btn" aria-label="Hide HUD" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Hide&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Hide the HUD. Reopen it from the BURNRATE tray menu.&lt;/span&gt;">
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
        </div>
      </header>

      <p class="date-row">
        <span class="date" id="hud-date">Today</span>
        <span class="hud-status-pill" id="hud-status-pill" data-state="live"><i></i><span id="hud-status-text">LIVE</span></span>
      </p>

      <section class="hero" id="hud-stat-today" aria-label="API-rate spend today">
        <strong id="hud-today">—</strong>
        <small>API-rate value<br>since midnight</small>
      </section>

      <section class="stats" aria-label="Live BurnRate KPIs">
        <article class="stat" id="hud-stat-burn">
          <header><span>Burn rate</span></header>
          <strong id="hud-burn-rate">—</strong>
          <small>per day · 7d avg</small>
        </article>
        <article class="stat" id="hud-stat-cache">
          <header><span>Cache hit</span></header>
          <strong id="hud-cache">—</strong>
          <small>last 24 hours</small>
        </article>
        <article class="stat" id="hud-stat-tokens">
          <header><span>Tokens</span></header>
          <strong id="hud-tokens">—</strong>
          <small>since midnight</small>
        </article>
      </section>

      <div id="hud-machines" role="radiogroup" aria-label="Computer filter"></div>

      <section class="live-list" id="hud-live" hidden aria-label="Running agents across computers">
        <div id="hud-agents"></div>
      </section>

      <div class="hud-mini-bar" id="hud-mini-bar" hidden role="button" tabindex="0" aria-label="Top plan bottleneck">
        <span id="hud-mini-lead">All subscription limits healthy</span>
        <strong id="hud-mini-pct">—</strong>
      </div>

      <section class="plans" aria-label="Subscription usage">
        <div class="plans-head">
          <span>Plans<span id="hud-quota-badge">—</span></span>
          <button type="button" class="hud-poll-btn" id="hud-poll-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Poll quotas now&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Immediately query all connected subscription providers and sync.&lt;/span&gt;"><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg><span>Poll</span></button>
        </div>
        <div id="hud-quotas">
          <p class="hud-empty">Waiting for subscription usage…</p>
        </div>
      </section>

      <footer class="foot">
        <span id="hud-computers-foot"></span>
        <span id="hud-updated-foot"></span>
      </footer>
    </main>

    <div id="hud-tip" role="tooltip" aria-hidden="true"></div>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        const root = document.documentElement;
        let selectedMachine = new URLSearchParams(location.search).get("machine") || "all";
        let pinned = true;
        try {
          const savedPin = localStorage.getItem("burnrate:hud-pinned");
          if (savedPin === "0") pinned = false;
        } catch {}

        const usd = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return "$" + Math.round(n).toLocaleString("en-US");
        };
        const usd2 = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return "$" + n.toLocaleString("en-US", {minimumFractionDigits: 2, maximumFractionDigits: 2});
        };
        const tok = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
          if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
          if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
          return String(Math.round(n));
        };
        const pctText = value => value == null ? "—" : (value > 0 && value < 1 ? "<1%" : Math.round(value) + "%");
        const esc = value => String(value ?? "").replace(/[&<>"']/g, ch => (
          {"&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"}[ch]
        ));
        const fmtNum = v => {
          const n = Number(v);
          if (!Number.isFinite(n)) return "—";
          if (Math.abs(n) >= 100) return Math.round(n).toLocaleString("en-US");
          if (Math.abs(n) >= 10) return String(Math.round(n * 10) / 10);
          return String(Math.round(n * 100) / 100);
        };
        const qtyText = (value, unit) => {
          const n = Number(value);
          if (!Number.isFinite(n)) return null;
          const u = String(unit || "");
          if (u === "usd") return usd2(n);
          if (u === "pct" || u === "percent") return Math.round(n) + "%";
          if (u === "credits") return fmtNum(n) + " credits";
          if (u === "unavailable" || u === "") return fmtNum(n);
          return fmtNum(n) + " " + u;
        };
        const tipRow = (text, cls) => text ? '<span class="row' + (cls ? " " + cls : "") + '">' + text + "</span>" : "";

        const tipEl = $("hud-tip");
        let tipTimer = null;
        let tipTarget = null;
        function showTip(target) {
          const html = target.dataset.tip;
          if (!html) return;
          tipEl.innerHTML = html;
          tipEl.classList.add("on");
          tipEl.setAttribute("aria-hidden", "false");
          tipEl.style.left = "0px";
          tipEl.style.top = "0px";
          const rect = target.getBoundingClientRect();
          const tw = tipEl.offsetWidth;
          const th = tipEl.offsetHeight;
          let x = rect.left + rect.width / 2 - tw / 2;
          x = Math.min(Math.max(8, x), Math.max(8, window.innerWidth - tw - 8));
          let y = rect.top - th - 8;
          if (y < 8) y = rect.bottom + 8;
          y = Math.min(y, Math.max(8, window.innerHeight - th - 8));
          tipEl.style.left = Math.round(x) + "px";
          tipEl.style.top = Math.round(y) + "px";
        }
        function hideTip() {
          if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
          tipEl.classList.remove("on");
          tipEl.setAttribute("aria-hidden", "true");
          tipTarget = null;
        }
        document.addEventListener("mouseover", event => {
          const target = event.target.closest("[data-tip]");
          if (target === tipTarget) return;
          if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
          if (!target || !target.dataset.tip) { hideTip(); return; }
          const picked = target;
          tipTimer = setTimeout(() => {
            tipTarget = picked;
            showTip(picked);
          }, 140);
        });
        document.addEventListener("mousedown", hideTip, true);
        window.addEventListener("scroll", hideTip, true);
        window.addEventListener("blur", hideTip);

        function setStat(id, text) {
          const el = $(id);
          if (!el || el.textContent === text) return;
          const first = el.textContent === "—";
          el.textContent = text;
          if (first) return;
          el.classList.remove("tick");
          void el.offsetWidth;
          el.classList.add("tick");
        }

        async function tauriInvoke(cmd, args = {}) {
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            try {
              return await internals.invoke(cmd, args);
            } catch {
              return null;
            }
          }
          return null;
        }

        document.addEventListener("mousedown", async event => {
          if (event.button !== 0 || event.detail > 1) return;
          if (event.target.closest("button, a, input, select, textarea, [role='button'], [role='radio'], .plan, .hud-agent, .hud-machine-chip")) return;
          const handled = await tauriInvoke("start_hud_drag");
          if (handled === null && !window.opener) {
            try {
              await fetch("/api/hud/native", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "drag" })
              });
            } catch {}
          }
        });

        function applyPinUi(stateVal) {
          pinned = Boolean(stateVal);
          try { localStorage.setItem("burnrate:hud-pinned", pinned ? "1" : "0"); } catch {}
          const btn = $("hud-pin-btn");
          if (btn) {
            btn.classList.toggle("active", pinned);
            btn.setAttribute("aria-pressed", String(pinned));
          }
        }
        applyPinUi(pinned);
        tauriInvoke("set_hud_pin", { pinned });

        $("hud-pin-btn").addEventListener("click", async () => {
          const nextPin = !pinned;
          const res = await tauriInvoke("set_hud_pin", { pinned: nextPin });
          if (typeof res === "boolean") {
            applyPinUi(res);
            return;
          }
          try {
            const resp = await fetch("/api/hud/native", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: nextPin ? "pin" : "unpin" })
            });
            const data = await resp.json();
            applyPinUi(typeof data?.pinned === "boolean" ? data.pinned : nextPin);
          } catch {
            applyPinUi(nextPin);
          }
        });

        $("hud-expand-btn").addEventListener("click", async () => {
          const handled = await tauriInvoke("show_dashboard");
          if (handled === null) window.open("/", "_blank");
        });

        $("hud-hide-btn").addEventListener("click", async () => {
          const handled = await tauriInvoke("hide_hud");
          if (handled === null) {
            try {
              await fetch("/api/hud/native", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "hide" })
              });
            } catch {}
            window.close();
          }
        });

        async function openToolInDashboard(toolKey) {
          const clean = String(toolKey || "").trim();
          try {
            if (clean) {
              localStorage.setItem("burnrate:focus-tool", JSON.stringify({ key: clean, ts: Date.now() }));
            }
          } catch {}
          const targetUrl = clean ? "/?focusTool=" + encodeURIComponent(clean) : "/";
          const handled = await tauriInvoke("show_dashboard");
          if (handled === null) {
            window.open(targetUrl, "burnrate-dashboard");
          }
        }

        let isMini = root.classList.contains("is-mini");
        let lastAppliedWidth = 0;
        let lastAppliedHeight = 0;

        async function syncHudWindowSize(force = false) {
          const shell = $("hud-shell");
          if (!shell) return;
          const targetWidth = isMini ? 296 : 380;
          const rect = shell.getBoundingClientRect();
          const measuredH = Math.ceil(rect.height || shell.offsetHeight || (isMini ? 200 : 540));
          const targetHeight = Math.max(160, Math.min(820, measuredH + 2));
          if (!force && targetWidth === lastAppliedWidth && Math.abs(targetHeight - lastAppliedHeight) <= 2) {
            return;
          }
          lastAppliedWidth = targetWidth;
          lastAppliedHeight = targetHeight;
          const handled = await tauriInvoke("set_hud_size", { width: targetWidth, height: targetHeight });
          if (handled === null) {
            if (window.opener) {
              try { window.resizeTo(targetWidth + 16, targetHeight + 38); } catch {}
            } else {
              try {
                await fetch("/api/hud/native", {
                  method: "POST",
                  headers: { "Content-Type": "application/json" },
                  body: JSON.stringify({ action: "resize", width: targetWidth, height: targetHeight })
                });
              } catch {}
            }
          }
          setTimeout(async () => {
            const rectAfter = shell.getBoundingClientRect();
            const hAfter = Math.max(160, Math.min(820, Math.ceil(rectAfter.height || shell.offsetHeight || targetHeight) + 2));
            if (Math.abs(hAfter - lastAppliedHeight) > 2) {
              lastAppliedHeight = hAfter;
              const h2 = await tauriInvoke("set_hud_size", { width: targetWidth, height: hAfter });
              if (h2 === null && !window.opener) {
                try {
                  await fetch("/api/hud/native", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ action: "resize", width: targetWidth, height: hAfter })
                  });
                } catch {}
              }
            }
          }, 45);
        }

        function applyMini(forceResize = false) {
          root.classList.toggle("is-mini", isMini);
          const btn = $("hud-mini-btn");
          if (btn) {
            btn.setAttribute("aria-pressed", String(isMini));
            btn.setAttribute("aria-label", isMini ? "Expand HUD" : "Mini HUD");
          }
          syncHudWindowSize(forceResize);
        }
        applyMini(true);
        $("hud-mini-btn").addEventListener("click", () => {
          isMini = !isMini;
          try { localStorage.setItem("burnrate:hud-mini", isMini ? "1" : "0"); } catch {}
          hideTip();
          applyMini(true);
        });
        $("hud-mini-bar").addEventListener("click", () => {
          const key = $("hud-mini-bar").dataset.providerKey || "";
          openToolInDashboard(key);
        });

        function applyTheme() {
          const dark = root.dataset.theme === "dark";
          const btn = $("hud-theme-btn");
          if (btn) {
            btn.setAttribute("aria-pressed", String(dark));
            btn.setAttribute("aria-label", dark ? "Switch to light theme" : "Switch to dark theme");
          }
        }
        applyTheme();
        $("hud-theme-btn").addEventListener("click", () => {
          const theme = root.dataset.theme === "dark" ? "light" : "dark";
          root.dataset.theme = theme;
          try { localStorage.setItem("burnrate:hud-theme", theme); } catch {}
          applyTheme();
        });

        $("hud-poll-btn").addEventListener("click", async event => {
          event.stopPropagation();
          const btn = $("hud-poll-btn");
          if (!btn || btn.disabled) return;
          btn.disabled = true;
          btn.classList.add("spinning");
          try {
            await fetch("/api/spend/quotas/refresh", { method: "POST", cache: "no-store" });
            await refresh();
          } catch {} finally {
            btn.disabled = false;
            btn.classList.remove("spinning");
          }
        });

        function windowElapsedPct(q) {
          if (!q || !q.resetsAt || q.payg) return null;
          const resetMs = Date.parse(q.resetsAt);
          if (!Number.isFinite(resetMs)) return null;
          const remMs = resetMs - Date.now();
          if (remMs <= 0) return null;
          const text = String((q.limitKey || "") + " " + (q.label || "")).toLowerCase();
          let durMs = null;
          if (text.includes("5h") || text.includes("5-hour") || text.includes("five-hour")) {
            durMs = 5 * 3600 * 1000;
          } else if (text.includes("week") || text.includes("7d")) {
            durMs = 7 * 86400 * 1000;
          } else if (text.includes("day") || text.includes("24h") || text.includes("daily")) {
            durMs = 86400 * 1000;
          }
          if (!durMs || remMs > durMs) return null;
          return Math.max(0, Math.min(100, 100 - (remMs / durMs) * 100));
        }

        function relTime(iso) {
          if (!iso) return "";
          const ms = Date.now() - new Date(iso).getTime();
          if (!Number.isFinite(ms)) return "";
          if (ms < 60000) return "just now";
          if (ms < 3600000) return Math.round(ms / 60000) + "m ago";
          if (ms < 86400000) return Math.round(ms / 3600000) + "h ago";
          return Math.round(ms / 86400000) + "d ago";
        }

        function renderMachines(machines) {
          const host = $("hud-machines");
          const list = Array.isArray(machines) ? machines : [];
          $("hud-computers-foot").textContent =
            list.length === 1 ? "1 computer" : list.length + " computers";
          if (list.length < 2) {
            host.replaceChildren();
            return;
          }
          const items = [{id: "all", name: "All", isLocal: true}, ...list];
          host.replaceChildren(...items.map(item => {
            const btn = document.createElement("button");
            btn.type = "button";
            btn.className = "hud-machine-chip" + (item.id === selectedMachine ? " active" : "");
            btn.setAttribute("role", "radio");
            btn.setAttribute("aria-checked", String(item.id === selectedMachine));
            const dot = document.createElement("i");
            const seenMs = item.lastSeenAt ? Date.parse(item.lastSeenAt) : NaN;
            const isFresh = Boolean(item.isLocal || item.id === "local" || (Number.isFinite(seenMs) && (Date.now() - seenMs) < 120000));
            if (isFresh) dot.className = "live";
            const label = document.createElement("span");
            label.textContent = item.name || item.id;
            btn.append(dot, label);
            const name = item.name || item.id;
            let where;
            if (item.id === "all") {
              where = "Usage aggregated across every connected computer.";
            } else if (item.isLocal || item.id === "local") {
              where = "This computer.";
            } else {
              const seen = relTime(item.lastSeenAt);
              where = "Paired spoke" + (seen ? " · last seen " + seen : "");
            }
            btn.dataset.tip =
              '<b class="tip-title">' + esc(name) + "</b>" + tipRow(esc(where));
            btn.addEventListener("click", () => {
              selectedMachine = item.id;
              refresh();
              refreshUsage();
              refreshMachines();
            });
            return btn;
          }));
        }

        function windowName(key, label) {
          const text = String(label || "").toLowerCase();
          let scope = "";
          if (key === "antigravity") {
            if (text.includes("gemini")) scope = "Gemini";
            else if (text.includes("claude") || text.includes("gpt")) scope = "Other";
          }
          let win = label || "Plan";
          if (text.includes("5-hour") || text.includes("five-hour") || text.includes("5h")) win = "5h";
          else if (text.includes("month")) win = "Month";
          else if (text.includes("week")) win = scope ? "Wk" : "Week";
          else if (text.includes("other model")) win = "Other";
          else if (text.includes("model")) win = "Models";
          else if (text.includes("fund") || text.includes("balance")) win = "Balance";
          return [scope, win].filter(Boolean).join(" ");
        }
        function windowShort(key, label) {
          const long = windowName(key, label).toLowerCase();
          const scope = long.startsWith("gemini ") ? "G·" : long.startsWith("other ") && long !== "other" ? "O·" : "";
          const win = long.replace(/^(gemini|other) /, "");
          const short = {"5h": "5H", "week": "WK", "wk": "WK", "month": "MO", "balance": "BAL", "models": "MOD", "other": "OTH"}[win];
          return scope + (short || win.slice(0, 3).toUpperCase());
        }

        function resetIn(iso) {
          if (!iso) return "";
          const ms = new Date(iso).getTime() - Date.now();
          if (!Number.isFinite(ms)) return "";
          if (ms <= 0) return "now";
          const hours = ms / 3600000;
          if (hours < 1) return Math.max(1, Math.round(ms / 60000)) + "m";
          if (hours < 48) return Math.round(hours) + "h";
          return Math.round(hours / 24) + "d";
        }

        function resetWhen(iso) {
          const date = new Date(iso || "");
          if (Number.isNaN(date.getTime())) return "";
          return date.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
        }

        function statTips(nav, totals) {
          const burn = nav.burnRatePerDay;
          $("hud-stat-burn").dataset.tip =
            '<b class="tip-title">Avg burn rate</b>' +
            tipRow(burn == null ? null : esc(usd(burn) + " per day"), "accent") +
            '<span class="sep"></span>' +
            tipRow("Reference usage value per day, averaged over the last 7 complete days. Today is excluded; derived providers are priced at published rates.");

          const today = nav.todayUsd;
          const coverage = nav.todayComplete === false ? "partial — some usage is unpriced" : "complete";
          $("hud-stat-today").dataset.tip =
            '<b class="tip-title">API spend today</b>' +
            tipRow(today == null ? null : esc(usd2(today) + " so far"), "accent") +
            '<span class="sep"></span>' +
            tipRow("Reference usage value since local midnight. Not a bill; provider-reported charges appear only when exact.") +
            tipRow(esc("Coverage: " + coverage + "."));

          const cache = totals.cacheReusePct;
          $("hud-stat-cache").dataset.tip =
            '<b class="tip-title">Cache hit rate</b>' +
            tipRow(cache == null ? null : esc(Math.round(Number(cache)) + "% of prompt tokens were cache reads"), "accent") +
            '<span class="sep"></span>' +
            tipRow("Cache-read input ÷ all prompt tokens (reads + fresh input + cache writes), rolling 24h. Unclassified usage is excluded, never estimated.");

          const todayTokens = nav.todayTokens ?? 0;
          const avg = nav.tokensAvgPerDay;
          $("hud-stat-tokens").dataset.tip =
            '<b class="tip-title">Tokens used today</b>' +
            tipRow(esc(tok(todayTokens) + " tokens since local midnight.")) +
            (avg == null ? "" : '<span class="sep"></span>' +
              tipRow(esc("7-day average: ≈ " + tok(avg) + " tokens/day"), "accent"));
        }

        const shortNames = {
          codex: "Codex",
          cursor: "Cursor",
          "claude-code": "Claude",
          antigravity: "Antigravity",
          grok: "Grok",
          opencode: "Z.AI",
          muse: "Muse",
          openrouter: "OpenRouter"
        };
        // Provider marks come from the shared sprite served at /logos.svg.
        const PLAN_LOGOS = {
          codex: "codex", "claude-code": "claude", cursor: "cursor", grok: "grok", opencode: "zai",
          antigravity: "antigravity", muse: "meta", openrouter: "openrouter", traycer: "traycer"
        };
        const MODEL_LOGOS = [
          [/claude|opus|sonnet|haiku/i, "claude"], [/gpt|codex/i, "codex"], [/grok/i, "grok"],
          [/glm|z\.ai/i, "zai"], [/muse/i, "meta"], [/gemini/i, "gemini"]
        ];
        const iconFor = id => {
          const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
          svg.setAttribute("viewBox", "0 0 24 24");
          svg.setAttribute("aria-hidden", "true");
          const use = document.createElementNS("http://www.w3.org/2000/svg", "use");
          use.setAttribute("href", "/logos.svg#" + id);
          svg.append(use);
          return svg;
        };

        function planGroups(data) {
          const groups = [];
          for (const provider of (data.capacity || [])) {
            const key = provider.providerKey || "";
            if (key === "cursor") continue;
            const name = shortNames[key] || provider.shortName || provider.providerName || key || "Plan";
            const sourceRows = Array.isArray(provider.rows) && provider.rows.length ? provider.rows : [provider];
            const rows = sourceRows.map(row => {
              const value = row.pct == null || row.pct === "" ? NaN : Number(row.pct);
              return {
                limitKey: row.limitKey || "",
                label: row.label || row.limitKey || provider.plan || "",
                title: [provider.providerName || name, row.label || provider.plan || ""].filter(Boolean).join(" · "),
                pct: Number.isFinite(value) ? value : null,
                payg: Boolean(row.isPayg || provider.isPayg),
                used: row.used == null || row.used === "" ? null : Number(row.used),
                allowance: row.allowance == null || row.allowance === "" ? null : Number(row.allowance),
                unit: row.unit || "",
                source: row.source || "",
                resetsAt: row.resetsAt || "",
                polledAt: row.polledAt || row.observedAt || provider.polledAt || "",
                forecast: row.forecast && typeof row.forecast === "object" ? row.forecast : {}
              };
            });
            const polledAt = provider.polledAt || (rows.find(r => r.polledAt)?.polledAt) || "";
            // An extra Claude account lane opens Claude Code's tool view.
            const toolKey = provider.toolKey || key;
            if (rows.length) groups.push({ key, toolKey, name, accountLabel: provider.accountLabel || "", plan: provider.plan || "", rows, providerName: provider.providerName || name, polledAt });
          }
          return groups;
        }
        const shownPeak = group => {
          const finite = group.rows.map(item => item.pct).filter(value => value != null);
          if (!finite.length) return group.rows.some(item => item.payg) ? -2 : -1;
          return Math.max(...finite);
        };

        function windowTip(q) {
          const when = resetWhen(q.resetsAt);
          const until = resetIn(q.resetsAt);
          const elapsed = windowElapsedPct(q);
          const f = q.forecast || {};
          const fstate = f.state || "";
          const usageRows = [];
          if (q.payg) {
            const balance = q.unit === "usd" && q.used != null
              ? "Balance " + usd2(q.used)
              : (q.used != null && qtyText(q.used, q.unit) ? qtyText(q.used, q.unit) + " remaining" : null);
            if (balance) usageRows.push(tipRow(esc(balance + " · pay-as-you-go"), "accent"));
          } else if (q.allowance != null && q.allowance > 0 && q.used != null) {
            usageRows.push(tipRow(esc(qtyText(q.used, q.unit) + " of " + qtyText(q.allowance, q.unit) + " used"), "accent"));
          } else if (q.used != null && (q.unit === "pct" || q.unit === "percent")) {
            usageRows.push(tipRow(esc(Math.round(q.used) + "% of the window cap used"), "accent"));
          }
          const resetRows = [];
          if (when) resetRows.push(tipRow(esc("Resets " + when + " · in " + (until || "under a minute"))));
          if (elapsed != null && q.pct != null && !q.payg) {
            const delta = Math.round(q.pct - elapsed);
            const paceCls = delta >= 15 ? "dry" : delta >= 5 ? "tight" : "safe";
            const paceText = delta > 2
              ? "Pace: +" + delta + "% ahead of even window pace (" + Math.round(elapsed) + "% elapsed)"
              : delta < -2
                ? "Pace: " + Math.abs(delta) + "% under safe even pace (" + Math.round(elapsed) + "% elapsed)"
                : "Pace: right on even window pace (" + Math.round(elapsed) + "% elapsed)";
            resetRows.push(tipRow(esc(paceText), paceCls));
          }
          if (fstate === "tight" || fstate === "dry") {
            if (f.line) resetRows.push(tipRow(esc(f.line), fstate));
            if (f.burnPctPerHour != null) resetRows.push(tipRow(esc("+" + (Math.round(f.burnPctPerHour * 10) / 10).toFixed(1) + "%/hr recent burn pace")));
          } else if (fstate === "stale") {
            resetRows.push(tipRow("Pace unknown — the newest quota sample is stale."));
          }
          const rowAge = relTime(q.polledAt);
          if (rowAge) resetRows.push(tipRow(esc("Polled " + rowAge + " · click to inspect tool")));
          if (q.pct == null && !q.payg && q.source) resetRows.push(tipRow(esc(q.source)));
          return '<b class="tip-title">' + esc(q.title) + "</b>" +
            usageRows.join("") +
            (resetRows.length ? '<span class="sep"></span>' + resetRows.join("") : "");
        }

        function windowRow(group, q, isPeak) {
          const cell = document.createElement("div");
          cell.className = "win" + (isPeak ? " is-peak" : "");
          const f = q.forecast || {};
          const fstate = f.state || "";
          const label = document.createElement("span");
          label.className = "win-label";
          const longLabel = document.createElement("span");
          longLabel.className = "long";
          longLabel.textContent = windowName(group.key, q.label);
          const shortLabel = document.createElement("span");
          shortLabel.className = "short";
          shortLabel.textContent = windowShort(group.key, q.label);
          label.append(longLabel, shortLabel);
          const meter = document.createElement("div");
          meter.className = "meter" + (q.payg ? " payg" : "");
          const fill = document.createElement("i");
          fill.className = "fill";
          meter.append(fill);
          if (q.pct != null && !q.payg) {
            const p = Math.max(0, Math.min(100, q.pct));
            meter.style.setProperty("--fill", p.toFixed(1) + "%");
            if (fstate === "dry" || p >= 90) cell.classList.add("crit");
            else if (fstate === "tight" || p >= 75) cell.classList.add("warn");
          }
          const elapsed = windowElapsedPct(q);
          if (elapsed != null && !q.payg) {
            const tick = document.createElement("i");
            tick.className = "win-pace-tick";
            tick.style.left = elapsed.toFixed(2) + "%";
            meter.append(tick);
          }
          const value = document.createElement("b");
          value.textContent = q.pct == null ? (q.payg ? "PAYG" : "—") : pctText(q.pct);
          if (q.pct == null && q.payg) value.className = "payg";
          const eta = document.createElement("i");
          eta.className = "win-reset";
          eta.textContent = resetIn(q.resetsAt);
          cell.append(label, meter, value, eta);
          cell.dataset.tip = windowTip(q);
          return cell;
        }

        function renderPlans(groups) {
          const badge = $("hud-quota-badge");
          badge.textContent = groups.length ? String(groups.length) : "—";
          badge.dataset.tip =
            '<b class="tip-title">Plans</b>' +
            tipRow(esc(groups.length ? groups.length + (groups.length === 1 ? " subscription tracked" : " subscriptions tracked") + ", sorted by what runs out first." : "No subscription usage yet."));
          const quotasRoot = $("hud-quotas");
          if (!groups.length) {
            quotasRoot.innerHTML = '<p class="hud-empty">No subscription usage yet.</p>';
            return;
          }
          quotasRoot.replaceChildren(...groups.map(group => {
            const card = document.createElement("article");
            card.className = "plan";
            card.setAttribute("aria-label", group.name);
            card.dataset.providerKey = group.key;
            card.addEventListener("click", () => openToolInDashboard(group.toolKey || group.key));
            const head = document.createElement("header");
            head.className = "plan-head";
            const mark = document.createElement("span");
            mark.className = "mark";
            mark.append(iconFor(PLAN_LOGOS[group.key] || (String(group.key).startsWith("claude") ? "claude" : "custom")));
            const name = document.createElement("b");
            if (group.accountLabel) {
              // Mini mode keeps only the account ("Work"); the Claude mark says the rest.
              const long = document.createElement("span");
              long.className = "long";
              long.textContent = group.name;
              const short = document.createElement("span");
              short.className = "short";
              short.textContent = group.accountLabel;
              name.append(long, short);
            } else {
              name.textContent = group.name;
            }
            const em = document.createElement("em");
            const planLabel = group.key === "opencode" && group.plan === "Max" ? "Coding Plan Max" : group.plan;
            const ageLabel = relTime(group.polledAt);
            em.textContent = [planLabel, ageLabel].filter(Boolean).join(" · ");
            head.append(mark, name, em);
            head.dataset.tip =
              '<b class="tip-title">' + esc(group.providerName) + "</b>" +
              tipRow(esc((group.plan ? group.plan + " · " : "") + (ageLabel ? "polled " + ageLabel + " · " : "") + "click to open tool detail."));
            const windows = document.createElement("div");
            windows.className = "windows";
            const peak = group.rows.reduce((best, row) => (row.pct != null && (best == null || row.pct > best.pct) ? row : best), null) || group.rows[0];
            for (const q of group.rows) windows.append(windowRow(group, q, q === peak));
            card.append(head, windows);
            return card;
          }));
        }

        function renderAgents(data) {
          const liveAgents = (data.activity || []).filter(item => String(item.state || "").toUpperCase() === "LIVE");
          const miniLive = $("hud-mini-live");
          miniLive.hidden = !liveAgents.length;
          miniLive.textContent = liveAgents.length ? "● " + liveAgents.length + " live" : "";
          const liveSection = $("hud-live");
          const agentsRoot = $("hud-agents");
          liveSection.hidden = !liveAgents.length;
          agentsRoot.replaceChildren(...liveAgents.slice(0, 3).map(agent => {
            const row = document.createElement("div");
            row.className = "hud-agent";
            const mainWrap = document.createElement("div");
            mainWrap.className = "hud-agent-main";
            const pulse = document.createElement("span");
            pulse.className = "hud-agent-mark";
            const modelId = MODEL_LOGOS.find(([pattern]) => pattern.test(String(agent.modelKey || "")))?.[1] || "custom";
            const badge = document.createElement("i");
            badge.className = "hud-pulse";
            pulse.append(iconFor(modelId), badge);
            const textWrap = document.createElement("div");
            textWrap.className = "hud-agent-text";
            const rawName = String(agent.name || "Active session").replace(/\s+/g, " ").trim() || "Active session";
            const shortName = rawName.length > 54 ? rawName.slice(0, 53).trimEnd() + "…" : rawName;
            const strong = document.createElement("strong");
            strong.textContent = shortName;
            const small = document.createElement("small");
            small.textContent = agent.modelKey || "live";
            textWrap.append(strong, small);
            mainWrap.append(pulse, textWrap);
            const tag = document.createElement("span");
            tag.className = "hud-machine-tag";
            tag.textContent = agent.machineName || "LIVE";
            row.append(mainWrap, tag);
            const modelLower = String(agent.modelKey || "").toLowerCase();
            const inferredTool = modelLower.includes("claude") ? "claude-code" : modelLower.includes("gpt") || modelLower.includes("codex") ? "codex" : modelLower.includes("grok") ? "grok" : modelLower.includes("muse") ? "muse" : "";
            const whereLabel = [agent.modelKey || "live", agent.machineName || ""].filter(Boolean).join(" · ");
            row.dataset.tip = '<b class="tip-title">' + esc(shortName) + '</b>' + tipRow(esc(whereLabel), "accent") + tipRow("Click to inspect tool in dashboard");
            row.addEventListener("click", () => openToolInDashboard(inferredTool));
            return row;
          }));
        }

        function renderUsage(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          $("hud-date").textContent = "Today · " + new Date().toLocaleDateString("en-US", {
            weekday: "short", month: "short", day: "numeric"
          });

          setStat("hud-burn-rate", nav.burnRatePerDay != null ? usd(nav.burnRatePerDay) : "—");
          setStat("hud-today", usd2(nav.todayUsd));
          setStat("hud-mini-today", usd2(nav.todayUsd));
          setStat("hud-cache", totals.cacheReusePct == null || totals.cacheReusePct === ""
            ? "—"
            : Math.round(Number(totals.cacheReusePct)) + "%");
          setStat("hud-tokens", tok(nav.todayTokens ?? 0));
          statTips(nav, totals);
          const imported = relTime(data.historyUpdatedAt);
          for (const id of ["hud-stat-today", "hud-stat-cache", "hud-stat-tokens", "hud-stat-burn"]) {
            const node = $(id);
            if (node) node.dataset.tip += '<span class="sep"></span>' +
              tipRow(esc("Recorded usage" + (imported ? " · imported " + imported : "") + ". Open the full dashboard to refresh history."));
          }
        }

        function renderSummary(data) {
          statusView.summary = data.status || "live";
          statusView.failing = data.failingSource || "";
          statusView.offline = false;
          paintStatus();
          renderAgents(data);

          const groups = planGroups(data);
          groups.sort((a, b) => shownPeak(b) - shownPeak(a));
          const topGroup = groups.find(g => shownPeak(g) >= 0) || groups[0] || null;
          const miniBar = $("hud-mini-bar");
          if (topGroup) {
            const topRow = topGroup.rows.find(r => r.pct != null) || topGroup.rows[0];
            const until = topRow ? resetIn(topRow.resetsAt) : "";
            $("hud-mini-lead").textContent = topGroup.name + (topRow ? " · " + windowName(topGroup.key, topRow.label) : "") + (until ? " (" + until + ")" : "");
            $("hud-mini-pct").textContent = topRow && topRow.pct != null ? pctText(topRow.pct) : "—";
            miniBar.dataset.providerKey = topGroup.toolKey || topGroup.key;
          } else {
            $("hud-mini-lead").textContent = "All subscription limits healthy";
            $("hud-mini-pct").textContent = "—";
            miniBar.dataset.providerKey = "";
          }
          renderPlans(groups);
          $("hud-updated-foot").textContent = "Updated " + new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
          syncHudWindowSize();
        }

        let livePending = false, usagePending = false, machinesPending = false;
        async function refresh() {
          if (livePending) return;
          livePending = true;
          const machine = selectedMachine;
          try {
            const res = await fetch("/api/spend/hud?machine=" + encodeURIComponent(machine || "all"), {cache: "no-store"});
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json();
            if (machine === selectedMachine) renderSummary(data);
          } catch {
            statusView.offline = true;
            paintStatus();
          } finally {
            livePending = false;
          }
        }

        async function refreshUsage() {
          if (usagePending) return;
          usagePending = true;
          const machine = selectedMachine;
          try {
            const res = await fetch("/api/spend/hud/usage?machine=" + encodeURIComponent(machine || "all"), {cache: "no-store"});
            if (!res.ok) return;
            const data = await res.json();
            if (machine === selectedMachine) renderUsage(data);
          } catch {} finally { usagePending = false; }
        }

        async function refreshMachines() {
          if (machinesPending) return;
          machinesPending = true;
          try {
            const res = await fetch("/api/machines", {cache: "no-store"});
            if (res.ok) renderMachines((await res.json()).machines || []);
          } catch {} finally { machinesPending = false; }
        }

        // This computer's own freshness (local only, never the hub's view):
        // a stalled source, a meter holding an old reading, a failing sync.
        const statusView = {summary: "loading", failing: "", offline: false, freshness: null};
        const ago = seconds => {
          const n = Number(seconds);
          if (seconds == null || !Number.isFinite(n)) return "";
          if (n < 90) return Math.round(n) + "s";
          if (n < 5400) return Math.round(n / 60) + "m";
          if (n < 172800) return Math.round(n / 3600) + "h";
          return Math.round(n / 86400) + "d";
        };
        function paintStatus() {
          const pill = $("hud-status-pill");
          const miniAlert = $("hud-mini-alert");
          const fresh = statusView.freshness;
          const all = fresh && Array.isArray(fresh.issues) ? fresh.issues : [];
          const issues = all.filter(item => item.severity !== "info");
          const notes = all.filter(item => item.severity === "info");
          const lead = issues[0];
          const summary = statusView.offline ? "offline" : statusView.summary;
          let state = summary === "success" || summary === "live" || summary === "loading" ? "live" : summary === "offline" ? "error" : summary;
          let label = summary === "offline" ? "OFFLINE" : summary === "stale" ? "STALE" : summary === "error" ? "ERR" : "LIVE";
          let title = summary === "offline" ? "Offline" : summary === "stale" ? "Stale" : summary === "error" ? "Ingest failed" : "Live ingest";
          let body = summary === "offline"
            ? tipRow(esc("The local BURNRATE service is unreachable."))
            : summary === "stale"
              ? tipRow(esc("Ingest has not refreshed on schedule" + (statusView.failing ? " · " + statusView.failing : "") + "."))
              : summary === "error"
                ? tipRow(esc("A source failed to collect" + (statusView.failing ? ": " + statusView.failing : "") + "."))
                : lead ? "" : tipRow(esc("Every source on this computer is collecting on schedule."));
          if (state === "live" && lead) {
            state = fresh.status === "alarm" ? "error" : "stale";
            label = (lead.short + " " + lead.state).toUpperCase() + (issues.length > 1 ? " +" + (issues.length - 1) : "");
            title = issues.length === 1 ? "1 source needs attention" : issues.length + " sources need attention";
          }
          if (!statusView.offline) {
            issues.forEach(item => {
              const age = ago(item.ageSeconds);
              body += tipRow(esc(item.short + " · " + item.state + (age ? " " + age : "")), item.severity === "alarm" ? "dry" : "tight") +
                tipRow(esc(item.detail));
            });
            if (notes.length) body += '<span class="sep"></span>' + notes.map(item => tipRow(esc(item.detail))).join("");
            const beat = fresh && fresh.ingest;
            if (beat && beat.lastPassSeconds != null) {
              body += '<span class="sep"></span>' + tipRow(esc("Last collection pass " + beat.lastPassSeconds + "s, " + (ago(beat.lastPassAgeSeconds) || "0s") + " ago."));
            }
          }
          pill.dataset.state = state;
          $("hud-status-text").textContent = label;
          pill.dataset.tip = '<b class="tip-title">' + esc(title) + "</b>" + body;
          miniAlert.hidden = state === "live";
          miniAlert.dataset.state = state;
          miniAlert.textContent = state === "live" ? "" : "! " + (lead && summary !== "offline" && summary !== "stale" && summary !== "error"
            ? lead.short.toUpperCase() + (issues.length > 1 ? " +" + (issues.length - 1) : "")
            : label);
          miniAlert.dataset.tip = pill.dataset.tip;
        }
        async function refreshFreshness() {
          try {
            const res = await fetch("/api/spend/freshness?view=hud", {cache: "no-store"});
            if (!res.ok) throw new Error(String(res.status));
            statusView.freshness = await res.json();
          } catch {
            statusView.freshness = null;
          }
          paintStatus();
        }

        refresh();
        refreshUsage();
        refreshMachines();
        refreshFreshness();
        setInterval(refresh, 5000);
        setInterval(refreshUsage, 20000);
        setInterval(refreshMachines, 20000);
        setInterval(refreshFreshness, 20000);
        window.addEventListener("focus", () => { refresh(); refreshUsage(); refreshMachines(); refreshFreshness(); });
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) { refresh(); refreshUsage(); refreshMachines(); refreshFreshness(); }
        });
        if (typeof EventSource === "function") {
          let debounce = null;
          try {
            const es = new EventSource("/api/spend/stream");
            es.addEventListener("update", () => {
              if (debounce) clearTimeout(debounce);
              debounce = setTimeout(() => { debounce = null; refresh(); }, 100);
            });
          } catch {}
        }
      })();
    </script>
  </body>
</html>
"""


def render_hud() -> str:
    return HUD_HTML
