from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app import aggregate
from spend_app.config import Settings
from spend_app.db import (
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_quota,
    upsert_unpriced_event,
    upsert_usage_event,
)
from spend_app.hud_data import _model_name, _window_start, recorded_usage
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
# 14:00 in New York; local midnight is 04:00 UTC.
NOW = datetime(2026, 10, 20, 18, tzinfo=UTC)


def iso(moment: datetime) -> str:
    return moment.isoformat().replace("+00:00", "Z")


def settings_for(tmp_path) -> Settings:
    settings = Settings(database_path=tmp_path / "hud.db", pricing_path=ROOT / "pricing",
                        anthropic_admin_key=None, openai_admin_key=None, timezone="America/New_York",
                        cache_hit_threshold=0.75, over_routing_token_ceiling=40000)
    initialize(settings.database_path)
    return settings


def quota(connection, provider_key: str, limit_key: str, resets_at: datetime) -> None:
    upsert_quota(connection, provider_key=provider_key, limit_key=limit_key, label=f"{provider_key} {limit_key}",
                 unit="pct", pct=10, used=None, allowance=None, is_payg=False,
                 resets_at=iso(resets_at), source="test", polled_at=iso(NOW))


def event(connection, raw_id: str, tool_key: str, model_key: str, when: datetime, *,
          fresh: int = 0, cached: int = 0, writes: int = 0, output: int = 0) -> None:
    upsert_usage_event(connection, UsageEvent(
        source="test", tool_key=tool_key, model_key=model_key, occurred_at=iso(when), session_id="s",
        project=None, input_tokens=fresh + cached, cached_input_tokens=cached, cache_write_tokens=writes,
        cache_write_1h_tokens=0, output_tokens=output, reasoning_tokens=None, cost_usd=None,
        computed_cost_usd=0, raw_id=raw_id, ingested_at=iso(NOW)))


def test_meter_windows_carry_their_burned_tokens_and_todays_models(tmp_path):
    aggregate.reset_memo()
    settings = settings_for(tmp_path)
    with connect(settings.database_path) as connection:
        quota(connection, "claude-code", "5h", NOW + timedelta(hours=2))
        quota(connection, "claude-code", "weekly", NOW + timedelta(days=3))
        quota(connection, "claude-code-work", "5h", NOW + timedelta(hours=1))
        quota(connection, "opencode", "weekly", NOW + timedelta(days=1))
        quota(connection, "antigravity", "gemini-weekly", NOW + timedelta(days=2))
        quota(connection, "antigravity", "3p-weekly", NOW + timedelta(days=2))
        quota(connection, "grok", "weekly", NOW + timedelta(days=2))

        event(connection, "c1", "claude-code", "claude-opus-5-5", NOW - timedelta(hours=1),
              fresh=400, cached=600, writes=100, output=50)
        # Earlier today, before this 5-hour window opened.
        event(connection, "c2", "claude-code", "claude-haiku-5-5", NOW - timedelta(hours=5), fresh=200, output=20)
        event(connection, "c3", "claude-code", "claude-opus-5-5", NOW - timedelta(days=2), fresh=500, output=500)
        # Before the weekly window opened.
        event(connection, "c4", "claude-code", "claude-opus-5-5", NOW - timedelta(days=5), fresh=9_000)
        # Z.AI counts OpenCode and ZCode, never OpenCode Go.
        event(connection, "z1", "zcode", "zcode:glm-5.3", NOW - timedelta(hours=1), fresh=300)
        event(connection, "g1", "opencode-go", "opencode-go:glm-5.3-flash", NOW - timedelta(hours=1), fresh=999)
        # Antigravity meters Gemini and the other models apart.
        event(connection, "a1", "antigravity", "antigravity:gemini-3.8-flash-high", NOW - timedelta(hours=1), fresh=40)
        event(connection, "a2", "antigravity", "antigravity:claude-4.6", NOW - timedelta(hours=1), fresh=60)
        upsert_unpriced_event(connection, UnpricedUsageEvent(
            source="test", tool_key="grok", model_key="supergrok:grok-4.7", occurred_at=iso(NOW - timedelta(hours=1)),
            session_id="g", project=None, input_tokens=70, cached_input_tokens=30, cache_write_tokens=0,
            cache_write_1h_tokens=0, output_tokens=5, reasoning_tokens=None, unclassified_tokens=0,
            telemetry_complete=True, cost_usd=None, raw_id="grok1", ingested_at=iso(NOW)))

    tokens = recorded_usage(settings, PricingEngine.load(settings.pricing_path), now=NOW)["windowTokens"]

    claude = tokens["claude-code"]
    assert claude["5h"] == {"since": iso(NOW - timedelta(hours=3)), "tokens": 1150,
                            "input": 500, "cached": 600, "output": 50}
    assert "today" not in claude["5h"]
    assert claude["weekly"]["tokens"] == 1150 + 220 + 1000
    assert claude["weekly"]["today"] == {"tokens": 1370, "models": [
        {"name": "Opus 5.5", "tokens": 1150}, {"name": "Haiku 5.5", "tokens": 220}]}
    assert "claude-code-work" not in tokens, "extra accounts have no collected usage"

    assert tokens["opencode"]["weekly"]["tokens"] == 300
    assert tokens["opencode"]["weekly"]["today"]["models"] == [{"name": "ZCode GLM 5.3", "tokens": 300}]
    assert tokens["antigravity"]["gemini-weekly"]["today"]["models"] == [{"name": "Gemini 3.8 Flash", "tokens": 40}]
    assert tokens["antigravity"]["3p-weekly"]["today"]["models"] == [{"name": "Claude 4.6", "tokens": 60}]
    assert tokens["grok"]["weekly"]["tokens"] == 75


def test_window_starts_follow_each_window_length():
    reset = "2026-03-31T12:00:00Z"
    assert _window_start("5h", reset) == datetime(2026, 3, 31, 7, tzinfo=UTC)
    assert _window_start("gemini-weekly", reset) == datetime(2026, 3, 24, 12, tzinfo=UTC)
    assert _window_start("monthly", reset) == datetime(2026, 2, 28, 12, tzinfo=UTC)
    assert _window_start("balance", reset) is None
    assert _window_start("5h", None) is None


def test_uncurated_model_names_read_cleanly():
    assert _model_name("opencode-go", "opencode-go:deepseek-v4.1-flash") == "DeepSeek V4.1 Flash"
    assert _model_name("antigravity", "antigravity:gpt-oss-120b") == "GPT OSS 120B"
    assert _model_name("codex", "gpt-5.6-sol") == "GPT 5.6 Sol"
    assert _model_name("claude-code", "claude-opus-5-5") == "Opus 5.5"
