"""Offline H1 test suite: error codes (B1, B2, B3), zero raw text leaks, and UI sanity."""

from __future__ import annotations

import json
from pathlib import Path
import ssl

import httpx
import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_model, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import LocationError
from spend_app.db import connect, initialize
from spend_app.error_codes import (
    ERROR_CATALOG,
    HUB_PROTOCOL_VERSION,
    CodedError,
    error_message,
    error_response,
    format_error,
)
from spend_app.native_desktop import DesktopError, NativeClient
from spend_app.setup_wizard import _call_central_server_account
from spend_app.wealthfront_window import DesktopBridge

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.test"
HUB_HEADERS = {"Origin": HUB_URL, "X-BURNRATE-Request": "1"}
LOCAL_HEADERS = {"Origin": "http://testserver", "X-BURNRATE-Request": "1"}
SETUP_TOKEN = "synthetic-setup-token-32-chars-h1"
PASSWORD = "synthetic-password-long-123"
SENTINEL = "RAW_INTERNAL_TRACE_9981"

EXPECTED_CODES = {
    "NET-001",
    "NET-002",
    "NET-003",
    "AUTH-101",
    "AUTH-102",
    "AUTH-103",
    "AUTH-104",
    "AUTH-105",
    "AUTH-106",
    "AUTH-107",
    "DEV-301",
    "DEV-302",
    "REQ-401",
    "REQ-404",
    "SYNC-201",
    "HUB-501",
    "HUB-502",
    "APP-901",
    "APP-902",
}


class _MemoryVault:
    def __init__(self) -> None:
        self._items: dict[str, str] = {}

    def put(self, secret: str, ref: str = "") -> str:
        self._items[ref] = secret
        return ref

    def get(self, ref: str) -> str:
        if ref not in self._items:
            raise LocationError(f"missing ref: {ref}")
        return self._items[ref]

    def delete(self, ref: str) -> None:
        self._items.pop(ref, None)


