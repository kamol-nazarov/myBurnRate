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
    assert rows[0].grok_turn_elapsed_ms == 60_000


def _call(name: str, when: datetime, prompt: int, out: int, elapsed: int | None = None) -> grok_native.GrokUsage:
    return grok_native.GrokUsage(name, when, (prompt, 0, 0, out), elapsed)


def test_a_turn_covers_exactly_the_calls_that_add_up_to_it() -> None:
    t = MONDAY.replace(hour=13)
    calls = [
        _call("a1", t, 100, 10), _call("a2", t + timedelta(minutes=1), 200, 20),          # turn A
        _call("b1", t + timedelta(minutes=30), 400, 40),                                   # interrupted, no record
        _call("b2", t + timedelta(minutes=31), 500, 50),
        _call("c1", t + timedelta(minutes=60), 300, 30), _call("c2", t + timedelta(minutes=61), 600, 60),  # turn C
        _call("d1", t + timedelta(minutes=90), 700, 70),                                   # still running
    ]
    turns = [_call("A", t + timedelta(minutes=2), 300, 30, 120_000),
             _call("C", t + timedelta(minutes=62), 900, 90, 120_000)]
    assert grok_native.covered_calls(calls, turns) == {"a1", "a2", "c1", "c2"}
    assert grok_native.covered_calls(calls, []) == set()


def test_overlapping_grok_turn_intervals_do_not_claim_the_same_call() -> None:
    end = MONDAY.replace(hour=13)
    calls = [_call("first", end - timedelta(seconds=15), 100, 10),
             _call("second", end + timedelta(seconds=5), 200, 20)]
    turns = [_call("A", end, 100, 10, 60_000),
             _call("B", end + timedelta(seconds=10), 300, 30, 60_000)]
    assert grok_native.covered_calls(calls, turns) == set()


def test_grok_turn_must_match_all_observed_components_and_calls() -> None:
    end = MONDAY.replace(hour=13)
    calls = [_call("A", end - timedelta(seconds=15), 100, 10),
             _call("B", end - timedelta(seconds=5), 200, 20)]
    assert not grok_native.covered_calls(calls, [_call("partial", end, 200, 20, 60_000)])
    different_cache = grok_native.GrokUsage("cache_diff", end, (300, 30, 0, 30), 60_000)
    assert not grok_native.covered_calls(calls, [different_cache])


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


def _stored(
    raw_id: str, sid: str, when: datetime, prompt: int, out: int,
    model="supergrok:grok-4.7", elapsed: int | None = None,
) -> UnpricedUsageEvent:
    return UnpricedUsageEvent(source="grok_local", tool_key="grok", model_key=model,
                              occurred_at=when.isoformat().replace("+00:00", "Z"), session_id=sid, project=None,
                              input_tokens=prompt, cached_input_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0,
                              output_tokens=out, reasoning_tokens=None, unclassified_tokens=0, telemetry_complete=True,
                              cost_usd=None, raw_id=raw_id, ingested_at="2026-10-26T06:00:00Z",
                              grok_turn_elapsed_ms=elapsed)


