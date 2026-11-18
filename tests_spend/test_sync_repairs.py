"""Usage repairs replicate transactionally without echoing imported history."""

from dataclasses import asdict, replace
from datetime import UTC, datetime, timedelta
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from spend_app.adapters.usage_repair import delete_event
from spend_app.db import (UsageEvent, UnpricedUsageEvent, backfill_event_epochs, backup_database, connect,
                          initialize, upsert_quota, upsert_unpriced_event, upsert_usage_event)
from spend_app.pricing import PricingEngine
from spend_app.sync import (
    apply_sync_push, export_sync_batch, list_machines, read_sync_cursor, register_machine,
    register_or_reconnect_machine, rename_machine, rotate_machine_token, sync_once, write_sync_cursor,
)

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
    payload.pop("usageContinuity")
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


class ReceiptHub:
    def __init__(self, pair):
        self.database, self.pricing = pair[1:]
        self.before_reply = None
        self.batches = []

    def post(self, _url, *, json, **_kwargs):
        self.batches.append(json)
        result = apply_sync_push(self.database, self.pricing, machine_id="laptop", body=json, now=NOW)
        if self.before_reply:
            self.before_reply(json, result)
        return SimpleNamespace(status_code=200, json=lambda: result)


def sync_local(local, pricing, client):
    return sync_once(SimpleNamespace(database_path=local), pricing, hub_url="https://hub.test",
                     token="fixture", collect_local=False, client=client)


@pytest.mark.parametrize("divergent_count", (2, 3))
def test_restore_spoke_preserves_hub_history_with_reused_sequences(pair, tmp_path, divergent_count):
    local, hub, pricing = pair
    client = ReceiptHub(pair)
    with connect(local) as db:
        upsert_usage_event(db, event("shared", 20))
    sync_local(local, pricing, client)
    restored = backup_database(local, tmp_path / "restored.db")
    with connect(local) as db:
        upsert_usage_event(db, event("hub-only-1", 100))
        upsert_usage_event(db, event("hub-only-2", 10))
    sync_local(local, pricing, client)
    # The restored fork can end below or exactly at the hub's numeric tip.
    with connect(restored) as db:
        upsert_usage_event(db, event("fork-new", 50))
        if divergent_count == 3:
            upsert_usage_event(db, event("fork-other", 5))
    result = sync_local(restored, pricing, client)
    assert not result.get("usageChangesPending")
    assert result["usageChangeSeq"] == divergent_count
    sync_local(restored, pricing, client)
    with connect(hub) as db:
        rows = dict(db.execute("SELECT raw_id, input_tokens FROM usage_events"))
    assert rows == {
        "m:laptop:shared": 20, "m:laptop:hub-only-1": 100,
        "m:laptop:hub-only-2": 10, "m:laptop:fork-new": 50,
        **({"m:laptop:fork-other": 5} if divergent_count == 3 else {}),
    }


def test_receipt_retries_with_same_prefix_and_interrupted_recovery(pair, tmp_path):
    local, hub, pricing = pair
    client = ReceiptHub(pair)
    with connect(local) as db:
        upsert_usage_event(db, event("shared", 20))
    sync_local(local, pricing, client)

    def disconnect(_batch, _result):
        raise ConnectionError("simulated response loss")

    with connect(local) as db:
        upsert_usage_event(db, event("shared", 40))
    restored = backup_database(local, tmp_path / "restored.db")
    client.before_reply = disconnect
    with pytest.raises(ConnectionError):
        sync_local(local, pricing, client)
    client.before_reply = None
    assert sync_local(local, pricing, client)["eventsWritten"] == 0
    with connect(local) as db:
        upsert_usage_event(db, event("shared", 60))
    sync_local(local, pricing, client)

    # The backup includes the previously accepted correction as an unsent
    # update. It must not undo a later valid update in a different branch.
    with connect(restored) as db:
        upsert_usage_event(db, event("fork-new", 7))

    def interrupt_recovery(batch, result):
        if batch["usageContinuity"].get("rebase") and not result.get("usageRetry"):
            raise ConnectionError("simulated recovery response loss")

    client.before_reply = interrupt_recovery
    with pytest.raises(ConnectionError):
        sync_local(restored, pricing, client)
    assert read_sync_cursor(restored, "https://hub.test")["usage_rebase"]
    client.before_reply = None
    assert sync_local(restored, pricing, client)["eventsWritten"] == 0
    assert "usage_rebase" not in read_sync_cursor(restored, "https://hub.test")
    assert total(hub) == 67
    with connect(restored) as db:
        delete_event(db, "fork-new")
    sync_local(restored, pricing, client)
    assert total(hub) == 60


