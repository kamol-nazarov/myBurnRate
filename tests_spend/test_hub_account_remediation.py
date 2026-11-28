"""Defensive checks for hub enrollment, authorization, and bounded input."""
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import auth_model, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.hub_update import create_hub_owner


WEB = {"Origin": "https://testserver", "X-BURNRATE-Request": "1"}
PASSWORD = "synthetic-owner-password"
SETUP = "synthetic-bootstrap-secret"
ACCOUNT = {"username": "owner@example.com", "password": PASSWORD}


@pytest.fixture
def hub(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "testserver")
    pricing = Path(__file__).resolve().parents[1] / "pricing"
    settings = Settings(
        database_path=tmp_path / "control.db", pricing_path=pricing, timezone="UTC",
        anthropic_admin_key=None, openai_admin_key=None, cache_hit_threshold=.75,
        over_routing_token_ceiling=40000,
    )
    auth_throttle.reset()
    with TestClient(create_app(settings, enable_scheduler=False), base_url="https://testserver",
                    client=("192.0.2.25", 50001)) as client:
        yield settings.database_path, client
    auth_throttle.reset()


def test_first_owner_is_created_with_the_configured_setup_credential(hub):
    _, client = hub
    response = client.post("/api/account/register", headers=WEB, json={**ACCOUNT, "setupToken": SETUP})
    assert response.status_code == 200, response.text
    cookies = response.headers.get_list("set-cookie")
    host_cookie = next(entry for entry in cookies if entry.startswith(auth_model.COOKIE + "="))
    assert "Secure" in host_cookie and "HttpOnly" in host_cookie and "Path=/" in host_cookie
    assert "Domain=" not in host_cookie
    assert not any(entry.startswith(auth_model.LEGACY_COOKIE + "=") for entry in cookies)


def test_setup_requires_an_independent_credential(hub):
    path, client = hub
    assert client.post("/api/account/register", headers=WEB, json=ACCOUNT).status_code == 403
    with connect(path) as connection:
        assert setup_wizard._read_accounts(connection)["users"] == {}


def test_hub_enrollment_retains_its_startup_role_when_another_app_starts(hub, monkeypatch):
    _, client = hub
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER")
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN")
    assert client.post("/api/account/register", headers=WEB, json=ACCOUNT).status_code == 403
    assert client.post("/api/account/register", headers=WEB, json={**ACCOUNT, "setupToken": SETUP}).status_code == 200


def test_local_owner_command_prompts_and_refuses_a_second_account(hub):
    path, _ = hub
    invoke = lambda email: create_hub_owner(path, email, prompt_fn=lambda _: PASSWORD, echo=lambda _: None)
    assert invoke(ACCOUNT["username"]) == 0
    assert invoke("second@example.com") == 2
    with connect(path) as connection:
        assert list(setup_wizard._read_accounts(connection)["users"]) == [ACCOUNT["username"]]


def test_machine_revocation_updates_bound_sessions_only(tmp_path):
    path = tmp_path / "scope.db"
    initialize(path)
    now = datetime(2026, 1, 1, tzinfo=UTC)
    owner = auth_model.Principal("owner", "owner", "owner")
    with connect(path) as connection:
        enrolled = auth_model.create_session(connection, owner, password_authenticated=True, now=now)
        independent = auth_model.create_session(connection, owner, password_authenticated=True, now=now)
    actor = auth_model.authenticate_session(path, enrolled["sessionToken"], now=now)
    assert actor is not None
    registered = sync.register_machine(path, name="Workstation", actor=actor, now=now)
    machine = registered["machine"]["id"]
    with connect(path) as connection:
        row = connection.execute("SELECT machine_id FROM owner_sessions WHERE id=?", (actor.session_id,)).fetchone()
        assert row["machine_id"] == machine
    sync.revoke_machine(path, machine_id=machine, actor=owner, now=now + timedelta(seconds=1))
    with connect(path) as connection:
        rows = {r["id"]:r["revoked_at"] for r in connection.execute("SELECT id,revoked_at FROM owner_sessions")}
        assert rows[enrolled["sessionId"]]
        assert rows[independent["sessionId"]] is None


def test_session_lifetime_is_capped_at_password_sign_in(tmp_path):
    path = tmp_path / "session.db"
    initialize(path)
    started = datetime(2026, 1, 1, tzinfo=UTC)
    renewed = started + timedelta(days=89)
    owner = auth_model.Principal("owner", "owner", "owner")
    with connect(path) as connection:
        current = auth_model.create_session(
            connection, owner, now=renewed, auth_time=started, password_authenticated=True,
        )
    assert auth_model.parse(current["expiresAt"]) == started + timedelta(days=90)
    assert auth_model.authenticate_session(path, current["sessionToken"], now=renewed)
    assert auth_model.authenticate_session(path, current["sessionToken"], now=started + timedelta(days=91)) is None


def test_password_check_has_equal_work_for_supported_accounts(monkeypatch):
    calls = []

    def derive(_password, salt, iterations):
        calls.append(iterations)
        return salt, "11" * 32

    monkeypatch.setattr(setup_wizard, "_hash_password", derive)
    for record in (None, {"iterations": setup_wizard.LEGACY_PBKDF2_ITERATIONS},
                   {"iterations": setup_wizard.PBKDF2_ITERATIONS}):
        calls.clear()
        if record:
            record.update(salt="aa" * 16, passwordHash="bb" * 32)
        assert not setup_wizard._verify_sign_in_password("synthetic", record)
        assert sum(calls) == setup_wizard.PBKDF2_ITERATIONS + 1


def test_audit_attributes_signin_and_counts_other_denials(hub):
    path, client = hub
    result = client.post("/api/account/login", headers=WEB, json=ACCOUNT)
    assert result.status_code == 401
    with connect(path) as connection:
        count = connection.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0]
        last = connection.execute("SELECT action,source FROM admin_audit ORDER BY id DESC LIMIT 1").fetchone()
        assert tuple(last) == ("login", "192.0.2.25")
    assert client.post("/api/setup/state", headers=WEB, json={}).status_code == 401
    with connect(path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM admin_audit").fetchone()[0] == count
    assert client.app.state.unauthenticated_denials == 2


def test_password_throttle_is_specific_to_the_resolved_address(hub):
    path, client = hub
    assert client.post("/api/account/register", headers=WEB, json={**ACCOUNT, "setupToken": SETUP}).status_code == 200
    isolated_key = f"{path}|{ACCOUNT['username']}|192.0.2.91"
    for _ in range(5):
        auth_throttle.fail(isolated_key)
    assert auth_throttle.retry_after(isolated_key) > 0
    assert client.post("/api/account/login", headers=WEB, json=ACCOUNT).status_code == 200


def test_forecast_preferences_reject_excessively_long_json(hub):
    _, client = hub
    assert client.post("/api/account/register", headers=WEB, json={**ACCOUNT, "setupToken": SETUP}).status_code == 200
    result = client.post("/api/capacity/forecast-preferences", headers=WEB, json={"note": "x" * 65536})
    assert 400 <= result.status_code < 500
