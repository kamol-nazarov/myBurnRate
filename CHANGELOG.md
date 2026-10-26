# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is licensed under Apache-2.0.

## [Unreleased]

## [0.5.2] - 2026-10-26

### Fixed

- Installing an update from the HUD works. The HUD page comes from the local
  service, which Tauri treats as a remote origin, and no capability allowed
  the app's own commands for it: the HUD offered each update, and every click
  was refused with "Command install_update not allowed by ACL". The two
  update commands are now declared in `build.rs` and allowed in the default
  capability, and a test checks both.
- A failed update stays offered, so "try again" retries it. Before, the first
  attempt removed it and every retry failed with "No update is waiting".
- The HUD says why an update failed, and so does the tray menu item, which
  then retries.
- The HUD card is all but opaque. At 80% opacity, text in the window behind
  read through it.

## [0.5.1] - 2026-10-26

### Fixed

- On Windows, a terminal window no longer flashes each time setup loads. The
  installed service has no console, and the two `tailscale serve` calls in
  setup started a console program without hiding it. A test now fails on any
  background command started without `creationflags` or `startupinfo`.

## [0.5.0] - 2026-10-26

### Added

- A real installer. The desktop app now ships its own copy of the BURNRATE
  service (built with PyInstaller), so a download needs no Python, git or
  PowerShell. The Windows installer installs per user, stops the app and
  service before replacing files, and its "delete app data" box also removes
  BURNRATE's database, settings and logs.
- The desktop app starts the service itself, restarts it when it dies or stops
  answering, and stops it on quit; on Windows a job object ends it even if the
  app crashes. Windows open on a "Starting BURNRATE" page instead of waiting.
- An installed app starts at sign-in (a tray switch turns it off) and checks
  GitHub Releases for signed updates; the HUD and tray offer to install one.
- `BURNRATE_DATA_ROOT` moves the database, settings and logs together, and a
  service on another data root gets its own process lock.
- macOS and Linux. The service runs there (file lock instead of a Windows
  mutex, notifications through `osascript` or `notify-send`, keys in the
  Keychain or the Secret Service), and releases include a `.dmg` for Apple
  Silicon plus a Linux AppImage and `.deb`. Tests run on all three systems.
- Releases are built on GitHub Actions from a version tag, with build
  provenance attestations, a `SHA256SUMS` file and a signed `latest.json` for
  in-app updates. The Windows installer is signed through SignPath once the
  project is approved (see `docs/code-signing.md`); the release stays a draft
  until it is published by hand.
- Scoop and winget manifests with every release.
- A Docker image and Compose file (with Caddy for HTTPS) to run a hub on a
  server; see `docs/self-hosting.md`.
- Cost per repository: the HUD lists today's value by the repository each
  session ran in. T3, Claude Code and Orca worktrees count toward their
  repository.
- A share button in the HUD copies the last seven days as an image: value at
  API rates against the plans, tokens, cache hit, sessions and top tools. It
  never names a repository.
- `burnrate demo` serves a month of made-up usage on `127.0.0.1:17377` without
  reading anything from the computer it runs on.
- A weekly workflow lists price cards that need a person's attention (a price
  window ending soon, or an OpenRouter price that moved) in one open issue. It
  never edits a card.
- A rate card for GPT-6.1 Terra ($1 input, $0.05 cached, $1.25 cache write,
  $5 output per million tokens; 2x / 1.5x above 272K input tokens).

### Changed

- BURNRATE no longer reuses Claude's sign-in. It never reads or refreshes
  `~/.claude/.credentials.json` and never calls Anthropic's plan-usage
  endpoint; setup's Claude switch is gone. Claude meters come from the plan
  usage Claude Code caches in `.claude.json` (now kept for up to a week and
  shown with its age, with a passed reset read as reset), Claude Desktop's
  history, or Traycer, whichever is newest.
- Release notes moved to `docs/releases/`.
- Code and tests no longer name a private hub host or checkout path.
- A shorter README with screenshots, downloads and first-open steps; the
  detail moved to `docs/user-guide.md`, `docs/configuration.md` and
  `docs/development.md`.
