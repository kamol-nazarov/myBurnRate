"""The shareable weekly card: last seven days, totals and tools, never repositories."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.config import Settings
from spend_app.db import connect, initialize
from spend_app.hud_data import week_card
from spend_app.pricing import PricingEngine
from spend_app.subscriptions import add_subscription

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 10, 22, 18, 0, tzinfo=UTC)


def _settings(database: Path) -> Settings:
    return Settings(
        database_path=database,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="America/New_York",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def _row(source: str, tool: str, model: str, when: datetime, session: str, project: str) -> UsageRow:
    return UsageRow(
        source=source, tool_key=tool, model_key=model, occurred_at=when, session_id=session, project=project,
        input_tokens=50_000, cached_input_tokens=45_000, cache_write_tokens=0, cache_write_1h_tokens=0,
        output_tokens=2_000, reasoning_tokens=None, cost_usd=None, raw_id=f"{source}:{session}:{when.isoformat()}",
    )


def test_the_card_sums_the_last_seven_days_by_tool_against_the_plans(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    claude = [_row("claude_local", "claude-code", "claude-opus-5-5", NOW - timedelta(days=day, hours=1), f"c{day}",
                   "secret-client-repo") for day in range(3)]
    codex = [_row("codex_local", "codex", "gpt-5.6-sol", NOW - timedelta(days=2), "x1", "secret-client-repo")]
    old = [_row("codex_local", "codex", "gpt-5.6-sol", NOW - timedelta(days=9), "x0", "secret-client-repo")]
    persist_rows(database_path=database, pricing=pricing, source="claude_local", usage_rows=claude)
    persist_rows(database_path=database, pricing=pricing, source="codex_local", usage_rows=codex + old)
    with connect(database) as connection:
        add_subscription(connection, tool_key="claude-code", name="Claude Max 20x", amount_usd=200.0,
                         cadence="monthly", start_date="2026-10-01", end_date=None)

    card = week_card(_settings(database), pricing, now=NOW)

    assert card["days"] == 7 and card["to"].startswith("2026-10-22")
    assert card["sessions"] == 4 and card["tokens"] == 4 * 52_000
    assert [tool["name"] for tool in card["tools"]] == ["Claude Code", "Codex"]
    assert round(sum(tool["usd"] for tool in card["tools"]), 6) == round(card["usd"], 6)
    assert 0 < card["planUsd"] < 200
    assert card["cacheReusePct"] > 80
    # A card meant for sharing never names a repository.
    assert "secret-client-repo" not in json.dumps(card)


def test_an_empty_week_is_a_zero_card_not_an_error(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    card = week_card(_settings(database), PricingEngine.load(ROOT / "pricing"), now=NOW)
    assert card["usd"] == 0.0 and card["tools"] == [] and card["planUsd"] is None
