"""Provider switches, transactional checkpoints, and v0.6.0 repair replay."""

from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal
import json
from pathlib import Path
import sqlite3
from threading import Barrier

import pytest

from spend_app.adapters import opencode_local as adapter, unchanged
from spend_app.adapters.common import persist_rows
from spend_app.db import connect, initialize, record_coverage_gap, write_opencode_progress
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push, export_sync_batch


ROOT = Path(__file__).resolve().parents[1]
SESSION = "synthetic-switch"


def snapshot(provider, day, inp, out, cost):
    return adapter.SessionSnapshot(
        SESSION, provider, "unlisted-switch-fixture", "fixture", inp, 0, 0,
        out, None, cost, datetime(2026, 11, day, 12, tzinfo=UTC),
    )


REPORTS = (
    snapshot("alpha", 14, 100, 20, 1.00),
    snapshot("beta", 15, 110, 22, 1.10),
    snapshot("alpha", 16, 120, 25, 1.20),
)


@pytest.fixture
def fixture(tmp_path):
    source, dest, hub = (tmp_path / name for name in ("source.db", "app.db", "hub.db"))
    with closing(sqlite3.connect(source)) as db, db:
        db.execute(
            "CREATE TABLE session(id TEXT PRIMARY KEY,project_id TEXT,directory TEXT,path TEXT,"
            "model TEXT,cost REAL,tokens_input INTEGER,tokens_output INTEGER,"
            "tokens_reasoning INTEGER,tokens_cache_read INTEGER,tokens_cache_write INTEGER,time_updated INTEGER)"
        )
    initialize(dest)
    return source, dest, hub, PricingEngine.load(ROOT / "pricing")


def write_snapshot(source, sample):
    with closing(sqlite3.connect(source)) as db, db:
        db.execute(
            "INSERT OR REPLACE INTO session VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (sample.session_id, None, sample.project, None,
             json.dumps({"id": sample.model_id, "providerID": sample.provider_id}),
             sample.cost_usd, sample.input_tokens, sample.output_tokens, sample.reasoning_tokens,
             sample.cached_input_tokens, sample.cache_write_tokens, int(sample.observed_at.timestamp() * 1000)),
        )


def measured(path):
    with connect(path) as db:
        return [
            tuple(row) for row in db.execute(
                "SELECT raw_id,tool_key,input_tokens,output_tokens,cost_usd "
                "FROM unpriced_usage_events ORDER BY occurred_at"
            )
        ]


def measured_totals(path):
    rows = measured(path)
    return (
        sum(row[2] for row in rows),
        sum(row[3] for row in rows),
        sum(Decimal(str(row[4])) for row in rows if row[4] is not None),
    )


def collect(source, dest, pricing):
    return adapter.ingest(database_path=dest, pricing=pricing, source_database=source)


def test_cumulative_provider_switch_counts_observed_deltas_after_move_and_restart(fixture):
    source, dest, _, pricing = fixture
    write_snapshot(source, REPORTS[0])
    collect(source, dest, pricing)
    moved = source.with_name("relocated.db")
    source.rename(moved)
    unchanged.reset_state()
    write_snapshot(moved, REPORTS[1])
    collect(moved, dest, pricing)
    unchanged.reset_state()
    write_snapshot(moved, REPORTS[2])
    collect(moved, dest, pricing)
    collect(moved, dest, pricing)
    assert [row[1:] for row in measured(dest)] == [
        ("opencode-provider:alpha", 100, 20, 1.0),
        ("opencode-provider:beta", 10, 2, 0.1),
        ("opencode-provider:alpha", 10, 3, 0.1),
    ]
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))

    # An older copy and a later lower counter cannot mint a new baseline.
    for sample in (REPORTS[0], replace(REPORTS[1], observed_at=datetime(2026, 11, 17, tzinfo=UTC))):
        write_snapshot(moved, sample)
        collect(moved, dest, pricing)
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))


def test_competing_cumulative_imports_share_the_writer_checkpoint(fixture, monkeypatch):
    source, dest, _, pricing = fixture
    write_snapshot(source, REPORTS[0])
    collect(source, dest, pricing)
    ready = Barrier(2)

    def parallel_persist(**kwargs):
        ready.wait(timeout=10)
        return persist_rows(**kwargs)

    monkeypatch.setattr(adapter, "persist_rows", parallel_persist)

    def note(sample):
        return adapter._ingest_cumulative(
            database_path=dest, pricing=pricing, source_database=source, snapshots=[sample]
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        first, second = (pool.submit(note, sample) for sample in REPORTS[1:])
        first.result(timeout=15)
        second.result(timeout=15)
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))


def test_same_timestamp_switch_uses_coherent_high_water_mark(fixture):
    source, dest, _, pricing = fixture
    samples = (
        REPORTS[0], replace(REPORTS[1], observed_at=REPORTS[0].observed_at), REPORTS[2],
    )
    for sample in samples:
        write_snapshot(source, sample)
        collect(source, dest, pricing)
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))


