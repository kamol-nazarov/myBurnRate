"""Tests for H4 · Remove pairing: dead code, routes, UI, CLI, upgrade migration, and redaction."""

from __future__ import annotations

import importlib
import json
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from tests_spend.local_client_fixture import local_client

from spend_app import aggregate, auth_model, auth_throttle, cli, hub_origin, redact, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
HUB_URL = "https://hub.test"
PASSWORD = "synthetic-password-long"
UI = {"Origin": "http://testserver", "X-BURNRATE-Request": "1"}
HUB_UI = {"Origin": HUB_URL, "X-BURNRATE-Request": "1"}
ADMIN = {**HUB_UI, "Authorization": "Bearer synthetic-admin"}


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


def _seed_usage(db_path: Path, raw_id: str, tokens: int) -> None:
    now = datetime.now(UTC).isoformat()
    with connect(db_path) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id=raw_id,
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id=raw_id,
                project="synthetic",
                occurred_at=now,
                input_tokens=tokens,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=0,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0.05,
                ingested_at=now,
                is_exact=True,
            ),
        )


def test_pairing_module_removed_and_hub_origin_extracted() -> None:
    with pytest.raises(ModuleNotFoundError):
        importlib.import_module("spend_app.pairing")

    assert hub_origin.hub_origin("HTTPS://Hub.Test:8449/") == "https://hub.test:8449"
    assert hub_origin.hub_origin("http://127.0.0.1:17331/") == "http://127.0.0.1:17331"
    assert hub_origin.hub_origin("http://localhost:17331") == "http://localhost:17331"
    with pytest.raises(ValueError, match="HTTPS"):
        hub_origin.hub_origin("http://remote-hub.example:8449")
    with pytest.raises(ValueError):
        hub_origin.hub_origin("https://hub.test/subpath")


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("POST", "/api/pairing/redeem", {"code": "brpair_fake", "name": "Laptop"}),
        ("GET", "/api/pairing/redeem", None),
        ("POST", "/api/setup/pair-hub", {"name": "Laptop"}),
        ("POST", "/api/setup/connect-spoke", {"code": "brpair_fake", "name": "Laptop"}),
    ],
)
def test_removed_pairing_endpoints_return_404_or_405(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: dict | None
) -> None:
    auth_throttle.reset()

    # An authorized local client reaches routing and sees the old routes are gone.
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN", raising=False)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    local_settings = _make_settings(tmp_path / "local.db")
    initialize(local_settings.database_path)
    with local_client(create_app(local_settings, enable_scheduler=False), local_settings.database_path) as client:
        local_res = (
            client.get(path, headers=UI)
            if method == "GET"
            else client.post(path, headers=UI, json=body)
        )
        assert local_res.status_code in (404, 405), (path, local_res.status_code, local_res.text)

    # Hub with bearer gating: unauthenticated returns 401 (no longer in _PUBLIC_ACCOUNT_PATHS), admin returns 404/405.
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-admin")
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hs = _make_settings(tmp_path / "hub.db")
    initialize(hs.database_path)
    with TestClient(create_app(hs, enable_scheduler=False), base_url=HUB_URL) as hub_client:
        anon_res = (
            hub_client.get(path, headers=HUB_UI)
            if method == "GET"
            else hub_client.post(path, headers=HUB_UI, json=body)
        )
        assert anon_res.status_code == 401, (path, anon_res.status_code, anon_res.text)

        admin_res = (
            hub_client.get(path, headers=ADMIN)
            if method == "GET"
            else hub_client.post(path, headers=ADMIN, json=body)
        )
        assert admin_res.status_code in (404, 405), (path, admin_res.status_code, admin_res.text)


