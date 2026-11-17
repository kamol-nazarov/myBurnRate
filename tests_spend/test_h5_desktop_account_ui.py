"""Comprehensive test suite for H5: Desktop account & computer management UI.

Covers:
1. ``DesktopBridge`` H5 methods (``spend_app/wealthfront_window.py``) backed by an
   in-process FastAPI hub ``TestClient`` + ``sync._HUB_TEST_CLIENTS`` and ``_MemoryVault``:
   - ``create_account``, ``sign_in``, ``sign_out``, ``change_password``,
     ``list_computers``, ``remove_computer``, background first sync & ``SYNC-201``,
     vault-only secret isolation, zero hub-URL/token leakage, and removal of
     ``open_owner_access``.
2. Headless Chrome UI tests (``spend_app/wealthfront_ui.html``):
   - Signed-out Sign in / Create account card, coded ``.h5-err`` banners,
     signed-in Computers list & inline Remove confirmation, Change password form,
     Sign out transition, shared Onboarding ``.h11-ob-account`` card, and XSS safety.
3. Responsive layout-overflow & theme tests across 304 / 336 / 380 px in both
   light and dark themes.
"""

from __future__ import annotations

import json
from pathlib import Path
import ssl
import threading
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient
import httpx
import pytest

from spend_app import aggregate, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.db import connect
from spend_app.error_codes import error_message
from spend_app.native_desktop import NativeClient
from spend_app.wealthfront_window import DesktopBridge
from tests_spend.test_h6_bridge_and_packaging import (
    HUB_URL,
    PASSWORD,
    SETUP_TOKEN,
    _MemoryVault,
    _create_paired_hub_and_spoke,
    _make_settings,
    _patch_native_to_local,
)
from tests_spend.test_wealthfront_ui import (
    DEFAULT_ONBOARDING,
    FAST,
    browser,
    build_page,
    demo_report,
    empty_report,
    needs_chrome,
    probe,
)


# ---------------------------------------------------------------------------
# Helpers & fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolated_h5_env(monkeypatch: pytest.MonkeyPatch):
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    sync._HUB_TEST_CLIENTS.clear()
    aggregate.reset_memo()
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOW_REGISTRATION", raising=False)
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


def _create_empty_hub_and_spoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[object, TestClient, object, object]:
    """Create a central hub with NO owner account yet, paired with a spoke."""
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP_TOKEN)
    hub_settings = _make_settings(tmp_path / "hub.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL)

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)

    spoke_settings = _make_settings(tmp_path / "spoke.db")
    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
    return hub_settings, hub_client, spoke_settings, spoke_app


def _make_bridge(
    tmp_path: Path,
    spoke_settings,
    *,
    vault: _MemoryVault | None = None,
    name: str = "prefs.json",
) -> DesktopBridge:
    return DesktopBridge(
        NativeClient(spoke_settings, port=17351),
        preferences=tmp_path / name,
        vault=vault if vault is not None else _MemoryVault(),
    )


def _assert_no_secrets_or_hub_url(obj: object, *, label: str = "payload") -> None:
    text = json.dumps(obj, sort_keys=True, default=str)
    assert "br_s_" not in text, f"owner session leaked in {label}: {text}"
    assert "br_m_" not in text, f"machine token leaked in {label}: {text}"
    assert HUB_URL not in text, f"hub URL leaked in {label}: {text}"
    assert "hub.test" not in text, f"hub host leaked in {label}: {text}"


def _wait_for_first_sync(bridge: DesktopBridge, *, timeout: float = 3.0) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with bridge._owner_session_lock:
            status = bridge._sync_status
        if status != "syncing":
            break
        time.sleep(0.01)
    with bridge._owner_session_lock:
        return {
            "status": bridge._sync_status,
            "code": bridge._sync_error_code,
            "message": bridge._sync_error_message,
        }


# ---------------------------------------------------------------------------
# 1. DesktopBridge H5 Methods (spend_app/wealthfront_window.py)
# ---------------------------------------------------------------------------


def test_h5_open_owner_access_removed_from_desktop_bridge():
    assert not hasattr(DesktopBridge, "open_owner_access")


def test_h5_create_account_registers_device_nonblocking_and_rejects_second_account(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_settings, hub_client, spoke_settings, _ = _create_empty_hub_and_spoke(
        tmp_path, monkeypatch
    )
    sync_started = threading.Event()
    release_sync = threading.Event()

    def _blocking_sync_once(*_args, **_kwargs):
        sync_started.set()
        release_sync.wait(timeout=5.0)
        return {"ok": True}

    monkeypatch.setattr(sync, "sync_once", _blocking_sync_once)

    vault = _MemoryVault()
    bridge = _make_bridge(tmp_path, spoke_settings, vault=vault)

    try:
        with hub_client:
            res = bridge.create_account("Owner@Example.com", PASSWORD, "Studio PC")
            assert res["ok"] is True, res
            assert res["computerName"] == "Studio PC"
            assert res["account"]["email"] == "owner@example.com"
            assert res["syncStarted"] is True
            assert res["syncState"] == {"status": "syncing", "code": None, "message": None}
            remote = [m for m in res["machines"] if m["id"] != "local"]
            assert len(remote) == 1
            assert remote[0]["name"] == "Studio PC"
            assert remote[0]["isCurrent"] is True
            assert res["machines"][0]["isCurrent"] is True
            # Returned immediately even before background sync completes
            assert not release_sync.is_set()
            assert sync_started.wait(timeout=2.0)

            # Verify hub persisted deviceId matching the spoke's device_id.v1
            spoke_dev_id = sync.get_or_create_device_id(spoke_settings.database_path)
            with connect(hub_settings.database_path) as conn:
                hub_state = sync._read_state(conn)
            mid = res["account"]["machineId"]
            assert hub_state["machines"][mid]["device_id"] == spoke_dev_id

            # Attempting a second account creation from another device fails with AUTH-103
            spoke2_settings = _make_settings(tmp_path / "spoke2.db")
            bridge2 = _make_bridge(
                tmp_path, spoke2_settings, vault=_MemoryVault(), name="prefs2.json"
            )
            dup = bridge2.create_account("second@example.com", PASSWORD, "Laptop")
            assert dup["ok"] is False
            assert dup["code"] == "AUTH-103"
            assert dup["message"]
            _assert_no_secrets_or_hub_url(dup, label="create_account duplicate")
    finally:
        release_sync.set()
        _wait_for_first_sync(bridge)


def test_h5_sign_in_reconnects_with_device_id_and_marks_current_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_settings, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})

    vault = _MemoryVault()
    bridge = _make_bridge(tmp_path, spoke_settings, vault=vault)

    with hub_client:
        res1 = bridge.sign_in("owner@example.com", PASSWORD, "Main Desk")
        assert res1["ok"] is True, res1
        assert res1["computerName"] == "Main Desk"
        assert res1["syncStarted"] is True
        assert res1["syncState"]["status"] == "syncing"
        remote1 = [m for m in res1["machines"] if m["id"] != "local"]
        assert len(remote1) == 1
        assert remote1[0]["isCurrent"] is True
        assert res1["machines"][0]["isCurrent"] is True
        first_mid = res1["account"]["machineId"]

        # Re-signing in from the same DB/deviceId reconnects the same machine record
        res2 = bridge.sign_in("owner@example.com", PASSWORD, "Main Desk Renamed")
        assert res2["ok"] is True, res2
        assert res2["account"]["machineId"] == first_mid
        remote2 = [m for m in res2["machines"] if m["id"] != "local"]
        assert len(remote2) == 1
        assert remote2[0]["name"] == "Main Desk Renamed"
        assert remote2[0]["isCurrent"] is True
        assert res2["machines"][0]["isCurrent"] is True
        _wait_for_first_sync(bridge)


