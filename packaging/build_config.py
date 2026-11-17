"""Generate ``spend_app/_build_config.py`` at build time from ``BURNRATE_BUILTIN_HUB_URL``.

Pure standard library so both isolated PEP 517 wheel builds (Hatchling) and
the native PyInstaller build can run it with no third-party dependencies.
"""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlsplit

BUILTIN_HUB_ENV = "BURNRATE_BUILTIN_HUB_URL"


def validate_builtin_hub_url(value: str | None) -> str:
    """Validate and normalize ``BURNRATE_BUILTIN_HUB_URL``.

    Empty / unset values return ``""`` (no built-in hub). Any non-empty value
    must be an ``https://`` origin with a host and no path, credentials,
    query string, or fragment.
    """
    raw = (value or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if parsed.scheme.lower() != "https" or not parsed.hostname:
        raise ValueError(
            f"{BUILTIN_HUB_ENV} must be an https:// origin with a host (got {raw!r})."
        )
    if parsed.username or parsed.password:
        raise ValueError(f"{BUILTIN_HUB_ENV} must not contain credentials.")
    if parsed.path not in ("", "/") or parsed.query or parsed.fragment:
        raise ValueError(
            f"{BUILTIN_HUB_ENV} must be a bare origin with no path, query, or fragment."
        )
    try:
        port = parsed.port
    except ValueError as exc:
        raise ValueError(f"{BUILTIN_HUB_ENV} has an invalid port.") from exc
    if port is not None and not (1 <= port <= 65535):
        raise ValueError(f"{BUILTIN_HUB_ENV} has an out-of-range port.")
    return f"https://{parsed.netloc.lower()}"


def render_build_config(url: str) -> str:
    clean = validate_builtin_hub_url(url)
    return (
        '"""Generated at build time; do not commit."""\n\n'
        f"BUILTIN_HUB_URL = {clean!r}\n"
    )


def write_build_config(path: Path, url: str | None = None) -> tuple[Path, str]:
    raw = os.environ.get(BUILTIN_HUB_ENV, "") if url is None else url
    clean = validate_builtin_hub_url(raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(render_build_config(clean), encoding="utf-8")
    return path, clean
