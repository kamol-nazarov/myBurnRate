# BURNRATE 0.5.7

Security release for paired-computer session management and the hub-update
health check. It closes the High finding from the v4 audit and the four Low
findings closed in the same pass.

## Security

- **Paired computers gate hub session management.** A spoke's loopback API no
  longer relays hub session listing, revocation, rotation or sign-out without a
  local sign-in. Signing in on the spoke issues a separate HttpOnly
  `burnrate_local` cookie; break-glass rotation is refused on a spoke and must
  be done on the hub. Responses never carry `br_bg_` or `br_s_` credentials.
- **The static bearer is restricted by default.** `BURNRATE_ACCESS_TOKEN` now
  works only on `/api/auth/bootstrap` and `/api/auth/rotate` unless automation
  explicitly sets `BURNRATE_ALLOW_ADMIN_BEARER=1`. The native desktop app still
  bootstraps normally.
- **Desktop launch hardening.** The launcher resolves PowerShell by absolute
  system path instead of `PATH`/CWD, and holds the binary without write or
  delete sharing across the Authenticode check and launch.
- **No token on the command line.** Machine registration suggests
  `--token-file <path>`; the token is still shown once in the response.
- **Smaller hub surface.** Anonymous requests to `/`, `/hud` and `/setup`
  redirect to `/auth`, and `/healthz?supervisor=1` is owner-only. Plain
  `/healthz` stays public for the watchdog and `burnrate hub update`.
- **Reduced write contention.** `last_used_at` is written at most once per
  minute per credential, so authenticated reads do not take the SQLite writer
  on every request.
- **Hub update.** The updater checks public liveness and reads the installed
  build over the already-authorized SSH connection.

## Upgrade notes

1. **Back up the hub before upgrading.** Use SQLite's backup API while running,
   or stop the hub and copy the complete database/configuration state.
2. **Automation using the static bearer** beyond bootstrap/rotation must set
   `BURNRATE_ALLOW_ADMIN_BEARER=1`. Normal browser and native-app use does not
   need it.
3. **Paired computers:** open `/auth` and sign in once before managing hub
   sessions or signing out. The hub owner session itself is unchanged; machine
   tokens do not need re-pairing for this release.
4. **Rotate the break-glass credential on the hub**, never from a paired
   computer. The replacement is returned once; save it securely.
5. The 0.5.7 hub image digest is published after the tag build; the reference
   Compose file is pinned to that digest in a follow-up release-metadata commit.

See [upgrade and rollback](../self-hosting.md#auth-model-upgrade-and-rollback).
An old binary does not enforce the new restrictions.

## Install and update

- **Installed native Windows app:** Settings → General → Check for updates, then
  **Install & restart**. Installed copies detect updates automatically after
  launch and daily; installation requires this explicit action.
- **Python/source installation:** install the release wheel into the existing
  Python environment and restart the desktop/service processes. Preserve its
  data directory.
- **Sidecar HUD:** use its update bar or the tray's **Check for updates**.
- **Self-hosted hub:** update the checkout/service with `burnrate hub update`,
  or deploy the digest-pinned `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.7`
  image once its digest is published.

Release assets are built and update-signed on GitHub Actions from this tag, with
`SHA256SUMS`, `native-latest.json`, `latest.json` and build-provenance
attestations. Verify downloads with
`gh attestation verify <file> --repo kamol-nazarov/myBurnRate`.

## Known limits

The spoke stores its 30-day hub owner session in its private SQLite
configuration; protect the OS account and data directory. Loopback installs
remain anonymous for local data, login throttling is process-local, two
upstream Cargo warnings are allowed, the CSP permits inline scripts, and the
Antigravity loopback RPC skips certificate verification. These are accepted
limits documented in `SECURITY.md`.
