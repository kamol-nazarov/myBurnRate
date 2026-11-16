"""Synthetic auth-model boundaries: no user databases or external services."""
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import auth_model as auth, auth_throttle, pairing as pairing_module, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]
ORIGIN = "https://hub.test"
SETUP_TOKEN = "synthetic-setup-token-32-chars-00"
PASSWORD = "synthetic-long-password"
ADMIN = {"Authorization": "Bearer synthetic-break-glass", "X-BURNRATE-Request": "1"}
UI = {"Origin": ORIGIN, "X-BURNRATE-Request": "1"}


def settings_at(path):
    return Settings(database_path=path / "test.db", pricing_path=ROOT / "pricing",
                    anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                    cache_hit_threshold=.75, over_routing_token_ceiling=40000)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP_TOKEN)
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "1")
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    settings = settings_at(tmp_path)
    with TestClient(create_app(settings, enable_scheduler=False), base_url=ORIGIN) as client:
        yield settings, client
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()


def owner(client):
    result = client.post("/api/account/register", headers=UI, json={"username": "synthetic-owner", "password": PASSWORD,
                         "setupToken": SETUP_TOKEN, "sessionOnly": True, "deviceLabel": "Synthetic browser"})
    assert result.status_code == 200, result.text
    return result


def machine_row(settings, mid, **changes):
    with connect(settings.database_path) as connection:
        state = sync._read_state(connection)
        state["machines"][mid].update(changes)
        sync._write_state(connection, state)


def test_machine_reads_are_own_projection_and_other_actions_forbidden(hub):
    settings, client = hub
    own = sync.register_machine(settings.database_path, name="Own computer")
    other = sync.register_machine(settings.database_path, name="PRIVATE-other-computer")
    mid, other_id = own["machine"]["id"], other["machine"]["id"]
    headers = {"Authorization": "Bearer " + own["token"], "X-BURNRATE-Request": "1"}
    for path in ("/api/spend/usage", "/api/spend/summary", "/api/spend/entity"):
        response = client.get(path, params={"machine": mid, "kind": "tool", "key": "codex"}, headers=headers)
        assert response.status_code == 200
        assert response.json()["machine"] == mid
        assert "PRIVATE-other" not in response.text and other_id not in response.text
        assert "plans" not in response.json() and "machines" not in response.json()
        assert client.get(path, params={"machine": other_id}, headers=headers).status_code == 403
        assert client.get(path, headers=headers).status_code == 403
        assert client.get(path, params=[("machine", mid), ("machine", "all")], headers=headers).status_code == 403
    for path in ("/api/spend/nav", "/api/subscriptions", "/api/setup/state", "/api/auth/sessions", "/api/auth/audit"):
        assert client.get(path, headers=headers).status_code == 403
    machines = client.get("/api/machines", headers=headers).json()["machines"]
    assert [m["id"] for m in machines] == [mid]
    assert client.post("/api/sync/push", headers=headers, json={"machineId": other_id}).status_code == 403
    assert client.post("/api/account/subscriptions", headers=headers, json={}).status_code == 403


@pytest.mark.parametrize("condition", ["expired", "revoked"])
def test_machine_expired_and_revoked_are_401(hub, condition):
    settings, client = hub
    reg = sync.register_machine(settings.database_path, name="Synthetic device")
    if condition == "expired":
        machine_row(settings, reg["machine"]["id"], expires_at=auth.stamp(auth.moment() - timedelta(seconds=1)))
    else:
        sync.revoke_machine(settings.database_path, machine_id=reg["machine"]["id"])
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + reg["token"]}, json={}).status_code == 401


def test_machine_rotation_is_atomic_requires_current_token_and_invalidates_old(hub):
    settings, client = hub
    reg = sync.register_machine(settings.database_path, name="Rotating device")
    headers = {"Authorization": "Bearer " + reg["token"], "X-BURNRATE-Request": "1"}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/auth/machine/rotate", headers=headers, json={}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 401]
    replacement = next(r.json() for r in responses if r.status_code == 200)
    assert auth.parse(replacement["expiresAt"]) > auth.moment() + timedelta(days=89)
    assert client.post("/api/sync/push", headers=headers, json={}).status_code == 401
    assert client.post("/api/auth/machine/rotate", headers={"Authorization": "Bearer br_m_invalid_synthetic", "X-BURNRATE-Request": "1"}, json={}).status_code == 401
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + replacement["token"]}, json={}).status_code == 200
    with connect(settings.database_path) as connection:
        row = sync._read_state(connection)["machines"][reg["machine"]["id"]]
        assert row["last_used_at"] and not row["rotation_required"]
        assert replacement["token"] not in json.dumps(row)


