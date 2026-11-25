"""Phase A/B tests for H6: DesktopBridge owner-session keychain/vault lifecycle, demo mode, audit, doctor, and packaging."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys
import tomllib
import types

from fastapi.testclient import TestClient
import pytest

from spend_app import aggregate, auth_throttle, cli, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_paths import LocationError
from spend_app.db import connect, initialize
from spend_app.error_codes import HUB_PROTOCOL_VERSION
from spend_app.native_desktop import DemoClient, NativeClient
from spend_app.wealthfront_window import DesktopBridge
from tests_spend.local_client_fixture import local_client

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.test"
HUB_HEADERS = {"Origin": HUB_URL, "X-BURNRATE-Request": "1"}
SETUP_TOKEN = "synthetic-setup-token-32-chars-h6"
PASSWORD = "synthetic-password-long-123"


class _MemoryVault:
    def __init__(self) -> None:
        self.items: dict[str, str] = {}

    def put(self, secret: str, ref: str = "") -> str:
        self.items[ref] = secret
        return ref

    def get(self, ref: str) -> str:
        if ref not in self.items:
            raise LocationError(f"missing ref: {ref}")
        return self.items[ref]

    def delete(self, ref: str) -> None:
        self.items.pop(ref, None)


class _BrokenVault:
    def put(self, secret: str, ref: str = "") -> str:
        raise LocationError("keyring unavailable on put")

    def get(self, ref: str) -> str:
        raise LocationError("keyring unavailable on get")

    def delete(self, ref: str) -> None:
        raise LocationError("keyring unavailable on delete")


class _LocalTestClientAdapter:
    """Bridges NativeClient's httpx.Client calls onto a local FastAPI TestClient."""

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


def _make_settings(db_path: Path) -> Settings:
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


@pytest.fixture(autouse=True)
def _clean_shared_state(monkeypatch: pytest.MonkeyPatch):
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    aggregate.reset_memo()
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_ALLOW_REGISTRATION", raising=False)
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.delenv("BURNRATE_BUILTIN_HUB_URL", raising=False)
    yield
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    sync._HUB_REJECTED_CREDENTIALS.clear()
    sync._HUB_CREDENTIAL_GENERATIONS.clear()
    aggregate.reset_memo()


def _create_paired_hub_and_spoke(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> tuple[Settings, TestClient, Settings, object]:
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", SETUP_TOKEN)
    hub_settings = _make_settings(tmp_path / "hub.db")
    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app, base_url=HUB_URL)
    reg = hub_client.post(
        "/api/account/register",
        headers={**HUB_HEADERS, "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION)},
        json={
            "username": "owner@example.com",
            "password": PASSWORD,
            "setupToken": SETUP_TOKEN,
            "sessionOnly": True,
        },
    )
    assert reg.status_code == 200, reg.text
    hub_client.cookies.clear()

    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN", raising=False)

    spoke_settings = _make_settings(tmp_path / "spoke.db")
    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)

    return hub_settings, hub_client, spoke_settings, spoke_app


def _patch_native_to_local(
    monkeypatch: pytest.MonkeyPatch, local_app, *, port: int = 17331
) -> TestClient:
    import spend_app.native_desktop as desktop

    base_url = f"http://127.0.0.1:{port}"
    tc = TestClient(local_app, base_url=base_url)

    def _factory(*_args, **_kwargs):
        return _LocalTestClientAdapter(tc, base_url)

    monkeypatch.setattr(desktop.httpx, "Client", _factory)
    return tc