def test_unproved_legacy_ack_does_not_prune_restored_changes(pair):
    local, _, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())

    class LegacyHub:
        def post(self, *_args, **_kwargs):
            return SimpleNamespace(status_code=200, json=lambda: {"ok": True, "usageChangeSeq": 999})

    result = sync_local(local, pricing, LegacyHub())
    assert result["usageChangesPending"] is True
    assert read_sync_cursor(local, "https://hub.test")["change_seq"] == 0
    with connect(local) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0] == 1


def test_new_hub_legacy_spoke_retains_history_on_detectable_fork(pair):
    local, hub, pricing = pair
    with connect(local) as db:
        upsert_usage_event(db, event())
    first, cursor = export_sync_batch(local)
    first.pop("usageContinuity")
    assert apply_sync_push(hub, pricing, machine_id="laptop", body=first)["usageChangeSeq"] == 1
    with connect(local) as db:
        upsert_usage_event(db, event("second", 50))
    normal, _ = export_sync_batch(local, cursor=cursor)
    normal.pop("usageContinuity")
    assert apply_sync_push(hub, pricing, machine_id="laptop", body=normal)["usageChangeSeq"] == 2
    restored = {
        "usageChanges": {"version": 1, "after": 1, "through": 2, "changes": [
            {"seq": 2, "kind": "usage", "raw_id": "new", "event": asdict(event("new", 2))},
        ]},
    }
    with pytest.raises(ValueError, match="upgrade the spoke"):
        apply_sync_push(hub, pricing, machine_id="laptop", body=restored)
    assert total(hub) == 150


def test_quarantined_legacy_conflict_has_recovery_guidance_after_upgrade():
    from spend_app.freshness import _sync_state

    status, alerts = _sync_state({
        "role": "spoke", "spokeConfig": {
            "enabled": True, "lastSyncAt": STAMP, "usageChangesPending": True,
            "usageLegacyConflicts": 1,
        },
    }, NOW, {"uptimeSeconds": 0})
    assert status["usageLegacyConflicts"] == 1
    assert [alert["key"] for alert in alerts] == ["hub-recovery"]


def test_legacy_hub_delivers_batches_without_pruning_until_its_upgrade(pair):
    local, hub, pricing = pair
    with connect(local) as db:
        for index in range(1001):
            upsert_usage_event(db, event(f"legacy-{index}", 1))

    class LegacyHub:
        acknowledged = 0

        def post(self, _url, *, json, **_kwargs):
            old_payload = {key: value for key, value in json.items() if key != "usageContinuity"}
            if old_payload["usageChanges"]["after"] == self.acknowledged:
                result = apply_sync_push(hub, pricing, machine_id="laptop", body=old_payload, now=NOW)
                self.acknowledged = result["usageChangeSeq"]
            else:
                result = {"ok": True, "eventsWritten": 0, "usageChangeSeq": self.acknowledged}
            return SimpleNamespace(status_code=200, json=lambda: result)

    legacy = LegacyHub()
    assert sync_local(local, pricing, legacy)["usageChangesPending"]
    assert total(hub) == 1001
    assert read_sync_cursor(local, "https://hub.test")["change_seq"] == 0
    with connect(local) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0] == 1001
        upsert_usage_event(db, event("legacy-0", 5))
    # The upgraded hub can verify the already stored rows rather than
    # trusting its old numeric acknowledgement for unconfirmed history.
    upgraded = sync_local(local, pricing, ReceiptHub(pair))
    assert upgraded["usageChangeSeq"] == 1002
    assert not upgraded.get("usageChangesPending")
    assert total(hub) == 1005
    with connect(local) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0] == 0