def _make_settings(db_path: Path) -> Settings:
    settings = Settings(
        database_path=db_path,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    initialize(settings.database_path)
    return settings


class _FakeResponse:
    def __init__(
        self,
        status_code: int = 200,
        payload=None,
        *,
        text: str | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
        invalid_json: bool = False,
    ) -> None:
        self.status_code = status_code
        self._payload = payload
        self.invalid_json = invalid_json
        self.headers = headers or {}
        self.cookies = cookies or {}
        self.is_success = 200 <= status_code < 300
        if text is not None:
            self._text = text
        elif payload is not None:
            self._text = json.dumps(payload)
        else:
            self._text = ""
        self.content = self._text.encode("utf-8")

    @property
    def text(self) -> str:
        return self._text

    def json(self):
        if self.invalid_json:
            raise ValueError(f"Invalid JSON: {self._text}")
        return self._payload


class _LocalTestClientAdapter:
    """Bridges NativeClient's httpx.Client calls onto a local FastAPI TestClient."""

    def __init__(self, test_client: TestClient, base_url: str) -> None:
        self._tc = test_client
        self._base_url = base_url.rstrip("/")

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def _rel(self, url: str) -> str:
        if url.startswith(self._base_url):
            return url[len(self._base_url) :] or "/"
        return url

    def get(self, url: str, **kwargs):
        return self._tc.get(self._rel(url), follow_redirects=False, **kwargs)

    def post(self, url: str, **kwargs):
        return self._tc.post(self._rel(url), follow_redirects=False, **kwargs)


def _patch_native_to_local(monkeypatch, local_app, port: int = 17331) -> TestClient:
    import spend_app.native_desktop as desktop

    base_url = f"http://127.0.0.1:{port}"
    tc = TestClient(local_app, base_url=base_url)

    def _factory(*_args, **_kwargs):
        return _LocalTestClientAdapter(tc, base_url)

    monkeypatch.setattr(desktop.httpx, "Client", _factory)
    return tc


def _make_bridge(local_settings: Settings, monkeypatch, local_app, *, port: int = 17331):
    tc = _patch_native_to_local(monkeypatch, local_app, port=port)
    client = NativeClient(local_settings, port=port)
    bridge = DesktopBridge(client, preferences=local_settings.database_path.parent / "prefs.json")
    return bridge, client, tc


@pytest.fixture(autouse=True)
def _clean_shared_state(monkeypatch):
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    aggregate.reset_memo()
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    yield
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    aggregate.reset_memo()


def _create_hub_and_local(tmp_path: Path, monkeypatch, *, register_owner: bool = False):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP_TOKEN)
    hub_settings = _make_settings(tmp_path / "hub.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL)
    if register_owner:
        res = hub_client.post(
            "/api/account/register",
            headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={
                "username": "owner@example.com",
                "password": PASSWORD,
                "setupToken": SETUP_TOKEN,
                "sessionOnly": True,
            },
        )
        assert res.status_code == 200, res.text
        hub_client.cookies.clear()

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    local_settings = _make_settings(tmp_path / "local.db")
    local_app = create_app(local_settings, enable_scheduler=False)
    local_client = TestClient(local_app, base_url="http://testserver")
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
    return hub_settings, hub_app, hub_client, local_settings, local_app, local_client


def _login_owner_token(hub_client: TestClient) -> str:
    res = hub_client.post(
        "/api/account/login",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
    )
    assert res.status_code == 200, res.text
    token = res.cookies.get(auth_model.COOKIE)
    assert token
    hub_client.cookies.clear()
    return token


def _patch_httpx_for_fake_responses(monkeypatch, handler):
    class _FakeHttpxClient:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return False

        def get(self, url: str, **kwargs):
            return handler("GET", url, kwargs)

        def post(self, url: str, **kwargs):
            return handler("POST", url, kwargs)

    monkeypatch.setattr(httpx, "Client", _FakeHttpxClient)


# ---------------------------------------------------------------------------
# 1. All 17 ERROR_CATALOG codes produced by real endpoint / client / bridge
# ---------------------------------------------------------------------------


def test_error_catalog_has_all_19_codes() -> None:
    assert set(ERROR_CATALOG) == EXPECTED_CODES
    assert len(ERROR_CATALOG) == 19


def test_req_404_catalog_and_error_response() -> None:
    entry = ERROR_CATALOG["REQ-404"]
    assert entry["status"] == 404
    assert isinstance(entry["message"], str)
    assert "desktop app" in entry["message"].lower()
    assert error_message("REQ-404") == entry["message"]

    resp = error_response("REQ-404", status_code=404, include_message=True)
    assert resp.status_code == 404
    assert json.loads(resp.body) == {
        "error": {
            "code": "REQ-404",
            "message": entry["message"],
        }
    }


def test_net_001_transport_and_invalid_url_paths(tmp_path: Path, monkeypatch) -> None:
    local_settings = _make_settings(tmp_path / "local.db")
    local_app = create_app(local_settings, enable_scheduler=False)
    local_client = TestClient(local_app, base_url="http://testserver")
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    # 1a. Missing / invalid BURNRATE_HUB_URL on DesktopBridge.connect_hub_account
    for bad_url in ("", "   ", "ftp://hub.test", "://missing", "https://:8443"):
        monkeypatch.setenv("BURNRATE_HUB_URL", bad_url)
        res = bridge.connect_hub_account("owner@example.com", PASSWORD)
        assert res["ok"] is False
        assert res["code"] == "NET-001"
        assert res["error"] == format_error("NET-001")

    # 1b. Missing / invalid BURNRATE_HUB_URL on /api/setup/account
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    for bad_url in ("", "ftp://hub.test", "https://"):
        monkeypatch.setenv("BURNRATE_HUB_URL", bad_url)
        missing_url_res = local_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
        )
        assert missing_url_res.status_code == 422
        assert missing_url_res.json() == {"error": {"code": "NET-001"}}

    # 1c. Transport/connection error in _call_central_server_account and /api/setup/account
    class _UnreachableHub:
        def get(self, _path, **_kwargs):
            raise httpx.ConnectError("Connection refused")

        def post(self, _path, **_kwargs):
            raise httpx.ConnectError("Connection refused")

    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _UnreachableHub())
    with pytest.raises(CodedError) as exc_info:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert exc_info.value.code == "NET-001"

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert setup_res.status_code == 503
    assert setup_res.json() == {"error": {"code": "NET-001"}}

    bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "NET-001"
    assert "Error NET-001" in bridge_res["error"]


