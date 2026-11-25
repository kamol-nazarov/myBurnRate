# Security Policy

myBurnRate reads AI coding tools' local telemetry, stores usage metadata in SQLite, and runs a local API
on `127.0.0.1` for the native desktop window. That boundary is the product, so reports about it are taken seriously. Thank you for
helping keep users safe.

## Supported versions

Security fixes are shipped for the latest released minor line.

| Version | Supported | Notes |
| --- | --- | --- |
| `0.6.x` | Yes | Current stable line. Fixes ship in the next `0.6.x` release. |
| `0.5.x` and earlier | No | Upgrade to the current stable line. |

Pre-release tags and unreleased branches are supported on a best-effort basis only. Every packaged
release is built by the tagged release workflow; there are no private hotfix builds. Always confirm
the version in **Settings → General** or with `burnrate --version` before reporting.

## Reporting a vulnerability

**Use GitHub's private vulnerability reporting:**
<https://github.com/kamol-nazarov/myBurnRate/security/advisories/new>

That is the only intake channel. There is no security mailbox. Do **not** open a public issue, pull
request or discussion containing exploit details, credentials, tokens, session files, `.env`
contents, database dumps, real telemetry, or prompts and code from a real session.

Please include:

- A short description of the issue and who it affects.
- Reproduction steps that do **not** require live provider credentials or someone else's data.
- The affected version, commit or release asset, if known.
- The security property you believe is broken: for example credential exposure, cross-machine data
  leakage, signature bypass, arbitrary file read, or remote code execution.
- Whether a secret may already be exposed. **Do not paste the secret**; say what kind it is and
  rotate it.

### What to expect

| Stage | Target |
| --- | --- |
| Acknowledgement that the report was received | 3 business days |
| Initial assessment and severity | 10 business days |
| Fix or documented mitigation for a confirmed high-severity issue | next release, normally within 30 days |
| Coordinated public disclosure | by agreement, normally within 90 days of the report |

These are targets for a volunteer-maintained project, not contractual deadlines. We will tell you if
a report is out of scope or if we need more information, and we will keep you updated while a fix is
in progress. If the issue is already public, say so in the report so coordination can start
immediately.

### Disclosure and credit

Please give maintainers time to investigate and ship a fix before public disclosure. We will agree a
disclosure date with you, publish a GitHub security advisory, and credit you by name or handle unless
you prefer to stay anonymous. There is **no paid bug bounty**; this is a free, community-maintained
project.

### Safe harbor

We consider good-faith security research authorized when you:

- test only against your own installation, your own hub, or a copy you control;
- avoid accessing, modifying or exfiltrating another person's data;
- avoid degrading service for others, and stop as soon as you have demonstrated the issue;
- give us reasonable time to fix the issue before publishing.

We will not pursue legal action for research conducted under those conditions. This statement does
not authorize testing of third-party services, provider APIs, GitHub infrastructure, or anyone else's
computer.

## Scope

### In scope

- The local HTTP service and API on `127.0.0.1` (authentication, authorization, CSRF and
  DNS-rebinding defenses, path handling, injection).
- The native desktop window: the WebView2/pywebview JavaScript bridge, local file loading, and theme and
  settings persistence.
- Hub and spoke: machine bearer tokens, email account authentication, sync ingestion,
  per-machine isolation, and upgrade handling.
- Credential handling: storage in the OS keychain, reuse of provider-owned keys, redaction in logs,
  API responses, `burnrate doctor` output and copied diagnostics.
- Installer and updater: `native-latest.json` parsing, URL and redirect allowlisting, size, SHA-256
  and Ed25519 signature checks, Authenticode publisher checks, and update staging.
- Data-handling guarantees: that prompts, responses, reasoning text, diffs, tool payloads and source
  code are neither stored nor transmitted.
- Vulnerabilities in dependencies as shipped in a release artifact.

### Out of scope

- Provider outages, billing disputes, rate-limit policy, account suspension and terms-of-service
  matters.
- Breakage caused by an undocumented provider format changing. Those readers are labeled
  experimental; report the compatibility break as a normal bug. Report it as security only if it
  leaks data or crosses another boundary above.
