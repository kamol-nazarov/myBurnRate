from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import httpx
import pytest

from spend_app.adapters.antigravity_local import canonical_model, parse_trajectory
from spend_app.pricing import PricingEngine


ROOT = Path(__file__).resolve().parents[1]


def test_parse_trajectory_keeps_exact_additive_cache_semantics() -> None:
    payload = {
        "trajectory": {
            "cascadeId": "conversation-1",
            "steps": [
                {},
                {},
                {
                    "metadata": {
                        "createdAt": "2026-09-02T17:30:13Z",
                        "completedAt": "2026-09-02T17:30:20Z",
                    }
                },
            ],
            "executorMetadatas": [
                {
                    "executionId": "execution-1",
                    "cascadeConfig": {"plannerConfig": {"modelName": "gemini-3.8-flash-high"}},
                }
            ],
            "generatorMetadata": [
                {
                    "executionId": "execution-1",
                    "stepIndices": [2],
                    "chatModel": {
                        "responseModel": "gemini-3.8-flash",
                        "usage": {
                            "inputTokens": "4068",
                            "cacheReadTokens": "16428",
                            "outputTokens": "131",
                            "thinkingOutputTokens": "56",
                            "responseId": "response-1",
                        },
                    },
                }
            ],
        }
    }
    rows = parse_trajectory(
        payload,
        summary={
            "lastModifiedTime": "2026-09-02T17:31:00Z",
            "workspaces": [{"workspaceFolderAbsoluteUri": "file:///C:/Dev/ExampleProject"}],
        },
    )

    assert len(rows) == 1
    row = rows[0]
    assert row.source == "antigravity_local"
    assert row.tool_key == "antigravity"
    assert row.model_key == "antigravity:gemini-3.8-flash-high"
    assert row.occurred_at == datetime(2026, 9, 2, 17, 30, 20, tzinfo=UTC)
    assert row.session_id == "conversation-1"
    assert row.project == "ExampleProject"
    assert row.input_tokens == 4068 + 16428
    assert row.cached_input_tokens == 16428
    assert row.output_tokens == 131
    assert row.reasoning_tokens == 56
    assert row.cache_write_tokens == 0


def test_parse_trajectory_skips_incomplete_generator_and_uses_response_model() -> None:
    payload = {
        "trajectory": {
            "cascadeId": "conversation-2",
            "steps": [],
            "generatorMetadata": [
                {"chatModel": {"usage": {}}},
                {
                    "chatModel": {
                        "responseModel": "claude-sonnet-4-6",
                        "usage": {"inputTokens": "10", "outputTokens": "2", "messageId": "m-2"},
                    }
                },
            ],
        }
    }
    rows = parse_trajectory(payload, summary={"lastModifiedTime": "2026-09-02T18:00:00Z"})
    assert len(rows) == 1
    assert rows[0].model_key == "antigravity:claude-sonnet-4-6"
    assert rows[0].input_tokens == 10
    assert rows[0].cached_input_tokens == 0


def test_canonical_model_never_cross_labels_another_harness() -> None:
    assert canonical_model("gemini-3.8-flash") == "antigravity:gemini-3.8-flash"


def test_gemini_38_flash_published_rate_uses_distinct_cache_price() -> None:
    engine = PricingEngine.load(ROOT / "pricing")
    cost = engine.compute(
        model_key="antigravity:gemini-3.8-flash-high",
        occurred_at=datetime(2026, 9, 2, 17, 30, tzinfo=UTC),
        input_tokens=4068 + 16428,
        cached_input_tokens=16428,
        cache_write_tokens=0,
        output_tokens=131,
    )
    expected = (
        Decimal(4068) * Decimal("0.75")
        + Decimal(16428) * Decimal("0.075")
        + Decimal(131) * Decimal("3.75")
    ) / Decimal(1_000_000)
    assert cost == expected


def _mock_history(monkeypatch, count):
    """Synthetic local RPC history; no process discovery or provider access."""
    from contextlib import nullcontext
    from spend_app.adapters import antigravity_local as adapter

    adapter.reset_state()
    monkeypatch.setattr(adapter, "_TRAJECTORY_SIGNATURES", {})
    monkeypatch.setattr(adapter, "_antigravity_local_connection", lambda: ("https://127.0.0.1:1234", "fixture"))
    monkeypatch.setattr(adapter.httpx, "Client", lambda **kwargs: nullcontext(object()))
    clock = [0.0]
    monkeypatch.setattr("time.monotonic", lambda: clock[0])
    summaries = {str(i): {"trajectoryId": str(i), "lastModifiedTime": "2026-11-01T12:00:00Z", "stepCount": 1} for i in range(count)}
    fetched = []

    def rpc(client, *, method, payload, **kwargs):
        if method == "GetAllCascadeTrajectories":
            return {"trajectorySummaries": summaries}
        sid = payload["cascadeId"]
        fetched.append(sid)
        return {"trajectory": {"cascadeId": sid, "generatorMetadata": [{"chatModel": {
            "responseModel": "gemini-3.8-flash-high", "usage": {
                "inputTokens": 100, "cacheReadTokens": 200, "outputTokens": 10, "responseId": f"response-{sid}",
            },
        }}]}}

    monkeypatch.setattr(adapter, "_rpc_json", rpc)
    return adapter, fetched, clock, rpc


