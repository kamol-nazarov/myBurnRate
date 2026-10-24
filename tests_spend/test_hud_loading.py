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
                        anthropic_admin_key=None, openai_admin_key=None, timezone="America/New_York",
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

    def no_dashboard_history(*args, **kwargs):
        raise AssertionError("Live HUD meters must not build dashboard-scale history")

    monkeypatch.setattr("spend_app.api.aggregate_summary_cached", no_dashboard_history)
    monkeypatch.setattr("spend_app.hud_data.recorded_usage", no_dashboard_history)
    # Window-burn pace reads a bounded slice of usage rows by design (the
    # forecast derives from measured usage); only dashboard-scale history
    # aggregation is forbidden on the live endpoint.
    app = create_app(settings, enable_scheduler=False, now=NOW)
    client = TestClient(app)
    for route in ("/hud", "/?hud=1"):
        html = client.get(route).text
        assert 'hudFetch("/api/spend/hud?machine=' in html
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


def test_hud_only_scheduler_collects_history_even_without_viewers(tmp_path, monkeypatch):
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
    try:
        for key in ("local-ingest", "provider-admin", "quota-poll", "activity"):
            jobs[key].func(**jobs[key].kwargs)
        # With no viewer anywhere, the local pass and the admin pass still
        # collect, and the quota/activity pollers still run.
        assert len(reads) == 2 and "codex_local" in reads[0]
        assert polls == ["quota", "activity"] and syncs == [1]
        jobs["local-ingest"].func()
        assert len(reads) == 3
    finally:
        freshness.reset_state()


def test_history_scan_finishes_every_source_when_dashboard_demand_expires(monkeypatch):
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
    _run_ingest_specs([(spec, {}) for spec in specs])
    assert calls == ["first", "second"], "a pass must not stop because the viewer went away"


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
        assert calls == ["start", "summary", "local-ingest", "provider-admin", "summary"]
        assert app.state.history_demand.active()
    assert calls[-1] == "stop"


