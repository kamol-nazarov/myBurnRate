"""H8 exposure guard, trusted proxy headers, and per-client-IP throttling tests."""

from __future__ import annotations

import logging
import re
from pathlib import Path
from unittest.mock import patch

import pytest
import yaml
from fastapi import Request
from fastapi.testclient import TestClient

import spend_app
from spend_app import (
    auth_model,
    auth_throttle,
    cli,
    exposure,
    service,
    setup_wizard,
    sync,
)
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import LocationError
from spend_app.connection_secrets import set_vault_factory
from spend_app.db import connect, initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.example.com"
HUB_HEADERS = {
    "Origin": HUB_URL,
    "X-BURNRATE-Request": "1",
    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
}
LOCAL_SPOKE_URL = "http://127.0.0.1"
LOCAL_SPOKE_HEADERS = {
    "Origin": LOCAL_SPOKE_URL,
    "X-BURNRATE-Request": "1",
}
PASSWORD = "synthetic-password-long-123"
NEW_PASSWORD = "synthetic-password-rotated-456"


class MemoryVault:
    def __init__(self, store: dict[str, str] | None = None) -> None:
        self._store = store if store is not None else {}

    def available(self) -> bool:
        return True

    def put(self, secret: str, ref: str | None = None) -> str:
        if not isinstance(secret, str) or not secret:
            raise LocationError("Invalid secret.")
        key = ref or "burnrate-mem"
        self._store[key] = secret
        return key

    def get(self, ref: str) -> str:
        if ref not in self._store:
            raise LocationError("Saved credential unavailable.")
        return self._store[ref]

    def delete(self, ref: str) -> None:
        self._store.pop(ref, None)


def _settings(db_path: Path) -> Settings:
    settings = Settings(
        database_path=db_path,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40_000,
    )
    initialize(settings.database_path)
    return settings