def test_net_002_host_and_origin_rejection(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    # 2a. Real hub Host rejection on /api/account/login -> 403 NET-002
    bad_host_res = hub_client.post(
        "/api/account/login",
        headers={
            "Host": "attacker.invalid",
            "Origin": "https://attacker.invalid",
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        },
        json={"username": "owner@example.com", "password": PASSWORD},
    )
    assert bad_host_res.status_code == 403
    assert bad_host_res.json() == {"error": {"code": "NET-002"}}

    # 2b. Fake hub returning 403 Host/CSRF/origin rejection -> mapped to NET-002 end-to-end
    class _HostRejectingHub:
        def get(self, path, **_kwargs):
            if path == "/api/hub/info":
                return _FakeResponse(
                    200,
                    {"service": "burnrate", "protocolVersion": HUB_PROTOCOL_VERSION},
                )
            return _FakeResponse(404, {})

        def post(self, _path, **_kwargs):
            return _FakeResponse(
                403,
                {"error": "Request rejected: unrecognized Host header."},
            )

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _HostRejectingHub())
    with pytest.raises(CodedError) as exc_info:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert exc_info.value.code == "NET-002"

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert setup_res.status_code == 403
    assert setup_res.json() == {"error": {"code": "NET-002"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "NET-002"
    assert "Error NET-002" in bridge_res["error"]


def test_net_003_tls_ssl_error_and_plain_http_rejection(tmp_path: Path, monkeypatch) -> None:
    local_settings = _make_settings(tmp_path / "local.db")
    local_app = create_app(local_settings, enable_scheduler=False)
    local_client = TestClient(local_app, base_url="http://testserver")
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    # 3a. ssl.SSLError during _call_central_server_account / /api/setup/account
    class _TlsFailingHub:
        def get(self, _path, **_kwargs):
            err = httpx.ConnectError("TLS handshake failed")
            err.__cause__ = ssl.SSLError("CERTIFICATE_VERIFY_FAILED: self-signed certificate")
            raise err

        def post(self, _path, **_kwargs):
            raise ssl.SSLError("CERTIFICATE_VERIFY_FAILED")

    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _TlsFailingHub())
    with pytest.raises(CodedError) as exc_info:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert exc_info.value.code == "NET-003"

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert setup_res.status_code == 502
    assert setup_res.json() == {"error": {"code": "NET-003"}}

    bridge_tls = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_tls["ok"] is False
    assert bridge_tls["code"] == "NET-003"
    assert "Error NET-003" in bridge_tls["error"]

    # 3b. Plain http:// non-loopback URL rejection
    with pytest.raises(CodedError) as exc_http:
        _call_central_server_account("http://hub.test", "/api/account/login", {"username": "a", "password": "b"})
    assert exc_http.value.code == "NET-003"

    monkeypatch.setenv("BURNRATE_HUB_URL", "http://hub.test")
    http_setup = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert http_setup.status_code == 422
    assert http_setup.json() == {"error": {"code": "NET-003"}}

    bridge_http = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_http["ok"] is False
    assert bridge_http["code"] == "NET-003"
    assert "Error NET-003" in bridge_http["error"]


def test_auth_101_wrong_password_and_unknown_user(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, _local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    wrong_pw = hub_client.post(
        "/api/account/login",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "owner@example.com", "password": "wrong-password-123"},
    )
    assert wrong_pw.status_code == 401
    assert wrong_pw.json() == {"error": {"code": "AUTH-101"}}

    unknown_user = hub_client.post(
        "/api/account/login",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "other@example.com", "password": PASSWORD},
    )
    assert unknown_user.status_code == 401
    assert unknown_user.json() == {"error": {"code": "AUTH-101"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    res_wrong = bridge.connect_hub_account("owner@example.com", "wrong-password-123")
    assert res_wrong["ok"] is False
    assert res_wrong["code"] == "AUTH-101"
    assert "Error AUTH-101" in res_wrong["error"]

    res_unknown = bridge.connect_hub_account("other@example.com", PASSWORD)
    assert res_unknown["ok"] is False
    assert res_unknown["code"] == "AUTH-101"
    assert "Error AUTH-101" in res_unknown["error"]


def test_auth_102_throttling_and_minute_pluralization(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, _local_app, _local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    # Real hub lockout on 5th failed login -> 429 AUTH-102 + Retry-After
    last_res = None
    for _ in range(auth_throttle.MAX_FAILURES):
        last_res = hub_client.post(
            "/api/account/login",
            headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={"username": "owner@example.com", "password": "wrong-password-123"},
        )
    assert last_res is not None
    assert last_res.status_code == 429
    body = last_res.json()
    assert body["error"]["code"] == "AUTH-102"
    retry_after = body["error"]["retryAfter"]
    assert isinstance(retry_after, int) and retry_after > 0
    assert int(last_res.headers["retry-after"]) == retry_after

    # Minute pluralization in format_error
    assert format_error("AUTH-102", retry_after=60) == "Too many attempts. Try again in 1 minute. Error AUTH-102"
    assert format_error("AUTH-102", retry_after=61) == "Too many attempts. Try again in 2 minutes. Error AUTH-102"
    assert format_error("AUTH-102", retry_after=None) == "Too many attempts. Try again in a few minutes. Error AUTH-102"

    # Singular vs plural propagation through NativeClient and DesktopBridge
    for secs, expected_unit in ((60, "1 minute."), (61, "2 minutes.")):
        _patch_httpx_for_fake_responses(
            monkeypatch,
            lambda _m, _u, _kw, s=secs: _FakeResponse(
                429,
                {"error": {"code": "AUTH-102", "retryAfter": s}},
                headers={"Retry-After": str(s)},
            ),
        )
        native = NativeClient(local_settings, port=17331)
        with pytest.raises(DesktopError) as exc_info:
            native.request("/api/setup/account", {"mode": "login"})
        assert exc_info.value.code == "AUTH-102"
        assert exc_info.value.status_code == 429
        assert expected_unit in str(exc_info.value)
        assert "Error AUTH-102" in str(exc_info.value)

        bridge = DesktopBridge(native, preferences=tmp_path / "prefs.json")
        bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
        assert bridge_res["ok"] is False
        assert bridge_res["code"] == "AUTH-102"
        assert expected_unit in bridge_res["error"]
        assert "Error AUTH-102" in bridge_res["error"]


def test_auth_103_hub_already_has_account(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    hub_reg = hub_client.post(
        "/api/account/register",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "second@example.com", "password": PASSWORD},
    )
    assert hub_reg.status_code == 403
    assert hub_reg.json() == {"error": {"code": "AUTH-103"}}

    setup_reg = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "register",
            "username": "second@example.com",
            "password": PASSWORD,
        },
    )
    assert setup_reg.status_code == 403
    assert setup_reg.json() == {"error": {"code": "AUTH-103"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account(
        "second@example.com", PASSWORD, mode="register"
    )
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "AUTH-103"
    assert "Error AUTH-103" in bridge_res["error"]


def test_auth_104_no_accounts_yet_on_hub(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=False
    )

    hub_login = hub_client.post(
        "/api/account/login",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "owner@example.com", "password": PASSWORD},
    )
    assert hub_login.status_code == 401
    assert hub_login.json() == {"error": {"code": "AUTH-104"}}

    setup_login = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
        },
    )
    assert setup_login.status_code == 401
    assert setup_login.json() == {"error": {"code": "AUTH-104"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD, mode="login")
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "AUTH-104"
    assert "Error AUTH-104" in bridge_res["error"]


def test_auth_105_short_password(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=False
    )

    hub_short = hub_client.post(
        "/api/account/register",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "owner@example.com", "password": "short-11-ch"},
    )
    assert hub_short.status_code == 422
    assert hub_short.json() == {"error": {"code": "AUTH-105"}}

    setup_short = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "register",
            "username": "owner@example.com",
            "password": "short-11-ch",
        },
    )
    assert setup_short.status_code == 422
    assert setup_short.json() == {"error": {"code": "AUTH-105"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account(
        "owner@example.com", "short-11-ch", mode="register"
    )
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "AUTH-105"
    assert "Error AUTH-105" in bridge_res["error"]


def test_auth_106_invalid_or_empty_username(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=False
    )

    for bad_user in ("", "   ", "a"):
        reg_res = hub_client.post(
            "/api/account/register",
            headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={"username": bad_user, "password": PASSWORD},
        )
        assert reg_res.status_code == 422
        assert reg_res.json() == {"error": {"code": "AUTH-106"}}

    login_empty = hub_client.post(
        "/api/account/login",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "   ", "password": PASSWORD},
    )
    assert login_empty.status_code == 422
    assert login_empty.json() == {"error": {"code": "AUTH-106"}}

    setup_empty = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "", "password": PASSWORD},
    )
    assert setup_empty.status_code == 422
    assert setup_empty.json() == {"error": {"code": "AUTH-106"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account("   ", PASSWORD)
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "AUTH-106"
    assert "Error AUTH-106" in bridge_res["error"]


def test_auth_107_expired_revoked_session_relay_and_b2_native_client_mapping(
    tmp_path: Path, monkeypatch
) -> None:
    hs, _ha, hub_client, local_settings, _local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    # 10a. Revoked / invalid owner session on protected hub routes -> 401 AUTH-107
    invalid_bearer = hub_client.get(
        "/api/spend/summary",
        headers={**HUB_HEADERS, "Authorization": "Bearer br_s_invalid_session_token"},
    )
    assert invalid_bearer.status_code == 401
    assert invalid_bearer.json() == {"error": {"code": "AUTH-107"}}

    # Pair local spoke via /api/setup/account, then revoke its hub owner session and verify /api/auth/sessions relay
    joined = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
            "computerName": "SpokeOne",
            "testSync": False,
        },
    )
    assert joined.status_code == 200, joined.text

    local_signin = local_client.post(
        "/api/setup/owner-session",
        headers=LOCAL_HEADERS,
        json={"username": "owner@example.com", "password": PASSWORD},
    )
    assert local_signin.status_code == 200, local_signin.text

    bridge = DesktopBridge(NativeClient(local_settings, port=17331), vault=_MemoryVault())
    assert bridge.owner_session_status()["hasOwnerSession"] is True

    # Revoke all active owner sessions in the hub DB
    with connect(hs.database_path) as conn:
        conn.execute("UPDATE owner_sessions SET revoked_at='2026-01-01T00:00:00Z' WHERE revoked_at IS NULL")
    sync.clear_hub_proxy_cache()

    relay_res = local_client.get("/api/auth/sessions", headers=LOCAL_HEADERS)
    assert relay_res.status_code == 401
    assert relay_res.json() == {"error": {"code": "AUTH-107"}}

    bridge_res = bridge.list_owner_sessions()
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "AUTH-107"
    assert bridge.owner_session_status()["hasOwnerSession"] is False

    # 10b. B2 check: NativeClient.request does NOT mask 401/403 as "could not authenticate this window"
    cases = [
        (401, None, "/api/spend/usage", None, "AUTH-107"),
        (401, {"error": "Session expired."}, "/api/auth/sessions", "wrong-access-token", "AUTH-107"),
        (403, {"error": "Request rejected: unrecognized Host header."}, "/api/setup/account", None, "NET-002"),
        (403, {"error": {"code": "NET-002"}}, "/api/spend/usage", None, "NET-002"),
        (403, {"error": {"code": "AUTH-103"}}, "/api/account/register", None, "AUTH-103"),
        (403, {"error": "This hub already has an account."}, "/api/account/register", None, "AUTH-103"),
    ]
    for status_code, payload, path, access_tok, expected_code in cases:
        if access_tok:
            monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", access_tok)
        else:
            monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
        _patch_httpx_for_fake_responses(
            monkeypatch,
            lambda _m, _u, _kw, sc=status_code, pl=payload: _FakeResponse(sc, pl),
        )
        native = NativeClient(local_settings, port=17331)
        with pytest.raises(DesktopError) as exc_info:
            native.request(path, {} if path.endswith(("register", "account")) else None)
        assert exc_info.value.code == expected_code
        assert "could not authenticate this window" not in str(exc_info.value).lower()


