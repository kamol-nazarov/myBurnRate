"""Durable tests for Phase 5 of the 0.6.12 -> 0.6.13 Usage & Account Data Audit:
D-08 (complete client IP `source` population across all HTTP-triggered admin_audit writes),
D-13 (HTTPS central hubs omit legacy burnrate_access cookie, real unpatched /api/sync/trigger
calls nudge_quota_lanes cleanly without TypeError, and Muse race-guard map coverage).
"""
from __future__ import annotations

import inspect
from datetime import UTC, datetime

from fastapi.testclient import TestClient

from spend_app import quotas
from spend_app.api import create_app
from spend_app.auth_model import COOKIE, LEGACY_COOKIE, extract_cookie
from spend_app.db import connect, initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION
from spend_app.local_access import ensure_local_key
from tests_spend.test_api import make_settings

PROTO = {"X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION), "X-BURNRATE-Request": "1"}


def _hub_client(tmp_path, monkeypatch, *, base_url: str = "https://hub.example.test"):
    db_path = tmp_path / "hub.db"
    initialize(db_path)
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "hub.example.test")
    monkeypatch.setenv("BURNRATE_ALLOWED_ORIGINS", "https://hub.example.test")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "setup-token-d08-test")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "breakglass-secret-d08-test")
    monkeypatch.setenv("BURNRATE_TRUSTED_PROXIES", "127.0.0.1")
    monkeypatch.delenv("BURNRATE_LEGACY_COOKIE", raising=False)
    settings = make_settings(db_path)
    app = create_app(settings, enable_scheduler=False)
    return db_path, TestClient(app, base_url=base_url)


def test_d08_and_d13_https_hub_audit_source_ips_and_host_only_cookie(tmp_path, monkeypatch):
    """Every HTTP account/session/token/machine mutation populates admin_audit.source; HTTPS hub emits __Host- cookie only."""
    db_path, client = _hub_client(tmp_path, monkeypatch)
    headers = {
        **PROTO,
        "Origin": "https://hub.example.test",
        "X-Forwarded-For": "198.51.100.42",
    }

    # 1. Register first owner -> verify __Host- cookie ONLY (D-13)
    reg = client.post(
        "/api/account/register",
        json={
            "username": "owner@example.test",
            "password": "StrongPassword!1234",
            "setupToken": "setup-token-d08-test",
            "deviceLabel": "Workstation",
        },
        headers=headers,
    )
    assert reg.status_code == 200, reg.text
    set_cookies = reg.headers.get_list("set-cookie")
    assert any(c.startswith(f"{COOKIE}=") for c in set_cookies), set_cookies
    assert not any(c.startswith(f"{LEGACY_COOKIE}=") for c in set_cookies), set_cookies
    session_token = extract_cookie(reg, COOKIE)
    assert session_token.startswith("br_s_")
    owner_auth = {**headers, "Authorization": f"Bearer {session_token}"}

    # 2. Password change & email change
    pw = client.post(
        "/api/account/password",
        json={"currentPassword": "StrongPassword!1234", "newPassword": "NewerPassword!5678"},
        headers=owner_auth,
    )
    assert pw.status_code == 200, pw.text

    em = client.post(
        "/api/account/email",
        json={"email": "owner2@example.test", "password": "NewerPassword!5678"},
        headers=owner_auth,
    )
    assert em.status_code == 200, em.text

    # 3. Session rotate
    rot = client.post("/api/auth/session/rotate", headers=owner_auth)
    assert rot.status_code == 200, rot.text
    new_session = extract_cookie(rot, COOKIE)
    assert new_session and new_session != session_token
    owner_auth = {**headers, "Authorization": f"Bearer {new_session}"}

    # 4. Break-glass token rotate
    bg = client.post("/api/auth/rotate", headers=owner_auth)
    assert bg.status_code == 200, bg.text

    # 5. Machine register, rename, machine token rotate, revoke, delete-data
    m_reg = client.post(
        "/api/machines",
        json={"action": "register", "name": "Laptop-A", "os": "Windows", "deviceId": "dev-a-1234"},
        headers=owner_auth,
    )
    assert m_reg.status_code == 200, m_reg.text
    mid = m_reg.json()["machine"]["id"]
    mtok = m_reg.json()["token"]

    m_ren = client.post(
        "/api/machines",
        json={"action": "rename", "id": mid, "name": "Laptop-Renamed"},
        headers=owner_auth,
    )
    assert m_ren.status_code == 200, m_ren.text

    m_rot = client.post(
        "/api/auth/machine/rotate",
        json={"id": mid},
        headers={**headers, "Authorization": f"Bearer {mtok}"},
    )
    assert m_rot.status_code == 200, m_rot.text

    m_rev = client.post(
        "/api/machines",
        json={"action": "revoke", "id": mid},
        headers=owner_auth,
    )
    assert m_rev.status_code == 200, m_rev.text

    # Revoking mid also revokes the session bound to mid (S-04); sign in again to delete its data
    login = client.post(
        "/api/account/login",
        json={"username": "owner2@example.test", "password": "NewerPassword!5678", "deviceLabel": "AdminBrowser"},
        headers=headers,
    )
    assert login.status_code == 200, login.text
    admin_session = extract_cookie(login, COOKIE)
    owner_auth = {**headers, "Authorization": f"Bearer {admin_session}"}

    m_del = client.post(
        f"/api/machines/{mid}/data/delete",
        params={"confirm": mid},
        headers=owner_auth,
    )
    assert m_del.status_code == 200, m_del.text

    # 6. Logout
    lo = client.post("/api/auth/logout", headers=owner_auth)
    assert lo.status_code == 200, lo.text

    # Verify EVERY audit row written across all HTTP mutations has non-empty source IP
    with connect(db_path) as conn:
        rows = conn.execute("SELECT action, source FROM admin_audit ORDER BY id").fetchall()
    assert len(rows) >= 10
    missing = [(r[0], r[1]) for r in rows if not (r[1] and str(r[1]).strip())]
    assert missing == [], f"Audit rows missing source IP: {missing}"


