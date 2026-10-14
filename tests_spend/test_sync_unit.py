from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app import aggregate
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import UsageEvent, connect, initialize, upsert_agent_run, upsert_session, upsert_usage_event
from spend_app.pricing import PricingEngine
from spend_app.sync import sync_once


NOW = datetime.now(timezone.utc)
PRICING_PATH = Path(__file__).resolve().parents[1] / "pricing"


def _make_settings(tmp_path: Path, name: str = "hub.db") -> Settings:
    db_path = tmp_path / name
    initialize(db_path)
    return Settings(
        database_path=db_path,
        pricing_path=PRICING_PATH,
        cursor_import_path=tmp_path / "imports",
        anthropic_admin_key=None,
        openai_admin_key=None,
        cursor_api_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def test_machine_registry_lifecycle_and_token_auth(tmp_path: Path, monkeypatch) -> None:
    aggregate.reset_memo()
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "hub-admin-secret")
    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(PRICING_PATH)
    app = create_app(settings, enable_scheduler=False)
    client = TestClient(app)

    # Admin endpoints require BURNRATE_ACCESS_TOKEN when configured
    unauth = client.get("/api/machines")
    assert unauth.status_code == 401

    admin_headers = {"Authorization": "Bearer hub-admin-secret"}
    initial = client.get("/api/machines", headers=admin_headers)
    assert initial.status_code == 200
    machines = initial.json()["machines"]
    assert len(machines) == 1
    assert machines[0]["id"] == "local"

    # Register a remote computer
    reg = client.post(
        "/api/machines",
        headers=admin_headers,
        json={"action": "register", "name": "MacBook Pro", "os": "darwin"},
    )
    assert reg.status_code == 200
    reg_body = reg.json()
    machine_id = reg_body["machine"]["id"]
    token = reg_body["token"]
    assert token.startswith(f"br_m_{machine_id}_")
    assert "--token" in reg_body["syncCommand"]
    assert len(reg_body["machines"]) == 2

    # /api/sync/push accepts the machine bearer token even when BURNRATE_ACCESS_TOKEN is set
    push_res = client.post(
        "/api/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "machine": {"name": "MacBook Pro", "hostname": "mbp.local", "os": "darwin"},
            "usageEvents": [],
            "sessions": [],
            "agentRuns": [],
        },
    )
    assert push_res.status_code == 200
    assert push_res.json()["machineId"] == machine_id

    # Rename the machine
    ren = client.post(
        "/api/machines",
        headers=admin_headers,
        json={"action": "rename", "id": machine_id, "name": "Work MacBook"},
    )
    assert ren.status_code == 200
    assert any(m["id"] == machine_id and m["name"] == "Work MacBook" for m in ren.json()["machines"])

    # Revoke the machine and verify its token can no longer push
    rev = client.post(
        "/api/machines",
        headers=admin_headers,
        json={"action": "revoke", "id": machine_id},
    )
    assert rev.status_code == 200
    rejected = client.post(
        "/api/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={"usageEvents": []},
    )
    assert rejected.status_code == 401