def _saved_history(database):
    from spend_app.db import connect
    with connect(database) as connection:
        return tuple(connection.execute("SELECT COUNT(*), SUM(input_tokens), SUM(output_tokens) FROM usage_events").fetchone())


def test_history_larger_than_one_batch_resumes_without_losing_or_duplicating_usage(tmp_path, monkeypatch):
    adapter, fetched, _, _ = _mock_history(monkeypatch, 101)
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")

    first = adapter.ingest(database_path=database, pricing=pricing)
    assert first["status"] == "partial"
    assert first["trajectoriesFetched"] == 100
    assert _saved_history(database) == (100, 30_000, 1_000)
    second = adapter.ingest(database_path=database, pricing=pricing)
    assert second["status"] == "success"
    assert second["trajectoriesFetched"] == 1
    assert fetched == [str(i) for i in range(101)]
    assert _saved_history(database) == (101, 30_300, 1_010)

    # Restarting drops the optimization cache, but stable response ids still
    # keep persisted requests and their cost from being counted twice.
    adapter.reset_state()
    adapter.ingest(database_path=database, pricing=pricing)
    adapter.ingest(database_path=database, pricing=pricing)
    assert _saved_history(database) == (101, 30_300, 1_010)


def test_reporting_deadline_preserves_progress_and_resumes_next_pass(tmp_path, monkeypatch):
    adapter, fetched, clock, rpc = _mock_history(monkeypatch, 2)
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")

    def slow_rpc(*args, **kwargs):
        result = rpc(*args, **kwargs)
        if kwargs["method"] == "GetCascadeTrajectory":
            clock[0] += adapter.REPORTING_TIMEOUT_SECONDS
        return result

    monkeypatch.setattr(adapter, "_rpc_json", slow_rpc)
    first = adapter.ingest(database_path=database, pricing=pricing)
    assert first["status"] == "partial"
    assert _saved_history(database) == (1, 300, 10)
    second = adapter.ingest(database_path=database, pricing=pricing)
    assert second["status"] == "success"
    assert fetched == ["0", "1"]
    assert _saved_history(database) == (2, 600, 20)


def test_rpc_failure_keeps_completed_histories_and_retries_the_failed_one(tmp_path, monkeypatch):
    adapter, fetched, _, rpc = _mock_history(monkeypatch, 2)
    database = tmp_path / "spend.db"
    pricing = PricingEngine.load(ROOT / "pricing")

    def failing_rpc(*args, **kwargs):
        if kwargs["method"] == "GetCascadeTrajectory" and kwargs["payload"]["cascadeId"] == "1":
            raise httpx.ReadTimeout("synthetic timeout")
        return rpc(*args, **kwargs)

    monkeypatch.setattr(adapter, "_rpc_json", failing_rpc)
    first = adapter.ingest(database_path=database, pricing=pricing)
    assert first["status"] == "partial"
    assert _saved_history(database) == (1, 300, 10)
    monkeypatch.setattr(adapter, "_rpc_json", rpc)
    assert adapter.ingest(database_path=database, pricing=pricing)["status"] == "success"
    assert fetched == ["0", "1"]
    assert _saved_history(database) == (2, 600, 20)


def test_failed_persistence_does_not_advance_history_cache(tmp_path, monkeypatch):
    adapter, fetched, _, _ = _mock_history(monkeypatch, 1)
    persist = adapter.persist_rows

    def fail(**kwargs):
        raise OSError("synthetic database error")

    monkeypatch.setattr(adapter, "persist_rows", fail)
    with pytest.raises(OSError, match="synthetic database error"):
        adapter.ingest(database_path=tmp_path / "spend.db", pricing=None)
    assert not adapter._TRAJECTORY_SIGNATURES
    monkeypatch.setattr(adapter, "persist_rows", persist)
    result = adapter.ingest(database_path=tmp_path / "spend.db", pricing=PricingEngine.load(ROOT / "pricing"))
    assert result["status"] == "success"
    assert fetched == ["0", "0"]
