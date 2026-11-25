import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app.adapters.claude_local import ingest, parse_file, reset_file_cache
from spend_app.db import connect
from spend_app.limits import _claude_active_sessions
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def _assistant(*, request_id=None, second=2, model="claude-opus-5", usage=None):
    record = {
        "type": "assistant", "timestamp": f"2026-09-01T00:00:{second:02d}Z",
        "sessionId": "session", "uuid": "envelope",
        "message": {
            "id": "message", "model": model,
            "usage": usage if usage is not None else {
                "input_tokens": 1_000, "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0, "output_tokens": 100,
            },
        },
    }
    if request_id is not None:
        record["requestId"] = request_id
    return record


def _ingest_assistants(path, database, pricing, *records):
    path.write_text("".join(json.dumps(record) + "\n" for record in records), encoding="utf-8")
    return ingest(database_path=database, pricing=pricing, session_glob=str(path))


def _claude_rows(database, lane="usage_events"):
    with connect(database) as connection:
        return [dict(row) for row in connection.execute(
            "SELECT * FROM " + lane + " WHERE source='claude_local' ORDER BY id"
        )]


def _session_model(database, sid="session"):
    with connect(database) as connection:
        return connection.execute("SELECT model_key FROM sessions WHERE session_id=?", (sid,)).fetchone()[0]


@pytest.mark.parametrize("reverse", [False, True])
def test_request_id_enrichment_keeps_one_charge_through_restart(tmp_path, reverse):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    legacy, explicit = _assistant(), _assistant(request_id="explicit")
    first, second = (explicit, legacy) if reverse else (legacy, explicit)
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, first)
    first_id = _claude_rows(database)[0]["raw_id"]
    reset_file_cache()
    _ingest_assistants(tmp_path / "second.jsonl", database, pricing, second)
    reset_file_cache()
    _ingest_assistants(tmp_path / "replay.jsonl", database, pricing, legacy, explicit)
    row, = _claude_rows(database)
    assert row["raw_id"] == first_id
    assert (row["input_tokens"], row["output_tokens"]) == (1_000, 100)
    assert row["computed_cost_usd"] == pytest.approx(0.0075)
    assert _claude_rows(database, "unpriced_usage_events") == []

    # An explicit request remains independent even when the message ID and
    # envelope UUID were reused by its producer.
    _ingest_assistants(tmp_path / "other.jsonl", database, pricing, _assistant(request_id="another"))
    assert len(_claude_rows(database)) == 2


