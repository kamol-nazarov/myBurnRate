"""Offline checks of Meta's published Standard-mode token rates and tiers."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.pricing import PricingEngine, UnpricedModelError


ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 9, 12, tzinfo=UTC)


@pytest.fixture
def engine():
    return PricingEngine.load(ROOT / "pricing")


@pytest.mark.parametrize(
    "model,expected",
    [
        ("muse:muse-ember-1.0", "0.010404"),
        ("muse:muse-spark-1.3", "0.12815"),
        ("muse:muse-wonder-1.0", "0.4162"),
    ],
)
def test_standard_muse_rates_price_fresh_cached_and_output(engine, model, expected):
    # 99K fresh input, 1K cached input, and 1K output. Cached input is a
    # subset of input_tokens; the fixture must not count it twice.
    assert engine.compute(
        model_key=model, occurred_at=NOW, input_tokens=100_000,
        cached_input_tokens=1_000, cache_write_tokens=0, output_tokens=1_000,
    ) == Decimal(expected)
    assert not engine.resolve(model, NOW).is_exact


def test_ember_tier_counts_cached_input_and_has_strict_boundary(engine):
    common = dict(model_key="muse:muse-ember-1.0", occurred_at=NOW,
                  cache_write_tokens=0, output_tokens=1_000)
    # Exactly 200K cached input remains short. One additional *cached* token
    # moves the whole request, including output, to the long-context tier.
    assert engine.compute(input_tokens=200_000, cached_input_tokens=200_000,
                          **common) == Decimal("0.0013")
    assert engine.compute(input_tokens=200_001, cached_input_tokens=200_001,
                          **common) == Decimal("0.003900012")


@pytest.mark.parametrize("model", ["muse:muse-spark-1.3", "muse:muse-wonder-1.0"])
def test_spark_and_wonder_have_no_long_context_premium(engine, model):
    assert engine.resolve(model, NOW).long_context_threshold is None


@pytest.mark.parametrize("model", [
    "muse:muse-wonder-full-1.0", "muse:unknown", "muse:muse-spark-1.3-contributor",
])
def test_other_muse_variants_do_not_inherit_standard_rates(engine, model):
    with pytest.raises(UnpricedModelError):
        engine.resolve(model, NOW)


@pytest.mark.parametrize("model", [
    "muse:muse-ember-1.0", "muse:muse-spark-1.3", "muse:muse-wonder-1.0",
])
def test_muse_prices_do_not_backdate_unverified_history(engine, model):
    with pytest.raises(UnpricedModelError):
        engine.resolve(model, datetime(2026, 11, 8, 23, 59, tzinfo=UTC))


def test_muse_msp_record_does_not_double_count_cached_tokens_or_rollups(engine, tmp_path):
    from spend_app.adapters.muse_records import reduce_records
    from spend_app.db import UnpricedUsageEvent, connect, initialize, retire_muse_rollup_events, upsert_unpriced_event

    records = [
        {
            "recorded_at": 1794355200000000,
            "payload_type": "runtime.session",
            "stream": {"id": "sess-rollup"},
            "payload": {
                "event": {
                    "id": "turn-real",
                    "kind": "model_completed",
                    "model": "muse-ember-1.0",
                    "cwd": "C:/Dev/ProjectA",
                    "usage": {
                        "input_tokens": 150_000,
                        "cached_tokens": 149_000,
                        "cache_read_tokens": 149_000,
                        "cache_write_tokens": 0,
                        "output_tokens": 1_000,
                        "reasoning_tokens": 200,
                    },
                }
            },
        },
        {
            "recorded_at": 1794355210000000,
            "payload_type": "runtime.session",
            "stream": {"id": "sess-rollup"},
            "payload": {
                "event": {
                    "id": "checkpoint-1",
                    "kind": "context_projection_checkpoint",
                    "usage": {
                        "input_tokens": 24_546_060,
                        "cached_tokens": 24_399_336,
                        "cache_read_tokens": 24_399_336,
                        "output_tokens": 41_915,
                    },
                }
            },
        },
        {
            "recorded_at": 1794355220000000,
            "payload_type": "runtime.session",
            "stream": {"id": "sess-rollup"},
            "payload": {
                "event": {
                    "id": "child-1",
                    "kind": "workflow_child_lifecycle",
                    "usage": {
                        "input_tokens": 500_000,
                        "cached_tokens": 480_000,
                        "output_tokens": 2_000,
                    },
                }
            },
        },
    ]
    rows, _ = reduce_records(records, file_key="sess-rollup")
    assert len(rows) == 1
    row = rows[0]
    assert row.model_key == "muse:muse-ember-1.0"
    assert row.input_tokens == 150_000
    assert row.cached_input_tokens == 149_000
    # 150K input stays below Ember's 200K long-context threshold (1K fresh @ $0.10/M + 149K cached @ $0.004/M + 1K out @ $0.50/M)
    cost = engine.compute(
        model_key=row.model_key,
        occurred_at=row.occurred_at,
        input_tokens=row.input_tokens,
        cached_input_tokens=row.cached_input_tokens,
        cache_write_tokens=row.cache_write_tokens,
        output_tokens=row.output_tokens,
    )
    assert cost == Decimal("0.001196")

    db = tmp_path / "muse-repair.db"
    initialize(db)
    with connect(db) as conn:
        conn.execute("DELETE FROM app_meta WHERE key = 'repair.muse_rollup_cleanup.v1'")
        upsert_unpriced_event(
            conn,
            UnpricedUsageEvent(
                source="muse_local",
                tool_key="muse",
                model_key="muse:unknown",
                occurred_at="2026-11-10T10:00:00Z",
                session_id="sess-rollup",
                project="ProjectA",
                input_tokens=48_945_396,
                cached_input_tokens=24_399_336,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=41_915,
                reasoning_tokens=0,
                unclassified_tokens=0,
                telemetry_complete=True,
                cost_usd=None,
                raw_id="muse-rollup-stale",
                ingested_at="2026-11-10T10:01:00Z",
            ),
        )
    from spend_app import db as db_mod
    db_mod._MUSE_ROLLUP_CHECKED.discard(str(db.resolve()))
    assert retire_muse_rollup_events(db) == 1
    with connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM unpriced_usage_events WHERE model_key='muse:unknown'").fetchone()[0] == 0