- One stated methodology for every token count and API-rate value
  (`docs/pricing.md`, "Methodology"). A tool's own cost estimate (OpenCode's
  `cost`) no longer outranks the published card; a provider's own charge still
  does. Rate cards can price peak hours, and the DeepSeek V4.1 Flash and V4
  Pro cards do.
- The HUD and dashboard show a capacity card only for a tool in use: one that
  has given a real meter reading, recorded usage in the last 30 days, or has
  an active plan. A tool that is not installed no longer shows an empty
  "quota unavailable" card; a used tool whose meter is missing still does.

### Fixed

- Grok Build usage was counted twice: once per model call and once per
  completed turn. Each turn now counts once, at Grok Build's own charge
  (`costUsdTicks`); calls of a turn that never completed still count. Every
  database, a hub included, removes its stored duplicates once at start-up
  after a backup to `backups/pre-grok-dedupe-*.db`, and a hub drops them from
  computers on older versions as they push.
- GPT-6 Sol, Grok 4.7, Grok 4.7 Fast and the DeepSeek API's `deepseek-flash`
  had no rate card, so their tokens counted toward today but their value did
  not. Claude Sonnet 5.5's cache hits were priced at $0.20 per million instead
  of the published $0.10. DeepSeek API reasoning tokens were dropped.
- The HUD's today figure marks a partial day with `≥` and says how many tokens
  have no published price. See `docs/burnrate-pricing-reconciliation-oct26.md`.

## [0.4.0-beta.1] - 2026-10-26

- Start the 0.4 line. The code is 0.3.0-beta.19's; the version moves because
  the HUD-first desktop app, hub and spoke sync, native shell and rebuilt setup
  are a different application from the dashboard the 0.3.0 betas began as.
  Production (the hub) now tracks the `release/0.4.0-beta.1` branch.

## [0.3.0-beta.19] - 2026-10-26

- Give each extra OpenCode profile (`~/.opencode-<name>`, or a directory in
  `BURNRATE_OPENCODE_PROFILE_DIRS`) its own OpenCode Go quota lane, such as
  **OpenCode Go · Profile 2**, read with that profile's own Console key. A
  profile on the same account as the default one is ignored.
- Add a DeepSeek balance lane. Connect a DeepSeek API key in Connections; the
  official `/user/balance` endpoint reports the USD balance left.
- Show the money left on pay-as-you-go lanes (OpenRouter, DeepSeek) on the HUD
  row itself instead of "PAYG", and carry a fresher local balance over the
  Hub's on a Spoke.
- Explain a rejected OpenRouter key: the credits endpoint needs a management
  key, not an inference key.
- Rebuild setup around the HUD in four steps: **This computer** (just this
  computer, join my hub with a pairing code or account, or make this the hub
  and create pairing codes), **What we found** (one honest row per HUD card:
  usage, meter, the fix, and the three sign-in-reuse switches), **Plans &
  balances** (plans suggested from the meters, OpenRouter/DeepSeek keys), and
  **Check** (live HUD preview and anything still needing you).
- **Set up later** stops the desktop app reopening setup on every launch; the
  HUD's new gear button and the tray reopen it. `/connections` opens the keys
  step, so keys work without the dashboard.
- Signing in to a hub account never creates one; registration needs at least
  8 characters. Setup changes require BURNRATE's own request header, so another
  web page cannot repoint this computer at a different hub.
- Connecting a tool's default location (Auto-detect) no longer switches off its
  activity and plan meter; only custom or disabled locations do.
- The wizard's first check answers in milliseconds: it reads the ingest log
  instead of running full diagnostics, and its scan only asks the scheduler to
  collect now instead of running a second import inside the request.
- Remember which session files were already read across restarts
  (`ingest_file_cache`), so the first pass after a restart skips unchanged
  Claude, Codex and Muse files instead of re-reading every one (Claude: about
  6 minutes down to under 2 seconds on a large history). OpenCode skips
  sessions whose fingerprint has not changed.
- Save long first reads in batches of 5,000 events, so a cold read no longer
  holds the database write lock for minutes.
- Move the usage de-duplication map out of `app_meta` into a compact
  `usage_keys` table. Existing entries move over in small background steps;
  reads fall back to the old rows until each step lands.
- A quota poll whose save fails (for example "database is locked") retries in
  a minute instead of waiting a whole interval with nothing recorded.