def test_dev_301_revoked_invalid_machine_token(tmp_path: Path, monkeypatch) -> None:
    hs, _ha, hub_client, local_settings, _local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    # Pair spoke, then revoke its machine token on the hub
    joined = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
            "computerName": "RevokeTarget",
            "testSync": False,
        },
    )
    assert joined.status_code == 200, joined.text
    machine_id = joined.json()["account"]["machineId"]
    sync.revoke_machine(hs.database_path, machine_id=machine_id)

    with connect(local_settings.database_path) as conn:
        revoked_token = setup_wizard.read_setup_state(conn)["spokeConfig"]["token"]

    # 11a. /api/sync/push with revoked/invalid br_m_ token -> 401 DEV-301
    push_res = hub_client.post(
        "/api/sync/push",
        headers={"Authorization": f"Bearer {revoked_token}", "Content-Type": "application/json"},
        json={"usageEvents": []},
    )
    assert push_res.status_code == 401
    assert push_res.json() == {"error": {"code": "DEV-301"}}

    # 11b. /api/setup/rotate-machine with revoked br_m_ token -> 401 DEV-301
    rotate_res = local_client.post("/api/setup/rotate-machine", headers=LOCAL_HEADERS, json={})
    assert rotate_res.status_code == 401
    assert rotate_res.json() == {"error": {"code": "DEV-301"}}

    # 11c. NativeClient.request bootstrap with br_m_ token against real hub app -> DesktopError DEV-301
    # Hub has a distinct break-glass secret while NativeClient holds the revoked br_m_ token.
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "hub-distinct-admin-secret-999")
    hub_app_2 = create_app(hs, enable_scheduler=False)
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", revoked_token)
    _patch_native_to_local(monkeypatch, hub_app_2, port=17332)
    native = NativeClient(hs, port=17332)
    with pytest.raises(DesktopError) as exc_info:
        native.request("/api/spend/usage")
    assert exc_info.value.code == "DEV-301"
    assert exc_info.value.status_code == 401
    assert "could not authenticate this window" not in str(exc_info.value).lower()


