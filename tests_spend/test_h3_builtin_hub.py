from __future__ import annotations

import importlib
import importlib.util
import io
import json
import logging
import os
import shutil
import subprocess
import sys
import types
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]
SETUP_HEADERS = {"X-BURNRATE-Request": "1", "Origin": "http://testserver"}


def _load_module(name: str, rel_path: str):
    path = ROOT / rel_path
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_settings(db_path: Path) -> Settings:
    return Settings(
        database_path=db_path,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40_000,
    )


def _local_client(settings):
    from spend_app.local_access import read_local_key

    app = create_app(settings, enable_scheduler=False)
    headers = {"Authorization": "Bearer " + read_local_key(settings.database_path)}
    return TestClient(app, headers=headers)


def _configured_test_hub(settings, monkeypatch):
    token = "configured-test-hub-recovery"
    with monkeypatch.context() as env:
        env.setenv("BURNRATE_CENTRAL_SERVER", "1")
        env.setenv("BURNRATE_ACCESS_TOKEN", token)
        app = create_app(settings, enable_scheduler=False)
    return TestClient(app, headers={"Authorization": "Bearer " + token})


# ---------------------------------------------------------------------------
# Build-time packaging/build_config.py
# ---------------------------------------------------------------------------


def test_validate_builtin_hub_url_accepts_empty_and_normalizes_https() -> None:
    build_config = _load_module("test_build_config_mod", "packaging/build_config.py")

    assert build_config.validate_builtin_hub_url(None) == ""
    assert build_config.validate_builtin_hub_url("") == ""
    assert build_config.validate_builtin_hub_url("   \t\n  ") == ""
    assert (
        build_config.validate_builtin_hub_url("https://hub.example.ts.net")
        == "https://hub.example.ts.net"
    )
    assert (
        build_config.validate_builtin_hub_url("https://hub.example.ts.net:8449")
        == "https://hub.example.ts.net:8449"
    )
    assert (
        build_config.validate_builtin_hub_url("  HTTPS://My-Hub.Tail0Ab1C2.ts.net:8449/  ")
        == "https://my-hub.tail0ab1c2.ts.net:8449"
    )


@pytest.mark.parametrize(
    "bad_url",
    [
        "http://hub.example",
        "http://localhost:17331",
        "http://127.0.0.1:8449",
        "hub.example:8449",
        "https://",
        "https://user:pass@hub.example:8449",
        "https://user@hub.example",
        "https://hub.example/api",
        "https://hub.example:8449/v1/",
        "https://hub.example?a=1",
        "https://hub.example#frag",
        "https://hub.example:0",
        "https://hub.example:65536",
        "https://hub.example:notaport",
    ],
)
def test_validate_builtin_hub_url_rejects_invalid_values(bad_url: str) -> None:
    build_config = _load_module("test_build_config_invalid", "packaging/build_config.py")
    with pytest.raises(ValueError):
        build_config.validate_builtin_hub_url(bad_url)


def test_render_and_write_build_config(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    build_config = _load_module("test_build_config_write", "packaging/build_config.py")

    rendered_empty = build_config.render_build_config("")
    assert "BUILTIN_HUB_URL = ''" in rendered_empty

    rendered = build_config.render_build_config("HTTPS://Hub.Example.net:8449/")
    assert "BUILTIN_HUB_URL = 'https://hub.example.net:8449'" in rendered

    with pytest.raises(ValueError):
        build_config.render_build_config("http://hub.example")

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "https://EnvHub.Example:8449/")
    out_file = tmp_path / "nested" / "_build_config.py"
    written_path, clean_url = build_config.write_build_config(out_file)
    assert written_path == out_file
    assert clean_url == "https://envhub.example:8449"
    assert out_file.is_file()
    assert "BUILTIN_HUB_URL = 'https://envhub.example:8449'" in out_file.read_text(
        encoding="utf-8"
    )


# ---------------------------------------------------------------------------
# hatch_build.py (CustomBuildHook)
# ---------------------------------------------------------------------------


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
    return _load_module("test_hatch_build_mod", "hatch_build.py")


