"""Shared BURNRATE hub/account/bridge error codes and safe user messages."""

from __future__ import annotations

import math
import ssl
from typing import Mapping

from fastapi.responses import JSONResponse

HUB_PROTOCOL_VERSION = 2

ERROR_CATALOG: dict[str, dict[str, object]] = {
    "NET-001": {
        "message": "Can't reach your hub. Check that this computer is on your network.",
        "status": 503,
    },
    "NET-002": {
        "message": "Your hub didn't accept this app's address.",
        "status": 403,
    },
    "NET-003": {
        "message": "Your hub's security certificate couldn't be checked.",
        "status": 502,
    },
    "AUTH-101": {
        "message": "That email and password don't match.",
        "status": 401,
    },
    "AUTH-102": {
        "message": "Too many attempts. Try again in N minutes.",
        "status": 429,
    },
    "AUTH-103": {
        "message": "An account already exists. Sign in instead.",
        "status": 403,
    },
    "AUTH-104": {
        "message": "No account yet. Create one first.",
        "status": 401,
    },
    "AUTH-105": {
        "message": "Use a password of at least 12 characters.",
        "status": 422,
    },
    "AUTH-106": {
        "message": "Enter a valid email address.",
        "status": 422,
    },
    "AUTH-107": {
        "message": "You've been signed out. Sign in again.",
        "status": 401,
    },
    "DEV-301": {
        "message": "This computer was removed from your account. Sign in to add it again.",
        "status": 401,
    },
    "DEV-302": {
        "message": "Another computer already uses this name. Pick a different name.",
        "status": 422,
    },
    "REQ-401": {
        "message": "That request was too large.",
        "status": 413,
    },
    "REQ-404": {
        "message": "That page isn't here. BURNRATE runs in the desktop app.",
        "status": 404,
    },
    "SYNC-201": {
        "message": "Signed in, but the first sync didn't finish. It will retry.",
        "status": 502,
    },
    "HUB-501": {
        "message": "Your hub had a problem. Try again later.",
        "status": 502,
    },
    "HUB-502": {
        "message": "Your hub needs updating to work with this version of BURNRATE.",
        "status": 426,
    },
    "APP-901": {
        "message": "BURNRATE's background service isn't running. Restart the app.",
        "status": 503,
    },
    "APP-902": {
        "message": "Update BURNRATE to work with your hub.",
        "status": 426,
    },
    "APP-903": {
        "message": "This computer's keychain couldn't save your sign-in. Check OS keychain access and try again.",
        "status": 503,
    },
}


def default_status_for_code(code: str) -> int:
    entry = ERROR_CATALOG.get(code)
    if entry is None:
        return 502
    return int(entry["status"])


def error_message(code: str, *, retry_after: int | None = None) -> str:
    if code == "AUTH-102":
        if retry_after is not None:
            try:
                seconds = int(retry_after)
            except (TypeError, ValueError):
                seconds = 0
            if seconds > 0:
                minutes = max(1, math.ceil(seconds / 60))
                unit = "minute" if minutes == 1 else "minutes"
                return f"Too many attempts. Try again in {minutes} {unit}."
        return "Too many attempts. Try again in a few minutes."
    entry = ERROR_CATALOG.get(code)
    if entry is None:
        return str(ERROR_CATALOG["HUB-501"]["message"])
    return str(entry["message"])


def format_error(code: str, *, retry_after: int | None = None) -> str:
    resolved = code if code in ERROR_CATALOG else "HUB-501"
    return f"{error_message(resolved, retry_after=retry_after)} Error {resolved}"


def error_payload(
    code: str,
    *,
    retry_after: int | None = None,
    include_message: bool = False,
) -> dict[str, object]:
    resolved = code if code in ERROR_CATALOG else "HUB-501"
    inner: dict[str, object] = {"code": resolved}
    if include_message:
        inner["message"] = error_message(resolved, retry_after=retry_after)
    if retry_after is not None:
        try:
            seconds = int(retry_after)
        except (TypeError, ValueError):
            seconds = 0
        if seconds > 0:
            inner["retryAfter"] = seconds
    return {"error": inner}


def error_response(
    code: str,
    *,
    status_code: int | None = None,
    headers: Mapping[str, str] | None = None,
    retry_after: int | None = None,
    include_message: bool = False,
) -> JSONResponse:
    resolved = code if code in ERROR_CATALOG else "HUB-501"
    status = int(status_code if status_code is not None else default_status_for_code(resolved))
    merged_headers = {"Cache-Control": "no-store", **(dict(headers) if headers else {})}
    if resolved == "AUTH-102" and retry_after is not None and "Retry-After" not in merged_headers:
        try:
            seconds = max(1, int(retry_after))
        except (TypeError, ValueError):
            seconds = 1
        merged_headers["Retry-After"] = str(seconds)
    return JSONResponse(
        error_payload(resolved, retry_after=retry_after, include_message=include_message),
        status_code=status,
        headers=merged_headers,
    )


class CodedError(ValueError):
    """Structured error carrying an explicit BURNRATE error code without raw text."""

    def __init__(
        self,
        code: str,
        *,
        status_code: int | None = None,
        retry_after: int | None = None,
    ):
        resolved = code if code in ERROR_CATALOG else "HUB-501"
        self.code = resolved
        self.status_code = int(
            status_code if status_code is not None else default_status_for_code(resolved)
        )
        self.retry_after = retry_after
        super().__init__(format_error(resolved, retry_after=retry_after))


