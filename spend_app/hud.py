"""Always-on-Top Sidecar HUD for BURNRATE (Tauri native window & browser sidecar)."""

from __future__ import annotations

from importlib.resources import files

HUD_TEMPLATE = r"""<!doctype html>
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
        try { if (localStorage.getItem("burnrate:hud-density") === "comfortable") document.documentElement.classList.add("is-comfortable"); } catch {}
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
        --glass: rgba(244, 242, 238, 0.97);
        --glass-hover: rgb(244, 242, 238);
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
        --glass: rgba(10, 10, 12, 0.97);
        --glass-hover: rgb(10, 10, 12);
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
        cursor: default;
        scrollbar-width: none;
        -webkit-font-smoothing: antialiased;
      }
      html::-webkit-scrollbar, body::-webkit-scrollbar { display: none; width: 0; height: 0; }
      body { min-height: 100vh; border: 1px solid var(--line); border-radius: 10px; }
      button { font: inherit; color: inherit; }
      .page { padding: 10px 14px 12px; }

      /* Glass: the native window is transparent so the card can round its
         corners. The window cannot blur what lies behind it, so the card is
         all but opaque: any more see-through and text in the window behind
         reads through the HUD. */
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
      html.is-glass.is-comfortable .page { width: 420px; }
      html.is-glass.is-mini.is-comfortable .page { width: 344px; }
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
      .top { position: relative; display: flex; align-items: center; justify-content: space-between; gap: 8px; min-height: 30px; cursor: grab; }
      .top:active, .hud-mini-bar:active { cursor: grabbing; }
      .brand { display: inline-flex; align-items: center; gap: 8px; min-width: 0; }
      .brand img { width: 20px; height: 20px; border-radius: 5px; display: block; }
      .brand b { font: 700 12px/1 var(--fd); letter-spacing: 0.08em; }
      .brand b span { color: var(--accent); }
      .hud-controls { position: relative; display: inline-flex; align-items: center; gap: 1px; flex: none; }
      .hud-more-wrap { position: relative; display: inline-flex; }
      .hud-more-menu {
        position: absolute;
        top: calc(100% + 4px);
        right: 0;
        z-index: 35;
        min-width: 176px;
        padding: 4px;
        border: 1px solid var(--tip-line);
        border-radius: 9px;
        background: var(--tip-bg);
        box-shadow: var(--tip-shadow);
        display: grid;
        gap: 1px;
        cursor: default;
      }
      .hud-more-menu .hud-btn {
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 9px;
        width: 100%;
        height: 30px;
        padding: 0 9px;
        color: var(--secondary);
        font: 500 11.5px/1.2 var(--sans);
        white-space: nowrap;
      }
      .hud-more-menu .hud-btn:hover { color: var(--text); }
      .hud-menu-label { flex: 1; text-align: left; }
      .hud-quick-plan {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        margin-top: 10px;
        padding: 7px 9px;
        border: 1px solid color-mix(in srgb, var(--accent) 28%, var(--line));
        border-radius: 8px;
        background: var(--accent-soft);
        font-size: 11px;
        line-height: 1.35;
      }
      .hud-quick-plan-copy { min-width: 0; color: var(--text); overflow-wrap: anywhere; }
      .hud-quick-plan-copy small { display: block; color: var(--muted); font-size: 10px; }
      .hud-quick-plan-actions { flex: none; display: inline-flex; align-items: center; gap: 4px; }
      .hud-quick-confirm {
        height: 24px;
        padding: 0 9px;
        border: 1px solid var(--accent);
        border-radius: 6px;
        background: var(--accent);
        color: #fff;
        font: 600 10.5px/1 var(--sans);
        cursor: pointer;
      }
      .hud-quick-dismiss {
        width: 24px;
        height: 24px;
        border: 0;
        border-radius: 6px;
        background: transparent;
        color: var(--muted);
        font-size: 14px;
        line-height: 1;
        cursor: pointer;
      }
      .hud-quick-dismiss:hover { background: var(--hair); color: var(--text); }
      html.is-mini .hud-more-wrap, html.is-mini .hud-quick-plan { display: none !important; }
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
        border: 0;
        background: none;
        padding: 4px 0;
        text-align: left;
        cursor: pointer;
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
      .plan { padding: 10px 0; border-bottom: 1px solid var(--hair); }
      .plan:last-child { border-bottom: 0; }
      .plan-head { display: flex; align-items: center; gap: 8px; min-width: 0; margin-bottom: 7px; }
      .mark { flex: none; width: 16px; height: 16px; display: grid; place-items: center; color: var(--text); }
      .mark svg { width: 15px; height: 15px; display: block; }
      .plan-head b { flex: none; font: 600 13.5px/1 var(--fd); letter-spacing: 0.01em; }
      .plan-head em { min-width: 0; margin-left: auto; overflow: hidden; color: var(--dim); font-size: 11px; font-style: normal; text-overflow: ellipsis; white-space: nowrap; }
      .plan-toggle:hover b { color: var(--accent); }
      .windows { display: grid; gap: 4px; }
      .win { position: relative; display: grid; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px 10px; padding: 5px 0; min-height: 24px; }
      .win-label { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
      .win-label .short { display: none; }
      .plan-head b .short { display: none; }
      html.is-mini .plan-head b .long { display: inline; }
      html.is-mini .plan-head b .short { display: none; }
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
      .win .win-reset { grid-column: 1 / -1; color: var(--muted); font: 500 11px/1.3 var(--sans); font-style: normal; text-align: left; }
      .win.warn .win-reset, .win.crit .win-reset { color: var(--secondary); }
      .hud-empty { padding: 12px 0 4px; color: var(--muted); font-size: 12px; }
      .foot { display: flex; justify-content: space-between; align-items: center; gap: 8px; margin-top: 10px; padding-top: 8px; border-top: 1px solid var(--line); color: var(--dim); font-size: 10.5px; }
      .hud-mini-bar, .mini-head { display: none !important; }

      /* Mini: keep each plan name readable above its tightest window. */
      html.is-mini .page { padding: 6px 10px 8px; }
      html.is-mini .brand, html.is-mini .date-row, html.is-mini .hero, html.is-mini .stats,
      html.is-mini .live-list, html.is-mini .plans > .plans-head, html.is-mini .foot,
      html.is-mini #hud-theme-btn, html.is-mini #hud-setup-btn, html.is-mini #hud-share-btn, html.is-mini #hud-expand-btn { display: none !important; }
      html.is-mini .mini-head { display: grid !important; flex: 1; grid-template-columns: minmax(0, 1fr) auto; align-items: center; gap: 5px 7px; min-width: 0; }
      .mini-head strong { font: 400 25px/1 var(--fd); letter-spacing: -0.01em; }
      .mini-head span { color: var(--dim); font-size: 10.5px; white-space: nowrap; }
      .mini-head .mini-live { color: var(--green); font: 600 9.5px/1 var(--fd); letter-spacing: 0.12em; text-transform: uppercase; }
      .mini-head .mini-today-label { grid-column: 1 / -1; font-size: 11px; color: var(--muted); }
      .mini-head .mini-live { grid-column: 1 / -1; }
      html.is-mini .top { min-height: 28px; }
      html.is-mini .hud-controls { align-self: flex-start; }
      html.is-mini .plans { margin-top: 4px; border-top: 1px solid var(--line); }
      html.is-mini .plan { display: grid; grid-template-columns: minmax(0, 1fr); gap: 2px; padding: 7px 0; }
      html.is-mini .plan-head { margin: 0; }
      html.is-mini .plan-head { gap: 6px; }
      html.is-mini .plan-head em { display: none; }
      html.is-mini .mark { width: 13px; height: 13px; }
      html.is-mini .mark svg { width: 13px; height: 13px; }
      html.is-mini .plan-head b { flex: 1 1 auto; min-width: 0; font-size: 12px; white-space: normal; overflow-wrap: anywhere; }
      html.is-mini .win { grid-template-columns: minmax(0, 1fr) auto; gap: 4px 8px; padding: 2px 0; }
      html.is-mini .win b { font-size: 14px; }
      html.is-mini .win b.payg { font-size: 9.5px; }
      html.is-mini .win b.bal { font-size: 12px; }
      html.is-mini .meter { height: 7px; }

      /* Glance information stays visible at both densities. */
      [hidden] { display: none !important; }
      button:focus-visible, a:focus-visible { outline: 2px solid var(--accent); outline-offset: 3px; }
      .hud-context-disclosure { margin-top: 8px; border-top: 1px solid var(--hair); }
      .hud-context-summary { display: flex; align-items: center; justify-content: space-between; gap: 8px; padding: 6px 0; cursor: pointer; list-style: none; }
      .hud-context-summary::-webkit-details-marker { display: none; }
      .hud-context-summary::after { content: "⌄"; flex: none; display: inline-block; line-height: 1; color: var(--muted); transition: transform .15s ease; }
      .hud-context-disclosure[open] .hud-context-summary::after { transform: rotate(180deg); }
      .hud-context-summary:focus-visible { outline: 2px solid var(--accent); outline-offset: 2px; }
      .hud-context-panel { padding: 4px 0 7px; }
      .hud-context-panel > .density-btn { display: block; margin-left: auto; }
      .hud-context-copy { min-width: 0; display: grid; gap: 3px; }
      #hud-machine-label { flex: 1 1 auto; min-width: 0; color: var(--secondary); font-size: 11px; font-weight: 600; overflow-wrap: anywhere; }
      #hud-updated-compact { flex: none; color: var(--muted); font-size: 10.5px; white-space: nowrap; }
      #hud-updated-compact.stale { color: var(--accent); }
      .density-btn, .coverage-btn { flex: none; border: 1px solid var(--line); border-radius: 6px; background: transparent; color: var(--secondary); min-height: 28px; padding: 4px 8px; font-size: 11px; cursor: pointer; }
      .coverage-btn { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 30%, var(--line)); background: var(--accent-soft); font: 600 10px/1.2 var(--sans); }
      .mini-head .coverage-btn { justify-self: end; }
      .hero small .coverage-btn { display: block; margin: 6px 0 0 auto; }
      .density-btn:hover, .plan-pin:hover, .plan-detail:hover { background: var(--hair); color: var(--text); }
      .attention-trigger { display: flex; align-items: center; justify-content: space-between; gap: 8px; width: 100%; margin-top: 10px; border: 1px solid color-mix(in srgb, var(--accent) 24%, var(--line)); border-radius: 7px; background: var(--accent-soft); color: var(--accent); padding: 8px 10px; font: 600 11px/1.35 var(--sans); text-align: left; cursor: pointer; }
      .attention-trigger::after { content: "›"; flex: none; display: inline-block; font-size: 16px; line-height: 1; transition: transform .15s ease; }
      .attention-trigger[aria-expanded="true"]::after { transform: rotate(90deg); }
      .attention-trigger[data-state="error"] { color: var(--danger); }
      .hud-disclosure { margin-top: 8px; padding: 12px; border: 1px solid var(--line); border-radius: 8px; background: var(--surface); cursor: default; }
      .hud-disclosure > header { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 9px; }
      .hud-disclosure h2 { font: 600 13px/1.3 var(--sans); }
      .hud-disclosure p, .hud-disclosure li { color: var(--secondary); font-size: 11px; line-height: 1.5; overflow-wrap: anywhere; }
      .hud-disclosure ul { padding-left: 16px; margin-top: 7px; }
      .hud-issue + .hud-issue { border-top: 1px solid var(--line); margin-top: 10px; padding-top: 10px; }
      .hud-issue strong { display: block; font-size: 12px; margin-bottom: 4px; }
      .issue-action { border: 1px solid var(--line); background: var(--sunken); padding: 6px 9px; margin-top: 8px; border-radius: 6px; font-size: 11px; min-height: 30px; cursor: pointer; }
      .issue-action + .issue-action { margin-left: 6px; }
      .plan-head { gap: 4px; }
      .plan-toggle { display: flex; align-items: center; gap: 7px; flex: 1; min-width: 0; padding: 2px 0; min-height: 30px; background: transparent; border: 0; text-align: left; cursor: pointer; }
      .plan-toggle b { flex: 1; min-width: 0; overflow-wrap: anywhere; line-height: 1.3; }
      .plan-chevron { flex: none; display: grid; place-items: center; width: 16px; height: 16px; overflow: hidden; line-height: 1; font-size: 16px; color: var(--muted); transition: transform .15s ease; }
      .plan-toggle[aria-expanded="true"] .plan-chevron { transform: rotate(90deg); }
      .plan-window-count { color: var(--muted); font: 500 10px/1.25 var(--sans); white-space: nowrap; }
      .plan-pin, .plan-detail { flex: none; display: grid; place-items: center; width: 28px; height: 28px; border: 0; border-radius: 6px; color: var(--dim); background: transparent; cursor: pointer; }
      .plan-pin[aria-pressed="true"] { color: var(--accent); background: var(--accent-soft); }
      .plan-pin svg, .plan-detail svg { width: 13px; height: 13px; fill: none; stroke: currentColor; stroke-width: 1.8; stroke-linecap: round; stroke-linejoin: round; }
      .plan-extra { display: grid; gap: 4px; }
      .win-label { font: 500 11px/1.3 var(--sans); letter-spacing: 0; text-transform: none; color: var(--secondary); white-space: normal; }
      .win > .meter { grid-column: 1 / -1; grid-row: 2; }
      .win-value { font-variant-numeric: tabular-nums; white-space: nowrap; }
      .win-freshness { grid-column: 1 / -1; color: var(--accent); font: 500 10.5px/1.35 var(--sans); }
      .balances { margin-top: 8px; border-top: 1px solid var(--line); padding-top: 10px; }
      .balances > .plans-head { padding-bottom: 6px; border: 0; }
      .balance-row { display: grid; grid-template-columns: minmax(0, 1fr) auto; gap: 4px 10px; padding: 9px 0; border-bottom: 1px solid var(--hair); }
      .balance-row:last-child { border-bottom: 0; }
      .balance-name { color: var(--text); font: 600 12px/1.3 var(--fd); overflow-wrap: anywhere; }
      .balance-value { color: var(--text); font: 500 16px/1.2 var(--fd); text-align: right; white-space: nowrap; }
      .balance-unit { color: var(--muted); font-size: 10px; text-align: right; }
      .balance-runway { grid-column: 1; grid-row: 2; color: var(--muted); font-size: 10.5px; line-height: 1.4; }
      .hud-urgent { margin-top: 9px; padding: 7px 9px; border-left: 2px solid var(--accent); color: var(--secondary); background: var(--accent-soft); font-size: 11px; line-height: 1.45; }
      .hud-urgent strong { color: var(--accent); }
      .hud-urgent[data-state="quiet"] { border-color: var(--line); background: transparent; color: var(--muted); }
      .hud-urgent[data-state="critical"] { border-color: var(--danger); color: var(--danger); background: color-mix(in srgb, var(--danger) 6%, transparent); }
      .balance-row.crit .balance-value { color: var(--danger); }
      .balance-row.warn .balance-value { color: var(--accent); }
      .plan-toggle:disabled { cursor: default; }
      .win-number .short, .win-reset-value .short { display: none; }

      /* Collapsed is a true glance strip, sized for the real eleven-provider
         account. Detailed wording, pin controls and extra windows remain in
         the expanded view or explicit disclosures. */
      html.is-mini .page { padding: 6px 10px; }
      html.is-mini .top { min-height: 34px; }
      html.is-mini .hud-btn { width: 24px; height: 24px; }
      html.is-mini .mini-head { gap: 3px 5px; }
      html.is-mini .mini-head strong { font-size: 21px; }
      html.is-mini .mini-today-label { grid-column: 1; grid-row: 2; font-size: 10px; }
      html.is-mini .mini-live { grid-column: 2; grid-row: 2; justify-self: end; text-align: right; font-size: 9px; }
      html.is-mini .coverage-btn { min-height: 22px; padding: 3px 6px; }
      html.is-mini .hud-context-disclosure { margin-top: 5px; }
      html.is-mini .hud-context-summary { padding: 4px 0; }
      html.is-mini #hud-machine-label { font-size: 10px; overflow: hidden; white-space: nowrap; text-overflow: ellipsis; }
      html.is-mini #hud-updated-compact { margin-left: auto; font-size: 10px; white-space: nowrap; }
      html.is-mini .attention-trigger { margin-top: 4px; padding: 4px 7px; min-height: 24px; font-size: 10px; }
      html.is-mini .hud-urgent { margin-top: 4px; padding: 4px 7px; font-size: 10px; }
      html.is-mini .plan { grid-template-columns: 112px minmax(0, 1fr); align-items: center; gap: 6px; padding: 3px 0; min-height: 29px; }
      html.is-mini .plan-toggle { min-height: 24px; gap: 4px; padding: 0; width: 100%; flex: 1 1 0; }
      html.is-mini .plan-head { gap: 0; }
      html.is-mini .plan-head b { font-size: 11.5px; line-height: 1.15; }
      html.is-mini .plan-chevron { width: 16px; height: 16px; font-size: 12px; }
      html.is-mini .plan-window-count, html.is-mini .plan-pin, html.is-mini .plan-detail { display: none; }
      html.is-mini .windows, html.is-mini .plan-extra { gap: 2px; }
      html.is-mini .win { grid-template-columns: 24px minmax(0, 1fr) 28px 24px; gap: 4px; padding: 0; min-height: 22px; }
      html.is-mini .win-label { grid-column: 1; grid-row: 1; font: 500 9px/1.1 var(--fd); white-space: nowrap; }
      html.is-mini .win-label .long, html.is-mini .win-number .long, html.is-mini .win-reset-value .long { display: none; }
      html.is-mini .win-label .short, html.is-mini .win-number .short, html.is-mini .win-reset-value .short { display: inline; }
      html.is-mini .win > .meter { grid-column: 2; grid-row: 1; height: 7px; }
      html.is-mini .win-value { grid-column: 3; grid-row: 1; font-size: 12px; }
      html.is-mini .win-unit, html.is-mini .win-reset-prefix, html.is-mini .win-freshness { display: none; }
      html.is-mini .win .win-reset { grid-column: 4; grid-row: 1; text-align: right; font: 500 10px/1 var(--fd); }
      html.is-mini .win.held .win-value { color: var(--accent); }
      html.is-mini .balances { margin-top: 4px; padding-top: 7px; }
      html.is-mini .balances > .plans-head { padding-bottom: 3px; font-size: 9px; }
      html.is-mini .balance-row { grid-template-columns: minmax(0, 1fr) auto; gap: 2px 8px; padding: 5px 0; }
      html.is-mini .balance-name { font-size: 11.5px; }
      html.is-mini .balance-value { font-size: 13px; }
      html.is-mini .balance-runway, html.is-mini .balance-unit { font-size: 9px; }
      html.is-mini.is-comfortable .plan { grid-template-columns: 132px minmax(0, 1fr); min-height: 34px; padding: 5px 0; }
      html.is-mini.is-comfortable .plan-head b { font-size: 13px; }
      html.is-mini.is-comfortable .win-label { font-size: 10px; }
      html.is-mini.is-comfortable .win-value { font-size: 13px; }
      html.is-comfortable .page { padding: 12px 16px 14px; }
      html.is-comfortable .hud-btn { width: 32px; height: 32px; }
      html.is-comfortable .plan { padding: 12px 0; }
      html.is-comfortable .plan-toggle { min-height: 34px; }
      html.is-comfortable .plan-head b, html.is-comfortable .balance-name { font-size: 14px; }
      html.is-comfortable .plan-window-count, html.is-comfortable .win-freshness, html.is-comfortable #hud-updated-compact, html.is-comfortable .balance-runway { font-size: 12px; }
      html.is-comfortable .win-label, html.is-comfortable .win .win-reset, html.is-comfortable .hud-disclosure p, html.is-comfortable .hud-disclosure li, html.is-comfortable .attention-trigger, html.is-comfortable #hud-machine-label { font-size: 12px; }
      html.is-comfortable .plan-pin, html.is-comfortable .plan-detail { width: 32px; height: 32px; }
      html.is-comfortable .win { padding: 6px 0; gap: 6px 10px; }
      /* Tall provider lists and open details remain visibly scrollable. */
      html { scrollbar-width: thin; scrollbar-color: var(--track) transparent; }
      html::-webkit-scrollbar { display: block; width: 5px; }
      html::-webkit-scrollbar-thumb { background: var(--track); border-radius: 3px; }
      html.is-glass .page:not(.morphing) { max-width: 100%; }

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
      .tt-hero span { color: var(--muted); font: 10.5px/1.3 var(--sans); text-align: right; }
      .tt-hero span b { display: block; color: var(--text); font-size: 11.5px; font-weight: 600; }
      .tt-kv { display: grid; grid-template-columns: auto minmax(0, 1fr); gap: 4px 12px; margin-top: 9px; }
      .tt-kv dt { color: var(--dim); }
      .tt-kv dd { min-width: 0; color: var(--text); font-weight: 600; text-align: right; }
      .tt-kv dd small { display: block; color: var(--dim); font-size: 10px; font-weight: 400; }
      .tt-kv dd.tt-note { grid-column: 1 / -1; margin-top: -1px; margin-bottom: 3px; color: var(--dim); font-size: 10.5px; font-weight: 400; line-height: 1.4; text-align: left; }
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
      html.is-comfortable .hud-more-menu .hud-btn { width: 100%; height: 32px; }
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
          <strong id="hud-mini-today">—</strong>
          <button type="button" id="hud-mini-coverage-btn" class="coverage-btn" aria-expanded="false" aria-controls="hud-coverage-panel" hidden>Partial</button>
          <span class="mini-today-label">API value today</span><span class="mini-live" id="hud-mini-live" hidden></span>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn" id="hud-mini-btn" aria-label="Collapse HUD" aria-pressed="false" data-tip>
            <svg class="icon-fold" viewBox="0 0 24 24" aria-hidden="true"><g><path d="M14 10l7-7"/><path d="M20 10h-6V4"/></g><g><path d="M3 21l7-7"/><path d="M4 14h6v6"/></g></svg>
          </button>
          <button type="button" class="hud-btn active" id="hud-pin-btn" aria-label="Pin always on top" aria-pressed="true" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 16v5"/><path class="pin-head" d="M9 4h6l-1 5 3 3v3H7v-3l3-3z"/></svg>
          </button>
          <div class="hud-more-wrap" id="hud-more-wrap">
            <button type="button" class="hud-btn" id="hud-more-btn" aria-label="More actions" aria-expanded="false" aria-controls="hud-more-menu" data-tip>
              <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="5" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="12" cy="12" r="1.5" fill="currentColor" stroke="none"/><circle cx="19" cy="12" r="1.5" fill="currentColor" stroke="none"/></svg>
            </button>
            <div class="hud-more-menu" id="hud-more-menu" role="menu" hidden>
              <button type="button" class="hud-btn" id="hud-expand-btn" role="menuitem" aria-label="Open dashboard">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M14 4h6v6M20 4l-9 9M18 14v5a1 1 0 0 1-1 1H5a1 1 0 0 1-1-1V7a1 1 0 0 1 1-1h5"/></svg>
                <span class="hud-menu-label">Open dashboard</span>
              </button>
              <button type="button" class="hud-btn" id="hud-setup-btn" role="menuitem" aria-label="Setup and connections">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="3"/><path d="M12 3v3M12 18v3M3 12h3M18 12h3M5.6 5.6l2.2 2.2M16.2 16.2l2.2 2.2M5.6 18.4l2.2-2.2M16.2 7.8l2.2-2.2"/></svg>
                <span class="hud-menu-label">Setup &amp; connections</span>
              </button>
              <button type="button" class="hud-btn" id="hud-share-btn" role="menuitem" aria-label="Copy this week's card">
                <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M12 15V3M7 8l5-5 5 5M5 13v7h14v-7"/></svg>
                <span class="hud-menu-label">Copy weekly card</span>
              </button>
              <button type="button" class="hud-btn" id="hud-theme-btn" role="menuitem" aria-label="Switch theme" aria-pressed="false">
                <svg viewBox="0 0 24 24" aria-hidden="true"><circle cx="12" cy="12" r="8"/><path d="M12 4a8 8 0 0 1 0 16z" fill="currentColor" stroke="none"/></svg>
                <span class="hud-menu-label" id="hud-theme-label">Switch theme</span>
              </button>
            </div>
          </div>
          <button type="button" class="hud-btn" id="hud-hide-btn" aria-label="Hide HUD" data-tip>
            <svg viewBox="0 0 24 24" aria-hidden="true"><path d="M5 12h14"/></svg>
          </button>
        </div>
      </header>

      <p class="date-row">
        <span class="date" id="hud-date">Today</span>
        <button type="button" class="hud-status-pill" id="hud-status-pill" data-state="live" aria-expanded="false" aria-controls="hud-attention-panel"><i></i><span id="hud-status-text">LIVE</span></button>
      </p>
      <button type="button" class="hud-update" id="hud-update" hidden></button>
      <p class="hud-notice" id="hud-notice" role="status" hidden></p>
      <p class="hud-notice" id="hud-owner-scope" role="status" hidden><span>Showing this computer only. </span><a href="/auth" id="hud-owner-signin">Sign in for All computers</a></p>

      <section class="hero" id="hud-stat-today" aria-label="API-rate spend today">
        <strong id="hud-today">—</strong>
        <small>API value today<br>since midnight<button type="button" id="hud-coverage-btn" class="coverage-btn" aria-expanded="false" aria-controls="hud-coverage-panel" hidden>Partial</button></small>
      </section>

      <details class="hud-context-disclosure" id="hud-context-disclosure">
        <summary class="hud-context-summary"><span id="hud-machine-label">All computers</span><span id="hud-updated-compact">Updating…</span></summary>
        <div class="hud-context-panel">
          <button type="button" class="density-btn" id="hud-density-btn" aria-pressed="false" aria-label="Use comfortable spacing">Compact</button>
          <div id="hud-machines" role="radiogroup" aria-label="Computer filter"></div>
          <a href="/auth" id="hud-owner-access">Owner access and sessions</a>
        </div>
      </details>
      <button type="button" class="attention-trigger" id="hud-mini-alert" aria-expanded="false" aria-controls="hud-attention-panel" hidden>1 source needs attention</button>

      <section class="hud-disclosure" id="hud-coverage-panel" aria-labelledby="hud-coverage-title" hidden>
        <header><h2 id="hud-coverage-title">Pricing coverage</h2><button type="button" class="hud-btn" id="hud-coverage-close" aria-label="Close pricing coverage">×</button></header>
        <div id="hud-coverage-detail"></div>
      </section>
      <section class="hud-disclosure" id="hud-attention-panel" aria-labelledby="hud-attention-title" hidden>
        <header><h2 id="hud-attention-title">Source status</h2><button type="button" class="hud-btn" id="hud-attention-close" aria-label="Close source status">×</button></header>
        <div id="hud-attention-content"></div>
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

      <section class="live-list" id="hud-live" hidden aria-label="Running agents across computers">
        <div id="hud-agents"></div>
      </section>

      <div class="hud-mini-bar" id="hud-mini-bar" hidden role="button" tabindex="0" aria-label="Top plan bottleneck">
        <span id="hud-mini-lead">All subscription limits healthy</span>
        <strong id="hud-mini-pct">—</strong>
      </div>

      <div class="hud-quick-plan" id="hud-quick-plan" hidden></div>

      <section class="plans" aria-label="Subscription usage">
        <div class="plans-head">
          <span>Plans<span id="hud-quota-badge">—</span></span>
          <button type="button" class="hud-poll-btn" id="hud-poll-btn" data-tip><svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 11a8 8 0 1 0-2.3 5.7M20 4v7h-7"/></svg><span>Poll</span></button>
        </div>
        <div class="hud-urgent" id="hud-urgent" hidden></div>
        <div id="hud-quotas">
          <p class="hud-empty">Waiting for subscription usage…</p>
        </div>
      </section>

      <section class="balances" id="hud-balances-section" aria-label="Pay-as-you-go balances" hidden>
        <div class="plans-head"><span>Balances</span><span>Pay as you go</span></div>
        <div id="hud-balances"></div>
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
/*__HUD_SCRIPT__*/
    </script>
  </body>
</html>
"""

