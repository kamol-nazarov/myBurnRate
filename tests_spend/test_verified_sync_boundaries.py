"""Defensive sync contracts backed by regular accounts and disposable stores."""

from datetime import UTC, datetime, timedelta

import pytest

from spend_app.aggregate import _enrich, _load_cost_buckets
from spend_app.auth_model import Principal
from spend_app.db import (
    ProviderCostBucket, UsageEvent, connect, ensure_sync_storage, initialize,
    upsert_cost_bucket, upsert_quota, upsert_usage_event,
)
from spend_app.machine_data import delete_machine_data
from spend_app.plans_value_store import _stored_resets_by_provider
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push, authenticate_machine_token, register_machine, revoke_machine
from spend_app.sync_continuity import prune_received_journal
from spend_app.sync_validation import MAX_SQLITE_INT, clean_usage_cost, validate_push


def _fixture_event(raw_id, moment):
    return UsageEvent(
        raw_id=raw_id, source="codex_local", tool_key="codex", model_key="gpt-5",
        occurred_at=moment.isoformat(), ingested_at=moment.isoformat(),
        session_id=None, project=None, input_tokens=12, cached_input_tokens=0,
        cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=4, reasoning_tokens=None,
        cost_usd=None, computed_cost_usd=0.01,
    )


def test_sync_body_validates_objects_and_representable_numbers():
    now = datetime.now(UTC)
    validate_push({"machine": {"name": "Example desktop"}, "usageEvents": []}, now)
    for incorrect in ([], {"machine": "desktop"}, {"usageEvents": ["event"]},
                      {"journalPosition": MAX_SQLITE_INT + 1}):
        with pytest.raises(ValueError):
            validate_push(incorrect, now)


