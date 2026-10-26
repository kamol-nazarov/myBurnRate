"""This computer's freshness report: stale sources, held meters, sync and the ingest heartbeat."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import freshness
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.db import connect, initialize, upsert_quota

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 19, 12, 0, tzinfo=UTC)


def _iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def _settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


@pytest.fixture(autouse=True)
def _reset():
    freshness.reset_state()
    # A process past its catch-up passes, unless a test says otherwise.
    freshness.pass_finished({}, [], 1.0, NOW)
    freshness.pass_finished({}, [], 1.0, NOW)
    yield
    freshness.reset_state()


def _run(connection, source: str, status: str, finished: datetime, error: str | None = None) -> None:
    connection.execute(
        "INSERT INTO ingest_runs(source, started_at, finished_at, status, error) VALUES(?,?,?,?,?)",
        (source, _iso(finished - timedelta(seconds=2)), _iso(finished), status, error),
    )


def test_quiet_computer_reports_ok(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        _run(connection, "codex_local", "success", NOW - timedelta(seconds=20))
        _run(connection, "openai_admin", "skipped", NOW - timedelta(seconds=20), "OPENAI_ADMIN_KEY is not configured")
    report = freshness.report(settings, NOW)
    assert report["status"] == "ok"
    assert report["issues"] == []
    assert report["sync"] is None and report["hub"] is None


def test_collection_resuming_after_long_idle_gets_watchdog_grace(monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(freshness.time, "monotonic", lambda: clock[0])
    freshness.reset_state()
    clock[0] += 7200
    assert freshness.heartbeat(NOW)["stalled"]
    freshness.collection_resumed()
    freshness.pass_started(NOW)
    assert not freshness.heartbeat(NOW)["stalled"]
    clock[0] += freshness.FIRST_PASS_STALLED_AFTER_SECONDS + 1
    assert freshness.heartbeat(NOW)["stalled"]
    # A new viewer cannot hide a pass that was already stuck.
    freshness.collection_resumed()
    assert freshness.heartbeat(NOW)["stalled"]


def test_collection_resuming_after_completed_pass_gets_watchdog_grace(monkeypatch):
    clock = [10000.0]
    monkeypatch.setattr(freshness.time, "monotonic", lambda: clock[0])
    freshness.reset_state()
    freshness.pass_finished({}, [], 1, NOW - timedelta(hours=2))
    clock[0] += 7200
    assert freshness.heartbeat(NOW)["stalled"]
    freshness.collection_resumed()
    assert not freshness.heartbeat(NOW)["stalled"]
    clock[0] += freshness.INGEST_STALLED_AFTER_SECONDS + 1
    assert freshness.heartbeat(NOW)["stalled"]


def test_failed_and_stale_sources_are_named_and_retired_ones_ignored(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        _run(connection, "traycer_local", "failed", NOW - timedelta(seconds=10), "A source link points outside the approved location.")
        _run(connection, "claude_local", "success", NOW - timedelta(minutes=9))
        _run(connection, "muse_local", "success", NOW - timedelta(days=3))
        _run(connection, "sync:work-computer", "success", NOW - timedelta(days=2))
    report = freshness.report(settings, NOW)
    by_key = {item["key"]: item for item in report["issues"]}
    assert by_key["traycer_local"]["state"] == "failed" and by_key["traycer_local"]["short"] == "Traycer"
    assert by_key["claude_local"]["state"] == "stale" and by_key["claude_local"]["ageSeconds"] == 540
    assert "muse_local" not in by_key  # not attempted for a day: removed, not stale
    assert report["status"] == "warn"
    assert report["machines"] == [
        {"key": "work-computer", "lastSeenAt": _iso(NOW - timedelta(days=2)), "ageSeconds": 172800, "state": "quiet"}
    ]


def test_meter_holding_an_old_reading_is_reported_with_the_reason(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        common = {"used": None, "allowance": None, "is_payg": False}
        upsert_quota(connection, provider_key="muse", limit_key="weekly", label="Muse Code weekly window", unit="pct",
                     pct=25.0, resets_at=_iso(NOW + timedelta(days=3)), source="muse_quota_endpoint",
                     polled_at=_iso(NOW - timedelta(hours=14)), **common)
        upsert_quota(connection, provider_key="muse", limit_key="5h", label="Muse Code 5-hour window — Endpoint answered without usage.",
                     unit="unavailable", pct=None, resets_at=None, source="muse_quota_endpoint",
                     polled_at=_iso(NOW - timedelta(minutes=1)), **common)
        # Fresh and window-reset readings are not held.
        upsert_quota(connection, provider_key="codex", limit_key="weekly", label="Codex weekly", unit="pct", pct=40.0,
                     resets_at=_iso(NOW + timedelta(days=2)), source="codex_local_telemetry",
                     polled_at=_iso(NOW - timedelta(minutes=2)), **common)
        upsert_quota(connection, provider_key="grok", limit_key="weekly", label="Grok weekly", unit="pct", pct=10.0,
                     resets_at=_iso(NOW - timedelta(hours=1)), source="grok_local_billing",
                     polled_at=_iso(NOW - timedelta(hours=3)), **common)
    issues = freshness.report(settings, NOW)["issues"]
    assert [item["key"] for item in issues] == ["muse:weekly"]
    held = issues[0]
    assert held["state"] == "held" and held["short"] == "Muse weekly" and held["ageSeconds"] == 14 * 3600
    assert held["detail"] == "Endpoint answered without usage."


def test_heartbeat_marks_a_stalled_collection_loop(monkeypatch) -> None:
    freshness.pass_started(NOW - timedelta(minutes=30))
    freshness.pass_finished({"claude_local": 18.24, "codex_local": 0.1}, ["traycer_local"], 21.3, NOW - timedelta(minutes=29))
    beat = freshness.heartbeat(NOW)
    assert beat["lastPassSeconds"] == 21.3 and beat["slowest"] == [["claude_local", 18.2]]
    assert beat["failed"] == ["traycer_local"] and beat["running"] is False
    assert beat["stalled"] is True
    assert freshness.heartbeat(NOW - timedelta(minutes=25))["stalled"] is False
    # A fresh process that has not finished its first pass is judged by uptime.
    freshness.reset_state()
    assert freshness.heartbeat(NOW)["stalled"] is False
    # A slow cold start (first pass) is allowed far longer than a steady pass.
    monkeypatch.setattr(freshness, "_STARTED_MONO", freshness.time.monotonic() - 900)
    assert freshness.heartbeat(NOW)["stalled"] is False
    monkeypatch.setattr(freshness, "_STARTED_MONO", freshness.time.monotonic() - 3600)
    assert freshness.heartbeat(NOW)["stalled"] is True


def test_spoke_reports_sync_and_hub_version(tmp_path: Path, monkeypatch) -> None:
    from spend_app.setup_wizard import read_setup_state, write_setup_state

    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {**state.get("spokeConfig", {}), "enabled": True, "lastSyncAt": _iso(NOW - timedelta(seconds=40)),
                                "lastSyncError": None}
        write_setup_state(connection, state)
    monkeypatch.setattr("spend_app.sync.fetch_from_configured_hub", lambda *args, **kwargs: {"version": "0.3.0-beta.9"})
    report = freshness.report(settings, NOW, background=False)
    assert report["sync"] == {"role": "spoke", "lastSyncAt": _iso(NOW - timedelta(seconds=40)), "ageSeconds": 40, "failing": False}
    assert report["hub"] == {"version": "0.3.0-beta.9", "build": None, "matches": False}
    mismatch = [item for item in report["issues"] if item["kind"] == "version"]
    assert mismatch and mismatch[0]["severity"] == "info"
    assert report["status"] == "ok"  # a version note alone does not raise the alarm

    with connect(settings.database_path) as connection:
        state = read_setup_state(connection)
        state["spokeConfig"]["lastSyncError"] = "hub unreachable"
        write_setup_state(connection, state)
    report = freshness.report(settings, NOW, background=False)
    sync = [item for item in report["issues"] if item["kind"] == "sync"]
    assert sync and sync[0]["state"] == "failed"


def test_freshness_route_answers_locally(tmp_path: Path, monkeypatch) -> None:
    def no_hub(*args, **kwargs):
        raise AssertionError("freshness must never be proxied to the hub")

    monkeypatch.setattr("spend_app.sync.fetch_from_configured_hub", no_hub)
    freshness.pass_finished({}, [], 1.0)
    freshness.pass_finished({}, [], 1.0)
    client = TestClient(create_app(_settings(tmp_path), enable_scheduler=False))
    payload = client.get("/api/spend/freshness").json()
    assert payload["status"] == "ok" and payload["issues"] == []
    assert set(payload["ingest"]) >= {"lastPassAt", "stalled", "uptimeSeconds"}


def test_first_pass_after_start_is_warming_not_stale(tmp_path: Path) -> None:
    settings = _settings(tmp_path)
    initialize(settings.database_path)
    with connect(settings.database_path) as connection:
        _run(connection, "claude_local", "success", NOW - timedelta(minutes=6))
        _run(connection, "traycer_local", "failed", NOW - timedelta(minutes=6), "boom")
    freshness.reset_state()
    report = freshness.report(settings, NOW)  # no pass has finished in this process yet
    states = {item["key"]: (item["state"], item["severity"]) for item in report["issues"]}
    assert states["local-ingest"] == ("warming", "info")
    assert "claude_local" not in states  # not reached by the first pass yet
    assert states["traycer_local"] == ("failed", "warn")  # a failure is still a failure
    freshness.pass_finished({}, [], 280.0, NOW - timedelta(seconds=60))
    states = {item["key"]: item["state"] for item in freshness.report(settings, NOW)["issues"]}
    assert states.get("local-ingest") == "warming"  # sources the cold pass reached early still look old
    freshness.pass_finished({}, [], 40.0, NOW - timedelta(seconds=1))
    states = {item["key"]: item["state"] for item in freshness.report(settings, NOW)["issues"]}
    assert states.get("claude_local") == "stale" and "local-ingest" not in states
