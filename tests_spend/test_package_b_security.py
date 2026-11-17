"""Comprehensive security tests for Package B (Account and session surface).

Covers audit items M1, L2, L3, L5, L7, L15, L16, and N1:
- M1: CSRF and content-type/header gates on account and setup routes
- L2: Break-glass GET ?token= redirect, query stripping, and no duplicate sessions
- L3: OpenAPI / Swagger / ReDoc disabled (404)
- L5: value_error_handler logging + fixed error body vs. UserFacingError pass-through
- L7: Cookie-only sessions, no machine token on register/login, spoke join & NativeClient
- L15: hub_origin.hub_origin HTTPS enforcement for non-loopback hosts
- L16: Setup token min length, non-ASCII safety, and per-user + global throttling
- N1: Static bearer disabled by default without conftest override
"""
from __future__ import annotations

import logging
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_model, auth_throttle, hub_origin, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import LocationError
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.native_desktop import DesktopError, NativeClient
from spend_app.wealthfront_window import DesktopBridge

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.test"
ACCESS_TOKEN = "synthetic-break-glass-token-00"
SETUP_TOKEN = "synthetic-setup-token-32-chars-00"
PASSWORD = "synthetic-long-password-123"
HUB_UI = {"Origin": HUB_URL, "X-BURNRATE-Request": "1"}


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


def _settings(tmp_path: Path, name: str = "spend.db") -> Settings:
    return Settings(
        database_path=tmp_path / name,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _seed_event(db_path: Path, raw_id: str, tokens: int) -> None:
    from datetime import UTC, datetime

    now = datetime.now(UTC).isoformat()
    initialize(db_path)
    with connect(db_path) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id=raw_id,
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id=raw_id,
                project="synthetic",
                occurred_at=now,
                input_tokens=tokens,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=0,
                reasoning_tokens=0,
                cost_usd=None,
                computed_cost_usd=0.1,
                ingested_at=now,
                is_exact=True,
            ),
        )


@pytest.fixture(autouse=True)
def _clean_package_b_state():
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_TEST_CLIENTS.clear()
    aggregate.reset_memo()
    yield
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_TEST_CLIENTS.clear()
    aggregate.reset_memo()


def _make_hub_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_name: str = "hub.db",
    base_url: str = HUB_URL,
    setup_token: str | None = SETUP_TOKEN,
    access_token: str = ACCESS_TOKEN,
    allow_admin_bearer: str | None = "1",
) -> tuple[Settings, TestClient]:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", access_token)
    if setup_token is None:
        monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    else:
        monkeypatch.setenv("BURNRATE_SETUP_TOKEN", setup_token)
    if allow_admin_bearer is None:
        monkeypatch.delenv("BURNRATE_ALLOW_ADMIN_BEARER", raising=False)
    else:
        monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", allow_admin_bearer)
    settings = _settings(tmp_path, db_name)
    app = create_app(settings, enable_scheduler=False)
    return settings, TestClient(app, base_url=base_url)


def _make_local_client(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    db_name: str = "local.db",
    base_url: str = "http://127.0.0.1:17331",
) -> tuple[Settings, TestClient]:
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    settings = _settings(tmp_path, db_name)
    app = create_app(settings, enable_scheduler=False)
    return settings, TestClient(app, base_url=base_url)


