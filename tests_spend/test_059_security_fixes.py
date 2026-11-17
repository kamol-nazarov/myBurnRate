"""Regression tests for 0.5.9 security findings S1, S2, S4, S6, S7, S8, B3, and B4."""

from __future__ import annotations

import contextlib
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
    get_vault,
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
    setup_wizard._GLOBAL_THROTTLE.reset()
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
    setup_wizard._GLOBAL_THROTTLE.reset()
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

        # 2. /api/setup/rotate-machine -> 503 APP-903, zero DB token
        orig_tok, orig_sess = _connect_cleanly()
        rot_vault_1 = _ReadExistingWriteFailVault(
            {MACHINE_TOKEN_VAULT_REF: orig_tok, OWNER_SESSION_VAULT_REF: orig_sess},
            vault_mode,
        )
        prev = set_vault_factory(lambda _db: rot_vault_1)
        try:
            res_rm = spoke_client.post("/api/setup/rotate-machine", headers=local_headers, json={})
            assert res_rm.status_code == 503, res_rm.text
            assert res_rm.json()["error"]["code"] == "APP-903"
            _assert_no_db_secrets(spoke_settings.database_path)
        finally:
            set_vault_factory(prev)

        # 3. setup_wizard.rotate_spoke_machine_token -> ok=False APP-903, zero DB token
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

        # 4. DesktopBridge.rotate_owner_session -> ok=False APP-903, zero DB ownerSession
        orig_tok3, orig_sess3 = _connect_cleanly()
        rot_vault_3 = _ReadExistingWriteFailVault(
            {MACHINE_TOKEN_VAULT_REF: orig_tok3, OWNER_SESSION_VAULT_REF: orig_sess3},
            vault_mode,
        )
        prev = set_vault_factory(lambda _db: rot_vault_3)
        try:
            rot_bridge = DesktopBridge(
                NativeClient(spoke_settings, port=17360),
                preferences=tmp_path / f"prefs_rot_{vault_mode}.json",
                vault=rot_vault_3,
            )
            res_ro = rot_bridge.rotate_owner_session()
            assert res_ro["ok"] is False
            assert res_ro["code"] == "APP-903"
            _assert_no_db_secrets(spoke_settings.database_path)
        finally:
            set_vault_factory(prev)

        # 5. /api/setup/owner-session & DesktopBridge acquisition writers -> APP-903
        prev = set_vault_factory(_bad_factory)
        try:
            res_os = spoke_client.post(
                "/api/setup/owner-session",
                headers=local_headers,
                json={"username": "owner@example.com", "password": PASSWORD},
            )
            assert res_os.status_code == 503, res_os.text
            assert res_os.json()["error"]["code"] == "APP-903"

            bridge = DesktopBridge(
                NativeClient(spoke_settings, port=17360),
                preferences=tmp_path / f"prefs_{vault_mode}.json",
                vault=bad_vault,
            )
            for res in (
                bridge.sign_in("owner@example.com", PASSWORD, "BadBridge"),
                bridge.sign_in_owner("owner@example.com", PASSWORD),
            ):
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


def test_s8_delayed_rotation_success_after_sign_out_owner_revokes_new_session(
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

    def _delayed_rotate(hub_url, path, headers, *, client=None, timeout=8.0):
        res = real_post(hub_url, path, headers, client=client, timeout=timeout)
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
    signout_res = bridge.sign_out_owner()
    assert signout_res["ok"] is True
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None

    allow_return.set()
    t_rot.join(timeout=3.0)

    assert rot_error == ["AUTH-107"]
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
    assert bridge.owner_session_status()["hasOwnerSession"] is False
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
    signin = bridge.sign_in_owner("owner@example.com", PASSWORD, "WinnerPC")
    assert signin["ok"] is True
    winner_tok = read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault)
    assert winner_tok and winner_tok != old_session

    allow_reject.set()
    t_rot.join(timeout=3.0)

    assert rot_error == ["AUTH-107"]
    assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) == winner_tok
    assert bridge.owner_session_status()["hasOwnerSession"] is True
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
        assert bridge.owner_session_status()["hasOwnerSession"] is False
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

        rot_issued = threading.Event()
        allow_publish = threading.Event()
        issued_tokens: list[str] = []
        real_pub = bridge._publish_owner_session

        def _paused_publish(new_tok, *args, **kwargs):
            issued_tokens.append(new_tok)
            rot_issued.set()
            assert allow_publish.wait(timeout=4.0)
            return real_pub(new_tok, *args, **kwargs)

        monkeypatch.setattr(bridge, "_publish_owner_session", _paused_publish)

        rot_box: list[dict] = []
        t_rot = threading.Thread(target=lambda: rot_box.append(bridge.rotate_owner_session()))
        t_rot.start()
        assert rot_issued.wait(timeout=3.0)

        # Sign out clears locally first (sets signedOut & bumps generation), then logs out old_tok
        so_res = bridge.sign_out()
        assert so_res == {"ok": True}
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None

        allow_publish.set()
        t_rot.join(timeout=4.0)

        assert len(rot_box) == 1
        assert rot_box[0]["ok"] is False
        assert rot_box[0]["code"] == "AUTH-107"
        assert len(issued_tokens) == 1
        new_tok = issued_tokens[0]
        assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, MACHINE_TOKEN_VAULT_REF, vault=vault) is None
        assert read_vault_secret(db_path, PENDING_REVOKE_VAULT_REF, vault=vault) is None
        assert bridge.owner_session_status()["hasOwnerSession"] is False
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
            # 1) sign_out_owner offline leaves machine token intact; next sync_once flushes pending revoke
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
            so_owner = bridge.sign_out_owner()
            assert so_owner["ok"] is False
            assert so_owner["code"] == "NET-001"
            assert read_vault_secret(db_path, OWNER_SESSION_VAULT_REF, vault=vault) is None
            assert bridge.owner_session_status()["hasOwnerSession"] is False

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
            assert bridge.owner_session_status()["hasOwnerSession"] is False

            with connect(db_path) as conn:
                st = setup_wizard.read_setup_state(conn)
                rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
            assert st["role"] == "local"
            assert st["spokeConfig"]["enabled"] is False
            assert st["spokeConfig"].get("signedOut") is True
            assert tok2 not in " ".join(f"{k}:{v}" for k, v in rows)

            # Next hub contact via list_owner_sessions / sign_in flushes tok2 + machine revoke
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
