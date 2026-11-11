"""Published November model coverage, without live provider requests."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app import aggregate
from spend_app.adapters.common import event_is_exact
from spend_app.pricing import PricingEngine, UnpricedModelError


ROOT = Path(__file__).resolve().parents[1]
RELEASE = datetime(2026, 11, 9, tzinfo=UTC)


def test_fable_52_prices_each_token_class_without_long_context_surcharge():
    engine = PricingEngine.load(ROOT / "pricing")
    parts = engine.components(
        model_key="claude-fable-5-2", occurred_at=RELEASE,
        input_tokens=600_000, cached_input_tokens=500_000,
        cache_write_tokens=100_000, cache_write_1h_tokens=50_000,
        output_tokens=100_000,
    )
    assert parts == {
        "fresh_input": Decimal("0.8"), "cached_input": Decimal("0.1"),
        "cache_write": Decimal("1.3"), "output": Decimal("4"),
    }
    with pytest.raises(UnpricedModelError):
        engine.resolve("claude-fable-5-2", RELEASE - timedelta(microseconds=1))
    # Adding the successor must not rewrite the old model's cost.
    assert engine.resolve("claude-fable-5-1", RELEASE).input_per_mtok == Decimal("10")


@pytest.mark.parametrize("suffix", ["", "-low", "-medium", "-high"])
def test_antigravity_opus_effort_names_keep_underlying_rates_and_derived_status(suffix):
    engine = PricingEngine.load(ROOT / "pricing")
    model = "antigravity:claude-opus-5-5" + suffix
    price = engine.resolve(model, RELEASE)
    assert price == engine.resolve("claude-opus-5-5", RELEASE)
    assert not event_is_exact("antigravity_local", price)
    event = {
        "source": "antigravity_local", "tool_key": "antigravity", "model_key": model,
        "occurred_at": RELEASE.isoformat(), "input_tokens": 100_000,
        "cached_input_tokens": 80_000, "cache_write_tokens": 0,
        "output_tokens": 10_000, "cost_usd": None,
    }
    enriched = aggregate._enrich([event], engine, _cache=False)[0]
    assert enriched["spend"] == Decimal("0.296")
    assert enriched["model_key"] == model  # pricing aliases never relabel stored usage
    assert not aggregate._event_is_exact(enriched)


def test_opencode_router_haiku_slug_uses_haiku_45_api_equivalent():
    engine = PricingEngine.load(ROOT / "pricing")
    model = "opencode:anthropic/claude-haiku-4.5"
    price = engine.resolve(model, RELEASE)
    assert price == engine.resolve("claude-haiku-4-5", RELEASE)
    assert not event_is_exact("opencode_local", price)
    assert engine.compute(
        model_key=model, occurred_at=RELEASE,
        input_tokens=100_000, cached_input_tokens=80_000,
        cache_write_tokens=10_000, cache_write_1h_tokens=2_000,
        output_tokens=10_000,
    ) == Decimal("0.092")
