"""Grok Build (xAI grok CLI) local ingest, quota snapshot, and live sessions.

Usage comes from ``shell.turn.inference_done`` records in the CLI's unified
log; the quota from its ``billing: fetched credits config`` records; live
sessions from ``active_sessions.json``. No network call is involved anywhere.
"""

from __future__ import annotations

from spend_app.adapters.event_identity import Observation
import json
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import spend_app.limits as limits
from spend_app.adapters import grok_local
from spend_app.adapters import traycer_local
from spend_app.adapters.common import UsageRow
from spend_app.aggregate import aggregate_summary
from spend_app.db import connect, initialize
from spend_app.pricing import PricingEngine
from spend_app.quotas import agent_run_records, grok_quota_samples

ROOT = Path(__file__).resolve().parents[1]
SID = "01a05fb0-9944-79a3-b111-1a1572034fe8"
SUB = "01a05fb3-cf80-7581-a576-13862d2bb4f5"


def _line(ts: str, msg: str, ctx: dict, sid: str | None = SID) -> str:
    return json.dumps({"ts": ts, "src": "shell", "pid": 27876, "lvl": "info", "msg": msg, "ctx": ctx, "sid": sid})


def _turn(ts: str, sid: str, loop: int, prompt: int, cached: int, completion: int, reasoning: int | None = None) -> str:
    ctx = {"loop_index": loop, "model_elapsed_ms": 100, "attempts": 1, "prompt_tokens": prompt,
           "cached_prompt_tokens": cached, "completion_tokens": completion, "tokens_per_sec": 100.0}
    if reasoning is not None:
        ctx["reasoning_tokens"] = reasoning
    return _line(ts, "shell.turn.inference_done", ctx, sid)


def _billing(ts: str, used: float, end: str, tier: str = "SuperGrok Heavy") -> str:
    return _line(ts, "billing: fetched credits config", {
        "config": {"creditUsagePercent": used, "currentPeriod": {"type": "USAGE_PERIOD_TYPE_WEEKLY", "start": "2026-08-27T03:40:35.074896+00:00", "end": end},
                   "onDemandCap": {"val": 0}, "onDemandUsed": {"val": 0}, "prepaidBalance": {"val": 0}},
        "subscriptionTier": tier}, None)


