# Architecture

BURNRATE is one Windows process: a FastAPI app, a SQLite WAL database, an in-process scheduler, and the Sidecar HUD. The full static dashboard is optional and off by default (`BURNRATE_ENABLE_DASHBOARD=1` turns it on). It is not an inference proxy, not a hosted service, and not a Grafana stack.

Internal Python packages in this tree remain `spend_app` (application) and `spend_web` (static assets). The public command is `burnrate`.

## Runtime

```text
  Codex / Claude Code / OpenCode / ZCode / Muse Code /
  Grok Build / Antigravity / Traycer
        │  inference path unchanged
        ▼
  Local files (read-only)          Optional HTTPS (opt-in)
  JSONL, SQLite mode=ro, logs        provider-owned admin/quota hosts
        │
        ▼
  Adapters  →  persist_rows()  →  PricingEngine (pricing/*.yaml)
        │
        ▼
  SQLite WAL  (%LOCALAPPDATA%\BURNRATE, or repo-relative in a checkout)
        │
        ├─ APScheduler (FastAPI lifespan)
        │    local ingest 15s · admin ingest 15m · subscriptions 15m
        │    quota tick 15s (per-lane cadence) · activity 4s
        ▼
  FastAPI  127.0.0.1:17331
        │
        ▼
  Sidecar HUD  /hud (Tauri window or browser)
  Optional dashboard  spend_web/{index.html, spend.css, spend.js, favicon.svg}
```

`burnrate serve` launches only this process. It does not start OpenTelemetry Collector, Prometheus, Grafana, a Traycer Prometheus exporter, or any legacy token UI.

## Bind and data directory

| Item | Default |
| --- | --- |
| Listen | `127.0.0.1:17331` only |
| Installed data | `%LOCALAPPDATA%\BURNRATE` |
| Dev checkout | repo-relative only when `BURNRATE_DEV=1` (a `.git` directory is not enough) |
| Timezone | this computer's IANA zone; `SPEND_TIMEZONE` overrides it, `UTC` when undetectable |

Override `SPEND_DATABASE_PATH` and `SPEND_PRICING_PATH` in `.env` if needed. `backup_database()` uses `sqlite3.Connection.backup()` and is available for operators; `initialize` does not call it automatically. See [troubleshooting](troubleshooting.md) for the stop-then-copy procedure.

## HTTP surface

| Path | Role |
| --- | --- |
| `GET /` | Opens the HUD (in the desktop app it hides the main window). Serves the dashboard HTML only with `BURNRATE_ENABLE_DASHBOARD=1`; shows the setup wizard on `/?app=1` until setup is complete |
| `GET /hud` | Sidecar HUD HTML |
| `GET /api/spend/hud` | HUD live meters and activity, without loading usage history |
| `GET /api/spend/hud/usage` | HUD spend statistics from recorded usage; each read keeps usage collection active |
| `GET /api/spend/freshness` | This computer's collection, quota and sync freshness (never proxied to a hub) |
| `GET /spend.css`, `/spend.js`, `/favicon.svg` | Static assets (content-hash cache busting) |
| `GET /healthz` | Liveness `{"status": "ok"}` |
| `GET /api/spend/summary` | Windowed aggregation (what the UI polls; navbar burn/today come from this payload) |
| `GET /api/spend/nav` | Navbar burn rate / today. Present for compatibility; the dashboard does not fetch it |
| `GET /api/spend/entity` | Tool or model drill-in (fetched when the UI opens a detail view) |
| `GET /api/spend/health` | Ingest/quota health from SQLite |
| `GET /api/spend/limits` | **Live compatibility probe.** Not used by the dashboard. Can reach authenticated quota collectors, including experimental ones. Do not expose it on a public surface. |

