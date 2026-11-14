"""Self-update for the native Windows app: a pinned feed, a verified download, a silent installer.

The host bridge calls three functions: :func:`update_status` (no network),
:func:`check_for_update` and :func:`install_update`. None of them raises.

Trust model, all of it checked before anything is executed:

* The feed and the installer come only from this repository's GitHub release assets. The
  feed URL is pinned, the installer URL must sit under the repository's release-download
  path, and redirects are followed by hand to GitHub's release-asset hosts over https.
* The installer's size and SHA-256 must equal the feed's, and its minisign signature (the
  format Tauri's signer writes) must verify against the public key pinned below, which is
  the same key as ``plugins.updater.pubkey`` in ``src-tauri/tauri.conf.json``. Ed25519 is
  implemented here in pure Python (RFC 8032) so no new dependency carries that decision.
* A running copy whose ``BURNRATE.exe`` is Authenticode-signed only accepts an installer
  signed by the same publisher; an update never downgrades from signed to unsigned.
* Only strictly newer semantic versions are offered. A file that fails any check is deleted.
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import io
import json
import os
import re
import subprocess
import sys
from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Protocol
from urllib.parse import urljoin, urlsplit

REPO = "kamol-nazarov/myBurnRate"
FEED_URL = f"https://github.com/{REPO}/releases/latest/download/native-latest.json"
RELEASES_URL = f"https://github.com/{REPO}/releases"
LATEST_PAGE_URL = f"{RELEASES_URL}/latest"
PLATFORM_KEY = "windows-x86_64"
INSTALL_MARKER = "BURNRATE.installed"
OPT_OUT_ENV = "BURNRATE_NO_UPDATE_CHECK"

# base64 of the minisign public key file; identical to plugins.updater.pubkey in tauri.conf.json.
PUBLIC_KEY_B64 = (
    "dW50cnVzdGVkIGNvbW1lbnQ6IG1pbmlzaWduIHB1YmxpYyBrZXk6IDVDQUNGNEMxNUMxRkNENkQKUldSdHpS"
    "OWN3ZlNzWE9yblR0Q1BIZUFTY3VhbWFFeHB2OWVkOVZhUjF5dVNaQm1CWHZTM0I3WkMK"
)

MAX_FEED_BYTES = 256 * 1024
MAX_INSTALLER_BYTES = 300 * 1024 * 1024
MAX_REDIRECTS = 5
MAX_NOTES = 4000
CHUNK = 256 * 1024

_DOWNLOAD_PATH = f"/{REPO}/releases/download/".casefold()
_RELEASES_PATH = f"/{REPO}/releases/".casefold()
_ASSET_HOSTS = frozenset(
    {"objects.githubusercontent.com", "release-assets.githubusercontent.com"}
)
_REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
_SILENT_UPDATE_ARGS = ("/S", "/UPDATE")


class UpdateError(Exception):
    """A problem worth telling the person about; the message is safe to show."""


class FeedError(UpdateError):
    pass


class SignatureError(UpdateError):
    pass


# ---------------------------------------------------------------------------------------
# Ed25519 (RFC 8032, section 5.1), verification only.
# ---------------------------------------------------------------------------------------

_P = 2**255 - 19
_L = 2**252 + 27742317777372353535851937790883648493
_D = (-121665 * pow(121666, _P - 2, _P)) % _P
_SQRT_M1 = pow(2, (_P - 1) // 4, _P)


def _inv(x: int) -> int:
    return pow(x, _P - 2, _P)


def _recover_x(y: int, sign: int) -> int | None:
    if y >= _P:
        return None
    x2 = (y * y - 1) * _inv(_D * y * y + 1) % _P
    if x2 == 0:
        return None if sign else 0
    x = pow(x2, (_P + 3) // 8, _P)
    if (x * x - x2) % _P:
        x = x * _SQRT_M1 % _P
    if (x * x - x2) % _P:
        return None
    if (x & 1) != sign:
        x = _P - x
    return x


Point = tuple[int, int, int, int]


def _decode_point(data: bytes) -> Point | None:
    """Decode a 32-byte point; ``None`` for any invalid or non-canonical encoding."""
    if len(data) != 32:
        return None
    value = int.from_bytes(data, "little")
    sign = value >> 255
    y = value & ((1 << 255) - 1)
    x = _recover_x(y, sign)
    if x is None:
        return None
    return (x, y, 1, x * y % _P)


def _add(p: Point, q: Point) -> Point:
    a = (p[1] - p[0]) * (q[1] - q[0]) % _P
    b = (p[1] + p[0]) * (q[1] + q[0]) % _P
    c = 2 * p[3] * q[3] * _D % _P
    d = 2 * p[2] * q[2] % _P
    e, f, g, h = b - a, d - c, d + c, b + a
    return (e * f % _P, g * h % _P, f * g % _P, e * h % _P)


_IDENTITY: Point = (0, 1, 1, 0)


def _mul(scalar: int, point: Point) -> Point:
    result = _IDENTITY
    addend = point
    while scalar:
        if scalar & 1:
            result = _add(result, addend)
        addend = _add(addend, addend)
        scalar >>= 1
    return result


def _same_point(p: Point, q: Point) -> bool:
    return (p[0] * q[2] - q[0] * p[2]) % _P == 0 and (
        p[1] * q[2] - q[1] * p[2]
    ) % _P == 0


_BASE_Y = 4 * _inv(5) % _P
_BASE_X = _recover_x(_BASE_Y, 0)
assert _BASE_X is not None
BASE: Point = (_BASE_X, _BASE_Y, 1, _BASE_X * _BASE_Y % _P)


def ed25519_verify(public_key: bytes, message: bytes, signature: bytes) -> bool:
    """RFC 8032 Ed25519 verification. Rejects S >= L and invalid point encodings."""
    if len(public_key) != 32 or len(signature) != 64:
        return False
    a_point = _decode_point(public_key)
    r_point = _decode_point(signature[:32])
    if a_point is None or r_point is None:
        return False
    s = int.from_bytes(signature[32:], "little")
    if s >= _L:
        return False
    k = (
        int.from_bytes(
            hashlib.sha512(signature[:32] + public_key + message).digest(), "little"
        )
        % _L
    )
    return _same_point(_mul(s, BASE), _add(r_point, _mul(k, a_point)))


# ---------------------------------------------------------------------------------------
# minisign (what Tauri's updater signer writes)
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class _PublicKey:
    key_id: bytes
    key: bytes


@dataclass(frozen=True)
class _Signature:
    algorithm: bytes
    key_id: bytes
    signature: bytes
    trusted_comment: bytes
    global_signature: bytes


def _b64(text: str, what: str) -> bytes:
    try:
        return base64.b64decode(text.strip(), validate=True)
    except (ValueError, TypeError):
        raise SignatureError(f"The {what} is not valid base64.") from None


def _lines(blob: bytes, what: str) -> list[str]:
    try:
        text = blob.decode("utf-8")
    except UnicodeDecodeError:
        raise SignatureError(f"The {what} is not text.") from None
    lines = [line.rstrip("\r") for line in text.split("\n")]
    while lines and not lines[-1]:
        lines.pop()
    return lines


def parse_public_key(public_key_b64: str) -> _PublicKey:
    lines = _lines(_b64(public_key_b64, "public key"), "public key")
    if len(lines) != 2 or not lines[0].startswith("untrusted comment:"):
        raise SignatureError("The public key file is malformed.")
    blob = _b64(lines[1], "public key")
    if len(blob) != 42 or blob[:2] != b"Ed":
        raise SignatureError("The public key is not an Ed25519 minisign key.")
    return _PublicKey(key_id=blob[2:10], key=blob[10:])


def parse_signature(signature_b64: str) -> _Signature:
    lines = _lines(_b64(signature_b64, "signature"), "signature")
    if len(lines) != 4:
        raise SignatureError("The signature file is malformed.")
    if not lines[0].startswith("untrusted comment:") or not lines[2].startswith(
        "trusted comment: "
    ):
        raise SignatureError("The signature file is malformed.")
    blob = _b64(lines[1], "signature")
    if len(blob) != 74:
        raise SignatureError("The signature has the wrong length.")
    global_signature = _b64(lines[3], "signature")
    if len(global_signature) != 64:
        raise SignatureError("The signature has the wrong length.")
    return _Signature(
        algorithm=blob[:2],
        key_id=blob[2:10],
        signature=blob[10:],
        trusted_comment=lines[2][len("trusted comment: ") :].encode("utf-8"),
        global_signature=global_signature,
    )


def trusted_fields(trusted_comment: bytes) -> dict[str, str]:
    """Tauri's signer writes ``timestamp:<unix>\tfile:<basename>[\tversion:<v>]``; first key wins."""
    fields: dict[str, str] = {}
    for part in trusted_comment.decode("utf-8", "replace").split("\t"):
        key, separator, value = part.partition(":")
        if separator and key not in fields:
            fields[key] = value
    return fields


