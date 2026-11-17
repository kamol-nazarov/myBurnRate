"""Phase A tests for H6: DesktopBridge owner-session keychain/vault lifecycle, demo mode, audit, doctor, and packaging."""

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
    monkeypatch.delenv("BURNRATE_BUILTIN_HUB_URL", raising=False)
    yield
    auth_throttle.reset()
    setup_wizard._GLOBAL_THROTTLE.reset()
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


def test_desktop_bridge_vault_persistence_across_instances_and_sign_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {"enabled": True, "hubUrl": HUB_URL}
        setup_wizard.write_setup_state(conn, state)

    shared_vault = _MemoryVault()
    with hub_client:
        bridge1 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs1.json",
            vault=shared_vault,
        )
        signed_in = bridge1.sign_in_owner(
            "owner@example.com", PASSWORD, device_label="Workstation 1"
        )
        assert signed_in["ok"] is True, signed_in
        assert signed_in["sessionId"]
        assert "br_s_" not in str(signed_in)
        assert DesktopBridge.OWNER_SESSION_VAULT_REF in shared_vault.items
        vault_token = shared_vault.items[DesktopBridge.OWNER_SESSION_VAULT_REF]
        assert vault_token.startswith("br_s_")

        # Strip DB copy to prove a second DesktopBridge instance restores from the shared Vault
        with connect(spoke_settings.database_path) as conn:
            st = setup_wizard.read_setup_state(conn)
            st["spokeConfig"].pop("ownerSession", None)
            setup_wizard.write_setup_state(conn, st)

        bridge2 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs2.json",
            vault=shared_vault,
        )
        status2 = bridge2.owner_session_status()
        assert status2["ok"] is True
        assert status2["hasOwnerSession"] is True
        assert status2["expiresAt"] == signed_in["expiresAt"]

        listed = bridge2.list_owner_sessions()
        assert listed["ok"] is True, listed
        assert listed["currentSessionId"] == signed_in["sessionId"]

        signed_out = bridge2.sign_out_owner()
        assert signed_out == {"ok": True}
        assert DesktopBridge.OWNER_SESSION_VAULT_REF not in shared_vault.items
        with pytest.raises(LocationError):
            shared_vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF)

        assert bridge2.owner_session_status() == {
            "ok": True,
            "hasOwnerSession": False,
            "expiresAt": None,
        }
        bridge3 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs3.json",
            vault=shared_vault,
        )
        assert bridge3.owner_session_status()["hasOwnerSession"] is False


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
        res = bridge1.sign_in_owner("owner@example.com", PASSWORD)
        assert res["ok"] is True, res
        assert bridge1.owner_session_status()["hasOwnerSession"] is True
        assert bridge1.list_owner_sessions()["ok"] is True

        # Second instance cannot read from Vault (LocationError), falls back to DB mirror
        bridge2 = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs2.json",
            vault=broken_vault,
        )
        assert bridge2.owner_session_status()["hasOwnerSession"] is True
        assert bridge2.list_owner_sessions()["ok"] is True

        # Sign-out handles LocationError on delete cleanly and clears DB mirror
        assert bridge2.sign_out_owner() == {"ok": True}
        assert bridge2.owner_session_status()["hasOwnerSession"] is False
        with connect(spoke_settings.database_path) as conn:
            sc = setup_wizard.read_setup_state(conn)["spokeConfig"]
        assert "ownerSession" not in sc


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
    status = bridge.owner_session_status()
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

    vault = _MemoryVault()
    with hub_client:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs.json",
            vault=vault,
        )
        res = bridge.connect_hub_account(
            "owner@example.com",
            PASSWORD,
            computer_name="Paired Desktop",
            mode="login",
        )
        assert res["ok"] is True, res
        with connect(spoke_settings.database_path) as conn:
            db_session = setup_wizard.read_setup_state(conn)["spokeConfig"]["ownerSession"]
        assert db_session.startswith("br_s_")
        assert vault.get(DesktopBridge.OWNER_SESSION_VAULT_REF) == db_session
        assert bridge.owner_session_status()["hasOwnerSession"] is True