def test_multi_machine_push_and_filtered_summary_and_entity(tmp_path: Path) -> None:
    aggregate.reset_memo()
    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(PRICING_PATH)

    occurred_local = NOW - timedelta(hours=1)
    local_cost = float(
        pricing.compute(
            model_key="gpt-5.6-sol",
            occurred_at=occurred_local,
            input_tokens=10_000,
            cached_input_tokens=2_000,
            cache_write_tokens=0,
            output_tokens=1_000,
        )
    )
    with connect(settings.database_path) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                raw_id="local-ev-1",
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id="sess-local",
                project="burnrate-hub",
                occurred_at=occurred_local.isoformat(),
                input_tokens=10_000,
                cached_input_tokens=2_000,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=1_000,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=local_cost,
                ingested_at=NOW.isoformat(),
                is_exact=True,
            ),
        )
        upsert_session(
            connection,
            session_id="sess-local",
            tool_key="codex",
            project="burnrate-hub",
            started_at=(NOW - timedelta(hours=1, minutes=10)).isoformat(),
            ended_at=occurred_local.isoformat(),
            model_key="gpt-5.6-sol",
        )
        upsert_agent_run(
            connection,
            id="agent-local",
            name="Hub Codex Agent",
            model_key="gpt-5.6-sol",
            state="live",
            started_at=(NOW - timedelta(minutes=8)).isoformat(),
            last_seen_at=(NOW - timedelta(minutes=1)).isoformat(),
        )

    app = create_app(settings, enable_scheduler=False)
    client = TestClient(app)

    # Register remote computer and push usage + live agent
    reg = client.post("/api/machines", json={"action": "register", "name": "Studio Desktop", "os": "win32"}).json()
    remote_id = reg["machine"]["id"]
    token = reg["token"]

    push = client.post(
        "/api/sync/push",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "machine": {"name": "Studio Desktop", "hostname": "studio-pc", "os": "win32"},
            "usageEvents": [
                {
                    "raw_id": "remote-ev-1",
                    "source": "claude_local",
                    "tool_key": "claude-code",
                    "model_key": "claude-opus-4-6",
                    "session_id": "sess-remote",
                    "project": "shader-engine",
                    "occurred_at": (NOW - timedelta(minutes=30)).isoformat(),
                    "input_tokens": 20_000,
                    "cached_input_tokens": 5_000,
                    "cache_write_tokens": 0,
                    "output_tokens": 4_000,
                }
            ],
            "sessions": [
                {
                    "session_id": "sess-remote",
                    "tool_key": "claude-code",
                    "project": "shader-engine",
                    "started_at": (NOW - timedelta(minutes=45)).isoformat(),
                    "ended_at": (NOW - timedelta(minutes=30)).isoformat(),
                    "model_key": "claude-opus-4-6",
                }
            ],
            "agentRuns": [
                {
                    "id": "agent-remote",
                    "name": "Studio Claude Agent",
                    "model_key": "claude-opus-4-6",
                    "state": "live",
                    "started_at": (NOW - timedelta(minutes=5)).isoformat(),
                    "last_seen_at": (NOW - timedelta(seconds=20)).isoformat(),
                }
            ],
            "replaceAgentRuns": True,
        },
    )
    assert push.status_code == 200
    assert push.json()["eventsWritten"] == 1

    # 1. Summary across all machines includes both computers
    all_summary = client.get("/api/spend/summary?window=1d&tool=all").json()
    assert all_summary["selectedMachine"] == "all"
    assert {m["id"] for m in all_summary["machines"]} == {"local", remote_id}
    assert all_summary["totals"]["records"] == 2
    assert all_summary["totals"]["tokens"] == (10_000 + 1_000) + (20_000 + 4_000)
    assert len(all_summary["activity"]) == 2
    by_agent = {item["name"]: item for item in all_summary["activity"]}
    assert by_agent["Hub Codex Agent"]["machineId"] == "local"
    assert by_agent["Studio Claude Agent"]["machineId"] == remote_id
    assert by_agent["Studio Claude Agent"]["machineName"] == "Studio Desktop"

    # 2. Summary filtered to remote machine only
    remote_summary = client.get(f"/api/spend/summary?window=1d&tool=all&machine={remote_id}").json()
    assert remote_summary["selectedMachine"] == remote_id
    assert remote_summary["totals"]["records"] == 1
    assert remote_summary["totals"]["tokens"] == 24_000
    assert [m["key"] for m in remote_summary["models"]] == ["claude-opus-4-6"]
    assert [a["name"] for a in remote_summary["activity"]] == ["Studio Claude Agent"]

    # 3. Summary filtered to local machine only
    local_summary = client.get("/api/spend/summary?window=1d&tool=all&machine=local").json()
    assert local_summary["selectedMachine"] == "local"
    assert local_summary["totals"]["records"] == 1
    assert local_summary["totals"]["tokens"] == 11_000
    assert [m["key"] for m in local_summary["models"]] == ["gpt-5.6-sol"]
    assert [a["name"] for a in local_summary["activity"]] == ["Hub Codex Agent"]

    # 4. Entity drilldown respects machine filter
    entity_remote = client.get(f"/api/spend/entity?kind=tool&key=claude-code&window=1d&machine={remote_id}").json()
    assert entity_remote["tokens"] == 24_000
    entity_local = client.get("/api/spend/entity?kind=tool&key=claude-code&window=1d&machine=local").json()
    assert entity_local["tokens"] == 0

    # 5. SSE stream endpoint emits initial ready frame
    stream_res = client.get("/api/spend/stream?once=1")
    assert stream_res.status_code == 200
    assert "event: ready" in stream_res.text
    assert remote_id in stream_res.text