def _check_minisign(
    signature_b64: str,
    public_key_b64: str,
    prehashed: Callable[[], bytes],
    raw: Callable[[], bytes],
    expected_file: str | None = None,
    expected_version: str | None = None,
) -> None:
    key = parse_public_key(public_key_b64)
    sig = parse_signature(signature_b64)
    if sig.key_id != key.key_id:
        raise SignatureError("The update is signed with a different key.")
    if sig.algorithm == b"ED":
        message = prehashed()
    elif sig.algorithm == b"Ed":
        message = raw()
    else:
        raise SignatureError("The update signature uses an unknown algorithm.")
    if not ed25519_verify(key.key, message, sig.signature):
        raise SignatureError("The update's signature does not match the file.")
    if not ed25519_verify(
        key.key, sig.signature + sig.trusted_comment, sig.global_signature
    ):
        raise SignatureError("The update's signature comment was altered.")
    # The global signature covers the trusted comment, so these fields are signed: one key
    # signs every BURNRATE installer, and this is what stops a validly signed *other* file
    # (the Tauri installer, an older native build) from passing as this one.
    fields = trusted_fields(sig.trusted_comment)
    if expected_file is not None and fields.get("file") != expected_file:
        raise SignatureError("The update's signature was made for a different file.")
    if (
        expected_version is not None
        and "version" in fields
        and _file_version(fields["version"]) != expected_version
    ):
        raise SignatureError("The update's signature was made for a different version.")