def test_dev_302_duplicate_machine_name_propagation(tmp_path: Path, monkeypatch) -> None:
    hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )
    owner_cookie = _login_owner_token(hub_client)

    # Register "SharedName" and revoke it -> re-registering "SharedName" without machine ID raises 422 DEV-302
    reg1 = hub_client.post(
        "/api/machines",
        headers={
            **HUB_HEADERS,
            "Authorization": f"Bearer {owner_cookie}",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        },
        json={"action": "register", "name": "SharedName"},
    )
    assert reg1.status_code == 200
    mid1 = reg1.json()["machine"]["id"]
    sync.revoke_machine(hs.database_path, machine_id=mid1)

    dup_reg = hub_client.post(
        "/api/machines",
        headers={
            **HUB_HEADERS,
            "Authorization": f"Bearer {owner_cookie}",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        },
        json={"action": "register", "name": "SharedName"},
    )
    assert dup_reg.status_code == 422
    assert dup_reg.json() == {"error": {"code": "DEV-302"}}

    # Also verify rename collision returns 422 DEV-302
    reg_a = sync.register_machine(hs.database_path, name="Alpha")
    reg_b = sync.register_machine(hs.database_path, name="Beta")
    rename_dup = hub_client.post(
        "/api/machines",
        headers={
            **HUB_HEADERS,
            "Authorization": f"Bearer {owner_cookie}",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        },
        json={"action": "rename", "id": reg_b["machine"]["id"], "name": reg_a["machine"]["name"]},
    )
    assert rename_dup.status_code == 422
    assert rename_dup.json() == {"error": {"code": "DEV-302"}}

    # Propagated via /api/setup/account and DesktopBridge.connect_hub_account (clash with active Alpha)
    setup_dup = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
            "computerName": "Alpha",
            "testSync": False,
        },
    )
    assert setup_dup.status_code == 422
    assert setup_dup.json() == {"error": {"code": "DEV-302"}}

    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)
    bridge_res = bridge.connect_hub_account(
        "owner@example.com", PASSWORD, computer_name="Alpha"
    )
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "DEV-302"
    assert "Error DEV-302" in bridge_res["error"]