def _load_hatch_build_module(monkeypatch: pytest.MonkeyPatch):
    try:
        import hatchling.builders.hooks.plugin.interface  # noqa: F401
    except ImportError:

        class _StubBuildHookInterface:
            def __init__(self, root, config, build_config, metadata, directory, target_name):
                self.root = root
                self.config = config
                self.build_config = build_config
                self.metadata = metadata
                self.directory = directory
                self.target_name = target_name

        hatchling_pkg = types.ModuleType("hatchling")
        builders_pkg = types.ModuleType("hatchling.builders")
        hooks_pkg = types.ModuleType("hatchling.builders.hooks")
        plugin_pkg = types.ModuleType("hatchling.builders.hooks.plugin")
        iface_mod = types.ModuleType("hatchling.builders.hooks.plugin.interface")
        iface_mod.BuildHookInterface = _StubBuildHookInterface
        for mod_name, mod in (
            ("hatchling", hatchling_pkg),
            ("hatchling.builders", builders_pkg),
            ("hatchling.builders.hooks", hooks_pkg),
            ("hatchling.builders.hooks.plugin", plugin_pkg),
            ("hatchling.builders.hooks.plugin.interface", iface_mod),
        ):
            monkeypatch.setitem(sys.modules, mod_name, mod)

    spec = importlib.util.spec_from_file_location("test_h6_hatch_build", ROOT / "hatch_build.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_desktop_bridge_public_method_allowlist_and_removed_setup_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import inspect

    expected_methods = {
        "add_profile", "begin_resize", "browse_location",
        "call_page", "change_password", "change_plan", "check_for_update", "close_window",
        "complete_onboarding", "confirm_suggested_plan", "connect_hub_account",
        "connect_source", "copy_image_to_clipboard", "create_account",
        "defer_onboarding", "disconnect_source", "dismiss_candidate",
        "dismiss_email_prompt", "fetch_analytics_summary",
        "fetch_entity_detail", "fetch_usage", "get_diagnostics_data",
        "get_diagnostics_report", "get_initial_state", "get_notification_settings",
        "get_onboarding_data", "get_plan_settings", "get_settings_data", "get_setup_check",
        "get_setup_progress", "get_setup_scan", "get_week_share_data",
        "install_update", "list_computers", "minimize_window", "opacity_state",
        "open_external", "quit_app", "remove_computer", "remove_profile",
        "rename_profile", "publish_effective_theme", "run_setup_scan",
        "save_analytics_window", "save_period", "save_provider_order",
        "send_test_notification", "set_account_email",
        "set_auto_fade", "set_close_to_tray", "set_close_to_tab", "set_hide_from_taskbar", "set_hud_density", "set_hud_mini",
        "set_idle_opacity", "set_low_quota_alerts", "set_notification_kind", "set_pill_limit",
        "set_setup_consent", "set_theme", "set_topmost",
        "set_ui_busy", "show_tab", "sign_in", "sign_out", "trigger_sync", "update_summary",
    }
    actual_methods = {
        name
        for name, val in inspect.getmembers(DesktopBridge)
        if not name.startswith("_") and callable(val)
    }
    assert actual_methods == expected_methods

    _, _, spoke_settings, spoke_app = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    origin = "http://127.0.0.1:17331"
    anonymous = TestClient(spoke_app, base_url=origin)
    with local_client(spoke_app, spoke_settings.database_path, base_url=origin) as spoke_client:
        hdrs = {"Origin": origin, "X-BURNRATE-Request": "1"}
        for path in ("/api/setup/owner-session", "/api/setup/rotate-machine"):
            assert anonymous.post(path, headers=hdrs, json={}).status_code == 401
            res = spoke_client.post(path, headers=hdrs, json={})
            assert res.status_code == 404
            assert res.json() == {"detail": "Not Found"}


def test_desktop_bridge_vault_persistence_across_instances_and_sign_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    monkeypatch.setattr(sync, "sync_once", lambda *_a, **_kw: {"ok": True})
    shared_vault = _MemoryVault()
    with hub_client:
        bridge1 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs1.json",
            vault=shared_vault,
        )
        signed_in = bridge1.sign_in("owner@example.com", PASSWORD, "Workstation 1")
        assert signed_in["ok"] is True, signed_in
        assert "br_s_" not in str(signed_in) and "br_m_" not in str(signed_in)
        assert DesktopBridge.OWNER_SESSION_VAULT_REF in shared_vault.items
        vault_token = shared_vault.items[DesktopBridge.OWNER_SESSION_VAULT_REF]
        assert vault_token.startswith("br_s_")

        with connect(spoke_settings.database_path) as conn:
            import json as _json
            raw_row = conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (setup_wizard.SETUP_META_KEY,)
            ).fetchone()
            raw_sc = (_json.loads(raw_row[0]) if raw_row else {}).get("spokeConfig") or {}
            sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in raw_sc and "token" not in raw_sc
        assert "ownerSession" not in sc and not sc.get("token")

        bridge2 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs2.json",
            vault=shared_vault,
        )
        status2 = bridge2._owner_session_status()
        assert status2["ok"] is True
        assert status2["hasOwnerSession"] is True

        listed = bridge2.list_computers()
        assert listed["ok"] is True, listed

        signed_out = bridge2.sign_out()
        assert signed_out == {"ok": True}
        assert DesktopBridge.OWNER_SESSION_VAULT_REF not in shared_vault.items
        with pytest.raises(LocationError):
            shared_vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)

        assert bridge2._owner_session_status() == {
            "ok": True,
            "hasOwnerSession": False,
            "expiresAt": None,
        }
        bridge3 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs3.json",
            vault=shared_vault,
        )
        assert bridge3._owner_session_status()["hasOwnerSession"] is False


