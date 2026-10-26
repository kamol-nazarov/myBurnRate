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
        anthropic_admin_key=None,
        openai_admin_key=None,
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
    # The transparent HUD window asks the page for its glass card.
    assert windows["hud"]["url"].endswith("/hud?glass=1")
    assert windows["hud"]["transparent"] is True

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


# Setup changes must come from BURNRATE's own pages (see _setup_request_refused).
SETUP_HEADERS = {"X-BURNRATE-Request": "1"}


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
        headers=SETUP_HEADERS,
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
        headers=SETUP_HEADERS,
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
        headers=SETUP_HEADERS,
        json={
            "computerName": "Main Workstation",
            "role": "hub",
            "presetKeys": ["grok-super", "claude-pro"],
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
            provider_key="grok",
            limit_key="weekly",
            label="Grok Build weekly",
            unit="pct",
            source="traycer_profile",
            polled_at=NOW.isoformat(),
            used=None,
            allowance=None,
            pct=96.29,
            resets_at=(NOW + timedelta(days=10)).isoformat(),
            is_payg=0,
        )

    # 1. Create account from MyDesktop with subscriptions
    reg_res = desktop_client.post(
        "/api/setup/account",
        headers=SETUP_HEADERS,
        json={
            "mode": "register",
            "username": "kamol",
            "password": "secret-password",
            "computerName": "MyDesktop",
            "serverUrl": central_url,
            "presetKeys": ["claude_max", "grok_super"],
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
        headers=SETUP_HEADERS,
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
    grok_limit = next(p for p in laptop_limits["providers"] if p["key"] == "grok")
    assert grok_limit["windows"][0]["usedPct"] == 96.29
    assert all(p["key"] != "cursor" for p in laptop_limits["providers"])


def test_central_server_spoke_heartbeat_keeps_hud_status_live(
    tmp_path: Path, monkeypatch
) -> None:
    from spend_app.sync import apply_sync_push, register_machine

    aggregate.reset_memo()
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    hub_settings = _make_settings(tmp_path / "central-hub")
    pricing = PricingEngine.load(hub_settings.pricing_path)
    initialize(hub_settings.database_path)

    t0 = NOW - timedelta(minutes=10)
    reg = register_machine(
        hub_settings.database_path, name="MyDesktop", os_name="win32", now=t0
    )
    machine_id = reg["machine"]["id"]

    # Initial push with 1 usage event at t0 (10 minutes ago)
    first = apply_sync_push(
        hub_settings.database_path,
        pricing,
        machine_id=machine_id,
        body={
            "machine": {"name": "MyDesktop", "os": "win32"},
            "usageEvents": [
                {
                    "raw_id": "ev-old-1",
                    "source": "codex_local",
                    "tool_key": "codex",
                    "model_key": "gpt-5.6-sol",
                    "session_id": "sess-1",
                    "project": "burnrate",
                    "occurred_at": t0.isoformat(),
                    "input_tokens": 1000,
                    "output_tokens": 500,
                    "ingested_at": t0.isoformat(),
                }
            ],
            "unpricedEvents": [],
            "sessions": [],
            "agentRuns": [],
            "quotas": [],
        },
        now=t0,
    )
    assert first["eventsWritten"] == 1

    # 10 minutes later (NOW), spoke pushes heartbeat/quotas with 0 new usage events
    heartbeat = apply_sync_push(
        hub_settings.database_path,
        pricing,
        machine_id=machine_id,
        body={
            "machine": {"name": "MyDesktop", "os": "win32"},
            "usageEvents": [],
            "unpricedEvents": [],
            "sessions": [],
            "agentRuns": [],
            "quotas": [],
        },
        now=NOW,
    )
    assert heartbeat["eventsWritten"] == 0

    summary = aggregate.aggregate_summary_cached(
        database_path=hub_settings.database_path,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone="UTC",
        cache_threshold=0.5,
        cadence_seconds=15,
        now=NOW,
    )
    assert summary["status"] == "live"


def test_usability_roadmap_endpoints_and_ui_controls(tmp_path: Path) -> None:
    settings = _make_settings(tmp_path / "usability")
    app = create_app(settings, enable_scheduler=False)
    client = TestClient(app)

    # 1. Quota refresh endpoint
    refresh_res = client.post("/api/spend/quotas/refresh")
    assert refresh_res.status_code == 200
    r_data = refresh_res.json()
    assert r_data["ok"] is True
    assert "refreshedAt" in r_data

    # 2. Spoke sync trigger endpoint
    trig_res = client.post("/api/sync/trigger")
    assert trig_res.status_code == 200
    t_data = trig_res.json()
    assert t_data["ok"] is True
    assert "triggeredAt" in t_data
    assert "setup" in t_data

    # 3. Onboarding exposes setup state for navbar sync pill
    onb_res = client.get("/api/onboarding").json()
    assert "setup" in onb_res
    assert "spokeConfig" in onb_res["setup"]

    # 4. Summary capacity rows include polledAt
    summary_res = client.get("/api/spend/summary", params={"window": "1d", "tool": "all"}).json()
    for card in summary_res.get("capacity", []):
        assert "polledAt" in card
        for row in card.get("rows", []):
            assert "polledAt" in row

    # 5. HUD HTML includes Poll Now, Mini mode, Dark mode, Pace tick, and Deep-link drilldown
    hud_html = client.get("/hud").text
    for token in (
        'id="hud-poll-btn"',
        'id="hud-mini-btn"',
        'id="hud-theme-btn"',
        'id="hud-mini-bar"',
        "openToolInDashboard",
        "windowElapsedPct",
        "win-pace-tick",
    ):
        assert token in hud_html, token


def _spoke_with_fake_hub(tmp_path: Path, monkeypatch, payloads: dict[str, dict]):
    from types import SimpleNamespace

    from spend_app import sync as sync_module
    from spend_app.setup_wizard import write_setup_state

    database = tmp_path / "spoke.db"
    initialize(database)
    with connect(database) as connection:
        write_setup_state(
            connection,
            {"role": "spoke", "spokeConfig": {"enabled": True, "hubUrl": "https://hub.test", "token": "spoke-token"}},
        )

    class FakeResponse:
        status_code = 200

        def __init__(self, body: dict) -> None:
            self._body = body

        def json(self) -> dict:
            import copy

            return copy.deepcopy(self._body)

    class FakeHub:
        def get(self, path, params=None, headers=None):
            return FakeResponse(payloads[path])

    monkeypatch.setitem(sync_module._HUB_TEST_CLIENTS, "https://hub.test", FakeHub())
    sync_module.clear_hub_proxy_cache()
    return SimpleNamespace(database_path=database)


def _iso(moment: datetime) -> str:
    return moment.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def test_spoke_masks_expired_hub_capacity_rows_without_inferring_zero(tmp_path: Path, monkeypatch) -> None:
    from spend_app.db import upsert_quota
    from spend_app.sync import fetch_from_configured_hub

    now = datetime.now(timezone.utc)
    past, future = _iso(now - timedelta(hours=5)), _iso(now + timedelta(days=6))
    synced = "sync:mydesktop-ed05:muse_quota_endpoint"
    hub_summary = {
        "capacity": [
            {
                "providerKey": "muse", "providerName": "Muse Code", "isPayg": False,
                "peakPct": 77.0, "primaryPct": 77.0, "forecast": {"etaHours": 1},
                "rows": [
                    {"limitKey": "5h", "label": "Muse Code 5-hour window", "pct": 77.0, "used": None, "allowance": None,
                     "unit": "pct", "resetsAt": past, "etaLabel": "due", "source": synced, "isPayg": False,
                     "order": 0, "isPrimary": True, "forecast": {"etaHours": 1}},
                    {"limitKey": "weekly", "label": "Muse Code weekly window", "pct": 25.0, "used": None, "allowance": None,
                     "unit": "pct", "resetsAt": future, "etaLabel": "6d", "source": synced, "isPayg": False,
                     "order": 1, "isPrimary": False, "forecast": None},
                ],
            },
            {
                "providerKey": "openrouter", "providerName": "OpenRouter", "isPayg": True,
                "rows": [
                    {"limitKey": "balance", "label": "OpenRouter balance", "pct": None, "used": 12.5, "allowance": None,
                     "unit": "usd", "resetsAt": past, "etaLabel": "available", "source": "sync:hub:openrouter",
                     "isPayg": True, "order": 0, "isPrimary": True},
                ],
            },
            {
                "providerKey": "grok", "providerName": "Grok", "isPayg": False, "peakPct": 10.0, "primaryPct": 10.0,
                "rows": [
                    {"limitKey": "weekly", "label": "Grok weekly window", "pct": 10.0, "used": None, "allowance": None,
                     "unit": "pct", "resetsAt": past, "etaLabel": "due", "source": "sync:mydesktop-ed05:grok_local",
                     "isPayg": False, "order": 0, "isPrimary": True},
                ],
            },
        ]
    }
    settings = _spoke_with_fake_hub(tmp_path, monkeypatch, {"/api/spend/summary": hub_summary})
    # A fresher local reading still replaces the masked Hub row.
    with connect(settings.database_path) as connection:
        upsert_quota(
            connection, provider_key="grok", limit_key="weekly", label="Grok weekly window", unit="pct",
            source="grok_local", polled_at=_iso(now), pct=42.0, resets_at=future,
        )

    data = fetch_from_configured_hub(settings, "/api/spend/summary", {"window": "1d"})
    cards = {card["providerKey"]: card for card in data["capacity"]}

    muse = {row["limitKey"]: row for row in cards["muse"]["rows"]}
    assert muse["5h"]["pct"] is None
    assert muse["5h"]["unit"] == "unavailable"
    assert muse["5h"]["resetsAt"] is None and muse["5h"]["etaLabel"] is None
    assert muse["5h"]["forecast"] is None
    assert muse["5h"]["source"] == synced
    assert muse["5h"]["label"].startswith("Muse Code 5-hour window — ")
    assert "No new zero was inferred" in muse["5h"]["label"]
    assert muse["weekly"]["pct"] == 25.0 and muse["weekly"]["resetsAt"] == future
    assert cards["muse"]["primaryPct"] is None and cards["muse"]["peakPct"] is None
    assert cards["muse"]["forecast"] is None

    balance = cards["openrouter"]["rows"][0]
    assert balance["used"] == 12.5 and balance["unit"] == "usd"

    grok = cards["grok"]["rows"][0]
    assert grok["pct"] == 42.0 and grok["source"] == "grok_local"


def test_spoke_drops_expired_hub_limits_windows(tmp_path: Path, monkeypatch) -> None:
    from spend_app.sync import fetch_from_configured_hub

    now = datetime.now(timezone.utc)
    past, future = _iso(now - timedelta(hours=5)), _iso(now + timedelta(days=6))
    hub_limits = {
        "providers": [
            {"key": "muse", "name": "muse", "status": "exact", "detail": "Muse Code 5-hour window", "windows": [
                {"key": "5h", "label": "Muse Code 5-hour window", "usedPct": 77.0, "used": None, "limit": None, "resetAt": past},
                {"key": "weekly", "label": "Muse Code weekly window", "usedPct": 25.0, "used": None, "limit": None, "resetAt": future},
            ]},
            {"key": "grok", "name": "grok", "status": "exact", "detail": "Grok weekly window", "windows": [
                {"key": "weekly", "label": "Grok weekly window", "usedPct": 10.0, "used": None, "limit": None, "resetAt": past},
            ]},
        ],
        "snapshot": True,
    }
    settings = _spoke_with_fake_hub(tmp_path, monkeypatch, {"/api/spend/limits": hub_limits})

    data = fetch_from_configured_hub(settings, "/api/spend/limits")
    providers = {provider["key"]: provider for provider in data["providers"]}
    assert [w["key"] for w in providers["muse"]["windows"]] == ["weekly"]
    assert providers["muse"]["status"] == "exact"
    assert providers["grok"]["windows"] == []
    assert providers["grok"]["status"] == "unavailable"


def test_spoke_overlay_never_resurrects_a_reset_superseded_or_older_local_reading(tmp_path: Path, monkeypatch) -> None:
    # Regression: the overlay took the newest *available* local row, so a
    # 77% Muse 5-hour reading from the morning kept being pasted over the Hub
    # long after its window reset and newer local polls said "unavailable".
    from spend_app.db import upsert_quota
    from spend_app.sync import fetch_from_configured_hub

    now = datetime.now(timezone.utc)
    past, future = _iso(now - timedelta(hours=13)), _iso(now + timedelta(days=6))
    synced = "sync:mydesktop-ed05:muse_quota_endpoint"
    hub_summary = {
        "capacity": [
            {
                "providerKey": "muse", "providerName": "Muse Code", "isPayg": False, "peakPct": 77.0, "primaryPct": 77.0,
                "rows": [
                    {"limitKey": "5h", "label": "Muse Code 5-hour window", "pct": 77.0, "unit": "pct", "resetsAt": past,
                     "polledAt": _iso(now - timedelta(hours=13)), "source": synced, "isPayg": False, "order": 0, "isPrimary": True},
                    {"limitKey": "weekly", "label": "Muse Code weekly window", "pct": 25.0, "unit": "pct", "resetsAt": future,
                     "polledAt": _iso(now - timedelta(minutes=10)), "source": synced, "isPayg": False, "order": 1, "isPrimary": False},
                ],
            },
            {
                "providerKey": "grok", "providerName": "Grok", "isPayg": False, "peakPct": 10.0, "primaryPct": 10.0,
                "rows": [
                    {"limitKey": "weekly", "label": "Grok weekly window", "pct": 10.0, "unit": "pct", "resetsAt": future,
                     "polledAt": _iso(now - timedelta(minutes=30)), "source": "sync:mydesktop-ed05:grok_local",
                     "isPayg": False, "order": 0, "isPrimary": True},
                ],
            },
        ]
    }
    settings = _spoke_with_fake_hub(tmp_path, monkeypatch, {"/api/spend/summary": hub_summary})
    with connect(settings.database_path) as connection:
        # Morning reading, then the endpoint stopped reporting the window.
        upsert_quota(connection, provider_key="muse", limit_key="5h", label="Muse Code 5-hour window", unit="pct",
                     source="muse_quota_endpoint", polled_at=_iso(now - timedelta(hours=13)), pct=77.0, resets_at=past)
        upsert_quota(connection, provider_key="muse", limit_key="5h", label="Muse Code 5-hour window — no usage reported",
                     unit="unavailable", source="muse_quota_endpoint", polled_at=_iso(now - timedelta(minutes=1)))
        # A local weekly reading older than the Hub's must not win.
        upsert_quota(connection, provider_key="muse", limit_key="weekly", label="Muse Code weekly window", unit="pct",
                     source="muse_quota_endpoint", polled_at=_iso(now - timedelta(hours=2)), pct=30.0, resets_at=future)
        # A fresher local reading still wins.
        upsert_quota(connection, provider_key="grok", limit_key="weekly", label="Grok weekly window", unit="pct",
                     source="grok_local", polled_at=_iso(now - timedelta(seconds=20)), pct=12.0, resets_at=future)

    data = fetch_from_configured_hub(settings, "/api/spend/summary", {"window": "1d"})
    cards = {card["providerKey"]: card for card in data["capacity"]}
    muse = {row["limitKey"]: row for row in cards["muse"]["rows"]}
    assert muse["5h"]["pct"] is None and muse["5h"]["unit"] == "unavailable"
    assert "No new zero was inferred" in muse["5h"]["label"]
    assert cards["muse"]["primaryPct"] is None
    assert muse["weekly"]["pct"] == 25.0 and muse["weekly"]["source"] == synced
    grok = cards["grok"]["rows"][0]
    assert grok["pct"] == 12.0 and grok["source"] == "grok_local"


def test_spoke_overlay_skips_a_local_reading_whose_window_already_reset(tmp_path: Path, monkeypatch) -> None:
    from spend_app.db import upsert_quota
    from spend_app.sync import fetch_from_configured_hub

    now = datetime.now(timezone.utc)
    hub_summary = {"capacity": [{
        "providerKey": "grok", "providerName": "Grok", "isPayg": False, "peakPct": None, "primaryPct": None,
        "rows": [{"limitKey": "weekly", "label": "Grok weekly window", "pct": None, "unit": "unavailable",
                  "resetsAt": None, "polledAt": None, "source": "unavailable", "isPayg": False, "order": 0, "isPrimary": True}],
    }]}
    settings = _spoke_with_fake_hub(tmp_path, monkeypatch, {"/api/spend/summary": hub_summary})
    with connect(settings.database_path) as connection:
        upsert_quota(connection, provider_key="grok", limit_key="weekly", label="Grok weekly window", unit="pct",
                     source="grok_local", polled_at=_iso(now - timedelta(days=8)), pct=96.0,
                     resets_at=_iso(now - timedelta(days=1)))

    data = fetch_from_configured_hub(settings, "/api/spend/summary", {"window": "1d"})
    row = data["capacity"][0]["rows"][0]
    assert row["pct"] is None and row["unit"] == "unavailable"


def test_cached_hub_payload_is_remasked_and_never_mutated() -> None:
    from spend_app.sync import _mask_expired_hub_quotas

    reset = datetime(2026, 10, 19, 10, 45, 17, tzinfo=timezone.utc)
    cached = {
        "capacity": [{"providerKey": "muse", "isPayg": False, "primaryPct": 77.0, "peakPct": 77.0, "rows": [
            {"limitKey": "5h", "label": "Muse Code 5-hour window", "pct": 77.0, "unit": "pct",
             "resetsAt": _iso(reset), "isPayg": False, "isPrimary": True},
        ]}],
        "providers": [{"key": "muse", "status": "exact", "windows": [
            {"key": "5h", "usedPct": 77.0, "resetAt": _iso(reset)},
        ]}],
    }
    before = _mask_expired_hub_quotas(cached, reset - timedelta(minutes=4))
    assert before["capacity"][0]["rows"][0]["pct"] == 77.0
    assert before["providers"][0]["windows"][0]["usedPct"] == 77.0

    after = _mask_expired_hub_quotas(cached, reset + timedelta(seconds=1))
    assert after["capacity"][0]["rows"][0]["pct"] is None
    assert after["capacity"][0]["primaryPct"] is None
    assert after["providers"][0]["windows"] == []
    assert after["providers"][0]["status"] == "unavailable"
    # The cached copy keeps the Hub's raw values for the next re-mask.
    assert cached["capacity"][0]["rows"][0]["pct"] == 77.0
    assert cached["providers"][0]["windows"][0]["usedPct"] == 77.0
