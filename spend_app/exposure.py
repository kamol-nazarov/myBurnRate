"""Hub bind exposure guard and trusted reverse-proxy client IP resolution."""

from __future__ import annotations

import argparse
import ipaddress
import logging
import os
import sys
from typing import Iterable

from starlette.types import ASGIApp, Receive, Scope, Send

_LOGGER = logging.getLogger("burnrate.exposure")
_TRUTHY = frozenset({"1", "true", "yes"})
_DISABLED_PROXY_VALUES = frozenset({"none", "off", "false"})
_LOOPBACK_HOSTNAMES = frozenset({"localhost"})
_DEFAULT_TRUSTED_PROXIES = ("127.0.0.1", "::1")

_RFC1918_NETWORKS = (
    ipaddress.IPv4Network("10.0.0.0/8"),
    ipaddress.IPv4Network("172.16.0.0/12"),
    ipaddress.IPv4Network("192.168.0.0/16"),
)
_CGNAT_NETWORK = ipaddress.IPv4Network("100.64.0.0/10")
_ULA_IPV6_NETWORK = ipaddress.IPv6Network("fc00::/7")
_TAILSCALE_IPV6_NETWORK = ipaddress.IPv6Network("fd7a:115c:a1e0::/48")


def central_server_enabled() -> bool:
    """Return True when BURNRATE is configured to run as a central hub."""
    return os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in _TRUTHY


def hub_allow_public() -> bool:
    """Return True when BURNRATE_HUB_ALLOW_PUBLIC=1 explicitly overrides the bind guard."""
    return os.getenv("BURNRATE_HUB_ALLOW_PUBLIC", "").strip().lower() in _TRUTHY


def _strip_brackets(value: str) -> str:
    cleaned = (value or "").strip()
    if cleaned.startswith("[") and cleaned.endswith("]"):
        return cleaned[1:-1].strip()
    return cleaned


def _unwrap_ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    candidate = _strip_brackets(value)
    if not candidate:
        return None
    try:
        parsed = ipaddress.ip_address(candidate)
    except ValueError:
        return None
    if isinstance(parsed, ipaddress.IPv6Address) and parsed.ipv4_mapped is not None:
        return parsed.ipv4_mapped
    return parsed


def is_loopback_bind_host(host: str) -> bool:
    """Return True only for loopback bind targets (127.0.0.0/8, ::1, localhost)."""
    cleaned = _strip_brackets(host).lower()
    if not cleaned:
        return False
    if cleaned in _LOOPBACK_HOSTNAMES:
        return True
    ip = _unwrap_ip(cleaned)
    return bool(ip is not None and ip.is_loopback)


def is_allowed_hub_bind_host(host: str) -> bool:
    """Return True for loopback, RFC1918, Tailscale CGNAT (100.64.0.0/10), or ULA/Tailscale IPv6."""
    if is_loopback_bind_host(host):
        return True
    ip = _unwrap_ip(host)
    if ip is None:
        return False
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        return False
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _CGNAT_NETWORK or any(ip in net for net in _RFC1918_NETWORKS)
    return ip in _TAILSCALE_IPV6_NETWORK or ip in _ULA_IPV6_NETWORK


def is_allowed_publish_host(host: str) -> bool:
    """Return True only for loopback, RFC1918, CGNAT (100.64.0.0/10), or Tailscale IPv6."""
    ip = _unwrap_ip(host)
    if ip is None:
        return False
    if ip.is_unspecified or ip.is_multicast or ip.is_link_local:
        return False
    if ip.is_loopback:
        return True
    if isinstance(ip, ipaddress.IPv4Address):
        return ip in _CGNAT_NETWORK or any(ip in net for net in _RFC1918_NETWORKS)
    return ip in _TAILSCALE_IPV6_NETWORK


