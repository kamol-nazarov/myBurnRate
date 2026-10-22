# BURNRATE

**Private, local-first AI coding-agent spend, quota & multi-computer telemetry application.**

Version: `0.3.0-beta.15` · Current release notes: [RELEASE-0.3.0-beta.15.md](RELEASE-0.3.0-beta.15.md) · [Changelog](CHANGELOG.md)

---

## Table of Contents

1. [What BURNRATE Is](#what-burnrate-is)
2. [Key Application Features](#key-application-features)
3. [Quick Start: Clone & Install from Release](#quick-start-clone--install-from-release)
4. [Running BURNRATE (Web, Background Service, or Native Desktop)](#running-burnrate-web-background-service-or-native-desktop)
5. [First-Run Application Setup Wizard](#first-run-application-setup-wizard)
6. [Multi-Computer Sync (Hub & Spoke)](#multi-computer-sync-hub--spoke)
7. [CLI Command Reference](#cli-command-reference)
8. [What the Dashboard Looks Like](#what-the-dashboard-looks-like)
9. [What It Measures and Does Not](#what-it-measures-and-does-not)
10. [Provider Compatibility and Stability](#provider-compatibility-and-stability)
11. [Privacy and Data-Flow Summary](#privacy-and-data-flow-summary)
12. [Exact vs Derived vs Unavailable](#exact-vs-derived-vs-unavailable)
13. [Configuration and Subscriptions](#configuration-and-subscriptions)
14. [Optional Tailscale Serve](#optional-tailscale-serve)
15. [Troubleshooting and `burnrate doctor`](#troubleshooting-and-burnrate-doctor)
16. [Development and Test Commands](#development-and-test-commands)
17. [License and Non-Affiliation](#license-and-non-affiliation)

---

## What BURNRATE Is

BURNRATE is a private, local-first application for tracking AI coding-agent token usage, cache efficiency, subscription capacity, run-dry pace forecasts, and API-equivalent cost across all your coding tools and computers.

It reads harness-local files and, if you opt in, a small set of read-only provider admin or quota APIs. It **never** sits on the inference path: **Codex**, **Claude Code**, **OpenCode**, **ZCode**, **Grok Build**, **Google Antigravity**, and **Traycer** keep talking to their providers directly. There is no hosted BURNRATE cloud account, no telemetry phone-home, and no inference proxy.

The default runtime is a single lightweight process: **FastAPI + SQLite + Sidecar HUD** bound to `127.0.0.1:17331`, with an optional **Tauri v2 native desktop shell** providing a Windows system tray icon and the frameless Always-on-Top HUD. The HUD is the product; the full dashboard is off by default and returns with `BURNRATE_ENABLE_DASHBOARD=1`.

- **Windows-first**, CPython 3.12.
- **Localhost-only** by default (`127.0.0.1:17331`).
- **Empty credentials are valid.** Local ingest needs zero API keys.
- **Missing data stays missing.** Unavailable metrics render as `—`, never `$0`.

---

## Key Application Features

- **Interactive 5-Step Setup Wizard (`/setup`)**: Configure your computer's name, pick a multi-computer role (**Primary Hub**, **Connected Spoke**, or **Standalone**), auto-scan & import local AI coding harnesses on the spot, select active monthly subscriptions with 1-click presets, and configure the Sidecar HUD.
- **Unified Spend & Capacity Dashboard (optional)**: Off by default. With `BURNRATE_ENABLE_DASHBOARD=1` it is served at `/`; otherwise `/` opens the HUD.
  - **Live Capacity & Run-Dry Forecasting**: Per-provider quota gauges sorted by *what runs out first*, projecting when a 5-hour or weekly window will exhaust based on your recent burn pace, with configurable in-app and browser alerts.
  - **Plans & Value**: Compare `This month` or `Last month` subscription expense against recorded API-equivalent usage, shared tool associations, and value multiples.
  - **Action Center**: Tracks quota thresholds, monitored-source issues, and unpriced models with snooze/acknowledge workflows.
  - **Token & Unit Economics**: Breakdown of fresh input, cached input, cache writes, output, and reasoning tokens by tool, model, project, and session, plus counterfactual cache-savings calculations.
- **Always-on-Top Sidecar HUD (`/hud`)**: Frameless floating window (`380` wide, sized to its content) in a graphite or paper theme. It leads with today’s API-rate spend, then burn rate, cache reuse, and tokens, then every subscription window as a segmented meter row: percentage, an even-pace tick, and the time until that window resets. **Mini** collapses it to a `296`-wide glance strip with one line per plan (its tightest window); click a plan to list all of its windows. A poll that returns no value never blanks a meter: it keeps its last reading (dimmed) until that window resets. Drag it from any empty surface; Mini, Theme, Pin, and Hide stay clickable, and a double-click does not maximize it.
- **Single-Account Multi-Computer Sync**: Pair laptops and secondary desktops to your primary Hub using a 1-click `brpair_...` pairing code. Spokes automatically push incremental token usage, sessions, and live agent state into your Hub database.
- **Native Desktop App (`src-tauri/`)**: Tauri v2 desktop wrapper with system tray menu (`Toggle Sidecar HUD`, `Toggle HUD Always on Top`, `Run Setup Wizard…`, `Quit BURNRATE`); its main window opens only for setup and automatic background process supervision.

---

## Quick Start: Clone & Install from Release

### Prerequisites

- **OS:** Windows 10 or Windows 11
- **Python:** [CPython 3.12](https://www.python.org/downloads/) (`py -3.12` or [`uv`](https://docs.astral.sh/uv/))
- **Shell:** PowerShell (`pwsh` or Windows PowerShell)

### One-Command Clone & Install

```powershell
git clone https://github.com/kamol-nazarov/myBurnRateOfficial.git
cd myBurnRateOfficial
.\install.ps1
```

Running `.\install.ps1` (or `.\scripts\install-burnrate.ps1`) automatically performs the entire installation in one step:
1. Creates the Python 3.12 `.venv` and installs the `burnrate` package.
2. Initializes the local SQLite database at `%LOCALAPPDATA%\BURNRATE\spend.db`.
3. Registers the windowless auto-start scheduled task (`BURNRATE Dashboard`) and starts the background service on `127.0.0.1:17331`.
4. Installs Desktop and Start Menu shortcuts (`BURNRATE.lnk`) with the custom high-DPI BURNRATE logo.
5. Automatically pops open the native **BURNRATE Desktop** application window (with custom taskbar & system tray icon) directly on the **Application Setup Wizard**.

<details>
<summary><strong>Manual Setup (Optional)</strong></summary>

If you prefer to create the virtual environment manually without registering the auto-start service:

```powershell
uv venv --python 3.12 .venv
.\.venv\Scripts\Activate.ps1
uv pip install .
burnrate init
burnrate doctor
```

</details>

Installed runs store SQLite and logs under `%LOCALAPPDATA%\BURNRATE` (`spend.db`). Set `BURNRATE_DEV=1` if you want to keep data repo-relative during development.

---

## Running BURNRATE (Web, Background Service, or Native Desktop)

You can run BURNRATE in three ways depending on your workflow:

### Option A: Foreground Server + Browser

```powershell
burnrate serve
```

Then open:
- **First-Run Setup Wizard:** [http://127.0.0.1:17331/setup](http://127.0.0.1:17331/setup)
- **Sidecar HUD:** [http://127.0.0.1:17331/hud](http://127.0.0.1:17331/hud) (also [http://127.0.0.1:17331](http://127.0.0.1:17331))
- **Full Dashboard (optional):** set `BURNRATE_ENABLE_DASHBOARD=1`, restart, and open [http://127.0.0.1:17331](http://127.0.0.1:17331)

The HUD starts live quota and activity polling immediately. Its spend statistics load separately and keep usage collection running in the background, without building dashboard summaries. HUD usage requests (and dashboard requests, when it is enabled) renew collection on this computer and, for HUD usage, on the paired hub. After two minutes without usage requests, collection finishes its current source and defers further scans. Live polling and spoke sync continue. On a paired spoke, the HUD shows the last hub copy at once and renews it in the background, so a slow or unreachable hub never stalls it.

### Option B: Windowless Windows Background Service (Auto-Start at Sign-In)

Use the included PowerShell lifecycle scripts to run BURNRATE silently in the background (`pythonw.exe`) and register a Windows Scheduled Task (`BURNRATE Dashboard`) that starts automatically when you sign in. Install also registers `BURNRATE Watchdog`, which runs `pythonw -m spend_app.watchdog` every 5 minutes, with no console window. It restarts the service when it stops answering or when no collection pass has finished for 10 minutes (30 for the first pass after a start), at most 3 restarts an hour,, and it leaves a service you stopped on purpose alone. Its actions are logged to `logs\watchdog.log`.

```powershell
# Register auto-start and watchdog scheduled tasks and launch in background
.\scripts\install-burnrate.ps1
.\scripts\start-burnrate.ps1

# Check live service health, PID, watchdog state, and database path
.\scripts\status-burnrate.ps1

# Restart the service (stops only the owned listener, starts it through the sign-in task)
.\scripts\restart-burnrate.ps1

# Stop the background service (the watchdog leaves it stopped until the next start)
.\scripts\stop-burnrate.ps1

# Uninstall scheduled task (keeps spend.db unless -PurgeData is passed)
.\scripts\uninstall-burnrate.ps1
```

### Option C: Native Desktop App & System Tray (`burnrate desktop`)

Launch the native Tauri v2 desktop application (which automatically supervises the local backend process, opens the main window and Always-on-Top Sidecar HUD, and docks in the Windows system tray):

```powershell
burnrate desktop
```

---

## First-Run Application Setup Wizard

When you open [http://127.0.0.1:17331/setup](http://127.0.0.1:17331/setup) (or click **Setup Wizard** in **Settings → Connected computers** / the system tray menu), the 5-step wizard walks you through:

1. **Step 1 — Computer Identity:** Name this machine (e.g., `Main Workstation`, `Studio Laptop`) and verify your detected OS and timezone.
2. **Step 2 — Multi-Computer Topology:**
   - **Primary Hub (`hub`):** Stores the master SQLite database and generates 1-click `brpair_...` pairing codes for your other computers.
   - **Connected Spoke (`spoke`):** Connects to your Primary Hub using a `brpair_...` pairing code and automatically streams local usage in the background.
   - **Standalone (`standalone`):** Tracks only this computer locally.
3. **Step 3 — AI Harnesses Auto-Scan & Import:** Automatically detects local installations of **Claude Code**, **OpenAI Codex CLI**, **OpenCode**, **ZCode**, **Grok CLI**, **Antigravity**, and **Traycer**, and imports historical usage and active agents with one click.
4. **Step 4 — Active Subscriptions:** Select from 1-click monthly plan presets (`Claude Max`, `Claude Pro`, `ChatGPT Pro`, `ChatGPT Plus`, `SuperGrok`, `Z.AI Coding Plan`, `OpenCode Go`, `OpenCode Go Plus`) or add custom plans so Plans & Value and daily burn proration work immediately.
5. **Step 5 — Sidecar HUD Configuration:** Choose whether the frameless Sidecar HUD opens automatically on launch and stays pinned Always-on-Top.

---

## Multi-Computer Sync (Hub & Spoke)

BURNRATE supports single-account, self-hosted multi-computer synchronization without any external cloud service:

1. **On your Primary Hub computer:**
   - Open `/setup` (Step 2) or **Settings → Connected computers → Add computer**.
   - Select your reachable Hub address (Tailscale `100.x.y.z` or LAN IP) and generate a **Pairing Code** (`brpair_...`).
2. **On your secondary Spoke computer:**
   - Install BURNRATE, open `/setup` (Step 2), choose **Connected Spoke**, and paste the `brpair_...` code.
   - Once connected, the Spoke's background scheduler automatically pushes incremental usage events, sessions, and live agent runs to the Hub every ingest cycle.
   - Alternatively, you can run the lightweight CLI sync agent directly:
     ```powershell
     burnrate sync --hub http://<hub-ip>:17331 --token br_m_<id>_<secret>
     ```
3. **Filtering by Computer:**
   - Once two or more computers are registered, a computer switcher (`All computers` + each machine name) appears in the top bar of both the main dashboard and the Sidecar HUD.

---

## CLI Command Reference

```powershell
burnrate --version
burnrate init                                  # Initialize %LOCALAPPDATA%\BURNRATE\spend.db
burnrate doctor                                # Read-only environment, asset & SQLite health check
burnrate serve [--host 127.0.0.1] [--port 17331]
burnrate desktop [--check-only]                # Launch Tauri v2 native desktop app & Sidecar HUD

# Multi-computer management & sync
burnrate machines list
burnrate machines register --name "MacBook Pro"
burnrate machines revoke --id <machine_id>
burnrate sync --hub http://<hub>:17331 --token <br_m_token> [--once]

# Subscription management
burnrate subscription list
burnrate subscription add --tool-key codex --name "Codex" --amount-usd 20 --cadence monthly --start-date 2026-09-01

# On-demand historical backfill (CLI-only)
burnrate backfill <source>
```

---

## What the Dashboard Looks Like

The dashboard is off by default (`BURNRATE_ENABLE_DASHBOARD=1` turns it back on). It is a single-page app titled **BURNRATE · AI Cost Intelligence**, styled like an instrument panel: a graphite (dark) or paper (light) ground that follows your OS until you pick one with the theme button, DIN-style Bahnschrift numerals, hairline grids instead of cards, and a single ember accent reserved for what needs your eye. Quota meters are segmented gauges; sections are numbered `01`, `02`, … After `burnrate serve`, open [http://127.0.0.1:17331](http://127.0.0.1:17331).

A typical overview (synthetic demo data — not a real account):

- **Top nav.** A full-width strip: gauge mark and wordmark, burn rate, today’s spend (in ember), metering status (live, stale, or failed), and chips for the HUD, sync, harnesses, monthly plans, Attention, and the theme.
- **Coverage banner.** Which tools are billed-exact versus published-rate (`≈`). Unpriced usage is named, never filled in.
- **Capacity.** “What runs out first”: a hairline grid with one gauge per subscription. The tightest window is the large figure beside its reset countdown, and every weekly, 5-hour, or monthly window gets a segmented meter with an even-pace tick. Meters turn ember from 60% and red from 85%. Missing quotas stay blank.
- **Activity.** In-progress turns only. Opening an editor or an idle shell does not appear here. Usage totals update when a turn completes.
- **Analysis window.** `15m`, `30m`, `1h`, `3h`, `6h`, `12h`, `1d`, `1w`, `1mo`, `MTD`, `YTD`, `All`. The selected range is the inverted segment.
- **KPI band.** Tracked value, measured tokens, API-equivalent cost per million tokens, cache reuse, and session count in one hairline-divided strip.
- **Token activity chart.** Stacked by tool or cumulative, with per-tool totals below and a model price board plus an ember weekday-by-hour heatmap further down.
- **Recoverable / forecast.** Cache savings versus a no-cache counterfactual, and a labeled forecast of plan cost versus published-rate value.
- **Unit economics.** Cost by model (click through for a tool or model detail page), token mix (cached input / fresh input / output), and any subscriptions you configured, as hairline tables with inline share bars.

The Sidecar HUD at [http://127.0.0.1:17331/hud](http://127.0.0.1:17331/hud) uses the same system in a narrow window: today’s spend as one large readout, a three-cell strip (burn rate, cache, tokens), any live agents, then one aligned meter row per plan window with its percentage and reset countdown. Mini mode keeps only today’s spend and each plan’s tightest window.

Public screenshots, when added, always use synthetic Northwind-style names and placeholder figures. They are not captures of a live database.

A fresh install with no harness history shows an empty state, not an error.

Use **Connect harness** for guided local discovery, manual locations, persistent connection status and optional documented provider API setup. See the [connection guide](docs/connections.md) for capabilities, credential requirements and development verification limits.

See [subscription management](docs/subscriptions.md) for effective-dated plans, and [earlier release notes](RELEASE-0.2.0-beta.2.md) for accounting, schema migration, source resilience, and credential consent details.

---

## What It Measures and Does Not

**Measures**

- Completed-turn token classes when the harness reports them: fresh input, cache read, cache write (including Anthropic 5-minute / 1-hour writes when distinct), output, and reasoning when it is a separate field.
- Cache hit rate from those measured components: cache-read input ÷ measured total input. Unclassified schemas are excluded from the rate, not estimated.
- Subscription burn you configure yourself (daily proration of the amount you entered).
- Published-rate *equivalents* from effective-dated YAML price cards.
- Capacity snapshots when a quota source exists.

**Does not measure**

- Prompts, responses, diffs, tool payloads, attachments, or source code.
- Inference latency as a spend proxy.
- “You should have used model X” as a fact.
- Spend for traffic with no trustworthy amount.
- Opening an editor, an idle Composer, or an idle shell as a live session.
- Direct xAI API usage or Requesty (neither is connected).

BURNRATE never reroutes completions. Cache behavior of the underlying providers is unchanged.

---

## Provider Compatibility and Stability

Official vs experimental is about the *interface BURNRATE uses*, not about whether the product itself is well known. Undocumented endpoints are labeled **experimental** and can fail independently.

| Provider / harness | Interface | Stability | Local files | Network | Tokens / spend |
| --- | --- | --- | --- | --- | --- |
| Codex Desktop | local JSONL + projection SQLite | supported | yes | none | tokens exact; spend exact when the price card is `exact` |
| Claude Code | local JSONL transcripts | supported | yes | none for usage | tokens exact; spend exact when the price card is `exact` |
| OpenCode | local session SQLite | supported | yes | Z.AI quota HTTPS if a Coding Plan key is on disk | session-level tokens; spend derived |
| ZCode | local `model_usage` SQLite | supported | yes | shares the Z.AI quota card | per-request tokens exact; spend derived |
| OpenCode Go | OpenCode messages with `providerID=opencode-go` + `opencode.ai/zen/go/v1/usage` | **experimental** usage endpoint, on by default | OpenCode's own credential store (read-only) | HTTPS to `opencode.ai` | per-message tokens; spend derived from the Go price card; 5-hour, weekly and monthly limits exact |
| Grok Build CLI | local `unified.jsonl` and session `updates.jsonl` | **experimental**, on by default | yes | none | tokens exact; spend derived |
| Direct xAI API | — | **not connected** | none | none | unavailable |
| Google Antigravity | localhost language-server RPC | **experimental**, on by default | log for port/CSRF | `https://127.0.0.1:<port>/` | tokens exact; spend derived |
| OpenRouter (via Traycer) | Traycer usage facts + optional credits API | Traycer path **experimental**, on by default | yes | `openrouter.ai` only with a management key | tokens classified; spend derived |
| Traycer per-CLI usage | `app-assets.db` `usage_analytics_facts` (older hosts: `chat.db`) | **experimental**, on by default | yes | none | per-CLI tokens; provider-reported cost only when Traycer marks it as such |
| OpenAI admin usage/cost | official Usage + Costs APIs | opt-in official | none | `api.openai.com` if `OPENAI_ADMIN_KEY` is set | exact |
| Anthropic admin usage/cost | official org usage/cost | opt-in official | none | `api.anthropic.com` if `ANTHROPIC_ADMIN_KEY` is set | exact |
| Claude OAuth usage | undocumented `/api/oauth/usage` | **experimental, opt-in** | `~/.claude/.credentials.json` | Anthropic + Claude platform | quota only; requires `BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE=1` |
| Additional Claude accounts | each profile's cached plan usage, else its `/api/oauth/usage` | **experimental**, on when a second profile is signed in | `~/.claude-<name>/.claude.json` (and `.credentials.json` for the fallback) | none while the profile is in use; Anthropic + Claude platform when idle, under the same opt-in | quota only, one lane per account |
| Requesty | — | **not connected** | none | none | not a rate authority |

Full file patterns, hosts, credentials, mutation risk, and double-count rules: [docs/providers.md](docs/providers.md).

### More than one Claude account

Claude Code keeps one signed-in account per config directory. Run a second account with `CLAUDE_CONFIG_DIR=%USERPROFILE%\.claude-work` (any `.claude-<name>` works), sign in once, and BURNRATE shows it as its own plan lane, **Claude Code · Work**, beside the default account in the dashboard, the Sidecar HUD, and the Mini HUD.

- Profiles are found automatically in `~/.claude-*`. Put other directories in `BURNRATE_CLAUDE_ACCOUNT_DIRS` (separated by `;` on Windows).
- A profile signed in to the same account as the default lane is ignored, so nothing is shown twice.
- While a profile is in use, its lane reads the plan usage Claude Code itself cached, which needs no network call. When that cache is more than 20 minutes old, the lane asks Anthropic's usage endpoint with that profile's own credentials, on the same throttled cadence as the default Claude lane.
- Lanes are named after the directory. The account's email address is never shown.

---

## Privacy and Data-Flow Summary

Default: **no provider network calls** until you enable an admin key or an experimental quota path. The HTTP listener is `127.0.0.1` only.

SQLite stores usage metadata: timestamps, model and provider keys, opaque ids, numeric token classes, priced/unpriced flags, and quota numbers. It does not store prompts, responses, tool arguments, file contents, env vars, or credentials.

Local harness databases are opened `mode=ro` with `PRAGMA query_only=ON`. JSONL and logs are opened for read. The only provider-file write in the product is Claude OAuth token refresh, and that path is **off unless** `BURNRATE_CLAUDE_OAUTH_REFRESH=1`.

Credentials are held in memory, sent only to the matching allowlisted host, never logged, never returned by the API, and never copied into SQLite. OpenRouter inference keys are not reused; balance polling requires `OPENROUTER_MANAGEMENT_KEY`.

The browser loads local CSS/JS only. There is no third-party analytics.

Details: [docs/privacy.md](docs/privacy.md).

---

## Exact vs Derived vs Unavailable

| Label | Meaning | UI |
| --- | --- | --- |
| **Exact** | Invoice-authoritative usage source **and** a YAML card with `exactness: exact`. Today that is Codex local / OpenAI admin and Claude local / Anthropic admin. | plain number |
| **Derived** / published-rate | Included-plan attribution at published API rates, or a card marked `exactness: derived`. Not a bill. | `≈` when complete, `≥` when only a priced subset is known |
| **Unavailable** | No trustworthy sample (missing file, skipped key, incomplete telemetry, unpriced model). | `—` — **never `$0`** |
| **Partial** | Some events in the window are priced and some are not. | named as partial |
| **Forecast** | Projection from paced usage. Incomplete pricing is a known-spend floor, not a complete projection. | labeled forecast |

Cache savings is the per-event no-cache counterfactual, not a blended ratio. If any cached event in the window is unpriced or telemetry-incomplete, savings is unavailable rather than silently small.

Reasoning tokens that the harness already includes in output are displayed as detail and are not added twice.

More: [docs/pricing.md](docs/pricing.md).

---

## Configuration and Subscriptions

Optional keys in `.env` (all empty by default — copy `.env.example` to `.env` if desired and never commit a populated `.env`):

| Variable | Role |
| --- | --- |
| `OPENAI_ADMIN_KEY` | OpenAI organization usage + cost |
| `ANTHROPIC_ADMIN_KEY` | Anthropic organization usage + cost |
| `OPENROUTER_MANAGEMENT_KEY` | OpenRouter credits (dedicated; not the inference key) |
| `BURNRATE_ENABLE_CLAUDE_OAUTH_USAGE` | Set to `1` to consent to native Claude OAuth usage reads |
| `BURNRATE_CLAUDE_OAUTH_REFRESH` | With usage consent enabled, set to `1` to additionally allow token refresh |
| `BURNRATE_ENABLE_ZAI_QUOTA` | Set to `1` to consent to quota reads using a stored Z.AI key |
| `BURNRATE_ENABLE_OPENCODE_GO_QUOTA` | Set to `1` to consent to OpenCode Go limit reads using OpenCode's stored Console key |
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

---

## Optional Tailscale Serve

BURNRATE never enables Tailscale, Serve, or Funnel. If you already run Tailscale and want the localhost dashboard on your tailnet:

```powershell
tailscale serve --bg --https=443 http://127.0.0.1:17331
```

Access URL: `https://<magicdns-name>/` — replace the placeholder with your own MagicDNS name. Funnel (public Internet exposure) is out of scope; see [docs/tailscale.md](docs/tailscale.md).

---

## Troubleshooting and `burnrate doctor`

```powershell
burnrate doctor
```

Doctor checks CPython 3.12, required imports, timezone data, a read-only SQLite integrity check, packaged pricing YAML, packaged web assets, and that the default bind is loopback. It does not migrate, does not walk provider paths, does not inspect credentials, and does not print secrets. Run `burnrate init` first; doctor will not create `spend.db`.

Common cases:

- Empty dashboard after install — expected until a harness writes usage (or run **Scan & Import Usage Now** in `/setup`).
- Admin job `skipped` — empty key; not a failure.
- `unavailable` / `—` — missing telemetry or pricing; not zero.
- Port `17331` already in use — another BURNRATE process is running (`.\scripts\status-burnrate.ps1`).
- Amber status pill naming a source (for example `MUSE WEEKLY HELD` or `TRAYCER FAILED`) — that source or meter on *this* computer stopped refreshing. Hover the pill (or open **Diagnostics → Freshness**) for the reason and its age; `GET /api/spend/freshness` returns the same report and is never proxied to the hub.
- Service logs live in `%LOCALAPPDATA%\BURNRATE\logs\dashboard.log` and rotate at 5 MB (three backups kept). Each local collection pass logs one line with its duration and slowest sources.
- Experimental provider down — that lane fails independently.

Full legend: [docs/troubleshooting.md](docs/troubleshooting.md).

---

## Development and Test Commands

For frontend development, edit the readable files in `frontend_src/`. Run
`npm ci --prefix scripts/asset-tools --ignore-scripts --no-audit --no-fund`, then
`node scripts/optimize-assets.cjs` to regenerate the committed `spend_web/`
assets and their checksums. Node is a development tool only; Python installs
use the committed assets. Existing public IDs and CSS classes are preserved.

```powershell
py -3.12 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -U pip
python -m pip install -e ".[dev]"
python -m pytest
```

`python -m pytest` collects `tests_spend`, which includes temporary SQLite,
application-startup and browser tests as well as pure units. For focused source-evidence
tests, run `python -m pytest -q tests_spend/test_source_evidence_unit.py`.

Optional localhost bench (dev only): `scripts/Bench-Burnrate.ps1` against `http://127.0.0.1:17331`. It prints p50/p95 without a committed baseline file. Do not point it at a tailnet hostname.

See also [CONTRIBUTING.md](CONTRIBUTING.md).

---

## License and Non-Affiliation

The project license is **Apache-2.0**. See [LICENSE](LICENSE).

BURNRATE is not affiliated with OpenAI, Anthropic, xAI, Google, Z.AI, OpenRouter, Requesty, Traycer, or their related products. Trademarks belong to their owners.

---

Docs: [architecture](docs/architecture.md) · [providers](docs/providers.md) · [connections](docs/connections.md) · [subscriptions](docs/subscriptions.md) · [privacy](docs/privacy.md) · [pricing](docs/pricing.md) · [Tailscale](docs/tailscale.md) · [troubleshooting](docs/troubleshooting.md)

Community: [SECURITY.md](SECURITY.md) · [CONTRIBUTING.md](CONTRIBUTING.md) · [CHANGELOG.md](CHANGELOG.md) · [LICENSE](LICENSE)
