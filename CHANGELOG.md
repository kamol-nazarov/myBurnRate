# Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and this project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

This project is licensed under Apache-2.0.

## [0.6.2] - 2026-11-18

### Added

- **Manage plans from the desktop.** Add plans, schedule price changes, record the last
  active day, and preview and confirm corrections from **Settings → Plans**.

### Fixed

- **The desktop Plans card lists active configured plans.** Disabled sources disappear from
  the desktop's current readings for that computer, and their recorded usage stays available.

## [0.6.1] - 2026-11-18

### Fixed

- **Settings → Sources scrolls through the complete provider list and tool scan status.**
  The Settings header and tabs stay in view, and refreshing the scan keeps your place.

## [0.6.0] - 2026-11-17

### Added

- **A small always-on-top tab for glancing at your limits.** It is a frameless strip with no taskbar
  button that replaces the old in-page Mini mode. Open it with **Shrink to tab** in the full window
  (`#tabBtn`) or **Show tab** in the tray menu. Clicking a provider, the open button or **Open
  BURNRATE** in a hover card brings the full window back. It lives in `spend_app/tab_window.py` and
  `spend_app/tab_ui.html`.
- **Each provider shows the window that runs out first.** The tab picks the window that will dry up
  soonest at your current pace (or, with no pace forecast, the one with the least left) and shows
  its % left, a fuel bar and a short status such as `dry in ~1h` or `resets in 5d`. A prepaid or
  pay-as-you-go balance shows its dollar figure. Usage-only feeds, which have no limit or balance to
  watch, are left off.
- **Colour says how urgent it is.** Red means about to run out: dry within about 2 hours at your
  current pace, 10% or less left, or a balance under $1. Amber means the window will run dry before
  it refills, or a balance is under $5. The badge on the brand icon counts the providers about to
  run out.
- **Real provider logos and profile names.** The tab reuses the full window's logo sprite and brand
  colours, so the two always match. A provider with several profiles gets one segment per profile,
  labelled like `Primary · 5-hour` or `Work · weekly`.
- **A hover card with every window.** Hover a segment, or focus it with the keyboard, to see each
  window with its % left, % used, reset time and a pace line such as "At this pace: dry in ~1h, 1h
  before it resets", plus an **Open BURNRATE** button. The window is clipped to the strip plus the
  card, so the rest of the screen stays clickable.
- **It fits the monitor it is on.** The tab shows up to five providers and folds calm ones into
  **+N fine**; providers that need attention are never folded just to make room. If the monitor's
  usable width cannot hold all of them, the rest fold into **+N to watch** (amber) or **+N urgent**
  (red). It is planned against that width at any
  display scaling, with compact fallbacks: the error chip shortens to its code, secondary labels
  drop, the controls tighten, and large balances are abbreviated (`$2.0K`) while the exact amount
  stays in the tooltip and the hover card.
- **Drag it from anywhere.** Press and move more than about 4 px to drag; a click without movement
  still opens the full window. The position is remembered (as `tabWindow`, separate from the full
  window) and kept on screen across monitor and display-scaling changes.
- **The tab follows Auto-fade.** With **Transparent when not hovered** on, it fades to your idle
  opacity about 1.5 seconds after the pointer leaves, and stays solid while the pointer is over the
  strip or its card, or keyboard focus is inside. With that setting off, it never fades.
- **Every state is covered.** Loading shows the brand mark and "Loading…" with no placeholder
  values. With no sources or limits it reads "No limits yet · Open BURNRATE to add a source". After
  a failed refresh the last values are dimmed and a chip shows the plain message with its code, for
  example "Can't reach your hub · NET-001". A provider with no reading yet is muted and says
  "Awaiting reading" (or a short reason such as "No key").
- **Keyboard accessible.** Segments, the overflow pills and the card's button are all reachable with
  Tab, focus opens the hover card and **Esc** closes it. Focus stays on the same provider or action
  when the tab refreshes.
- **The app remembers which window you left open.** The last surface (the tab or the full window)
  is saved as `surface` and restored on launch. A second launch asks the running app, through a
  named event (`spend_app/native_activation.py`), to open its full window. Switching is
  transactional: the tab appears first and the full window hides only once the tab is really
  showing, so a failure leaves the full window up rather than no window at all.
- **Limits first, with a refill timeline for every window.** The full window groups providers most
  urgent first and tags them **About to run out**, **Dry before refill** or **Out of quota**. Each
  window gets a timeline from now to its reset: usable time at your pace, the stretch where it is
  dry before refilling, and a refill marker, with a note like "Dry in ~1h · refills in 2h". A window
  with no pace forecast shows a soft level bar of what is left ("Refills in 3h"), and an exhausted
  one reads "Out · refills in 2h". The time ruler and its legend appear only while at least one lane
  is drawn on the time axis. Balances show the amount, "balance left" and the runway.