def validate_publish_host(
    host: str,
    *,
    allow_public: bool | None = None,
) -> str:
    """Validate an external publish IP address (e.g. Compose BURNRATE_TAILNET_IP)."""
    cleaned = (host or "").strip()
    if not cleaned:
        raise ValueError(
            "refusing to publish without an explicit address: set BURNRATE_TAILNET_IP to "
            "loopback (127.0.0.1/::1), RFC1918 private, CGNAT (100.64.0.0/10), or "
            "Tailscale IPv6 (fd7a:115c:a1e0::/48)."
        )
    ip = _unwrap_ip(cleaned)
    if ip is None:
        raise ValueError(
            f"refusing to publish on {host!r}: expected an IP address (hostnames are not allowed)."
        )
    if is_allowed_publish_host(cleaned):
        return cleaned
    override = hub_allow_public() if allow_public is None else bool(allow_public)
    if override:
        _LOGGER.warning(
            "BURNRATE_HUB_ALLOW_PUBLIC=1 is set: allowing publish address %r. "
            "First-account registration requires no setup token and release builds may embed "
            "the hub address; ensure access is restricted at the network layer.",
            cleaned,
        )
        return cleaned
    raise ValueError(
        f"refusing to publish on {host!r}: publish only on loopback (127.0.0.1/::1), "
        "RFC1918 private, CGNAT (100.64.0.0/10), or Tailscale IPv6 (fd7a:115c:a1e0::/48), "
        "or set BURNRATE_HUB_ALLOW_PUBLIC=1 to override."
    )


def validate_bind_host(
    host: str,
    *,
    central: bool | None = None,
    allow_public: bool | None = None,
) -> str:
    """Validate the bind host for local-only or hub mode, raising ValueError on refusal."""
    is_central = central_server_enabled() if central is None else bool(central)
    override = hub_allow_public() if allow_public is None else bool(allow_public)

    if not is_central:
        if not is_loopback_bind_host(host):
            raise ValueError(
                f"refusing to bind {host!r}; BURNRATE listens on 127.0.0.1 only"
            )
        return host

    if is_allowed_hub_bind_host(host):
        return host

    if override:
        _LOGGER.warning(
            "BURNRATE_HUB_ALLOW_PUBLIC=1 is set: allowing hub bind to %r. "
            "First-account registration requires no setup token and release builds may embed "
            "the hub address; ensure access is restricted at the network layer.",
            host,
        )
        return host

    raise ValueError(
        f"refusing to bind hub to {host!r}: hubs must bind to loopback (127.0.0.1/::1), "
        "RFC1918 private, Tailscale CGNAT (100.64.0.0/10), or Tailscale IPv6 addresses. "
        "Bind 127.0.0.1 behind Caddy/Tailscale Serve, or set BURNRATE_HUB_ALLOW_PUBLIC=1 to override."
    )


def _parse_proxy_entry(token: str) -> list[str]:
    clean = _strip_brackets(token)
    if not clean:
        return []
    lowered = clean.lower()
    if lowered in {"*", "0.0.0.0/0", "::/0"}:
        raise ValueError(
            "Wildcard trusted proxy ('*', '0.0.0.0/0', '::/0') is not allowed; "
            "configure explicit proxy IPs or CIDRs in BURNRATE_TRUSTED_PROXIES."
        )
    if lowered in _LOOPBACK_HOSTNAMES:
        return list(_DEFAULT_TRUSTED_PROXIES)
    if "/" in clean:
        try:
            net = ipaddress.ip_network(clean, strict=False)
        except ValueError as exc:
            raise ValueError(f"Invalid trusted proxy CIDR {token!r}: {exc}") from exc
        if net.prefixlen == 0:
            raise ValueError(
                f"Wildcard trusted proxy CIDR {token!r} is not allowed; specify a narrower range."
            )
        return [str(net)]
    ip = _unwrap_ip(clean)
    if ip is None:
        raise ValueError(f"Invalid trusted proxy address {token!r}; expected an IP or CIDR.")
    return [str(ip)]


