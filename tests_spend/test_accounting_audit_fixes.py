"""One valuation/token projection, incomplete observations, and elapsed windows."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app import aggregate
from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.config import Settings
from spend_app.db import connect
from spend_app.hud_data import recorded_usage, repo_breakdown, week_card
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 11, 9, 18, tzinfo=UTC)


def settings(tmp_path):
    return Settings(database_path=tmp_path / "usage.db", pricing_path=ROOT / "pricing",
                    anthropic_admin_key=None, openai_admin_key=None, timezone="America/New_York",
                    cache_hit_threshold=0.75, over_routing_token_ceiling=40_000)


def observation(**overrides):
    row = UsageRow(source="codex_local", tool_key="codex", model_key="gpt-6-astra",
                   occurred_at=NOW - timedelta(hours=1), session_id="synthetic", project="northwind",
                   input_tokens=100_000, cached_input_tokens=90_000, cache_write_tokens=0,
                   cache_write_1h_tokens=0, output_tokens=1_000, reasoning_tokens=0,
                   cost_usd=None, raw_id="synthetic-event")
    return replace(row, **overrides)


def test_unknown_cache_split_is_not_charged_as_fresh_or_shown_as_zero(tmp_path):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    row = observation(cached_input_tokens=0, telemetry_complete=False)
    persist_rows(database_path=config.database_path, pricing=engine, source=row.source, usage_rows=[row])
    hud = recorded_usage(config, engine, now=NOW)
    assert hud["navigation"]["todayUsd"] is None
    assert hud["navigation"]["todayComplete"] is False
    assert hud["navigation"]["todayTokens"] == 101_000
    assert hud["totals"]["cacheReusePct"] is None
    assert hud["repos"]["totalUsd"] is None
    assert hud["repos"]["rows"][0]["usd"] is None
    card = week_card(config, engine, now=NOW)
    assert card["usd"] is None and card["complete"] is False
    assert card["tools"] == [] and card["cacheReusePct"] is None


def test_mixed_complete_and_unknown_usage_has_only_a_proven_partial_value(tmp_path):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    rows = [observation(), observation(raw_id="unknown-cache", cached_input_tokens=0, telemetry_complete=False)]
    persist_rows(database_path=config.database_path, pricing=engine, source="codex_local", usage_rows=rows)
    hud = recorded_usage(config, engine, now=NOW)
    assert hud["navigation"]["todayUsd"] == 0.24
    assert hud["navigation"]["todayComplete"] is False
    assert hud["navigation"]["todayUnpricedTokens"] == 101_000
    assert hud["totals"]["cacheReusePct"] is None
    with connect(config.database_path) as db:
        events = aggregate.usage_projection(db, engine, NOW - timedelta(days=1), NOW)
    assert aggregate._cache_savings(events=events, pricing=engine) is None


@pytest.mark.parametrize("source,expected", [("grok_local", 1.25), ("opencode_local", None)])
def test_incomplete_usage_preserves_charge_but_not_harness_estimate(tmp_path, source, expected):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    row = observation(source=source, telemetry_complete=False, cost_usd=1.25)
    persist_rows(database_path=config.database_path, pricing=engine, source=source, usage_rows=[row])
    nav = recorded_usage(config, engine, now=NOW)["navigation"]
    assert nav["todayUsd"] == expected
    assert nav["todayComplete"] is (expected is not None)


def test_new_card_prices_history_in_totals_repositories_and_weekly_tools(tmp_path):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    persist_rows(database_path=config.database_path, pricing=PricingEngine([]), source="codex_local",
                 usage_rows=[observation()])
    hud, week = recorded_usage(config, engine, now=NOW), week_card(config, engine, now=NOW)
    assert hud["navigation"]["todayUsd"] == hud["repos"]["totalUsd"] == week["usd"] == 0.24
    assert hud["repos"]["rows"][0]["tokens"] == hud["navigation"]["todayTokens"] == week["tokens"] == 101_000
    assert sum(row["usd"] for row in week["tools"]) == week["usd"]
    assert hud["totals"]["cacheReusePct"] == week["cacheReusePct"] == 90.0


def test_repository_counts_cache_subsets_once():
    result = repo_breakdown([dict(project="northwind", spend=Decimal("1"),
                                 input_tokens=100_000, cached_input_tokens=90_000,
                                 cache_write_tokens=10_000, cache_write_1h_tokens=8_000,
                                 output_tokens=1_000)])
    assert result["rows"][0]["tokens"] == 111_000


def test_measured_zero_is_distinct_from_missing_value(tmp_path):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    row = observation(input_tokens=0, cached_input_tokens=0, output_tokens=0)
    persist_rows(database_path=config.database_path, pricing=engine, source=row.source, usage_rows=[row])
    nav = recorded_usage(config, engine, now=NOW)["navigation"]
    assert nav["todayUsd"] == 0.0 and nav["todayComplete"] is True


@pytest.mark.parametrize("now", [datetime(2026, 3, 8, 16, tzinfo=UTC), datetime(2026, 11, 1, 17, tzinfo=UTC)])
@pytest.mark.parametrize("key,span", [("1d", timedelta(days=1)), ("1w", timedelta(days=7)), ("6h", timedelta(hours=6))])
def test_rolling_and_previous_windows_keep_elapsed_duration_across_dst(now, key, span):
    window = aggregate.resolve_window(key, now=now, timezone="America/New_York")
    assert window.end - window.start == span
    assert window.previous_end - window.previous_start == span
    assert window.previous_end == window.start


def test_repository_calendar_day_is_not_truncated_to_24_hours(tmp_path):
    config, engine = settings(tmp_path), PricingEngine.load(ROOT / "pricing")
    # Fall-back Sunday is 25 hours. At 23:30 local, 00:05 is older than 24h.
    now = datetime(2026, 11, 2, 4, 30, tzinfo=UTC)
    row = observation(occurred_at=datetime(2026, 11, 1, 4, 5, tzinfo=UTC))
    persist_rows(database_path=config.database_path, pricing=engine, source=row.source, usage_rows=[row])
    hud = recorded_usage(config, engine, now=now)
    assert hud["navigation"]["todayUsd"] == hud["repos"]["totalUsd"] == 0.24