def test_login_cookie_foreign_origin_logout_and_reuse(hub):
    _, client = hub
    first = owner(client)
    first_token = first.cookies[auth.COOKIE]
    client.cookies.clear()
    login = client.post("/api/account/login", headers=UI, json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True})
    assert login.status_code == 200
    assert "sessionToken" not in login.json()
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    token = login.cookies[auth.COOKIE]
    assert token != "synthetic-break-glass" and len(token) >= 43
    assert client.post("/api/machines", headers={"Origin": "https://foreign.invalid", "X-BURNRATE-Request": "1"}, json={"name": "No"}).status_code == 403
    assert client.post("/api/machines", headers={"Origin": "null", "X-BURNRATE-Request": "1"}, json={"name": "No"}).status_code == 403
    assert client.post("/api/machines", headers=UI, json={"name": "Yes"}).status_code == 200
    assert client.post("/api/auth/logout", headers=UI, json={}).status_code == 200
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + token}).status_code == 401
    client.cookies.set(auth.COOKIE, token, path="/api")
    assert client.get("/api/machines").status_code == 401
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + first_token}).status_code == 200


def test_session_expiry_revocation_rotation_and_future_roles(hub):
    settings, client = hub
    first = owner(client)
    token = first.cookies[auth.COOKIE]
    rotated = client.post("/api/auth/session/rotate", headers=UI, json={})
    assert rotated.status_code == 200
    assert "sessionToken" not in rotated.json()
    new_token = rotated.cookies[auth.COOKIE]
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + token}).status_code == 401
    current = rotated.json()
    revoked = client.post("/api/auth/sessions/revoke", headers=UI, json={"id": current["sessionId"]})
    assert revoked.status_code == 200
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + new_token}).status_code == 401
    client.cookies.clear()
    login = client.post("/api/account/login", headers=UI, json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True}).json()
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE owner_sessions SET expires_at=? WHERE id=?", (auth.stamp(auth.moment() - timedelta(seconds=1)), login["sessionId"]))
    assert client.get("/api/machines").status_code == 401
    assert not auth.Principal("owner", "future", "member").permits("hub.read")


def test_static_bearer_is_restricted_unless_opted_in(hub, monkeypatch):
    _, client = hub
    monkeypatch.delenv("BURNRATE_ALLOW_ADMIN_BEARER", raising=False)
    assert client.get("/api/machines", headers=ADMIN).status_code == 401
    assert client.post("/api/auth/bootstrap", headers=ADMIN).status_code == 200


def test_last_used_updates_are_throttled(tmp_path):
    path = tmp_path / "throttle.db"
    initialize(path)
    first_moment = auth.moment()
    with connect(path) as connection:
        created = auth.create_session(
            connection, auth.Principal("owner", "subject", "owner"), "Synthetic", now=first_moment
        )
    token = created["sessionToken"]
    assert auth.authenticate_session(path, token, now=first_moment) is not None
    with connect(path) as connection:
        first = connection.execute(
            "SELECT last_used_at FROM owner_sessions WHERE id=?", (created["sessionId"],)
        ).fetchone()[0]
    assert auth.authenticate_session(path, token, now=first_moment + timedelta(seconds=30))
    with connect(path) as connection:
        second = connection.execute(
            "SELECT last_used_at FROM owner_sessions WHERE id=?", (created["sessionId"],)
        ).fetchone()[0]
    assert second == first
    assert auth.authenticate_session(path, token, now=first_moment + timedelta(seconds=61))
    with connect(path) as connection:
        third = connection.execute(
            "SELECT last_used_at FROM owner_sessions WHERE id=?", (created["sessionId"],)
        ).fetchone()[0]
    assert third != first


def test_break_glass_bootstrap_automation_opt_out_and_rotation(hub, monkeypatch):
    _, client = hub
    assert client.get("/api/machines", headers=ADMIN).status_code == 200
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "0")
    assert client.get("/api/machines", headers=ADMIN).status_code == 401
    bootstrap = client.post("/api/auth/bootstrap", headers=ADMIN)
    assert bootstrap.status_code == 200 and "sessionToken" not in bootstrap.json()
    assert bootstrap.cookies[auth.COOKIE].startswith("br_s_")
    assert client.get("/api/machines").status_code == 200
    rotated = client.post("/api/auth/rotate", headers=UI).json()
    client.cookies.clear()
    assert client.post("/api/auth/bootstrap", headers=ADMIN).status_code == 401
    assert client.post("/api/auth/bootstrap", headers={"Authorization": "Bearer " + rotated["token"]}).status_code == 200