@pytest.fixture(autouse=True)
def _reset_h8_state(monkeypatch: pytest.MonkeyPatch):
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    for env_key in (
        "BURNRATE_CENTRAL_SERVER",
        "BURNRATE_HUB_ALLOW_PUBLIC",
        "BURNRATE_TRUSTED_PROXIES",
        "FORWARDED_ALLOW_IPS",
        "BURNRATE_HOST",
        "BURNRATE_HUB_URL",
        "BURNRATE_ACCESS_TOKEN",
        "BURNRATE_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(env_key, raising=False)
    yield
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()


def _mock_service_runtime(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict:
    captured: dict = {}

    def fake_run(*args, **kwargs):
        captured["args"] = args
        captured.update(kwargs)

    monkeypatch.setenv("BURNRATE_DATA_ROOT", str(tmp_path))
    monkeypatch.setattr(service, "acquire_mutex", lambda _name=None: None)
    monkeypatch.setattr(service, "release_mutex", lambda _mutex: None)
    monkeypatch.setattr(service, "configure_logging", lambda: tmp_path / "logs" / "dashboard.log")
    monkeypatch.setattr("uvicorn.run", fake_run)
    return captured


# ---------------------------------------------------------------------------
# 1. Exposure bind guard & publish guard (exposure.py, cli.py, service.py)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.1.1", "::1", "[::1]", "localhost", "::ffff:127.0.0.1"])
def test_h8_non_hub_mode_allows_loopback_hosts(host: str) -> None:
    assert exposure.is_loopback_bind_host(host) is True
    assert exposure.validate_bind_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "0.0.0.0",
        "::",
        "[::]",
        "::ffff:0.0.0.0",
        "*",
        "8.8.8.8",
        "198.51.100.10",
        "2001:4860:4860::8888",
        "10.0.0.5",
        "172.16.0.10",
        "192.168.1.20",
        "100.64.0.1",
        "fd7a:115c:a1e0::1",
        "169.254.1.1",
        "fe80::1",
        "example.com",
        "",
    ],
)
def test_h8_non_hub_mode_refuses_non_loopback_even_when_allow_public_set(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("BURNRATE_HUB_ALLOW_PUBLIC", "1")
    assert exposure.is_loopback_bind_host(host) is False
    with pytest.raises(ValueError, match="127\\.0\\.0\\.1"):
        exposure.validate_bind_host(host)
    with pytest.raises(ValueError):
        exposure.validate_bind_host(host, central=False, allow_public=True)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "[::1]",
        "localhost",
        "::ffff:127.0.0.1",
        "10.0.0.5",
        "172.16.0.10",
        "192.168.1.20",
        "100.64.0.1",
        "100.127.255.254",
        "fd7a:115c:a1e0::1",
        "[fd7a:115c:a1e0::1]",
        "fc00::1",
    ],
)
def test_h8_hub_mode_allows_loopback_rfc1918_and_tailnet_hosts(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    assert exposure.is_allowed_hub_bind_host(host) is True
    assert exposure.validate_bind_host(host) == host


@pytest.mark.parametrize(
    "host",
    [
        "8.8.8.8",
        "198.51.100.10",
        "2001:4860:4860::8888",
        "0.0.0.0",
        "::",
        "[::]",
        "*",
        "::ffff:0.0.0.0",
        "169.254.1.1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "example.com",
        "hub.tailnet.ts.net",
    ],
)
def test_h8_hub_mode_refuses_public_wildcard_link_local_and_hostnames(
    monkeypatch: pytest.MonkeyPatch, host: str
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    assert exposure.is_allowed_hub_bind_host(host) is False
    with pytest.raises(ValueError, match="refusing to bind hub"):
        exposure.validate_bind_host(host)


@pytest.mark.parametrize(
    "host",
    [
        "127.0.0.1",
        "::1",
        "[::1]",
        "::ffff:127.0.0.1",
        "10.0.0.5",
        "172.16.0.10",
        "192.168.1.20",
        "100.64.0.1",
        "100.127.255.254",
        "fd7a:115c:a1e0::1",
        "[fd7a:115c:a1e0::1]",
    ],
)
def test_s5_publish_guard_accepts_loopback_rfc1918_cgnat_and_tailnet_ipv6(
    host: str, capsys
) -> None:
    assert exposure.is_allowed_publish_host(host) is True
    assert exposure.validate_publish_host(f"  {host}  ") == host
    assert exposure.main(["check-publish", host]) == 0
    assert capsys.readouterr().err == ""


@pytest.mark.parametrize(
    "host",
    [
        "",
        "   ",
        "0.0.0.0",
        "::",
        "[::]",
        "::ffff:0.0.0.0",
        "*",
        "8.8.8.8",
        "198.51.100.10",
        "2001:4860:4860::8888",
        "fc00::1",
        "fd00::1",
        "169.254.1.1",
        "fe80::1",
        "224.0.0.1",
        "ff02::1",
        "localhost",
        "example.com",
        "hub.tailnet.ts.net",
    ],
)
def test_s5_publish_guard_rejects_empty_wildcard_public_non_tailscale_ula_link_local_multicast_and_hostnames(
    host: str, capsys
) -> None:
    assert exposure.is_allowed_publish_host(host) is False
    with pytest.raises(ValueError):
        exposure.validate_publish_host(host)
    assert exposure.main(["check-publish", host]) == 2
    assert capsys.readouterr().err


def test_s5_publish_guard_allow_public_permits_valid_ips_but_still_rejects_empty_and_hostnames(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, capsys
) -> None:
    for ip_host in ("0.0.0.0", "::", "198.51.100.10"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
            assert exposure.validate_publish_host(ip_host, allow_public=True) == ip_host
        assert any("BURNRATE_HUB_ALLOW_PUBLIC=1" in r.message for r in caplog.records)

    monkeypatch.setenv("BURNRATE_HUB_ALLOW_PUBLIC", "1")
    for ip_host in ("0.0.0.0", "::", "198.51.100.10"):
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
            assert exposure.main(["check-publish", ip_host]) == 0
        assert any("BURNRATE_HUB_ALLOW_PUBLIC=1" in r.message for r in caplog.records)

    for bad_host in ("", "   ", "localhost", "example.com"):
        with pytest.raises(ValueError):
            exposure.validate_publish_host(bad_host, allow_public=True)
        with pytest.raises(ValueError):
            exposure.validate_publish_host(bad_host)
        assert exposure.main(["check-publish", bad_host]) == 2
        assert capsys.readouterr().err


def test_h8_hub_allow_public_override_logs_warning_and_allows_cli_and_service(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_HUB_ALLOW_PUBLIC", "1")

    with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
        assert exposure.validate_bind_host("0.0.0.0") == "0.0.0.0"
        assert exposure.validate_bind_host("198.51.100.10") == "198.51.100.10"
    assert any("BURNRATE_HUB_ALLOW_PUBLIC=1" in r.message for r in caplog.records)

    cli_captured: dict = {}

    def fake_cli_run(*args, **kwargs):
        cli_captured["args"] = args
        cli_captured.update(kwargs)

    monkeypatch.setattr("uvicorn.run", fake_cli_run)
    monkeypatch.setattr("sys.argv", ["burnrate", "serve", "--host", "0.0.0.0"])
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
        assert cli.main() == 0
    assert cli_captured["host"] == "0.0.0.0"
    assert any("BURNRATE_HUB_ALLOW_PUBLIC=1" in r.message for r in caplog.records)

    svc_captured = _mock_service_runtime(monkeypatch, tmp_path)
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
        assert service.serve(host="0.0.0.0", port=17331) == 0
    assert svc_captured["host"] == "0.0.0.0"
    assert any("BURNRATE_HUB_ALLOW_PUBLIC=1" in r.message for r in caplog.records)


def test_h8_cli_and_service_refuse_disallowed_hosts_and_pass_proxy_settings(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys
) -> None:
    def boom(*_args, **_kwargs):
        raise AssertionError("uvicorn.run must not be called on refused bind or proxy config")

    # Non-hub mode rejects RFC1918/CGNAT/public even with BURNRATE_HUB_ALLOW_PUBLIC=1
    monkeypatch.setenv("BURNRATE_HUB_ALLOW_PUBLIC", "1")
    monkeypatch.setattr("uvicorn.run", boom)
    monkeypatch.setattr("sys.argv", ["burnrate", "serve", "--host", "100.64.0.1"])
    assert cli.main() == 2
    assert "100.64.0.1" in capsys.readouterr().err

    _mock_service_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("uvicorn.run", boom)
    with pytest.raises(SystemExit):
        service.serve(host="100.64.0.1", port=17331)

    # Hub mode allows Tailscale CGNAT via BURNRATE_HOST default and passes default loopback proxies
    monkeypatch.delenv("BURNRATE_HUB_ALLOW_PUBLIC", raising=False)
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_HOST", "100.64.0.1")

    captured = _mock_service_runtime(monkeypatch, tmp_path)
    monkeypatch.setattr("sys.argv", ["burnrate", "serve"])
    assert cli.main() == 0
    assert captured["host"] == "100.64.0.1"
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1", "::1"]

    captured.clear()
    assert service.serve(host="100.64.0.1", port=17331) == 0
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1", "::1"]

    # Custom BURNRATE_TRUSTED_PROXIES is forwarded to uvicorn.run
    monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", "127.0.0.1, 10.0.0.0/8")
    captured.clear()
    assert cli.main() == 0
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1", "10.0.0.0/8"]

    captured.clear()
    assert service.serve(host="100.64.0.1", port=17331) == 0
    assert captured["proxy_headers"] is True
    assert captured["forwarded_allow_ips"] == ["127.0.0.1", "10.0.0.0/8"]

    # BURNRATE_TRUSTED_PROXIES=none disables proxy_headers
    monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", "none")
    captured.clear()
    assert cli.main() == 0
    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] is None

    captured.clear()
    assert service.serve(host="100.64.0.1", port=17331) == 0
    assert captured["proxy_headers"] is False
    assert captured["forwarded_allow_ips"] is None

    # Wildcard proxy values refuse startup in both CLI and service
    monkeypatch.setattr("uvicorn.run", boom)
    for bad_proxy in ("*", "0.0.0.0/0"):
        monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", bad_proxy)
        assert cli.main() == 2
        assert "Wildcard" in capsys.readouterr().err
        with pytest.raises(SystemExit):
            service.serve(host="127.0.0.1", port=17331)


# ---------------------------------------------------------------------------
# 2. Trusted proxies & real client IP (exposure.py, api.py)
# ---------------------------------------------------------------------------


def test_h8_trusted_proxies_parsing_defaults_fallbacks_and_rejections(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    assert exposure.trusted_proxies() == ("127.0.0.1", "::1")

    monkeypatch.setenv("FORWARDED_ALLOW_IPS", "10.0.0.2, localhost")
    assert exposure.trusted_proxies() == ("10.0.0.2", "127.0.0.1", "::1")

    monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", "172.16.0.0/12, [::1], 172.16.0.0/12")
    assert exposure.trusted_proxies() == ("172.16.0.0/12", "::1")

    for disabled in ("none", "OFF", "False"):
        monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", disabled)
        assert exposure.trusted_proxies() == ()

    for invalid in ("*", "0.0.0.0/0", "::/0", "10.0.0.0/0", "not-an-ip", "10.0.0.1/99"):
        monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", invalid)
        with pytest.raises(ValueError):
            exposure.trusted_proxies()
        with pytest.raises(ValueError):
            create_app(_settings(tmp_path / "bad_proxy.db"), enable_scheduler=False)


def test_h8_middleware_trusts_loopback_proxy_and_sets_secure_cookie(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", HUB_URL)

    app = create_app(_settings(tmp_path / "proxy_hub.db"), enable_scheduler=False)

    @app.get("/_test/whoami")
    def _whoami(request: Request):
        return {
            "client": request.client.host if request.client else None,
            "scheme": request.url.scheme,
            "resolved": exposure.resolve_client_ip(request),
        }

    # Loopback Caddy peer (127.0.0.1 and ::1) updates client IP, scheme, and Secure cookie
    with TestClient(app, base_url="http://hub.example.com", client=("127.0.0.1", 50000)) as client:
        info = client.get(
            "/_test/whoami",
            headers={"X-Forwarded-For": "100.64.0.15", "X-Forwarded-Proto": "https"},
        )
        assert info.status_code == 200
        assert info.json() == {"client": "100.64.0.15", "scheme": "https", "resolved": "100.64.0.15"}

        reg = client.post(
            "/api/account/register",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.15",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert reg.status_code == 200, reg.text
        set_cookie = reg.headers.get("set-cookie", "")
        assert "Secure" in set_cookie

    with TestClient(app, base_url="http://hub.example.com", client=("::1", 50000)) as v6_client:
        info_v6 = v6_client.get(
            "/_test/whoami",
            headers={"X-Forwarded-For": "[fd7a:115c:a1e0::5]:41234", "X-Forwarded-Proto": "https"},
        )
        assert info_v6.status_code == 200
        assert info_v6.json() == {
            "client": "fd7a:115c:a1e0::5",
            "scheme": "https",
            "resolved": "fd7a:115c:a1e0::5",
        }


def test_h8_middleware_ignores_spoofed_headers_from_untrusted_peer(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", "http://hub.example.com")

    app = create_app(_settings(tmp_path / "untrusted.db"), enable_scheduler=False)

    @app.get("/_test/whoami")
    def _whoami(request: Request):
        return {
            "client": request.client.host if request.client else None,
            "scheme": request.url.scheme,
            "resolved": exposure.resolve_client_ip(request),
        }

    with TestClient(app, base_url="http://hub.example.com", client=("198.51.100.50", 1234)) as client:
        info = client.get(
            "/_test/whoami",
            headers={"X-Forwarded-For": "100.64.0.25", "X-Forwarded-Proto": "https"},
        )
        assert info.status_code == 200
        assert info.json() == {
            "client": "198.51.100.50",
            "scheme": "http",
            "resolved": "198.51.100.50",
        }

        reg = client.post(
            "/api/account/register",
            headers={
                "Origin": "http://hub.example.com",
                "X-BURNRATE-Request": "1",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
                "X-Forwarded-For": "100.64.0.25",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert reg.status_code == 200, reg.text
        set_cookie = reg.headers.get("set-cookie", "")
        assert "Secure" not in set_cookie


def test_h8_resolve_client_ip_walks_right_to_left_and_ignores_injection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeRequest:
        def __init__(self, host: str | None, headers: dict[str, str] | None = None):
            self.client = type("Client", (), {"host": host})() if host is not None else None
            self.headers = headers or {}

    monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", "127.0.0.1,10.0.0.0/8")

    # Right-to-left skipping trusted 10.0.0.2 stops at 100.64.0.25, ignoring spoofed 203.0.113.9
    req = FakeRequest("127.0.0.1", {"x-forwarded-for": "203.0.113.9, 100.64.0.25, 10.0.0.2"})
    assert exposure.resolve_client_ip(req) == "100.64.0.25"

    # Unwraps ::ffff: and strips optional port (IPv4:port and [mapped-v6]:port)
    req_mapped = FakeRequest("::ffff:127.0.0.1", {"x-forwarded-for": "::ffff:100.64.0.44"})
    assert exposure.resolve_client_ip(req_mapped) == "100.64.0.44"
    req_mapped_port = FakeRequest("::ffff:127.0.0.1", {"x-forwarded-for": "[::ffff:100.64.0.45]:54321"})
    assert exposure.resolve_client_ip(req_mapped_port) == "100.64.0.45"
    req_v4_port = FakeRequest("127.0.0.1", {"x-forwarded-for": "100.64.0.46:54321"})
    assert exposure.resolve_client_ip(req_v4_port) == "100.64.0.46"

    # Ignores delimiter injection / malformed tokens and keeps valid IP
    req_inject = FakeRequest("127.0.0.1", {"x-forwarded-for": "evil|user, 100.64.0.9"})
    assert exposure.resolve_client_ip(req_inject) == "100.64.0.9"

    # If all forwarded tokens are malformed, falls back to peer IP
    req_all_bad = FakeRequest("127.0.0.1", {"x-forwarded-for": "evil|user, not-an-ip"})
    assert exposure.resolve_client_ip(req_all_bad) == "127.0.0.1"

    # Sanitizes non-IP peer strings against delimiter injection
    req_bad_peer = FakeRequest("weird|peer")
    assert exposure.resolve_client_ip(req_bad_peer) == "weird_peer"


# ---------------------------------------------------------------------------
# 3. Per-client-IP login, registration, & credential change throttling
# ---------------------------------------------------------------------------


def _register_owner(client: TestClient, *, headers: dict[str, str] | None = None) -> None:
    merged = {**HUB_HEADERS, **(headers or {})}
    res = client.post(
        "/api/account/register",
        headers=merged,
        json={"username": "owner@example.com", "password": PASSWORD},
    )
    assert res.status_code == 200, res.text
    client.cookies.clear()


def _create_hub_and_paired_spoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    username: str = "owner@example.com",
    legacy_username: str | None = None,
    name_suffix: str = "s3",
):
    vault_stores: dict[str, dict[str, str]] = {}

    def _vault_factory(database_path):
        key = str(database_path)
        return MemoryVault(vault_stores.setdefault(key, {}))

    previous_factory = set_vault_factory(_vault_factory)

    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", HUB_URL)
    hub_settings = _settings(tmp_path / f"hub_{name_suffix}.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL, client=("127.0.0.1", 50000))

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    spoke_settings = _settings(tmp_path / f"spoke_{name_suffix}.db")
    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    from spend_app.local_access import read_local_key

    spoke_client = TestClient(spoke_app, base_url=LOCAL_SPOKE_URL, client=("127.0.0.1", 50001),
                              headers={"Authorization": "Bearer " + read_local_key(spoke_settings.database_path)})

    if legacy_username is None:
        reg = hub_client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": username, "password": PASSWORD, "sessionOnly": True},
        )
        assert reg.status_code == 200, reg.text
        owner_session = reg.cookies[auth_model.COOKIE]
        hub_client.cookies.clear()
    else:
        salt_hex, pw_hash = setup_wizard._hash_password(
            PASSWORD, iterations=setup_wizard.PBKDF2_ITERATIONS
        )
        rec = {
            "id": "acct-legacy-email-1",
            "username": legacy_username,
            "email": username,
            "role": "owner",
            "salt": salt_hex,
            "passwordHash": pw_hash,
            "iterations": setup_wizard.PBKDF2_ITERATIONS,
            "createdAt": auth_model.stamp(),
        }
        with connect(hub_settings.database_path) as conn:
            setup_wizard._write_accounts(conn, {"version": 1, "users": {legacy_username: rec}})
        login = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": username, "password": PASSWORD, "sessionOnly": True},
        )
        assert login.status_code == 200, login.text
        owner_session = login.cookies[auth_model.COOKIE]
        hub_client.cookies.clear()

    return (
        hub_settings,
        hub_client,
        spoke_settings,
        spoke_client,
        owner_session,
        previous_factory,
    )


def _pair_spoke(spoke_client: TestClient, *, username: str = "owner@example.com") -> None:
    res = spoke_client.post(
        "/api/setup/account",
        headers=LOCAL_SPOKE_HEADERS,
        json={
            "mode": "login",
            "username": username,
            "password": PASSWORD,
            "computerName": "PairedSpoke",
            "testSync": False,
        },
    )
    assert res.status_code == 200, res.text
    spoke_client.cookies.clear()


def _assert_locked_out(response) -> None:
    assert response.status_code == 429, response.text
    payload = response.json()
    assert payload["error"]["code"] == "AUTH-102"
    assert payload["error"]["retryAfter"] >= 1
    assert int(response.headers["Retry-After"]) >= 1


def test_h8_login_throttle_isolates_spokes_behind_trusted_proxy(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", HUB_URL)

    app = create_app(_settings(tmp_path / "login_throttle.db"), enable_scheduler=False)

    with TestClient(app, base_url=HUB_URL, client=("127.0.0.1", 50000)) as proxy_client:
        _register_owner(
            proxy_client,
            headers={"X-Forwarded-For": "100.64.0.10", "X-Forwarded-Proto": "https"},
        )

        # Spoke A fails 5 times -> 1..4 are 401 AUTH-101, 5th triggers 429 AUTH-102 + Retry-After
        for attempt in range(4):
            bad = proxy_client.post(
                "/api/account/login",
                headers={
                    **HUB_HEADERS,
                    "X-Forwarded-For": "100.64.0.10",
                    "X-Forwarded-Proto": "https",
                },
                json={"username": "owner@example.com", "password": "wrong-password-123"},
            )
            assert bad.status_code == 401, (attempt, bad.text)
            assert bad.json() == {"error": {"code": "AUTH-101"}}

        locked = proxy_client.post(
            "/api/account/login",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.10",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": "wrong-password-123"},
        )
        _assert_locked_out(locked)

        # Even with the right password, Spoke A stays locked out
        still_locked = proxy_client.post(
            "/api/account/login",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.10",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        _assert_locked_out(still_locked)

        # Spoke B (different X-Forwarded-For behind Caddy) is NOT locked out
        spoke_b = proxy_client.post(
            "/api/account/login",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.11",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert spoke_b.status_code == 200, spoke_b.text
        assert spoke_b.json()["ok"] is True


def test_h8_untrusted_peer_cannot_bypass_login_throttle_by_spoofing_xff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", HUB_URL)

    app = create_app(_settings(tmp_path / "spoof_throttle.db"), enable_scheduler=False)

    with TestClient(app, base_url=HUB_URL, client=("127.0.0.1", 50000)) as proxy_client:
        _register_owner(
            proxy_client,
            headers={"X-Forwarded-For": "100.64.0.10", "X-Forwarded-Proto": "https"},
        )

    # Untrusted peer varies X-Forwarded-For on every attempt; all map to 100.64.0.99
    with TestClient(app, base_url=HUB_URL, client=("100.64.0.99", 50000)) as attacker_client:
        for idx in range(4):
            res = attacker_client.post(
                "/api/account/login",
                headers={
                    **HUB_HEADERS,
                    "X-Forwarded-For": f"100.64.0.{77 + idx}",
                    "X-Forwarded-Proto": "https",
                },
                json={"username": "owner@example.com", "password": "wrong-password-123"},
            )
            assert res.status_code == 401, (idx, res.text)
            assert res.json() == {"error": {"code": "AUTH-101"}}

        locked = attacker_client.post(
            "/api/account/login",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.88",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": "wrong-password-123"},
        )
        _assert_locked_out(locked)

    # Legitimate spoke 100.64.0.77 behind trusted proxy is unaffected
    with TestClient(app, base_url=HUB_URL, client=("127.0.0.1", 50000)) as proxy_client:
        legit = proxy_client.post(
            "/api/account/login",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.77",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert legit.status_code == 200, legit.text


def test_h8_closed_registration_throttles_per_client_ip_across_distinct_emails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.com")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", HUB_URL)

    app = create_app(_settings(tmp_path / "reg_throttle.db"), enable_scheduler=False)

    with TestClient(app, base_url=HUB_URL, client=("127.0.0.1", 50000)) as proxy_client:
        _register_owner(
            proxy_client,
            headers={"X-Forwarded-For": "100.64.0.10", "X-Forwarded-Proto": "https"},
        )

        # Spoke A probes closed /api/account/register with 5 distinct emails -> 429 on 5th
        for idx in range(4):
            res = proxy_client.post(
                "/api/account/register",
                headers={
                    **HUB_HEADERS,
                    "X-Forwarded-For": "100.64.0.10",
                    "X-Forwarded-Proto": "https",
                },
                json={"username": f"probe{idx}@example.com", "password": PASSWORD},
            )
            assert res.status_code == 403, (idx, res.text)
            assert res.json() == {"error": {"code": "AUTH-103"}}

        locked = proxy_client.post(
            "/api/account/register",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.10",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "probe4@example.com", "password": PASSWORD},
        )
        _assert_locked_out(locked)

        # Spoke B has an isolated per-IP registration bucket -> still gets 403 AUTH-103
        spoke_b = proxy_client.post(
            "/api/account/register",
            headers={
                **HUB_HEADERS,
                "X-Forwarded-For": "100.64.0.11",
                "X-Forwarded-Proto": "https",
            },
            json={"username": "other-spoke@example.com", "password": PASSWORD},
        )
        assert spoke_b.status_code == 403, spoke_b.text
        assert spoke_b.json() == {"error": {"code": "AUTH-103"}}


def test_s3_login_lockout_blocks_direct_and_spoke_password_and_email_changes_without_verifying(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    hub_settings, hub_client, _, spoke_client, owner_session, prev_factory = (
        _create_hub_and_paired_spoke(tmp_path, monkeypatch, name_suffix="lockout")
    )
    try:
        with hub_client, spoke_client, patch.dict(sync._HUB_TEST_CLIENTS, {HUB_URL: hub_client}):
            _pair_spoke(spoke_client)

            # Exhaust login budget on /api/account/login (4 x 401, 5th -> 429 AUTH-102)
            for _ in range(4):
                bad = hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": "wrong-password-123"},
                )
                assert bad.status_code == 401
                assert bad.json() == {"error": {"code": "AUTH-101"}}

            locked = hub_client.post(
                "/api/account/login",
                headers=HUB_HEADERS,
                json={"username": "owner@example.com", "password": "wrong-password-123"},
            )
            _assert_locked_out(locked)

            # Spy on _verify_password: no locked-out request may execute PBKDF2 verification
            verify_calls = 0
            real_verify = setup_wizard._verify_password

            def _spy_verify(*args, **kwargs):
                nonlocal verify_calls
                verify_calls += 1
                return real_verify(*args, **kwargs)

            monkeypatch.setattr(setup_wizard, "_verify_password", _spy_verify)

            bearer_headers = {**HUB_HEADERS, "Authorization": f"Bearer {owner_session}"}
            assert not spoke_client.cookies

            for guess in ("wrong-current-password", PASSWORD):
                direct_pw = hub_client.post(
                    "/api/account/password",
                    headers=bearer_headers,
                    json={"currentPassword": guess, "newPassword": NEW_PASSWORD},
                )
                _assert_locked_out(direct_pw)

                direct_email = hub_client.post(
                    "/api/account/email",
                    headers=bearer_headers,
                    json={"email": "attacker@example.com", "password": guess},
                )
                _assert_locked_out(direct_email)

                spoke_pw = spoke_client.post(
                    "/api/setup/account/password",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"currentPassword": guess, "newPassword": NEW_PASSWORD},
                )
                _assert_locked_out(spoke_pw)

                spoke_email = spoke_client.post(
                    "/api/setup/account/email",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"email": "attacker@example.com", "password": guess},
                )
                _assert_locked_out(spoke_email)

            assert verify_calls == 0

            # Neither email nor password changed; fresh login with old PASSWORD works after reset
            with connect(hub_settings.database_path) as conn:
                users = setup_wizard._read_accounts(conn)["users"]
                assert "owner@example.com" in users
                assert "attacker@example.com" not in users

            auth_throttle.reset()
            assert (
                hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": PASSWORD},
                ).status_code
                == 200
            )
            hub_client.cookies.clear()
            assert (
                hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": NEW_PASSWORD},
                ).status_code
                == 401
            )
    finally:
        set_vault_factory(prev_factory)


def test_s3_wrong_password_and_email_guesses_share_budget_and_lock_out_login_per_ip(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, hub_client, _, spoke_client, owner_session, prev_factory = _create_hub_and_paired_spoke(
        tmp_path, monkeypatch, name_suffix="reverse"
    )
    try:
        with hub_client, spoke_client, patch.dict(sync._HUB_TEST_CLIENTS, {HUB_URL: hub_client}):
            _pair_spoke(spoke_client)
            bearer_headers = {**HUB_HEADERS, "Authorization": f"Bearer {owner_session}"}

            # 1) Direct /api/account/password & /api/account/email wrong guesses share the 5-attempt budget
            for idx in range(2):
                res_pw = hub_client.post(
                    "/api/account/password",
                    headers=bearer_headers,
                    json={"currentPassword": f"wrong-{idx}", "newPassword": NEW_PASSWORD},
                )
                assert res_pw.status_code == 401 and res_pw.json() == {"error": {"code": "AUTH-101"}}
                res_em = hub_client.post(
                    "/api/account/email",
                    headers=bearer_headers,
                    json={"email": "new@example.com", "password": f"wrong-em-{idx}"},
                )
                assert res_em.status_code == 401 and res_em.json() == {"error": {"code": "AUTH-101"}}

            fifth = hub_client.post(
                "/api/account/password",
                headers=bearer_headers,
                json={"currentPassword": "wrong-5th", "newPassword": NEW_PASSWORD},
            )
            _assert_locked_out(fifth)

            # Locks out /api/account/login (even with right password) for 127.0.0.1
            locked_login = hub_client.post(
                "/api/account/login",
                headers=HUB_HEADERS,
                json={"username": "owner@example.com", "password": PASSWORD},
            )
            _assert_locked_out(locked_login)

            # Different client IP via X-Forwarded-For from trusted proxy 127.0.0.1 is NOT locked out
            other_ip_login = hub_client.post(
                "/api/account/login",
                headers={**HUB_HEADERS, "X-Forwarded-For": "100.64.0.77"},
                json={"username": "owner@example.com", "password": PASSWORD},
            )
            assert other_ip_login.status_code == 200, other_ip_login.text
            hub_client.cookies.clear()

            # 2) Anonymous spoke relay /api/setup/account/{password,email} also shares and triggers lockout
            auth_throttle.reset()
            for idx in range(2):
                s_pw = spoke_client.post(
                    "/api/setup/account/password",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"currentPassword": f"spoke-wrong-{idx}", "newPassword": NEW_PASSWORD},
                )
                assert s_pw.status_code == 401 and s_pw.json() == {"error": {"code": "AUTH-101"}}
                s_em = spoke_client.post(
                    "/api/setup/account/email",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"email": "new@example.com", "password": f"spoke-wrong-em-{idx}"},
                )
                assert s_em.status_code == 401 and s_em.json() == {"error": {"code": "AUTH-101"}}

            spoke_fifth = spoke_client.post(
                "/api/setup/account/email",
                headers=LOCAL_SPOKE_HEADERS,
                json={"email": "new@example.com", "password": "spoke-wrong-5th"},
            )
            _assert_locked_out(spoke_fifth)

            locked_login_2 = hub_client.post(
                "/api/account/login",
                headers=HUB_HEADERS,
                json={"username": "owner@example.com", "password": PASSWORD},
            )
            _assert_locked_out(locked_login_2)

            other_ip_login_2 = hub_client.post(
                "/api/account/login",
                headers={**HUB_HEADERS, "X-Forwarded-For": "100.64.0.88"},
                json={"username": "owner@example.com", "password": PASSWORD},
            )
            assert other_ip_login_2.status_code == 200, other_ip_login_2.text
            hub_client.cookies.clear()

    finally:
        set_vault_factory(prev_factory)


def test_s3_correct_password_or_email_change_resets_per_account_throttle(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, hub_client, _, spoke_client, owner_session, prev_factory = _create_hub_and_paired_spoke(
        tmp_path, monkeypatch, name_suffix="reset"
    )
    try:
        with hub_client, spoke_client, patch.dict(sync._HUB_TEST_CLIENTS, {HUB_URL: hub_client}):
            _pair_spoke(spoke_client)
            bearer_headers = {**HUB_HEADERS, "Authorization": f"Bearer {owner_session}"}

            # 4 failed password guesses -> one away from lockout
            for idx in range(4):
                res = hub_client.post(
                    "/api/account/password",
                    headers=bearer_headers,
                    json={"currentPassword": f"wrong-{idx}", "newPassword": NEW_PASSWORD},
                )
                assert res.status_code == 401

            # Succeeding with current password resets the per-account throttle
            ok = hub_client.post(
                "/api/account/password",
                headers=bearer_headers,
                json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
            )
            assert ok.status_code == 200 and ok.json() == {"ok": True}

            # Subsequent logins have a fresh 4-failure runway before 5th triggers 429
            for idx in range(4):
                bad = hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": f"wrong-after-{idx}"},
                )
                assert bad.status_code == 401, (idx, bad.text)
            _assert_locked_out(
                hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": "wrong-after-4"},
                )
            )
    finally:
        set_vault_factory(prev_factory)


def test_s3_legacy_username_and_email_share_guessing_budget_both_directions(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _, hub_client, _, spoke_client, owner_session, prev_factory = _create_hub_and_paired_spoke(
        tmp_path,
        monkeypatch,
        username="owner@example.com",
        legacy_username="legacy-owner",
        name_suffix="legacy",
    )
    try:
        with hub_client, spoke_client, patch.dict(sync._HUB_TEST_CLIENTS, {HUB_URL: hub_client}):
            _pair_spoke(spoke_client, username="legacy-owner")
            bearer_headers = {**HUB_HEADERS, "Authorization": f"Bearer {owner_session}"}

            # Direction 1a: Lock out login via legacy username -> password & email changes blocked
            for _ in range(4):
                assert (
                    hub_client.post(
                        "/api/account/login",
                        headers=HUB_HEADERS,
                        json={"username": "legacy-owner", "password": "wrong"},
                    ).status_code
                    == 401
                )
            _assert_locked_out(
                hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "legacy-owner", "password": "wrong"},
                )
            )
            _assert_locked_out(
                hub_client.post(
                    "/api/account/password",
                    headers=bearer_headers,
                    json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
                )
            )
            _assert_locked_out(
                spoke_client.post(
                    "/api/setup/account/email",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"email": "updated@example.com", "password": PASSWORD},
                )
            )

            # Direction 1b: Lock out login via email -> password & email changes blocked
            auth_throttle.reset()
            for _ in range(4):
                assert (
                    hub_client.post(
                        "/api/account/login",
                        headers=HUB_HEADERS,
                        json={"username": "owner@example.com", "password": "wrong"},
                    ).status_code
                    == 401
                )
            _assert_locked_out(
                hub_client.post(
                    "/api/account/login",
                    headers=HUB_HEADERS,
                    json={"username": "owner@example.com", "password": "wrong"},
                )
            )
            _assert_locked_out(
                spoke_client.post(
                    "/api/setup/account/password",
                    headers=LOCAL_SPOKE_HEADERS,
                    json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
                )
            )
            _assert_locked_out(
                hub_client.post(
                    "/api/account/email",
                    headers=bearer_headers,
                    json={"email": "updated@example.com", "password": PASSWORD},
                )
            )

            # Direction 2: Lock out via password/email guesses -> locks out login via BOTH identifiers
            auth_throttle.reset()
            for idx in range(4):
                assert (
                    hub_client.post(
                        "/api/account/password",
                        headers=bearer_headers,
                        json={"currentPassword": f"bad-{idx}", "newPassword": NEW_PASSWORD},
                    ).status_code
                    == 401
                )
            _assert_locked_out(
                hub_client.post(
                    "/api/account/email",
                    headers=bearer_headers,
                    json={"email": "updated@example.com", "password": "bad-5"},
                )
            )
            for ident in ("legacy-owner", "owner@example.com"):
                _assert_locked_out(
                    hub_client.post(
                        "/api/account/login",
                        headers=HUB_HEADERS,
                        json={"username": ident, "password": PASSWORD},
                    )
                )
    finally:
        set_vault_factory(prev_factory)


