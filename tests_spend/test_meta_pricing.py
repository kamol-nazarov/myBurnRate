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