def pairing(client, **kwargs):
    result = client.post("/api/setup/pair-hub", headers={**ADMIN, "X-BURNRATE-Request": "1"},
                         json={"name": "Paired device", "hubUrl": ORIGIN, **kwargs})
    assert result.status_code == 200, result.text
    assert "br_m_" not in result.text
    return result.json()


def test_pairing_expiry_single_use_host_binding_and_repair(hub):
    settings, client = hub
    invitation = pairing(client)
    assert auth.moment() + timedelta(minutes=14) < auth.parse(invitation["expiresAt"]) <= auth.moment() + timedelta(minutes=15)
    payload = {"pairingCode": invitation["pairingCode"]}
    assert client.post("/api/pairing/redeem", headers={"Host": "other.invalid", "X-BURNRATE-Request": "1"}, json=payload).status_code == 403
    # Another allowed Host also fails: allowlisting is not grant binding.
    assert client.post("/api/pairing/redeem", headers={"Host": "localhost", "X-BURNRATE-Request": "1"}, json=payload).status_code == 403
    result = client.post("/api/pairing/redeem", headers=UI, json=payload)
    assert result.status_code == 200
    reg = result.json()
    assert auth.parse(reg["expiresAt"]) > auth.moment() + timedelta(days=89)
    assert client.post("/api/pairing/redeem", headers=UI, json=payload).status_code == 403
    sync.revoke_machine(settings.database_path, machine_id=reg["machine"]["id"])
    repaired = pairing(client, machineId=reg["machine"]["id"])
    new = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": repaired["pairingCode"]})
    assert new.status_code == 200 and new.json()["machine"]["id"] == reg["machine"]["id"]
    assert new.json()["token"] != reg["token"]
    expiring = pairing(client, name="Expiring device")
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE pairing_grants SET expires_at=? WHERE used_at IS NULL", (auth.stamp(auth.moment() - timedelta(seconds=1)),))
    assert client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": expiring["pairingCode"]}).status_code == 403


def test_pairing_concurrent_redemption_only_one_succeeds(hub):
    _, client = hub
    code = pairing(client)["pairingCode"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": code}), range(2)))
    assert sorted(r.status_code for r in results) == [200, 403]


def test_pairing_and_registration_enforce_active_name_uniqueness(hub):
    settings, client = hub
    first = sync.register_machine(settings.database_path, name="Workstation")
    first_id = first["machine"]["id"]

    # Direct new-id registration with a colliding name is refused, while /api/machines
    # break-glass register reconnects the single active machine id with a rotated token.
    with pytest.raises(ValueError, match="already exists"):
        sync.register_machine(settings.database_path, name="workstation")
    reg_dup = client.post(
        "/api/machines", headers=ADMIN, json={"action": "register", "name": "WORKSTATION"}
    )
    assert reg_dup.status_code == 200
    assert reg_dup.json()["machine"]["id"] == first_id
    first_token = reg_dup.json()["token"]
    assert first_token != first["token"]
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + first["token"]}, json={}).status_code == 401

    # Invite -> rename One -> rename Two to One -> redeem old invite is refused (403).
    one = sync.register_machine(settings.database_path, name="One")
    two = sync.register_machine(settings.database_path, name="Two")
    stale_invite = pairing(client, name="One")
    sync.rename_machine(settings.database_path, machine_id=one["machine"]["id"], name="Renamed One")
    sync.rename_machine(settings.database_path, machine_id=two["machine"]["id"], name="One")
    stale_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": stale_invite["pairingCode"]})
    assert stale_redeem.status_code == 403
    with connect(settings.database_path) as connection:
        machines = sync._read_state(connection)["machines"]
        active_ones = [
            mid for mid, rec in machines.items()
            if not rec.get("revoked") and rec["name"].lower() == "one"
        ]
        assert active_ones == [two["machine"]["id"]]

    # Seed legacy colliding active records directly to verify name-only pairing/reconnect
    # rejects ambiguity while explicit machineId succeeds (for non-colliding name).
    second_id = "machine-legacy-dup"
    with connect(settings.database_path) as connection:
        state = sync._read_state(connection)
        state["machines"][second_id] = {
            **state["machines"][first_id],
            "id": second_id,
            "name": "workstation",
            "token_hash": auth.digest("br_m_legacy_dup_token"),
        }
        sync._write_state(connection, state)

    actor = auth.Principal("owner", "break-glass", "owner")
    with pytest.raises(ValueError, match="machine ID"):
        pairing_module.issue(settings.database_path, actor, hub_url=ORIGIN, name="WORKSTATION")

    refused = client.post(
        "/api/setup/pair-hub",
        headers={**ADMIN, "X-BURNRATE-Request": "1"},
        json={"name": "WORKSTATION", "hubUrl": ORIGIN},
    )
    assert refused.status_code == 422
    assert "pairingCode" not in refused.text

    # Name-only reconnect and token rotation against colliding active names both refuse.
    with pytest.raises(ValueError, match="machine ID"):
        sync.register_or_reconnect_machine(settings.database_path, name="WORKSTATION")
    with pytest.raises(ValueError, match="already exists"):
        sync.rotate_machine_token(settings.database_path, first_token)

    # Explicit machineId with a unique name succeeds cleanly.
    sync.rename_machine(settings.database_path, machine_id=second_id, name="Workstation Two")
    explicit = pairing(client, name="WORKSTATION", machineId=first_id)
    redeemed = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": explicit["pairingCode"]})
    assert redeemed.status_code == 200
    assert redeemed.json()["machine"]["id"] == first_id