- Collection runs in its own worker process in the desktop service, so a long
  read never slows the HUD or the API. A stuck worker is replaced.
- Count usage from extra profiles: an extra Claude account's `projects` folder
  and an extra OpenCode profile's sessions are recorded under that card's key
  (`claude-code-work`, `opencode-go-2`), so each card gets its own burn rate.
- Setup lists plans that need a look (a plan for a retired tool, or a custom
  plan that matches a known card) and fixes them in one click, on the hub when
  this computer is a spoke.
- Muse Code's meter says it is idle when Meta's endpoint stops reporting a few
  hours after the last use, instead of looking broken. Antigravity's says the
  app is closed when its language server is not running.
- Hubs: a hub that has an account accepts no new ones unless
  `BURNRATE_ALLOW_REGISTRATION=1` (computers join with pairing codes); there
  is no built-in hub address; the hub check compares builds (version plus
  commit), not just version numbers; and `burnrate hub update` updates a
  self-hosted hub over ssh and reports the build it now runs.
- The docs now match the defaults: the desktop service turns the sign-in-reuse
  reads on at first start, and setup shows each as a switch.
- Balance runway: OpenRouter and DeepSeek cards say how long the balance lasts
  at the recent spend ("about 12 days at your current spend ($3.40/day)"),
  from the last 14 days of readings. Top-ups are not spend. The HUD shows the
  days left in the reset column, with the daily spend in its tooltip.
- Desktop notifications from the service as Windows toasts: a plan passes its
  alert level, a plan will run out before it resets, a meter has not updated
  for over an hour, or hub sync keeps failing. Each is sent once per window or
  stretch and waits out quiet hours unless a run-out is under two hours away.
  Setup's Check step has a switch for each and a test button;
  `BURNRATE_DESKTOP_NOTIFICATIONS=0` turns them all off.
- **Copy diagnostics** in setup's Check step: versions, freshness, meters,
  recent failed runs and the tail of the service logs, with keys, e-mail
  addresses and the home folder removed. Nothing is sent anywhere.

## [0.3.0-beta.18] - 2026-10-24

- Kill HUD staleness at the root: local collection no longer waits for a
  viewer, so a hidden window, a sleeping laptop or a wedged poll can never
  stop ingestion. A stalled collection loop is always reported as an alarm
  instead of being masked as "deferred".
- Show the data's age in the HUD (`Updated 12s ago`) and alarm when the data,
  the live-meter poll or the recorded-usage poll goes quiet. Every HUD fetch
  now has a 12-second abort, so one hung request cannot freeze later polls.
- Forecast run-dry from measured token burn when quota pace is stale or
  missing (`basis: usage`), including 5-hour and weekly windows. A quiet last
  hour is zero burn, not the window's historical average.
- Refresh HUD payloads with `lastIngestAt`, `lastIngestAgeSeconds` and
  `stale`, computed from the newest successful ingest pass.
- Keep provider data read-only; provider schema selection is unchanged.
- Update the stale test fixtures that still expected Cursor collection, and
  drop configured-but-never-attempted lanes from a group whose other sources
  have real evidence.

## [0.3.0-beta.17] - 2026-10-22

- Fix "today" counting from UTC midnight when `SPEND_TIMEZONE` is unset.
  The default timezone is now this computer's own IANA zone, so every
  since-local-midnight total covers the local calendar day; an evening in
  New York previously showed only the usage after 00:00Z (20:00 local).
  `SPEND_TIMEZONE` still overrides, and UTC remains the fallback when the
  system zone cannot be detected.
- Count interactive Codex CLI sessions again. Current CLI builds write
  originator `codex-tui`, which the rollout reader rejected as unsupported,
  silently dropping those sessions from the ledger.

- Stop the HUD's recorded usage flipping between this computer and the hub.
  A spoke keeps the last hub copy across a sync and answers a HUD poll at
  once while that copy renews, and a window-token total follows a row whose
  tokens were revised in place. The computer filter keeps each computer's
  own spend, and this computer's filter still counts the usage collected
  here when the hub has not answered.
