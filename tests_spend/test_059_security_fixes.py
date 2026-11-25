"""Regression tests for 0.5.9 security findings S1, S2, S4, S6-S12, and B3-B6."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
import json
import logging
import sqlite3
from pathlib import Path
import re
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from spend_app import aggregate, auth_model, auth_throttle, cli, connection_secrets, db as db_mod, setup_wizard, sync
from spend_app.api import create_app
from spend_app.connection_paths import LocationError
from spend_app.connection_secrets import (
    MACHINE_TOKEN_VAULT_REF,
    OWNER_SESSION_VAULT_REF,
    clear_spoke_credentials,
    publish_spoke_credentials,
    read_vault_secret,
    set_vault_factory,
)
from spend_app.db import connect, initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION, CodedError, error_message
from spend_app.native_desktop import NativeClient
from spend_app.pricing import PricingEngine
from spend_app.wealthfront_window import DesktopBridge
from tests_spend.test_h5_desktop_account_ui import (
    _h5_api_js,
    _h5_settings_fixture,
    _wait_for_first_sync,
)
from tests_spend.test_h6_bridge_and_packaging import (
    HUB_URL,
    PASSWORD,
    _MemoryVault,
    _create_paired_hub_and_spoke,
    _make_settings,
    _patch_native_to_local,
)
from tests_spend.test_wealthfront_ui import (
    FAST,
    browser,
    build_page,
    demo_report,
    needs_chrome,
    probe,
)


ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _isolated_059_env(monkeypatch: pytest.MonkeyPatch):
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    sync._HUB_TEST_CLIENTS.clear()
    aggregate.reset_memo()
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.delenv("BURNRATE_BUILTIN_HUB_URL", raising=False)
    yield
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    sync._HUB_TEST_CLIENTS.clear()
    aggregate.reset_memo()


class _MismatchVault:
    """Fake vault whose put() stores a mismatched secret so read-back fails."""

    def __init__(self) -> None:
        self._store: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def put(self, secret: str, ref: str | None = None) -> str:
        use_ref = ref or "burnrate-mismatch"
        self._store[use_ref] = secret + "-corrupted"
        return use_ref

    def get(self, ref: str) -> str:
        if ref not in self._store:
            raise LocationError("Missing.")
        return self._store[ref]

    def delete(self, ref: str) -> None:
        self._store.pop(ref, None)


# ---------------------------------------------------------------------------
# S1: No interpolated inline event handlers & quote-bearing machine ID XSS test
# ---------------------------------------------------------------------------


def test_s1_template_has_no_interpolated_inline_event_handlers() -> None:
    html = (ROOT / "spend_app" / "wealthfront_ui.html").read_text(encoding="utf-8")
    matches = re.findall(r"""\bon[a-z]+\s*=\s*(?:"[^"]*"|'[^']*')""", html, re.IGNORECASE)
    offenders = [m for m in matches if "${" in m]
    assert offenders == [], f"Found interpolated inline event handler(s): {offenders}"