def test_sync_201_initial_sync_failure_warning(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, _hub_client, _local_settings, _local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )

    def _boom(*_args, **_kwargs):
        raise RuntimeError("synthetic sync transport timeout")

    monkeypatch.setattr(setup_wizard, "sync_once", _boom)

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
            "computerName": "SpokeSyncFail",
            "testSync": True,
        },
    )
    assert setup_res.status_code == 200, setup_res.text
    payload = setup_res.json()
    assert payload["ok"] is True
    assert payload["syncResult"]["ok"] is False
    assert payload["syncResult"]["code"] == "SYNC-201"
    assert payload["syncResult"]["error"] == format_error("SYNC-201")

    # Separate local DB for DesktopBridge test
    local_settings_2 = _make_settings(tmp_path / "local2.db")
    local_app_2 = create_app(local_settings_2, enable_scheduler=False)
    bridge, _native, _tc = _make_bridge(local_settings_2, monkeypatch, local_app_2)
    bridge_res = bridge.connect_hub_account(
        "owner@example.com", PASSWORD, computer_name="SpokeSyncFail2"
    )
    assert bridge_res["ok"] is True
    assert bridge_res["warningCode"] == "SYNC-201"
    assert bridge_res["warning"].endswith("Error SYNC-201")


def test_hub_501_server_error_and_invalid_json(tmp_path: Path, monkeypatch) -> None:
    local_settings = _make_settings(tmp_path / "local.db")
    local_app = create_app(local_settings, enable_scheduler=False)
    local_client = TestClient(local_app, base_url="http://testserver")
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    for fake_resp in (
        _FakeResponse(500, {"error": "Internal Error"}),
        _FakeResponse(502, text="<html>Bad Gateway</html>", invalid_json=True),
        _FakeResponse(200, text="{broken-json", invalid_json=True),
    ):
        class _BrokenHub:
            def __init__(self, resp):
                self._resp = resp

            def get(self, path, **_kwargs):
                if path == "/api/hub/info":
                    return _FakeResponse(
                        200,
                        {"service": "burnrate", "protocolVersion": HUB_PROTOCOL_VERSION},
                    )
                return _FakeResponse(404, {})

            def post(self, _path, **_kwargs):
                return self._resp

        monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _BrokenHub(fake_resp))

        with pytest.raises(CodedError) as exc_info:
            _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
        assert exc_info.value.code == "HUB-501"

        setup_res = local_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
        )
        assert setup_res.status_code == 502
        assert setup_res.json() == {"error": {"code": "HUB-501"}}

        bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
        assert bridge_res["ok"] is False
        assert bridge_res["code"] == "HUB-501"
        assert "Error HUB-501" in bridge_res["error"]


def test_hub_502_b3_version_handshake_and_protocol_header(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )
    owner_cookie = _login_owner_token(hub_client)
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    # 15a. Real hub rejects incoming X-BURNRATE-Protocol: 99 with 426 HUB-502
    for path, payload, extra_headers in (
        ("/api/account/login", {"username": "owner@example.com", "password": PASSWORD}, {}),
        ("/api/account/register", {"username": "owner@example.com", "password": PASSWORD}, {}),
        ("/api/machines", {"action": "register", "name": "Test"}, {"Authorization": f"Bearer {owner_cookie}"}),
    ):
        res = hub_client.post(
            path,
            headers={**HUB_HEADERS, **extra_headers, "X-BURNRATE-Protocol": "99"},
            json=payload,
        )
        assert res.status_code == 426, path
        assert res.json() == {"error": {"code": "HUB-502"}}

    # 15b. Handshake variants producing HUB-502:
    # - /api/hub/info returns protocolVersion: 1
    # - /api/hub/info returns 404 and /healthz returns {"status": "ok"} without protocolVersion
    # - /api/hub/info returns 426
    class _OldHub:
        def __init__(self, mode: str) -> None:
            self.mode = mode

        def get(self, path, **_kwargs):
            if path == "/api/hub/info":
                if self.mode == "v1":
                    return _FakeResponse(200, {"service": "burnrate", "protocolVersion": 1})
                if self.mode == "legacy_healthz":
                    return _FakeResponse(404, {"detail": "Not Found"})
                return _FakeResponse(426, {"error": "Hub needs updating."})
            if path == "/healthz":
                return _FakeResponse(200, {"status": "ok"})
            return _FakeResponse(404, {})

        def post(self, _path, **_kwargs):
            raise AssertionError("POST should not be called when handshake fails")

    for mode in ("v1", "legacy_healthz", "426"):
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _OldHub(mode))
        with pytest.raises(CodedError) as exc_info:
            _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
        assert exc_info.value.code == "HUB-502"

        setup_res = local_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
        )
        assert setup_res.status_code == 426
        assert setup_res.json() == {"error": {"code": "HUB-502"}}

        bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
        assert bridge_res["ok"] is False
        assert bridge_res["code"] == "HUB-502"
        assert "Error HUB-502" in bridge_res["error"]


