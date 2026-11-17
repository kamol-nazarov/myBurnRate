"""Synthetic paired-spoke authorization regressions; no network or real data."""
from datetime import datetime, timedelta, timezone
import secrets
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_model, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import LocationError
from spend_app.connection_secrets import (
    MACHINE_TOKEN_VAULT_REF,
    OWNER_SESSION_VAULT_REF,
    clear_spoke_credentials,
    read_vault_secret,
)
from spend_app.db import UsageEvent, connect, upsert_usage_event
from spend_app.native_desktop import NativeClient
from spend_app.pricing import PricingEngine
from spend_app.wealthfront_window import DesktopBridge

ROOT = Path(__file__).resolve().parents[1]
HUB = "https://hub.test"
SETUP_TOKEN = "synthetic-setup-token-32-chars-00"
PASSWORD = "synthetic-password-long"
UI = {"Origin": "http://testserver", "X-BURNRATE-Request": "1"}
HUB_UI = {"Origin": HUB, "X-BURNRATE-Request": "1"}
ADMIN = {**HUB_UI, "Authorization": "Bearer synthetic-admin"}


class _MemoryVault:
    def __init__(self):
        self._items: dict[str, str] = {}

    def put(self, secret: str, ref: str | None = None) -> str:
        ref = ref or "burnrate-mem"
        self._items[ref] = secret
        return ref

    def get(self, ref: str) -> str:
        if ref not in self._items:
            raise LocationError("Saved credential unavailable.")
        return self._items[ref]

    def delete(self, ref: str) -> None:
        self._items.pop(ref, None)


def settings(path):
    return Settings(database_path=path, pricing_path=ROOT / "pricing", anthropic_admin_key=None,
                    openai_admin_key=None, timezone="UTC", cache_hit_threshold=.75,
                    over_routing_token_ceiling=40000)


def seed(path, raw_id, tokens):
    now = datetime.now(timezone.utc).isoformat()
    with connect(path) as conn:
        upsert_usage_event(conn, UsageEvent(raw_id=raw_id, source="codex_local", tool_key="codex",
            model_key="gpt-5.6-sol", session_id=raw_id, project="synthetic", occurred_at=now,
            input_tokens=tokens, cached_input_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0,
            output_tokens=0, reasoning_tokens=0, cost_usd=None, computed_cost_usd=.1,
            ingested_at=now, is_exact=True))


@pytest.fixture
def pair(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-admin")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP_TOKEN)
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()
    hs = settings(tmp_path / "hub.db")
    hub_app = create_app(hs, enable_scheduler=False)
    with TestClient(hub_app, base_url=HUB) as hub:
        registered = hub.post("/api/account/register", headers=HUB_UI, json={"username": "owner@example.com", "password": PASSWORD,
            "setupToken": SETUP_TOKEN, "sessionOnly": True})
        assert registered.status_code == 200, registered.text
        hub.cookies.clear()
        seed(hs.database_path, "hub-event", 11000)
        monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB, hub)
        monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
        monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
        ss = settings(tmp_path / "spoke.db")
        with TestClient(create_app(ss, enable_scheduler=False), base_url="http://testserver") as spoke:
            seed(ss.database_path, "spoke-event", 7000)
            joined = spoke.post("/api/setup/account", headers=UI, json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Laptop",
                "testSync": True,
            })
            assert joined.status_code == 200, joined.text
            # Leave sync connected without an active owner session so tests can verify sign-in/fallback.
            clear_spoke_credentials(ss.database_path, clear_owner_session=True)
            spoke.cookies.clear()
            sync.clear_hub_proxy_cache()
            yield hs, hub, ss, spoke
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()


def state(settings):
    with connect(settings.database_path) as conn:
        return setup_wizard.read_setup_state(conn)["spokeConfig"]


def machine_token_of(settings):
    assert not state(settings).get("token")
    return read_vault_secret(settings.database_path, MACHINE_TOKEN_VAULT_REF)


def owner_session_of(settings):
    assert "ownerSession" not in state(settings)
    return read_vault_secret(settings.database_path, OWNER_SESSION_VAULT_REF)


def summary(client):
    response = client.get("/api/spend/summary", params={"window": "1d", "machine": "all"})
    assert response.status_code == 200, response.text
    return response.json()