def test_h5_sign_in_coded_errors(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
    bridge = _make_bridge(tmp_path, spoke_settings)

    with hub_client:
        # Bad email -> AUTH-106
        bad_email = bridge.sign_in("not-an-email@", PASSWORD, "PC")
        assert bad_email["ok"] is False
        assert bad_email["code"] == "AUTH-106"
        assert bad_email["message"]

        empty_email = bridge.sign_in("   ", PASSWORD, "PC")
        assert empty_email == {"ok": False, "code": "AUTH-106", "message": error_message("AUTH-106")}

        # Wrong password -> AUTH-101
        wrong_pw = bridge.sign_in("owner@example.com", "wrong-password-123", "PC")
        assert wrong_pw["ok"] is False
        assert wrong_pw["code"] == "AUTH-101"
        assert wrong_pw["message"]

    # Unknown / unconfigured hub -> NET-001
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    no_hub_res = bridge.sign_in("owner@example.com", PASSWORD, "PC")
    assert no_hub_res["ok"] is False
    assert no_hub_res["code"] == "NET-001"
    assert no_hub_res["message"]

    # Unreachable hub transport failure -> NET-001
    sync._HUB_TEST_CLIENTS.clear()
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)

    class _ConnectErrorClient:
        def __init__(self, *_a, **_kw):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *_a):
            return False

        def get(self, *_a, **_kw):
            raise httpx.ConnectError("connection refused to https://hub.test")

        def post(self, *_a, **_kw):
            raise httpx.ConnectError("connection refused to https://hub.test")

    monkeypatch.setattr(httpx, "Client", _ConnectErrorClient)
    net_res = bridge.sign_in("owner@example.com", PASSWORD, "PC")
    assert net_res["ok"] is False
    assert net_res["code"] == "NET-001"
    _assert_no_secrets_or_hub_url(net_res, label="NET-001 connect error")

    # Unverified TLS certificate transport error -> NET-003
    class _TlsErrorClient(_ConnectErrorClient):
        def get(self, *_a, **_kw):
            raise ssl.SSLCertVerificationError(1, "certificate verify failed for https://hub.test")

    monkeypatch.setattr(httpx, "Client", _TlsErrorClient)
    tls_res = bridge.sign_in("owner@example.com", PASSWORD, "PC")
    assert tls_res["ok"] is False
    assert tls_res["code"] == "NET-003"
    _assert_no_secrets_or_hub_url(tls_res, label="NET-003 tls error")

    # Non-HTTPS remote hub URL -> NET-003
    monkeypatch.setenv("BURNRATE_HUB_URL", "http://remote.example")
    non_https_res = bridge.sign_in("owner@example.com", PASSWORD, "PC")
    assert non_https_res["ok"] is False
    assert non_https_res["code"] == "NET-003"

    # Hub server HTTP 500 -> HUB-501
    err_app = FastAPI()

    @err_app.get("/api/hub/info")
    def _hub_info():
        return {"protocolVersion": 2}

    @err_app.post("/api/account/login")
    def _login_500():
        from fastapi.responses import JSONResponse

        return JSONResponse({"error": "database crashed"}, status_code=500)

    err_client = TestClient(err_app, base_url=HUB_URL)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, err_client)
    with err_client:
        srv_res = bridge.sign_in("owner@example.com", PASSWORD, "PC")
    assert srv_res["ok"] is False
    assert srv_res["code"] == "HUB-501"
    _assert_no_secrets_or_hub_url(srv_res, label="HUB-501")


def test_h5_vault_secret_isolation_and_zero_hub_url_or_token_leakage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    _patch_native_to_local(monkeypatch, spoke_app, port=17351)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})

    spoke2_settings = _make_settings(tmp_path / "spoke2.db")
    bridge2 = _make_bridge(tmp_path, spoke2_settings, vault=_MemoryVault(), name="p2.json")

    vault = _MemoryVault()
    bridge = _make_bridge(tmp_path, spoke_settings, vault=vault, name="p1.json")

    with hub_client:
        other = bridge2.sign_in("owner@example.com", PASSWORD, "Other Machine")
        assert other["ok"] is True, other
        _wait_for_first_sync(bridge2)
        other_id = other["account"]["machineId"]

        signed_in = bridge.sign_in("owner@example.com", PASSWORD, "Primary Machine")
        assert signed_in["ok"] is True, signed_in
        _wait_for_first_sync(bridge)
        _assert_no_secrets_or_hub_url(signed_in, label="sign_in")

        # 1. Both tokens exist ONLY in the vault
        owner_tok = vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)
        mach_tok = vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)
        assert owner_tok.startswith("br_s_")
        assert mach_tok.startswith("br_m_")

        # 2. Verify SQLite spokeConfig & all app_meta rows never store plaintext secrets
        with connect(spoke_settings.database_path) as conn:
            raw_row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_setup = json.loads(raw_row[0]) if raw_row else {}
            raw_spoke = raw_setup.get("spokeConfig") or {}
            assert "ownerSession" not in raw_spoke
            assert not raw_spoke.get("token")
            setup_state = setup_wizard.read_setup_state(conn)
            spoke_cfg = setup_state.get("spokeConfig") or {}
            assert "ownerSession" not in spoke_cfg
            assert not spoke_cfg.get("token")
            rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
        for k, v in rows:
            blob = f"{k}={v}"
            assert "br_s_" not in blob, f"owner session in app_meta[{k}]"
            assert "br_m_" not in blob, f"machine token in app_meta[{k}]"

        # 3. Every bridge return value stays free of br_s_, br_m_, and HUB_URL
        _assert_no_secrets_or_hub_url(bridge.list_computers(), label="list_computers")
        _assert_no_secrets_or_hub_url(bridge.get_settings_data(), label="get_settings_data")
        _assert_no_secrets_or_hub_url(bridge.get_onboarding_data(), label="get_onboarding_data")
        _assert_no_secrets_or_hub_url(
            bridge.change_password("wrong-pass-xyz", "new-password-1234"),
            label="change_password error",
        )
        _assert_no_secrets_or_hub_url(
            bridge.remove_computer(other_id), label="remove_computer"
        )
        _assert_no_secrets_or_hub_url(bridge.sign_out(), label="sign_out")


def test_h5_background_first_sync_nonblocking_and_sync_201_on_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    _patch_native_to_local(monkeypatch, spoke_app, port=17351)

    entered = threading.Event()
    proceed = threading.Event()

    def _failing_slow_sync(*_args, **_kwargs):
        entered.set()
        proceed.wait(timeout=5.0)
        raise RuntimeError(f"sync exploded talking to {HUB_URL}")

    monkeypatch.setattr(sync, "sync_once", _failing_slow_sync)

    bridge = _make_bridge(tmp_path, spoke_settings)
    try:
        with hub_client:
            res = bridge.sign_in("owner@example.com", PASSWORD, "Slow Sync PC")
            assert res["ok"] is True, res
            assert res["syncStarted"] is True
            assert res["syncState"] == {"status": "syncing", "code": None, "message": None}
            # Sign-in returned before sync_once was allowed to finish
            assert not proceed.is_set()
            assert entered.wait(timeout=2.0)

            proceed.set()
            final_sync = _wait_for_first_sync(bridge)
            assert final_sync["status"] == "error"
            assert final_sync["code"] == "SYNC-201"
            assert final_sync["message"]
            _assert_no_secrets_or_hub_url(final_sync, label="syncState")

            settings = bridge.get_settings_data()
            assert settings["ok"] is True
            assert settings["syncState"] == final_sync
    finally:
        proceed.set()
        _wait_for_first_sync(bridge)


def test_h5_change_password_validation_auth_and_subsequent_sign_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
    bridge = _make_bridge(tmp_path, spoke_settings)

    with hub_client:
        # Client-side validations
        assert bridge.change_password("", "new-password-1234")["code"] == "AUTH-101"
        assert bridge.change_password(PASSWORD, "short")["code"] == "AUTH-105"

        # Unauthenticated before sign-in
        unauth = bridge.change_password(PASSWORD, "new-password-1234")
        assert unauth["ok"] is False
        assert unauth["code"] == "AUTH-107"

        # Sign in first
        signed_in = bridge.sign_in("owner@example.com", PASSWORD, "Desk PC")
        assert signed_in["ok"] is True, signed_in
        _wait_for_first_sync(bridge)

        # Wrong current password -> AUTH-101
        wrong_cur = bridge.change_password("incorrect-current-pw", "new-password-1234")
        assert wrong_cur["ok"] is False
        assert wrong_cur["code"] == "AUTH-101"

        # Reset throttle before happy path so wrong-password attempt doesn't rate-limit
        auth_throttle.reset()

        new_pw = "brand-new-secure-password-456"
        ok_res = bridge.change_password(PASSWORD, new_pw)
        assert ok_res == {"ok": True}

        # Sign out, then verify old password fails and new password signs in cleanly
        assert bridge.sign_out() == {"ok": True}
        assert bridge.sign_in("owner@example.com", PASSWORD, "Desk PC")["code"] == "AUTH-101"
        relogin = bridge.sign_in("owner@example.com", new_pw, "Desk PC")
        assert relogin["ok"] is True, relogin
        _wait_for_first_sync(bridge)


