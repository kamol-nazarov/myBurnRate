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
6. Tag the release branch and push the tag:
   `git tag -a v<version> -m "BURNRATE v<version>"` then
   `git push origin v<version>`.
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
9. Once both the release workflow and the tag's Hub image build pass (verify
   with `docker buildx imagetools inspect ghcr.io/kamol-nazarov/myburnrate-hub:<version>`
   that `:<version>` matches the pinned `sha256:<digest>` in
   `deploy/hub/compose.yaml`), move `prod` to the release:
   `git push origin v<version>^{commit}:refs/heads/prod` (a fast-forward; it
   fails rather than rewrite history). Do not guess an image digest before the
   candidate dispatch publishes it.

## Built-in hub address

Release builds read `BURNRATE_BUILTIN_HUB_URL` at build time and embed it into the
git-ignored `spend_app/_build_config.py` (`BUILTIN_HUB_URL`) via `packaging/build_config.py`:

- **Wheel builds:** `hatch_build.py` (configured under `[tool.hatch.build.hooks.custom]` in
  `pyproject.toml`) validates `BURNRATE_BUILTIN_HUB_URL` for all targets and writes
  `spend_app/_build_config.py` into non-editable wheels without leaving generated files
  in the source tree.
- **Native Windows builds:** `packaging/build_native.py` validates and writes
  `spend_app/_build_config.py` before invoking PyInstaller and removes it in a `finally`
  block afterwards.
- **Release workflow:** `.github/workflows/release.yml` sets `BURNRATE_BUILTIN_HUB_URL`
  from the `BURNRATE_BUILTIN_HUB_URL` repository **secret** in the native Windows installer
  step and the wheel/sdist step, and fails if the secret is missing. It is a secret, not a
  variable, so the address is masked in the public workflow logs and never committed:
  `gh secret set BURNRATE_BUILTIN_HUB_URL`.

The build fails if `BURNRATE_BUILTIN_HUB_URL` is set to anything other than a bare
`https://` origin (non-HTTPS schemes, embedded credentials, paths, query strings, and
fragments are rejected). Leaving it empty or unset builds an app with no built-in hub.

## The update-signing key

In-app updates and `scripts/install-native.ps1` are verified with an Ed25519
minisign updater key. The private key and its password are the repository secrets
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