def test_spare_name_only_invitation_invalidated_when_machine_revoked(hub):
    settings, client = hub
    invite_one = pairing(client, name="Review Box")
    invite_two = pairing(client, name="review box")

    # Both name-only invites for the new machine reserve the exact same machine_id.
    with connect(settings.database_path) as connection:
        rows = connection.execute(
            "SELECT machine_id, by_name, used_at FROM pairing_grants ORDER BY created_at"
        ).fetchall()
        assert len(rows) == 2
        assert rows[0]["machine_id"] and rows[0]["machine_id"] == rows[1]["machine_id"]
        assert rows[0]["by_name"] == 1 and rows[1]["by_name"] == 1

    # Redeem the first invitation -> creates machine with that reserved id.
    first_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": invite_one["pairingCode"]})
    assert first_redeem.status_code == 200
    reserved_id = first_redeem.json()["machine"]["id"]
    assert reserved_id == rows[0]["machine_id"]

    # Revoking the machine invalidates the spare invitation and blocks redeeming it.
    sync.revoke_machine(settings.database_path, machine_id=reserved_id)
    with connect(settings.database_path) as connection:
        used_vals = [r["used_at"] for r in connection.execute("SELECT used_at FROM pairing_grants").fetchall()]
        assert all(used_vals)
    spare_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": invite_two["pairingCode"]})
    assert spare_redeem.status_code == 403

    # Even if a reserved name-only invite wasn't bound to an existing machine yet,
    # creating and revoking a different machine with that name blocks redeeming it.
    reserved_invite = pairing(client, name="Raced Box")
    other = sync.register_machine(settings.database_path, name="Raced Box")
    sync.revoke_machine(settings.database_path, machine_id=other["machine"]["id"])
    raced_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": reserved_invite["pairingCode"]})
    assert raced_redeem.status_code == 403


def test_revoked_machine_cannot_be_revived_by_name_only_pairing_or_reconnect(hub):
    settings, client = hub
    owner(client)
    client.cookies.clear()

    reg = sync.register_machine(settings.database_path, name="Revoked Box")
    mid = reg["machine"]["id"]
    sync.revoke_machine(settings.database_path, machine_id=mid)

    actor = auth.Principal("owner", "break-glass", "owner")
    with pytest.raises(ValueError, match="machine ID"):
        pairing_module.issue(settings.database_path, actor, hub_url=ORIGIN, name="revoked box")

    refused_pair = client.post(
        "/api/setup/pair-hub",
        headers={**ADMIN, "X-BURNRATE-Request": "1"},
        json={"name": "revoked box", "hubUrl": ORIGIN},
    )
    assert refused_pair.status_code == 422
    assert "pairingCode" not in refused_pair.text

    with pytest.raises(ValueError, match="machine ID"):
        sync.register_or_reconnect_machine(settings.database_path, name="revoked box")
    with pytest.raises(ValueError, match="machine ID"):
        sync.register_machine(settings.database_path, name="revoked box")
    refused_break_glass = client.post(
        "/api/machines",
        headers=ADMIN,
        json={"action": "register", "name": "revoked box"},
    )
    assert refused_break_glass.status_code == 422

    login = client.post(
        "/api/account/login",
        headers=UI,
        json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True},
    )
    assert login.status_code == 200
    refused_owner_reg = client.post(
        "/api/machines",
        headers=UI,
        json={"action": "register", "name": "revoked box"},
    )
    assert refused_owner_reg.status_code == 422
    client.cookies.clear()

    # Even with machine_id, reconnect=True must never revive a revoked record.
    with connect(settings.database_path) as connection:
        with pytest.raises(ValueError, match="revoked"):
            sync._register_in_connection(
                connection, name="Revoked Box", reconnect=True, machine_id=mid
            )
        assert sync._read_state(connection)["machines"][mid]["revoked"] is True

    # Fresh pairing with explicit machineId still works and restores sync.
    repaired = pairing(client, name="Revoked Box", machineId=mid)
    redeemed = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": repaired["pairingCode"]})
    assert redeemed.status_code == 200
    new_token = redeemed.json()["token"]
    assert redeemed.json()["machine"]["id"] == mid
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + new_token}, json={}).status_code == 200


