"""Collection that survives restarts, short write locks, and a worker process."""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app.config import Settings
from spend_app.db import connect, initialize

ROOT = Path(__file__).resolve().parents[1]


def settings_for(tmp_path: Path) -> Settings:
    value = Settings(database_path=tmp_path / "spend.db", pricing_path=ROOT / "pricing", anthropic_admin_key=None,
                     openai_admin_key=None, timezone="UTC", cache_hit_threshold=0.75, over_routing_token_ceiling=40000,
                     logs_path=tmp_path / "logs")
    initialize(value.database_path)
    return value


def claude_line(message_id: str, when: str, output: int = 5) -> str:
    return json.dumps({"type": "assistant", "timestamp": when, "sessionId": "s1", "cwd": "C:/work/demo",
                       "uuid": "u-" + message_id, "message": {"id": message_id, "model": "claude-opus-5",
                       "usage": {"input_tokens": 10, "cache_read_input_tokens": 0,
                                 "cache_creation_input_tokens": 0, "output_tokens": output}}})


def test_a_restart_does_not_reread_unchanged_transcripts(tmp_path: Path, monkeypatch) -> None:
    from spend_app.adapters import claude_local, file_cache
    from spend_app.pricing import PricingEngine

    transcript = tmp_path / "projects" / "demo" / "s1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(claude_line("m1", "2026-10-26T01:00:00Z") + "\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    glob = str(tmp_path / "projects" / "**" / "*.jsonl")
    claude_local.reset_file_cache()
    first = claude_local.ingest(database_path=settings.database_path, pricing=pricing, session_glob=glob)
    assert first["eventsWritten"] == 1

    # A new process: nothing in memory, the record comes from the database.
    claude_local._FILE_SIGNATURES.clear()
    claude_local._FILE_COUNTS.clear()
    file_cache._LOADED.clear()
    parsed = []
    original = claude_local.parse_file_with_health
    monkeypatch.setattr(claude_local, "parse_file_with_health", lambda path: parsed.append(path) or original(path))
    second = claude_local.ingest(database_path=settings.database_path, pricing=pricing, session_glob=glob)
    assert parsed == [] and second["filesSkippedUnchanged"] == 1 and second["eventsSeen"] == 1

    # A changed file is read again.
    with transcript.open("a", encoding="utf-8") as stream:
        stream.write(claude_line("m2", "2026-10-26T01:05:00Z") + "\n")
    third = claude_local.ingest(database_path=settings.database_path, pricing=pricing, session_glob=glob)
    assert len(parsed) == 1 and third["eventsWritten"] == 1
    claude_local.reset_file_cache()
    with connect(settings.database_path) as connection:
        assert not connection.execute("SELECT 1 FROM ingest_file_cache WHERE source='claude_local'").fetchone()


def test_a_large_first_read_is_saved_in_batches(tmp_path: Path, monkeypatch) -> None:
    from spend_app.adapters import claude_local, file_cache
    from spend_app.pricing import PricingEngine

    monkeypatch.setattr(file_cache, "BATCH_EVENTS", 2)
    folder = tmp_path / "projects" / "demo"
    folder.mkdir(parents=True)
    for index in range(5):
        (folder / f"s{index}.jsonl").write_text(claude_line(f"m{index}", f"2026-10-26T01:0{index}:00Z") + "\n", encoding="utf-8")
    settings = settings_for(tmp_path)
    claude_local.reset_file_cache()
    result = claude_local.ingest(database_path=settings.database_path, pricing=PricingEngine.load(settings.pricing_path),
                                 session_glob=str(tmp_path / "projects" / "**" / "*.jsonl"))
    claude_local.reset_file_cache()
    assert result["eventsWritten"] == 5 and result["files"] == 5
    with connect(settings.database_path) as connection:
        runs = connection.execute("SELECT COUNT(*) FROM ingest_runs WHERE source='claude_local'").fetchone()[0]
    assert runs >= 3, "each batch commits on its own instead of one long transaction"