def test_app_901_native_client_and_oversized_json(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, _local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=False
    )

    # 16a. NativeClient.request transport/HTTP/JSON/status errors -> APP-901
    def _raise_http_error(_m, _u, _kw):
        raise httpx.ConnectError("Local daemon not running")

    _patch_httpx_for_fake_responses(monkeypatch, _raise_http_error)
    native = NativeClient(local_settings, port=17331)
    with pytest.raises(DesktopError) as exc_http:
        native.request("/api/spend/usage")
    assert exc_http.value.code == "APP-901"
    assert exc_http.value.status_code == 503

    for resp, expected_status in (
        (_FakeResponse(302, headers={"Location": "/auth"}), 302),
        (_FakeResponse(200, text="{not-json", invalid_json=True), 200),
        (_FakeResponse(503, {"detail": "busy"}), 503),
    ):
        _patch_httpx_for_fake_responses(monkeypatch, lambda _m, _u, _kw, r=resp: r)
        with pytest.raises(DesktopError) as exc_info:
            native.request("/api/spend/usage")
        assert exc_info.value.code == "APP-901"
        assert exc_info.value.status_code == expected_status

    # 16b. Oversized JSON (>64 KiB) -> 422 REQ-401
    huge_payload = {"mode": "register", "username": "owner@example.com", "password": PASSWORD, "pad": "x" * 70000}
    oversized_hub = hub_client.post(
        "/api/account/register",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json=huge_payload,
    )
    assert oversized_hub.status_code == 422
    assert oversized_hub.json() == {"error": {"code": "REQ-401"}}

    oversized_setup = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json=huge_payload,
    )
    assert oversized_setup.status_code == 422
    assert oversized_setup.json() == {"error": {"code": "REQ-401"}}


def test_app_902_b3_version_handshake_and_old_client_header(tmp_path: Path, monkeypatch) -> None:
    _hs, _ha, hub_client, local_settings, local_app, local_client = _create_hub_and_local(
        tmp_path, monkeypatch, register_owner=True
    )
    owner_cookie = _login_owner_token(hub_client)
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    # 17a. Real hub rejects incoming X-BURNRATE-Protocol: 1 with 426 APP-902
    for path, payload, extra_headers in (
        ("/api/account/login", {"username": "owner@example.com", "password": PASSWORD}, {}),
        ("/api/account/register", {"username": "owner@example.com", "password": PASSWORD}, {}),
        ("/api/machines", {"action": "register", "name": "Test"}, {"Authorization": f"Bearer {owner_cookie}"}),
    ):
        res = hub_client.post(
            path,
            headers={**HUB_HEADERS, **extra_headers, "X-BURNRATE-Protocol": "1"},
            json=payload,
        )
        assert res.status_code == 426, path
        assert res.json() == {"error": {"code": "APP-902"}}

    # 17b. /api/hub/info returns protocolVersion: 99 -> client & DesktopBridge surface APP-902
    class _NewerHub:
        def get(self, path, **_kwargs):
            if path == "/api/hub/info":
                return _FakeResponse(200, {"service": "burnrate", "protocolVersion": 99})
            return _FakeResponse(404, {})

        def post(self, _path, **_kwargs):
            raise AssertionError("POST should not be called when client is outdated")

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _NewerHub())
    with pytest.raises(CodedError) as exc_info:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert exc_info.value.code == "APP-902"

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert setup_res.status_code == 426
    assert setup_res.json() == {"error": {"code": "APP-902"}}

    bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_res["ok"] is False
    assert bridge_res["code"] == "APP-902"
    assert "Error APP-902" in bridge_res["error"]

    # 17c. Hub rejects incoming X-BURNRATE-Protocol: 2 with 426 client-upgrade response -> APP-902
    class _RequireV3Hub:
        def get(self, path, **_kwargs):
            if path == "/api/hub/info":
                return _FakeResponse(200, {"service": "burnrate", "protocolVersion": 2})
            return _FakeResponse(404, {})

        def post(self, _path, **_kwargs):
            return _FakeResponse(426, {"error": "Please update BURNRATE client."})

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _RequireV3Hub())
    with pytest.raises(CodedError) as exc_post:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert exc_post.value.code == "APP-902"

    bridge_post = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_post["ok"] is False
    assert bridge_post["code"] == "APP-902"


