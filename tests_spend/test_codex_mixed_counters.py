"""Counter omissions must not bill an already-recorded request twice."""
import copy
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.adapters.codex_records import reduce_records
from spend_app.adapters.event_identity import reconcile
from spend_app.pricing import PricingEngine
from spend_app.source_health import SourceHealth
from test_provider_compat_unit import FakeEventDB
from test_provider_review_fixes_unit import save


def counts(value):
    return dict(input_tokens=value, cached_input_tokens=0, output_tokens=0,
                total_tokens=value, reasoning_output_tokens=0)


def event(second, *, cumulative=None, last=None, request=None):
    return {
        "type": "event_msg", "timestamp": f"2026-11-09T00:00:{second:02}Z",
        "payload": {"type": "token_count", "info": {
            "total_token_usage": counts(cumulative) if cumulative is not None else None,
            "last_token_usage": counts(last) if last is not None else None,
            "request_id": request,
        }},
    }


def reduce(events):
    health = SourceHealth()
    _, observations = reduce_records([
        {"type": "session_meta", "timestamp": "2026-11-09T00:00:00Z",
         "payload": {"id": "synthetic-session", "originator": "codex_cli_rs"}},
        {"type": "turn_context", "payload": {"model": "gpt-6-astra"}},
        *events,
    ], health, "fallback")
    return observations, health


@pytest.mark.parametrize("requests", [False, True])
def test_cumulative_resumes_after_last_only_without_rebilling(requests):
    events = [
        event(1, cumulative=100_000, last=100_000, request="r1" if requests else None),
        event(2, last=10_000, request="r2" if requests else None),
        event(3, cumulative=120_000, last=10_000, request="r3" if requests else None),
    ]
    observations, health = reduce(events)
    assert [o.row.input_tokens for o in observations] == [100_000, 10_000, 10_000]
    assert not health.partial
    engine = PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")
    assert sum(engine.compute(
        model_key=o.row.model_key, occurred_at=o.row.occurred_at,
        input_tokens=o.row.input_tokens, cached_input_tokens=o.row.cached_input_tokens,
        cache_write_tokens=o.row.cache_write_tokens, output_tokens=o.row.output_tokens,
    ) for o in observations) == Decimal("1.2")
    # Repeated reads and a fresh process's identity ledger keep the same total.
    db = FakeEventDB()
    save(db, reconcile(db, reduce(events[:2])[0]))
    db = copy.deepcopy(db)
    save(db, reconcile(db, observations))
    save(db, reconcile(db, observations))
    assert sum(row["input_tokens"] for row in db.events.values()) == 120_000


def test_equal_cumulative_snapshot_consumes_pending_last_only_baseline():
    observations, _ = reduce([
        event(1, cumulative=100, last=100), event(2, last=10),
        event(3, cumulative=110, last=10), event(4, cumulative=120, last=10),
    ])
    assert [o.row.input_tokens for o in observations] == [100, 10, 10]


def test_repeated_explicit_last_only_request_advances_baseline_once():
    observations, _ = reduce([
        event(1, cumulative=100, last=100, request="r1"),
        event(2, last=10, request="r2"), event(3, last=10, request="r2"),
        event(4, cumulative=120, last=10, request="r3"),
    ])
    assert [o.row.input_tokens for o in observations] == [100, 10, 10]


@pytest.mark.parametrize("requests", [False, True])
def test_full_replay_repairs_the_old_inflated_counter_interval(requests):
    observations, _ = reduce([
        event(1, cumulative=100_000, last=100_000, request="r1" if requests else None),
        event(2, last=10_000, request="r2" if requests else None),
        event(3, cumulative=120_000, last=10_000, request="r3" if requests else None),
    ])
    # Version 4 persisted the middle request without a counter interval and
    # the last request as 100k..120k, double-counting that middle request.
    old_id = dict(observations[2].components).get("request_equivalent_counter", observations[2].row.raw_id)
    legacy = [observations[0], replace(observations[1], components=()),
              replace(observations[2], row=replace(observations[2].row, input_tokens=20_000, raw_id=old_id),
                      components=tuple((k, 100_000 if k == "counter_start" else v)
                                       for k, v in observations[2].components
                                       if k != "request_equivalent_counter"))]
    db = FakeEventDB()
    save(db, reconcile(db, legacy))
    assert sum(row["input_tokens"] for row in db.events.values()) == 130_000
    db = copy.deepcopy(db)
    issues = []
    save(db, reconcile(db, observations, issues))
    assert sum(row["input_tokens"] for row in db.events.values()) == 120_000
    assert not issues
