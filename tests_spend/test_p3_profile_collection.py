"""Unit and integration tests for P3 (multi-profile collection pass)."""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

import pytest

from spend_app import (
    claude_accounts,
    collector,
    connection_paths,
    connections,
    db as spend_db,
    opencode_profiles,
    profile_detect,
    profile_store,
)
from spend_app.adapters import (
    antigravity_local,
    claude_local,
    codex_local,
    file_cache,
    grok_local,
    muse_local,
    profiles_local,
    unchanged,
)
from spend_app.config import Settings
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
TS_ISO = "2026-11-12T12:00:00Z"
TS_MS = int(datetime(2026, 11, 12, 12, 0, tzinfo=UTC).timestamp() * 1000)


def _clear_all_caches() -> None:
    claude_local.reset_file_cache()
    codex_local.reset_file_cache()
    muse_local.reset_file_cache()
    grok_local.reset_state()
    antigravity_local.reset_state()
    unchanged.reset_state()
    profile_detect.reset_cache()
    claude_accounts.reset_cache()
    opencode_profiles.reset_cache()
    file_cache._LOADED.clear()
    file_cache._DATABASES.clear()
    connection_paths._DATABASE_IDENTITIES.clear()


@pytest.fixture(autouse=True)
def _isolated_home_and_caches(tmp_path: Path, monkeypatch):
    fake_home = tmp_path / "fake_home"
    fake_home.mkdir(parents=True, exist_ok=True)
    appdata = tmp_path / "appdata"
    localappdata = tmp_path / "localappdata"
    appdata.mkdir()
    localappdata.mkdir()

    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("USERPROFILE", str(fake_home))
    monkeypatch.setenv("APPDATA", str(appdata))
    monkeypatch.setenv("LOCALAPPDATA", str(localappdata))
    for name in (
        "XDG_DATA_HOME",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "GROK_HOME",
        "OPENCODE_DB",
        "OPENCODE_API_KEY",
        "BURNRATE_CLAUDE_ACCOUNT_DIRS",
        "BURNRATE_OPENCODE_PROFILE_DIRS",
        "BURNRATE_CODEX_PROFILE_DIRS",
        "OPENAI_ADMIN_KEY",
        "ANTHROPIC_ADMIN_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "DEEPSEEK_BALANCE_KEY",
    ):
        monkeypatch.delenv(name, raising=False)

    _clear_all_caches()
    yield fake_home
    _clear_all_caches()


