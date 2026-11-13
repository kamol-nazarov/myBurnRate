"""Build the portable native application. Requires PyInstaller and Tcl/Tk."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "build"


def main() -> int:
    from build_service import commit

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
    archive = shutil.make_archive(
        str(BUILD / "BURNRATE-native"),
        "zip",
        root_dir=bundle.parent,
        base_dir=bundle.name,
    )
    print(f"Native application: {bundle}")
    print(f"Portable package: {archive}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