def test_identity_records_move_out_of_app_meta_in_steps(tmp_path: Path) -> None:
    from spend_app import db as dbm
    from spend_app.adapters.common import stable_id
    from spend_app.adapters.usage_repair import read_meta, write_meta

    settings = settings_for(tmp_path)
    keys = {f"usage.alias.v1:{stable_id('codex_local', f'a{index}')}": f"owner-{index}" for index in range(25)}
    keys["usage.identity.v1:" + stable_id("claude_local", "x")] = {"raw_id": "x", "rank": [1, 1, 0], "field_ranks": {"a": [1, 1]} | {f"f{n}": [n, 1] for n in range(20)}}
    with connect(settings.database_path) as connection:
        connection.execute("DELETE FROM app_meta WHERE key=?", (dbm.USAGE_KEYS_MARKER,))
        for key, value in keys.items():
            connection.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", (key, json.dumps(value)))
    dbm._USAGE_KEYS_DONE.clear()
    first = next(iter(keys))
    with connect(settings.database_path) as connection:
        write_meta(connection, first, "rewritten")  # newer than its app_meta copy
    assert dbm.migrate_usage_keys_step(settings.database_path, limit=10) == 10
    with connect(settings.database_path) as connection:
        assert all(read_meta(connection, key, None) == (value if key != first else "rewritten") for key, value in keys.items())
    while dbm.migrate_usage_keys_step(settings.database_path, limit=10):
        pass
    with connect(settings.database_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM app_meta WHERE key >= 'usage.' AND key < 'usage/'").fetchone()[0] == 0
        assert connection.execute("SELECT 1 FROM app_meta WHERE key=?", (dbm.USAGE_KEYS_MARKER,)).fetchone()
        assert read_meta(connection, first, None) == "rewritten"
        stored = connection.execute("SELECT value FROM usage_keys WHERE kind=1").fetchone()[0]
    assert isinstance(stored, bytes), "a large identity record is stored compressed"


def test_a_lane_whose_readings_were_not_saved_retries_soon(tmp_path: Path) -> None:
    from spend_app import quotas
    from spend_app.quotas import QuotaLaneScheduler, QuotaSample, poll_quotas

    clock = [1000.0]
    lanes = QuotaLaneScheduler({"codex": (15, 300)}, clock=lambda: clock[0])
    sample = QuotaSample(provider_key="codex", limit_key="weekly", label="Codex weekly window", unit="pct",
                         source="codex_local_telemetry", pct=10.0)
    settings = settings_for(tmp_path)
    original = quotas.connect

    def locked(_path):
        raise RuntimeError("database is locked")

    quotas.connect = locked
    try:
        with pytest.raises(RuntimeError):
            poll_quotas(settings.database_path, collectors={"codex": lambda: [sample]}, lanes=lanes,
                        activity=lambda _key: False)
    finally:
        quotas.connect = original
    clock[0] += quotas.RETRY_AFTER_FAILED_SAVE_SECONDS
    assert lanes.due("codex"), "retried after a minute, not after the 5-minute idle interval"
    poll_quotas(settings.database_path, collectors={"codex": lambda: [sample]}, lanes=lanes, activity=lambda _key: False)
    clock[0] += 60
    assert not lanes.due("codex"), "a saved poll waits for the lane's interval"


def test_collection_runs_in_a_worker_process(tmp_path: Path, monkeypatch) -> None:
    from spend_app.collector import CollectorWorker

    empty_home = tmp_path / "home"
    empty_home.mkdir()
    for name in ("USERPROFILE", "HOME", "APPDATA", "LOCALAPPDATA", "XDG_DATA_HOME"):
        monkeypatch.setenv(name, str(empty_home))
    for name in ("CODEX_HOME", "CLAUDE_CONFIG_DIR", "GROK_HOME"):
        monkeypatch.delenv(name, raising=False)
    worker = CollectorWorker(settings_for(tmp_path))
    try:
        reply = worker.run_pass(timeout=120)
        assert "claude_local" in reply["timings"] and isinstance(reply["results"], dict)
        process = worker._process
        assert process.pid != os.getpid() and process.is_alive()
        assert worker.run_pass(timeout=120)["timings"], "the same worker serves the next pass"
    finally:
        worker.stop()
    assert not process.is_alive()


def test_extra_profile_usage_is_recorded_under_its_own_card(tmp_path: Path, monkeypatch) -> None:
    from spend_app import claude_accounts, opencode_profiles
    from spend_app.adapters import claude_local, profiles_local
    from spend_app.pricing import PricingEngine

    work = tmp_path / ".claude-work"
    transcript = work / "projects" / "demo" / "w1.jsonl"
    transcript.parent.mkdir(parents=True)
    transcript.write_text(claude_line("work-1", "2026-10-26T02:00:00Z").replace('"s1"', '"w1"') + "\n", encoding="utf-8")
    account = claude_accounts.ClaudeAccount(key="claude-code-work", label="Work", config_dir=work,
                                            state_path=work / ".claude.json",
                                            account_uuid="work-uuid", plan="Claude Max")
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: (account,))
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())
    settings = settings_for(tmp_path)
    claude_local.reset_file_cache()
    result = profiles_local.ingest(database_path=settings.database_path, pricing=PricingEngine.load(settings.pricing_path))
    claude_local.reset_file_cache()
    assert result["eventsWritten"] == 1
    with connect(settings.database_path) as connection:
        assert connection.execute("SELECT tool_key FROM usage_events").fetchone()[0] == "claude-code-work"
        assert connection.execute("SELECT tool_key FROM sessions WHERE session_id='w1'").fetchone()[0] == "claude-code-work"
    # Nothing to read: no run is recorded for the step.
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    assert profiles_local.ingest(database_path=settings.database_path, pricing=None)["status"] == "skipped"


