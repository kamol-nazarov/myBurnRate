"""Organization usage and billed charges with synthetic provider transports."""

from datetime import UTC, datetime, timedelta

import httpx
import pytest

from spend_app.adapters.openai_admin import ingest
from spend_app.aggregate import usage_projection
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.native_usage import usage_report
from spend_app.pricing import PricingEngine
from spend_app.sync import apply_sync_push
from tests_spend.test_api import make_settings


START = datetime(2026, 11, 14, 12, tzinfo=UTC)
END = START + timedelta(hours=1)
MODEL = "gpt-6-astra"


def _openai_bucket(*, input_tokens=300_000, requests=3, model=MODEL):
    result = {
        "input_tokens": input_tokens,
        "output_tokens": 0,
        "model": model,
        "service_tier": "default",
    }
    if requests is not None:
        result["num_model_requests"] = requests
    return {
        "data": [{
            "start_time": int(START.timestamp()),
            "end_time": int(END.timestamp()),
            "results": [result],
        }],
        "has_more": False,
    }


@pytest.mark.parametrize(
    "tokens,requests,expected",
    [(300_000, 3, None), (300_000, None, None), (300_000, 1, 6), (272_000, 3, 2.72)],
)
def test_request_tier_from_admin_bucket(tmp_path, tokens, requests, expected):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    payload = _openai_bucket(input_tokens=tokens, requests=requests)
    with httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(200, json=payload))) as client:
        outcome = ingest(
            database_path=settings.database_path,
            pricing=pricing,
            admin_key="synthetic",
            start=START,
            end=END,
            client=client,
        )

    with connect(settings.database_path) as connection:
        projected, = usage_projection(connection, pricing, START, END)
        record = connection.execute(
            "SELECT computed_cost_usd, is_exact FROM usage_events"
        ).fetchone()
    org = usage_report(settings, pricing, now=END)["organizationTotal"]
    assert org["tokens"] == projected["input_tokens"] == tokens
    assert org["valueUsd"] == expected
    if expected is None:
        assert record is None
        assert projected["spend"] is None
        assert org["unpricedEvents"] == 1
        assert outcome["status"] == "partial"
    else:
        assert tuple(record) == (expected, 1)
        assert outcome["status"] == "success"


def test_legacy_unqualified_bucket_does_not_reprice_as_a_single_request(tmp_path):
    settings = make_settings(tmp_path / "org.db")
    pricing = PricingEngine.load(settings.pricing_path)
    initialize(settings.database_path)
    old_record = dict(
        source="openai_admin",
        tool_key="codex",
        model_key=MODEL,
        occurred_at=START.isoformat(),
        session_id=None,
        project=None,
        input_tokens=300_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=0,
        reasoning_tokens=None,
        cost_usd=None,
        computed_cost_usd=6,
        raw_id="old-bucket",
        ingested_at=START.isoformat(),
        is_exact=True,
    )
    with connect(settings.database_path) as connection:
        upsert_usage_event(connection, UsageEvent(**old_record))
    assert usage_report(settings, pricing, now=END)["organizationTotal"]["valueUsd"] is None
    three_requests = pricing.compute(
        model_key=MODEL,
        occurred_at=START,
        input_tokens=100_000,
        cached_input_tokens=0,
        cache_write_tokens=0,
        output_tokens=0,
    ) * 3
    assert three_requests == 3

    hub = make_settings(tmp_path / "hub.db")
    apply_sync_push(
        hub.database_path, pricing, machine_id="spoke-test",
        body={"usageEvents": [old_record]}, now=END,
    )
    with connect(hub.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0
    report = usage_report(hub, pricing, now=END)["organizationTotal"]
    assert report["tokens"] == 300_000
    assert report["valueUsd"] is None
