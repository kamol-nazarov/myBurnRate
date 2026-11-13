"""Focused regression coverage for edge cases from the bug fix review."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app.db import connect, initialize
from spend_app.plan_service import apply_mutation, list_plans

NOW = datetime(2026, 11, 13, 12, tzinfo=UTC)
TODAY = NOW.date()


def _values(**changes):
    return {
        "name": "Example plan",
        "tool_key": "codex",
        "amount_usd": "30",
        "cadence": "monthly",
        "start_date": "2026-11-01",
        "end_date": None,
    } | changes


def _mutation(database, operation, request_id, *, today=TODAY, **fields):
    with connect(database) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return apply_mutation(
            connection,
            {"operation": operation, "request_id": request_id, **fields},
            today,
        )


@pytest.fixture
def paired_clients(tmp_path, monkeypatch):
    from spend_app import sync
    from spend_app.api import create_app
    from spend_app.setup_wizard import read_setup_state, write_setup_state
    from tests_spend.test_api import make_settings

    hub = make_settings(tmp_path / "hub.db")
    spoke = make_settings(tmp_path / "spoke.db")
    hub_client = TestClient(create_app(hub, enable_scheduler=False))
    spoke_client = TestClient(create_app(spoke, enable_scheduler=False))
    token = sync.register_machine(hub.database_path, name="Laptop", os_name="win32")[
        "token"
    ]
    with connect(spoke.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "hubUrl": "https://hub.example.invalid",
            "token": token,
            "enabled": True,
        }
        write_setup_state(connection, state)
    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": hub_client}
    )
    sync.clear_hub_proxy_cache()
    yield hub, spoke, hub_client, spoke_client
    sync.clear_hub_proxy_cache()
    hub_client.close()
    spoke_client.close()


def _post(client, body):
    return client.post(
        "/api/subscriptions",
        headers={
            "Origin": "http://127.0.0.1",
            "Host": "127.0.0.1",
            "X-BURNRATE-Request": "1",
        },
        json=body,
    )


def test_retry_after_hub_commit_and_lost_reply_is_idempotent(
    paired_clients, monkeypatch
):
    hub, spoke, hub_client, client = paired_clients
    from spend_app import sync

    real_hub = sync._HUB_TEST_CLIENTS["https://hub.example.invalid"]

    class CommitThenDisconnect:
        def post(self, *args, **kwargs):
            response = real_hub.post(*args, **kwargs)
            assert response.status_code == 200
            raise OSError("reply lost after hub commit")

    monkeypatch.setattr(
        sync,
        "_HUB_TEST_CLIENTS",
        {"https://hub.example.invalid": CommitThenDisconnect()},
    )
    body = {
        "operation": "add",
        "request_id": "retry-after-lost-reply",
        "values": _values(),
    }
    first = _post(client, body)
    assert first.status_code == 503, first.text
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": hub_client}
    )
    second = _post(client, body)
    assert second.status_code == 200, second.text
    assert second.json() == {"id": 1, "version": 1}
    with connect(hub.database_path) as connection:
        assert len(list_plans(connection, NOW.date())["plans"]) == 1


@pytest.mark.parametrize(
    ("status", "payload"),
    [(401, {"error": "invalid hub token"}), (422, {"error": "bad plan"})],
)
def test_spoke_forwards_hub_rejection_without_local_write(
    paired_clients, monkeypatch, status, payload
):
    _, spoke, _, client = paired_clients
    from spend_app import sync

    class Rejected:
        status_code = status

        def json(self):
            return payload

    class RejectingHub:
        def post(self, *args, **kwargs):
            return Rejected()

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": RejectingHub()}
    )
    result = _post(
        client,
        {"operation": "add", "request_id": f"reject-{status}", "values": _values()},
    )
    assert result.status_code == status, result.text
    assert result.json()["error"] == payload["error"]
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []


@pytest.mark.parametrize("payload", [[], {}, {"error": "oops"}])
def test_spoke_rejects_success_body_without_mutation_result(
    paired_clients, monkeypatch, payload
):
    _, spoke, _, client = paired_clients
    from spend_app import sync

    class InvalidSuccess:
        status_code = 200

        def json(self):
            return payload

    class InvalidHub:
        def post(self, *args, **kwargs):
            return InvalidSuccess()

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": InvalidHub()}
    )
    result = _post(
        client,
        {"operation": "add", "request_id": "invalid-success-body", "values": _values()},
    )
    assert result.status_code == 503, result.text
    with connect(spoke.database_path) as connection:
        assert list_plans(connection, NOW.date())["plans"] == []


def test_strict_standalone_fallback_and_legacy_non_strict_behavior(
    tmp_path, monkeypatch
):
    from spend_app import sync
    from spend_app.setup_wizard import read_setup_state, write_setup_state
    from tests_spend.test_api import make_settings

    settings = make_settings(tmp_path / "standalone.db")
    initialize(settings.database_path)
    assert (
        sync.post_to_configured_hub(
            settings, "/api/account/subscriptions", {}, strict=True
        )
        is None
    )
    with connect(settings.database_path) as connection:
        state = read_setup_state(connection)
        state["role"] = "spoke"
        state["spokeConfig"] = {
            "hubUrl": "https://hub.example.invalid",
            "token": "token",
            "enabled": True,
        }
        write_setup_state(connection, state)

    class OfflineHub:
        def post(self, *args, **kwargs):
            raise OSError("offline")

    monkeypatch.setattr(
        sync, "_HUB_TEST_CLIENTS", {"https://hub.example.invalid": OfflineHub()}
    )
    with pytest.raises(sync.HubMutationError):
        sync.post_to_configured_hub(
            settings, "/api/account/subscriptions", {}, strict=True
        )
    assert (
        sync.post_to_configured_hub(settings, "/api/account/subscriptions", {}) is None
    )


def test_usage_cache_revision_is_bounded_and_survives_journal_prune(tmp_path):
    from spend_app.db import (
        UsageEvent,
        prune_usage_changes,
        upsert_usage_event,
        usage_cache_revision,
    )

    database = tmp_path / "cache-revision.db"
    initialize(database)
    start = datetime(2026, 11, 13, 10, tzinfo=UTC)
    end = datetime(2026, 11, 13, 12, tzinfo=UTC)
    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="test",
                tool_key="claude-code",
                model_key="claude-opus-5-5",
                occurred_at="2026-11-13T10:30:00Z",
                session_id=None,
                project=None,
                input_tokens=100,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=10,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0,
                raw_id="corrected-event",
                ingested_at="2026-11-13T11:00:00Z",
            ),
        )
        before_prune = usage_cache_revision(connection, start, end)
        connection.execute(
            "INSERT OR REPLACE INTO app_meta(key,value) VALUES('sync.cursor.v1:hub', ?)",
            ('{"change_seq": 1}',),
        )
        assert prune_usage_changes(connection) == 1
        assert (
            connection.execute("SELECT COUNT(*) FROM usage_changes").fetchone()[0] == 0
        )
        assert usage_cache_revision(connection, start, end) == before_prune


def test_usage_cache_revision_triggers_handle_hour_boundaries_moves_and_deletes(
    tmp_path,
):
    from spend_app.db import UsageEvent, upsert_usage_event, usage_cache_revision

    database = tmp_path / "cache-trigger-boundaries.db"
    initialize(database)
    start = datetime(2026, 11, 13, 12, tzinfo=UTC)
    end = datetime(2026, 11, 13, 14, tzinfo=UTC)

    def upsert(connection, raw_id, occurred_at):
        upsert_usage_event(
            connection,
            UsageEvent(
                source="test",
                tool_key="codex",
                model_key="gpt-5.6-sol",
                occurred_at=occurred_at,
                session_id=None,
                project=None,
                input_tokens=100,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=0,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0,
                raw_id=raw_id,
                ingested_at="2026-11-13T14:30:00Z",
            ),
        )

    with connect(database) as connection:
        # Floor division before the Unix epoch must put 23:30 in hour -1.
        upsert(connection, "pre-epoch", "1969-12-31T23:30:00Z")
        pre_epoch = usage_cache_revision(
            connection,
            datetime(1969, 12, 31, 22, tzinfo=UTC),
            datetime(1970, 1, 1, 0, tzinfo=UTC),
        )
        assert pre_epoch == ((-1, 1),)

        upsert(connection, "moved", "2026-11-13T12:30:00Z")
        initial = usage_cache_revision(connection, start, end)
        assert initial == ((start.timestamp() // 3600, 1),)

        # [start, end) excludes a row whose event time is exactly the end.
        upsert(connection, "at-end", "2026-11-13T14:00:00Z")
        assert usage_cache_revision(connection, start, end) == initial

        # A cross-hour correction touches both source and destination buckets.
        upsert(connection, "moved", "2026-11-13T13:30:00Z")
        moved = usage_cache_revision(connection, start, end)
        assert moved == (
            (start.timestamp() // 3600, 2),
            (end.timestamp() // 3600 - 1, 1),
        )

        connection.execute("DELETE FROM usage_events WHERE raw_id='moved'")
        deleted = usage_cache_revision(connection, start, end)
        assert deleted == (
            (start.timestamp() // 3600, 2),
            (end.timestamp() // 3600 - 1, 2),
        )


def test_window_burn_skips_unrelated_recent_and_empty_sync_but_reads_imported_correction(
    tmp_path, monkeypatch
):
    from spend_app import aggregate, hud_data, sync
    from spend_app.db import (
        UsageEvent,
        initialize,
        upsert_usage_event,
        usage_cache_revision,
    )
    from spend_app.pricing import PricingEngine

    aggregate.reset_memo()
    database = tmp_path / "bounded-hud-cache.db"
    initialize(database)
    now = datetime(2026, 10, 20, 18, 30, tzinfo=UTC)
    reset = now + timedelta(hours=2)
    start = datetime(2026, 10, 20, 15, 30, tzinfo=UTC)
    settled_end = datetime(2026, 10, 20, 17, 0, tzinfo=UTC)
    today_start = datetime(2026, 10, 20, 4, tzinfo=UTC)
    capacity = [
        {
            "providerKey": "claude-code",
            "rows": [{"limitKey": "5h", "resetsAt": reset.isoformat()}],
        }
    ]
    pricing = PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")

    with connect(database) as connection:
        upsert_usage_event(
            connection,
            UsageEvent(
                source="sync:machine-1",
                tool_key="claude-code",
                model_key="claude-opus-5-5",
                occurred_at="2026-10-20T16:00:00Z",
                session_id="machine-1:s",
                project=None,
                input_tokens=100,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=10,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0,
                raw_id="m:machine-1:stable",
                ingested_at="2026-10-20T16:10:00Z",
            ),
        )
    reads = []
    original_burn_rows = hud_data._burn_rows

    def count_burn_rows(connection, tools, row_start, row_end, machine):
        if row_end <= settled_end:
            reads.append((row_start, row_end))
        return original_burn_rows(connection, tools, row_start, row_end, machine)

    monkeypatch.setattr(hud_data, "_burn_rows", count_burn_rows)
    with connect(database) as connection:
        hud_data.window_burn(connection, capacity, now=now, today_start=today_start)
        assert len(reads) == 1

        # A new event in the rolling hour must not invalidate the settled slice.
        upsert_usage_event(
            connection,
            UsageEvent(
                source="test",
                tool_key="claude-code",
                model_key="claude-haiku-5-5",
                occurred_at="2026-10-20T18:00:00Z",
                session_id=None,
                project=None,
                input_tokens=20,
                cached_input_tokens=0,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=0,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0,
                raw_id="recent-unrelated",
                ingested_at="2026-10-20T18:20:00Z",
            ),
        )
        hud_data.window_burn(connection, capacity, now=now, today_start=today_start)
        assert len(reads) == 1

    with connect(database) as connection:
        revision_before_empty = usage_cache_revision(connection, start, settled_end)
    sync.apply_sync_push(
        database,
        pricing,
        machine_id="machine-1",
        body={"usageEvents": [], "unpricedEvents": [], "sessions": [], "quotas": []},
        now=now,
    )
    with connect(database) as connection:
        assert (
            usage_cache_revision(connection, start, settled_end)
            == revision_before_empty
        )
        hud_data.window_burn(connection, capacity, now=now, today_start=today_start)
        assert len(reads) == 1

        # A synced correction changes model attribution with the same event id,
        # time, token totals and ingestion stamp, so it must invalidate its hour.
    sync.apply_sync_push(
        database,
        pricing,
        machine_id="machine-1",
        body={
            "usageEvents": [
                {
                    "raw_id": "stable",
                    "tool_key": "claude-code",
                    "model_key": "claude-haiku-5-5",
                    "occurred_at": "2026-10-20T16:00:00Z",
                    "session_id": "s",
                    "project": None,
                    "input_tokens": 100,
                    "cached_input_tokens": 0,
                    "cache_write_tokens": 0,
                    "cache_write_1h_tokens": 0,
                    "output_tokens": 10,
                    "source": "test",
                    "ingested_at": "2026-10-20T16:10:00Z",
                }
            ],
            "unpricedEvents": [],
            "sessions": [],
            "quotas": [],
        },
        now=now,
    )
    with connect(database) as connection:
        hud_data.window_burn(connection, capacity, now=now, today_start=today_start)
        assert len(reads) == 2


def test_preview_of_nonfinal_term_respects_ended_plan_boundary(tmp_path):
    database = tmp_path / "ended-multiterm-preview.db"
    initialize(database)
    created = _mutation(database, "add", "multi-add", values=_values())
    _mutation(
        database,
        "schedule",
        "multi-schedule",
        plan_id=created["id"],
        expected_version=1,
        effective_date="2026-11-06",
        values={"amount_usd": "60", "cadence": "monthly"},
        today=datetime(2026, 11, 5, tzinfo=UTC).date(),
    )
    _mutation(
        database,
        "end",
        "multi-end",
        plan_id=created["id"],
        expected_version=2,
        end_date="2026-11-10",
    )
    with connect(database) as connection:
        terms = list_plans(connection, NOW.date())["plans"][0]["terms"]
    preview = _mutation(
        database,
        "correct",
        "multi-correct-preview",
        plan_id=created["id"],
        expected_version=3,
        term_id=terms[0]["id"],
        values={
            "name": "Example plan",
            "tool_key": "codex",
            "amount_usd": "90",
            "cadence": "monthly",
            "start_date": "2026-11-01",
            "end_date": "2026-11-05",
        },
        preview=True,
    )
    assert Decimal(preview["before_usd"]) == Decimal(15)
    assert Decimal(preview["after_usd"]) == Decimal(25)