- Deployments you exposed yourself by binding to a non-loopback address or by putting a proxy,
  tunnel or Tailscale Funnel in front of the service. The default configuration is loopback-only.
- Reports that depend on another person's credentials, session files or machine.
- Local denial of service, physical access, a compromised user account, or a machine already running
  malicious software with the user's privileges.
- Missing hardening headers, scanner output or dependency version bumps with no demonstrated impact.
- Social engineering, and vulnerabilities in GitHub, provider services or other third-party
  infrastructure.

## Security properties we rely on

These are invariants, not aspirations. A change that breaks one is a security bug.

- **Loopback by default.** The service binds `127.0.0.1:17331`. On a desktop, API requests require the
  local installation credential except for minimal hub discovery. Basic `/healthz` remains available
  for local supervision. A hub (`BURNRATE_CENTRAL_SERVER=1`) requires authentication for protected
  API access. Creating its first owner through the API requires the configured
  `BURNRATE_SETUP_TOKEN` or a valid recovery bearer. The hub administrator can instead use
  `burnrate hub create-owner` locally. Supported hub ingress uses an HTTP reverse proxy to
  carry the client address. A raw TCP forward presents all clients as loopback.
- **Read-only harness access.** The shared SQLite reader used by local adapters and SQLite schema
  inspection use `mode=ro` and `PRAGMA query_only=ON`. The saved-profile readers for Claude,
  OpenCode and quota discovery use both on their BURNRATE config database, as do the pricing
  audit and subscription-upgrade reader. JSONL and log files are opened for reading.
  Claude account discovery reads account IDs, plan tiers and cached usage from `.claude.json`.
  BURNRATE does not read Claude sign-in tokens or credential files. It may invoke the local
  Traycer CLI for quota and activity metadata.
- **Conversation handling.** BURNRATE extracts usage data from transcripts without retaining
  the prompts, responses, reasoning text, diffs, attachments, code, tool arguments or tool results.
  Local metadata can include titles and short summaries provided by a source tool.
  Synced activity records carry a fixed tool label.
- **Provider credentials stay out of SQLite and API responses.** Keys added in BURNRATE use Windows
  Credential Manager, the macOS Keychain or the Secret Service. Keys supplied through a per-user
  `.env` are plaintext in that file. Recognized credential formats are scrubbed before ingest
  errors are written.
- **Explicit principals.** The desktop installation key authorizes local API access. The hub
  separates owner sessions, paired machine credentials and unauthenticated callers. Protected
  routes require a principal with the matching permission. The human role on the hub is `owner`.
- **Machine tokens are read-isolated and expire.** Machine tokens push only as themselves and read
  only an own-machine usage projection (`usage`, `summary`, `entity` with one exact `machine` ID)
  and their own registry entry. They cannot read hub aggregates, plans, accounts or settings.
  New tokens expire after **90 days**, record creation/last use/expiry, and store only SHA-256 hashes
  on the hub. Refresh requires the current unexpired token, succeeds once and invalidates it.
  Expired/revoked/unknown credentials get 401; authenticated scope violations get 403.
- **Expiring owner sessions.** Owner credentials use **30-day** sessions. Rotation stays within
  a **90-day** limit from password sign-in, or from issuance for recovery sessions.
  The hub stores hashes, device binding where available,
  authentication time, creation/last-use/expiry, and revocation. Repeated unsuccessful password
  checks throttle the account and caller address together.
  `/api/account/*` and `/api/auth/*` provide sign-in, session listing/revocation, logout and credential rotation.
- **Spoke session management.** A connected computer's loopback API requires its local key
  or a session issued for that installation. The local auth context and bootstrap routes serve
  that API. Hub owner session management runs through the native desktop bridge and the
  credential for the issuing hub. Break-glass rotation runs on the hub.
