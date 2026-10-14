"""Always-on-Top Sidecar HUD for BURNRATE (Tauri native window & browser sidecar)."""

from __future__ import annotations

HUD_HTML = """<!doctype html>
<html lang="en">
  <head>
    <meta charset="utf-8">
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <meta name="theme-color" content="#0b0d10">
    <title>BURNRATE · Sidecar HUD</title>
    <link rel="icon" type="image/svg+xml" href="/favicon.svg">
    <style>
      :root {
        color-scheme: dark;
        --bg: #0b0d10;
        --surface: #111419;
        --raised: #171b22;
        --border: rgba(255, 255, 255, 0.08);
        --border-strong: rgba(255, 255, 255, 0.15);
        --text: #e8ecf2;
        --secondary: #a4acb9;
        --muted: #6e7685;
        --accent: #d9a441;
        --cyan: #5cd6e8;
        --green: #63c689;
        --danger: #ef6461;
        --font-sans: Inter, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
        --font-mono: "JetBrains Mono", "Cascadia Mono", Consolas, monospace;
      }
      * { box-sizing: border-box; margin: 0; padding: 0; }
      html, body {
        height: 100%;
        background: var(--bg);
        color: var(--text);
        font-family: var(--font-sans);
        font-size: 13px;
        line-height: 1.4;
        overflow-x: hidden;
        user-select: none;
      }
      .hud-shell {
        display: flex;
        flex-direction: column;
        min-height: 100vh;
        padding: 10px 12px 12px;
        gap: 10px;
      }
      .hud-titlebar {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding-bottom: 8px;
        border-bottom: 1px solid var(--border);
        cursor: grab;
      }
      .hud-brand {
        display: flex;
        align-items: center;
        gap: 8px;
        min-width: 0;
      }
      .hud-brand img {
        width: 22px;
        height: 22px;
        border-radius: 6px;
      }
      .hud-wordmark {
        font-size: 12px;
        font-weight: 700;
        letter-spacing: 0.08em;
      }
      .hud-wordmark strong { color: var(--accent); }
      .hud-status-pill {
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 2px 7px;
        border-radius: 999px;
        background: var(--surface);
        border: 1px solid var(--border);
        font-size: 10px;
        font-family: var(--font-mono);
        color: var(--secondary);
      }
      .hud-status-pill i {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--green);
        box-shadow: 0 0 8px var(--green);
      }
      .hud-status-pill[data-state="stale"] i {
        background: var(--accent);
        box-shadow: none;
      }
      .hud-status-pill[data-state="error"] i {
        background: var(--danger);
        box-shadow: none;
      }
      .hud-controls {
        display: flex;
        align-items: center;
        gap: 4px;
      }
      .hud-btn {
        appearance: none;
        background: var(--surface);
        border: 1px solid var(--border);
        color: var(--secondary);
        border-radius: 6px;
        padding: 4px 7px;
        font-size: 11px;
        font-family: var(--font-sans);
        cursor: pointer;
        transition: border-color 0.15s, color 0.15s, background 0.15s;
      }
      .hud-btn:hover {
        color: var(--text);
        border-color: var(--border-strong);
        background: var(--raised);
      }
      .hud-btn.active {
        color: var(--accent);
        border-color: rgba(217, 164, 65, 0.45);
        background: rgba(217, 164, 65, 0.12);
      }
      .hud-kpis {
        display: grid;
        grid-template-columns: repeat(2, minmax(0, 1fr));
        gap: 8px;
      }
      .hud-kpi {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 8px 10px;
      }
      .hud-kpi span {
        display: block;
        font-size: 10px;
        text-transform: uppercase;
        letter-spacing: 0.06em;
        color: var(--muted);
      }
      .hud-kpi strong {
        display: block;
        margin-top: 2px;
        font-family: var(--font-mono);
        font-size: 17px;
        font-weight: 600;
        font-variant-numeric: tabular-nums;
        color: var(--text);
      }
      .hud-kpi small {
        display: block;
        margin-top: 2px;
        font-size: 11px;
        color: var(--secondary);
        font-family: var(--font-mono);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .hud-machines {
        display: flex;
        align-items: center;
        gap: 5px;
        overflow-x: auto;
        padding-bottom: 2px;
        scrollbar-width: none;
      }
      .hud-machines::-webkit-scrollbar { display: none; }
      .hud-machine-chip {
        appearance: none;
        display: inline-flex;
        align-items: center;
        gap: 5px;
        padding: 4px 9px;
        border-radius: 999px;
        background: var(--surface);
        border: 1px solid var(--border);
        color: var(--secondary);
        font-size: 11px;
        white-space: nowrap;
        cursor: pointer;
      }
      .hud-machine-chip.active {
        color: var(--text);
        border-color: var(--accent);
        background: rgba(217, 164, 65, 0.14);
      }
      .hud-machine-chip i {
        width: 6px;
        height: 6px;
        border-radius: 50%;
        background: var(--muted);
      }
      .hud-machine-chip i.live {
        background: var(--green);
        box-shadow: 0 0 6px var(--green);
      }
      .hud-section {
        background: var(--surface);
        border: 1px solid var(--border);
        border-radius: 10px;
        padding: 9px 10px;
        display: flex;
        flex-direction: column;
        gap: 7px;
      }
      .hud-section-head {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
      }
      .hud-section-head h2 {
        font-size: 11px;
        text-transform: uppercase;
        letter-spacing: 0.07em;
        color: var(--secondary);
        font-weight: 600;
      }
      .hud-badge {
        font-family: var(--font-mono);
        font-size: 10px;
        padding: 1px 6px;
        border-radius: 999px;
        background: var(--raised);
        color: var(--secondary);
        border: 1px solid var(--border);
      }
      .hud-badge.live {
        color: var(--green);
        border-color: rgba(99, 198, 137, 0.35);
        background: rgba(99, 198, 137, 0.1);
      }
      .hud-list {
        display: flex;
        flex-direction: column;
        gap: 6px;
      }
      .hud-agent {
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding: 6px 8px;
        border-radius: 8px;
        background: var(--raised);
        border: 1px solid var(--border);
      }
      .hud-agent-main {
        display: flex;
        align-items: center;
        gap: 7px;
        min-width: 0;
      }
      .hud-pulse {
        width: 7px;
        height: 7px;
        border-radius: 50%;
        background: var(--green);
        box-shadow: 0 0 8px var(--green);
        flex-shrink: 0;
        animation: hudPulse 1.8s ease-in-out infinite;
      }
      @keyframes hudPulse {
        0%, 100% { opacity: 1; transform: scale(1); }
        50% { opacity: 0.45; transform: scale(0.86); }
      }
      .hud-agent-text {
        min-width: 0;
      }
      .hud-agent-text strong {
        display: block;
        font-size: 12px;
        color: var(--text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .hud-agent-text small {
        display: block;
        font-size: 10px;
        font-family: var(--font-mono);
        color: var(--secondary);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .hud-machine-tag {
        font-family: var(--font-mono);
        font-size: 10px;
        padding: 2px 6px;
        border-radius: 5px;
        background: rgba(92, 214, 232, 0.1);
        color: var(--cyan);
        border: 1px solid rgba(92, 214, 232, 0.25);
        white-space: nowrap;
        flex-shrink: 0;
      }
      .hud-quota {
        display: flex;
        flex-direction: column;
        gap: 4px;
      }
      .hud-quota-top {
        display: flex;
        align-items: baseline;
        justify-content: space-between;
        gap: 6px;
        font-size: 11px;
      }
      .hud-quota-top strong {
        font-weight: 600;
        color: var(--text);
        white-space: nowrap;
        overflow: hidden;
        text-overflow: ellipsis;
      }
      .hud-quota-top span {
        font-family: var(--font-mono);
        font-size: 11px;
        color: var(--secondary);
        flex-shrink: 0;
      }
      .hud-track {
        height: 5px;
        border-radius: 999px;
        background: var(--raised);
        overflow: hidden;
      }
      .hud-track i {
        display: block;
        height: 100%;
        border-radius: 999px;
        background: var(--green);
        transition: width 0.25s ease;
      }
      .hud-empty {
        font-size: 11px;
        color: var(--muted);
        padding: 4px 0;
      }
      .hud-footer {
        margin-top: auto;
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 8px;
        padding-top: 6px;
        border-top: 1px solid var(--border);
        font-size: 10px;
        font-family: var(--font-mono);
        color: var(--muted);
      }
    </style>
  </head>
  <body>
    <main class="hud-shell" id="hud-shell">
      <header class="hud-titlebar" data-tauri-drag-region>
        <div class="hud-brand" data-tauri-drag-region>
          <img src="/favicon.svg" alt="" width="22" height="22">
          <span class="hud-wordmark" data-tauri-drag-region>BURN<strong>RATE</strong></span>
          <span class="hud-status-pill" id="hud-status-pill" data-state="live">
            <i></i><span id="hud-status-text">LIVE</span>
          </span>
        </div>
        <div class="hud-controls">
          <button type="button" class="hud-btn active" id="hud-pin-btn" title="Keep Sidecar HUD Always on Top" aria-pressed="true">Pin</button>
          <button type="button" class="hud-btn" id="hud-expand-btn" title="Open Full Dashboard">Dashboard ↗</button>
          <button type="button" class="hud-btn" id="hud-hide-btn" title="Hide Sidecar HUD">×</button>
        </div>
      </header>

      <div class="hud-machines" id="hud-machines" role="radiogroup" aria-label="Computer filter"></div>

      <section class="hud-kpis" aria-label="Live BurnRate KPIs">
        <article class="hud-kpi">
          <span>Burn rate</span>
          <strong id="hud-burn-rate">—</strong>
          <small id="hud-today">Today —</small>
        </article>
        <article class="hud-kpi">
          <span>24h Tokens</span>
          <strong id="hud-tokens">—</strong>
          <small id="hud-cache">Cache reuse —</small>
        </article>
      </section>

      <section class="hud-section" aria-label="Running agents across computers">
        <div class="hud-section-head">
          <h2>Running now</h2>
          <span class="hud-badge" id="hud-live-badge">0 live</span>
        </div>
        <div class="hud-list" id="hud-agents">
          <p class="hud-empty">No agents running right now.</p>
        </div>
      </section>

      <section class="hud-section" aria-label="Subscription capacity">
        <div class="hud-section-head">
          <h2>What runs out first</h2>
          <span class="hud-badge" id="hud-quota-badge">—</span>
        </div>
        <div class="hud-list" id="hud-quotas">
          <p class="hud-empty">Waiting for capacity snapshot…</p>
        </div>
      </section>

      <footer class="hud-footer">
        <span id="hud-computers-foot">1 computer connected</span>
        <span id="hud-updated-foot">—</span>
      </footer>
    </main>

    <script>
      (() => {
        const $ = id => document.getElementById(id);
        let selectedMachine = new URLSearchParams(location.search).get("machine") || "all";
        let pinned = true;

        const usd = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          return n >= 1000 ? "$" + Math.round(n).toLocaleString("en-US") : "$" + n.toFixed(2);
        };
        const tok = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          if (!Number.isFinite(n)) return "—";
          if (n >= 1e9) return (n / 1e9).toFixed(2) + "B";
          if (n >= 1e6) return (n / 1e6).toFixed(2) + "M";
          if (n >= 1e3) return (n / 1e3).toFixed(1) + "K";
          return String(Math.round(n));
        };
        const pct = v => {
          const n = v == null || v === "" ? NaN : Number(v);
          return Number.isFinite(n) ? n.toFixed(0) + "%" : "—";
        };
        const quotaColor = v => v >= 85 ? "#ef6461" : v >= 65 ? "#d9a441" : "#63c689";

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
          const items = [{id: "all", name: "All (" + Math.max(1, list.length) + ")", isLocal: true}, ...list];
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
          $("hud-computers-foot").textContent =
            list.length === 1 ? "1 computer connected" : list.length + " computers connected";
        }

        function renderSummary(data) {
          const nav = data.navigation || {};
          const totals = data.totals || {};
          const status = data.status || "live";
          $("hud-status-pill").dataset.state = status === "success" ? "live" : status;
          $("hud-status-text").textContent = status === "stale" ? "STALE" : status === "error" ? "ERR" : "LIVE";

          $("hud-burn-rate").textContent = nav.burnRatePerDay != null ? usd(nav.burnRatePerDay) + "/d" : "—";
          $("hud-today").textContent = "Today " + usd(nav.todayUsd ?? totals.trackedValue);
          $("hud-tokens").textContent = tok(totals.tokens);
          $("hud-cache").textContent = "Cache reuse " + pct(totals.cacheReusePct);

          renderMachines(data.machines || []);

          const liveRows = (data.activity || []).filter(r => {
            const s = String(r?.state || "").toLowerCase();
            return s === "live" || s === "running";
          });
          const badge = $("hud-live-badge");
          badge.textContent = liveRows.length + " live";
          badge.classList.toggle("live", liveRows.length > 0);

          const agentsRoot = $("hud-agents");
          if (!liveRows.length) {
            agentsRoot.innerHTML = '<p class="hud-empty">No agents running right now.</p>';
          } else {
            agentsRoot.replaceChildren(...liveRows.map(row => {
              const card = document.createElement("div");
              card.className = "hud-agent";
              const started = Date.parse(row.startedAt || "");
              const mins = Number.isFinite(started) ? Math.max(0, Math.round((Date.now() - started) / 60000)) : null;
              const age = mins == null ? "" : mins < 1 ? "now" : mins < 60 ? mins + "m" : Math.round(mins / 60) + "h";
              const main = document.createElement("div");
              main.className = "hud-agent-main";
              const pulse = document.createElement("i");
              pulse.className = "hud-pulse";
              const text = document.createElement("div");
              text.className = "hud-agent-text";
              const title = document.createElement("strong");
              title.textContent = row.name || "Agent";
              const sub = document.createElement("small");
              sub.textContent = [row.modelKey || "model", age].filter(Boolean).join(" · ");
              text.append(title, sub);
              main.append(pulse, text);
              const tag = document.createElement("span");
              tag.className = "hud-machine-tag";
              tag.textContent = row.machineName || (row.machineId === "local" ? "Local" : row.machineId || "Local");
              card.append(main, tag);
              return card;
            }));
          }

          const quotas = [];
          for (const provider of (data.capacity || [])) {
            if (provider.isPayg) continue;
            for (const row of (provider.rows || [])) {
              if (row.pct != null && Number.isFinite(Number(row.pct))) {
                quotas.push({
                  provider: provider.providerName || provider.providerKey,
                  label: row.label || row.key,
                  pct: Number(row.pct),
                  resetsAt: row.resetsAt
                });
              }
            }
          }
          quotas.sort((a, b) => b.pct - a.pct);
          const topQuotas = quotas.slice(0, 4);
          $("hud-quota-badge").textContent = topQuotas.length ? pct(topQuotas[0].pct) + " peak" : "—";
          const quotasRoot = $("hud-quotas");
          if (!topQuotas.length) {
            quotasRoot.innerHTML = '<p class="hud-empty">No active subscription limits reported.</p>';
          } else {
            quotasRoot.replaceChildren(...topQuotas.map(q => {
              const wrap = document.createElement("div");
              wrap.className = "hud-quota";
              const top = document.createElement("div");
              top.className = "hud-quota-top";
              const name = document.createElement("strong");
              name.textContent = q.provider + " · " + q.label;
              const val = document.createElement("span");
              val.textContent = pct(q.pct);
              val.style.color = quotaColor(q.pct);
              top.append(name, val);
              const track = document.createElement("div");
              track.className = "hud-track";
              const fill = document.createElement("i");
              fill.style.width = Math.max(0, Math.min(100, q.pct)).toFixed(1) + "%";
              fill.style.background = quotaColor(q.pct);
              track.append(fill);
              wrap.append(top, track);
              return wrap;
            }));
          }

          $("hud-updated-foot").textContent = new Date().toLocaleTimeString([], {hour: "numeric", minute: "2-digit", second: "2-digit"});
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