def test_h5_list_computers_sorts_current_first_and_requires_auth(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})

    bridge = _make_bridge(tmp_path, spoke_settings)
    spoke_other = _make_settings(tmp_path / "other.db")
    other_bridge = _make_bridge(tmp_path, spoke_other, name="other_prefs.json")

    with hub_client:
        unauth = bridge.list_computers()
        assert unauth["ok"] is False
        assert unauth["code"] == "AUTH-107"

        assert other_bridge.sign_in("owner@example.com", PASSWORD, "Alpha Remote")["ok"] is True
        _wait_for_first_sync(other_bridge)

        # Current computer name sorts alphabetically AFTER "Alpha Remote", yet must be listed first
        res_in = bridge.sign_in("owner@example.com", PASSWORD, "Zulu Current")
        assert res_in["ok"] is True, res_in
        _wait_for_first_sync(bridge)

        listed = bridge.list_computers()
        assert listed["ok"] is True, listed
        remote = [m for m in listed["machines"] if m["id"] != "local"]
        names = [m["name"] for m in remote]
        assert names == ["Zulu Current", "Alpha Remote"]
        assert listed["machines"][0]["isCurrent"] is True
        assert listed["machines"][0]["name"] == "Zulu Current"
        assert remote[0]["isCurrent"] is True
        assert remote[1]["isCurrent"] is False


def test_h5_remove_computer_revokes_other_and_refuses_current_local_all(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    hub_settings, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(
        tmp_path, monkeypatch
    )
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})

    spoke_other = _make_settings(tmp_path / "other.db")
    other_vault = _MemoryVault()
    other_bridge = _make_bridge(tmp_path, spoke_other, vault=other_vault, name="other.json")

    vault = _MemoryVault()
    bridge = _make_bridge(tmp_path, spoke_settings, vault=vault)

    with hub_client:
        other_res = other_bridge.sign_in("owner@example.com", PASSWORD, "Laptop-2")
        assert other_res["ok"] is True, other_res
        _wait_for_first_sync(other_bridge)
        other_id = other_res["account"]["machineId"]
        other_machine_tok = other_vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)

        cur_res = bridge.sign_in("owner@example.com", PASSWORD, "Workstation-1")
        assert cur_res["ok"] is True, cur_res
        _wait_for_first_sync(bridge)
        cur_id = cur_res["account"]["machineId"]

        # Refuses removing empty, "local", "all", or current computer with DEV-302
        for bad_target in ("", "local", "all", cur_id):
            err = bridge.remove_computer(bad_target)
            assert err["ok"] is False, (bad_target, err)
            assert err["code"] == "DEV-302"
            assert err["message"]

        # Revoking other computer succeeds and returns updated active machines list
        removed = bridge.remove_computer(other_id)
        assert removed["ok"] is True, removed
        rem_ids = [m["id"] for m in removed["machines"] if m["id"] != "local"]
        assert rem_ids == [cur_id]
        assert other_id not in [m["id"] for m in removed["machines"]]
        assert removed["machines"][0]["isCurrent"] is True

        # Revoked machine token no longer authenticates against hub
        assert (
            sync.authenticate_machine_token(hub_settings.database_path, other_machine_tok)
            is None
        )


def test_h5_sign_out_revokes_session_clears_vault_disables_spoke_and_handles_offline(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})

    vault = _MemoryVault()
    bridge = _make_bridge(tmp_path, spoke_settings, vault=vault)

    with hub_client:
        res = bridge.sign_in("owner@example.com", PASSWORD, "Desk PC")
        assert res["ok"] is True, res
        _wait_for_first_sync(bridge)
        owner_tok = vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)

        # If hub becomes unreachable while signed in, sign_out clears local state first,
        # queues pending hub revocation in the vault, and returns NET-001.
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
        offline_out = bridge.sign_out()
        assert offline_out["ok"] is False
        assert offline_out["code"] == "NET-001"
        assert DesktopBridge.OWNER_SESSION_VAULT_REF not in vault.items
        assert DesktopBridge.MACHINE_TOKEN_VAULT_REF not in vault.items
        assert DesktopBridge.PENDING_REVOKE_VAULT_REF in vault.items

        # Restore hub reachability and sign out cleanly (flushes pending revoke)
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
        ok_out = bridge.sign_out()
        assert ok_out == {"ok": True}
        assert DesktopBridge.PENDING_REVOKE_VAULT_REF not in vault.items

        # Vault & in-memory secrets cleared
        assert DesktopBridge.OWNER_SESSION_VAULT_REF not in vault.items
        assert DesktopBridge.MACHINE_TOKEN_VAULT_REF not in vault.items
        assert bridge._owner_session is None
        assert bridge._machine_token is None

        # Spoke state disabled & returned to local role
        with connect(spoke_settings.database_path) as conn:
            raw_row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_st = json.loads(raw_row[0]) if raw_row else {}
            st = setup_wizard.read_setup_state(conn)
        assert raw_st.get("account") == {}
        assert "ownerSession" not in (raw_st.get("spokeConfig") or {})
        assert not (raw_st.get("spokeConfig") or {}).get("token")
        assert st["role"] == "local"
        assert st["account"]["username"] == ""
        assert st["account"]["machineId"] == ""
        assert st["spokeConfig"]["enabled"] is False
        assert "ownerSession" not in st["spokeConfig"]
        assert not st["spokeConfig"].get("token")

        # Hub session token has been revoked
        auth_check = hub_client.get(
            "/api/machines",
            headers={"Authorization": f"Bearer {owner_tok}", "X-BURNRATE-Request": "1"},
        )
        assert auth_check.status_code == 401


def test_h5_vault_only_end_to_end_sign_in_sync_rotate_sign_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from spend_app.connection_paths import LocationError
    from spend_app.connection_secrets import get_vault
    from spend_app.pricing import PricingEngine

    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)

    # No explicit vault= passed: uses conftest's injected vault factory directly.
    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17351),
        preferences=tmp_path / "prefs-e2e.json",
    )

    with hub_client:
        res = bridge.sign_in("owner@example.com", PASSWORD, "E2E Desk")
        assert res["ok"] is True, res
        sync_state = _wait_for_first_sync(bridge)
        assert sync_state["status"] == "ok", sync_state

        vault = get_vault(spoke_settings.database_path)
        sess_tok = vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)
        mach_tok_1 = vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)
        assert sess_tok.startswith("br_s_")
        assert mach_tok_1.startswith("br_m_")

        # SQLite setup state & app_meta must NOT store plaintext tokens
        with connect(spoke_settings.database_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            raw_setup_row = conn.execute(
                "SELECT value FROM app_meta WHERE key = ?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_spoke = (json.loads(raw_setup_row[0]) if raw_setup_row else {}).get("spokeConfig") or {}
            raw_rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
        assert not raw_spoke.get("token")
        assert "ownerSession" not in raw_spoke
        assert not st["spokeConfig"].get("token")
        assert not st["spokeConfig"].get("ownerSession")
        joined_meta = json.dumps([(r[0], r[1]) for r in raw_rows])
        assert mach_tok_1 not in joined_meta
        assert sess_tok not in joined_meta

        # Background scheduler sync reads the machine token from the vault
        bg_res = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine([]), client=hub_client
        )
        assert bg_res is not None and bg_res.get("ok") is True, bg_res

        # Rotation updates the vault and leaves DB free of plaintext token
        rot_res = setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client)
        assert rot_res is not None and rot_res.get("ok") is True, rot_res
        mach_tok_2 = vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)
        assert mach_tok_2.startswith("br_m_")
        assert mach_tok_2 != mach_tok_1
        with connect(spoke_settings.database_path) as conn:
            st_after_rot = setup_wizard.read_setup_state(conn)
        assert not st_after_rot["spokeConfig"].get("token")

        # Sync works with the rotated token from the vault
        bg_res_2 = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine([]), client=hub_client
        )
        assert bg_res_2 is not None and bg_res_2.get("ok") is True, bg_res_2

        # Sign out clears vault tokens completely
        out_res = bridge.sign_out()
        assert out_res == {"ok": True}
        with pytest.raises(LocationError):
            vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)
        with pytest.raises(LocationError):
            vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)


