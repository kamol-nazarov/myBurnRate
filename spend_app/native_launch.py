"""Trust checks for the desktop HUD binary before the local API launches it.

``/api/hud/native`` starts a fixed-path executable so the browser UI can bring
up the desktop window. The paths are fixed, but anyone who can write to one of
them can put a different binary there. Before spawning, verify the file's
Authenticode signature and refuse a signed-but-foreign or tampered binary.

Release builds are unsigned while code signing is pending, so an unsigned file
is allowed by default and reported in the response. Set
``BURNRATE_REQUIRE_SIGNED_NATIVE=1`` to refuse unsigned binaries too. The
publisher allowlist defaults to SignPath (the release signer) and the project
name for locally signed builds; override it with
``BURNRATE_NATIVE_PUBLISHERS`` (comma-separated substrings of the certificate
subject).
"""

from __future__ import annotations

import base64
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path

LOG = logging.getLogger("burnrate.native_launch")

PUBLISHERS_ENV = "BURNRATE_NATIVE_PUBLISHERS"
REQUIRE_SIGNED_ENV = "BURNRATE_REQUIRE_SIGNED_NATIVE"
DEFAULT_PUBLISHERS = ("SignPath Foundation", "myBurnRate")

VALID = "valid"
UNSIGNED = "unsigned"
UNTRUSTED = "untrusted"
FOREIGN = "foreign"
TAMPERED = "tampered"
UNKNOWN = "unknown"

# A valid signature from someone else, or a signature that no longer matches
# the file, is refused even without strict mode.
BLOCKED_ALWAYS = frozenset({FOREIGN, TAMPERED})


@contextmanager
def locked_binary(path: Path):
    """Hold the executable without write/delete sharing across verify and launch.

    Windows only; a no-op elsewhere. ``CreateFileW`` with ``FILE_SHARE_READ``
    denies other processes write or delete access, so the file cannot be
    swapped between the Authenticode check and ``CreateProcess``.
    """
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    generic_read = 0x80000000
    share_read = 0x00000001
    open_existing = 3
    attribute_normal = 0x80
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    close = kernel32.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(
        str(path), generic_read, share_read, None, open_existing, attribute_normal, None,
    )
    if handle in (None, -1, ctypes.c_void_p(-1).value):
        raise OSError(ctypes.get_last_error(), "Could not lock the desktop binary for verification")
    try:
        yield
    finally:
        close(handle)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def _publishers() -> tuple[str, ...]:
    configured = os.getenv(PUBLISHERS_ENV, "")
    names = tuple(part.strip() for part in configured.split(",") if part.strip())
    return names or DEFAULT_PUBLISHERS


def _powershell() -> str | None:
    """Resolve PowerShell by absolute OS path, never through PATH or the CWD."""
    if sys.platform != "win32":
        return None
    candidates = [
        Path(os.environ.get("SystemRoot", r"C:\Windows"))
        / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe",
        Path(os.environ.get("ProgramFiles", r"C:\Program Files"))
        / "PowerShell" / "7" / "pwsh.exe",
    ]
    return next((str(candidate) for candidate in candidates if candidate.is_file()), None)


def _authenticode(path: Path) -> tuple[str, str]:
    """Return (status, certificate subject) for ``path``."""
    shell = _powershell()
    if shell is None:
        raise RuntimeError("PowerShell is not available for a signature check")
    quoted = str(path).replace("'", "''")
    script = (
        f"$sig = Get-AuthenticodeSignature -LiteralPath '{quoted}'; "
        "$subject = if ($sig.SignerCertificate) { $sig.SignerCertificate.Subject } else { '' }; "
        "[pscustomobject]@{ status = $sig.Status.ToString(); subject = $subject } "
        "| ConvertTo-Json -Compress"
    )
    encoded = base64.b64encode(script.encode("utf-16-le")).decode("ascii")
    completed = subprocess.run(
        [shell, "-NoProfile", "-NonInteractive", "-EncodedCommand", encoded],
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    if completed.returncode != 0:
        raise RuntimeError((completed.stderr or "signature check failed").strip()[:200])
    report = json.loads(completed.stdout or "{}")
    return str(report.get("status") or ""), str(report.get("subject") or "")


def signature_state(path: Path) -> str:
    """Classify the binary's Authenticode signature."""
    status, subject = _authenticode(path)
    normalized = status.strip().casefold()
    if normalized == "valid":
        haystack = subject.casefold()
        trusted = any(name.casefold() in haystack for name in _publishers())
        return VALID if trusted else FOREIGN
    if normalized in {"notsigned", "notsupported"}:
        return UNSIGNED
    if normalized == "hashmismatch":
        return TAMPERED
    if normalized in {"nottrusted", "unknownerror"}:
        # A self-signed local build: not a publisher we know, but not tampered.
        return UNTRUSTED
    return UNKNOWN


def evaluate(path: Path) -> tuple[bool, str]:
    """Decide whether ``path`` may be launched, and report its signature state.

    Returns ``(allowed, state)``. Verification failures are reported as
    ``unknown``: allowed by default so a missing PowerShell cannot break the
    HUD, refused when ``BURNRATE_REQUIRE_SIGNED_NATIVE`` is set.
    """
    strict = _truthy(os.getenv(REQUIRE_SIGNED_ENV))
    try:
        state = signature_state(path)
    except Exception as exc:  # pragma: no cover - depends on the host shell
        LOG.warning("Could not verify the signature of %s: %s", path, exc)
        state = UNKNOWN
    if state == VALID:
        return True, state
    if state in BLOCKED_ALWAYS:
        LOG.warning("Refusing to launch %s: %s signature", path, state)
        return False, state
    if strict:
        LOG.warning(
            "Refusing to launch %s: %s signature and %s is set",
            path,
            state,
            REQUIRE_SIGNED_ENV,
        )
        return False, state
    if state == UNSIGNED:
        LOG.warning(
            "Launching an unsigned desktop binary at %s; set %s=1 to refuse it",
            path,
            REQUIRE_SIGNED_ENV,
        )
    return True, state
