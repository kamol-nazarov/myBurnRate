"""H8 exposure guard, trusted proxy headers, and per-client-IP throttling tests."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest
import yaml
from fastapi import Request
from fastapi.testclient import TestClient

from spend_app import auth_throttle, cli, exposure, service, setup_wizard
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.example.com"
HUB_HEADERS = {
    "Origin": HUB_URL,
    "X-BURNRATE-Request": "1",
    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
}
PASSWORD = "synthetic-password-long-123"


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
    setup_wizard._GLOBAL_THROTTLE.reset()
    for env_key in (
        "BURNRATE_CENTRAL_SERVER",
        "BURNRATE_HUB_ALLOW_PUBLIC",
        "BURNRATE_TRUSTED_PROXIES",
        "FORWARDED_ALLOW_IPS",
        "BURNRATE_HOST",
        "BURNRATE_ACCESS_TOKEN",
        "BURNRATE_ALLOWED_ORIGINS",
    ):
        monkeypatch.delenv(env_key, raising=False)
    yield
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()


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
# 1. Exposure bind guard (exposure.py, cli.py, service.py)
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
# 3. Per-client-IP login & registration throttling behind proxy (setup_wizard.py)
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
        assert locked.status_code == 429, locked.text
        assert locked.json()["error"]["code"] == "AUTH-102"
        assert locked.json()["error"]["retryAfter"] >= 1
        assert int(locked.headers["Retry-After"]) >= 1

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
        assert still_locked.status_code == 429
        assert still_locked.json()["error"]["code"] == "AUTH-102"

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
        assert locked.status_code == 429, locked.text
        assert locked.json()["error"]["code"] == "AUTH-102"
        assert locked.json()["error"]["retryAfter"] >= 1
        assert int(locked.headers["Retry-After"]) >= 1

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
        assert locked.status_code == 429, locked.text
        assert locked.json()["error"]["code"] == "AUTH-102"
        assert locked.json()["error"]["retryAfter"] >= 1
        assert int(locked.headers["Retry-After"]) >= 1

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


# ---------------------------------------------------------------------------
# 4. Deploy & docs verification
# ---------------------------------------------------------------------------


def test_h8_deploy_compose_is_tailnet_only_and_drops_deprecated_registration_vars() -> None:
    compose_path = ROOT / "deploy" / "hub" / "compose.yaml"
    raw = compose_path.read_text(encoding="utf-8")
    data = yaml.safe_load(raw)

    hub = data["services"]["hub"]
    caddy = data["services"]["caddy"]
    assert "@sha256:" in hub["image"]
    assert "@sha256:" in caddy["image"]

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


def test_h8_self_hosting_docs_cover_tailnet_only_and_exposure_controls() -> None:
    doc = (ROOT / "docs" / "self-hosting.md").read_text(encoding="utf-8")
    lowered = doc.lower()

    assert "tailnet-only" in lowered
    assert "no setup token" in lowered
    assert "embed the hub address" in lowered or "_build_config.py" in doc
    assert "create the owner account right after deploying" in lowered
    assert "BURNRATE_HUB_ALLOW_PUBLIC" in doc
    assert "BURNRATE_TRUSTED_PROXIES" in doc

    # Deprecated vars must only appear as deprecated/ignored notices, never as setup instructions
    assert "BURNRATE_SETUP_TOKEN` and\n   `BURNRATE_ALLOW_REGISTRATION` are deprecated and ignored" in doc
    assert "export BURNRATE_SETUP_TOKEN" not in doc
    assert "export BURNRATE_ALLOW_REGISTRATION" not in doc
    assert "BURNRATE_SETUP_TOKEN=" not in doc
    assert "BURNRATE_ALLOW_REGISTRATION=" not in doc
