"""Write latest.json, the Tauri updater feed, from a release's signed bundles.

    python packaging/make_latest_json.py --tag v0.4.0-beta.2 --repo owner/name --dir release/

Each platform entry points at the asset on the GitHub release and carries the
contents of its .sig file. The feed is served from
https://github.com/<repo>/releases/latest/download/latest.json, so only the
release marked Latest offers updates.

The native Windows app has its own feed, native-latest.json, written with --native:

    python packaging/make_latest_json.py --native --tag v0.6.0 --repo owner/name --dir release/

It describes myBurnRate_<version>_x64-setup.exe (url, signature, sha256, size) and is read
by spend_app/native_update.py, which verifies it again on the user's computer. The native
installer never appears in latest.json: that feed belongs to the Tauri updater.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

# (platform key, how the updater bundle's file name ends)
PLATFORMS = (
    ("windows-x86_64", "_x64-setup.exe"),
    ("darwin-aarch64", "_aarch64.app.tar.gz"),
    ("darwin-x86_64", "_x64.app.tar.gz"),
    ("linux-x86_64", "_amd64.AppImage"),
)


# The native installer ends in _x64-setup.exe like the Tauri one (BURNRATE_<v>_x64-setup.exe), so every
# Tauri-side glob must skip this prefix. Must equal spend_app.native_update.INSTALLER_PREFIX (a test pins it).
NATIVE_PREFIX = "myBurnRate_"


def native_installer_name(version: str) -> str:
    """Must stay equal to spend_app.native_update.installer_name (a test pins this)."""
    return f"{NATIVE_PREFIX}{version.removeprefix('v').split('+', 1)[0]}_x64-setup.exe"


def trusted_comment_fields(signature_text: str) -> dict[str, str]:
    """Fields of the minisign trusted comment (``timestamp:..<TAB>file:..<TAB>version:..``)."""
    try:
        lines = base64.b64decode(signature_text.strip(), validate=True).decode("utf-8").split("\n")
    except (ValueError, UnicodeDecodeError):
        raise SystemExit("The update signature is not a base64 minisign file") from None
    if len(lines) < 4 or not lines[2].startswith("trusted comment: "):
        raise SystemExit("The update signature has no trusted comment")
    fields: dict[str, str] = {}
    for part in lines[2][len("trusted comment: "):].split("\t"):
        key, separator, value = part.partition(":")
        if separator and key not in fields:
            fields[key] = value
    return fields


def check_native_signature(installer: Path) -> str:
    """The signature next to ``installer`` must exist and name exactly that file; returns its text."""
    signature = installer.with_name(installer.name + ".sig")
    if not signature.is_file():
        raise SystemExit(f"{installer.name} has no update signature ({signature.name})")
    text = signature.read_text(encoding="utf-8").strip()
    signed_name = trusted_comment_fields(text).get("file")
    if signed_name != installer.name:
        raise SystemExit(
            f"{signature.name} was made for {signed_name!r}, not {installer.name!r}: "
            "sign the installer under its final name"
        )
    return text


def build_native_feed(directory: Path, *, tag: str, repo: str, notes: str = "", now: datetime | None = None) -> dict:
    version = tag.removeprefix("v")
    installer = directory / native_installer_name(version)
    if not installer.is_file():
        raise SystemExit(f"No native installer {installer.name} in {directory}")
    signature = check_native_signature(installer)
    data = installer.read_bytes()
    return {
        "version": version,
        "notes": notes or f"See https://github.com/{repo}/releases/tag/{tag}",
        "pub_date": (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "platforms": {
            "windows-x86_64": {
                "url": f"https://github.com/{repo}/releases/download/{tag}/{installer.name}",
                "signature": signature,
                "sha256": hashlib.sha256(data).hexdigest(),
                "size": len(data),
            }
        },
    }


def build_feed(directory: Path, *, tag: str, repo: str, notes: str = "", now: datetime | None = None) -> dict:
    version = tag.removeprefix("v")
    platforms = {}
    for key, suffix in PLATFORMS:
        for bundle in sorted(directory.glob(f"*{suffix}")):
            if bundle.name.startswith(NATIVE_PREFIX):
                continue  # the native app's installer belongs to native-latest.json
            signature = bundle.with_name(bundle.name + ".sig")
            if not signature.is_file():
                raise SystemExit(f"{bundle.name} has no update signature ({signature.name})")
            platforms[key] = {
                "signature": signature.read_text(encoding="utf-8").strip(),
                "url": f"https://github.com/{repo}/releases/download/{tag}/{bundle.name}",
            }
    if not platforms:
        raise SystemExit(f"No update bundles in {directory}")
    return {
        "version": version,
        "notes": notes or f"See https://github.com/{repo}/releases/tag/{tag}",
        "pub_date": (now or datetime.now(UTC)).isoformat().replace("+00:00", "Z"),
        "platforms": platforms,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag")
    parser.add_argument("--repo")
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--native", action="store_true", help="write native-latest.json instead of latest.json")
    parser.add_argument("--verify-native-sig", type=Path, metavar="INSTALLER",
                        help="check that INSTALLER's .sig names INSTALLER, then stop")
    args = parser.parse_args()
    if args.verify_native_sig is not None:
        check_native_signature(args.verify_native_sig)
        print(f"{args.verify_native_sig.name}: signature names this file")
        return 0
    if not (args.tag and args.repo and args.dir):
        parser.error("--tag, --repo and --dir are required")
    if args.native:
        native = build_native_feed(args.dir, tag=args.tag, repo=args.repo)
        (args.dir / "native-latest.json").write_text(json.dumps(native, indent=2) + "\n", encoding="utf-8")
        print("native-latest.json:", native["version"])
        return 0
    feed = build_feed(args.dir, tag=args.tag, repo=args.repo)
    (args.dir / "latest.json").write_text(json.dumps(feed, indent=2) + "\n", encoding="utf-8")
    print("latest.json:", ", ".join(feed["platforms"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
