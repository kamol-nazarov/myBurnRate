"""Regression tests for 0.5.9 security findings S1, S2, S4, S6, and S7."""

from __future__ import annotations

import json
from pathlib import Path
import re
import threading
import time

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from fastapi.testclient import TestClient
import pytest

from spend_app import aggregate, auth_throttle, cli, setup_wizard, sync
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
from spend_app.error_codes import error_message
from spend_app.native_desktop import NativeClient
from spend_app.pricing import PricingEngine
from spend_app.wealthfront_window import DesktopBridge
from tests_spend.test_h5_desktop_account_ui import (
    _h5_api_js,
    _h5_settings_fixture,
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
