# Releasing

Releases are built only on GitHub Actions
([release workflow](../.github/workflows/release.yml)), never on a
maintainer's computer, so every download traces back to a tagged commit.

1. Bump the version in these files:
   - `pyproject.toml` (PEP 440, for example `0.4.0b2`);
   - `spend_app/__init__.py`;
   - `src-tauri/Cargo.toml`, `src-tauri/Cargo.lock` and
     `src-tauri/tauri.conf.json`;
   - the version line in the README.
2. Write `docs/releases/RELEASE-<version>.md` (it becomes the release notes)
   and move the changelog's `[Unreleased]` entries under the new version.
3. Commit, push, and wait for CI and the Desktop workflow to pass.
4. Tag and push: `git tag -a v<version> -m "BURNRATE v<version>"` then
   `git push origin v<version>`.
5. The release workflow:
   - checks that the tag matches the versions in the code;
   - builds and update-signs the Windows, macOS and Linux bundles;
   - code-signs the Windows installer through SignPath when signing is set
     up (see [code signing](code-signing.md)), which needs an approver;
   - writes `latest.json` and `SHA256SUMS` and attests the build provenance;
   - creates a **draft** release with the wheel and sdist.
6. Check the draft and publish it as the latest release. Installed apps find
   the update within about six hours, or at once from the tray's **Check for
   updates**.
7. Command-line installs:
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

Verify a download with:

- `gh attestation verify <file> --repo kamol-nazarov/myBurnRate`; or
- `sha256sum -c SHA256SUMS`.