def verify_minisign(
    data: bytes,
    signature_b64: str,
    public_key_b64: str = PUBLIC_KEY_B64,
    *,
    expected_file: str | None = None,
    expected_version: str | None = None,
) -> None:
    """Verify ``data`` against a Tauri/minisign signature; raises :class:`SignatureError`.

    With ``expected_file`` the signed trusted comment must carry ``file:<expected_file>``.
    """
    _check_minisign(
        signature_b64,
        public_key_b64,
        lambda: hashlib.blake2b(data, digest_size=64).digest(),
        lambda: data,
        expected_file,
        expected_version,
    )


def verify_minisign_file(
    path: Path,
    signature_b64: str,
    public_key_b64: str = PUBLIC_KEY_B64,
    *,
    expected_file: str | None = None,
    expected_version: str | None = None,
) -> None:
    def prehashed() -> bytes:
        digest = hashlib.blake2b(digest_size=64)
        with open(path, "rb") as handle:
            while chunk := handle.read(CHUNK):
                digest.update(chunk)
        return digest.digest()

    def raw() -> bytes:
        if path.stat().st_size > MAX_INSTALLER_BYTES:
            raise SignatureError("The update file is too large.")
        return path.read_bytes()

    _check_minisign(
        signature_b64, public_key_b64, prehashed, raw, expected_file, expected_version
    )


# ---------------------------------------------------------------------------------------
# Versions (semver 2.0 precedence)
# ---------------------------------------------------------------------------------------

_SEMVER = re.compile(
    r"^v?(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)"
    r"(?:-((?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*)?$"
)
_PEP440 = re.compile(
    r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:(a|b|rc)(0|[1-9]\d*))?$"
)
_PEP440_WORDS = {"a": "alpha", "b": "beta", "rc": "rc"}

VersionKey = tuple[int, int, int, int, tuple[tuple[int, Any], ...]]