def test_hatch_build_hook_wheel_generates_temp_config_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    hatch_build = _load_hatch_build_module(monkeypatch)
    checkout_build_config = ROOT / "spend_app" / "_build_config.py"
    assert not checkout_build_config.exists()

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "HTTPS://WheelHub.Example:8449/")
    hook = hatch_build.CustomBuildHook(str(ROOT), {}, None, None, str(tmp_path), "wheel")
    build_data: dict = {}

    hook.initialize("standard", build_data)
    try:
        assert not checkout_build_config.exists()
        force_include = build_data.get("force_include") or {}
        matching = [src for src, dst in force_include.items() if dst == "spend_app/_build_config.py"]
        assert len(matching) == 1
        generated = Path(matching[0])
        assert generated.is_file()
        assert generated.parent != ROOT / "spend_app"
        assert "BUILTIN_HUB_URL = 'https://wheelhub.example:8449'" in generated.read_text(
            encoding="utf-8"
        )
        temp_dir = generated.parent
    finally:
        hook.finalize("standard", build_data, str(tmp_path / "out.whl"))

    assert not temp_dir.exists()
    assert not checkout_build_config.exists()


@pytest.mark.parametrize(
    ("target_name", "version"),
    [("sdist", "standard"), ("wheel", "editable")],
)
def test_hatch_build_hook_skips_non_wheel_or_editable_and_validates_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    target_name: str,
    version: str,
) -> None:
    hatch_build = _load_hatch_build_module(monkeypatch)

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "https://valid-hub.example")
    hook = hatch_build.CustomBuildHook(str(ROOT), {}, None, None, str(tmp_path), target_name)
    build_data: dict = {}
    hook.initialize(version, build_data)
    assert "force_include" not in build_data
    hook.finalize(version, build_data, str(tmp_path / "artifact"))

    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "http://invalid-plain-http.example")
    bad_hook = hatch_build.CustomBuildHook(str(ROOT), {}, None, None, str(tmp_path), target_name)
    with pytest.raises(ValueError):
        bad_hook.initialize(version, {})


# ---------------------------------------------------------------------------
# packaging/build_native.py
# ---------------------------------------------------------------------------


def test_build_native_writes_and_cleans_build_config_on_success_and_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    build_native = _load_module("test_build_native_mod", "packaging/build_native.py")
    checkout_build_config = ROOT / "spend_app" / "_build_config.py"
    assert not checkout_build_config.exists()

    fake_build_dir = tmp_path / "build"
    fake_generated = tmp_path / "spend_app" / "_build_config.py"
    monkeypatch.setattr(build_native, "BUILD", fake_build_dir)
    monkeypatch.setattr(build_native, "GENERATED_BUILD_CONFIG", fake_generated)
    monkeypatch.setattr(build_native, "find_makensis", lambda: None)
    monkeypatch.setattr(shutil, "copy2", lambda src, dst: None)
    monkeypatch.setattr(shutil, "make_archive", lambda base_name, fmt, **kw: f"{base_name}.{fmt}")
    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "HTTPS://NativeHub.Example:8449/")

    seen_during_pyinstaller: list[str] = []

    def fake_run_ok(cmd, check=False, **kwargs):
        if "PyInstaller" in cmd:
            assert fake_generated.is_file()
            seen_during_pyinstaller.append(fake_generated.read_text(encoding="utf-8"))
            bundle_dir = fake_build_dir / "native" / "BURNRATE"
            bundle_dir.mkdir(parents=True, exist_ok=True)
            (bundle_dir / "BURNRATE.exe").write_bytes(b"stub")
        return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_ok)
    rc = build_native.main([])
    assert rc == 0
    assert len(seen_during_pyinstaller) == 1
    assert "BUILTIN_HUB_URL = 'https://nativehub.example:8449'" in seen_during_pyinstaller[0]
    assert not fake_generated.exists()
    assert not checkout_build_config.exists()

    # Failure during PyInstaller must still remove GENERATED_BUILD_CONFIG in finally.
    seen_before_fail: list[bool] = []

    def fake_run_fail(cmd, check=False, **kwargs):
        if "PyInstaller" in cmd:
            seen_before_fail.append(fake_generated.is_file())
            raise subprocess.CalledProcessError(1, cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout="deadbeef\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run_fail)
    with pytest.raises(subprocess.CalledProcessError):
        build_native.main([])
    assert seen_before_fail == [True]
    assert not fake_generated.exists()
    assert not checkout_build_config.exists()

    # Invalid BURNRATE_BUILTIN_HUB_URL exits 1 before creating _build_config.py or running PyInstaller.
    pyinstaller_called: list[bool] = []
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda cmd, **kwargs: pyinstaller_called.append(True),
    )
    monkeypatch.setenv("BURNRATE_BUILTIN_HUB_URL", "http://insecure.example")
    assert build_native.main([]) == 1
    assert pyinstaller_called == []
    assert not fake_generated.exists()
    assert not checkout_build_config.exists()


