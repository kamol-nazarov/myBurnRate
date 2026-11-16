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
ORIGIN = "http://hub.test"
PASSWORD = "synthetic-long-password"
ADMIN = {"Authorization": "Bearer synthetic-break-glass"}
UI = {"Origin": ORIGIN, "X-BURNRATE-Request": "1"}


def settings_at(path):
    return Settings(database_path=path / "test.db", pricing_path=ROOT / "pricing",
                    anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                    cache_hit_threshold=.75, over_routing_token_ceiling=40000)


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-break-glass")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "synthetic-setup")
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
    result = client.post("/api/account/register", json={"username": "synthetic-owner", "password": PASSWORD,
                         "setupToken": "synthetic-setup", "sessionOnly": True, "deviceLabel": "Synthetic browser"})
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
    headers = {"Authorization": "Bearer " + own["token"]}
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
    headers = {"Authorization": "Bearer " + reg["token"]}
    with ThreadPoolExecutor(2) as pool:
        responses = list(pool.map(lambda _: client.post("/api/auth/machine/rotate", headers=headers, json={}), range(2)))
    assert sorted(r.status_code for r in responses) == [200, 401]
    replacement = next(r.json() for r in responses if r.status_code == 200)
    assert auth.parse(replacement["expiresAt"]) > auth.moment() + timedelta(days=89)
    assert client.post("/api/sync/push", headers=headers, json={}).status_code == 401
    assert client.post("/api/auth/machine/rotate", headers={"Authorization": "Bearer br_m_invalid_synthetic"}, json={}).status_code == 401
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + replacement["token"]}, json={}).status_code == 200
    with connect(settings.database_path) as connection:
        row = sync._read_state(connection)["machines"][reg["machine"]["id"]]
        assert row["last_used_at"] and not row["rotation_required"]
        assert replacement["token"] not in json.dumps(row)


def test_login_cookie_foreign_origin_logout_and_reuse(hub):
    _, client = hub
    first = owner(client)
    client.cookies.clear()
    login = client.post("/api/account/login", json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True})
    assert login.status_code == 200
    cookie = login.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=lax" in cookie and "path=/" in cookie
    token = login.json()["sessionToken"]
    assert token != "synthetic-break-glass" and len(token) >= 43
    assert client.post("/api/machines", headers={"Origin": "https://foreign.invalid"}, json={"name": "No"}).status_code == 403
    assert client.post("/api/machines", headers={"Origin": "null"}, json={"name": "No"}).status_code == 403
    assert client.post("/api/machines", headers=UI, json={"name": "Yes"}).status_code == 200
    assert client.post("/api/auth/logout", headers=UI, json={}).status_code == 200
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + token}).status_code == 401
    client.cookies.set(auth.COOKIE, token, path="/api")
    assert client.get("/api/machines").status_code == 401
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + first.json()["sessionToken"]}).status_code == 200


def test_session_expiry_revocation_rotation_and_future_roles(hub):
    settings, client = hub
    session = owner(client).json()
    token = session["sessionToken"]
    rotated = client.post("/api/auth/session/rotate", headers=UI, json={})
    assert rotated.status_code == 200
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + token}).status_code == 401
    current = rotated.json()
    revoked = client.post("/api/auth/sessions/revoke", headers=UI, json={"id": current["sessionId"]})
    assert revoked.status_code == 200
    assert client.get("/api/machines", headers={"Authorization": "Bearer " + current["sessionToken"]}).status_code == 401
    client.cookies.clear()
    login = client.post("/api/account/login", json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True}).json()
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
    assert bootstrap.status_code == 200 and bootstrap.json()["sessionToken"].startswith("br_s_")
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
    assert client.post("/api/pairing/redeem", headers={"Host": "other.invalid"}, json=payload).status_code == 403
    # Another allowed Host also fails: allowlisting is not grant binding.
    assert client.post("/api/pairing/redeem", headers={"Host": "localhost"}, json=payload).status_code == 403
    result = client.post("/api/pairing/redeem", json=payload)
    assert result.status_code == 200
    reg = result.json()
    assert auth.parse(reg["expiresAt"]) > auth.moment() + timedelta(days=89)
    assert client.post("/api/pairing/redeem", json=payload).status_code == 403
    sync.revoke_machine(settings.database_path, machine_id=reg["machine"]["id"])
    repaired = pairing(client, machineId=reg["machine"]["id"])
    new = client.post("/api/pairing/redeem", json={"pairingCode": repaired["pairingCode"]})
    assert new.status_code == 200 and new.json()["machine"]["id"] == reg["machine"]["id"]
    assert new.json()["token"] != reg["token"]
    expiring = pairing(client, name="Expiring device")
    with connect(settings.database_path) as connection:
        connection.execute("UPDATE pairing_grants SET expires_at=? WHERE used_at IS NULL", (auth.stamp(auth.moment() - timedelta(seconds=1)),))
    assert client.post("/api/pairing/redeem", json={"pairingCode": expiring["pairingCode"]}).status_code == 403


