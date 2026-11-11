"""New cards must price recorded history immediately, without re-ingestion."""

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.adapters.common import UsageRow, persist_rows
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.pricing import PricingEngine


def test_hud_revalues_previously_unpriced_models(tmp_path):
    root = Path(__file__).resolve().parents[1]
    when = datetime(2026, 11, 9, 12, tzinfo=UTC)
    models = ("antigravity:claude-opus-5-5-high", "antigravity:gemini-4-argon",
              "antigravity:gemini-4-argon-high", "claude-fable-5-2", "muse:muse-ember-1.0")
    db = tmp_path / "usage.db"
    rows = [UsageRow(source="codex_local", tool_key="codex", model_key=model,
                     occurred_at=when, session_id="fixture", project=None,
                     input_tokens=100_000, cached_input_tokens=80_000,
                     cache_write_tokens=0, cache_write_1h_tokens=0,
                     output_tokens=1_000, reasoning_tokens=0, cost_usd=None,
                     raw_id="fixture-" + str(index)) for index, model in enumerate(models)]
    persist_rows(database_path=db, pricing=PricingEngine([]), source="codex_local", usage_rows=rows)
    settings = Settings(database_path=db, pricing_path=root / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="UTC",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    client = TestClient(create_app(settings, enable_scheduler=False,
                                  now=datetime(2026, 11, 9, 18, tzinfo=UTC)))
    nav = client.get("/api/spend/hud/usage").json()["navigation"]
    # Opus .116 + Argon .058 twice + Fable .216 + Ember .00282.
    assert Decimal(str(nav["todayUsd"])) == Decimal("0.45082")
    assert nav["todayUnpricedModels"] == []
    assert nav["todayUnpricedTokens"] == 0
    assert nav["todayComplete"] is True