def test_setup_state_html_ui_and_bridge_omit_pairing_controls(tmp_path: Path) -> None:
    from spend_app.wealthfront_window import DesktopBridge, load_ui_html

    ss = _make_settings(tmp_path / "local.db")
    initialize(ss.database_path)
    with local_client(create_app(ss, enable_scheduler=False), ss.database_path) as client:
        state_res = client.get("/api/setup/state")
        assert state_res.status_code == 200
        state_json = state_res.json()
        assert "networkAddresses" not in state_json
        assert "port" not in state_json

        for removed_page in ("/setup", "/auth"):
            page_res = client.get(removed_page)
            assert page_res.status_code == 404
            assert page_res.json()["error"]["code"] == "REQ-404"

    wf_html = load_ui_html()
    for removed in (
        "setPairMode",
        "submitCreatePair",
        "submitJoinPair",
        "joinCodeInput",
        "join_with_code",
        "/api/setup/pair-hub",
        "/api/setup/connect-spoke",
    ):
        assert removed not in wf_html
    # Legacy self-hosted hub pairing method must remain removed (distinct from cloud_create_pairing_code).
    import re
    assert re.search(r"(?<![A-Za-z0-9_])create_pairing_code\b", wf_html) is None

    assert not hasattr(DesktopBridge, "create_pairing_code")
    assert not hasattr(DesktopBridge, "join_with_code")
    native_src = (ROOT / "spend_app" / "native_desktop.py").read_text(encoding="utf-8")
    for removed in ("create_pairing_code", "join_with_code", "/api/setup/pair-hub", "/api/setup/connect-spoke"):
        assert removed not in native_src