from pathlib import Path

# Embed the packaged browser entry so the HUD needs no additional request and
# keeps the same inline-script CSP and early theme bootstrap in every install.
_HUD_JS_PATH = Path(__file__).resolve().parents[1] / "spend_web" / "hud" / "hud.js"


def _read_hud_js() -> str:
    if _HUD_JS_PATH.is_file():
        return _HUD_JS_PATH.read_text(encoding="utf-8")
    return files("spend_web").joinpath("hud", "hud.js").read_text(encoding="utf-8")


HUD_HTML = HUD_TEMPLATE.replace("/*__HUD_SCRIPT__*/\n", _read_hud_js())
_HUD_CACHE: tuple[int | None, str] = (
    _HUD_JS_PATH.stat().st_mtime_ns if _HUD_JS_PATH.is_file() else None,
    HUD_HTML,
)


def render_hud(*, dashboard: bool = False) -> str:
    global _HUD_CACHE, HUD_HTML
    try:
        mtime_ns = _HUD_JS_PATH.stat().st_mtime_ns if _HUD_JS_PATH.is_file() else None
    except OSError:
        mtime_ns = _HUD_CACHE[0]
    if mtime_ns is not None and mtime_ns != _HUD_CACHE[0]:
        HUD_HTML = HUD_TEMPLATE.replace("/*__HUD_SCRIPT__*/\n", _read_hud_js())
        _HUD_CACHE = (mtime_ns, HUD_HTML)
    html = _HUD_CACHE[1]
    if dashboard:
        return html
    return html.replace('<html lang="en">', '<html lang="en" data-dashboard="off">', 1)
