"""Write native-latest.json, the native Windows app's updater feed, from a signed installer.

    python packaging/make_latest_json.py --native --tag v0.6.0 --repo owner/name --dir release/

It describes myBurnRate_<version>_x64-setup.exe (url, signature, sha256, size) and is read
by spend_app/native_update.py, which verifies it again on the user's computer.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path

# Must equal spend_app.native_update.INSTALLER_PREFIX (a test pins it).
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


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tag")
    parser.add_argument("--repo")
    parser.add_argument("--dir", type=Path)
    parser.add_argument("--native", action="store_true", help="write native-latest.json (default)")
    parser.add_argument("--verify-native-sig", type=Path, metavar="INSTALLER",
                        help="check that INSTALLER's .sig names INSTALLER, then stop")
    args = parser.parse_args()
    if args.verify_native_sig is not None:
        check_native_signature(args.verify_native_sig)
        print(f"{args.verify_native_sig.name}: signature names this file")
        return 0
    if not (args.tag and args.repo and args.dir):
        parser.error("--tag, --repo and --dir are required")
    native = build_native_feed(args.dir, tag=args.tag, repo=args.repo)
    (args.dir / "native-latest.json").write_text(json.dumps(native, indent=2) + "\n", encoding="utf-8")
    print("native-latest.json:", native["version"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
