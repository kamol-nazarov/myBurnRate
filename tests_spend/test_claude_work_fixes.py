"""Regression tests for Claude Work profile status and Traycer session deduplication."""

from __future__ import annotations

import json
from pathlib import Path

from spend_app.adapters import profiles_local, traycer_local
from spend_app.connections import Store
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.pricing import PricingEngine
from spend_app.profile_store import load as load_profiles


def _seed_claude_work_event(db: Path, session_id: str, *, source: str = "claude_local", tool_key: str = "claude-code-work", raw_id: str = "cw-1") -> None:
    initialize(db)
    with connect(db) as conn:
        upsert_usage_event(
            conn,
            UsageEvent(
                raw_id=raw_id,
                source=source,
                tool_key=tool_key,
                model_key="claude-opus-5-5",
                occurred_at="2026-11-27T12:00:00Z",
                ingested_at="2026-11-27T12:00:01Z",
                session_id=session_id,
                project="VehicleDesk",
                input_tokens=100,
                cached_input_tokens=50,
                cache_write_tokens=0,
                cache_write_1h_tokens=0,
                output_tokens=20,
                reasoning_tokens=None,
                cost_usd=None,
                computed_cost_usd=0.05,
            ),
        )


def test_claude_work_profile_marks_receiving_usage_when_sessions_split_by_lane(tmp_path: Path):
    db = tmp_path / "spend.db"
    work_dir = tmp_path / ".claude-work"
    work_dir.mkdir()
    (work_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "work-uuid-1"}}),
        encoding="utf-8",
    )
    _seed_claude_work_event(db, "15b5b0bc-ffe7-4b1d-8bf1-2d177cab2120")

    store = Store(db)
    with store.transaction() as state:
        state["profiles"].setdefault("claude_local", {})["claude-code-work"] = {
            "source": "claude_local",
            "bindingId": "claude_local:claude-code-work",
            "name": "Work",
            "location": str(work_dir),
            "origin": "detected",
            "mode": "detected",
            "enabled": True,
            "revision": 1,
            "state": "waiting_activity",
            "detail": "Readable; waiting for usage.",
            "capabilities": ["usage"],
            "toolKey": "claude-code-work",
        }

    result = profiles_local.ingest(database_path=db, pricing=PricingEngine([]))
    assert result["status"] in {"success", "partial"}

    with connect(db) as conn:
        loaded = load_profiles(conn)
    prof = loaded["profiles"]["claude_local"]["claude-code-work"]
    assert prof["state"] == "receiving_usage"


def test_traycer_covered_sessions_and_dedup_recognize_claude_profile_lanes(tmp_path: Path):
    db = tmp_path / "spend.db"
    sid = "15b5b0bc-ffe7-4b1d-8bf1-2d177cab2120"
    _seed_claude_work_event(db, sid, source="claude_local", tool_key="claude-code-work", raw_id="native-work")
    _seed_claude_work_event(db, sid, source="traycer_local", tool_key="claude-code", raw_id="traycer-dup")

    covered = traycer_local.covered_sessions(db, {sid})
    assert ("claude-code", sid) in covered

    with connect(db) as conn:
        traycer_local.drop_native_duplicates(conn)
        rows = conn.execute("SELECT raw_id, source, tool_key FROM usage_events ORDER BY raw_id").fetchall()
    assert [(r["raw_id"], r["source"], r["tool_key"]) for r in rows] == [
        ("native-work", "claude_local", "claude-code-work")
    ]