def test_audit_actor_exactly_once_append_only_and_no_credential_material(hub):
    settings, client = hub
    first = owner(client)
    first_token = first.cookies[auth.COOKIE]
    client.cookies.clear()
    assert client.post("/api/account/login", headers=UI, json={"username": "synthetic-owner", "password": "synthetic-wrong-password"}).status_code == 401
    reg = client.post("/api/machines", headers=ADMIN, json={"name": "Synthetic device"}).json()
    mid = reg["machine"]["id"]
    for action in ("rename", "revoke"):
        assert client.post("/api/machines", headers=ADMIN, json={"action": action, "id": mid, "name": "Safe name"}).status_code == 200
    with connect(settings.database_path) as connection:
        rows = [dict(row) for row in connection.execute("SELECT * FROM admin_audit")]
        for action in ("account.register", "session.create", "machine.register", "machine.rename", "machine.revoke"):
            assert sum(row["action"] == action and row["outcome"] == "success" for row in rows) == 1
        machine_audit = next(row for row in rows if row["action"] == "machine.register")
        assert machine_audit["actor_type"] == "owner" and machine_audit["actor_id"] == "break-glass"
        assert any(row["action"] == "login" and row["outcome"] == "failure" and row["actor_type"] == "unauthenticated" for row in rows)
        material = json.dumps(rows)
        assert all(secret not in material for secret in (PASSWORD, first_token, reg["token"], "synthetic-break-glass", "synthetic-owner"))
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM admin_audit")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE admin_audit SET outcome='changed'")


def test_dropped_admin_audit_table_and_triggers_recover_on_restart(tmp_path):
    db_path = tmp_path / "audit-recover.db"
    initialize(db_path)
    with connect(db_path) as connection:
        assert connection.execute("SELECT 1 FROM app_meta WHERE key=?", (auth.MARKER,)).fetchone()
        auth.audit(connection, auth.Principal("owner", "test", "owner"), "machine.register", "m1")
        connection.execute("DROP TABLE admin_audit")
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name IN ('admin_audit', 'admin_audit_no_update', 'admin_audit_no_delete')"
        ).fetchall() == []

    # Restarting via initialize() / ensure_schema() recreates the table and both append-only triggers
    # even when auth_model.v1 marker is already present.
    initialize(db_path)
    with connect(db_path) as connection:
        triggers = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='admin_audit'"
            )
        }
        assert triggers == {"admin_audit_no_update", "admin_audit_no_delete"}
        auth.audit(connection, auth.Principal("owner", "test", "owner"), "machine.rename", "m1")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("UPDATE admin_audit SET outcome='tampered'")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            connection.execute("DELETE FROM admin_audit")


def test_legacy_migration_once_and_055_spoke_push_without_new_setup_token(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    settings = settings_at(tmp_path)
    token = "br_m_legacy_synthetic-legacy-value"
    created = auth.moment() - timedelta(days=5)
    salt, pw_hash = setup_wizard._hash_password(PASSWORD, iterations=setup_wizard.LEGACY_PBKDF2_ITERATIONS)
    from spend_app.db import _initialize_base
    _initialize_base(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.executemany("INSERT INTO app_meta VALUES(?,?)", [
            ("machines.v1", json.dumps({"version": 1, "machines": {"legacy": {"id": "legacy", "name": "Legacy", "token_hash": auth.digest(token), "created_at": auth.stamp(created)}}})),
            ("accounts.v1", json.dumps({"version": 1, "users": {"synthetic-owner": {"username": "synthetic-owner", "salt": salt, "passwordHash": pw_hash, "createdAt": auth.stamp(created)}}})),
        ])
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        first_meta = list(map(tuple, connection.execute("SELECT * FROM app_meta WHERE key IN ('accounts.v1','machines.v1','auth_model.v1') ORDER BY key")))
        count = connection.execute("SELECT count(*) FROM admin_audit").fetchone()[0]
        rec = sync._read_state(connection)["machines"]["legacy"]
        assert rec["rotation_required"] and auth.parse(rec["expires_at"]) == created + timedelta(days=180)
        deadline = auth.parse(rec["rotation_deadline"])
        assert deadline is not None
        assert auth.moment() + timedelta(days=auth.ROTATION_GRACE_DAYS - 1) < deadline <= auth.moment() + timedelta(days=auth.ROTATION_GRACE_DAYS, minutes=1)
    initialize(settings.database_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        assert first_meta == list(map(tuple, connection.execute("SELECT * FROM app_meta WHERE key IN ('accounts.v1','machines.v1','auth_model.v1') ORDER BY key")))
        assert connection.execute("SELECT count(*) FROM admin_audit").fetchone()[0] == count == 1
    with TestClient(create_app(settings, enable_scheduler=False), base_url=ORIGIN) as client:
        assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + token}, json={"machine": {"name": "Legacy"}, "usageEvents": [], "sessions": [], "agentRuns": []}).status_code == 200
        assert client.post("/api/account/login", headers=UI, json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True}).status_code == 200


