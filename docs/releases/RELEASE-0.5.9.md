# BURNRATE 0.5.9

Account-only multi-computer sync. Your hub is now one email-and-password
account, the hub address is built into the app, and the desktop app is the only
interface: every web page is gone, and the dashboard features you wanted to keep
moved into the app.

## Your hub account

- **Email and password, nothing else.** The first account registers without a
  setup token and registration then closes for good, so nobody else can claim
  your hub. Passwords are at least 12 characters.
- **Change or reset your password.** Settings → Hub account has **Change
  password**, which needs the current one and signs your other sessions out. If
  you forget it, run `burnrate hub reset-password <email>` on the hub's machine.
- **Each computer registers once.** Every install has its own device id, so
  signing in again reconnects the same computer instead of adding a duplicate.
  Machine tokens refresh themselves before they expire.
- **Settings → Hub account** shows your Computers with **Remove**, plus **Sign
  in**, **Create account**, **Change password** and **Sign out**. Signing in
  returns right away; the first sync runs in the background.
- **Stored in your keychain.** The owner session and machine token live only in
  the OS keychain, never in the database. Older installs move over on first start.

## A built-in hub address and clear errors

- **No address to type.** Release builds carry your hub address, and it never
  appears in the app, API, logs or diagnostics. For testing, set
  `BURNRATE_HUB_URL` in `%LOCALAPPDATA%\BURNRATE\.env`.
- **Plain English with a code.** Sign-in and sync problems read like "That email
  and password don't match. Error AUTH-101", never raw server text. If a sign-in
  works but the first sync doesn't, you see `SYNC-201` and BURNRATE retries. If
  the keychain can't save your sign-in you see `APP-903`.
- **Mismatched versions say so.** The app and hub now check a protocol version
  (2), so an out-of-date hub or app asks for an update instead of misbehaving.

## Hub safety

- The hub refuses to start on a public address unless
  `BURNRATE_HUB_ALLOW_PUBLIC=1`. Loopback, private and Tailscale addresses are
  fine.
- Behind a reverse proxy, it trusts `X-Forwarded-For` only from
  `BURNRATE_TRUSTED_PROXIES`, and sign-in is throttled per real client.

## Now in the desktop app

- **Analytics.** Ranges from 15 minutes to year-to-date, a tool filter, token
  charts (stacked or cumulative), a weekday-by-hour heatmap, cost by model with
  a drilldown, "Where money leaks", forecast against your plans and a coverage
  banner for unpriced models.
- **Running now and Diagnostics.** Live agent sessions, and a Diagnostics view
  with per-computer freshness, ingest, quota pollers and variance.
- **Compact mode.** A mini bar, density toggle, a copyable weekly share card and
  a one-click plan suggestion.
- **Setup and settings.** A full setup scan with quota consent switches,
  notification toggles with **Send a test**, **Copy diagnostics** and **Sync now**.

## Upgrading from 0.5.8

1. **Computers already paired keep syncing.** Their machine tokens are
   unchanged and nobody has to sign in again. An older account whose name isn't
   an email keeps working; the app asks once for an email.
2. **Keep the hub on your tailnet and create the account right after
   deploying.** First registration needs no setup token, so anyone who can reach
   a brand-new hub can claim it. See [self-hosting](../self-hosting.md).
3. **Hub environment.** Delete `BURNRATE_SETUP_TOKEN` and
   `BURNRATE_ALLOW_REGISTRATION`; they are ignored. Behind Caddy or another
   proxy, set `BURNRATE_TRUSTED_PROXIES`.
4. **Pairing codes are gone.** Startup drops the unused `pairing_grants` table.
5. **Browser links stop working.** `/`, `/hud`, `/setup` and `/auth` now return
   `404`. Use the desktop app, or `burnrate desktop --demo` for sample data.

## Removed

- **Every web page.** The browser dashboard, HUD, setup wizard, sign-in and
  connections pages are removed. The service now serves JSON only. The attention
  incidents panel, Repos today, the plan manager screens, live push (SSE), probe
  mode, the role switcher with LAN addresses, the Plans & Balances setup step
  and the HUD preview cards were dropped with them.
- **Pairing codes and routes**, including `burnrate machines register`,
  `sync --hub --token` and `desktop --hub --token`.
- **The Tauri HUD** (`src-tauri/`) and its release bundle. The native installer
  and its update feed are unchanged.
- **Scoop and winget manifests.** They are no longer published.
- **Browser auto-open** in `burnrate demo`.

See the [changelog](../../CHANGELOG.md) for file and route names.
