"""Transcripts written under ~/.claude/projects are attributed to the account that ran them.

Claude Code records a session in ``<config dir>/session-env/<sessionId>/`` of the account's
own config dir; the work account's Traycer-managed harness dir is found by account_folders.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from spend_app import claude_accounts, claude_session_lanes, connection_paths, connections, profile_detect, profile_store
from spend_app.adapters import claude_local, file_cache
from spend_app.adapters.claude_local import ingest, reset_file_cache
from spend_app.db import TOOL_KEY_OVERRIDES, connect, initialize
from spend_app.pricing import PricingEngine

ROOT = Path(__file__).resolve().parents[1]
PERSONAL, WORK, OTHER = "uuid-personal", "uuid-work", "uuid-other"
SID_TOP = "11111111-1111-4111-8111-111111111111"
SID_PERSONAL = "22222222-2222-4222-8222-222222222222"
SID_AMBIGUOUS = "33333333-3333-4333-8333-333333333333"
LANE = "claude-code-work"


def _clear() -> None:
    claude_session_lanes.reset_cache()
    claude_accounts.reset_cache()
    profile_detect.reset_cache()
    reset_file_cache()
    file_cache._LOADED.clear()
    file_cache._DATABASES.clear()
    connection_paths._DATABASE_IDENTITIES.clear()


@pytest.fixture(autouse=True)
def home(tmp_path: Path, monkeypatch) -> Path:
    fake = tmp_path / "home"
    fake.mkdir()
    monkeypatch.setenv("HOME", str(fake))
    monkeypatch.setenv("USERPROFILE", str(fake))
    for name in ("CLAUDE_CONFIG_DIR", "BURNRATE_CLAUDE_ACCOUNT_DIRS", "BURNRATE_CLAUDE_HARNESS_DIRS"):
        monkeypatch.delenv(name, raising=False)
    _clear()
    yield fake
    _clear()


def _signed_in(folder: Path, uuid: str, state: Path | None = None) -> None:
    folder.mkdir(parents=True, exist_ok=True)
    (state or folder / ".claude.json").write_text(json.dumps({"oauthAccount": {"accountUuid": uuid}}), encoding="utf-8")


def _session_env(folder: Path, *session_ids: str) -> None:
    for sid in session_ids:
        (folder / "session-env" / sid).mkdir(parents=True, exist_ok=True)


def _profile(provider: str, key: str, location: Path, *, enabled: bool = True) -> dict:
    return {
        "source": provider, "bindingId": provider if key == "default" else f"{provider}:{key}",
        "name": "Default" if key == "default" else key, "location": str(location),
        "origin": "default" if key == "default" else "manual", "enabled": enabled, "revision": 1,
        "toolKey": profile_store.profile_tool_key(provider, key), "accountDigest": None,
    }


def _store(database: Path, home: Path, *lanes: tuple[str, bool]) -> None:
    claude = {"default": _profile("claude_local", "default", home / ".claude" / "projects")}
    for key, enabled in lanes:
        claude[key] = _profile("claude_local", key, home / ".claude-work" / "projects", enabled=enabled)
    initialize(database)
    with connections.Store(database).transaction() as state:
        state["profiles"] = {"claude_local": claude}


def _transcript(path: Path, sid: str, message: str, *, tokens: int = 100) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant", "timestamp": "2026-11-12T12:00:00Z", "sessionId": sid, "cwd": "C:/work/demo",
        "uuid": f"uuid-{message}",
        "message": {"id": message, "model": "claude-opus-5", "usage": {
            "input_tokens": tokens, "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0, "output_tokens": 25}},
    }
    with path.open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(record) + "\n")
    return path


class World:
    """A personal account in ~/.claude and a work account used through a Traycer harness dir."""

    def __init__(self, tmp_path: Path, home: Path) -> None:
        self.home = home
        self.database = tmp_path / "spend.db"
        self.pricing = PricingEngine.load(ROOT / "pricing")
        self.projects = home / ".claude" / "projects"
        self.harness = home / ".traycer" / "host" / "harness-accounts" / "claude-code" / "profile-work"
        _signed_in(home / ".claude", PERSONAL, home / ".claude.json")
        (home / ".claude-work" / "projects").mkdir(parents=True)
        _signed_in(home / ".claude-work", WORK)
        _signed_in(self.harness, WORK)
        _store(self.database, home, (LANE, True))

    def ingest(self) -> dict:
        return ingest(database_path=self.database, pricing=self.pricing,
                      session_glob=str(self.projects / "**" / "*.jsonl"))

    def fresh_pass(self) -> dict:
        """A later pass (past the 60 s discovery cache); the file cache is kept."""
        claude_session_lanes.reset_cache()
        claude_accounts.reset_cache()
        profile_detect.reset_cache()
        return self.ingest()

    def rows(self, table: str = "usage_events") -> dict[str, str]:
        with connect(self.database) as connection:
            return {row["raw_id"]: row["tool_key"] for row in connection.execute(
                f"SELECT raw_id, tool_key FROM {table} WHERE source='claude_local'")}

    def by_session(self) -> dict[str, str]:
        with connect(self.database) as connection:
            return {row["session_id"]: row["tool_key"] for row in connection.execute(
                "SELECT session_id, tool_key FROM usage_events WHERE source='claude_local'")}

    def sessions(self) -> dict[str, str]:
        with connect(self.database) as connection:
            return {row["session_id"]: row["tool_key"] for row in connection.execute("SELECT session_id, tool_key FROM sessions")}


@pytest.fixture
def world(tmp_path: Path, home: Path) -> World:
    return World(tmp_path, home)


def _link(link: Path, target: Path) -> None:
    link.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.symlink(target, link, target_is_directory=True)
    except OSError:
        if os.name != "nt":
            raise
        subprocess.run(["cmd", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)


def test_session_id_comes_from_the_transcript_path() -> None:
    assert claude_session_lanes.session_id_for_path(Path("p/app/ABC.jsonl")) == "abc"
    assert claude_session_lanes.session_id_for_path(Path(f"p/app/{SID_TOP}/subagents/agent-1.jsonl")) == SID_TOP


def test_top_level_and_subagent_transcripts_move_to_the_work_lane(world: World) -> None:
    _session_env(world.harness, SID_TOP)
    _session_env(world.home / ".claude", SID_PERSONAL)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    _transcript(world.projects / "app" / SID_TOP / "subagents" / "agent-a1.jsonl", SID_TOP, "sub")
    _transcript(world.projects / "app" / f"{SID_PERSONAL}.jsonl", SID_PERSONAL, "mine")
    _transcript(world.projects / "app" / "44444444-4444-4444-8444-444444444444.jsonl",
                "44444444-4444-4444-8444-444444444444", "unknown-session")

    assert world.ingest()["status"] == "success"

    tools = world.by_session()
    assert tools[SID_TOP] == LANE
    assert sorted(world.rows().values()).count(LANE) == 2  # top-level + subagent rows
    assert tools[SID_PERSONAL] == "claude-code"
    assert tools["44444444-4444-4444-8444-444444444444"] == "claude-code"  # in no session-env
    sessions = world.sessions()
    assert sessions[SID_TOP] == LANE
    assert sessions[SID_PERSONAL] == "claude-code"


def test_session_in_the_dot_claude_work_folder_counts_too(world: World) -> None:
    _session_env(world.home / ".claude-work", SID_TOP)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    world.ingest()
    assert world.by_session() == {SID_TOP: LANE}


def test_ambiguous_session_stays_on_the_default_lane(world: World, home: Path) -> None:
    # Claimed by a second lane's account, and one seen in the personal profile as well.
    _signed_in(home / ".claude-other", OTHER)
    (home / ".claude-other" / "projects").mkdir()
    initialize(world.database)
    with connections.Store(world.database).transaction() as state:
        state["profiles"]["claude_local"]["claude-code-other"] = _profile(
            "claude_local", "claude-code-other", home / ".claude-other" / "projects")
    _session_env(world.harness, SID_AMBIGUOUS, SID_PERSONAL)
    _session_env(home / ".claude-other", SID_AMBIGUOUS)
    _session_env(home / ".claude", SID_PERSONAL)
    _transcript(world.projects / "app" / f"{SID_AMBIGUOUS}.jsonl", SID_AMBIGUOUS, "both")
    _transcript(world.projects / "app" / f"{SID_PERSONAL}.jsonl", SID_PERSONAL, "mine")
    world.ingest()
    assert world.by_session() == {SID_AMBIGUOUS: "claude-code", SID_PERSONAL: "claude-code"}


def test_disabled_lane_gets_no_attribution(tmp_path: Path, home: Path) -> None:
    world = World(tmp_path, home)
    _store(world.database, home, (LANE, False))
    _session_env(world.harness, SID_TOP)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    world.ingest()
    assert world.by_session() == {SID_TOP: "claude-code"}


def test_lane_signed_into_the_default_account_is_skipped(world: World, home: Path) -> None:
    _signed_in(home / ".claude-work", PERSONAL)  # "work" folder is really the personal account
    _signed_in(world.harness, PERSONAL)
    _session_env(world.harness, SID_TOP)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    world.ingest()
    assert world.by_session() == {SID_TOP: "claude-code"}


def test_linked_session_env_is_ignored(world: World, tmp_path: Path) -> None:
    elsewhere = tmp_path / "elsewhere"
    _session_env(elsewhere, SID_TOP)
    shutil_target = world.harness / "session-env"
    _link(shutil_target, elsewhere / "session-env")
    # A linked entry inside a real session-env directory is ignored as well.
    _link(world.home / ".claude-work" / "session-env" / SID_AMBIGUOUS, elsewhere / "session-env" / SID_TOP)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    _transcript(world.projects / "app" / f"{SID_AMBIGUOUS}.jsonl", SID_AMBIGUOUS, "other")
    world.ingest()
    assert world.by_session() == {SID_TOP: "claude-code", SID_AMBIGUOUS: "claude-code"}


def test_only_uuid_named_entries_count_and_the_listing_is_bounded(world: World, monkeypatch) -> None:
    _session_env(world.harness, "not-a-session", SID_TOP.upper())
    assert claude_session_lanes.session_lanes(world.database) == {SID_TOP: LANE}
    monkeypatch.setattr(claude_session_lanes, "MAX_SESSION_ENV_ENTRIES", 1)
    assert len(claude_session_lanes._session_ids(world.harness)) <= 1


def test_reingest_moves_existing_rows_in_place_and_journals_the_change(world: World) -> None:
    from datetime import UTC, datetime

    from spend_app.sync import apply_sync_push, export_sync_batch

    hub = world.database.parent / "hub.db"
    # Import stamps ingested_at using the process clock. The hub should use a
    # matching clock while accepting the fixture's historical transcripts.
    now = max(datetime.now(UTC), datetime(2026, 11, 13, tzinfo=UTC))
    initialize(hub)
    path = _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    _transcript(world.projects / "app" / f"{SID_PERSONAL}.jsonl", SID_PERSONAL, "mine")
    world.ingest()
    before = world.rows()
    assert set(before.values()) == {"claude-code"}
    initial, cursor = export_sync_batch(world.database)
    apply_sync_push(hub, world.pricing, machine_id="spoke", body=initial, now=now)
    with connect(hub) as connection:
        assert {r["tool_key"] for r in connection.execute("SELECT tool_key FROM usage_events")} == {"claude-code"}
    with connect(world.database) as connection:
        mark = connection.execute("SELECT COALESCE(MAX(seq),0) FROM usage_changes").fetchone()[0]

    # The session shows up in the work account's session-env (the file itself is untouched).
    _session_env(world.harness, SID_TOP)
    parsed = []
    original = claude_local.parse_file_with_health
    claude_local.parse_file_with_health = lambda p: parsed.append(Path(p).name) or original(p)
    try:
        summary = world.fresh_pass()
    finally:
        claude_local.parse_file_with_health = original

    assert summary["status"] == "success"
    assert parsed == [path.name]  # only the file that moved lanes was re-read
    after = world.rows()
    assert set(after) == set(before)  # same raw ids: updated in place, no duplicates
    assert sorted(after.values()) == ["claude-code", LANE]
    assert world.sessions()[SID_TOP] == LANE
    moved = {raw for raw, tool in after.items() if tool == LANE}
    with connect(world.database) as connection:
        journaled = {row[0] for row in connection.execute("SELECT raw_id FROM usage_changes WHERE seq > ?", (mark,))}
    assert journaled == moved
    batch, _ = export_sync_batch(world.database, cursor=cursor)
    assert {change["raw_id"] for change in batch["usageChanges"]["changes"]} == moved
    apply_sync_push(hub, world.pricing, machine_id="spoke", body=batch, now=now)
    with connect(hub) as connection:
        hub_rows = [r["tool_key"] for r in connection.execute("SELECT tool_key FROM usage_events")]
    assert sorted(hub_rows) == ["claude-code", LANE]  # the hub updated the moved row, no duplicate

    # A third pass is quiet again: nothing re-read, nothing journaled.
    parsed.clear()
    claude_local.parse_file_with_health = lambda p: parsed.append(Path(p).name) or original(p)
    try:
        world.fresh_pass()
    finally:
        claude_local.parse_file_with_health = original
    assert parsed == []


def test_attribution_survives_claude_code_pruning_the_session_env(world: World) -> None:
    _session_env(world.harness, SID_TOP)
    path = _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "first")
    _transcript(world.projects / "app" / f"{SID_PERSONAL}.jsonl", SID_PERSONAL, "mine")
    world.ingest()
    with connect(world.database) as connection:
        assert claude_session_lanes.read_remembered(connection) == {SID_TOP: LANE}  # only attributed ids are kept

    import shutil
    shutil.rmtree(world.harness / "session-env")  # pruned after ~30 days
    assert claude_session_lanes.session_lanes(world.database) == {SID_TOP: LANE}
    _clear()
    assert claude_session_lanes.session_lanes(world.database) == {SID_TOP: LANE}

    _transcript(path, SID_TOP, "second")  # the session continues; its file is re-read
    world.fresh_pass()
    assert set(world.by_session().values()) == {LANE, "claude-code"}
    assert world.by_session()[SID_TOP] == LANE
    assert world.sessions()[SID_TOP] == LANE
    assert len(world.rows()) == 3


def test_remembered_attribution_is_ignored_not_deleted_while_the_lane_is_disabled(world: World, home: Path) -> None:
    _session_env(world.harness, SID_TOP)
    _transcript(world.projects / "app" / f"{SID_TOP}.jsonl", SID_TOP, "top")
    world.ingest()
    _store(world.database, home, (LANE, False))
    _clear()
    assert claude_session_lanes.session_lanes(world.database) == {}
    with connect(world.database) as connection:
        assert claude_session_lanes.read_remembered(connection) == {SID_TOP: LANE}
    _store(world.database, home, (LANE, True))
    _clear()
    assert claude_session_lanes.session_lanes(world.database) == {SID_TOP: LANE}


def test_default_only_install_never_scans_the_home_folder(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "spend.db"
    initialize(database)
    monkeypatch.setattr(claude_accounts, "account_folders", lambda *_a, **_k: pytest.fail("scanned"))
    assert claude_session_lanes.session_lanes(database) == {}


def test_profile_runs_keep_their_override_and_skip_attribution(world: World, monkeypatch) -> None:
    monkeypatch.setattr(claude_session_lanes, "session_lanes", lambda _db: pytest.fail("profile run asked for lanes"))
    folder = world.home / ".claude-work" / "projects"
    _transcript(folder / "w" / f"{SID_TOP}.jsonl", SID_TOP, "work-profile")
    token = TOOL_KEY_OVERRIDES.set({"claude-code": LANE})
    try:
        summary = ingest(database_path=world.database, pricing=world.pricing, session_glob=str(folder / "**" / "*.jsonl"))
    finally:
        TOOL_KEY_OVERRIDES.reset(token)
    assert summary["status"] == "success"
    assert world.by_session() == {SID_TOP: LANE}
    assert world.sessions() == {SID_TOP: LANE}
