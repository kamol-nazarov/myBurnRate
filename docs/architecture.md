# Architecture

BURNRATE is a local-first architecture: a FastAPI API service (`127.0.0.1:17331`), a SQLite WAL database, an in-process scheduler, and the native desktop window (`spend_app/wealthfront_window.py` loading `spend_app/wealthfront_ui.html`). It is not an inference proxy, not a hosted service, and not a Grafana stack.

The internal Python package in this tree is `spend_app`. The public command is `burnrate`.

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
  FastAPI  127.0.0.1:17331  (/api/*, /healthz)
        │
        ▼
  Native desktop window  (wealthfront_window.py + wealthfront_ui.html)
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
| `GET /api/spend/hud` | Live meters and activity, without loading usage history |
| `GET /api/spend/hud/usage` | Spend statistics from recorded usage; each read keeps usage collection active |
| `GET /api/spend/freshness` | This computer's collection, quota and sync freshness (never proxied to a hub) |
| `GET /healthz` | Liveness `{"status": "ok"}` |
| `GET /api/spend/summary` | Windowed aggregation (what the UI polls; navbar burn/today come from this payload) |
| `GET /api/spend/nav` | Navbar burn rate / today. Present for compatibility; the dashboard does not fetch it |
| `GET /api/spend/entity` | Tool or model drill-in (fetched when the UI opens a detail view) |
| `GET /api/spend/health` | Ingest/quota health from SQLite |
| `GET /api/spend/limits` | **Live compatibility probe.** Not used by the dashboard. Can reach authenticated quota collectors, including experimental ones. Do not expose it on a public surface. |

Non-API paths return `404` JSON (`REQ-404`); no HTML pages are served over HTTP. Responses never include credentials or raw provider documents. API JSON is `Cache-Control: no-store`. Security headers include `X-Content-Type-Options`, `Referrer-Policy: no-referrer`, `X-Frame-Options: DENY`, and `Content-Security-Policy: default-src 'none'; base-uri 'none'; form-action 'none'; object-src 'none'; frame-ancestors 'none'`.

The desktop window polls `/api/spend/hud` and `/api/spend/hud/usage` via the local client. A meter whose newest poll returns no value keeps its last reading (dimmed) until that window resets, a plan missing from one response stays for up to 15 minutes, and spend figures hold until the local date changes. Both payloads carry `lastIngestAt`, `lastIngestAgeSeconds` and `stale`; on a paired spoke both reads serve the last hub copy at once and renew it in the background (`HUD_HUB_STALE_SECONDS`, 300 s), so a slow or unreachable hub never stalls a refresh.

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
| `usage_changes` | ordered local usage identities changed by inserts, corrections or deletions; written transactionally by triggers |
| `subscriptions` / `subscription_daily_costs` | operator-configured plans and materialized daily proration |
| `pricing_gaps` / `coverage_gap_events` | unpriced or incomplete coverage, no prompts |

Credentials never enter these tables. Project fields store directory **names**, not full home paths.

Schema 12 adds a usage change journal. Export batches read a consistent snapshot
and send the latest state or a deletion for each changed identity. Hubs acknowledge
the sequence transactionally, so replay cannot restore an older correction.
Imported machine rows do not echo back into the journal. Malformed historical
timestamps are diagnosed and skipped explicitly rather than blocking a batch;
repairing the local row creates a new change to synchronize.

Upgrade both spoke and hub to replicate corrections and deletions. Older hubs
still accept ordinary upserts, but cannot acknowledge the new journal; pending
repairs stay queued and the desktop app requests a hub update. Existing rows
seed the journal during migration. Events deleted before the journal existed
cannot be reconstructed from the remaining local database.

## Scheduler

Local ingest runs serially every 15 seconds (`max_instances=1`): Codex, Claude Code, Traycer (Grok/OpenRouter projections), OpenCode, ZCode, Grok Build, Antigravity, Muse Code. A failed experimental adapter does not skip the rest of the cycle. Collection is unconditional: a pass runs whether or not a viewer is open, and adapters skip unchanged inputs cheaply. A HUD or dashboard request only pulls the next pass forward, so a dead viewer cannot freeze ingestion. A stalled loop is always reported by freshness and restarted by the watchdog.

Admin ingest (OpenAI, Anthropic) runs every 15 minutes over the last two hours.

Quota collectors tick every 15 seconds; each lane decides whether it is due:

| Lane | Active / idle seconds |
| --- | --- |
| Codex (local telemetry) | 15 / 15 |
| OpenRouter credits | 60 / 300 |
| DeepSeek balance | 60 / 300 |
| Claude Code | 90 / 300 |
| OpenCode / Z.AI | 30 / 300 |
| OpenCode Go (each profile) | 30 / 300 |
| Muse Code | 30 / 300 |
| Grok Build | 30 / 300 |
| Antigravity | 90 / 300 |

Activity (live cards) polls every 4 seconds.

The scheduler owns the collector worker's lifetime. Shutdown stops admission and
interrupts an in-flight worker before closing shared resources. Quota and activity
updates also request immediate spoke sync using the configured pricing engine;
follow-up failures produce sanitized log warnings.

Native supervision uses a bounded `/healthz?supervisor=1` probe with application
identity, process ID and collection progress. The detailed response is
owner-only on a hub; the local watchdog keeps using it on loopback. It preserves
startup grace and a restart budget, and restarts only its owned service. An open
TCP port alone is not application health; unrelated listeners are left untouched.

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
