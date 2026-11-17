"""Validation and normalization for hub HTTP(S) origins."""
from __future__ import annotations

import ipaddress
from urllib.parse import urlsplit


def hub_origin(url: object) -> str:
    parsed = urlsplit(str(url or "").strip())
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError(
            "Use a hub HTTP(S) origin without a path, credentials, query or fragment."
        )
    host = parsed.hostname.lower()
    is_loopback = host == "localhost"
    if not is_loopback:
        try:
            is_loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            is_loopback = False
    if parsed.scheme != "https" and not is_loopback:
        raise ValueError("Use an HTTPS origin for non-loopback hubs.")
    return f"{parsed.scheme}://{parsed.netloc.lower()}"