def sign_in(client):
    response = client.post("/api/setup/owner-session", headers=UI,
                           json={"username": "owner@example.com", "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response


def test_spoke_local_session_gates_hub_owner_actions(pair):
    hs, hub, ss, spoke = pair
    anonymous = TestClient(spoke.app, base_url="http://testserver")
    for method, path, body, expected in (
        ("GET", "/api/auth/sessions", None, 401),
        ("POST", "/api/auth/sessions/revoke", {"id": "unknown"}, 401),
        ("POST", "/api/auth/logout", {}, 401),
        ("POST", "/api/auth/session/rotate", {}, 401),
        ("POST", "/api/auth/rotate", {}, 403),
    ):
        response = (anonymous.get(path, headers=UI) if method == "GET"
                    else anonymous.post(path, headers=UI, json=body))
        assert response.status_code == expected, (path, response.status_code, response.text)
        assert "br_bg_" not in response.text and "br_s_" not in response.text

    ctx = anonymous.get("/api/auth/context")
    assert ctx.status_code == 200
    assert ctx.json() == {"spoke": True, "hasOwner": False, "localSession": False}

    bridge = DesktopBridge(NativeClient(ss, port=17331))
    signed = bridge.sign_in_owner("owner@example.com", PASSWORD)
    assert signed["ok"] is True
    assert signed["sessionId"] and signed["expiresAt"]
    assert "br_s_" not in str(signed)

    status = bridge.owner_session_status()
    assert status == {"ok": True, "hasOwnerSession": True, "expiresAt": signed["expiresAt"]}

    # Spoke HTTP /api/auth/* endpoints remain 401 (and /api/auth/rotate stays 403) even while signed in.
    for method, path, body, expected in (
        ("GET", "/api/auth/sessions", None, 401),
        ("POST", "/api/auth/sessions/revoke", {"id": "unknown"}, 401),
        ("POST", "/api/auth/logout", {}, 401),
        ("POST", "/api/auth/session/rotate", {}, 401),
        ("GET", "/api/auth/audit", None, 401),
        ("POST", "/api/auth/rotate", {}, 403),
    ):
        response = (spoke.get(path, headers=UI) if method == "GET"
                    else spoke.post(path, headers=UI, json=body))
        assert response.status_code == expected, (path, response.status_code, response.text)
        assert "br_bg_" not in response.text and "br_s_" not in response.text

    # Break-glass rotation is never relayed from a paired computer.
    with connect(hs.database_path) as conn:
        assert conn.execute("SELECT 1 FROM app_meta WHERE key='auth.break_glass.v1'").fetchone() is None

    listed = bridge.list_owner_sessions()
    assert listed["ok"] is True and listed["sessions"]
    assert listed["currentSessionId"] == signed["sessionId"]

    logged_out = bridge.sign_out_owner()
    assert logged_out == {"ok": True}
    assert not owner_session_of(ss)
    assert bridge.owner_session_status() == {"ok": True, "hasOwnerSession": False, "expiresAt": None}
    assert bridge.list_owner_sessions()["code"] == "AUTH-107"
    assert spoke.get("/api/auth/sessions", headers=UI).status_code == 401


def test_paired_spoke_owner_signin_logout_and_machine_sync(pair):
    hs, hub, ss, spoke = pair
    before = summary(spoke)
    assert before["totals"]["tokens"] == 7000
    machine_token = machine_token_of(ss)
    response = sign_in(spoke)
    assert response.json()["ok"] is True and response.json().get("expiresAt")
    owner_token = owner_session_of(ss)
    assert owner_token and owner_token != machine_token and owner_token not in response.text
    wide = summary(spoke)
    assert wide["totals"]["tokens"] == 18000
    assert len(wide["machines"]) == 2
    bridge = DesktopBridge(NativeClient(ss, port=17331))
    logout = bridge.sign_out_owner()
    assert logout == {"ok": True}
    assert not owner_session_of(ss)
    assert bridge.owner_session_status()["hasOwnerSession"] is False
    hub.cookies.clear()
    assert hub.get("/api/machines", headers={"Authorization": "Bearer " + owner_token}).status_code == 401
    assert summary(spoke)["totals"]["tokens"] == 7000
    result = sync.sync_once(ss, PricingEngine.load(ss.pricing_path), hub_url=HUB, token=machine_token,
                            machine_name="Laptop", collect_local=False, client=hub)
    assert result["ok"]


@pytest.mark.parametrize("condition", ["revoked", "expired"])
def test_rejected_owner_session_discards_wide_cache_and_falls_back(pair, condition):
    hs, hub, ss, spoke = pair
    sign_in(spoke)
    token = owner_session_of(ss)
    assert summary(spoke)["totals"]["tokens"] == 18000
    with connect(hs.database_path) as conn:
        if condition == "expired":
            conn.execute("UPDATE owner_sessions SET expires_at=? WHERE token_hash=?",
                         (auth_model.stamp(auth_model.moment() - timedelta(seconds=1)), auth_model.digest(token)))
        else:
            conn.execute("UPDATE owner_sessions SET revoked_at=? WHERE token_hash=?",
                         (auth_model.stamp(auth_model.moment()), auth_model.digest(token)))
    # Force a fresh hub validation while preserving the cached wide payload.
    assert sync.fetch_from_configured_hub(ss, "/api/spend/summary",
        {"window": "1d", "tool": "all", "machine": "all"}, ttl_seconds=0) is None
    assert summary(spoke)["totals"]["tokens"] == 7000
    assert summary(spoke)["totals"]["tokens"] == 7000


def test_spoke_machine_rotation_persists_replacement(pair):
    _, hub, ss, spoke = pair
    old = machine_token_of(ss)
    response = spoke.post("/api/setup/rotate-machine", headers=UI, json={})
    assert response.status_code == 200, response.text
    new = machine_token_of(ss)
    assert new != old and new not in response.text
    assert hub.post("/api/sync/push", headers={"Authorization": "Bearer " + old}, json={}).status_code == 401
    result = sync.sync_once(ss, PricingEngine.load(ss.pricing_path), hub_url=HUB, token=new,
                            machine_name="Laptop", collect_local=False, client=hub)
    assert result["ok"]


def test_spoke_login_refuses_revoked_machine_name_and_expired_rotation_deadline(pair, tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    hs, hub, ss, spoke = pair
    with connect(ss.database_path) as conn:
        machine_id = setup_wizard.read_setup_state(conn)["account"]["machineId"]

    # Stamp rotation_required within grace window -> rotate-machine succeeds.
    with connect(hs.database_path) as conn:
        hub_state = sync._read_state(conn)
        hub_state["machines"][machine_id]["rotation_required"] = True
        hub_state["machines"][machine_id]["rotation_deadline"] = auth_model.stamp(
            auth_model.moment() + timedelta(days=2)
        )
        sync._write_state(conn, hub_state)
    ok_rotate = spoke.post("/api/setup/rotate-machine", headers=UI, json={})
    assert ok_rotate.status_code == 200, ok_rotate.text

    # Past rotation_deadline -> rotate-machine is rejected with 401.
    with connect(hs.database_path) as conn:
        hub_state = sync._read_state(conn)
        hub_state["machines"][machine_id]["rotation_required"] = True
        hub_state["machines"][machine_id]["rotation_deadline"] = auth_model.stamp(
            auth_model.moment() - timedelta(seconds=1)
        )
        sync._write_state(conn, hub_state)
    expired_rotate = spoke.post("/api/setup/rotate-machine", headers=UI, json={})
    assert expired_rotate.status_code == 401, expired_rotate.text

    # Revoking the machine on the hub prevents name-only revival, and a new spoke (distinct deviceId) gets a new record.
    sync.revoke_machine(hs.database_path, machine_id=machine_id)
    name_only = hub.post("/api/machines", headers=ADMIN, json={"action": "register", "name": "laptop"})
    assert name_only.status_code == 422, name_only.text
    fresh = settings(tmp_path / "fresh-revoked.db")
    with TestClient(create_app(fresh, enable_scheduler=False), base_url="http://testserver") as fresh_spoke:
        joined = fresh_spoke.post("/api/setup/account", headers=UI, json={
            "mode": "login", "username": "owner@example.com", "password": PASSWORD,
            "computerName": "laptop", "testSync": False,
        })
        assert joined.status_code == 200, joined.text
        assert joined.json()["account"]["machineId"] != machine_id
    with connect(hs.database_path) as conn:
        assert sync._read_state(conn)["machines"][machine_id]["revoked"] is True


def test_setup_account_on_synced_spoke_preserves_machine_identity(pair, monkeypatch):
    _, hub, ss, spoke = pair
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    original_token = machine_token_of(ss)
    response = spoke.post("/api/setup/account", headers=UI, json={"mode": "login", "username": "owner@example.com",
        "password": PASSWORD, "computerName": "Second laptop", "testSync": True})
    assert response.status_code == 200, response.text
    owner_tok = owner_session_of(ss)
    mach_tok = machine_token_of(ss)
    assert owner_tok and mach_tok and owner_tok != mach_tok
    assert owner_tok not in response.text and mach_tok not in response.text
    assert mach_tok == original_token
    assert summary(spoke)["totals"]["tokens"] == 18000
    assert len(summary(spoke)["machines"]) == 2




def test_fresh_setup_account_enables_owner_view(pair, tmp_path, monkeypatch):
    _, hub, _, _ = pair
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    fresh = settings(tmp_path / "fresh.db")
    with TestClient(create_app(fresh, enable_scheduler=False), base_url="http://testserver") as spoke:
        seed(fresh.database_path, "fresh-event", 3000)
        response = spoke.post("/api/setup/account", headers=UI, json={"mode": "login", "username": "owner@example.com",
            "password": PASSWORD, "computerName": "Fresh laptop", "testSync": True})
        assert response.status_code == 200, response.text
        owner_tok = owner_session_of(fresh)
        mach_tok = machine_token_of(fresh)
        assert owner_tok and mach_tok and owner_tok != mach_tok
        assert owner_tok not in response.text and mach_tok not in response.text
        assert summary(spoke)["totals"]["tokens"] == 21000
        assert len(summary(spoke)["machines"]) == 3




def test_nonempty_machine_projections_and_forged_raw_id(pair):
    hs, hub, _, _ = pair
    own = sync.register_machine(hs.database_path, name="Own synthetic computer")
    other = sync.register_machine(hs.database_path, name="PRIVATE_OTHER_COMPUTER")
    own_id, other_id = own["machine"]["id"], other["machine"]["id"]
    now = datetime.now(timezone.utc).isoformat()
    def push(reg, event):
        return hub.post("/api/sync/push", headers={"Authorization": "Bearer " + reg["token"]},
                        json={"usageEvents": [event], "sessions": [], "agentRuns": []})
    first = {"raw_id": "own-event", "source": "codex_local", "tool_key": "codex",
             "model_key": "gpt-5.6-sol", "session_id": "own-session", "project": "own-project",
             "occurred_at": now, "input_tokens": 7000, "output_tokens": 3000}
    second = {"raw_id": "PRIVATE_OTHER_EVENT", "source": "claude_local", "tool_key": "claude-code",
              "model_key": "claude-opus-4-6", "session_id": "PRIVATE_OTHER_SESSION",
              "project": "PRIVATE_OTHER_PROJECT", "occurred_at": now, "input_tokens": 23000,
              "output_tokens": 2000}
    assert push(own, first).status_code == 200
    assert push(other, second).status_code == 200
    headers = {"Authorization": "Bearer " + own["token"]}
    for path in ("/api/spend/usage", "/api/spend/summary", "/api/spend/entity"):
        response = hub.get(path, params={"machine": own_id, "kind": "tool", "key": "codex"}, headers=headers)
        assert response.status_code == 200, response.text
        data = response.json()
        assert data["machine"] == own_id and data["scope"] == "computer"
        assert data["total"]["tokens"] == 10000 and data["total"]["events"] == 1
        assert [(tool["key"], tool["tokens"]) for tool in data["tools"]] == [("codex", 10000)]
        assert data["tools"][0]["models"][0]["key"] == "gpt-5.6-sol"
        assert "PRIVATE_OTHER" not in response.text and other_id not in response.text
        assert "claude-opus-4-6" not in response.text
        assert "plans" not in data and "machines" not in data and "settings" not in data
    forged = {**first, "raw_id": f"m:{other_id}:PRIVATE_OTHER_EVENT"}
    response = push(own, forged)
    with connect(hs.database_path) as conn:
        rows = {row["raw_id"]: row["input_tokens"] for row in conn.execute("SELECT raw_id, input_tokens FROM usage_events UNION ALL SELECT raw_id, input_tokens FROM unpriced_usage_events")}
    assert rows[f"m:{other_id}:PRIVATE_OTHER_EVENT"] == 23000
    assert response.status_code == 403, response.text




def test_hud_fallback_excludes_old_hub_imports_and_shows_signin(pair):
    _, _, ss, spoke = pair
    seed(ss.database_path, "m:old-hub:PRIVATE_IMPORTED_USAGE", 31000)
    for path in ("/api/spend/hud", "/api/spend/hud/usage"):
        response = spoke.get(path, params={"machine": "all"})
        assert response.status_code == 200, response.text
        payload = response.json()
        assert payload["scope"] == "local-fallback"
        assert payload["warning"]
        assert "authUrl" not in payload
        if path.endswith("/usage"):
            assert payload["navigation"]["todayTokens"] == 7000
        else:
            assert payload["selectedMachine"] == "local"
        assert "PRIVATE_IMPORTED_USAGE" not in response.text
    removed = spoke.get("/hud")
    assert removed.status_code == 404
    assert removed.json()["error"]["code"] == "REQ-404"


def test_local_token_protected_first_registration_requires_owner(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    recovery = secrets.token_urlsafe(32)
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", recovery)
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    app = create_app(settings(tmp_path / "protected.db"), enable_scheduler=False)
    payload = {"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True}
    with TestClient(app) as client:
        denied = client.post("/api/account/register", headers={"X-BURNRATE-Request": "1"}, json=payload)
        assert denied.status_code in (401, 403), denied.text
        assert "sessionToken" not in denied.text
        registered = client.post("/api/account/register", json=payload,
            headers={"Authorization": "Bearer " + recovery, "X-BURNRATE-Request": "1"})
        assert registered.status_code == 200, registered.text
        assert "sessionToken" not in registered.text
        assert registered.cookies[auth_model.COOKIE].startswith("br_s_")


def test_rotated_recovery_remains_protected_after_environment_removed(tmp_path, monkeypatch):
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    recovery = secrets.token_urlsafe(32)
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", recovery)
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    local = settings(tmp_path / "restart.db")
    with TestClient(create_app(local, enable_scheduler=False)) as client:
        rotated = client.post("/api/auth/rotate", headers={"Authorization": "Bearer " + recovery})
        assert rotated.status_code == 200, rotated.text
        new = rotated.json()["token"]
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    with TestClient(create_app(local, enable_scheduler=False)) as client:
        assert client.get("/api/machines").status_code == 401
        denied = client.post("/api/account/register", headers={"X-BURNRATE-Request": "1"}, json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True})
        assert denied.status_code in (401, 403) and "sessionToken" not in denied.text
        old = client.post("/api/auth/bootstrap", headers={"Authorization": "Bearer " + recovery})
        assert old.status_code == 401
        fresh = client.post("/api/auth/bootstrap", headers={"Authorization": "Bearer " + new})
        assert fresh.status_code == 200 and "sessionToken" not in fresh.text
        assert fresh.cookies[auth_model.COOKIE].startswith("br_s_")


def test_concurrent_breakglass_rotation_is_single_winner(tmp_path, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    recovery = secrets.token_urlsafe(32)
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", recovery)
    gate = Barrier(2, timeout=10)
    original = auth_model.break_glass_matches
    def synchronized_match(path, candidate, configured):
        result = original(path, candidate, configured)
        assert result, "Both requests must validate the original credential before rotation"
        gate.wait()
        return result
    monkeypatch.setattr(auth_model, "break_glass_matches", synchronized_match)
    with TestClient(create_app(settings(tmp_path / "concurrent.db"), enable_scheduler=False)) as client:
        def rotate(_):
            return client.post("/api/auth/rotate", headers={"Authorization": "Bearer " + recovery})
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(rotate, range(2)))
        assert sorted(response.status_code for response in responses) == [200, 401]
        assert sum("token" in response.json() for response in responses) == 1



def test_session_rotation_and_revocation_failures_are_audited_once(pair):
    hs, hub, _, _ = pair
    bad_token = "br_s_synthetic-invalid-secret"
    hub.cookies.clear()
    failed_rotation = hub.post("/api/auth/session/rotate",
                               headers={"Authorization": "Bearer " + bad_token}, json={})
    assert failed_rotation.status_code == 401

    def rows(action):
        with connect(hs.database_path) as conn:
            return [dict(row) for row in conn.execute(
                "SELECT * FROM admin_audit WHERE action=? ORDER BY id", (action,))]

    rotations = rows("session.rotate")
    assert len(rotations) == 1 and rotations[0]["outcome"] == "failure"
    assert rotations[0]["target"] == "/api/auth/session/rotate"
    unknown = "synthetic-arbitrary-secret-in-id"
    denied = hub.post("/api/auth/sessions/revoke", headers=ADMIN, json={"id": unknown})
    assert denied.status_code == 404
    revocations = rows("session.revoke")
    assert len(revocations) == 1 and revocations[0]["outcome"] == "failure"
    assert revocations[0]["target"] == "/api/auth/sessions/revoke"
    assert unknown not in str(revocations) and bad_token not in str(rotations)

    login = hub.post("/api/account/login", headers=HUB_UI, json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True})
    assert login.status_code == 200, login.text
    session_id = login.json()["sessionId"]
    hub.cookies.clear()
    revoked = hub.post("/api/auth/sessions/revoke", headers=ADMIN, json={"id": session_id})
    assert revoked.status_code == 200, revoked.text
    revocations = rows("session.revoke")
    assert len(revocations) == 2
    assert sum(row["outcome"] == "success" and row["target"] == session_id for row in revocations) == 1
    again = hub.post("/api/auth/sessions/revoke", headers=ADMIN, json={"id": session_id})
    assert again.status_code == 404
    revocations = rows("session.revoke")
    assert len(revocations) == 3 and revocations[-1]["outcome"] == "failure"
    assert revocations[-1]["target"] == "/api/auth/sessions/revoke"


def test_paired_spoke_blocks_local_owner_creation_but_hub_signin_works(pair):
    _, _, ss, spoke = pair
    page = spoke.get("/auth")
    assert page.status_code == 404
    assert page.json()["error"]["code"] == "REQ-404"
    before = owner_session_of(ss)
    with connect(ss.database_path) as conn:
        assert not setup_wizard._read_accounts(conn)["users"]
    attempt = spoke.post("/api/account/register", headers=UI, json={"username": "synthetic-local-owner@example.com",
                         "password": PASSWORD, "sessionOnly": True})
    assert attempt.status_code == 403, attempt.text
    assert "sessionToken" not in attempt.text and owner_session_of(ss) == before
    with connect(ss.database_path) as conn:
        assert not setup_wizard._read_accounts(conn)["users"]
    sign_in(spoke)
    assert owner_session_of(ss)
    assert summary(spoke)["totals"]["tokens"] == 18000


def test_new_identity_reconnect_resends_local_usage_and_keeps_owner_session(pair):
    hs, hub, ss, spoke = pair
    sign_in(spoke)
    hub.cookies.clear()
    assert summary(spoke)["totals"]["tokens"] == 18000
    with connect(ss.database_path) as conn:
        current = setup_wizard.read_setup_state(conn)
        old_id = current["account"]["machineId"]
        current["spokeConfig"]["enabled"] = False
        setup_wizard.write_setup_state(conn, current)
        conn.execute("DELETE FROM app_meta WHERE key='device_id.v1'")
    repaired = spoke.post("/api/setup/account", headers=UI, json={
        "mode": "login",
        "username": "owner@example.com",
        "password": PASSWORD,
        "computerName": "Replacement laptop",
        "testSync": True,
    })
    assert repaired.status_code == 200, repaired.text
    assert (owner_session_of(ss) or "").startswith("br_s_")
    with connect(ss.database_path) as conn:
        new_id = setup_wizard.read_setup_state(conn)["account"]["machineId"]
    assert new_id and new_id != old_id
    with connect(hs.database_path) as conn:
        row = conn.execute("SELECT input_tokens FROM usage_events WHERE raw_id=? UNION ALL SELECT input_tokens FROM unpriced_usage_events WHERE raw_id=?",
                           (f"m:{new_id}:spoke-event", f"m:{new_id}:spoke-event")).fetchone()
    assert row is not None and row["input_tokens"] == 7000, repaired.json()
    assert repaired.json()["syncResult"]["ok"]


def test_old_inflight_sync_cannot_overwrite_new_hub_connection(pair, monkeypatch):
    from concurrent.futures import ThreadPoolExecutor
    from threading import Event

    hs, hub, ss, spoke = pair
    sign_in(spoke)
    hub.cookies.clear()
    old_cursor = sync.read_sync_cursor(ss.database_path, HUB)
    old_generation = old_cursor["generation"]
    entered, resume = Event(), Event()
    original_write = sync.write_sync_cursor

    def paused_write(path, url, cursor, **kwargs):
        if kwargs.get("expected_generation") == old_generation:
            entered.set()
            assert resume.wait(10), "Timed out waiting for replacement hub connection"
            try:
                return original_write(path, url, cursor, **kwargs)
            except RuntimeError as exc:
                rejected.append(str(exc))
                raise
        return original_write(path, url, cursor, **kwargs)

    rejected = []
    monkeypatch.setattr(sync, "write_sync_cursor", paused_write)
    pricing = PricingEngine.load(ss.pricing_path)
    with ThreadPoolExecutor(max_workers=1) as pool:
        old_job = pool.submit(setup_wizard.maybe_run_configured_spoke_sync, ss, pricing, client=hub)
        try:
            assert entered.wait(10), "Old sync did not reach its cursor write"
            with connect(ss.database_path) as conn:
                current = setup_wizard.read_setup_state(conn)
                current["spokeConfig"]["enabled"] = False
                setup_wizard.write_setup_state(conn, current)
                conn.execute("DELETE FROM app_meta WHERE key='device_id.v1'")
            repaired = spoke.post("/api/setup/account", headers=UI, json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "Race replacement",
                "testSync": True,
            })
            assert repaired.status_code == 200, repaired.text
            replacement_cursor = sync.read_sync_cursor(ss.database_path, HUB)
            assert replacement_cursor["generation"] != old_generation
            with connect(ss.database_path) as conn:
                new_id = setup_wizard.read_setup_state(conn)["account"]["machineId"]
        finally:
            resume.set()
        result = old_job.result(timeout=10)
    assert not result["ok"] and rejected and "Hub connection changed" in rejected[0]
    assert sync.read_sync_cursor(ss.database_path, HUB) == replacement_cursor
    with connect(ss.database_path) as conn:
        assert setup_wizard.read_setup_state(conn)["account"]["machineId"] == new_id
    with connect(hs.database_path) as conn:
        raw_id = f"m:{new_id}:spoke-event"
        row = conn.execute("SELECT input_tokens FROM usage_events WHERE raw_id=? UNION ALL "
                           "SELECT input_tokens FROM unpriced_usage_events WHERE raw_id=?", (raw_id, raw_id)).fetchone()
    assert row is not None and row["input_tokens"] == 7000


def test_account_reconnect_survives_failed_initial_sync_and_retries(pair, monkeypatch):
    import httpx

    hs, hub, ss, spoke = pair
    sign_in(spoke)
    hub.cookies.clear()
    before_token = machine_token_of(ss)
    old_cursor = sync.read_sync_cursor(ss.database_path, HUB)
    with connect(ss.database_path) as conn:
        current = setup_wizard.read_setup_state(conn)
        machine_id = current["account"]["machineId"]
        current["spokeConfig"]["enabled"] = False
        setup_wizard.write_setup_state(conn, current)
    seed(ss.database_path, "retry-snapshot", 2100)
    original_post = hub.post

    def fail_push(url, *args, **kwargs):
        if str(url).endswith("/api/sync/push"):
            return httpx.Response(503, text="synthetic outage")
        return original_post(url, *args, **kwargs)

    with monkeypatch.context() as patch:
        patch.setattr(hub, "post", fail_push)
        response = spoke.post("/api/setup/account", headers=UI, json={
            "mode": "login",
            "username": "owner@example.com",
            "password": PASSWORD,
            "computerName": "Laptop",
            "testSync": True,
        })
    assert response.status_code == 200, response.text
    saved = state(ss)
    new_token = machine_token_of(ss)
    assert new_token != before_token and owner_session_of(ss)
    assert new_token not in response.text
    assert saved["lastSyncError"] and not response.json()["syncResult"]["ok"]
    cursor = sync.read_sync_cursor(ss.database_path, HUB)
    assert cursor["generation"] != old_cursor["generation"]
    assert cursor["credential_hash"] == sync._token_hash(new_token)
    assert sync.authenticate_machine_token(hs.database_path, before_token) is None
    assert sync.authenticate_machine_token(hs.database_path, new_token) == machine_id
    retry = setup_wizard.maybe_run_configured_spoke_sync(ss, PricingEngine.load(ss.pricing_path), client=hub)
    assert retry["ok"], retry
    assert state(ss)["lastSyncError"] is None
    with connect(hs.database_path) as conn:
        raw_id = f"m:{machine_id}:retry-snapshot"
        row = conn.execute("SELECT input_tokens FROM usage_events WHERE raw_id=? UNION ALL "
                           "SELECT input_tokens FROM unpriced_usage_events WHERE raw_id=?", (raw_id, raw_id)).fetchone()
    assert row is not None and row["input_tokens"] == 2100


def test_disabled_spoke_account_login_rebinds_and_reseeds(pair, monkeypatch):
    hs, hub, ss, spoke = pair
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    old_token = machine_token_of(ss)
    old_cursor = sync.read_sync_cursor(ss.database_path, HUB)
    with connect(ss.database_path) as conn:
        old_state = setup_wizard.read_setup_state(conn)
        old_machine_id = old_state["account"]["machineId"]
        name = old_state["computerName"]
        old_state["role"] = "standalone"
        old_state["spokeConfig"]["enabled"] = False
        setup_wizard.write_setup_state(conn, old_state)
    seed(ss.database_path, "disabled-snapshot", 1700)
    response = spoke.post("/api/setup/account", headers=UI,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD,
              "computerName": name, "testSync": True})
    assert response.status_code == 200, response.text
    saved = state(ss)
    new_token = machine_token_of(ss)
    assert saved["enabled"] and new_token != old_token and owner_session_of(ss)
    cursor = sync.read_sync_cursor(ss.database_path, HUB)
    assert cursor["generation"] != old_cursor["generation"]
    assert cursor["credential_hash"] == sync._token_hash(new_token)
    assert response.json()["syncResult"]["ok"]
    with connect(ss.database_path) as conn:
        machine_id = setup_wizard.read_setup_state(conn)["account"]["machineId"]
    assert machine_id == old_machine_id
    with connect(hs.database_path) as conn:
        raw_id = f"m:{machine_id}:disabled-snapshot"
        row = conn.execute("SELECT input_tokens FROM usage_events WHERE raw_id=? UNION ALL "
                           "SELECT input_tokens FROM unpriced_usage_events WHERE raw_id=?", (raw_id, raw_id)).fetchone()
    assert row is not None and row["input_tokens"] == 1700


def test_expired_reused_machine_does_not_block_owner_login(pair, monkeypatch):
    hs, hub, ss, spoke = pair
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    before_token = machine_token_of(ss)
    cursor = sync.read_sync_cursor(ss.database_path, HUB)
    with connect(ss.database_path) as conn:
        setup = setup_wizard.read_setup_state(conn)
        machine_id, name = setup["account"]["machineId"], setup["computerName"]
    with connect(hs.database_path) as conn:
        machines = sync._read_state(conn)
        machines["machines"][machine_id]["expires_at"] = auth_model.stamp(auth_model.moment() - timedelta(seconds=1))
        sync._write_state(conn, machines)
    response = spoke.post("/api/setup/account", headers=UI,
        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD,
              "computerName": name, "testSync": True})
    assert response.status_code == 200, response.text
    saved = state(ss)
    assert machine_token_of(ss) == before_token and owner_session_of(ss)
    assert saved["lastSyncError"] and not response.json()["syncResult"]["ok"]
    assert sync.read_sync_cursor(ss.database_path, HUB) == cursor
    assert summary(spoke)["totals"]["tokens"] == 18000
    assert len(summary(spoke)["machines"]) == 2