# ---------------------------------------------------------------------------
# 4. Deploy & docs verification (S5 compose publish guard & conditional pin)
# ---------------------------------------------------------------------------


def validate_compose_image_pins(
    compose_text: str,
    *,
    changelog_text: str,
    app_version: str,
) -> None:
    """Validate Caddy and Hub image pins in deploy/hub/compose.yaml.

    - ``caddy`` must always be pinned to an explicit version + ``@sha256:`` digest.
    - ``myburnrate-hub`` must be ``:<ver>@sha256:<64-hex>`` or
      ``:<ver>-rc.<N>@sha256:<64-hex>``, EXCEPT a tag-only ``:<ver>`` pin is
      permitted while ``<ver> == app_version`` AND ``CHANGELOG.md`` still has
      ``## [<ver>] - Unreleased``.
    """
    assert re.search(r"caddy:\d+\.\d+\.\d+@sha256:[0-9a-f]{64}", compose_text), (
        "caddy image must be digest-pinned"
    )
    hub_refs = re.findall(r"myburnrate-hub:([^\s\"'}]+)", compose_text)
    assert hub_refs, "deploy/hub/compose.yaml must reference myburnrate-hub"

    unreleased_re = re.compile(
        rf"^## \[{re.escape(app_version)}\] - Unreleased\s*$", re.MULTILINE
    )
    is_unreleased = bool(unreleased_re.search(changelog_text))
    digest_re = re.compile(r"^\d+\.\d+\.\d+(?:-rc\.\d+)?@sha256:[0-9a-f]{64}$")
    tag_only_re = re.compile(r"^(\d+\.\d+\.\d+)$")

    for ref in hub_refs:
        if digest_re.fullmatch(ref):
            continue
        match = tag_only_re.fullmatch(ref)
        assert match is not None, f"Invalid myburnrate-hub image reference: {ref!r}"
        tag_version = match.group(1)
        assert tag_version == app_version, (
            f"Tag-only myburnrate-hub:{tag_version} does not match app_version {app_version}"
        )
        assert is_unreleased, (
            f"Tag-only myburnrate-hub:{tag_version} is not allowed once CHANGELOG.md is dated"
        )