def _write_log(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def test_turns_become_rows_with_model_and_project_from_session_lines(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [
        _line("2026-09-02T01:16:44.436Z", "model changed", {"model": "grok-4.6"}),
        _line("2026-09-02T01:16:49.908Z", "session created", {"cwd": "C:\\\\Dev\\\\ExampleProject"}),
        _turn("2026-09-02T01:25:05.000Z", SID, 1, 21598, 3328, 318, 155),
        _turn("2026-09-02T01:24:58.267Z", SUB, 16, 66945, 128, 30, 25),
        _line("2026-09-02T01:26:00.000Z", "shell.tool.exec_done", {"tool": "read_file"}),
        _turn("2026-09-02T01:27:00.000Z", SID, 2, 0, 0, 0),
    ])
    rows, state = grok_local.parse_log(log)
    assert [row.session_id for row in rows] == [SID, SUB], "zero-token turns are skipped"
    first, sub = rows
    assert first.model_key == "supergrok:grok-4.6" and first.tool_key == "grok" and first.source == "grok_local"
    assert first.project == "ExampleProject"
    assert (first.input_tokens, first.cached_input_tokens, first.output_tokens, first.reasoning_tokens) == (21598, 3328, 318, 155)
    assert first.cost_usd is None, "subscription usage carries no metered cost"
    assert sub.model_key == "supergrok:unknown", "a different session does not inherit an unrelated model"
    assert sub.project is None
    assert first.raw_id != sub.raw_id
    assert state["offset"] == log.stat().st_size


def test_turns_without_model_evidence_retain_unpriced_usage(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [_turn("2026-09-02T01:25:05.000Z", SID, 1, 100, 10, 5)])
    rows, _state = grok_local.parse_log(log)
    assert len(rows) == 1 and rows[0].model_key == "supergrok:unknown"
    assert rows[0].input_tokens == 100 and rows[0].output_tokens == 5


def test_reader_is_incremental_and_restarts_after_truncation(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [_line("2026-09-02T01:00:00Z", "model changed", {"model": "grok-4.6"}), _turn("2026-09-02T01:01:00Z", SID, 1, 100, 10, 5)])
    rows, state = grok_local.parse_log(log)
    assert len(rows) == 1
    with log.open("a", encoding="utf-8") as handle:
        handle.write(_turn("2026-09-02T01:02:00Z", SID, 2, 200, 20, 6) + "\n")
        handle.write('{"ts":"2026-09-02T01:03:00Z","msg":"shell.turn.inference_done","sid":"' + SID)  # partial line
    rows, state = grok_local.parse_log(log, state)
    assert [row.raw_id for row in rows] and len(rows) == 1, "only new complete lines are returned"
    second_offset = state["offset"]
    rows, state = grok_local.parse_log(log, state)
    assert rows == [] and state["offset"] == second_offset, "a partial trailing line waits"
    _write_log(
        log,
        [
            _line("2026-09-02T02:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T02:00:01Z", SID, 1, 50, 0, 5),
        ],
    )  # CLI truncated the log
    rows, state = grok_local.parse_log(log, state)
    assert len(rows) == 1 and rows[0].input_tokens == 50 and rows[0].model_key == "supergrok:grok-4.6"
    assert state["offset"] == log.stat().st_size


def test_ingest_persists_derived_rows_and_tolerates_a_missing_log(tmp_path: Path) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    missing = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    assert missing["status"] == "failed" and missing["eventsSeen"] == 0
    _write_log(log, [_line("2026-09-02T01:00:00Z", "model changed", {"model": "grok-4.6"}), _turn("2026-09-02T01:01:00Z", SID, 1, 100_000, 50_000, 10_000)])
    first = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    second = grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    assert first["rows"] == 1 and second["rows"] == 0
    with connect(database) as connection:
        stored = connection.execute("SELECT tool_key, model_key, cost_usd, computed_cost_usd FROM usage_events").fetchall()
    assert len(stored) == 1
    tool_key, model_key, cost, computed = stored[0]
    assert (tool_key, model_key, cost) == ("grok", "supergrok:grok-4.6", None)
    # xai:grok-4.6 published rates: $2/M input, $0.50/M cached, $6/M output (below the 200k long-context tier).
    assert abs(computed - (0.05 * 2.00 + 0.05 * 0.50 + 0.01 * 6.00)) < 1e-6
    summary = aggregate_summary(database_path=database, pricing=pricing, window_key="all", tool="all", timezone="UTC", cache_threshold=0.5, now=datetime(2026, 9, 2, 2, tzinfo=UTC))
    tool = next(item for item in summary["tools"] if item["key"] == "grok")
    assert tool["tokens"] == 110_000 and tool["isExact"] is False


def test_quota_comes_from_the_newest_local_billing_snapshot(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [
        _billing("2026-09-02T00:10:00Z", 40.0, "2026-09-03T03:40:35.074896+00:00"),
        _turn("2026-09-02T00:11:00Z", SID, 1, 10, 0, 1),
        _billing("2026-09-02T01:16:49.969Z", 53.0, "2026-09-03T03:40:35.074896+00:00"),
    ])
    now = datetime(2026, 9, 2, 1, 30, tzinfo=UTC)
    payload = limits._grok_limits_from_log(log, now=now)
    assert payload["status"] == "exact" and payload["source"] == "grok_local_billing" and payload["plan"] == "SuperGrok Heavy"
    window = payload["windows"][0]
    assert window["usedPct"] == 53.0 and window["resetAt"] == "2026-09-03T03:40:35.074896Z"
    assert "2026-09-02T01:16:49.969Z" in payload["detail"]
    samples = grok_quota_samples(payload, source=payload["source"])
    assert samples[0].pct == 53.0 and samples[0].resets_at == "2026-09-03T03:40:35Z" and samples[0].source == "grok_local_billing"
    stale = limits._grok_limits_from_log(log, now=datetime(2026, 9, 2, 1, 32, tzinfo=UTC))
    assert stale["status"] == "unavailable" and "older than 15 minutes" in stale["detail"]
    assert limits._grok_limits_from_log(tmp_path / "absent.jsonl")["status"] == "unavailable"

    def no_traycer(*_args, **_kwargs):
        raise AssertionError("Traycer fallback must not run when the local snapshot is current")

    monkeypatch.setattr(limits, "_traycer_profile_rate_limits", no_traycer)
    uncached = limits._grok_limits_uncached(log, now=now)
    assert uncached["source"] == "grok_local_billing", "the local snapshot wins over the Traycer path"


def test_stale_local_and_traycer_snapshots_leave_grok_quota_unavailable(tmp_path: Path, monkeypatch) -> None:
    log = tmp_path / "unified.jsonl"
    _write_log(log, [_billing("2026-09-02T01:00:00Z", 0.0, "2026-09-03T03:40:35.074896+00:00")])
    now = datetime(2026, 9, 2, 1, 16, tzinfo=UTC)
    monkeypatch.setattr(
        limits,
        "_traycer_profile_rate_limits",
        lambda _provider: {
            "rateLimits": {
                "available": True,
                "subscriptionTier": "SuperGrok Heavy",
                "period": {"usedPercent": 0.0, "resetsAt": int((now + timedelta(days=1)).timestamp() * 1000)},
            },
            "usageUpdatedAt": int((now - timedelta(minutes=16)).timestamp() * 1000),
        },
    )
    payload = limits._grok_limits_uncached(log, now=now)
    assert payload["status"] == "unavailable"
    assert "older than 15 minutes" in payload["detail"]


def test_live_sessions_need_a_running_pid_and_carry_title_and_model(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(json.dumps({"info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"}, "session_summary": "Demo session", "current_model_id": "grok-4.6"}), encoding="utf-8")
    os.utime(summary, (now.timestamp(), now.timestamp()))
    active = tmp_path / "active_sessions.json"
    active.write_text(json.dumps([
        {"session_id": SID, "pid": 111, "cwd": "C:\\\\Dev\\\\ExampleProject", "opened_at": "2026-09-02T01:16:49.909310900Z"},
        {"session_id": "dead", "pid": 222, "cwd": "C:\\\\Dev\\\\Other", "opened_at": "2026-09-02T01:00:00Z"},
    ]), encoding="utf-8")
    limits._GROK_SESSION_DIRS.clear()
    live = limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now)
    assert live == [{"sessionId": SID, "title": "Demo session", "model": "supergrok:grok-4.6", "startedAt": "2026-09-02T01:16:49.909310900Z"}]
    records = agent_run_records({"activeAgents": [], "unmeteredTurns": [], "grokSessions": live})
    assert [(r.id, r.name, r.model_key, r.state) for r in records] == [(f"grok:{SID}", "Demo session", "supergrok:grok-4.6", "live")]
    stale_stamp = (now - timedelta(minutes=6)).timestamp()
    os.utime(summary, (stale_stamp, stale_stamp))
    assert limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now) == []
    assert limits._grok_active_sessions(tmp_path / "missing.json", sessions) == []