@pytest.mark.parametrize("completed_prompt,completed_output", [(1000, 150), (100, 10)])
def test_later_grok_turn_never_consumes_a_retained_interrupted_call(
    tmp_path: Path, completed_prompt: int, completed_output: int
) -> None:
    from spend_app.adapters import unchanged
    from spend_app.adapters.usage_repair import delete_event
    from spend_app.sync import apply_sync_push, export_sync_batch

    root = tmp_path / ".grok"
    log = root / "logs" / "unified.jsonl"
    log.parent.mkdir(parents=True)
    at = MONDAY.replace(hour=10)
    log.write_text(
        _grok_line(at, "model changed", {"model": "grok-4.7"}) + "\n"
        + _grok_call(at + timedelta(seconds=10), 100, 0, 10) + "\n",
        encoding="utf-8",
    )
    spoke, hub = tmp_path / "spoke.db", tmp_path / "hub.db"
    pricing = _engine()
    grok_local.ingest(database_path=spoke, pricing=pricing, log_path=root)
    with connect(spoke) as connection:
        call_id = connection.execute("SELECT raw_id FROM usage_events WHERE tool_key='grok'").fetchone()[0]
    updates = root / "sessions" / "project" / SID / "updates.jsonl"
    updates.parent.mkdir(parents=True)
    end = at + timedelta(hours=3)
    updates.write_text(
        json.dumps(_usage_record(SID, end, "e1", [(completed_prompt, 0, completed_output)], 1_000))
        + "\n",
        encoding="utf-8",
    )
    grok_local.ingest(database_path=spoke, pricing=pricing, log_path=root)
    # Re-create the historical missing-row state, then replay the retained source.
    with connect(spoke) as connection:
        delete_event(connection, call_id)
    batch, cursor = export_sync_batch(spoke, cursor=None, limit=1000)
    apply_sync_push(hub, pricing, machine_id="laptop-1", body=batch)
    with connect(spoke) as connection:
        connection.execute("ALTER TABLE usage_events DROP COLUMN grok_turn_elapsed_ms")
        connection.execute("ALTER TABLE unpriced_usage_events DROP COLUMN grok_turn_elapsed_ms")
        connection.execute("UPDATE app_meta SET value='12' WHERE key='schema_version'")
    grok_local.reset_state()
    unchanged.reset_state()
    grok_local.ingest(database_path=spoke, pricing=pricing, log_path=root)
    for _ in range(2):
        batch, cursor = export_sync_batch(spoke, cursor=cursor, limit=1000)
        apply_sync_push(hub, pricing, machine_id="laptop-1", body=batch)
    for database in (spoke, hub):
        with connect(database) as connection:
            parts = aggregate._tracked_value(
                connection, pricing, at, end + timedelta(seconds=1), "all", "UTC"
            )
            elapsed = connection.execute(
                "SELECT grok_turn_elapsed_ms FROM usage_events WHERE raw_id LIKE '%grok-native-usage:%'"
            ).fetchone()[0]
        assert elapsed == 60_000
        assert parts.tokens == 110 + completed_prompt + completed_output
        assert parts.records == 2


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
                              model="supergrok:grok-4.7-build", elapsed=60_000)):
            upsert_unpriced_event(connection, event)
    from spend_app import db

    db._GROK_DEDUPE_CHECKED.clear()
    assert dedupe_grok_calls(database) == 2
    assert dedupe_grok_calls(database) == 0
    with connect(database) as connection:
        left = [row[0] for row in connection.execute("SELECT raw_id FROM unpriced_usage_events")]
    assert left == ["m:laptop-1:grok-native-usage:1"]
    assert list((database.parent / "backups").glob("pre-grok-dedupe-*.db"))


@pytest.mark.parametrize("elapsed", [60_000, None, -100, True, 10**27])
def test_a_hub_drops_calls_only_with_a_proven_turn_interval(tmp_path: Path, elapsed: int | None) -> None:
    from spend_app.sync import apply_sync_push

    database = tmp_path / "spend.db"
    initialize(database)
    t = MONDAY.replace(hour=6)

    def item(raw_id, when, prompt, out, model="supergrok:grok-4.7"):
        return {"source": "grok_local", "tool_key": "grok", "model_key": model, "raw_id": raw_id, "session_id": SID,
                "occurred_at": when.isoformat().replace("+00:00", "Z"), "input_tokens": prompt, "output_tokens": out}

    apply_sync_push(database, _engine(), machine_id="laptop-1", body={"unpricedEvents": [
        item("grok-local:1", t, 100, 10), item("grok-local:2", t + timedelta(seconds=5), 200, 20)]})
    turn = {"unpricedEvents": [
        {**item("grok-native-usage:1", t + timedelta(seconds=6), 300, 30, model="supergrok:grok-4.7-build"),
         "grok_turn_elapsed_ms": elapsed}]}
    invalid = elapsed is not None and (type(elapsed) is not int or elapsed < 0 or elapsed > 10**12)
    if invalid:
        with pytest.raises(ValueError, match="Invalid sync usage count"):
            apply_sync_push(database, _engine(), machine_id="laptop-1", body=turn)
    else:
        apply_sync_push(database, _engine(), machine_id="laptop-1", body=turn)
    with connect(database) as connection:
        left = [row[0] for row in connection.execute("SELECT raw_id FROM unpriced_usage_events WHERE tool_key='grok'")]
    expected = set() if invalid else {"m:laptop-1:grok-native-usage:1"}
    if elapsed != 60_000:
        expected.update({"m:laptop-1:grok-local:1", "m:laptop-1:grok-local:2"})
    assert set(left) == expected


