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


def _windows_processes() -> list[tuple[int, str]] | None:
    """(pid, executable name) of every running process, read with the Toolhelp API.

    Windows only. Reading the list in-process starts no program, so nothing can
    flash a window, and it costs milliseconds where PowerShell or tasklist cost
    a fresh process each time. None when the list cannot be read.
    """
    import ctypes
    from ctypes import wintypes

    class ProcessEntry(ctypes.Structure):
        _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD), ("th32ProcessID", wintypes.DWORD),
                    ("th32DefaultHeapID", ctypes.c_size_t), ("th32ModuleID", wintypes.DWORD),
                    ("cntThreads", wintypes.DWORD), ("th32ParentProcessID", wintypes.DWORD),
                    ("pcPriClassBase", ctypes.c_long), ("dwFlags", wintypes.DWORD),
                    ("szExeFile", ctypes.c_wchar * 260)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    kernel32.Process32FirstW.argtypes = kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    snapshot = kernel32.CreateToolhelp32Snapshot(0x2, 0)  # TH32CS_SNAPPROCESS
    if not snapshot or snapshot == wintypes.HANDLE(-1).value:
        return None
    entry = ProcessEntry()
    entry.dwSize = ctypes.sizeof(ProcessEntry)
    processes = []
    try:
        more = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while more:
            processes.append((entry.th32ProcessID, entry.szExeFile))
            more = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return processes


def _windows_command_line(pid: int) -> str | None:
    """A process's command line (Windows 8.1 and later); None when it cannot be read."""
    import ctypes
    from ctypes import wintypes

    class UnicodeString(ctypes.Structure):
        _fields_ = [("Length", ctypes.c_ushort), ("MaximumLength", ctypes.c_ushort), ("Buffer", ctypes.c_void_p)]

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    ntdll = ctypes.WinDLL("ntdll")
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    ntdll.NtQueryInformationProcess.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.ULONG,
                                                ctypes.POINTER(wintypes.ULONG)]
    ntdll.NtQueryInformationProcess.restype = ctypes.c_long
    handle = kernel32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not handle:
        return None
    try:
        # A command line holds at most 32,767 characters.
        buffer = ctypes.create_string_buffer(ctypes.sizeof(UnicodeString) + 65_536)
        written = wintypes.ULONG(0)
        status = ntdll.NtQueryInformationProcess(handle, 60, buffer, len(buffer), ctypes.byref(written))
        if status != 0:  # 60 = ProcessCommandLineInformation
            return None
        text = UnicodeString.from_buffer(buffer)
        return ctypes.wstring_at(text.Buffer, text.Length // 2) if text.Buffer else ""
    finally:
        kernel32.CloseHandle(handle)


def process_command_lines(name_fragment: str, *, timeout: float = 8) -> list[str]:
    """Command lines of running processes whose executable name contains ``name_fragment``.

    Used for local discovery only; the lines are kept in memory and never logged.
    """
    fragment = name_fragment.lower()
    if os.name == "nt":
        try:
            processes = _windows_processes() or []
            lines = [_windows_command_line(pid) for pid, name in processes if fragment in name.lower()]
        except (OSError, AttributeError, ValueError):
            return []
        return [line.strip() for line in lines if line and line.strip()]
    try:
        completed = subprocess.run(["ps", "-A", "-o", "args="], capture_output=True, text=True,
                                   timeout=timeout, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return []
    lines = [line.strip() for line in completed.stdout.splitlines() if line.strip()]
    return [line for line in lines if fragment in Path(line.split(" ", 1)[0]).name.lower()]


def process_running(name_fragment: str) -> bool | None:
    """True or False when the process list could be read; None when it could not."""
    if os.name == "nt":
        try:
            processes = _windows_processes()
        except (OSError, AttributeError, ValueError):
            return None
        if processes is None:
            return None
        image = name_fragment.lower() + ".exe"
        return any(name.lower() == image for _, name in processes)
    try:
        completed = subprocess.run(["ps", "-A", "-o", "comm="], capture_output=True, text=True, timeout=5, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None
    if completed.returncode != 0:
        return None
    return any(name_fragment.lower() in Path(line.strip()).name.lower() for line in completed.stdout.splitlines())