def test_end_to_end_sync_once_client_to_hub(tmp_path: Path) -> None:
    aggregate.reset_memo()
    hub_settings = _make_settings(tmp_path, "hub.db")
    remote_settings = _make_settings(tmp_path, "remote.db")
    pricing = PricingEngine.load(PRICING_PATH)

    occurred = NOW - timedelta(minutes=15)
    cost = float(
        pricing.compute(
            model_key="gpt-5.6-sol",
            occurred_at=occurred,
            input_tokens=4_000,
            cached_input_tokens=1_000,
            cache_write_tokens=0,
            output_tokens=500,
        )
    )
    with connect(remote_settings.database_path) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                raw_id="laptop-ev-1",
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id="laptop-sess-1",
                project="mobile-app",
                occurred_at=occurred.isoformat(),
                input_tokens=4_000,
                cached_input_tokens=1_000,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=500,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=cost,
                ingested_at=NOW.isoformat(),
                is_exact=True,
            ),
        )

    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app)
    reg = hub_client.post("/api/machines", json={"action": "register", "name": "Field Laptop"}).json()

    # Run sync_once from remote_settings -> hub_client
    first_sync = sync_once(
        remote_settings,
        pricing,
        hub_url="http://testserver",
        token=reg["token"],
        machine_name="Field Laptop",
        collect_local=False,
        client=hub_client,
    )
    assert first_sync["ok"] is True
    assert first_sync["eventsWritten"] == 1

    # Second sync with no new events writes 0 new usage events (cursor advanced)
    second_sync = sync_once(
        remote_settings,
        pricing,
        hub_url="http://testserver",
        token=reg["token"],
        machine_name="Field Laptop",
        collect_local=False,
        client=hub_client,
    )
    assert second_sync["ok"] is True
    assert second_sync["eventsWritten"] == 0

    summary = hub_client.get("/api/spend/summary?window=1d&tool=all").json()
    assert summary["totals"]["records"] == 1
    assert summary["totals"]["tokens"] == 4_500


def test_sidecar_hud_and_tauri_native_scaffold(tmp_path: Path, monkeypatch) -> None:
    import json
    import sys
    from spend_app.cli import main as cli_main

    settings = _make_settings(tmp_path)
    app = create_app(settings, enable_scheduler=False)
    client = TestClient(app)

    for path in ("/hud", "/?hud=1"):
        res = client.get(path)
        assert res.status_code == 200
        body = res.text
        assert "BURNRATE · Sidecar HUD" in body
        assert "data-tauri-drag-region" in body
        assert "toggle_hud_pin" in body
        assert "show_dashboard" in body
        assert "hide_hud" in body
        assert "/api/spend/stream" in body
        assert 'id="hud-machines"' in body
        assert 'id="hud-agents"' in body
        assert 'id="hud-quotas"' in body

    repo_root = Path(__file__).resolve().parents[1]
    tauri_conf = json.loads((repo_root / "src-tauri" / "tauri.conf.json").read_text(encoding="utf-8"))
    windows = {w["label"]: w for w in tauri_conf["app"]["windows"]}
    assert "main" in windows and "hud" in windows
    assert windows["hud"]["alwaysOnTop"] is True
    assert windows["hud"]["decorations"] is False
    assert windows["hud"]["url"].endswith("/hud")

    lib_rs = (repo_root / "src-tauri" / "src" / "lib.rs").read_text(encoding="utf-8")
    for symbol in (
        "toggle_hud",
        "show_hud",
        "hide_hud",
        "toggle_hud_pin",
        "show_dashboard",
        "open_setup_wizard",
        "TrayIconBuilder",
        "BackendSupervisor",
    ):
        assert symbol in lib_rs

    monkeypatch.setattr(sys, "argv", ["spend_app", "desktop", "--check-only"])
    assert cli_main() == 0


