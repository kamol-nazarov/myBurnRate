# BURNRATE 0.5.6

Security hotfix for hub authentication, pairing and machine isolation, with separate OpenCode
provider usage rows including Isoquant. This release also includes the security hardening completed
after 0.5.5.

## Security

- **Owner sessions:** normal UI access uses independent 30-day sessions with sign-in, logout,
  device labels, revocation and credential controls at `/auth`. Session tokens are hashed on the
  hub; browser cookies are HttpOnly and SameSite=Lax, with same-origin mutation checks.
- **Machine isolation:** a paired computer can push as itself and read only its own usage. Machine
  tokens expire after 90 days and support current-token rotation. Other computers, hub aggregates,
  accounts, plans and settings require owner access.
- **Short-lived pairing:** invitations expire after 15 minutes, redeem once at the issuing hub
  origin, and yield an expiring machine token. Re-pairing preserves usage and protects against
  overlapping or failed sync jobs.
- **Admin audit:** authentication and administrative actions record actor, action, target, outcome
  and time in an append-only local table, without credentials or conversation content.
- **Existing hardening:** fail-closed hub access, first-owner bootstrap protection, exact Host
  checks, login throttles, bounded request bodies, verified native launch and installer signatures,
  pinned build tooling and hash-locked Python dependencies.

`BURNRATE_ACCESS_TOKEN` is deprecated for normal UI use. It remains available for bootstrap,
recovery and explicitly configured legacy automation. Set `BURNRATE_ALLOW_ADMIN_BEARER=0` to limit
it to recovery/session bootstrap and credential rotation.

## OpenCode usage

- Isoquant appears as **OpenCode · Isoquant**; other providers have separate recorded-usage rows.
  Z.AI coding-plan and newly recorded OpenCode Go usage keep their existing plan/profile lanes.
- Historical events are reassigned in place without duplicate usage or changed counts and costs.
  Provider switches preserve historical attribution; the cumulative-history backfill runs once.
- Legacy Go history without matching message evidence stays in **Go (profile unknown)**, outside
  quota rows. Matching source messages refine it onto the correct profile when available.
- Unknown model prices retain counted tokens as unpriced. Published model rates represent
  API-equivalent value, not a provider invoice. Provider chart colors are stable and Grok route
  evidence remains visible.

## Upgrade notes

1. **Back up the hub before upgrading.** Use SQLite's backup API while running, or stop the hub
   and copy the complete database/configuration state. Do not copy only a live WAL database file.
2. Existing accounts and machines migrate idempotently. Owners retain their passwords and do
   not repeat the setup-token step. Legacy machine expiry is original creation time plus 180 days,
   with rotation required; tokens already older than that must be re-paired.
3. Existing 0.5.5 spokes can keep pushing with unexpired migrated credentials. For **All computers**,
   upgrade the spoke and sign in once with the hub owner account. A separate owner session grants
   that view; without it the app shows scoped local usage and a sign-in notice.
4. Rotate machine credentials before expiry using the owner-access controls. After expiry, use
   a fresh pairing invitation. Old invitations that embed machine tokens are no longer accepted.

See [upgrade and rollback](../self-hosting.md#auth-model-upgrade-and-rollback). An old binary does
not enforce the new scope or expiry, and restoring an old backup may revive credentials.

## Install and update

- **Installed native Windows app:** Settings → General → Check for updates, then **Install & restart**.
  Installed copies detect updates automatically after launch and daily; installation requires this
  explicit action. Portable and Python/source installations do not install updates automatically.
- **Python/source installation:** install the release wheel into the application's existing Python
  environment and restart its desktop/service processes. Preserve its data directory.
- **Sidecar HUD:** use its update bar or the tray's **Check for updates**.
- **Self-hosted hub:** update the checkout/service with `burnrate hub update`, or deploy the
  digest-pinned `ghcr.io/kamol-nazarov/myburnrate-hub:0.5.6` image once available.

Release assets are built and update-signed on GitHub Actions from this tag, with `SHA256SUMS`,
`native-latest.json`, `latest.json` and build-provenance attestations. Verify downloads with
`gh attestation verify <file> --repo kamol-nazarov/myBurnRate`.