def test_h8_deploy_compose_is_tailnet_only_and_drops_deprecated_registration_vars() -> None:
    compose_path = ROOT / "deploy" / "hub" / "compose.yaml"
    raw = compose_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    publish_check = data["services"]["publish-check"]
    hub = data["services"]["hub"]
    caddy = data["services"]["caddy"]

    validate_compose_image_pins(
        raw, changelog_text=changelog, app_version=spend_app.__version__
    )

    # S5: publish-check service runs exposure check-publish with required env vars
    publish_cmd = " ".join(str(part) for part in publish_check["command"])
    assert "spend_app.exposure" in publish_cmd
    assert "check-publish" in publish_cmd
    assert "BURNRATE_TAILNET_IP" in publish_check["environment"]
    assert "BURNRATE_HUB_ALLOW_PUBLIC" in publish_check["environment"]

    # Both hub (which binds host ports) and caddy gate startup on publish-check
    assert hub["depends_on"]["publish-check"]["condition"] == "service_completed_successfully"
    assert caddy["depends_on"]["publish-check"]["condition"] == "service_completed_successfully"

    env = hub["environment"]
    assert env["BURNRATE_CENTRAL_SERVER"] == "1"
    assert "BURNRATE_ACCESS_TOKEN" in env
    assert "BURNRATE_ALLOWED_HOSTS" in env
    assert "BURNRATE_TRUSTED_PROXIES" in env
    assert "BURNRATE_SETUP_TOKEN" not in env
    assert "BURNRATE_ALLOW_REGISTRATION" not in env
    assert "BURNRATE_SETUP_TOKEN" not in raw
    assert "BURNRATE_ALLOW_REGISTRATION" not in raw

    ports = hub["ports"]
    assert ports
    for port_mapping in ports:
        assert str(port_mapping).startswith("${BURNRATE_TAILNET_IP")

    lowered = raw.lower()
    assert "tailnet" in lowered
    assert "no setup token" in lowered
    assert "embed" in lowered and "hub address" in lowered
    assert "create the owner account immediately" in lowered


