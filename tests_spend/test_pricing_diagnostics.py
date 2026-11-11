"""Coverage reasons follow current cards and stored observations without writes."""

import json
import sqlite3
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.db import connect, initialize
from spend_app.diagnostics import source_reports
from spend_app.pricing import PricingEngine
from spend_app.pricing_audit import coverage_by_source
from spend_app.timeutil import epoch_micros

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 9, 18, tzinfo=UTC)


def metadata_db():
    connection = sqlite3.connect(":memory:")
    for table in ("usage_events", "unpriced_usage_events"):
        connection.execute(
            f"CREATE TABLE {table} (source TEXT, model_key TEXT, occurred_at TEXT, "
            "occurred_epoch_us INTEGER, input_tokens INTEGER, cache_write_tokens INTEGER, "
            "output_tokens INTEGER, telemetry_complete INTEGER)"
        )
    return connection


def insert(
    connection,
    model,
    when=NOW,
    *,
    complete=1,
    source="codex_local",
    table="unpriced_usage_events",
):
    stamp = when.isoformat() if isinstance(when, datetime) else when
    epoch = epoch_micros(when) if isinstance(when, datetime) else None
    connection.execute(
        f"INSERT INTO {table} VALUES(?,?,?,?,?,?,?,?)",
        (source, model, stamp, epoch, 100, 10, 1, complete),
    )


def test_distinguishes_identity_current_history_components_and_bad_dates():
    db = metadata_db()
    engine = PricingEngine.load(ROOT / "pricing")
    insert(db, "gpt-6-astra", table="usage_events")
    insert(db, "gpt-6-astra", datetime(2026, 1, 1, tzinfo=UTC))
    insert(db, "gpt-6-astra", complete=0)
    insert(db, "gpt-6-astra", "bad-stamp")
    insert(db, "unverified-model")
    insert(db, "muse:unknown", source="muse_local")
    before = db.total_changes
    result = coverage_by_source(db, engine, now=NOW)
    assert db.total_changes == before
    codex = result["codex_local"]
    assert codex["records"] == 5
    assert (
        codex["pricedRecords"] == 2
    )  # A card match does not prove complete telemetry.
    assert codex["missingRateRecords"] == 3
    assert (
        codex["missingRateTokens"] == 333
    )  # Cached/reasoning subsets are never added twice.
    assert {r["code"]: r["records"] for r in codex["reasons"]} == {
        "missing_historical_rate": 1,
        "incomplete_telemetry": 1,
        "invalid_timestamp": 1,
        "missing_current_rate": 1,
    }
    assert result["muse_local"]["reasons"][0]["code"] == "unknown_model_identity"


def test_corrections_deletions_and_new_cards_are_visible_without_stale_cache():
    db = metadata_db()
    engine = PricingEngine.load(ROOT / "pricing")
    insert(db, "new-model", complete=0)
    first = coverage_by_source(db, engine, now=NOW)["codex_local"]
    assert first["missingRateRecords"] == first["incompleteRecords"] == 1
    db.execute(
        "UPDATE unpriced_usage_events SET model_key='gpt-6-astra',telemetry_complete=1"
    )
    second = coverage_by_source(db, engine, now=NOW)["codex_local"]
    assert second["reasons"] == []
    db.execute("UPDATE unpriced_usage_events SET model_key='new-model'")
    card = replace(
        engine.resolve("gpt-6-astra", NOW), model_key="new-model", aliases=()
    )
    updated = PricingEngine([*engine.prices, card])
    assert coverage_by_source(db, updated, now=NOW)["codex_local"]["reasons"] == []
    db.execute("DELETE FROM unpriced_usage_events")
    assert coverage_by_source(db, updated, now=NOW) == {}


def test_alias_interval_boundaries_and_expired_current_rate():
    db = metadata_db()
    engine = PricingEngine.load(ROOT / "pricing")
    floor = NOW - timedelta(days=1)
    card = replace(
        engine.resolve("gpt-6-astra", NOW),
        model_key="fixture-model",
        aliases=("fixture-alias",),
        effective_from=floor,
        effective_to=NOW,
    )
    pricing = PricingEngine([card])
    insert(db, "fixture-alias", floor)
    insert(db, "fixture-alias", NOW - timedelta(microseconds=1))
    insert(db, "fixture-alias", NOW)
    insert(db, "fixture-alias", floor - timedelta(microseconds=1))
    result = coverage_by_source(db, pricing, now=NOW)["codex_local"]
    assert result["pricedRecords"] == result["missingRateRecords"] == 2
    assert result["models"][0]["currentRate"] is False
    assert {r["code"] for r in result["reasons"]} == {
        "missing_current_rate",
        "missing_historical_rate",
    }


def test_grouped_read_resolves_per_model_not_per_event(monkeypatch):
    db = metadata_db()
    engine = PricingEngine.load(ROOT / "pricing")
    for _ in range(1000):
        insert(db, "gpt-6-astra", table="usage_events")
        insert(db, "claude-opus-5-5", source="claude_local")
    calls = []
    original = engine.resolve
    monkeypatch.setattr(
        engine,
        "resolve",
        lambda model, when: (calls.append(model), original(model, when))[1],
    )
    # Existing epoch columns must avoid re-parsing all history timestamps.
    monkeypatch.setattr(
        "spend_app.pricing_audit._epoch",
        lambda value: (_ for _ in ()).throw(AssertionError(value)),
    )
    queries = []
    db.set_trace_callback(queries.append)
    report = coverage_by_source(db, engine, now=NOW)
    assert sum(v["records"] for v in report.values()) == 2000
    assert sorted(calls) == ["claude-opus-5-5", "gpt-6-astra"]
    assert sum(q.startswith("WITH observations") for q in queries) == 1


def test_source_diagnostics_replaces_obsolete_gap_labels_and_sanitizes_models(tmp_path):
    path = tmp_path / "diagnostics.db"
    initialize(path)
    engine = PricingEngine.load(ROOT / "pricing")
    with connect(path) as db:
        db.execute(
            "INSERT INTO pricing_gaps(model_key,source,first_seen_at,last_seen_at,sample_raw_id) "
            "VALUES('gpt-6-astra','codex_local',?,?,'old-gap')",
            (NOW.isoformat(), NOW.isoformat()),
        )
        for model in ("gpt-6-astra", "C:/Users/Private/model"):
            db.execute(
                "INSERT INTO unpriced_usage_events(source,tool_key,model_key,occurred_at,"
                "input_tokens,cached_input_tokens,cache_write_tokens,output_tokens,raw_id,ingested_at) "
                "VALUES('codex_local','codex',?,?,100,0,0,1,?,?)",
                (model, NOW.isoformat(), model, NOW.isoformat()),
            )
        report = next(
            r
            for r in source_reports(db, NOW, pricing=engine)
            if r["source"] == "codex_local"
        )
        assert report["pricingMissing"] == ["unidentified model"]
        assert "Private" not in json.dumps(report)
        assert report["pricingCoverage"]["records"] == 2
        assert report["pricingCoverage"]["missingRateRecords"] == 1
        astra = next(
            m
            for m in report["pricingCoverage"]["models"]
            if m["model"] == "gpt-6-astra"
        )
        assert astra["reasons"] == []
        db.execute("DELETE FROM unpriced_usage_events")
        empty = next(
            r
            for r in source_reports(db, NOW, pricing=engine)
            if r["source"] == "codex_local"
        )
        assert empty["pricingMissing"] == []
        assert empty["pricingCoverage"]["models"] == []
