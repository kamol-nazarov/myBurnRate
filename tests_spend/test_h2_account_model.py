"""H2 account-model test suite: emails, atomic first registration, device IDs,
password change/reset, audit privacy, and automatic machine-token refresh."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta
import json
import logging
from pathlib import Path
from threading import Barrier

import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_model, auth_throttle, cli, hub_update, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION
from spend_app.native_desktop import NativeClient
from spend_app.pricing import PricingEngine
from spend_app.wealthfront_window import DesktopBridge

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.test"
HUB_HEADERS = {
    "Origin": HUB_URL,
    "X-BURNRATE-Request": "1",
    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
}
LOCAL_HEADERS = {
    "Origin": "http://testserver",
    "X-BURNRATE-Request": "1",
}
PASSWORD = "synthetic-password-long-123"
NEW_PASSWORD = "synthetic-password-rotated-456"


def _settings(db_path: Path) -> Settings:
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


class _LocalTestClientAdapter:
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


def _make_bridge(local_settings: Settings, monkeypatch: pytest.MonkeyPatch, local_app, *, port: int = 17331):
    import spend_app.native_desktop as desktop

    base_url = f"http://127.0.0.1:{port}"
    tc = TestClient(local_app, base_url=base_url)
    monkeypatch.setattr(desktop.httpx, "Client", lambda *_a, **_kw: _LocalTestClientAdapter(tc, base_url))
    native = NativeClient(local_settings, port=port)
    bridge = DesktopBridge(native, preferences=local_settings.database_path.parent / "prefs.json")
    return bridge, tc


@pytest.fixture(autouse=True)
def _reset_shared(monkeypatch: pytest.MonkeyPatch):
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


def _setup_hub_and_spoke(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, *, hub_env: bool = True):
    if hub_env:
        monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL)

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)

    spoke_settings = _settings(tmp_path / "spoke.db")
    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    spoke_client = TestClient(spoke_app, base_url="http://testserver")
    return hub_settings, hub_app, hub_client, spoke_settings, spoke_app, spoke_client


def _seed_legacy_account(
    db_path: Path,
    *,
    username: str = "owner",
    password: str = PASSWORD,
    iterations: int = setup_wizard.LEGACY_PBKDF2_ITERATIONS,
    account_id: str | None = None,
) -> str:
    initialize(db_path)
    salt_hex, pw_hash = setup_wizard._hash_password(password, iterations=iterations)
    rec: dict[str, object] = {
        "username": username,
        "role": "owner",
        "salt": salt_hex,
        "passwordHash": pw_hash,
        "iterations": iterations,
        "createdAt": auth_model.stamp(),
    }
    if account_id is not None:
        rec["id"] = account_id
    with connect(db_path) as conn:
        setup_wizard._write_accounts(conn, {"version": 1, "users": {username: rec}})
    return str(account_id or "")


# ---------------------------------------------------------------------------
# 1. Email accounts & legacy migration
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_email",
    [
        "",
        "   ",
        "owner",
        "no-at.example.com",
        "@example.com",
        "alice@",
        "alice@localhost",
        "alice@.example.com",
        "alice@example.12",
        "alice..bob@example.com",
        ".alice@example.com",
        "alice.@example.com",
        "alice bob@example.com",
    ],
)
def test_h2_registration_validates_email_and_returns_auth_106(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, bad_email: str
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        res = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": bad_email, "password": PASSWORD},
        )
        assert res.status_code == 422, (bad_email, res.text)
        assert res.json() == {"error": {"code": "AUTH-106"}}
    assert setup_wizard.normalize_email(bad_email) is None
    assert setup_wizard.is_valid_email(bad_email) is False


def test_h2_registration_lowercases_trims_email_and_populates_account_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        res = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "  Owner.Name+Tag@Example.COM  ", "password": PASSWORD},
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert data["account"] == {
            "username": "owner.name+tag@example.com",
            "email": "owner.name+tag@example.com",
            "needsEmail": False,
        }
        client.cookies.clear()
        login_upper = client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "OWNER.NAME+TAG@example.com", "password": PASSWORD},
        )
        assert login_upper.status_code == 200, login_upper.text
        assert login_upper.json()["account"]["email"] == "owner.name+tag@example.com"
        assert login_upper.json()["account"]["needsEmail"] is False

    with connect(hub_settings.database_path) as conn:
        accounts = setup_wizard._read_accounts(conn)
        assert list(accounts["users"].keys()) == ["owner.name+tag@example.com"]
        rec = accounts["users"]["owner.name+tag@example.com"]
        assert rec["email"] == "owner.name+tag@example.com"
        assert rec["username"] == "owner.name+tag@example.com"
        assert rec["iterations"] == setup_wizard.PBKDF2_ITERATIONS
        assert setup_wizard.account_needs_email(rec) is False


def test_h2_legacy_non_email_account_signs_in_and_migrates_via_hub_spoke_and_bridge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hs, _ha, hub_client, ss, spoke_app, spoke_client = _setup_hub_and_spoke(tmp_path, monkeypatch)
    _seed_legacy_account(hs.database_path, username="owner", password=PASSWORD, account_id="acct-legacy-1")

    with hub_client, spoke_client:
        # 1. Direct /api/account/login flags needsEmail=True and upgrades PBKDF2 to 600_000
        hub_login = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "OWNER", "password": PASSWORD},
        )
        assert hub_login.status_code == 200, hub_login.text
        hub_acct = hub_login.json()["account"]
        assert hub_acct["username"] == "owner"
        assert hub_acct["needsEmail"] is True
        assert "email" not in hub_acct

        with connect(hs.database_path) as conn:
            upgraded = setup_wizard._read_accounts(conn)["users"]["owner"]
            assert upgraded["iterations"] == setup_wizard.PBKDF2_ITERATIONS

        # 2. Spoke /api/setup/account signs in and persists needsEmail=True
        join_res = spoke_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={
                "mode": "login",
                "username": "owner",
                "password": PASSWORD,
                "computerName": "LegacySpoke",
                "testSync": False,
            },
        )
        assert join_res.status_code == 200, join_res.text
        assert join_res.json()["account"]["needsEmail"] is True
        assert join_res.json()["setup"]["account"]["needsEmail"] is True
        assert join_res.json()["setup"]["account"]["emailPromptDismissed"] is False

        # 3. Dismiss email prompt on spoke via both HTTP and DesktopBridge
        dismiss_http = spoke_client.post(
            "/api/setup/account/dismiss-email-prompt", headers=LOCAL_HEADERS, json={}
        )
        assert dismiss_http.status_code == 200, dismiss_http.text
        assert dismiss_http.json()["setup"]["account"]["emailPromptDismissed"] is True

        # Reset dismissed flag, test DesktopBridge.dismiss_email_prompt + set_account_email
        with connect(ss.database_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            st["account"]["emailPromptDismissed"] = False
            setup_wizard.write_setup_state(conn, st)

        bridge, _tc = _make_bridge(ss, monkeypatch, spoke_app)
        bridge_dismiss = bridge.dismiss_email_prompt()
        assert bridge_dismiss["ok"] is True
        assert bridge_dismiss["setup"]["account"]["emailPromptDismissed"] is True

        # 4. DesktopBridge validation + wrong password + direct /api/account/email checks
        bad_email_bridge = bridge.set_account_email("not-an-email", PASSWORD)
        assert bad_email_bridge["ok"] is False and bad_email_bridge["code"] == "AUTH-106"

        empty_pw_bridge = bridge.set_account_email("Owner@Example.com", "")
        assert empty_pw_bridge["ok"] is False and empty_pw_bridge["code"] == "AUTH-101"

        wrong_pw_bridge = bridge.set_account_email("Owner@Example.com", "wrong-password-123")
        assert wrong_pw_bridge["ok"] is False and wrong_pw_bridge["code"] == "AUTH-101"

        bad_hub_email = hub_client.post(
            "/api/account/email",
            headers=HUB_HEADERS,
            json={"email": "not-an-email", "password": PASSWORD},
        )
        assert bad_hub_email.status_code == 422
        assert bad_hub_email.json() == {"error": {"code": "AUTH-106"}}

        bad_hub_pw = hub_client.post(
            "/api/account/email",
            headers=HUB_HEADERS,
            json={"email": "owner@example.com", "password": "wrong-password-123"},
        )
        assert bad_hub_pw.status_code == 401
        assert bad_hub_pw.json() == {"error": {"code": "AUTH-101"}}

        # Also validate /api/setup/account/email invalid email directly
        bad_spoke_email = spoke_client.post(
            "/api/setup/account/email",
            headers=LOCAL_HEADERS,
            json={"email": "still-bad", "password": PASSWORD},
        )
        assert bad_spoke_email.status_code == 422
        assert bad_spoke_email.json() == {"error": {"code": "AUTH-106"}}

        # 5. Valid DesktopBridge.set_account_email -> calls /api/setup/account/email -> hub /api/account/email
        ok_bridge = bridge.set_account_email("  Owner.Migrated@Example.COM ", PASSWORD)
        assert ok_bridge["ok"] is True, ok_bridge
        assert ok_bridge["account"]["username"] == "owner.migrated@example.com"
        assert ok_bridge["account"]["needsEmail"] is False
        assert ok_bridge["account"]["emailPromptDismissed"] is False

        # Hub DB is re-keyed, legacy key removed, opaque id preserved, audit logged
        with connect(hs.database_path) as conn:
            users = setup_wizard._read_accounts(conn)["users"]
            assert "owner" not in users
            assert list(users.keys()) == ["owner.migrated@example.com"]
            rec = users["owner.migrated@example.com"]
            assert rec["id"] == "acct-legacy-1"
            assert rec["email"] == "owner.migrated@example.com"
            assert rec["username"] == "owner.migrated@example.com"
            audits = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM admin_audit WHERE action='account.email_set' ORDER BY id"
                )
            ]
        assert any(r["outcome"] == "failure" and r["target"] == "acct-legacy-1" for r in audits)
        assert sum(r["outcome"] == "success" and r["target"] == "acct-legacy-1" for r in audits) == 1

        # Old non-email username no longer logs in; new email does with needsEmail=False
        hub_client.cookies.clear()
        old_login = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner", "password": PASSWORD},
        )
        assert old_login.status_code == 401 and old_login.json() == {"error": {"code": "AUTH-101"}}

        new_login = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner.migrated@example.com", "password": PASSWORD},
        )
        assert new_login.status_code == 200, new_login.text
        assert new_login.json()["account"] == {
            "username": "owner.migrated@example.com",
            "email": "owner.migrated@example.com",
            "needsEmail": False,
        }


def test_h2_email_change_requires_owner_session_and_bridge_validates_register_locally(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hs, _ha, hub_client, ss, spoke_app, spoke_client = _setup_hub_and_spoke(tmp_path, monkeypatch)
    machine = sync.register_machine(hs.database_path, name="MachineOnly")

    with hub_client, spoke_client:
        # Unauthenticated /api/account/email -> 401 AUTH-107
        assert hub_client.post(
            "/api/account/email",
            headers=HUB_HEADERS,
            json={"email": "new@example.com", "password": PASSWORD},
        ).status_code == 401
        # Machine bearer /api/account/email -> 403 DEV-301
        machine_res = hub_client.post(
            "/api/account/email",
            headers={**HUB_HEADERS, "Authorization": f"Bearer {machine['token']}"},
            json={"email": "new@example.com", "password": PASSWORD},
        )
        assert machine_res.status_code == 403
        assert machine_res.json() == {"error": {"code": "DEV-301"}}

        # Standalone spoke /api/setup/account/email -> 409 AUTH-107
        standalone_res = spoke_client.post(
            "/api/setup/account/email",
            headers=LOCAL_HEADERS,
            json={"email": "new@example.com", "password": PASSWORD},
        )
        assert standalone_res.status_code == 409
        assert standalone_res.json() == {"error": {"code": "AUTH-107"}}

        # DesktopBridge.connect_hub_account(mode="register") validates email & password length locally
        bridge, _tc = _make_bridge(ss, monkeypatch, spoke_app)
        bad_reg_email = bridge.connect_hub_account("not-an-email", PASSWORD, mode="register")
        assert bad_reg_email["ok"] is False and bad_reg_email["code"] == "AUTH-106"

        bad_reg_pw = bridge.connect_hub_account("Owner@Example.com", "short", mode="register")
        assert bad_reg_pw["ok"] is False and bad_reg_pw["code"] == "AUTH-105"


# ---------------------------------------------------------------------------
# 2. First-account registration, closure, concurrency, AUTH-104 & env warnings
# ---------------------------------------------------------------------------


def test_h2_empty_hub_login_returns_auth_104_and_registration_closes_permanently(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        empty_login = client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert empty_login.status_code == 401
        assert empty_login.json() == {"error": {"code": "AUTH-104"}}

        first = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert first.status_code == 200, first.text
        client.cookies.clear()

        # Closed for different email
        second_diff = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "other@example.com", "password": PASSWORD},
        )
        assert second_diff.status_code == 403
        assert second_diff.json() == {"error": {"code": "AUTH-103"}}

        # Closed even for the same email
        second_same = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert second_same.status_code == 403
        assert second_same.json() == {"error": {"code": "AUTH-103"}}

        # Closed even when BURNRATE_ALLOW_REGISTRATION=1 is set
        monkeypatch.setenv("BURNRATE_ALLOW_REGISTRATION", "1")
        with_allow_env = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "third@example.com", "password": PASSWORD},
        )
        assert with_allow_env.status_code == 403
        assert with_allow_env.json() == {"error": {"code": "AUTH-103"}}


def test_h2_concurrent_first_account_registration_allows_single_winner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "race.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    n_racers = 4
    gate = Barrier(n_racers, timeout=15)

    def _race(idx: int):
        with TestClient(hub_app, base_url=HUB_URL) as c:
            gate.wait()
            return c.post(
                "/api/account/register",
                headers=HUB_HEADERS,
                json={"username": f"racer{idx}@example.com", "password": PASSWORD},
            )

    with ThreadPoolExecutor(max_workers=n_racers) as pool:
        responses = list(pool.map(_race, range(n_racers)))

    statuses = sorted(r.status_code for r in responses)
    assert statuses == [200] + [403] * (n_racers - 1), [(r.status_code, r.text) for r in responses]
    for r in responses:
        if r.status_code == 403:
            assert r.json() == {"error": {"code": "AUTH-103"}}

    with connect(hub_settings.database_path) as conn:
        users = setup_wizard._read_accounts(conn)["users"]
        assert len(users) == 1
        reg_audits = conn.execute(
            "SELECT COUNT(*) FROM admin_audit WHERE action='account.register' AND outcome='success'"
        ).fetchone()[0]
        assert reg_audits == 1


def test_h2_deprecated_account_env_warnings_and_startup_logs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOW_REGISTRATION", raising=False)
    assert setup_wizard.deprecated_account_env_warnings() == []

    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", "legacy-token")
    monkeypatch.setenv("BURNRATE_ALLOW_REGISTRATION", "1")
    warnings = setup_wizard.deprecated_account_env_warnings()
    assert len(warnings) == 2
    assert any("BURNRATE_SETUP_TOKEN" in w for w in warnings)
    assert any("BURNRATE_ALLOW_REGISTRATION" in w for w in warnings)

    with caplog.at_level(logging.WARNING, logger="burnrate.setup"):
        create_app(_settings(tmp_path / "warn.db"), enable_scheduler=False)
    logged = "\n".join(r.message for r in caplog.records if r.name == "burnrate.setup")
    assert "BURNRATE_SETUP_TOKEN" in logged
    assert "BURNRATE_ALLOW_REGISTRATION" in logged


# ---------------------------------------------------------------------------
# 3. Device ID persistence, keyed register/reconnect, DEV-302 & legacy adoption
# ---------------------------------------------------------------------------


def test_h2_device_id_persists_in_app_meta_and_exports_in_sync_batch(tmp_path: Path) -> None:
    db_path = tmp_path / "spoke.db"
    initialize(db_path)
    first = sync.get_or_create_device_id(db_path)
    assert first.startswith("dev-")
    assert sync.get_or_create_device_id(db_path) == first

    with connect(db_path) as conn:
        stored = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (sync.DEVICE_ID_META_KEY,)
        ).fetchone()
    assert stored is not None and stored[0] == first

    payload, _next_cursor = sync.export_sync_batch(db_path)
    assert payload["machine"]["deviceId"] == first

    # Corrupted/too-short value in app_meta triggers regeneration on next call
    with connect(db_path) as conn:
        conn.execute(
            "UPDATE app_meta SET value='bad' WHERE key=?", (sync.DEVICE_ID_META_KEY,)
        )
    regenerated = sync.get_or_create_device_id(db_path)
    assert regenerated != "bad" and regenerated.startswith("dev-")
    assert sync.get_or_create_device_id(db_path) == regenerated


def test_h2_machines_register_keys_by_device_id_and_blocks_name_clash(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        reg_owner = client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert reg_owner.status_code == 200

        # Register device A -> creates machine record
        dev_a = "dev-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
        m1 = client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "register", "name": "Workstation", "os": "win32", "deviceId": dev_a},
        )
        assert m1.status_code == 200, m1.text
        mid_a = m1.json()["machine"]["id"]
        tok_a1 = m1.json()["token"]

        # Same deviceId with renamed computerName reconnects SAME machine ID and rotates token
        m1_reconnect = client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "register", "name": "Workstation Renamed", "os": "win32", "deviceId": dev_a},
        )
        assert m1_reconnect.status_code == 200, m1_reconnect.text
        assert m1_reconnect.json()["machine"]["id"] == mid_a
        assert m1_reconnect.json()["machine"]["name"] == "Workstation Renamed"
        tok_a2 = m1_reconnect.json()["token"]
        assert tok_a2 != tok_a1

        assert client.post("/api/sync/push", headers={"Authorization": f"Bearer {tok_a1}"}, json={}).status_code == 401
        assert client.post("/api/sync/push", headers={"Authorization": f"Bearer {tok_a2}"}, json={}).status_code == 200

        # Name collision with a DIFFERENT active deviceId -> 422 DEV-302
        dev_b = "dev-bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
        clash = client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "register", "name": "workstation renamed", "os": "darwin", "deviceId": dev_b},
        )
        assert clash.status_code == 422, clash.text
        assert clash.json() == {"error": {"code": "DEV-302"}}

        # New deviceId with unique name creates a distinct machine record
        m2 = client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "register", "name": "MacBook", "os": "darwin", "deviceId": dev_b},
        )
        assert m2.status_code == 200, m2.text
        assert m2.json()["machine"]["id"] != mid_a


def test_h2_legacy_machine_adopts_device_id_on_push_and_never_steals_active_owner(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "hub.db")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        owner_reg = sync.register_machine(hub_settings.database_path, name="OwnerOwned", device_id="dev-1111111111111111")
        legacy_1 = sync.register_machine(hub_settings.database_path, name="LegacyOne")
        legacy_2 = sync.register_machine(hub_settings.database_path, name="LegacyTwo")

        with connect(hub_settings.database_path) as conn:
            st = sync._read_state(conn)
            assert "device_id" not in st["machines"][legacy_1["machine"]["id"]]
            assert "device_id" not in st["machines"][legacy_2["machine"]["id"]]

        # LegacyOne pushes with its deviceId -> adopts it
        adopted_dev = "dev-2222222222222222"
        push1 = client.post(
            "/api/sync/push",
            headers={"Authorization": f"Bearer {legacy_1['token']}"},
            json={"machine": {"name": "LegacyOne", "deviceId": adopted_dev}},
        )
        assert push1.status_code == 200, push1.text
        with connect(hub_settings.database_path) as conn:
            st = sync._read_state(conn)
            assert st["machines"][legacy_1["machine"]["id"]].get("device_id") == adopted_dev

        # LegacyTwo pushes claiming OwnerOwned's deviceId -> push still succeeds, never steals device_id
        push2 = client.post(
            "/api/sync/push",
            headers={"Authorization": f"Bearer {legacy_2['token']}"},
            json={"machine": {"name": "LegacyTwo", "deviceId": "dev-1111111111111111"}},
        )
        assert push2.status_code == 200, push2.text
        with connect(hub_settings.database_path) as conn:
            st = sync._read_state(conn)
            assert "device_id" not in st["machines"][legacy_2["machine"]["id"]]
            assert st["machines"][owner_reg["machine"]["id"]].get("device_id") == "dev-1111111111111111"


# ---------------------------------------------------------------------------
# 4. Password change (hub & spoke)
# ---------------------------------------------------------------------------


def test_h2_password_change_validates_rotates_and_revokes_other_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hs, _ha, hub_client, ss, _sa, spoke_client = _setup_hub_and_spoke(tmp_path, monkeypatch)
    pricing = PricingEngine.load(hs.pricing_path)

    with hub_client, spoke_client:
        # Anon rejected on /api/account/password
        assert hub_client.post(
            "/api/account/password",
            headers=HUB_HEADERS,
            json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        ).status_code == 401

        # Seed owner with legacy 120k PBKDF2 to ensure rotation upgrades to 600k
        _seed_legacy_account(
            hs.database_path,
            username="owner@example.com",
            password=PASSWORD,
            iterations=setup_wizard.LEGACY_PBKDF2_ITERATIONS,
            account_id="acct-pw-1",
        )
        machine = sync.register_machine(hs.database_path, name="Mach")
        assert hub_client.post(
            "/api/account/password",
            headers={**HUB_HEADERS, "Authorization": f"Bearer {machine['token']}"},
            json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        ).status_code == 403

        # Create caller session (s1) + another browser session (s2) + spoke session (s3)
        l1 = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert l1.status_code == 200
        s1_token = l1.cookies[auth_model.COOKIE]
        hub_client.cookies.clear()

        l2 = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert l2.status_code == 200
        s2_token = l2.cookies[auth_model.COOKIE]
        hub_client.cookies.clear()

        # Connect spoke (gets s3)
        spoke_join = spoke_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "SpokePW",
                "testSync": False,
            },
        )
        assert spoke_join.status_code == 200, spoke_join.text
        with connect(ss.database_path) as conn:
            s3_token = setup_wizard.read_setup_state(conn)["spokeConfig"]["ownerSession"]

        # Wrong current password -> 401 AUTH-101
        wrong_cur = hub_client.post(
            "/api/account/password",
            headers={**HUB_HEADERS, "Authorization": f"Bearer {s1_token}"},
            json={"currentPassword": "wrong-current-password", "newPassword": NEW_PASSWORD},
        )
        assert wrong_cur.status_code == 401 and wrong_cur.json() == {"error": {"code": "AUTH-101"}}

        # Short new password -> 422 AUTH-105
        short_new = hub_client.post(
            "/api/account/password",
            headers={**HUB_HEADERS, "Authorization": f"Bearer {s1_token}"},
            json={"currentPassword": PASSWORD, "newPassword": "short"},
        )
        assert short_new.status_code == 422 and short_new.json() == {"error": {"code": "AUTH-105"}}

        # Spoke relays /api/setup/account/password with s3_token: wrong password -> 401 AUTH-101
        spoke_wrong = spoke_client.post(
            "/api/setup/account/password",
            headers=LOCAL_HEADERS,
            json={"currentPassword": "wrong-current-password", "newPassword": NEW_PASSWORD},
        )
        assert spoke_wrong.status_code == 401 and spoke_wrong.json() == {"error": {"code": "AUTH-101"}}

        # Change password via spoke (keeps s3 valid, revokes s1 and s2!)
        with connect(hs.database_path) as conn:
            before_rec = dict(setup_wizard._read_accounts(conn)["users"]["owner@example.com"])
        ok_spoke = spoke_client.post(
            "/api/setup/account/password",
            headers=LOCAL_HEADERS,
            json={"currentPassword": PASSWORD, "newPassword": NEW_PASSWORD},
        )
        assert ok_spoke.status_code == 200 and ok_spoke.json() == {"ok": True}

        with connect(hs.database_path) as conn:
            after_rec = setup_wizard._read_accounts(conn)["users"]["owner@example.com"]
            assert after_rec["salt"] != before_rec["salt"]
            assert after_rec["passwordHash"] != before_rec["passwordHash"]
            assert after_rec["iterations"] == setup_wizard.PBKDF2_ITERATIONS
            assert after_rec.get("passwordChangedAt")

        # s1 and s2 are revoked; s3 remains valid!
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {s1_token}"}).status_code == 401
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {s2_token}"}).status_code == 401
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {s3_token}"}).status_code == 200

        # Old password rejected, new password succeeds
        assert hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        ).status_code == 401
        rel_ok = hub_client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": NEW_PASSWORD, "sessionOnly": True},
        )
        assert rel_ok.status_code == 200


# ---------------------------------------------------------------------------
# 5. Hub CLI `burnrate hub reset-password <email>`
# ---------------------------------------------------------------------------


def test_h2_hub_cli_reset_password_validates_rotates_and_revokes_all_sessions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "cli_hub.db")
    _seed_legacy_account(
        hub_settings.database_path,
        username="owner@example.com",
        password=PASSWORD,
        iterations=setup_wizard.LEGACY_PBKDF2_ITERATIONS,
        account_id="acct-cli-1",
    )

    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        l1 = client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        s1 = l1.cookies[auth_model.COOKIE]
        client.cookies.clear()
        l2 = client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        s2 = l2.cookies[auth_model.COOKIE]
        client.cookies.clear()

        # Unknown email -> 2, does not prompt or revoke sessions
        messages: list[str] = []
        rc_missing = hub_update.reset_hub_password(
            hub_settings.database_path,
            "nobody@example.com",
            prompt_fn=lambda _p: (_ for _ in ()).throw(AssertionError("should not prompt")),
            echo=messages.append,
        )
        assert rc_missing == 2
        assert any("No matching account" in m for m in messages)

        # Mismatched passwords -> 2, sessions stay valid
        answers = iter(["first-long-password-1", "second-long-password-2"])
        messages.clear()
        rc_mismatch = hub_update.reset_hub_password(
            hub_settings.database_path,
            "OWNER@Example.COM",
            prompt_fn=lambda _p: next(answers),
            echo=messages.append,
        )
        assert rc_mismatch == 2
        assert any("do not match" in m for m in messages)
        assert client.get("/api/machines", headers={"Authorization": f"Bearer {s1}"}).status_code == 200

        # Too-short password -> 2, sessions stay valid
        answers_short = iter(["too-short", "too-short"])
        messages.clear()
        rc_short = hub_update.reset_hub_password(
            hub_settings.database_path,
            "OWNER@Example.COM",
            prompt_fn=lambda _p: next(answers_short),
            echo=messages.append,
        )
        assert rc_short == 2
        assert any("12" in m for m in messages)
        assert client.get("/api/machines", headers={"Authorization": f"Bearer {s1}"}).status_code == 200

        # Valid reset via `burnrate hub reset-password <email>` CLI dispatcher
        import sys as _sys

        cli_answers = iter([NEW_PASSWORD, NEW_PASSWORD])
        monkeypatch.setitem(
            hub_update.reset_hub_password.__kwdefaults__, "prompt_fn", lambda _p="": next(cli_answers)
        )
        monkeypatch.setattr(cli, "load_settings", lambda: hub_settings)
        monkeypatch.setattr(_sys, "argv", ["burnrate", "hub", "reset-password", "OWNER@Example.COM"])
        rc_ok = cli.main()
        assert rc_ok == 0

        # ALL owner_sessions are revoked
        assert client.get("/api/machines", headers={"Authorization": f"Bearer {s1}"}).status_code == 401
        assert client.get("/api/machines", headers={"Authorization": f"Bearer {s2}"}).status_code == 401

        with connect(hub_settings.database_path) as conn:
            rec = setup_wizard._read_accounts(conn)["users"]["owner@example.com"]
            assert rec["iterations"] == setup_wizard.PBKDF2_ITERATIONS
            active_sessions = conn.execute(
                "SELECT COUNT(*) FROM owner_sessions WHERE revoked_at IS NULL"
            ).fetchone()[0]
            assert active_sessions == 0
            audits = [
                dict(row)
                for row in conn.execute(
                    "SELECT * FROM admin_audit WHERE action='account.password_reset'"
                )
            ]
        assert len(audits) == 1
        assert audits[0]["actor_type"] == "system"
        assert audits[0]["actor_id"] == "local-cli"
        assert audits[0]["target"] == "acct-cli-1"
        assert audits[0]["outcome"] == "success"

        # Old password fails, new password succeeds
        assert client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        ).status_code == 401
        assert client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": NEW_PASSWORD},
        ).status_code == 200


# ---------------------------------------------------------------------------
# 6. Audit privacy
# ---------------------------------------------------------------------------


def test_h2_audit_logs_never_leak_emails_usernames_or_passwords(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _settings(tmp_path / "audit_hub.db")
    initial_email = "SecretOwner@Example.COM"
    updated_email = "UpdatedSecretEmail@Example.COM"
    pw1 = "SuperSecretPassword-111"
    pw2 = "SuperSecretPassword-222"
    pw3 = "SuperSecretPassword-333"

    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as client:
        assert client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": initial_email, "password": pw1},
        ).status_code == 200

        # Failed login + successful login
        assert client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": initial_email, "password": "WrongSecretPassword-999"},
        ).status_code == 401
        assert client.post(
            "/api/account/login",
            headers=HUB_HEADERS,
            json={"username": initial_email, "password": pw1},
        ).status_code == 200

        # Email change + password change
        assert client.post(
            "/api/account/email",
            headers=HUB_HEADERS,
            json={"email": updated_email, "password": pw1},
        ).status_code == 200
        assert client.post(
            "/api/account/password",
            headers=HUB_HEADERS,
            json={"currentPassword": pw1, "newPassword": pw2},
        ).status_code == 200

        # Register + revoke machine
        m_res = client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "register", "name": "AuditBox"},
        )
        assert m_res.status_code == 200
        mid = m_res.json()["machine"]["id"]
        assert client.post(
            "/api/machines",
            headers=HUB_HEADERS,
            json={"action": "revoke", "id": mid},
        ).status_code == 200

    # CLI password reset
    answers = iter([pw3, pw3])
    assert hub_update.reset_hub_password(
        hub_settings.database_path,
        updated_email,
        prompt_fn=lambda _p: next(answers),
        echo=lambda _m: None,
    ) == 0

    with connect(hub_settings.database_path) as conn:
        rec = setup_wizard._read_accounts(conn)["users"][updated_email.lower()]
        acct_id = rec["id"]
        rows = [dict(row) for row in conn.execute("SELECT * FROM admin_audit ORDER BY id")]

    by_action = {a: [r for r in rows if r["action"] == a] for a in (
        "account.register",
        "login",
        "account.password_change",
        "account.password_reset",
        "account.email_set",
        "machine.revoke",
    )}
    for action, matched in by_action.items():
        assert matched, f"Missing audit row for {action}"
        for row in matched:
            target = row["target"]
            assert target in {acct_id, mid} or str(target).startswith("/api/"), (action, row)

    serialized = json.dumps(rows).lower()
    for secret in (
        initial_email.lower(),
        updated_email.lower(),
        "secretowner",
        "updatedsecretemail",
        pw1.lower(),
        pw2.lower(),
        pw3.lower(),
        "wrongsecretpassword",
    ):
        assert secret not in serialized


# ---------------------------------------------------------------------------
# 7. Machine token auto-refresh
# ---------------------------------------------------------------------------


def test_h2_maybe_run_configured_spoke_sync_auto_rotates_near_expiry_and_on_flag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hs, _ha, hub_client, ss, _sa, spoke_client = _setup_hub_and_spoke(tmp_path, monkeypatch)
    pricing = PricingEngine.load(ss.pricing_path)

    with hub_client, spoke_client:
        assert hub_client.post(
            "/api/account/register",
            headers=HUB_HEADERS,
            json={"username": "owner@example.com", "password": PASSWORD},
        ).status_code == 200
        hub_client.cookies.clear()

        joined = spoke_client.post(
            "/api/setup/account",
            headers=LOCAL_HEADERS,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "AutoRotateBox",
                "testSync": False,
            },
        )
        assert joined.status_code == 200, joined.text
        mid = joined.json()["account"]["machineId"]

        with connect(ss.database_path) as conn:
            token_0 = setup_wizard.read_setup_state(conn)["spokeConfig"]["token"]

        # 1. Token expiring well outside grace window (ROTATION_GRACE_DAYS + 5) -> NO rotation
        far_exp = auth_model.stamp(auth_model.moment() + timedelta(days=auth_model.ROTATION_GRACE_DAYS + 5))
        with connect(hs.database_path) as conn:
            hst = sync._read_state(conn)
            hst["machines"][mid]["expires_at"] = far_exp
            hst["machines"][mid]["rotation_required"] = False
            sync._write_state(conn, hst)

        res_no_rot = setup_wizard.maybe_run_configured_spoke_sync(ss, pricing, client=hub_client)
        assert res_no_rot is not None and res_no_rot["ok"] is True
        assert "tokenRotated" not in res_no_rot
        with connect(ss.database_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            active_tok = sync._vault_get_secret(ss.database_path, "burnrate-machine-token") or st["spokeConfig"]["token"]
            assert active_tok == token_0
            assert st["spokeConfig"]["tokenExpiresAt"] == far_exp
            assert st["spokeConfig"]["rotationRequired"] is False

        # 2. Token expiring within ROTATION_GRACE_DAYS (e.g. 5 days) -> auto-rotates
        near_exp = auth_model.stamp(auth_model.moment() + timedelta(days=auth_model.ROTATION_GRACE_DAYS - 3))
        with connect(hs.database_path) as conn:
            hst = sync._read_state(conn)
            hst["machines"][mid]["expires_at"] = near_exp
            sync._write_state(conn, hst)

        res_near = setup_wizard.maybe_run_configured_spoke_sync(ss, pricing, client=hub_client)
        assert res_near is not None and res_near["ok"] is True
        assert res_near.get("tokenRotated") is True
        assert res_near.get("rotationRequired") is False
        with connect(ss.database_path) as conn:
            st1 = setup_wizard.read_setup_state(conn)
            token_1 = sync._vault_get_secret(ss.database_path, "burnrate-machine-token") or st1["spokeConfig"]["token"]
            assert token_1 and token_1 != token_0
            assert auth_model.parse(st1["spokeConfig"]["tokenExpiresAt"]) > auth_model.moment() + timedelta(days=80)
            assert st1["spokeConfig"]["rotationRequired"] is False
            cursor1 = sync.read_sync_cursor(ss.database_path, HUB_URL)
            assert cursor1["credential_hash"] == sync._token_hash(token_1)

        # Old token rejected on hub, new token accepted
        assert hub_client.post("/api/sync/push", headers={"Authorization": f"Bearer {token_0}"}, json={}).status_code == 401
        assert hub_client.post("/api/sync/push", headers={"Authorization": f"Bearer {token_1}"}, json={}).status_code == 200

        # 3. Hub marks rotation_required=True (with grace deadline) even with distant expiry -> auto-rotates
        with connect(hs.database_path) as conn:
            hst = sync._read_state(conn)
            hst["machines"][mid]["rotation_required"] = True
            hst["machines"][mid]["rotation_deadline"] = auth_model.stamp(
                auth_model.moment() + timedelta(days=auth_model.ROTATION_GRACE_DAYS)
            )
            sync._write_state(conn, hst)

        res_req = setup_wizard.maybe_run_configured_spoke_sync(ss, pricing, client=hub_client)
        assert res_req is not None and res_req["ok"] is True
        assert res_req.get("tokenRotated") is True
        assert res_req.get("rotationRequired") is False
        with connect(ss.database_path) as conn:
            st2 = setup_wizard.read_setup_state(conn)
            token_2 = sync._vault_get_secret(ss.database_path, "burnrate-machine-token") or st2["spokeConfig"]["token"]
            assert token_2 != token_1
            assert st2["spokeConfig"]["rotationRequired"] is False
            cursor2 = sync.read_sync_cursor(ss.database_path, HUB_URL)
            assert cursor2["credential_hash"] == sync._token_hash(token_2)
        with connect(hs.database_path) as conn:
            hub_rec = sync._read_state(conn)["machines"][mid]
            assert hub_rec["rotation_required"] is False

        assert hub_client.post("/api/sync/push", headers={"Authorization": f"Bearer {token_1}"}, json={}).status_code == 401
        assert hub_client.post("/api/sync/push", headers={"Authorization": f"Bearer {token_2}"}, json={}).status_code == 200