def test_a_dead_collection_loop_is_a_watchdog_stall_even_without_viewers(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    freshness.reset_state()
    monkeypatch.setattr(freshness, "_STARTED_MONO", freshness.time.monotonic() - 7200)
    try:
        report = freshness.report(settings, NOW, history_active=False)
        assert report["ingest"]["stalled"] is True
        assert report["status"] == "alarm"
        assert report["issues"][0]["state"] == "stalled"
        hud = freshness.report(settings, NOW, include_history=False)
        assert hud["issues"] == []
    finally:
        freshness.reset_state()


def test_hud_recorded_usage_keeps_collection_active_without_building_dashboard(tmp_path, monkeypatch):
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
    assert app.state.history_demand.active()


def test_hud_only_collects_new_usage_across_midnight_and_renews_demand(tmp_path, monkeypatch):
    from spend_app.adapters.common import UsageRow, persist_rows
    import spend_app.api as api

    settings = settings_for(tmp_path)
    current = [datetime(2026, 10, 21, 3, 59, 50, tzinfo=UTC)]
    clock = [0.0]
    demand = HistoryDemand(clock=lambda: clock[0])
    schedulers = []

    class Clock(datetime):
        @classmethod
        def now(cls, tz=None):
            return current[0].astimezone(tz)

    def scheduler(*args, **kwargs):
        result = create_scheduler(*args, **kwargs)
        schedulers.append(result)
        return result

    def collect(jobs, settings, pricing, **kwargs):
        persist_rows(database_path=settings.database_path, pricing=pricing, source="codex_local",
                     usage_rows=[UsageRow(source="codex_local", tool_key="codex", model_key="gpt-5.6-sol",
                                          occurred_at=current[0] - timedelta(seconds=1), session_id="hud-session",
                                          project=None, input_tokens=1000, cached_input_tokens=800,
                                          cache_write_tokens=0, cache_write_1h_tokens=0, cost_usd=None,
                                          output_tokens=100, reasoning_tokens=None,
                                          raw_id=current[0].isoformat())])

    def no_dashboard(*args, **kwargs):
        raise AssertionError("HUD polling must not build dashboard summaries")

    monkeypatch.setattr(api, "datetime", Clock)
    monkeypatch.setattr(api, "HistoryDemand", lambda: demand)
    monkeypatch.setattr(api, "create_scheduler", scheduler)
    monkeypatch.setattr(api, "aggregate_summary_cached", no_dashboard)
    monkeypatch.setattr("spend_app.scheduler._run_ingest_specs", collect)
    monkeypatch.setattr("spend_app.scheduler.recently_requested_summaries", list)
    monkeypatch.setattr("spend_app.setup_wizard.maybe_run_configured_spoke_sync", lambda *args: None)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    freshness.reset_state()
    try:
        app = create_app(settings)
        client = TestClient(app)
        job = schedulers[0].get_job("local-ingest")
        assert client.get("/api/spend/hud").status_code == 200
        assert not demand.active()
        # Collection no longer waits for a viewer: the first pass already
        # brings the event in.
        job.func()
        first = client.get("/api/spend/hud/usage").json()["navigation"]
        assert first["todayTokens"] == 1100 and first["todayUsd"] > 0
        job.func()
        before = client.get("/api/spend/hud/usage").json()["navigation"]
        assert before["todayTokens"] == 1100 and before["todayUsd"] == first["todayUsd"]

        # HUD polls keep renewing the demand lease, including past the
        # original two-minute lease and a local calendar-day rollover.
        clock[0] = 100
        current[0] = datetime(2026, 10, 21, 4, 1, 30, tzinfo=UTC)
        assert client.get("/api/spend/hud/usage").json()["navigation"]["todayTokens"] == 0
        clock[0] = 150
        assert demand.active()
        job.func()
        after = client.get("/api/spend/hud/usage").json()["navigation"]
        assert after["todayTokens"] == 1100 and after["todayUsd"] == before["todayUsd"]

        clock[0] = 271
        assert not demand.active()
        assert client.get("/api/spend/summary", headers={"Referer": "http://localhost/hud"}).status_code == 200
        assert demand.active()
    finally:
        freshness.reset_state()


def test_hud_usage_activates_collection_on_both_spoke_and_hub(tmp_path, monkeypatch):
    hub_settings = settings_for(tmp_path / "hub")
    spoke_settings = settings_for(tmp_path / "spoke")
    hub = create_app(hub_settings, enable_scheduler=False, now=NOW)
    hub_client = TestClient(hub)
    spoke = create_app(spoke_settings, enable_scheduler=False, now=NOW)

    def fetch(settings, path, params, **_):
        if settings is hub_settings:
            return None
        # The spoke must request its own usage before reading unified totals.
        if path.endswith("/usage"):
            assert spoke.state.history_demand.active()
        return hub_client.get(path, params=params).json()

    monkeypatch.setattr("spend_app.sync.fetch_from_configured_hub", fetch)
    client = TestClient(spoke)
    assert client.get("/api/spend/hud").status_code == 200
    assert not hub.state.history_demand.active() and not spoke.state.history_demand.active()
    assert client.get("/api/spend/hud/usage").status_code == 200
    assert hub.state.history_demand.active() and spoke.state.history_demand.active()


def test_hud_usage_schedules_collection_once_and_renews_on_each_poll(tmp_path, monkeypatch):
    calls = []
    clock = [0.0]
    demand = HistoryDemand(clock=lambda: clock[0])

    class Scheduler:
        def modify_job(self, key, **kwargs):
            calls.append(key)

    monkeypatch.setattr("spend_app.api.HistoryDemand", lambda: demand)
    monkeypatch.setattr("spend_app.api.create_scheduler", lambda *args, **kwargs: Scheduler())
    monkeypatch.setattr("spend_app.freshness.collection_resumed", lambda: calls.append("resumed"))
    client = TestClient(create_app(settings_for(tmp_path), now=NOW))
    expected = ["resumed", "local-ingest", "provider-admin"]
    assert client.get("/api/spend/hud/usage").status_code == 200
    assert calls == expected
    clock[0] = 100
    assert client.get("/api/spend/hud/usage").status_code == 200
    clock[0] = 150
    assert demand.active() and calls == expected
    clock[0] = 221
    assert not demand.active()
    assert client.get("/api/spend/hud/usage").status_code == 200
    assert calls == expected * 2


def test_hud_freshness_reports_a_stalled_loop_without_any_polling(tmp_path, monkeypatch):
    settings = settings_for(tmp_path)
    app = create_app(settings, enable_scheduler=False, now=NOW)
    client = TestClient(app)
    freshness.reset_state()
    monkeypatch.setattr(freshness, "_STARTED_MONO", freshness.time.monotonic() - 7200)
    try:
        # The HUD view no longer hides collection issues behind viewer
        # demand: a stalled loop is visible before any poll happens.
        before = client.get("/api/spend/freshness?view=hud").json()
        assert any(issue["kind"] == "ingest" and issue["state"] == "stalled" for issue in before["issues"])
        assert client.get("/api/spend/hud/usage").status_code == 200
        after = client.get("/api/spend/freshness?view=hud").json()
        assert any(issue["kind"] == "ingest" and issue["state"] == "stalled" for issue in after["issues"])
    finally:
        freshness.reset_state()