def test_rotation_required_backfill_api_surface_and_deadline_enforcement(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "1")
    settings = settings_at(tmp_path)
    legacy_token = "br_m_legacy_synthetic-grace-token"
    fixed_deadline = auth.stamp(auth.moment() + timedelta(days=3))
    created = auth.moment() - timedelta(days=10)
    from spend_app.db import _initialize_base
    _initialize_base(settings.database_path)
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            "INSERT INTO app_meta VALUES(?,?)",
            ("machines.v1", json.dumps({"version": 1, "machines": {
                "legacy": {"id": "legacy", "name": "Legacy", "token_hash": auth.digest(legacy_token), "created_at": auth.stamp(created)},
            }})),
        )
    initialize(settings.database_path)

    # Simulate an existing migrated DB (auth_model.v1 present) with a rotation_required machine
    # lacking rotation_deadline, plus one with an existing deadline and one not requiring rotation.
    needs_backfill = sync.register_machine(settings.database_path, name="Needs Backfill")
    keeps_deadline = sync.register_machine(settings.database_path, name="Keeps Deadline")
    stays_clear = sync.register_machine(settings.database_path, name="No Rotation")
    machine_row(settings, needs_backfill["machine"]["id"], rotation_required=True)
    machine_row(settings, keeps_deadline["machine"]["id"], rotation_required=True, rotation_deadline=fixed_deadline)

    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        machines = sync._read_state(connection)["machines"]
        backfilled = auth.parse(machines[needs_backfill["machine"]["id"]].get("rotation_deadline"))
        assert backfilled is not None
        assert auth.moment() + timedelta(days=auth.ROTATION_GRACE_DAYS - 1) < backfilled <= auth.moment() + timedelta(days=auth.ROTATION_GRACE_DAYS, minutes=1)
        assert machines[keeps_deadline["machine"]["id"]]["rotation_deadline"] == fixed_deadline
        assert not machines[stays_clear["machine"]["id"]].get("rotation_deadline")

    with TestClient(create_app(settings, enable_scheduler=False), base_url=ORIGIN) as client:
        listed = client.get("/api/machines", headers=ADMIN)
        assert listed.status_code == 200
        by_id = {m["id"]: m for m in listed.json()["machines"]}
        assert by_id["legacy"]["rotationRequired"] is True
        assert by_id["legacy"]["rotationDeadline"]
        assert by_id[needs_backfill["machine"]["id"]]["rotationRequired"] is True
        assert by_id[needs_backfill["machine"]["id"]]["rotationDeadline"]
        assert by_id[stays_clear["machine"]["id"]]["rotationRequired"] is False
        assert by_id[stays_clear["machine"]["id"]]["rotationDeadline"] is None

        # Stamped token works before deadline and rotates cleanly.
        legacy_headers = {"Authorization": "Bearer " + legacy_token, "X-BURNRATE-Request": "1"}
        assert client.post("/api/sync/push", headers=legacy_headers, json={}).status_code == 200
        rotated = client.post("/api/auth/machine/rotate", headers=legacy_headers, json={})
        assert rotated.status_code == 200
        new_token = rotated.json()["token"]
        assert client.post("/api/sync/push", headers=legacy_headers, json={}).status_code == 401
        assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + new_token}, json={}).status_code == 200
        after_rotate = {m["id"]: m for m in client.get("/api/machines", headers=ADMIN).json()["machines"]}
        assert after_rotate["legacy"]["rotationRequired"] is False
        assert after_rotate["legacy"]["rotationDeadline"] is None

        # Once past rotation_deadline, both /api/sync/push and /api/auth/machine/rotate return 401.
        past_deadline = auth.stamp(auth.moment() - timedelta(seconds=1))
        machine_row(settings, needs_backfill["machine"]["id"], rotation_deadline=past_deadline)
        expired_headers = {"Authorization": "Bearer " + needs_backfill["token"], "X-BURNRATE-Request": "1"}
        assert client.post("/api/sync/push", headers=expired_headers, json={}).status_code == 401
        assert client.post("/api/auth/machine/rotate", headers=expired_headers, json={}).status_code == 401

        # Exact boundary (deadline == now) is also refused.
        boundary = auth.moment()
        machine_row(settings, keeps_deadline["machine"]["id"], rotation_deadline=auth.stamp(boundary))
        assert sync.authenticate_machine_token(settings.database_path, keeps_deadline["token"], now=boundary) is None

        # Missing or invalid rotation_deadline with rotation_required=True is also refused (401).
        with connect(settings.database_path) as connection:
            state = sync._read_state(connection)
            state["machines"][keeps_deadline["machine"]["id"]].pop("rotation_deadline", None)
            sync._write_state(connection, state)
        missing_headers = {"Authorization": "Bearer " + keeps_deadline["token"], "X-BURNRATE-Request": "1"}
        assert client.post("/api/sync/push", headers=missing_headers, json={}).status_code == 401
        assert client.post("/api/auth/machine/rotate", headers=missing_headers, json={}).status_code == 401

        machine_row(settings, keeps_deadline["machine"]["id"], rotation_deadline="not-a-timestamp")
        assert client.post("/api/sync/push", headers=missing_headers, json={}).status_code == 401
        assert client.post("/api/auth/machine/rotate", headers=missing_headers, json={}).status_code == 401