def test_s5_conditional_image_pin_rule_negative_and_positive_cases() -> None:
    caddy_line = (
        "image: caddy:2.11.9@sha256:4e2425f6009577e7c651010f16679c28f7d12df3e7b1914f941a7a3370746b3c\n"
    )
    tag_only_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9}\n"
    )
    digest_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9@sha256:"
        + ("a" * 64)
        + "}\n"
    )
    rc_digest_compose = (
        caddy_line
        + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-rc.1@sha256:"
        + ("a" * 64)
        + "}\n"
    )

    unreleased_changelog = "# Changelog\n\n## [0.5.9] - Unreleased\n\n### Added\n"
    dated_changelog = "# Changelog\n\n## [0.5.9] - 2026-11-17\n\n### Added\n"

    # Negative 1: Tag-only compose image with a dated CHANGELOG.md heading must fail
    with pytest.raises(AssertionError):
        validate_compose_image_pins(
            tag_only_compose, changelog_text=dated_changelog, app_version="0.5.9"
        )

    # Negative 2: Tag-only compose image with a mismatched version must fail even with Unreleased
    for bad_ver_compose in (
        caddy_line + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.8}\n",
        caddy_line + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.6.0}\n",
    ):
        with pytest.raises(AssertionError):
            validate_compose_image_pins(
                bad_ver_compose, changelog_text=unreleased_changelog, app_version="0.5.9"
            )

    # Negative 3: Tag-only rc pin (without @sha256:) must fail even when Unreleased
    with pytest.raises(AssertionError):
        validate_compose_image_pins(
            caddy_line
            + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-rc.1}\n",
            changelog_text=unreleased_changelog,
            app_version="0.5.9",
        )

    # Negative 4: Non-rc suffix with digest must fail
    with pytest.raises(AssertionError):
        validate_compose_image_pins(
            caddy_line
            + "image: ${BURNRATE_HUB_IMAGE:-ghcr.io/kamol-nazarov/myburnrate-hub:0.5.9-dirty@sha256:"
            + ("a" * 64)
            + "}\n",
            changelog_text=unreleased_changelog,
            app_version="0.5.9",
        )

    # Positive: Digest-pinned (final or rc candidate) passes regardless of dated vs unreleased changelog
    for good_compose in (digest_compose, rc_digest_compose):
        validate_compose_image_pins(
            good_compose, changelog_text=unreleased_changelog, app_version="0.5.9"
        )
        validate_compose_image_pins(
            good_compose, changelog_text=dated_changelog, app_version="0.5.9"
        )


