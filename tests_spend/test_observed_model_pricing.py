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


def test_claude_mythos_5_2_official_pricing_and_boundary(pricing):
    launch = datetime(2026, 11, 9, tzinfo=UTC)
    with pytest.raises(UnpricedModelError):
        pricing.resolve("claude-mythos-5-2", launch - timedelta(microseconds=1))
    # 1M fresh input ($8), 1M cached read ($0.20), 1M 5m cache write ($10), 1M output ($40) = $58.20
    cost = pricing.compute(
        model_key="claude-mythos-5-2",
        occurred_at=launch,
        input_tokens=2_000_000,
        cached_input_tokens=1_000_000,
        cache_write_tokens=1_000_000,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("58.20")
    assert pricing.resolve("opencode:anthropic/claude-mythos-5-2", launch) == pricing.resolve("claude-mythos-5-2", launch)


def test_gemini_4_neon_and_tiered_aliases(pricing):
    launch = datetime(2026, 11, 18, tzinfo=UTC)
    with pytest.raises(UnpricedModelError):
        pricing.resolve("antigravity:gemini-4-neon", launch - timedelta(microseconds=1))
    # 1M fresh ($3), 1M cached ($0.15), 1M output ($15) = $18.15
    cost = pricing.compute(
        model_key="antigravity:gemini-4-neon-xhigh",
        occurred_at=launch,
        input_tokens=2_000_000,
        cached_input_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    assert cost == Decimal("18.15")
    assert pricing.resolve("antigravity:gemini-4-hydrogen-tiered-high", launch) == pricing.resolve(
        "antigravity:gemini-4-hydrogen", launch
    )
    assert pricing.resolve("opencode:deepseek-v4.1-flash", launch) == pricing.resolve(
        "deepseek:deepseek-flash", launch
    )


def test_deepseek_v4_pro_peak_and_off_peak_rates(pricing):
    # Off-peak UTC (17:00 UTC) vs peak UTC weekday (08:00 UTC inside [6, 10) Mon-Fri)
    off_peak = datetime(2026, 11, 19, 17, 0, tzinfo=UTC)
    peak = datetime(2026, 11, 19, 8, 0, tzinfo=UTC)
    off_cost = pricing.compute(
        model_key="deepseek:deepseek-v4-pro",
        occurred_at=off_peak,
        input_tokens=2_000_000,
        cached_input_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    peak_cost = pricing.compute(
        model_key="deepseek:deepseek-v4-pro",
        occurred_at=peak,
        input_tokens=2_000_000,
        cached_input_tokens=1_000_000,
        cache_write_tokens=0,
        output_tokens=1_000_000,
    )
    assert off_cost == Decimal("2.662")
    assert peak_cost == off_cost * Decimal("2")


@pytest.mark.parametrize("model_key", [
    "gpt-5.4",
    "o3",
    "xai:grok-build-0.1",
    "zcode:glm-5",
    "openrouter:qwen/qwen3-coder",
    "opencode:qwen3-coder",
    "openrouter:meta-llama/llama-4-maverick",
    "opencode:devstral-2512",
    "antigravity:gpt-oss-120b",
    "openrouter:moonshotai/kimi-k3.1-pro",
    "openrouter:minimax/minimax-m3.1-flash",
    "openrouter:google/gemma-4-31b-it",
])
def test_popular_proprietary_and_open_source_models_resolve(pricing, model_key):
    verified_nov19 = datetime(2026, 11, 19, tzinfo=UTC)
    rate = pricing.resolve(model_key, verified_nov19)
    assert rate.input_per_mtok >= Decimal("0")
    assert rate.output_per_mtok >= Decimal("0")
    assert rate.source_url.startswith("https://")


def test_recent_model_display_names():
    from spend_app.aggregate import display_model

    assert display_model("claude-mythos-5-2") == "Claude Mythos 5.2"
    assert display_model("gemini-4-hydrogen") == "Gemini 4 Hydrogen"
    assert display_model("gemini-4-neon") == "Gemini 4 Neon"
    assert display_model("muse:muse-wonder-full-1.0") == "Muse Wonder Full 1.0"