def test_parse_overflow_and_unmatched_malformed_deadlines_do_not_break_other_machines(hub):
    settings, client = hub
    assert auth.parse("9999-12-31T23:59:59-23:59") is None

    bad_machine = sync.register_machine(settings.database_path, name="Overflow Box")
    good_machine = sync.register_machine(settings.database_path, name="Healthy Box")
    machine_row(
        settings,
        bad_machine["machine"]["id"],
        rotation_required=True,
        rotation_deadline="9999-12-31T23:59:59-23:59",
    )
    # Overflowing deadline on bad_machine must fail closed (401) and never affect good_machine.
    assert client.post(
        "/api/sync/push",
        headers={"Authorization": "Bearer " + bad_machine["token"]},
        json={},
    ).status_code == 401
    assert client.post(
        "/api/auth/machine/rotate",
        headers={"Authorization": "Bearer " + bad_machine["token"], "X-BURNRATE-Request": "1"},
        json={},
    ).status_code == 401
    assert client.post(
        "/api/sync/push",
        headers={"Authorization": "Bearer " + good_machine["token"]},
        json={},
    ).status_code == 200


def test_owner_session_machines_register_follows_reconnect_rules(hub):
    settings, client = hub
    first_owner = owner(client)
    bearer_token = first_owner.cookies[auth.COOKIE]
    bearer_headers = {"Authorization": "Bearer " + bearer_token, "X-BURNRATE-Request": "1"}

    # No matching machine -> registers a new machine.
    created = client.post(
        "/api/machines",
        headers=UI,
        json={"action": "register", "name": "Owner Box", "os": "win32"},
    )
    assert created.status_code == 200, created.text
    first_id = created.json()["machine"]["id"]
    first_token = created.json()["token"]

    # Exactly one active match (case-insensitive) -> reconnects same ID with rotated token.
    client.cookies.clear()
    reconnected = client.post(
        "/api/machines",
        headers=bearer_headers,
        json={"action": "register", "name": "owner box", "os": "win32"},
    )
    assert reconnected.status_code == 200, reconnected.text
    assert reconnected.json()["machine"]["id"] == first_id
    second_token = reconnected.json()["token"]
    assert second_token != first_token
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + first_token}, json={}).status_code == 401
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + second_token}, json={}).status_code == 200

    # Revoked match -> refused with 422 (must re-pair by machine ID).
    sync.revoke_machine(settings.database_path, machine_id=first_id)
    revoked_reg = client.post(
        "/api/machines",
        headers=bearer_headers,
        json={"action": "register", "name": "Owner Box", "os": "win32"},
    )
    assert revoked_reg.status_code == 422

    # Multiple active legacy matches -> refused with 422.
    a = sync.register_machine(settings.database_path, name="Dup Owner")
    dup_id = "machine-legacy-owner-dup"
    with connect(settings.database_path) as connection:
        state = sync._read_state(connection)
        state["machines"][dup_id] = {
            **state["machines"][a["machine"]["id"]],
            "id": dup_id,
            "name": "dup owner",
            "token_hash": auth.digest("br_m_legacy_owner_dup"),
        }
        sync._write_state(connection, state)
    ambiguous = client.post(
        "/api/machines",
        headers=bearer_headers,
        json={"action": "register", "name": "DUP OWNER", "os": "win32"},
    )
    assert ambiguous.status_code == 422