def test_desktop_bridge_falls_back_gracefully_when_vault_raises_location_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {"enabled": True, "hubUrl": HUB_URL}
        setup_wizard.write_setup_state(conn, state)

    broken_vault = _BrokenVault()
    with hub_client:
        bridge1 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs1.json",
            vault=broken_vault,
        )
        res = bridge1.sign_in("owner@example.com", PASSWORD, "Broken Box")
        assert res["ok"] is False
        assert res["code"] == "APP-903"
        assert bridge1._owner_session_status()["hasOwnerSession"] is False
        with connect(spoke_settings.database_path) as conn:
            sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc

        bridge2 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs2.json",
            vault=broken_vault,
        )
        assert bridge2._owner_session_status()["hasOwnerSession"] is False
        assert bridge2.sign_out() == {"ok": True}


def test_desktop_bridge_owner_session_status_clears_expired_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spoke_settings = _make_settings(tmp_path / "spoke_expired.db")
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "enabled": True,
            "hubUrl": HUB_URL,
            "ownerSession": "br_s_expired_local_session",
            "ownerSessionExpiresAt": "2020-01-01T00:00:00Z",
        }
        setup_wizard.write_setup_state(conn, state)

    vault = _MemoryVault()
    vault.put("br_s_expired_local_session", DesktopBridge.OWNER_SESSION_VAULT_REF)

    bridge = DesktopBridge(
        NativeClient(spoke_settings, port=17331),
        preferences=tmp_path / "prefs.json",
        vault=vault,
    )
    status = bridge._owner_session_status()
    assert status == {"ok": True, "hasOwnerSession": False, "expiresAt": None}
    assert DesktopBridge.OWNER_SESSION_VAULT_REF not in vault.items
    with connect(spoke_settings.database_path) as conn:
        sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
    assert "ownerSession" not in sc
    assert "ownerSessionExpiresAt" not in sc


def test_desktop_bridge_connect_hub_account_syncs_owner_session_to_vault(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client, spoke_settings, spoke_app = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    _patch_native_to_local(monkeypatch, spoke_app, port=17331)

    from spend_app.connection_secrets import get_vault

    with hub_client:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs.json",
        )
        res = bridge.connect_hub_account(
            "owner@example.com",
            PASSWORD,
            computer_name="Paired Desktop",
            mode="login",
        )
        assert res["ok"] is True, res
        with connect(spoke_settings.database_path) as conn:
            sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc
        assert "token" not in sc or not sc["token"]
        vault_session = get_vault(spoke_settings.database_path).get(
            DesktopBridge.OWNER_SESSION_VAULT_REF
        )
        assert vault_session.startswith("br_s_")
        assert bridge._owner_session_status()["hasOwnerSession"] is True