- Show tokens burned inside each quota window when hovering a HUD meter, split
  into fresh input, output and cache reads, from the usage already collected for
  that plan (`windowTokens` on `/api/spend/hud/usage`). Weekly meters also list
  today's tokens since local midnight by model. Z.AI counts OpenCode and ZCode
  usage only, Antigravity splits Gemini from its other models, and extra Claude
  accounts show no figure because only the default profile's sessions are
  collected.
- Rebuild the HUD tooltips: one card layout (title, figure, label/value rows,
  footer), short labels on the icon buttons, a single pace verdict per meter,
  and tips that close when the pointer leaves the window, follow the hovered
  row through a data refresh, and are swept if their row is no longer hovered.
- Make the native HUD window transparent. The card is see-through at rest and
  firms up under the pointer; tooltips stay solid. A tall tooltip grows the
  window downward instead of being clipped.
- Animate collapse/expand between the full and mini HUD, and fix the mini
  button drawing both of its icons at once: one icon now turns between
  "collapse" and "expand".
- Fix Z.AI quota readings freezing after a key change: every stored Coding Plan
  key is tried (OpenCode 1 and 2, ZCode, and now glm-acp-agent or
  `Z_AI_API_KEY`), the one Z.AI accepts is remembered, and a rejected key is
  reported as an authentication error instead of an empty reading.

## [0.3.0-beta.16] - 2026-10-22

- Add OpenCode Go. OpenCode messages served by Go (`providerID=opencode-go`)
  are now their own tool instead of counting toward the Z.AI Coding Plan, with
  reasoning billed as output, and are valued with a new `pricing/opencode-go.yaml`
  card built from the official Go rates (it reproduces OpenCode's recorded Go
  cost). A new OpenCode Go card shows the 5-hour, weekly and monthly limits from
  OpenCode's usage endpoint, using the Console key OpenCode already stores
  (consent `BURNRATE_ENABLE_OPENCODE_GO_QUOTA`, on by default; no card without a
  Go subscription). OpenCode Go and Go Plus are plan presets, and the HUD shows
  a pace tick for monthly windows.
- Read the Z.AI Coding Plan key from OpenCode 2's credential store when the
  OpenCode 1 `auth.json` has none.
- Keep usage collection active while the HUD is open, including on paired
  spokes and hubs, so spend and tokens continue updating after midnight.
  Quota meters still load independently of collection and dashboard summaries.
- Give resumed collection its own watchdog grace period after idle time, and
  show collection failures in the active HUD's freshness report.
- Make the HUD the product. The full dashboard is off by default and returns
  with `BURNRATE_ENABLE_DASHBOARD=1`; otherwise `/` opens the HUD, the desktop
  app starts with its main window hidden (shown only for setup), and the tray
  menu no longer offers the dashboard. In Mini mode, clicking a plan lists all
  of its windows.
- Keep HUD numbers on screen while polling. A meter whose newest poll returns
  no value holds its last reading (dimmed, explained in its tooltip) until
  that window resets, a plan missing from one response stays for up to 15
  minutes, and spend figures hold until the date changes. A manual poll's
  result shows as soon as it lands.
- On a paired spoke, serve the HUD's last hub copy at once and renew it in the
  background, so a slow or unreachable hub no longer stalls each refresh. New
  local quota data marks the copy stale instead of discarding it, and a failed
  hub read falls back to that copy rather than a local-only view.
- Cut the Grok Build collection pass from about 15 seconds to about 3 while
  a session is active: unchanged `updates.jsonl` files reuse their parse,
  only sessions whose rows changed are reconciled again (every session is
  fully reconciled at least every five minutes), and the session files are
  listed once per pass. Stored rows are identical to a full pass.
- Fix a Grok reconcile crash when a session had both unified-log rows and a
  recorded signal total.
- Remove Cursor: its local, usage-service, Admin API and CSV collectors, the
  Cursor quota and activity readers, the `network-ingest` job, the
  `cursor-local`/`cursor-csv`/`cursor-admin` backfill commands, and the
  `CURSOR_API_KEY`, `CURSOR_IMPORT_PATH` and
  `BURNRATE_ENABLE_CURSOR_USAGE_SERVICE` settings. Traycer runs launched in
  Cursor are skipped. Cursor usage duplicates SuperGrok usage, so it is no
  longer counted: each database (local and Hub) removes its recorded Cursor
  rows once, after a verified backup in `backups/pre-cursor-removal-*.db`,
  without changing the schema version, and a Hub drops Cursor rows pushed by
  computers on an older version. A saved Cursor plan groups with SuperGrok.