def test_h5_failing_vault_returns_app_903_and_preserves_tokens(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from spend_app.connection_paths import LocationError
    from spend_app.connection_secrets import get_vault, set_vault_factory

    class _RefusingPutVault:
        def available(self) -> bool:
            return True

        def put(self, secret: str, ref: str | None = None) -> str:
            raise LocationError("Keychain refused write")

        def get(self, ref: str) -> str:
            raise LocationError("missing")

        def delete(self, ref: str) -> None:
            pass

    class _MismatchReadbackVault:
        def __init__(self, store: dict[str, str]):
            self._store = store

        def available(self) -> bool:
            return True

        def put(self, secret: str, ref: str | None = None) -> str:
            # Pretend write succeeded without persisting the new value
            return ref or "burnrate-x"

        def get(self, ref: str) -> str:
            if ref not in self._store:
                raise LocationError("missing")
            return self._store[ref]

        def delete(self, ref: str) -> None:
            self._store.pop(ref, None)

    # 1. Failing vault on create_account -> APP-903, stays local, no plaintext file
    _, empty_hub_client, spoke_empty, _ = _create_empty_hub_and_spoke(tmp_path / "empty", monkeypatch)
    prev_factory = set_vault_factory(lambda _db: _RefusingPutVault())
    try:
        with empty_hub_client:
            b_empty = DesktopBridge(
                NativeClient(spoke_empty, port=17352),
                preferences=tmp_path / "empty" / "prefs.json",
            )
            res_create = b_empty.create_account("owner@example.com", PASSWORD, "Locked Box")
            assert res_create["ok"] is False
            assert res_create["code"] == "APP-903"
            assert "keychain" in res_create["message"].lower()
            _assert_no_secrets_or_hub_url(res_create, label="create_account APP-903")
            with connect(spoke_empty.database_path) as conn:
                st = setup_wizard.read_setup_state(conn)
            assert st["role"] != "spoke"
            assert st["spokeConfig"]["enabled"] is False
    finally:
        set_vault_factory(prev_factory)

    # 2. Mismatched readback on sign_in -> APP-903, does not save partial owner session
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path / "paired", monkeypatch)
    mismatch_store: dict[str, str] = {}
    prev_factory = set_vault_factory(lambda _db: _MismatchReadbackVault(mismatch_store))
    try:
        with hub_client:
            b_sign = DesktopBridge(
                NativeClient(spoke_settings, port=17353),
                preferences=tmp_path / "paired" / "prefs.json",
            )
            res_signin = b_sign.sign_in("owner@example.com", PASSWORD, "Mismatch Box")
            assert res_signin["ok"] is False
            assert res_signin["code"] == "APP-903"
            assert "keychain" in res_signin["message"].lower()
            assert b_sign._owner_session_status()["hasOwnerSession"] is False
            assert mismatch_store == {}
    finally:
        set_vault_factory(prev_factory)

    # 3. Partial write failure (session succeeds, machine token fails) rolls back session
    class _FailMachineTokenOnlyVault:
        def __init__(self):
            self.items: dict[str, str] = {}

        def put(self, secret: str, ref: str = "") -> str:
            if ref == DesktopBridge.MACHINE_TOKEN_VAULT_REF:
                raise LocationError("refused machine token")
            self.items[ref] = secret
            return ref

        def get(self, ref: str) -> str:
            if ref not in self.items:
                raise LocationError("missing")
            return self.items[ref]

        def delete(self, ref: str) -> None:
            self.items.pop(ref, None)

    partial_vault = _FailMachineTokenOnlyVault()
    with hub_client:
        b_partial = DesktopBridge(
            NativeClient(spoke_settings, port=17354),
            preferences=tmp_path / "paired" / "prefs_partial.json",
            vault=partial_vault,
        )
        res_partial = b_partial.sign_in("owner@example.com", PASSWORD, "Partial Box")
        assert res_partial["ok"] is False
        assert res_partial["code"] == "APP-903"
        assert DesktopBridge.OWNER_SESSION_VAULT_REF not in partial_vault.items
        assert b_partial._owner_session_status()["hasOwnerSession"] is False

    # 4. Failing vault on rotate_spoke_machine_token preserves old credential & cursor locally
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
    with hub_client:
        b_ok = DesktopBridge(
            NativeClient(spoke_settings, port=17355),
            preferences=tmp_path / "paired" / "prefs_ok.json",
        )
        res_ok = b_ok.sign_in("owner@example.com", PASSWORD, "Rotate Box")
        assert res_ok["ok"] is True, res_ok
        _wait_for_first_sync(b_ok)

        real_vault = get_vault(spoke_settings.database_path)
        old_tok = real_vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF)
        before_cursor = sync.read_sync_cursor(spoke_settings.database_path, HUB_URL)

        # Install vault that rejects replacing the existing token (preserving old_tok in store)
        backing_store = {
            DesktopBridge.MACHINE_TOKEN_VAULT_REF: old_tok,
            DesktopBridge.OWNER_SESSION_VAULT_REF: real_vault.get(
                DesktopBridge.OWNER_SESSION_VAULT_REF
            ),
        }
        prev_factory = set_vault_factory(lambda _db: _MismatchReadbackVault(backing_store))
        try:
            rot_fail = setup_wizard.rotate_spoke_machine_token(spoke_settings, client=hub_client)
            assert rot_fail is not None
            assert rot_fail["ok"] is False
            assert rot_fail["code"] == "APP-903"
            after_cursor = sync.read_sync_cursor(spoke_settings.database_path, HUB_URL)
            assert after_cursor == before_cursor
            assert backing_store[DesktopBridge.MACHINE_TOKEN_VAULT_REF] == old_tok
            with connect(spoke_settings.database_path) as conn:
                st_rot = setup_wizard.read_setup_state(conn)
            assert not st_rot["spokeConfig"].get("token")
        finally:
            set_vault_factory(prev_factory)


def test_h5_legacy_sqlite_secrets_migrate_on_startup_and_fallback_when_vault_unavailable(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from spend_app.connection_secrets import get_vault, set_vault_factory
    from spend_app.pricing import PricingEngine

    # Case A: Vault available -> DesktopBridge startup migrates token & ownerSession out of DB
    hub_settings, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(
        tmp_path / "mig_ok", monkeypatch
    )
    reg = sync.register_machine(
        hub_settings.database_path,
        name="Pre-0.5.9 PC",
        os_name="win32",
        device_id="dev-legacy-mig-01",
    )
    legacy_token = reg["token"]
    legacy_session = "br_s_legacy_owner_session_token"
    with connect(spoke_settings.database_path) as conn:
        st = setup_wizard.default_setup_state()
        st["role"] = "spoke"
        st["computerName"] = "Pre-0.5.9 PC"
        st["account"]["machineId"] = reg["machine"]["id"]
        st["spokeConfig"].update(
            hubUrl=HUB_URL,
            token=legacy_token,
            ownerSession=legacy_session,
            enabled=True,
        )
        setup_wizard.write_setup_state(conn, st)

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17356),
        preferences=tmp_path / "mig_ok" / "prefs.json",
    )
    vault = get_vault(spoke_settings.database_path)
    assert vault.get(DesktopBridge.MACHINE_TOKEN_VAULT_REF) == legacy_token
    assert vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF) == legacy_session
    with connect(spoke_settings.database_path) as conn:
        raw_mig_row = conn.execute(
            "SELECT value FROM app_meta WHERE key = ?", (setup_wizard.SETUP_META_KEY,)
        ).fetchone()
        raw_mig_spoke = (json.loads(raw_mig_row[0]) if raw_mig_row else {}).get("spokeConfig") or {}
        migrated_st = setup_wizard.read_setup_state(conn)
    assert "token" not in raw_mig_spoke
    assert "ownerSession" not in raw_mig_spoke
    assert not migrated_st["spokeConfig"].get("token")
    assert not migrated_st["spokeConfig"].get("ownerSession")
    assert bridge._get_machine_token() == legacy_token

    with hub_client:
        sync_res = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine([]), client=hub_client
        )
        assert sync_res is not None and sync_res.get("ok") is True, sync_res

    # Case B: Vault unavailable -> leave secrets in SQLite DB and keep syncing
    hub_b, hub_client_b, spoke_b, _ = _create_paired_hub_and_spoke(
        tmp_path / "mig_no_vault", monkeypatch
    )
    reg_b = sync.register_machine(
        hub_b.database_path,
        name="No-Keychain PC",
        os_name="linux",
        device_id="dev-legacy-mig-02",
    )
    legacy_token_b = reg_b["token"]
    legacy_session_b = "br_s_legacy_no_vault_session"
    with connect(spoke_b.database_path) as conn:
        st_b = setup_wizard.default_setup_state()
        st_b["role"] = "spoke"
        st_b["computerName"] = "No-Keychain PC"
        st_b["account"]["machineId"] = reg_b["machine"]["id"]
        st_b["spokeConfig"].update(
            hubUrl=HUB_URL,
            token=legacy_token_b,
            ownerSession=legacy_session_b,
            enabled=True,
        )
        setup_wizard.write_setup_state(conn, st_b)

    prev_factory = set_vault_factory(lambda _db: None)
    try:
        bridge_b = DesktopBridge(
            NativeClient(spoke_b, port=17357),
            preferences=tmp_path / "mig_no_vault" / "prefs.json",
        )
        with hub_client_b:
            sync_res_b = setup_wizard.maybe_run_configured_spoke_sync(
                spoke_b, PricingEngine([]), client=hub_client_b
            )
            assert sync_res_b is not None and sync_res_b.get("ok") is True, sync_res_b
        with connect(spoke_b.database_path) as conn:
            kept_st = setup_wizard.read_setup_state(conn)
        assert kept_st["spokeConfig"].get("token") == legacy_token_b
        assert kept_st["spokeConfig"].get("ownerSession") == legacy_session_b
        assert bridge_b._get_machine_token() == legacy_token_b
    finally:
        set_vault_factory(prev_factory)