def test_desktop_bridge_demo_mode_rejects_owner_session_actions(tmp_path: Path) -> None:
    bridge = DesktopBridge(
        DemoClient(),
        preferences=tmp_path / "demo_prefs.json",
        demo=True,
        vault=_MemoryVault(),
    )
    assert bridge._owner_session_status() == {
        "ok": True,
        "hasOwnerSession": False,
        "expiresAt": None,
    }
    for res in (
        bridge.sign_in("owner@example.com", PASSWORD),
        bridge.create_account("owner@example.com", PASSWORD),
        bridge.sign_out(),
        bridge.change_password(PASSWORD, "new-pass-word-123"),
        bridge.remove_computer("machine-1"),
    ):
        assert res["ok"] is False
        assert "demo mode" in res["message"].lower()


def test_pyproject_wheel_packages_and_hatch_build_exclude_spend_web(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject_text = (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    pyproject = tomllib.loads(pyproject_text)
    wheel_cfg = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel_cfg["packages"] == ["spend_app"]
    force_include_cfg = wheel_cfg.get("force-include", {})
    assert not any("spend_web" in str(k) or "spend_web" in str(v) for k, v in force_include_cfg.items())
    # Git-ignored leftovers (e.g. __pycache__) can outlive the deletion in old checkouts.
    web_files = [p for p in (ROOT / "spend_web").rglob("*") if p.is_file()] if (ROOT / "spend_web").exists() else []
    assert all("__pycache__" in p.parts for p in web_files), web_files

    hatch_build = _load_hatch_build_module(monkeypatch)

    # 1) Even if spend_web/ existed on disk, wheel standard build only includes _build_config.py
    root_with_web = tmp_path / "with_web"
    (root_with_web / "spend_web").mkdir(parents=True)
    (root_with_web / "spend_web" / "index.html").write_text("<html></html>", encoding="utf-8")

    hook_with = hatch_build.CustomBuildHook(
        str(root_with_web), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data_with: dict = {}
    try:
        hook_with.initialize("standard", build_data_with)
    finally:
        hook_with.finalize("standard", build_data_with, "")
    fi_with = build_data_with["force_include"]
    assert list(fi_with.values()) == ["spend_app/_build_config.py"]
    assert not any("spend_web" in v for v in fi_with.values())

    # 2) Editable wheel build skips force_include
    hook_editable = hatch_build.CustomBuildHook(
        str(root_with_web), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data_editable: dict = {}
    try:
        hook_editable.initialize("editable", build_data_editable)
    finally:
        hook_editable.finalize("editable", build_data_editable, "")
    assert "force_include" not in build_data_editable

    # 3) sdist build skips force_include
    hook_sdist = hatch_build.CustomBuildHook(
        str(root_with_web), {}, {}, None, str(tmp_path / "dist"), "sdist"
    )
    build_data_sdist: dict = {}
    try:
        hook_sdist.initialize("standard", build_data_sdist)
    finally:
        hook_sdist.finalize("standard", build_data_sdist, "")
    assert "force_include" not in build_data_sdist


def test_cli_check_desktop_assets_ok_and_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    assert cli._DESKTOP_ASSETS == ("wealthfront_ui.html", "tab_ui.html")
    ok_check = cli._check_desktop_assets()
    assert ok_check["id"] == "desktop_assets"
    assert ok_check["status"] == "ok"
    assert "wealthfront_ui.html" in ok_check["detail"]

    empty_pkg = tmp_path / "empty_spend_app"
    empty_pkg.mkdir()
    monkeypatch.setattr(cli, "files", lambda _pkg: empty_pkg)
    fail_check = cli._check_desktop_assets()
    assert fail_check["id"] == "desktop_assets"
    assert fail_check["status"] == "fail"
    assert "wealthfront_ui.html" in fail_check["detail"]