def test_pairing_concurrent_redemption_only_one_succeeds(hub):
    _, client = hub
    code = pairing(client)["pairingCode"]
    with ThreadPoolExecutor(2) as pool:
        results = list(pool.map(lambda _: client.post("/api/pairing/redeem", json={"pairingCode": code}), range(2)))
    assert sorted(r.status_code for r in results) == [200, 403]


def test_pairing_refuses_colliding_names_but_allows_explicit_machine_id(hub):
    settings, client = hub
    first = sync.register_machine(settings.database_path, name="Workstation")
    second = sync.register_machine(settings.database_path, name="workstation")
    first_id, second_id = first["machine"]["id"], second["machine"]["id"]
    assert first_id != second_id

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

    # Name-only reconnect is equally refused when multiple active machines share a name.
    with pytest.raises(ValueError, match="machine ID"):
        sync.register_or_reconnect_machine(settings.database_path, name="WORKSTATION")

    # Explicit machineId succeeds both at the unit level and over HTTP.
    direct = pairing_module.issue(
        settings.database_path, actor, hub_url=ORIGIN, name="WORKSTATION", machine_id=second_id
    )
    assert direct["ok"] is True

    explicit = pairing(client, name="WORKSTATION", machineId=first_id)
    redeemed = client.post("/api/pairing/redeem", json={"pairingCode": explicit["pairingCode"]})
    assert redeemed.status_code == 200
    assert redeemed.json()["machine"]["id"] == first_id


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

    refused_login = client.post(
        "/api/account/login",
        json={"username": "synthetic-owner", "password": PASSWORD, "computerName": "revoked box"},
    )
    assert refused_login.status_code == 422

    # Even with machine_id, reconnect=True must never revive a revoked record.
    with connect(settings.database_path) as connection:
        with pytest.raises(ValueError, match="revoked"):
            sync._register_in_connection(
                connection, name="Revoked Box", reconnect=True, machine_id=mid
            )
        assert sync._read_state(connection)["machines"][mid]["revoked"] is True

    # Fresh pairing with explicit machineId still works and restores sync.
    repaired = pairing(client, name="Revoked Box", machineId=mid)
    redeemed = client.post("/api/pairing/redeem", json={"pairingCode": repaired["pairingCode"]})
    assert redeemed.status_code == 200
    new_token = redeemed.json()["token"]
    assert redeemed.json()["machine"]["id"] == mid
    assert client.post("/api/sync/push", headers={"Authorization": "Bearer " + new_token}, json={}).status_code == 200


def test_audit_actor_exactly_once_append_only_and_no_credential_material(hub):
    settings, client = hub
    session = owner(client).json()
    client.cookies.clear()
    assert client.post("/api/account/login", json={"username": "synthetic-owner", "password": "synthetic-wrong-password"}).status_code == 401
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
        assert all(secret not in material for secret in (PASSWORD, session["sessionToken"], reg["token"], "synthetic-break-glass", "synthetic-owner"))
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
        assert client.post("/api/account/login", json={"username": "synthetic-owner", "password": PASSWORD, "sessionOnly": True}).status_code == 200


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
        legacy_headers = {"Authorization": "Bearer " + legacy_token}
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
        expired_headers = {"Authorization": "Bearer " + needs_backfill["token"]}
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
        missing_headers = {"Authorization": "Bearer " + keeps_deadline["token"]}
        assert client.post("/api/sync/push", headers=missing_headers, json={}).status_code == 401
        assert client.post("/api/auth/machine/rotate", headers=missing_headers, json={}).status_code == 401

        machine_row(settings, keeps_deadline["machine"]["id"], rotation_deadline="not-a-timestamp")
        assert client.post("/api/sync/push", headers=missing_headers, json={}).status_code == 401
        assert client.post("/api/auth/machine/rotate", headers=missing_headers, json={}).status_code == 401