def test_request_id_enrichment_moves_unpriced_event_on_same_identity(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    _ingest_assistants(tmp_path / "legacy.jsonl", database, pricing,
                       _assistant(model="", usage={"input_tokens": 1_000, "output_tokens": 100}))
    unpriced, = _claude_rows(database, "unpriced_usage_events")
    _ingest_assistants(tmp_path / "explicit.jsonl", database, pricing,
                       _assistant(request_id="explicit", second=3))
    priced, = _claude_rows(database)
    assert priced["raw_id"] == unpriced["raw_id"]
    assert priced["computed_cost_usd"] == pytest.approx(0.0075)
    assert _claude_rows(database, "unpriced_usage_events") == []


def test_envelope_proof_is_required_for_request_id_link(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    unrelated = {**_assistant(request_id="explicit"), "uuid": "different-envelope"}
    _ingest_assistants(tmp_path / "legacy.jsonl", database, pricing, _assistant())
    _ingest_assistants(tmp_path / "explicit.jsonl", database, pricing, unrelated)
    assert len(_claude_rows(database)) == 2


@pytest.mark.parametrize("reverse", [False, True])
@pytest.mark.parametrize("provider_on_explicit", [False, True])
def test_request_enrichment_uses_single_reported_provider(tmp_path, reverse, provider_on_explicit):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    fallback, explicit = _assistant(), _assistant(request_id="request")
    (explicit if provider_on_explicit else fallback)["providerId"] = "local-proxy"
    first, second = (explicit, fallback) if reverse else (fallback, explicit)
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, first)
    initial, = _claude_rows(database)
    reset_file_cache()
    _ingest_assistants(tmp_path / "second.jsonl", database, pricing, second)
    result, = _claude_rows(database)
    assert result["raw_id"] == initial["raw_id"]
    assert result["computed_cost_usd"] == pytest.approx(0.0075)


def test_request_enrichment_does_not_merge_distinct_reported_providers(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    fallback = {**_assistant(), "providerId": "first-proxy"}
    explicit = {**_assistant(request_id="request"), "providerId": "second-proxy"}
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, fallback)
    _ingest_assistants(tmp_path / "second.jsonl", database, pricing, explicit)
    assert len(_claude_rows(database)) == 2


def test_saved_fallback_cannot_revise_request_after_envelope_becomes_ambiguous(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    _ingest_assistants(tmp_path / "fallback.jsonl", database, pricing, _assistant())
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, _assistant(request_id="first"))
    _ingest_assistants(tmp_path / "second.jsonl", database, pricing, _assistant(request_id="second"))
    accepted = _claude_rows(database)
    assert len(accepted) == 2
    reset_file_cache()
    changed = _assistant(second=3, usage={"input_tokens": 9_000})
    result = _ingest_assistants(tmp_path / "changed.jsonl", database, pricing, changed)
    assert _claude_rows(database) == accepted
    assert "claude_envelope_request_ambiguity" in result["issues"]
    del changed["uuid"]
    _ingest_assistants(tmp_path / "without-envelope.jsonl", database, pricing, changed)
    assert _claude_rows(database) == accepted


def test_request_id_cannot_claim_a_fallback_shared_by_distinct_envelopes(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    first, other = _assistant(), {**_assistant(), "uuid": "second-envelope"}
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, first)
    _ingest_assistants(tmp_path / "second.jsonl", database, pricing, other)
    explicit = _assistant(request_id="explicit")
    result = _ingest_assistants(tmp_path / "known.jsonl", database, pricing, explicit)
    assert len(_claude_rows(database)) == 2
    assert "claude_envelope_request_ambiguity" in result["issues"]


def test_replaying_old_duplicate_charges_journals_correction_to_hub(tmp_path, monkeypatch):
    from spend_app.adapters import claude_local, event_identity
    from spend_app.db import initialize
    from spend_app.sync import apply_sync_push, export_sync_batch

    database, hub = tmp_path / "spoke.db", tmp_path / "hub.db"
    initialize(hub)
    pricing = PricingEngine.load(ROOT / "pricing")
    fixture = tmp_path / "duplicate.jsonl"
    legacy, explicit = _assistant(), _assistant(request_id="explicit")
    # The importer stamps ingested_at with the process clock.
    now = max(datetime.now(UTC), datetime(2026, 9, 2, tzinfo=UTC))
    # Simulate the pre-repair parser's persisted usage rows and file cache,
    # while leaving both underlying transcript records available for replay.
    with monkeypatch.context() as old:
        old.setattr(claude_local, "PARSER_VERSION", "claude-message-3")
        old.setattr(event_identity, "_link_claude_requests", lambda _, observations, issues: (observations, {}))
        _ingest_assistants(fixture, database, pricing, legacy, explicit)
    assert len(_claude_rows(database)) == 2
    initial, cursor = export_sync_batch(database)
    apply_sync_push(hub, pricing, machine_id="claude-spoke", body=initial, now=now)
    assert len(_claude_rows(hub)) == 2

    repair = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert repair["files"] == 1  # The parser-version bump invalidated its old file cache.
    corrected, _ = export_sync_batch(database, cursor=cursor)
    assert len(_claude_rows(database)) == 1
    assert any(change["kind"] == "delete" for change in corrected["usageChanges"]["changes"])
    for _ in range(2):
        apply_sync_push(hub, pricing, machine_id="claude-spoke", body=corrected, now=now)
    row, = _claude_rows(hub)
    assert row["computed_cost_usd"] == pytest.approx(0.0075)


def test_older_model_copy_cannot_reprice_newer_observation(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    usage = {"input_tokens": 1_000_000, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 100}
    newer = _assistant(request_id="explicit", usage=usage)
    older = _assistant(request_id="explicit", second=1, model="claude-sonnet-5", usage=usage)
    _ingest_assistants(tmp_path / "newer.jsonl", database, pricing, newer)
    reset_file_cache()
    for path in ("older.jsonl", "replay.jsonl"):
        _ingest_assistants(tmp_path / path, database, pricing, older)
        row, = _claude_rows(database)
        assert row["model_key"] == "claude-opus-5"
        assert row["computed_cost_usd"] == pytest.approx(5.0025)
        assert _session_model(database) == "claude-opus-5"
    corrected = _assistant(request_id="explicit", second=3, model="claude-sonnet-5", usage=usage)
    _ingest_assistants(tmp_path / "new-authority.jsonl", database, pricing, corrected)
    row, = _claude_rows(database)
    assert row["model_key"] == "claude-sonnet-5"
    assert row["computed_cost_usd"] == pytest.approx(2.001)
    assert _session_model(database) == "claude-sonnet-5"


def test_legacy_model_metadata_requires_current_evidence_to_reprice(tmp_path):
    from spend_app.adapters.common import stable_id
    from spend_app.adapters.usage_repair import read_meta, write_meta

    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    _ingest_assistants(tmp_path / "initial.jsonl", database, pricing, _assistant(request_id="explicit"))
    raw_id = _claude_rows(database)[0]["raw_id"]
    with connect(database) as connection:
        key = "usage.identity.v1:" + stable_id("claude_local", raw_id)
        identity = read_meta(connection, key, None)
        del identity["components"]["model"]
        del identity["field_ranks"]["model"]
        write_meta(connection, key, identity)
    _ingest_assistants(tmp_path / "older.jsonl", database, pricing,
                       _assistant(request_id="explicit", second=1, model="claude-sonnet-5"))
    assert _claude_rows(database)[0]["model_key"] == "claude-opus-5"
    # Replay of the saved revision establishes an independently ranked model.
    _ingest_assistants(tmp_path / "current.jsonl", database, pricing, _assistant(request_id="explicit"))
    with connect(database) as connection:
        assert read_meta(connection, key, None)["components"]["model"] == "claude-opus-5"


def test_equal_authority_model_conflict_is_unpriced_until_correction(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, _assistant(request_id="explicit"))
    conflict = _assistant(request_id="explicit", model="claude-sonnet-5")
    _ingest_assistants(tmp_path / "conflicting.jsonl", database, pricing, conflict)
    assert _claude_rows(database) == []
    unpriced, = _claude_rows(database, "unpriced_usage_events")
    assert unpriced["model_key"] == "unknown"
    assert _session_model(database) == "unknown"
    assert (unpriced["input_tokens"], unpriced["output_tokens"]) == (1_000, 100)
    conflict["message"]["stop_reason"] = "end_turn"
    _ingest_assistants(tmp_path / "authoritative.jsonl", database, pricing, conflict)
    assert _claude_rows(database)[0]["model_key"] == "claude-sonnet-5"
    assert _session_model(database) == "claude-sonnet-5"
    assert _claude_rows(database, "unpriced_usage_events") == []


@pytest.mark.parametrize(("five_minutes", "one_hour", "priced_hour", "cost"), [
    (100_000, 900_000, 900_000, 9.625),
    (100_000, None, 900_000, 9.625),
    (1_000_000, None, 0, 6.25),
    (100_000, 200_000, None, None),
    (100_000, -1, None, None),
    (None, None, None, None),
])
def test_cache_duration_must_support_exact_charge(tmp_path, five_minutes, one_hour, priced_hour, cost):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    parts = (("ephemeral_5m_input_tokens", five_minutes), ("ephemeral_1h_input_tokens", one_hour))
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1_000_000,
             "cache_creation": {name: value for name, value in parts if value is not None}}
    _ingest_assistants(tmp_path / "cache.jsonl", database, pricing, _assistant(usage=usage))
    if cost is None:
        assert _claude_rows(database) == []
        row, = _claude_rows(database, "unpriced_usage_events")
        assert not row["telemetry_complete"]
    else:
        row, = _claude_rows(database)
        assert row["cache_write_1h_tokens"] == priced_hour
        assert row["computed_cost_usd"] == pytest.approx(cost)
        assert row["is_exact"]
    assert row["cache_write_tokens"] == 1_000_000


def test_corrected_total_rederives_one_hour_without_inventing_observation(tmp_path):
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    usage = {"input_tokens": 0, "cache_read_input_tokens": 0, "output_tokens": 0,
             "cache_creation_input_tokens": 1_000_000,
             "cache_creation": {"ephemeral_5m_input_tokens": 100_000}}
    _ingest_assistants(tmp_path / "first.jsonl", database, pricing, _assistant(usage=usage))
    _ingest_assistants(tmp_path / "corrected.jsonl", database, pricing,
                       _assistant(second=3, usage={"cache_creation_input_tokens": 800_000}))
    row, = _claude_rows(database)
    assert row["cache_write_tokens"] == 800_000
    assert row["cache_write_1h_tokens"] == 700_000
    assert row["computed_cost_usd"] == pytest.approx(7.625)


def write_fixture(path: Path) -> None:
    message = {
        "id": "msg_fixture",
        "model": "claude-opus-5",
        "content": [{"type": "text", "text": "SECRET_PROMPT_DO_NOT_STORE reply"}],
        "output_tokens": 100,
        "usage": {
            "input_tokens": 2,
            "cache_read_input_tokens": 1000,
            "cache_creation_input_tokens": 500,
            "output_tokens": 100,
            "output_tokens_details": {"thinking_tokens": 20},
            "cache_creation": {
                "ephemeral_1h_input_tokens": 500,
                "ephemeral_5m_input_tokens": 0,
            },
        },
    }
    rows = [
        {
            "type": "assistant",
            "timestamp": "2026-08-30T20:00:00Z",
            "sessionId": "claude-session",
            "cwd": r"C:\Dev\ExampleProject",
            "uuid": "outer-1",
            "message": message,
        },
        {
            "type": "assistant",
            "timestamp": "2026-08-30T20:00:01Z",
            "sessionId": "claude-session",
            "cwd": r"C:\Dev\ExampleProject",
            "uuid": "outer-2",
            "message": message,
        },
    ]
    path.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")


def test_parser_deduplicates_repeated_assistant_snapshots(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    write_fixture(fixture)
    session, events = parse_file(fixture)
    assert session["id"] == "claude-session"
    assert session["project"] == "ExampleProject"
    assert session["model_key"] == "claude-opus-5"
    assert len(events) == 1
    assert events[0].input_tokens == 1002
    assert events[0].cache_write_tokens == 500
    assert events[0].cache_write_1h_tokens == 500
    assert events[0].raw_id == "claude-message:8b80abb61333615fbb559a883ba765bb63ba0b03b98abe596737285f4b371901"


def test_ingest_prices_one_hour_writes_and_is_idempotent(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    pricing = PricingEngine.load(ROOT / "pricing")
    first = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    second = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert first["eventsWritten"] == 1
    assert first["duplicatesRemoved"] == 1
    assert second["eventsWritten"] == 0
    assert second["filesSkippedUnchanged"] == 1
    with connect(database) as connection:
        event_count = connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0]
        session_count = connection.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        row = connection.execute(
            """
            SELECT cache_write_1h_tokens, computed_cost_usd, is_exact, project,
                   session_id, model_key, raw_id
            FROM usage_events
            """
        ).fetchone()
        session = connection.execute(
            "SELECT session_id, project, model_key, tool_key FROM sessions"
        ).fetchone()
    assert event_count == 1
    assert session_count == 1
    assert row[0] == 500
    assert round(row[1], 6) == 0.00801
    assert tuple(row[2:]) == (
        1,
        "ExampleProject",
        "claude-session",
        "claude-opus-5",
        "claude-message:8b80abb61333615fbb559a883ba765bb63ba0b03b98abe596737285f4b371901",
    )
    assert tuple(session) == ("claude-session", "ExampleProject", "claude-opus-5", "claude-code")


def test_unpriced_model_is_recorded_and_run_fails_loudly(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    message = {
        "id": "msg_unpriced",
        "model": "unpriced-claude",
        "usage": {
            "input_tokens": 10,
            "cache_read_input_tokens": 0,
            "cache_creation_input_tokens": 0,
            "output_tokens": 5,
        },
    }
    fixture.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-30T20:00:00Z",
                "sessionId": "claude-unpriced",
                "cwd": r"C:\Dev\ExampleProject",
                "uuid": "outer-unpriced",
                "message": message,
            }
        )
        + "\n",
        encoding="utf-8",
    )
    result = ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(fixture),
    )
    assert result["status"] == "partial"
    assert result["eventsWritten"] == 0
    assert result["unpricedModels"] == ["unpriced-claude"]
    with connect(database) as connection:
        gap = connection.execute("SELECT model_key, occurrences FROM pricing_gaps").fetchone()
        unpriced = connection.execute(
            "SELECT COUNT(*) FROM unpriced_usage_events"
        ).fetchone()[0]
    assert tuple(gap) == ("unpriced-claude", 1)
    assert unpriced == 1