def test_grok_turn_boundary_survives_a_late_legacy_payload(tmp_path: Path) -> None:
    from spend_app.sync import apply_sync_push

    database = tmp_path / "hub.db"
    t = MONDAY.replace(hour=6)
    pricing = _engine()
    call = {
        "source": "grok_local", "tool_key": "grok", "model_key": "supergrok:grok-4.7",
        "session_id": SID, "raw_id": "grok-local:before-turn",
        "occurred_at": t.isoformat(), "input_tokens": 200, "output_tokens": 20,
    }
    turn = {**call, "raw_id": "grok-native-usage:turn", "occurred_at": (t + timedelta(seconds=30)).isoformat()}
    apply_sync_push(database, pricing, machine_id="laptop-1", body={
        "unpricedEvents": [{**turn, "grok_turn_elapsed_ms": 60_000}],
    })
    apply_sync_push(database, pricing, machine_id="laptop-1", body={"unpricedEvents": [call, turn]})
    with connect(database) as connection:
        assert [tuple(row) for row in connection.execute(
            "SELECT raw_id, grok_turn_elapsed_ms FROM unpriced_usage_events"
        )] == [("m:laptop-1:grok-native-usage:turn", 60_000)]


def test_grok_cleanup_keeps_calls_from_another_machine_with_the_same_session_id(tmp_path: Path) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    t = MONDAY.replace(hour=6)
    events = [
        _stored("m:home:grok-local:1", SID, t, 100, 10),
        _stored("m:work:grok-local:1", SID, t, 200, 20),
        _stored("m:work:grok-native-usage:1", SID, t + timedelta(seconds=10), 300, 30, elapsed=60_000),
    ]
    with connect(database) as connection:
        for event in events:
            upsert_unpriced_event(connection, event)
        assert grok_native.drop_covered_calls(connection, {f"m:work:{SID}"}) == 0
        upsert_unpriced_event(connection, replace(events[2], input_tokens=200, output_tokens=20))
        assert grok_native.drop_covered_calls(connection, {f"m:work:{SID}"}) == 1
        assert {row[0] for row in connection.execute("SELECT raw_id FROM unpriced_usage_events")} == {
            events[0].raw_id, events[2].raw_id,
        }


# Grok calls whose model line the CLI trimmed ------------------------------------

def _grok_line(when: datetime, msg: str, ctx: dict) -> str:
    return json.dumps({"ts": when.isoformat().replace("+00:00", "Z"), "src": "shell", "pid": 1, "lvl": "info",
                       "msg": msg, "ctx": ctx, "sid": SID})


def _grok_call(when: datetime, prompt: int, cached: int, out: int) -> str:
    return _grok_line(when, "shell.turn.inference_done", {"loop_index": 1, "model_elapsed_ms": 10, "attempts": 1,
                                                           "prompt_tokens": prompt, "cached_prompt_tokens": cached,
                                                           "completion_tokens": out})


def _grok_stored(database: Path) -> tuple[list[tuple], list[str]]:
    with connect(database) as connection:
        priced = [tuple(row) for row in connection.execute(
            "SELECT model_key, computed_cost_usd FROM usage_events WHERE tool_key='grok'")]
        unpriced = [row[0] for row in connection.execute(
            "SELECT model_key FROM unpriced_usage_events WHERE tool_key='grok'")]
    return priced, unpriced