# ---------------------------------------------------------------------------
# 2. Desktop UI States (spend_app/wealthfront_ui.html via Headless Chrome)
# ---------------------------------------------------------------------------


def _json_js(value: object) -> str:
    return json.dumps(value).replace("</", "<\\/")


def _h5_settings_fixture(
    *,
    signed_in: bool = False,
    email: str = "owner@example.com",
    computer_name: str = "Desk PC",
    machines: list[dict] | None = None,
    sync_state: dict | None = None,
) -> dict:
    if machines is None:
        machines = (
            [
                {
                    "id": "m-cur",
                    "name": computer_name,
                    "os": "win32",
                    "online": True,
                    "isCurrent": True,
                    "lastSeenAt": "2026-11-16T12:00:00Z",
                },
                {
                    "id": "m-other",
                    "name": "MacBook Pro",
                    "os": "darwin",
                    "online": False,
                    "isCurrent": False,
                    "lastSeenAt": "2026-11-16T10:00:00Z",
                },
            ]
            if signed_in
            else []
        )
    return {
        "ok": True,
        "connections": [],
        "machines": machines,
        "setup": {
            "account": (
                {"username": email, "email": email, "machineId": "m-cur"}
                if signed_in
                else {}
            ),
            "spokeConfig": {
                "enabled": signed_in,
                "hubConfigured": signed_in,
                "hasOwnerSession": signed_in,
            },
            "computerName": computer_name,
        },
        "hasOwnerSession": signed_in,
        "accountEmail": email if signed_in else "",
        "computerName": computer_name,
        "syncState": sync_state or {"status": "idle", "code": None, "message": None},
        "hubAvailable": True,
    }


def _h5_onboarding_fixture(
    *,
    signed_in: bool = False,
    email: str = "owner@example.com",
    computer_name: str = "Desk PC",
    machines: list[dict] | None = None,
) -> dict:
    out = json.loads(json.dumps(DEFAULT_ONBOARDING))
    out["hubConnected"] = signed_in
    out["hasOwnerSession"] = signed_in
    out["accountEmail"] = email if signed_in else ""
    out["computerName"] = computer_name
    out["machines"] = machines or []
    return out


def _h5_scan_fixture() -> dict:
    return {
        "ok": True,
        "lanes": [
            {
                "key": "claude-code",
                "name": "Claude",
                "plan": "Claude Max 20x",
                "tool": "claude-code",
                "usage": {"state": "ok", "text": "Reading ~/.claude"},
                "meter": {"state": "ok", "text": "72% remaining (5h)", "detail": None, "fix": None},
                "consent": None,
            }
        ],
        "usageOnly": [],
        "notFound": [],
        "consent": [],
        "progress": {"heartbeat": {}, "sources": []},
        "sources": [],
        "sourceDetails": {},
        "checkedAt": "2026-11-16T12:00:00Z",
    }


def _h5_api_js(
    *,
    settings: dict,
    onboarding: dict | None = None,
    scan: dict | None = None,
    sign_in_res: dict | None = None,
    create_account_res: dict | None = None,
    sign_out_res: dict | None = None,
    change_password_res: dict | None = None,
    remove_computer_res: dict | None = None,
    extra_js: str = "",
) -> str:
    onb_data = onboarding if onboarding is not None else _h5_onboarding_fixture()
    scan_data = scan if scan is not None else _h5_scan_fixture()
    return f"""
    window.__h5Settings = {_json_js(settings)};
    window.__h5Onboarding = {_json_js(onb_data)};
    window.__h5Scan = {_json_js(scan_data)};
    window.__h5SignInRes = {_json_js(sign_in_res)};
    window.__h5CreateAccountRes = {_json_js(create_account_res)};
    window.__h5SignOutRes = {_json_js(sign_out_res)};
    window.__h5ChangePwRes = {_json_js(change_password_res)};
    window.__h5RemoveComputerRes = {_json_js(remove_computer_res)};

    api.get_settings_data = async function () {{
      window.__calls.push(["get_settings_data"]);
      return JSON.parse(JSON.stringify(window.__h5Settings));
    }};
    api.get_onboarding_data = async function () {{
      window.__calls.push(["get_onboarding_data"]);
      return JSON.parse(JSON.stringify(window.__h5Onboarding));
    }};
    api.get_setup_scan = async function () {{
      window.__calls.push(["get_setup_scan"]);
      return JSON.parse(JSON.stringify(window.__h5Scan));
    }};
    api.sign_in = async function (email, pw, comp) {{
      window.__calls.push(["sign_in", email, pw, comp]);
      if (window.__h5SignInRes) return JSON.parse(JSON.stringify(window.__h5SignInRes));
      var cname = comp || window.__h5Settings.computerName || "Desk PC";
      var machs = [
        {{ id: "m-cur", name: cname, os: "win32", online: true, isCurrent: true, lastSeenAt: "2026-11-16T12:00:00Z" }},
        {{ id: "m-other", name: "MacBook Pro", os: "darwin", online: false, isCurrent: false, lastSeenAt: "2026-11-16T10:00:00Z" }}
      ];
      window.__h5Settings = Object.assign({{}}, window.__h5Settings, {{
        hasOwnerSession: true,
        accountEmail: email,
        computerName: cname,
        machines: machs,
        setup: Object.assign({{}}, window.__h5Settings.setup || {{}}, {{
          account: {{ username: email, email: email, machineId: "m-cur" }},
          computerName: cname,
          spokeConfig: {{ enabled: true, hubConfigured: true, hasOwnerSession: true }}
        }})
      }});
      window.__h5Onboarding = Object.assign({{}}, window.__h5Onboarding, {{
        hubConnected: true,
        hasOwnerSession: true,
        accountEmail: email,
        computerName: cname,
        machines: machs
      }});
      return {{
        ok: true,
        account: {{ username: email, email: email, machineId: "m-cur" }},
        computerName: cname,
        machines: machs,
        syncStarted: false,
        syncState: {{ status: "ok", code: null, message: null }}
      }};
    }};
    api.create_account = async function (email, pw, comp) {{
      window.__calls.push(["create_account", email, pw, comp]);
      if (window.__h5CreateAccountRes) return JSON.parse(JSON.stringify(window.__h5CreateAccountRes));
      var cname = comp || window.__h5Settings.computerName || "Desk PC";
      var machs = [
        {{ id: "m-cur", name: cname, os: "win32", online: true, isCurrent: true, lastSeenAt: "2026-11-16T12:00:00Z" }}
      ];
      window.__h5Settings = Object.assign({{}}, window.__h5Settings, {{
        hasOwnerSession: true,
        accountEmail: email,
        computerName: cname,
        machines: machs,
        setup: Object.assign({{}}, window.__h5Settings.setup || {{}}, {{
          account: {{ username: email, email: email, machineId: "m-cur" }},
          computerName: cname,
          spokeConfig: {{ enabled: true, hubConfigured: true, hasOwnerSession: true }}
        }})
      }});
      return {{
        ok: true,
        account: {{ username: email, email: email, machineId: "m-cur" }},
        computerName: cname,
        machines: machs,
        syncStarted: false,
        syncState: {{ status: "ok", code: null, message: null }}
      }};
    }};
    api.sign_out = async function () {{
      window.__calls.push(["sign_out"]);
      if (window.__h5SignOutRes) return JSON.parse(JSON.stringify(window.__h5SignOutRes));
      window.__h5Settings = Object.assign({{}}, window.__h5Settings, {{
        hasOwnerSession: false,
        accountEmail: "",
        machines: [],
        syncState: {{ status: "idle", code: null, message: null }},
        setup: Object.assign({{}}, window.__h5Settings.setup || {{}}, {{
          account: {{}},
          spokeConfig: {{ enabled: false, hubConfigured: false, hasOwnerSession: false }}
        }})
      }});
      window.__h5Onboarding = Object.assign({{}}, window.__h5Onboarding, {{
        hubConnected: false,
        hasOwnerSession: false,
        accountEmail: "",
        machines: []
      }});
      return {{ ok: true }};
    }};
    api.change_password = async function (cur, nw) {{
      window.__calls.push(["change_password", cur, nw]);
      if (window.__h5ChangePwRes) return JSON.parse(JSON.stringify(window.__h5ChangePwRes));
      return {{ ok: true }};
    }};
    api.list_computers = async function () {{
      window.__calls.push(["list_computers"]);
      return {{ ok: true, machines: window.__h5Settings.machines || [] }};
    }};
    api.remove_computer = async function (mid) {{
      window.__calls.push(["remove_computer", mid]);
      if (window.__h5RemoveComputerRes) return JSON.parse(JSON.stringify(window.__h5RemoveComputerRes));
      var remaining = (window.__h5Settings.machines || []).filter(function (m) {{ return m.id !== mid; }});
      window.__h5Settings.machines = remaining;
      return {{ ok: true, machines: remaining }};
    }};
    api.trigger_sync = async function () {{
      window.__calls.push(["trigger_sync"]);
      return {{ ok: true, results: [] }};
    }};
    {extra_js}
    """


