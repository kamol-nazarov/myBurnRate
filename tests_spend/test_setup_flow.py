"""The setup wizard: role first, honest per-card checks, plans and balances, then a live check."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spend_app import claude_accounts
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connections import suppresses_default_profile
from spend_app.db import connect, initialize, upsert_quota
from spend_app.hud import render_hud
from spend_app.pricing import PricingEngine
from spend_app.setup_wizard import setup_router

ROOT = Path(__file__).resolve().parents[1]
SETUP = {"X-BURNRATE-Request": "1"}


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> Settings:
    monkeypatch.delenv("BURNRATE_ENABLE_DASHBOARD", raising=False)
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    value = Settings(database_path=tmp_path / "spend.db", pricing_path=ROOT / "pricing",
                     anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                     cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(value.database_path)
    return value


@pytest.fixture
def client(settings: Settings) -> TestClient:
    return TestClient(create_app(settings, enable_scheduler=False))


def test_the_wizard_has_four_steps_and_no_dead_end(client: TestClient) -> None:
    page = client.get("/setup").text
    for step in range(1, 5):
        assert f'id="wiz-step-{step}"' in page
    assert "Just this computer" in page and "Join my hub" in page and "Make this the hub" in page
    assert "Set up later" in page and "Skip to" not in page
    # No fake password: the field is empty until the user types.
    assert 'placeholder="••••••••"' not in page
    assert '"X-BURNRATE-Request"' in page


def test_setup_changes_need_the_burnrate_header(client: TestClient) -> None:
    for path in ("/api/setup/complete", "/api/setup/defer", "/api/setup/consent", "/api/setup/scan",
                 "/api/setup/pair-hub", "/api/setup/connect-spoke", "/api/setup/account"):
        response = client.post(path, json={})
        assert response.status_code == 403, path
        assert "setup page" in response.json()["error"]


def test_set_up_later_stops_the_wizard_opening_on_every_launch(client: TestClient) -> None:
    assert 'id="wizard-stepper"' in client.get("/?app=1").text
    response = client.post("/api/setup/defer", json={}, headers=SETUP)
    assert response.status_code == 200 and response.json()["setup"]["deferredAt"]
    assert 'id="wizard-stepper"' not in client.get("/?app=1").text
    # Still reachable on purpose.
    assert 'id="wizard-stepper"' in client.get("/setup").text
    finished = client.post("/api/setup/complete", json={"role": "standalone"}, headers=SETUP).json()
    assert finished["setup"]["completed"] is True and finished["setup"]["role"] == "standalone"
    assert finished["setup"]["deferredAt"] is None


def test_signing_in_never_creates_an_account(client: TestClient) -> None:
    unknown = client.post("/api/account/login", json={"username": "alex", "password": "long-enough-1"})
    assert unknown.status_code == 401 and "no account named alex" in unknown.json()["error"].lower()
    short = client.post("/api/account/register", json={"username": "alex", "password": "short"})
    assert short.status_code == 422 and "8 characters" in short.json()["error"]
    assert client.post("/api/account/register", json={"username": "alex", "password": "long-enough-1"}).status_code == 200
    assert client.post("/api/account/login", json={"username": "alex", "password": "long-enough-1"}).status_code == 200
    assert client.post("/api/account/login", json={"username": "alex", "password": "wrong-password"}).status_code == 401


def test_registering_from_the_wizard_checks_the_password_before_calling_the_hub(client: TestClient) -> None:
    response = client.post("/api/setup/account", headers=SETUP, json={
        "mode": "register", "username": "alex", "password": "short", "serverUrl": "https://unreachable.invalid"})
    assert response.status_code == 422 and "8 characters" in response.json()["error"]


def test_consent_switches_apply_now_and_are_saved(client: TestClient, tmp_path: Path, monkeypatch) -> None:
    import os

    for name in ("BURNRATE_ENABLE_ZAI_QUOTA", "BURNRATE_ENABLE_OPENCODE_GO_QUOTA"):
        monkeypatch.setenv(name, "1")
    monkeypatch.setattr("spend_app.config.default_app_dir", lambda: tmp_path / "app")
    state = client.get("/api/setup/state").json()
    assert {item["key"]: item["enabled"] for item in state["consent"]}["zai"] is True
    response = client.post("/api/setup/consent", headers=SETUP, json={"switches": {"zai": False, "opencode_go": False}})
    assert response.status_code == 200
    assert {item["key"]: item["enabled"] for item in response.json()["consent"]} == {
        "zai": False, "opencode_go": False}
    assert os.environ["BURNRATE_ENABLE_ZAI_QUOTA"] == "0" and os.environ["BURNRATE_ENABLE_OPENCODE_GO_QUOTA"] == "0"
    saved = (tmp_path / "app" / ".env").read_text(encoding="utf-8")
    assert "BURNRATE_ENABLE_ZAI_QUOTA=0" in saved and "BURNRATE_ENABLE_OPENCODE_GO_QUOTA=0" in saved
    # There is no Claude switch: Claude's sign-in is never reused.
    for bad_switches in ({"everything": True}, {"claude": True}):
        bad = client.post("/api/setup/consent", headers=SETUP, json={"switches": bad_switches})
        assert bad.status_code == 422


def _quota(connection, provider: str, limit: str, *, pct=None, unit="pct", label=None, polled: datetime,
           resets: datetime | None = None) -> None:
    upsert_quota(connection, provider_key=provider, limit_key=limit, label=label or f"{provider} {limit}",
                 used=None, allowance=None, unit=unit, pct=pct, resets_at=iso(resets) if resets else None,
                 source=f"{provider}_test", polled_at=iso(polled), is_payg=False)


def test_check_says_which_meters_are_current_held_or_missing(client: TestClient, settings: Settings) -> None:
    now = datetime.now(UTC)
    with connect(settings.database_path) as connection:
        _quota(connection, "codex", "weekly", pct=22.0, polled=now, resets=now + timedelta(days=3))
        _quota(connection, "antigravity", "gemini-5h", pct=6.0, polled=now - timedelta(days=2),
               resets=now + timedelta(hours=3))
        _quota(connection, "muse", "5h", unit="unavailable", polled=now,
               label="Muse Code 5-hour window \u2014 No usage window was reported for this account.")
    data = client.get("/api/setup/check").json()
    lanes = {row["key"]: row for row in data["lanes"]}
    assert lanes["codex"]["meter"]["state"] == "ok" and lanes["codex"]["meter"]["text"].startswith("22%")
    assert lanes["antigravity"]["meter"]["state"] == "held"
    assert "Open Antigravity" in lanes["antigravity"]["meter"]["fix"]
    assert lanes["muse"]["meter"]["state"] == "unavailable"
    assert lanes["muse"]["meter"]["detail"] == "No usage window was reported for this account."
    assert lanes["opencode"]["consent"] == "zai" and lanes["codex"]["consent"] is None
    # Measured lanes without a plan get a suggestion; tracked ones do not.
    suggested = {item["tool_key"] for item in data["plans"]["suggestions"]}
    assert {"codex", "antigravity"} <= suggested and "muse" not in suggested


def test_a_tracked_plan_is_not_suggested_again(client: TestClient, settings: Settings) -> None:
    now = datetime.now(UTC)
    with connect(settings.database_path) as connection:
        _quota(connection, "codex", "weekly", pct=22.0, polled=now, resets=now + timedelta(days=3))
    client.post("/api/setup/complete", headers=SETUP, json={
        "role": "standalone", "customPlans": [{"tool_key": "codex", "name": "ChatGPT Pro", "amount_usd": 200}]})
    data = client.get("/api/setup/check").json()
    assert "codex" not in {item["tool_key"] for item in data["plans"]["suggestions"]}
    assert any(plan["name"] == "ChatGPT Pro" for plan in data["plans"]["active"])


def test_progress_lists_each_sources_latest_run(client: TestClient, settings: Settings) -> None:
    with connect(settings.database_path) as connection:
        connection.execute("INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written) "
                           "VALUES('codex_local', '2026-10-26T04:00:00Z', '2026-10-26T04:00:05Z', 'success', 12)")
    data = client.get("/api/setup/progress").json()
    codex = next(item for item in data["sources"] if item["source"] == "codex_local")
    assert (codex["name"], codex["status"], codex["events"]) == ("Codex", "success", 12)
    assert "running" in data["heartbeat"]


def test_scan_returns_at_once_and_asks_the_scheduler_to_collect(settings: Settings) -> None:
    calls = []
    app = FastAPI()
    app.include_router(setup_router(settings, PricingEngine.load(settings.pricing_path), collect_now=lambda: calls.append(1)))
    response = TestClient(app).post("/api/setup/scan", json={}, headers=SETUP)
    assert response.status_code == 200 and response.json()["started"]
    assert calls == [1]


def test_connections_page_opens_the_balances_step(client: TestClient) -> None:
    response = client.get("/connections", follow_redirects=False)
    assert response.status_code == 307 and response.headers["location"] == "/setup?step=3"
    connections = client.get("/api/setup/state").json()["apiConnections"]["connections"]
    sources = {row["source"] for row in connections}
    assert {"openrouter", "deepseek"} <= sources
    assert all("credentialRef" not in row for row in connections)


def test_connecting_the_default_location_keeps_activity_and_meters() -> None:
    from spend_app.connections import default_location
    from spend_app.providers import REGISTRY

    default = default_location(REGISTRY.get("codex_local"), None)
    assert suppresses_default_profile("codex_local", {"enabled": True, "location": default}) is False
    assert suppresses_default_profile("codex_local", {"enabled": True, "location": "D:/elsewhere/sessions"}) is True
    assert suppresses_default_profile("codex_local", {"enabled": False, "location": default}) is True
    assert suppresses_default_profile("codex_local", None) is False


def test_the_hud_opens_setup_even_without_the_dashboard() -> None:
    html = render_hud(dashboard=False)
    assert 'id="hud-setup-btn"' in html
    assert 'invoke("open_setup_wizard")' in html


def _add_plan(settings: Settings, name: str, tool_key: str, amount: str) -> None:
    from datetime import date

    from spend_app.plan_service import apply_mutation

    with connect(settings.database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        apply_mutation(connection, {"operation": "add", "request_id": f"test-{name.replace(' ', '-')}", "values": {
            "name": name, "tool_key": tool_key, "amount_usd": amount, "cadence": "monthly",
            "start_date": "2026-10-01", "end_date": None}}, date(2026, 10, 26))


def test_plan_cleanup_ends_retired_plans_and_links_custom_ones(client: TestClient, settings: Settings) -> None:
    _add_plan(settings, "Curso Ultra", "custom", "0.01")
    _add_plan(settings, "Antigravity Ultra 200", "custom", "200")
    _add_plan(settings, "Claude Max 20x", "claude-code", "200")
    with connect(settings.database_path) as connection:
        # A plan saved before Cursor was retired still says cursor.
        connection.execute("UPDATE subscriptions SET tool_key='cursor' WHERE name='Curso Ultra'")
    issues = {item["name"]: item for item in client.get("/api/setup/plan-issues").json()["issues"]}
    assert set(issues) == {"Curso Ultra", "Antigravity Ultra 200"}
    assert issues["Curso Ultra"]["fix"] == "end" and issues["Antigravity Ultra 200"]["fixLabel"] == "Link to Antigravity"
    for name in ("Curso Ultra", "Antigravity Ultra 200"):
        item = issues[name]
        response = client.post("/api/setup/plans/fix", headers=SETUP,
                               json={"planId": item["planId"], "version": item["version"], "fix": item["fix"]})
        assert response.status_code == 200, response.text
    assert client.get("/api/setup/plan-issues").json()["issues"] == []
    with connect(settings.database_path) as connection:
        assert connection.execute("SELECT tool_key FROM subscriptions WHERE name='Antigravity Ultra 200'").fetchone()[0] == "antigravity"
        assert connection.execute("SELECT p.end_date FROM subscription_plans p JOIN subscriptions s ON s.plan_id=p.id "
                                  "WHERE s.name='Curso Ultra'").fetchone()[0] is not None


def test_the_hub_applies_a_plan_change_forwarded_by_a_spoke(client: TestClient, settings: Settings) -> None:
    from spend_app.sync import register_machine

    _add_plan(settings, "Muse Code", "custom", "15")
    token = register_machine(settings.database_path, name="Laptop", os_name="win32")["token"]
    plan = client.get("/api/subscriptions").json()["plans"][0]
    response = client.post("/api/account/subscriptions", headers={"Authorization": f"Bearer {token}"}, json={
        "action": "plan", "operation": "end", "request_id": "spoke-end-muse", "plan_id": plan["id"],
        "expected_version": plan["version"], "end_date": "2026-10-26"})
    assert response.status_code == 200, response.text
    assert client.get("/api/subscriptions").json()["plans"][0]["end_date"] == "2026-10-26"


def test_a_hub_with_an_account_accepts_no_new_ones(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("BURNRATE_ALLOW_REGISTRATION", raising=False)
    assert client.post("/api/account/register", json={"username": "owner", "password": "long-enough-1"}).status_code == 200
    stranger = client.post("/api/account/register", json={"username": "stranger", "password": "long-enough-2"})
    assert stranger.status_code == 403 and "pairing code" in stranger.json()["error"]
    # The owner registering again from another computer still signs in.
    assert client.post("/api/account/register", json={"username": "owner", "password": "long-enough-1"}).status_code == 200
    monkeypatch.setenv("BURNRATE_ALLOW_REGISTRATION", "1")
    assert client.post("/api/account/register", json={"username": "second", "password": "long-enough-3"}).status_code == 200


def test_there_is_no_built_in_hub_address(client: TestClient, monkeypatch) -> None:
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER_URL", raising=False)
    assert client.get("/api/setup/state").json()["centralServerUrl"] == ""
    response = client.post("/api/setup/account", headers=SETUP,
                           json={"mode": "login", "username": "alex", "password": "long-enough-1"})
    assert response.status_code == 422 and "hub's address" in response.json()["error"]


def test_hub_update_runs_the_deploy_steps_and_refuses_odd_arguments() -> None:
    from spend_app.hub_update import update_hub

    calls, lines = [], []

    class Done:
        returncode = 0
        stdout = '{"service": "BURNRATE", "version": "0.3.0-beta.18", "build": "0.3.0-beta.18+abc123"}\n'
        stderr = ""

    assert update_hub(ssh="my-hub", path="/opt/burnrate", service="burnrate-hub", branch="main",
                      run=lambda step, **kwargs: calls.append(step) or Done(), echo=lines.append) == 0
    assert calls == [["ssh", "my-hub", "cd /opt/burnrate && git fetch origin && git checkout main && git merge --ff-only "
                      "origin/main && sudo systemctl restart burnrate-hub && sleep 4 && curl -s http://127.0.0.1:17331/api/spend"]]
    assert lines[-1] == "Hub now runs 0.3.0-beta.18+abc123."
    calls.clear()
    assert update_hub(ssh="my-hub; rm -rf /", path="/opt/burnrate", service="burnrate-hub", branch="main",
                      run=lambda step, **kwargs: calls.append(step), echo=lines.append) == 2
    assert calls == []
    assert update_hub(ssh="", path="/opt/burnrate", service="s", branch="main", echo=lines.append) == 2


def test_a_hub_on_the_same_version_but_other_code_is_reported(tmp_path, monkeypatch) -> None:
    from spend_app import freshness
    from spend_app.build import build_id
    from spend_app.setup_wizard import read_setup_state, write_setup_state

    settings = Settings(database_path=tmp_path / "f.db", pricing_path=ROOT / "pricing", anthropic_admin_key=None,
                        openai_admin_key=None, timezone="UTC", cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {**state["spokeConfig"], "enabled": True, "lastSyncAt": iso(datetime.now(UTC))}
        write_setup_state(connection, state)
    freshness.reset_state()
    version = build_id().split("+")[0]
    monkeypatch.setattr("spend_app.sync.fetch_from_configured_hub",
                        lambda *args, **kwargs: {"version": version, "build": version + "+0000000other"})
    report = freshness.report(settings, background=False)
    assert report["hub"]["matches"] is False
    assert "burnrate hub update" in next(item for item in report["issues"] if item["kind"] == "version")["detail"]
    freshness.reset_state()
    monkeypatch.setattr("spend_app.sync.fetch_from_configured_hub", lambda *args, **kwargs: {"version": version, "build": build_id()})
    assert freshness.report(settings, background=False)["hub"]["matches"] is True


def test_copy_diagnostics_removes_keys_addresses_and_the_home_folder(client: TestClient, settings, tmp_path, monkeypatch) -> None:
    from spend_app.support_report import build_report, redact

    home = Path("C:/Users/someone")
    sample = ("Authorization: Bearer abc.def-123 key=sk-or-v1-0123456789abcdef api_key=\"plainsecret\" "
              r"mail me@example.com at C:\Users\someone\.claude and C:/Users/other/x and /home/dev/y")
    clean = redact(sample, home=home)
    for leaked in ("abc.def-123", "sk-or-v1", "plainsecret", "me@example.com", "someone", "other", "dev/"):
        assert leaked not in clean, leaked
    assert "~" in clean and "[user]" in clean

    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "dashboard.log").write_text("".join(f"line {n}\n" for n in range(100)) + "ERROR token=hunter2\n", encoding="utf-8")
    text = build_report(settings, log_dir=logs)
    assert text.startswith("BURNRATE diagnostics") and "## Versions" in text and "## Needs attention" in text
    assert "line 99" in text and "line 10\n" not in text and "hunter2" not in text

    refused = client.post("/api/setup/diagnostics")
    assert refused.status_code == 403
    monkeypatch.setattr("spend_app.support_report.build_report", lambda _settings: "report")
    assert client.post("/api/setup/diagnostics", headers=SETUP).json() == {"text": "report"}


def test_setup_switches_desktop_notifications_per_kind(client: TestClient, monkeypatch) -> None:
    shown = client.get("/api/setup/notifications").json()
    assert shown["available"] is False  # tests are not the desktop service
    assert [kind["key"] for kind in shown["kinds"]] == ["threshold", "dry", "held", "sync"]
    assert client.post("/api/setup/notifications", json={"kinds": {"held": False}}).status_code == 403
    saved = client.post("/api/setup/notifications", json={"kinds": {"held": False}}, headers=SETUP).json()
    assert {kind["key"]: kind["on"] for kind in saved["kinds"]}["held"] is False
    assert client.post("/api/setup/notifications", json={"kinds": {"bogus": True}}, headers=SETUP).status_code == 422
    sent = []
    monkeypatch.setattr("spend_app.notify.show_toast", lambda title, body, tag: sent.append(title))
    assert client.post("/api/setup/notifications/test", headers=SETUP).json() == {"ok": True}
    assert sent == ["BURNRATE notifications are on"]
