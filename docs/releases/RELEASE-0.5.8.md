# BURNRATE 0.5.8

Security release for the v0.5.7 audit. It closes the High finding (the source
installer started an unverified desktop app), the three Medium findings and the
Low findings, plus four issues found while verifying them.

## Security

- **Verified desktop installs only.** `install.ps1` installs the desktop app
  through `install-native.ps1`, which checks the pinned URL, size, SHA-256 and
  minisign signature. It no longer downloads an exe from the `origin` remote.
  Local `target` builds and the cargo fallback need `-Dev`. The installer embeds
  its verifier and public key, so the `irm | iex` one-liner fetches nothing
  else.
- **Pinned launch trust.** The HUD launcher treats a signed file as BURNRATE
  only when its certificate thumbprint is pinned. A failed signature check is
  always refused, `/api/hud/native` launches only the installed app, and its
  responses carry reason codes instead of paths or error text.
- **No cross-site sign-in.** Login, registration and pairing redemption require
  a same-origin JSON request with the `X-BURNRATE-Request` header, so a form
  post from another site cannot create the first owner.
- **Secrets stay out of URLs and bodies.** A break-glass `?token=` link
  redirects to a token-free URL. Sessions are delivered only as the HttpOnly
  cookie, and login/registration no longer return machine tokens.
- **HTTPS hubs.** Every setup, pairing, sync and relay path rejects a
  non-loopback `http://` hub before sending anything.
- **Hardened setup token.** Digest comparison, throttled failures, and a hub
  that refuses to start with a `BURNRATE_SETUP_TOKEN` under 32 characters.
- **Clean error storage.** Collection errors use fixed messages by default. One
  shared redactor covers stored errors, source reasons and the diagnostics copy,
  and existing error rows and saved spoke sync errors are scrubbed on upgrade.
- **Safer pairing.** Name-only codes bind only to a single active machine,
  cannot revive a revoked machine, and are cancelled on revocation. Machine
  names stay unique.
- **Rotation enforced.** Legacy machine tokens marked for rotation stop working
  14 days after upgrade.
- **Hub view follows rejection.** A spoke HUD stops showing the hub view as soon
  as the hub rejects its session, and never serves a cached view past its limit.
- **Smaller surface.** API docs are off, unexpected validation errors return a
  fixed code, the CSP adds `base-uri 'none'` and `object-src 'none'`, and web
  pages can no longer trigger the desktop update install.
- **Local hardening.** Proxy-free update traffic, `taskkill` from System32,
  fail-closed junction checks, capped agent-run names and home-relative paths
  in `burnrate doctor`.

## Upgrade notes

1. **Back up the hub before upgrading.** Use SQLite's backup API while running,
   or stop the hub and copy the complete database/configuration state.
2. **Hubs must be HTTPS.** A spoke paired to a non-loopback `http://` hub stops
   syncing until it is re-paired with an `https://` address.
3. **Setup token length.** A hub with `BURNRATE_SETUP_TOKEN` set to fewer than
   32 characters will not start. Generate one with
   `python -c "import secrets; print(secrets.token_urlsafe(32))"`.
4. **Legacy machine tokens** flagged for rotation must be re-paired within 14
   days of upgrading; `GET /api/machines` reports each `rotationDeadline`.
5. **Pending pairing codes** issued before the upgrade are expired. Issue a new
   code.
6. **Automation and scripts** calling login, registration or pairing redemption
   must send `Content-Type: application/json` and `X-BURNRATE-Request: 1`, and
   read the session from the `burnrate_access` cookie instead of the body.
7. **`BURNRATE_NATIVE_PUBLISHERS` is removed.** Use
   `BURNRATE_NATIVE_THUMBPRINTS` to trust a specific signing certificate.
8. The 0.5.8 hub image digest is published after the tag build; the reference
   Compose file is pinned to that digest in a follow-up release-metadata commit.

See [upgrade and rollback](../self-hosting.md#auth-model-upgrade-and-rollback).
An old binary does not enforce the new restrictions.

## Install and update

- **Installed native Windows app:** Settings → General → Check for updates, then
  **Install & restart**.
- **Python/source installation:** install the release wheel into the existing
  Python environment and restart the desktop/service processes. Preserve its
  data directory.
- **Sidecar HUD:** use the tray's **Check for updates**. The HUD no longer shows
  an update button.
- **Self-hosted hub:** update with `burnrate hub update`, or deploy the
  digest-pinned `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.8` image once its
  digest is published.

Release assets are built and update-signed on GitHub Actions from this tag, with
`SHA256SUMS`, `native-latest.json`, `latest.json` and build-provenance
attestations. Verify downloads with
`gh attestation verify <file> --repo kamol-nazarov/myBurnRate`.

## Known limits

The CSP still permits inline scripts (removal is planned), the Antigravity
loopback RPC skips certificate verification because the language server uses an
in-memory self-signed certificate, and the pinned launch thumbprint list is
empty until SignPath signing is in place, so unsigned desktop builds still launch
unless `BURNRATE_REQUIRE_SIGNED_NATIVE=1`. Redaction is defence in depth:
anything after a secret field on the same line is removed, and lines over 8 KiB
in stored errors are omitted.
