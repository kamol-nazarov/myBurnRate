# Configuration and the command line

## Command line

```powershell
burnrate --version
burnrate init                                  # Initialize %LOCALAPPDATA%\BURNRATE\spend.db
burnrate doctor                                # Read-only environment, asset & SQLite health check
burnrate serve [--host 127.0.0.1] [--port 17331]
burnrate desktop [--demo] [--check-only]        # Compact native usage application
burnrate demo [--port 17377] [--data-root DIR] # Serve made-up data on 127.0.0.1; nothing is collected

# Multi-computer management & sync
burnrate machines list
burnrate machines revoke --id <machine_id>
burnrate sync --token-file <path-to-token> [--once]
# or: set BURNRATE_SYNC_TOKEN and omit both flags
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
| `BURNRATE_ACCESS_TOKEN` | Bootstrap/break-glass credential and explicitly configured legacy bearer automation. Normal UI uses a 30-day owner session at `/auth`. Rotation persists only a hash and invalidates the old environment value |
| `BURNRATE_ALLOW_ADMIN_BEARER` | Default `0` restricts the static credential to `/api/auth/bootstrap` and `/api/auth/rotate`. Set `1` only when automation must use the static bearer for other API calls |
| `BURNRATE_HUB_ALLOW_PUBLIC` | Hub mode only (`BURNRATE_CENTRAL_SERVER=1`): set to `1` to allow binding to a public/wildcard interface instead of loopback, RFC 1918, or Tailscale interfaces |
| `BURNRATE_TRUSTED_PROXIES` | Comma-separated reverse-proxy IPs or CIDRs whose `X-Forwarded-For` / `X-Forwarded-Proto` headers are trusted (falls back to `FORWARDED_ALLOW_IPS`, default `127.0.0.1,::1`; wildcards `*`, `0.0.0.0/0`, `::/0` are rejected; `none` disables) |
| `BURNRATE_HUB_URL` | Optional hub address override (e.g. in `%LOCALAPPDATA%\BURNRATE\.env` for testing). Takes precedence over the build-time address in `spend_app/_build_config.py` |
| `BURNRATE_HUB_SSH` / `BURNRATE_HUB_PATH` / `BURNRATE_HUB_SERVICE` | Defaults for `burnrate hub update` |
| `BURNRATE_DESKTOP_NOTIFICATIONS` | Desktop notifications from the desktop service; `0` turns them off (the CLI never sends them) |
| `SPEND_DATABASE_PATH` | SQLite path (default `%LOCALAPPDATA%\BURNRATE`) |
| `SPEND_PRICING_PATH` | YAML cards (packaged default) |
| `SPEND_TIMEZONE` | IANA zone for midnight and windows (default `UTC`) |
| `BURNRATE_ENABLE_DASHBOARD` | Set to `1` to serve the full dashboard at `/` (off by default; the HUD is the product) |
| `BURNRATE_REQUIRE_SIGNED_NATIVE` | Set to `1` to refuse the HUD's native-window launch unless the desktop binary carries a valid Authenticode signature matching a pinned certificate thumbprint |
| `BURNRATE_NATIVE_THUMBPRINTS` | Comma-separated, case-insensitive SHA-1 certificate thumbprints (spaces stripped) trusted to sign the desktop binary (empty by default until SignPath enrolment) |

Fresh `burnrate init` does **not** seed plan prices automatically. You can pick plans interactively in the **Setup Wizard (`/setup`)**, in the dashboard's **Plans & Value** dialog (when the dashboard is enabled), or via the CLI:

```powershell
burnrate subscription add --tool-key codex --name "Codex" --amount-usd 20 --cadence monthly --start-date 2026-09-01
burnrate subscription list
```

`--cadence` is `monthly`, `quarterly`, or `annual`. Daily cost is `amount / days in the period`. That proration is not provider billing and is excluded from API-equivalent fields.

BURNRATE no longer collects Cursor usage or polls Cursor quota. Cursor usage duplicates SuperGrok usage, so it is not counted: on first start each database (this computer's and the Hub's) removes its recorded Cursor rows after saving a verified backup to `backups/pre-cursor-removal-*.db`, and a Hub drops Cursor rows pushed by computers still on an older version.

## Hub address resolution

Release builds embed the hub address at build time into the git-ignored module
`spend_app/_build_config.py` (`BUILTIN_HUB_URL`). At runtime, BURNRATE resolves
the hub address in this order:

1. `BURNRATE_HUB_URL` from the environment or per-user `.env`
   (`%LOCALAPPDATA%\BURNRATE\.env`, skipped under `pytest` so tests stay hermetic);
2. `spend_app._build_config.BUILTIN_HUB_URL` when `_build_config.py` was generated
   during packaging.

Source checkouts do not commit `spend_app/_build_config.py`, so with neither file
nor `BURNRATE_HUB_URL` set, the app runs without a hub. When a hub address is
configured, any existing `spokeConfig.hubUrl` saved in SQLite migrates to the
resolved address on startup, and the resolved address is redacted from API
responses, `syncCommand`, logs, error messages, and diagnostics reports.

## Tailscale

BURNRATE never turns on Tailscale, Serve or Funnel. To reach the HUD from your tailnet, see
[Optional Tailscale Serve](tailscale.md).

## Hub authentication migration

No new dependency is required. Sessions last 30 days; machine tokens last 90 days;
these durations are fixed policy constants.
`machines.v1` and `accounts.v1` migrate automatically, in one transaction with marker `auth_model.v1`.
The obsolete `pairing_grants` table is dropped automatically on upgrade; computers
that were already paired keep their machine tokens and keep syncing.
Legacy machine hashes are retained; expiry is the original `created_at` plus 180 days (not 180 days
from upgrade), with `rotation_required=true`. A token already older than that needs reconnection.
Account password hashes and creation dates stay intact; stable IDs and owner roles are added.

The hub never asks an existing owner for a setup token (`BURNRATE_SETUP_TOKEN` and
`BURNRATE_ALLOW_REGISTRATION` are deprecated and ignored). Old 0.5.5 spokes can still push
with unexpired migrated tokens, but must upgrade/sign in for hub-wide reads. `/auth` on a spoke
stores a separate owner session server-side; it is excluded from public setup state and diagnostics.
Use **Rotate this computer's token** before machine expiry; after expiry reconnect from the app.
Explicit reconnection resends a usage snapshot so a replacement machine identity never reuses the old
sync cursor. Same-hub reconnection keeps a separate owner session; sign-in and token rotation keep
the machine cursor intact.
See [self-hosting](self-hosting.md#auth-model-upgrade-and-rollback) for backups and rollback.
