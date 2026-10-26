"""Build the frozen service and stage it for the desktop bundle.

Run with a Python 3.12 that has BURNRATE's dependencies and PyInstaller:

    uv venv .venv-build --python 3.12
    uv pip install --python .venv-build -r pyproject.toml pyinstaller
    .venv-build/Scripts/python packaging/build_service.py   (bin/python elsewhere)

The result lands in src-tauri/resources/burnrate-service/, which the Tauri
bundle ships next to the app.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BUILD = ROOT / "packaging" / "build"
STAGE = ROOT / "src-tauri" / "resources" / "burnrate-service"
KEEP = {"README.md"}


def commit() -> str:
    sha = os.environ.get("GITHUB_SHA", "").strip()
    if sha:
        return sha
    result = subprocess.run(["git", "-C", str(ROOT), "rev-parse", "HEAD"], capture_output=True, text=True, check=False)
    return result.stdout.strip()


def main() -> int:
    BUILD.mkdir(parents=True, exist_ok=True)
    (BUILD / "BUILD_COMMIT").write_text(commit(), encoding="utf-8")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", "--noconfirm", "--clean",
         "--distpath", str(BUILD / "dist"), "--workpath", str(BUILD / "work"),
         str(ROOT / "packaging" / "burnrate-service.spec")],
        check=True,
    )
    built = BUILD / "dist" / "burnrate-service"
    STAGE.mkdir(parents=True, exist_ok=True)
    for entry in STAGE.iterdir():
        if entry.name in KEEP:
            continue
        shutil.rmtree(entry) if entry.is_dir() else entry.unlink()
    for entry in built.iterdir():
        target = STAGE / entry.name
        shutil.copytree(entry, target) if entry.is_dir() else shutil.copy2(entry, target)
    print(f"staged {built} -> {STAGE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
