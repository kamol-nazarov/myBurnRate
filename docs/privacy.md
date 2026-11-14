# Privacy

BURNRATE is local-first. The default install makes **no provider network calls**, binds only `127.0.0.1`, and stores usage metadata — not conversation content.

This page is the disclosure. Pair it with [providers.md](providers.md) for hosts and file patterns.

## What SQLite stores

Column-level, in the BURNRATE database only:

| Kind | Fields |
| --- | --- |
| Identity | `source`, `tool_key`, `model_key`, opaque `session_id` / `raw_id`, project **name** (folder basename, not a full home path) |
| Live cards | `agent_runs.name` may copy a harness session/chat **title** (not the prompt body). Titles appear on `/api/spend/summary` activity rows. |
| Time | `occurred_at`, `ingested_at`, quota `resets_at`, session start/end |
| Tokens | `input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `cache_write_1h_tokens`, `output_tokens`, optional `reasoning_tokens`, `unclassified_tokens` |
| Money | optional provider-reported `cost_usd`, YAML `computed_cost_usd`, `is_exact`, subscription amounts you entered |
| Coverage | `telemetry_complete`, priced vs unpriced flags, gap issue codes |
| Quota | `used`, `allowance`, `unit`, `pct`, `is_payg`, NULL when unknown |
| Operations | ingest `status`, redacted `error`, event counts |

## What is never stored

- Prompts, responses, reasoning text, diffs, attachments
- Tool names with arguments or results
- File contents, source code, clipboard
- Environment variables, `.env` values, API keys, OAuth refresh tokens, CSRF tokens
- Raw admin API bodies, trajectory JSON, Composer documents
- Credential-shaped strings in logs (`bearer`, `basic`, `sk-…`, `x-api-key`, `Authorization`, `zai_…`, `xai-…`, `or-v1-…`, and JWT-shaped tokens are stripped before `ingest_runs.error` is written)

Some adapters parse a larger record in memory (a Claude JSONL line, a Traycer projection, an Antigravity PROD_UI trajectory) and then keep only the metadata above. The extra content is discarded.

## Local file access

Harness stores are read-only:

- SQLite: `file:<path>?mode=ro` plus `PRAGMA query_only=ON`
- JSONL / logs / CSV: open for read (`r` / `rb`)
- No writes to Codex, Claude transcripts, OpenCode, ZCode, Grok logs, Traycer `chat.db`, or Antigravity logs

There are no exceptions: BURNRATE never writes another tool's files. It also never reads, reuses or refreshes Claude's sign-in (`%USERPROFILE%\.claude\.credentials.json`). Claude meters come from the plan usage Claude Code and Claude Desktop cache on disk themselves, or from Traycer.

Traycer CLI (`profile-rate-limits`, `agent list`) is a local process spawn, not a file write. BURNRATE captures stdout JSON and does not feed prompts. Side effects inside `traycer.exe` are outside BURNRATE’s control; the spawn is **experimental**.

## Network

Default: none, apart from the installed native app's update check (see [Update checks](#update-checks-native-app)).

Opt-in destinations are HTTPS only, host-allowlisted, timed out, and not inference completions:

| When | Host | Credential |
| --- | --- | --- |
| `OPENAI_ADMIN_KEY` set | `api.openai.com` | Bearer admin key |
| `ANTHROPIC_ADMIN_KEY` set | `api.anthropic.com` | `x-api-key` |
| `OPENROUTER_MANAGEMENT_KEY` set | `openrouter.ai` | dedicated management key |
| Z.AI Coding Plan key found on disk | `api.z.ai` | Coding Plan API key |
| Antigravity running (experimental) | `https://127.0.0.1:<port>/` | ephemeral localhost CSRF |

Empty admin keys skip with a named reason, exit 0, and never log the key. Credential-bearing HTTP clients set `trust_env=False` so `HTTP_PROXY` cannot redirect secrets.

BURNRATE does not post prompts or chat completions to a model endpoint.

## Update checks (native app)

The native Windows app can look for a newer version. It is the one request BURNRATE makes by
itself that is not to a provider or a hub you set up:

- **Who checks, and when.** Only a copy installed with `myBurnRate_<version>_x64-setup.exe`
  checks on its own: once about a minute after it starts, then once a day. A portable zip, a
  source install, `--demo` and the smoke test never contact GitHub unless you press
  **Check for updates** in Settings -> General.
- **What is sent.** A plain HTTPS `GET` to
  `https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/native-latest.json`,
  which GitHub redirects to its release-asset host. The request carries no usage data and no
  identifier beyond the `User-Agent` header `BURNRATE/<app version>`; GitHub sees your IP address
  as it does for any web request.
- **Installing is a separate step.** Nothing is downloaded or run until you click **Install &
  restart**. The installer comes from the same repository's release page, and is checked
  (size, SHA-256, a signature against a key built into the app, and for a signed app the
  publisher's Authenticode signature) before it is started.
- **Turn it off.** Set the environment variable `BURNRATE_NO_UPDATE_CHECK=1`. The app then never
  contacts GitHub for updates, automatic or manual, and the button reports that nothing is
  available.

The older Tauri desktop app has its own check; see [code signing](code-signing.md#privacy).

## Bind and browser

- Default listener: `127.0.0.1:17331`. Not `0.0.0.0`.
- No third-party analytics, no Google Fonts CDN, no browser calls to provider APIs.
- CSP and the usual security headers are on. The HUD reads `/api/spend/hud`, `/api/spend/hud/usage`, `/api/spend/freshness`, and `/api/machines`; the dashboard, when enabled with `BURNRATE_ENABLE_DASHBOARD=1`, reads `/api/spend/summary`, `/api/spend/health`, and `/api/spend/entity` (on drill-in). These are persisted SQLite (or, on a paired spoke, the hub's copy), not live provider probes; only the HUD's **Poll** button asks the quota collectors to poll now. `/api/spend/nav` exists but is unused by the UI.

Optional Tailscale Serve is an operator choice and is never enabled by BURNRATE. Funnel is not a product feature. See [tailscale.md](tailscale.md).

## Credentials

| Rule | Detail |
| --- | --- |
| In memory only | Keys are read from the environment or from the provider’s own local store |
| Never in SQLite | Not in `usage_events`, `quotas`, or `ingest_runs` |
| Never in API JSON | Including `/api/spend/limits` error strings (redacted) |
| Never in doctor output | Paths may be generic; secrets are not echoed |
| OpenRouter | Inference key ≠ `OPENROUTER_MANAGEMENT_KEY` |
| Claude | Claude Code's cached plan usage, Claude Desktop history and Traycer; Claude's sign-in is never read |

`GET /api/spend/limits` can fire every quota collector, including the experimental Traycer CLI. Keep it off any public reverse proxy.

## How to wipe

1. Stop BURNRATE (`burnrate` process, or `scripts/stop-burnrate.ps1` / `scripts/uninstall-burnrate.ps1`).
2. Delete the data directory (`%LOCALAPPDATA%\BURNRATE`, or the repo-relative DB in a checkout).
3. Uninstall does **not** delete `*.db` unless you pass `-PurgeData`.

Deleting BURNRATE’s database does not delete Codex, Claude, Cursor, or other harness history. Those files were only read.