# ---------------------------------------------------------------------------
# 1. M1: CSRF & content-type/header gates
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "path",
    ["/api/account/register", "/api/account/login"],
)
def test_m1_cross_site_and_form_posts_rejected_and_no_owner_created(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    settings, client = _make_hub_client(tmp_path, monkeypatch)
    payload_json = (
        f'{{"username":"attacker","password":"{PASSWORD}","setupToken":"{SETUP_TOKEN}"}}'
    )
    with client:
        # 1) Cross-site via Sec-Fetch-Site even with custom header & JSON
        res_fetch = client.post(
            path,
            headers={
                "X-BURNRATE-Request": "1",
                "Sec-Fetch-Site": "cross-site",
                "Content-Type": "application/json",
            },
            content=payload_json,
        )
        assert res_fetch.status_code == 403

        # 2) Foreign Origin
        res_origin = client.post(
            path,
            headers={
                "X-BURNRATE-Request": "1",
                "Origin": "https://evil.example",
                "Content-Type": "application/json",
            },
            content=payload_json,
        )
        assert res_origin.status_code == 403

        # 3) Null Origin
        res_null = client.post(
            path,
            headers={
                "X-BURNRATE-Request": "1",
                "Origin": "null",
                "Content-Type": "application/json",
            },
            content=payload_json,
        )
        assert res_null.status_code == 403

        # 4) Cross-site text/plain POST without X-BURNRATE-Request (form / no-cors)
        res_text_cross = client.post(
            path,
            headers={
                "Origin": "https://evil.example",
                "Content-Type": "text/plain",
            },
            content=payload_json,
        )
        assert res_text_cross.status_code == 403

        # 5) Same-origin text/plain POST without X-BURNRATE-Request -> 403
        res_text_no_hdr = client.post(
            path,
            headers={
                "Origin": HUB_URL,
                "Content-Type": "text/plain",
            },
            content=payload_json,
        )
        assert res_text_no_hdr.status_code == 403

        # 6) HTML form application/x-www-form-urlencoded POST -> 403 (or 415 with header)
        res_form = client.post(
            path,
            data={"username": "attacker", "password": PASSWORD, "setupToken": SETUP_TOKEN},
        )
        assert res_form.status_code == 403

        res_form_with_hdr = client.post(
            path,
            headers={"X-BURNRATE-Request": "1"},
            data={"username": "attacker", "password": PASSWORD, "setupToken": SETUP_TOKEN},
        )
        assert res_form_with_hdr.status_code == 415

    with connect(settings.database_path) as conn:
        accounts = setup_wizard._read_accounts(conn)
        assert accounts["users"] == {}


def test_m1_cookie_authenticated_account_post_requires_same_origin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        reg = client.post(
            "/api/account/register",
            headers=HUB_UI,
            json={
                "username": "owner@example.com",
                "password": PASSWORD,
                "sessionOnly": True,
            },
        )
        assert reg.status_code == 200
        assert auth_model.COOKIE in client.cookies

        # Missing Origin with cookie -> 403 "Cookie mutations require the same origin."
        for path in ("/api/account/login", "/api/account/register"):
            res = client.post(
                path,
                headers={"X-BURNRATE-Request": "1"},
                json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
            )
            assert res.status_code == 403
            assert res.json()["error"] == "Cookie mutations require the same origin."

            # Scheme mismatch (http vs https) with cookie -> 403 same message
            res_scheme = client.post(
                path,
                headers={"Origin": "http://hub.test", "X-BURNRATE-Request": "1"},
                json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
            )
            assert res_scheme.status_code == 403
            assert res_scheme.json()["error"] == "Cookie mutations require the same origin."

        # Same-origin Origin succeeds on login
        ok_login = client.post(
            "/api/account/login",
            headers=HUB_UI,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert ok_login.status_code == 200


@pytest.mark.parametrize(
    "path",
    ["/api/account/register", "/api/account/login"],
)
def test_m1_get_on_public_account_paths_not_allowed(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, path: str
) -> None:
    _, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        anon_get = client.get(path)
        assert anon_get.status_code == 401

        auth_get = client.get(path, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"})
        assert auth_get.status_code == 405


def test_m1_bounded_json_gates_across_account_and_setup_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client = _make_hub_client(tmp_path, monkeypatch)
    with hub_client:
        for path in ("/api/account/register", "/api/account/login"):
            missing_hdr = hub_client.post(
                path,
                headers={"Origin": HUB_URL, "Content-Type": "application/json"},
                content="{}",
            )
            assert missing_hdr.status_code == 403, (path, missing_hdr.text)

            wrong_ct = hub_client.post(
                path,
                headers={
                    "Origin": HUB_URL,
                    "X-BURNRATE-Request": "1",
                    "Content-Type": "text/plain",
                },
                content="{}",
            )
            assert wrong_ct.status_code == 415, (path, wrong_ct.text)

    _, local_client = _make_local_client(tmp_path, monkeypatch)
    with local_client:
        missing_hdr = local_client.post(
            "/api/setup/account",
            headers={"Origin": "http://127.0.0.1:17331", "Content-Type": "application/json"},
            content="{}",
        )
        assert missing_hdr.status_code == 403

        wrong_ct = local_client.post(
            "/api/setup/account",
            headers={
                "Origin": "http://127.0.0.1:17331",
                "X-BURNRATE-Request": "1",
                "Content-Type": "text/plain",
            },
            content="{}",
        )
        assert wrong_ct.status_code == 415


# ---------------------------------------------------------------------------
# 2. L2: Removed ?token= web-page redirects return REQ-404 and mint no cookies
# ---------------------------------------------------------------------------


def test_l2_break_glass_get_token_returns_req_404_and_sets_no_cookie(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        for path in (f"/?token={ACCESS_TOKEN}", f"/hud?token={ACCESS_TOKEN}&foo=bar"):
            res = client.get(path, follow_redirects=False)
            assert res.status_code == 404
            assert res.json()["error"]["code"] == "REQ-404"
            assert "location" not in res.headers
            assert "set-cookie" not in res.headers
            assert auth_model.COOKIE not in client.cookies
            assert ACCESS_TOKEN not in res.text

        with connect(settings.database_path) as conn:
            session_count = conn.execute("SELECT COUNT(*) FROM owner_sessions").fetchone()[0]
        assert session_count == 0


# ---------------------------------------------------------------------------
# 3. L3: OpenAPI / Docs disabled
# ---------------------------------------------------------------------------


def test_l3_openapi_and_docs_disabled(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _, local_client = _make_local_client(tmp_path, monkeypatch)
    with local_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert local_client.get(path).status_code == 404

    _, hub_client = _make_hub_client(tmp_path, monkeypatch)
    with hub_client:
        for path in ("/docs", "/redoc", "/openapi.json"):
            assert hub_client.get(path).status_code == 404
            assert (
                hub_client.get(
                    path, headers={"Authorization": f"Bearer {ACCESS_TOKEN}"}
                ).status_code
                == 404
            )


# ---------------------------------------------------------------------------
# 4. L5: value_error_handler and UserFacingError
# ---------------------------------------------------------------------------


def test_l5_user_facing_error_passes_message_through(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = _make_local_client(tmp_path, monkeypatch)
    headers = {"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"}
    with client:
        bad_threshold = client.post(
            "/api/capacity/forecast-preferences",
            headers=headers,
            json={"threshold": 0},
        )
        assert bad_threshold.status_code == 400
        assert bad_threshold.json() == {"error": "Threshold must be from 1 to 99"}

        bad_clock = client.post(
            "/api/capacity/forecast-preferences",
            headers=headers,
            json={"quietStart": "99:99"},
        )
        assert bad_clock.status_code == 400
        assert bad_clock.json() == {"error": "Quiet hours use HH:MM"}

        unknown_alert = client.post(
            "/api/capacity/forecast-alerts/dismiss",
            headers=headers,
            json={"id": "does-not-exist"},
        )
        assert unknown_alert.status_code == 400
        assert unknown_alert.json() == {"error": "Unknown alert"}


def test_l5_internal_value_error_logged_and_masked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _, client = _make_local_client(tmp_path, monkeypatch)
    headers = {"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"}
    with client:
        with caplog.at_level(logging.ERROR, logger="burnrate.api"):
            non_numeric = client.post(
                "/api/capacity/forecast-preferences",
                headers=headers,
                json={"threshold": "abc"},
            )
        assert non_numeric.status_code == 400
        assert non_numeric.json() == {"error": "invalid_request"}
        assert "invalid literal" not in non_numeric.text
        assert any(
            rec.name == "burnrate.api" and "Unhandled ValueError" in rec.message
            for rec in caplog.records
        )

        caplog.clear()

        def _boom(*_args, **_kwargs):
            raise ValueError("secret internal path C:/private/keys.db")

        monkeypatch.setattr("spend_app.capacity_forecast.save_preferences", _boom)
        with caplog.at_level(logging.ERROR, logger="burnrate.api"):
            internal_err = client.post(
                "/api/capacity/forecast-preferences",
                headers=headers,
                json={"threshold": 80},
            )
        assert internal_err.status_code == 400
        assert internal_err.json() == {"error": "invalid_request"}
        assert "secret internal path" not in internal_err.text
        assert "C:/private/keys.db" not in internal_err.text
        assert any(
            rec.name == "burnrate.api" and "secret internal path" in (rec.exc_text or "")
            for rec in caplog.records
        )


# ---------------------------------------------------------------------------
# 5. L7: Cookie-only sessions & no machine token on register/login
# ---------------------------------------------------------------------------


def test_l7_auth_endpoints_omit_session_token_and_machine_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        # Register with sessionOnly=False
        reg_full = client.post(
            "/api/account/register",
            headers=HUB_UI,
            json={
                "username": "owner@example.com",
                "password": PASSWORD,
                "sessionOnly": False,
            },
        )
        assert reg_full.status_code == 200, reg_full.text
        reg_json = reg_full.json()
        assert "sessionToken" not in reg_json
        for forbidden in ("machine", "token", "machines"):
            assert forbidden not in reg_json
        assert "burnrate_access=br_s_" in reg_full.headers.get("set-cookie", "")

        # Subsequent register fails closed (AUTH-103) and leaks nothing
        client.cookies.clear()
        reg_closed = client.post(
            "/api/account/register",
            headers=HUB_UI,
            json={
                "username": "owner@example.com",
                "password": PASSWORD,
                "sessionOnly": True,
            },
        )
        assert reg_closed.status_code == 403, reg_closed.text
        assert reg_closed.json()["error"] == {"code": "AUTH-103"}
        for forbidden in ("sessionToken", "machine", "token", "machines"):
            assert forbidden not in reg_closed.json()

        # Login with sessionOnly=False
        client.cookies.clear()
        login_full = client.post(
            "/api/account/login",
            headers=HUB_UI,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": False},
        )
        assert login_full.status_code == 200, login_full.text
        login_json = login_full.json()
        assert "sessionToken" not in login_json
        for forbidden in ("machine", "token", "machines"):
            assert forbidden not in login_json
        assert "burnrate_access=br_s_" in login_full.headers.get("set-cookie", "")

        # Login with sessionOnly=True
        client.cookies.clear()
        login_sess = client.post(
            "/api/account/login",
            headers=HUB_UI,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert login_sess.status_code == 200, login_sess.text
        assert "sessionToken" not in login_sess.json()
        for forbidden in ("machine", "token", "machines"):
            assert forbidden not in login_sess.json()
        assert "burnrate_access=br_s_" in login_sess.headers.get("set-cookie", "")

        # Neither register nor login created any remote machine
        with connect(settings.database_path) as conn:
            remote_machines = [
                m for m in sync.list_machines(conn) if m["id"] != "local"
            ]
        assert remote_machines == []

        # /api/auth/bootstrap omits sessionToken and sets cookie
        client.cookies.clear()
        boot = client.post(
            "/api/auth/bootstrap",
            headers={**HUB_UI, "Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={},
        )
        assert boot.status_code == 200, boot.text
        boot_json = boot.json()
        assert "sessionToken" not in boot_json
        assert boot_json["ok"] is True and boot_json["sessionId"] and boot_json["expiresAt"]
        assert "burnrate_access=br_s_" in boot.headers.get("set-cookie", "")

        # /api/auth/session/rotate omits sessionToken and sets new cookie
        rotated = client.post("/api/auth/session/rotate", headers=HUB_UI, json={})
        assert rotated.status_code == 200, rotated.text
        rot_json = rotated.json()
        assert "sessionToken" not in rot_json
        assert rot_json["ok"] is True and rot_json["sessionId"] != boot_json["sessionId"]
        assert "burnrate_access=br_s_" in rotated.headers.get("set-cookie", "")


def test_l7_spoke_join_register_login_owner_session_and_rotate_end_to_end(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_settings, hub_client = _make_hub_client(tmp_path, monkeypatch, db_name="hub.db")
    _seed_event(hub_settings.database_path, "hub-evt-1", 5000)

    spoke1_settings, spoke1 = _make_local_client(tmp_path, monkeypatch, db_name="spoke1.db")
    _seed_event(spoke1_settings.database_path, "spoke1-evt-1", 3000)

    spoke2_settings, spoke2 = _make_local_client(tmp_path, monkeypatch, db_name="spoke2.db")
    _seed_event(spoke2_settings.database_path, "spoke2-evt-1", 2000)

    local_ui = {"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"}

    with hub_client, spoke1, spoke2:
        monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)

        # 1) Spoke 1 joins via /api/setup/account (mode=register)
        res1 = spoke1.post(
            "/api/setup/account",
            headers=local_ui,
            json={
                "mode": "register",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Spoke One",
                "testSync": True,
            },
        )
        assert res1.status_code == 200, res1.text
        body1 = res1.json()
        assert body1["ok"] is True
        assert body1["syncResult"]["ok"] is True
        assert body1["setup"]["spokeConfig"]["hasOwnerSession"] is True
        assert body1["setup"]["spokeConfig"]["hasToken"] is True
        assert body1["account"]["machineId"]
        assert any(m["name"] == "Spoke One" for m in body1["machines"])

        from spend_app.connection_secrets import (
            MACHINE_TOKEN_VAULT_REF,
            OWNER_SESSION_VAULT_REF,
            get_vault,
        )

        with connect(spoke1_settings.database_path) as conn:
            sc1 = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc1
        assert not sc1.get("token")
        spoke1_vault = get_vault(spoke1_settings.database_path)
        initial_owner_session = spoke1_vault.get(OWNER_SESSION_VAULT_REF)
        assert initial_owner_session.startswith("br_s_")
        assert spoke1_vault.get(MACHINE_TOKEN_VAULT_REF).startswith("br_m_")

        # 2) Spoke 2 joins via /api/setup/account (mode=login)
        res2 = spoke2.post(
            "/api/setup/account",
            headers=local_ui,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Spoke Two",
                "testSync": True,
            },
        )
        assert res2.status_code == 200, res2.text
        body2 = res2.json()
        assert body2["ok"] is True
        assert body2["syncResult"]["ok"] is True
        assert body2["account"]["machineId"] != body1["account"]["machineId"]
        assert any(m["name"] == "Spoke Two" for m in body2["machines"])

        # Hub now aggregates all 3 machines' tokens (5000 + 3000 + 2000 = 10000)
        hub_summary = hub_client.get(
            "/api/spend/summary?window=1d&machine=all",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        assert hub_summary.status_code == 200
        assert hub_summary.json()["totals"]["tokens"] == 10000

        # 3) Spoke 1 signs in owner via DesktopBridge (never exposing br_s_ to caller)
        bridge = DesktopBridge(NativeClient(spoke1_settings, port=17331), vault=spoke1_vault)
        sign_in = bridge.sign_in_owner(
            "owner@example.com", PASSWORD, device_label="Spoke One Desktop"
        )
        assert sign_in["ok"] is True, sign_in
        assert sign_in["sessionId"]
        assert sign_in["expiresAt"]
        assert "sessionToken" not in sign_in
        assert "br_s_" not in str(sign_in)
        with connect(spoke1_settings.database_path) as conn:
            sc_after_signin = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc_after_signin
        session_before = spoke1_vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)
        assert session_before.startswith("br_s_")
        assert session_before != initial_owner_session

        listed = bridge.list_owner_sessions()
        assert listed["ok"] is True, listed
        assert listed["currentSessionId"] == sign_in["sessionId"]
        assert len(listed["sessions"]) >= 2
        assert "br_s_" not in str(listed)

        # 4) Spoke 1 rotates hub session via DesktopBridge
        rot = bridge.rotate_owner_session()
        assert rot["ok"] is True, rot
        assert "sessionToken" not in rot
        assert "br_s_" not in str(rot)
        assert rot["sessionId"] != sign_in["sessionId"]
        with connect(spoke1_settings.database_path) as conn:
            sc_after_rot = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc_after_rot
        session_after = spoke1_vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)
        assert session_after.startswith("br_s_")
        assert session_after != session_before

        # Old hub session is revoked; new hub session is valid on hub
        hub_client.cookies.clear()
        assert (
            hub_client.get(
                "/api/machines", headers={"Authorization": f"Bearer {session_before}"}
            ).status_code
            == 401
        )
        assert (
            hub_client.get(
                "/api/machines", headers={"Authorization": f"Bearer {session_after}"}
            ).status_code
            == 200
        )

        # 5) Spoke 1 revokes another active owner session via DesktopBridge
        listed_after_rot = bridge.list_owner_sessions()
        assert listed_after_rot["ok"] is True, listed_after_rot
        other_sids = [
            s["id"]
            for s in listed_after_rot["sessions"]
            if s["id"] != rot["sessionId"] and not s.get("revoked_at")
        ]
        assert other_sids
        target_sid = other_sids[0]
        rev = bridge.revoke_owner_session(target_sid)
        assert rev == {"ok": True}
        listed_after_rev = bridge.list_owner_sessions()
        assert listed_after_rev["ok"] is True
        revoked_row = next(s for s in listed_after_rev["sessions"] if s["id"] == target_sid)
        assert revoked_row.get("revoked_at") is not None


def test_l7_native_client_extracts_cookie_from_set_cookie_header_and_cookies_jar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import spend_app.native_desktop as desktop

    class FakeResponse:
        def __init__(self, status=200, payload=None, *, headers=None, cookies=None):
            self.status_code = status
            self._payload = payload
            self.headers = headers if headers is not None else {}
            self.cookies = cookies if cookies is not None else {}
            self.is_success = 200 <= status < 300

        def json(self):
            return self._payload

    def make_transport_factory(bootstrap_response: FakeResponse):
        calls = []

        class FakeClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                calls.append(("POST", url, kwargs))
                if url.endswith("/api/auth/bootstrap"):
                    return bootstrap_response
                return FakeResponse(payload={"ok": True})

            def get(self, url, **kwargs):
                calls.append(("GET", url, kwargs))
                return FakeResponse(payload={"total": {"events": 0}, "tools": []})

        return FakeClient, calls

    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "local-secret")

    # 1) Empty .cookies, Set-Cookie header fallback (httpx.Headers)
    hdrs = httpx.Headers(
        [("set-cookie", "burnrate_access=br_s_from_header; HttpOnly; Path=/; SameSite=Lax")]
    )
    factory1, calls1 = make_transport_factory(
        FakeResponse(payload={"ok": True, "sessionId": "s1"}, headers=hdrs, cookies={})
    )
    monkeypatch.setattr(desktop.httpx, "Client", factory1)
    client1 = NativeClient(_settings(tmp_path, "n1.db"), port=17432)
    assert client1.snapshot("today", "all")["tools"] == []
    assert calls1[-1][2]["headers"]["Authorization"] == "Bearer br_s_from_header"

    # 2) Populated .cookies jar, empty headers
    factory2, calls2 = make_transport_factory(
        FakeResponse(
            payload={"ok": True, "sessionId": "s2"},
            headers={},
            cookies={"burnrate_access": "br_s_from_jar"},
        )
    )
    monkeypatch.setattr(desktop.httpx, "Client", factory2)
    client2 = NativeClient(_settings(tmp_path, "n2.db"), port=17433)
    assert client2.snapshot("today", "all")["tools"] == []
    assert calls2[-1][2]["headers"]["Authorization"] == "Bearer br_s_from_jar"

    # 3) Missing cookie in both .cookies and headers raises DesktopError
    factory3, _ = make_transport_factory(
        FakeResponse(payload={"ok": True}, headers={}, cookies={})
    )
    monkeypatch.setattr(desktop.httpx, "Client", factory3)
    client3 = NativeClient(_settings(tmp_path, "n3.db"), port=17434)
    with pytest.raises(DesktopError, match="invalid session response"):
        client3.snapshot("today", "all")


# ---------------------------------------------------------------------------
# 6. L15: hub_origin HTTPS enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://hub.example",
        "http://192.168.1.10:8449",
        "http://[::2]:8449",
    ],
)
def test_l15_hub_origin_rejects_plain_http_non_loopback(bad_url: str) -> None:
    with pytest.raises(ValueError, match="HTTPS"):
        hub_origin.hub_origin(bad_url)


@pytest.mark.parametrize(
    ("good_url", "expected"),
    [
        ("https://hub.example", "https://hub.example"),
        ("http://localhost:8449", "http://localhost:8449"),
        ("http://127.0.0.1:8449", "http://127.0.0.1:8449"),
        ("http://127.0.1.5:8449", "http://127.0.1.5:8449"),
        ("http://[::1]:8449", "http://[::1]:8449"),
    ],
)
def test_l15_hub_origin_allows_https_and_loopback(good_url: str, expected: str) -> None:
    assert hub_origin.hub_origin(good_url) == expected


# ---------------------------------------------------------------------------
# 7. L16: Setup token hardening
# ---------------------------------------------------------------------------


def test_l16_deprecated_setup_env_vars_warn_without_blocking_startup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "x" * 31)
    monkeypatch.setenv("BURNRATE_ALLOW_REGISTRATION", "1")
    with caplog.at_level(logging.WARNING, logger="burnrate.setup"):
        create_app(_settings(tmp_path, "deprecated_hub.db"), enable_scheduler=False)
    messages = [rec.message for rec in caplog.records if rec.name == "burnrate.setup"]
    assert any("BURNRATE_SETUP_TOKEN" in m and "deprecated" in m.lower() for m in messages)
    assert any("BURNRATE_ALLOW_REGISTRATION" in m and "deprecated" in m.lower() for m in messages)

    caplog.clear()
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    with caplog.at_level(logging.WARNING, logger="burnrate.setup"):
        create_app(_settings(tmp_path, "local_short.db"), enable_scheduler=False)


def test_l16_non_ascii_setup_token_never_returns_500(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        # Non-ASCII UTF-8 in JSON body setupToken -> ignored on first registration (200), never 500
        res_body = client.post(
            "/api/account/register",
            headers=HUB_UI,
            json={
                "username": "owner@example.com",
                "password": PASSWORD,
                "setupToken": "tökën-ünicode-🔑-32-chars-padding",
            },
        )
        assert res_body.status_code == 200

        # Non-ASCII latin-1 in header X-BURNRATE-Setup-Token -> 403 AUTH-103 (closed), never 500
        res_hdr = client.post(
            "/api/account/register",
            headers=[(b"origin", HUB_URL.encode()), (b"x-burnrate-request", b"1"), (b"x-burnrate-setup-token", b"tok\xe9n-non-ascii-header")],
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert res_hdr.status_code == 403


def test_l16_failed_login_throttles_per_user_and_global(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = _make_hub_client(tmp_path, monkeypatch)
    with client:
        assert client.post(
            "/api/account/register",
            headers=HUB_UI,
            json={"username": "owner@example.com", "password": PASSWORD},
        ).status_code == 200
        client.cookies.clear()

        # Per-user throttle: MAX_FAILURES = 5
        for attempt in range(4):
            res = client.post(
                "/api/account/login",
                headers=HUB_UI,
                json={"username": "alice@example.com", "password": "wrong-password"},
            )
            assert res.status_code == 401, (attempt, res.text)

        locked = client.post(
            "/api/account/login",
            headers=HUB_UI,
            json={"username": "alice@example.com", "password": "wrong-password"},
        )
        assert locked.status_code == 429
        assert int(locked.headers["Retry-After"]) >= 1

        # Reset throttles and verify _GLOBAL_THROTTLE (max_failures=30 across distinct users)
        auth_throttle.reset()
        setup_wizard._GLOBAL_THROTTLE.reset()
        for idx in range(29):
            res = client.post(
                "/api/account/login",
                headers=HUB_UI,
                json={
                    "username": f"user-{idx:02d}@example.com",
                    "password": "wrong-password",
                },
            )
            assert res.status_code == 401, (idx, res.text)

        global_locked = client.post(
            "/api/account/login",
            headers=HUB_UI,
            json={"username": "brand-new-user@example.com", "password": "wrong-password"},
        )
        assert global_locked.status_code == 429
        assert int(global_locked.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------------------
# 8. N1: Static bearer disabled by default without conftest override
# ---------------------------------------------------------------------------


def test_n1_static_bearer_disabled_by_default_except_bootstrap(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, client = _make_hub_client(
        tmp_path,
        monkeypatch,
        allow_admin_bearer=None,
    )
    with client:
        denied = client.get(
            "/api/spend/summary?window=1d",
            headers={"Authorization": f"Bearer {ACCESS_TOKEN}"},
        )
        assert denied.status_code == 401

        boot = client.post(
            "/api/auth/bootstrap",
            headers={**HUB_UI, "Authorization": f"Bearer {ACCESS_TOKEN}"},
            json={},
        )
        assert boot.status_code == 200, boot.text
        assert "sessionToken" not in boot.json()
        session = boot.cookies[auth_model.COOKIE]

        allowed = client.get(
            "/api/spend/summary?window=1d",
            headers={"Authorization": f"Bearer {session}"},
        )
        assert allowed.status_code == 200


def _install_recording_hub_transport(
    monkeypatch: pytest.MonkeyPatch, bad_url: str = "http://hub.example"
) -> tuple[list[tuple[str, str]], list[tuple[str, str]]]:
    """Record any outbound HTTP attempt via either httpx.Client or _HUB_TEST_CLIENTS."""
    httpx_calls: list[tuple[str, str]] = []
    test_client_calls: list[tuple[str, str]] = []

    class _FakeResponse:
        status_code = 200
        content = b"{}"
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

        def json(self) -> dict:
            return {}

    class _RecordingHttpxClient:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *args) -> None:
            return None

        def post(self, url: str, *args, **kwargs):
            httpx_calls.append(("POST", str(url)))
            return _FakeResponse()

        def get(self, url: str, *args, **kwargs):
            httpx_calls.append(("GET", str(url)))
            return _FakeResponse()

        def request(self, method: str, url: str, *args, **kwargs):
            httpx_calls.append((method, str(url)))
            return _FakeResponse()

    class _RecordingTestClient:
        def post(self, url: str, *args, **kwargs):
            test_client_calls.append(("POST", str(url)))
            return _FakeResponse()

        def get(self, url: str, *args, **kwargs):
            test_client_calls.append(("GET", str(url)))
            return _FakeResponse()

    monkeypatch.setattr(httpx, "Client", _RecordingHttpxClient)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, bad_url, _RecordingTestClient())
    return httpx_calls, test_client_calls


@pytest.mark.parametrize("mode", ["register", "login"])
def test_l15_setup_account_rejects_plain_http_without_outbound_call_or_persistence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, mode: str
) -> None:
    spoke_settings, spoke_client = _make_local_client(tmp_path, monkeypatch, db_name="spoke_acct.db")
    monkeypatch.setenv("BURNRATE_HUB_URL", "http://hub.example")
    with spoke_client:
        httpx_calls, test_client_calls = _install_recording_hub_transport(monkeypatch, "http://hub.example")
        res = spoke_client.post(
            "/api/setup/account",
            headers={"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"},
            json={
                "mode": mode,
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Laptop",
            },
        )
    assert res.status_code == 422
    assert res.json()["error"] == {"code": "NET-003"}
    assert httpx_calls == []
    assert test_client_calls == []
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
    assert state["role"] == "hub"
    assert state["spokeConfig"]["enabled"] is False
    assert state["spokeConfig"]["hubUrl"] == ""
    assert state["spokeConfig"]["token"] == ""
    assert "serverUrl" not in state["account"]


def test_l15_saved_plain_http_config_blocked_at_egress_without_sending_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.pricing import PricingEngine

    spoke_settings, spoke_client = _make_local_client(tmp_path, monkeypatch, db_name="spoke_saved_http.db")
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "enabled": True,
            "hubUrl": "http://hub.example",
            "token": "br_m_saved_secret_token",
            "ownerSession": "br_s_saved_owner_session",
        }
        setup_wizard.write_setup_state(conn, state)

    with spoke_client:
        httpx_calls, test_client_calls = _install_recording_hub_transport(monkeypatch, "http://hub.example")
        local_headers = {"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"}

        # 1) Direct post_to_configured_hub strict/non-strict
        with pytest.raises(sync.HubMutationError, match="HTTPS") as exc_info:
            sync.post_to_configured_hub(
                spoke_settings, "/api/account/subscriptions", {"action": "plan"}, strict=True
            )
        assert exc_info.value.status_code == 422
        assert (
            sync.post_to_configured_hub(
                spoke_settings, "/api/account/subscriptions", {"action": "plan"}, strict=False
            )
            is None
        )

        # 2) Subscription mutation via HTTP endpoint
        sub_res = spoke_client.post(
            "/api/subscriptions",
            headers=local_headers,
            json={"operation": "add", "request_id": "req-1", "values": {}},
        )
        assert sub_res.status_code == 422
        assert "HTTPS" in sub_res.json()["error"]

        # 3) Spoke HTTP auth routes are closed locally (401 AUTH-107) and DesktopBridge fails closed with NET-003
        sessions_res = spoke_client.get("/api/auth/sessions")
        assert sessions_res.status_code == 401
        assert sessions_res.json()["error"] == {"code": "AUTH-107"}

        rotate_res = spoke_client.post("/api/auth/session/rotate", headers=local_headers, json={})
        assert rotate_res.status_code == 401
        assert rotate_res.json()["error"] == {"code": "AUTH-107"}

        logout_res = spoke_client.post("/api/auth/logout", headers=local_headers, json={})
        assert logout_res.status_code == 401
        assert logout_res.json()["error"] == {"code": "AUTH-107"}

        bridge = DesktopBridge(NativeClient(spoke_settings, port=17331), vault=_MemoryVault())
        for res in (
            bridge.sign_in_owner("owner@example.com", PASSWORD),
            bridge.list_owner_sessions(),
            bridge.rotate_owner_session(),
            bridge.sign_out_owner(),
            bridge.get_owner_audit(),
        ):
            assert res["ok"] is False
            assert res["code"] == "NET-003"

        # 4) Direct fetch_from_configured_hub
        assert sync.fetch_from_configured_hub(spoke_settings, "/api/spend/hud", ttl_seconds=0) is None

        # 5) Setup spoke owner-session / rotate-machine / background sync / sync_once
        owner_res = spoke_client.post(
            "/api/setup/owner-session",
            headers=local_headers,
            json={"username": "owner", "password": PASSWORD},
        )
        assert owner_res.status_code == 422
        assert owner_res.json()["error"] == {"code": "NET-003"}

        rot_machine_res = spoke_client.post("/api/setup/rotate-machine", headers=local_headers, json={})
        assert rot_machine_res.status_code == 422
        assert rot_machine_res.json()["error"] == {"code": "NET-003"}

        bg_res = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine.load(spoke_settings.pricing_path)
        )
        assert bg_res is not None and bg_res["ok"] is False and bg_res["error"] == "sync_failed"

        with pytest.raises(ValueError, match="HTTPS"):
            sync.sync_once(
                spoke_settings,
                PricingEngine.load(spoke_settings.pricing_path),
                hub_url="http://hub.example",
                token="br_m_saved_secret_token",
                collect_local=False,
            )

    assert httpx_calls == []
    assert test_client_calls == []
    with connect(spoke_settings.database_path) as conn:
        saved = setup_wizard.read_setup_state(conn)
    assert saved["spokeConfig"]["hubUrl"] == "http://hub.example"


def test_l15_loopback_http_stays_allowed_across_setup_and_egress(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.pricing import PricingEngine

    loopback_url = "http://127.0.0.1:8449"
    _, hub_client = _make_hub_client(tmp_path, monkeypatch, db_name="loopback_hub.db", base_url=loopback_url)
    _, spoke_client = _make_local_client(tmp_path, monkeypatch, db_name="loopback_spoke.db")
    spoke_settings = _settings(tmp_path, "loopback_spoke.db")
    with hub_client, spoke_client:
        monkeypatch.setenv("BURNRATE_HUB_URL", loopback_url)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, loopback_url, hub_client)

        acct_res = spoke_client.post(
            "/api/setup/account",
            headers={"Origin": "http://127.0.0.1:17331", "X-BURNRATE-Request": "1"},
            json={
                "mode": "register",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Loopback Spoke",
                "testSync": True,
            },
        )
        assert acct_res.status_code == 200, acct_res.text

        fetched = sync.fetch_from_configured_hub(spoke_settings, "/api/machines", ttl_seconds=0)
        assert isinstance(fetched, dict) and isinstance(fetched.get("machines"), list)

        bg_sync = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine.load(spoke_settings.pricing_path)
        )
        assert bg_sync is not None and bg_sync.get("ok") is True
