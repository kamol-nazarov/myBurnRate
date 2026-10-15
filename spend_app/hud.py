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
        --paper: rgba(246, 245, 241, 0.74);
        --ink: #1c1b19;
        --muted: #6f6a62;
        --faint: #8a847b;
        --peach: rgba(246, 199, 180, 0.86);
        --yellow: rgba(246, 227, 154, 0.86);
        --pink: rgba(246, 211, 222, 0.86);
        --blue: rgba(201, 223, 246, 0.86);
        --docs: rgba(238, 242, 246, 0.8);
        --card: rgba(247, 248, 250, 0.88);
        --plate: rgba(255, 255, 255, 0.94);
        --serif: "Iowan Old Style", "Palatino Linotype", Palatino, Georgia, serif;
        --sans: "Segoe UI", sans-serif;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      html, body {
        height: 100%;
        background: transparent;
        color: var(--ink);
        font-family: var(--sans);
        font-size: 13px;
        line-height: 1.3;
        overflow: auto;
        user-select: none;
        cursor: grab;
        scrollbar-width: thin;
        scrollbar-color: rgba(28, 27, 25, 0.28) transparent;
      }
      html::-webkit-scrollbar,
      #hud-quotas::-webkit-scrollbar { width: 10px; }
      html::-webkit-scrollbar-thumb,
      #hud-quotas::-webkit-scrollbar-thumb {
        background: rgba(28, 27, 25, 0.18);
        border: 3px solid var(--paper);
        border-radius: 99px;
      }
      .page {
        min-height: 100%;
        padding: 12px 12px 10px;
        background: var(--paper);
      }
      .top {
        display: flex;
        align-items: flex-start;
        justify-content: space-between;
        gap: 12px;
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
        letter-spacing: 0.12em;
        text-transform: uppercase;
        color: var(--faint);
      }
      .hud-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 4px;
        font-size: 10px;
        font-weight: 650;
        letter-spacing: 0.08em;
        color: var(--faint);
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
        font-size: 28px;
        letter-spacing: -0.03em;
        line-height: 1;
      }
      .hud-controls { display: flex; gap: 2px; flex: none; }
      .hud-btn {
        border: 0;
        background: transparent;
        color: var(--faint);
        font: inherit;
        font-size: 11px;
        font-weight: 650;
        padding: 4px 6px;
        border-radius: 8px;
        cursor: pointer;
      }
      .hud-btn:hover { background: rgba(28, 27, 25, 0.06); color: var(--ink); }
      .hud-btn.active { color: var(--ink); }
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
        padding: 8px 8px 9px;
        border-radius: 14px;
        display: flex;
        flex-direction: column;
      }
      .stat header span {
        font-size: 9px;
        font-weight: 650;
        letter-spacing: 0.1em;
        text-transform: uppercase;
      }
      .stat strong {
        margin-top: 4px;
        font-family: var(--serif);
        font-size: 20px;
        font-weight: 460;
        letter-spacing: -0.03em;
        line-height: 1;
      }
      .stat small { display: none; }
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
        display: flex;
        align-items: center;
        justify-content: flex-start;
        gap: 6px;
        min-width: 0;
        padding: 5px 10px;
        border-radius: 10px;
        background: var(--plate);
      }
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
    </style>
  </head>
  <body data-tauri-drag-region="deep">
    <main class="page" id="hud-shell">
      <header class="top">
        <div class="titles">
          <p class="date-row">
            <span class="date" id="hud-date">Today</span>
            <span class="hud-status-pill" id="hud-status-pill" data-state="live">
              <i></i><span id="hud-status-text">LIVE</span>
            </span>
          </p>
          <h1>Usage</h1>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn active" id="hud-pin-btn" title="Keep Sidecar HUD Always on Top" aria-pressed="true">Pin</button>
          <button type="button" class="hud-btn" id="hud-expand-btn" title="Open Full Dashboard">Open</button>
          <button type="button" class="hud-btn" id="hud-hide-btn" title="Hide Sidecar HUD">Hide</button>
        </div>
      </header>

      <div id="hud-machines" role="radiogroup" aria-label="Computer filter"></div>

      <section class="stats" aria-label="Live BurnRate KPIs">
        <article class="stat peach">
          <header><span>Burn</span></header>
          <strong id="hud-burn-rate">—</strong>
          <small>Per day</small>
        </article>
        <article class="stat yellow">
          <header><span>Today</span></header>
          <strong id="hud-today">—</strong>
          <small>Tracked so far</small>
        </article>
        <article class="stat pink">
          <header><span>Cache</span></header>
          <strong id="hud-cache">—</strong>
        </article>
        <article class="stat blue">
          <header><span>Tokens</span></header>
          <strong id="hud-tokens">—</strong>
        </article>
      </section>

      <section class="live-list" id="hud-live" hidden aria-label="Running agents across computers">
        <div id="hud-agents"></div>
      </section>

      <section class="plans" aria-label="Subscription usage">
        <div class="plans-head">
          <span>Plans</span>
          <span id="hud-quota-badge">—</span>
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

    <script>
      (() => {
        document.addEventListener("mousedown", event => {
          if (event.detail === 2) event.stopImmediatePropagation();
        }, true);

        const $ = id => document.getElementById(id);
        let selectedMachine = new URLSearchParams(location.search).get("machine") || "all";
        let pinned = true;

        const usd = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return "$" + Math.round(n).toLocaleString("en-US");
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

        async function tauriInvoke(cmd, args = {}) {
          const internals = window.__TAURI_INTERNALS__;
          if (internals && typeof internals.invoke === "function") {
            return internals.invoke(cmd, args);
          }
          return null;
        }

        $("hud-pin-btn").addEventListener("click", async () => {
          const res = await tauriInvoke("toggle_hud_pin");
          pinned = typeof res === "boolean" ? res : !pinned;
          $("hud-pin-btn").classList.toggle("active", pinned);
          $("hud-pin-btn").setAttribute("aria-pressed", String(pinned));
        });

        $("hud-expand-btn").addEventListener("click", async () => {
          const handled = await tauriInvoke("show_dashboard");
          if (handled === null) window.open("/", "_blank");
        });

        $("hud-hide-btn").addEventListener("click", async () => {
          const handled = await tauriInvoke("hide_hud");
          if (handled === null) window.close();
        });

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

        function renderSummary(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          const status = data.status || "live";
          $("hud-status-pill").dataset.state = status === "success" ? "live" : status;
          $("hud-status-text").textContent = status === "stale" ? "STALE" : status === "error" ? "ERR" : "LIVE";
          $("hud-date").textContent = new Date().toLocaleDateString("en-US", {
            weekday: "long", month: "long", day: "numeric"
          });

          $("hud-burn-rate").textContent = nav.burnRatePerDay != null ? usd(nav.burnRatePerDay) : "—";
          $("hud-today").textContent = usd(nav.todayUsd ?? totals.trackedValue);
          $("hud-cache").textContent = totals.cacheReusePct == null || totals.cacheReusePct === ""
            ? "—"
            : Math.round(Number(totals.cacheReusePct)) + "%";
          $("hud-tokens").textContent = tok(totals.tokens);

          renderMachines(data.machines || []);

          const shortNames = {
            codex: "Codex",
            cursor: "Cursor",
            "claude-code": "Claude",
            antigravity: "Antigravity",
            grok: "Grok",
            opencode: "Z.AI",
            openrouter: "OpenRouter"
          };
          const marks = {
            codex: "M8.086.457a6.105 6.105 0 013.046-.415c1.333.153 2.521.72 3.564 1.7a.117.117 0 00.107.029c1.408-.346 2.762-.224 4.061.366l.063.03.154.076c1.357.703 2.33 1.77 2.918 3.198.278.679.418 1.388.421 2.126a5.655 5.655 0 01-.18 1.631.167.167 0 00.04.155 5.982 5.982 0 011.578 2.891c.385 1.901-.01 3.615-1.183 5.14l-.182.22a6.063 6.063 0 01-2.934 1.851.162.162 0 00-.108.102c-.255.736-.511 1.364-.987 1.992-1.199 1.582-2.962 2.462-4.948 2.451-1.583-.008-2.986-.587-4.21-1.736a.145.145 0 00-.14-.032c-.518.167-1.04.191-1.604.185a5.924 5.924 0 01-2.595-.622 6.058 6.058 0 01-2.146-1.781c-.203-.269-.404-.522-.551-.821a7.74 7.74 0 01-.495-1.283 6.11 6.11 0 01-.017-3.064.166.166 0 00.008-.074.115.115 0 00-.037-.064 5.958 5.958 0 01-1.38-2.202 5.196 5.196 0 01-.333-1.589 6.915 6.915 0 01.188-2.132c.45-1.484 1.309-2.648 2.577-3.493.282-.188.55-.334.802-.438.286-.12.573-.22.861-.304a.129.129 0 00.087-.087A6.016 6.016 0 015.635 2.31C6.315 1.464 7.132.846 8.086.457zm-.804 7.85a.848.848 0 00-1.473.842l1.694 2.965-1.688 2.848a.849.849 0 001.46.864l1.94-3.272a.849.849 0 00.007-.854l-1.94-3.393zm5.446 6.24a.849.849 0 000 1.695h4.848a.849.849 0 000-1.696h-4.848z",
            "claude-code": "M4.709 15.955l4.72-2.647.08-.23-.08-.128H9.2l-.79-.048-2.698-.073-2.339-.097-2.266-.122-.571-.121L0 11.784l.055-.352.48-.321.686.06 1.52.103 2.278.158 1.652.097 2.449.255h.389l.055-.157-.134-.098-.103-.097-2.358-1.596-2.552-1.688-1.336-.972-.724-.491-.364-.462-.158-1.008.656-.722.881.06.225.061.893.686 1.908 1.476 2.491 1.833.365.304.145-.103.019-.073-.164-.274-1.355-2.446-1.446-2.49-.644-1.032-.17-.619a2.97 2.97 0 01-.104-.729L6.283.134 6.696 0l.996.134.42.364.62 1.414 1.002 2.229 1.555 3.03.456.898.243.832.091.255h.158V9.01l.128-1.706.237-2.095.23-2.695.08-.76.376-.91.747-.492.584.28.48.685-.067.444-.286 1.851-.559 2.903-.364 1.942h.212l.243-.242.985-1.306 1.652-2.064.73-.82.85-.904.547-.431h1.033l.76 1.129-.34 1.166-1.064 1.347-.881 1.142-1.264 1.7-.79 1.36.073.11.188-.02 2.856-.606 1.543-.28 1.841-.315.833.388.091.395-.328.807-1.969.486-2.309.462-3.439.813-.042.03.049.061 1.549.146.662.036h1.622l3.02.225.79.522.474.638-.079.485-1.215.62-1.64-.389-3.829-.91-1.312-.329h-.182v.11l1.093 1.068 2.006 1.81 2.509 2.33.127.578-.322.455-.34-.049-2.205-1.657-.851-.747-1.926-1.62h-.128v.17l.444.649 2.345 3.521.122 1.08-.17.353-.608.213-.668-.122-1.374-1.925-1.415-2.167-1.143-1.943-.14.08-.674 7.254-.316.37-.729.28-.607-.461-.322-.747.322-1.476.389-1.924.315-1.53.286-1.9.17-.632-.012-.042-.14.018-1.434 1.967-2.18 2.945-1.726 1.845-.414.164-.717-.37.067-.662.401-.589 2.388-3.036 1.44-1.882.93-1.086-.006-.158h-.055L4.132 18.56l-1.13.146-.487-.456.061-.746.231-.243 1.908-1.312-.006.006z",
            cursor: "M22.106 5.68L12.5.135a.998.998 0 00-.998 0L1.893 5.68a.84.84 0 00-.419.726v11.186c0 .3.16.577.42.727l9.607 5.547a.999.999 0 00.998 0l9.608-5.547a.84.84 0 00.42-.727V6.407a.84.84 0 00-.42-.726zm-.603 1.176L12.228 22.92c-.063.108-.228.064-.228-.061V12.34a.59.59 0 00-.295-.51l-9.11-5.26c-.107-.062-.063-.228.062-.228h18.55c.264 0 .428.286.296.514z",
            grok: "M9.27 15.29l7.978-5.897c.391-.29.95-.177 1.137.272.98 2.369.542 5.215-1.41 7.169-1.951 1.954-4.667 2.382-7.149 1.406l-2.711 1.257c3.889 2.661 8.611 2.003 11.562-.953 2.341-2.344 3.066-5.539 2.388-8.42l.006.007c-.983-4.232.242-5.924 2.75-9.383.06-.082.12-.164.179-.248l-3.301 3.305v-.01L9.267 15.292M7.623 16.723c-2.792-2.67-2.31-6.801.071-9.184 1.761-1.763 4.647-2.483 7.166-1.425l2.705-1.25a7.808 7.808 0 00-1.829-1A8.975 8.975 0 005.984 5.83c-2.533 2.536-3.33 6.436-1.962 9.764 1.022 2.487-.653 4.246-2.34 6.022-.599.63-1.199 1.259-1.682 1.925l7.62-6.815",
            opencode: "M12.105 2L9.927 4.953H.653L2.83 2h9.276zM23.254 19.048L21.078 22h-9.242l2.174-2.952h9.244zM24 2L9.264 22H0L14.736 2H24z",
            antigravity: "M12 4.5 20 18.5H4z",
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
            const name = shortNames[key] || provider.providerName || key || "Plan";
            const sourceRows = Array.isArray(provider.rows) && provider.rows.length ? provider.rows : [provider];
            const rows = sourceRows.map(row => {
              const value = row.pct == null || row.pct === "" ? NaN : Number(row.pct);
              return {
                label: row.label || row.limitKey || provider.plan || "",
                title: [provider.providerName || name, row.label || provider.plan || ""].filter(Boolean).join(" · "),
                pct: Number.isFinite(value) ? value : null,
                payg: Boolean(row.isPayg || provider.isPayg),
                resetsAt: row.resetsAt || ""
              };
            });
            if (rows.length) groups.push({ key, name, plan: provider.plan || "", rows });
          }
          const shownPeak = group => {
            const finite = group.rows.map(item => item.pct).filter(value => value != null);
            if (!finite.length) return group.rows.some(item => item.payg) ? -2 : -1;
            return Math.max(...finite);
          };
          groups.sort((a, b) => shownPeak(b) - shownPeak(a));
          $("hud-quota-badge").textContent = groups.length ? String(groups.length) : "—";
          const quotasRoot = $("hud-quotas");
          if (!groups.length) {
            quotasRoot.innerHTML = '<p class="hud-empty">No subscription usage yet.</p>';
          } else {
            quotasRoot.replaceChildren(...groups.map(group => {
              const card = document.createElement("article");
              card.className = "plan" + (group.rows.length > 2 ? " is-stack" : "");
              card.setAttribute("aria-label", group.name);
              const mark = document.createElement("span");
              mark.className = "mark";
              mark.append(iconFor(group.key));
              const name = document.createElement("b");
              name.textContent = group.name;
              const main = document.createElement("div");
              main.className = "plan-main";
              const em = document.createElement("em");
              em.textContent = group.key === "opencode" && group.plan === "Max" ? "Coding Plan Max" : group.plan;
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
                cell.title = q.title + (when ? " · resets " + when : "");
                const label = document.createElement("span");
                label.textContent = windowName(group.key, q.label);
                const value = document.createElement("b");
                value.textContent = q.pct == null ? (q.payg ? "Payg" : "—") : pctText(q.pct);
                cell.append(label);
                if (until) {
                  const eta = document.createElement("i");
                  eta.textContent = until;
                  eta.title = "Resets " + when;
                  cell.append(eta);
                }
                cell.append(value);
                windows.append(cell);
              }
              main.append(name, em);
              card.append(mark, main, windows);
              return card;
            }));
          }
          $("hud-updated-foot").textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit"});
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
        setInterval(refresh, 10000);
        if (typeof EventSource === "function") {
          let debounce = null;
          try {
            const es = new EventSource("/api/spend/stream");
            es.addEventListener("update", () => {
              if (debounce) clearTimeout(debounce);
              debounce = setTimeout(() => { debounce = null; refresh(); }, 120);
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