def parse_version(text: str) -> VersionKey:
    """Order key for a semantic version; a prerelease sorts below its release."""
    if not isinstance(text, str) or len(text) > 64:
        raise ValueError("invalid version")
    value = text.strip()
    match = _SEMVER.match(value)
    if match is None:
        legacy = _PEP440.match(value)
        if legacy is None:
            raise ValueError(f"invalid version: {text!r}")
        major, minor, patch, tag, number = legacy.groups()
        pre = f"{_PEP440_WORDS[tag]}.{int(number)}" if tag else None
        match_groups = (major, minor, patch, pre)
    else:
        match_groups = match.groups()
    major, minor, patch, pre = match_groups
    if pre is None:
        return (int(major), int(minor), int(patch), 1, ())
    identifiers = tuple(
        (0, int(part)) if part.isdigit() else (1, part) for part in pre.split(".")
    )
    return (int(major), int(minor), int(patch), 0, identifiers)


def is_newer(candidate: str, current: str) -> bool:
    return parse_version(candidate) > parse_version(current)


INSTALLER_PREFIX = "myBurnRate_"
INSTALLER_SUFFIX = "_x64-setup.exe"
# Dev builds before the rename used this prefix: their leftovers are cleaned, never accepted.
LEGACY_INSTALLER_PREFIX = "BURNRATE-native_"


def installer_name(version: str) -> str:
    """The one file name a native installer for ``version`` may have.

    It ends like the Tauri installer's name (``BURNRATE_<v>_x64-setup.exe``) but the prefixes
    differ, and the signed ``file:`` field plus the feed URL must both carry this exact name.
    """
    return f"{INSTALLER_PREFIX}{_file_version(version)}{INSTALLER_SUFFIX}"


def _file_version(text: str) -> str:
    """The part of a semantic version safe to use in a file name (no build metadata)."""
    return text.strip().removeprefix("v").split("+", 1)[0]


# ---------------------------------------------------------------------------------------
# Feed
# ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Asset:
    url: str
    signature: str
    sha256: str
    size: int


@dataclass(frozen=True)
class Feed:
    version: str
    notes: str
    asset: Asset


def _clean_notes(value: Any) -> str:
    if not isinstance(value, str):
        return ""
    text = "".join(ch for ch in value if ch == "\n" or ch >= " ")
    return text.strip()[:MAX_NOTES]


def validate_asset_url(url: Any) -> str:
    """The installer must be a plain https github.com release-download URL of this repository."""
    if not isinstance(url, str) or not url or len(url) > 2048:
        raise FeedError("The update has no download address.")
    if any(ord(ch) < 33 or ord(ch) > 126 for ch in url):
        raise FeedError("The update's download address is not allowed.")
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise FeedError("The update's download address is not allowed.") from None
    if (
        parts.scheme != "https"
        or parts.hostname != "github.com"
        or port not in (None, 443)
        or "@" in parts.netloc
        or parts.query
        or parts.fragment
        or "%" in parts.path
        or "\\" in parts.path
        or ".." in parts.path.split("/")
        or not parts.path.casefold().startswith(_DOWNLOAD_PATH)
        or len(parts.path) == len(_DOWNLOAD_PATH)
    ):
        raise FeedError("The update's download address is not allowed.")
    return url


def validate_redirect(url: str) -> str:
    """A redirect may only land on github.com's releases or a GitHub release-asset host, over https."""
    parts = urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        raise UpdateError("The download was redirected somewhere unexpected.") from None
    host = parts.hostname or ""
    allowed_host = host in _ASSET_HOSTS or (
        host == "github.com" and parts.path.casefold().startswith(_RELEASES_PATH)
    )
    if (
        parts.scheme != "https"
        or not allowed_host
        or port not in (None, 443)
        or "@" in parts.netloc
        or any(ord(ch) < 33 or ord(ch) > 126 for ch in url)
    ):
        raise UpdateError("The download was redirected somewhere unexpected.")
    return url


