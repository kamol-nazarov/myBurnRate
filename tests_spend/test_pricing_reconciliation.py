"""The 2026-10-26 reconciliation: one value rule, every request priced once.

See docs/burnrate-pricing-reconciliation-oct26.md for the day these encode.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from spend_app import aggregate
from spend_app.adapters import grok_local, grok_native
from spend_app.config import Settings
from spend_app.db import (
    UnpricedUsageEvent,
    connect,
    dedupe_grok_calls,
    initialize,
    upsert_unpriced_event,
)
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
MONDAY = datetime(2026, 10, 26, tzinfo=UTC)
SID = "01a17659-8eb4-78e1-8790-6dbb0754e5fa"


def _engine() -> PricingEngine:
    return PricingEngine.load(ROOT / "pricing")


def _price(engine, model, when, prompt, cached, out, writes=0):
    return engine.compute(model_key=model, occurred_at=when, input_tokens=prompt, cached_input_tokens=cached,
                          cache_write_tokens=writes, output_tokens=out)


# Pricing ---------------------------------------------------------------------

def test_deepseek_flash_bills_double_in_published_peak_hours() -> None:
    engine = _engine()
    # 7,511 fresh + 6,144 cached + 80 output: OpenCode recorded $0.001193082
    # for this step at 04:47 UTC, an off-peak hour.
    step = {"prompt": 13_655, "cached": 6_144, "out": 80}
    for model in ("opencode-go:deepseek-v4.1-flash", "opencode:deepseek-flash"):
        assert _price(engine, model, MONDAY.replace(hour=4, minute=47), **step) == Decimal("0.001193082")
        assert _price(engine, model, MONDAY.replace(hour=7), **step) == Decimal("0.002386164")
        assert _price(engine, model, MONDAY.replace(hour=3, minute=59), **step) == Decimal("0.002386164")
        assert _price(engine, model, MONDAY.replace(hour=10), **step) == Decimal("0.001193082")
        saturday = MONDAY + timedelta(days=5)
        assert _price(engine, model, saturday.replace(hour=7), **step) == Decimal("0.001193082")


@pytest.mark.parametrize("fields, message", [
    ({"peak_hours_utc": [[1, 4]]}, "peak_weekdays"),
    ({"peak_hours_utc": [[4, 1]], "peak_weekdays": ["mon"], "peak_multiplier": 2}, "start < end"),
    ({"peak_hours_utc": [[1, 4]], "peak_weekdays": ["someday"], "peak_multiplier": 2}, "weekday names"),
    ({"peak_multiplier": 2}, "need peak_hours_utc"),
])
def test_peak_fields_are_validated(tmp_path: Path, fields: dict, message: str) -> None:
    card = {"provider": "deepseek", "exactness": "derived", "prices": [{
        "model_key": "deepseek:test", "input_per_mtok": 1, "cached_input_per_mtok": 0.1, "cache_write_per_mtok": 0,
        "output_per_mtok": 2, "effective_from": "2026-10-01T00:00:00Z",
        "source_url": "https://api-docs.deepseek.com/quick_start/pricing/", **fields}]}
    (tmp_path / "deepseek.yaml").write_text(json.dumps(card), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        PricingEngine.load(tmp_path)


def test_gpt_6_sol_is_priced_with_its_long_context_tier() -> None:
    engine = _engine()
    when = MONDAY.replace(hour=5)
    # $2 input, $0.20 cached, $10 output; over 272K input: 2x input, 1.5x output.
    assert _price(engine, "gpt-6-sol", when, 200_000, 190_000, 1_000) == Decimal("0.068")
    assert _price(engine, "gpt-6-sol", when, 300_000, 290_000, 1_000) == Decimal("0.171")


def test_gpt_61_terra_is_priced_from_its_model_page() -> None:
    engine = _engine()
    when = MONDAY.replace(hour=20)
    # $1 input, $0.05 cached, $1.25 cache write, $5 output; over 272K input 2x / 1.5x.
    assert _price(engine, "gpt-6.1-terra", when, 200_000, 190_000, 2_000, writes=1_000) == Decimal("0.03075")
    assert _price(engine, "gpt-6.1-terra", when, 300_000, 290_000, 2_000) == Decimal("0.064")


def test_grok_47_cards_reproduce_grok_builds_own_charges() -> None:
    engine = _engine()
    when = MONDAY.replace(hour=4, minute=59)
    # One-call turn recorded with costUsdTicks 2,185,040,000: long-context tier.
    assert _price(engine, "supergrok:grok-4.7-build", when, 203_612, 202_880, 1_058) == Decimal("0.218504")
    assert _price(engine, "supergrok:grok-4.7", when, 100_000, 90_000, 1_000) == Decimal("0.071")
    # Grok 4.7 Fast is twice Grok 4.7 below 200k prompt tokens.
    assert _price(engine, "supergrok:grok-4.7-build-fast", when, 28_000, 1_664, 1_000) == Decimal("0.119008")


# Grok turns and calls ----------------------------------------------------------

def _usage_record(sid: str, end: datetime, event: str, calls: list[tuple[int, int, int]], ticks: int | None,
                  model: str = "grok-4.7-build") -> dict:
    prompt = sum(call[0] for call in calls)
    cached = sum(call[1] for call in calls)
    out = sum(call[2] for call in calls)
    usage = {"inputTokens": prompt, "outputTokens": out, "totalTokens": prompt + out, "cachedReadTokens": cached,
             "cacheCreationTokens": 0, "reasoningTokens": 0, "modelCalls": len(calls)}
    if ticks is not None:
        usage["costUsdTicks"] = ticks
    usage["modelUsage"] = {model: dict(usage)}
    return {"method": "session/update", "params": {
        "sessionId": sid, "_meta": {"agentTimestampMs": int(end.timestamp() * 1000), "eventId": event},
        "update": {"sessionUpdate": "turn_completed", "usage": usage, "elapsed_ms": 60_000}}}


def test_a_turn_record_carries_grok_builds_charge() -> None:
    record = _usage_record(SID, MONDAY.replace(hour=4, minute=3), "e1",
                           [(1_730_029, 1_538_816, 27_833)], 13_188_320_000)
    rows, issues = grok_native.reduce_updates([record])
    assert not issues
    assert rows[0].cost_usd == pytest.approx(1.318832)
    assert rows[0].model_key == "supergrok:grok-4.7-build"


def _call(name: str, when: datetime, prompt: int, out: int) -> tuple:
    return (name, when, prompt, out)


def test_a_turn_covers_exactly_the_calls_that_add_up_to_it() -> None:
    t = MONDAY.replace(hour=13)
    calls = [
        _call("a1", t, 100, 10), _call("a2", t + timedelta(minutes=1), 200, 20),          # turn A
        _call("b1", t + timedelta(minutes=30), 400, 40),                                   # interrupted, no record
        _call("b2", t + timedelta(minutes=31), 500, 50),
        _call("c1", t + timedelta(minutes=60), 300, 30), _call("c2", t + timedelta(minutes=61), 600, 60),  # turn C
        _call("d1", t + timedelta(minutes=90), 700, 70),                                   # still running
    ]
    turns = [("A", t + timedelta(minutes=2), 300, 30), ("C", t + timedelta(minutes=62), 900, 90)]
    assert grok_native.covered_calls(calls, turns) == {"a1", "a2", "c1", "c2"}
    assert grok_native.covered_calls(calls, []) == set()


def test_ingest_keeps_each_turn_once_and_the_calls_no_turn_counts(tmp_path: Path) -> None:
    root = tmp_path / ".grok"
    (root / "logs").mkdir(parents=True)
    start = MONDAY.replace(hour=5)
    calls = [(100_000, 90_000, 1_000), (120_000, 110_000, 2_000)]

    def line(when: datetime, msg: str, ctx: dict) -> str:
        return json.dumps({"ts": when.isoformat().replace("+00:00", "Z"), "src": "shell", "pid": 1, "lvl": "info",
                           "msg": msg, "ctx": ctx, "sid": SID})

    def inference(when, prompt, cached, out) -> str:
        return line(when, "shell.turn.inference_done", {"loop_index": 1, "model_elapsed_ms": 10, "attempts": 1,
                                                         "prompt_tokens": prompt, "cached_prompt_tokens": cached,
                                                         "completion_tokens": out})

    log = [line(start, "model changed", {"model": "grok-4.7"})]
    log += [inference(start + timedelta(seconds=10 * i), *call) for i, call in enumerate(calls, 1)]
    log.append(inference(start + timedelta(minutes=10), 50_000, 40_000, 500))  # a turn that never completed
    (root / "logs" / "unified.jsonl").write_text("\n".join(log) + "\n", encoding="utf-8")
    session = root / "sessions" / "project" / SID
    session.mkdir(parents=True)
    # Grok Build's charge: each call on its own tier, $0.071 + $0.087.
    turn = _usage_record(SID, start + timedelta(seconds=25), "e1", calls, 1_580_000_000)
    (session / "updates.jsonl").write_text(json.dumps(turn) + "\n", encoding="utf-8")

    database = tmp_path / "spend.db"
    initialize(database)
    grok_local.reset_state()
    grok_local.ingest(database_path=database, pricing=_engine(), log_path=root)
    grok_local.reset_state()
    with connect(database) as connection:
        stored = [dict(row) for table in ("usage_events", "unpriced_usage_events")
                  for row in connection.execute(f"SELECT * FROM {table} WHERE tool_key='grok'")]
        parts = aggregate._tracked_value(connection, _engine(), start - timedelta(hours=1), start + timedelta(hours=1),
                                         "all", "UTC")
    kinds = sorted(row["raw_id"].split(":")[0] for row in stored)
    assert kinds == ["grok-local", "grok-native-usage"]
    assert parts.tokens == 223_000 + 50_500
    # The turn at its own charge (not $0.316, the summed prompt on the long tier)
    # plus the interrupted call at the card.
    assert parts.usage_value == Decimal("0.158") + Decimal("0.043")
    assert parts.complete


def _stored(raw_id: str, sid: str, when: datetime, prompt: int, out: int, model="supergrok:grok-4.7") -> UnpricedUsageEvent:
    return UnpricedUsageEvent(source="grok_local", tool_key="grok", model_key=model,
                              occurred_at=when.isoformat().replace("+00:00", "Z"), session_id=sid, project=None,
                              input_tokens=prompt, cached_input_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0,
                              output_tokens=out, reasoning_tokens=None, unclassified_tokens=0, telemetry_complete=True,
                              cost_usd=None, raw_id=raw_id, ingested_at="2026-10-26T06:00:00Z")


def test_the_one_time_repair_dedupes_a_hub_and_keeps_a_backup(tmp_path: Path) -> None:
    database = tmp_path / "hub" / "spend.db"
    database.parent.mkdir()
    initialize(database)
    t = MONDAY.replace(hour=6)
    sid = "m:laptop-1:" + SID
    with connect(database) as connection:
        connection.execute("DELETE FROM app_meta WHERE key=?", ("repair.grok_call_dedupe.v1",))
        for event in (_stored("m:laptop-1:grok-local:1", sid, t, 100, 10),
                      _stored("m:laptop-1:grok-local:2", sid, t + timedelta(seconds=5), 200, 20),
                      _stored("m:laptop-1:grok-native-usage:1", sid, t + timedelta(seconds=6), 300, 30,
                              model="supergrok:grok-4.7-build")):
            upsert_unpriced_event(connection, event)
    from spend_app import db

    db._GROK_DEDUPE_CHECKED.clear()
    assert dedupe_grok_calls(database) == 2
    assert dedupe_grok_calls(database) == 0
    with connect(database) as connection:
        left = [row[0] for row in connection.execute("SELECT raw_id FROM unpriced_usage_events")]
    assert left == ["m:laptop-1:grok-native-usage:1"]
    assert list((database.parent / "backups").glob("pre-grok-dedupe-*.db"))


def test_a_hub_drops_calls_an_older_computer_pushes_with_their_turn(tmp_path: Path) -> None:
    from spend_app.sync import apply_sync_push

    database = tmp_path / "spend.db"
    initialize(database)
    t = MONDAY.replace(hour=6)

    def item(raw_id, when, prompt, out, model="supergrok:grok-4.7"):
        return {"source": "grok_local", "tool_key": "grok", "model_key": model, "raw_id": raw_id, "session_id": SID,
                "occurred_at": when.isoformat().replace("+00:00", "Z"), "input_tokens": prompt, "output_tokens": out}

    apply_sync_push(database, _engine(), machine_id="laptop-1", body={"unpricedEvents": [
        item("grok-local:1", t, 100, 10), item("grok-local:2", t + timedelta(seconds=5), 200, 20)]})
    apply_sync_push(database, _engine(), machine_id="laptop-1", body={"unpricedEvents": [
        item("grok-native-usage:1", t + timedelta(seconds=6), 300, 30, model="supergrok:grok-4.7-build")]})
    with connect(database) as connection:
        left = [row[0] for row in connection.execute("SELECT raw_id FROM unpriced_usage_events WHERE tool_key='grok'")]
    assert left == ["m:laptop-1:grok-native-usage:1"]


# The value rule -----------------------------------------------------------------

def _event(**fields) -> dict:
    base = {"source": "opencode_local", "tool_key": "opencode-go", "model_key": "opencode-go:deepseek-v4.1-flash",
            "occurred_at": "2026-10-26T07:00:00Z", "input_tokens": 13_655, "cached_input_tokens": 6_144,
            "cache_write_tokens": 0, "cache_write_1h_tokens": 0, "output_tokens": 80, "cost_usd": 0.001193082,
            "raw_id": "opencode-message:1", "session_id": "s", "telemetry_complete": True}
    return {**base, **fields}


def test_a_tools_own_estimate_never_outranks_the_published_card() -> None:
    engine = _engine()
    peak_step = aggregate._enrich([_event()], engine, _cache=False)[0]
    assert peak_step["spend"] == Decimal("0.002386164")  # OpenCode's off-peak estimate was 0.001193082
    no_card = aggregate._enrich([_event(model_key="opencode:some-new-model")], engine, _cache=False)[0]
    assert no_card["spend"] == Decimal("0.001193082")
    grok_turn = aggregate._enrich([_event(source="grok_local", tool_key="grok", model_key="supergrok:grok-4.7-build",
                                          input_tokens=1_730_029, cached_input_tokens=1_538_816, output_tokens=27_833,
                                          cost_usd=1.318832)], engine, _cache=False)[0]
    assert grok_turn["spend"] == Decimal("1.318832")  # a provider's charge keeps precedence


def test_the_hud_says_how_much_of_today_has_no_price(tmp_path: Path) -> None:
    from spend_app.hud_data import recorded_usage

    database = tmp_path / "spend.db"
    initialize(database)
    t = MONDAY.replace(hour=15)
    with connect(database) as connection:
        upsert_unpriced_event(connection, replace(_stored("x:1", "s", t, 1_000, 100), model_key="opencode:mystery",
                                                  source="opencode_local", tool_key="opencode"))
    settings = Settings(database_path=database, pricing_path=ROOT / "pricing", anthropic_admin_key=None,
                        openai_admin_key=None, timezone="UTC", cache_hit_threshold=0.75,
                        over_routing_token_ceiling=40000)
    nav = recorded_usage(settings, _engine(), now=t + timedelta(hours=1))["navigation"]
    assert nav["todayComplete"] is False
    assert nav["todayUnpricedTokens"] == 1_100
    assert nav["todayUnpricedModels"] == ["opencode:mystery"]