def _stored_text(database: Path) -> str:
    chunks: list[str] = []
    with connect(database) as connection:
        tables = [
            row[0]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")
        ]
        for table in tables:
            for row in connection.execute(f"SELECT * FROM {table}"):
                chunks.extend(str(value) for value in row if value is not None)
    return "\n".join(chunks)


def test_replay_skips_duplicate_raw_ids_when_file_cache_misses(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_file_cache()
    first = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    reset_file_cache()
    second = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert first["eventsWritten"] == 1
    assert second["eventsWritten"] == 0
    with connect(database) as connection:
        raw_ids = [row[0] for row in connection.execute("SELECT raw_id FROM usage_events")]
    assert raw_ids == ["claude-message:8b80abb61333615fbb559a883ba765bb63ba0b03b98abe596737285f4b371901"]


def test_omitted_thinking_tokens_stay_null(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    fixture.write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-08-30T20:00:00Z",
                "sessionId": "claude-partial",
                "cwd": r"C:\Dev\ExampleProject",
                "uuid": "outer-partial",
                "message": {
                    "id": "msg_partial",
                    "model": "claude-opus-5",
                    "usage": {
                        "input_tokens": 10,
                        "cache_read_input_tokens": 0,
                        "cache_creation_input_tokens": 0,
                        "output_tokens": 5,
                    },
                },
            }
        )
        + "\n",
        encoding="utf-8",
    )
    _session, events = parse_file(fixture)
    assert len(events) == 1
    assert events[0].reasoning_tokens is None
    assert events[0].input_tokens == 10
    assert events[0].output_tokens == 5