def test_idle_grok_shell_is_not_a_live_session(tmp_path: Path) -> None:
    """A running grok pid with stale session-dir files is an idle shell, not live."""
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(
        json.dumps({"info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"}, "session_summary": "Idle shell", "current_model_id": "grok-4.6"}),
        encoding="utf-8",
    )
    os.utime(summary, ((now - timedelta(minutes=6)).timestamp(),) * 2)
    active = tmp_path / "active_sessions.json"
    active.write_text(
        json.dumps([{"session_id": SID, "pid": 111, "cwd": "C:\\\\Dev\\\\ExampleProject", "opened_at": "2026-09-02T01:16:49Z"}]),
        encoding="utf-8",
    )
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now) == []


def test_coverage_start_is_the_first_log_record(tmp_path: Path) -> None:
    log = tmp_path / "unified.jsonl"
    assert grok_local.coverage_start(log) is None
    _write_log(log, [_line("2026-09-01T12:10:40.496Z", "shell.tool.exec_done", {}), _turn("2026-09-01T12:10:45Z", SID, 1, 5, 0, 1)])
    assert grok_local.coverage_start(log) == datetime(2026, 9, 1, 12, 10, 40, 496000, tzinfo=UTC)


def test_coverage_start_keeps_persisted_history_after_log_rotation(tmp_path: Path) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    _write_log(
        log,
        [
            _line("2026-09-01T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-01T12:00:01Z", SID, 1, 1000, 0, 100),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    first = grok_local.coverage_start(log, database)
    assert first == datetime(2026, 9, 1, 12, 0, tzinfo=UTC)
    _write_log(
        log,
        [
            _line("2026-09-02T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T12:00:01Z", SID, 1, 10, 0, 1),
        ],
    )
    assert grok_local.coverage_start(log) == datetime(2026, 9, 2, 12, 0, tzinfo=UTC)
    rotated = grok_local.coverage_start(log, database)
    assert rotated == datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)
    assert rotated < datetime(2026, 9, 2, 12, 0, tzinfo=UTC)


def test_rotated_grok_log_does_not_reopen_traycer_history(tmp_path: Path, monkeypatch) -> None:
    grok_local.reset_state()
    database = tmp_path / "spend.db"
    initialize(database)
    pricing = PricingEngine.load(ROOT / "pricing")
    log = tmp_path / "unified.jsonl"
    _write_log(
        log,
        [
            _line("2026-09-01T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-01T12:00:01Z", SID, 1, 1000, 0, 100),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    _write_log(
        log,
        [
            _line("2026-09-02T12:00:00Z", "model changed", {"model": "grok-4.6"}),
            _turn("2026-09-02T12:00:01Z", SID, 1, 10, 0, 1),
        ],
    )
    grok_local.ingest(database_path=database, pricing=pricing, log_path=log)
    boundary = grok_local.coverage_start(log, database)
    t0 = datetime(2026, 9, 1, 12, 0, 1, tzinfo=UTC)

    def row(when: datetime) -> UsageRow:
        return UsageRow(
            source="traycer_local",
            tool_key="grok",
            model_key="grok:m",
            occurred_at=when,
            session_id="traycer:c",
            project="p",
            input_tokens=1000,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=100,
            reasoning_tokens=None,
            cost_usd=None,
            raw_id=f"traycer-grok-{when.isoformat()}",
        )

    fake_db = tmp_path / "chat.db"
    fake_db.write_bytes(b"")
    monkeypatch.setattr(traycer_local, "parse_database", lambda _path, **kwargs: [Observation(row(t0), (), t0.timestamp(), "traycer-projection")])
    seen: list[UsageRow] = []

    def capture(**kwargs):
        seen.extend(kwargs["usage_rows"])
        return {"status": "success"}

    monkeypatch.setattr(traycer_local, "persist_rows", capture)
    result = traycer_local.ingest(
        database_path=database,
        pricing=None,
        database_glob=str(fake_db),
        grok_covered_from=boundary,
    )
    assert result["mirroredGrokEvents"] == 1 and result["eventsSeen"] == 0
    assert seen == []
    summary = aggregate_summary(
        database_path=database,
        pricing=pricing,
        window_key="all",
        tool="grok",
        timezone="UTC",
        cache_threshold=0.5,
        now=datetime(2026, 9, 3, tzinfo=UTC),
    )
    assert summary["totals"]["tokens"] == 1111


def test_live_session_omits_model_when_current_model_id_is_missing(tmp_path: Path) -> None:
    now = datetime(2026, 9, 2, 2, tzinfo=UTC)
    sessions = tmp_path / "sessions"
    project = sessions / "C%3A%5CDev%5CExampleProject"
    (project / SID).mkdir(parents=True)
    summary = project / SID / "summary.json"
    summary.write_text(
        json.dumps(
            {
                "info": {"id": SID, "cwd": "C:\\\\Dev\\\\ExampleProject"},
                "session_summary": "Demo session",
                "current_model_id": None,
            }
        ),
        encoding="utf-8",
    )
    os.utime(summary, (now.timestamp(), now.timestamp()))
    active = tmp_path / "active_sessions.json"
    active.write_text(
        json.dumps(
            [
                {
                    "session_id": SID,
                    "pid": 111,
                    "cwd": "C:\\\\Dev\\\\ExampleProject",
                    "opened_at": "2026-09-02T01:16:49.909310900Z",
                }
            ]
        ),
        encoding="utf-8",
    )
    limits._GROK_SESSION_DIRS.clear()
    live = limits._grok_active_sessions(active, sessions, alive=lambda pid: pid == 111, now=now)
    assert live == [
        {
            "sessionId": SID,
            "title": "Demo session",
            "model": None,
            "startedAt": "2026-09-02T01:16:49.909310900Z",
        }
    ]
    records = agent_run_records({"activeAgents": [], "unmeteredTurns": [], "grokSessions": live})
    assert records[0].model_key is None


def test_grok_session_dir_rejects_path_escape_and_finds_nested_subagents(tmp_path: Path) -> None:
    sessions = tmp_path / "sessions"
    project = sessions / "proj"
    nested = project / "parent-sid" / "subagents" / SID
    nested.mkdir(parents=True)
    secret = tmp_path / "secret-dir"
    secret.mkdir()
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(SID, sessions) == nested.resolve()
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir("..", sessions) is None
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(r"..\..\secret-dir", sessions) is None
    limits._GROK_SESSION_DIRS.clear()
    assert limits._grok_session_dir(str(secret), sessions) is None


def test_traycer_drops_grok_rows_the_cli_log_already_covers(tmp_path: Path, monkeypatch) -> None:
    def row(tool: str, when: datetime) -> UsageRow:
        return UsageRow(source="traycer_local", tool_key=tool, model_key=f"{tool}:m", occurred_at=when, session_id="traycer:c", project="p",
                        input_tokens=10, cached_input_tokens=0, cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=1,
                        reasoning_tokens=None, cost_usd=None, raw_id=f"{tool}-{when.isoformat()}")
    boundary = datetime(2026, 9, 1, 12, 10, 40, tzinfo=UTC)
    rows = [row("grok", boundary - timedelta(hours=1)), row("grok", boundary), row("grok", boundary + timedelta(hours=1)), row("openrouter", boundary + timedelta(hours=1))]
    fake_db = tmp_path / "chat.db"
    fake_db.write_bytes(b"")
    monkeypatch.setattr(traycer_local, "parse_database", lambda _path, **kwargs: [Observation(r, (), r.occurred_at.timestamp(), "traycer-projection") for r in rows])
    seen: list[UsageRow] = []

    def capture(**kwargs):
        seen.extend(kwargs["usage_rows"])
        return {"status": "success"}

    monkeypatch.setattr(traycer_local, "persist_rows", capture)
    result = traycer_local.ingest(database_path=tmp_path / "spend.db", pricing=None, database_glob=str(fake_db), grok_covered_from=boundary)
    assert result["mirroredGrokEvents"] == 2 and result["eventsSeen"] == 2
    assert [r.raw_id for r in seen] == [rows[0].raw_id, rows[3].raw_id], "history before the log and non-Grok rows stay"
    seen.clear()
    traycer_local.ingest(database_path=tmp_path / "spend.db", pricing=None, database_glob=str(fake_db))
    assert len(seen) == 4, "without a Grok log nothing is dropped"


def test_session_updates_reuse_the_parse_of_unchanged_files(tmp_path, monkeypatch) -> None:
    # Regression: every pass re-read every sessions/*/*/updates.jsonl (hundreds
    # of MB on a busy machine), which made Grok the slowest local source.
    import json as _json

    from spend_app.adapters import grok_local

    def update(session: str, ms: int, event: str) -> str:
        return _json.dumps({"params": {"sessionId": session, "_meta": {"agentTimestampMs": ms, "eventId": event},
                                       "update": {"usage": {"inputTokens": 10, "outputTokens": 2,
                                                            "cachedReadTokens": 0, "cachedWriteTokens": 0}}}}) + "\n"

    root = tmp_path / ".grok"
    first = root / "sessions" / "project" / "s1" / "updates.jsonl"
    second = root / "sessions" / "project" / "s2" / "updates.jsonl"
    first.parent.mkdir(parents=True)
    second.parent.mkdir(parents=True)
    first.write_text(update("s1", 1_790_000_000_000, "e1"), encoding="utf-8")
    second.write_text(update("s2", 1_790_000_100_000, "e2"), encoding="utf-8")
    grok_local.reset_state()
    rows, issues, found = grok_local._session_updates(root)
    assert found == 2 and len(rows) == 2 and issues == []

    opened = []
    original = grok_local.json.loads
    monkeypatch.setattr(grok_local.json, "loads", lambda text, *a, **k: opened.append(1) or original(text, *a, **k))
    again, _issues, _found = grok_local._session_updates(root)
    assert [row.raw_id for row in again] == [row.raw_id for row in rows]
    assert opened == []  # neither file was re-parsed

    with second.open("a", encoding="utf-8") as stream:
        stream.write(update("s2", 1_790_000_200_000, "e3"))
    grown, _issues, _found = grok_local._session_updates(root)
    assert len(grown) == 3 and len(opened) == 2  # only the changed file was re-read
    grok_local.reset_state()


def test_native_pass_reconciles_only_changed_sessions_with_full_pass_results(tmp_path, monkeypatch) -> None:
    # Regression: an active session appends on every turn, so every pass re-read
    # and re-reconciled every session ever recorded (~15s with ~500 sessions).
    from spend_app.adapters import grok_native

    def update(session: str, ms: int, event: str, tokens: int = 10) -> str:
        return json.dumps({"params": {"sessionId": session, "_meta": {"agentTimestampMs": ms, "eventId": event},
                                      "update": {"usage": {"inputTokens": tokens, "outputTokens": 2,
                                                           "cachedReadTokens": 0, "cachedWriteTokens": 0}}}}) + "\n"

    root = tmp_path / ".grok"
    for sid in ("s1", "s2", "s3"):
        (root / "sessions" / "project" / sid).mkdir(parents=True)
        (root / "sessions" / "project" / sid / "updates.jsonl").write_text(
            update(sid, 1_790_000_000_000, sid + "-e1"), encoding="utf-8")
    # s2 carries a coarse total; its later decrease is reported on every pass.
    (root / "sessions" / "project" / "s2" / "signals.json").write_text(json.dumps({"totalTokens": 500}), encoding="utf-8")
    pricing = PricingEngine.load(ROOT / "pricing")
    incremental, full = tmp_path / "incremental.db", tmp_path / "full.db"
    initialize(incremental)
    initialize(full)
    grok_local.reset_state()
    grok_local.ingest(database_path=incremental, pricing=pricing, log_path=root)
    (root / "sessions" / "project" / "s2" / "signals.json").write_text(json.dumps({"totalTokens": 400}), encoding="utf-8")
    grok_local.ingest(database_path=incremental, pricing=pricing, log_path=root)

    reconciled, opened = [], []
    original_session = grok_native._reconcile_session
    monkeypatch.setattr(grok_native, "_reconcile_session",
                        lambda connection, sid, rows, issues: reconciled.append(sid) or original_session(connection, sid, rows, issues))
    original_loads = grok_native.json.loads
    monkeypatch.setattr(grok_native.json, "loads", lambda text, *a, **k: opened.append(1) or original_loads(text, *a, **k))
    with (root / "sessions" / "project" / "s1" / "updates.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(update("s1", 1_790_000_100_000, "s1-e2", 30))
    result = grok_local.ingest(database_path=incremental, pricing=pricing, log_path=root)
    assert reconciled == ["s1"]  # s2 and s3 reuse their previous reconciliation
    assert len(opened) == 2  # only the changed file was re-read
    assert "grok_signal_decrease_without_revision_evidence" in result["issues"]  # unchanged s2 still reports it

    # A cold pass over the same files and history stores exactly the same rows.
    monkeypatch.undo()
    grok_local.reset_state()
    with connect(incremental) as source, connect(full) as target:
        meta = source.execute("SELECT key, value FROM app_meta WHERE key LIKE 'grok.native%'").fetchall()
        target.executemany("INSERT INTO app_meta(key, value) VALUES(?, ?)", [tuple(row) for row in meta])
    cold = grok_local.ingest(database_path=full, pricing=pricing, log_path=root)
    def stored(database: Path, table: str) -> list[dict]:
        with connect(database) as connection:
            rows = connection.execute(f"SELECT * FROM {table} WHERE source='grok_local' ORDER BY raw_id").fetchall()
        return [{k: row[k] for k in row.keys() if k not in {"id", "ingested_at"}} for row in rows]

    for table in ("usage_events", "unpriced_usage_events"):
        assert stored(incremental, table) == stored(full, table)
    assert stored(full, "usage_events") + stored(full, "unpriced_usage_events")
    assert {key: cold[key] for key in ("status", "eventsSeen", "eventsAccepted")} == {key: result[key] for key in ("status", "eventsSeen", "eventsAccepted")}
    grok_local.reset_state()


def test_signal_total_is_saved_when_the_session_also_has_unified_rows(tmp_path: Path) -> None:
    from spend_app.adapters.common import stable_id
    from spend_app.adapters.grok_native import _reconcile_session
    from spend_app.adapters.usage_repair import read_meta

    database = tmp_path / "spend.db"
    initialize(database)
    when = datetime(2026, 10, 22, 12, tzinfo=UTC)

    def row(raw_id: str, **tokens) -> UsageRow:
        return UsageRow(source="grok_local", tool_key="grok", model_key="supergrok:grok-4.6", occurred_at=when,
                        session_id="s1", project=None, input_tokens=tokens.get("input", 0), cached_input_tokens=0,
                        cache_write_tokens=0, cache_write_1h_tokens=0, output_tokens=tokens.get("output", 0),
                        reasoning_tokens=None, cost_usd=None, raw_id=raw_id,
                        unclassified_tokens=tokens.get("unclassified", 0))

    incoming = [row("grok-local:1", input=100, output=20),
                row(stable_id("grok-native-total", "s1"), unclassified=500)]
    issues: list[str] = []
    with connect(database) as connection:
        rows = list(_reconcile_session(connection, "s1", incoming, issues))
        state = read_meta(connection, "grok.native.v1:" + stable_id("s1"), {})
    assert state["total"] == 500
    assert {r.raw_id: r.unclassified_tokens for r in rows}[stable_id("grok-native-total", "s1")] == 380
    assert not issues