def test_h8_self_hosting_and_releasing_docs_cover_publish_check_and_digest_pin() -> None:
    doc = (ROOT / "docs" / "self-hosting.md").read_text(encoding="utf-8")
    lowered = doc.lower()

    assert "tailnet-only" in lowered
    assert "no setup token" in lowered
    assert "embed the hub address" in lowered or "_build_config.py" in doc
    assert "create the owner account right after deploying" in lowered
    assert "BURNRATE_HUB_ALLOW_PUBLIC" in doc
    assert "BURNRATE_TRUSTED_PROXIES" in doc
    assert "publish-check" in doc
    assert "0.5.9" in doc

    # Deprecated vars must only appear as deprecated/ignored notices, never as setup instructions
    assert "BURNRATE_SETUP_TOKEN` and\n   `BURNRATE_ALLOW_REGISTRATION` are deprecated and ignored" in doc
    assert "export BURNRATE_SETUP_TOKEN" not in doc
    assert "export BURNRATE_ALLOW_REGISTRATION" not in doc
    assert "BURNRATE_SETUP_TOKEN=" not in doc
    assert "BURNRATE_ALLOW_REGISTRATION=" not in doc

    releasing = (ROOT / "docs" / "releasing.md").read_text(encoding="utf-8")
    assert "deploy/hub/compose.yaml" in releasing
    assert "hub-image.yml" in releasing
    assert "<version>-rc.<run_number>@sha256:<digest>" in releasing
    assert "0.5.9-rc.<run_number>@sha256:<digest>" in releasing
    assert "forbid dating the changelog without a digest pin" in releasing
    assert "git tag -a v<version>" in releasing

    dispatch_idx = releasing.index("manually dispatch")
    pin_idx = releasing.index("0.5.9-rc.<run_number>@sha256:<digest>")
    date_idx = releasing.index("then date `CHANGELOG.md`")
    tag_idx = releasing.index("git tag -a v<version>")
    assert dispatch_idx < pin_idx < date_idx < tag_idx