## [0.3.0-beta.15] - 2026-10-20

- Start HUD quota and activity polling without importing transcript history or
  computing the full dashboard summary. Recorded spend statistics load through
  a separate endpoint so they cannot hold up the live meters.
- Request historical collection only from a visible full dashboard. After two
  minutes without dashboard requests, finish the current source and defer
  further scans. Quota polling, activity polling, and spoke sync keep running.
- Keep deferred history separate from HUD freshness and watchdog stall checks.
- Move live-stream database and hub reads off the web server's event loop.
  Detect committed database changes with SQLite's change counter instead of
  scanning usage history for every viewer every two seconds.

## [0.3.0-beta.14] - 2026-10-19

- Stop the console window that opened every 5 minutes. uv had written the
  console launcher as `.venv\Scripts\pythonw.exe`, so the watchdog (and the
  dashboard at sign-in) each opened a console, and closing it ended the
  service. Install now swaps in the base interpreter's windowless venv launcher
  whenever it finds a console one.
- Track every signed-in Claude Code account. Each extra profile (for example
  `~/.claude-work`, or any directory in `BURNRATE_CLAUDE_ACCOUNT_DIRS`) gets its
  own quota lane, "Claude Code · Work", in the dashboard, Sidecar HUD, and Mini
  HUD. The lane reads the plan usage Claude Code cached for that account (no
  network) and falls back to that profile's own OAuth usage when the cache is
  older than 20 minutes. A spoke shows the lane even before its hub knows it.

## [0.3.0-beta.13] - 2026-10-19

- Redesign the dashboard, Sidecar HUD, and setup wizard as one "Instrument"
  system: a graphite (dark) or paper (light) ground that follows the OS until
  you pick one with the new theme button, Bahnschrift (DIN) numerals and labels,
  hairline grids instead of pastel cards, numbered sections, and a single ember
  accent for what needs attention.
- Show every quota window as a segmented meter with an even-pace tick, colored by
  that window's own pressure (neutral, ember from 60%, red from 85%), so a hot
  weekly window is flagged even when the card's lead window is quiet.
- Rebuild the Sidecar HUD around one large "today" readout, a three-cell strip
  (burn rate, cache, tokens), and aligned per-window meter rows. Mini mode is now
  a glance strip: today's spend plus one line per plan showing its tightest window.
- Replace the brand mark and native app icons with a gauge whose redline is ember.
- Show each provider's real logo wherever it appears: capacity gauges, live
  agents, tool filters, the chart legend and tooltip (tinted in the tool's chart
  color), model and subscription rows, the price ladder, tool detail pages, data
  health, every dialog, the Sidecar HUD (including Mini), and the setup wizard.
  The marks ship as one cached sprite, `/logos.svg`.
- Render the weekday-by-hour heatmap on an ember ramp with a square-root scale so
  quiet hours stay distinguishable from empty ones.
- Stop the dashboard from scrolling itself down on load at phone widths.
- Keep usage meters current: Cursor's usage service now fetches only the recent
  tail on its own scheduler job instead of re-downloading the whole history in
  the shared local pass, which had held every other source behind it for
  minutes. On a spoke, a fresher local quota reading now replaces the hub's
  older one.
- Skip re-reconciling OpenCode, ZCode, and Grok stores whose files have not
  changed since the last clean pass (a full pass still runs every 5 minutes).
- Stop re-reading every Grok session `updates.jsonl` on each pass. An unchanged
  file reuses its parse, so this step drops from about 6 s (777 MB across 446
  files here) to under 1 s.
- Stop re-reading idle Claude Code transcripts on every pass. A transcript with
  any permanently unusable record (an older revision, a conflicting cache
  duration) was never cached, so about 1 GB of days-old transcripts was parsed
  and reconciled every 30 seconds. The Claude pass drops from about 20 s to
  about 4 s, and those files are still reported as partial.
- Fix the Traycer source, which failed on every pass with "A source link points
  outside the approved location": its duplicate check opened BURNRATE's own
  database through the provider-file confinement. Usage that only Traycer
  recorded (Codex, Claude Code and pre-log Grok runs it launched) is now
  counted; Cursor runs it launched are left to Cursor's own local store.