def test_desktop_bridge_demo_mode_rejects_owner_session_actions(tmp_path: Path) -> None:
    bridge = DesktopBridge(
        DemoClient(),
        preferences=tmp_path / "demo_prefs.json",
        demo=True,
        vault=_MemoryVault(),
    )
    assert bridge.owner_session_status() == {
        "ok": True,
        "hasOwnerSession": False,
        "expiresAt": None,
    }
    for res in (
        bridge.sign_in_owner("owner@example.com", PASSWORD),
        bridge.list_owner_sessions(),
        bridge.revoke_owner_session("sess-1"),
        bridge.rotate_owner_session(),
        bridge.sign_out_owner(),
        bridge.get_owner_audit(),
    ):
        assert res["ok"] is False
        assert "demo mode" in res["error"].lower()


def test_desktop_bridge_get_owner_audit_authenticated_and_signed_out(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, hub_client, spoke_settings, _ = _create_paired_hub_and_spoke(tmp_path, monkeypatch)
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        state = setup_wizard.read_setup_state(conn)
        state["role"] = "spoke"
        state["spokeConfig"] = {"enabled": True, "hubUrl": HUB_URL}
        setup_wizard.write_setup_state(conn, state)

    vault = _MemoryVault()
    with hub_client:
        bridge = DesktopBridge(
            NativeClient(spoke_settings, port=17331),
            preferences=tmp_path / "prefs.json",
            vault=vault,
        )
        signed_out_audit = bridge.get_owner_audit()
        assert signed_out_audit["ok"] is False
        assert signed_out_audit["code"] == "AUTH-107"

        signed_in = bridge.sign_in_owner("owner@example.com", PASSWORD)
        assert signed_in["ok"] is True, signed_in

        audit = bridge.get_owner_audit()
        assert audit["ok"] is True, audit
        assert isinstance(audit["actions"], list)
        assert any("account" in str(a.get("action") or "") for a in audit["actions"])


def test_pyproject_wheel_packages_and_hatch_build_spend_web_inclusion(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    assert pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"] == ["spend_app"]

    hatch_build = _load_hatch_build_module(monkeypatch)

    # 1) Wheel standard build with spend_web/ present includes non-__pycache__ / non-.pyc files
    root_with_web = tmp_path / "with_web"
    (root_with_web / "spend_web" / "sub").mkdir(parents=True)
    (root_with_web / "spend_web" / "__pycache__").mkdir(parents=True)
    index_file = root_with_web / "spend_web" / "index.html"
    sub_file = root_with_web / "spend_web" / "sub" / "app.js"
    pyc_file = root_with_web / "spend_web" / "ignored.pyc"
    cache_file = root_with_web / "spend_web" / "__pycache__" / "mod.cpython-312.pyc"
    index_file.write_text("<html></html>", encoding="utf-8")
    sub_file.write_text("console.log(1);", encoding="utf-8")
    pyc_file.write_bytes(b"pyc")
    cache_file.write_bytes(b"cache")

    hook_with = hatch_build.CustomBuildHook(
        str(root_with_web), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data_with: dict = {}
    try:
        hook_with.initialize("standard", build_data_with)
    finally:
        hook_with.finalize("standard", build_data_with, "")
    fi_with = build_data_with["force_include"]
    assert fi_with[str(index_file)] == "spend_web/index.html"
    assert fi_with[str(sub_file)] == "spend_web/sub/app.js"
    assert str(pyc_file) not in fi_with
    assert str(cache_file) not in fi_with
    assert "spend_app/_build_config.py" in fi_with.values()

    # 2) Wheel standard build without spend_web/ skips spend_web cleanly
    root_without_web = tmp_path / "without_web"
    root_without_web.mkdir()
    hook_without = hatch_build.CustomBuildHook(
        str(root_without_web), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data_without: dict = {}
    try:
        hook_without.initialize("standard", build_data_without)
    finally:
        hook_without.finalize("standard", build_data_without, "")
    fi_without = build_data_without["force_include"]
    assert not any(v.startswith("spend_web/") for v in fi_without.values())
    assert "spend_app/_build_config.py" in fi_without.values()

    # 3) Editable wheel build skips force_include
    hook_editable = hatch_build.CustomBuildHook(
        str(root_with_web), {}, {}, None, str(tmp_path / "dist"), "wheel"
    )
    build_data_editable: dict = {}
    try:
        hook_editable.initialize("editable", build_data_editable)
    finally:
        hook_editable.finalize("editable", build_data_editable, "")
    assert "force_include" not in build_data_editable

    # 4) sdist build skips force_include
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
