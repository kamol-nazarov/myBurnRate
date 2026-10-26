"""Where other apps keep their files, and which processes run, on each platform."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


def app_support_dir() -> Path:
    """Per-user application data: %APPDATA%, ~/Library/Application Support, or ~/.config."""
    if os.name == "nt":
        return Path(os.environ.get("APPDATA") or (Path.home() / "AppData" / "Roaming"))
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support"
    return Path(os.environ.get("XDG_CONFIG_HOME") or (Path.home() / ".config"))


def executable(name: str) -> str:
    return name + ".exe" if os.name == "nt" else name


def process_command_lines(name_fragment: str, *, timeout: float = 8) -> list[str]:
    """Command lines of running processes whose executable name contains ``name_fragment``.

    Used for local discovery only; the lines are kept in memory and never logged.
    """
    fragment = name_fragment.lower()
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 "Get-CimInstance Win32_Process | Where-Object { $_.Name -like '*" + name_fragment.replace("'", "")
                 + "*' } | Select-Object -ExpandProperty CommandLine"],
                capture_output=True, text=True, timeout=timeout, check=False, creationflags=NO_WINDOW,
            )
        else:
            completed = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True,
                                       timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    if os.name == "nt":
        return lines
    return [line for line in lines if fragment in Path(line.split(" ", 1)[0]).name.lower()]


def process_running(name_fragment: str) -> bool | None:
    """True or False when the process list could be read; None when it could not."""
    try:
        if os.name == "nt":
            completed = subprocess.run(
                ["tasklist", "/FI", f"IMAGENAME eq {name_fragment}.exe", "/NH", "/FO", "CSV"],
                capture_output=True, text=True, timeout=5, check=False, creationflags=NO_WINDOW,
            )
            return name_fragment.lower() in completed.stdout.lower()
        completed = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return any(name_fragment.lower() in Path(line.strip()).name.lower() for line in completed.stdout.splitlines())