- **Break-glass deprecation.** `BURNRATE_ACCESS_TOKEN` is for recovery/bootstrap and explicitly
  configured automation, not normal UI authentication. By default it is accepted only on
  `/api/auth/bootstrap`, `/api/auth/rotate`, and protected first-owner registration at
  `/api/account/register`. Set `BURNRATE_ALLOW_ADMIN_BEARER=1` to enable legacy
  bearer automation. `/api/auth/rotate` replaces it with a hash persisted in SQLite; the old
  environment value cannot regain access on restart. The replacement is returned once.
  A connected computer never relays break-glass rotation; do it on the hub.
- **Machine credentials.** Each connected computer registers with its persistent device ID for a
  90-day machine token (auto-rotated in the background before expiry). Revocation invalidates its
  machine token and owner sessions bound to the device. A new password sign-in can re-enroll it.
  The owner can see and revoke sessions without a device binding.
- **Admin audit.** `admin_audit` records actor type/ID, fixed action, target ID, outcome and UTC time.
  Sign-in, registration and middleware-recorded events include the resolved source address.
  Some internal events have no request address.
  SQLite triggers reject UPDATE/DELETE. Tokens, hashes, passwords, labels, request
  bodies and conversation content are never audit payloads. This is a local append-only record,
  not a tamper-proof external ledger against a database administrator.
- **Owner view on spokes.** A machine token grants sync and own-machine reads. For All computers, the owner
  signs in on each spoke. The OS credential store holds the owner session and machine token for the
  issuing hub origin. The hub stores their hashes. On sign-out,
  expiry or rejected access, show this computer's recorded usage and a sign-in notice. A freshly
  cached view can persist until its freshness TTL, which defaults to one second and varies by
  endpoint. Authentication rejection purges the credential's cache. Offline fallback uses
  previously authenticated data only before the session's known expiry. Protect the spoke's
  OS account and private data directory.
  A stolen active machine token can refresh itself until revoked; expiry bounds inactive leakage,
  not an active token thief. Revoke the computer after exposure. Monitor audit-table disk growth.
- **Allowlisted network egress.** Provider meters use HTTPS, explicit destination allowlists,
  timeouts and `trust_env=False`. The default-on Z.AI, OpenCode Go and Muse meters reuse those tools'
  sign-ins and can be disabled in Sources. The Antigravity language service is local. See the
  [privacy disclosure](docs/privacy.md) for hosts and credentials.
- **Update verification before execution.** Desktop updates verify the URL, byte size,
  SHA-256, minisign signature and version. A signed Windows installation also requires the
  update to satisfy its installed publisher check. Failed downloads are deleted.
- **Checked desktop launch.** `/api/hud/native` only starts the installed desktop binary
  (`%LOCALAPPDATA%\Programs\BURNRATE\BURNRATE.exe`) after an Authenticode check. A binary is trusted as `valid` only when
  Authenticode validation succeeds and its certificate thumbprint matches an enrolled pin.
  Set the build pins with `BURNRATE_NATIVE_BUILD_THUMBPRINTS` and any operator pins with
  `BURNRATE_NATIVE_THUMBPRINTS`. While the list is empty, `untrusted` and
  `unsigned` development builds can launch unless `BURNRATE_REQUIRE_SIGNED_NATIVE=1`.
  A valid signature from a different publisher is `foreign` after enrolment. The launcher refuses
  foreign, tampered and unknown verification results.
- **Release provenance.** Desktop assets are built from the release tag. The Compose hub image is
  built from the release branch and promoted by digest in the signed release. The Compose
  operator verifies the image digest's provenance attestation. Its optional desktop update also
  verifies the wheel signature and attestation. Both the Compose and source-based hub operators
  verify the release tag. The source-based operator also verifies the exact commit. The repository owner
  must configure the release environment, rulesets and trusted signers before relying on these gates.
- **Continuous checks.** CI runs a hermetic test suite on Windows, macOS and Linux, Python and
  npm dependency audits, and a secret scan on every push.

## If a secret is exposed

1. Rotate the credential with the provider immediately. Treat any key pasted into an issue, log or
   commit as compromised.
2. Do not open a public issue to ask whether it is a problem.
3. Report it through the private advisory above and describe the kind of secret, the version and
   where it appeared. Do not repeat the value.
4. If the exposure is in this repository's history, maintainers will coordinate history cleanup and
   advisory publication.
