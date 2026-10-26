# Configuration and the command line

## Command line

```powershell
burnrate --version
burnrate init                                  # Initialize %LOCALAPPDATA%\BURNRATE\spend.db
burnrate doctor                                # Read-only environment, asset & SQLite health check
burnrate serve [--host 127.0.0.1] [--port 17331]
burnrate desktop [--check-only]                # Launch Tauri v2 native desktop app & Sidecar HUD
burnrate demo [--port 17377] [--data-root DIR] # Serve made-up data on 127.0.0.1; nothing is collected

# Multi-computer management & sync
burnrate machines list
burnrate machines register --name "MacBook Pro"
burnrate machines revoke --id <machine_id>
burnrate sync --hub http://<hub>:17331 --token <br_m_token> [--once]
burnrate hub update --ssh <host> [--push-local] [--dry-run]  # Fast-forward a self-hosted hub, restart it, print its build

# Subscription management
burnrate subscription list
burnrate subscription add --tool-key codex --name "Codex" --amount-usd 20 --cadence monthly --start-date 2026-09-01

# On-demand historical backfill (CLI-only)
burnrate backfill <source>
```

## Settings and plans

Optional keys in `.env` (all empty by default — copy `.env.example` to `.env` if desired and never commit a populated `.env`):

| Variable | Role |
| --- | --- |
| `OPENAI_ADMIN_KEY` | OpenAI organization usage + cost |
| `ANTHROPIC_ADMIN_KEY` | Anthropic organization usage + cost |
| `OPENROUTER_MANAGEMENT_KEY` | OpenRouter credits (dedicated; not the inference key) |
| `BURNRATE_ENABLE_ZAI_QUOTA` | Z.AI credit reads using a stored Coding Plan key. On by default in the desktop service, which sets it to `1` on first start; setup's **What we found** step shows it as a switch that writes `1`/`0` to the per-user `.env`. Unset (off) for the CLI |
| `BURNRATE_ENABLE_OPENCODE_GO_QUOTA` | OpenCode Go usage reads using OpenCode's stored Console key. On by default in the desktop service, which sets it to `1` on first start; setup's **What we found** step shows it as a switch that writes `1`/`0` to the per-user `.env`. Unset (off) for the CLI |
| `DEEPSEEK_BALANCE_KEY` | DeepSeek key for the balance card (prefer connecting it in setup's **Plans & balances**) |
| `BURNRATE_ALLOW_REGISTRATION` | Set to `1` on a hub to accept new accounts after the first (off: computers join with pairing codes) |
| `BURNRATE_CENTRAL_SERVER_URL` | Default hub address offered when signing in (no built-in hub) |
| `BURNRATE_HUB_SSH` / `BURNRATE_HUB_PATH` / `BURNRATE_HUB_SERVICE` | Defaults for `burnrate hub update` |
| `BURNRATE_DESKTOP_NOTIFICATIONS` | Desktop notifications from the desktop service; `0` turns them off (the CLI never sends them) |
| `SPEND_DATABASE_PATH` | SQLite path (default `%LOCALAPPDATA%\BURNRATE`) |
| `SPEND_PRICING_PATH` | YAML cards (packaged default) |
| `SPEND_TIMEZONE` | IANA zone for midnight and windows (default `UTC`) |
| `BURNRATE_ENABLE_DASHBOARD` | Set to `1` to serve the full dashboard at `/` (off by default; the HUD is the product) |

Fresh `burnrate init` does **not** seed plan prices automatically. You can pick plans interactively in the **Setup Wizard (`/setup`)**, in the dashboard's **Plans & Value** dialog (when the dashboard is enabled), or via the CLI:

```powershell
burnrate subscription add --tool-key codex --name "Codex" --amount-usd 20 --cadence monthly --start-date 2026-09-01
burnrate subscription list
```

`--cadence` is `monthly`, `quarterly`, or `annual`. Daily cost is `amount / days in the period`. That proration is not provider billing and is excluded from API-equivalent fields.

BURNRATE no longer collects Cursor usage or polls Cursor quota. Cursor usage duplicates SuperGrok usage, so it is not counted: on first start each database (this computer's and the Hub's) removes its recorded Cursor rows after saving a verified backup to `backups/pre-cursor-removal-*.db`, and a Hub drops Cursor rows pushed by computers still on an older version.

## Tailscale

BURNRATE never turns on Tailscale, Serve or Funnel. To reach the HUD from your tailnet, see
[Optional Tailscale Serve](tailscale.md).
