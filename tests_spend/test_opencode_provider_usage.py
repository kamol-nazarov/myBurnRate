"""Synthetic regressions for OpenCode provider attribution and replay."""
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from spend_app.adapters import opencode_schema as schema, unchanged
from spend_app.adapters.common import stable_id
from spend_app.adapters.opencode_local import ingest, parse_database
from spend_app.db import connect, initialize
from spend_app.native_usage import usage_report
from spend_app.pricing import PricingEngine
from spend_app.wealthfront_window import merge_provider_entries

ROOT = Path(__file__).resolve().parents[1]


def message(provider, *, v2=False, message_id="same", model="unlisted-synthetic"):
    data = {"id": message_id, "sessionID": "session", "role": "assistant",
            "providerID": provider, "modelID": model,
            "tokens": {"input": 10, "output": 5, "reasoning": 2,
                       "cache": {"read": 3, "write": 1}},
            "time": {"created": 1760000000000, "completed": 1760000001000}}
    if v2:
        data["model"] = {"id": data.pop("modelID"), "providerID": data.pop("providerID")}
        data.pop("role")
    return data


@pytest.mark.parametrize("format", ["v1", "v2", "json"])
@pytest.mark.parametrize("provider,key", [
    ("isoquant", "opencode-provider:isoquant"),
    ("other/vendor name", "opencode-provider:other%2Fvendor%20name"),
    ("zai-coding-plan", "opencode"), ("opencode-go", "opencode-go"),
    (None, "opencode-provider:unknown"),
])
def test_message_provider_classification_and_pricing_namespace(format, provider, key):
    data = message(provider, v2=format == "v2")
    parsed = schema.parse_message(data, format=format, role="assistant")
    assert parsed.row.tool_key == key
    assert parsed.row.model_key == ("opencode-go:" if provider == "opencode-go" else "opencode:") + "unlisted-synthetic"
    assert parsed.row.raw_id == stable_id("opencode-message", provider or "unknown", "same")
    assert parsed.row.reasoning_tokens == 2
    assert parsed.row.output_tokens == (7 if provider == "opencode-go" else 5)


def test_equal_message_ids_from_different_providers_remain_distinct():
    a = schema.parse_message(message("isoquant"))
    b = schema.parse_message(message("another"))
    rows, conflicts = schema.merge_messages([a, b])
    assert conflicts == 0
    assert len(rows) == 2
    assert len({r.row.raw_id for r in rows}) == 2
    assert sum(r.row.input_tokens for r in rows) == 26


def write_messages(path):
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)")
        for provider in ("isoquant", "another", "zai-coding-plan", "opencode-go", "traycer-openrouter"):
            db.execute("INSERT INTO message VALUES(?,?,?)", ("same", "session", json.dumps(message(provider))))