def test_setup_wizard_pairing_and_complete(tmp_path: Path) -> None:
    from decimal import Decimal
    from spend_app.setup_wizard import decode_pairing_code, maybe_run_configured_spoke_sync

    hub_settings = _make_settings(tmp_path / "hub")
    spoke_settings = _make_settings(tmp_path / "spoke")
    pricing = PricingEngine.load(hub_settings.pricing_path)

    hub_app = create_app(hub_settings, enable_scheduler=False)
    hub_client = TestClient(hub_app)

    spoke_app = create_app(spoke_settings, enable_scheduler=False)
    spoke_client = TestClient(spoke_app)

    # 1. Setup Wizard HTML served at /setup, /?setup=1, and automatically on /?app=1 before completion
    for path in ("/setup", "/?setup=1", "/?app=1"):
        res = hub_client.get(path)
        assert res.status_code == 200
        assert "BURNRATE · Application Setup Wizard" in res.text
        assert 'id="wizard-stepper"' in res.text
        assert "/api/setup/complete" in res.text

    fav_res = hub_client.get("/favicon.ico")
    assert fav_res.status_code == 200
    assert len(fav_res.content) > 100

    # 2. Initial setup state
    state_res = hub_client.get("/api/setup/state").json()
    assert state_res["setup"]["completed"] is False
    assert len(state_res["presets"]) >= 4

    # 3. Hub generates 1-click pairing code for Spoke
    pair_res = hub_client.post(
        "/api/setup/pair-hub",
        json={"remoteName": "Studio Laptop", "hubHost": "http://testserver"},
    )
    assert pair_res.status_code == 200
    pair_data = pair_res.json()
    assert pair_data["ok"] is True
    assert pair_data["pairingCode"].startswith("brpair_")
    decoded = decode_pairing_code(pair_data["pairingCode"])
    assert decoded["hubUrl"] == "http://testserver"
    assert decoded["token"].startswith("br_m_")

    # 4. Seed an event on the Spoke and connect Spoke via pairing code
    initialize(spoke_settings.database_path)
    with connect(spoke_settings.database_path) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id="spoke-wiz-ev-1",
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id="sess-spoke-wiz",
                project="wizard-spoke",
                occurred_at=(NOW - timedelta(minutes=12)).isoformat(),
                input_tokens=6_000,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=2_000,
                reasoning_tokens=0,
                cost_usd=None,
                computed_cost_usd=0.09,
                ingested_at=NOW.isoformat(),
                is_exact=True,
            ),
        )

    connect_res = spoke_client.post(
        "/api/setup/connect-spoke",
        json={
            "pairingCode": pair_data["pairingCode"],
            "computerName": "Studio Laptop",
            "testSync": False,
        },
    )
    assert connect_res.status_code == 200
    assert connect_res.json()["setup"]["role"] == "spoke"
    assert connect_res.json()["setup"]["spokeConfig"]["hasToken"] is True

    # Background spoke sync pushes spoke events to hub
    bg_sync = maybe_run_configured_spoke_sync(spoke_settings, pricing, client=hub_client)
    assert bg_sync is not None and bg_sync["ok"] is True
    assert bg_sync["eventsWritten"] == 1

    # 5. Complete setup wizard on Hub with preset & custom subscriptions + computer name
    complete_res = hub_client.post(
        "/api/setup/complete",
        json={
            "computerName": "Main Workstation",
            "role": "hub",
            "presetKeys": ["cursor-pro", "claude-pro"],
            "customPlans": [
                {"tool_key": "codex", "name": "ChatGPT Team", "amount_usd": "30", "cadence": "monthly"}
            ],
            "hudConfig": {"enabled": True, "alwaysOnTop": True},
        },
    )
    assert complete_res.status_code == 200
    complete_data = complete_res.json()
    assert complete_data["ok"] is True
    assert complete_data["setup"]["completed"] is True
    assert complete_data["setup"]["computerName"] == "Main Workstation"
    assert len(complete_data["subscriptions"]) == 3

    onboarding = hub_client.get("/api/onboarding").json()
    assert onboarding["setupCompleted"] is True
    local_machine = next(m for m in onboarding["machines"] if m["id"] == "local")
    assert local_machine["name"] == "Main Workstation"

    # 6. Spoke read-through returns unified Hub machines and usage (both Hub + Spoke)
    from spend_app.sync import register_hub_test_client

    register_hub_test_client("http://testserver", hub_client)
    spoke_machines = spoke_client.get("/api/machines").json()["machines"]
    spoke_machine_names = {m["name"] for m in spoke_machines}
    assert "Main Workstation" in spoke_machine_names
    assert "Studio Laptop" in spoke_machine_names
    spoke_summary = spoke_client.get("/api/spend/summary?window=1d").json()
    assert {m["name"] for m in spoke_summary["machines"]} >= {"Main Workstation", "Studio Laptop"}