def test_prompt_text_is_not_persisted(tmp_path: Path) -> None:
    fixture = tmp_path / "claude.jsonl"
    database = tmp_path / "spend.db"
    write_fixture(fixture)
    reset_file_cache()
    ingest(
        database_path=database,
        pricing=PricingEngine.load(ROOT / "pricing"),
        session_glob=str(fixture),
    )
    assert "SECRET_PROMPT_DO_NOT_STORE" in fixture.read_text(encoding="utf-8")
    assert "SECRET_PROMPT_DO_NOT_STORE" not in _stored_text(database)
    assert "C:\\Dev\\ExampleProject" not in _stored_text(database)


def test_idle_terminal_or_expired_claude_session_is_not_live(tmp_path: Path) -> None:
    now = datetime(2026, 9, 4, 12, tzinfo=UTC)
    project = tmp_path / "project"
    project.mkdir()

    def write_session(name: str, events: list[dict], modified_at: float) -> None:
        path = project / f"{name}.jsonl"
        path.write_text("\n".join(json.dumps(event) for event in events) + "\n", encoding="utf-8")
        os.utime(path, (modified_at, modified_at))

    write_session(
        "live-session",
        [
            {"type": "custom-title", "customTitle": "Live Claude task", "sessionId": "live-session"},
            {"type": "user", "timestamp": "2026-09-04T11:59:00Z", "sessionId": "live-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:55Z",
                "sessionId": "live-session",
                "message": {"model": "claude-opus-5", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "complete-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:59:00Z", "sessionId": "complete-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:59:30Z",
                "sessionId": "complete-session",
                "message": {"model": "claude-opus-5", "stop_reason": "end_turn"},
            },
        ],
        now.timestamp(),
    )
    write_session(
        "idle-editor",
        [{"type": "custom-title", "customTitle": "Idle editor", "sessionId": "idle-editor"}],
        now.timestamp(),
    )
    write_session(
        "stale-session",
        [
            {"type": "user", "timestamp": "2026-09-04T11:48:00Z", "sessionId": "stale-session"},
            {
                "type": "assistant",
                "timestamp": "2026-09-04T11:48:30Z",
                "sessionId": "stale-session",
                "message": {"model": "claude-opus-5", "stop_reason": "tool_use"},
            },
        ],
        now.timestamp() - 11 * 60,
    )
    sessions = _claude_active_sessions(tmp_path, now=now)
    assert [row["sessionId"] for row in sessions] == ["live-session"]


def test_unchanged_file_with_quarantined_records_is_not_reparsed(tmp_path: Path, monkeypatch) -> None:
    # Regression: a transcript with a permanently unusable record (here, an
    # invalid JSON line) was never cached, so every pass re-read and
    # re-reconciled it. Large idle transcripts like that made the Claude pass
    # take ~20 seconds. The cached pass must still report the file as partial.
    from spend_app.adapters import claude_local

    fixture = tmp_path / "claude.jsonl"
    write_fixture(fixture)
    with fixture.open("a", encoding="utf-8") as stream:
        stream.write("{not json\n")
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    reset_file_cache()
    first = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    calls = []
    original = claude_local.parse_file_with_health
    monkeypatch.setattr(claude_local, "parse_file_with_health", lambda path: calls.append(path) or original(path))
    second = ingest(database_path=database, pricing=pricing, session_glob=str(fixture))
    assert calls == []
    assert second["filesSkippedUnchanged"] == 1
    assert "claude_partial_source_metadata" in first["issues"]
    assert "claude_partial_source_metadata" in second["issues"]
    assert second["quarantined"] == first["quarantined"] >= 1
    assert second["status"] == first["status"] == "partial"
    reset_file_cache()


def _lane_ingest(tmp_path, database, pricing):
    return ingest(database_path=database, pricing=pricing, session_glob=str(tmp_path / "**" / "*.jsonl"))


def test_default_run_tags_attributed_sessions_with_their_lane(tmp_path, monkeypatch):
    from spend_app import claude_session_lanes

    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    top = tmp_path / "session.jsonl"
    sub = tmp_path / "session" / "subagents" / "agent-1.jsonl"
    other = tmp_path / "other.jsonl"
    for path, request, sid in ((top, "r1", "session"), (sub, "r2", "session"), (other, "r3", "other")):
        path.parent.mkdir(parents=True, exist_ok=True)
        record = _assistant(request_id=request)
        record["sessionId"] = sid
        record["uuid"] = f"envelope-{request}"
        record["message"]["id"] = f"message-{request}"
        path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    monkeypatch.setattr(claude_session_lanes, "session_lanes", lambda _db: {"session": "claude-code-work"})
    reset_file_cache()
    result = _lane_ingest(tmp_path, database, pricing)
    assert result["files"] == 3
    with connect(database) as connection:
        events = {r["session_id"]: r["tool_key"] for r in connection.execute("SELECT * FROM usage_events")}
        sessions = {r["session_id"]: r["tool_key"] for r in connection.execute("SELECT * FROM sessions")}
        remembered = claude_session_lanes.read_remembered(connection)
    assert events == {"session": "claude-code-work", "other": "claude-code"}
    assert sessions == events
    assert remembered == {"session": "claude-code-work"}  # parent id, shared by the subagent transcript
    assert sum(r["tool_key"] == "claude-code-work" for r in _claude_rows(database)) == 2

    # Unchanged files stay cached under their lane's identity.
    assert _lane_ingest(tmp_path, database, pricing)["filesSkippedUnchanged"] == 3
    reset_file_cache()


def test_file_that_moves_lanes_is_reparsed_but_other_files_keep_their_cache(tmp_path, monkeypatch):
    from spend_app import claude_session_lanes
    from spend_app.adapters import claude_local

    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")
    path = tmp_path / "session.jsonl"
    stay = tmp_path / "stay.jsonl"
    for target, sid in ((path, "session"), (stay, "stay")):
        record = _assistant(request_id=sid)
        record["sessionId"], record["uuid"] = sid, f"envelope-{sid}"
        record["message"]["id"] = f"message-{sid}"
        target.write_text(json.dumps(record) + "\n", encoding="utf-8")
    lanes = {}
    monkeypatch.setattr(claude_session_lanes, "session_lanes", lambda _db: lanes)
    reset_file_cache()
    _lane_ingest(tmp_path, database, pricing)
    before = {row["raw_id"] for row in _claude_rows(database)}
    lanes["session"] = "claude-code-work"
    calls = []
    original = claude_local.parse_file_with_health
    monkeypatch.setattr(claude_local, "parse_file_with_health", lambda p: calls.append(Path(p).name) or original(p))
    result = _lane_ingest(tmp_path, database, pricing)
    assert calls == ["session.jsonl"]
    assert result["filesSkippedUnchanged"] == 1
    rows = _claude_rows(database)
    assert {row["raw_id"] for row in rows} == before
    assert {row["session_id"]: row["tool_key"] for row in rows} == {"session": "claude-code-work", "stay": "claude-code"}
    reset_file_cache()