def test_push_route_reports_malformed_requests_as_unprocessable(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from spend_app.api import create_app
    from tests_spend.test_api import make_settings

    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    monkeypatch.setenv("BURNRATE_ACCESS_TOKEN", "synthetic-administrator")
    settings = make_settings(tmp_path / "fixture.sqlite3")
    registered = register_machine(settings.database_path, name="Example desktop")
    headers = {"Authorization": f"Bearer {registered['token']}"}
    client = TestClient(create_app(settings, enable_scheduler=False))
    for body in ([], {"machine": "desktop"}, {"journalPosition": MAX_SQLITE_INT + 1}):
        result = client.post("/api/sync/push", json=body, headers=headers)
        assert result.status_code == 422
    good = client.post("/api/sync/push", json={"usageEvents": []}, headers=headers)
    assert good.status_code == 200
    with connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM ingest_runs").fetchone()[0] == 1


def test_sync_received_usage_bounds_preserve_regular_batches():
    from dataclasses import asdict

    now = datetime.now(UTC)
    items = [asdict(_fixture_event(f"fixture-{index}", now)) for index in range(1_000)]
    payload = {"usageEvents": items}
    validate_push(payload, now)
    example = dict(items[0], model_key="m" * 129)
    with pytest.raises(ValueError):
        validate_push({"usageEvents": [example]}, now)


def test_machine_credential_read_path_and_revocation(tmp_path, monkeypatch):
    database = tmp_path / "fixture.sqlite3"
    fixture = register_machine(database, name="Example desktop")
    assert authenticate_machine_token(database, fixture["token"]) == fixture["machine"]["id"]

    def no_write_connection(*_args, **_kwargs):
        pytest.fail("credential check unexpectedly acquired a write connection")

    with monkeypatch.context() as own:
        own.setattr("spend_app.sync.connect", no_write_connection)
        assert authenticate_machine_token(database, fixture["token"]) == fixture["machine"]["id"]
    revoke_machine(database, machine_id=fixture["machine"]["id"])
    monkeypatch.setattr("spend_app.sync.connect", no_write_connection)
    assert authenticate_machine_token(database, fixture["token"]) is None


def test_provider_buckets_keep_first_reporter_and_its_regular_corrections(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    initialize(database)
    now = datetime.now(UTC)
    start = now - timedelta(days=1)
    scope = "a" * 64
    bucket_id = "openai-cost:" + "b" * 64 + ":org:" + scope

    def row(machine, amount, observed):
        return ProviderCostBucket(
            source="openai_admin", starting_at=start.isoformat(), ending_at=now.isoformat(),
            project_id=None, line_item=None, model_key=None, cost_usd=amount,
            raw_id=f"m:{machine}:{bucket_id}", ingested_at=observed.isoformat(),
            organization_scope=scope,
        )

    with connect(database) as conn:
        upsert_cost_bucket(conn, row("desktop-a", 10, start))
        upsert_cost_bucket(conn, row("desktop-b", 11, now))
        current, = _load_cost_buckets(conn, start, now)
        assert (current["cost_usd"], current["conflicting"]) == (10, True)
        upsert_cost_bucket(conn, row("desktop-a", 12, now))
        current, = _load_cost_buckets(conn, start, now)
        assert current["cost_usd"] == 12


def test_quota_reader_prefers_hub_observation_for_same_limit(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    initialize(database)
    now = datetime.now(UTC)
    with connect(database) as conn:
        for source, offset in [("codex_rate_limits", 1), ("sync:desktop:codex_rate_limits", 2)]:
            upsert_quota(
                conn, provider_key="codex", limit_key="weekly", label="Weekly", unit="pct",
                source=source, polled_at=(now + timedelta(minutes=offset)).isoformat(),
                resets_at=(now + timedelta(days=offset)).isoformat(), pct=25,
            )
        assert _stored_resets_by_provider(conn) == {
            "codex": [("weekly", (now + timedelta(days=1)).isoformat())],
        }


def test_synced_quota_poll_clock_is_bounded(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    now = datetime.now(UTC)
    apply_sync_push(
        database, PricingEngine([]), machine_id="desktop", now=now,
        body={"quotas": [{
            "provider_key": "codex", "limit_key": "weekly", "label": "Weekly",
            "unit": "pct", "source": "codex_rate_limits", "pct": 29,
            "polled_at": (now + timedelta(minutes=6)).isoformat(),
        }]},
    )
    with connect(database) as conn:
        polled_at, pct = conn.execute("SELECT polled_at,pct FROM quotas").fetchone()
    assert datetime.fromisoformat(polled_at) == now + timedelta(minutes=5)
    assert pct == 29


def test_legacy_nonfinite_cost_is_unpriced_and_storage_repair_preserves_event(tmp_path):
    from dataclasses import asdict, replace

    database = tmp_path / "fixture.sqlite3"
    initialize(database)
    row = replace(_fixture_event("fixture", datetime.now(UTC)), computed_cost_usd=float("inf"))
    assert clean_usage_cost(asdict(row))["computed_cost_usd"] is None
    enriched, = _enrich([asdict(row)], PricingEngine([]))
    assert enriched["spend"] is None
    assert not enriched["telemetry_complete"]
    with connect(database) as conn:
        upsert_usage_event(conn, row)
        conn.execute("DELETE FROM app_meta WHERE key='sync.safe_storage.v1'")
    ensure_sync_storage(database)
    ensure_sync_storage(database)
    with connect(database) as conn:
        assert conn.execute("SELECT 1 FROM usage_events").fetchone() is None
        assert tuple(conn.execute("SELECT raw_id,cost_usd FROM unpriced_usage_events").fetchone()) == ("fixture", None)


def test_received_journal_prune_removes_both_old_receipts_and_conflicts(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    initialize(database)
    now = int(datetime.now(UTC).timestamp())
    with connect(database) as conn:
        for mark, observed in [(b"expired", now - 100 * 86400), (b"current", now)]:
            conn.execute(
                "INSERT INTO sync_usage_receipts(machine_id,mark,received_at) VALUES(?,?,?)",
                ("desktop", mark, observed),
            )
            conn.execute(
                "INSERT INTO sync_usage_conflicts(machine_id,mark,raw_id,change,received_at) VALUES(?,?,?,?,?)",
                ("desktop", mark, "fixture", "{}", observed),
            )
        prune_received_journal(conn)
        for table in ("sync_usage_receipts", "sync_usage_conflicts"):
            assert [row[0] for row in conn.execute(f"SELECT mark FROM {table}")] == [b"current"]


def test_data_removal_requires_owner_and_revocation_and_retains_other_data(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    first = register_machine(database, name="First computer")["machine"]["id"]
    second = register_machine(database, name="Second computer")["machine"]["id"]
    row_now = datetime.now(UTC)
    with connect(database) as conn:
        for mid in (first, second):
            upsert_usage_event(conn, _fixture_event(f"m:{mid}:fixture", row_now))
    owner = Principal("owner", "owner-test", "owner")
    with pytest.raises(PermissionError):
        delete_machine_data(database, first, actor=Principal("machine", first))
    with pytest.raises(ValueError):
        delete_machine_data(database, first, actor=owner)
    revoke_machine(database, machine_id=first)
    result = delete_machine_data(database, first, actor=owner, create_backup=True)
    backup = tmp_path / "backups" / result["backup"] / "spend.db"
    with connect(backup) as conn:
        assert conn.execute("SELECT count(*) FROM usage_events").fetchone()[0] == 2
    with connect(database) as conn:
        assert [row[0] for row in conn.execute("SELECT raw_id FROM usage_events")] == [f"m:{second}:fixture"]
        assert conn.execute("SELECT action FROM admin_audit WHERE target=?", (first,)).fetchone()[0] == "machine.data.delete"


def test_unavailable_quota_labels_pass_sync_validation_up_to_512_chars(tmp_path):
    database = tmp_path / "fixture.sqlite3"
    now = datetime.now(UTC)
    long_label = (
        "Antigravity Claude/GPT models · Five-hour remaining — "
        "Antigravity is running but its quota lookup failed (RuntimeError). It may still be starting."
    )
    assert len(long_label) > 128
    apply_sync_push(
        database, PricingEngine([]), machine_id="desktop", now=now,
        body={"quotas": [{
            "provider_key": "antigravity", "limit_key": "3p-5h", "label": long_label,
            "unit": "unavailable", "source": "antigravity_local_rpc", "pct": None,
            "polled_at": now.isoformat(),
        }]},
    )
    with connect(database) as conn:
        stored_label = conn.execute("SELECT label FROM quotas").fetchone()[0]
    assert stored_label == long_label
    with pytest.raises(ValueError):
        validate_push({"quotas": [{
            "provider_key": "antigravity", "limit_key": "3p-5h", "label": "x" * 513,
            "unit": "unavailable", "source": "antigravity_local_rpc", "pct": None,
            "polled_at": now.isoformat(),
        }]}, now)