def test_ingest_report_merge_and_repeat(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "usage.db"
    write_messages(source)
    initialize(dest)
    pricing = PricingEngine.load(ROOT / "pricing")
    first = ingest(database_path=dest, pricing=pricing, source_database=source)
    second = ingest(database_path=dest, pricing=pricing, source_database=source)
    assert first["status"] in {"success", "partial"}
    assert second.get("eventsWritten", 0) == 0
    assert second.get("unpricedEventsWritten", 0) == 0
    with connect(dest) as db:
        rows = [dict(r) for table in ("usage_events", "unpriced_usage_events")
                for r in db.execute(f"SELECT * FROM {table}")]
    assert len(rows) == 4
    assert {r["tool_key"] for r in rows} == {"opencode", "opencode-go", "opencode-provider:isoquant", "opencode-provider:another"}
    assert sum(r["input_tokens"] for r in rows) == 52
    assert all(r["model_key"].endswith(":unlisted-synthetic") for r in rows)
    settings = SimpleNamespace(database_path=dest, timezone="UTC")
    report = usage_report(settings, pricing, now=datetime(2026, 11, 1, tzinfo=UTC), period="all")
    tools = {r["key"]: r for r in report["tools"]}
    assert set(tools) == {r["tool_key"] for r in rows}
    assert tools["opencode-provider:isoquant"]["name"] == "OpenCode · Isoquant"
    assert all(r["tokens"] > 0 and r["valueUsd"] is None for r in tools.values())
    assert report["total"]["tokens"] == sum(r["tokens"] for r in tools.values())
    hud = {"capacity": [{"providerKey": "opencode", "title": "Z.AI", "rows": []},
                        {"providerKey": "opencode-go", "title": "OpenCode Go", "rows": []}]}
    merged = merge_provider_entries(report, hud, {}, report, report)
    by_key = {r["key"]: r for r in merged}
    assert set(tools) <= set(by_key)
    assert by_key["opencode"]["burn"]["dayTokens"] == tools["opencode"]["tokens"]
    assert by_key["opencode-provider:isoquant"]["burn"]["dayTokens"] == tools["opencode-provider:isoquant"]["tokens"]


def test_old_message_fingerprint_replays_existing_event_without_duplicate(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "usage.db"
    write_messages(source)
    initialize(dest)
    pricing = PricingEngine.load(ROOT / "pricing")
    ingest(database_path=dest, pricing=pricing, source_database=source)
    event_id = stable_id("opencode-message", "isoquant", "same")
    with connect(dest) as db:
        db.execute("UPDATE unpriced_usage_events SET tool_key='opencode' WHERE raw_id=?", (event_id,))
        # Simulate a cache signature produced by the previous parser version.
        db.execute("UPDATE ingest_file_cache SET signature=? WHERE source LIKE 'opencode_local:sessions:%'", (json.dumps(["old-parser-fingerprint", []]),))
        db.execute("DELETE FROM ingest_file_cache WHERE source NOT LIKE 'opencode_local:sessions:%'")
    unchanged.reset_state()
    ingest(database_path=dest, pricing=pricing, source_database=source)
    with connect(dest) as db:
        rows = list(db.execute("SELECT raw_id,tool_key FROM unpriced_usage_events WHERE raw_id=?", (event_id,)))
    assert len(rows) == 1
    assert rows[0][1] == "opencode-provider:isoquant"


@pytest.mark.parametrize("ambiguous,expected", [(False, "opencode-provider:isoquant"), (True, "opencode-provider:unknown")])
def test_cumulative_provider_and_unchanged_progress_reassignment(tmp_path, ambiguous, expected):
    source, dest = tmp_path / "source.db", tmp_path / "usage.db"
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE session(id TEXT,project_id TEXT,directory TEXT,path TEXT,model TEXT,cost REAL,tokens_input INTEGER,tokens_output INTEGER,tokens_reasoning INTEGER,tokens_cache_read INTEGER,tokens_cache_write INTEGER,time_updated INTEGER)")
        db.execute("INSERT INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)", ("s", None, None, None, json.dumps({"id":"unlisted-synthetic", "providerID":"isoquant"}), None, 10, 5, 2, 3, 1, 1760000001000))
    assert parse_database(source)[0].tool_key == "opencode-provider:isoquant"
    initialize(dest)
    pricing = PricingEngine.load(ROOT / "pricing")
    ingest(database_path=dest, pricing=pricing, source_database=source)
    with connect(dest) as db:
        db.execute("UPDATE unpriced_usage_events SET tool_key='opencode'")
        if ambiguous:
            db.execute("INSERT INTO opencode_session_progress SELECT session_id, ?, input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, reasoning_tokens, cost_usd, model_id, observed_at FROM opencode_session_progress WHERE session_id=?", ("other", "s"))
        unchanged.reset_state()
        before = [tuple(row) for row in db.execute("SELECT * FROM opencode_session_progress ORDER BY session_id,provider_id")]
    ingest(database_path=dest, pricing=pricing, source_database=source)
    with connect(dest) as db:
        rows = list(db.execute("SELECT tool_key,input_tokens,output_tokens FROM unpriced_usage_events"))
        after = [tuple(row) for row in db.execute("SELECT * FROM opencode_session_progress ORDER BY session_id,provider_id")]
    assert [tuple(row) for row in rows] == [(expected, 13, 5)]
    assert before == after





def test_merge_names_legacy_hub_provider_and_does_not_infer_billing():
    key = "opencode-provider:isoquant"
    report = {"tools": [{"key": key, "name": key, "tokens": 42, "models": []}]}
    row = next(r for r in merge_provider_entries(report) if r["key"] == key)
    assert row["name"] == "OpenCode · Isoquant"
    assert row["windows"] == []
    assert row["isPayg"] is False
    assert row["plan"] == "Recorded usage"


def test_known_model_reference_value_survives_separate_provider_rows(tmp_path):
    source, dest = tmp_path / "source.db", tmp_path / "usage.db"
    occurred = datetime(2026, 10, 10, tzinfo=UTC)
    timestamp = int(occurred.timestamp() * 1000)
    with sqlite3.connect(source) as db:
        db.execute("CREATE TABLE message(id TEXT, session_id TEXT, data TEXT)")
        for provider in ("isoquant", "another"):
            data = message(provider, model="gpt-6-sol")
            data["time"] = {"created": timestamp, "completed": timestamp}
            db.execute("INSERT INTO message VALUES(?,?,?)", ("same", "session", json.dumps(data)))
    initialize(dest)
    pricing = PricingEngine.load(ROOT / "pricing")
    result = ingest(database_path=dest, pricing=pricing, source_database=source)
    assert result["eventsWritten"] == 2
    with connect(dest) as db:
        rows = [dict(r) for r in db.execute("SELECT * FROM usage_events")]
    assert {r["tool_key"] for r in rows} == {"opencode-provider:isoquant", "opencode-provider:another"}
    assert {r["model_key"] for r in rows} == {"opencode:gpt-6-sol"}
    expected = float(pricing.compute(model_key="opencode:gpt-6-sol", occurred_at=occurred,
                                     input_tokens=13, cached_input_tokens=3,
                                     cache_write_tokens=1, cache_write_1h_tokens=0, output_tokens=5))
    assert expected > 0
    assert all(r["computed_cost_usd"] == pytest.approx(expected) for r in rows)
    report = usage_report(SimpleNamespace(database_path=dest, timezone="UTC"), pricing,
                          now=datetime(2026, 11, 1, tzinfo=UTC), period="all")
    tools = {r["key"]: r for r in report["tools"]}
    assert set(tools) == {"opencode-provider:isoquant", "opencode-provider:another"}
    assert all(r["valueUsd"] == pytest.approx(expected) for r in tools.values())
    assert report["total"]["valueUsd"] == pytest.approx(2 * expected)


def test_later_message_reattributes_coarse_delta_without_rewriting_it(tmp_path):
    from spend_app.adapters.opencode_granular import reconcile_messages
    from spend_app.adapters.opencode_local import DELTA_ISSUE
    from spend_app.db import UsageEvent, record_coverage_gap, upsert_usage_event, write_opencode_progress

    dest = tmp_path / "usage.db"
    initialize(dest)
    timestamp = "2025-10-01T00:00:00Z"
    event = UsageEvent(
        source="opencode_local", tool_key="opencode", model_key="opencode:gpt-6-sol",
        occurred_at=timestamp, session_id="session", project="synthetic",
        input_tokens=120, cached_input_tokens=20, cache_write_tokens=4,
        cache_write_1h_tokens=0, output_tokens=30, reasoning_tokens=3,
        cost_usd=0.123, computed_cost_usd=0.456, raw_id="historical-delta",
        ingested_at=timestamp, is_exact=False,
    )
    fine = schema.parse_message(message("isoquant"))
    assert fine.started_at > datetime(2025, 10, 1, tzinfo=UTC)
    with connect(dest) as db:
        upsert_usage_event(db, event)
        record_coverage_gap(db, raw_id=event.raw_id, source=event.source,
                            tool_key=event.tool_key, model_key=event.model_key,
                            occurred_at=timestamp, issue=DELTA_ISSUE)
        write_opencode_progress(db, session_id="session", provider_id="isoquant",
                                input_tokens=100, cached_input_tokens=20,
                                cache_write_tokens=4, output_tokens=30,
                                reasoning_tokens=3, cost_usd=0.123,
                                model_id="gpt-6-sol", observed_at=timestamp)
        before = dict(db.execute("SELECT * FROM usage_events WHERE raw_id=?", (event.raw_id,)).fetchone())
        before_gap = dict(db.execute("SELECT * FROM coverage_gap_events WHERE raw_id=?", (event.raw_id,)).fetchone())
        issues = []
        outputs = reconcile_messages(db, [fine], issues)
        after = dict(db.execute("SELECT * FROM usage_events WHERE raw_id=?", (event.raw_id,)).fetchone())
        after_gap = dict(db.execute("SELECT * FROM coverage_gap_events WHERE raw_id=?", (event.raw_id,)).fetchone())
        assert db.execute("SELECT COUNT(*) FROM usage_events WHERE raw_id=?", (event.raw_id,)).fetchone()[0] == 1
    assert not issues
    assert [row.raw_id for row in outputs] == [fine.row.raw_id]
    assert after.pop("tool_key") == "opencode-provider:isoquant"
    assert before.pop("tool_key") == "opencode"
    assert after == before
    assert after_gap.pop("tool_key") == "opencode-provider:isoquant"
    assert before_gap.pop("tool_key") == "opencode"
    assert after_gap == before_gap
    assert after_gap["issue"] == DELTA_ISSUE