def trusted_proxies() -> tuple[str, ...]:
    """Return validated trusted reverse-proxy IPs/CIDRs from env (default: loopback)."""
    raw = os.getenv("BURNRATE_TRUSTED_PROXIES")
    if raw is None or not raw.strip():
        raw = os.getenv("FORWARDED_ALLOW_IPS")
    if raw is None or not raw.strip():
        return _DEFAULT_TRUSTED_PROXIES
    stripped = raw.strip()
    if stripped.lower() in _DISABLED_PROXY_VALUES:
        return ()
    entries: list[str] = []
    seen: set[str] = set()
    for part in stripped.split(","):
        item = part.strip()
        if not item:
            continue
        for parsed in _parse_proxy_entry(item):
            if parsed not in seen:
                seen.add(parsed)
                entries.append(parsed)
    return tuple(entries)


def _compile_trusted_proxies(
    proxies: Iterable[str],
) -> tuple[
    set[ipaddress.IPv4Address | ipaddress.IPv6Address],
    list[ipaddress.IPv4Network | ipaddress.IPv6Network],
]:
    hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address] = set()
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for entry in proxies:
        if "/" in entry:
            networks.append(ipaddress.ip_network(entry, strict=False))
        else:
            ip = _unwrap_ip(entry)
            if ip is not None:
                hosts.add(ip)
    return hosts, networks


def _ip_in_trusted(
    ip: ipaddress.IPv4Address | ipaddress.IPv6Address,
    hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> bool:
    return ip in hosts or any(ip in net for net in networks)


def is_trusted_proxy(
    host: str | None,
    proxies: Iterable[str] | None = None,
) -> bool:
    """Return True when ``host`` matches a configured trusted proxy IP or CIDR."""
    if not host:
        return False
    ip = _unwrap_ip(host)
    if ip is None:
        return False
    proxy_list = trusted_proxies() if proxies is None else tuple(proxies)
    if not proxy_list:
        return False
    hosts, networks = _compile_trusted_proxies(proxy_list)
    return _ip_in_trusted(ip, hosts, networks)


def _extract_forwarded_host(token: str) -> str:
    value = (token or "").strip()
    if not value:
        return ""
    if value.startswith("["):
        end = value.find("]")
        if end == -1:
            return value
        host = value[1:end]
        rest = value[end + 1 :]
        if not rest or rest.startswith(":"):
            return host
        return value
    if value.count(":") == 1:
        host, port = value.rsplit(":", 1)
        if port.isdigit():
            return host
    return value


def _resolve_forwarded_client_ip(
    x_forwarded_for: str,
    hosts: set[ipaddress.IPv4Address | ipaddress.IPv6Address],
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network],
) -> str | None:
    tokens = [part.strip() for part in x_forwarded_for.split(",") if part.strip()]
    parsed_ips: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
    for token in tokens:
        candidate = _unwrap_ip(_extract_forwarded_host(token))
        if candidate is not None:
            parsed_ips.append(candidate)
    if not parsed_ips:
        return None
    for candidate in reversed(parsed_ips):
        if not _ip_in_trusted(candidate, hosts, networks):
            return str(candidate)
    return None


def _header_values(headers: object, target_name: str) -> list[str]:
    if headers is None:
        return []
    if hasattr(headers, "getlist"):
        return [str(v) for v in headers.getlist(target_name) if str(v).strip()]
    if isinstance(headers, (list, tuple)):
        target_bytes = target_name.lower().encode("latin1")
        out: list[str] = []
        for k, v in headers:
            key = k.lower() if isinstance(k, bytes) else str(k).lower().encode("latin1")
            if key == target_bytes:
                val = v.decode("latin1") if isinstance(v, bytes) else str(v)
                if val.strip():
                    out.append(val)
        return out
    val = headers.get(target_name) if hasattr(headers, "get") else None
    return [str(val)] if val and str(val).strip() else []