def test_central_server_account_login_subscriptions_and_multi_computer_sync(
    tmp_path: Path, monkeypatch
) -> None:
    from spend_app.db import upsert_quota
    from spend_app.setup_wizard import maybe_run_configured_spoke_sync
    from spend_app.sync import register_hub_test_client

    aggregate.reset_memo()
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")

    central_settings = _make_settings(tmp_path / "tk-server")
    desktop_settings = _make_settings(tmp_path / "mydesktop")
    laptop_settings = _make_settings(tmp_path / "macbook")
    pricing = PricingEngine.load(central_settings.pricing_path)

    central_client = TestClient(create_app(central_settings, enable_scheduler=False))
    desktop_client = TestClient(create_app(desktop_settings, enable_scheduler=False))
    laptop_client = TestClient(create_app(laptop_settings, enable_scheduler=False))

    central_url = "https://tk-server.test"
    register_hub_test_client(central_url, central_client)

    # Seed local usage and quota on MyDesktop
    with connect(desktop_settings.database_path) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id="desktop-ev-1",
                source="claude_local",
                tool_key="claude-code",
                model_key="claude-opus-4-6",
                session_id="sess-desktop-1",
                project="burnrate",
                occurred_at=(NOW - timedelta(minutes=20)).isoformat(),
                input_tokens=15_000,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=5_000,
                reasoning_tokens=0,
                cost_usd=None,
                computed_cost_usd=0.25,
                ingested_at=NOW.isoformat(),
                is_exact=True,
            ),
        )
        upsert_quota(
            conn,
            provider_key="cursor",
            limit_key="monthly",
            label="Cursor Plan usage ($385.17 / $400.00)",
            unit="usd",
            source="cursor_usage_service",
            polled_at=NOW.isoformat(),
            used=385.17,
            allowance=400.0,
            pct=96.29,
            resets_at=(NOW + timedelta(days=10)).isoformat(),
            is_payg=0,
        )

    # 1. Create account from MyDesktop with subscriptions
    reg_res = desktop_client.post(
        "/api/setup/account",
        json={
            "mode": "register",
            "username": "kamol",
            "password": "secret-password",
            "computerName": "MyDesktop",
            "serverUrl": central_url,
            "presetKeys": ["claude_max", "cursor_ultra"],
            "customPlans": [
                {"tool_key": "opencode", "name": "Z.AI Coding Plan", "amount_usd": 10.0, "cadence": "monthly"}
            ],
            "testSync": True,
        },
    )
    assert reg_res.status_code == 200
    reg_data = reg_res.json()
    assert reg_data["ok"] is True
    assert reg_data["account"]["username"] == "kamol"
    assert len(reg_data["subscriptions"]) == 3

    # 2. Seed local usage on MacBook and just sign in (no need to re-enter subscriptions)
    with connect(laptop_settings.database_path) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id="laptop-ev-1",
                source="codex_local",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                session_id="sess-laptop-1",
                project="burnrate-mobile",
                occurred_at=(NOW - timedelta(minutes=10)).isoformat(),
                input_tokens=8_000,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=2_000,
                reasoning_tokens=0,
                cost_usd=None,
                computed_cost_usd=0.12,
                ingested_at=NOW.isoformat(),
                is_exact=True,
            ),
        )

    login_res = laptop_client.post(
        "/api/setup/account",
        json={
            "mode": "login",
            "username": "kamol",
            "password": "secret-password",
            "computerName": "MacBook Pro",
            "serverUrl": central_url,
            "testSync": True,
        },
    )
    assert login_res.status_code == 200
    login_data = login_res.json()
    assert login_data["ok"] is True
    assert len(login_data["subscriptions"]) == 3

    # 3. Both computers see unified usage, quotas, subscriptions, and connected computer list from tk-server
    laptop_summary = laptop_client.get("/api/spend/summary?window=1d").json()
    assert laptop_summary["totals"]["records"] == 2
    assert laptop_summary["totals"]["tokens"] == 30_000
    machine_names = [m["name"] for m in laptop_summary["machines"]]
    assert set(machine_names) == {"MyDesktop", "MacBook Pro"}

    laptop_subs = laptop_client.get("/api/subscriptions").json()
    assert len(laptop_subs["plans"]) == 3

    laptop_limits = laptop_client.get("/api/spend/limits").json()
    cursor_limit = next(p for p in laptop_limits["providers"] if p["key"] == "cursor")
    assert cursor_limit["windows"][0]["usedPct"] == 96.29



