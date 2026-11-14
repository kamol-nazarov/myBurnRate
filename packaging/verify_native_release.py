"""Check a native installer and its update signature the way an installed app will.

    python packaging/verify_native_release.py release/myBurnRate_0.6.0_x64-setup.exe

The release workflow runs this on the FINAL installer (signed, if code signing ran) right after
`tauri signer sign`. It fails unless the .sig exists, its signed `file:` field is exactly the
installer's file name, and the signature verifies against the update key pinned in the app
(spend_app/native_update.py), so a release can never ship a feed the app would refuse.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1:
        print(__doc__, file=sys.stderr)
        return 2
    installer = Path(args[0])
    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from make_latest_json import check_native_signature

    from spend_app import native_update

    signature = check_native_signature(installer)
    version = installer.name.removeprefix(native_update.INSTALLER_PREFIX).removesuffix(
        native_update.INSTALLER_SUFFIX
    )
    if native_update.installer_name(version) != installer.name:
        raise SystemExit(f"{installer.name} is not a native installer file name")
    native_update.verify_minisign_file(
        installer, signature, expected_file=installer.name, expected_version=version
    )
    print(
        f"{installer.name}: signature verifies against the pinned update key and names this file"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
