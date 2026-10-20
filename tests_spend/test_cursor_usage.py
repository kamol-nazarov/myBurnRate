from __future__ import annotations

from datetime import UTC, datetime

from spend_app.adapters.cursor_usage import parse_events


def test_cursor_usage_service_preserves_additive_cache_and_provider_cost() -> None:
    rows = parse_events(
        [
            {
                "timestamp": str(int(datetime(2026, 9, 2, 19, 38, tzinfo=UTC).timestamp() * 1000)),
                "model": "gemini-3.8-flash-high",
                "kind": "composer",
                "conversationId": "conversation-1",
                "chargedCents": 7.130258,
                "tokenUsage": {
                    "inputTokens": 69_218,
                    "cacheReadTokens": 207_981,
                    "outputTokens": 1_083,
                },
            }
        ]
    )
    assert len(rows) == 1
    row = rows[0]
    assert row.source == "cursor_usage_service"
    assert row.tool_key == "cursor"
    assert row.model_key == "cursor:gemini-3.8-flash-high"
    assert row.input_tokens == 69_218 + 207_981
    assert row.cached_input_tokens == 207_981
    assert row.output_tokens == 1_083
    assert row.cost_usd == 0.07130258
    assert row.session_id == "conversation-1"


def test_cursor_usage_service_rejects_pre_cutover_rows() -> None:
    assert parse_events(
        [
            {
                "timestamp": str(int(datetime(2026, 9, 1, 23, 59, tzinfo=UTC).timestamp() * 1000)),
                "model": "gemini-3.8-flash-high",
                "tokenUsage": {"inputTokens": 1},
            }
        ]
    ) == []


def _fake_cursor_service(monkeypatch, events: list[dict]) -> list[dict]:
    """Capture each GetFilteredUsageEvents request body and answer with ``events``."""
    from spend_app.adapters import cursor_usage

    requests: list[dict] = []

    class Response:
        def raise_for_status(self) -> None:
            return None

        def json(self) -> dict:
            return {"usageEventsDisplay": events, "totalUsageEventsCount": len(events)}

    class Client:
        def __init__(self, *args, **kwargs) -> None:
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc) -> None:
            return None

        def post(self, url, *, headers=None, json=None):
            requests.append(json)
            return Response()

    monkeypatch.setattr("spend_app.integration_policy.authorize", lambda name: (True, None))
    monkeypatch.setattr("spend_app.integration_policy.identity_headers", lambda: {})
    monkeypatch.setattr(cursor_usage, "_cursor_access_token", lambda: "token")
    monkeypatch.setattr(cursor_usage, "_assert_allowed_https_host", lambda url, hosts: None)
    monkeypatch.setattr(cursor_usage.httpx, "Client", Client)
    cursor_usage.reset_state()
    return requests


def test_cursor_usage_service_fetches_only_the_recent_tail(tmp_path, monkeypatch) -> None:
    # Regression: every poll re-downloaded the whole history since the cutover
    # (hundreds of pages, minutes per poll) and stalled every other local source.
    from datetime import timedelta
    from pathlib import Path

    from spend_app.adapters import cursor_usage
    from spend_app.pricing import PricingEngine

    pricing = PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")
    database = tmp_path / "spend.db"
    newest = datetime.now(UTC).replace(microsecond=0) - timedelta(minutes=5)
    event = {
        "timestamp": str(int(newest.timestamp() * 1000)),
        "model": "gemini-3.8-flash-high",
        "conversationId": "conversation-1",
        "tokenUsage": {"inputTokens": 10, "outputTokens": 2},
    }
    requests = _fake_cursor_service(monkeypatch, [event])
    millis = lambda moment: str(int(moment.timestamp() * 1000))  # noqa: E731

    cursor_usage.ingest(database_path=database, pricing=pricing)
    assert requests[-1]["startDate"] == millis(cursor_usage.AUTHORITY_START)  # empty store: full backfill

    cursor_usage._NEXT_POLL_AT = 0.0
    cursor_usage.ingest(database_path=database, pricing=pricing)
    assert requests[-1]["startDate"] == millis(newest - cursor_usage.INCREMENTAL_OVERLAP)

    cursor_usage.reset_state()  # a restart heals a longer gap once
    cursor_usage.ingest(database_path=database, pricing=pricing)
    assert requests[-1]["startDate"] == millis(newest - cursor_usage.RESTART_LOOKBACK)
    cursor_usage.reset_state()
