# Releasing

Releases are built only on GitHub Actions
([release workflow](../.github/workflows/release.yml)), never on a
maintainer's computer, so every download traces back to a tagged commit.

## Branches

- **`prod`** is the default branch. It always contains the latest published
  release, and may also carry documentation-only commits that change no
  shipped code, so the repository's front page, the README and the links in
  release notes describe what people actually download.
- **`main`** collects finished work. Feature branches merge into `main`;
  releases are tagged from it.
- `prod` only moves forward: to a published release tag (step 8 below), or to a
  documentation or metadata commit that changes no shipped code.

## Steps

1. If dependencies changed, refresh `uv.lock` and the three hash-locked
   exports (`requirements-build.txt`, `requirements-dev.txt`,
   `requirements-ci.txt`) with `uv lock` and `uv export --frozen` (see
   [contributing](../CONTRIBUTING.md#dependency-locks)). CI refuses a stale
   lock.
2. Bump the version in these files:
   - `pyproject.toml` (PEP 440, for example `0.4.0b2`);
   - `spend_app/__init__.py`;
   - `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock` and
     `src-tauri/tauri.conf.json`;
   - the current-release line and release-notes link in the README.
3. Write `docs/releases/RELEASE-<version>.md` (it becomes the release notes)
   and move the changelog's `[Unreleased]` entries under the new version.
4. Commit, push, and wait for CI and the Desktop workflow to pass.
5. Tag and push: `git tag -a v<version> -m "BURNRATE v<version>"` then
   `git push origin v<version>`.
6. The release workflow:
   - checks that the tag matches the versions in the code;
   - builds and update-signs the Windows, macOS and Linux bundles;
   - builds the native Windows app's installer
     (`myBurnRate_<version>_x64-setup.exe`), has SignPath sign it when
     configured, then makes its update signature for those final bytes and
     checks that the `.sig` names exactly that file and verifies against the
     key pinned in the app;
   - code-signs the Windows installer through SignPath when signing is set
     up (see [code signing](code-signing.md)), which needs an approver;
   - writes `latest.json` (Tauri), `native-latest.json` (native) and
     `SHA256SUMS` and attests the build provenance;
   - creates a **draft** release with the wheel and sdist.
7. Check the draft and publish it as the latest release. Installed apps find
   the update within about six hours, or at once from the tray's **Check for
   updates**.
8. Move `prod` to the release: `git push origin v<version>^{commit}:refs/heads/prod`
   (a fast-forward; it fails rather than rewrite history).
   Once the tag's Hub image build has passed, verify its digest with
   `docker buildx imagetools inspect ghcr.io/kamol-nazarov/myburnrate-hub:<version>`
   and pin that `tag@sha256:...` in `deploy/hub/compose.yaml`. Commit this release metadata
   on `main` and fast-forward `prod` to it; keep the published tag unchanged. The image's
   digest is available only after its tag build, so do not guess it before tagging.
9. Command-line installs:
   - **Scoop** needs nothing more. The release carries `myburnrate.json`, so
     `scoop install https://github.com/kamol-nazarov/myBurnRate/releases/latest/download/myburnrate.json`
     installs the latest release.
   - **winget:** unzip the release's `winget-manifests-<version>.zip` into
     `manifests/k/KamolNazarov/myBurnRate/<version>/` of a fork of
     [microsoft/winget-pkgs](https://github.com/microsoft/winget-pkgs), check
     it with `winget validate`, and open a pull request; `wingetcreate submit`
     does the same in one step. The winget maintainers review new packages,
     and the installer should be code-signed first.

## The update-signing key

In-app updates are verified with a Tauri updater key. The private key and its
password are the repository secrets `TAURI_SIGNING_PRIVATE_KEY` and
`TAURI_SIGNING_PRIVATE_KEY_PASSWORD`. The public key is
`plugins.updater.pubkey` in `src-tauri/tauri.conf.json`.

Keep an offline backup of the private key: if it is lost, installed apps
cannot verify any later update, and people would have to reinstall.

The native app (`spend_app/native_update.py`) pins the same public key, and a test asserts that
the constant equals `plugins.updater.pubkey`, so rotating the key means changing both together.
It reads `native-latest.json`
(`{version, notes, pub_date, platforms: {"windows-x86_64": {url, signature, sha256, size}}}`)
from the release marked Latest, and accepts an installer only when its signature, size, SHA-256,
file name and version all match (see [native app](native-app.md#install-and-update)). The
signature records the installer's file name, so the workflow signs the installer under its final
name and the file must not be renamed afterwards. The native installer is deliberately kept out
of `latest.json` and the Scoop and winget manifests. The release's `publish` job needs the native
job to succeed, so a release is never missing its native feed.

Verify a download with:

- `gh attestation verify <file> --repo kamol-nazarov/myBurnRate`; or
- `sha256sum -c SHA256SUMS`.

## Auth model migration gate

Before releasing the auth-model change, run all synthetic auth/migration/spoke regressions and the
existing hardening/launch tests on Windows, macOS and Linux. No runtime dependency was added.
Communicate the fixed 90-day machine / 30-day owner / 15-minute invitation lifetimes, one-time
`created_at + 180 days` legacy grace, the new `/auth` controls and optional
`BURNRATE_ALLOW_ADMIN_BEARER=0`. Preserve a consistent pre-upgrade SQLite/config backup before
any deployment. See [upgrade and rollback](self-hosting.md#auth-model-upgrade-and-rollback): an old
binary does not enforce the new scope/expiry, and restoring a backup may revive old credentials.
The old `?token=` bootstrap is deprecated; it now issues a session cookie, never a static cookie.