def test_interrupted_cumulative_import_does_not_advance_progress_or_usage(fixture, monkeypatch):
    source, dest, _, pricing = fixture
    write_snapshot(source, REPORTS[0])
    collect(source, dest, pricing)
    write_snapshot(source, REPORTS[1])

    def fail_after_first_checkpoint(*args, **kwargs):
        write_opencode_progress(*args, **kwargs)
        raise sqlite3.OperationalError("synthetic interrupted write")

    with monkeypatch.context() as patch:
        patch.setattr(adapter, "write_opencode_progress", fail_after_first_checkpoint)
        assert collect(source, dest, pricing)["status"] == "failed"
    assert measured_totals(dest) == (100, 20, Decimal("1.0"))
    with connect(dest) as connection:
        assert not connection.execute(
            "SELECT 1 FROM opencode_session_progress WHERE provider_id='beta'"
        ).fetchone()
    collect(source, dest, pricing)
    assert measured_totals(dest) == (110, 22, Decimal("1.10"))


def seed_legacy(dest, pricing, *, separate_locations=False):
    """Recreate audited v0.6.0 state with a checkpoint per provider."""
    already = {}
    for observation in REPORTS:
        key = f"{observation.session_id}\x1f{observation.provider_id}"
        rows, progress, labels = adapter.plan_rows([observation], already)
        already.update(progress)
        row, = rows
        _, issue = labels[0]

        def finish(connection):
            scope = "b" * 24 if separate_locations and observation.provider_id == "beta" else "a" * 24
            for session in (observation.session_id, f"{scope}:{observation.session_id}"):
                write_opencode_progress(
                    connection, session_id=session, provider_id=observation.provider_id,
                    **progress[key],
                )
            record_coverage_gap(connection, raw_id=row.raw_id, source=row.source, tool_key=row.tool_key,
                                model_key=row.model_key, occurred_at=row.occurred_at.isoformat(), issue=issue)

        persist_rows(database_path=dest, pricing=pricing, source=adapter.SOURCE, usage_rows=rows, finalize=finish)


def test_verified_legacy_corrections_keep_raw_ids_and_sync_once(fixture):
    source, dest, hub, pricing = fixture
    write_snapshot(source, REPORTS[-1])
    seed_legacy(dest, pricing)
    assert measured_totals(dest) == (230, 47, Decimal("2.3"))
    original = measured(dest)
    payload, cursor = export_sync_batch(dest)
    apply_sync_push(hub, pricing, machine_id="laptop", body=payload)
    assert measured_totals(hub) == (230, 47, Decimal("2.3"))

    result = collect(source, dest, pricing)
    assert result["historicalCorrections"] == 2
    assert result["historicalUnresolved"] == 0
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))
    assert [row[0] for row in measured(dest)] == [row[0] for row in original]
    corrected, cursor = export_sync_batch(dest, cursor=cursor)
    assert {entry["raw_id"] for entry in corrected["usageChanges"]["changes"]} == {row[0] for row in original[1:]}
    for _ in range(2):
        apply_sync_push(hub, pricing, machine_id="laptop", body=corrected)
        assert measured_totals(hub) == (120, 25, Decimal("1.20"))
    unchanged.reset_state()
    assert collect(source, dest, pricing)["historicalCorrections"] == 0
    assert export_sync_batch(dest, cursor=cursor)[0]["usageChanges"]["changes"] == []


def test_unverifiable_legacy_usage_is_reported_and_preserved(fixture):
    source, dest, _, pricing = fixture
    write_snapshot(source, REPORTS[-1])
    seed_legacy(dest, pricing)
    with connect(dest) as db:
        db.execute("UPDATE unpriced_usage_events SET input_tokens=111 WHERE tool_key='opencode-provider:beta'")
    before = measured(dest)
    result = collect(source, dest, pricing)
    assert result["historicalCorrections"] == 0
    assert result["historicalUnresolved"] == 1
    assert "opencode_prior_provider_switch_could_not_be_verified" in result["issues"]
    assert measured(dest) == before
    with closing(sqlite3.connect(source)) as db, db:
        db.execute("DELETE FROM session")
    unchanged.reset_state()
    retained = collect(source, dest, pricing)
    assert retained["retainedHistoricalUnresolved"] == 1
    assert measured(dest) == before


def test_unrelated_location_checkpoints_do_not_prove_historical_switch(fixture):
    source, dest, _, pricing = fixture
    seed_legacy(dest, pricing, separate_locations=True)
    result = collect(source, dest, pricing)
    assert result["historicalCorrections"] == 0
    assert result["retainedHistoricalUnresolved"] == 1
    assert measured_totals(dest) == (230, 47, Decimal("2.3"))


def test_repair_reaches_closed_session_using_retained_local_evidence(fixture):
    source, dest, _, pricing = fixture
    seed_legacy(dest, pricing)

    # The old source no longer retains the session, but the BURNRATE store
    # has every original raw event and terminal provider checkpoint.
    result = collect(source, dest, pricing)
    assert result["historicalCorrections"] == 2
    assert measured_totals(dest) == (120, 25, Decimal("1.20"))
