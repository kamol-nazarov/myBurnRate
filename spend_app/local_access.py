"""Per-install bearer for local API clients and the local API service."""
from __future__ import annotations

import json
import os
from pathlib import Path
import re
import secrets
import stat
import tempfile

from spend_app.config import default_app_dir
from spend_app.connection_paths import is_name_surrogate


def key_path(database_path: Path) -> Path:
    return Path(database_path).parent / ".burnrate-auth" / "access.key"


def _windows_user_sid() -> str:
    import ctypes as c
    from ctypes import wintypes as w

    kernel = c.WinDLL("kernel32", use_last_error=True)
    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel.GetCurrentProcess.restype = w.HANDLE
    kernel.CloseHandle.argtypes = [w.HANDLE]
    kernel.LocalFree.argtypes = [w.HLOCAL]
    kernel.LocalFree.restype = w.HLOCAL
    advapi.OpenProcessToken.argtypes = [w.HANDLE, w.DWORD, c.POINTER(w.HANDLE)]
    advapi.GetTokenInformation.argtypes = [
        w.HANDLE, c.c_int, c.c_void_p, w.DWORD, c.POINTER(w.DWORD)
    ]
    advapi.ConvertSidToStringSidW.argtypes = [c.c_void_p, c.POINTER(w.LPWSTR)]
    handle = w.HANDLE()
    if not advapi.OpenProcessToken(kernel.GetCurrentProcess(), 8, c.byref(handle)):
        raise c.WinError(c.get_last_error())
    try:
        length = w.DWORD()
        advapi.GetTokenInformation(handle, 1, None, 0, c.byref(length))
        if not length.value:
            raise c.WinError(c.get_last_error())
        data = c.create_string_buffer(length.value)
        if not advapi.GetTokenInformation(handle, 1, data, length, c.byref(length)):
            raise c.WinError(c.get_last_error())
        sid = c.cast(data, c.POINTER(c.c_void_p))[0]
        converted = w.LPWSTR()
        if not advapi.ConvertSidToStringSidW(sid, c.byref(converted)):
            raise c.WinError(c.get_last_error())
        try:
            return converted.value
        finally:
            kernel.LocalFree(c.cast(converted, w.HLOCAL))
    finally:
        kernel.CloseHandle(handle)


def _restrict_windows(path: Path, *, directory: bool) -> None:
    import ctypes as c
    from ctypes import wintypes as w

    advapi = c.WinDLL("advapi32", use_last_error=True)
    kernel = c.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [w.HLOCAL]
    kernel.LocalFree.restype = w.HLOCAL
    advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        w.LPCWSTR, w.DWORD, c.POINTER(c.c_void_p), c.POINTER(w.DWORD)
    ]
    advapi.SetFileSecurityW.argtypes = [w.LPCWSTR, w.DWORD, c.c_void_p]
    inherit = "OICI" if directory else ""
    principals = dict.fromkeys((_windows_user_sid(), "SY", "BA"))
    acl = "D:P" + "".join(f"(A;{inherit};FA;;;{sid})" for sid in principals)
    descriptor = c.c_void_p()
    if not advapi.ConvertStringSecurityDescriptorToSecurityDescriptorW(
        acl, 1, c.byref(descriptor), None
    ):
        raise c.WinError(c.get_last_error())
    try:
        # DACL_SECURITY_INFORMATION | PROTECTED_DACL_SECURITY_INFORMATION
        if not advapi.SetFileSecurityW(str(path), 0x80000004, descriptor):
            raise c.WinError(c.get_last_error())
    finally:
        kernel.LocalFree(descriptor)


def _restrict(path: Path, *, directory: bool) -> None:
    information = path.lstat()
    mode = stat.S_ISDIR if directory else stat.S_ISREG
    if not mode(information.st_mode) or is_name_surrogate(path):
        raise PermissionError("Local credential storage must use ordinary filesystem entries.")
    if os.name == "nt":
        _restrict_windows(path, directory=directory)
    else:
        if information.st_uid != os.geteuid():
            raise PermissionError("Local credential storage must be owned by the current user.")
        os.chmod(path, 0o700 if directory else 0o600, follow_symlinks=False)


_PRIVATE_SUBDIRS = (".burnrate-auth", "backups", "logs", "run", "updates", "snapshots")
_PRIVATE_FILES = ("native-window.json", ".env")


def protect_file(path: Path) -> None:
    """Apply private owner-only ACL/mode to a single existing regular file."""
    target = Path(path)
    try:
        info = target.lstat()
    except OSError:
        return
    if stat.S_ISREG(info.st_mode) and not is_name_surrogate(target):
        _restrict(target, directory=False)