- **Pace and urgency data, computed once.** `merge_provider_entries` now adds a `pace` object to
  every window (`state`, `hoursToDry`, `hoursDryBeforeReset`, `runsDryAt`; `null` when there is no
  fresh forecast) and `urgency`, `profile`, `fundsRunwayInfo` and `displayRank` to every provider.
  `spend_app/provider_urgency.py` holds the one rule. The tab and the full window use the same
  Python-computed order (`displayRank`), and the page has no JavaScript copy of the rule. Profiles
  of one provider sit together only when they are exactly tied. The existing `forecast` and
  `runway` text lines are unchanged.
- **A desktop log.** The desktop app now writes
  `%LOCALAPPDATA%\BURNRATE\logs\desktop.log` with its warnings and window transitions (such as
  Shrink to tab), rotating at 512 KB with three backups and redacted like the other logs. It is not
  written in `--demo` or `--smoke-test` runs.

### Changed

- **The full window is 440 × 640.** It opens at 440 × 640 (it was 380 × 556) with a minimum of
  380 × 520 (it was 336 × 440), set by `DEFAULT_SIZE` and `MIN_SIZE` in
  `spend_app/native_geometry.py`. Today, Running now, cost by model with its drilldown, token mix,
  the activity heatmap and plans are all still there, restyled for the new size and the limits-first
  layout. Plans show prices such as `$200/mo` from the analytics summary's subscriptions.
- **Window actions moved to the footer.** The footer holds Diagnostics, Share week, **Keep on top**
  (moved out of the title bar), the density toggle and Settings. The title bar keeps the
  live/Sample/Offline pill, the computer selector, refresh, **Shrink to tab** and close.
- **Clearer full-window states.** When no source has reported yet, the limits card says "Waiting for
  the first readings" with an **Add a source** link. A failed refresh shows a banner with the plain
  message and its error code, keeps the last values and marks them stale. An unavailable provider is
  a muted lane that says why.
- **Urgency order, unless you chose your own.** The full window sorts most urgent first. A saved
  drag-to-sort order still wins there (the hint reads "your order"); the tab always uses urgency
  order.
- **The tab never refreshes on its own.** It shows what the full window last fetched and updates
  when that changes, so the two agree and the hub gets no extra requests.
- **Tray menu.** **Show BURNRATE** is now **Open BURNRATE**, and there is a new **Show tab** item.
  Clicking the tray icon hides whichever window is up, or brings back the one you used last.
- **The sample data shows the new design.** `burnrate desktop --demo` now has Claude Code about to
  run out (54% left on its 5-hour window), Codex and OpenCode Go drying before they refill, Grok
  Build healthy and OpenRouter on a $23.40 balance.
- **`burnrate doctor` checks the tab page.** Its `desktop_assets` check now also looks for
  `tab_ui.html`.

### Fixed

- **The window opens at its true default size.** The frameless window used to come up a little
  smaller than its default size when nothing had been saved. It is now sized before it is shown.

### Removed

- **The in-page Mini mode.** The mini bar and the Full/Mini toggle are gone from the full window,
  replaced by the tab. An old saved `hudMini` preference moves to the tab surface and is removed.
  The bridge method `set_hud_mini` stays as a no-op so an old page can't fail.

### Upgrade notes

- **The window opens at the new size unless you saved one.** A saved size is kept, except that
  anything below the new 380 × 520 minimum is raised to it.
- **If you left the app in Mini mode, it opens as the tab.** The old `hudMini` preference becomes
  the tab surface on first start. Otherwise it opens as the full window; use **Shrink to tab** once
  and the app remembers.
- **A saved provider order keeps overriding urgency order in the full window.** Dragging providers
  defines your own order. To go back to most urgent first, use **Reset** under Settings → Custom
  provider order (it shows only while a custom order is saved). The tab is not affected: it always
  sorts by urgency.
- **No hub or protocol change.** The protocol version stays at 2, the same as 0.5.9, and no hub
  code, setting or database migration changed. A 0.6.0 desktop works with a 0.5.9 hub.

## [0.5.9] - 2026-11-17

### Added

- **One hub account per hub, email and password only.** Your hub account is your email address and a
  password of at least 12 characters. The first account can register without a setup token, and
  registration then closes for good (`AUTH-103`), so nobody else can claim your hub. You can change
  your password from the desktop app (it needs the current one and signs your other sessions out),
  and a forgotten password is reset on the hub's own machine with
  `burnrate hub reset-password <email>`. Account creation, sign-in, password changes and computer
  removal are written to the hub's audit log. `BURNRATE_SETUP_TOKEN` and
  `BURNRATE_ALLOW_REGISTRATION` are no longer used.
