# BURNRATE v0.3.0-beta.19

Setup is rebuilt around the HUD in four steps. **This computer** picks just
this computer, joining your hub with a pairing code, or making this the hub.
**What we found** shows one row per HUD card with its usage, its meter, the fix,
and the three sign-in-reuse switches. **Plans & balances** suggests plans from
what the meters report, connects OpenRouter and DeepSeek keys, and fixes plans
that need a look in one click. **Check** previews the live HUD and lists
anything still needing you.

Restarts no longer re-read history. Read files are remembered across restarts,
so the first pass after a restart skips unchanged Claude, Codex and Muse files
(Claude: about six minutes down to under two seconds on a large history).
OpenCode skips unchanged sessions, and a session whose running total moves
re-reads only itself. Long first reads save in batches of 5,000 events instead
of holding the database for minutes. The usage de-duplication map moves out of
`app_meta` into a compact table in small background steps. A meter reading
that fails to save retries in a minute. In the desktop service, collection
runs in its own worker process.

New readings:

- Each extra OpenCode profile has its own OpenCode Go lane. Extra Claude
  accounts and OpenCode profiles record usage under their own card, so each
  card has its own burn rate and run-out forecast.
- A DeepSeek balance. OpenRouter explains a key that is not a management key.
- Balance runway: after a day of readings, OpenRouter and DeepSeek cards say
  how long the money lasts at the recent spend ("about 12 days at your current
  spend ($3.40/day)"). Top-ups are not spend.
- Muse Code says it is idle instead of looking broken. Antigravity says when
  the app is closed.

The desktop service now sends Windows notifications when a plan passes its
alert level (90% unless set otherwise), when a plan will run out before it
resets, when a meter has not updated for over an hour, or when hub sync keeps
failing. Each is sent once per window or stretch, and quiet hours hold them
back unless a run-out is under two hours away. Setup's Check step has a switch
for each and a test button. **Copy diagnostics** in the same step copies
versions, freshness, meters, recent failed runs and log lines, with keys,
e-mail addresses and the home folder removed.

Hubs: a hub that already has an account accepts no new ones unless
`BURNRATE_ALLOW_REGISTRATION=1`; computers join with pairing codes. There is
no built-in hub address. The hub check compares builds (version plus commit),
and `burnrate hub update` updates a self-hosted hub over ssh.

This is the first published release since beta.15. Beta.16 to beta.18 (HUD
window token burn, local-midnight "today", unconditional collection and
measured-burn forecasts) are included; see the changelog.

Verification for this release: the full suite passes (1,164 tests, a handful
skipped). The build ran live on a spoke and its hub before release. The spoke
went from 70–114 s collection passes to 19–25 s, and an extra OpenCode
profile's 3,859 events showed on their own card through the hub. A test
notification reached Windows' notification history, and the copied
diagnostics contained no key, e-mail address or home path.

Restart BURNRATE after installing so the service and native shell both pick
up this build.
