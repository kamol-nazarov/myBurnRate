"""Build the native application: a portable zip, and a per-user installer when NSIS is available.

    python packaging/build_native.py [--require-installer] [--require-signature]

Requires PyInstaller and Tcl/Tk. Outputs, in packaging/build/:

* native/BURNRATE/                       the PyInstaller bundle (BURNRATE.exe + _internal)
* BURNRATE-native.zip                    the portable package
* myBurnRate_<version>_x64-setup.exe  the installer (needs makensis; see burnrate-native.nsi)

BURNRATE.exe is Authenticode-signed before it is packaged and the installer after it is built,
when signing is configured through environment variables (see sign_windows.py and
docs/code-signing.md). With nothing configured the build is unsigned and says so.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "build"
NSIS_SCRIPT = ROOT / "packaging" / "burnrate-native.nsi"


def find_makensis(env=None, which=shutil.which) -> str | None:
    env = os.environ if env is None else env
    explicit = env.get("BURNRATE_MAKENSIS")
    if explicit:
        return explicit if Path(explicit).is_file() else None
    found = which("makensis")
    if found:
        return found
    for root in (env.get("ProgramFiles(x86)"), env.get("ProgramFiles")):
        if root and (Path(root) / "NSIS" / "makensis.exe").is_file():
            return str(Path(root) / "NSIS" / "makensis.exe")
    return None


def numeric_version(version: str) -> str:
    """x.y.z.0 for the installer's file-version resource."""
    match = re.match(r"^(\d+)\.(\d+)\.(\d+)", version)
    if match is None:
        raise SystemExit(f"Cannot derive a numeric version from {version!r}")
    return ".".join([*match.groups(), "0"])


def build_installer(makensis: str, bundle: Path, version: str, output: Path) -> None:
    if not re.fullmatch(r"[0-9A-Za-z.+-]+", version):
        raise SystemExit(f"Unexpected characters in version {version!r}")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.unlink(missing_ok=True)
    subprocess.run(
        [
            makensis,
            "/V2",
            f"/DVERSION={version}",
            f"/DVI_VERSION={numeric_version(version)}",
            f"/DSOURCE_DIR={bundle}",
            f"/DICON={ROOT / 'src-tauri' / 'icons' / 'icon.ico'}",
            f"/DOUTFILE={output}",
            str(NSIS_SCRIPT),
        ],
        check=True,
    )
    if not output.is_file():
        raise SystemExit(f"makensis did not produce {output}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--require-signature",
        action="store_true",
        help="fail unless BURNRATE.exe and the installer are Authenticode-signed",
    )
    parser.add_argument(
        "--require-installer",
        action="store_true",
        help="fail if the installer cannot be built (makensis missing)",
    )
    args = parser.parse_args(argv)

    sys.path.insert(0, str(ROOT))
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from build_service import commit
    from sign_windows import Signer, SigningError

    from spend_app import __version__
    from spend_app.native_update import installer_name

    signer = Signer(require=args.require_signature)
    makensis = find_makensis()
    try:
        signer.check_ready()
    except SigningError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if args.require_installer and makensis is None:
        print(
            "error: makensis (NSIS) was not found; install NSIS or set BURNRATE_MAKENSIS",
            file=sys.stderr,
        )
        return 1

    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "BUILD_COMMIT").write_text(commit(), encoding="utf-8")
    subprocess.run(
        [
            sys.executable,
            "-m",
            "PyInstaller",
            "--noconfirm",
            "--distpath",
            str(BUILD / "native"),
            "--workpath",
            str(BUILD / "native-work"),
            str(ROOT / "packaging" / "burnrate-native.spec"),
        ],
        check=True,
    )
    bundle = BUILD / "native" / "BURNRATE"
    shutil.copy2(
        ROOT / "src-tauri" / "icons" / "icon.ico", bundle / "_internal" / "burnrate.ico"
    )
    for name in ("LICENSE", "NOTICE"):
        shutil.copy2(ROOT / name, bundle / name)

    try:
        signed = [signer.sign(bundle / "BURNRATE.exe")]
        archive = shutil.make_archive(
            str(BUILD / "BURNRATE-native"),
            "zip",
            root_dir=bundle.parent,
            base_dir=bundle.name,
        )
        installer = BUILD / installer_name(__version__)
        if makensis is None:
            print(
                "NSIS (makensis) not found: skipping the installer. The portable zip is still built."
            )
        else:
            build_installer(makensis, bundle, __version__, installer)
            signed.append(signer.sign(installer))
        if args.require_signature:
            signer.require_signed(
                [bundle / "BURNRATE.exe", *([installer] if makensis else [])]
            )
    except (SigningError, subprocess.CalledProcessError, SystemExit) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1

    print(f"Native application: {bundle}")
    print(f"Portable package: {archive}")
    if makensis is not None:
        print(f"Installer: {installer}")
    if not all(signed):
        print("Signing: skipped (see the line above).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