@needs_chrome
@browser
def test_h5_ui_signed_out_tabs_prefill_validation_submit_and_no_legacy_owner_links(
    tmp_path: Path,
):
    settings = _h5_settings_fixture(signed_in=False, computer_name="Studio-Workstation")
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.getElementById('hubAccountUser'));
    await wait(30);

    const out = {};
    out.compPrefill = document.getElementById('hubAccountComp').value;
    out.loginTabSelected = document.getElementById('authMode_login').getAttribute('aria-selected');
    out.loginBtnText = document.getElementById('hubSignInBtn').textContent.trim();
    const initialBody = document.getElementById('sheetBody').textContent;
    out.hasLegacyOwnerLink = /owner access/i.test(initialBody);
    out.hasHubAddr = /https?:\\/\\//i.test(initialBody) || /hub\\.test/i.test(initialBody);
    out.hasSyncCardWhenSignedOut = !!document.getElementById('h11SyncCard');

    // Switch to Create account tab -> button label updates & 12+ char validation emits AUTH-105
    document.getElementById('authMode_register').click();
    await wait(20);
    out.regTabSelected = document.getElementById('authMode_register').getAttribute('aria-selected');
    out.regBtnText = document.getElementById('hubSignInBtn').textContent.trim();

    document.getElementById('hubAccountUser').value = 'alice@example.com';
    document.getElementById('hubAccountPass').value = 'short123';
    document.getElementById('hubSignInBtn').click();
    await wait(20);
    const shortErr = document.querySelector('#h5AccountCard .h5-err');
    out.shortErrText = shortErr ? shortErr.textContent.replace(/\\s+/g, ' ').trim() : '';
    out.createCallsAfterShort = window.__calls.filter(c => c[0] === 'create_account').length;

    // Submit valid registration
    document.getElementById('hubAccountUser').value = 'alice@example.com';
    document.getElementById('hubAccountPass').value = 'long-valid-password-123';
    document.getElementById('hubAccountComp').value = 'Studio-Box';
    document.getElementById('hubSignInBtn').click();
    await until(() => document.getElementById('h5SignOutBtn'));
    await wait(30);
    out.createCalls = window.__calls.filter(c => c[0] === 'create_account');
    out.signedInText = document.getElementById('h5AccountCard').textContent.replace(/\\s+/g, ' ').trim();

    // Sign out, then verify Sign in flow triggers api.sign_in
    document.getElementById('h5SignOutBtn').click();
    await until(() => document.getElementById('hubAccountUser'));
    await wait(30);
    document.getElementById('authMode_login').click();
    await wait(20);
    document.getElementById('hubAccountUser').value = 'bob@example.com';
    document.getElementById('hubAccountPass').value = 'long-valid-password-123';
    document.getElementById('hubAccountComp').value = 'Bob-PC';
    document.getElementById('hubSignInBtn').click();
    await until(() => document.getElementById('h5SignOutBtn'));
    await wait(30);
    out.signInCalls = window.__calls.filter(c => c[0] === 'sign_in');
    out.bobBanner = document.getElementById('h5AccountCard').textContent.replace(/\\s+/g, ' ').trim();
    report(out);
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=settings,
        api_js=_h5_api_js(settings=settings),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=820)
    assert r["compPrefill"] == "Studio-Workstation"
    assert r["loginTabSelected"] == "true"
    assert r["loginBtnText"] == "Sign in"
    assert r["hasLegacyOwnerLink"] is False
    assert r["hasHubAddr"] is False
    assert r["hasSyncCardWhenSignedOut"] is False
    assert r["regTabSelected"] == "true"
    assert r["regBtnText"] == "Create account"
    assert "Error AUTH-105" in r["shortErrText"]
    assert r["createCallsAfterShort"] == 0
    assert r["createCalls"] == [
        ["create_account", "alice@example.com", "long-valid-password-123", "Studio-Box"]
    ]
    assert "Signed in as alice@example.com" in r["signedInText"]
    assert r["signInCalls"] == [
        ["sign_in", "bob@example.com", "long-valid-password-123", "Bob-PC"]
    ]
    assert "Signed in as bob@example.com" in r["bobBanner"]