Responses never include credentials or raw provider documents. API JSON is `Cache-Control: no-store`. Security headers include `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and a Content-Security-Policy.

The HUD reads `/api/spend/hud` every 5 seconds and `/api/spend/hud/usage` every 20 seconds. A meter whose newest poll returns no value keeps its last reading (dimmed) until that window resets, a plan missing from one response stays for up to 15 minutes, and spend figures hold until the local date changes. On a paired spoke both reads serve the last hub copy at once and renew it in the background (`HUD_HUB_STALE_SECONDS`, 300 s), so a slow or unreachable hub never stalls a refresh.

Windows: `15m`, `30m`, `1h`, `3h`, `6h`, `12h`, `1d`, `1w`, `1mo`, `mtd`, `ytd`, `all` (plus legacy aliases `7d`, `30d`, `MTD`, `YTD`, `All`).

## Persistence

Events are provider-agnostic once stored:

| Table | What is kept |
| --- | --- |
| `usage_events` | source, tool, model, time, opaque session/project ids, token classes, optional provider `cost_usd`, computed YAML cost, `is_exact` |
| `unpriced_usage_events` | same identity/token fields plus `unclassified_tokens` and `telemetry_complete`; no invented spend |
| `provider_cost_buckets` | admin cost buckets (OpenAI / Anthropic) |
| `sessions` | opaque session ids for Codex and Claude local |
| `quotas` | used / allowance / pct / reset / unit / PAYG flag; unused fields stay NULL |
| `agent_runs` | live-turn cards (id, name, model, state, timestamps) |
| `ingest_runs` | status, event counts, redacted error |
| `subscriptions` / `subscription_daily_costs` | operator-configured plans and materialized daily proration |
| `pricing_gaps` / `coverage_gap_events` | unpriced or incomplete coverage, no prompts |

Credentials never enter these tables. Project fields store directory **names**, not full home paths.

## Scheduler

Local ingest runs serially every 15 seconds (`max_instances=1`): Codex, Claude Code, Traycer (Grok/OpenRouter projections), OpenCode, ZCode, Grok Build, Antigravity, Muse Code. A failed experimental adapter does not skip the rest of the cycle. Usage collection runs while the HUD (or the dashboard, when enabled) requests usage; after two minutes without such requests it finishes the current source and defers further scans, while quota and activity polling continue.

Admin ingest (OpenAI, Anthropic) runs every 15 minutes over the last two hours.

Quota collectors tick every 15 seconds; each lane decides whether it is due:

| Lane | Active / idle seconds |
| --- | --- |
| Codex (local telemetry) | 15 / 15 |
| OpenRouter credits | 60 / 300 |
| Claude Code | 90 / 300 |
| OpenCode / Z.AI | 30 / 300 |
| OpenCode Go | 30 / 300 |
| Muse Code | 30 / 300 |
| Grok Build | 30 / 300 |
| Antigravity | 90 / 300 |

Activity (live cards) polls every 4 seconds.

Cursor is no longer collected or counted. Cursor usage duplicates SuperGrok usage, so it is not counted: on first start each database (this computer's and the Hub's) removes its recorded Cursor rows after saving a verified backup to `backups/pre-cursor-removal-*.db`, and a Hub drops Cursor rows pushed by computers still on an older version.

Direct xAI and Requesty have no collector.

## Provider boundary

Adapters live under `spend_app/adapters/`. Each source reports availability, authority, freshness, interface stability (`supported` / `experimental`), and a reason when data is skipped, failed, partial, or unavailable. Vocabulary is `exact` | `derived` | `partial` | `unavailable`.

The aggregator consumes the event schema above plus those capability reports. Adding a provider should not require a long `if tool_key == …` chain in aggregation. Experimental providers fail independently.

Local readers must not write harness stores. Network clients talk only to allowlisted HTTPS hosts with timeouts; credential-bearing clients set `trust_env=False` so process proxy environment cannot redirect secrets. Errors are redacted before they are persisted.

See [providers](providers.md) for the matrix.

## Optional Windows helpers

Supported helpers (when present under `scripts/`) launch the same FastAPI process — not a six-process telemetry stack:

- `scripts/install-burnrate.ps1` — per-user scheduled task `BURNRATE Dashboard`
- `scripts/start-burnrate.ps1` / `stop-burnrate.ps1` / `status-burnrate.ps1`
- `scripts/uninstall-burnrate.ps1` — does not delete `%LOCALAPPDATA%\BURNRATE\*.db` unless `-PurgeData`

The process mutex is `Local\BURNRATE-Dashboard`. The PID file lives under `%LOCALAPPDATA%\BURNRATE\run`. Bind checks require `127.0.0.1`.

## Out of default install

Not started, not health-checked, not documented as how to run BURNRATE:

- OpenTelemetry Collector, Prometheus, Grafana
- Legacy token/cache UI
- Traycer Prometheus exporter
- Tailscale (optional operator choice; see [tailscale.md](tailscale.md))
