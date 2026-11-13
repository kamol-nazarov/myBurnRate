"""Portable native usage application, with Tk and the existing collector."""
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules, copy_metadata

ROOT = Path(SPECPATH).parent
datas = [(str(ROOT / "pricing"), "pricing")]
datas += [(str(ROOT / "src-tauri" / "icons" / "32x32.png"), ".")]
datas += collect_data_files("spend_web") + collect_data_files("tzdata") + collect_data_files("certifi")
datas += copy_metadata("APScheduler") + copy_metadata("keyring")
stamp = ROOT / "packaging" / "build" / "BUILD_COMMIT"
if stamp.is_file():
    datas.append((str(stamp), "."))
a = Analysis([str(ROOT / "packaging" / "burnrate_native.py")], pathex=[str(ROOT)], datas=datas,
    hiddenimports=collect_submodules("spend_app") + collect_submodules("spend_web")
        + collect_submodules("uvicorn") + collect_submodules("apscheduler") + collect_submodules("keyring.backends")
        + ["tkinter", "tkinter.ttk"],
    excludes=["pytest", "_pytest", "IPython", "unittest", "pydoc", "doctest", "idlelib", "turtledemo"],
    noarchive=False)
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True, name="BURNRATE", console=False,
    icon=str(ROOT / "src-tauri" / "icons" / "icon.ico") if sys.platform == "win32" else None, upx=False)
COLLECT(exe, a.binaries, a.datas, name="BURNRATE", upx=False)