def _make_settings(tmp_path: Path, *, name: str = "spend.db") -> Settings:
    settings = Settings(
        database_path=tmp_path / name,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    spend_db.initialize(settings.database_path)
    return settings


def _write_v2_profiles(db_path: Path, profiles_by_provider: dict[str, dict[str, dict]]) -> None:
    store = connections.Store(db_path)
    with store.transaction() as state:
        state["profiles"] = profiles_by_provider


def _profile_record(
    provider: str,
    key: str,
    location: str | Path,
    *,
    enabled: bool = True,
    revision: int = 1,
    origin: str | None = None,
    tool_key: str | None = None,
) -> dict:
    resolved_origin = origin or ("default" if key == "default" else "manual")
    resolved_tool = tool_key if tool_key is not None else profile_store.profile_tool_key(provider, key)
    return {
        "source": provider,
        "bindingId": provider if key == "default" else f"{provider}:{key}",
        "name": "Default" if key == "default" else key,
        "location": str(location),
        "origin": resolved_origin,
        "enabled": enabled,
        "revision": revision,
        "toolKey": resolved_tool,
        "accountDigest": None,
    }


def _write_claude_transcript(
    path: Path,
    *,
    session_id: str,
    message_id: str,
    when: str = TS_ISO,
    model: str = "claude-opus-5",
    cwd: str = "C:/work/demo",
    input_tokens: int = 100,
    output_tokens: int = 25,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "timestamp": when,
        "sessionId": session_id,
        "cwd": cwd,
        "uuid": f"uuid-{message_id}",
        "message": {
            "id": message_id,
            "model": model,
            "usage": {
                "input_tokens": input_tokens,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": output_tokens,
            },
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_codex_rollout(
    path: Path,
    *,
    session_id: str,
    when: str = TS_ISO,
    model: str = "gpt-5.4-mini",
    cwd: str = "C:/work/codex-demo",
    input_tokens: int = 200,
    cached_tokens: int = 50,
    output_tokens: int = 40,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    total = input_tokens + output_tokens
    lines = [
        {
            "type": "session_meta",
            "timestamp": when,
            "payload": {
                "id": session_id,
                "originator": "codex_cli_rs",
                "cwd": cwd,
            },
        },
        {
            "type": "turn_context",
            "timestamp": when,
            "payload": {
                "turn_id": f"turn-{session_id}",
                "model": model,
            },
        },
        {
            "type": "event_msg",
            "timestamp": when,
            "payload": {
                "type": "token_count",
                "info": {
                    "last_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": 5,
                        "total_tokens": total,
                    },
                    "total_token_usage": {
                        "input_tokens": input_tokens,
                        "cached_input_tokens": cached_tokens,
                        "output_tokens": output_tokens,
                        "reasoning_output_tokens": 5,
                        "total_tokens": total,
                    },
                },
            },
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


def _write_opencode_db(
    db_path: Path,
    *,
    messages: list[dict] = (),
    cumulative_sessions: list[dict] = (),
) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            """
            CREATE TABLE session_message (
                id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL,
                role TEXT,
                time_updated INTEGER,
                data TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE session (
                id TEXT PRIMARY KEY,
                project_id TEXT,
                directory TEXT,
                path TEXT,
                model TEXT,
                cost REAL,
                tokens_input INTEGER,
                tokens_output INTEGER,
                tokens_reasoning INTEGER,
                tokens_cache_read INTEGER,
                tokens_cache_write INTEGER,
                time_updated INTEGER
            )
            """
        )
        for msg in messages:
            conn.execute(
                "INSERT INTO session_message(id, session_id, role, time_updated, data) VALUES(?,?,?,?,?)",
                (
                    msg["id"],
                    msg["sessionID"],
                    "assistant",
                    msg.get("time_updated", TS_MS),
                    json.dumps(msg),
                ),
            )
        for sess in cumulative_sessions:
            conn.execute(
                """
                INSERT INTO session(
                    id, project_id, directory, path, model, cost,
                    tokens_input, tokens_output, tokens_reasoning,
                    tokens_cache_read, tokens_cache_write, time_updated
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    sess["id"],
                    sess.get("project_id", "proj"),
                    sess.get("directory", "C:/work/oc"),
                    sess.get("path", "C:/work/oc"),
                    json.dumps({"providerID": sess["providerID"], "id": sess["modelID"]}),
                    sess.get("cost"),
                    sess.get("tokens_input", 100),
                    sess.get("tokens_output", 25),
                    sess.get("tokens_reasoning", 0),
                    sess.get("tokens_cache_read", 10),
                    sess.get("tokens_cache_write", 0),
                    sess.get("time_updated", TS_MS),
                ),
            )
    return db_path


def _write_zcode_db(
    db_path: Path,
    *,
    session_id: str,
    usage_id: str,
    provider_id: str = "builtin:zai-coding-plan",
    model_id: str = "glm-5.3",
    directory: str = "C:/work/zcode-app",
) -> Path:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        conn.execute("CREATE TABLE session (id TEXT PRIMARY KEY, directory TEXT)")
        conn.execute(
            """
            CREATE TABLE model_usage (
                id TEXT PRIMARY KEY,
                session_id TEXT,
                provider_id TEXT,
                model_id TEXT,
                completed_at INTEGER,
                input_tokens INTEGER,
                output_tokens INTEGER,
                reasoning_tokens INTEGER,
                cache_creation_input_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                computed_total_tokens INTEGER,
                raw_usage_json TEXT,
                status TEXT
            )
            """
        )
        conn.execute("INSERT INTO session(id, directory) VALUES(?, ?)", (session_id, directory))
        conn.execute(
            """
            INSERT INTO model_usage(
                id, session_id, provider_id, model_id, completed_at,
                input_tokens, output_tokens, reasoning_tokens,
                cache_creation_input_tokens, cache_read_input_tokens,
                computed_total_tokens, raw_usage_json, status
            ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
            """,
            (
                usage_id,
                session_id,
                provider_id,
                model_id,
                TS_MS,
                100,
                20,
                0,
                0,
                10,
                120,
                "{}",
                "completed",
            ),
        )
    return db_path


def _write_grok_updates(
    path: Path,
    *,
    session_id: str,
    event_id: str,
    model_id: str = "grok-4.7",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "params": {
            "sessionId": session_id,
            "_meta": {"agentTimestampMs": TS_MS, "eventId": event_id},
            "update": {
                "_meta": {"modelId": model_id},
                "usage": {
                    "inputTokens": 100,
                    "outputTokens": 20,
                    "cachedReadTokens": 10,
                    "cachedWriteTokens": 0,
                    "modelUsage": {model_id: {}},
                },
            },
        }
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_muse_log(
    path: Path,
    *,
    session_id: str,
    event_id: str,
    model: str = "muse-spark-1.3",
    cwd: str = "C:/work/muse-proj",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "recorded_at": TS_ISO,
        "session_id": session_id,
        "event_id": event_id,
        "model": model,
        "cwd": cwd,
        "usage": {
            "input_tokens": 50,
            "cached_tokens": 10,
            "output_tokens": 15,
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_antigravity_cache(
    path: Path,
    *,
    session_id: str,
    response_id: str,
    model_id: str = "gemini-4-argon",
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = [
        {"type": "session_meta", "sessionId": session_id, "modelId": model_id},
        {
            "type": "usage",
            "sessionId": session_id,
            "responseId": response_id,
            "timestamp": TS_MS,
            "input": 100,
            "output": 20,
            "cacheRead": 10,
            "cacheWrite": 0,
        },
    ]
    path.write_text("\n".join(json.dumps(line) for line in lines) + "\n", encoding="utf-8")
    return path


# ---------------------------------------------------------------------------
# 1. Two Claude Code profiles (<dir>/projects and <dir> locations)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_projects_subdir", [True, False])
def test_claude_default_and_non_default_profiles_distinct_tool_keys(
    tmp_path: Path,
    monkeypatch,
    _isolated_home_and_caches: Path,
    use_projects_subdir: bool,
) -> None:
    fake_home = _isolated_home_and_caches
    monkeypatch.setattr(
        antigravity_local,
        "_antigravity_local_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("not running")),
    )

    # Default Claude profile in fake home
    _write_claude_transcript(
        fake_home / ".claude" / "projects" / "app" / "c-def.jsonl",
        session_id="claude-sess-default",
        message_id="msg-def-1",
    )

    # Non-default Claude profile: test both <dir>/projects and <dir> locations
    work_root = fake_home / ".claude-work"
    _write_claude_transcript(
        work_root / "projects" / "work-app" / "c-work.jsonl",
        session_id="claude-sess-work",
        message_id="msg-work-1",
    )
    work_location = work_root / "projects" if use_projects_subdir else work_root

    # Also test a flat <dir> without a projects/ subdir when use_projects_subdir is False
    if not use_projects_subdir:
        flat_root = tmp_path / "claude-flat"
        _write_claude_transcript(
            flat_root / "c-flat.jsonl",
            session_id="claude-sess-flat",
            message_id="msg-flat-1",
        )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    claude_profiles = {
        "default": _profile_record("claude_local", "default", fake_home / ".claude" / "projects"),
        "claude-code-work": _profile_record("claude_local", "claude-code-work", work_location),
    }
    if not use_projects_subdir:
        claude_profiles["claude-code-flat"] = _profile_record(
            "claude_local", "claude-code-flat", tmp_path / "claude-flat"
        )
    _write_v2_profiles(settings.database_path, {"claude_local": claude_profiles})

    summary = collector.run_local_pass(settings, pricing)
    assert summary["errors"] == []

    with spend_db.connect(settings.database_path) as conn:
        events = {
            row["session_id"]: row["tool_key"]
            for row in conn.execute("SELECT session_id, tool_key FROM usage_events WHERE source='claude_local'")
        }
        sessions = {
            row["session_id"]: row["tool_key"]
            for row in conn.execute("SELECT session_id, tool_key FROM sessions")
        }

    assert events["claude-sess-default"] == "claude-code"
    assert events["claude-sess-work"] == "claude-code-work"
    assert sessions["claude-sess-default"] == "claude-code"
    assert sessions["claude-sess-work"] == "claude-code-work"
    if not use_projects_subdir:
        assert events["claude-sess-flat"] == "claude-code-flat"
        assert sessions["claude-sess-flat"] == "claude-code-flat"


# ---------------------------------------------------------------------------
# 2. Two Codex profiles (<dir>/sessions and <dir> locations)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("use_sessions_subdir", [True, False])
def test_codex_default_and_non_default_profiles_distinct_tool_keys(
    tmp_path: Path,
    monkeypatch,
    _isolated_home_and_caches: Path,
    use_sessions_subdir: bool,
) -> None:
    fake_home = _isolated_home_and_caches
    monkeypatch.setattr(
        antigravity_local,
        "_antigravity_local_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("not running")),
    )

    _write_codex_rollout(
        fake_home / ".codex" / "sessions" / "2026" / "11" / "12" / "rollout-def.jsonl",
        session_id="codex-sess-default",
    )

    work_root = fake_home / ".codex-work"
    _write_codex_rollout(
        work_root / "sessions" / "2026" / "11" / "12" / "rollout-work.jsonl",
        session_id="codex-sess-work",
    )
    work_location = work_root / "sessions" if use_sessions_subdir else work_root

    if not use_sessions_subdir:
        flat_root = tmp_path / "codex-flat"
        _write_codex_rollout(flat_root / "rollout-flat.jsonl", session_id="codex-sess-flat")

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    codex_profiles = {
        "default": _profile_record("codex_local", "default", fake_home / ".codex" / "sessions"),
        "codex-work": _profile_record("codex_local", "codex-work", work_location),
    }
    if not use_sessions_subdir:
        codex_profiles["codex-flat"] = _profile_record(
            "codex_local", "codex-flat", tmp_path / "codex-flat"
        )
    _write_v2_profiles(settings.database_path, {"codex_local": codex_profiles})

    summary = collector.run_local_pass(settings, pricing)
    assert summary["errors"] == []

    with spend_db.connect(settings.database_path) as conn:
        events = {
            row["session_id"]: row["tool_key"]
            for row in conn.execute("SELECT session_id, tool_key FROM usage_events WHERE source='codex_local'")
        }
        sessions = {
            row["session_id"]: row["tool_key"]
            for row in conn.execute("SELECT session_id, tool_key FROM sessions")
        }

    assert events["codex-sess-default"] == "codex"
    assert events["codex-sess-work"] == "codex-work"
    assert sessions["codex-sess-default"] == "codex"
    assert sessions["codex-sess-work"] == "codex-work"
    if not use_sessions_subdir:
        assert events["codex-sess-flat"] == "codex-flat"
        assert sessions["codex-sess-flat"] == "codex-flat"


# ---------------------------------------------------------------------------
# 3. collector.run_local_pass ordering, results, timings, and ingest_runs
# ---------------------------------------------------------------------------


def test_run_local_pass_runs_profiles_local_first_without_profiles_local_ingest_run(
    tmp_path: Path,
    monkeypatch,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    monkeypatch.setattr(
        antigravity_local,
        "_antigravity_local_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("not running")),
    )

    _write_claude_transcript(
        fake_home / ".claude" / "projects" / "app" / "def.jsonl",
        session_id="s-def",
        message_id="m-def",
    )
    _write_claude_transcript(
        fake_home / ".claude-work" / "projects" / "app" / "work.jsonl",
        session_id="s-work",
        message_id="m-work",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "default": _profile_record("claude_local", "default", fake_home / ".claude" / "projects"),
                "claude-code-work": _profile_record(
                    "claude_local", "claude-code-work", fake_home / ".claude-work"
                ),
            }
        },
    )

    call_order: list[str] = []
    orig_profiles_ingest = profiles_local.ingest
    from spend_app import scheduler

    orig_run_ingest_specs = scheduler._run_ingest_specs

    def tracked_profiles_ingest(*, database_path, pricing):
        call_order.append("profiles_local")
        return orig_profiles_ingest(database_path=database_path, pricing=pricing)

    def tracked_run_ingest_specs(*args, **kwargs):
        call_order.append("registry_pass")
        return orig_run_ingest_specs(*args, **kwargs)

    monkeypatch.setattr(profiles_local, "ingest", tracked_profiles_ingest)
    monkeypatch.setattr(scheduler, "_run_ingest_specs", tracked_run_ingest_specs)

    summary = collector.run_local_pass(settings, pricing)

    assert call_order == ["profiles_local", "registry_pass"]
    assert summary["errors"] == []
    assert summary["results"]["profiles_local"] == {"written": 1, "status": "success"}
    assert "profiles_local" in summary["timings"]
    assert isinstance(summary["timings"]["profiles_local"], float)
    assert summary["timings"]["profiles_local"] >= 0.0

    with spend_db.connect(settings.database_path) as conn:
        profiles_runs = conn.execute(
            "SELECT COUNT(*) FROM ingest_runs WHERE source='profiles_local'"
        ).fetchone()[0]
        claude_runs = conn.execute(
            "SELECT events_written, status FROM ingest_runs WHERE source='claude_local' ORDER BY id"
        ).fetchall()

    assert profiles_runs == 0
    # Profile pass wrote its claude_local run first, then registry pass wrote default claude_local latest
    assert [(r["events_written"], r["status"]) for r in claude_runs] == [
        (1, "success"),
        (1, "success"),
    ]


def test_run_local_pass_records_skipped_when_no_non_default_profiles(
    tmp_path: Path,
    monkeypatch,
    _isolated_home_and_caches: Path,
) -> None:
    monkeypatch.setattr(
        antigravity_local,
        "_antigravity_local_connection",
        lambda: (_ for _ in ()).throw(RuntimeError("not running")),
    )
    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    summary = collector.run_local_pass(settings, pricing)
    assert summary["errors"] == []
    assert summary["results"]["profiles_local"] == {"written": 0, "status": "skipped"}
    assert "profiles_local" in summary["timings"]

    with spend_db.connect(settings.database_path) as conn:
        assert (
            conn.execute("SELECT COUNT(*) FROM ingest_runs WHERE source='profiles_local'").fetchone()[0]
            == 0
        )


# ---------------------------------------------------------------------------
# 4. Default profiles skipped by profiles_local, bindings view & suppresses_default_profile
# ---------------------------------------------------------------------------


def test_default_profiles_skipped_by_profiles_local_and_bindings_view_default_only(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    default_claude = fake_home / ".claude" / "projects"
    _write_claude_transcript(
        default_claude / "p" / "def.jsonl",
        session_id="s-only-default",
        message_id="m-only-default",
    )
    work_claude = fake_home / ".claude-work"
    _write_claude_transcript(
        work_claude / "projects" / "p" / "work.jsonl",
        session_id="s-work-extra",
        message_id="m-work-extra",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    # First: only default profile enabled -> profiles_local must skip it completely
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "default": _profile_record("claude_local", "default", default_claude),
            }
        },
    )
    skipped = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert skipped == {"status": "skipped", "eventsWritten": 0, "unpricedEventsWritten": 0}
    with spend_db.connect(settings.database_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0

    # Add non-default profile: Store.read()["bindings"] still holds ONLY default
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "default": _profile_record("claude_local", "default", default_claude),
                "claude-code-work": _profile_record("claude_local", "claude-code-work", work_claude),
            }
        },
    )
    state = connections.Store(settings.database_path).read()
    assert list(state["bindings"].keys()) == ["claude_local"]
    assert state["bindings"]["claude_local"]["bindingId"] == "claude_local"
    assert state["bindings"]["claude_local"]["toolKey"] == "claude-code"
    assert "claude-code-work" not in state["bindings"]

    # suppresses_default_profile applies to the default binding only; the non-default profile
    # at a custom folder never enters bindings and therefore never suppresses the default tool.
    assert connections.suppresses_default_profile("claude_local", state["bindings"]["claude_local"]) is False
    assert connections.suppresses_default_profile(
        "claude_local", {"enabled": True, "location": str(tmp_path / "custom-default")}
    ) is True
    assert connections.suppresses_default_profile(
        "claude_local", {"enabled": False, "location": str(default_claude)}
    ) is True

    # profiles_local collects ONLY the non-default profile, not the default profile
    res = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert res["status"] == "success"
    assert res["eventsWritten"] == 1
    with spend_db.connect(settings.database_path) as conn:
        rows = conn.execute("SELECT session_id, tool_key FROM usage_events").fetchall()
    assert [(r["session_id"], r["tool_key"]) for r in rows] == [("s-work-extra", "claude-code-work")]


# ---------------------------------------------------------------------------
# 5. Disabled non-default profiles are NOT collected
# ---------------------------------------------------------------------------


def test_disabled_non_default_profiles_are_not_collected(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    enabled_dir = fake_home / ".claude-enabled"
    disabled_dir = fake_home / ".claude-disabled"
    _write_claude_transcript(
        enabled_dir / "projects" / "a" / "on.jsonl",
        session_id="sess-on",
        message_id="msg-on",
    )
    _write_claude_transcript(
        disabled_dir / "projects" / "a" / "off.jsonl",
        session_id="sess-off",
        message_id="msg-off",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    # All non-default profiles disabled -> skipped
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "claude-code-disabled": _profile_record(
                    "claude_local", "claude-code-disabled", disabled_dir, enabled=False
                ),
            }
        },
    )
    only_disabled = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert only_disabled == {"status": "skipped", "eventsWritten": 0, "unpricedEventsWritten": 0}

    # One enabled, one disabled -> only enabled collected
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "claude-code-enabled": _profile_record(
                    "claude_local", "claude-code-enabled", enabled_dir, enabled=True
                ),
                "claude-code-disabled": _profile_record(
                    "claude_local", "claude-code-disabled", disabled_dir, enabled=False
                ),
            }
        },
    )
    mixed = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert mixed["status"] == "success"
    assert mixed["eventsWritten"] == 1

    with spend_db.connect(settings.database_path) as conn:
        event_rows = conn.execute("SELECT session_id, tool_key FROM usage_events").fetchall()
        session_rows = conn.execute("SELECT session_id, tool_key FROM sessions").fetchall()

    assert [(r["session_id"], r["tool_key"]) for r in event_rows] == [("sess-on", "claude-code-enabled")]
    assert [(r["session_id"], r["tool_key"]) for r in session_rows] == [("sess-on", "claude-code-enabled")]


# ---------------------------------------------------------------------------
# 6. opencode_local non-default profile tags opencode-go & coverage_gap_events
# ---------------------------------------------------------------------------


def test_opencode_non_default_profile_tags_go_and_coverage_gaps_keeps_other_providers(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    oc_db = _write_opencode_db(
        fake_home / ".opencode-work" / "opencode.db",
        messages=[
            {
                "id": "msg-go-1",
                "sessionID": "oc-sess-go",
                "role": "assistant",
                "providerID": "opencode-go",
                "modelID": "glm-5.3",
                "tokens": {
                    "input": 100,
                    "output": 20,
                    "reasoning": 5,
                    "cache": {"read": 10, "write": 0},
                    "total": 135,
                },
                "time": {"created": TS_MS - 1000, "completed": TS_MS},
                "path": {"root": "C:/work/oc-go"},
            },
            {
                "id": "msg-zai-1",
                "sessionID": "oc-sess-zai",
                "role": "assistant",
                "providerID": "zai-coding-plan",
                "modelID": "glm-5.3-flash",
                "tokens": {
                    "input": 80,
                    "output": 15,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                    "total": 95,
                },
                "time": {"created": TS_MS - 1000, "completed": TS_MS},
                "path": {"root": "C:/work/oc-zai"},
            },
            {
                "id": "msg-anth-1",
                "sessionID": "oc-sess-anth",
                "role": "assistant",
                "providerID": "anthropic",
                "modelID": "anthropic/claude-haiku-4.5",
                "tokens": {
                    "input": 60,
                    "output": 12,
                    "reasoning": 0,
                    "cache": {"read": 0, "write": 0},
                    "total": 72,
                },
                "time": {"created": TS_MS - 1000, "completed": TS_MS},
                "path": {"root": "C:/work/oc-anth"},
            },
        ],
        cumulative_sessions=[
            {
                "id": "oc-sess-cum-go",
                "providerID": "opencode-go",
                "modelID": "glm-5.3",
                "tokens_input": 120,
                "tokens_output": 30,
            },
            {
                "id": "oc-sess-cum-anth",
                "providerID": "anthropic",
                "modelID": "anthropic/claude-haiku-4.5",
                "tokens_input": 50,
                "tokens_output": 10,
            },
        ],
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    _write_v2_profiles(
        settings.database_path,
        {
            "opencode_local": {
                "opencode-go-work": _profile_record("opencode_local", "opencode-go-work", oc_db),
            }
        },
    )

    res = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert res["status"] in {"success", "partial"}
    assert res["eventsWritten"] == 5

    with spend_db.connect(settings.database_path) as conn:
        event_tools = {
            row["session_id"]: row["tool_key"]
            for row in conn.execute("SELECT session_id, tool_key FROM usage_events WHERE source='opencode_local'")
        }
        gap_rows = conn.execute(
            "SELECT model_key, tool_key FROM coverage_gap_events WHERE source='opencode_local'"
        ).fetchall()

    assert event_tools == {
        "oc-sess-go": "opencode-go-work",
        "oc-sess-zai": "opencode",
        "oc-sess-anth": "opencode-provider:anthropic",
        "oc-sess-cum-go": "opencode-go-work",
        "oc-sess-cum-anth": "opencode-provider:anthropic",
    }
    gap_tools = {row["model_key"]: row["tool_key"] for row in gap_rows}
    assert gap_tools["opencode:glm-5.3"] == "opencode-go-work"
    assert gap_tools["opencode:anthropic/claude-haiku-4.5"] == "opencode-provider:anthropic"


# ---------------------------------------------------------------------------
# 7. zcode_local non-default profile in custom-named folder
# ---------------------------------------------------------------------------


def test_zcode_non_default_profile_custom_folder_tags_zcode_work(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    zcode_work = fake_home / ".zcode-work"
    _write_zcode_db(
        zcode_work / "cli" / "db" / "db.sqlite",
        session_id="zc-sess-work",
        usage_id="zc-usage-work-1",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    _write_v2_profiles(
        settings.database_path,
        {
            "zcode_local": {
                "zcode-work": _profile_record("zcode_local", "zcode-work", zcode_work),
            }
        },
    )

    res = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert res["status"] == "success"
    assert res["eventsWritten"] == 1

    with spend_db.connect(settings.database_path) as conn:
        rows = conn.execute(
            "SELECT session_id, tool_key, model_key FROM usage_events WHERE source='zcode_local'"
        ).fetchall()
    assert [(r["session_id"], r["tool_key"], r["model_key"]) for r in rows] == [
        ("zc-sess-work", "zcode-work", "zcode:glm-5.3")
    ]


# ---------------------------------------------------------------------------
# 8. grok_local, muse_local, and antigravity_local non-default profiles
# ---------------------------------------------------------------------------


def test_grok_muse_and_antigravity_non_default_profiles_tag_rows_and_sessions(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches

    grok_dir = fake_home / ".grok-work"
    _write_grok_updates(
        grok_dir / "sessions" / "proj" / "gs-work" / "updates.jsonl",
        session_id="gs-work",
        event_id="grok-ev-1",
    )

    muse_dir = fake_home / ".muse-work"
    _write_muse_log(
        muse_dir / "sessions" / "2026" / "11" / "12" / "m-work.jsonl",
        session_id="muse-sess-work",
        event_id="muse-ev-1",
    )

    ag_cache_dir = fake_home / "ag-cache-work"
    _write_antigravity_cache(
        ag_cache_dir / "sessions" / "ag-work.jsonl",
        session_id="ag-sess-work",
        response_id="ag-resp-work-1",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    _write_v2_profiles(
        settings.database_path,
        {
            "grok_local": {
                "grok-work": _profile_record("grok_local", "grok-work", grok_dir),
            },
            "muse_local": {
                "muse-work": _profile_record("muse_local", "muse-work", muse_dir),
            },
            "antigravity_local": {
                "antigravity-work": _profile_record(
                    "antigravity_local", "antigravity-work", ag_cache_dir
                ),
            },
        },
    )

    res = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert res["status"] == "success"
    assert res["eventsWritten"] == 3

    with spend_db.connect(settings.database_path) as conn:
        by_source = {
            row["source"]: row["tool_key"]
            for row in conn.execute("SELECT source, tool_key FROM usage_events")
        }
        muse_sessions = conn.execute(
            "SELECT session_id, tool_key FROM sessions WHERE session_id='muse-sess-work'"
        ).fetchall()

    assert by_source == {
        "grok_local": "grok-work",
        "muse_local": "muse-work",
        "antigravity_local": "antigravity-work",
    }
    assert [(r["session_id"], r["tool_key"]) for r in muse_sessions] == [
        ("muse-sess-work", "muse-work")
    ]


# ---------------------------------------------------------------------------
# 9. MissingLocation skipped cleanly & broken profile fails without blocking valid ones
# ---------------------------------------------------------------------------


def test_missing_location_skipped_cleanly_without_failing_pass(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    missing_dir = fake_home / ".claude-missing"
    valid_dir = fake_home / ".claude-valid"
    _write_claude_transcript(
        valid_dir / "projects" / "app" / "valid.jsonl",
        session_id="sess-valid",
        message_id="msg-valid",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)

    # Only a missing location -> skipped cleanly
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "claude-code-missing": _profile_record(
                    "claude_local", "claude-code-missing", missing_dir
                ),
            }
        },
    )
    only_missing = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert only_missing == {"status": "skipped", "eventsWritten": 0, "unpricedEventsWritten": 0}

    # Missing location alongside a valid profile -> succeeds and collects the valid profile
    _write_v2_profiles(
        settings.database_path,
        {
            "claude_local": {
                "claude-code-missing": _profile_record(
                    "claude_local", "claude-code-missing", missing_dir
                ),
                "claude-code-valid": _profile_record(
                    "claude_local", "claude-code-valid", valid_dir
                ),
            }
        },
    )
    with_valid = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert with_valid["status"] == "success"
    assert with_valid["eventsWritten"] == 1

    with spend_db.connect(settings.database_path) as conn:
        rows = conn.execute("SELECT session_id, tool_key FROM usage_events").fetchall()
    assert [(r["session_id"], r["tool_key"]) for r in rows] == [("sess-valid", "claude-code-valid")]


def test_broken_profile_returns_failed_status_while_collecting_remaining_valid_profiles(
    tmp_path: Path,
    _isolated_home_and_caches: Path,
) -> None:
    fake_home = _isolated_home_and_caches
    # Empty existing directory for antigravity_local causes antigravity_cache.ingest to raise ValueError
    broken_ag_dir = fake_home / "ag-empty-broken"
    broken_ag_dir.mkdir(parents=True)

    valid_muse_dir = fake_home / ".muse-valid"
    _write_muse_log(
        valid_muse_dir / "sessions" / "2026" / "11" / "12" / "m-ok.jsonl",
        session_id="muse-ok-sess",
        event_id="muse-ok-ev",
    )

    settings = _make_settings(tmp_path)
    pricing = PricingEngine.load(settings.pricing_path)
    _write_v2_profiles(
        settings.database_path,
        {
            "antigravity_local": {
                "antigravity-broken": _profile_record(
                    "antigravity_local", "antigravity-broken", broken_ag_dir
                ),
            },
            "muse_local": {
                "muse-valid": _profile_record("muse_local", "muse-valid", valid_muse_dir),
            },
        },
    )

    res = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert res["status"] == "failed"
    assert res["eventsWritten"] == 1
    assert "antigravity-broken:ValueError" in res.get("issues", [])

    with spend_db.connect(settings.database_path) as conn:
        events = conn.execute("SELECT session_id, tool_key FROM usage_events").fetchall()
        sessions = conn.execute("SELECT session_id, tool_key FROM sessions").fetchall()
    assert [(r["session_id"], r["tool_key"]) for r in events] == [("muse-ok-sess", "muse-valid")]
    assert [(r["session_id"], r["tool_key"]) for r in sessions] == [("muse-ok-sess", "muse-valid")]
