"""Plans reference value obeys the same telemetry and historical-rate rules."""

from decimal import Decimal
from pathlib import Path

import pytest

from spend_app.plans_value import assemble_unassigned_summary, resolve_event_reference_usd
from spend_app.pricing import PricingEngine


@pytest.fixture
def pricing():
    return PricingEngine.load(Path(__file__).resolve().parents[1] / "pricing")


def event(**overrides):
    return {"source": "codex_local", "tool_key": "codex", "model_key": "gpt-6-astra",
            "occurred_at": "2026-11-09T12:00:00Z", "input_tokens": 100_000,
            "cached_input_tokens": 90_000, "cache_write_tokens": 0,
            "output_tokens": 1_000, "telemetry_complete": True, **overrides}


def test_complete_history_revalues_identically_from_either_table(pricing):
    from_priced = assemble_unassigned_summary([event()], [], pricing=pricing)
    from_unpriced = assemble_unassigned_summary([], [event()], pricing=pricing)
    assert from_priced == from_unpriced
    assert Decimal(from_unpriced["usageValueUsd"]) == Decimal("0.24")
    assert from_unpriced["usageValueStatus"] == "complete"
    assert from_unpriced["pricedTokens"] == 101_000
    assert from_unpriced["unpricedTokens"] == 0


@pytest.mark.parametrize("incomplete", [False, 0])
@pytest.mark.parametrize("stored", [{}, {"computed_cost_usd": 1.05}, {"reference_value_usd": 1.05}, {"cost_usd": 1.05}])
def test_missing_cache_telemetry_cannot_produce_reference_lower_bound(pricing, incomplete, stored):
    row = event(cached_input_tokens=0, telemetry_complete=incomplete, **stored)
    assert resolve_event_reference_usd(row, pricing) is None
    for priced, unpriced in (([row], []), ([], [row])):
        summary = assemble_unassigned_summary(priced, unpriced, pricing=pricing)
        assert summary["usageValueUsd"] is None
        assert summary["usageValueStatus"] == "unavailable"
        assert summary["unpricedTokens"] == 101_000


def test_mixed_unpriced_storage_keeps_only_supported_reference_value(pricing):
    summary = assemble_unassigned_summary([], [event(), event(telemetry_complete=False, cached_input_tokens=0)], pricing=pricing)
    assert Decimal(summary["usageValueUsd"]) == Decimal("0.24")
    assert summary["usageValueStatus"] == "lower_bound"


def test_historical_revaluation_keeps_scope_exclusions_and_no_charge_fallback(pricing):
    for row in (event(source="openai_admin", cost_usd=25), event(scope_conflict=True),
                event(model_key="unknown-model", cost_usd=25)):
        summary = assemble_unassigned_summary([], [row], pricing=pricing)
        assert summary["usageValueUsd"] is None
