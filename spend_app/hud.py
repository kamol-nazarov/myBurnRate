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
        // A native shell with a transparent HUD window asks for glass; older
        // shells and the browser sidecar stay opaque.
        if (new URLSearchParams(location.search).get("glass") === "1") document.documentElement.classList.add("is-glass");
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
        --tip-line: rgba(20, 18, 15, 0.12);
        --tip-shadow: 0 12px 30px -12px rgba(20, 18, 15, 0.4), 0 2px 6px -2px rgba(20, 18, 15, 0.12);
        --glass: rgba(244, 242, 238, 0.8);
        --glass-hover: rgba(244, 242, 238, 0.96);
        --glass-line: rgba(20, 18, 15, 0.14);
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
        --tip-bg: #18181b;
        --tip-line: rgba(255, 255, 255, 0.1);
        --tip-shadow: 0 14px 36px -10px rgba(0, 0, 0, 0.9);
        --glass: rgba(10, 10, 12, 0.78);
        --glass-hover: rgba(10, 10, 12, 0.95);
        --glass-line: rgba(255, 255, 255, 0.12);
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

      /* Glass: the native window is transparent; the card is see-through at
         rest and firms up under the pointer so it stays easy to read. */
      html.is-glass, html.is-glass body { background: transparent; }
      html.is-glass body { min-height: 0; border: 0; border-radius: 0; }
      html.is-glass .page {
        /* A fixed width, so the card never follows the window mid-morph. */
        width: 380px;
        background: var(--glass);
        border: 1px solid var(--glass-line);
        border-radius: 12px;
        transition: background-color 0.25s ease;
      }
      html.is-glass.is-mini .page { width: 296px; }
      /* Whatever shows through shifts the card's tone, so quiet labels sit a
         step closer to the text colour than on the opaque card. */
      html.is-glass { --dim: #66625c; --muted: #55524c; }
      html.is-glass[data-theme="dark"] { --dim: #94908a; --muted: #aaa69f; }
      html.is-glass body:hover .page { background: var(--glass-hover); }
      html.is-glass .hud-machine-chip.active { background: color-mix(in srgb, var(--text) 88%, transparent); }
      html.is-glass #hud-machines { background: color-mix(in srgb, var(--sunken) 60%, transparent); }
      html.is-glass .hud-pulse { border-color: transparent; }
      .page.morphing { overflow: hidden; }

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
      /* One icon for collapse/expand: each corner arrow turns about its own
         centre, so "inward" becomes "outward" as the HUD changes size. */
      .icon-fold g { transform-box: fill-box; transform-origin: center; transition: transform 0.36s cubic-bezier(.3, .7, .2, 1); }
      html.is-mini .icon-fold g { transform: rotate(180deg); }

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
      .hud-update {
        display: block;
        width: 100%;
        margin: 0 0 8px;
        padding: 6px 10px;
        border: 1px solid var(--accent);
        border-radius: 8px;
        background: transparent;
        color: var(--accent);
        font: 600 11px/1.3 var(--fd);
        letter-spacing: 0.04em;
        text-align: left;
        cursor: pointer;
      }
      .hud-update[hidden] { display: none; }
      .hud-notice { margin: 0 0 8px; color: var(--secondary); font: 600 11px/1.3 var(--fd); letter-spacing: 0.04em; }
      .hud-notice[hidden] { display: none; }
      .hud-update:disabled { cursor: default; opacity: 0.7; }
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

      /* Repos: today's API-rate value by repository. */
      .repos { margin-top: 12px; }
      .repo-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 10px; padding: 6px 0 5px; border-bottom: 1px solid var(--line); }
      .repo-row:last-child { border-bottom: 0; }
      .repo-name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; font-size: 12px; font-weight: 600; }
      .repo-row.other .repo-name { color: var(--dim); font-weight: 400; }
      .repo-value { color: var(--secondary); font: 11px/1.3 var(--mono); text-align: right; white-space: nowrap; }
      .repo-bar { grid-column: 1 / -1; height: 3px; border-radius: 2px; background: var(--sunken); overflow: hidden; }
      .repo-bar i { display: block; height: 100%; background: var(--accent); }
      html.is-mini .repos { display: none !important; }

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
      .win b.bal { font-size: 13px; }
      .win.warn b { color: var(--accent); }
      .win.crit b { color: var(--danger); }
      .win.held b, .win.held .meter { opacity: 0.7; }
      html[data-dashboard="off"] #hud-expand-btn { display: none; }
      html[data-dashboard="off"] .plan, html[data-dashboard="off"] .hud-agent { cursor: default; }
      html[data-dashboard="off"].is-mini .plan { cursor: pointer; }
      .win .win-reset { color: var(--dim); font: 500 11px/1 var(--fd); font-style: normal; text-align: right; }
      .win.warn .win-reset, .win.crit .win-reset { color: var(--secondary); }
      .hud-empty { padding: 12px 0 4px; color: var(--muted); font-size: 12px; }
      .foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line); color: var(--dim); font-size: 10.5px; }
      .hud-mini-bar, .mini-head { display: none !important; }

      /* Mini: a glance strip. One line per plan, its tightest window only. */
      html.is-mini .page { padding: 6px 10px 8px; }
      html.is-mini .brand, html.is-mini .date-row, html.is-mini .hero, html.is-mini .stats,
      html.is-mini #hud-machines, html.is-mini .live-list, html.is-mini .plans-head, html.is-mini .foot,
      html.is-mini #hud-theme-btn, html.is-mini #hud-setup-btn, html.is-mini #hud-share-btn, html.is-mini #hud-expand-btn { display: none !important; }
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
      html.is-mini .plan.open .win:not(.is-peak) { display: grid; }
      html.is-mini .win { grid-template-columns: 34px minmax(0, 1fr) 36px 26px; gap: 6px; }
      html.is-mini .win b { font-size: 14px; }
      html.is-mini .win b.payg { font-size: 9.5px; }
      html.is-mini .win b.bal { font-size: 12px; }
      html.is-mini .meter { height: 7px; }

      /* Tooltips: one solid card, a title row, an optional big figure,
         label/value rows and a muted footer. Never translucent. */
      #hud-tip {
        position: fixed;
        top: 0;
        left: 0;
        z-index: 40;
        width: max-content;
        max-width: 252px;
        padding: 9px 11px 10px;
        border-radius: 9px;
        background: var(--tip-bg);
        border: 1px solid var(--tip-line);
        box-shadow: var(--tip-shadow);
        font-size: 11.5px;
        line-height: 1.4;
        color: var(--secondary);
        pointer-events: none;
        visibility: hidden;
        opacity: 0;
        transform: translateY(3px);
        transition: opacity 0.14s ease, transform 0.14s ease, visibility 0s linear 0.14s;
      }
      #hud-tip.on { visibility: visible; opacity: 1; transform: none; transition: opacity 0.14s ease, transform 0.14s ease; }
      #hud-tip.snap { transition: none; }
      #hud-tip.is-wide { width: 252px; }
      #hud-tip.is-label { padding: 5px 8px; border-radius: 7px; color: var(--text); font-size: 11px; font-weight: 600; white-space: nowrap; }
      .tt-head { display: flex; align-items: baseline; justify-content: space-between; gap: 10px; }
      .tt-head b { min-width: 0; overflow: hidden; color: var(--text); font: 600 12.5px/1.25 var(--fd); text-overflow: ellipsis; white-space: nowrap; }
      .tt-head span { flex: none; color: var(--dim); font-size: 10.5px; }
      .tt-hero { display: flex; align-items: flex-end; justify-content: space-between; gap: 10px; margin-top: 7px; }
      .tt-hero strong { color: var(--text); font: 400 24px/0.9 var(--fd); letter-spacing: -0.01em; }
      .tt-hero span { color: var(--muted); font-size: 10.5px; line-height: 1.3; text-align: right; }
      .tt-hero span b { display: block; color: var(--text); font-size: 11.5px; font-weight: 600; }
      .tt-kv { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; margin-top: 9px; }
      .tt-kv dt { color: var(--dim); }
      .tt-kv dd { min-width: 0; color: var(--text); font-weight: 600; text-align: right; }
      .tt-kv dd small { display: block; color: var(--dim); font-size: 10px; font-weight: 400; }
      .tt-line { margin-top: 6px; }
      .tt-line.dim, .tt-foot { color: var(--dim); }
      .tt-foot { margin-top: 9px; font-size: 10.5px; }
      .tt-sep { height: 1px; margin: 9px 0 8px; border: 0; background: var(--tip-line); }
      .tt-sep + .tt-foot { margin-top: 0; }
      .tt-section { display: flex; justify-content: space-between; gap: 10px; color: var(--dim); font: 600 9.5px/1.2 var(--fd); letter-spacing: 0.14em; text-transform: uppercase; }
      .tt-section b { color: var(--text); font: 600 11.5px/1 var(--fd); letter-spacing: 0; }
      .tt-models { display: grid; gap: 6px; margin-top: 7px; list-style: none; }
      .tt-models li { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 3px 10px; align-items: baseline; }
      .tt-models span { overflow: hidden; color: var(--text); text-overflow: ellipsis; white-space: nowrap; }
      .tt-models b { color: var(--secondary); font-weight: 600; }
      .tt-models i { grid-column: 1 / -1; display: block; height: 3px; border-radius: 2px; background: var(--track); }
      .tt-models i::before { content: ""; display: block; width: var(--share, 0%); min-width: 2px; height: 100%; border-radius: inherit; background: var(--meter); }
      .tt-models li.more span, .tt-models li.more b { color: var(--dim); font-weight: 400; }
      #hud-tip .safe { color: var(--green); }
      #hud-tip .tight { color: var(--accent); }
      #hud-tip .dry { color: var(--danger); }
      @media (prefers-reduced-motion: reduce) {
        *, *::before, *::after { animation: none !important; transition: none !important; }
      }
      #hud-updated-foot.stale { color: #ff5f56; font-weight: 700; }
    </style>
  </head>
  <body>
    <main class="page" id="hud-shell">
      <div id="hud-body">
      <header class="top">
        <span class="brand"><img src="/favicon.svg" alt=""><b>BURN<span>RATE</span></b></span>
        <div class="mini-head" aria-live="polite">
          <strong id="hud-mini-today">—</strong><span class="mini-today-label">today</span><span class="mini-live" id="hud-mini-live" hidden></span><span class="mini-alert" id="hud-mini-alert" hidden></span>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn" id="hud-mini-btn" aria-label="Collapse HUD" aria-pressed="false" data-tip>
            <svg class="icon-fold" viewBox="0 0 24 24" aria-hidden="true"><g><path d="M14 10l7-7"/><path d="M20 10h-6V4"/></g><g><path d="M3 21l7-7"/><path d="M4 14h6v6"/></g></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-theme-btn" aria-label="Switch theme" aria-pressed="false" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-share-btn" aria-label="Copy this week's card" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3M7 8l5-5 5 5M5 13v7h14v-7"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-setup-btn" aria-label="Setup and connections" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.2 2.2M16.2 16.2l2.2 2.2M5.6 18.4l2.2-2.2M16.2 7.8l2.2-2.2"/></svg>
          </button>
          <button type="button" class="hud-btn active" id="hud-pin-btn" aria-label="Pin always on top" aria-pressed="true" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16v5"/><path class="pin-head" d="M9 4h6l-1 5 3 3v3H7v-3l3-3z"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-expand-btn" aria-label="Open dashboard" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
          </button>
          <button type="button" class="hud-btn" id="hud-hide-btn" aria-label="Hide HUD" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
        </div>
      </header>

      <p class="date-row">
        <span class="date" id="hud-date">Today</span>
        <span class="hud-status-pill" id="hud-status-pill" data-state="live"><i></i><span id="hud-status-text">LIVE</span></span>
      </p>
      <button type="button" class="hud-update" id="hud-update" hidden></button>
      <p class="hud-notice" id="hud-notice" role="status" hidden></p>

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
          <button type="button" class="hud-poll-btn" id="hud-poll-btn" data-tip><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg><span>Poll</span></button>
        </div>
        <div id="hud-quotas">
          <p class="hud-empty">Waiting for subscription usage…</p>
        </div>
      </section>

      <section class="repos" id="hud-repos" hidden aria-label="Today by repository">
        <div class="plans-head"><span>Repos today</span></div>
        <div id="hud-repo-rows"></div>
      </section>

      <footer class="foot">
        <span id="hud-computers-foot"></span>
        <span id="hud-updated-foot"></span>
      </footer>
      </div>
    </main>

    <div id="hud-tip" role="tooltip" aria-hidden="true"></div>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        const root = document.documentElement;
        // The full dashboard is optional (BURNRATE_ENABLE_DASHBOARD=1).
        const dashboardOn = root.dataset.dashboard !== "off";
        const glass = root.classList.contains("is-glass");
        // True while the card animates between the full and mini layouts.
        let morphing = false;
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
        // A balance short enough for the meter's value column; the tip shows cents.
        const usdShort = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          if (n >= 1000) return "$" + (n / 1000).toFixed(1) + "K";
          if (n >= 10) return "$" + Math.round(n);
          return "$" + n.toFixed(2);
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
        const tok1 = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          const a = Math.abs(n);
          if (a >= 1e9) return (n / 1e9).toFixed(a >= 1e10 ? 1 : 2) + "B";
          if (a >= 1e6) return (n / 1e6).toFixed(a >= 1e8 ? 0 : 1) + "M";
          if (a >= 1e3) return (n / 1e3).toFixed(a >= 1e5 ? 0 : 1) + "K";
          return String(Math.round(n));
        };

        // Tooltip markup. Every builder escapes its own text.
        const T = {
          head: (title, sub) => '<div class="tt-head"><b>' + esc(title) + "</b>" + (sub ? "<span>" + esc(sub) + "</span>" : "") + "</div>",
          hero: (big, strong, side) => '<div class="tt-hero"><strong>' + esc(big) + "</strong><span>" +
            (strong ? "<b>" + esc(strong) + "</b>" : "") + esc(side || "") + "</span></div>",
          kv: rows => {
            const body = rows.filter(Boolean).map(([key, value, cls, note]) =>
              "<dt>" + esc(key) + "</dt><dd" + (cls ? ' class="' + cls + '"' : "") + ">" + esc(value) +
              (note ? "<small>" + esc(note) + "</small>" : "") + "</dd>").join("");
            return body ? '<dl class="tt-kv">' + body + "</dl>" : "";
          },
          line: (text, cls) => text ? '<p class="tt-line' + (cls ? " " + cls : "") + '">' + esc(text) + "</p>" : "",
          section: (label, value) => '<div class="tt-section"><span>' + esc(label) + "</span>" + (value ? "<b>" + esc(value) + "</b>" : "") + "</div>",
          sep: '<hr class="tt-sep">',
          foot: text => text ? '<p class="tt-foot">' + esc(text) + "</p>" : ""
        };

        // An element's tip is built when shown (and rebuilt on every data
        // refresh while open), so an open tip always shows current values.
        const tipContent = new WeakMap();
        function setTip(el, content, kind = "wide") {
          if (!el) return;
          tipContent.set(el, {content, kind});
          el.setAttribute("data-tip", "");
        }
        const labelTip = (el, text) => setTip(el, text, "label");

        const tipEl = $("hud-tip");
        const TIP_DELAY_MS = 320;
        const TIP_WARM_MS = 600;
        const tip = {target: null, pending: null, timer: 0, leaveTimer: 0, restoreTimer: 0, hiddenAt: 0, x: -1, y: -1, inside: false, grown: false};
        const tipUnderPointer = () => {
          if (!tip.inside || tip.x < 0) return null;
          const el = document.elementFromPoint(tip.x, tip.y);
          return el ? el.closest("[data-tip]") : null;
        };
        function placeTip(target) {
          const rect = target.getBoundingClientRect();
          const tw = tipEl.offsetWidth;
          const th = tipEl.offsetHeight;
          const x = Math.min(Math.max(6, rect.left + rect.width / 2 - tw / 2), Math.max(6, window.innerWidth - tw - 6));
          let y = rect.top - th - 6;
          if (y < 6) {
            y = rect.bottom + 6;
            const bottom = y + th + 6;
            if (bottom > window.innerHeight) {
              // Glass: the window is transparent past the card, so it can
              // grow downward for a tall tip instead of clipping it.
              if (glass && !morphing) growWindowForTip(bottom);
              else y = Math.max(6, window.innerHeight - th - 6);
            }
          }
          tipEl.style.left = Math.round(x) + "px";
          tipEl.style.top = Math.round(y) + "px";
        }
        function showTip(target, snap) {
          const entry = target && target.isConnected ? tipContent.get(target) : null;
          const value = entry ? (typeof entry.content === "function" ? entry.content() : entry.content) : "";
          if (!value) { hideTip(); return; }
          clearTimeout(tip.leaveTimer);
          clearTimeout(tip.restoreTimer);
          tip.target = target;
          tipEl.className = "is-" + entry.kind + (snap ? " snap" : "");
          tipEl.innerHTML = entry.kind === "label" ? esc(value) : value;
          placeTip(target);
          tipEl.classList.add("on");
          tipEl.setAttribute("aria-hidden", "false");
        }
        function hideTip() {
          clearTimeout(tip.timer);
          clearTimeout(tip.leaveTimer);
          tip.pending = null;
          if (tip.target) tip.hiddenAt = Date.now();
          tip.target = null;
          tipEl.classList.remove("on", "snap");
          tipEl.setAttribute("aria-hidden", "true");
          if (tip.grown) {
            clearTimeout(tip.restoreTimer);
            tip.restoreTimer = setTimeout(() => {
              if (tip.target) return;
              tip.grown = false;
              syncHudWindowSize(true);
            }, 160);
          }
        }
        function wantTip(target) {
          if (morphing) { hideTip(); return; }
          if (target && (target === tip.target || target === tip.pending)) { clearTimeout(tip.leaveTimer); return; }
          clearTimeout(tip.timer);
          tip.pending = null;
          if (!target) {
            // A short grace so crossing the gap between two rows moves the
            // open tip instead of closing and reopening it.
            if (tip.target) { clearTimeout(tip.leaveTimer); tip.leaveTimer = setTimeout(hideTip, 90); }
            return;
          }
          if (tip.target || Date.now() - tip.hiddenAt < TIP_WARM_MS) { showTip(target, Boolean(tip.target)); return; }
          tip.pending = target;
          tip.timer = setTimeout(() => {
            tip.pending = null;
            const live = target.isConnected ? target : tipUnderPointer();
            if (live && tip.inside) showTip(live, false);
          }, TIP_DELAY_MS);
        }
        // After a re-render the hovered element may be a new node: follow the
        // pointer to it, or close the tip when nothing tipped is under it.
        function refreshTip() {
          if (!tip.target) return;
          const target = tip.target.isConnected && tip.target.matches(":hover") ? tip.target : tipUnderPointer();
          if (target) showTip(target, true); else hideTip();
        }
        document.addEventListener("pointermove", event => { tip.x = event.clientX; tip.y = event.clientY; tip.inside = true; }, {passive: true});
        document.addEventListener("pointerover", event => {
          tip.inside = true;
          tip.x = event.clientX;
          tip.y = event.clientY;
          wantTip(event.target instanceof Element ? event.target.closest("[data-tip]") : null);
        });
        const leaveWindow = () => { tip.inside = false; hideTip(); };
        document.addEventListener("pointerout", event => { if (!event.relatedTarget) leaveWindow(); });
        document.documentElement.addEventListener("pointerleave", leaveWindow);
        document.addEventListener("pointerdown", hideTip, true);
        window.addEventListener("wheel", hideTip, {passive: true});
        window.addEventListener("scroll", hideTip, true);
        window.addEventListener("blur", hideTip);
        document.addEventListener("visibilitychange", () => { if (document.hidden) hideTip(); });
        // Last line of defence against a tip left behind by a missed event.
        setInterval(() => { if (tip.target && !(tip.target.isConnected && tip.target.matches(":hover"))) refreshTip(); }, 700);

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

        // An installed app checks for updates in the background and offers one here.
        function showUpdate(version) {
          const bar = $("hud-update");
          if (!bar || !version || bar.disabled) return;
          bar.textContent = "BURNRATE " + version + " is ready · install and restart";
          bar.hidden = false;
        }
        $("hud-update").addEventListener("click", async () => {
          const bar = $("hud-update");
          bar.disabled = true;
          bar.textContent = "Installing the update…";
          // A successful install never answers: the app closes for the installer or restarts.
          await tauriInvoke("install_update");
          bar.disabled = false;
          bar.textContent = "The update did not install · try again";
        });
        tauriInvoke("pending_update").then(showUpdate);
        window.__TAURI__?.event?.listen?.("burnrate://update-available", event => showUpdate(event.payload));
        labelTip($("hud-pin-btn"), () => pinned ? "Unpin from top" : "Keep on top");
        labelTip($("hud-mini-btn"), () => isMini ? "Expand" : "Collapse");
        labelTip($("hud-theme-btn"), () => root.dataset.theme === "dark" ? "Light theme" : "Dark theme");
        labelTip($("hud-expand-btn"), "Open dashboard");
        labelTip($("hud-setup-btn"), "Setup & connections");
        labelTip($("hud-share-btn"), "Copy this week's card to share");
        $("hud-share-btn").addEventListener("click", () => shareWeek());
        // Setup is reachable from the HUD even with the dashboard off: the
        // desktop app opens it in its main window, a browser in a new tab.
        $("hud-setup-btn").addEventListener("click", async () => {
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            try { await internals.invoke("open_setup_wizard"); return; } catch {}
          }
          window.open("/setup", "burnrate-setup");
        });
        labelTip($("hud-hide-btn"), "Hide · reopen from the tray");
        labelTip($("hud-poll-btn"), "Poll every plan now");

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
          if (!dashboardOn) return;
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
        const hudWidth = () => isMini ? 296 : 380;
        // An opaque window frames the card with the body's 1px border; a
        // glass card carries its own border inside its measured rect.
        const frameExtra = glass ? 0 : 2;
        const windowHeightFor = cardHeight => Math.max(160, Math.min(820, Math.ceil(cardHeight) + frameExtra));

        async function setWindowSize(width, height) {
          const handled = await tauriInvoke("set_hud_size", { width, height });
          if (handled !== null) return;
          if (window.opener) {
            try { window.resizeTo(width + 16, height + 38); } catch {}
            return;
          }
          try {
            await fetch("/api/hud/native", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ action: "resize", width, height })
            });
          } catch {}
        }

        function measuredCardHeight() {
          const shell = $("hud-shell");
          return shell.getBoundingClientRect().height || shell.offsetHeight || (isMini ? 200 : 540);
        }

        async function syncHudWindowSize(force = false) {
          // A morph or a tall tip owns the window size until it finishes.
          if (morphing || tip.grown) return;
          const targetWidth = hudWidth();
          const targetHeight = windowHeightFor(measuredCardHeight());
          if (!force && targetWidth === lastAppliedWidth && Math.abs(targetHeight - lastAppliedHeight) <= 2) {
            return;
          }
          lastAppliedWidth = targetWidth;
          lastAppliedHeight = targetHeight;
          await setWindowSize(targetWidth, targetHeight);
          setTimeout(() => {
            if (morphing || tip.grown) return;
            const settled = windowHeightFor(measuredCardHeight());
            if (Math.abs(settled - lastAppliedHeight) > 2) {
              lastAppliedHeight = settled;
              setWindowSize(targetWidth, settled);
            }
          }, 45);
        }

        function growWindowForTip(bottom) {
          const height = Math.min(820, Math.ceil(bottom));
          if (height <= window.innerHeight) return;
          tip.grown = true;
          clearTimeout(tip.restoreTimer);
          setWindowSize(lastAppliedWidth || hudWidth(), height);
        }

        function paintMiniButton() {
          const btn = $("hud-mini-btn");
          btn.setAttribute("aria-pressed", String(isMini));
          btn.setAttribute("aria-label", isMini ? "Expand HUD" : "Collapse HUD");
        }
        paintMiniButton();
        syncHudWindowSize(true);

        // Collapse/expand: the content dissolves, the card glides to its new
        // size with the next layout laid out at its final width (so nothing
        // reflows mid-flight), then the content settles in. A growing card
        // gets its window first; a shrinking one gives it back at the end.
        const MORPH = {fadeOut: 90, resize: 300, fadeIn: 220};
        async function setMini(next) {
          if (morphing || next === isMini) return;
          try { localStorage.setItem("burnrate:hud-mini", next ? "1" : "0"); } catch {}
          hideTip();
          const shell = $("hud-shell");
          const body = $("hud-body");
          if (matchMedia("(prefers-reduced-motion: reduce)").matches || typeof shell.animate !== "function") {
            isMini = next;
            root.classList.toggle("is-mini", isMini);
            paintMiniButton();
            syncHudWindowSize(true);
            return;
          }
          morphing = true;
          try {
            const from = shell.getBoundingClientRect();
            shell.classList.add("morphing");
            shell.style.width = from.width + "px";
            shell.style.height = from.height + "px";
            await body.animate(
              [{opacity: 1, transform: "none"}, {opacity: 0, transform: "scale(0.985)"}],
              {duration: MORPH.fadeOut, easing: "cubic-bezier(.4, 0, 1, 1)", fill: "forwards"}
            ).finished;

            isMini = next;
            root.classList.toggle("is-mini", isMini);
            paintMiniButton();
            const toWidth = hudWidth();
            shell.style.width = toWidth + "px";
            shell.style.height = "auto";
            const toHeight = shell.getBoundingClientRect().height;
            const box = getComputedStyle(shell);
            const inset = ["paddingLeft", "paddingRight", "borderLeftWidth", "borderRightWidth"]
              .reduce((sum, name) => sum + (parseFloat(box[name]) || 0), 0);
            body.style.width = (toWidth - inset) + "px";
            shell.style.width = from.width + "px";
            shell.style.height = from.height + "px";

            if (toWidth > from.width || toHeight > from.height) {
              await setWindowSize(Math.max(toWidth, Math.ceil(from.width)), windowHeightFor(Math.max(toHeight, from.height)));
            }
            const resize = shell.animate(
              [{width: from.width + "px", height: from.height + "px"}, {width: toWidth + "px", height: toHeight + "px"}],
              {duration: MORPH.resize, easing: "cubic-bezier(.22, .8, .24, 1)"}
            );
            body.getAnimations().forEach(animation => animation.cancel());
            const fadeIn = body.animate(
              [{opacity: 0, transform: "translateY(4px)"}, {opacity: 1, transform: "none"}],
              {duration: MORPH.fadeIn, delay: MORPH.resize * 0.2, easing: "cubic-bezier(.2, .7, .3, 1)", fill: "backwards"}
            );
            await Promise.all([resize.finished, fadeIn.finished]);
          } catch {} finally {
            shell.style.width = "";
            shell.style.height = "";
            body.style.width = "";
            shell.classList.remove("morphing");
            body.getAnimations().forEach(animation => animation.cancel());
            morphing = false;
            syncHudWindowSize(true);
          }
        }
        $("hud-mini-btn").addEventListener("click", () => setMini(!isMini));
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
            await refresh(true);
            // A paired spoke answers from its last Hub copy while renewing
            // it; read once more so the Hub's side of the poll shows promptly.
            setTimeout(() => refresh(true), 1500);
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
          } else if (text.includes("month")) {
            // A monthly window runs from the same day of the previous month.
            const start = new Date(resetMs);
            start.setUTCMonth(start.getUTCMonth() - 1);
            durMs = resetMs - start.getTime();
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
            labelTip(btn, () => {
              if (item.id === "all") return "Every computer combined";
              if (item.isLocal || item.id === "local") return "This computer";
              const seen = relTime(item.lastSeenAt);
              return "Paired computer" + (seen ? " · seen " + seen : "");
            });
            btn.addEventListener("click", () => {
              selectedMachine = item.id;
              windowTokens = {};
              refresh();
              refreshUsage(true);
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

        // Days a balance lasts at the recent spend, short enough for the reset column.
        const runwayDays = q => q.payg && q.runway && q.runway.days != null && Number.isFinite(Number(q.runway.days))
          ? Number(q.runway.days) : null;
        function runwayShort(days) {
          if (days < 1) return Math.max(1, Math.round(days * 24)) + "h";
          if (days < 100) return Math.round(days) + "d";
          return days < 365 ? Math.round(days / 30) + "mo" : "1y+";
        }
        const runwayState = days => days < 3 ? "dry" : days < 7 ? "tight" : "safe";

        function resetWhen(iso) {
          const date = new Date(iso || "");
          if (Number.isNaN(date.getTime())) return "";
          return date.toLocaleString([], {month: "short", day: "numeric", hour: "numeric", minute: "2-digit"});
        }

        // Feed health: the newest instant each poll delivered data, plus the
        // age the server reports for the data itself. A wedged request, a dead
        // HUD poller and a stalled collector are all visible; none of them can
        // silently leave a number on screen pretending to be current.
        const FEED_LATE_MS = 30000;
        const USAGE_LATE_MS = 45000;
        const DATA_STALE_SECONDS = 600;
        const feedHealth = {live: 0, usage: 0, machines: 0, freshness: 0, dataAt: null};
        const lastGood = () => feedHealth.live;
        // The payload's age is measured when it was generated; keep the
        // newest data instant across the live and usage reads so neither can
        // rewind the indicator.
        function noteDataAge(data) {
          const age = Number(data.lastIngestAgeSeconds);
          if (!Number.isFinite(age)) return;
          const generated = Date.parse(data.generatedAt || "") || Date.now();
          const at = generated - age * 1000;
          if (feedHealth.dataAt == null || at > feedHealth.dataAt) feedHealth.dataAt = at;
        }
        const dataAgeSeconds = () => (
          feedHealth.dataAt == null ? null : Math.max(0, (Date.now() - feedHealth.dataAt) / 1000)
        );
        const feedLate = () => (
          (feedHealth.live > 0 && Date.now() - feedHealth.live > FEED_LATE_MS)
          || (feedHealth.usage > 0 && Date.now() - feedHealth.usage > USAGE_LATE_MS)
          || (dataAgeSeconds() != null && dataAgeSeconds() > DATA_STALE_SECONDS)
        );
        function paintUpdated() {
          const foot = $("hud-updated-foot");
          if (!feedHealth.live) { foot.textContent = "Updating…"; foot.classList.remove("stale"); return; }
          const ageS = dataAgeSeconds();
          if (ageS != null) {
            foot.textContent = ageS < 2 ? "Updated now" : "Updated " + ago(Math.round(ageS)) + " ago";
          } else {
            foot.textContent = "Waiting for collection";
          }
          foot.classList.toggle("stale", feedLate());
          if (feedLate()) {
            const pill = $("hud-status-pill");
            pill.dataset.state = "stale";
            $("hud-status-text").textContent = "STALE";
            const miniAlert = $("hud-mini-alert");
            miniAlert.hidden = false;
            miniAlert.dataset.state = "stale";
            miniAlert.textContent = "! DATA STALE";
          }
        }

        // Stat tips read the latest usage response when shown.
        let usageView = {nav: {}, totals: {}, importedAt: null};
        const importedFoot = () => {
          const imported = relTime(usageView.importedAt);
          return imported ? "Usage imported " + imported : "";
        };
        setTip($("hud-stat-today"), () => {
          const nav = usageView.nav;
          return T.head("API-rate value", "today") +
            T.hero(usd2(nav.todayUsd), "", "since local midnight") +
            T.line("What today's usage would cost at published API rates. Not a bill.", "dim") +
            (nav.todayComplete === false && nav.todayUsd != null
              ? T.line(nav.todayUnpricedTokens > 0
                  ? "At least this much: " + tok(nav.todayUnpricedTokens) + " tokens have no published price (" +
                    (nav.todayUnpricedModels || []).map(model => String(model).replace(/^[a-z-]+:/, "")).join(", ") + ")."
                  : "At least this much: some usage arrived without complete token counts.", "tight")
              : "") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-burn"), () => {
          const burn = usageView.nav.burnRatePerDay;
          return T.head("Burn rate", "7-day average") +
            T.hero(burn == null ? "—" : usd(burn), "", "per day") +
            T.line("Average API-rate value over the last 7 full days. Today is not counted.", "dim") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-cache"), () => {
          const cache = usageView.totals.cacheReusePct;
          return T.head("Cache hit", "last 24 hours") +
            T.hero(cache == null ? "—" : Math.round(Number(cache)) + "%", "", "of prompt tokens") +
            T.line("Prompt tokens served from cache instead of processed fresh.", "dim") +
            T.foot(importedFoot());
        });
        setTip($("hud-stat-tokens"), () => {
          const nav = usageView.nav;
          return T.head("Tokens", "today") +
            T.hero(tok1(nav.todayTokens ?? 0), "", "since local midnight") +
            T.kv([nav.tokensAvgPerDay == null ? null : ["7-day average", tok1(nav.tokensAvgPerDay) + " / day"]]) +
            T.foot(importedFoot());
        });

        const shortNames = {
          codex: "Codex",
          "claude-code": "Claude",
          antigravity: "Antigravity",
          grok: "Grok",
          opencode: "Z.AI",
          "opencode-go": "OpenCode Go",
          muse: "Muse",
          openrouter: "OpenRouter",
          deepseek: "DeepSeek"
        };
        // Provider marks come from the shared sprite served at /logos.svg.
        const PLAN_LOGOS = {
          codex: "codex", "claude-code": "claude", grok: "grok", opencode: "zai", "opencode-go": "opencode",
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
                forecast: row.forecast && typeof row.forecast === "object" ? row.forecast : {},
                runway: row.runway && typeof row.runway === "object" ? row.runway : null
              };
            });
            const polledAt = provider.polledAt || (rows.find(r => r.polledAt)?.polledAt) || "";
            // An extra Claude account lane opens Claude Code's tool view.
            const toolKey = provider.toolKey || key;
            if (rows.length) groups.push({ key, toolKey, name, accountLabel: provider.accountLabel || "", plan: provider.plan || "", rows, providerName: provider.providerName || name, polledAt });
          }
          return groups;
        }
        // The last real reading of every window, per computer filter. A poll
        // that comes back without a value (a slow provider, a Hub hiccup, a
        // lane between samples) keeps the number on screen until the window
        // resets, and a card missing from one response stays for a while.
        const heldReadings = new Map();
        const HELD_CARD_MS = 15 * 60 * 1000;
        const windowOpen = q => {
          const reset = Date.parse(q.resetsAt || "");
          return !Number.isFinite(reset) || reset > Date.now();
        };
        function holdReadings(groups) {
          const now = Date.now();
          const scope = selectedMachine + "|";
          const seen = new Set();
          for (const group of groups) {
            const key = scope + group.key;
            seen.add(key);
            const prior = heldReadings.get(key);
            group.rows = group.rows.map(q => {
              if (q.pct != null || q.payg) return q;
              const last = prior && prior.group.rows.find(row => row.limitKey === q.limitKey);
              return last && last.pct != null && windowOpen(last) ? {...last, held: true} : q;
            });
            heldReadings.set(key, {group, seenAt: now});
          }
          for (const [key, entry] of heldReadings) {
            if (seen.has(key) || !key.startsWith(scope)) continue;
            if (now - entry.seenAt > HELD_CARD_MS) { heldReadings.delete(key); continue; }
            const rows = entry.group.rows.filter(q => q.pct != null && windowOpen(q)).map(q => ({...q, held: true}));
            if (rows.length) groups.push({...entry.group, rows});
          }
          return groups;
        }
        const shownPeak = group => {
          const finite = group.rows.map(item => item.pct).filter(value => value != null);
          if (!finite.length) return group.rows.some(item => item.payg) ? -2 : -1;
          return Math.max(...finite);
        };

        // Tokens burned inside each plan window, from the usage read and
        // scoped to the selected computer like every other usage figure.
        let windowTokens = {};
        const burnFor = (group, q) => {
          const lane = (windowTokens[group.key] || {})[q.limitKey];
          return lane && Number.isFinite(Number(lane.tokens)) ? lane : null;
        };

        function windowTitle(key, label) {
          const name = windowName(key, label);
          const scoped = /^(Gemini|Other) (.+)$/.exec(name);
          const win = scoped ? scoped[2] : name;
          const text = {"5h": "5-hour window", "Week": "Weekly window", "Wk": "Weekly window", "Month": "Monthly window"}[win] || win;
          return scoped ? scoped[1] + " · " + text.toLowerCase() : text;
        }

        function modelList(today) {
          const models = Array.isArray(today.models) ? today.models : [];
          if (!models.length) return T.line("Nothing yet since midnight.", "dim");
          const total = Number(today.tokens) || models.reduce((sum, m) => sum + Number(m.tokens || 0), 0) || 1;
          const items = models.slice(0, 4).map(m =>
            "<li><span>" + esc(m.name) + "</span><b>" + esc(tok1(m.tokens)) + '</b><i style="--share:' +
            Math.min(100, Number(m.tokens || 0) / total * 100).toFixed(1) + '%"></i></li>');
          const rest = models.slice(4);
          if (rest.length) {
            items.push('<li class="more"><span>' + esc(rest.length === 1 ? "1 more model" : rest.length + " more models") + "</span><b>" +
              esc(tok1(rest.reduce((sum, m) => sum + Number(m.tokens || 0), 0))) + "</b></li>");
          }
          return '<ul class="tt-models">' + items.join("") + "</ul>";
        }

        function windowTip(group, q) {
          const f = q.forecast || {};
          const fstate = f.state || "";
          let html = T.head(group.providerName || group.name, windowTitle(group.key, q.label));
          if (q.payg) {
            const balance = q.unit === "usd" && q.used != null ? usd2(q.used) : (q.used != null ? qtyText(q.used, q.unit) : null);
            html += T.hero(balance || "—", "", balance ? "left · pay as you go" : "pay as you go");
          } else if (q.pct != null) {
            const until = resetIn(q.resetsAt);
            html += T.hero(pctText(q.pct), until ? (until === "now" ? "resets now" : "resets in " + until) : "", resetWhen(q.resetsAt));
          } else {
            html += T.hero("—", "", "no reading");
          }

          const rows = [];
          const runway = q.payg ? q.runway : null;
          const days = runwayDays(q);
          if (days != null) {
            const spend = usd2(runway.dailyUsd) + "/day over the last " + Math.max(1, Math.round(Number(runway.basisDays) || 0)) + " days";
            rows.push(["Runway", String(runway.line || "").split(" at ")[0] || runwayShort(days), runwayState(days), spend]);
          } else if (runway && runway.line) {
            rows.push(["Runway", runway.state === "idle" ? "no recent spend" : "learning", runway.state === "idle" ? "safe" : "", runway.line]);
          }
          if (!q.payg && q.allowance != null && q.allowance > 0 && q.used != null) {
            rows.push(["Used", qtyText(q.used, q.unit) + " of " + qtyText(q.allowance, q.unit)]);
          }
          const lane = q.payg ? null : burnFor(group, q);
          if (lane) {
            const tokens = Number(lane.tokens);
            rows.push(["Burned", tokens ? tok1(tokens) + " tokens" : "none recorded", "",
              tokens ? tok1(lane.input) + " in · " + tok1(lane.output) + " out · " + tok1(lane.cached) + " cached" : ""]);
          }
          // One pace verdict: a run-dry forecast from the recent burn wins
          // over the whole-window average, which it would otherwise contradict.
          const elapsed = windowElapsedPct(q);
          if (fstate === "tight" || fstate === "dry") {
            const [when, before] = String(f.line || "").replace(/^pace\s*→\s*/, "").replace(/\)$/, "").split(" (");
            const lately = f.burnPctPerHour != null ? "+" + (Math.round(f.burnPctPerHour * 10) / 10).toFixed(1) + "%/h lately" : "";
            const measured = f.basis === "usage" && f.tokensPerHour ? " · " + tok1(f.tokensPerHour) + " tok/h measured" : "";
            rows.push(["Pace", when || "running dry", fstate, [before, lately + measured].filter(Boolean).join(" · ")]);
          } else if (elapsed != null && q.pct != null && !q.payg) {
            const delta = Math.round(q.pct - elapsed);
            rows.push(["Pace",
              delta > 2 ? delta + "% ahead of even" : delta < -2 ? Math.abs(delta) + "% under even" : "on even pace",
              delta >= 15 ? "dry" : delta >= 5 ? "tight" : "safe",
              Math.round(elapsed) + "% of the window gone"]);
          } else if (fstate === "stale" && q.pct != null) {
            rows.push(["Pace", "unknown", "", "the newest sample is stale"]);
          }
          html += T.kv(rows);
          const detail = String(q.label || "").split(" · ").slice(1).join(" · ");
          if (q.pct == null && !q.payg && detail) html += T.line(detail, "dim");

          if (lane && lane.today) html += T.sep + T.section("Today by model", tok1(lane.today.tokens)) + modelList(lane.today);

          const age = relTime(q.polledAt);
          if (q.held) html += T.line("Showing the reading from " + (age || "earlier") + "; the newest poll came back empty.", "tight");
          else html += T.foot(age ? "Polled " + age : "");
          return html;
        }

        function windowRow(group, q, isPeak) {
          const cell = document.createElement("div");
          cell.className = "win" + (isPeak ? " is-peak" : "") + (q.held ? " held" : "");
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
          // A pay-as-you-go lane with a known balance shows the money left, not just "PAYG".
          const balance = q.pct == null && q.payg && q.unit === "usd" && q.used != null ? usdShort(q.used) : "";
          value.textContent = q.pct == null ? (balance || (q.payg ? "PAYG" : "—")) : pctText(q.pct);
          if (q.pct == null && q.payg) value.className = balance ? "bal" : "payg";
          const eta = document.createElement("i");
          eta.className = "win-reset";
          const days = runwayDays(q);
          eta.textContent = days != null ? runwayShort(days) : resetIn(q.resetsAt);
          if (days != null && days < 3) cell.classList.add("crit");
          else if (days != null && days < 7) cell.classList.add("warn");
          cell.append(label, meter, value, eta);
          setTip(cell, () => windowTip(group, q));
          return cell;
        }

        const openPlans = new Set();
        function renderPlans(groups) {
          const badge = $("hud-quota-badge");
          badge.textContent = groups.length ? String(groups.length) : "—";
          labelTip(badge, groups.length ? (groups.length === 1 ? "1 plan" : groups.length + " plans") + " · tightest first" : "No plans yet");
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
            // Without the dashboard, a click in mini mode lists every window of the plan.
            card.classList.toggle("open", openPlans.has(group.key));
            card.addEventListener("click", () => {
              if (dashboardOn) { openToolInDashboard(group.toolKey || group.key); return; }
              if (!isMini) return;
              if (!openPlans.delete(group.key)) openPlans.add(group.key);
              card.classList.toggle("open", openPlans.has(group.key));
              hideTip();
              syncHudWindowSize();
            });
            const head = document.createElement("header");
            head.className = "plan-head";
            const mark = document.createElement("span");
            mark.className = "mark";
            const extraLogo = String(group.key).startsWith("claude") ? "claude" : String(group.key).startsWith("opencode-go-") ? "opencode" : "custom";
            mark.append(iconFor(PLAN_LOGOS[group.key] || extraLogo));
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
            // The full row already shows plan and age; the tip earns its
            // place only where that text is hidden or a click does something.
            setTip(head, () => {
              if (!isMini && !dashboardOn) return "";
              const age = relTime(group.polledAt);
              return T.head(group.providerName, planLabel) +
                T.foot([age ? "Polled " + age : "", dashboardOn ? "Click for tool detail" : "Click to show every window"].filter(Boolean).join(" · "));
            });
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
            setTip(row, () => T.head(shortName) + T.line(whereLabel, "dim") + (dashboardOn ? T.foot("Click to open it in the dashboard") : ""));
            if (dashboardOn) row.addEventListener("click", () => openToolInDashboard(inferredTool));
            return row;
          }));
        }

        let heldStats = {day: "", byMachine: {}};
        function renderUsage(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          $("hud-date").textContent = "Today · " + new Date().toLocaleDateString("en-US", {
            weekday: "short", month: "short", day: "numeric"
          });

          // A read without a value keeps this computer's figure until the date
          // changes. Another computer's spend is a different figure.
          const day = new Date().toDateString();
          if (heldStats.day !== day) heldStats = {day, byMachine: {}};
          const slot = heldStats.byMachine[selectedMachine] || (heldStats.byMachine[selectedMachine] = {});
          const keep = (name, value) => value == null || value === ""
            ? slot[name]
            : (slot[name] = value);
          const today = keep("today", nav.todayUsd);
          const burn = keep("burn", nav.burnRatePerDay);
          const cache = keep("cache", totals.cacheReusePct);
          setStat("hud-burn-rate", burn != null ? usd(burn) : "—");
          // A day with usage no card can price is a floor: mark the figure.
          const floor = nav.todayComplete === false && today != null ? "≥" : "";
          setStat("hud-today", floor + usd2(today));
          setStat("hud-mini-today", floor + usd2(today));
          setStat("hud-cache", cache == null ? "—" : Math.round(Number(cache)) + "%");
          setStat("hud-tokens", tok(nav.todayTokens ?? 0));
          usageView = {nav: {...nav, todayUsd: today, burnRatePerDay: burn}, totals: {cacheReusePct: cache}, importedAt: data.historyUpdatedAt};
          noteDataAge(data);
          // An older hub sends no window tokens; meter tips then omit them.
          if (data.windowTokens && typeof data.windowTokens === "object") windowTokens = data.windowTokens;
          renderRepos(data.repos);
          refreshTip();
        }

        let noticeTimer = 0;
        function notice(text) {
          const line = $("hud-notice");
          line.textContent = text;
          line.hidden = false;
          clearTimeout(noticeTimer);
          noticeTimer = setTimeout(() => { line.hidden = true; }, 4000);
        }

        // The weekly card: a 1200 x 630 image of the last seven days, copied
        // to the clipboard. Totals and tool names only, never repositories.
        const CARD_GAUGE = ["M19.9 42L13 46M18 35H10M19.9 28L13 24M25 22.9L21 16M32 21V13M39 22.9L43 16",
                            "M44.1 28L51 24M46 35H54M44.1 42L51 46", "M32 35L44.5 22.5"];
        function drawWeekCard(week) {
          const canvas = document.createElement("canvas");
          canvas.width = 1200;
          canvas.height = 630;
          const ctx = canvas.getContext("2d");
          const display = "Bahnschrift, 'DIN Alternate', 'Segoe UI', system-ui, sans-serif";
          const body = "'Segoe UI Variable Text', 'Segoe UI', system-ui, -apple-system, sans-serif";
          const ink = "#f2f0eb", muted = "#9a968f", ember = "#ff6b22", line = "rgba(255,255,255,0.12)";
          ctx.fillStyle = "#0a0a0c";
          ctx.fillRect(0, 0, 1200, 630);
          ctx.fillStyle = "#121215";
          ctx.fillRect(40, 40, 1120, 550);
          ctx.strokeStyle = line;
          ctx.strokeRect(40.5, 40.5, 1119, 549);
          // Gauge mark and wordmark.
          ctx.save();
          ctx.translate(80, 76);
          ctx.scale(0.75, 0.75);
          ctx.lineCap = "round";
          ctx.lineWidth = 4.2;
          ctx.strokeStyle = ink;
          ctx.stroke(new Path2D(CARD_GAUGE[0]));
          ctx.strokeStyle = ember;
          ctx.stroke(new Path2D(CARD_GAUGE[1]));
          ctx.lineWidth = 3;
          ctx.stroke(new Path2D(CARD_GAUGE[2]));
          ctx.fillStyle = ink;
          ctx.beginPath();
          ctx.arc(32, 35, 4.4, 0, Math.PI * 2);
          ctx.fill();
          ctx.restore();
          ctx.fillStyle = ink;
          ctx.font = "600 22px " + display;
          if ("letterSpacing" in ctx) ctx.letterSpacing = "5px";
          ctx.fillText("BURNRATE", 140, 113);
          if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
          const from = new Date(week.from), to = new Date(week.to);
          const range = from.toLocaleDateString("en-US", {month: "short", day: "numeric"}) + " – " +
            to.toLocaleDateString("en-US", {month: "short", day: "numeric", year: "numeric"});
          ctx.fillStyle = muted;
          ctx.font = "20px " + body;
          ctx.textAlign = "right";
          ctx.fillText("Last 7 days · " + range, 1120, 110);
          ctx.textAlign = "left";
          // Headline: the week's value at API rates.
          ctx.fillStyle = ink;
          ctx.font = "400 120px " + display;
          ctx.fillText(usd(week.usd), 80, 270);
          ctx.fillStyle = muted;
          ctx.font = "26px " + body;
          ctx.fillText("of AI coding at published API rates", 84, 316);
          if (week.planUsd) {
            ctx.fillStyle = ember;
            ctx.font = "600 26px " + body;
            const times = week.usd / week.planUsd;
            ctx.fillText("on " + usd(week.planUsd) + " of plans" + (times >= 1.5 ? " · " + times.toFixed(1) + "× the plan cost" : ""), 84, 356);
          }
          // Stats row.
          const stats = [["Tokens", tok(week.tokens)], ["Cache hit", week.cacheReusePct == null ? "—" : Math.round(week.cacheReusePct) + "%"],
                         ["Sessions", String(week.sessions || 0)]];
          stats.forEach(([label, value], index) => {
            const x = 84 + index * 190;
            ctx.fillStyle = muted;
            ctx.font = "600 15px " + display;
            if ("letterSpacing" in ctx) ctx.letterSpacing = "2px";
            ctx.fillText(label.toUpperCase(), x, 446);
            if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
            ctx.fillStyle = ink;
            ctx.font = "400 44px " + display;
            ctx.fillText(value, x, 496);
          });
          // Top tools with share bars.
          const tools = (week.tools || []).slice(0, 4);
          const top = Math.max(...tools.map(tool => tool.usd), 0);
          ctx.fillStyle = muted;
          ctx.font = "600 15px " + display;
          if ("letterSpacing" in ctx) ctx.letterSpacing = "2px";
          ctx.fillText("TOP TOOLS", 760, 196);
          if ("letterSpacing" in ctx) ctx.letterSpacing = "0px";
          tools.forEach((tool, index) => {
            const y = 240 + index * 70;
            ctx.fillStyle = ink;
            ctx.font = "600 22px " + body;
            ctx.fillText(tool.name, 760, y);
            ctx.fillStyle = muted;
            ctx.font = "20px " + body;
            ctx.textAlign = "right";
            ctx.fillText(usd(tool.usd), 1120, y);
            ctx.textAlign = "left";
            ctx.fillStyle = "rgba(255,255,255,0.08)";
            ctx.fillRect(760, y + 14, 360, 6);
            ctx.fillStyle = ember;
            ctx.fillRect(760, y + 14, top ? Math.max(4, 360 * tool.usd / top) : 0, 6);
          });
          // Footer.
          ctx.strokeStyle = line;
          ctx.beginPath();
          ctx.moveTo(80, 540.5);
          ctx.lineTo(1120, 540.5);
          ctx.stroke();
          ctx.fillStyle = muted;
          ctx.font = "18px " + body;
          ctx.fillText("Measured locally with BURNRATE · nothing left my computer", 80, 572);
          ctx.textAlign = "right";
          ctx.fillText("github.com/kamol-nazarov/myBurnRate", 1120, 572);
          ctx.textAlign = "left";
          return canvas;
        }

        async function shareWeek() {
          let week;
          try {
            const res = await hudFetch("/api/spend/share/week?machine=" + encodeURIComponent(selectedMachine || "all"));
            if (!res.ok) throw new Error(String(res.status));
            week = await res.json();
          } catch {
            notice("This week's card could not be made");
            return;
          }
          const blob = await new Promise(resolve => drawWeekCard(week).toBlob(resolve, "image/png"));
          try {
            await navigator.clipboard.write([new ClipboardItem({"image/png": blob})]);
            notice("This week's card is on the clipboard · paste it anywhere");
          } catch {
            window.open(URL.createObjectURL(blob), "_blank");
            notice("This week's card opened in a new window");
          }
        }

        // Today's value by repository; an older hub sends none and the section stays hidden.
        function renderRepos(repos) {
          const rows = repos && Array.isArray(repos.rows) ? repos.rows.filter(row => Number(row.usd) > 0) : [];
          $("hud-repos").hidden = !rows.length;
          if (!rows.length) return;
          const top = Math.max(...rows.map(row => Number(row.usd) || 0));
          const line = (label, usdValue, tokens, extraClass) => {
            const item = document.createElement("div");
            item.className = "repo-row" + (extraClass ? " " + extraClass : "");
            const name = document.createElement("span");
            name.className = "repo-name";
            name.textContent = label;
            const value = document.createElement("span");
            value.className = "repo-value";
            value.textContent = usd2(usdValue) + " · " + tok(tokens || 0);
            const bar = document.createElement("span");
            bar.className = "repo-bar";
            const fill = document.createElement("i");
            fill.style.width = (top ? Math.max(2, (Number(usdValue) || 0) / top * 100) : 0) + "%";
            bar.append(fill);
            item.append(name, value, bar);
            return item;
          };
          const items = rows.map(row => line(row.project, row.usd, row.tokens));
          if (repos.other && Number(repos.other.usd) > 0) items.push(line("Other", repos.other.usd, repos.other.tokens, "other"));
          $("hud-repo-rows").replaceChildren(...items);
        }

        function renderSummary(data) {
          statusView.summary = data.status || "live";
          statusView.failing = data.failingSource || "";
          statusView.offline = false;
          paintStatus();
          renderAgents(data);

          const groups = holdReadings(planGroups(data));
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
          feedHealth.live = Date.now();
          noteDataAge(data);
          paintUpdated();
          syncHudWindowSize();
          refreshTip();
        }

        // Every poll is bounded: a hung request aborts and reports offline
        // instead of wedging the one-in-flight guard forever.
        const FETCH_TIMEOUT_MS = 12000;
        const hudFetch = (url, options = {}) => {
          const controller = new AbortController();
          const timer = setTimeout(() => controller.abort(), FETCH_TIMEOUT_MS);
          return fetch(url, {...options, cache: "no-store", signal: controller.signal})
            .finally(() => clearTimeout(timer));
        };
        let livePending = null, liveAgain = false, usagePending = false, usageAgain = false, machinesPending = false;
        // One read at a time. A queued request (a manual poll, a stream
        // update) runs again once the current read finishes; timer ticks
        // never stack up behind a slow read.
        function refresh(queue = false) {
          if (livePending) {
            if (queue) liveAgain = true;
            return livePending;
          }
          livePending = (async () => {
            do {
              liveAgain = false;
              const machine = selectedMachine;
              try {
                const res = await hudFetch("/api/spend/hud?machine=" + encodeURIComponent(machine || "all"));
                if (!res.ok) throw new Error(String(res.status));
                const data = await res.json();
                if (machine === selectedMachine) renderSummary(data);
              } catch {
                statusView.offline = true;
                paintStatus();
              }
            } while (liveAgain);
          })().finally(() => { livePending = null; });
          return livePending;
        }

        async function refreshUsage(queue = false) {
          if (usagePending) {
            if (queue) usageAgain = true;
            return;
          }
          usagePending = true;
          try {
            do {
              usageAgain = false;
              const machine = selectedMachine;
              try {
                const res = await hudFetch("/api/spend/hud/usage?machine=" + encodeURIComponent(machine || "all"));
                if (!res.ok) continue;
                const data = await res.json();
                feedHealth.usage = Date.now();
                if (machine === selectedMachine) renderUsage(data);
              } catch {}
            } while (usageAgain);
          } finally { usagePending = false; }
        }

        async function refreshMachines() {
          if (machinesPending) return;
          machinesPending = true;
          try {
            const res = await hudFetch("/api/machines");
            if (res.ok) { feedHealth.machines = Date.now(); renderMachines((await res.json()).machines || []); refreshTip(); }
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
        let statusTip = "";
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
            ? T.line("The local BURNRATE service is unreachable.", "dim")
            : summary === "stale"
              ? T.line("Ingest has not refreshed on schedule" + (statusView.failing ? ": " + statusView.failing : "") + ".", "dim")
              : summary === "error"
                ? T.line("A source failed to collect" + (statusView.failing ? ": " + statusView.failing : "") + ".", "dim")
                : lead ? "" : T.line("Every source on this computer is collecting on schedule.", "dim");
          if (state === "live" && lead) {
            state = fresh.status === "alarm" ? "error" : "stale";
            label = (lead.short + " " + lead.state).toUpperCase() + (issues.length > 1 ? " +" + (issues.length - 1) : "");
            title = issues.length === 1 ? "1 source needs attention" : issues.length + " sources need attention";
          }
          let foot = "";
          if (!statusView.offline) {
            body += T.kv(issues.map(item => {
              const age = ago(item.ageSeconds);
              return [item.short, item.state + (age ? " " + age : ""), item.severity === "alarm" ? "dry" : "tight", item.detail];
            }));
            if (notes.length) body += T.sep + notes.map(item => T.line(item.detail, "dim")).join("");
            const beat = fresh && fresh.ingest;
            if (beat && beat.lastPassSeconds != null) {
              foot = "Last collection pass " + beat.lastPassSeconds + "s, " + (ago(beat.lastPassAgeSeconds) || "0s") + " ago";
            }
          }
          pill.dataset.state = state;
          $("hud-status-text").textContent = label;
          statusTip = T.head(title) + body + T.foot(foot);
          miniAlert.hidden = state === "live";
          miniAlert.dataset.state = state;
          miniAlert.textContent = state === "live" ? "" : "! " + (lead && summary !== "offline" && summary !== "stale" && summary !== "error"
            ? lead.short.toUpperCase() + (issues.length > 1 ? " +" + (issues.length - 1) : "")
            : label);
          refreshTip();
        }
        setTip($("hud-status-pill"), () => statusTip);
        setTip($("hud-mini-alert"), () => statusTip);
        async function refreshFreshness() {
          try {
            const res = await hudFetch("/api/spend/freshness?view=hud");
            if (!res.ok) throw new Error(String(res.status));
            statusView.freshness = await res.json();
            feedHealth.freshness = Date.now();
          } catch {
            statusView.freshness = null;
          }
          paintStatus();
        }

        refresh();
        refreshUsage();
        refreshMachines();
        refreshFreshness();
        setInterval(paintUpdated, 1000);
        setInterval(() => refresh(), 5000);
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
              debounce = setTimeout(() => { debounce = null; refresh(true); }, 100);
            });
          } catch {}
        }
      })();
    </script>
  </body>
</html>
"""


def render_hud(*, dashboard: bool = False) -> str:
    if dashboard:
        return HUD_HTML
    return HUD_HTML.replace('<html lang="en">', '<html lang="en" data-dashboard="off">', 1)
