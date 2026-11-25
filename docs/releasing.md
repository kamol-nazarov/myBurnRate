# Releasing

Releases are built only on GitHub Actions
([release workflow](../.github/workflows/release.yml)), never on a
maintainer's computer, so every download traces back to a tagged commit.

## Branches

- **`prod`** is the default branch. It always contains the latest published
  release, and may also carry documentation-only commits that change no
  shipped code, so the repository's front page, the README and the links in
  release notes describe what people actually download.
- **`release/<version>`** collects the work for one release, for example
  `release/0.5.8`. Cut it from `prod`; feature and fix branches merge into it,
  and the release is tagged from it. There is no long-lived `main` branch.
- `prod` only moves forward: to a published release tag (step 9 below), or to a
  documentation or metadata commit that changes no shipped code. A release
  branch merges into `prod` only once its release is ready.

## Steps

1. If dependencies changed, refresh `uv.lock` and the three hash-locked
   exports (`requirements-build.txt`, `requirements-dev.txt`,
   `requirements-ci.txt`) with `uv lock` and `uv export --frozen` (see
   [contributing](../CONTRIBUTING.md#dependency-locks)). CI refuses a stale
   lock.
2. Bump the version in these files:
   - `pyproject.toml` (PEP 440, for example `0.4.0b2`);
   - `spend_app/__init__.py`;
   - the current-release line and release-notes link in the README.
3. Write `docs/releases/RELEASE-<version>.md` (it becomes the release notes)
   and move the changelog's `[Unreleased]` entries under `## [<version>] - Unreleased`.
4. Commit on `release/<version>`, push it, and wait for CI to pass. Keep
   `deploy/hub/compose.yaml` on its tag-only `:<version>` pin while
   `CHANGELOG.md` still says `- Unreleased`, then manually dispatch
   [`.github/workflows/hub-image.yml`](../.github/workflows/hub-image.yml)
   from `release/<version>` with `version=<version>`. The workflow validates
   `spend_app.__version__`, publishes `ghcr.io/kamol-nazarov/myburnrate-hub:<version>-rc.<run_number>`,
   and prints its digest.
5. Pin `deploy/hub/compose.yaml` (`publish-check` and `hub`) to the published
   candidate `<version>-rc.<run_number>@sha256:<digest>` (for example
   `0.5.9-rc.<run_number>@sha256:<digest>`), then date `CHANGELOG.md`
   (`## [<version>] - YYYY-MM-DD`) and commit on `release/<version>`. The
   security tests allow a tag-only pin only while the changelog heading is
   still `- Unreleased`, and forbid dating the changelog without a digest pin.
6. Open a pull request from the release branch to prod and wait for the required checks.
   After merging, the owner signs the exact reviewed commit using their release signing key
   and publishes the signed version tag. The release jobs require the protected release environment.
   On the tag push, [`.github/workflows/hub-image.yml`](../.github/workflows/hub-image.yml)
   verifies that the candidate image's digest matches the digest pinned in
   `deploy/hub/compose.yaml`, re-tags that identical digest as `:<version>` and
   `:latest`, verifies the re-tagged `:<version>` digest matches, and attests
   build provenance.
7. The release workflow:
   - checks that the tag matches the version in `spend_app/__init__.py`;
   - builds the native Windows app's installer
     (`myBurnRate_<version>_x64-setup.exe`), has SignPath sign it when
     configured (which needs an approver; see [code signing](code-signing.md)),
     then makes its update signature for those final bytes and checks that the
     `.sig` names exactly that file and verifies against the key pinned in the
     app;
   - signs and verifies the release wheel with the same updater key. The optional
     `wheel` entry in `native-latest.json` serves managed Python desktop installs;
   - writes `native-latest.json` and `SHA256SUMS` and attests the build
     provenance;
   - creates a **draft** release with the installer, wheel and sdist.
8. Check the draft and publish it as the latest release. Installed native apps
   find the update after launch or daily, or at once from **Settings → General →
   Check for updates**.
9. Verify the Hub promotion matches the pinned image digest. The Hub image
   is built from the reviewed release branch candidate and promoted by digest
   on the signed tag. The release workflow's Windows and Python assets are built
   from the tag commit. For the PowerShell helper, run with `-PrepareCandidate`
   on the release branch, complete the pull request and sign the tag, then run
   again to verify the tag and assets before deployment.

## Hub address in builds

Public release workflows set `BURNRATE_PUBLIC_BUILD=1`. Their desktop builds
contain no built-in hub address. Set `BURNRATE_HUB_URL` in the per-user settings
on each device that should connect to a particular hub.

Private builds may still set `BURNRATE_BUILTIN_HUB_URL` to a bare HTTPS origin.
The packaging helper rejects credentials, paths, query strings, and fragments.

## The update-signing key

In-app updates and `scripts/install-native.ps1` are verified with an Ed25519
minisign updater key. The private key and its password are secrets in the protected release environment
`TAURI_SIGNING_PRIVATE_KEY` and `TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. The
corresponding public key is pinned in `spend_app/native_update.py`
(`PUBLIC_KEY_B64`) and `scripts/minisign-verify.ps1` (`$MinisignPublicKey`).

Keep an offline backup of the private key: if it is lost, installed apps
cannot verify any later update, and people would have to reinstall.

Rotating the key requires updating `PUBLIC_KEY_B64` in
`spend_app/native_update.py` and `$MinisignPublicKey` in
`scripts/minisign-verify.ps1` together. The app reads `native-latest.json`
(`{version, notes, pub_date, platforms: {"windows-x86_64": {url, signature, sha256, size}}}`)
from the release marked Latest, and accepts an installer only when its signature, size, SHA-256,
file name and version all match (see [native app](native-app.md#install-and-update)). The
signature records the installer's file name, so the workflow signs the installer under its final
name and the file must not be renamed afterwards. The release's `publish` job needs the
`native-windows` job to succeed, so a release is never missing its native feed.

Verify a download with:

- `gh attestation verify <file> --repo kamol-nazarov/myBurnRate`; or
- `sha256sum -c SHA256SUMS`.

## Auth model migration gate

Before releasing the auth-model change, run all synthetic auth/migration/spoke regressions and the
existing hardening/launch tests on Windows, macOS and Linux. No runtime dependency was added.
Communicate the fixed 90-day machine / 30-day owner lifetimes, one-time
`created_at + 180 days` legacy grace, and the static bearer now being
restricted by default (set `BURNRATE_ALLOW_ADMIN_BEARER=1` only for automation). Preserve a consistent pre-upgrade SQLite/config backup before
any deployment. See [upgrade and rollback](self-hosting.md#auth-model-upgrade-and-rollback): an old
binary does not enforce the new scope/expiry, and restoring a backup may revive old credentials.