@pytest.mark.parametrize("legacy_count", (1, 3))
def test_pre_checkpoint_backup_retains_conflicts_and_syncs_fresh_reused_positions(
    pair, tmp_path, legacy_count,
):
    local, hub, pricing = pair
    client = ReceiptHub(pair)
    with connect(local) as db:
        upsert_usage_event(db, event("shared", 20))
        # A 0.6.0 spoke had the usage journal but no immutable checkpoints.
        db.execute("DROP TRIGGER journal_usage_changes_checkpoint")
        db.execute("DROP TABLE usage_change_marks")
        db.execute("DELETE FROM app_meta WHERE key LIKE 'usage_changes.checkpoints.%'")
    restored = backup_database(local, tmp_path / "before-legacy-ack.db")
    initialize(local)
    with connect(local) as db:
        for i in range(1, legacy_count):
            upsert_usage_event(db, event(f"accepted-{i}", 1))
    old_payload, old_cursor = export_sync_batch(local)
    old_payload.pop("usageContinuity")
    apply_sync_push(hub, pricing, machine_id="laptop", body=old_payload, now=NOW)
    # Simulate the numeric ACK persisted by the old spoke before the upgrade.
    write_sync_cursor(local, "https://hub.test", {
        key: value for key, value in old_cursor.items() if key != "usage_stream"
    })
    with connect(local) as db:
        upsert_usage_event(db, event("shared", 60))
    sync_local(local, pricing, client)

    initialize(restored)
    with connect(restored) as db:
        upsert_usage_event(db, event("fork-new", 5))
    for _ in range(2):
        result = sync_local(restored, pricing, client)
        assert result["usageLegacyConflicts"] == 1 and result["usageChangesPending"]
        assert total(hub) == 65 + legacy_count - 1
    with connect(hub) as db:
        conflict = db.execute("SELECT raw_id,change FROM sync_usage_conflicts").fetchone()
        assert conflict["raw_id"] == "shared"
        assert json.loads(conflict["change"])["event"]["input_tokens"] == 20

    with connect(restored) as db:
        upsert_usage_event(db, event("shared", 61))
    assert not sync_local(restored, pricing, client).get("usageChangesPending")
    assert total(hub) == 66 + legacy_count - 1


def test_hub_rechecks_machine_credential_under_acknowledgement_lock(pair):
    from hashlib import sha256

    local, hub, pricing = pair
    spoke = register_machine(hub, name="Laptop")
    machine_id, token = spoke["machine"]["id"], spoke["token"]
    with connect(local) as db:
        upsert_usage_event(db, event())
    payload, _ = export_sync_batch(local)
    new_token = rotate_machine_token(hub, token)["token"]
    with pytest.raises(PermissionError):
        apply_sync_push(hub, pricing, machine_id=machine_id, body=payload,
                        expected_token_hash=sha256(token.encode()).hexdigest())
    assert total(hub) == 0
    result = apply_sync_push(hub, pricing, machine_id=machine_id, body=payload,
                             expected_token_hash=sha256(new_token.encode()).hexdigest())
    assert result["usageChangeSeq"] == 1
    assert total(hub) == 100