@needs_chrome
@browser
def test_s1_quote_bearing_machine_id_cannot_execute_js_on_remove_or_confirm(
    tmp_path: Path,
) -> None:
    hostile_id = "x');globalThis.__burnrate_probe=1;//"
    settings = _h5_settings_fixture(
        signed_in=True,
        machines=[
            {
                "id": "m-cur",
                "name": "Current PC",
                "os": "win32",
                "online": True,
                "isCurrent": True,
                "lastSeenAt": "2026-11-16T12:00:00Z",
            },
            {
                "id": hostile_id,
                "name": "Hostile PC",
                "os": "linux",
                "online": True,
                "isCurrent": False,
                "lastSeenAt": "2026-11-16T11:00:00Z",
            },
        ],
    )
    probe_js = """
    globalThis.__burnrate_probe = undefined;
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.querySelectorAll('#h5AccountCard [data-h5-remove-ask]').length > 0);
    await wait(30);

    const askBtn = document.querySelector('#h5AccountCard [data-h5-remove-ask]');
    const askId = askBtn ? askBtn.dataset.h5RemoveAsk : null;
    askBtn.click();
    await until(() => document.querySelectorAll('#h5AccountCard [data-h5-remove-confirm]').length > 0);
    await wait(20);
    const probeAfterAsk = globalThis.__burnrate_probe;

    const confirmBtn = document.querySelector('#h5AccountCard [data-h5-remove-confirm]');
    const confirmId = confirmBtn ? confirmBtn.dataset.h5RemoveConfirm : null;
    confirmBtn.click();
    await wait(40);
    const probeAfterConfirm = globalThis.__burnrate_probe;

    report({
      askId,
      confirmId,
      probeAfterAsk: probeAfterAsk === undefined ? 'clean' : probeAfterAsk,
      probeAfterConfirm: probeAfterConfirm === undefined ? 'clean' : probeAfterConfirm,
      calls: window.__calls,
    });
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=None,
        api_js=_h5_api_js(settings=settings),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=860)
    assert r["askId"] == hostile_id
    assert r["confirmId"] == hostile_id
    assert r["probeAfterAsk"] == "clean"
    assert r["probeAfterConfirm"] == "clean"
    assert ["remove_computer", hostile_id] in r["calls"]


# ---------------------------------------------------------------------------
# S2: Every credential writer publishes strictly to vault and returns APP-903
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("vault_mode", ["unavailable", "mismatch"])
def test_s2_all_credential_writers_enforce_vault_only_and_app903(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], vault_mode: str
) -> None:
    hub_settings, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(
        tmp_path / f"s2_{vault_mode}", monkeypatch
    )
    _patch_native_to_local(monkeypatch, spoke_app, port=17360)
    spoke_client = TestClient(spoke_app, base_url="http://127.0.0.1:17360")
    local_headers = {
        "Host": "127.0.0.1:17360",
        "Origin": "http://127.0.0.1:17360",
        "X-BURNRATE-Request": "1",
    }

    bad_vault = None if vault_mode == "unavailable" else _MismatchVault()

    def _bad_factory(_db):
        return bad_vault

    class _ReadExistingWriteFailVault:
        """Reads pre-existing credentials cleanly while failing or corrupting writes."""

        def __init__(self, initial: dict[str, str], mode: str) -> None:
            self._store = dict(initial)
            self._mode = mode

        def available(self) -> bool:
            return True

        def put(self, secret: str, ref: str | None = None) -> str:
            if self._mode == "unavailable":
                raise LocationError("Keychain write refused.")
            use_ref = ref or "burnrate-mismatch"
            self._store[use_ref] = secret + "-corrupted"
            return use_ref

        def get(self, ref: str) -> str:
            if ref not in self._store:
                raise LocationError("Missing.")
            return self._store[ref]

        def delete(self, ref: str) -> None:
            self._store.pop(ref, None)

    def _connect_cleanly() -> tuple[str, str]:
        clear_spoke_credentials(spoke_settings.database_path, clear_machine_token=True, clear_owner_session=True)
        res_ok = spoke_client.post(
            "/api/setup/account",
            headers=local_headers,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": PASSWORD,
                "computerName": "GoodVault-PC",
                "testSync": False,
            },
        )
        assert res_ok.status_code == 200, res_ok.text
        tok = read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF)
        sess = read_vault_secret(spoke_settings.database_path, OWNER_SESSION_VAULT_REF)
        assert tok and sess
        return tok, sess

    def _assert_no_db_secrets(db_path: Path) -> None:
        with connect(db_path) as conn:
            raw_row = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_sc = (json.loads(raw_row[0]) if raw_row else {}).get("spokeConfig") or {}
        assert "token" not in raw_sc
        assert "ownerSession" not in raw_sc

    with hub_client, spoke_client:
        # 1. /api/setup/account -> 503 APP-903, zero DB secrets
        prev = set_vault_factory(_bad_factory)
        try:
            res_acct = spoke_client.post(
                "/api/setup/account",
                headers=local_headers,
                json={
                    "mode": "login",
                    "username": "owner@example.com",
                    "password": PASSWORD,
                    "computerName": "BadVault-PC",
                    "testSync": False,
                },
            )
            assert res_acct.status_code == 503, res_acct.text
            assert res_acct.json()["error"]["code"] == "APP-903"
            _assert_no_db_secrets(spoke_settings.database_path)
        finally:
            set_vault_factory(prev)

        # 2. setup_wizard.rotate_spoke_machine_token -> ok=False APP-903, zero DB token
        orig_tok2, orig_sess2 = _connect_cleanly()
        rot_vault_2 = _ReadExistingWriteFailVault(
            {MACHINE_TOKEN_VAULT_REF: orig_tok2, OWNER_SESSION_VAULT_REF: orig_sess2},
            vault_mode,
        )
        prev = set_vault_factory(lambda _db: rot_vault_2)
        try:
            rot_fn = setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client)
            assert rot_fn is not None and rot_fn.get("ok") is False
            assert rot_fn.get("code") == "APP-903"
            _assert_no_db_secrets(spoke_settings.database_path)
        finally:
            set_vault_factory(prev)

        # 3. DesktopBridge acquisition writers -> APP-903
        prev = set_vault_factory(_bad_factory)
        try:
            bridge = DesktopBridge(
                NativeClient(spoke_settings, port=17360),
                preferences=tmp_path / f"prefs_{vault_mode}.json",
                vault=bad_vault,
            )
            res = bridge.sign_in("owner@example.com", PASSWORD, "BadBridge")
            assert res["ok"] is False
            assert res["code"] == "APP-903"

            # DesktopBridge.create_account on a fresh hub with no account yet
            monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
            fresh_hub = create_app(
                _make_settings(tmp_path / f"fresh_hub_{vault_mode}.db"),
                enable_scheduler=False,
            )
            monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
            fresh_hub_client = TestClient(fresh_hub, base_url=HUB_URL)
            sync._HUB_TEST_CLIENTS[HUB_URL] = fresh_hub_client
            try:
                with fresh_hub_client:
                    res_ca = bridge.create_account("first@example.com", PASSWORD, "BadBridgeCreate")
                    assert res_ca["ok"] is False
                    assert res_ca["code"] == "APP-903"
            finally:
                sync._HUB_TEST_CLIENTS[HUB_URL] = hub_client

            _assert_no_db_secrets(spoke_settings.database_path)

            # 6. CLI desktop --token-file -> exit code 2 + APP-903 + zero DB token
            tok_file = tmp_path / f"tok_{vault_mode}.txt"
            tok_file.write_text("br_m_cli_test_token\n", encoding="utf-8")
            cli_db = tmp_path / f"cli_{vault_mode}.db"
            monkeypatch.setenv("SPEND_DATABASE_PATH", str(cli_db))
            monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
            import sys
            monkeypatch.setattr(sys, "argv", ["burnrate", "desktop", "--token-file", str(tok_file)])
            capsys.readouterr()
            rc = cli.main()
            assert rc == 2
            assert "APP-903" in capsys.readouterr().out
            _assert_no_db_secrets(cli_db)
        finally:
            set_vault_factory(prev)


# ---------------------------------------------------------------------------
# S4: Legacy migration scrubs SQLite/WAL remnants & auto-rotates credentials
# ---------------------------------------------------------------------------


def test_s4_legacy_migration_scrubs_db_and_auto_rotates_token_and_owner_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_settings, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(
        tmp_path / "s4", monkeypatch
    )

    with hub_client:
        login_res = hub_client.post(
            "/api/account/login",
            headers={"X-BURNRATE-Request": "1"},
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert login_res.status_code == 200
        legacy_session = login_res.cookies.get("burnrate_session") or ""
        if not legacy_session:
            from spend_app.auth_model import extract_cookie

            legacy_session = extract_cookie(login_res) or ""
        assert legacy_session.startswith("br_s_")

        reg_res = hub_client.post(
            "/api/machines",
            headers={"Authorization": f"Bearer {legacy_session}", "X-BURNRATE-Request": "1"},
            json={"action": "register", "name": "Legacy10PC", "os": "win32", "deviceId": "dev-s4-01"},
        )
        assert reg_res.status_code == 200
        legacy_token = reg_res.json()["token"]
        assert legacy_token.startswith("br_m_")

        # Build a realistic legacy DB with 10 historical machine records + legacy secrets
        initialize(spoke_settings.database_path)
        for i in range(10):
            sync.register_machine(
                spoke_settings.database_path,
                name=f"HistMachine-{i}",
                os_name="win32",
                device_id=f"hist-dev-{i}",
            )
        with connect(spoke_settings.database_path) as conn:
            conn.execute("PRAGMA secure_delete = OFF")
            st = setup_wizard.default_setup_state()
            st["role"] = "spoke"
            st["computerName"] = "Legacy10PC"
            st["spokeConfig"].update(
                hubUrl=HUB_URL,
                token=legacy_token,
                ownerSession=legacy_session,
                enabled=True,
            )
            setup_wizard.write_setup_state(conn, st)
            conn.commit()
        sync.prepare_spoke_sync(spoke_settings.database_path, HUB_URL, legacy_token)

        outcome = sync.migrate_legacy_spoke_secrets(spoke_settings.database_path)
        assert outcome == {"token": True, "ownerSession": True}

        # Raw DB, WAL, and SHM files must not retain either legacy credential
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(spoke_settings.database_path) + suffix)
            if p.exists():
                raw = p.read_bytes()
                assert legacy_token.encode("utf-8") not in raw, f"Legacy token found in {p.name}"
                assert legacy_session.encode("utf-8") not in raw, f"Legacy session found in {p.name}"

        with connect(spoke_settings.database_path) as conn:
            mig_row = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_mig_sc = (json.loads(mig_row[0]) if mig_row else {}).get("spokeConfig") or {}
            mig_sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "token" not in raw_mig_sc and "ownerSession" not in raw_mig_sc
        assert mig_sc.get("rotationRequired") is True
        assert mig_sc.get("ownerSessionRotationRequired") is True

        # Next spoke sync auto-rotates machine token
        sync_res = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine([]), client=hub_client
        )
        assert sync_res is not None and sync_res.get("ok") is True, sync_res
        assert sync_res.get("tokenRotated") is True
        new_token = read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF)
        assert new_token and new_token != legacy_token

        # Old machine token is now revoked on hub
        assert (
            hub_client.post(
                "/api/sync/push",
                headers={"Authorization": f"Bearer {legacy_token}"},
                json={"events": []},
            ).status_code
            == 401
        )

        # Next owner bridge hub call auto-rotates migrated owner session
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17362),
            preferences=tmp_path / "s4_prefs.json",
        )
        listed = bridge.list_computers()
        assert listed["ok"] is True, listed
        new_session = read_vault_secret(spoke_settings.database_path, OWNER_SESSION_VAULT_REF)
        assert new_session and new_session != legacy_session
        with connect(spoke_settings.database_path) as conn:
            after_sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert after_sc.get("ownerSessionRotationRequired") is False

        # Old owner session is now revoked on hub
        hub_client.cookies.clear()
        assert (
            hub_client.get(
                "/api/machines", headers={"Authorization": f"Bearer {legacy_session}"}
            ).status_code
            == 401
        )


def test_s4_rejected_migrated_owner_session_clears_vault_and_returns_auth107(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_settings, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(
        tmp_path / "s4_rej", monkeypatch
    )
    with hub_client:
        login_res = hub_client.post(
            "/api/account/login",
            headers={"X-BURNRATE-Request": "1"},
            json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
        )
        assert login_res.status_code == 200
        from spend_app.auth_model import extract_cookie

        stale_session = login_res.cookies.get("burnrate_session") or extract_cookie(login_res) or ""
        assert stale_session.startswith("br_s_")
        # Revoke that session on the hub so rotation will be rejected (401)
        hub_client.post(
            "/api/auth/logout",
            headers={"Authorization": f"Bearer {stale_session}", "X-BURNRATE-Request": "1"},
        )
        hub_client.cookies.clear()

        initialize(spoke_settings.database_path)
        with connect(spoke_settings.database_path) as conn:
            st = setup_wizard.default_setup_state()
            st["role"] = "spoke"
            st["spokeConfig"].update(
                hubUrl=HUB_URL,
                ownerSession=stale_session,
                enabled=True,
            )
            setup_wizard.write_setup_state(conn, st)

        outcome = sync.migrate_legacy_spoke_secrets(spoke_settings.database_path)
        assert outcome == {"token": False, "ownerSession": True}
        assert read_vault_secret(spoke_settings.database_path, OWNER_SESSION_VAULT_REF) == stale_session

        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17364),
            preferences=tmp_path / "s4_rej_prefs.json",
        )
        listed = bridge.list_computers()
        assert listed["ok"] is False
        assert listed["code"] == "AUTH-107"
        assert read_vault_secret(spoke_settings.database_path, OWNER_SESSION_VAULT_REF) is None
        with connect(spoke_settings.database_path) as conn:
            sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert not sc.get("ownerSessionRotationRequired")


# ---------------------------------------------------------------------------
# S6: Migration cannot clobber a concurrent fresh credential publication
# ---------------------------------------------------------------------------


def test_s6_concurrent_migration_does_not_clobber_newer_vault_credential(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "s6.db"
    initialize(db_path)
    with connect(db_path) as conn:
        st = setup_wizard.default_setup_state()
        st["role"] = "spoke"
        st["spokeConfig"].update(
            hubUrl=HUB_URL,
            token="br_m_stale_legacy",
            ownerSession="br_s_stale_legacy",
            enabled=True,
        )
        setup_wizard.write_setup_state(conn, st)

    base_vault = _MemoryVault()
    put_entered = threading.Event()
    release_put = threading.Event()

    class _SlowOnLegacyVault:
        def available(self) -> bool:
            return base_vault.available()

        def put(self, secret: str, ref: str | None = None) -> str:
            if "stale_legacy" in secret:
                put_entered.set()
                release_put.wait(timeout=2.0)
            return base_vault.put(secret, ref)

        def get(self, ref: str) -> str:
            return base_vault.get(ref)

        def delete(self, ref: str) -> None:
            base_vault.delete(ref)

    slow_vault = _SlowOnLegacyVault()

    def _run_migration():
        sync.migrate_legacy_spoke_secrets(db_path, vault=slow_vault)

    t_mig = threading.Thread(target=_run_migration)
    t_mig.start()
    assert put_entered.wait(timeout=2.0)

    publish_done = threading.Event()

    def _run_publish():
        def _apply(_conn, state):
            sc = dict(state.get("spokeConfig") or {})
            sc["enabled"] = True
            sc["hubConfigured"] = True
            sc["rotationRequired"] = False
            sc["ownerSessionRotationRequired"] = False
            state["spokeConfig"] = sc

        publish_spoke_credentials(
            db_path,
            owner_session="br_s_fresh_winner",
            machine_token="br_m_fresh_winner",
            vault=slow_vault,
            apply_state=_apply,
        )
        publish_done.set()

    t_pub = threading.Thread(target=_run_publish)
    t_pub.start()
    time.sleep(0.05)
    # Concurrent publisher must be serialized behind db_secret_lock
    assert not publish_done.is_set()
    release_put.set()
    t_mig.join(timeout=3.0)
    t_pub.join(timeout=3.0)
    assert publish_done.is_set()

    assert base_vault.get(MACHINE_TOKEN_VAULT_REF) == "br_m_fresh_winner"
    assert base_vault.get(OWNER_SESSION_VAULT_REF) == "br_s_fresh_winner"


# ---------------------------------------------------------------------------
# S7: 404 hub response never bypasses the fixed error catalogue
# ---------------------------------------------------------------------------


def test_s7_hostile_404_error_detail_never_reaches_bridge_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hostile_app = FastAPI()

    @hostile_app.get("/api/hub/info")
    def _hub_info():
        return {"ok": True, "protocolVersion": 2, "product": "burnrate"}

    @hostile_app.post("/api/account/login")
    def _hostile_login():
        return JSONResponse(
            {"error": r"sqlite C:\Users\victim\secret.db br_m_leak123"},
            status_code=404,
        )

    @hostile_app.get("/api/machines")
    def _hostile_machines():
        return JSONResponse(
            {"error": r"internal path /var/lib/burnrate/secret.db br_s_leak456"},
            status_code=404,
        )

    hostile_client = TestClient(hostile_app, base_url=HUB_URL)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hostile_client)

    spoke_settings = _make_settings(tmp_path / "s7_spoke.db")
    initialize(spoke_settings.database_path)
    vault = _MemoryVault()
    vault.put("br_s_valid_local", OWNER_SESSION_VAULT_REF)

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17363),
        preferences=tmp_path / "s7_prefs.json",
        vault=vault,
    )

    with hostile_client:
        res_login = bridge.sign_in("owner@example.com", PASSWORD, "TestPC")
        assert res_login["ok"] is False
        assert res_login["code"] == "HUB-501"
        assert res_login["message"] == error_message("HUB-501")
        for forbidden in ("secret.db", "victim", "br_m_leak123"):
            assert forbidden not in json.dumps(res_login)

        res_list = bridge.list_computers()
        assert res_list["ok"] is False
        assert res_list["code"] == "HUB-501"
        assert res_list["message"] == error_message("HUB-501")
        for forbidden in ("secret.db", "/var/lib", "br_s_leak456"):
            assert forbidden not in json.dumps(res_list)


# ---------------------------------------------------------------------------
# Round 2 · Residual S4: durable & retryable cleanup marker and WAL/VACUUM
# ---------------------------------------------------------------------------


def test_s4_busy_checkpoint_keeps_cleanup_marker_and_retries_cleanly(
    tmp_path: Path,
) -> None:
    db_path = tmp_path / "s4_busy.db"
    initialize(db_path)
    vault = _MemoryVault()

    legacy_tok = "br_m_busy_legacy_token_12345"
    legacy_sess = "br_s_busy_legacy_session_67890"
    with connect(db_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            **dict(state.get("spokeConfig") or {}),
            "enabled": True,
            "hubConfigured": True,
            "hubUrl": HUB_URL,
            "token": legacy_tok,
            "ownerSession": legacy_sess,
        }
        setup_wizard.write_setup_state(conn, state)

    # Pin the WAL with an active reader snapshot so wal_checkpoint(TRUNCATE) reports busy=1.
    reader = sqlite3.connect(db_path, isolation_level=None)
    try:
        reader.execute("BEGIN")
        reader.execute("SELECT key FROM app_meta LIMIT 1").fetchall()

        outcome = sync.migrate_legacy_spoke_secrets(db_path, vault=vault)
        assert outcome == {"token": True, "ownerSession": True}
        assert vault.get(MACHINE_TOKEN_VAULT_REF) == legacy_tok
        assert vault.get(OWNER_SESSION_VAULT_REF) == legacy_sess

        with connect(db_path) as conn:
            raw_row = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            sc = (json.loads(raw_row[0]).get("spokeConfig") or {}) if raw_row else {}
            assert "token" not in sc
            assert "ownerSession" not in sc
            marker = conn.execute(
                "SELECT value FROM app_meta WHERE key=?",
                (connection_secrets.CLEANUP_PENDING_META_KEY,),
            ).fetchone()
            assert marker is not None
    finally:
        reader.execute("COMMIT")
        reader.close()

    # Subsequent migration retries sanitation even though JSON secrets are already removed.
    outcome2 = sync.migrate_legacy_spoke_secrets(db_path, vault=vault)
    assert outcome2 == {"token": False, "ownerSession": False}

    with connect(db_path) as conn:
        marker_after = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (connection_secrets.CLEANUP_PENDING_META_KEY,),
        ).fetchone()
        assert marker_after is None

    raw_bytes = db_path.read_bytes()
    wal_path = Path(f"{db_path}-wal")
    if wal_path.exists():
        raw_bytes += wal_path.read_bytes()
    assert legacy_tok.encode("utf-8") not in raw_bytes
    assert legacy_sess.encode("utf-8") not in raw_bytes


def test_s4_vacuum_failure_logs_redacted_warning_preserves_marker_and_retries(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    db_path = tmp_path / "s4_vacuum_fail.db"
    initialize(db_path)
    vault = _MemoryVault()

    legacy_tok = "br_m_vac_legacy_token_abcde"
    legacy_sess = "br_s_vac_legacy_session_fghij"
    secret_hub = "https://secret-hub.example.com"
    with connect(db_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            **dict(state.get("spokeConfig") or {}),
            "enabled": True,
            "hubConfigured": True,
            "hubUrl": secret_hub,
            "token": legacy_tok,
            "ownerSession": legacy_sess,
        }
        setup_wizard.write_setup_state(conn, state)

    real_connect = sync.connect

    class _FailVacuumConn:
        def __init__(self, inner) -> None:
            self._inner = inner
            self._conn = None

        def __enter__(self):
            self._conn = self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def execute(self, sql, *args, **kwargs):
            if str(sql).strip().upper() == "VACUUM":
                raise sqlite3.OperationalError(
                    f"disk full while compacting {secret_hub} with {legacy_tok}"
                )
            return self._conn.execute(sql, *args, **kwargs)

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(sync, "connect", lambda path: _FailVacuumConn(real_connect(path)))

    with caplog.at_level(logging.WARNING, logger="spend_app.sync"):
        outcome = sync.migrate_legacy_spoke_secrets(db_path, vault=vault)
    assert outcome == {"token": True, "ownerSession": True}
    assert any("cleanup marker" in rec.message for rec in caplog.records)
    assert secret_hub not in caplog.text
    assert legacy_tok not in caplog.text
    assert legacy_sess not in caplog.text

    with real_connect(db_path) as conn:
        marker = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (connection_secrets.CLEANUP_PENDING_META_KEY,),
        ).fetchone()
        assert marker is not None

    # Restore normal connect and verify retry clears the marker cleanly.
    monkeypatch.setattr(sync, "connect", real_connect)
    sync.migrate_legacy_spoke_secrets(db_path, vault=vault)
    with real_connect(db_path) as conn:
        marker_after = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (connection_secrets.CLEANUP_PENDING_META_KEY,),
        ).fetchone()
        assert marker_after is None


# ---------------------------------------------------------------------------
# Round 2 · S8: lifecycle fencing for owner rotation, clearing & fresh login
# ---------------------------------------------------------------------------


def test_s8_delayed_rotation_success_after_sign_out_revokes_new_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    login = hub_client.post(
        "/api/account/login",
        headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={"username": "owner@example.com", "password": PASSWORD, "sessionOnly": True},
    )
    assert login.status_code == 200
    old_session = auth_model.extract_cookie(login) or ""
    assert old_session.startswith("br_s_")
    hub_client.cookies.clear()

    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    publish_spoke_credentials(
        db_path,
        owner_session=old_session,
        vault=vault,
        apply_state=lambda _c, st: st["spokeConfig"].update(
            {"hubConfigured": True, "ownerSessionRotationRequired": True}
        ),
    )

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17364),
        preferences=tmp_path / "s8_rot_prefs.json",
        vault=vault,
    )

    real_post = sync._hub_post_with_cookie_isolation
    rotated_on_hub = threading.Event()
    allow_return = threading.Event()
    issued_tokens: list[str] = []

    def _delayed_rotate(hub_url, path, headers, *, json_body=None, client=None, timeout=8.0):
        res = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
        if path == "/api/auth/session/rotate":
            tok = auth_model.extract_cookie(res) or ""
            if tok:
                issued_tokens.append(tok)
            rotated_on_hub.set()
            assert allow_return.wait(timeout=3.0)
        return res

    monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _delayed_rotate)

    rot_error: list[str] = []

    def _run_rotation():
        try:
            sync.ensure_fresh_owner_session(
                db_path, HUB_URL, old_session, vault=vault, client=hub_client
            )
        except CodedError as exc:
            rot_error.append(exc.code)

    t_rot = threading.Thread(target=_run_rotation)
    t_rot.start()
    assert rotated_on_hub.wait(timeout=3.0)

    # Owner signs out while the delayed rotation response is still in flight.
    signout_res = bridge.sign_out()
    assert signout_res["ok"] is True
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None

    allow_return.set()
    t_rot.join(timeout=3.0)

    assert rot_error == ["AUTH-107"]
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
    assert bridge._owner_session_status()["hasOwnerSession"] is False
    assert len(issued_tokens) == 1
    probe_new = hub_client.get(
        "/api/machines",
        headers={
            "Authorization": f"Bearer {issued_tokens[0]}",
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        },
    )
    assert probe_new.status_code == 401


def test_s8_stale_rotation_rejection_does_not_delete_newer_sign_in_credential(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    old_session = "br_s_stale_legacy_session"
    publish_spoke_credentials(
        db_path,
        owner_session=old_session,
        vault=vault,
        apply_state=lambda _c, st: st["spokeConfig"].update(
            {"hubConfigured": True, "ownerSessionRotationRequired": True}
        ),
    )

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17365),
        preferences=tmp_path / "s8_rej_prefs.json",
        vault=vault,
    )

    rotate_started = threading.Event()
    allow_reject = threading.Event()

    class _Fake401:
        status_code = 401
        content = b'{"code":"AUTH-107","error":"Unauthorized"}'
        headers: dict[str, str] = {}
        cookies: dict[str, str] = {}

        def json(self):
            return {"code": "AUTH-107", "error": "Unauthorized"}

    def _delayed_reject(hub_url, path, headers, *, client=None, timeout=8.0):
        if path == "/api/auth/session/rotate":
            rotate_started.set()
            assert allow_reject.wait(timeout=3.0)
            return _Fake401()
        raise AssertionError(f"Unexpected call {path}")

    monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _delayed_reject)

    rot_error: list[str] = []

    def _run_rotation():
        try:
            sync.ensure_fresh_owner_session(
                db_path, HUB_URL, old_session, vault=vault, client=hub_client
            )
        except CodedError as exc:
            rot_error.append(exc.code)

    t_rot = threading.Thread(target=_run_rotation)
    t_rot.start()
    assert rotate_started.wait(timeout=3.0)

    # Fresh sign-in replaces the legacy session before the stale rejection returns.
    signin = bridge.sign_in("owner@example.com", PASSWORD, "WinnerPC")
    assert signin["ok"] is True
    winner_tok = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault)
    assert winner_tok and winner_tok != old_session

    allow_reject.set()
    t_rot.join(timeout=3.0)

    assert rot_error == ["AUTH-107"]
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) == winner_tok
    assert bridge._owner_session_status()["hasOwnerSession"] is True
    with connect(db_path) as conn:
        sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert sc.get("ownerSessionRotationRequired") is False


def test_s8_cross_process_clear_and_publish_stay_consistent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "s8_cross_proc.db"
    initialize(db_path)
    vault = _MemoryVault()

    # Disable in-process threading lock so SQLite BEGIN IMMEDIATE carries serialization across
    # simulated separate processes.
    class _NoOpLock:
        def acquire(self): pass
        def release(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(connection_secrets, "db_secret_lock", lambda _p: _NoOpLock())

    publish_spoke_credentials(
        db_path,
        owner_session="br_s_initial_cross",
        vault=vault,
        apply_state=lambda _c, st: st["spokeConfig"].update({"ownerSessionRotationRequired": False}),
    )

    # Case 1: publish holds BEGIN IMMEDIATE while clear arrives concurrently.
    pub_entered = threading.Event()
    allow_pub_commit = threading.Event()

    def _slow_apply(_conn, st):
        st["spokeConfig"]["ownerSessionRotationRequired"] = False
        pub_entered.set()
        assert allow_pub_commit.wait(timeout=3.0)

    t_pub = threading.Thread(
        target=publish_spoke_credentials,
        args=(db_path,),
        kwargs={"owner_session": "br_s_mid_winner", "vault": vault, "apply_state": _slow_apply},
    )
    t_pub.start()
    assert pub_entered.wait(timeout=3.0)

    t_clr = threading.Thread(
        target=clear_spoke_credentials,
        args=(db_path,),
        kwargs={"clear_owner_session": True, "vault": vault},
    )
    t_clr.start()
    time.sleep(0.05)
    # Clear must NOT delete the vault entry before acquiring BEGIN IMMEDIATE.
    assert vault.get(OWNER_SESSION_VAULT_REF) == "br_s_mid_winner"

    allow_pub_commit.set()
    t_pub.join(timeout=3.0)
    t_clr.join(timeout=3.0)

    # Clear ran after publish committed, so BOTH vault and DB metadata are cleared.
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
    with connect(db_path) as conn:
        sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert "ownerSession" not in sc
        gen_after_clear = int(sc.get("credentialGeneration") or 0)

    # Case 2: clear holds BEGIN IMMEDIATE while publish arrives concurrently.
    clr_entered = threading.Event()
    allow_clr_commit = threading.Event()

    def _slow_clear_prepare(_conn, _st):
        clr_entered.set()
        assert allow_clr_commit.wait(timeout=3.0)

    t_clr2 = threading.Thread(
        target=clear_spoke_credentials,
        args=(db_path,),
        kwargs={
            "clear_owner_session": True,
            "vault": vault,
            "prepare_state": _slow_clear_prepare,
        },
    )
    t_clr2.start()
    assert clr_entered.wait(timeout=3.0)

    t_pub2 = threading.Thread(
        target=publish_spoke_credentials,
        args=(db_path,),
        kwargs={
            "owner_session": "br_s_final_winner",
            "vault": vault,
            "apply_state": lambda _c, st: st["spokeConfig"].update(
                {"ownerSessionRotationRequired": False}
            ),
        },
    )
    t_pub2.start()
    time.sleep(0.05)
    # Publish is blocked behind clear's SQLite lock.
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None

    allow_clr_commit.set()
    t_clr2.join(timeout=3.0)
    t_pub2.join(timeout=3.0)

    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) == "br_s_final_winner"
    with connect(db_path) as conn:
        sc2 = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert int(sc2.get("credentialGeneration") or 0) == gen_after_clear + 2
        assert sc2.get("ownerSessionRotationRequired") is False


def test_s8_fresh_sign_in_uses_supplied_session_even_when_revoked_legacy_stored(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()

    # Seed an invalid/revoked legacy session with ownerSessionRotationRequired=True.
    publish_spoke_credentials(
        db_path,
        owner_session="br_s_revoked_legacy_token",
        vault=vault,
        apply_state=lambda _c, st: st["spokeConfig"].update(
            {"hubConfigured": True, "ownerSessionRotationRequired": True}
        ),
    )

    # Distinct fresh token passed directly to ensure_fresh_owner_session must pass through untouched.
    assert (
        sync.ensure_fresh_owner_session(
            db_path,
            HUB_URL,
            "br_s_distinct_fresh_login",
            vault=vault,
            client=hub_client,
        )
        == "br_s_distinct_fresh_login"
    )

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17366),
        preferences=tmp_path / "s8_fresh_prefs.json",
        vault=vault,
    )
    res = bridge.sign_in("owner@example.com", PASSWORD, "FreshReconnectPC")
    assert res["ok"] is True, res
    stored_sess = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault)
    stored_mach = read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault)
    assert stored_sess and stored_sess != "br_s_revoked_legacy_token"
    assert stored_mach and stored_mach.startswith("br_m_")
    with connect(db_path) as conn:
        sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert sc.get("ownerSessionRotationRequired") is False


# ---------------------------------------------------------------------------
# Round 2 · B3: partial publication migrates or preserves untouched legacy
# ---------------------------------------------------------------------------


def test_b3_partial_publish_migrates_untouched_legacy_and_preserves_on_failure(
    tmp_path: Path,
) -> None:
    # 1) Legacy machine-only upgrade: publishing owner_session migrates DB token cleanly.
    db_m = tmp_path / "b3_machine_legacy.db"
    initialize(db_m)
    with connect(db_m) as conn:
        st = setup_wizard.read_setup_state(conn)
        st["role"] = "spoke"
        st["spokeConfig"] = {**dict(st.get("spokeConfig") or {}), "enabled": True, "token": "br_m_legacy_keep"}
        setup_wizard.write_setup_state(conn, st)

    v1 = _MemoryVault()
    publish_spoke_credentials(db_m, owner_session="br_s_new_owner", vault=v1)
    assert v1.get(OWNER_SESSION_VAULT_REF) == "br_s_new_owner"
    assert v1.get(MACHINE_TOKEN_VAULT_REF) == "br_m_legacy_keep"
    with connect(db_m) as conn:
        raw_row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()
        sc = (json.loads(raw_row[0]).get("spokeConfig") or {}) if raw_row else {}
        assert "token" not in sc
        assert "ownerSession" not in sc
        assert sc.get("rotationRequired") is True

    # 2) Legacy owner-only upgrade: publishing machine_token migrates DB ownerSession cleanly.
    db_o = tmp_path / "b3_owner_legacy.db"
    initialize(db_o)
    with connect(db_o) as conn:
        st = setup_wizard.read_setup_state(conn)
        st["role"] = "spoke"
        st["spokeConfig"] = {
            **dict(st.get("spokeConfig") or {}),
            "enabled": True,
            "ownerSession": "br_s_legacy_keep",
        }
        setup_wizard.write_setup_state(conn, st)

    v2 = _MemoryVault()
    publish_spoke_credentials(db_o, machine_token="br_m_new_machine", vault=v2)
    assert v2.get(MACHINE_TOKEN_VAULT_REF) == "br_m_new_machine"
    assert v2.get(OWNER_SESSION_VAULT_REF) == "br_s_legacy_keep"
    with connect(db_o) as conn:
        raw_row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()
        sc = (json.loads(raw_row[0]).get("spokeConfig") or {}) if raw_row else {}
        assert "token" not in sc
        assert "ownerSession" not in sc
        assert sc.get("ownerSessionRotationRequired") is True

    # 3) Counterpart migration failure must preserve the unmigrated DB secret rather than dropping it.
    db_fail = tmp_path / "b3_counterpart_fail.db"
    initialize(db_fail)
    with connect(db_fail) as conn:
        st = setup_wizard.read_setup_state(conn)
        st["role"] = "spoke"
        st["spokeConfig"] = {
            **dict(st.get("spokeConfig") or {}),
            "enabled": True,
            "token": "br_m_must_not_be_lost",
        }
        setup_wizard.write_setup_state(conn, st)

    class _FailMachineRefVault(_MemoryVault):
        def put(self, secret: str, ref: str | None = None) -> str:
            if ref == MACHINE_TOKEN_VAULT_REF:
                raise LocationError("Keychain write refused for machine ref")
            return super().put(secret, ref)

    v3 = _FailMachineRefVault()
    publish_spoke_credentials(db_fail, owner_session="br_s_owner_ok", vault=v3)
    assert v3.get(OWNER_SESSION_VAULT_REF) == "br_s_owner_ok"
    with connect(db_fail) as conn:
        sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert sc.get("token") == "br_m_must_not_be_lost"


# ---------------------------------------------------------------------------
# Round 2 · B4: SQLite commit failure restores prior vault refs & state
# ---------------------------------------------------------------------------


def test_b4_sqlite_commit_failure_restores_prior_vault_refs_and_preserves_metadata(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "b4_commit_fail.db"
    initialize(db_path)
    vault = _MemoryVault()

    publish_spoke_credentials(
        db_path,
        owner_session="br_s_prior_owner",
        machine_token="br_m_prior_machine",
        vault=vault,
        apply_state=lambda _c, st: st.update({"computerName": "PriorPC"}),
    )
    with connect(db_path) as conn:
        st_before = setup_wizard.read_setup_state(conn)
        gen_before = int((st_before.get("spokeConfig") or {}).get("credentialGeneration") or 0)
        assert st_before.get("computerName") == "PriorPC"

    real_connect = db_mod.connect

    class _FailCommitConn:
        def __init__(self, inner) -> None:
            self._inner = inner
            self._conn = None

        def __enter__(self):
            self._conn = self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def commit(self):
            raise sqlite3.OperationalError("simulated disk I/O error on commit")

        def __getattr__(self, name):
            return getattr(self._conn, name)

    monkeypatch.setattr(db_mod, "connect", lambda path: _FailCommitConn(real_connect(path)))

    with pytest.raises(sqlite3.OperationalError, match="disk I/O error"):
        publish_spoke_credentials(
            db_path,
            owner_session="br_s_new_rolled_back",
            machine_token="br_m_new_rolled_back",
            vault=vault,
            apply_state=lambda _c, st: st.update({"computerName": "UpdatedPC"}),
        )

    assert vault.get(OWNER_SESSION_VAULT_REF) == "br_s_prior_owner"
    assert vault.get(MACHINE_TOKEN_VAULT_REF) == "br_m_prior_machine"

    monkeypatch.setattr(db_mod, "connect", real_connect)
    with real_connect(db_path) as conn:
        st_after = setup_wizard.read_setup_state(conn)
        assert st_after.get("computerName") == "PriorPC"
        assert int((st_after.get("spokeConfig") or {}).get("credentialGeneration") or 0) == gen_before

    # Also verify empty prior vault refs are deleted again on commit failure.
    db_empty = tmp_path / "b4_empty_prior.db"
    initialize(db_empty)
    empty_vault = _MemoryVault()
    monkeypatch.setattr(db_mod, "connect", lambda path: _FailCommitConn(real_connect(path)))
    with pytest.raises(sqlite3.OperationalError):
        publish_spoke_credentials(
            db_empty,
            owner_session="br_s_temp",
            machine_token="br_m_temp",
            vault=empty_vault,
        )
    assert read_vault_secret(db_empty, OWNER_SESSION_VAULT_REF, vault=empty_vault) is None
    assert read_vault_secret(db_empty, MACHINE_TOKEN_VAULT_REF, vault=empty_vault) is None



# ---------------------------------------------------------------------------
# Round 3 ? S8 residual: Clear-first sign-out & vault-only pending revoke
# ---------------------------------------------------------------------------


def test_s8_r3_rotation_publishes_before_clear_then_sign_out_revokes_new_token(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import PENDING_REVOKE_VAULT_REF

    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17380),
        preferences=tmp_path / "s8_r3_before_clear.json",
        vault=vault,
    )

    with hub_client:
        res_in = bridge.sign_in("owner@example.com", PASSWORD, "R3PC1")
        assert res_in["ok"] is True
        _wait_for_first_sync(bridge)
        old_tok = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) or ""
        assert old_tok.startswith("br_s_")

        # Mark rotation required as if legacy session/expiry triggered auto-rotation
        with connect(db_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            st["spokeConfig"]["ownerSessionRotationRequired"] = True
            setup_wizard.write_setup_state(conn, st)

        # Pause sign_out right before clear_spoke_credentials so rotation publishes N first
        signout_ready = threading.Event()
        rotation_done = threading.Event()
        real_clear = connection_secrets.clear_spoke_credentials

        def _paused_clear(*args, **kwargs):
            signout_ready.set()
            assert rotation_done.wait(timeout=4.0)
            return real_clear(*args, **kwargs)

        monkeypatch.setattr(connection_secrets, "clear_spoke_credentials", _paused_clear)

        out_box: list[dict] = []
        t_so = threading.Thread(target=lambda: out_box.append(bridge.sign_out()))
        t_so.start()
        assert signout_ready.wait(timeout=3.0)

        new_tok = sync.ensure_fresh_owner_session(
            db_path, HUB_URL, old_tok, vault=vault, client=hub_client
        )
        assert new_tok != old_tok
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) == new_tok
        rotation_done.set()
        t_so.join(timeout=4.0)

        assert out_box == [{"ok": True}]
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, PENDING_REVOKE_VAULT_REF, vault=vault) is None
        assert bridge._owner_session_status()["hasOwnerSession"] is False
        with connect(db_path) as conn:
            sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert sc.get("signedOut") is True
        assert sc.get("enabled") is False

        for tok in (old_tok, new_tok):
            probe_res = hub_client.get(
                "/api/machines",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "X-BURNRATE-Request": "1",
                    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
                },
            )
            assert probe_res.status_code == 401


def test_s8_r3_rotation_publishes_after_sign_out_clear_is_revoked(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import PENDING_REVOKE_VAULT_REF

    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17381),
        preferences=tmp_path / "s8_r3_after_clear.json",
        vault=vault,
    )

    with hub_client:
        res_in = bridge.sign_in("owner@example.com", PASSWORD, "R3PC2")
        assert res_in["ok"] is True
        _wait_for_first_sync(bridge)
        old_tok = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) or ""

        with connect(db_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            st["spokeConfig"]["ownerSessionRotationRequired"] = True
            setup_wizard.write_setup_state(conn, st)

        rot_issued = threading.Event()
        allow_publish = threading.Event()
        issued_tokens: list[str] = []
        real_pub = connection_secrets.publish_spoke_credentials

        def _paused_publish(db_p, *, owner_session=None, **kwargs):
            if owner_session:
                issued_tokens.append(owner_session)
                rot_issued.set()
                assert allow_publish.wait(timeout=4.0)
            return real_pub(db_p, owner_session=owner_session, **kwargs)

        monkeypatch.setattr(connection_secrets, "publish_spoke_credentials", _paused_publish)

        rot_box: list[str] = []

        def _run_rot():
            try:
                sync.ensure_fresh_owner_session(
                    db_path, HUB_URL, old_tok, vault=vault, client=hub_client
                )
            except CodedError as exc:
                rot_box.append(exc.code)

        t_rot = threading.Thread(target=_run_rot)
        t_rot.start()
        assert rot_issued.wait(timeout=3.0)

        # Sign out clears locally first (sets signedOut & bumps generation), then logs out old_tok
        so_res = bridge.sign_out()
        assert so_res == {"ok": True}
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None

        allow_publish.set()
        t_rot.join(timeout=4.0)

        assert rot_box == ["AUTH-107"]
        assert len(issued_tokens) == 1
        new_tok = issued_tokens[0]
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, PENDING_REVOKE_VAULT_REF, vault=vault) is None
        assert bridge._owner_session_status()["hasOwnerSession"] is False
        with connect(db_path) as conn:
            sc = setup_wizard.read_setup_state(conn).get("spokeConfig") or {}
        assert sc.get("signedOut") is True

        for tok in (old_tok, new_tok):
            probe_res = hub_client.get(
                "/api/machines",
                headers={
                    "Authorization": f"Bearer {tok}",
                    "X-BURNRATE-Request": "1",
                    "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
                },
            )
            assert probe_res.status_code == 401


def test_s8_r3_offline_revoke_queues_in_vault_only_and_flushes_on_next_hub_contact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from spend_app.connection_secrets import PENDING_REVOKE_VAULT_REF, read_pending_revokes

    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    prev_factory = set_vault_factory(lambda _db: vault)
    try:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17382),
            preferences=tmp_path / "s8_r3_offline.json",
            vault=vault,
        )
        with hub_client:
            # 1) owner-only clear + offline revoke leaves machine token intact; next sync_once flushes pending revoke
            res_in = bridge.sign_in("owner@example.com", PASSWORD, "R3OfflinePC")
            assert res_in["ok"] is True
            _wait_for_first_sync(bridge)
            tok1 = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) or ""
            mtok1 = read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert tok1 and mtok1

            sync._HUB_TEST_CLIENTS.clear()

            class _OfflineClient:
                def __init__(self, *_a, **_kw):
                    pass

                def __enter__(self):
                    return self

                def __exit__(self, *_a):
                    return False

                def post(self, *_a, **_kw):
                    raise httpx.ConnectError("offline")

            monkeypatch.setattr(httpx, "Client", _OfflineClient)
            bridge._clear_owner_session()
            connection_secrets.revoke_or_queue_owner_session(db_path, HUB_URL, tok1, vault=vault)
            assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
            assert bridge._owner_session_status()["hasOwnerSession"] is False

            pending = read_pending_revokes(db_path, vault=vault)
            assert any(item["token"] == tok1 for item in pending)
            with connect(db_path) as conn:
                rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
            joined = " ".join(f"{k}:{v}" for k, v in rows)
            assert tok1 not in joined
            assert "pending-revoke" not in joined.lower()
            assert "pendingRevoke" not in joined

            # Restore hub reachability; next hub contact via sync_once flushes pending revoke
            monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
            probe_before = hub_client.get(
                "/api/machines",
                headers={"Authorization": f"Bearer {tok1}", "X-BURNRATE-Request": "1"},
            )
            assert probe_before.status_code == 200

            sync.sync_once(
                spoke_settings,
                PricingEngine.load(spoke_settings.pricing_path),
                hub_url=HUB_URL,
                token=mtok1,
                machine_name="R3OfflinePC",
                collect_local=False,
                client=hub_client,
            )
            assert PENDING_REVOKE_VAULT_REF not in vault.items
            probe_after = hub_client.get(
                "/api/machines",
                headers={"Authorization": f"Bearer {tok1}", "X-BURNRATE-Request": "1"},
            )
            assert probe_after.status_code == 401

            # 2) Full sign_out offline clears both vault refs, queues machine+owner, and flushes on next bridge hub call
            res_in2 = bridge.sign_in("owner@example.com", PASSWORD, "R3OfflinePC")
            assert res_in2["ok"] is True
            _wait_for_first_sync(bridge)
            tok2 = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) or ""
            assert tok2

            sync._HUB_TEST_CLIENTS.clear()
            so_full = bridge.sign_out()
            assert so_full["ok"] is False
            assert so_full["code"] == "NET-001"
            assert OWNER_SESSION_VAULT_REF not in vault.items
            assert MACHINE_TOKEN_VAULT_REF not in vault.items
            assert PENDING_REVOKE_VAULT_REF in vault.items
            assert bridge._owner_session_status()["hasOwnerSession"] is False

            with connect(db_path) as conn:
                st = setup_wizard.read_setup_state(conn)
                rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
            assert st["role"] == "local"
            assert st["spokeConfig"]["enabled"] is False
            assert st["spokeConfig"].get("signedOut") is True
            assert tok2 not in " ".join(f"{k}:{v}" for k, v in rows)

            # Next hub contact via sign_in flushes tok2 + machine revoke
            monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
            res_in3 = bridge.sign_in("owner@example.com", PASSWORD, "R3OfflinePC")
            assert res_in3["ok"] is True
            _wait_for_first_sync(bridge)
            assert PENDING_REVOKE_VAULT_REF not in vault.items
            probe_tok2 = hub_client.get(
                "/api/machines",
                headers={"Authorization": f"Bearer {tok2}", "X-BURNRATE-Request": "1"},
            )
            assert probe_tok2.status_code == 401
    finally:
        set_vault_factory(prev_factory)


# ---------------------------------------------------------------------------
# Round 3 ? B4 residual: Fenced compensation when another process wins
# ---------------------------------------------------------------------------


def test_b4_r3_commit_failure_does_not_clobber_concurrent_winner_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "b4_r3_concurrent.db"
    initialize(db_path)
    vault = _MemoryVault()

    class _NoOpLock:
        def acquire(self): pass
        def release(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(connection_secrets, "db_secret_lock", lambda _p: _NoOpLock())

    publish_spoke_credentials(
        db_path,
        owner_session="br_s_initial",
        machine_token="br_m_initial",
        vault=vault,
    )

    real_connect = db_mod.connect
    race_done = False

    class _RaceOnCommitConn:
        def __init__(self, inner) -> None:
            self._inner = inner
            self._conn = None
            self._is_publish_tx = False

        def __enter__(self):
            self._conn = self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def execute(self, sql, *args, **kwargs):
            if "BEGIN IMMEDIATE" in str(sql):
                self._is_publish_tx = True
            return self._conn.execute(sql, *args, **kwargs)

        def commit(self):
            nonlocal race_done
            if self._is_publish_tx and not race_done:
                race_done = True
                # Release A's SQLite write lock (simulating A's commit aborting) so B can win
                # in the gap before A runs _compensate_publish_failure.
                self._conn.rollback()
                monkeypatch.setattr(db_mod, "connect", real_connect)
                publish_spoke_credentials(
                    db_path,
                    owner_session="br_s_winner_b",
                    machine_token="br_m_winner_b",
                    vault=vault,
                    apply_state=lambda _c, st: st.update({"computerName": "WinnerB"}),
                )
                raise sqlite3.OperationalError("process A commit failed after B won")
            return self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _connect_race(path):
        return _RaceOnCommitConn(real_connect(path))

    monkeypatch.setattr(db_mod, "connect", _connect_race)

    with pytest.raises(sqlite3.OperationalError, match="process A commit failed"):
        publish_spoke_credentials(
            db_path,
            owner_session="br_s_loser_a",
            machine_token="br_m_loser_a",
            vault=vault,
        )

    assert vault.get(OWNER_SESSION_VAULT_REF) == "br_s_winner_b"
    assert vault.get(MACHINE_TOKEN_VAULT_REF) == "br_m_winner_b"
    with real_connect(db_path) as conn:
        st = setup_wizard.read_setup_state(conn)
        assert st.get("computerName") == "WinnerB"

    # Case 2: Process B wins only on owner_session while machine_token was empty beforehand;
    # A's compensation must keep B's owner_session AND delete A's uncommitted machine_token.
    db_partial = tmp_path / "b4_r3_partial.db"
    initialize(db_partial)
    v_partial = _MemoryVault()
    publish_spoke_credentials(db_partial, owner_session="br_s_old", vault=v_partial)

    race_done_2 = False

    class _PartialRaceConn:
        def __init__(self, inner) -> None:
            self._inner = inner
            self._conn = None
            self._is_publish_tx = False

        def __enter__(self):
            self._conn = self._inner.__enter__()
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._inner.__exit__(exc_type, exc, tb)

        def execute(self, sql, *args, **kwargs):
            if "BEGIN IMMEDIATE" in str(sql):
                self._is_publish_tx = True
            return self._conn.execute(sql, *args, **kwargs)

        def commit(self):
            nonlocal race_done_2
            if self._is_publish_tx and not race_done_2:
                race_done_2 = True
                self._conn.rollback()
                monkeypatch.setattr(db_mod, "connect", real_connect)
                publish_spoke_credentials(
                    db_partial,
                    owner_session="br_s_partial_b",
                    vault=v_partial,
                )
                raise sqlite3.OperationalError("process A partial commit failed")
            return self._conn.commit()

        def __getattr__(self, name):
            return getattr(self._conn, name)

    def _connect_partial(path):
        return _PartialRaceConn(real_connect(path))

    monkeypatch.setattr(db_mod, "connect", _connect_partial)
    with pytest.raises(sqlite3.OperationalError, match="partial commit failed"):
        publish_spoke_credentials(
            db_partial,
            owner_session="br_s_orphan_a",
            machine_token="br_m_orphan_a",
            vault=v_partial,
        )

    assert v_partial.get(OWNER_SESSION_VAULT_REF) == "br_s_partial_b"
    assert read_vault_secret(db_partial, MACHINE_TOKEN_VAULT_REF, vault=v_partial) is None


# ---------------------------------------------------------------------------
# Round 4 ? S9, S10, S11, B5: Pending-revoke terminal rules, cross-process
# atomicity, machine self-revoke & full sign-out, bounded queue & diagnostics
# ---------------------------------------------------------------------------


def test_s9_r4_rejected_and_redirected_revokes_stay_queued_per_operation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import (
        enqueue_pending_revokes,
        flush_pending_revokes,
        is_terminal_revoke_response,
        read_pending_revokes,
    )

    # 1) Exhaustive terminal vs non-terminal classifier checks
    for status in (301, 302, 307, 308, 400, 403, 429, 500, 503):
        assert is_terminal_revoke_response("owner_logout", status, {"error": {"code": "AUTH-107"}}) is False
        assert is_terminal_revoke_response("machine_revoke_owner", status, {"error": {"code": "DEV-301"}}) is False
        assert is_terminal_revoke_response("machine_self_revoke", status, {"error": {"code": "DEV-301"}}) is False

    assert is_terminal_revoke_response("owner_logout", 200) is True
    assert is_terminal_revoke_response("owner_logout", 401) is True
    assert is_terminal_revoke_response("owner_logout", 404) is False

    # Owner token 401/403 does NOT prove target machine was revoked
    assert is_terminal_revoke_response("machine_revoke_owner", 401, {"error": {"code": "AUTH-107"}}) is False
    assert is_terminal_revoke_response("machine_revoke_owner", 403, {"error": {"code": "AUTH-107"}}) is False
    assert is_terminal_revoke_response("machine_revoke_owner", 200) is True
    assert is_terminal_revoke_response("machine_revoke_owner", 404) is True
    assert is_terminal_revoke_response("machine_revoke_owner", 410) is True
    assert is_terminal_revoke_response("machine_revoke_owner", 422, {"error": {"code": "DEV-301"}}) is True
    assert is_terminal_revoke_response("machine_revoke_owner", 422, {"error": {"code": "DEV-302"}}) is True

    # Self-revoke 401/404/410 proves that specific machine token is already invalid
    assert is_terminal_revoke_response("machine_self_revoke", 200) is True
    assert is_terminal_revoke_response("machine_self_revoke", 401) is True
    assert is_terminal_revoke_response("machine_self_revoke", 404) is True
    assert is_terminal_revoke_response("machine_self_revoke", 410) is True
    assert is_terminal_revoke_response("machine_self_revoke", 422, {"error": {"code": "DEV-301"}}) is True

    # 2) flush_pending_revokes keeps 302/400/403/429 and 401-on-machine_revoke_owner queued
    # and enforces follow_redirects=False
    db_path = tmp_path / "s9_flush.db"
    initialize(db_path)
    vault = _MemoryVault()
    enqueue_pending_revokes(
        db_path,
        [
            {"kind": "owner_logout", "token": "br_s_302", "hubUrl": HUB_URL},
            {"kind": "owner_logout", "token": "br_s_403", "hubUrl": HUB_URL},
            {"kind": "owner_logout", "token": "br_s_429", "hubUrl": HUB_URL},
            {"kind": "machine_revoke_owner", "token": "br_s_401_owner", "machineId": "machine-abc", "hubUrl": HUB_URL},
            {"kind": "machine_self_revoke", "token": "br_m_403_self", "hubUrl": HUB_URL},
            {"kind": "owner_logout", "token": "br_s_401_term", "hubUrl": HUB_URL},
            {"kind": "machine_self_revoke", "token": "br_m_401_term", "hubUrl": HUB_URL},
        ],
        vault=vault,
    )

    class _FakeRes:
        def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
            self.status_code = status_code
            self._body = body
            self.content = json.dumps(body).encode("utf-8") if body is not None else b""
            self.headers = headers or {}

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    seen_follow_redirects: list[bool] = []

    class _StubClient:
        cookies = None

        def post(self, path, json=None, headers=None, follow_redirects=True):
            seen_follow_redirects.append(follow_redirects)
            auth = (headers or {}).get("Authorization", "")
            tok = auth.removeprefix("Bearer ")
            if tok == "br_s_302":
                return _FakeRes(302, {}, {"Location": f"{HUB_URL}/other"})
            if tok == "br_s_403":
                return _FakeRes(403, {"error": {"code": "AUTH-107"}})
            if tok == "br_s_429":
                return _FakeRes(429, {"error": {"code": "AUTH-102"}}, {"Retry-After": "12"})
            if tok == "br_s_401_owner":
                return _FakeRes(401, {"error": {"code": "AUTH-107"}})
            if tok == "br_m_403_self":
                return _FakeRes(403, {"error": {"code": "NET-002"}})
            if tok == "br_s_401_term":
                return _FakeRes(401, {"error": {"code": "AUTH-107"}})
            if tok == "br_m_401_term":
                return _FakeRes(401, {"error": {"code": "DEV-301"}})
            raise AssertionError(f"Unexpected token {tok}")

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _StubClient())
    ok = flush_pending_revokes(db_path, HUB_URL, vault=vault, client=_StubClient())
    assert ok is False
    assert seen_follow_redirects and all(fr is False for fr in seen_follow_redirects)

    remaining = {e["token"]: e for e in read_pending_revokes(db_path, vault=vault)}
    assert set(remaining.keys()) == {
        "br_s_302",
        "br_s_403",
        "br_s_429",
        "br_s_401_owner",
        "br_m_403_self",
    }
    assert remaining["br_s_429"]["attempts"] == 1
    assert remaining["br_s_429"]["lastError"] == "AUTH-102"
    assert remaining["br_s_429"].get("nextAttemptAt")

    # 3) Stale rotation cleanup in ensure_fresh_owner_session
    # must queue orphaned replacement tokens on 403/429/302 (never discard)
    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path / "stale_sub", monkeypatch
    )
    stale_db = spoke_settings.database_path
    stale_vault = _MemoryVault()
    with hub_client:
        login_res = hub_client.post(
            "/api/account/login",
            headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert login_res.status_code == 200
        old_tok = auth_model.extract_cookie(login_res) or ""
        hub_client.cookies.clear()

        publish_spoke_credentials(
            stale_db,
            owner_session=old_tok,
            vault=stale_vault,
            apply_state=lambda _c, st: st["spokeConfig"].update(
                {"hubConfigured": True, "ownerSessionRotationRequired": True}
            ),
        )
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17401),
            preferences=tmp_path / "s9_stale_prefs.json",
            vault=stale_vault,
        )

        real_post = sync._hub_post_with_cookie_isolation
        rotated_evt = threading.Event()
        release_evt = threading.Event()
        issued_new: list[str] = []

        def _intercept_stale(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            if path == "/api/auth/session/rotate":
                r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
                tok = auth_model.extract_cookie(r) or ""
                if tok:
                    issued_new.append(tok)
                rotated_evt.set()
                assert release_evt.wait(timeout=3.0)
                return r
            if path == "/api/auth/logout" and headers.get("Authorization") == f"Bearer {issued_new[0]}":
                return _FakeRes(403, {"error": {"code": "AUTH-107"}})
            return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _intercept_stale)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)

        errs: list[str] = []

        def _worker():
            try:
                sync.ensure_fresh_owner_session(
                    stale_db, HUB_URL, old_tok, vault=stale_vault, client=hub_client
                )
            except CodedError as exc:
                errs.append(exc.code)

        t = threading.Thread(target=_worker)
        t.start()
        assert rotated_evt.wait(timeout=3.0)
        bridge._clear_owner_session()
        release_evt.set()
        t.join(timeout=3.0)

        assert errs == ["AUTH-107"]
        assert len(issued_new) == 1
        queued = read_pending_revokes(stale_db, vault=stale_vault)
        assert any(q["kind"] == "owner_logout" and q["token"] == issued_new[0] for q in queued)

        # Also verify ensure_fresh_owner_session stale-rotation cleanup queuing on 429
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
        res_relogin = bridge.sign_in("owner@example.com", PASSWORD, "R4BridgeStale")
        assert res_relogin["ok"] is True
        _wait_for_first_sync(bridge)
        tok_429 = read_vault_secret(stale_db, OWNER_SESSION_VAULT_REF, vault=stale_vault) or ""
        with connect(stale_db) as conn:
            st_429 = setup_wizard.read_setup_state(conn)
            st_429["spokeConfig"]["ownerSessionRotationRequired"] = True
            setup_wizard.write_setup_state(conn, st_429)

        b_rot_evt = threading.Event()
        b_rel_evt = threading.Event()
        b_issued: list[str] = []

        def _intercept_429(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            if path == "/api/auth/session/rotate":
                r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
                tok = auth_model.extract_cookie(r) or ""
                if tok:
                    b_issued.append(tok)
                b_rot_evt.set()
                assert b_rel_evt.wait(timeout=3.0)
                return r
            if path == "/api/auth/logout" and b_issued and headers.get("Authorization") == f"Bearer {b_issued[0]}":
                return _FakeRes(429, {"error": {"code": "AUTH-102"}})
            return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _intercept_429)
        b_errs: list[str] = []

        def _b_worker():
            try:
                sync.ensure_fresh_owner_session(
                    stale_db, HUB_URL, tok_429, vault=stale_vault, client=hub_client
                )
            except CodedError as exc:
                b_errs.append(exc.code)

        t_b = threading.Thread(target=_b_worker)
        t_b.start()
        assert b_rot_evt.wait(timeout=3.0)
        bridge._clear_owner_session()
        b_rel_evt.set()
        t_b.join(timeout=3.0)

        assert b_errs == ["AUTH-107"]
        assert len(b_issued) == 1
        queued_b = read_pending_revokes(stale_db, vault=stale_vault)
        assert any(q["kind"] == "owner_logout" and q["token"] == b_issued[0] for q in queued_b)


def test_s10_r4_concurrent_enqueue_and_flush_lose_zero_entries_without_inproc_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import (
        enqueue_pending_revokes,
        flush_pending_revokes,
        read_pending_revokes,
    )

    db_path = tmp_path / "s10_concurrent.db"
    initialize(db_path)
    vault = _MemoryVault()

    class _NoOpLock:
        def acquire(self): pass
        def release(self): pass
        def __enter__(self): return self
        def __exit__(self, *args): return False

    monkeypatch.setattr(connection_secrets, "db_secret_lock", lambda _p: _NoOpLock())

    # 1) Concurrent enqueue vs enqueue with in-proc lock disabled
    def _enq(tok: str):
        enqueue_pending_revokes(
            db_path,
            [{"kind": "owner_logout", "token": tok, "hubUrl": HUB_URL}],
            vault=vault,
        )

    threads = [threading.Thread(target=_enq, args=(f"br_s_conc_{i}",)) for i in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=5.0)

    tokens = {e["token"] for e in read_pending_revokes(db_path, vault=vault)}
    assert tokens == {f"br_s_conc_{i}" for i in range(10)}

    # 2) Enqueue during flush HTTP phase must NOT be lost when flush reconciles
    db_flush = tmp_path / "s10_flush_race.db"
    initialize(db_flush)
    v_flush = _MemoryVault()
    enqueue_pending_revokes(
        db_flush,
        [{"kind": "owner_logout", "token": "br_s_initial_flush", "hubUrl": HUB_URL}],
        vault=v_flush,
    )

    in_http = threading.Event()
    allow_http_finish = threading.Event()

    class _FakeOk:
        status_code = 200
        content = b'{"ok":true}'
        headers: dict[str, str] = {}

        def json(self):
            return {"ok": True}

    def _blocking_post(hub_url, path, headers, *, json_body=None, client=None, timeout=8.0):
        in_http.set()
        assert allow_http_finish.wait(timeout=3.0)
        return _FakeOk()

    monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _blocking_post)

    t_flush = threading.Thread(
        target=flush_pending_revokes,
        args=(db_flush, HUB_URL),
        kwargs={"vault": v_flush},
    )
    t_flush.start()
    assert in_http.wait(timeout=3.0)

    # Concurrent enqueue runs while HTTP is in flight (must NOT block on SQLite lock!)
    enqueue_pending_revokes(
        db_flush,
        [{"kind": "machine_self_revoke", "token": "br_m_added_during_http", "hubUrl": HUB_URL}],
        vault=v_flush,
    )
    allow_http_finish.set()
    t_flush.join(timeout=3.0)

    remaining = [e["token"] for e in read_pending_revokes(db_flush, vault=v_flush)]
    assert remaining == ["br_m_added_during_http"]


def test_s11_r4_hub_self_revoke_and_full_sign_out_revokes_machine_only_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from spend_app.connection_secrets import (
        PENDING_REVOKE_VAULT_REF,
        read_pending_revokes,
        read_vault_secret,
    )

    hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )

    with hub_client:
        # 1) Hub /api/machines/self/revoke security properties
        m1 = sync.register_machine(hub_settings.database_path, name="SelfRevokeM1")
        m2 = sync.register_machine(hub_settings.database_path, name="SelfRevokeM2")
        m1_id, m1_tok = m1["machine"]["id"], m1["token"]
        m2_id, m2_tok = m2["machine"]["id"], m2["token"]

        # Unauthenticated denied
        r_unauth = hub_client.post(
            "/api/machines/self/revoke",
            headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={},
        )
        assert r_unauth.status_code == 401

        # Owner session denied (machine-only endpoint)
        login = hub_client.post(
            "/api/account/login",
            headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert login.status_code == 200
        owner_tok = auth_model.extract_cookie(login) or ""
        hub_client.cookies.clear()
        r_owner = hub_client.post(
            "/api/machines/self/revoke",
            headers={
                "Authorization": f"Bearer {owner_tok}",
                "X-BURNRATE-Request": "1",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
            json={},
        )
        assert r_owner.status_code == 403

        # Machine m1 cannot revoke foreign machine m2
        r_foreign = hub_client.post(
            "/api/machines/self/revoke",
            headers={
                "Authorization": f"Bearer {m1_tok}",
                "X-BURNRATE-Request": "1",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
            json={"id": m2_id},
        )
        assert r_foreign.status_code == 403
        assert sync.authenticate_machine_token(hub_settings.database_path, m2_tok) == m2_id

        # Machine m1 can revoke itself; replay yields 401 DEV-301
        r_self = hub_client.post(
            "/api/machines/self/revoke",
            headers={
                "Authorization": f"Bearer {m1_tok}",
                "X-BURNRATE-Request": "1",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
            json={},
        )
        assert r_self.status_code == 200
        assert r_self.json() == {"ok": True, "machineId": m1_id}
        r_replay = hub_client.post(
            "/api/machines/self/revoke",
            headers={
                "Authorization": f"Bearer {m1_tok}",
                "X-BURNRATE-Request": "1",
                "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
            },
            json={},
        )
        assert r_replay.status_code == 401
        assert r_replay.json()["error"]["code"] == "DEV-301"

        # 2) Client machine-only full sign_out online: sign_in -> _clear_owner_session -> sign_out
        vault = _MemoryVault()
        prev_factory = set_vault_factory(lambda _db: vault)
        try:
            monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
            bridge = DesktopBridge(
                NativeClient(spoke_settings, port=17402),
                preferences=tmp_path / "s11_prefs.json",
                vault=vault,
            )
            assert bridge.sign_in("owner@example.com", PASSWORD, "MachineOnlyOnline")["ok"] is True
            _wait_for_first_sync(bridge)
            mtok1 = read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert mtok1

            bridge._clear_owner_session()
            assert read_vault_secret(spoke_settings.database_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
            assert read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF, vault=vault) == mtok1

            full_out = bridge.sign_out()
            assert full_out == {"ok": True}
            assert read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
            assert PENDING_REVOKE_VAULT_REF not in vault.items
            assert sync.authenticate_machine_token(hub_settings.database_path, mtok1) is None

            # 3) Client machine-only full sign_out OFFLINE -> queues machine_self_revoke -> flushes on reconnect
            assert bridge.sign_in("owner@example.com", PASSWORD, "MachineOnlyOffline")["ok"] is True
            _wait_for_first_sync(bridge)
            mtok2 = read_vault_secret(spoke_settings.database_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert mtok2
            bridge._clear_owner_session()

            sync._HUB_TEST_CLIENTS.clear()

            class _OfflineClient:
                def __init__(self, *_a, **_kw): pass
                def __enter__(self): return self
                def __exit__(self, *_a): return False
                def post(self, *_a, **_kw): raise httpx.ConnectError("offline")

            monkeypatch.setattr(httpx, "Client", _OfflineClient)
            offline_out = bridge.sign_out()
            assert offline_out["ok"] is False
            assert offline_out["code"] == "NET-001"
            assert MACHINE_TOKEN_VAULT_REF not in vault.items
            pending = read_pending_revokes(spoke_settings.database_path, vault=vault)
            assert any(p["kind"] == "machine_self_revoke" and p["token"] == mtok2 for p in pending)

            # Reconnect and flush
            monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
            assert connection_secrets.flush_pending_revokes(
                spoke_settings.database_path, HUB_URL, vault=vault, client=hub_client
            ) is True
            assert PENDING_REVOKE_VAULT_REF not in vault.items
            assert sync.authenticate_machine_token(hub_settings.database_path, mtok2) is None
        finally:
            set_vault_factory(prev_factory)


def test_b5_r4_queue_bounds_dedupe_backoff_broken_vault_and_diagnostics_count(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from spend_app import freshness, support_report
    from spend_app.connection_secrets import (
        MAX_FLUSH_BATCH,
        MAX_PENDING_REVOKES,
        enqueue_pending_revokes,
        flush_pending_revokes,
        pending_revoke_count,
        read_pending_revokes,
    )

    db_path = tmp_path / "b5_bounds.db"
    initialize(db_path)
    vault = _MemoryVault()

    # 1) Dedupe by (kind, tokenHash, hubUrl, machineId)
    enqueue_pending_revokes(
        db_path,
        [
            {"kind": "owner_logout", "token": "br_s_dup", "hubUrl": HUB_URL},
            {"kind": "owner_logout", "token": "br_s_dup", "hubUrl": HUB_URL},
        ],
        vault=vault,
    )
    assert pending_revoke_count(db_path, vault=vault) == 1

    # 2) Fill up to MAX_PENDING_REVOKES (64); 65th fails with APP-903 when offline/unflushable
    # without evicting any existing entry
    batch_64 = [
        {"kind": "owner_logout", "token": f"br_s_cap_{i}", "hubUrl": HUB_URL}
        for i in range(63)
    ]
    enqueue_pending_revokes(db_path, batch_64, vault=vault)
    assert pending_revoke_count(db_path, vault=vault) == MAX_PENDING_REVOKES

    with pytest.raises(CodedError) as exc_info:
        enqueue_pending_revokes(
            db_path,
            [{"kind": "owner_logout", "token": "br_s_overflow_65", "hubUrl": HUB_URL}],
            vault=vault,
            allow_flush_on_overflow=False,
        )
    assert exc_info.value.code == "APP-903"
    assert pending_revoke_count(db_path, vault=vault) == MAX_PENDING_REVOKES
    assert all(e["token"] != "br_s_overflow_65" for e in read_pending_revokes(db_path, vault=vault))

    # 3) Per-contact flush batch cap (8) + exponential backoff
    calls: list[str] = []

    class _BatchClient:
        cookies = None

        def post(self, path, json=None, headers=None, follow_redirects=False):
            tok = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            calls.append(tok)

            class _R:
                status_code = 500
                content = b'{"error":{"code":"HUB-501"}}'
                headers: dict[str, str] = {}

                def json(self):
                    return {"error": {"code": "HUB-501"}}

            return _R()

    t0 = datetime(2026, 5, 1, 12, 0, 0, tzinfo=UTC)
    assert flush_pending_revokes(db_path, HUB_URL, vault=vault, client=_BatchClient(), now=t0) is False
    assert len(calls) == MAX_FLUSH_BATCH

    # Immediate re-flush 1s later skips the 8 backed-off items and picks the next 8 due items!
    calls.clear()
    assert flush_pending_revokes(
        db_path, HUB_URL, vault=vault, client=_BatchClient(), now=t0 + timedelta(seconds=1)
    ) is False
    assert len(calls) == MAX_FLUSH_BATCH

    # If only 2 backed-off items exist, immediate flush sends 0 calls until backoff expires
    db_bo = tmp_path / "b5_backoff.db"
    initialize(db_bo)
    v_bo = _MemoryVault()
    enqueue_pending_revokes(
        db_bo,
        [{"kind": "owner_logout", "token": "br_s_bo_1", "hubUrl": HUB_URL}],
        vault=v_bo,
    )
    calls.clear()
    assert flush_pending_revokes(db_bo, HUB_URL, vault=v_bo, client=_BatchClient(), now=t0) is False
    assert len(calls) == 1
    calls.clear()
    assert flush_pending_revokes(
        db_bo, HUB_URL, vault=v_bo, client=_BatchClient(), now=t0 + timedelta(seconds=2)
    ) is False
    assert len(calls) == 0  # still in 5s backoff
    assert flush_pending_revokes(
        db_bo, HUB_URL, vault=v_bo, client=_BatchClient(), now=t0 + timedelta(seconds=6)
    ) is False
    assert len(calls) == 1  # retried after backoff expired

    # 4) _BrokenVault surfaces APP-903 on failed pending-revoke write
    class _WriteFailVault(_MemoryVault):
        def put(self, secret: str, ref: str | None = None) -> str:
            if ref == connection_secrets.PENDING_REVOKE_VAULT_REF:
                raise connection_secrets.LocationError("keychain refused write")
            return super().put(secret, ref)

    v_broken = _WriteFailVault()
    db_broken = tmp_path / "b5_broken.db"
    initialize(db_broken)
    publish_spoke_credentials(
        db_broken,
        owner_session="br_s_broken_signout",
        vault=v_broken,
        apply_state=lambda _c, st: st.update(
            {"role": "spoke", "spokeConfig": {**dict(st.get("spokeConfig") or {}), "enabled": True, "hubUrl": HUB_URL}}
        ),
    )

    class _OfflineClient:
        def __init__(self, *_a, **_kw): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def post(self, *_a, **_kw): raise httpx.ConnectError("offline")

    monkeypatch.setattr(httpx, "Client", _OfflineClient)
    sync._HUB_TEST_CLIENTS.clear()
    broken_settings = _make_settings(db_broken)
    bridge_broken = DesktopBridge(
        NativeClient(broken_settings, port=17403),
        preferences=tmp_path / "b5_broken_prefs.json",
        vault=v_broken,
    )
    res_broken = bridge_broken.sign_out()
    assert res_broken["ok"] is False
    assert res_broken["code"] == "APP-903"

    # Also test bridge.sign_out() when queue is already at MAX_PENDING_REVOKES (64) & offline:
    # returns APP-903 without dropping local owner/machine credentials.
    db_cap = tmp_path / "b5_bridge_cap.db"
    initialize(db_cap)
    v_cap = _MemoryVault()
    publish_spoke_credentials(
        db_cap,
        owner_session="br_s_cap_preserved",
        machine_token="br_m_cap_preserved",
        vault=v_cap,
        apply_state=lambda _c, st: st.update(
            {"role": "spoke", "spokeConfig": {**dict(st.get("spokeConfig") or {}), "enabled": True, "hubUrl": HUB_URL}}
        ),
    )
    enqueue_pending_revokes(
        db_cap,
        [{"kind": "owner_logout", "token": f"br_s_full_{i}", "hubUrl": HUB_URL} for i in range(MAX_PENDING_REVOKES)],
        vault=v_cap,
    )
    cap_bridge = DesktopBridge(
        NativeClient(_make_settings(db_cap), port=17404),
        preferences=tmp_path / "b5_cap_prefs.json",
        vault=v_cap,
    )
    res_cap = cap_bridge.sign_out()
    assert res_cap["ok"] is False
    assert res_cap["code"] == "APP-903"
    assert read_vault_secret(db_cap, OWNER_SESSION_VAULT_REF, vault=v_cap) == "br_s_cap_preserved"
    assert read_vault_secret(db_cap, MACHINE_TOKEN_VAULT_REF, vault=v_cap) == "br_m_cap_preserved"
    assert pending_revoke_count(db_cap, vault=v_cap) == MAX_PENDING_REVOKES

    # 5) Diagnostics exposes count ONLY (never tokens or hub URLs)
    db_diag = tmp_path / "b5_diag.db"
    initialize(db_diag)
    v_diag = _MemoryVault()
    canary_tok = "br_s_canary_secret_diag_999"
    canary_hub = "https://canary-hub.example.test"
    enqueue_pending_revokes(
        db_diag,
        [
            {"kind": "owner_logout", "token": canary_tok, "hubUrl": canary_hub},
            {"kind": "machine_self_revoke", "token": "br_m_canary_2", "hubUrl": canary_hub},
        ],
        vault=v_diag,
    )
    diag_settings = _make_settings(db_diag)
    prev_factory = set_vault_factory(lambda _db: v_diag)
    try:
        fresh = freshness.report(diag_settings, background=False)
        assert fresh["pendingRevokeCount"] == 2
        assert canary_tok not in json.dumps(fresh)
        assert canary_hub not in json.dumps(fresh)

        rep_text = support_report.build_report(diag_settings, log_dir=tmp_path / "no_logs")
        assert "Pending revocations: 2" in rep_text
        assert canary_tok not in rep_text
        assert canary_hub not in rep_text

        class _DiagClient:
            settings = diag_settings

            def ensure_service(self): pass

            def request(self, path, _body=None):
                if path == "/api/spend/health":
                    return {"ingest": [], "quotas": []}
                if path == "/api/spend/freshness":
                    return fresh
                return {}

        diag_bridge = DesktopBridge(
            _DiagClient(),
            preferences=tmp_path / "b5_diag_prefs.json",
            vault=v_diag,
        )
        ddata = diag_bridge.get_diagnostics_data()
        assert ddata["ok"] is True
        assert ddata["pendingRevokeCount"] == 2
        assert canary_tok not in json.dumps(ddata)
        assert canary_hub not in json.dumps(ddata)
    finally:
        set_vault_factory(prev_factory)


# ---------------------------------------------------------------------------
# Round 5: S12, residual B5, and B6
# ---------------------------------------------------------------------------


def test_s12_r5_stale_machine_rotation_after_full_sign_out_revokes_or_queues_successor(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import (
        flush_pending_revokes,
        read_pending_revokes,
    )

    class _FakeRes:
        def __init__(self, status_code: int, body: dict | None = None, headers: dict | None = None):
            self.status_code = status_code
            self._body = body
            self.content = json.dumps(body).encode("utf-8") if body is not None else b""
            self.headers = headers or {}

        def json(self):
            if self._body is None:
                raise ValueError("no json")
            return self._body

    hub_settings, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(
        tmp_path / "s12", monkeypatch
    )
    spoke_db = spoke_settings.database_path
    vault = _MemoryVault()
    prev_factory = set_vault_factory(lambda _db: vault)
    try:
        monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17420),
            preferences=tmp_path / "s12_prefs.json",
            vault=vault,
        )

        # 1) Background rotate_spoke_machine_token: M -> N online while full sign_out clears M
        assert bridge.sign_in("owner@example.com", PASSWORD, "S12BgOnline")["ok"] is True
        _wait_for_first_sync(bridge)
        bridge._clear_owner_session()
        mtok_bg = read_vault_secret(spoke_db, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
        assert mtok_bg

        real_post = sync._hub_post_with_cookie_isolation
        bg_rot_evt = threading.Event()
        bg_rel_evt = threading.Event()
        bg_issued_n: list[str] = []

        def _intercept_bg_online(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            if path == "/api/auth/machine/rotate":
                r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
                if r.status_code == 200:
                    bg_issued_n.append(str(r.json().get("token") or ""))
                bg_rot_evt.set()
                assert bg_rel_evt.wait(timeout=3.0)
                return r
            return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _intercept_bg_online)
        bg_res: list[object] = []

        def _bg_worker():
            bg_res.append(setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client))

        t_bg = threading.Thread(target=_bg_worker)
        t_bg.start()
        assert bg_rot_evt.wait(timeout=3.0)
        assert bridge.sign_out() == {"ok": True}
        bg_rel_evt.set()
        t_bg.join(timeout=3.0)

        assert len(bg_issued_n) == 1 and bg_issued_n[0]
        assert bg_res and bg_res[0] == {"ok": False, "status": 409}
        assert sync.authenticate_machine_token(hub_settings.database_path, mtok_bg) is None
        assert sync.authenticate_machine_token(hub_settings.database_path, bg_issued_n[0]) is None
        assert read_vault_secret(spoke_db, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
        with connect(spoke_db) as conn:
            assert setup_wizard.read_setup_state(conn)["spokeConfig"].get("signedOut") is True

        # 2) Background rotate_spoke_machine_token: when self-revoke fails (offline/403), N is durably queued
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
        assert bridge.sign_in("owner@example.com", PASSWORD, "S12BgOffline")["ok"] is True
        _wait_for_first_sync(bridge)
        bridge._clear_owner_session()
        assert read_vault_secret(spoke_db, MACHINE_TOKEN_VAULT_REF, vault=vault)

        bg_rot_evt.clear()
        bg_rel_evt.clear()
        bg_issued_n2: list[str] = []

        def _intercept_bg_queue(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            if path == "/api/auth/machine/rotate":
                r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
                if r.status_code == 200:
                    bg_issued_n2.append(str(r.json().get("token") or ""))
                bg_rot_evt.set()
                assert bg_rel_evt.wait(timeout=3.0)
                return r
            if path == "/api/machines/self/revoke" and bg_issued_n2 and headers.get("Authorization") == f"Bearer {bg_issued_n2[0]}":
                return _FakeRes(503, {"error": {"code": "HUB-501"}})
            return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _intercept_bg_queue)
        bg_res2: list[object] = []

        t_bg2 = threading.Thread(
            target=lambda: bg_res2.append(setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client))
        )
        t_bg2.start()
        assert bg_rot_evt.wait(timeout=3.0)
        assert bridge.sign_out() == {"ok": True}
        bg_rel_evt.set()
        t_bg2.join(timeout=3.0)

        assert bg_res2 and bg_res2[0] == {"ok": False, "status": 409}
        assert len(bg_issued_n2) == 1 and bg_issued_n2[0]
        pending_bg = read_pending_revokes(spoke_db, vault=vault)
        assert any(q["kind"] == "machine_self_revoke" and q["token"] == bg_issued_n2[0] for q in pending_bg)
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
        assert flush_pending_revokes(spoke_db, HUB_URL, vault=vault, client=hub_client, ignore_backoff=True) is True
        assert sync.authenticate_machine_token(hub_settings.database_path, bg_issued_n2[0]) is None

        # 4) Failed / fenced auth & rotation callers revoke/queue issued tokens cleanly:
        # 4a) DesktopBridge._authenticate_and_register when /api/machines fails revokes session_token
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
        real_call_hub = bridge._call_hub
        captured_login_tok: list[str] = []

        def _fail_reg(hub_url, method, p, **kw):
            data, res, err = real_call_hub(hub_url, method, p, **kw)
            if p == "/api/account/login" and err is None:
                tok = auth_model.extract_cookie(res) or ""
                if tok:
                    captured_login_tok.append(tok)
            if p == "/api/machines":
                return None, _FakeRes(500, {"error": {"code": "HUB-501"}}), {"ok": False, "code": "HUB-501", "message": "err"}
            return data, res, err

        monkeypatch.setattr(bridge, "_call_hub", _fail_reg)
        res_reg_fail = bridge.sign_in("owner@example.com", PASSWORD, "S12RegFail")
        assert res_reg_fail["ok"] is False
        assert len(captured_login_tok) == 1
        r_check = hub_client.get("/api/machines", headers={"Authorization": f"Bearer {captured_login_tok[0]}"})
        assert r_check.status_code == 401
        monkeypatch.setattr(bridge, "_call_hub", real_call_hub)

        # 4b) DesktopBridge._authenticate_and_register when publish fails with APP-903 revokes BOTH tokens
        class _FailCredWriteVault(_MemoryVault):
            def put(self, secret: str, ref: str | None = None) -> str:
                if ref == MACHINE_TOKEN_VAULT_REF:
                    raise LocationError("keychain blocked machine token")
                return super().put(secret, ref)

        v_fail_reg = _FailCredWriteVault()
        bridge_fail_reg = DesktopBridge(
            NativeClient(spoke_settings, port=17421),
            preferences=tmp_path / "s12_fail_reg_prefs.json",
            vault=v_fail_reg,
        )
        seen_pair: list[str] = []
        real_bridge_call = bridge_fail_reg._call_hub

        def _capture_pair(hub_url, method, p, **kw):
            data, res, err = real_bridge_call(hub_url, method, p, **kw)
            if p == "/api/account/login" and err is None:
                seen_pair.append(auth_model.extract_cookie(res) or "")
            if p == "/api/machines" and err is None and isinstance(data, dict):
                seen_pair.append(str(data.get("token") or ""))
            return data, res, err

        monkeypatch.setattr(bridge_fail_reg, "_call_hub", _capture_pair)
        res_app903 = bridge_fail_reg.sign_in("owner@example.com", PASSWORD, "S12App903Pair")
        assert res_app903["ok"] is False and res_app903["code"] == "APP-903"
        assert len(seen_pair) == 2
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {seen_pair[0]}"}).status_code == 401
        assert sync.authenticate_machine_token(hub_settings.database_path, seen_pair[1]) is None

        # 4c) setup_connect_account fenced by concurrent sign_out revokes/queues BOTH tokens
        monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
        assert bridge.sign_in("owner@example.com", PASSWORD, "S12ConnectBase")["ok"] is True
        _wait_for_first_sync(bridge)
        assert bridge.sign_out() == {"ok": True}

        acct_evt = threading.Event()
        acct_rel = threading.Event()
        real_prepare_sync = sync.prepare_spoke_sync

        def _pause_before_publish(db_p, h_url, tok):
            gen = real_prepare_sync(db_p, h_url, tok)
            acct_evt.set()
            assert acct_rel.wait(timeout=3.0)
            return gen

        monkeypatch.setattr(sync, "prepare_spoke_sync", _pause_before_publish)
        acct_issued: list[str] = []
        real_acct_call = setup_wizard._call_central_server_account

        def _wrap_acct_call(s_url, ep, pl):
            u, d, o_tok = real_acct_call(s_url, ep, pl)
            acct_issued.append(o_tok)
            return u, d, o_tok

        monkeypatch.setattr(setup_wizard, "_call_central_server_account", _wrap_acct_call)
        acct_resps: list[object] = []
        with TestClient(spoke_app) as spoke_client:
            t_acct = threading.Thread(
                target=lambda: acct_resps.append(
                    spoke_client.post(
                        "/api/setup/account",
                        headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
                        json={"mode": "login", "username": "owner@example.com", "password": PASSWORD, "computerName": "S12FenceAcct", "testSync": False},
                    )
                )
            )
            t_acct.start()
            assert acct_evt.wait(timeout=3.0)
            assert bridge.sign_out() == {"ok": True}
            acct_rel.set()
            t_acct.join(timeout=3.0)

        assert len(acct_resps) == 1
        assert acct_resps[0].status_code == 409
        assert acct_resps[0].json()["error"]["code"] == "AUTH-107"
        assert len(acct_issued) == 1 and acct_issued[0]
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {acct_issued[0]}"}).status_code == 401
        assert read_vault_secret(spoke_db, MACHINE_TOKEN_VAULT_REF, vault=vault) is None

        # 4d) ensure_fresh_owner_session APP-903 vault failure revokes/queues new_token
        monkeypatch.setattr(sync, "prepare_spoke_sync", real_prepare_sync)
        monkeypatch.setattr(setup_wizard, "_call_central_server_account", real_acct_call)
        login_r = hub_client.post(
            "/api/account/login",
            headers={"X-BURNRATE-Request": "1", "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
            json={"username": "owner@example.com", "password": PASSWORD},
        )
        assert login_r.status_code == 200
        old_s = auth_model.extract_cookie(login_r) or ""
        hub_client.cookies.clear()

        v_block_owner = _MemoryVault()
        publish_spoke_credentials(
            spoke_db,
            owner_session=old_s,
            vault=v_block_owner,
            apply_state=lambda _c, st: st["spokeConfig"].update({"hubConfigured": True, "ownerSessionRotationRequired": True}),
        )

        class _BlockOwnerPutVault(_MemoryVault):
            def __init__(self, seed: dict[str, str]):
                super().__init__()
                self.items = dict(seed)

            def put(self, secret: str, ref: str | None = None) -> str:
                if ref == OWNER_SESSION_VAULT_REF:
                    raise LocationError("blocked owner write")
                return super().put(secret, ref)

        v_err = _BlockOwnerPutVault(v_block_owner.items)
        rot_captured: list[str] = []

        def _cap_rot(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
            if path == "/api/auth/session/rotate":
                tok = auth_model.extract_cookie(r) or ""
                if tok:
                    rot_captured.append(tok)
            return r

        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _cap_rot)
        with pytest.raises(CodedError) as app903_exc:
            sync.ensure_fresh_owner_session(spoke_db, HUB_URL, old_s, vault=v_err, client=hub_client)
        assert app903_exc.value.code == "APP-903"
        assert len(rot_captured) == 1
        assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {rot_captured[0]}"}).status_code == 401
    finally:
        set_vault_factory(prev_factory)


def test_b5_r5_sign_out_persists_revoke_intent_before_clearing_credentials(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import httpx
    from spend_app.connection_secrets import (
        PENDING_REVOKE_VAULT_REF,
        enqueue_pending_revokes,
        pending_revoke_count,
    )

    class _OfflineClient:
        def __init__(self, *_a, **_kw): pass
        def __enter__(self): return self
        def __exit__(self, *_a): return False
        def post(self, *_a, **_kw): raise httpx.ConnectError("offline")

    def _seed_spoke(db_p: Path, vault_obj: _MemoryVault, *, owner: str, machine: str) -> None:
        initialize(db_p)
        publish_spoke_credentials(
            db_p,
            owner_session=owner,
            machine_token=machine,
            vault=vault_obj,
            apply_state=lambda _c, st: st.update(
                {
                    "role": "spoke",
                    "account": {"username": "owner@example.com", "machineId": "machine-b5r5"},
                    "spokeConfig": {
                        **dict(st.get("spokeConfig") or {}),
                        "enabled": True,
                        "hubConfigured": True,
                        "hubUrl": HUB_URL,
                    },
                }
            ),
        )

    # 1) 63 existing + 2 new (full sign_out) offline -> APP-903; BOTH credentials & SQLite intact
    db_63 = tmp_path / "b5r5_63.db"
    v_63 = _MemoryVault()
    _seed_spoke(db_63, v_63, owner="br_s_preserve_63", machine="br_m_preserve_63")
    enqueue_pending_revokes(
        db_63,
        [{"kind": "owner_logout", "token": f"br_s_pre_{i}", "hubUrl": HUB_URL} for i in range(63)],
        vault=v_63,
    )
    assert pending_revoke_count(db_63, vault=v_63) == 63

    monkeypatch.setattr(httpx, "Client", _OfflineClient)
    sync._HUB_TEST_CLIENTS.clear()
    bridge_63 = DesktopBridge(
        NativeClient(_make_settings(db_63), port=17430),
        preferences=tmp_path / "b5r5_63_prefs.json",
        vault=v_63,
    )
    assert bridge_63._get_owner_session_token() == "br_s_preserve_63"
    assert bridge_63._get_machine_token() == "br_m_preserve_63"

    res_63 = bridge_63.sign_out()
    assert res_63["ok"] is False and res_63["code"] == "APP-903"
    assert read_vault_secret(db_63, OWNER_SESSION_VAULT_REF, vault=v_63) == "br_s_preserve_63"
    assert read_vault_secret(db_63, MACHINE_TOKEN_VAULT_REF, vault=v_63) == "br_m_preserve_63"
    assert bridge_63._owner_session == "br_s_preserve_63"
    assert bridge_63._machine_token == "br_m_preserve_63"
    assert pending_revoke_count(db_63, vault=v_63) == 63
    with connect(db_63) as conn:
        st_63 = setup_wizard.read_setup_state(conn)
        assert st_63["role"] == "spoke"
        assert st_63["spokeConfig"]["enabled"] is True
        assert not st_63["spokeConfig"].get("signedOut")

    # 2) _WriteFailVault failing on PENDING_REVOKE_VAULT_REF for sign_out -> APP-903; creds intact
    class _WriteFailPendingVault(_MemoryVault):
        def put(self, secret: str, ref: str | None = None) -> str:
            if ref == PENDING_REVOKE_VAULT_REF:
                raise LocationError("keychain refused pending write")
            return super().put(secret, ref)

    db_wf = tmp_path / "b5r5_writefail.db"
    v_wf = _WriteFailPendingVault()
    _seed_spoke(db_wf, v_wf, owner="br_s_wf", machine="br_m_wf")
    bridge_wf = DesktopBridge(
        NativeClient(_make_settings(db_wf), port=17432),
        preferences=tmp_path / "b5r5_wf_prefs.json",
        vault=v_wf,
    )
    assert bridge_wf._get_owner_session_token() == "br_s_wf"
    assert bridge_wf._get_machine_token() == "br_m_wf"

    res_wf_full = bridge_wf.sign_out()
    assert res_wf_full["ok"] is False and res_wf_full["code"] == "APP-903"
    assert read_vault_secret(db_wf, OWNER_SESSION_VAULT_REF, vault=v_wf) == "br_s_wf"
    assert read_vault_secret(db_wf, MACHINE_TOKEN_VAULT_REF, vault=v_wf) == "br_m_wf"
    assert bridge_wf._owner_session == "br_s_wf"
    assert bridge_wf._machine_token == "br_m_wf"
    with connect(db_wf) as conn:
        st_wf = setup_wizard.read_setup_state(conn)
        assert st_wf["role"] == "spoke"
        assert st_wf["spokeConfig"]["enabled"] is True
        assert not st_wf["spokeConfig"].get("signedOut")

    # 3) Normal online sign_out: write PENDING_REVOKE BEFORE deleting creds, then ack
    ops: list[tuple[str, str]] = []

    class _RecordingVault(_MemoryVault):
        def put(self, secret: str, ref: str | None = None) -> str:
            ops.append(("put", str(ref)))
            return super().put(secret, ref)

        def delete(self, ref: str) -> None:
            ops.append(("delete", str(ref)))
            return super().delete(ref)

    _hub_s, hub_client, spoke_s, _app = _create_paired_hub_and_spoke(tmp_path / "b5r5_online", monkeypatch)
    v_rec = _RecordingVault()
    prev_factory = set_vault_factory(lambda _db: v_rec)
    try:
        monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
        bridge_on = DesktopBridge(
            NativeClient(spoke_s, port=17433),
            preferences=tmp_path / "b5r5_on_prefs.json",
            vault=v_rec,
        )
        with hub_client:
            assert bridge_on.sign_in("owner@example.com", PASSWORD, "B5R5Online")["ok"] is True
            _wait_for_first_sync(bridge_on)
            ops.clear()
            assert bridge_on.sign_out() == {"ok": True}
            assert ops.index(("put", PENDING_REVOKE_VAULT_REF)) < ops.index(("delete", MACHINE_TOKEN_VAULT_REF))
            assert PENDING_REVOKE_VAULT_REF not in v_rec.items
    finally:
        set_vault_factory(prev_factory)


def test_b6_r5_flush_backoff_and_retry_after_measured_after_http_duration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app.connection_secrets import (
        enqueue_pending_revokes,
        flush_pending_revokes,
        read_pending_revokes,
    )

    db_path = tmp_path / "b6_clock.db"
    initialize(db_path)
    vault = _MemoryVault()

    enqueue_pending_revokes(
        db_path,
        [
            {"kind": "owner_logout", "token": "br_s_b6_slow1", "hubUrl": HUB_URL},
            {"kind": "owner_logout", "token": "br_s_b6_slow2", "hubUrl": HUB_URL},
        ],
        vault=vault,
    )

    t0 = datetime(2026, 6, 1, 12, 0, 0, tzinfo=UTC)
    clock_now = [t0]

    class _FakeRes:
        def __init__(self, status_code: int, body: dict, headers: dict[str, str] | None = None):
            self.status_code = status_code
            self._body = body
            self.content = json.dumps(body).encode("utf-8")
            self.headers = headers or {}

        def json(self):
            return self._body

    calls: list[str] = []

    class _SlowClient:
        cookies = None

        def post(self, path, json=None, headers=None, follow_redirects=False):
            tok = (headers or {}).get("Authorization", "").removeprefix("Bearer ")
            calls.append(tok)
            # Each HTTP call takes 12 seconds -> 24 seconds total across the batch
            clock_now[0] = clock_now[0] + timedelta(seconds=12)
            if tok == "br_s_b6_slow1":
                return _FakeRes(500, {"error": {"code": "HUB-501"}})
            if tok == "br_s_b6_slow2":
                return _FakeRes(429, {"error": {"code": "AUTH-102"}}, {"Retry-After": "10"})
            raise AssertionError(f"Unexpected token {tok}")

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, _SlowClient())
    assert flush_pending_revokes(
        db_path, HUB_URL, vault=vault, client=_SlowClient(), clock=lambda: clock_now[0]
    ) is False
    assert calls == ["br_s_b6_slow1", "br_s_b6_slow2"]
    assert clock_now[0] == t0 + timedelta(seconds=24)

    items = {e["token"]: e for e in read_pending_revokes(db_path, vault=vault)}
    # Both cooldowns measured from completion (>= t0 + 24s), NOT t0!
    assert items["br_s_b6_slow1"]["nextAttemptAt"] == "2026-06-01T12:00:29Z"
    assert items["br_s_b6_slow2"]["nextAttemptAt"] == "2026-06-01T12:00:34Z"

    # 2 seconds after batch finished (t0 + 26s): neither item is due -> 0 HTTP calls
    calls.clear()
    clock_now[0] = t0 + timedelta(seconds=26)
    assert flush_pending_revokes(
        db_path, HUB_URL, vault=vault, client=_SlowClient(), clock=lambda: clock_now[0]
    ) is False
    assert calls == []

    # 6 seconds after batch finished (t0 + 30s): slow1 (5s backoff) is due, slow2 (10s Retry-After) waits
    calls.clear()
    clock_now[0] = t0 + timedelta(seconds=30)
    assert flush_pending_revokes(
        db_path, HUB_URL, vault=vault, client=_SlowClient(), clock=lambda: clock_now[0]
    ) is False
    assert calls == ["br_s_b6_slow1"]

    # After Retry-After expires (t0 + 35s): slow2 is retried
    calls.clear()
    clock_now[0] = t0 + timedelta(seconds=35)
    assert flush_pending_revokes(
        db_path, HUB_URL, vault=vault, client=_SlowClient(), clock=lambda: clock_now[0]
    ) is False
    assert calls == ["br_s_b6_slow2"]
# ---------------------------------------------------------------------------
# Round 6 ? S13, B7, B8 regression tests
# ---------------------------------------------------------------------------


def test_s13_r6_snapshot_failure_fails_closed_and_signed_out_login_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path / "s13", monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    prev_factory = set_vault_factory(lambda _db: vault)
    try:
        monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17440),
            preferences=tmp_path / "s13_prefs.json",
            vault=vault,
        )

        with hub_client:
            # 1) Snapshot failure on an existing DB returns APP-903 BEFORE any _call_hub call
            initialize(db_path)
            called_hub: list[tuple[str, str]] = []
            real_call_hub = bridge._call_hub

            def _spy_call_hub(hub_url, method, path, **kw):
                called_hub.append((method, path))
                return real_call_hub(hub_url, method, path, **kw)

            monkeypatch.setattr(bridge, "_call_hub", _spy_call_hub)
            real_read = setup_wizard.read_setup_state
            monkeypatch.setattr(
                setup_wizard,
                "read_setup_state",
                lambda *_a, **_kw: (_ for _ in ()).throw(sqlite3.OperationalError("db corrupt")),
            )

            res_snap_fail = bridge.sign_in("owner@example.com", PASSWORD, "S13SnapFail")
            assert res_snap_fail["ok"] is False
            assert res_snap_fail["code"] == "APP-903"
            assert called_hub == []

            res_create_fail = bridge.create_account("owner@example.com", PASSWORD, "S13SnapCreateFail")
            assert res_create_fail["ok"] is False
            assert res_create_fail["code"] == "APP-903"
            assert called_hub == []

            # 2) Restore DB reads; fresh sign_in after full sign_out (signedOut=True) succeeds
            monkeypatch.setattr(setup_wizard, "read_setup_state", real_read)
            monkeypatch.setattr(bridge, "_call_hub", real_call_hub)

            res_in1 = bridge.sign_in("owner@example.com", PASSWORD, "S13FirstPC")
            assert res_in1["ok"] is True
            _wait_for_first_sync(bridge)
            assert bridge.sign_out() == {"ok": True}
            with connect(db_path) as conn:
                assert setup_wizard.read_setup_state(conn)["spokeConfig"].get("signedOut") is True

            res_in2 = bridge.sign_in("owner@example.com", PASSWORD, "S13SecondPC")
            assert res_in2["ok"] is True
            _wait_for_first_sync(bridge)
            assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault)
            assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault)
            with connect(db_path) as conn:
                sc_after = setup_wizard.read_setup_state(conn)["spokeConfig"]
                assert not sc_after.get("signedOut")

            # 3) Concurrent sign_out during in-flight sign_in fences publication & revokes BOTH tokens
            assert bridge.sign_out() == {"ok": True}
            reg_done = threading.Event()
            allow_pub = threading.Event()
            minted_pair: list[str] = []

            def _pause_on_reg(hub_url, method, path, **kw):
                data, res, err = real_call_hub(hub_url, method, path, **kw)
                if path == "/api/account/login" and err is None:
                    minted_pair.append(auth_model.extract_cookie(res) or "")
                if path == "/api/machines" and method == "POST" and err is None and isinstance(data, dict):
                    minted_pair.append(str(data.get("token") or ""))
                    reg_done.set()
                    assert allow_pub.wait(timeout=4.0)
                return data, res, err

            monkeypatch.setattr(bridge, "_call_hub", _pause_on_reg)
            in_box: list[dict] = []
            t_in = threading.Thread(
                target=lambda: in_box.append(bridge.sign_in("owner@example.com", PASSWORD, "S13RacePC"))
            )
            t_in.start()
            assert reg_done.wait(timeout=3.0)

            assert bridge.sign_out() == {"ok": True}
            allow_pub.set()
            t_in.join(timeout=4.0)

            assert len(in_box) == 1
            assert in_box[0]["ok"] is False
            assert len(minted_pair) == 2
            assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
            assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
            assert hub_client.get(
                "/api/machines", headers={"Authorization": f"Bearer {minted_pair[0]}"}
            ).status_code == 401
            assert sync.authenticate_machine_token(_hub_settings.database_path, minted_pair[1]) is None
    finally:
        set_vault_factory(prev_factory)


def test_b7_r6_machine_rotation_and_bg_sync_succeed_after_owner_only_clear(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hub_settings, hub_client, spoke_settings, _spoke_app = _create_paired_hub_and_spoke(
        tmp_path / "b7", monkeypatch
    )
    db_path = spoke_settings.database_path
    vault = _MemoryVault()
    prev_factory = set_vault_factory(lambda _db: vault)
    try:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17441),
            preferences=tmp_path / "b7_prefs.json",
            vault=vault,
        )
        with hub_client:
            real_sync_once = sync.sync_once
            monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
            assert bridge.sign_in("owner@example.com", PASSWORD, "B7MachinePC")["ok"] is True
            _wait_for_first_sync(bridge)
            monkeypatch.setattr(sync, "sync_once", real_sync_once)

            tok_m = read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert tok_m.startswith("br_m_")

            # Owner-only clear with mark_signed_out=True (simulates owner expiry/clear without full sign-out)
            clear_spoke_credentials(
                db_path,
                clear_owner_session=True,
                mark_signed_out=True,
                vault=vault,
            )
            assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
            assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) == tok_m

            # 1) Direct rotate_spoke_machine_token succeeds (M -> N) and revokes M
            rot_res = setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client)
            assert rot_res is not None and rot_res.get("ok") is True, rot_res
            tok_n = read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert tok_n and tok_n != tok_m
            assert sync.authenticate_machine_token(hub_settings.database_path, tok_m) is None
            assert sync.authenticate_machine_token(hub_settings.database_path, tok_n) is not None

            # 2) maybe_run_configured_spoke_sync with rotationRequired=True rotates N -> P and syncs
            with connect(db_path) as conn:
                st = setup_wizard.read_setup_state(conn)
                st["spokeConfig"]["rotationRequired"] = True
                setup_wizard.write_setup_state(conn, st)

            bg_res = setup_wizard.maybe_run_configured_spoke_sync(
                spoke_settings, PricingEngine([]), client=hub_client
            )
            assert bg_res is not None and bg_res.get("ok") is True, bg_res
            assert bg_res.get("tokenRotated") is True
            tok_p = read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) or ""
            assert tok_p and tok_p != tok_n
            assert sync.authenticate_machine_token(hub_settings.database_path, tok_n) is None
            assert sync.authenticate_machine_token(hub_settings.database_path, tok_p) is not None

            # 3) Subsequent background sync continues working with P
            bg_res2 = setup_wizard.maybe_run_configured_spoke_sync(
                spoke_settings, PricingEngine([]), client=hub_client
            )
            assert bg_res2 is not None and bg_res2.get("ok") is True, bg_res2
    finally:
        set_vault_factory(prev_factory)


def test_b8_r6_failed_cleanup_and_queue_retains_in_memory_warns_and_flushes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    from spend_app import freshness
    from spend_app.connection_secrets import (
        MAX_PENDING_REVOKES,
        PENDING_REVOKE_VAULT_REF,
        clear_in_memory_pending_revokes,
        enqueue_pending_revokes,
        flush_pending_revokes,
        has_revoke_retention_warning,
        pending_revoke_count,
        retained_pending_revoke_count,
    )

    clear_in_memory_pending_revokes()
    secret_hub = "https://b8-secret-hub.example.com:8443"
    hub_settings, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(
        tmp_path / "b8", monkeypatch
    )
    monkeypatch.setenv("BURNRATE_HUB_URL", secret_hub)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, secret_hub, hub_client)
    _patch_native_to_local(monkeypatch, spoke_app, port=17442)
    db_path = spoke_settings.database_path

    class _WriteFailPendingVault(_MemoryVault):
        def put(self, secret: str, ref: str | None = None) -> str:
            if ref == PENDING_REVOKE_VAULT_REF:
                raise LocationError("keychain refused pending revoke write")
            return super().put(secret, ref)

    class _FakeErrRes:
        def __init__(self, status_code: int, code: str) -> None:
            self.status_code = status_code
            self._payload = {"error": {"code": code}}
            self.content = json.dumps(self._payload).encode("utf-8")
            self.headers: dict[str, str] = {}
            self.cookies: dict[str, str] = {}

        def json(self):
            return self._payload

    # 1) bridge.sign_in where /api/machines fails and logout+queue both fail -> APP-903 + memory retention
    v_wf = _WriteFailPendingVault()
    prev_factory = set_vault_factory(lambda _db: v_wf)
    try:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17442),
            preferences=tmp_path / "b8_prefs.json",
            vault=v_wf,
        )
        real_call_hub = bridge._call_hub
        real_post = sync._hub_post_with_cookie_isolation
        minted_sess: list[str] = []

        def _fail_reg_call(hub_url, method, path, **kw):
            data, res, err = real_call_hub(hub_url, method, path, **kw)
            if path == "/api/account/login" and err is None:
                minted_sess.append(auth_model.extract_cookie(res) or "")
            if path == "/api/machines" and method == "POST":
                return None, _FakeErrRes(500, "HUB-501"), {"ok": False, "code": "HUB-501", "message": "err"}
            return data, res, err

        def _fail_logout_post(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
            if path == "/api/auth/logout":
                return _FakeErrRes(503, "HUB-501")
            return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

        monkeypatch.setattr(bridge, "_call_hub", _fail_reg_call)
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _fail_logout_post)

        with hub_client, caplog.at_level(logging.WARNING, logger="spend_app.connection_secrets"):
            res_fail = bridge.sign_in("owner@example.com", PASSWORD, "B8SignInFail")

        assert res_fail["ok"] is False
        assert res_fail["code"] == "APP-903"
        assert len(minted_sess) == 1 and minted_sess[0]
        assert retained_pending_revoke_count() == 1
        assert has_revoke_retention_warning() is True
        assert pending_revoke_count(db_path, vault=v_wf) == 1
        assert "retained in memory" in caplog.text
        assert minted_sess[0] not in caplog.text
        assert secret_hub not in caplog.text

        # Surface warning in both freshness.report() and DesktopBridge.get_diagnostics_data()
        rep_warn = freshness.report(spoke_settings, background=False)
        assert rep_warn["pendingRevokeRetentionWarning"] is True
        assert any(i.get("key") == "pending-revoke-retention" and i.get("severity") == "warn" for i in rep_warn["issues"])

        with TestClient(spoke_app):
            pass
        diag = bridge.get_diagnostics_data()
        assert diag["ok"] is True
        assert diag.get("pendingRevokeRetentionWarning") is True
        assert diag["pendingRevokeCount"] >= 1

        # Next hub contact flushes retained token even when vault still refuses writes
        monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
        with hub_client:
            assert flush_pending_revokes(db_path, secret_hub, vault=v_wf, client=hub_client, ignore_backoff=True) is True
            assert hub_client.get("/api/machines", headers={"Authorization": f"Bearer {minted_sess[0]}"}).status_code == 401
        assert retained_pending_revoke_count() == 0
        assert has_revoke_retention_warning() is False
        assert freshness.report(spoke_settings, background=False)["pendingRevokeRetentionWarning"] is False
        assert "pendingRevokeRetentionWarning" not in bridge.get_diagnostics_data()

        # 2) rotate_spoke_machine_token fence conflict + full queue + failing self-revoke -> APP-903 + retains & flushes
        monkeypatch.setattr(bridge, "_call_hub", real_call_hub)
        v_full = _MemoryVault()
        set_vault_factory(lambda _db: v_full)
        bridge2 = DesktopBridge(
            NativeClient(spoke_settings, port=17442),
            preferences=tmp_path / "b8_prefs2.json",
            vault=v_full,
        )
        with hub_client:
            real_sync_once = sync.sync_once
            monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
            assert bridge2.sign_in("owner@example.com", PASSWORD, "B8RotateFull")["ok"] is True
            _wait_for_first_sync(bridge2)
            monkeypatch.setattr(sync, "sync_once", real_sync_once)

            # Fill pending queue to MAX_PENDING_REVOKES with machine_self_revoke (intercepted with 503 during test)
            enqueue_pending_revokes(
                db_path,
                [{"kind": "machine_self_revoke", "token": f"br_m_fill_{i}", "hubUrl": secret_hub} for i in range(MAX_PENDING_REVOKES)],
                vault=v_full,
            )

            rot_evt = threading.Event()
            rel_evt = threading.Event()
            issued_m: list[str] = []

            def _intercept_rot_full(hub_url, path, headers, *, client=None, timeout=8.0, json_body=None):
                if path == "/api/auth/machine/rotate":
                    r = real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)
                    if r.status_code == 200:
                        issued_m.append(str(r.json().get("token") or ""))
                    rot_evt.set()
                    assert rel_evt.wait(timeout=3.0)
                    return r
                if path == "/api/machines/self/revoke":
                    return _FakeErrRes(503, "HUB-501")
                return real_post(hub_url, path, headers, json_body=json_body, client=client, timeout=timeout)

            monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", _intercept_rot_full)
            rot_out: list[object] = []
            t_r = threading.Thread(
                target=lambda: rot_out.append(setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client))
            )
            t_r.start()
            assert rot_evt.wait(timeout=3.0)
            # Clear creds locally (without revoking to hub since queue is full) so rotate hits conflict
            clear_spoke_credentials(db_path, clear_machine_token=True, clear_owner_session=True, mark_signed_out=True, vault=v_full)
            rel_evt.set()
            t_r.join(timeout=3.0)

            assert rot_out and isinstance(rot_out[0], dict) and rot_out[0].get("code") == "APP-903"
            assert len(issued_m) == 1 and issued_m[0]
            assert retained_pending_revoke_count() == 1
            assert has_revoke_retention_warning() is True

            # Flush with live hub revokes retained machine token (over batches) and clears warning
            monkeypatch.setattr(sync, "_hub_post_with_cookie_isolation", real_post)
            flushed = False
            for _ in range(12):
                if flush_pending_revokes(db_path, secret_hub, vault=v_full, client=hub_client, ignore_backoff=True):
                    flushed = True
                    break
            assert flushed is True
            assert sync.authenticate_machine_token(hub_settings.database_path, issued_m[0]) is None
            assert retained_pending_revoke_count() == 0
            assert has_revoke_retention_warning() is False
    finally:
        clear_in_memory_pending_revokes()
        set_vault_factory(prev_factory)
