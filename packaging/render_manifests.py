"""Write the Scoop and winget manifests for a release's Windows installer.

    python packaging/render_manifests.py --tag v0.4.0-beta.2 --repo owner/name --dir release/

Writes ``myburnrate.json`` (Scoop) and ``winget-manifests-<version>.zip``
(the three winget manifest files) into the release directory. Install with
Scoop straight from a release:

    scoop install https://github.com/<repo>/releases/latest/download/myburnrate.json

The winget files are submitted to microsoft/winget-pkgs by a maintainer
(see docs/releasing.md).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path

PACKAGE_ID = "KamolNazarov.myBurnRate"
DESCRIPTION = ("Local-first AI coding-agent usage, plan meters and API-equivalent cost in an always-on-top HUD. "
               "Reads your tools' local files; nothing leaves your computer unless you connect a hub you run.")


def installer(directory: Path) -> Path:
    found = sorted(directory.glob("*_x64-setup.exe"))
    if len(found) != 1:
        raise SystemExit(f"Expected one Windows installer in {directory}, found {len(found)}")
    return found[0]


def scoop_manifest(*, version: str, url: str, sha256: str, repo: str) -> dict:
    return {
        "version": version,
        "description": DESCRIPTION,
        "homepage": f"https://github.com/{repo}",
        "license": "Apache-2.0",
        "architecture": {"64bit": {"url": url + "#/setup.exe", "hash": sha256}},
        # The NSIS installer installs per user into Scoop's folder, quietly.
        "installer": {"args": ["/S", "/D=$dir"], "file": "setup.exe"},
        "uninstaller": {"file": "uninstall.exe", "args": ["/S"]},
        "checkver": {"github": f"https://github.com/{repo}"},
        "autoupdate": {"architecture": {"64bit": {
            "url": f"https://github.com/{repo}/releases/download/v$version/BURNRATE_$version_x64-setup.exe#/setup.exe",
            "hash": {"url": f"https://github.com/{repo}/releases/download/v$version/SHA256SUMS"},
        }}},
    }


def winget_files(*, version: str, url: str, sha256: str, repo: str) -> dict[str, str]:
    base = f"PackageIdentifier: {PACKAGE_ID}\nPackageVersion: {version}\n"
    return {
        f"{PACKAGE_ID}.yaml": base + "DefaultLocale: en-US\nManifestType: version\nManifestVersion: 1.6.0\n",
        f"{PACKAGE_ID}.installer.yaml": base + (
            "InstallerType: nullsoft\n"
            "Scope: user\n"
            "InstallModes:\n  - interactive\n  - silent\n"
            "UpgradeBehavior: install\n"
            "Installers:\n"
            "  - Architecture: x64\n"
            f"    InstallerUrl: {url}\n"
            f"    InstallerSha256: {sha256.upper()}\n"
            "ManifestType: installer\nManifestVersion: 1.6.0\n"
        ),
        f"{PACKAGE_ID}.locale.en-US.yaml": base + (
            "PackageLocale: en-US\n"
            "Publisher: BURNRATE contributors\n"
            "PackageName: myBurnRate\n"
            f"PackageUrl: https://github.com/{repo}\n"
            "License: Apache-2.0\n"
            f"LicenseUrl: https://github.com/{repo}/blob/main/LICENSE\n"
            f"ShortDescription: {DESCRIPTION.split('. ')[0]}.\n"
            f"Description: {DESCRIPTION}\n"
            "Tags:\n  - ai\n  - claude-code\n  - codex\n  - token-usage\n  - developer-tools\n"
            f"ReleaseNotesUrl: https://github.com/{repo}/releases/tag/v{version}\n"
            "ManifestType: defaultLocale\nManifestVersion: 1.6.0\n"
        ),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag", required=True)
    parser.add_argument("--repo", required=True)
    parser.add_argument("--dir", required=True, type=Path)
    args = parser.parse_args()
    version = args.tag.removeprefix("v")
    setup = installer(args.dir)
    sha256 = hashlib.sha256(setup.read_bytes()).hexdigest()
    url = f"https://github.com/{args.repo}/releases/download/{args.tag}/{setup.name}"
    scoop = scoop_manifest(version=version, url=url, sha256=sha256, repo=args.repo)
    (args.dir / "myburnrate.json").write_text(json.dumps(scoop, indent=4) + "\n", encoding="utf-8")
    with zipfile.ZipFile(args.dir / f"winget-manifests-{version}.zip", "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, text in winget_files(version=version, url=url, sha256=sha256, repo=args.repo).items():
            bundle.writestr(name, text)
    print(f"manifests for {setup.name} ({sha256[:12]})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
