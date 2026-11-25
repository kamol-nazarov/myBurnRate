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


def bound_hub_origin(spoke: dict, configured: str = "") -> str:
    """A stored credential is usable only for its original configured origin."""
    stored = spoke.get("credentialOrigin") or spoke.get("hubUrl")
    if not stored or spoke.get("requiresHubSignIn"):
        raise ValueError("Sign in to the hub to connect this computer.")
    origin = hub_origin(stored)
    if configured and hub_origin(configured) != origin:
        raise ValueError("The hub changed. Sign in to the new hub.")
    return origin