def protect_directory_tree(root: Path) -> int:
    """Recursively apply private ACL/mode to ``root`` and ordinary descendants.

    Skips symbolic links, junctions, and entries that disappear concurrently.
    Returns the number of protected entries.
    """
    target = Path(root)
    try:
        info = target.lstat()
    except OSError:
        return 0
    if not stat.S_ISDIR(info.st_mode) or is_name_surrogate(target):
        raise PermissionError("Local credential storage must use ordinary filesystem entries.")
    _restrict(target, directory=True)
    protected = 1
    stack = [target]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            child = Path(entry.path)
            try:
                child_info = child.lstat()
            except OSError:
                continue
            if is_name_surrogate(child):
                continue
            if stat.S_ISDIR(child_info.st_mode):
                try:
                    _restrict(child, directory=True)
                    protected += 1
                    stack.append(child)
                except OSError:
                    continue
            elif stat.S_ISREG(child_info.st_mode):
                try:
                    _restrict(child, directory=False)
                    protected += 1
                except OSError:
                    continue
    return protected


def protect_data_tree(database_path: Path, *, protect_root: bool | None = None) -> int:
    """Harden BURNRATE's database, private files, and managed subfolders recursively.

    On upgraded Windows installs, protecting the parent folder alone does not
    strip inherited ACEs (such as sandbox user groups) from pre-existing
    ``backups/`` or ``logs/`` subfolders. Walking managed subfolders fixes that
    without touching unrelated files when a custom database path is used.
    """
    db_path = Path(database_path)
    data_dir = db_path.parent
    protected = 0
    is_default_root = False
    try:
        is_default_root = data_dir.resolve() == default_app_dir().resolve()
    except OSError:
        is_default_root = False
    should_protect_root = is_default_root if protect_root is None else protect_root
    if should_protect_root and data_dir.exists():
        info = data_dir.lstat()
        if not stat.S_ISDIR(info.st_mode) or is_name_surrogate(data_dir):
            raise PermissionError("Local credential storage must use ordinary filesystem entries.")
        _restrict(data_dir, directory=True)
        protected += 1
    for candidate in (
        db_path,
        Path(str(db_path) + "-wal"),
        Path(str(db_path) + "-shm"),
        *(data_dir / name for name in _PRIVATE_FILES),
    ):
        try:
            info = candidate.lstat()
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode) and not is_name_surrogate(candidate):
            _restrict(candidate, directory=False)
            protected += 1
    for name in _PRIVATE_SUBDIRS:
        subdir = data_dir / name
        if not subdir.exists():
            continue
        try:
            if is_name_surrogate(subdir):
                continue
            protected += protect_directory_tree(subdir)
        except OSError:
            continue
    return protected


def read_local_key(database_path: Path) -> str | None:
    path = key_path(database_path)
    try:
        if is_name_surrogate(path.parent) or is_name_surrogate(path):
            raise PermissionError("Local credential storage must not be redirected.")
        flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        with os.fdopen(os.open(path, flags), "rb") as source:
            information = os.fstat(source.fileno())
            if not stat.S_ISREG(information.st_mode) or (
                os.name != "nt" and (information.st_uid != os.geteuid()
                                     or stat.S_IMODE(information.st_mode) != 0o600)
            ):
                raise PermissionError("The local credential file must be private.")
            if information.st_size not in {64, 65, 66}:
                raise ValueError("The local credential file is invalid.")
            value = source.read(67)
    except FileNotFoundError:
        return None
    if not re.fullmatch(rb"[0-9a-f]{64}(?:\r?\n)?", value):
        raise ValueError("The local credential file is invalid.")
    return value[:64].decode("ascii")


def ensure_local_key(database_path: Path) -> str:
    path = key_path(database_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    protect_data_tree(database_path)
    _restrict(path.parent, directory=True)
    if not path.exists():
        descriptor, name = tempfile.mkstemp(dir=path.parent)
        pending = Path(name)
        try:
            _restrict(pending, directory=False)
            with os.fdopen(descriptor, "wb", closefd=False) as stream:
                stream.write(secrets.token_hex(32).encode("ascii") + b"\n")
                stream.flush()
                os.fsync(descriptor)
            os.close(descriptor)
            descriptor = -1
            try:
                os.link(pending, path)
            except FileExistsError:
                pass
        finally:
            if descriptor != -1:
                os.close(descriptor)
            pending.unlink(missing_ok=True)
    _restrict(path, directory=False)
    token = read_local_key(database_path)
    if token is None:
        raise FileNotFoundError("The local credential file is unavailable.")
    return token


def remove_hub_credentials(database_path: Path) -> None:
    """Discard obsolete hub-managed machine credentials on a paired desktop."""
    from spend_app.db import connect
    from spend_app.setup_wizard import read_setup_state

    with connect(database_path) as connection:
        if read_setup_state(connection).get("role") != "spoke":
            return
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key='machines.v1'"
        ).fetchone()
        if row is None:
            return
        state = json.loads(row[0])
        machines = state.get("machines", {})
        retained = {identifier: record for identifier, record in machines.items()
                    if not isinstance(record, dict) or not record.get("token_hash")}
        if retained == machines:
            return
        state["machines"] = retained
        connection.execute("UPDATE app_meta SET value=? WHERE key='machines.v1'",
                           (json.dumps(state, separators=(",", ":")),))