def test_a_moving_cumulative_total_rereads_only_its_own_opencode_session(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from spend_app.adapters import opencode_granular, opencode_local

    database = tmp_path / "spend.db"
    initialize(database)
    source = tmp_path / "opencode.db"
    source.write_bytes(b"")
    message = lambda session, n: SimpleNamespace(row=SimpleNamespace(session_id=session, raw_id=f"{session}-{n}"))
    messages = [message("A", 1), message("A", 2), message("B", 1)]
    totals = {"A": 10, "B": 5}
    snapshot = lambda session: SimpleNamespace(session_id=session, provider_id="p", model_id="m", total=totals[session])
    monkeypatch.setattr(opencode_granular, "read_source", lambda path: (messages, [source], [], ["v2"]))
    monkeypatch.setattr(opencode_local, "read_session_snapshots", lambda path: [snapshot("A"), snapshot("B")])
    seen_totals = []
    # The cumulative step writes a row every pass, as it does while a session is open.
    monkeypatch.setattr(opencode_local, "_ingest_cumulative", lambda *, snapshots, **_: seen_totals.append(
        [item.total for item in snapshots]) or {"status": "success", "eventsWritten": 1})
    reread = []

    def fake_persist(*, database_path, usage_rows, finalize, **_):
        reread.append(sorted(row.raw_id for row in usage_rows))
        with connect(database_path) as connection:
            finalize(connection)
        return {"status": "success", "eventsWritten": len(usage_rows)}

    monkeypatch.setattr(opencode_granular, "persist_rows", fake_persist)
    collect = lambda: opencode_granular._collect(database_path=database, pricing=SimpleNamespace(prices=()), path=source)
    collect()
    collect()
    totals["A"] = 12
    collect()
    assert reread == [["A-1", "A-2", "B-1"], [], ["A-1", "A-2"]]
    # The cumulative step and the fingerprints read the same totals.
    assert seen_totals == [[10, 5], [10, 5], [12, 5]]