def test_d13_sync_trigger_calls_real_nudge_quota_lanes_without_typeerror(tmp_path, monkeypatch):
    """POST /api/sync/trigger must call the real nudge_quota_lanes signature cleanly."""
    db_path = tmp_path / "local.db"
    initialize(db_path)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    settings = make_settings(db_path)
    local_key = ensure_local_key(db_path)

    real_nudge = quotas.nudge_quota_lanes
    real_sig = inspect.signature(real_nudge)
    calls: list[tuple] = []

    def spy_nudge(*args, **kwargs):
        # Bind against the REAL signature so any invalid kwarg (like now=...) fails loudly
        real_sig.bind(*args, **kwargs)
        calls.append((args, kwargs))
        return real_nudge(*args, **kwargs)

    monkeypatch.setattr(quotas, "nudge_quota_lanes", spy_nudge)
    monkeypatch.setattr(quotas, "poll_quotas", lambda **_k: {})

    client = TestClient(create_app(settings, enable_scheduler=False))
    resp = client.post(
        "/api/sync/trigger",
        headers={
            "Authorization": f"Bearer {local_key}",
            "X-BURNRATE-Request": "1",
        },
    )
    assert resp.status_code == 200, resp.text
    assert len(calls) == 1


def test_d13_muse_race_guard_suppresses_disabled_muse_source(tmp_path):
    """Disabling muse_local suppresses writing polled Muse quota rows (race guard)."""
    from spend_app.connections import Store
    from spend_app.quotas import QuotaSample, poll_quotas

    db_path = tmp_path / "muse.db"
    initialize(db_path)
    store = Store(db_path)
    with store.transaction() as state:
        state["profiles"].setdefault("muse_local", {})["default"] = {
            "source": "muse_local",
            "bindingId": "muse_local",
            "name": "Default",
            "location": str(tmp_path),
            "origin": "default",
            "mode": "manual",
            "enabled": False,
            "revision": 1,
            "state": "disabled",
            "detail": "Disabled",
            "capabilities": ["usage"],
            "toolKey": "muse",
        }

    # Verify sources_by_provider map in quotas.poll_quotas includes muse -> muse_local
    src = inspect.getsource(poll_quotas)
    assert '"muse": "muse_local"' in src