def test_renamed_machine_heartbeat_and_reconnect_keep_owner_label(pair):
    _local, hub, pricing = pair
    first = register_machine(hub, name="Office PC", device_id="device-one")["machine"]["id"]
    rename_machine(hub, machine_id=first, name="Studio PC")
    second = register_machine(hub, name="Office PC", device_id="device-two")["machine"]["id"]
    result = apply_sync_push(hub, pricing, machine_id=first, body={
        "machine": {"name": "Office PC"}, "usageEvents": [asdict(event("new", 20))],
    })
    assert result["eventsWritten"] == 1
    assert register_or_reconnect_machine(
        hub, name="Office PC", device_id="device-one",
    )["machine"]["id"] == first
    with connect(hub) as db:
        names = {row["id"]: row["name"] for row in list_machines(db)}
        raw_id, = db.execute("SELECT raw_id FROM usage_events").fetchone()
    assert names[first] == "Studio PC"
    assert names[second] == "Office PC"
    assert raw_id == f"m:{first}:new"


def test_first_upgraded_heartbeat_preserves_evidenced_legacy_rename(pair):
    from spend_app.auth_model import Principal, audit, ensure_schema
    from spend_app.sync import _read_state, _write_state

    _local, hub, pricing = pair
    machine = register_machine(hub, name="Office PC", now=NOW)["machine"]["id"]
    ensure_schema(hub)
    with connect(hub) as db:
        old_state = _read_state(db)
        old_state.pop("owner_names_migrated", None)
        old_state["machines"][machine]["name"] = "Studio PC"
        _write_state(db, old_state)
        audit(db, Principal(kind="owner"), "machine.rename", machine, now=NOW + timedelta(minutes=1))
    apply_sync_push(hub, pricing, machine_id=machine, body={
        "machine": {"name": "Office PC"}, "usageEvents": [asdict(event("new", 20))],
    }, now=NOW + timedelta(minutes=2))
    with connect(hub) as db:
        record = _read_state(db)["machines"][machine]
        assert record["reported_name"] == "Office PC"
        assert record["name"] == record["owner_name"] == "Studio PC"


def test_balance_disconnect_and_reconnect_preserve_other_origins(pair):
    from spend_app.aggregate import _capacity_from_rows

    local, hub, pricing = pair
    provider = {
        "provider_key": "deepseek", "limit_key": "balance",
        "label": "DeepSeek funds remaining", "source": "deepseek_balance_api", "is_payg": True,
    }

    def save_local(polled_at, unit, used):
        with connect(local) as db:
            upsert_quota(db, **provider, polled_at=polled_at, unit=unit, used=used)

    def push_from(origin, rows):
        apply_sync_push(hub, pricing, machine_id=origin, body={"quotas": rows}, now=NOW)

    def displayed_balance():
        with connect(hub) as db:
            rows = [dict(row) for row in db.execute("SELECT * FROM quotas")]
        card = next(
            row for row in _capacity_from_rows(rows, now=NOW, month_to_date={})
            if row["providerKey"] == "deepseek"
        )
        if card["fundsRemainingUsd"] is None:
            assert card["rows"][0]["etaLabel"] == "unavailable"
        return card["fundsRemainingUsd"], rows

    save_local("2026-11-09T10:00:00Z", "usd", 31.25)
    before, _ = export_sync_batch(local)
    push_from("laptop", before["quotas"])
    assert displayed_balance()[0] == 31.25
    save_local("2026-11-09T10:05:00Z", "unavailable", None)
    after, _ = export_sync_batch(local)
    push_from("laptop", after["quotas"])
    push_from("laptop", before["quotas"])
    assert displayed_balance()[0] is None
    push_from("studio", [{**provider, "polled_at": "2026-11-09T10:05:00Z", "used": 51.0, "unit": "usd"}])
    amount, rows = displayed_balance()
    assert amount == 51.0
    assert {row["source"] for row in rows} == {
        "sync:laptop:deepseek_balance_api", "sync:studio:deepseek_balance_api",
    }
    save_local("2026-11-09T10:10:00Z", "usd", 17)
    reconnected, _ = export_sync_batch(local)
    push_from("laptop", reconnected["quotas"])
    assert displayed_balance()[0] == 17.0
