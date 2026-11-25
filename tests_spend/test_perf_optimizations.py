"""Regression and correctness tests for incremental ingest & query optimizations."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app import aggregate
from spend_app.adapters import claude_local, opencode_granular, traycer_local, unchanged
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]


def _claude_turn(sid: str, req_id: str, msg_id: str, stamp: str, inp: int = 1000, cached: int = 400, out: int = 150) -> str:
    return json.dumps({
        "type": "assistant",
        "sessionId": sid,
        "requestId": req_id,
        "uuid": f"env-{req_id}",
        "timestamp": stamp,
        "cwd": "C:/Dev/sample",
        "message": {
            "id": msg_id,
            "model": "claude-sonnet-4-6",
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": inp,
                "cache_read_input_tokens": cached,
                "cache_creation_input_tokens": 0,
                "output_tokens": out,
            },
        },
    }) + "\n"


def test_claude_incremental_tail_matches_full_ingest_and_handles_rewrite(tmp_path, monkeypatch):
    pricing = PricingEngine.load(ROOT / "pricing")
    transcript = tmp_path / "sess-tail.jsonl"
    transcript.write_text(
        _claude_turn("sess-tail", "req-1", "msg-1", "2026-11-25T10:00:00Z", inp=500, cached=100, out=50),
        encoding="utf-8",
    )

    db_inc = tmp_path / "inc.db"
    db_full = tmp_path / "full.db"
    claude_local.reset_file_cache()
    monkeypatch.setattr(claude_local, "_TAIL_MIN_BYTES", 0)

    r1 = claude_local.ingest(database_path=db_inc, pricing=pricing, session_glob=str(transcript))
    assert r1["status"] == "success" and r1["eventsWritten"] == 1

    # Append a second turn — incremental tail should read & write only the 1 new event.
    with transcript.open("a", encoding="utf-8") as handle:
        handle.write(_claude_turn("sess-tail", "req-2", "msg-2", "2026-11-25T10:05:00Z", inp=800, cached=200, out=90))

    r2 = claude_local.ingest(database_path=db_inc, pricing=pricing, session_glob=str(transcript))
    assert r2["status"] == "success"
    assert r2["eventsWritten"] == 1
    assert r2["eventsSeen"] == 2

    # Compare against a cold full-file ingest into a separate database.
    claude_local.reset_file_cache()
    claude_local.ingest(database_path=db_full, pricing=pricing, session_glob=str(transcript))

    with connect(db_inc) as c_inc, connect(db_full) as c_full:
        cols = "raw_id, session_id, tool_key, model_key, input_tokens, cached_input_tokens, output_tokens"
        rows_inc = [tuple(r) for r in c_inc.execute(f"SELECT {cols} FROM usage_events ORDER BY raw_id")]
        rows_full = [tuple(r) for r in c_full.execute(f"SELECT {cols} FROM usage_events ORDER BY raw_id")]
    assert rows_inc == rows_full and len(rows_inc) == 2

    # Rewriting the prefix invalidates the tail state cleanly and falls back to full parse.
    transcript.write_text(
        _claude_turn("sess-tail", "req-3", "msg-3", "2026-11-25T10:10:00Z", inp=900, cached=300, out=100),
        encoding="utf-8",
    )
    r3 = claude_local.ingest(database_path=db_inc, pricing=pricing, session_glob=str(transcript))
    assert r3["status"] == "success" and r3["eventsWritten"] == 1


def _make_opencode_db(path: Path, rows: list[tuple[str, str, int, dict]]) -> None:
    con = sqlite3.connect(path)
    con.execute(
        "CREATE TABLE session_message ("
        "id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER, "
        "time_created INTEGER, time_updated INTEGER, data TEXT)"
    )
    for idx, (msg_id, sid, updated_ms, payload) in enumerate(rows, 1):
        con.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (msg_id, sid, "assistant", idx, updated_ms, updated_ms, json.dumps(payload)),
        )
    con.commit()
    con.close()


def test_opencode_watermark_fast_path_reads_only_changed_sessions(tmp_path, monkeypatch):
    pricing = PricingEngine.load(ROOT / "pricing")
    oc_db = tmp_path / "opencode.db"
    base_ms = 1_795_500_000_000

    def msg_payload(mid: str, sid: str, ts: int, out_toks: int) -> dict:
        return {
            "id": mid,
            "sessionID": sid,
            "role": "assistant",
            "providerID": "openai",
            "modelID": "gpt-5.4",
            "time": {"created": ts, "completed": ts + 100},
            "tokens": {"input": 500, "output": out_toks, "cache": {"read": 100, "write": 0}},
        }

    _make_opencode_db(
        oc_db,
        [
            ("m1", "s1", base_ms, msg_payload("m1", "s1", base_ms, 50)),
            ("m2", "s2", base_ms + 1000, msg_payload("m2", "s2", base_ms + 1000, 60)),
        ],
    )

    spend_db = tmp_path / "spend.db"
    unchanged.reset_state()
    monkeypatch.setattr(opencode_granular, "WATERMARK_MIN_ROWS", 1)

    r1 = opencode_granular.ingest(database_path=spend_db, pricing=pricing, source_database=oc_db)
    assert r1["status"] == "success" and r1["eventsWritten"] == 2

    # Add a new message ONLY to session s2.
    con = sqlite3.connect(oc_db)
    con.execute(
        "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
        ("m3", "s2", "assistant", 3, base_ms + 5000, base_ms + 5000, json.dumps(msg_payload("m3", "s2", base_ms + 5000, 75))),
    )
    con.commit()
    con.close()

    queried_sessions: list[list[str] | None] = []
    real_read_messages = opencode_granular.read_messages

    def spy_read_messages(connection, columns, limit=None, session_ids=None):
        queried_sessions.append(list(session_ids) if session_ids is not None else None)
        return real_read_messages(connection, columns, limit=limit, session_ids=session_ids)

    monkeypatch.setattr(opencode_granular, "read_messages", spy_read_messages)
    r2 = opencode_granular.ingest(database_path=spend_db, pricing=pricing, source_database=oc_db)
    assert r2["status"] == "success"
    assert queried_sessions == [["s2"]]
    assert r2["eventsWritten"] == 1
    assert r2["sessionsSkippedUnchanged"] == 1


def test_traycer_unchanged_replay_and_native_duplicate_invalidation(tmp_path):
    pricing = PricingEngine.load(ROOT / "pricing")
    facts_db = tmp_path / "app-assets.db"
    fcon = sqlite3.connect(facts_db)
    fcon.execute(
        """
        CREATE TABLE usage_analytics_facts (
            fact_id TEXT, occurred_at INTEGER, chat_id TEXT, harness_id TEXT, model TEXT,
            usage_completeness TEXT, uncached_input_tokens INTEGER, cache_read_input_tokens INTEGER,
            cache_creation_tokens INTEGER, output_tokens INTEGER, reasoning_tokens INTEGER,
            cost_usd REAL, cost_provenance TEXT, tui_session_id TEXT
        )
        """
    )
    fcon.execute(
        "INSERT INTO usage_analytics_facts VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        ("f1", 1_795_500_000_000, "chat1", "claude", "claude-sonnet-4-6", "measured", 1000, 200, 0, 150, 0, 0.0, "estimated", "native-sess-1"),
    )
    fcon.commit()
    fcon.close()

    spend_db = tmp_path / "spend.db"
    unchanged.reset_state()
    r1 = traycer_local.ingest(database_path=spend_db, pricing=pricing, database_glob=str(facts_db))
    assert r1["status"] == "success" and r1["eventsWritten"] == 1

    # Unchanged second pass replays cheaply without re-ingesting.
    r2 = traycer_local.ingest(database_path=spend_db, pricing=pricing, database_glob=str(facts_db))
    assert r2.get("unchanged") is True and r2["eventsWritten"] == 0

    # When a native claude_local row arrives for the same session, Traycer must detect it and drop its duplicate.
    with connect(spend_db) as con:
        con.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, occurred_epoch_us, session_id,
                input_tokens, cached_input_tokens, cache_write_tokens, output_tokens,
                computed_cost_usd, is_exact, raw_id, ingested_at
            ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                "claude_local", "claude-code", "claude-sonnet-4-6",
                "2026-11-25T10:00:00Z", 1_795_500_000_000_000, "native-sess-1",
                1200, 200, 0, 150, 0.01, 1, "claude-native-1", "2026-11-25T10:00:01Z",
            ),
        )
    r3 = traycer_local.ingest(database_path=spend_db, pricing=pricing, database_glob=str(facts_db))
    assert not r3.get("unchanged")
    with connect(spend_db) as con:
        sources = [r[0] for r in con.execute("SELECT source FROM usage_events WHERE session_id='native-sess-1'")]
    assert sources == ["claude_local"]


def test_latest_quota_rows_handles_large_history_identically(tmp_path):
    db = tmp_path / "spend.db"
    initialize(db)
    base = datetime(2026, 11, 25, 10, 0, 0, tzinfo=UTC)
    with connect(db) as con:
        for i in range(250):
            ts = (base + timedelta(seconds=i * 15)).isoformat().replace("+00:00", "Z")
            con.execute(
                """
                INSERT INTO quotas(provider_key, limit_key, label, pct, used, allowance, unit, resets_at, polled_at, source, is_payg)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)
                """,
                ("claude-code", "5h", "5h window", float(i % 100), None, None, "percent", "2026-11-25T16:00:00Z", ts, "claude_code_cache", 0),
            )
    aggregate.reset_memo()
    with connect(db) as con:
        rows = aggregate._load_quota_rows(con, now=base + timedelta(hours=2))
    assert len(rows) == 1
    assert rows[0]["pct"] == float(249 % 100)