def parse_feed(raw: bytes) -> Feed:
    try:
        data = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise FeedError("The update feed is not valid JSON.") from None
    if not isinstance(data, dict):
        raise FeedError("The update feed is malformed.")
    version = data.get("version")
    if not isinstance(version, str):
        raise FeedError("The update feed has no version.")
    try:
        parse_version(version)
    except ValueError:
        raise FeedError("The update feed's version is not valid.") from None
    platforms = data.get("platforms")
    entry = platforms.get(PLATFORM_KEY) if isinstance(platforms, dict) else None
    if not isinstance(entry, dict):
        raise FeedError("The update feed has no Windows installer.")
    signature = entry.get("signature")
    sha256 = entry.get("sha256")
    size = entry.get("size")
    if not isinstance(signature, str) or not signature.strip() or len(signature) > 8192:
        raise FeedError("The update has no signature.")
    if not isinstance(sha256, str) or re.fullmatch(r"[0-9a-fA-F]{64}", sha256) is None:
        raise FeedError("The update has no valid checksum.")
    if (
        isinstance(size, bool)
        or not isinstance(size, int)
        or not 0 < size <= MAX_INSTALLER_BYTES
    ):
        raise FeedError("The update's size is not valid or is too large.")
    clean_version = version.strip().removeprefix("v")
    url = validate_asset_url(entry.get("url"))
    segments = urlsplit(url).path.split("/")
    # /<owner>/<repo>/releases/download/<tag>/<file>
    if (
        len(segments) != 7
        or segments[5] != f"v{_file_version(clean_version)}"
        or segments[6] != installer_name(clean_version)
    ):
        raise FeedError("The update's download is not this version's native installer.")
    return Feed(
        version=clean_version,
        notes=_clean_notes(data.get("notes")),
        asset=Asset(
            url=url,
            signature=signature.strip(),
            sha256=sha256.lower(),
            size=size,
        ),
    )


# ---------------------------------------------------------------------------------------
# HTTP: a small injectable surface so tests never touch the network
# ---------------------------------------------------------------------------------------


class Fetched(Protocol):
    status: int
    headers: Mapping[str, str]

    def iter_chunks(self, size: int) -> Iterator[bytes]: ...

    def close(self) -> None: ...


Fetcher = Callable[[str, float], Fetched]


class _HttpxResponse:
    def __init__(self, client: Any, response: Any) -> None:
        self._client = client
        self._response = response
        self.status = response.status_code
        self.headers = {key.lower(): value for key, value in response.headers.items()}

    def iter_chunks(self, size: int) -> Iterator[bytes]:
        return self._response.iter_bytes(size)

    def close(self) -> None:
        with contextlib.suppress(Exception):
            self._response.close()
        with contextlib.suppress(Exception):
            self._client.close()


def _httpx_fetch(url: str, timeout: float) -> Fetched:
    import ssl

    import certifi
    import httpx

    from spend_app import __version__

    context = ssl.create_default_context(cafile=certifi.where())
    client = httpx.Client(
        verify=context,
        timeout=timeout,
        follow_redirects=False,
        headers={
            "User-Agent": f"BURNRATE/{__version__}",
            "Accept": "application/octet-stream, application/json",
        },
    )
    try:
        return _HttpxResponse(
            client, client.send(client.build_request("GET", url), stream=True)
        )
    except BaseException:
        client.close()
        raise


def _download(
    url: str,
    *,
    fetch: Fetcher,
    timeout: float,
    max_bytes: int,
    sink: BinaryIO,
    on_chunk: Callable[[bytes], None] | None = None,
) -> int:
    """Stream ``url`` into ``sink``, following redirects by hand. Returns the byte count."""
    for _ in range(MAX_REDIRECTS + 1):
        response = fetch(url, timeout)
        try:
            if response.status in _REDIRECT_CODES:
                location = response.headers.get("location") or response.headers.get(
                    "Location"
                )
                if not location:
                    raise UpdateError(
                        "The download was redirected without a destination."
                    )
                url = validate_redirect(urljoin(url, location))
                continue
            if response.status != 200:
                raise UpdateError(
                    f"GitHub answered with HTTP status {response.status}."
                )
            declared = response.headers.get("content-length")
            if (
                declared is not None
                and declared.isdigit()
                and int(declared) > max_bytes
            ):
                raise UpdateError("The download is larger than allowed.")
            total = 0
            for chunk in response.iter_chunks(CHUNK):
                total += len(chunk)
                if total > max_bytes:
                    raise UpdateError("The download is larger than allowed.")
                sink.write(chunk)
                if on_chunk is not None:
                    on_chunk(chunk)
            return total
        finally:
            response.close()
    raise UpdateError("The download was redirected too many times.")


