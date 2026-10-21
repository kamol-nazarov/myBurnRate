from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from spend_app import aggregate, freshness
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize, upsert_agent_run, upsert_quota
from spend_app.pricing import PricingEngine
from spend_app.scheduler import HistoryDemand, create_scheduler

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def settings_for(tmp_path):
    settings = Settings(database_path=tmp_path / "hud.db", pricing_path=ROOT / "pricing",
                        cursor_import_path=tmp_path / "imports", anthropic_admin_key=None,
                        openai_admin_key=None, cursor_api_key=None, timezone="America/New_York",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    return settings


def test_hud_reads_live_meters_without_loading_history(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    with connect(settings.database_path) as connection:
        upsert_quota(connection, provider_key="grok", limit_key="weekly", label="Grok weekly",
                     unit="pct", pct=42, used=None, allowance=None, is_payg=False,
                     resets_at=(NOW + timedelta(days=2)).isoformat(), source="grok_local",
                     polled_at=NOW.isoformat())
        upsert_agent_run(connection, id="grok:1", name="Live agent", model_key="grok",
                         state="live", started_at=NOW.isoformat(), last_seen_at=NOW.isoformat())

    def no_history(*args, **kwargs):
        raise AssertionError("Live HUD meters must not load historical usage")

    monkeypatch.setattr("spend_app.api.aggregate_summary_cached", no_history)
    monkeypatch.setattr("spend_app.hud_data.recorded_usage", no_history)
    monkeypatch.setattr(aggregate, "_load_events", no_history)
    monkeypatch.setattr(aggregate, "_load_unpriced_events", no_history)
    app = create_app(settings, enable_scheduler=False, now=NOW)
    client = TestClient(app)
    for route in ("/hud", "/?hud=1"):
        html = client.get(route).text
        assert 'fetch("/api/spend/hud?machine=' in html
        assert "/api/spend/summary" not in html
    payload = client.get("/api/spend/hud").json()
    grok = next(card for card in payload["capacity"] if card["providerKey"] == "grok")
    assert grok["primaryPct"] == 42
    assert payload["activity"][0]["name"] == "Live agent"
    assert payload["status"] == "live"
    assert not app.state.history_demand.active()


def test_dashboard_demand_expires_without_further_dashboard_use():
    clock = [0.0]
    demand = HistoryDemand(clock=lambda: clock[0], ttl_seconds=120)
    assert not demand.active()
    assert demand.touch() and demand.active()
    clock[0] = 100
    assert not demand.touch()
    clock[0] = 219
    assert demand.active()
    clock[0] = 220
    assert not demand.active()


def test_hud_only_scheduler_skips_history_but_keeps_live_polling(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    active = [False]
    reads, polls, syncs = [], [], []
    monkeypatch.setattr("spend_app.scheduler._run_ingest_specs", lambda jobs, *args, **kwargs: reads.append([spec.key for spec, _ in jobs]))
    monkeypatch.setattr("spend_app.scheduler.poll_quotas", lambda **kwargs: polls.append("quota"))
    monkeypatch.setattr("spend_app.scheduler.poll_activity", lambda **kwargs: polls.append("activity"))
    monkeypatch.setattr("spend_app.setup_wizard.maybe_run_configured_spoke_sync", lambda *args: syncs.append(1))
    monkeypatch.setattr("spend_app.scheduler.recently_requested_summaries", list)
    scheduler = create_scheduler(settings, PricingEngine.load(settings.pricing_path), history_active=lambda: active[0])
    jobs = {job.id: job for job in scheduler.get_jobs()}
    for key in ("local-ingest", "network-ingest", "provider-admin", "quota-poll", "activity"):
        jobs[key].func(**jobs[key].kwargs)
    assert reads == [] and polls == ["quota", "activity"] and syncs == [1]
    active[0] = True
    try:
        jobs["local-ingest"].func()
        jobs["network-ingest"].func()
        assert len(reads) == 2 and "codex_local" in reads[0] and reads[1] == ["cursor_usage_service"]
        active[0] = False
        jobs["local-ingest"].func()
        assert len(reads) == 2
    finally:
        freshness.reset_state()


def test_history_scan_stops_between_sources_when_dashboard_demand_expires(monkeypatch):
    from spend_app.scheduler import _run_ingest_specs

    active, calls = [True], []

    def reader(spec):
        def run():
            calls.append(spec.key)
            active[0] = False
            return {"status": "success"}

        return run

    monkeypatch.setattr("spend_app.scheduler._ingest_callable", reader)
    specs = [SimpleNamespace(key=key, stability="official") for key in ("first", "second")]
    _run_ingest_specs([(spec, {}) for spec in specs], should_continue=lambda: active[0])
    assert calls == ["first"]


def test_startup_does_not_warm_history_and_dashboard_requests_activate_it(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    calls = []

    class Scheduler:
        def start(self):
            calls.append("start")

        def shutdown(self, *, wait):
            calls.append("stop")

        def modify_job(self, key, **kwargs):
            calls.append(key)

    monkeypatch.setattr("spend_app.api.create_scheduler", lambda *args, **kwargs: Scheduler())

    def summary(**kwargs):
        calls.append("summary")
        return {"status": "live"}

    monkeypatch.setattr("spend_app.api.aggregate_summary_cached", summary)
    app = create_app(settings)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert client.get("/hud").status_code == 200
        assert client.get("/api/spend/hud").status_code == 200
        assert calls == ["start"] and not app.state.history_demand.active()
        assert client.get("/api/spend/summary").status_code == 200
        assert not app.state.history_demand.active()
        assert client.get("/api/spend/summary", headers={"X-Burnrate-View": "dashboard"}).status_code == 200
        assert calls == ["start", "summary", "local-ingest", "network-ingest", "provider-admin", "summary"]
        assert app.state.history_demand.active()
    assert calls[-1] == "stop"


def test_deferred_history_is_not_a_watchdog_stall(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    freshness.reset_state()
    monkeypatch.setattr(freshness, "_STARTED_MONO", freshness.time.monotonic() - 7200)
    try:
        report = freshness.report(settings, NOW, history_active=False)
        assert report["ingest"]["deferred"] and not report["ingest"]["stalled"]
        assert report["status"] == "ok"
        assert report["issues"][0]["state"] == "deferred"
        hud = freshness.report(settings, NOW, include_history=False)
        assert hud["issues"] == []
    finally:
        freshness.reset_state()


def test_hud_recorded_usage_does_not_activate_history_or_build_dashboard(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    from tests_spend.test_api import add_fixture_event

    add_fixture_event(settings.database_path)
    now = datetime.now(UTC)
    pricing = PricingEngine.load(settings.pricing_path)
    expected = aggregate.aggregate_summary(database_path=settings.database_path, pricing=pricing,
                                           window_key="1d", tool="all", timezone=settings.timezone,
                                           cache_threshold=0.75, now=now)

    def no_dashboard(*args, **kwargs):
        raise AssertionError("HUD usage must not build dashboard history")

    for name in ("aggregate_summary", "_month_usage", "_heatmap_cells", "_day_coverage"):
        monkeypatch.setattr(aggregate, name, no_dashboard)
    app = create_app(settings, enable_scheduler=False, now=now)
    payload = TestClient(app).get("/api/spend/hud/usage").json()
    for key in ("todayUsd", "todayTokens", "burnRatePerDay", "tokensAvgPerDay", "todayComplete"):
        assert payload["navigation"][key] == expected["navigation"][key]
    assert payload["totals"]["cacheReusePct"] == expected["totals"]["cacheReusePct"]
    # An existing native HUD keeps its old script across a backend restart.
    legacy = TestClient(app).get("/api/spend/summary", headers={"Referer": "http://127.0.0.1:17331/hud"})
    assert legacy.status_code == 200
    assert legacy.json()["navigation"]["todayTokens"] == payload["navigation"]["todayTokens"]
    assert not app.state.history_demand.active()
