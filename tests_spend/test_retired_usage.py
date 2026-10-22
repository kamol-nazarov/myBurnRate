"""Cursor usage duplicates SuperGrok usage: it is removed once and never counted."""

from __future__ import annotations

import json
from pathlib import Path

from spend_app import db as db_module
from spend_app.db import (
    RETIRED_USAGE_KEY,
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    retire_cursor_usage,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push

ROOT = Path(__file__).resolve().parents[1]
WHEN = "2026-10-01T12:00:00Z"


def usage(raw_id: str, tool: str, source: str) -> UsageEvent:
    return UsageEvent(source=source, tool_key=tool, model_key=f"{tool}:m", occurred_at=WHEN, session_id=None,
                      project=None, input_tokens=10, cached_input_tokens=0, cache_write_tokens=0,
                      cache_write_1h_tokens=0, output_tokens=5, reasoning_tokens=None, cost_usd=None,
                      computed_cost_usd=0.01, raw_id=raw_id, ingested_at=WHEN)


def unpriced(raw_id: str, tool: str, source: str) -> UnpricedUsageEvent:
    return UnpricedUsageEvent(source=source, tool_key=tool, model_key=f"{tool}:m", occurred_at=WHEN,
                              session_id=None, project=None, input_tokens=10, cached_input_tokens=0,
                              cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=5, reasoning_tokens=None,
                              unclassified_tokens=0, telemetry_complete=True, cost_usd=None, raw_id=raw_id,
                              ingested_at=WHEN)


def counts(database: Path) -> dict:
    with connect(database) as connection:
        return {
            table: dict(connection.execute(f"SELECT tool_key, COUNT(*) FROM {table} GROUP BY tool_key").fetchall())
            for table in ("usage_events", "unpriced_usage_events")
        }


def test_recorded_cursor_usage_is_removed_once_after_a_backup(tmp_path, monkeypatch):
    database = tmp_path / "spend.db"
    initialize(database)
    with connect(database) as connection:
        connection.execute("DELETE FROM app_meta WHERE key=?", (RETIRED_USAGE_KEY,))
        upsert_usage_event(connection, usage("c1", "cursor", "cursor_usage_service"))
        upsert_usage_event(connection, usage("g1", "grok", "grok_local"))
        upsert_unpriced_event(connection, unpriced("c2", "cursor", "cursor_local"))
        connection.execute("INSERT INTO pricing_gaps(model_key, source, first_seen_at, last_seen_at, occurrences, sample_raw_id) "
                           "VALUES('cursor:grok-bot', 'cursor_usage_service', ?, ?, 1, 'c3')", (WHEN, WHEN))
        connection.execute("INSERT INTO ingest_runs(source, started_at, finished_at, status) VALUES('cursor_local', ?, ?, 'partial')",
                           (WHEN, WHEN))
        connection.execute("INSERT INTO app_meta(key, value) VALUES('usage.alias.v1:cursor_local:abc', '\"x\"')")
        connection.execute("INSERT INTO app_meta(key, value) VALUES('sync.cursor.v1:keep', '{}')")
    monkeypatch.setattr(db_module, "_RETIRED_USAGE_CHECKED", set())

    assert retire_cursor_usage(database) == 2
    assert counts(database) == {"usage_events": {"grok": 1}, "unpriced_usage_events": {}}
    with connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM pricing_gaps WHERE model_key LIKE 'cursor:%'").fetchone()
        assert not connection.execute("SELECT 1 FROM ingest_runs WHERE source='cursor_local'").fetchone()
        keys = {row[0] for row in connection.execute("SELECT key FROM app_meta")}
        marker = json.loads(connection.execute("SELECT value FROM app_meta WHERE key=?", (RETIRED_USAGE_KEY,)).fetchone()[0])
    assert "usage.alias.v1:cursor_local:abc" not in keys
    assert "sync.cursor.v1:keep" in keys, "the sync pagination cursor is not Cursor data"
    assert marker["removedEvents"] == 2
    backups = list((tmp_path / "backups").glob("pre-cursor-removal-*.db"))
    assert len(backups) == 1
    assert counts(backups[0]) == {"usage_events": {"cursor": 1, "grok": 1}, "unpriced_usage_events": {"cursor": 1}}

    # Later calls only read the marker, even from a fresh process.
    with connect(database) as connection:
        upsert_usage_event(connection, usage("c4", "cursor", "cursor_usage_service"))
    monkeypatch.setattr(db_module, "_RETIRED_USAGE_CHECKED", set())
    assert retire_cursor_usage(database) == 0
    assert len(list((tmp_path / "backups").glob("*.db"))) == 1


def test_a_database_without_cursor_usage_takes_no_backup(tmp_path):
    database = tmp_path / "spend.db"
    initialize(database)
    assert not (tmp_path / "backups").exists()
    with connect(database) as connection:
        assert connection.execute("SELECT 1 FROM app_meta WHERE key=?", (RETIRED_USAGE_KEY,)).fetchone()


def test_hub_drops_cursor_usage_pushed_by_an_older_spoke(tmp_path):
    database = tmp_path / "hub.db"
    initialize(database)

    def item(raw_id: str, tool: str, source: str) -> dict:
        return {"raw_id": raw_id, "source": source, "tool_key": tool, "model_key": f"{tool}:m", "occurred_at": WHEN,
                "input_tokens": 10, "output_tokens": 5}

    apply_sync_push(database, PricingEngine.load(ROOT / "pricing"), machine_id="laptop-1", body={
        "usageEvents": [item("a", "cursor", "cursor_usage_service"), item("b", "grok", "grok_local")],
        "unpricedEvents": [item("c", "cursor", "cursor_local")],
        "sessions": [{"session_id": "s", "tool_key": "cursor", "started_at": WHEN, "model_key": "cursor:m"}],
        "agentRuns": [{"id": "cursor:1", "name": "Cursor", "model_key": "cursor:grok-4.6", "state": "LIVE"},
                      {"id": "grok:1", "name": "Grok", "model_key": "supergrok:grok-4.6", "state": "LIVE"}],
    })
    totals = counts(database)
    assert "cursor" not in totals["usage_events"] and "cursor" not in totals["unpriced_usage_events"]
    assert sum(totals["usage_events"].values()) + sum(totals["unpriced_usage_events"].values()) == 1
    with connect(database) as connection:
        assert not connection.execute("SELECT 1 FROM sessions WHERE tool_key='cursor'").fetchone()
        runs = [row[0] for row in connection.execute("SELECT model_key FROM agent_runs")]
    assert runs == ["supergrok:grok-4.6"]