- Add a freshness alarm. `GET /api/spend/freshness` reports, for this computer
  only, every source that failed or went stale, quota meters holding a reading
  that newer polls could not confirm (with the provider's reason), hub sync
  failures, a stalled collection loop, quiet synced computers, and a hub running
  a different BURNRATE version. The Sidecar HUD status pill (and the Mini HUD)
  and the dashboard status pill name the first issue in amber, and Diagnostics
  lists them all.
- Add a watchdog: `install-burnrate.ps1` registers `BURNRATE Watchdog`, which runs
  `pythonw -m spend_app.watchdog` every 5 minutes and restarts the service through
  the new `restart-burnrate.ps1` when it stops answering or its collection loop
  stalls (10 minutes without a finished pass; 30 for the first pass after a
  start). An explicit `stop-burnrate.ps1` is respected; restarts are capped at
  3 an hour.
- Rotate `dashboard.log` at 5 MB with three backups, and drop per-request HTTP
  and per-job scheduler chatter, which had grown the log to about 150 MB. Each
  local collection pass now logs one line with its duration and slowest sources.

## [0.3.0-beta.9] - 2026-10-14

- Add central account server hosting on `tk-server` (`/api/account/register`,
  `/api/account/login`, `/api/account/subscriptions`) so any computer logged into
  the account automatically syncs usage, live provider quotas, and subscriptions.
- Simplify the Application Setup Wizard (`/setup`) to a 2-step flow: Step 1
  logs in (or creates an account with subscription presets), and Step 2 connects
  the AI coding providers installed on that computer.

## [0.3.0-beta.8] - 2026-10-14

- Add interactive 5-step Application Setup Wizard (`/setup` and `/api/setup/*`)
  for configuring computer identity, multi-computer Hub/Spoke topology with
  1-click `brpair_...` pairing codes, live local AI harness scanning, subscription
  presets, and Sidecar HUD preferences.
- Add single-account multi-computer sync hub (`/api/machines`, `/api/sync/push`,
  `/api/spend/stream`, and automatic background spoke-to-hub sync) with per-machine
  dashboard filtering.
- Add Tauri v2 native desktop shell (`src-tauri/`) with system tray controls,
  background process supervision, and frameless Always-on-Top Sidecar HUD (`/hud`).
- Redesign the dashboard so overview, dialogs, and the tool page share one
  layout, and replace the visible hour grid with the price of a million tokens
  by model.
- Read Antigravity's current language-server token from the running process when
  the desktop log no longer records it, so the quota call is not sent with a
  token from an older startup.
- Keep scanning a local source when one directory sits on Windows' 259-character
  path limit. Cursor local was failing the whole ingest on an empty agent folder.
- Price Claude Opus 5.5, Claude Sonnet 5.5, and GPT-6.1 Sol from their published
  October 2026 cards. The Haiku 4.5 snapshot id and OpenCode GLM 5.3 resolve to
  cards already on file. Cursor Grok Bot stays unpriced: neither Cursor nor xAI
  publishes a token rate for that usage row.
- Price Claude Haiku 5.5 from the published October 7, 2026 card. Prompts over
  100,000 tokens, counting cache writes, use the 5x rates.
- Project a run-dry pace on each capacity lane from the last hour of quota
  samples, using the last 24 hours when that hour is flat. Sort "what runs
  out first" by that time. Alert once per quota window when usage crosses the
  threshold and the pace runs dry before reset, through an in-app banner and
  one browser notification. Quiet hours, a 97% escalation, and per-subscription
  thresholds are configurable. Phone push is not included.

## [0.3.0-beta.6] - 2026-09-20

- Add dated published-rate aliases for current Cursor, OpenCode, ZCode, xAI and
  OpenAI model identifiers. Preserve explicit gaps where a provider uses
  time-of-day pricing that the current card format cannot represent faithfully.

## [0.3.0-beta.5] - 2026-09-10

- Add in-app Action Center v1 for proven quota thresholds, monitored-source
  failures and missing applicable model pricing. Persist acknowledgment, elapsed
  snooze, stable episodes, replay watermarks, compact preferences and bounded history.
- Show evaluator lag, ineligible evidence and distinct closure outcomes. Reuse
  existing diagnostics/actions; no external notifications, provider permission
  changes, price edits or automatic fixes. This feature candidate is not deployed.
- Share pure source-reason redaction between ingestion compatibility and the
  Plans & Value loader/report projections, preserving their distinct status and
  wording policies. Remove temporary period-helper fallbacks now that the helper
  ships in the required package; clarify the full suite's test boundaries.

## [0.3.0-beta.4] - 2026-09-09

- Scope collection evidence to relevant sources, preserve prior-success dates
  alongside failed attempts, and distinguish current freshness from historical
  capture. Explain safe source reasons and structured record exclusions.
- Fix row-level connection-help focus restoration, including hidden or replaced
  openers and parent-dialog reopen races, without discarding plan drafts.

- Add Plans & Value beside subscription management: calendar-prorated configured
  cost, recorded API-equivalent usage, qualified reference multiples, shared-tool
  grouping, unassigned usage, and separate pricing and collection evidence.
- Explain active intervals, cost formulas, missing prices and attribution limits;
  discard stale period responses and refresh comparisons after plan changes.
- Bundle optional dialogs together to retain the existing total asset budgets.
  Tool association is an assumption, not provider-account identity; actual charges
  are never added to reference value. This is a beta prerelease.

## [0.3.0-beta.3] - 2026-09-08

- Keep model-specific Codex quota pools separate from the main weekly capacity
  card; accept genuine zero/downward corrections without guessing legacy IDs.
- Preserve Codex source observation freshness independently of polling, hide
  expired/unverified quota values, and honor configured scope and managed
  usage-only/disabled bindings without changing measured usage history.

## [0.3.0-beta.2] - 2026-09-08

- Treat absent automatic harness defaults as not detected, while missing
  configured locations and real access/schema errors remain visible failures.
- Preserve OpenCode child-result counters, unpriced models and partial status
  across cumulative and mixed-format collection; restore optional ZCode project labels.
- Restore incremental Grok append reads and per-projection Traycer caching;
  correct duplicate-removal accounting and retain contradictory Codex totals
  as incomplete evidence rather than discarding them.

- Correct Codex request/counter identity transitions across files and replay;
  retain explicit-request equivalence and existing historical aliases.
- Persist OpenCode message start evidence for reverse cumulative reconciliation;
  defer ambiguous older checkpoints without advancing their progress.
- Merge Claude partial usage by field revision across files and persisted state,
  preserving omitted one-hour writes and exposing conflicting evidence.
- Conserve ZCode transcript totals; contradictory or missing component splits
  retain a trustworthy reported total as unclassified usage.

- Repair OpenCode v1/v2/legacy message selection, scoped cumulative remainder
  handling and unsupported-schema reporting without changing source event IDs.
- Support evidenced Codex CLI/Desktop roots and counter forms, Claude message
  revisions, Grok process/session metadata and native totals, and ZCode metadata
  alternatives. Preserve old IDs through exact alias records where provable.
- Add manual populated Antigravity cache and selected Cursor JSON export input;
  neither starts cache producers nor borrows their credentials.
- Bound admin reporting pagination, retain closed-hour/day reporting windows,
  revisit seven days for delayed reports, and honor longer quota Retry-After.
- Add an optional, explicitly enabled Claude status-line snapshot bridge using
  provider percentages only. Managed local connections remain usage-only.
- Retain third-party MIT notices and document ambiguous identity/coverage limits
  and the separately authorized release checks still required.

## [0.3.0-beta.1] - 2026-09-08

- Add an optional Connect harness wizard with bounded local discovery, manual
  verification, revisioned bindings, per-source retries and connection lifecycle.
- Resolve managed locations and credentials on collection cycles; preserve
  measured history when disabled and keep current-binding health separate.
- Add explicit documented provider API setup using Windows Credential Manager,
  with protected requests and no automatic native-credential borrowing.
- Redesign subscription management as a list and step wizard; add navbar and
  panel entry points and a read-only harness/integration status dialog.

## [0.2.0-beta.3] - 2026-09-07

- Fix summary crashes when a heatmap event has unavailable pricing. Unknown
  values are excluded from dollar totals; known values, including zero, remain.

## [0.2.0-beta.2] - 2026-09-07

- Separate usage value, configured plan accrual, and provider-reported charges.
- Preserve UTC ordering through schema 11 and improve malformed-record isolation.
- Require explicit consent for native credential usage and serve persisted limits on GET.
- Correct heatmap units, partial-value labels, and keyboard interactions.
- See [release notes and upgrade guidance](docs/releases/RELEASE-0.2.0-beta.2.md).

## [0.2.0-beta.1] - 2026-09-06

- Added effective-dated browser subscription management, historical correction previews,
  edit conflicts, idempotent retries and backup-gated schema 10 migration.
- Added optional onboarding and persisted actionable source diagnostics.
- Required browser/race execution, isolated wheel smoke and deterministic LF assets.
- Preserved request ownership, provider isolation and read-only doctor.

See [complete release notes](docs/releases/RELEASE-0.2.0.md).

## [0.1.2-beta.1] - 2026-09-06

- Fix stuck refresh state and overlapping detail/diagnostics polling.
- Guard request success, failure, cancellation, cleanup, and delayed scroll restoration.
- Keep selected-range validity independent of loading; recover through Retry.
- Preserve current navigation and range on Back/Home, with matching-cache reuse.
- Keep health requests independent of summary rendering and retain startup prefetch.
- Add deterministic browser regressions and helper-asset integrity checks.

See [hotfix patch notes](docs/releases/HOTFIX-0.1.2.md).

## [0.1.1-beta.1] - 2026-09-06

### Fixed

- Reject stale or mismatched time-range snapshots and show a loading chart until the selected range arrives.
- Render summaries without waiting for diagnostics; reuse startup prefetch and prevent overlapping automatic summary refreshes.
- Reuse historical event pricing with bounded caches that invalidate on event or pricing changes.
- Add the official GPT-6 Astra Standard API rate card and display name, including the long-context threshold.

This patch retains the public beta designation and does not change provider routing or database schema.

## [0.1.0-beta.1] - 2026-09-04

Initial public beta of BURNRATE: a local-first Windows dashboard for AI
coding-agent token usage, cache efficiency, subscription capacity, and
published-rate cost equivalents. BURNRATE reads harness-local files and
optional read-only provider APIs. It does not sit on the inference path, does
not require a BURNRATE account, and does not ship Grafana.

### Added

- Installable `burnrate` CLI on CPython 3.12 with `init`, `doctor`, `serve`,
  `subscription add`, and `subscription list`.
- FastAPI + SQLite dashboard bound to `127.0.0.1:17331` by default.
- Empty subscription list on a fresh database. Users add their own plan
  amounts; proration is not provider billing.
- Local, read-only adapters for Codex, Claude Code, OpenCode, and ZCode
  files/SQLite stores. Prompts and responses are not retained.
- Optional, opt-in provider admin and quota readers. Missing credentials skip
  with a named reason and never render as `$0`.
- Experimental adapters for undocumented interfaces, isolated so one failure
  cannot zero out other providers: Cursor usage service, Anthropic OAuth
  usage, Antigravity localhost RPC, Traycer readers, and Grok Build log
  scrape.
- Effective-dated `pricing/*.yaml` cards with `exact`, `derived`, unpriced,
  incomplete, and unavailable kept distinct.
- System/UI font stack. Google Fonts are not bundled and are not loaded from a
  CDN.
- Windows helpers that start, stop, status, install, and uninstall the
  dashboard only. Uninstall does not delete the user database unless
  `-PurgeData` is passed.
- Community files: `LICENSE` (Apache-2.0), `NOTICE`, `SECURITY.md`,
  `CONTRIBUTING.md`, and `CODE_OF_CONDUCT.md`.

### Security

- Default listener is localhost only.
- Credentials are not logged, not returned by the API, and not copied into
  SQLite.
- Claude OAuth credential-file refresh is off unless an explicit opt-in
  environment variable is set.
- Report vulnerabilities privately; see `SECURITY.md`. Do not file public
  issues with secrets.

### Notes

- This is an unreleased public beta. Experimental provider interfaces may
  change without a major version bump.
- Grafana, Prometheus, and the OpenTelemetry Collector are not part of
  BURNRATE and are not distributed with this beta.
- Licensed under Apache-2.0.