# ---------------------------------------------------------------------------
# Runtime resolution (spend_app/config.py)
# ---------------------------------------------------------------------------


def test_resolve_hub_url_precedence_and_no_hub_clean(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app import config

    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL="https://builtin.example:8449"),
    )
    assert config.builtin_hub_url() == "https://builtin.example:8449"
    assert config.resolve_hub_url() == "https://builtin.example:8449"

    monkeypatch.setenv("BURNRATE_HUB_URL", "HTTPS://Override.Example:9443/")
    assert config.resolve_hub_url() == "https://override.example:9443"

    # Invalid built-in URL raises ValueError.
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL="http://bad-builtin.example"),
    )
    with pytest.raises(ValueError):
        config.builtin_hub_url()

    # Empty built-in URL and no override -> "", app runs cleanly with hubAvailable=False.
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL=""),
    )
    assert config.resolve_hub_url() == ""

    settings = _make_settings(tmp_path / "nohub.db")
    initialize(settings.database_path)
    with _local_client(settings) as client:
        res = client.get("/api/setup/state")
        assert res.status_code == 200
        payload = res.json()
        assert payload["hubAvailable"] is False
        assert "centralServerUrl" not in payload


def test_load_user_env_hermetic_under_pytest_and_dotenv_overrides_builtin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    spec = importlib.util.spec_from_file_location("test_real_config_mod", ROOT / "spend_app/config.py")
    assert spec is not None and spec.loader is not None
    config = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "test_real_config_mod", config)
    spec.loader.exec_module(config)

    monkeypatch.delenv("BURNRATE_DATA_ROOT", raising=False)
    monkeypatch.delenv("BURNRATE_DEV", raising=False)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))

    # While PYTEST_CURRENT_TEST is present, load_user_env() is a no-op.
    assert "PYTEST_CURRENT_TEST" in os.environ
    config.load_user_env()
    assert not (tmp_path / "BURNRATE" / ".env").exists()

    # Outside pytest, LOCALAPPDATA/BURNRATE/.env overrides _build_config.BUILTIN_HUB_URL.
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL="https://builtin.example:8449"),
    )
    env_dir = tmp_path / "BURNRATE"
    env_dir.mkdir(parents=True, exist_ok=True)
    (env_dir / ".env").write_text(
        "BURNRATE_HUB_URL=https://dotenv-override.example:8449\n",
        encoding="utf-8",
    )

    saved_pytest = os.environ.get("PYTEST_CURRENT_TEST")
    saved_hub = os.environ.get("BURNRATE_HUB_URL")
    saved_zai = os.environ.get("BURNRATE_ENABLE_ZAI_QUOTA")
    saved_oc = os.environ.get("BURNRATE_ENABLE_OPENCODE_GO_QUOTA")
    saved_muse = os.environ.get("BURNRATE_ENABLE_MUSE_QUOTA")
    try:
        os.environ.pop("PYTEST_CURRENT_TEST", None)
        os.environ.pop("BURNRATE_HUB_URL", None)
        assert config.resolve_hub_url() == "https://dotenv-override.example:8449"
    finally:
        for key, prev in (
            ("PYTEST_CURRENT_TEST", saved_pytest),
            ("BURNRATE_HUB_URL", saved_hub),
            ("BURNRATE_ENABLE_ZAI_QUOTA", saved_zai),
            ("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", saved_oc),
            ("BURNRATE_ENABLE_MUSE_QUOTA", saved_muse),
        ):
            if prev is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = prev


# ---------------------------------------------------------------------------
# DB migration (db.initialize -> migrate_setup_hub_url)
# ---------------------------------------------------------------------------