def test_a_trimmed_log_keeps_the_model_a_call_was_stored_with(tmp_path: Path) -> None:
    # 2026-10-28: Grok cut the head of unified.jsonl, the next pass read its
    # calls again without their "model changed" line and moved them to unknown.
    root = tmp_path / ".grok"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "unified.jsonl"
    start = MONDAY.replace(hour=13)
    call = _grok_call(start + timedelta(seconds=10), 80_000, 70_000, 1_000)
    log.write_text(_grok_line(start, "model changed", {"model": "grok-4.7-build-fast"}) + "\n" + call + "\n",
                   encoding="utf-8")
    database = tmp_path / "spend.db"
    initialize(database)
    grok_local.reset_state()
    grok_local.ingest(database_path=database, pricing=_engine(), log_path=root)
    log.write_text(call + "\n", encoding="utf-8")
    grok_local.ingest(database_path=database, pricing=_engine(), log_path=root)
    grok_local.reset_state()
    priced, unpriced = _grok_stored(database)
    assert [model for model, _cost in priced] == ["supergrok:grok-4.7-build-fast"] and unpriced == []


def test_a_call_without_a_model_takes_the_one_its_session_records(tmp_path: Path) -> None:
    root = tmp_path / ".grok"
    (root / "logs").mkdir(parents=True)
    log = root / "logs" / "unified.jsonl"
    log.write_text(_grok_call(MONDAY.replace(hour=13), 80_000, 70_000, 1_000) + "\n", encoding="utf-8")
    database = tmp_path / "spend.db"
    initialize(database)
    grok_local.reset_state()
    grok_local.ingest(database_path=database, pricing=_engine(), log_path=root)
    assert _grok_stored(database) == ([], ["supergrok:unknown"])

    # The log has since dropped the call; the session's updates.jsonl names
    # the model selected for its prompts, and the stored call is priced.
    log.write_text("", encoding="utf-8")
    session = root / "sessions" / "project" / SID
    session.mkdir(parents=True)
    prompt = {"method": "session/update", "params": {"sessionId": SID, "update": {
        "sessionUpdate": "user_message_chunk", "_meta": {"modelId": "grok-4.7-build-fast"}}}}
    (session / "updates.jsonl").write_text(json.dumps(prompt) + "\n", encoding="utf-8")
    grok_local.ingest(database_path=database, pricing=_engine(), log_path=root)
    grok_local.reset_state()
    priced, unpriced = _grok_stored(database)
    assert unpriced == []
    assert priced == [("supergrok:grok-4.7-build-fast", pytest.approx(0.01 * 4 + 0.07 * 1 + 0.001 * 12))]


def test_turn_names_outrank_the_recorded_selection(tmp_path: Path) -> None:
    from spend_app.adapters.common import UsageRow

    database = tmp_path / "spend.db"
    initialize(database)
    t = MONDAY.replace(hour=6)
    call = UsageRow("grok_local", "grok", grok_native.UNKNOWN, t + timedelta(minutes=1), SID, None,
                    100, 0, 0, 0, 10, None, None, "grok-local:1")
    turn = replace(call, model_key="supergrok:grok-4.7-build", occurred_at=t, raw_id="grok-native-usage:1",
                   input_tokens=300, output_tokens=30)

    def models(incoming, recorded):
        with connect(database) as connection:
            rows = grok_native._reconcile_session(connection, SID, incoming, [], recorded=frozenset(recorded))
            return {row.raw_id: row.model_key for row in rows}

    # Grok Build records the selection as grok-4.7 and its turns as grok-4.7-build.
    assert models([call, turn], {"supergrok:grok-4.7"})["grok-local:1"] == "supergrok:grok-4.7-build"
    assert models([call], {"supergrok:grok-4.7"})["grok-local:1"] == "supergrok:grok-4.7"
    assert models([call], {"supergrok:grok-4.7", "supergrok:grok-4.6"})["grok-local:1"] == grok_native.UNKNOWN


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