# ---------------------------------------------------------------------------------------
# Authenticode and process helpers (Windows)
# ---------------------------------------------------------------------------------------

_AUTHENTICODE_SCRIPT = (
    "$ErrorActionPreference='Stop';"
    "$s=Get-AuthenticodeSignature -LiteralPath $env:BURNRATE_AUTHCHECK_PATH;"
    "$c=$s.SignerCertificate;"
    "[pscustomobject]@{status=[string]$s.Status;"
    "thumbprint=$(if($c){$c.Thumbprint}else{''});"
    "subject=$(if($c){$c.Subject}else{''})}|ConvertTo-Json -Compress"
)


def powershell_authenticode(path: Path) -> dict[str, str] | None:
    """Authenticode status of ``path`` via a non-interactive PowerShell; the path is passed
    through the environment, never spliced into the command. ``None`` when it cannot be read."""
    if sys.platform != "win32":
        return None
    root = os.environ.get("SystemRoot") or r"C:\Windows"
    shell = Path(root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    env = dict(os.environ, BURNRATE_AUTHCHECK_PATH=str(path))
    try:
        result = subprocess.run(
            [
                str(shell),
                "-NoProfile",
                "-NonInteractive",
                "-ExecutionPolicy",
                "Bypass",
                "-Command",
                _AUTHENTICODE_SCRIPT,
            ],
            capture_output=True,
            text=True,
            timeout=60,
            env=env,
            creationflags=0x08000000,  # CREATE_NO_WINDOW
            check=False,
        )
        data = json.loads(result.stdout)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not isinstance(data, dict):
        return None
    return {
        key: str(data.get(key) or "") for key in ("status", "thumbprint", "subject")
    }


def check_signer_policy(
    app: dict[str, str] | None, installer: dict[str, str] | None
) -> None:
    """A signed app only accepts a validly signed installer from the same publisher.

    This is a secondary control. Certificates issued by SignPath Foundation share one subject
    across every project they sign, so a matching subject does not prove the installer is
    BURNRATE's; the minisign check against the pinned key (and the signed file name) is the
    primary one. What this adds is that a signed app never silently becomes an unsigned one.
    """
    if app is None:
        raise UpdateError(
            "Could not read this app's signature, so the update was not installed."
        )
    app_signed = app.get("status") != "NotSigned" and bool(
        app.get("subject") or app.get("thumbprint")
    )
    if not app_signed:
        return
    if installer is None:
        raise UpdateError("Could not verify the update's publisher signature.")
    same_signer = bool(installer.get("thumbprint")) and installer.get(
        "thumbprint"
    ) == app.get("thumbprint")
    same_subject = bool(installer.get("subject")) and installer.get(
        "subject"
    ) == app.get("subject")
    if installer.get("status") != "Valid" or not (same_signer or same_subject):
        raise UpdateError("The update is not signed by the same publisher as this app.")


def _launch_detached(path: Path, args: list[str]) -> None:
    flags = 0
    if sys.platform == "win32":
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    subprocess.Popen(
        [str(path), *args],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        cwd=str(path.parent),
        creationflags=flags,
    )


@contextlib.contextmanager
def _deny_writes(path: Path) -> Iterator[None]:
    """Hold the file open without write/delete sharing, so what was verified is what runs."""
    if sys.platform != "win32":
        yield
        return
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.CreateFileW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]  # fmt: skip
    kernel32.CreateFileW.restype = wintypes.HANDLE
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    handle = kernel32.CreateFileW(str(path), 0x80000000, 0x1, None, 3, 0, None)
    if handle in (None, wintypes.HANDLE(-1).value):
        raise OSError(ctypes.get_last_error(), "could not lock the downloaded update")
    try:
        yield
    finally:
        kernel32.CloseHandle(handle)


# ---------------------------------------------------------------------------------------
# The updater
# ---------------------------------------------------------------------------------------

Progress = Callable[[dict[str, Any]], None]


