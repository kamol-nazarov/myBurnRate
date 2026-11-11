"""Usage repairs replicate transactionally without echoing imported history."""

from dataclasses import asdict, replace
from datetime import UTC, datetime
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spend_app.adapters.usage_repair import delete_event
from spend_app.db import (UsageEvent, UnpricedUsageEvent, backfill_event_epochs, connect,
                          initialize, upsert_unpriced_event, upsert_usage_event)
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push, export_sync_batch, read_sync_cursor, sync_once

NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)
STAMP = NOW.isoformat()
ROOT = Path(__file__).resolve().parents[1]


def event(raw_id="event-a", tokens=100):
    return UsageEvent("codex_local", "codex", "gpt-6-astra", STAMP, "fixture-session", "fixture",
                      tokens, 0, 0, 0, 1, None, None, 0.01, raw_id, STAMP)


def total(path, table="usage_events"):
    with connect(path) as db:
        return db.execute(f"SELECT COALESCE(SUM(input_tokens),0) FROM {table}").fetchone()[0]


@pytest.fixture
def pair(tmp_path):
    local, hub = tmp_path / "local.db", tmp_path / "hub.db"
    initialize(local)
    initialize(hub)
    return local, hub, PricingEngine.load(ROOT / "pricing")


def push(pair, cursor=None, limit=500):
    local, hub, pricing = pair
    payload, cursor = export_sync_batch(local, cursor=cursor, limit=limit)
    result = apply_sync_push(hub, pricing, machine_id="laptop", body=payload, now=NOW)
    assert result["usageChangeSeq"] == cursor["change_seq"]
    return payload, cursor


def test_corrections_deletions_replay_and_no_echo(pair):
    local, hub, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
    original, cursor = push(pair)
    assert total(hub) == 100
    with connect(local) as db:
        upsert_usage_event(db, event(tokens=50))
    corrected, cursor = push(pair, cursor)
    assert total(hub) == 50
    # Retrying an older acknowledged request must not undo the correction.
    apply_sync_push(hub, pricing, machine_id="laptop", body=original, now=NOW)
    assert total(hub) == 50
    with connect(local) as db:
        delete_event(db, "event-a")
    deleted, cursor = push(pair, cursor)
    assert deleted["usageChanges"]["changes"][0]["kind"] == "delete"
    assert total(hub) == 0
    apply_sync_push(hub, pricing, machine_id="laptop", body=corrected, now=NOW)
    apply_sync_push(hub, pricing, machine_id="laptop", body=deleted, now=NOW)
    assert total(hub) == 0
    echo, _ = export_sync_batch(hub)
    assert echo["usageEvents"] == echo["unpricedEvents"] == echo["usageChanges"]["changes"] == []


def test_priced_unpriced_transitions_use_current_state_once(pair):
    local, hub, _ = pair
    unknown = asdict(event())
    unknown.pop("computed_cost_usd")
    unknown.pop("is_exact")
    unknown.update(unclassified_tokens=0, telemetry_complete=False)
    with connect(local) as db:
        upsert_unpriced_event(db, UnpricedUsageEvent(**unknown))
    _, cursor = push(pair)
    assert total(hub, "unpriced_usage_events") == 100
    with connect(local) as db:
        upsert_usage_event(db, event(tokens=80))
    _, cursor = push(pair, cursor)
    assert total(hub) == 80 and total(hub, "unpriced_usage_events") == 0
    with connect(local) as db:
        upsert_unpriced_event(db, UnpricedUsageEvent(**unknown))
    push(pair, cursor)
    assert total(hub) == 0 and total(hub, "unpriced_usage_events") == 100


def test_journal_rolls_back_with_event_transaction(pair):
    local, _, _ = pair
    with pytest.raises(RuntimeError), connect(local) as db:
        upsert_usage_event(db, event())
        raise RuntimeError("simulated failed transaction")
    payload, cursor = export_sync_batch(local)
    assert payload["usageChanges"]["changes"] == []
    assert cursor["change_seq"] == 0


def test_small_batches_coalesce_repeated_changes_and_deliver_all(pair):
    local, hub, _ = pair
    with connect(local) as db:
        upsert_usage_event(db, event(tokens=100))
        upsert_usage_event(db, event(tokens=50))
        upsert_usage_event(db, event("event-b", 30))
    cursor = None
    for _ in range(3):
        payload, cursor = push(pair, cursor, limit=1)
    assert total(hub) == 80
    payload, _ = push(pair, cursor, limit=1)
    assert payload["usageChanges"]["changes"] == []


def test_legacy_cursor_replays_existing_events_into_new_protocol(pair):
    local, hub, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
    legacy = {"usageEvents": [asdict(event())]}
    apply_sync_push(hub, pricing, machine_id="laptop", body=legacy, now=NOW)
    with connect(local) as db:
        upsert_usage_event(db, event(tokens=50))
    _, cursor = push(pair, {"usage_id": 999, "unpriced_id": 999})
    assert cursor["change_seq"] > 0
    assert total(hub) == 50