def test_legacy_null_machine_id_grants_expired_on_migration_and_refused_by_redeem(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.setenv("BURNRATE_ALLOW_ADMIN_BEARER", "1")
    settings = settings_at(tmp_path)
    from spend_app.db import _initialize_base
    from spend_app.setup_wizard import encode_pairing_code

    _initialize_base(settings.database_path)
    now = auth.moment()
    future = auth.stamp(now + timedelta(minutes=10))
    past_created = auth.stamp(now)
    legacy_secret = "br_p_legacy-null-secret"
    explicit_secret = "br_p_legacy-explicit-secret"
    existing_machine = sync.register_machine(settings.database_path, name="Existing Legacy")
    existing_id = existing_machine["machine"]["id"]

    # Simulate a pre-E pairing_grants table without by_name column and an unused NULL machine_id grant.
    with connect(settings.database_path) as connection:
        connection.execute("DROP TABLE IF EXISTS pairing_grants")
        connection.execute(
            "CREATE TABLE pairing_grants (id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, "
            "hub_origin TEXT NOT NULL, machine_id TEXT, machine_name TEXT NOT NULL, os_name TEXT NOT NULL, "
            "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT)"
        )
        connection.execute(
            "INSERT INTO pairing_grants VALUES(?,?,?,?,?,?,?,?,?)",
            ("grant-null", auth.digest(legacy_secret), ORIGIN, None, "Legacy Null", "", past_created, future, None),
        )
        connection.execute(
            "INSERT INTO pairing_grants VALUES(?,?,?,?,?,?,?,?,?)",
            ("grant-explicit", auth.digest(explicit_secret), ORIGIN, existing_id, "Existing Legacy", "", past_created, future, None),
        )

    auth.ensure_schema(settings.database_path)
    with connect(settings.database_path) as connection:
        cols = {row[1] for row in connection.execute("PRAGMA table_info(pairing_grants)")}
        assert "by_name" in cols
        null_row = connection.execute("SELECT expires_at FROM pairing_grants WHERE id='grant-null'").fetchone()
        assert auth.parse(null_row["expires_at"]) <= auth.moment()
        explicit_row = connection.execute("SELECT expires_at FROM pairing_grants WHERE id='grant-explicit'").fetchone()
        assert auth.parse(explicit_row["expires_at"]) > auth.moment()

    null_code = encode_pairing_code(hub_url=ORIGIN, token=legacy_secret, machine_id="grant-null", machine_name="Legacy Null", hub_name="Hub")
    explicit_code = encode_pairing_code(hub_url=ORIGIN, token=explicit_secret, machine_id="grant-explicit", machine_name="Existing Legacy", hub_name="Hub")

    with TestClient(create_app(settings, enable_scheduler=False), base_url=ORIGIN) as client:
        assert client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": null_code}).status_code == 403

        # Defence in depth: even if a NULL machine_id grant is unexpired, redeem still refuses it.
        with connect(settings.database_path) as connection:
            connection.execute("UPDATE pairing_grants SET expires_at=? WHERE id='grant-null'", (future,))
        assert client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": null_code}).status_code == 403

        # Explicit legacy machine_id grant remains redeemable.
        ok = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": explicit_code})
        assert ok.status_code == 200
        assert ok.json()["machine"]["id"] == existing_id


def test_pairing_reservation_does_not_reuse_renamed_machine_id(hub):
    settings, client = hub
    orig = sync.register_machine(settings.database_path, name="Old Name")
    orig_id = orig["machine"]["id"]
    orig_token = orig["token"]

    old_invite = pairing(client, name="Old Name")
    sync.rename_machine(settings.database_path, machine_id=orig_id, name="Renamed Machine")

    fresh_invite = pairing(client, name="Old Name")
    with connect(settings.database_path) as connection:
        rows = connection.execute("SELECT id, machine_id FROM pairing_grants ORDER BY created_at").fetchall()
        assert len(rows) == 2
        assert rows[0]["machine_id"] == orig_id
        assert rows[1]["machine_id"] and rows[1]["machine_id"] != orig_id

    # Redeeming the fresh invite creates a new machine instead of rotating Renamed Machine.
    fresh_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": fresh_invite["pairingCode"]})
    assert fresh_redeem.status_code == 200
    assert fresh_redeem.json()["machine"]["id"] != orig_id
    assert fresh_redeem.json()["machine"]["name"] == "Old Name"

    # Redeeming the pre-rename by_name invite for the renamed machine is refused, and Renamed Machine keeps its token.
    stale_redeem = client.post("/api/pairing/redeem", headers=UI, json={"pairingCode": old_invite["pairingCode"]})
    assert stale_redeem.status_code == 403
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + orig_token}, json={}).status_code == 200
    with connect(settings.database_path) as connection:
        assert sync._read_state(connection)["machines"][orig_id]["name"] == "Renamed Machine"