def test_migrate_setup_hub_url_strips_server_url_and_updates_hub_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "migrate.db"
    initialize(db_path)
    legacy_payload = {
        "version": 1,
        "role": "spoke",
        "account": {
            "username": "owner",
            "serverUrl": "https://old-hub.example:8449",
            "machineId": "m-1",
        },
        "spokeConfig": {
            "enabled": True,
            "hubUrl": "https://old-hub.example:8449",
            "token": "br_m_tok",
        },
    }
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (setup_wizard.SETUP_META_KEY, json.dumps(legacy_payload)),
        )

    monkeypatch.setenv("BURNRATE_HUB_URL", "https://new-hub.example:8449")
    initialize(db_path)

    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT value FROM app_meta WHERE key=?",
            (setup_wizard.SETUP_META_KEY,),
        ).fetchone()
    stored = json.loads(row[0])
    assert "serverUrl" not in stored["account"]
    assert stored["spokeConfig"]["hubUrl"] == "https://new-hub.example:8449"


def test_migrate_setup_hub_url_preserves_hub_url_when_resolved_empty(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path = tmp_path / "preserve.db"
    initialize(db_path)
    legacy_payload = {
        "version": 1,
        "role": "spoke",
        "account": {
            "username": "owner",
            "machineId": "m-1",
        },
        "spokeConfig": {
            "enabled": True,
            "hubUrl": "https://preserved-hub.example:8449",
            "token": "br_m_tok",
        },
    }
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (setup_wizard.SETUP_META_KEY, json.dumps(legacy_payload)),
        )

    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL=""),
    )
    initialize(db_path)

    with connect(db_path) as conn:
        saved = setup_wizard.read_setup_state(conn)
    assert saved["spokeConfig"]["hubUrl"] == "https://preserved-hub.example:8449"


# ---------------------------------------------------------------------------
# CLI / pairing / API / UI / native_onboarding
# ---------------------------------------------------------------------------


def test_cli_sync_uses_resolve_hub_url_when_hub_omitted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    from spend_app import cli

    settings = _make_settings(tmp_path / "cli.db")
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    seen_calls: list[dict] = []

    def fake_sync_once(_settings, _pricing, *, hub_url, token, machine_name=None, **kwargs):
        seen_calls.append({"hub_url": hub_url, "token": token, "machine_name": machine_name})
        return {"ok": True, "eventsWritten": 0}

    monkeypatch.setattr(sync, "sync_once", fake_sync_once)
    token_file = tmp_path / "sync-token.txt"
    token_file.write_text("br_m_testtoken\n", encoding="utf-8")
    monkeypatch.setenv("BURNRATE_HUB_URL", "https://cli-hub.example:8449")
    monkeypatch.setattr(sys, "argv", ["burnrate", "sync", "--token-file", str(token_file), "--once"])
    assert cli._main() == 0
    assert seen_calls == [{"hub_url": "https://cli-hub.example:8449", "token": "br_m_testtoken", "machine_name": None}]
    capsys.readouterr()

    # Without configured hub, exit code 2 with clear message.
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setitem(
        sys.modules,
        "spend_app._build_config",
        types.SimpleNamespace(BUILTIN_HUB_URL=""),
    )
    monkeypatch.setattr(sys, "argv", ["burnrate", "sync", "--token-file", str(token_file), "--once"])
    assert cli._main() == 2
    err = capsys.readouterr().err
    assert "No hub is configured" in err


def test_machines_sync_command_omits_hub_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _make_settings(tmp_path / "hub.db")
    initialize(settings.database_path)

    monkeypatch.setenv("BURNRATE_HUB_URL", "https://hub.example:8449")
    with _local_client(settings) as client:
        machine_res = client.post(
            "/api/machines",
            headers=SETUP_HEADERS,
            json={"action": "register", "name": "CLI Box", "os": "win32"},
        )
        assert machine_res.status_code == 200, machine_res.text
        machine_body = machine_res.json()
        assert "--hub" not in machine_body["syncCommand"]
        assert "python -m spend_app sync --token-file" in machine_body["syncCommand"]
        assert "https://hub.example:8449" not in machine_res.text


