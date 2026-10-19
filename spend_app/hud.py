"""Always-on-Top Sidecar HUD for BURNRATE (Tauri native window & browser sidecar)."""

from __future__ import annotations

HUD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#f6f5f1">
    <title>BURNRATE · Sidecar HUD</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <style>
      :root {
        color-scheme: light;
        --paper: rgba(246, 245, 241, 0.92);
        --ink: #1c1b19;
        --muted: #6f6a62;
        --faint: #8a847b;
        --peach: rgba(246, 199, 180, 0.88);
        --yellow: rgba(246, 227, 154, 0.88);
        --pink: rgba(246, 211, 222, 0.88);
        --blue: rgba(201, 223, 246, 0.88);
        --docs: rgba(235, 239, 244, 0.86);
        --card: rgba(247, 248, 250, 0.92);
        --plate: rgba(255, 255, 255, 0.96);
        --chrome-bg: rgba(28, 27, 25, 0.055);
        --chrome-border: rgba(28, 27, 25, 0.1);
        --pill-bg: rgba(255, 255, 255, 0.78);
        --ok: #7dcea0;
        --warn: #d9a441;
        --crit: #c45c4a;
        --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
        --sans: "Segoe UI", system-ui, -apple-system, sans-serif;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      html, body {
        background: transparent;
        color: var(--ink);
        font-family: var(--sans);
        font-size: 13px;
        line-height: 1.3;
        overflow-x: hidden;
        overflow-y: auto;
        user-select: none;
        cursor: grab;
        scrollbar-width: thin;
        scrollbar-color: rgba(28, 27, 25, 0.28) transparent;
      }
      body:active { cursor: grabbing; }
      html::-webkit-scrollbar,
      #hud-quotas::-webkit-scrollbar { width: 10px; }
      html::-webkit-scrollbar-thumb,
      #hud-quotas::-webkit-scrollbar-thumb {
        background: rgba(28, 27, 25, 0.18);
        border: 3px solid var(--paper);
        border-radius: 99px;
      }
      .page {
        min-height: 0;
        padding: 8px 12px 10px;
        border-radius: 18px;
        background: var(--paper);
        border: 1px solid var(--chrome-border);
        box-shadow: 0 16px 38px rgba(15, 17, 22, 0.18), inset 0 1px 0 rgba(255, 255, 255, 0.65);
      }
      body.is-pinned .page {
        border-color: rgba(217, 164, 65, 0.38);
      }
      .hud-chrome-bar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 4px 6px 6px;
        margin: -2px -4px 6px;
        border-bottom: 1px solid var(--chrome-border);
      }
      .hud-chrome-brand {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        min-width: 0;
      }
      .hud-grip {
        display: inline-grid;
        grid-template-columns: repeat(3, 3px);
        gap: 2px;
        padding: 2px 1px;
        opacity: 0.45;
      }
      .hud-grip i {
        width: 3px;
        height: 3px;
        border-radius: 50%;
        background: var(--ink);
        display: block;
      }
      .hud-brand-word {
        font-size: 10px;
        font-weight: 800;
        letter-spacing: 0.14em;
        text-transform: uppercase;
        color: var(--ink);
      }
      .hud-brand-word strong {
        color: #c88a1e;
      }
      .hud-pin-badge {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 9px;
        font-weight: 700;
        letter-spacing: 0.06em;
        text-transform: uppercase;
        padding: 1.5px 6px;
        border-radius: 999px;
        background: rgba(217, 164, 65, 0.18);
        color: #9e6c11;
      }
      .hud-pin-badge[hidden] { display: none; }
      .top {
        display: flex;
        align-items: flex-end;
        justify-content: space-between;
        gap: 10px;
      }
      .titles { min-width: 0; }
      .date-row {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
      }
      .date {
        font-size: 11px;
        font-weight: 600;
        letter-spacing: 0.1em;
        text-transform: uppercase;
        color: var(--faint);
      }
      .hud-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 9.5px;
        font-weight: 700;
        letter-spacing: 0.08em;
        padding: 1.5px 6px;
        border-radius: 999px;
        background: var(--pill-bg);
        color: var(--muted);
      }
      .hud-status-pill i {
        width: 6px;
        height: 6px;
        border-radius: 99px;
        background: #7dcea0;
      }
      .hud-status-pill[data-state="stale"] i { background: #d9a441; }
      .hud-status-pill[data-state="error"] i { background: #c45c4a; }
      h1 {
        margin-top: 2px;
        font-family: var(--serif);
        font-weight: 560;
        font-size: 26px;
        letter-spacing: -0.03em;
        line-height: 1;
      }
      .hud-controls {
        display: inline-flex;
        align-items: center;
        gap: 2px;
        flex: none;
        padding: 2px;
        border-radius: 10px;
        background: var(--chrome-bg);
        border: 1px solid var(--chrome-border);
      }
      .hud-btn {
        border: 0;
        background: transparent;
        color: var(--muted);
        font: inherit;
        font-size: 10.5px;
        font-weight: 650;
        padding: 3px 7px;
        border-radius: 7px;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 4px;
        transition: background 0.12s ease, color 0.12s ease, box-shadow 0.12s ease;
      }
      .hud-btn:hover { background: rgba(28, 27, 25, 0.08); color: var(--ink); }
      .hud-btn.active {
        background: var(--ink);
        color: #fff;
        box-shadow: 0 1px 3px rgba(0, 0, 0, 0.16);
      }
      .hud-btn-pin-dot {
        width: 5px;
        height: 5px;
        border-radius: 50%;
        background: currentColor;
        opacity: 0.55;
      }
      .hud-btn.active .hud-btn-pin-dot {
        background: #f5b841;
        opacity: 1;
        box-shadow: 0 0 6px #f5b841;
      }
      .hud-btn--close {
        padding: 3px 6px;
        font-weight: 700;
      }
      .hud-btn--close:hover {
        background: rgba(196, 92, 74, 0.18);
        color: #b43c29;
      }
      #hud-machines {
        display: flex;
        flex-wrap: wrap;
        gap: 6px;
        margin-top: 8px;
      }
      #hud-machines:empty { display: none; }
      .hud-machine-chip {
        display: inline-flex;
        align-items: center;
        gap: 6px;
        border: 0;
        background: var(--plate);
        color: var(--muted);
        font: inherit;
        font-size: 12px;
        font-weight: 600;
        padding: 4px 10px;
        border-radius: 999px;
        cursor: pointer;
      }
      .hud-machine-chip.active { background: var(--ink); color: #fff; }
      .hud-machine-chip i {
        width: 6px;
        height: 6px;
        border-radius: 99px;
        background: #c8c2b8;
      }
      .hud-machine-chip i.live { background: #7dcea0; }
      .stats {
        display: grid;
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 6px;
        margin-top: 10px;
      }
      .stat {
        min-width: 0;
        min-height: 78px;
        padding: 8px 8px 7px;
        border-radius: 14px;
        display: flex;
        flex-direction: column;
      }
      .stat header span {
        font-size: 9px;
        font-weight: 650;
        letter-spacing: 0.09em;
        text-transform: uppercase;
        line-height: 1.3;
        display: block;
      }
      .stat strong {
        margin-top: auto;
        padding-top: 5px;
        font-family: var(--serif);
        font-size: 20px;
        font-weight: 460;
        letter-spacing: -0.03em;
        line-height: 1;
        border-radius: 6px;
        width: max-content;
        max-width: 100%;
      }
      .stat strong.tick { animation: stat-tick 0.8s ease; }
      @keyframes stat-tick {
        0% { background: rgba(255, 255, 255, 0.6); }
        100% { background: transparent; }
      }
      .stat small {
        display: block;
        margin-top: 3px;
        font-size: 9px;
        line-height: 1.25;
        color: rgba(63, 58, 51, 0.72);
      }
      .peach { background: var(--peach); }
      .yellow { background: var(--yellow); }
      .pink { background: var(--pink); }
      .blue { background: var(--blue); }
      .live-list { margin-top: 10px; min-width: 0; }
      #hud-agents { display: grid; gap: 8px; min-width: 0; }
      .live-list[hidden] { display: none; }
      .hud-agent {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        min-width: 0;
        max-width: 100%;
        padding: 10px 12px;
        border-radius: 16px;
        background: var(--plate);
      }
      .hud-agent-main { display: flex; align-items: center; gap: 8px; min-width: 0; }
      .hud-pulse {
        width: 7px;
        height: 7px;
        border-radius: 99px;
        background: #7dcea0;
        flex: none;
      }
      .hud-agent-text { min-width: 0; }
      .hud-agent-text strong, .hud-agent-text small {
        display: block;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .hud-agent-text strong { font-size: 13px; }
      .hud-agent-text small { font-size: 11px; color: var(--muted); }
      .hud-machine-tag {
        flex: none;
        font-size: 11px;
        color: var(--muted);
      }
      .plans {
        margin-top: 10px;
        padding: 8px;
        border-radius: 18px;
        background: var(--docs);
      }
      .plans-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        padding: 2px 4px 4px;
        color: var(--muted);
        font-size: 12px;
        font-weight: 600;
      }
      .plan {
        display: grid;
        grid-template-columns: 20px max-content minmax(0, 1fr);
        gap: 4px 8px;
        align-items: center;
        margin-top: 4px;
        padding: 6px 8px;
        border-radius: 12px;
        background: var(--card);
      }
      .mark {
        grid-row: 1;
        grid-column: 1;
        width: 20px;
        height: 20px;
        display: grid;
        place-items: center;
        color: var(--ink);
      }
      .mark svg { width: 15px; height: 15px; display: block; }
      .plan-main {
        grid-column: 2;
        grid-row: 1;
        display: flex;
        flex-direction: column;
        max-width: 118px;
        min-width: 0;
      }
      .plan b { font-size: 13px; font-weight: 650; }
      .plan em {
        font-style: normal;
        font-size: 10px;
        color: var(--muted);
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
      }
      .windows {
        grid-column: 3;
        grid-row: 1;
        display: grid;
        gap: 4px;
        min-width: 0;
      }
      .plan.is-stack .windows {
        grid-column: 1 / -1;
        grid-row: 2;
      }
      .win {
        position: relative;
        overflow: hidden;
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 6px;
        min-width: 0;
        padding: 5px 10px 9px;
        border-radius: 10px;
        background: var(--plate);
      }
      .win::before {
        content: "";
        position: absolute;
        left: 0;
        right: 0;
        bottom: 0;
        height: 3px;
        background: rgba(28, 27, 25, 0.07);
      }
      .win::after {
        content: "";
        position: absolute;
        left: 0;
        bottom: 0;
        height: 3px;
        width: var(--fill, 0%);
        background: var(--fillc, var(--ok));
        transition: width 0.45s ease;
      }
      .win.crit::after { background: var(--crit); }
      .win.warn::after { background: var(--warn); }
      .win span {
        min-width: 0;
        overflow: hidden;
        text-overflow: ellipsis;
        white-space: nowrap;
        font-size: 12px;
        font-weight: 650;
        color: var(--muted);
      }
      .win i {
        flex: none;
        font-style: normal;
        font-size: 11px;
        font-weight: 650;
        color: var(--faint);
      }
      .win b {
        flex: none;
        margin-left: auto;
        font-family: var(--serif);
        font-size: 20px;
        font-weight: 460;
        letter-spacing: -0.03em;
        line-height: 1;
        color: var(--ink);
      }
      .win.warn i, .win.warn b { color: #a8751b; }
      .win.crit i, .win.crit b { color: #ad4630; }
      .hud-empty { font-size: 12px; color: var(--muted); padding: 8px 6px 4px; }
      .foot {
        display: flex;
        justify-content: space-between;
        gap: 8px;
        margin-top: 12px;
        padding: 0 6px;
        font-size: 11px;
        color: var(--faint);
      }
      #hud-tip {
        position: fixed;
        z-index: 40;
        max-width: 252px;
        padding: 8px 10px;
        border-radius: 12px;
        background: #fffdf8;
        border: 1px solid rgba(28, 27, 25, 0.12);
        box-shadow: 0 8px 22px rgba(28, 27, 25, 0.16);
        font-size: 11px;
        line-height: 1.45;
        color: var(--muted);
        pointer-events: none;
        opacity: 0;
        transform: translateY(2px);
        transition: opacity 0.12s ease, transform 0.12s ease;
      }
      #hud-tip.on { opacity: 1; transform: none; }
      #hud-tip .tip-title {
        display: block;
        font-size: 11.5px;
        font-weight: 650;
        color: var(--ink);
        margin-bottom: 3px;
      }
      #hud-tip .row { display: block; }
      #hud-tip .row.accent { color: var(--ink); font-weight: 600; }
      #hud-tip .sep { display: block; height: 1px; background: rgba(28, 27, 25, 0.09); margin: 5px 0; }
      #hud-tip .row.dry { color: #ad4630; font-weight: 600; }
      #hud-tip .row.tight { color: #a8751b; font-weight: 600; }
      #hud-tip .row.safe { color: #2b7a52; font-weight: 600; }
      .plans-head-right {
        display: inline-flex;
        align-items: center;
        gap: 6px;
      }
      .hud-poll-btn {
        border: 1px solid rgba(28, 27, 25, 0.14);
        background: var(--plate);
        color: var(--ink);
        font: inherit;
        font-size: 10.5px;
        font-weight: 650;
        padding: 2px 8px;
        border-radius: 999px;
        cursor: pointer;
        display: inline-flex;
        align-items: center;
        gap: 4px;
      }
      .hud-poll-btn:hover { background: var(--card); }
      .hud-poll-btn:disabled { opacity: 0.6; cursor: wait; }
      .hud-poll-btn i {
        display: inline-block;
        font-style: normal;
      }
      .hud-poll-btn.spinning i {
        animation: hud-spin 0.75s linear infinite;
      }
      @keyframes hud-spin {
        to { transform: rotate(360deg); }
      }
      .plan, .hud-agent {
        cursor: pointer;
        transition: transform 0.12s ease, box-shadow 0.12s ease, background 0.12s ease;
      }
      .plan:hover, .hud-agent:hover {
        transform: translateY(-1px);
        box-shadow: 0 4px 12px rgba(28, 27, 25, 0.08);
      }
      .plan-age {
        font-size: 9.5px;
        font-weight: 600;
        color: var(--faint);
        letter-spacing: 0.01em;
      }
      .win-pace-tick {
        position: absolute;
        bottom: 0;
        height: 6px;
        width: 2px;
        background: rgba(28, 27, 25, 0.45);
        z-index: 2;
        pointer-events: none;
        border-radius: 1px;
      }
      .hud-mini-bar {
        display: none !important;
      }
      body.is-mini { font-size: 11px; }
      body.is-mini .page { padding: 6px 8px 6px; border-radius: 14px; }
      body.is-mini .hud-chrome-bar { padding: 2px 4px 4px; margin: -1px -2px 4px; }
      body.is-mini .hud-brand-word { font-size: 8.5px; }
      body.is-mini .hud-pin-badge { font-size: 8px; padding: 1px 5px; }
      body.is-mini .top { gap: 6px; align-items: center; }
      body.is-mini .date-row { gap: 5px; }
      body.is-mini .date { font-size: 9px; letter-spacing: 0.08em; }
      body.is-mini .hud-status-pill { font-size: 8.5px; gap: 3px; padding: 1px 5px; }
      body.is-mini .hud-status-pill i { width: 5px; height: 5px; }
      body.is-mini h1 { font-size: 17px; margin-top: 1px; }
      body.is-mini .hud-controls { gap: 1px; padding: 1.5px; border-radius: 8px; }
      body.is-mini .hud-btn { font-size: 9px; padding: 2px 5px; border-radius: 5px; gap: 3px; }
      body.is-mini #hud-machines { gap: 4px; margin-top: 5px; }
      body.is-mini .hud-machine-chip { font-size: 10px; padding: 2px 7px; gap: 4px; }
      body.is-mini .hud-machine-chip i { width: 5px; height: 5px; }
      body.is-mini .stats {
        grid-template-columns: repeat(4, minmax(0, 1fr));
        gap: 4px;
        margin-top: 5px;
      }
      body.is-mini .stat {
        min-height: 44px;
        padding: 4px 6px;
        border-radius: 9px;
      }
      body.is-mini .stat header span {
        font-size: 7.5px;
        letter-spacing: 0.06em;
        line-height: 1.15;
      }
      body.is-mini .stat strong { font-size: 14px; padding-top: 2px; }
      body.is-mini .stat small {
        font-size: 7.5px;
        margin-top: 1px;
        line-height: 1.1;
      }
      body.is-mini .live-list { margin-top: 5px; }
      body.is-mini #hud-agents { gap: 4px; }
      body.is-mini .hud-agent {
        padding: 5px 8px;
        border-radius: 10px;
        gap: 6px;
      }
      body.is-mini .hud-pulse { width: 5px; height: 5px; }
      body.is-mini .hud-agent-text strong { font-size: 11px; }
      body.is-mini .hud-agent-text small,
      body.is-mini .hud-machine-tag { font-size: 9.5px; }
      body.is-mini .plans {
        margin-top: 5px;
        padding: 5px;
        border-radius: 12px;
      }
      body.is-mini .plans-head {
        padding: 1px 3px 3px;
        font-size: 10px;
      }
      body.is-mini .hud-poll-btn {
        font-size: 9px;
        padding: 1px 6px;
        gap: 3px;
      }
      body.is-mini .plan {
        grid-template-columns: 15px max-content minmax(0, 1fr);
        gap: 2px 5px;
        margin-top: 3px;
        padding: 4px 6px;
        border-radius: 8px;
      }
      body.is-mini .mark { width: 15px; height: 15px; }
      body.is-mini .mark svg { width: 12px; height: 12px; }
      body.is-mini .plan-main { max-width: 82px; }
      body.is-mini .plan b { font-size: 10.5px; line-height: 1.1; }
      body.is-mini .plan em { font-size: 8.5px; line-height: 1.1; }
      body.is-mini .windows { gap: 3px; }
      body.is-mini .win {
        padding: 3px 6px 5px;
        border-radius: 7px;
        gap: 4px;
      }
      body.is-mini .win::before,
      body.is-mini .win::after { height: 2px; }
      body.is-mini .win-pace-tick { height: 4px; width: 1.5px; }
      body.is-mini .win span { font-size: 9.5px; }
      body.is-mini .win i { font-size: 8.5px; }
      body.is-mini .win b { font-size: 13.5px; }
      body.is-mini .foot {
        margin-top: 5px;
        padding: 0 3px;
        font-size: 9px;
      }

      body[data-theme="dark"] {
        color-scheme: dark;
        --paper: rgba(13, 16, 22, 0.96);
        --ink: #eef2f7;
        --muted: #9aa3b2;
        --faint: #758092;
        --peach: rgba(217, 122, 84, 0.22);
        --yellow: rgba(217, 164, 65, 0.22);
        --pink: rgba(212, 106, 132, 0.22);
        --blue: rgba(88, 154, 228, 0.22);
        --docs: rgba(21, 26, 34, 0.94);
        --card: rgba(28, 34, 44, 0.95);
        --plate: rgba(36, 43, 56, 0.96);
        --chrome-bg: rgba(255, 255, 255, 0.05);
        --chrome-border: rgba(255, 255, 255, 0.1);
        --pill-bg: rgba(255, 255, 255, 0.07);
      }
      body[data-theme="dark"] .page {
        box-shadow: 0 18px 42px rgba(0, 0, 0, 0.55), inset 0 1px 0 rgba(255, 255, 255, 0.08);
      }
      body[data-theme="dark"].is-pinned .page {
        border-color: rgba(245, 184, 65, 0.42);
      }
      body[data-theme="dark"] .hud-pin-badge {
        background: rgba(245, 184, 65, 0.16);
        color: #f5c45e;
      }
      body[data-theme="dark"] .stat small { color: rgba(226, 232, 240, 0.68); }
      body[data-theme="dark"] .hud-btn:hover { background: rgba(255, 255, 255, 0.09); }
      body[data-theme="dark"] .hud-btn.active {
        background: #eef2f7;
        color: #0e1117;
      }
      body[data-theme="dark"] .hud-btn.active .hud-btn-pin-dot {
        background: #d98218;
        box-shadow: 0 0 6px #d98218;
      }
      body[data-theme="dark"] .hud-poll-btn { border-color: rgba(255, 255, 255, 0.14); }
      body[data-theme="dark"] .win::before { background: rgba(255, 255, 255, 0.09); }
      body[data-theme="dark"] .win-pace-tick { background: rgba(255, 255, 255, 0.65); }
      body[data-theme="dark"] .win.warn i, body[data-theme="dark"] .win.warn b { color: #f3be5c; }
      body[data-theme="dark"] .win.crit i, body[data-theme="dark"] .win.crit b { color: #f28270; }
      body[data-theme="dark"] #hud-tip {
        background: #161b23;
        border-color: rgba(255, 255, 255, 0.14);
        box-shadow: 0 10px 26px rgba(0, 0, 0, 0.45);
      }
      body[data-theme="dark"] #hud-tip .sep { background: rgba(255, 255, 255, 0.1); }
    </style>
  </head>
  <body data-tauri-drag-region="deep" class="is-pinned">
    <main class="page" id="hud-shell">
      <div class="hud-chrome-bar" id="hud-chrome-bar" data-tauri-drag-region>
        <div class="hud-chrome-brand" data-tauri-drag-region>
          <span class="hud-grip" aria-hidden="true"><i></i><i></i><i></i><i></i><i></i><i></i></span>
          <span class="hud-brand-word">BURN<strong>RATE</strong> · HUD</span>
          <span class="hud-pin-badge" id="hud-pin-badge">Pinned</span>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn" id="hud-mini-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Mini HUD&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Shrink the HUD into a compact size while keeping all metrics and plans visible.&lt;/span&gt;" aria-pressed="false">Mini</button>
          <button type="button" class="hud-btn" id="hud-theme-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Theme&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Toggle light or dark HUD palette.&lt;/span&gt;" aria-pressed="false">Dark</button>
          <button type="button" class="hud-btn active" id="hud-pin-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Pin Always-on-Top&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Keep the Sidecar HUD floating above all other windows.&lt;/span&gt;" aria-pressed="true"><i class="hud-btn-pin-dot" aria-hidden="true"></i><span id="hud-pin-text">Pin</span></button>
          <button type="button" class="hud-btn" id="hud-expand-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Open Dashboard&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Open the full dashboard with forecasts, unit economics and plans.&lt;/span&gt;">Open</button>
          <button type="button" class="hud-btn hud-btn--close" id="hud-hide-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Hide HUD&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Hide the HUD. Reopen it from the BURNRATE tray icon or dashboard.&lt;/span&gt;" aria-label="Hide HUD">&#x2715;</button>
        </div>
      </div>

      <header class="top" data-tauri-drag-region>
        <div class="titles" data-tauri-drag-region>
          <p class="date-row">
            <span class="date" id="hud-date">Today</span>
            <span class="hud-status-pill" id="hud-status-pill" data-state="live">
              <i></i><span id="hud-status-text">LIVE</span>
            </span>
          </p>
          <h1>Usage</h1>
        </div>
      </header>

      <div id="hud-machines" role="radiogroup" aria-label="Computer filter"></div>

      <section class="stats" aria-label="Live BurnRate KPIs">
        <article class="stat peach" id="hud-stat-burn">
          <header><span>Avg burn rate</span></header>
          <strong id="hud-burn-rate">—</strong>
          <small>last 7 days</small>
        </article>
        <article class="stat yellow" id="hud-stat-today">
          <header><span>API spend today</span></header>
          <strong id="hud-today">—</strong>
          <small>since midnight</small>
        </article>
        <article class="stat pink" id="hud-stat-cache">
          <header><span>Cache hit rate</span></header>
          <strong id="hud-cache">—</strong>
          <small>last 24 hours</small>
        </article>
        <article class="stat blue" id="hud-stat-tokens">
          <header><span>Tokens used today</span></header>
          <strong id="hud-tokens">—</strong>
          <small>running daily</small>
        </article>
      </section>

      <section class="live-list" id="hud-live" hidden aria-label="Running agents across computers">
        <div id="hud-agents"></div>
      </section>

      <div class="hud-mini-bar" id="hud-mini-bar" hidden role="button" tabindex="0" aria-label="Top plan bottleneck">
        <span id="hud-mini-lead">All subscription limits healthy</span>
        <strong id="hud-mini-pct">—</strong>
      </div>

      <section class="plans" aria-label="Subscription usage">
        <div class="plans-head">
          <span>Plans <span id="hud-quota-badge">—</span></span>
          <div class="plans-head-right">
            <button type="button" class="hud-poll-btn" id="hud-poll-btn" data-tip="&lt;b class=&quot;tip-title&quot;&gt;Poll quotas now&lt;/b&gt;&lt;span class=&quot;row&quot;&gt;Immediately query all connected subscription providers and sync.&lt;/span&gt;"><i>↻</i><span>Poll</span></button>
          </div>
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
        document.addEventListener("mousedown", event => {
          if (event.detail === 2) {
            event.stopImmediatePropagation();
            return;
          }
          if (event.button === 0 && !event.target.closest("button, a, input, .plan, .hud-agent, .hud-machine-chip")) {
            const internals = window.__TAURI_INTERNALS__;
            if (internals && typeof internals.invoke === "function") {
              internals.invoke("start_hud_drag").catch(() => {});
            }
          }
        }, true);

        const $ = id => document.getElementById(id);
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
          return "$" + (Math.round(n * 100) / 100).toLocaleString("en-US", {minimumFractionDigits: 2});
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
          el.textContent = text;
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

        function applyPinUi(stateVal) {
          pinned = Boolean(stateVal);
          try { localStorage.setItem("burnrate:hud-pinned", pinned ? "1" : "0"); } catch {}
          const btn = $("hud-pin-btn");
          if (btn) {
            btn.classList.toggle("active", pinned);
            btn.setAttribute("aria-pressed", String(pinned));
          }
          const badge = $("hud-pin-badge");
          if (badge) badge.hidden = !pinned;
          document.body.classList.toggle("is-pinned", pinned);
        }

        async function syncInitialPin() {
          const tauriState = await tauriInvoke("set_hud_pin", { pinned });
          if (typeof tauriState === "boolean") {
            applyPinUi(tauriState);
            return;
          }
          applyPinUi(pinned);
          if (pinned) {
            try {
              await fetch("/api/hud/native", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ action: "pin" })
              });
            } catch {}
          }
        }
        syncInitialPin();

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
            if (data && data.launchedNative && window.opener) {
              window.close();
              return;
            }
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
          if (handled === null) window.close();
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

        let isMini = false;
        try { isMini = localStorage.getItem("burnrate:hud-mini") === "1"; } catch {}
        let lastAppliedWidth = 0;
        let lastAppliedHeight = 0;

        async function syncHudWindowSize(force = false) {
          const shell = $("hud-shell");
          if (!shell) return;
          const targetWidth = isMini ? 336 : 440;
          const rect = shell.getBoundingClientRect();
          const measuredH = Math.ceil(rect.height || shell.offsetHeight || (isMini ? 340 : 540));
          const targetHeight = Math.max(180, Math.min(820, measuredH + 2));
          if (!force && targetWidth === lastAppliedWidth && Math.abs(targetHeight - lastAppliedHeight) <= 2) {
            return;
          }
          lastAppliedWidth = targetWidth;
          lastAppliedHeight = targetHeight;
          const handled = await tauriInvoke("set_hud_size", { width: targetWidth, height: targetHeight });
          if (handled === null && window.opener) {
            try { window.resizeTo(targetWidth + 16, targetHeight + 38); } catch {}
          }
          setTimeout(async () => {
            const rectAfter = shell.getBoundingClientRect();
            const hAfter = Math.max(180, Math.min(820, Math.ceil(rectAfter.height || shell.offsetHeight || targetHeight) + 2));
            if (Math.abs(hAfter - lastAppliedHeight) > 2) {
              lastAppliedHeight = hAfter;
              await tauriInvoke("set_hud_size", { width: targetWidth, height: hAfter });
            }
          }, 45);
        }

        function applyMini(forceResize = false) {
          document.body.classList.toggle("is-mini", isMini);
          const btn = $("hud-mini-btn");
          if (btn) {
            btn.classList.toggle("active", isMini);
            btn.setAttribute("aria-pressed", String(isMini));
          }
          syncHudWindowSize(forceResize);
        }
        applyMini(true);
        $("hud-mini-btn").addEventListener("click", () => {
          isMini = !isMini;
          try { localStorage.setItem("burnrate:hud-mini", isMini ? "1" : "0"); } catch {}
          applyMini(true);
        });
        $("hud-mini-bar").addEventListener("click", () => {
          const key = $("hud-mini-bar").dataset.providerKey || "";
          openToolInDashboard(key);
        });

        let theme = "light";
        try {
          const saved = localStorage.getItem("burnrate:hud-theme");
          if (saved === "dark" || saved === "light") theme = saved;
          else if (window.matchMedia && window.matchMedia("(prefers-color-scheme: dark)").matches) theme = "dark";
        } catch {}
        function applyTheme() {
          document.body.dataset.theme = theme;
          const btn = $("hud-theme-btn");
          if (btn) {
            const dark = theme === "dark";
            btn.classList.toggle("active", dark);
            btn.setAttribute("aria-pressed", String(dark));
            btn.textContent = dark ? "Light" : "Dark";
          }
        }
        applyTheme();
        $("hud-theme-btn").addEventListener("click", () => {
          theme = theme === "dark" ? "light" : "dark";
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
          const root = $("hud-machines");
          const list = Array.isArray(machines) ? machines : [];
          $("hud-computers-foot").textContent =
            list.length === 1 ? "1 computer connected" : list.length + " computers connected";
          if (list.length < 2) {
            root.replaceChildren();
            return;
          }
          const items = [{id: "all", name: "All", isLocal: true}, ...list];
          root.replaceChildren(...items.map(item => {
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
          if (text.includes("5-hour") || text.includes("five-hour") || text.includes("5h")) {
            win = key === "antigravity" ? "5h" : "5 hour";
          } else if (text.includes("month")) win = "month";
          else if (text.includes("week")) win = "week";
          else if (text.includes("other model")) win = "Other";
          else if (text.includes("model")) win = "Models";
          else if (text.includes("fund") || text.includes("balance")) win = "Balance";
          const left = [scope, win].filter(Boolean).join(" ");
          return left.charAt(0).toUpperCase() + left.slice(1);
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
            tipRow(burn == null ? null : esc(usd(burn) + " per day")) +
            '<span class="sep"></span>' +
            tipRow("Reference usage value per day, averaged over the last 7 complete days. Today is excluded; derived providers are priced at published rates.");

          const today = nav.todayUsd ?? (totals.trackedValue != null ? totals.trackedValue : null);
          const coverage = nav.todayComplete === false ? "partial — some usage is unpriced" : "complete";
          $("hud-stat-today").dataset.tip =
            '<b class="tip-title">API spend today</b>' +
            tipRow(today == null ? null : esc(usd2(today) + " so far")) +
            '<span class="sep"></span>' +
            tipRow("Reference usage value since local midnight. Not a bill; provider-reported charges appear only when exact.") +
            tipRow(esc("Coverage: " + coverage + "."));

          const cache = totals.cacheReusePct;
          $("hud-stat-cache").dataset.tip =
            '<b class="tip-title">Cache hit rate</b>' +
            tipRow(cache == null ? null : esc(Math.round(Number(cache)) + "% of prompt tokens were cache reads")) +
            '<span class="sep"></span>' +
            tipRow("Cache-read input ÷ all prompt tokens (reads + fresh input + cache writes), rolling 24h. Unclassified usage is excluded, never estimated.");

          const todayTokens = nav.todayTokens != null ? nav.todayTokens : totals.tokens;
          const avg = nav.tokensAvgPerDay;
          $("hud-stat-tokens").dataset.tip =
            '<b class="tip-title">Tokens used today</b>' +
            tipRow(esc(tok(todayTokens) + " tokens since local midnight — a running daily total.")) +
            (avg == null ? "" : '<span class="sep"></span>' +
              tipRow(esc("7-day average: ≈ " + tok(avg) + " tokens/day"), "accent"));
        }

        function renderSummary(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          const status = data.status || "live";
          const pill = $("hud-status-pill");
          pill.dataset.state = status === "success" ? "live" : status;
          $("hud-status-text").textContent = status === "stale" ? "STALE" : status === "error" ? "ERR" : "LIVE";
          const failing = data.failingSource ? " · " + data.failingSource : "";
          pill.dataset.tip =
            '<b class="tip-title">' + (status === "stale" ? "Stale" : status === "error" ? "Offline" : "Live ingest") + "</b>" +
            tipRow(esc(
              status === "error"
                ? "The local BURNRATE service is unreachable."
                : status === "stale"
                  ? "Ingest has not refreshed on schedule" + failing + "."
                  : "Local harness ingest is refreshing on schedule."
            ));
          $("hud-date").textContent = new Date().toLocaleDateString("en-US", {
            weekday: "long", month: "long", day: "numeric"
          });

          setStat("hud-burn-rate", nav.burnRatePerDay != null ? usd(nav.burnRatePerDay) : "—");
          setStat("hud-today", usd(nav.todayUsd ?? totals.trackedValue));
          setStat("hud-cache", totals.cacheReusePct == null || totals.cacheReusePct === ""
            ? "—"
            : Math.round(Number(totals.cacheReusePct)) + "%");
          setStat("hud-tokens", tok(nav.todayTokens != null ? nav.todayTokens : totals.tokens));
          statTips(nav, totals);

          renderMachines(data.machines || []);

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
          const marks = {
            codex: "M8.086.457a6.105 6.105 0 013.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 00.107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198.278.679.418 1.388.421 2.126a5.655 5.655 0 01-.18 1.631.167.167 0 00.04.155 5.982 5.982 0 011.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 01-2.934 1.851.162.162 0 00-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 00-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 01-2.595-.622 6.058 6.058 0 01-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 01-.495-1.283 6.11 6.11 0 01-.017-3.064.166.166 0 00.008-.074.115.115 0 00-.037-.064 5.958 5.958 0 01-1.38-2.202 5.196 5.196 0 01-.333-1.589 6.915 6.915 0 01.188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 00.087-.087A6.016 6.016 0 015.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 00-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 001.46.864l1.94-3.272a.849.849 0 00.007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 000 1.695h4.848a.849.849 0 000-1.696h-4.848z",
            "claude-code": "M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z",
            cursor: "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z",
            grok: "M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305h-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815",
            opencode: "M12.105 2L9.927 4.953H.653L2.83 2h9.276zM23.254 19.048L21.078 22h-9.242l2.174-2.952h9.244zM24 2L9.264 22H0L14.736 2H24z",
            antigravity: "M12 4.5 20 18.5H4z",
            muse: "M3 19V5l9 9 9-9v14h-3V11.5l-6 6-6-6V19H3z",
            openrouter: "M7 10a2 2 0 100 4 2 2 0 000-4zm10-4.5a2 2 0 100 4 2 2 0 000-4zM17 14.5a2 2 0 100 4 2 2 0 000-4z"
          };
          const iconFor = key => {
            const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
            svg.setAttribute("viewBox", "0 0 24 24");
            svg.setAttribute("aria-hidden", "true");
            const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
            path.setAttribute("fill", "currentColor");
            path.setAttribute("d", marks[key] || marks.openrouter);
            svg.append(path);
            return svg;
          };

          const groups = [];
          for (const provider of (data.capacity || [])) {
            const key = provider.providerKey || "";
            if (key === "cursor") continue;
            const name = shortNames[key] || provider.providerName || key || "Plan";
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
            if (rows.length) groups.push({ key, name, plan: provider.plan || "", rows, providerName: provider.providerName || name, polledAt });
          }
          const shownPeak = group => {
            const finite = group.rows.map(item => item.pct).filter(value => value != null);
            if (!finite.length) return group.rows.some(item => item.payg) ? -2 : -1;
            return Math.max(...finite);
          };
          groups.sort((a, b) => shownPeak(b) - shownPeak(a));
          const badge = $("hud-quota-badge");
          badge.textContent = groups.length ? String(groups.length) : "—";
          badge.dataset.tip =
            '<b class="tip-title">Plans</b>' +
            tipRow(esc(groups.length ? groups.length + (groups.length === 1 ? " subscription tracked" : " subscriptions tracked") + ", sorted by what runs out first." : "No subscription usage yet."));
          const liveAgents = (data.activity || []).filter(item => String(item.state || "").toUpperCase() === "LIVE");
          const liveSection = $("hud-live");
          const agentsRoot = $("hud-agents");
          if (liveSection && agentsRoot) {
            liveSection.hidden = !liveAgents.length;
            agentsRoot.replaceChildren(...liveAgents.slice(0, 3).map(agent => {
              const row = document.createElement("div");
              row.className = "hud-agent";
              const mainWrap = document.createElement("div");
              mainWrap.className = "hud-agent-main";
              const pulse = document.createElement("i");
              pulse.className = "hud-pulse";
              const textWrap = document.createElement("div");
              textWrap.className = "hud-agent-text";
              const strong = document.createElement("strong");
              strong.textContent = agent.name || "Active session";
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
              row.dataset.tip = '<b class="tip-title">' + esc(agent.name || "Active agent") + '</b>' + tipRow(esc((agent.modelKey || "model") + " · click to inspect in dashboard"));
              row.addEventListener("click", () => openToolInDashboard(inferredTool));
              return row;
            }));
          }

          const topGroup = groups.find(g => shownPeak(g) >= 0) || groups[0] || null;
          const miniBar = $("hud-mini-bar");
          if (miniBar) {
            if (topGroup) {
              const topRow = topGroup.rows.find(r => r.pct != null) || topGroup.rows[0];
              const until = topRow ? resetIn(topRow.resetsAt) : "";
              $("hud-mini-lead").textContent = topGroup.name + (topRow ? " · " + windowName(topGroup.key, topRow.label) : "") + (until ? " (" + until + ")" : "");
              $("hud-mini-pct").textContent = topRow && topRow.pct != null ? pctText(topRow.pct) : "—";
              miniBar.dataset.providerKey = topGroup.key;
            } else {
              $("hud-mini-lead").textContent = "All subscription limits healthy";
              $("hud-mini-pct").textContent = "—";
              miniBar.dataset.providerKey = "";
            }
          }

          const quotasRoot = $("hud-quotas");
          if (!groups.length) {
            quotasRoot.innerHTML = '<p class="hud-empty">No subscription usage yet.</p>';
          } else {
            quotasRoot.replaceChildren(...groups.map(group => {
              const card = document.createElement("article");
              card.className = "plan" + (group.rows.length > 2 ? " is-stack" : "");
              card.setAttribute("aria-label", group.name);
              card.dataset.providerKey = group.key;
              card.addEventListener("click", () => openToolInDashboard(group.key));
              const mark = document.createElement("span");
              mark.className = "mark";
              mark.append(iconFor(group.key));
              const name = document.createElement("b");
              name.textContent = group.name;
              const main = document.createElement("div");
              main.className = "plan-main";
              const em = document.createElement("em");
              const planLabel = group.key === "opencode" && group.plan === "Max" ? "Coding Plan Max" : group.plan;
              const ageLabel = relTime(group.polledAt);
              em.textContent = [planLabel, ageLabel].filter(Boolean).join(" · ");
              const windows = document.createElement("div");
              windows.className = "windows";
              const count = group.rows.length;
              const cols = count <= 1 ? 1 : count === 3 ? 3 : 2;
              windows.style.gridTemplateColumns = "repeat(" + cols + ", minmax(0, 1fr))";
              for (const q of group.rows) {
                const cell = document.createElement("div");
                cell.className = "win";
                const when = resetWhen(q.resetsAt);
                const until = resetIn(q.resetsAt);
                const elapsed = windowElapsedPct(q);
                const f = q.forecast || {};
                const fstate = f.state || "";
                const label = document.createElement("span");
                label.textContent = windowName(group.key, q.label);
                const value = document.createElement("b");
                value.textContent = q.pct == null ? (q.payg ? "Payg" : "—") : pctText(q.pct);

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
                if (q.pct == null && !q.payg && q.source) {
                  resetRows.push(tipRow(esc(q.source)));
                }
                cell.dataset.tip =
                  '<b class="tip-title">' + esc(q.title) + "</b>" +
                  usageRows.join("") +
                  (resetRows.length ? '<span class="sep"></span>' + resetRows.join("") : "");

                if (q.pct != null && !q.payg) {
                  const p = Math.max(0, Math.min(100, q.pct));
                  cell.style.setProperty("--fill", p.toFixed(1) + "%");
                  if (fstate === "dry" || p >= 90) cell.classList.add("crit");
                  else if (fstate === "tight" || p >= 75) cell.classList.add("warn");
                }
                if (elapsed != null && !q.payg) {
                  const tick = document.createElement("i");
                  tick.className = "win-pace-tick";
                  tick.style.left = elapsed.toFixed(1) + "%";
                  cell.append(tick);
                }

                cell.append(label);
                if (until) {
                  const eta = document.createElement("i");
                  eta.textContent = until;
                  cell.append(eta);
                }
                cell.append(value);
                windows.append(cell);
              }
              main.append(name, em);
              main.dataset.tip =
                '<b class="tip-title">' + esc(group.providerName) + "</b>" +
                tipRow(esc((group.plan ? group.plan + " · " : "") + (ageLabel ? "polled " + ageLabel + " · " : "") + "click to open tool detail."));
              card.append(mark, main, windows);
              return card;
            }));
          }
          $("hud-updated-foot").textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
          syncHudWindowSize();
        }

        async function refresh() {
          const suffix = selectedMachine && selectedMachine !== "all" ? "&machine=" + encodeURIComponent(selectedMachine) : "";
          try {
            const res = await fetch("/api/spend/summary?window=1d&tool=all" + suffix, {cache: "no-store"});
            if (!res.ok) throw new Error(String(res.status));
            const data = await res.json();
            renderSummary(data);
          } catch {
            $("hud-status-pill").dataset.state = "error";
            $("hud-status-text").textContent = "OFFLINE";
          }
        }

        refresh();
        setInterval(refresh, 5000);
        window.addEventListener("focus", refresh);
        document.addEventListener("visibilitychange", () => {
          if (!document.hidden) refresh();
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