@needs_chrome
@browser
@pytest.mark.parametrize(
    ("code", "msg", "mode"),
    [
        ("AUTH-101", "That email and password don't match.", "login"),
        ("NET-001", "Can't reach your hub. Check that this computer is on your network.", "login"),
        ("AUTH-103", "An account already exists. Sign in instead.", "register"),
    ],
)
def test_h5_ui_coded_error_banners_signed_out(tmp_path: Path, code: str, msg: str, mode: str):
    settings = _h5_settings_fixture(signed_in=False)
    err_payload = {"ok": False, "code": code, "message": msg}
    probe_js = f"""
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.getElementById('hubAccountUser'));
    setAccountAuthMode({json.dumps(mode)});
    await wait(20);
    document.getElementById('hubAccountUser').value = 'owner@example.com';
    document.getElementById('hubAccountPass').value = 'very-long-password-123';
    document.getElementById('hubSignInBtn').click();
    await until(() => document.querySelector('#h5AccountCard .h5-err'));
    const errEl = document.querySelector('#h5AccountCard .h5-err');
    report({{
      bold: errEl.querySelector('b') ? errEl.querySelector('b').textContent.trim() : '',
      tail: errEl.querySelector('span') ? errEl.querySelector('span').textContent.trim() : '',
      full: errEl.textContent.replace(/\\s+/g, ' ').trim(),
    }});
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=settings,
        api_js=_h5_api_js(
            settings=settings,
            sign_in_res=err_payload,
            create_account_res=err_payload,
        ),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=820)
    assert r["bold"] == msg
    assert f"Error {code}" in r["tail"]
    assert f"Error {code}" in r["full"]


@needs_chrome
@browser
def test_h5_ui_sync_201_banner_shown_when_signed_in(tmp_path: Path):
    settings = _h5_settings_fixture(
        signed_in=True,
        email="owner@example.com",
        sync_state={
            "status": "error",
            "code": "SYNC-201",
            "message": "Signed in, but the first sync didn't finish. It will retry.",
        },
    )
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.querySelector('#h5AccountCard .h5-err'));
    const errEl = document.querySelector('#h5AccountCard .h5-err');
    const badge = document.querySelector('#h5AccountCard .state-badge.warn');
    report({
      badge: badge ? badge.textContent.trim() : '',
      bold: errEl.querySelector('b').textContent.trim(),
      tail: errEl.querySelector('span').textContent.trim(),
    });
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=settings,
        api_js=_h5_api_js(settings=settings),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=820)
    assert r["badge"] == "Sync issue"
    assert "first sync didn't finish" in r["bold"]
    assert "Error SYNC-201" in r["tail"]


@needs_chrome
@browser
def test_h5_ui_signed_in_computers_and_inline_remove_flow(tmp_path: Path):
    settings = _h5_settings_fixture(signed_in=True, email="owner@example.com", computer_name="Desk PC")
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.querySelector('#h5AccountCard .h5-comp-row'));
    await wait(30);

    const out = {};
    out.signedInHint = document.querySelector('#h5AccountCard .h5-hint').textContent.trim();
    const curRow = document.querySelector('[data-machine-id="m-cur"]');
    const otherRow = document.querySelector('[data-machine-id="m-other"]');
    out.curBadge = curRow.querySelector('.state-badge.ok') ? curRow.querySelector('.state-badge.ok').textContent.trim() : '';
    out.curMeta = curRow.querySelector('.h5-comp-meta') ? curRow.querySelector('.h5-comp-meta').textContent.trim() : '';
    out.hasSyncCardWhenSignedIn = !!document.getElementById('h11SyncCard');
    out.curHasRemove = !!curRow.querySelector('[data-h5-remove-ask]');
    out.otherHasRemove = !!otherRow.querySelector('[data-h5-remove-ask="m-other"]');

    // Click Remove -> opens inline confirmation; Cancel closes without calling API
    otherRow.querySelector('[data-h5-remove-ask="m-other"]').click();
    await until(() => document.querySelector('.h5-comp-confirm'));
    out.confirmPrompt = document.querySelector('.h5-comp-confirm span').textContent.trim();
    document.querySelector('.h5-comp-confirm button.btn-secondary').click();
    await wait(20);
    out.confirmClosedOnCancel = !document.querySelector('.h5-comp-confirm');
    out.removeCallsAfterCancel = window.__calls.filter(c => c[0] === 'remove_computer').length;

    // Click Remove -> Confirm invokes remove_computer('m-other') and updates list
    document.querySelector('[data-h5-remove-ask="m-other"]').click();
    await until(() => document.querySelector('[data-h5-remove-confirm="m-other"]'));
    document.querySelector('[data-h5-remove-confirm="m-other"]').click();
    await until(() => !document.querySelector('[data-machine-id="m-other"]'));
    out.removeCalls = window.__calls.filter(c => c[0] === 'remove_computer');
    out.remainingIds = [...document.querySelectorAll('#h5AccountCard .h5-comp-row')].map(el => el.dataset.machineId);
    report(out);
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=settings,
        api_js=_h5_api_js(settings=settings),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=820)
    assert r["signedInHint"] == "Signed in as owner@example.com"
    assert r["curBadge"] == "This computer"
    assert "this computer" not in r["curMeta"].lower()
    assert r["curMeta"].startswith("Synced ")
    assert r["hasSyncCardWhenSignedIn"] is True
    assert r["curHasRemove"] is False
    assert r["otherHasRemove"] is True
    assert r["confirmPrompt"] == "Remove MacBook Pro?"
    assert r["confirmClosedOnCancel"] is True
    assert r["removeCallsAfterCancel"] == 0
    assert r["removeCalls"] == [["remove_computer", "m-other"]]
    assert r["remainingIds"] == ["m-cur"]


@needs_chrome
@browser
def test_h5_ui_change_password_validation_error_success_and_sign_out(tmp_path: Path):
    settings = _h5_settings_fixture(signed_in=True, email="owner@example.com")
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.getElementById('h5ChangePwToggleBtn'));

    const out = {};
    document.getElementById('h5ChangePwToggleBtn').click();
    await until(() => document.getElementById('h5ChangePwForm'));
    out.toggleHiddenWhileOpen = !document.getElementById('h5ChangePwToggleBtn');
    out.cancelButtonCount = [...document.querySelectorAll('#h5AccountCard button')].filter(b => b.textContent.trim() === 'Cancel').length;
    out.formHasCancelBtn = !!document.getElementById('h5CancelPwBtn');

    // 1. Short new password -> client-side AUTH-105
    document.getElementById('h5CurPass').value = 'current-pass-123';
    document.getElementById('h5NewPass').value = 'short';
    document.getElementById('h5ConfirmPass').value = 'short';
    document.getElementById('h5SavePwBtn').click();
    await wait(20);
    out.shortErr = document.querySelector('#h5ChangePwForm .h5-err').textContent.replace(/\\s+/g, ' ').trim();

    // 2. Mismatched passwords -> client-side AUTH-105
    document.getElementById('h5CurPass').value = 'current-pass-123';
    document.getElementById('h5NewPass').value = 'new-password-1234';
    document.getElementById('h5ConfirmPass').value = 'different-password-1234';
    document.getElementById('h5SavePwBtn').click();
    await wait(20);
    out.mismatchErr = document.querySelector('#h5ChangePwForm .h5-err').textContent.replace(/\\s+/g, ' ').trim();
    out.callsAfterClientErrors = window.__calls.filter(c => c[0] === 'change_password').length;

    // 3. Bridge failure AUTH-101 keeps form open and shows coded error
    window.__h5ChangePwRes = { ok: false, code: 'AUTH-101', message: "That email and password don't match." };
    document.getElementById('h5CurPass').value = 'wrong-current-pass';
    document.getElementById('h5NewPass').value = 'new-password-1234';
    document.getElementById('h5ConfirmPass').value = 'new-password-1234';
    document.getElementById('h5SavePwBtn').click();
    await until(() => window.__calls.some(c => c[0] === 'change_password'));
    await wait(20);
    out.serverErr = document.querySelector('#h5ChangePwForm .h5-err').textContent.replace(/\\s+/g, ' ').trim();
    out.formStillOpenOnErr = !!document.getElementById('h5ChangePwForm');

    // 4. Bridge success closes form cleanly
    window.__h5ChangePwRes = null;
    document.getElementById('h5CurPass').value = 'current-pass-123';
    document.getElementById('h5NewPass').value = 'new-password-1234';
    document.getElementById('h5ConfirmPass').value = 'new-password-1234';
    document.getElementById('h5SavePwBtn').click();
    await until(() => !document.getElementById('h5ChangePwForm'));
    await wait(20);
    out.pwCalls = window.__calls.filter(c => c[0] === 'change_password');
    out.formClosedOnOk = !document.getElementById('h5ChangePwForm');

    // 5. Sign out transitions to signed-out UI
    document.getElementById('h5SignOutBtn').click();
    await until(() => document.getElementById('hubAccountUser'));
    await wait(20);
    out.signOutCalls = window.__calls.filter(c => c[0] === 'sign_out').length;
    out.signedOutHeader = document.querySelector('#h5AccountCard .state-badge').textContent.trim();
    report(out);
    """
    html = build_page(
        pref="light",
        report=demo_report(),
        init={"demo": False},
        settings=settings,
        api_js=_h5_api_js(settings=settings),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=860)
    assert r["toggleHiddenWhileOpen"] is True
    assert r["cancelButtonCount"] == 1
    assert r["formHasCancelBtn"] is True
    assert "Error AUTH-105" in r["shortErr"]
    assert "Error AUTH-105" in r["mismatchErr"]
    assert r["callsAfterClientErrors"] == 0
    assert "Error AUTH-101" in r["serverErr"]
    assert r["formStillOpenOnErr"] is True
    assert r["pwCalls"] == [
        ["change_password", "wrong-current-pass", "new-password-1234"],
        ["change_password", "current-pass-123", "new-password-1234"],
    ]
    assert r["formClosedOnOk"] is True
    assert r["signOutCalls"] == 1
    assert r["signedOutHeader"] == "Not signed in"