def test_journal_seeds_existing_rows_on_upgrade(pair):
    local, hub, _ = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
        for name, in db.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'journal_%'").fetchall():
            db.execute(f"DROP TRIGGER {name}")
        db.execute("DROP TABLE usage_changes")
        db.execute("DELETE FROM app_meta WHERE key='usage_changes.initialized.v1'")
        db.execute("UPDATE app_meta SET value='11' WHERE key='schema_version'")
    initialize(local)
    push(pair)
    assert total(hub) == 100
    with connect(local) as db:
        first = db.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0]
    initialize(local)
    with connect(local) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0] == first


def test_old_hub_does_not_acknowledge_unknown_tombstones(pair):
    local, _, _ = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
        delete_event(db, "event-a")
    class OldHub:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "eventsWritten": 0})
    result = sync_once(SimpleNamespace(database_path=local), pair[2], hub_url="https://hub.test",
                       token="fixture", collect_local=False, client=OldHub())
    assert result["usageChangesPending"] is True
    assert read_sync_cursor(local, "https://hub.test")["change_seq"] == 0


def test_failed_push_never_advances_cursor(pair):
    local, _, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
    class FailedHub:
        def post(self, *args, **kwargs):
            return SimpleNamespace(status_code=503, text="unavailable")
    with pytest.raises(RuntimeError):
        sync_once(SimpleNamespace(database_path=local), pricing, hub_url="https://hub.test",
                  token="fixture", collect_local=False, client=FailedHub())
    assert read_sync_cursor(local, "https://hub.test") == {"usage_id": 0, "unpriced_id": 0}


def test_sequence_gap_and_invalid_timestamp_roll_back(pair):
    local, hub, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
    payload, _ = export_sync_batch(local)
    payload["usageChanges"]["after"] = payload["usageChanges"]["through"]
    payload["usageChanges"]["changes"] = []
    with pytest.raises(ValueError, match="gap"):
        apply_sync_push(hub, pricing, machine_id="laptop", body=payload, now=NOW)
    with pytest.raises(ValueError, match="timestamp"):
        apply_sync_push(hub, pricing, machine_id="laptop",
                        body={"usageEvents": [asdict(event()), asdict(replace(event("bad"), occurred_at="invalid"))]}, now=NOW)
    assert total(hub) == 0


def test_invalid_epoch_backfill_makes_progress_and_reports_counts(pair):
    local, _, _ = pair
    with connect(local) as db:
        upsert_usage_event(db, replace(event("bad"), occurred_at="invalid-timestamp"))
        upsert_usage_event(db, event("good"))
        db.execute("UPDATE usage_events SET occurred_epoch_us=NULL")
        # A strict execution budget turns an accidental retry loop into a failure.
        db.set_progress_handler(lambda: 1, 20_000)
        assert backfill_event_epochs(db, batch_size=1) == 1
        assert backfill_event_epochs(db, batch_size=1) == 0
        row = db.execute("SELECT value FROM app_meta WHERE key='migration.invalid_event_timestamps'").fetchone()
        assert json.loads(row[0]) == {"usage_events": 1}
        assert db.execute("SELECT occurred_epoch_us FROM usage_events WHERE raw_id='bad'").fetchone()[0] is None
        assert db.execute("SELECT occurred_epoch_us FROM usage_events WHERE raw_id='good'").fetchone()[0] is not None


def test_bad_history_is_skipped_without_blocking_good_history_and_later_repairs_sync(pair):
    local, hub, pricing = pair
    # A bad local correction must not delete a previously valid remote value.
    apply_sync_push(hub, pricing, machine_id="laptop", body={"usageEvents": [asdict(event("bad", 20))]}, now=NOW)
    with connect(local) as db:
        upsert_usage_event(db, replace(event("bad", 30), occurred_at="invalid"))
        upsert_usage_event(db, event("good", 100))
    payload, cursor = push(pair)
    changes = payload["usageChanges"]["changes"]
    assert changes[0]["kind"] == "skip" and changes[0]["reason"] == "invalid_timestamp"
    assert [row["raw_id"] for row in payload["usageEvents"]] == ["good"]
    assert total(hub) == 120
    with connect(hub) as db:
        run = db.execute("SELECT status,error FROM ingest_runs ORDER BY id DESC LIMIT 1").fetchone()
        assert run["status"] == "partial" and "invalid timestamps" in run["error"]
    empty, _ = push(pair, cursor)
    assert empty["usageChanges"]["changes"] == []
    with connect(local) as db:
        upsert_usage_event(db, event("bad", 30))
    repaired, _ = push(pair, cursor)
    assert repaired["usageChanges"]["changes"][0]["kind"] == "usage"
    assert total(hub) == 130