# ---------------------------------------------------------------------------
# 2. Never leak raw server text
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("status_code", "payload", "is_html"),
    [
        (401, {"error": f"Bad login: {SENTINEL}"}, False),
        (403, {"detail": f"Forbidden host: {SENTINEL}"}, False),
        (422, {"error": f"Validation trace: {SENTINEL}"}, False),
        (500, {"detail": f"Traceback: {SENTINEL}"}, False),
        (500, f"<html><body>{SENTINEL}</body></html>", True),
    ],
)
def test_never_leak_raw_hub_text(
    tmp_path: Path, monkeypatch, status_code: int, payload, is_html: bool
) -> None:
    local_settings = _make_settings(tmp_path / "local.db")
    local_app = create_app(local_settings, enable_scheduler=False)
    local_client = TestClient(local_app, base_url="http://testserver")
    bridge, _native, _tc = _make_bridge(local_settings, monkeypatch, local_app)

    class _LeakyHub:
        def get(self, path, **_kwargs):
            if path == "/api/hub/info":
                return _FakeResponse(
                    200,
                    {"service": "burnrate", "protocolVersion": HUB_PROTOCOL_VERSION},
                )
            return _FakeResponse(404, {})

        def post(self, _path, **_kwargs):
            if is_html:
                return _FakeResponse(status_code, text=payload, invalid_json=True)
            return _FakeResponse(status_code, payload)

    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _LeakyHub())

    with pytest.raises(CodedError) as exc_call:
        _call_central_server_account(HUB_URL, "/api/account/login", {"username": "a", "password": "b"})
    assert SENTINEL not in str(exc_call.value)
    assert SENTINEL not in repr(exc_call.value)

    setup_res = local_client.post(
        "/api/setup/account",
        headers=LOCAL_HEADERS,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD},
    )
    assert SENTINEL not in setup_res.text

    bridge_res = bridge.connect_hub_account("owner@example.com", PASSWORD)
    assert bridge_res["ok"] is False
    assert SENTINEL not in json.dumps(bridge_res)


def test_never_leak_raw_local_tracker_503_text(tmp_path: Path, monkeypatch) -> None:
    local_settings = _make_settings(tmp_path / "local.db")

    for resp in (
        _FakeResponse(503, {"error": f"SQLite panic: {SENTINEL}"}),
        _FakeResponse(503, {"detail": f"Internal detail: {SENTINEL}"}),
        _FakeResponse(503, text=f"<html><body>{SENTINEL}</body></html>", invalid_json=True),
    ):
        _patch_httpx_for_fake_responses(monkeypatch, lambda _m, _u, _kw, r=resp: r)
        native = NativeClient(local_settings, port=17331)

        with pytest.raises(DesktopError) as exc_native:
            native.request("/api/spend/usage")
        assert exc_native.value.code == "APP-901"
        assert SENTINEL not in str(exc_native.value)

        bridge = DesktopBridge(native, preferences=tmp_path / "prefs.json")
        monkeypatch.setattr(native, "ensure_service", lambda: None)

        res_connect = bridge.connect_hub_account("owner@example.com", PASSWORD)
        assert res_connect["ok"] is False
        assert SENTINEL not in json.dumps(res_connect)

        res_settings = bridge.get_settings_data()
        assert res_settings["ok"] is False
        assert SENTINEL not in json.dumps(res_settings)


# ---------------------------------------------------------------------------
# 3. UI error formatting sanity & /api/hub/info unauthenticated contract
# ---------------------------------------------------------------------------


def test_ui_error_formatting_includes_all_17_codes_and_warning_toast() -> None:
    wealthfront_html = (ROOT / "spend_app" / "wealthfront_ui.html").read_text(encoding="utf-8")
    assert "showToast(res.warning" in wealthfront_html


def test_hub_info_is_unauthenticated_with_protocol_header_and_zero_secrets(
    tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "super-secret-admin-token-99")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-secret-token-32-chars-minimum!")

    hub_settings = _make_settings(tmp_path / "hub.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL)

    # Unauthenticated GET (no Authorization header, no cookies)
    res = hub_client.get("/api/hub/info")
    assert res.status_code == 200
    assert res.json() == {"service": "burnrate", "protocolVersion": 2}
    assert res.headers.get("x-burnrate-protocol") == "2"

    low_body = res.text.lower()
    low_headers = json.dumps(dict(res.headers)).lower()
    for secret_marker in ("super-secret", "setup-secret", "br_s_", "br_m_", "br_bg_", "password", "hash", "salt"):
        assert secret_marker not in low_body
        assert secret_marker not in low_headers