- **Each computer registers once.** Every install creates its own device id, so signing in again on
  the same computer reconnects the same entry (with a fresh machine token) instead of adding a
  duplicate. A name already used by a different computer is refused (`DEV-302`). Machine tokens
  refresh themselves before they expire.
- **The hub address is built into the app.** Release builds embed it from
  `BURNRATE_BUILTIN_HUB_URL` at build time (`https://` only; the build fails otherwise) through the
  generated `spend_app/_build_config.py`. For testing, `BURNRATE_HUB_URL` in the environment or in
  `%LOCALAPPDATA%\BURNRATE\.env` overrides it. The address is never shown or asked for in the app,
  and it is redacted from API responses, `syncCommand`, logs, error messages and the diagnostics
  copy. A hub address already saved in an older install moves to the resolved address on start.
- **Plain-English errors with a code.** Sign-in and sync failures now read like "That email and
  password don't match. Error AUTH-101" instead of raw server text. The codes live in
  `spend_app/error_codes.py` and fall into seven families: `NET` (can't reach the hub), `AUTH`
  (sign-in), `DEV` (this computer), `SYNC`, `HUB`, `APP` and `REQ`. `/api/hub/info` and the
  `X-BURNRATE-Protocol` header report protocol version 2, so an app and hub that no longer match
  say so (`HUB-502` or `APP-902`) instead of failing in odd ways.
- **Hub exposure guard.** In hub mode the hub refuses to start on a public or wildcard address
  unless you set `BURNRATE_HUB_ALLOW_PUBLIC=1`. Loopback, private (RFC 1918) and Tailscale
  addresses are fine. The hub trusts `X-Forwarded-For` and `X-Forwarded-Proto` only from the
  proxies listed in `BURNRATE_TRUSTED_PROXIES` (default loopback), and sign-in and registration are
  throttled per real client instead of one shared bucket behind the proxy.
- **Hub account screen in the desktop app.** Settings → Hub account lets you sign in or create the
  account, see your Computers with **Remove**, **Change password**, and **Sign out**. Removing a
  computer revokes its machine sync token only; any existing owner session on that device stays
  valid until signed out there or revoked by changing your password. Signing in
  returns as soon as this computer is registered; the first sync runs in the background, and if it
  fails you see `SYNC-201` and it retries by itself.
- **Credentials live only in your OS keychain.** The owner session and machine token are no longer
  kept in the SQLite database. Copies from older installs move to the keychain on first start
  (and are removed from the database only once the keychain read-back matches). If the keychain
  can't save a sign-in, you get `APP-903` instead of a silent fallback.
- **Dashboard features now in the desktop app.** Everything from the old web dashboard that you
  asked to keep:
  - **Analytics.** Time ranges from 15m to YTD and a tool filter, token-activity charts
    (stacked or cumulative), a weekday-by-hour heatmap, the "What a million costs" ladder, cost by
    model with a per-model and per-tool drilldown, "Where money leaks", forecast against plans, and
    a pricing-coverage banner for unpriced models.
  - **Running now and Diagnostics.** A live list of active agent sessions, and a Diagnostics view
    with per-computer freshness, ingest passes, quota pollers, unpriced models and
    provider-versus-computed variance.
  - **Compact mode extras.** A mini bar, a compact or comfortable density toggle, a weekly share
    card you can copy as an image, and a one-click quick plan suggestion.
  - **Setup and settings.** A full setup scan across every tool, with the experimental quota
    consent switches for Z.AI and OpenCode Go, per-kind notification toggles with **Send a test**,
    **Copy diagnostics**, and **Sync now**.

### Changed

- **Desktop commands replace the browser.** `burnrate demo` no longer opens a browser (use
  `burnrate desktop --demo` for the desktop app), and `burnrate serve` and `demo` now run only the
  JSON API. `burnrate doctor` checks the packaged `desktop_assets`.
- **Hub setup is leaner.** Signing in with your hub account is the only way to join a hub, and the
  hub must be reachable only over your tailnet or private network (see `docs/self-hosting.md`).
- **Upgrade migration drops `pairing_grants`.** Existing databases drop the obsolete
  `pairing_grants` table on startup (discarding any unused pairing codes). Computers that were
  already paired keep their machine tokens and keep syncing without signing in again.
- **Icons moved.** App icons now live in `assets/icons/` (they were in `src-tauri/icons/`).

### Security

- **Public hubs need a deliberate choice.** The bind guard (`BURNRATE_HUB_ALLOW_PUBLIC=1`), the
  trusted-proxy rule and per-client throttling above keep an open first-registration hub from being
  exposed or brute-forced by accident. Deploy it on your tailnet and create the account right
  away.
- **Credentials leave the database.** Owner sessions and machine tokens sit in the OS keychain
  only, not in SQLite. After an upgrade moves old copies to the keychain, the database is
  securely vacuumed (retried until it completes) and the moved credentials are rotated on the
  next hub contact, so a copy of the old database file can't be used to sign in.
- **Signing out revokes on the hub, not just locally.** **Sign out** clears this computer first,
  then revokes its owner session and machine token on the hub. A computer can revoke its own
  machine token (`POST /api/machines/self/revoke`) when no owner session is available. If the hub
  can't be reached, the revocations wait in the keychain and run on the next hub contact.
  Credentials minted by a sign-in or rotation that lost a race are revoked the same way.
- **Password and email changes share the sign-in guess limit.** While sign-in is locked out
  (`AUTH-102`), changing the password or email is refused without checking the current password.
- **Compose refuses public addresses.** The hub Compose file runs a `publish-check` step before
  Caddy and the hub start, and refuses wildcard, public or hostname addresses unless
  `BURNRATE_HUB_ALLOW_PUBLIC=1`.
- **Fewer ways in from the app window.** The legacy owner-session bridge methods and the
  `/api/setup/owner-session` and `/api/setup/rotate-machine` routes are gone; the desktop app's
  callable methods are fixed by a test.
- **No hub address in the app's output.** The built-in address is redacted from the UI, API,
  logs and diagnostics.
- **Errors never carry server text.** The desktop app shows fixed messages and codes, not raw
  responses from the hub.

### Removed

- **Browser HTML pages and asset pipeline removed.** Removed `/`, `/hud`, `/setup`, `/auth`,
  `/connections`, `spend_app/auth_page.py`, `spend_app/hud.py`, `spend_web/` (including
  `starting.html`), `frontend_src/`, and browser asset-build/preview scripts. All non-API paths
  now return `404` JSON (`REQ-404`), while `/api/*`, `/healthz`, and the native desktop window
  (`spend_app/wealthfront_ui.html`) remain intact; spoke owner sessions are held by the desktop
  bridge instead of the removed `burnrate_local` cookie.
- **Features that left with the pages.** The attention incidents panel, Repos today, the plan
  manager screens, the live-push (SSE) consumer, probe mode, the standalone/hub role switcher with
  LAN addresses, the Plans & Balances setup step, and the HUD preview cards are gone and have no
  replacement.
- **Pairing codes and pairing routes removed.** Computers now join the hub solely by signing in
  to your hub account. Removed `spend_app/pairing.py` (keeping `hub_origin` in
  `spend_app/hub_origin.py`), `/api/pairing/redeem`, `/api/setup/pair-hub`,
  `/api/setup/connect-spoke`, `PAIRING_PREFIX`, `encode_pairing_code`, `decode_pairing_code`,
  `ensure_tailscale_hub_proxy`, `detect_network_addresses`, `PAIRING_MINUTES`, desktop pairing UI
  and bridge methods (`create_pairing_code`, `join_with_code`), and CLI `machines register`,
  `sync --hub --token`, and `desktop --hub --token`. Redaction keeps masking historical `brpair_`
  tokens in old logs.
- **The Tauri HUD is retired.** `src-tauri/` is gone, along with `.github/workflows/desktop.yml`
  and the Tauri bundle, `latest.json` and update-signing steps in the release workflow. The native
  installer and `native-latest.json` are unchanged.
- **Scoop and winget manifests are no longer published.** They pointed at the Tauri bundle;
  `packaging/render_manifests.py` is removed.

### Upgrade notes

- **Retire the old Tauri HUD and stop any old background service.** The 0.5.9 installer leaves the
  old Tauri HUD installation in place: quit the old Tauri HUD from the system tray and uninstall it
  from Windows **Apps & features**. Also stop any running older BURNRATE background service before
  starting 0.5.9 so the new protocol-v2 service can take over cleanly.
- **Already-paired computers keep syncing.** Their machine tokens and sync cursors are unchanged,
  with no need to sign in again. An older hub account whose name isn't an email address keeps
  working, and the desktop app asks you once to add an email.
- **Put the hub on your tailnet and create the account right after deploying.** The first
  registration needs no setup token, so anyone who can reach a new hub can claim it. See
  `docs/self-hosting.md`.
- **Hub env vars.** Remove `BURNRATE_SETUP_TOKEN` and `BURNRATE_ALLOW_REGISTRATION` from the hub's
  environment (they are ignored now). Behind Caddy or another proxy, set
  `BURNRATE_TRUSTED_PROXIES` to its address, and set `BURNRATE_HUB_ALLOW_PUBLIC=1` only if you
  really must bind a public address.
- **Anything that opened a page in a browser** (links to `/`, `/hud`, `/setup`, `/auth`) now gets
  a `404`. Use the desktop app, or `burnrate desktop --demo` for sample data.

## [0.5.8] - 2026-11-16

### Security

- **The source installer only starts a verified desktop app.** `install.ps1` no longer downloads
  `burnrate-desktop.exe` from the `origin` remote with only a size check. It installs through
  `install-native.ps1`, which checks the pinned URL, size, SHA-256 and minisign signature. Local
  `target` builds and the cargo fallback require `-Dev`. `install-native.ps1` now embeds its
  verifier and public key, so the `irm | iex` one-liner never fetches a verifier.
- **Native HUD launch trusts a pinned certificate, not a subject name.** A signed file counts as
  BURNRATE only when its thumbprint is pinned (`BURNRATE_NATIVE_THUMBPRINTS`); any other SignPath
  signature is untrusted. A failed Authenticode check (`UnknownError`) is always refused.
  `/api/hud/native` launches only the installed app (plus dev builds with `BURNRATE_DEV=1`) and
  returns reason codes, not paths or exception text.
- **Login, registration and pairing redemption require a same-origin JSON request.** These routes
  no longer skip the cross-site check, and request bodies need `Content-Type: application/json` and
  `X-BURNRATE-Request`, so a cross-site form post cannot create the first owner.
- **Session secrets stay out of URLs and response bodies.** A break-glass `?token=` link redirects to
  the same page without the token and reuses an existing session. Session tokens are delivered only
  as the HttpOnly cookie, and login/registration no longer return machine tokens.
- **Hubs must use HTTPS** (loopback excepted). Every setup, pairing, sync and relay path checks the
  hub address before sending credentials; a saved `http://` hub fails closed.
- **The first-owner setup token is hardened.** Comparison is by digest, failures count toward the
  login throttle, and a hub refuses to start with a `BURNRATE_SETUP_TOKEN` under 32 characters.
- **Stored errors no longer carry raw exception text.** Collection errors use fixed messages by
  default, including the default local collection and the scheduler. One shared redactor now
  covers every stored error, source reason and diagnostics copy. Existing error rows and saved
  spoke sync errors are scrubbed once on upgrade.
- **Pairing and machine identity.** A name-only pairing code binds only when exactly one active
  machine has that name, can no longer revive a revoked machine, and is cancelled when that machine
  is revoked. Machine names stay unique, and pending codes from before this release are expired.
- **Legacy machine tokens marked for rotation are refused after a 14-day grace period.**
- **The spoke HUD stops showing a hub view after the hub rejects its session,** and never serves a
  cached owner view older than its limit.
- **Smaller attack surface.** `/docs`, `/redoc` and `/openapi.json` are disabled; unexpected
  `ValueError`s return a fixed `invalid_request`; the CSP adds `base-uri 'none'` and
  `object-src 'none'`; web pages can no longer call the desktop update-install command.
- **Local hardening.** The update client ignores proxy environment variables; the NSIS hook calls
  `taskkill` from System32; a failed junction check is treated as a link; agent-run names are
  capped at 120 characters; `burnrate doctor` prints home-relative paths.

### Added

- **Several profiles per local tool.** Claude Code, Codex, OpenCode, ZCode, Grok Build,
  Antigravity and Muse can each track more than one profile folder. Every profile's usage is kept
  under its own card, and profiles signed in to the same account share one limit reading instead
  of being counted twice. Traycer and API-key providers stay single-profile.
- **Profile auto-detection.** BURNRATE finds Claude Code (`~/.claude-*`), Codex (`~/.codex-*`)
  and OpenCode (`~/.opencode-*`) profile folders, plus folders listed in
  `BURNRATE_CLAUDE_ACCOUNT_DIRS`, `BURNRATE_CODEX_PROFILE_DIRS` and
  `BURNRATE_OPENCODE_PROFILE_DIRS`. Each tool's default folder is tracked automatically; other
  folders are shown as **detected** and are read only after you add them. Detection reads each
  folder's sign-in file only, never transcripts, and stores account ids as SHA-256 digests.
- **Usage limits per signed-in profile**, including Codex: each extra Codex profile gets its own
  weekly window.
- **Grouped sources in Settings.** Sources are grouped by provider, with **+ Add profile**,
  **Add** and **Dismiss** for detected folders, and **Rename** and **Remove** (**Stop tracking**
  for the default) on each profile.

### Changed

- The HUD no longer shows an "update available" button. Install updates from the tray menu or
  Settings.
- `BURNRATE_NATIVE_PUBLISHERS` is replaced by `BURNRATE_NATIVE_THUMBPRINTS`.
- The admin audit table and its triggers are recreated on start if missing.
- "Claude Code (second instance)" is replaced by profiles. A configured second instance becomes a
  Claude Code profile; usage it recorded before the upgrade stays with the main Claude Code card,
  and only new usage is kept separate.
- Source status reflects what is actually found: `receiving`, `added`, `detected`, `not found`,
  `disabled` or `error`, replacing "already monitored".
- Connection settings move to `connections.v2`. The previous `connections.v1` is left in place,
  including its credential references, so an older binary can still read it after a rollback.
- If a profile's approved folder is moved or replaced by a junction, BURNRATE stops reading it and
  marks it as needing attention until you reconnect it.

## [0.5.7] - 2026-11-14

### Security

- **Paired-computer session management now requires a local sign-in.** The spoke's loopback API
  previously relayed hub session listing, revocation, rotation and sign-out using the stored owner
  session without checking the caller. A new `burnrate_local` HttpOnly cookie, issued when the owner
  signs in on the spoke, gates those routes; break-glass rotation is refused on a spoke and must be
  done on the hub. Anonymous local requests now get 401.
- **The static bearer is restricted by default.** `BURNRATE_ALLOW_ADMIN_BEARER` now defaults to `0`,
  so `BURNRATE_ACCESS_TOKEN` only works on `/api/auth/bootstrap` and `/api/auth/rotate` unless
  automation explicitly opts in with `1`.
- **Desktop launch checks resist local tampering.** PowerShell is resolved by absolute system path
  instead of `PATH`/CWD, and the binary is held without write or delete sharing across the
  signature check and launch.
- **Machine registration no longer prints a token on a command line.** The suggested sync command
  uses `--token-file`; the token is still shown once in the response.
- **Hub shell pages redirect anonymous visitors to `/auth`,** and `/healthz?supervisor=1` is
  owner-only on a hub. Liveness stays public for the watchdog and `burnrate hub update`.

### Fixed

- `burnrate hub update` checks public service health after restart and reads the installed
  build over SSH, avoiding a false update failure from the protected `/api/spend` endpoint.

## [0.5.6] - 2026-11-14

### Added

- OpenCode provider rows: Isoquant and other custom providers now have separate usage rows,
  alongside the existing Z.AI and OpenCode Go plan rows. Historical message events are reassigned
  in place; ambiguous legacy provider attribution stays explicit, and unpriced tokens remain counted.
  Provider rows have stable chart colors, retain Grok route evidence, and preserve earlier attribution
  when a session switches provider. The cumulative-history backfill runs once.
  Legacy Go totals without matching message evidence stay on a recorded-usage row; matching messages
  refine them onto the source profile's plan. Provider switches before upgrade retain the stored
  historical provider.

### Security

- **Authorization model replaced (V2-M01, V2-M02, V2-L05).** Explicit owner/machine/anonymous
  principals; isolated own-machine usage reads; hashed 90-day machine tokens and one-time refresh;
  hashed 30-day owner sessions with HttpOnly SameSite=Lax cookies, device labels, revocation and
  credential controls at `/auth`; 15-minute, Host-bound, one-use pairing invitations; append-only
  admin audit. Spokes use a separate owner session for All computers and show scoped usage when
  owner access ends. Legacy machine records migrate once to creation + 180-day expiry and require
  rotation. Existing owners keep their passwords without repeating the setup-token step.
- **Static bearer deprecated for the UI.** `BURNRATE_ACCESS_TOKEN` remains break-glass and explicitly
  configured legacy automation; `BURNRATE_ALLOW_ADMIN_BEARER=0` limits it to bootstrap/rotation.
  Credential rotation persists a replacement hash, so the old environment value cannot revive.

- **Hub authentication now fails closed.** With `BURNRATE_CENTRAL_SERVER=1`,
  every API read and write requires a valid principal (owner session, explicitly configured
  bearer automation, or a scoped machine token). The Docker reference deployment requires the token,
  sets `BURNRATE_ALLOWED_HOSTS`/`BURNRATE_ALLOWED_ORIGINS`, and documents the
  owner session flow.
- **First-account takeover fixed.** Creating the first account on a hub also
  requires `BURNRATE_SETUP_TOKEN`; registration and login are rate-limited and
  answer unknown users and wrong passwords identically. Passwords now use
  PBKDF2-SHA256 at 600,000 iterations with transparent migration from 120,000.
- **Reads are Host-checked.** An exact Host allowlist now covers GETs and pages,
  closing the DNS-rebinding read path; test hostnames are accepted only under
  pytest.
- **Bearer handling fixed.** Any `Authorization: Bearer` value no longer skips
  the origin/CSRF guard; only an authenticated admin or machine token does.
- **Supply chain.** GitHub Actions are pinned to commit SHAs, the Tauri CLI and
  PyInstaller are pinned, Python dependencies install from hash-locked exports
  of `uv.lock`, and CI adds `cargo audit`/`npm audit` with Cargo and npm
  Dependabot coverage.
- **Installer verification.** The one-line installer now verifies the
  installer's Ed25519 minisign signature against the pinned update key in
  addition to its SHA-256.
- **Desktop and tokens.** The Tauri shell has a concrete CSP and grants commands
  only on port 17331; sync tokens can come from a file or environment instead of
  a command line. POSIX data, `.env` and database files are private, and
  `br_m_`/`brpair_` values are redacted from copied diagnostics.
- **Low-finding hardening.** `/api/hud/native` verifies the desktop binary's
  Authenticode signature and publisher before launching it, and
  `BURNRATE_REQUIRE_SIGNED_NATIVE=1` refuses unsigned builds. The reference
  compose file pins the hub and Caddy images by digest, the weekly price job
  installs from the hash-locked export, and a hub that already has an owner
  warns when `BURNRATE_SETUP_TOKEN` is still set.

### Changed

- Rewrote the README for the 0.5.5 native app: public installers, current
  provider coverage, the exact/derived/unavailable accuracy model, privacy
  guarantees and supply-chain verification.
- Replaced the beta-era security policy with a supported-version table, private
  advisory intake, response targets, scope and the invariants the product
  relies on.
- Modernized `CONTRIBUTING.md` for the native window, the three-OS CI matrix and
  the UI/layout guardrails, aligned the Code of Conduct's enforcement channel,
  and completed the Apache-2.0 appendix copyright line.

## [0.5.5] - 2026-11-14

### Added

- `scripts/install-native.ps1`: a one-line per-user Windows PowerShell installer
  (`irm https://raw.githubusercontent.com/kamol-nazarov/myBurnRate/prod/scripts/install-native.ps1 | iex`)
  that fetches `native-latest.json` from the latest GitHub Release, validates the
  release URL, byte size and SHA-256 digest before running anything, installs into
  `%LOCALAPPDATA%\Programs\BURNRATE`, and launches `myBurnRate`.

### Changed

- Redesigned the native desktop window for higher density and calmer scanning:
  a compact split hero pairing the API-equivalent figure with an inline interval
  timeline, an integrated three-column summary strip (`Burn rate`, `Cache hit`,
  `Value vs plans`), a tactile segmented period control (`Today`, `7 days`,
  `Month`, `All`), a compact computer-scope selector, and a two-column provider
  tile grid that expands full-width for window meters and an aligned two-column
  token breakdown ledger (`Alt+Arrow` and 2D pointer drag-and-drop supported,
  stacking cleanly to one column at narrow widths).
- Switched the native desktop window's offline embedded variable font to
  **Manrope** (`spend_app/fonts/manrope-latin.woff2`, SIL OFL 1.1).
- Updated `README.md` and `docs/native-app.md` with direct public release
  install instructions and screenshots for the published native installer.

### Fixed

- Kept the computer-scope popover inside narrow viewports and ensured hero,
  timeline, and summary metrics distinguish measured zero from missing history.
- Included `src-tauri/icons` in `.dockerignore` and `Dockerfile` so the
  multi-arch `ghcr.io/kamol-nazarov/myburnrate-hub` container image builds
  cleanly on release tags.

## [0.5.4] - 2026-11-13

### Added

- A native desktop usage window for Windows. It shows API-equivalent value and
  recorded tokens for today, the last 7 days, this month or all time, with a
  provider logo, a drawer of limits and token detail per tool, a chart, and a
  filter for each paired computer. It is frameless, with its own minimize and
  close buttons and resize edges, and uses the embedded Plus Jakarta Sans font.
  Source connections and computer pairing are in its Settings. A portable
  Windows build and CI smoke checks exercise both the interface and collector.
- Remaining usage, not just usage. Each row shows what is left of a plan window
  or prepaid balance. Several accounts of one tool (for example two Claude Code
  profiles) are separate rows with their own tag. Hovering a row shows that
  day's token burn, rows can be dragged into your own order, and the window can
  fade when the pointer leaves it, with pinning on top kept independent of the
  fade.
- Sign in to a hub from Settings -> Computers: enter the hub address, your
  account and this computer's name, and the computer joins "All computers"
  without a pairing code.
- Dark and System themes for the native window, a guided first-run screen that
  finds local tools and takes an API key in place, keyboard navigation with
  focus rings and Alt+Up/Down row reordering, and reduced-motion support.
- The native window remembers its position and size, and closing it keeps
  BURNRATE in the notification area, with Show, Refresh, Settings and Quit on
  the tray icon. The tray warns when a prepaid balance runs low; plan-limit
  alerts still come from the background service, and one Settings switch
  controls both.
- A per-user Windows installer for the native app (`myBurnRate_<version>_x64-setup.exe`)
  with in-app updates: an installed copy checks `native-latest.json` on GitHub
  once after it starts and then daily, and installs only after you click, once the
  download's size, SHA-256, update signature and (for a signed app) publisher
  match. Portable and source copies never check on their own, and
  `BURNRATE_NO_UPDATE_CHECK=1` turns every check off. See the privacy notes.
- Optional Authenticode signing for local native builds through `BURNRATE_SIGN_*`
  environment variables, and a native release job that signs the installer through
  SignPath when it is configured.
- Gemini 4 Carbon (Antigravity, every effort level) is priced: $2.00 input,
  $0.10 cached input and $10.00 output per million tokens until 1 December 2026,
  then $4.00, $0.20 and $20.00.

### Changed

- The hub bounds its expensive reads. The summary and HUD endpoints run on one
  dedicated worker and answer 503 with `Retry-After` when it is busy, so health
  checks stay responsive while a large read is running. `burnrate hub update`
  now needs a clean checkout, installs the Python dependencies, checks the
  installed certificate bundle and runs a host security gate before it restarts
  the service. Keep `BURNRATE_SUMMARY_PREWARM=0` on the central hub.
- Tauri command permissions, an app-wide CSRF and DNS-rebinding guard, a single
  rotating service log, SQLite retention pruning, and a lighter start (the HUD
  starts alone and the main window loads only when opened) from the security
  and resource audit.
- The native app uses distinct marks for ZCode and Antigravity.
- Python installs and source distributions now include the native app's icons.

### Fixed

- Reject invalid and revoked machine bearer tokens, preserve pairing identity
  during offline fallback, and retry source changes with the same mutation ID
  after a lost response.
- Keep imported usage corrections visible in cached summaries without rescanning
  settled history after unrelated recent activity or empty synchronization runs.
- Preserve hub errors when changing plans from a paired computer. Correct plan
  boundaries, quarterly costs, telemetry timestamps, and invalid pricing inputs.
- A paired computer can edit and add plans that live only on the hub, forwards
  the hub's rejection or conflict instead of saving locally, and fails an
  offline save without writing anything. A correction preview comes from the hub.
- A quarterly plan accrues its exact calendar quarter, and the preview of a
  plan term that has not ended respects the plan's end date.
- Timestamps outside the valid range are quarantined, telemetry without a time
  zone is read as UTC, price cards with non-finite rates are rejected, an
  explicit free one-hour cache-write rate is respected, and negative usage is
  quarantined without losing the valid rows beside it.
- An ingestion source that keeps failing no longer loses the record of its last
  success to retention pruning. A settled model correction moves tokens to the
  right meter in the HUD.
- Muse tokens are no longer counted twice, and an idle Muse weekly window is
  informational until Muse is used again.
- On Windows the single-instance lock is scoped to each desktop, and clicking
  the shortcut brings an open BURNRATE window forward.
- The native window keeps "unavailable" when it is resized, waits for a cold
  full-history read before declaring a hub unavailable, and no longer lets
  Windows repaint a caption over the frameless window.

## [0.5.3] - 2026-10-28

### Fixed

- Grok calls keep their model when the Grok CLI trims its log. The CLI cuts
  the head of `unified.jsonl`, and the next pass read the remaining calls
  again without their "model changed" line: calls already priced as
  `grok-4.7-build-fast` moved to unknown, and the HUD showed the day as a
  floor with "2.19M tokens have no published price (unknown)". A call read
  again without a model keeps the one it was stored with. A call with no
  model in a session with no completed turn takes the one model its
  `updates.jsonl` records (`_meta.modelId` on each prompt), which also
  prices calls already stored as unknown.

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
- On Windows, the service no longer starts a program to list processes. It
  found Antigravity's language server by running PowerShell on every
  collection pass, about every six seconds; it now reads the process list
  and command lines through the Windows API, in about 12 ms. The
  no-console-window test also covers helpers handed `run=subprocess.run`, and
  a new test fails if process discovery starts any program.

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
