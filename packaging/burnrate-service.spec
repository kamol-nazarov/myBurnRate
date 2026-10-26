# PyInstaller spec for the service the desktop app ships (see build_service.py).
# One folder, no console window. The web assets, price tables and time zone
# data travel with it; spend_app finds them next to its own package, as in a
# checkout.

import sys
from pathlib import Path

from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent

datas = [(str(ROOT / "pricing"), "pricing")]
build_commit = ROOT / "packaging" / "build" / "BUILD_COMMIT"
if build_commit.is_file():
    datas.append((str(build_commit), "."))
datas += collect_data_files("spend_web")
datas += collect_data_files("tzdata")
datas += collect_data_files("certifi")
# APScheduler and keyring find their parts through package metadata.
datas += copy_metadata("APScheduler") + copy_metadata("keyring")
hiddenimports = (
    collect_submodules("spend_app")
    + collect_submodules("spend_web")
    + collect_submodules("uvicorn")
    + collect_submodules("apscheduler")
    + collect_submodules("keyring.backends")
)

a = Analysis(
    [str(ROOT / "packaging" / "burnrate_service.py")],
    pathex=[str(ROOT)],
    datas=datas,
    hiddenimports=hiddenimports,
    excludes=["tkinter", "pytest", "_pytest", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)
exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="burnrate-service",
    console=False,
    icon=str(ROOT / "src-tauri" / "icons" / "icon.ico") if sys.platform == "win32" else None,
    upx=False,
)
COLLECT(exe, a.binaries, a.datas, name="burnrate-service", upx=False)
