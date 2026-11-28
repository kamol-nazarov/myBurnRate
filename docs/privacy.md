# Privacy

BURNRATE is local-first. Its default listener binds to `127.0.0.1`. Quota meters can read existing sign-ins and contact the provider when the relevant switch is enabled. Pairing a computer enables metadata sync to the hub you configure.

This page is the disclosure. Pair it with [providers.md](providers.md) for hosts and file patterns.

## What SQLite stores

Column-level, in the BURNRATE database only:

| Kind | Fields |
| --- | --- |
| Identity | `source`, `tool_key`, `model_key`, opaque `session_id` / `raw_id`, project **name** (folder basename, not a full home path) |
| Live cards | On the originating computer, `agent_runs.name` may contain a harness session title. A paired hub receives and stores the fixed tool label. |
| Time | `occurred_at`, `ingested_at`, quota `resets_at`, session start/end |
| Tokens | `input_tokens`, `cached_input_tokens`, `cache_write_tokens`, `cache_write_1h_tokens`, `output_tokens`, optional `reasoning_tokens`, `unclassified_tokens` |
| Money | optional provider-reported `cost_usd`, YAML `computed_cost_usd`, `is_exact`, subscription amounts you entered |
| Coverage | `telemetry_complete`, priced vs unpriced flags, gap issue codes |
| Quota | `used`, `allowance`, `unit`, `pct`, `is_payg`, NULL when unknown |
| Operations | ingest `status`, redacted `error`, event counts |
| Hub access | Owner account details, password and credential hashes, paired computer records, audit action/target/outcome, and resolved caller address for relevant audit events |

## What a paired computer sends to its hub

| Data | Fields |
| --- | --- |
| Computer | Display name, which may start as the operating system's host name, operating system label, and persistent device ID |
| Usage and sessions | Source, tool, model, project or folder name, opaque event and session IDs, observation and session times, token counts, coverage flags, and reported or calculated costs |
| Activity | Opaque run ID, fixed tool label, model, state, and start/last-seen times |
| Provider billing | Provider/organization and bucket identifiers, project/model/line item where available, billing period, amount, and reported time |
| Quotas | Provider, limit, label, used/allowed values, unit, percentage, pay-as-you-go flag, reset and poll times, and reading source |
| Sync progress | Journal positions and proofs, including event IDs needed to acknowledge changes or deletions |
| Account and plan actions | Sign-in or registration inputs sent to the selected hub over HTTPS, and plan IDs, names, effective dates, amounts, cadence and terms exchanged during owner plan synchronization |

The desktop keeps session titles in its local activity store. An upgraded hub also replaces titles received from older desktops and cleans the titles in its active database. Existing backup archives follow their own retention policy.

## What is excluded from usage records and sync

- Prompts, responses, reasoning text, diffs, attachments
- Tool names with arguments or results
- File contents, source code, clipboard
- Environment variables, `.env` values, API keys, OAuth refresh tokens, CSRF tokens
- Raw admin API bodies, trajectory JSON, Composer documents
- Credential-shaped strings in logs (`bearer`, `basic`, `sk-…`, `x-api-key`, `Authorization`, `zai_…`, `xai-…`, `or-v1-…`, and JWT-shaped tokens are stripped before `ingest_runs.error` is written)

Some adapters parse a larger record in memory (a Claude JSONL line, a Traycer projection, an Antigravity PROD_UI trajectory) and then keep only the metadata above. The extra content is discarded.

## Local file access

Harness stores are read-only:

- SQLite: adapters and schema inspection use `file:<path>?mode=ro` plus `PRAGMA query_only=ON`; so do the Claude, OpenCode, quota and saved-profile readers, the pricing audit, and the subscription-upgrade source reader
- JSONL / logs / CSV: open for read (`r` / `rb`)
- No writes to Codex, Claude transcripts, OpenCode, ZCode, Grok logs, Traycer `chat.db`, or Antigravity logs

BURNRATE opens these tool files for reading. Claude account discovery reads account IDs, plan tiers and cached usage from `.claude.json` (leaving Claude sign-in tokens and credential files untouched). Codex discovery reads `tokens.account_id` from `~/.codex*/auth.json`, and OpenCode discovery reads `opencode.db` / `auth.json` key metadata (hashed locally with an install-scoped HMAC-SHA256 key; never surfaced in API JSON or stored as unsalted hashes). Claude meters come from cached local plan usage or the Traycer CLI.

Traycer CLI (`profile-rate-limits`, `agent list`) is a local process spawn, not a file write. BURNRATE captures stdout JSON and does not feed prompts. Side effects inside `traycer.exe` are outside BURNRATE’s control; the spawn is **experimental** and can be disabled with `BURNRATE_ENABLE_TRAYCER_CLI=0`.

## Network

The desktop service and CLI load the same user settings. Z.AI, OpenCode Go and Muse quota meters are enabled by default. Each uses its named switch in Sources or the per-user `.env`. Reused-sign-in meters need an existing local sign-in. Other network lanes require their own configuration or a running local tool.