def resolve_client_ip(
    request,
    *,
    proxies: Iterable[str] | None = None,
) -> str:
    """Resolve the real client IP, trusting X-Forwarded-For only from configured proxies."""
    peer_raw = getattr(getattr(request, "client", None), "host", None) or ""
    peer_ip = _unwrap_ip(peer_raw)
    proxy_list = trusted_proxies() if proxies is None else tuple(proxies)

    if peer_ip is not None and proxy_list:
        hosts, networks = _compile_trusted_proxies(proxy_list)
        if _ip_in_trusted(peer_ip, hosts, networks):
            xff_values = _header_values(getattr(request, "headers", None), "x-forwarded-for")
            if xff_values:
                forwarded = _resolve_forwarded_client_ip(", ".join(xff_values), hosts, networks)
                if forwarded:
                    return forwarded
        return str(peer_ip)

    if peer_ip is not None:
        return str(peer_ip)
    safe = peer_raw.strip().replace("|", "_")
    return safe or "local"


def resolve_forwarded_proto(
    headers: object,
    peer_host: str | None,
    *,
    proxies: Iterable[str] | None = None,
) -> str | None:
    """Return 'http'/'https'/'ws'/'wss' from X-Forwarded-Proto only if peer is trusted."""
    if not is_trusted_proxy(peer_host, proxies=proxies):
        return None
    values = _header_values(headers, "x-forwarded-proto")
    if not values:
        return None
    proto = values[-1].split(",")[-1].strip().lower()
    if proto in {"http", "https", "ws", "wss"}:
        return proto
    return None


class TrustedProxyHeadersMiddleware:
    """ASGI middleware honoring X-Forwarded-For/X-Forwarded-Proto only from trusted proxies."""

    def __init__(
        self,
        app: ASGIApp,
        trusted: Iterable[str] | None = None,
    ) -> None:
        self.app = app
        self._explicit_proxies: tuple[str, ...] | None = (
            tuple(trusted) if trusted is not None else None
        )

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") not in {"http", "websocket"}:
            await self.app(scope, receive, send)
            return

        proxy_list = (
            self._explicit_proxies
            if self._explicit_proxies is not None
            else trusted_proxies()
        )
        if proxy_list:
            client_addr = scope.get("client")
            peer_host = client_addr[0] if client_addr else None
            peer_ip = _unwrap_ip(peer_host or "")
            if peer_ip is not None:
                hosts, networks = _compile_trusted_proxies(proxy_list)
                if _ip_in_trusted(peer_ip, hosts, networks):
                    raw_headers = scope.get("headers") or ()
                    proto_values = _header_values(raw_headers, "x-forwarded-proto")
                    if proto_values:
                        proto = proto_values[-1].split(",")[-1].strip().lower()
                        if proto in {"http", "https", "ws", "wss"}:
                            if scope["type"] == "websocket":
                                scope["scheme"] = proto.replace("http", "ws")
                            else:
                                scope["scheme"] = proto
                    xff_values = _header_values(raw_headers, "x-forwarded-for")
                    if xff_values:
                        resolved = _resolve_forwarded_client_ip(
                            ", ".join(xff_values), hosts, networks
                        )
                        if resolved:
                            scope["client"] = (resolved, 0)

        if scope["type"] == "http" and scope.get("scheme") == "https":
            async def send_https(message):
                if message["type"] == "http.response.start":
                    from starlette.datastructures import MutableHeaders

                    MutableHeaders(scope=message)["Strict-Transport-Security"] = "max-age=31536000"
                await send(message)

            await self.app(scope, receive, send_https)
        else:
            await self.app(scope, receive, send)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="spend_app.exposure")
    sub = parser.add_subparsers(dest="command", required=True)
    pub = sub.add_parser("check-publish", help="Validate a hub publish IP address")
    pub.add_argument("host", nargs="?", default="", help="Publish IP (e.g. BURNRATE_TAILNET_IP)")
    args = parser.parse_args(argv)
    if args.command == "check-publish":
        try:
            validate_publish_host(args.host)
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