@needs_chrome
@browser
def test_h5_ui_shared_onboarding_hub_account_form(tmp_path: Path):
    settings = _h5_settings_fixture(signed_in=False, computer_name="New-Laptop")
    onboarding = _h5_onboarding_fixture(signed_in=False, computer_name="New-Laptop")
    probe_js = """
    await until(() => document.querySelector('.ob .h11-ob-account'));
    await wait(40);

    const card = document.querySelector('.ob .h11-ob-account');
    const out = {};
    out.hasUser = !!document.getElementById('obHubAccountUser');
    out.hasPass = !!document.getElementById('obHubAccountPass');
    out.hasComp = !!document.getElementById('obHubAccountComp');
    out.compPrefill = document.getElementById('obHubAccountComp').value;
    out.hasBtn = !!document.getElementById('obHubSignInBtn');
    out.hasLegacyOwnerLink = /owner access/i.test(card.textContent);
    out.hasHubAddr = /https?:\\/\\//i.test(card.textContent) || /hub\\.test/i.test(card.textContent);

    document.getElementById('obHubAccountUser').value = 'onb@example.com';
    document.getElementById('obHubAccountPass').value = 'long-valid-password-123';
    document.getElementById('obHubAccountComp').value = 'Onb-PC';
    document.getElementById('obHubSignInBtn').click();
    await until(() => document.getElementById('ob_h5SignOutBtn'));
    await wait(30);
    out.signInCalls = window.__calls.filter(c => c[0] === 'sign_in');
    out.signedInText = document.querySelector('.ob .h11-ob-account').textContent.replace(/\\s+/g, ' ').trim();
    report(out);
    """
    html = build_page(
        pref="light",
        report=empty_report(),
        init={"demo": False},
        onboarding=onboarding,
        settings=settings,
        api_js=_h5_api_js(settings=settings, onboarding=onboarding),
        probe=probe_js,
        extra_css=FAST,
    )
    r = probe(html, 380, tmp_path, height=960)
    assert r["hasUser"] and r["hasPass"] and r["hasComp"] and r["hasBtn"]
    assert r["compPrefill"] == "New-Laptop"
    assert r["hasLegacyOwnerLink"] is False
    assert r["hasHubAddr"] is False
    assert r["signInCalls"] == [
        ["sign_in", "onb@example.com", "long-valid-password-123", "Onb-PC"]
    ]
    assert "Signed in as onb@example.com" in r["signedInText"]


@needs_chrome
@browser
def test_h5_ui_xss_escaped_in_account_machines_and_errors(tmp_path: Path):
    payload_img = '<img src=x onerror="window.__h5xss=1">'
    payload_script = "<script>window.__h5xss=2</script>"
    settings = _h5_settings_fixture(
        signed_in=True,
        email=f"owner+{payload_img}@example.com",
        computer_name=payload_script,
        machines=[
            {
                "id": "m-cur",
                "name": f"PC {payload_img}",
                "os": "win32",
                "online": True,
                "isCurrent": True,
                "lastSeenAt": "2026-11-16T12:00:00Z",
            },
            {
                "id": "m-other",
                "name": f"Other {payload_script}",
                "os": "linux",
                "online": True,
                "isCurrent": False,
                "lastSeenAt": "2026-11-16T12:00:00Z",
            },
        ],
        sync_state={
            "status": "error",
            "code": "SYNC-201",
            "message": f"Sync error {payload_img} {payload_script}",
        },
    )
    probe_js = """
    await until(() => document.querySelector('.provider-row'));
    await openSettings('computers');
    await until(() => document.querySelector('#h5AccountCard .h5-comp-row'));
    await wait(30);

    // Open remove confirmation to exercise confirming prompt escaping as well
    document.querySelector('[data-h5-remove-ask="m-other"]').click();
    await until(() => document.querySelector('.h5-comp-confirm'));
    await wait(20);

    const card = document.getElementById('h5AccountCard');
    report({
      xssFlag: typeof window.__h5xss === 'undefined' ? 'clean' : window.__h5xss,
      imgsInSheet: document.querySelectorAll('#sheetBody img').length,
      scriptsInSheet: document.querySelectorAll('#sheetBody script').length,
      emailLiteral: card.textContent.includes('<img src=x onerror='),
      machLiteral: card.textContent.includes('<script>window.__h5xss=2<' + '/script>'),
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
    assert r["xssFlag"] == "clean"
    assert r["imgsInSheet"] == 0
    assert r["scriptsInSheet"] == 0
    assert r["emailLiteral"] is True
    assert r["machLiteral"] is True


# ---------------------------------------------------------------------------
# 3. Layout Overflow & Theme Tests (304px, 336px, 380px in light & dark)
# ---------------------------------------------------------------------------


LAYOUT_PROBE = """
function inspectH5Card(stage) {
  const card = document.getElementById('h5AccountCard');
  if (!card) return { stage, missing: true };
  const vw = window.innerWidth;
  const bad = [];
  for (const el of [card, ...card.querySelectorAll('*')]) {
    const cs = getComputedStyle(el);
    if (cs.display === 'none' || cs.visibility === 'hidden') continue;
    const rect = el.getBoundingClientRect();
    if (rect.width <= 0 || rect.height <= 0) continue;
    if (rect.right > vw + 1 || rect.left < -1) {
      bad.push({
        tag: el.tagName,
        id: el.id || '',
        cls: typeof el.className === 'string' ? el.className : '',
        left: rect.left,
        right: rect.right,
        vw,
      });
    }
  }
  return {
    stage,
    docOverflow: document.documentElement.scrollWidth > vw,
    scrollWidth: document.documentElement.scrollWidth,
    vw,
    bad,
    small: smallText(),
  };
}

await until(() => document.querySelector('.provider-row'));
await openSettings('computers');
await until(() => document.getElementById('hubAccountUser'));
await wait(40);
const stages = [];

// 1. Signed-out state
stages.push(inspectH5Card('signed_out'));

// 2. Error-with-code state
document.getElementById('hubAccountUser').value = 'owner@example.com';
document.getElementById('hubAccountPass').value = 'very-long-password-123';
document.getElementById('hubSignInBtn').click();
await until(() => document.querySelector('#h5AccountCard .h5-err'));
await wait(30);
stages.push(inspectH5Card('error_with_code'));

// 3. Signed-in with Computers (+ Remove confirm open)
window.__h5SignInRes = null;
document.getElementById('hubAccountUser').value = 'owner@example.com';
document.getElementById('hubAccountPass').value = 'very-long-password-123';
document.getElementById('hubSignInBtn').click();
await until(() => document.querySelector('[data-h5-remove-ask="m-other"]'));
await wait(30);
document.querySelector('[data-h5-remove-ask="m-other"]').click();
await until(() => document.querySelector('.h5-comp-confirm'));
await wait(30);
stages.push(inspectH5Card('signed_in_remove_confirm'));

// 4. Change password expanded
document.getElementById('h5ChangePwToggleBtn').click();
await until(() => document.getElementById('h5ChangePwForm'));
await wait(30);
stages.push(inspectH5Card('change_password_expanded'));

report({ theme: document.documentElement.dataset.theme, stages });
"""


@needs_chrome
@browser
@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("width", [304, 336, 380])
def test_h5_layout_no_overflow_and_readable_text_across_widths_and_themes(
    tmp_path: Path, theme: str, width: int
):
    settings = _h5_settings_fixture(signed_in=False, computer_name="Desktop-Office-Workstation")
    html = build_page(
        pref=theme,
        color_scheme=theme,
        report=demo_report(),
        init={"demo": False, "theme": theme},
        settings=settings,
        api_js=_h5_api_js(
            settings=settings,
            sign_in_res={
                "ok": False,
                "code": "AUTH-101",
                "message": "That email and password don't match.",
            },
        ),
        probe=LAYOUT_PROBE,
        extra_css=FAST,
    )
    r = probe(html, width, tmp_path, height=900)
    assert r["theme"] == theme
    assert len(r["stages"]) == 4
    for st in r["stages"]:
        assert not st.get("missing"), st
        assert st["docOverflow"] is False, st
        assert st["bad"] == [], st
        assert st["small"] == [], st
