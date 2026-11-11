"""Dated official cards and explicit harness aliases; no private usage fixtures."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.pricing import PricingEngine, UnpricedModelError

ROOT = Path(__file__).resolve().parents[1]
VERIFIED = datetime(2026, 11, 9, tzinfo=UTC)


@pytest.fixture(scope="module")
def pricing():
    return PricingEngine.load(ROOT / "pricing")


@pytest.mark.parametrize("alias,canonical", [
    ("opencode:gpt-5.6-sol", "gpt-5.6-sol"),
    ("opencode:gpt-6-sol", "gpt-6-sol"),
    ("opencode:gpt-6-luna", "gpt-6-luna"),
    ("opencode:openai/gpt-6-luna", "gpt-6-luna"),
    ("opencode:composer-2.5", "cursor:composer-2.5"),
    ("opencode:composer-2.5-fast", "cursor:composer-2.5-fast"),
    ("opencode:z-ai/glm-5.3-flash", "opencode:glm-5.3-flash"),
    ("opencode:deepseek/deepseek-v4.1-flash-1017", "deepseek:deepseek-flash"),
    ("opencode:grok-4.7", "xai:grok-4.7"),
])
def test_explicit_harness_aliases_use_canonical_rates(pricing, alias, canonical):
    assert pricing.resolve(alias, VERIFIED) == pricing.resolve(canonical, VERIFIED)


def test_luna_launch_boundary_and_request_tiers(pricing):
    launch = datetime(2026, 9, 22, tzinfo=UTC)
    with pytest.raises(UnpricedModelError):
        pricing.resolve("gpt-6-luna", launch - timedelta(microseconds=1))
    values = dict(model_key="gpt-6-luna", occurred_at=launch, cached_input_tokens=200_000,
                  cache_write_tokens=1_000, output_tokens=1_000)
    assert pricing.compute(input_tokens=272_000, **values) == Decimal("0.009825")
    assert pricing.compute(input_tokens=272_001, **values) == Decimal("0.0194002")
    assert pricing.resolve("opencode:gpt-6-sol", launch).input_per_mtok == Decimal("2")


@pytest.mark.parametrize("model,expected", [
    ("opencode:gpt-5.4-mini", "0.555"),
    ("opencode-go:kimi-k3.1-pro", "1.242"),
    ("opencode:grok-4.20-0309-non-reasoning", "0.3325"),
    ("openrouter:deepseek/deepseek-v4.1-flash", "0.07035"),
    ("openrouter:glm-5.3-flash", "0.0645"),
])
def test_verified_rates_and_no_invented_history(pricing, model, expected):
    # 10k fresh, 100k cached, 1k writes, 120k output: separate token classes.
    assert pricing.compute(model_key=model, occurred_at=VERIFIED, input_tokens=110_000,
                           cached_input_tokens=100_000, cache_write_tokens=1_000,
                           output_tokens=120_000) == Decimal(expected)
    with pytest.raises(UnpricedModelError):
        pricing.resolve(model, VERIFIED - timedelta(microseconds=1))


def test_mercury_launch_discount_is_not_the_struck_list_rate(pricing):
    launch = datetime(2026, 10, 19, tzinfo=UTC)
    assert pricing.compute(model_key="opencode:inception/mercury-3", occurred_at=launch,
                           input_tokens=1_000_000, cached_input_tokens=900_000,
                           cache_write_tokens=0, output_tokens=100_000) == Decimal("0.054")
    with pytest.raises(UnpricedModelError):
        pricing.resolve("inception:mercury-3", launch - timedelta(microseconds=1))


@pytest.mark.parametrize("model", ["unknown", "muse:unknown", "opencode:union-alpha",
                                   "opencode:stealth/union-alpha", "opencode:glm-5.3-highspeed"])
def test_unidentified_or_unverified_variants_stay_unpriced(pricing, model):
    with pytest.raises(UnpricedModelError):
        pricing.resolve(model, VERIFIED)