def test_setup_account_ignores_server_url_in_request_body(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()

    hub_settings = _make_settings(tmp_path / "hub.db")
    spoke_settings = _make_settings(tmp_path / "spoke.db")
    initialize(hub_settings.database_path)
    initialize(spoke_settings.database_path)

    configured_hub = "https://configured-hub.example:8449"
    monkeypatch.setenv("BURNRATE_HUB_URL", configured_hub)

    with _configured_test_hub(hub_settings, monkeypatch) as hub_client, _local_client(spoke_settings) as spoke_client:
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, configured_hub, hub_client)
        reg = hub_client.post(
            "/api/account/register",
            headers={"X-BURNRATE-Request": "1"},
            json={
                "username": "owner@example.com",
                "password": "correct-horse-88",
                "computerName": "HubBox",
                "os": "win32",
            },
        )
        assert reg.status_code == 200, reg.text

        res = spoke_client.post(
            "/api/setup/account",
            headers=SETUP_HEADERS,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": "correct-horse-88",
                "computerName": "SpokeBox",
                "serverUrl": "https://attacker-override.invalid:9999",
                "testSync": False,
            },
        )
        assert res.status_code == 200, res.text
        data = res.json()
        assert "serverUrl" not in data["account"]
        assert "serverUrl" not in data["setup"]["account"]
        assert "hubUrl" not in data["setup"]["spokeConfig"]
        assert data["setup"]["spokeConfig"]["hubConfigured"] is True
        assert "attacker-override.invalid" not in res.text

        with connect(spoke_settings.database_path) as conn:
            saved = setup_wizard.read_setup_state(conn)
        assert saved["spokeConfig"]["hubUrl"] == configured_hub


def test_ui_html_has_no_hub_url_inputs() -> None:
    from spend_app.wealthfront_window import load_ui_html

    wealthfront_html = load_ui_html()

    for forbidden in (
        "#hubAccountUrl",
        'id="hubAccountUrl"',
        "#pairHubUrl",
        'id="pairHubUrl"',
        "esc(spoke.hubUrl)",
        "defaultHubUrl",
    ):
        assert forbidden not in wealthfront_html


def test_native_onboarding_recognizes_spoke_hub_configured(tmp_path: Path) -> None:
    from spend_app.native_onboarding import build_onboarding_data

    class FakeClient:
        def request(self, path: str):
            if path == "/api/connections":
                return {"connections": []}
            if path == "/api/setup/state":
                return {
                    "setup": {
                        "spokeConfig": {
                            "enabled": True,
                            "hubConfigured": True,
                        }
                    }
                }
            raise AssertionError(f"Unexpected path {path}")

    payload = build_onboarding_data(
        FakeClient(),
        which=lambda _cmd: None,
        env={},
        home=tmp_path,
    )
    assert payload["ok"] is True
    assert payload["hubConnected"] is True


# ---------------------------------------------------------------------------
# Acceptance leak test
# ---------------------------------------------------------------------------


