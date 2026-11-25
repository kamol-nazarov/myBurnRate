"""Account setup regressions using synthetic hub and spoke databases."""

from datetime import date
from pathlib import Path
import secrets

import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate, auth_throttle, setup_wizard, sync
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.plan_service import (
    apply_mutation,
    create_plan,
    effective_terms,
    list_plans,
)
from tests_spend.local_client_fixture import local_client

HUB = "https://hub.test"
UI = {"Origin": "http://testserver", "X-BURNRATE-Request": "1"}
HUB_UI = {"Origin": HUB, "X-BURNRATE-Request": "1"}
OWNER = {"username": "plans@example.com", "password": "synthetic-long-password"}


def _settings(path):
    return Settings(
        database_path=path,
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


@pytest.fixture
def clients(tmp_path, monkeypatch):
    auth_throttle.reset()
    sync.clear_hub_proxy_cache()
    aggregate.reset_memo()
    hs, ss = (_settings(tmp_path / path) for path in ("hub.db", "spoke.db"))
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-local-admin")
    setup_credential = secrets.token_urlsafe(24)
    monkeypatch.setenv("BURNRATE_SETUP_TOKEN", setup_credential)
    hub = TestClient(create_app(hs, enable_scheduler=False), base_url=HUB)
    assert (
        hub.post("/api/account/register", headers=HUB_UI, json=OWNER | {"setupToken": setup_credential}).status_code == 200
    )
    hub.cookies.clear()
    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB, hub)
    monkeypatch.setenv("BURNRATE_HUB_URL", HUB)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER")
    monkeypatch.delenv("BURNRATE_ACCESS_TOKEN")
    monkeypatch.delenv("BURNRATE_SETUP_TOKEN")
    spoke = local_client(
        create_app(ss, enable_scheduler=False), ss.database_path, base_url="http://testserver"
    )
    yield hs, hub, ss, spoke
    spoke.close()
    hub.close()
    sync.clear_hub_proxy_cache()


