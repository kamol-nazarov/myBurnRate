"""Phase 3 of docs/PERFORMANCE_PLAN.md: one summary computation per ingest cycle.

Live requests describe the data as of the latest completed ingest cycle (the
data clock); the full summary is memoised per (clock, data fingerprint) and
pre-warmed for the windows viewers actually asked for.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

from tests_spend.local_client_fixture import local_client

from spend_app import aggregate
from spend_app.aggregate import (
    aggregate_health,
    aggregate_health_cached,
    aggregate_summary,
    aggregate_summary_cached,
    data_clock,
    recently_requested_summaries,
    record_summary_request,
)
from spend_app.api import GZIP_MINIMUM_SIZE, create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize, upsert_quota
from spend_app.scheduler import create_scheduler
from tests_spend.test_aggregation import NOW, TZ, add_event, fixture_database
from tests_spend.test_api import add_fixture_event


ROOT = Path(__file__).resolve().parents[1]


def _settings(tmp_path: Path, database: Path | None = None) -> Settings:
    return Settings(
        database_path=database or tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone=TZ,
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _record_run(database: Path, finished_at: str, status: str = "success") -> None:
    with connect(database) as connection:
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written) VALUES(?,?,?,?,0)",
            ("codex_local", finished_at, finished_at, status),
        )


def test_data_clock_is_the_latest_completed_cycle_or_the_real_clock(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    moment = datetime(2026, 9, 1, 20, 31, 44, tzinfo=UTC)
    assert data_clock(database, 15, moment) == moment, "no cycle yet: real clock"
    _record_run(database, "2026-09-01T20:31:31.250000Z")
    assert data_clock(database, 15, moment) == datetime(2026, 9, 1, 20, 31, 31, 250_000, tzinfo=UTC)
    _record_run(database, "2026-09-01T20:31:40Z", status="failed")
    assert data_clock(database, 15, moment) == datetime(2026, 9, 1, 20, 31, 31, 250_000, tzinfo=UTC), "failed runs do not advance the clock"
    stalled = moment + timedelta(minutes=5)
    assert data_clock(database, 15, stalled) == stalled, "a stalled ingest falls back to the real clock"
    assert data_clock(tmp_path / "missing.db", 15, moment) == moment


def test_cached_summary_is_reused_per_clock_and_invalidated_by_data(tmp_path: Path) -> None:
    aggregate.reset_memo()
    database, pricing = fixture_database(tmp_path)
    kwargs = dict(
        database_path=database,
        pricing=pricing,
        window_key="1d",
        tool="all",
        timezone=TZ,
        cache_threshold=0.75,
        cadence_seconds=15,
    )
    first = aggregate_summary_cached(**kwargs, now=NOW)
    again = aggregate_summary_cached(**kwargs, now=NOW)
    # The returned payload is a shallow copy (it carries the machine list), so
    # reuse is asserted on the memoised computation, not object identity.
    assert again == first, "same clock and same data must reuse the computed payload"
    assert first == aggregate_summary(**kwargs, now=NOW)

    next_cycle = aggregate_summary_cached(**kwargs, now=NOW + timedelta(seconds=15))
    assert next_cycle != first
    assert next_cycle["generatedAt"] != first["generatedAt"]

    with connect(database) as connection:
        upsert_quota(
            connection,
            provider_key="codex",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="codex_local",
            polled_at="2026-08-30T22:30:00Z",
            pct=31.0,
            resets_at=(NOW + timedelta(days=5)).isoformat(),
        )
        from spend_app.codex_quota import current_scope, save_metadata
        from spend_app.quotas import QuotaSample
        save_metadata(connection, QuotaSample(
            "codex", "weekly", "Weekly", "percent", "codex_local", pct=31.0,
            resets_at=(NOW + timedelta(days=5)).isoformat(), observed_at=NOW.isoformat(),
            pool_id="codex", scope=current_scope()), "2026-08-30T22:30:00Z")
    after_quota = aggregate_summary_cached(**kwargs, now=NOW)
    assert after_quota["capacity"] != first["capacity"]
    codex = next(card for card in after_quota["capacity"] if card["providerKey"] == "codex")
    assert codex["peakPct"] == 31.0

    add_event(
        database,
        pricing,
        raw_id="codex-fresh",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-fresh",
        occurred=NOW - timedelta(minutes=5),
        input_tokens=10_000,
        cached=5_000,
        writes=0,
        output=1_000,
    )
    after_event = aggregate_summary_cached(**kwargs, now=NOW)
    assert after_event is not after_quota
    assert after_event["totals"]["records"] == after_quota["totals"]["records"] + 1
    aggregate.reset_memo()


def test_live_requests_use_the_data_clock_and_injected_clock_is_untouched(tmp_path: Path) -> None:
    aggregate.reset_memo()
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    settings = _settings(tmp_path, database)
    live = local_client(create_app(settings, enable_scheduler=False), settings.database_path)
    # No completed cycle yet: the real clock is used and the just-written row is visible.
    body = live.get("/api/spend/summary", params={"window": "1d"}).json()
    assert body["totals"]["records"] == 1
    cycle_end = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    _record_run(database, cycle_end)
    body = live.get("/api/spend/summary", params={"window": "1d"}).json()
    assert body["generatedAt"] == cycle_end
    assert body["window"]["to"] == cycle_end
    assert body["totals"]["records"] == 1
    entity = live.get("/api/spend/entity", params={"kind": "model", "key": "gpt-5.6-sol", "window": "1d"}).json()
    assert entity["window"]["to"] == cycle_end
    assert entity["sessions"]["rows"][0]["id"] == "api-session"
    assert ("1d", "all") in recently_requested_summaries(), "two polls mark the window as watched"

    pinned = local_client(create_app(settings, enable_scheduler=False, now=NOW), settings.database_path)
    assert pinned.get("/api/spend/summary", params={"window": "1d"}).json()["generatedAt"] == "2026-08-30T23:00:00Z"
    aggregate.reset_memo()


def test_ingest_cycle_prewarms_only_recently_requested_windows(tmp_path: Path, monkeypatch) -> None:
    aggregate.reset_memo()
    database, _pricing = fixture_database(tmp_path)
    settings = _settings(tmp_path, database)
    monkeypatch.setattr("spend_app.connections.execute", lambda *args, **kwargs: {"status":"skipped", "eventsSeen":0})
    for name in ("codex", "claude", "traycer", "opencode"):
        monkeypatch.setattr(f"spend_app.scheduler.ingest_{name}_local", lambda **_kwargs: {"status": "success"})
    warmed: list[tuple[str, str]] = []
    real = aggregate.aggregate_summary_cached

    def spy(**kwargs):
        warmed.append((kwargs["window_key"], kwargs["tool"]))
        return real(**kwargs)

    monkeypatch.setattr("spend_app.scheduler.aggregate_summary_cached", spy)
    scheduler = create_scheduler(settings, aggregate.PricingEngine.load(ROOT / "pricing"))
    job = {job.id: job for job in scheduler.get_jobs()}["local-ingest"]
    job.func()
    assert warmed == [], "nothing is warmed while nobody is watching"
    record_summary_request("15m", "all")
    record_summary_request("1w", "codex")
    job.func()
    assert warmed == [], "a single one-off request (a probe) does not drive pre-warming"
    record_summary_request("15m", "all")
    record_summary_request("1w", "codex")
    job.func()
    assert warmed == [("15m", "all"), ("1w", "codex")]
    aggregate.reset_memo()


def test_health_is_reused_per_clock_and_invalidated_by_its_inputs(tmp_path: Path) -> None:
    aggregate.reset_memo()
    database, pricing = fixture_database(tmp_path)
    first = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert aggregate_health_cached(database_path=database, now=NOW, timezone=TZ) is first
    assert first == aggregate_health(database_path=database, now=NOW, timezone=TZ)
    assert aggregate_health_cached(database_path=database, now=NOW + timedelta(seconds=15), timezone=TZ) is not first

    with connect(database) as connection:
        connection.execute(
            "INSERT INTO ingest_runs(source,started_at,finished_at,status,events_written,error) VALUES(?,?,?,?,?,?)",
            ("codex_local", "2026-08-30T22:59:00Z", "2026-08-30T22:59:01Z", "failed", 0, "boom"),
        )
    after_run = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_run is not first
    assert {row["source"]: row["status"] for row in after_run["ingest"]}["codex_local"] == "failed"

    with connect(database) as connection:
        upsert_quota(
            connection,
            provider_key="grok",
            limit_key="weekly",
            label="Weekly",
            unit="percent",
            source="traycer_local",
            polled_at="2026-08-30T22:45:00Z",
            pct=40.0,
        )
    after_quota = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_quota is not after_run
    assert next(q for q in after_quota["quotas"] if q["providerKey"] == "grok")["lastPoll"] == "2026-08-30T22:45:00Z"

    add_event(
        database,
        pricing,
        raw_id="codex-health",
        tool="codex",
        model="gpt-5.6-sol",
        session="session-h",
        occurred=NOW - timedelta(hours=1),
        input_tokens=1_000,
        cached=100,
        writes=0,
        output=10,
    )
    after_event = aggregate_health_cached(database_path=database, now=NOW, timezone=TZ)
    assert after_event is not after_quota
    aggregate.reset_memo()


def test_json_endpoints_are_gzip_compressed_and_no_store(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    add_fixture_event(database)
    client = local_client(create_app(_settings(tmp_path, database), enable_scheduler=False, now=datetime.now(UTC)), database)

    summary_gz = client.get("/api/spend/summary?window=1d&tool=all", headers={"Accept-Encoding": "gzip"})
    assert summary_gz.status_code == 200
    expected = "gzip" if len(summary_gz.content) >= GZIP_MINIMUM_SIZE else None
    assert summary_gz.headers.get("content-encoding") == expected

    summary_raw = client.get("/api/spend/summary?window=1d&tool=all", headers={"Accept-Encoding": "identity"})
    assert summary_raw.status_code == 200
    assert summary_raw.headers.get("content-encoding") is None
    assert summary_gz.content == summary_raw.content

    health = client.get("/api/spend/health")
    assert health.status_code == 200
    assert health.headers["cache-control"] == "no-store"


def test_bench_script_and_baseline_agree_and_stay_read_only() -> None:
    script = (ROOT / "scripts" / "Bench-Burnrate.ps1").read_text(encoding="utf-8")
    assert "/api/spend/limits" not in script
    assert "/api/spend/summary" in script and "/api/spend/health" in script
    assert "127.0.0.1:17331" in script
    assert "Select-Object -Skip 1" in script, "the cold first request is excluded from p50/p95"
    assert "PERF REGRESSION" in script and "exit 1" in script