@pytest.mark.parametrize(
    "argv",
    [
        ["burnrate", "machines", "register", "--name", "Laptop"],
        ["burnrate", "sync", "--hub", "https://hub.test", "--once"],
        ["burnrate", "sync", "--token", "br_m_fake", "--once"],
        ["burnrate", "desktop", "--hub", "https://hub.test", "--check-only"],
        ["burnrate", "desktop", "--token", "br_m_fake", "--check-only"],
    ],
)
def test_cli_rejects_removed_pairing_commands_and_flags(
    monkeypatch: pytest.MonkeyPatch, argv: list[str]
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    with pytest.raises(SystemExit) as exc_info:
        cli._main()
    assert exc_info.value.code == 2


def test_cli_retains_supported_sync_and_machines_subcommands(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.delenv("BURNRATE_SYNC_TOKEN", raising=False)
    token_file = tmp_path / "machine.token"
    token_file.write_text("br_m_from_file\n", encoding="utf-8")
    assert cli._resolve_sync_token(str(token_file)) == "br_m_from_file"

    monkeypatch.setenv("BURNRATE_SYNC_TOKEN", "br_m_from_env")
    assert cli._resolve_sync_token(str(token_file)) == "br_m_from_env"
    assert cli._resolve_sync_token() == "br_m_from_env"

    db_path = tmp_path / "machines.db"
    initialize(db_path)
    reg = sync.register_machine(db_path, name="Spoke CLI", os_name="win32")
    monkeypatch.setattr(cli, "load_settings", lambda: _make_settings(db_path))

    monkeypatch.setattr(sys, "argv", ["burnrate", "machines", "list"])
    assert cli._main() == 0
    listed = json.loads(capsys.readouterr().out)
    assert any(m["id"] == reg["machine"]["id"] for m in listed["machines"])

    monkeypatch.setattr(sys, "argv", ["burnrate", "machines", "revoke", "--id", reg["machine"]["id"]])
    assert cli._main() == 0
    revoked = json.loads(capsys.readouterr().out)
    assert all(m["id"] != reg["machine"]["id"] for m in revoked["machines"])


def test_upgrade_drops_pairing_grants_idempotently_and_preserves_paired_machine(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()

    hub_db = tmp_path / "hub-upgrade.db"
    spoke_db = tmp_path / "spoke-upgrade.db"

    # Build pre-0.5.9 databases that already carry auth_model.v1 AND a leftover pairing_grants table.
    hub_reg = sync.register_machine(hub_db, name="Legacy Spoke", os_name="win32", device_id="dev-legacy-spoke-0001")
    machine_id = hub_reg["machine"]["id"]
    token = hub_reg["token"]
    cursor_key = sync._cursor_key(HUB_URL)
    saved_cursor = {"generation": "gen-legacy-01", "credential_hash": sync._token_hash(token), "change_seq": 0}

    with connect(hub_db) as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pairing_grants ("
            "id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, machine_id TEXT, machine_name TEXT, "
            "os TEXT, created_at TEXT NOT NULL, expires_at TEXT NOT NULL, redeemed_at TEXT, revoked_at TEXT)"
        )
        conn.execute(
            "INSERT INTO pairing_grants VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "grant-1",
                "hash-1",
                machine_id,
                "Legacy Spoke",
                "win32",
                auth_model.stamp(auth_model.moment() - timedelta(days=5)),
                auth_model.stamp(auth_model.moment() + timedelta(minutes=10)),
                auth_model.stamp(auth_model.moment() - timedelta(days=5)),
                None,
            ),
        )
        assert conn.execute("SELECT 1 FROM app_meta WHERE key=?", (auth_model.MARKER,)).fetchone()

    initialize(spoke_db)
    _seed_usage(spoke_db, "legacy-spoke-evt", 4200)
    with connect(spoke_db) as conn:
        st = setup_wizard.default_setup_state()
        st["role"] = "spoke"
        st["computerName"] = "Legacy Spoke"
        st["account"]["machineId"] = machine_id
        st["spokeConfig"].update(hubUrl=HUB_URL, token=token, enabled=True)
        setup_wizard.write_setup_state(conn, st)
        conn.execute("INSERT OR REPLACE INTO app_meta(key, value) VALUES(?, ?)", (cursor_key, json.dumps(saved_cursor)))
        conn.execute(
            "CREATE TABLE IF NOT EXISTS pairing_grants (id TEXT PRIMARY KEY, token_hash TEXT NOT NULL)"
        )
        conn.execute("INSERT INTO pairing_grants VALUES('spoke-stale-grant', 'h')")

    # Simulate pre-existing schema needing only auth_model.ensure_schema(), then full initialize() again.
    auth_model.ensure_schema(hub_db)
    auth_model.ensure_schema(hub_db)  # idempotent on second call
    initialize(hub_db)

    auth_model.ensure_schema(spoke_db)
    auth_model.ensure_schema(spoke_db)
    initialize(spoke_db)

    for db_file in (hub_db, spoke_db):
        with sqlite3.connect(db_file) as raw:
            assert (
                raw.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='pairing_grants'").fetchone()
                is None
            )

    # Paired machine record, auth token, and sync cursor survive intact.
    assert sync.authenticate_machine_token(hub_db, token) == machine_id
    with connect(spoke_db) as conn:
        migrated_cursor = json.loads(
            conn.execute("SELECT value FROM app_meta WHERE key=?", (cursor_key,)).fetchone()[0]
        )
    assert migrated_cursor["generation"] == "gen-legacy-01"
    assert migrated_cursor["credential_hash"] == sync._token_hash(token)

    # Upgraded spoke still syncs against hub /api/sync/push, and hub can revoke it without pairing_grants.
    hub_settings = _make_settings(hub_db)
    spoke_settings = _make_settings(spoke_db)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB_URL)
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    with TestClient(create_app(hub_settings, enable_scheduler=False), base_url=HUB_URL) as hub_client:
        monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB_URL, hub_client)
        res = setup_wizard.maybe_run_configured_spoke_sync(
            spoke_settings, PricingEngine.load(spoke_settings.pricing_path), client=hub_client
        )
        assert res is not None and res["ok"], res

    with connect(hub_db) as conn:
        row = conn.execute(
            "SELECT input_tokens FROM usage_events WHERE raw_id=? "
            "UNION ALL SELECT input_tokens FROM unpriced_usage_events WHERE raw_id=?",
            (f"m:{machine_id}:legacy-spoke-evt", f"m:{machine_id}:legacy-spoke-evt"),
        ).fetchone()
    assert row is not None and row[0] == 4200

    revoked = sync.revoke_machine(hub_db, machine_id=machine_id, actor=auth_model.Principal("owner", "o1", "owner"))
    assert all(m["id"] != machine_id for m in revoked["machines"])
    assert sync.authenticate_machine_token(hub_db, token) is None


def test_redact_preserves_legacy_brpair_pattern() -> None:
    sample = "Failed redeeming brpair_abc123XYZ_token_payload for Laptop"
    cleaned = redact.redact(sample)
    assert "brpair_" not in cleaned
    assert "[redacted]" in cleaned