def _add(settings, name, *, start="2026-01-01", end=None, amount=200, tool="custom"):
    initialize(settings.database_path)
    with connect(settings.database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        return apply_mutation(
            conn,
            {
                "operation": "add",
                "request_id": f"add-{name.replace(' ', '-')}-{start}-{amount}",
                "values": {
                    "name": name,
                    "tool_key": tool,
                    "start_date": start,
                    "end_date": end,
                    "amount_usd": amount,
                    "cadence": "monthly",
                },
            },
            date(2026, 1, 1),
        )["id"]


def _sign_in(spoke, **fields):
    reply = spoke.post(
        "/api/setup/account",
        headers=UI,
        json={
            **OWNER,
            "mode": "login",
            "computerName": "Laptop",
            "testSync": False,
            **fields,
        },
    )
    assert reply.status_code == 200, reply.text
    return reply.json()


def _stored(settings):
    with connect(settings.database_path) as conn:
        return list_plans(conn, date(2026, 11, 1))["plans"]


def test_sign_in_transfers_dated_terms_once_and_keeps_hub_changes(clients):
    hs, _, ss, spoke = clients
    hub_id = _add(hs, "Shared label", amount=25)
    canceled = _add(ss, "Ended plan", end="2026-02-28")
    scheduled = _add(ss, "Scheduled plan", start="2040-04-01", tool="codex")
    recurring = _add(ss, "Shared label", amount=10)
    assert canceled == hub_id
    with connect(ss.database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        apply_mutation(
            conn,
            {
                "operation": "schedule",
                "request_id": "price-revision",
                "plan_id": recurring,
                "expected_version": 1,
                "effective_date": "2026-12-01",
                "values": {"amount_usd": 20, "cadence": "monthly"},
            },
            date(2026, 1, 1),
        )

    _sign_in(spoke, presetKeys=["claude_pro"])
    plans = _stored(hs)
    assert len(plans) == len(_stored(ss)) == 5
    ended = next(p for p in plans if p["terms"][0]["name"] == "Ended plan")
    assert ended["id"] != hub_id
    assert ended["end_date"] == "2026-02-28"
    assert [(t["start_date"], t["end_date"], t["status"]) for t in ended["terms"]] == [
        ("2026-01-01", "2026-02-28", "ended")
    ]
    with connect(hs.database_path) as conn:
        assert not any(
            t["name"] == "Ended plan"
            and t["start_date"] <= "2026-11-01" <= (t["end_date"] or "9999-12-31")
            for t in effective_terms(conn)
        )
    comparison = [p for p in plans if p["terms"][0]["name"] == "Shared label"]
    hosted = next(p for p in comparison if p["id"] == hub_id)
    adopted = next(p for p in comparison if p["id"] != hub_id)
    assert hosted["terms"][0]["amount_usd"] == "25.0"
    assert [
        (t["start_date"], t["end_date"], t["amount_usd"]) for t in adopted["terms"]
    ] == [
        ("2026-01-01", "2026-11-30", "10.0"),
        ("2026-12-01", None, "20.0"),
    ]
    assert any(p["terms"][0]["start_date"] == "2040-04-01" for p in plans)

    with connect(hs.database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        apply_mutation(
            conn,
            {
                "operation": "end",
                "request_id": "end-hub-owner-plan",
                "plan_id": adopted["id"],
                "expected_version": 1,
                "end_date": "2026-11-17",
            },
            date(2026, 11, 17),
        )
    _sign_in(spoke)
    assert len(_stored(hs)) == len(_stored(ss)) == 5
    assert (
        next(p for p in _stored(ss) if p["id"] == recurring)["end_date"] == "2026-11-17"
    )
    assert (
        next(p for p in _stored(ss) if p["id"] == scheduled)["terms"][0]["start_date"]
        == "2040-04-01"
    )


def test_upgraded_hub_withholds_undated_legacy_sign_in_history(clients):
    hs, hub, _, _ = clients
    reply = hub.post(
        "/api/account/login",
        headers=HUB_UI,
        json={
            **OWNER,
            "presetKeys": ["codex_plus"],
            "customPlans": [
                {"name": "Ended plan", "tool_key": "custom", "amount_usd": 200}
            ],
        },
    )
    assert reply.status_code == 200
    assert [p["terms"][0]["name"] for p in _stored(hs)] == ["ChatGPT Plus"]


def test_new_spoke_preserves_local_history_when_old_hub_cannot_import(
    clients, monkeypatch
):
    hs, hub, ss, spoke = clients
    _add(ss, "Ended plan", end="2026-02-28")
    _add(hs, "Legacy hub plan", end="2025-06-30", start="2025-01-01")

    class LegacyHub:
        def __getattr__(self, name):
            return getattr(hub, name)

        def post(self, path, *, json, **kwargs):
            if path == "/api/account/login":
                assert json.get("customPlans", []) == []
                json = {
                    k: v
                    for k, v in json.items()
                    if k not in {"subscriptionHistory", "newCustomPlans"}
                }
            return hub.post(path, json=json, **kwargs)

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB, LegacyHub())
    result = _sign_in(spoke)
    assert result["subscriptionHistoryPending"] is True
    assert [p["terms"][0]["name"] for p in _stored(hs)] == ["Legacy hub plan"]
    assert {p["terms"][0]["name"]: p["end_date"] for p in _stored(ss)} == {
        "Ended plan": "2026-02-28",
        "Legacy hub plan": "2025-06-30",
    }

    monkeypatch.setitem(sync._HUB_TEST_CLIENTS, HUB, hub)
    upgrade = _sign_in(spoke)
    assert upgrade["subscriptionHistoryPending"] is False
    assert len(_stored(hs)) == len(_stored(ss)) == 2


def test_dated_import_keeps_older_association_and_fractional_amount(clients):
    hs, _, ss, spoke = clients
    plan = _add(ss, "Research plan", end="2026-02-28")
    long_name = "Longer historical subscription name retained during transfer " * 2
    with connect(ss.database_path) as conn:
        conn.execute(
            "UPDATE subscriptions SET name=?,tool_key=?,amount_usd=? WHERE plan_id=?",
            (long_name, "research-assistant", 19.995, plan),
        )
    assert _sign_in(spoke)["subscriptionHistoryPending"] is False
    assert len(_stored(hs)) == len(_stored(ss)) == 1
    term = _stored(hs)[0]["terms"][0]
    assert (term["name"], term["tool_key"], term["amount_usd"], term["end_date"]) == (
        long_name,
        "research-assistant",
        "19.995",
        "2026-02-28",
    )


def test_long_history_transfers_in_bounded_batches(clients):
    hs, _, ss, spoke = clients
    initialize(ss.database_path)
    with connect(ss.database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        for i in range(270):
            create_plan(
                conn,
                {
                    "name": f"Historical {i:03d}",
                    "tool_key": "codex",
                    "amount_usd": 25,
                    "cadence": "monthly",
                    "start_date": "2026-01-01",
                    "end_date": "2026-02-28",
                },
            )
    result = _sign_in(spoke)
    assert result["subscriptionHistoryPending"] is False
    assert len(_stored(hs)) == len(_stored(ss)) == 270
    assert all(p["end_date"] == "2026-02-28" for p in _stored(hs))
    _sign_in(spoke)
    assert len(_stored(hs)) == 270


def test_desktop_sign_in_transfers_history_through_native_entrypoint(
    clients, monkeypatch
):
    from spend_app.native_desktop import NativeClient
    from spend_app.wealthfront_window import DesktopBridge

    hs, _, ss, _ = clients
    _add(ss, "Former plan", end="2026-02-28")
    _add(hs, "Hub plan", amount=15)
    bridge = DesktopBridge(NativeClient(ss), demo=False)
    monkeypatch.setattr(bridge, "_run_background_first_sync", lambda **_kwargs: None)
    result = bridge.sign_in(OWNER["username"], OWNER["password"], "Laptop")
    assert result["ok"] is True, result
    assert "warning" not in result
    assert sorted(p["terms"][0]["name"] for p in _stored(ss)) == [
        "Former plan",
        "Hub plan",
    ]
    assert (
        next(p for p in _stored(hs) if p["terms"][0]["name"] == "Former plan")[
            "end_date"
        ]
        == "2026-02-28"
    )


def _confirm_fields(issue):
    return {
        name: issue[name]
        for name in ("planId", "termId", "version", "fix", "confirmation")
        if name in issue
    }


def test_setup_plan_confirmation_cannot_fall_back_to_another_database(
    clients, monkeypatch
):
    hs, _, ss, spoke = clients
    hub_id = _add(hs, "Codex Family")
    local_id = _add(ss, "Antigravity Personal")
    assert hub_id == local_id
    _sign_in(spoke)
    displayed = spoke.get("/api/setup/plan-issues").json()
    assert displayed["where"] == "hub"
    issue = next(item for item in displayed["issues"] if item["planId"] == hub_id)
    assert issue["name"] == "Codex Family"
    confirmation = _confirm_fields(issue)
    before_hub, before_spoke = _stored(hs), _stored(ss)
    with monkeypatch.context() as offline:
        offline.setattr(
            sync, "fetch_from_configured_hub", lambda *_args, **_kwargs: None
        )
        failed = spoke.post("/api/setup/plans/fix", headers=UI, json=confirmation)
        assert failed.status_code == 409, failed.text
        assert spoke.get("/api/setup/plan-issues").json() == {
            "issues": [],
            "where": "hub",
            "available": False,
        }
    assert _stored(hs) == before_hub
    assert _stored(ss) == before_spoke

    fixed = spoke.post("/api/setup/plans/fix", headers=UI, json=confirmation)
    assert fixed.status_code == 200, fixed.text
    assert (
        next(p for p in _stored(hs) if p["id"] == hub_id)["terms"][0]["tool_key"]
        == "codex"
    )
    assert _stored(ss) == before_spoke
    assert (
        spoke.post("/api/setup/plans/fix", headers=UI, json=confirmation).status_code
        == 409
    )


def test_setup_plan_confirmation_rejects_a_changed_pairing(clients):
    hs, _, ss, spoke = clients
    _add(hs, "Codex Family")
    _sign_in(spoke)
    issue = spoke.get("/api/setup/plan-issues").json()["issues"][0]
    before = _stored(hs)
    with connect(ss.database_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        state = setup_wizard.read_setup_state(conn)
        state["spokeConfig"]["credentialGeneration"] += 1
        setup_wizard.write_setup_state(conn, state)
    reply = spoke.post(
        "/api/setup/plans/fix",
        headers=UI,
        json=_confirm_fields(issue),
    )
    assert reply.status_code == 409
    assert _stored(hs) == before