def test_acceptance_sentinel_hub_url_never_leaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from spend_app import redact as redact_mod
    from spend_app import support_report

    sentinel_host = "secret-hub-sentinel.tail0ab1c2.ts.net"
    sentinel_url = f"https://{sentinel_host}:8449"
    sentinel_terms = (
        sentinel_url,
        f"{sentinel_host}:8449",
        sentinel_host,
        "secret-hub-sentinel",
    )

    def _assert_no_leak(text: str, *, context: str) -> None:
        lowered = text.lower()
        for term in sentinel_terms:
            assert term.lower() not in lowered, f"Leaked {term!r} in {context}: {text}"

    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()

    monkeypatch.setenv("BURNRATE_HUB_URL", sentinel_url)
    hub_settings = _make_settings(tmp_path / "accept_hub.db")
    spoke_settings = _make_settings(tmp_path / "accept_spoke.db")
    initialize(hub_settings.database_path)
    initialize(spoke_settings.database_path)

    with _configured_test_hub(hub_settings, monkeypatch) as hub_client, _local_client(spoke_settings) as spoke_client:
        # 1. /api/setup/state
        state_res = spoke_client.get("/api/setup/state")
        assert state_res.status_code == 200
        assert state_res.json()["hubAvailable"] is True
        _assert_no_leak(state_res.text, context="/api/setup/state")

        # 2. /api/setup/account (success)
        reg = hub_client.post(
            "/api/account/register",
            headers={"X-BURNRATE-Request": "1"},
            json={
                "username": "owner@example.com",
                "password": "correct-horse-88",
                "computerName": "HubNode",
                "os": "win32",
            },
        )
        assert reg.status_code == 200, reg.text

        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, sentinel_url, hub_client)
        acct_ok = spoke_client.post(
            "/api/setup/account",
            headers=SETUP_HEADERS,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": "correct-horse-88",
                "computerName": "SpokeNode",
                "testSync": True,
            },
        )
        assert acct_ok.status_code == 200, acct_ok.text
        _assert_no_leak(acct_ok.text, context="/api/setup/account success")

        # 3. /api/setup/account (connection-error failure)
        monkeypatch.delitem(sync._HUB_TEST_CLIENTS, sentinel_url, raising=False)

        class FailingHttpClient:
            def __init__(self, *args, **kwargs):
                pass

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def post(self, url, **kwargs):
                raise httpx.ConnectError(f"Connection refused connecting to {url}")

        monkeypatch.setattr(httpx, "Client", FailingHttpClient)
        acct_err = spoke_client.post(
            "/api/setup/account",
            headers=SETUP_HEADERS,
            json={
                "mode": "login",
                "username": "owner@example.com",
                "password": "correct-horse-88",
                "computerName": "SpokeNode",
                "testSync": False,
            },
        )
        assert acct_err.status_code == 503
        assert acct_err.json() == {"error": {"code": "NET-001"}}
        _assert_no_leak(acct_err.text, context="/api/setup/account connection error")

        # 5. /api/machines
        machines_post = hub_client.post(
            "/api/machines",
            headers=SETUP_HEADERS,
            json={"action": "register", "name": "ScriptBox", "os": "linux"},
        )
        assert machines_post.status_code == 200, machines_post.text
        _assert_no_leak(machines_post.text, context="POST /api/machines")

        machines_get = hub_client.get("/api/machines")
        assert machines_get.status_code == 200
        _assert_no_leak(machines_get.text, context="GET /api/machines")


    # 7. RedactingFormatter + HubUrlRedactingFilter on burnrate.api and httpx loggers
    redact_mod.install_log_redaction()
    log_stream = io.StringIO()
    handler = logging.StreamHandler(log_stream)
    handler.setFormatter(redact_mod.RedactingFormatter("%(name)s %(levelname)s %(message)s"))

    plain_stream = io.StringIO()
    plain_handler = logging.StreamHandler(plain_stream)
    plain_handler.setFormatter(logging.Formatter("%(name)s %(levelname)s %(message)s"))

    api_logger = logging.getLogger("burnrate.api")
    httpx_logger = logging.getLogger("httpx")
    orig_api_level = api_logger.level
    orig_httpx_level = httpx_logger.level
    api_logger.setLevel(logging.INFO)
    httpx_logger.setLevel(logging.INFO)
    api_logger.addHandler(handler)
    api_logger.addHandler(plain_handler)
    httpx_logger.addHandler(handler)
    httpx_logger.addHandler(plain_handler)
    try:
        api_logger.error("Failed talking to %s (%s)", sentinel_url, sentinel_host)
        httpx_logger.info(
            'HTTP Request: POST %s/api/sync/push "HTTP/1.1 502 Bad Gateway"',
            sentinel_url,
        )
        try:
            raise RuntimeError(f"Timeout contacting {sentinel_url}/api/machines")
        except RuntimeError:
            api_logger.exception("Unhandled error")
    finally:
        api_logger.removeHandler(handler)
        api_logger.removeHandler(plain_handler)
        httpx_logger.removeHandler(handler)
        httpx_logger.removeHandler(plain_handler)
        api_logger.setLevel(orig_api_level)
        httpx_logger.setLevel(orig_httpx_level)

    _assert_no_leak(log_stream.getvalue(), context="RedactingFormatter logs")
    _assert_no_leak(plain_stream.getvalue(), context="HubUrlRedactingFilter logs")

    # 8. support_report.build_report()
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    (log_dir / "dashboard.log").write_text(
        f"ERROR failed sync to {sentinel_url}/api/sync/push on host {sentinel_host}\n",
        encoding="utf-8",
    )
    with connect(spoke_settings.database_path) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, error) VALUES(?,?,?,?,?)",
            (
                "sync",
                datetime.now(UTC).isoformat(),
                datetime.now(UTC).isoformat(),
                "failed",
                f"ConnectError: {sentinel_url}/api/sync/push ({sentinel_host})",
            ),
        )
    report = support_report.build_report(spoke_settings, log_dir=log_dir)
    _assert_no_leak(report, context="support_report.build_report()")