| When | Host | Credential |
| --- | --- | --- |
| `OPENAI_ADMIN_KEY` set | `api.openai.com` | Bearer admin key |
| `ANTHROPIC_ADMIN_KEY` set | `api.anthropic.com` | `x-api-key` |
| `OPENROUTER_MANAGEMENT_KEY` set | `openrouter.ai` | dedicated management key |
| DeepSeek balance account connected or `DEEPSEEK_BALANCE_KEY` set | `api.deepseek.com` | dedicated balance key |
| Z.AI quota enabled by default with `BURNRATE_ENABLE_ZAI_QUOTA` | `api.z.ai` | existing Coding Plan key found in OpenCode/ZCode, `~/.config/glm-acp-agent/credentials.json`, or `Z_AI_API_KEY` |
| OpenCode Go quota enabled by default with `BURNRATE_ENABLE_OPENCODE_GO_QUOTA` | `opencode.ai` | OpenCode Console key from the selected profile |
| Muse quota enabled by default with `BURNRATE_ENABLE_MUSE_QUOTA` | `api.meta.ai` (`POST /muse-code/key` every 30–300s) | saved Muse Code session (`~/.config/muse/auth.json`) |
| Antigravity running (experimental) | `127.0.0.1` over HTTPS on its local language-server port | ephemeral localhost CSRF |
| Traycer installed and `BURNRATE_ENABLE_TRAYCER_CLI` not set to `0` | Local Traycer CLI process | Traycer manages its own sign-in and side effects |
| Hub paired or sign-in requested | The HTTPS origin shown for your configured hub | Machine bearer for sync or owner credentials for account/owner actions |
| Native update checks and release asset downloads | `github.com`, `objects.githubusercontent.com`, `release-assets.githubusercontent.com` | Release requests and app version identifier |

Empty admin keys skip with a named reason, exit 0, and never log the key. Credential-bearing HTTP clients set `trust_env=False` so `HTTP_PROXY` cannot redirect secrets.

BURNRATE does not post prompts or chat completions to a model endpoint.

## Update checks (native app)

The native Windows app can look for a newer version.

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


## Bind and browser

- Default listener: `127.0.0.1:17331`. Not `0.0.0.0`.
- No third-party analytics, no Google Fonts CDN, no browser calls to provider APIs.
- CSP (`default-src 'none'`) and the usual security headers are on; no HTML pages are served over HTTP. The desktop app reads `/api/spend/usage`, `/api/spend/summary`, `/api/spend/entity`, `/api/spend/hud`, `/api/spend/hud/usage`, `/api/spend/freshness`, and `/api/machines`. These read persisted SQLite (or, on a paired spoke, the hub's copy), not live provider probes; only explicit refresh actions ask the quota collectors to poll now. `/api/spend/nav` exists for compatibility.

Optional Tailscale Serve is an operator choice and is never enabled by BURNRATE. Funnel is not a product feature. See [tailscale.md](tailscale.md).

## Credentials

| Rule | Detail |
| --- | --- |
| Provider key storage | In-app keys use the OS credential store. Keys supplied through the per-user `.env` remain plaintext in that file. |
| Paired credentials | The desktop uses the OS credential store for the machine token and owner session and sends them only to their issuing hub origin. |
| Never in SQLite | Not in `usage_events`, `quotas`, or `ingest_runs` |
| Never in API JSON | Including `/api/spend/limits` error strings (redacted) |
| Never in doctor output | Paths may be generic; secrets are not echoed |
| OpenRouter | Inference key ≠ `OPENROUTER_MANAGEMENT_KEY` |
| Claude | Cached plan usage and account metadata, Claude Desktop history, and the local Traycer CLI. Claude sign-in tokens and credential files are outside the read path. |

`GET /api/spend/limits` serves persisted quota snapshots from SQLite only; live quota polling runs in the background scheduler or on explicit `POST /api/spend/quotas/refresh`.

Local `agent_runs.name` session titles pass through `redact()` before storage, non-live (`no_data`) rows expire after 24 hours, and setting `BURNRATE_HIDE_LOCAL_TITLES=1` stores fixed tool labels locally as well.

## How to wipe

1. Stop BURNRATE (`burnrate` process, or `scripts/stop-burnrate.ps1` / `scripts/uninstall-burnrate.ps1`).
2. Run `burnrate wipe --credentials --data --yes` to delete BURNRATE-owned OS Credential Manager / Keychain secrets, the local database, `.burnrate-auth\access.key`, logs, backups, and `%TEMP%\burnrate-wv2-sessions`.
3. On Windows, ticking **Also delete my myBurnRate data** in the GUI uninstaller or running `scripts/uninstall-burnrate.ps1 -PurgeData` clears OS Credential Manager entries, `%LOCALAPPDATA%\BURNRATE` (including `.burnrate-auth`), and `%TEMP%\burnrate-wv2-sessions`. Uninstall without those options keeps your data.
4. On a hub, `burnrate machines delete-data --id <id> --confirm <id>` (or `POST /api/machines/{id}/data/delete`) deletes a revoked computer's usage, sessions, quotas, and gaps with `PRAGMA secure_delete = ON` + `VACUUM` (pass `--keep-backup` / `keep_backup=true` only if you want a pre-delete backup; a minimal revocation tombstone stays in `machines.v1`). To remove the hub owner account and sessions locally, run `burnrate hub delete-owner <email> --yes`.
5. In optional **myBurnRate Cloud** mode (disabled by default until you sign in via Settings), each verified user's usage metadata is stored in an isolated per-user SQLite database (`users/<user_id>/spend.db`). Signup, password resets, and email changes use 6-digit codes sent via Resend; additional computers can be linked with one-time 8-character pairing codes (`br_cs_*` / `br_cm_*` tokens stored in your OS keychain). Deleting your cloud account from Settings (`POST /api/cloud/account/delete`) permanently removes your account rows, sessions, machine tokens, and isolated cloud SQLite database immediately while leaving local usage on your own computers untouched.

Deleting BURNRATE’s database does not delete Codex, Claude, Cursor, or other harness history. Those files were only read.
