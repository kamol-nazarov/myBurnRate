# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is licensed under Apache-2.0.

## [Unreleased]

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
- See [release notes and upgrade guidance](RELEASE-0.2.0-beta.2.md).

## [0.2.0-beta.1] - 2026-09-06

- Added effective-dated browser subscription management, historical correction previews,
  edit conflicts, idempotent retries and backup-gated schema 10 migration.
- Added optional onboarding and persisted actionable source diagnostics.
- Required browser/race execution, isolated wheel smoke and deterministic LF assets.
- Preserved request ownership, provider isolation and read-only doctor.

See [complete release notes](RELEASE-0.2.0.md).

## [0.1.2-beta.1] - 2026-09-06

- Fix stuck refresh state and overlapping detail/diagnostics polling.
- Guard request success, failure, cancellation, cleanup, and delayed scroll restoration.
- Keep selected-range validity independent of loading; recover through Retry.
- Preserve current navigation and range on Back/Home, with matching-cache reuse.
- Keep health requests independent of summary rendering and retain startup prefetch.
- Add deterministic browser regressions and helper-asset integrity checks.

See [hotfix patch notes](HOTFIX-0.1.2.md).

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
