# Security Policy

myBurnRate reads AI coding tools' local telemetry, stores usage metadata in SQLite, and serves a UI
on `127.0.0.1`. That boundary is the product, so reports about it are taken seriously. Thank you for
helping keep users safe.

## Supported versions

Security fixes are shipped for the latest released minor line.

| Version | Supported | Notes |
| --- | --- | --- |
| `0.5.x` | Yes | Current stable line. Fixes ship in the next `0.5.x` release. |
| `0.4.x` and earlier | No | Upgrade to `0.5.x`; older lines receive no fixes. |

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
- The native desktop window: the WebView2/pywebview JavaScript bridge, local file loading, theme and
  settings persistence, and the Tauri shell's command permissions and CSP.
- Hub and spoke: pairing codes, machine bearer tokens, account authentication, sync ingestion,
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

- **Loopback by default.** The service binds `127.0.0.1:17331`. There is no built-in remote access,
  no cloud account and no relay. A hub (`BURNRATE_CENTRAL_SERVER=1`) fails closed without
  `BURNRATE_ACCESS_TOKEN`; creating its first account also requires `BURNRATE_SETUP_TOKEN`.
- **Read-only harness access.** Another tool's SQLite database is opened with `mode=ro` and
  `PRAGMA query_only=ON`; JSONL and log files are opened for reading. myBurnRate never writes another
  tool's files and never reads, reuses or refreshes Claude's sign-in.
- **No conversation content.** Prompts, responses, reasoning text, diffs, attachments, code, tool
  arguments and tool results are parsed in memory where a format requires it and then discarded.
  Only metadata is persisted.
- **Credentials stay out of SQLite and API responses.** Optional provider keys live in Windows
  Credential Manager, the macOS Keychain or the Secret Service. Credential-shaped strings are
  stripped before ingest errors are written.
- **Machine tokens are write-scoped, not read-isolated.** A machine token can push only as its own
  machine, but the hub is single-owner: its read routes cover every machine on that hub. Treat a
  machine token as a hub read credential as well as a sync credential.
- **Allowlisted network egress.** Credential-bearing HTTP clients use an explicit host allowlist,
  HTTPS only, timeouts, and `trust_env=False` so `HTTP_PROXY` cannot redirect secrets. Meters that
  reuse a key another tool stores are on by default with a switch; every other provider call needs a
  key the user adds. There is no inference proxy.
- **Update verification before execution.** A native update is rejected unless the URL, byte size,
  SHA-256, signer and version all match the pinned expectations, and failed downloads are deleted.
- **Checked desktop launch.** `/api/hud/native` only starts a fixed-path binary after an Authenticode
  check. A valid signature from an unknown publisher, or a signature that no longer matches the file,
  is refused. Unsigned release builds are allowed while signing is pending and reported in the
  response; set `BURNRATE_REQUIRE_SIGNED_NATIVE=1` to refuse them, and
  `BURNRATE_NATIVE_PUBLISHERS` to change the allowed signer names.
- **Reproducible releases.** Artifacts are built on GitHub Actions from a tagged commit, listed in
  `SHA256SUMS`, and carry build provenance attestations.
- **Continuous checks.** CI runs a hermetic test suite on Windows, macOS and Linux, Python, Rust and
  npm dependency audits, and a secret scan on every push.

## If a secret is exposed

1. Rotate the credential with the provider immediately. Treat any key pasted into an issue, log or
   commit as compromised.
2. Do not open a public issue to ask whether it is a problem.
3. Report it through the private advisory above and describe the kind of secret, the version and
   where it appeared. Do not repeat the value.
4. If the exposure is in this repository's history, maintainers will coordinate history cleanup and
   advisory publication.