def _enabled(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes"}


class Updater:
    """All the moving parts injectable; the module-level functions use one default instance."""

    def __init__(
        self,
        *,
        fetcher: Fetcher | None = None,
        launcher: Callable[[Path, list[str]], None] | None = None,
        authenticode: Callable[[Path], dict[str, str] | None] | None = None,
        env: Mapping[str, str] | None = None,
        frozen: bool | None = None,
        executable: Path | None = None,
        platform: str | None = None,
        data_dir: Path | None = None,
        current_version: str | None = None,
        lock: Callable[[Path], contextlib.AbstractContextManager[None]] | None = None,
        public_key: str = PUBLIC_KEY_B64,
    ) -> None:
        self._fetcher = fetcher
        self._launcher = launcher or _launch_detached
        self._authenticode = authenticode or powershell_authenticode
        self._env = env
        self._frozen = frozen
        self._executable = executable
        self._platform = platform
        self._data_dir = data_dir
        self._current = current_version
        self._lock = lock or _deny_writes
        self._public_key = public_key

    # -- context -------------------------------------------------------------------

    def current_version(self) -> str:
        if self._current is not None:
            return self._current
        from spend_app import __version__

        return __version__

    def _environ(self) -> Mapping[str, str]:
        return self._env if self._env is not None else os.environ

    def _is_frozen(self) -> bool:
        return (
            bool(getattr(sys, "frozen", False))
            if self._frozen is None
            else self._frozen
        )

    def _exe(self) -> Path:
        return (
            self._executable if self._executable is not None else Path(sys.executable)
        )

    def _is_windows(self) -> bool:
        return (
            self._platform if self._platform is not None else sys.platform
        ) == "win32"

    def kind(self) -> str:
        if not self._is_frozen():
            return "source"
        try:
            marker = self._exe().parent / INSTALL_MARKER
            return "installer" if marker.is_file() else "portable"
        except OSError:
            return "portable"

    def can_auto_update(self) -> bool:
        return self.kind() == "installer" and self._is_windows()

    def _updates_dir(self) -> Path:
        if self._data_dir is not None:
            return self._data_dir / "updates"
        from spend_app.config import default_app_dir

        return default_app_dir() / "updates"

    def _fetch(self) -> Fetcher:
        return self._fetcher or _httpx_fetch

    # -- public surface ------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        try:
            return {
                "kind": self.kind(),
                "current": self.current_version(),
                "canAutoUpdate": self.can_auto_update(),
            }
        except Exception:  # noqa: BLE001 -- the contract is "never raises"
            return {"kind": "source", "current": "", "canAutoUpdate": False}

    def _result(self, **fields: Any) -> dict[str, Any]:
        base: dict[str, Any] = {
            "ok": True,
            "current": self.current_version(),
            "available": False,
            "version": "",
            "notes": "",
            "pageUrl": LATEST_PAGE_URL,
            "canInstall": False,
        }
        base.update(fields)
        return base

    def check(self, timeout: float = 10.0) -> dict[str, Any]:
        try:
            if _enabled(self._environ().get(OPT_OUT_ENV)):
                return self._result()
            feed = self._read_feed(timeout)
            if not is_newer(feed.version, self.current_version()):
                return self._result()
            return self._result(
                available=True,
                version=feed.version,
                notes=feed.notes,
                pageUrl=f"{RELEASES_URL}/tag/v{_file_version(feed.version)}",
                canInstall=self.can_auto_update(),
            )
        except Exception as exc:  # noqa: BLE001 -- the contract is "never raises"
            return self._result(
                ok=False, error=_describe(exc, "Could not check for updates.")
            )

    def install(self, progress: Progress | None = None) -> dict[str, Any]:
        try:
            return self._install(progress)
        except Exception as exc:  # noqa: BLE001 -- the contract is "never raises"
            return {
                "ok": False,
                "error": _describe(exc, "Could not install the update."),
            }

    # -- internals -----------------------------------------------------------------

    def _read_feed(self, timeout: float) -> Feed:
        sink = io.BytesIO()
        _download(
            FEED_URL,
            fetch=self._fetch(),
            timeout=timeout,
            max_bytes=MAX_FEED_BYTES,
            sink=sink,
        )
        return parse_feed(sink.getvalue())

    @staticmethod
    def _report(progress: Progress | None, phase: str, percent: float | None) -> None:
        if progress is None:
            return
        with contextlib.suppress(Exception):
            progress({"phase": phase, "percent": percent})

    def _install(self, progress: Progress | None) -> dict[str, Any]:
        if _enabled(self._environ().get(OPT_OUT_ENV)):
            raise UpdateError("Update checks are turned off on this computer.")
        if not self.can_auto_update():
            raise UpdateError(
                "This copy of BURNRATE cannot update itself. Download the new version instead."
            )
        self._report(progress, "checking", None)
        feed = self._read_feed(15.0)
        if not is_newer(feed.version, self.current_version()):
            raise UpdateError("BURNRATE is already up to date.")

        folder = self._updates_dir()
        folder.mkdir(parents=True, exist_ok=True)
        self._remove_stale(folder)
        name = installer_name(feed.version)
        final = folder / name
        part = folder / (name + ".part")
        launched = False
        try:
            self._download_asset(feed.asset, part, progress)
            os.replace(part, final)
            with self._lock(final):
                self._report(progress, "verifying", None)
                verify_minisign_file(
                    final,
                    feed.asset.signature,
                    self._public_key,
                    expected_file=name,
                    expected_version=_file_version(feed.version),
                )
                self._check_publisher(final)
                self._report(progress, "launching", None)
                self._launcher(final, list(_SILENT_UPDATE_ARGS))
                launched = True
        finally:
            if not launched:
                for leftover in (part, final):
                    with contextlib.suppress(OSError):
                        leftover.unlink()
        return {"ok": True, "restarting": True}

    def _download_asset(
        self, asset: Asset, destination: Path, progress: Progress | None
    ) -> None:
        digest = hashlib.sha256()
        seen = 0

        def on_chunk(chunk: bytes) -> None:
            nonlocal seen
            digest.update(chunk)
            seen += len(chunk)
            self._report(progress, "downloading", min(100.0, seen * 100.0 / asset.size))

        self._report(progress, "downloading", 0.0)
        with open(destination, "wb") as sink:
            total = _download(
                asset.url,
                fetch=self._fetch(),
                timeout=30.0,
                max_bytes=min(asset.size, MAX_INSTALLER_BYTES),
                sink=sink,
                on_chunk=on_chunk,
            )
        if total != asset.size:
            raise UpdateError("The downloaded update is not the expected size.")
        if digest.hexdigest() != asset.sha256:
            raise UpdateError("The downloaded update's checksum does not match.")

    def _check_publisher(self, installer: Path) -> None:
        app = self._authenticode(self._exe())
        check_signer_policy(
            app,
            None
            if app is None or app.get("status") == "NotSigned"
            else self._authenticode(installer),
        )

    @staticmethod
    def _remove_stale(folder: Path) -> None:
        for prefix in (INSTALLER_PREFIX, LEGACY_INSTALLER_PREFIX):
            for pattern in (
                f"{prefix}*{INSTALLER_SUFFIX}",
                f"{prefix}*{INSTALLER_SUFFIX}.part",
            ):
                for old in folder.glob(pattern):
                    with contextlib.suppress(OSError):
                        old.unlink()


def _describe(exc: Exception, fallback: str) -> str:
    if isinstance(exc, UpdateError):
        return str(exc) or fallback
    name = type(exc).__name__
    if "Timeout" in name:
        return "GitHub did not answer in time. Try again in a moment."
    if "Connect" in name or "Network" in name or isinstance(exc, OSError):
        return "Could not reach GitHub. Check your connection."
    return fallback


_DEFAULT = Updater()


def update_status() -> dict[str, Any]:
    return _DEFAULT.status()


def check_for_update(timeout: float = 10.0) -> dict[str, Any]:
    return _DEFAULT.check(timeout)


def install_update(progress: Progress | None = None) -> dict[str, Any]:
    return _DEFAULT.install(progress)


__all__ = [
    "FEED_URL",
    "INSTALL_MARKER",
    "PUBLIC_KEY_B64",
    "SignatureError",
    "Updater",
    "check_for_update",
    "ed25519_verify",
    "install_update",
    "is_newer",
    "parse_feed",
    "parse_version",
    "update_status",
    "verify_minisign",
    "verify_minisign_file",
]