def _parse_retry_after(
    data: object, headers: Mapping[str, str] | None = None
) -> int | None:
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict) and err.get("retryAfter") is not None:
            try:
                val = int(err["retryAfter"])
                if val > 0:
                    return val
            except (TypeError, ValueError):
                pass
    if headers:
        for key, value in headers.items():
            if str(key).lower() == "retry-after":
                try:
                    val = int(str(value).strip())
                    if val > 0:
                        return val
                except (TypeError, ValueError):
                    pass
    return None


def extract_error_code(
    data: object,
    *,
    headers: Mapping[str, str] | None = None,
) -> tuple[str | None, int | None]:
    retry_after = _parse_retry_after(data, headers)
    if isinstance(data, dict):
        err = data.get("error")
        if isinstance(err, dict):
            code = err.get("code")
            if isinstance(code, str) and code in ERROR_CATALOG:
                return code, retry_after
        code_top = data.get("code")
        if isinstance(code_top, str) and code_top in ERROR_CATALOG:
            return code_top, retry_after
    return None, retry_after


_TLS_MARKERS = (
    "ssl",
    "tls",
    "certificate",
    "cert_verify",
    "wrong version number",
    "handshake",
    "self-signed",
    "self signed",
    "hostname mismatch",
    "pkix",
    "x509",
)


def is_tls_exception(exc: BaseException) -> bool:
    seen: set[int] = set()
    cur: BaseException | None = exc
    while cur is not None and id(cur) not in seen:
        seen.add(id(cur))
        if isinstance(cur, ssl.SSLError):
            return True
        name = type(cur).__name__.lower()
        text = str(cur).lower()
        if any(marker in name or marker in text for marker in _TLS_MARKERS):
            return True
        cur = cur.__cause__ or cur.__context__
    return False


def map_transport_error(exc: BaseException) -> tuple[str, int]:
    if isinstance(exc, CodedError):
        return exc.code, exc.status_code
    if is_tls_exception(exc):
        return "NET-003", default_status_for_code("NET-003")
    return "NET-001", default_status_for_code("NET-001")


def map_http_error(
    status_code: int,
    data: object = None,
    *,
    headers: Mapping[str, str] | None = None,
    endpoint_path: str = "",
    has_machine_token: bool = False,
    local_service: bool = False,
) -> CodedError:
    code, retry_after = extract_error_code(data, headers=headers)
    if code is not None:
        return CodedError(code, status_code=status_code, retry_after=retry_after)

    raw = ""
    if isinstance(data, dict):
        raw_err = data.get("error")
        if isinstance(raw_err, str):
            raw = raw_err
        elif isinstance(data.get("detail"), str):
            raw = str(data["detail"])
    raw_low = raw.lower()

    if status_code == 426:
        if "client" in raw_low or "update burnrate" in raw_low:
            return CodedError("APP-902", status_code=426)
        return CodedError("HUB-502", status_code=426)

    if status_code == 429:
        return CodedError("AUTH-102", status_code=429, retry_after=retry_after)

    if status_code == 403:
        if (
            "unrecognized host" in raw_low
            or "host header" in raw_low
            or "burnrate origin" in raw_low
            or "csrf" in raw_low
        ):
            return CodedError("NET-002", status_code=403)
        if (
            "already has an account" in raw_low
            or "setup token" in raw_low
            or "burnrate_setup_token" in raw_low
            or "owner account on your hub" in raw_low
            or "bootstrap authorization" in raw_low
            or endpoint_path == "/api/account/register"
        ):
            return CodedError("AUTH-103", status_code=403)
        return CodedError("AUTH-107", status_code=403)

    path_only = endpoint_path.split("?", 1)[0]
    if status_code == 401:
        if (
            has_machine_token
            or path_only.startswith("/api/machines")
            or "/machine" in path_only
            or "machine token" in raw_low
        ):
            return CodedError("DEV-301", status_code=401)
        if endpoint_path in {"/api/account/login", "/api/account/register"}:
            return CodedError("AUTH-101", status_code=401)
        return CodedError("AUTH-107", status_code=401)

    if status_code in (409, 422):
        if "12 characters" in raw_low or "password" in raw_low:
            return CodedError("AUTH-105", status_code=status_code)
        if "email" in raw_low or "username" in raw_low:
            return CodedError("AUTH-106", status_code=status_code)
        if (
            "already exists" in raw_low
            or "machine name" in raw_low
            or "multiple or revoked" in raw_low
            or path_only == "/api/machines"
        ):
            return CodedError("DEV-302", status_code=status_code)
        if "https" in raw_low:
            return CodedError("NET-003", status_code=status_code)
        if "hub's address" in raw_low or "hub address" in raw_low:
            return CodedError("NET-001", status_code=status_code)
        return CodedError(
            "AUTH-106" if endpoint_path in {"/api/account/login", "/api/account/register", "/api/setup/account"} else "HUB-501",
            status_code=status_code,
        )

    if status_code >= 500:
        default_code = (
            "APP-901"
            if local_service
            and not endpoint_path.startswith(("/api/setup/account", "/api/account/"))
            else "HUB-501"
        )
        return CodedError(default_code, status_code=502 if not local_service else status_code)

    return CodedError("APP-901" if local_service else "HUB-501", status_code=status_code)


def check_protocol_version(raw_header: str | None) -> str | None:
    """Validate an incoming X-BURNRATE-Protocol header if provided.

    Returns an error code ('APP-902' or 'HUB-502') on mismatch, or None if
    absent or compatible.
    """
    if raw_header is None or not str(raw_header).strip():
        return None
    try:
        client_version = int(str(raw_header).strip())
    except (TypeError, ValueError):
        return "APP-902"
    if client_version < HUB_PROTOCOL_VERSION:
        return "APP-902"
    if client_version > HUB_PROTOCOL_VERSION:
        return "HUB-502"
    return None
