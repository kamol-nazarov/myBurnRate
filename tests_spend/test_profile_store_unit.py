"""Unit and persistence tests for spend_app.profile_store and multi-profile connections."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest

from spend_app import connections as c
from spend_app import db as spend_db
from spend_app import profile_store as ps
from spend_app.claude_accounts import ClaudeAccount
from spend_app.connection_paths import LocationError
from spend_app.opencode_profiles import OpenCodeProfile
from spend_app.providers import REGISTRY


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _create_meta_db(db_path: Path, entries: dict[str, str] | None = None, *, with_ingest_runs: bool = False) -> None:
    with spend_db.connect(db_path) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
        if with_ingest_runs:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS ingest_runs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    status TEXT NOT NULL,
                    events_written INTEGER NOT NULL DEFAULT 0,
                    error TEXT
                )
                """
            )
        for key, value in (entries or {}).items():
            conn.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (key, value),
            )


def _read_app_meta(db_path: Path) -> dict[str, str]:
    with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        rows = conn.execute("SELECT key, value FROM app_meta").fetchall()
    return {row[0]: row[1] for row in rows}


def _v1_payload(
    *,
    bindings: dict | None = None,
    requests: dict | None = None,
    legacy: list | None = None,
    cleanup: list | None = None,
) -> dict:
    return {
        "version": 1,
        "bindings": bindings if bindings is not None else {},
        "requests": requests if requests is not None else {},
        "legacy": legacy if legacy is not None else [],
        "cleanup": cleanup if cleanup is not None else [],
    }


def _mutate_body(source: str = "codex_local", revision: int = 0, operation: str = "connect", **kwargs) -> dict:
    return {
        "source": source,
        "revision": revision,
        "operation": operation,
        "location": "C:/fake/sessions",
        "requestId": str(uuid4()),
        **kwargs,
    }


class _FakeInspector:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.usable = False
        self.fail = False

    def normalize(self, raw: str | None, meta) -> str:
        if self.fail or not raw:
            raise LocationError("Missing location")
        return raw

    def inspect(self, source: str, path: str, meta) -> dict:
        self.calls.append((source, path))
        return {"state": "readable", "usableSample": self.usable, "detail": "Readable sample"}


class _FakeVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.enabled = True

    def available(self) -> bool:
        return self.enabled

    def put(self, secret: str, ref: str | None = None) -> str:
        ref = ref or f"burnrate-{uuid4().hex}"
        self.values[ref] = secret
        return ref

    def get(self, ref: str) -> str:
        return self.values[ref]

    def delete(self, ref: str) -> None:
        self.values.pop(ref, None)


def _make_service(tmp_path: Path, monkeypatch) -> c.Service:
    for name in (
        "OPENAI_ADMIN_KEY",
        "ANTHROPIC_ADMIN_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "DEEPSEEK_BALANCE_KEY",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_DATA_HOME",
        "GROK_HOME",
    ):
        monkeypatch.delenv(name, raising=False)
    db_path = tmp_path / "burnrate.db"
    settings = SimpleNamespace(
        database_path=db_path,
        openai_admin_key=None,
        anthropic_admin_key=None,
    )
    return c.Service(
        settings,
        c.Store(db_path),
        _FakeInspector(),
        _FakeVault(),
        Mock(),
    )


# ---------------------------------------------------------------------------
# 1. DEFAULT_TOOL_KEYS & supports_profiles
# ---------------------------------------------------------------------------


def test_default_tool_keys_and_supports_profiles():
    expected = {
        "claude_local": "claude-code",
        "codex_local": "codex",
        "opencode_local": "opencode-go",
        "zcode_local": "zcode",
        "grok_local": "grok",
        "antigravity_local": "antigravity",
        "muse_local": "muse",
    }
    assert ps.DEFAULT_TOOL_KEYS == expected
    assert len(ps.DEFAULT_TOOL_KEYS) == 7

    for provider in expected:
        assert ps.supports_profiles(provider) is True
        assert ps.profile_tool_key(provider, "default") == expected[provider]
        assert ps.profile_tool_key(provider, f"{expected[provider]}-work") == f"{expected[provider]}-work"

    excluded = (
        "traycer_local",
        "openai_admin",
        "anthropic_admin",
        "openrouter",
        "deepseek",
        "xai",
        "claude_local_alt",
        "",
        "unknown_provider",
    )
    for provider in excluded:
        assert ps.supports_profiles(provider) is False
        assert ps.profile_tool_key(provider, "default") is None


# ---------------------------------------------------------------------------
# 2. profile_key_for_folder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider,folder,expected",
    [
        ("claude_local", Path("/home/user/.claude-work"), "claude-code-work"),
        ("claude_local", Path("/home/user/My Team Folder!"), "claude-code-my-team-folder"),
        ("claude_local", Path("/home/user/..."), "claude-code-account"),
        ("opencode_local", Path("/home/user/.opencode-go-2"), "opencode-go-2"),
        ("opencode_local", Path("/home/user/.opencode-work"), "opencode-go-work"),
        ("opencode_local", Path("/home/user/..."), "opencode-go-profile"),
        ("codex_local", Path("/home/user/.codex-work"), "codex-work"),
        ("codex_local", Path("/home/user/codex_team"), "codex-team"),
        ("codex_local", Path("/home/user/custom-dir"), "codex-custom-dir"),
        ("codex_local", Path("/home/user/.codex-"), "codex-profile"),
        ("zcode_local", Path("/home/user/.zcode-alt"), "zcode-alt"),
        ("grok_local", Path("/home/user/.grok-work"), "grok-work"),
        ("antigravity_local", Path("/home/user/.antigravity_beta"), "antigravity-beta"),
        ("muse_local", Path("/home/user/.muse-studio"), "muse-studio"),
    ],
)
def test_profile_key_for_folder_derivations(provider, folder, expected):
    assert ps.profile_key_for_folder(provider, folder) == expected


def test_profile_key_for_folder_collision_handling():
    folder = Path("/home/user/.codex-work")
    assert ps.profile_key_for_folder("codex_local", folder, existing=()) == "codex-work"
    assert ps.profile_key_for_folder("codex_local", folder, existing=("codex-work",)) == "codex-work-2"
    assert (
        ps.profile_key_for_folder(
            "codex_local", folder, existing=("codex-work", "codex-work-2")
        )
        == "codex-work-3"
    )
    assert (
        ps.profile_key_for_folder(
            "claude_local",
            Path("/home/user/.claude-team"),
            existing={"claude-code-team", "claude-code-team-2", "claude-code-team-3"},
        )
        == "claude-code-team-4"
    )


@pytest.mark.parametrize(
    "provider",
    ["traycer_local", "openai_admin", "anthropic_admin", "openrouter", "deepseek", "unknown"],
)
def test_profile_key_for_folder_rejects_unsupported_provider(provider):
    with pytest.raises(ValueError, match="does not support multiple profiles"):
        ps.profile_key_for_folder(provider, Path("/home/user/.some-dir"))


# ---------------------------------------------------------------------------
# 3. v1 -> v2 migration
# ---------------------------------------------------------------------------


def test_migrate_v1_to_v2_plain_state_and_api_providers():
    v1_state = _v1_payload(
        bindings={
            "codex_local": {
                "source": "codex_local",
                "enabled": True,
                "revision": 3,
                "location": "C:/Users/alice/.codex",
                "state": "receiving_usage",
                "bindingId": "codex_local",
            },
            "openai_admin": {
                "source": "openai_admin",
                "enabled": True,
                "revision": 2,
                "location": "",
                "mode": "api",
                "credentialRef": "burnrate-abc",
                "state": "awaiting_ingest",
            },
            "traycer_local": {
                "source": "traycer_local",
                "enabled": False,
                "revision": 1,
                "location": "C:/Users/alice/.traycer",
                "state": "disabled",
            },
        },
        requests={"req-1": {"fingerprint": "fp-1", "result": {"requestId": "req-1"}}},
        legacy=["codex_local", "grok_local"],
        cleanup=["burnrate-orphan"],
    )

    v2 = ps.migrate_v1_to_v2(
        v1_state, legacy_sources=["grok_local", "muse_local"], discover=False
    )

    assert v2["version"] == 2
    assert "bindings" not in v2
    assert v2["requests"] == {"req-1": {"fingerprint": "fp-1", "result": {"requestId": "req-1"}}}
    assert v2["cleanup"] == ["burnrate-orphan"]
    assert v2["legacy"] == ["codex_local", "grok_local", "muse_local"]

    codex = v2["profiles"]["codex_local"]["default"]
    assert codex["name"] == "Default"
    assert codex["origin"] == "default"
    assert codex["location"] == "C:/Users/alice/.codex"
    assert codex["toolKey"] == "codex"
    assert codex["enabled"] is True
    assert codex["revision"] == 3
    assert codex["accountDigest"] is None

    openai = v2["profiles"]["openai_admin"]["default"]
    assert openai["name"] == "Default"
    assert openai["origin"] == "default"
    assert openai["location"] == ""
    assert openai["toolKey"] is None
    assert openai["credentialRef"] == "burnrate-abc"

    traycer = v2["profiles"]["traycer_local"]["default"]
    assert traycer["name"] == "Default"
    assert traycer["origin"] == "default"
    assert traycer["toolKey"] is None
    assert traycer["enabled"] is False

    # Mutating v1 after migration does not mutate v2
    v1_state["requests"]["req-2"] = {}
    v1_state["cleanup"].append("extra")
    assert "req-2" not in v2["requests"]
    assert v2["cleanup"] == ["burnrate-orphan"]


def test_ensure_migrated_and_store_bootstrap_preserve_v1_and_never_persist_bindings(tmp_path):
    db_path = tmp_path / "v1.db"
    v1_raw = json.dumps(
        _v1_payload(
            bindings={
                "codex_local": {
                    "source": "codex_local",
                    "enabled": True,
                    "revision": 1,
                    "location": "C:/Users/alice/.codex",
                    "state": "awaiting_ingest",
                },
                "openai_admin": {
                    "source": "openai_admin",
                    "enabled": True,
                    "revision": 1,
                    "location": "",
                    "mode": "api",
                },
            },
            requests={"r1": {"fingerprint": "f1", "result": {"ok": True}}},
            legacy=["codex_local"],
            cleanup=["burnrate-1"],
        )
    )
    _create_meta_db(db_path, {ps.V1_KEY: v1_raw}, with_ingest_runs=True)
    with spend_db.connect(db_path) as conn:
        conn.execute(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written) "
            "VALUES('zcode_local', '2026-08-30T10:00:00Z', '2026-08-30T10:01:00Z', 'success', 4)"
        )
        conn.execute(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written) "
            "VALUES('grok_local', '2026-08-30T10:00:00Z', '2026-08-30T10:01:00Z', 'partial', 2)"
        )
        conn.execute(
            "INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written) "
            "VALUES('muse_local', '2026-08-30T10:00:00Z', '2026-08-30T10:01:00Z', 'failed', 0)"
        )

    c.Store(db_path).bootstrap()

    meta = _read_app_meta(db_path)
    assert meta[ps.V1_KEY] == v1_raw
    assert meta[ps.MIGRATED_KEY] == "1"
    persisted_v2 = json.loads(meta[ps.KEY])
    assert "bindings" not in persisted_v2
    assert persisted_v2["version"] == 2
    assert persisted_v2["requests"] == {"r1": {"fingerprint": "f1", "result": {"ok": True}}}
    assert persisted_v2["cleanup"] == ["burnrate-1"]
    assert set(persisted_v2["legacy"]) == {"codex_local", "zcode_local", "grok_local"}
    assert persisted_v2["profiles"]["codex_local"]["default"]["toolKey"] == "codex"
    assert persisted_v2["profiles"]["openai_admin"]["default"]["toolKey"] is None


def test_db_initialize_migrates_v1_state_and_is_idempotent(tmp_path, monkeypatch):
    db_path = tmp_path / "init.db"
    spend_db.initialize(db_path)

    v1_raw = json.dumps(
        _v1_payload(
            bindings={
                "codex_local": {
                    "source": "codex_local",
                    "enabled": True,
                    "revision": 2,
                    "location": "C:/Users/alice/.codex",
                    "state": "receiving_usage",
                }
            },
            legacy=["codex_local"],
        )
    )
    # Reset connection metadata back to v1-only before re-running initialize
    with spend_db.connect(db_path) as conn:
        conn.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (ps.V1_KEY, v1_raw),
        )

    extra_acc = ClaudeAccount(
        key="claude-code-work",
        label="Work",
        config_dir=tmp_path / ".claude-work",
        state_path=tmp_path / ".claude-work" / ".claude.json",
        account_uuid="work-uuid",
        plan="Claude Max",
    )
    monkeypatch.setattr("spend_app.claude_accounts.extra_accounts", lambda: (extra_acc,))

    spend_db.initialize(db_path)

    meta = _read_app_meta(db_path)
    assert meta[ps.MIGRATED_KEY] == "1"
    assert meta[ps.V1_KEY] == v1_raw
    v2 = json.loads(meta[ps.KEY])
    assert "bindings" not in v2
    assert v2["profiles"]["codex_local"]["default"]["revision"] == 2
    assert v2["profiles"]["claude_local"]["claude-code-work"]["origin"] == "detected"

    # Subsequent user edits and deletions survive re-running initialize/bootstrap/ensure_migrated
    with c.Store(db_path).transaction() as state:
        del state["profiles"]["claude_local"]["claude-code-work"]
        state["profiles"]["codex_local"]["default"]["name"] = "Primary Codex"
        state["profiles"]["codex_local"]["default"]["enabled"] = False

    spend_db.initialize(db_path)
    c.Store(db_path).bootstrap()
    with spend_db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        after = ps.ensure_migrated(conn)

    assert "claude-code-work" not in after["profiles"].get("claude_local", {})
    assert after["profiles"]["codex_local"]["default"]["name"] == "Primary Codex"
    assert after["profiles"]["codex_local"]["default"]["enabled"] is False
    assert _read_app_meta(db_path)[ps.V1_KEY] == v1_raw


def test_claude_local_alt_and_default_claude_digest_migration(tmp_path):
    home = tmp_path / "home"
    default_dir = home / ".claude"
    default_dir.mkdir(parents=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "default-uuid-1"}}), encoding="utf-8"
    )

    alt_projects_dir = tmp_path / ".claude-work"
    (alt_projects_dir / "projects").mkdir(parents=True)
    (alt_projects_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "alt-uuid-1"}}), encoding="utf-8"
    )

    alt_transcripts_dir = tmp_path / ".claude-team"
    (alt_transcripts_dir / "transcripts").mkdir(parents=True)
    # Missing .claude.json -> accountDigest should be None

    v1_projects = _v1_payload(
        bindings={
            "claude_local": {
                "source": "claude_local",
                "enabled": True,
                "revision": 1,
                "location": str(default_dir),
                "state": "receiving_usage",
            },
            "claude_local_alt": {
                "source": "claude_local_alt",
                "enabled": True,
                "revision": 4,
                "location": str(alt_projects_dir / "projects"),
                "state": "awaiting_ingest",
            },
        }
    )

    v2 = ps.migrate_v1_to_v2(v1_projects, discover=False)
    assert "claude_local_alt" not in v2["profiles"]
    claude_profiles = v2["profiles"]["claude_local"]
    assert claude_profiles["default"]["accountDigest"] == _sha256("default-uuid-1")
    assert claude_profiles["default"]["toolKey"] == "claude-code"

    alt = claude_profiles["claude-code-work"]
    assert alt["origin"] == "manual"
    assert alt["source"] == "claude_local"
    assert alt["bindingId"] == "claude_local:claude-code-work"
    assert alt["toolKey"] == "claude-code-work"
    assert alt["revision"] == 4
    assert alt["location"] == str(alt_projects_dir / "projects")
    assert alt["accountDigest"] == _sha256("alt-uuid-1")

    # Trailing /transcripts stripped and unreadable/missing .claude.json gives accountDigest=None
    v1_transcripts = _v1_payload(
        bindings={
            "claude_local": {
                "source": "claude_local",
                "enabled": True,
                "revision": 1,
                "location": str(tmp_path / "missing-default"),
            },
            "claude_local_alt": {
                "source": "claude_local_alt",
                "enabled": True,
                "revision": 2,
                "location": str(alt_transcripts_dir / "transcripts"),
            },
        }
    )
    v2_t = ps.migrate_v1_to_v2(v1_transcripts, discover=False)
    assert v2_t["profiles"]["claude_local"]["default"]["accountDigest"] is None
    alt_t = v2_t["profiles"]["claude_local"]["claude-code-team"]
    assert alt_t["bindingId"] == "claude_local:claude-code-team"
    assert alt_t["toolKey"] == "claude-code-team"
    assert alt_t["origin"] == "manual"
    assert alt_t["accountDigest"] is None


def test_migration_discovers_extra_claude_and_opencode_profiles_and_deduplicates(tmp_path, monkeypatch):
    work_dir = tmp_path / ".claude-work"
    (work_dir / "projects").mkdir(parents=True)
    (work_dir / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "uuid-work"}}), encoding="utf-8"
    )
    other_dir = tmp_path / ".claude-other"
    other_dir.mkdir(parents=True)
    dup_digest_dir = tmp_path / ".claude-dup-uuid"
    dup_digest_dir.mkdir(parents=True)

    oc_default_db = tmp_path / "opencode-default" / "opencode.db"
    oc_default_db.parent.mkdir(parents=True)
    oc_default_db.write_bytes(b"")
    oc_extra_dir = tmp_path / ".opencode-go-2"
    oc_extra_db = oc_extra_dir / "opencode" / "opencode.db"
    oc_extra_db.parent.mkdir(parents=True)
    oc_extra_db.write_bytes(b"")

    monkeypatch.setattr(
        "spend_app.claude_accounts.extra_accounts",
        lambda: (
            # Same folder (once /projects is stripped) as claude_local_alt -> deduped
            ClaudeAccount(
                key="claude-code-work",
                label="Work",
                config_dir=work_dir,
                state_path=work_dir / ".claude.json",
                account_uuid="uuid-work",
                plan="Claude Max",
            ),
            # Different folder, same accountUuid -> deduped by accountDigest
            ClaudeAccount(
                key="claude-code-dup-uuid",
                label="Dup Uuid",
                config_dir=dup_digest_dir,
                state_path=dup_digest_dir / ".claude.json",
                account_uuid="uuid-work",
                plan="Claude Pro",
            ),
            # Distinct account -> added as detected
            ClaudeAccount(
                key="claude-code-other",
                label="Other",
                config_dir=other_dir,
                state_path=other_dir / ".claude.json",
                account_uuid="uuid-other",
                plan="Claude Pro",
            ),
        ),
    )
    monkeypatch.setattr(
        "spend_app.opencode_profiles.extra_profiles",
        lambda: (
            # Same DB location as default opencode_local -> deduped
            OpenCodeProfile(
                key="opencode-go-dup",
                label="Dup",
                data_dir=oc_default_db.parent,
                database=oc_default_db,
            ),
            # Distinct profile -> added as detected
            OpenCodeProfile(
                key="opencode-go-2",
                label="Profile 2",
                data_dir=oc_extra_dir,
                database=oc_extra_db,
            ),
        ),
    )

    v1_state = _v1_payload(
        bindings={
            "claude_local_alt": {
                "source": "claude_local_alt",
                "enabled": True,
                "revision": 2,
                "location": str(work_dir / "projects"),
            },
            "opencode_local": {
                "source": "opencode_local",
                "enabled": True,
                "revision": 1,
                "location": str(oc_default_db),
            },
        }
    )

    v2 = ps.migrate_v1_to_v2(v1_state, discover=True)

    claude_profiles = v2["profiles"]["claude_local"]
    assert set(claude_profiles) == {"claude-code-work", "claude-code-other"}
    assert claude_profiles["claude-code-work"]["origin"] == "manual"
    assert claude_profiles["claude-code-work"]["revision"] == 2

    detected_claude = claude_profiles["claude-code-other"]
    assert detected_claude["origin"] == "detected"
    assert detected_claude["enabled"] is True
    assert detected_claude["revision"] == 1
    assert detected_claude["state"] == "awaiting_ingest"
    assert detected_claude["bindingId"] == "claude_local:claude-code-other"
    assert detected_claude["toolKey"] == "claude-code-other"
    assert detected_claude["accountDigest"] == _sha256("uuid-other")
    assert detected_claude["location"] == str(other_dir)

    oc_profiles = v2["profiles"]["opencode_local"]
    assert set(oc_profiles) == {"default", "opencode-go-2"}
    detected_oc = oc_profiles["opencode-go-2"]
    assert detected_oc["origin"] == "detected"
    assert detected_oc["enabled"] is True
    assert detected_oc["revision"] == 1
    assert detected_oc["state"] == "awaiting_ingest"
    assert detected_oc["bindingId"] == "opencode_local:opencode-go-2"
    assert detected_oc["toolKey"] == "opencode-go-2"
    assert detected_oc["accountDigest"] is None
    assert detected_oc["location"] == str(oc_extra_db)


def test_ensure_migrated_marker_atomic_and_respects_existing_marker(tmp_path, monkeypatch):
    db_path = tmp_path / "atomic.db"
    v1_raw = json.dumps(
        _v1_payload(
            bindings={
                "codex_local": {
                    "source": "codex_local",
                    "enabled": True,
                    "revision": 1,
                    "location": "C:/fake/.codex",
                }
            }
        )
    )
    _create_meta_db(db_path, {ps.V1_KEY: v1_raw})

    # If the transaction rolls back, neither connections.v2 nor profiles.migrated.v1 is persisted
    with pytest.raises(RuntimeError, match="abort"):
        with spend_db.connect(db_path) as conn:
            conn.execute("BEGIN IMMEDIATE")
            ps.ensure_migrated(conn)
            raise RuntimeError("abort")

    meta_after_rollback = _read_app_meta(db_path)
    assert ps.KEY not in meta_after_rollback
    assert ps.MIGRATED_KEY not in meta_after_rollback
    assert meta_after_rollback[ps.V1_KEY] == v1_raw

    # Commit writes both connections.v2 and profiles.migrated.v1 in one transaction
    with spend_db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        first = ps.ensure_migrated(conn)

    meta_committed = _read_app_meta(db_path)
    assert meta_committed[ps.MIGRATED_KEY] == "1"
    assert json.loads(meta_committed[ps.KEY]) == first

    # Re-running ensure_migrated when marker is present is a no-op even if extra_accounts changes
    monkeypatch.setattr(
        "spend_app.claude_accounts.extra_accounts",
        lambda: (_ for _ in ()).throw(AssertionError("should not rediscover")),
    )
    with spend_db.connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        second = ps.ensure_migrated(conn)
    assert second == first


# ---------------------------------------------------------------------------
# 4. Read-only guarantees & Profile accessors
# ---------------------------------------------------------------------------


def test_load_on_readonly_connection_migrates_in_memory_without_writing(tmp_path, monkeypatch):
    db_path = tmp_path / "ro-v1.db"
    v1_raw = json.dumps(
        _v1_payload(
            bindings={
                "codex_local": {
                    "source": "codex_local",
                    "enabled": True,
                    "revision": 2,
                    "location": "C:/fake/.codex",
                }
            },
            legacy=["codex_local"],
        )
    )
    _create_meta_db(db_path, {ps.V1_KEY: v1_raw})
    monkeypatch.setattr(
        "spend_app.claude_accounts.extra_accounts",
        lambda: (
            ClaudeAccount(
                key="claude-code-ro",
                label="Ro",
                config_dir=tmp_path / ".claude-ro",
                state_path=tmp_path / ".claude-ro" / ".claude.json",
                account_uuid="uuid-ro",
                plan=None,
            ),
        ),
    )

    with closing(sqlite3.connect(db_path.absolute().as_uri() + "?mode=ro", uri=True)) as ro_conn:
        loaded = ps.load(ro_conn)

    assert loaded["version"] == 2
    assert loaded["profiles"]["codex_local"]["default"]["location"] == "C:/fake/.codex"
    assert loaded["profiles"]["claude_local"]["claude-code-ro"]["origin"] == "detected"

    meta = _read_app_meta(db_path)
    assert set(meta) == {ps.V1_KEY}
    assert meta[ps.V1_KEY] == v1_raw


def test_load_returns_empty_state_when_app_meta_missing_or_empty(tmp_path):
    empty_db = tmp_path / "no-tables.db"
    with closing(sqlite3.connect(empty_db)) as conn:
        assert ps.load(conn) == ps.empty_state()

    meta_only_db = tmp_path / "meta-only.db"
    _create_meta_db(meta_only_db, {})
    with closing(sqlite3.connect(meta_only_db.absolute().as_uri() + "?mode=ro", uri=True)) as conn:
        assert ps.load(conn) == ps.empty_state()


def test_enabled_profiles_missing_uninitialized_and_populated(tmp_path):
    missing = tmp_path / "does-not-exist.db"
    assert ps.enabled_profiles(missing) == []
    assert not missing.exists()

    uninitialized = tmp_path / "uninitialized.db"
    with closing(sqlite3.connect(uninitialized)):
        pass
    assert ps.enabled_profiles(uninitialized) == []

    populated = tmp_path / "populated.db"
    v2_state = ps.empty_state()
    v2_state["profiles"] = {
        "claude_local": {
            "default": {
                "source": "claude_local",
                "name": "Default",
                "location": "C:/Users/alice/.claude",
                "origin": "default",
                "enabled": True,
                "revision": 2,
                "accountDigest": "digest-default",
                "toolKey": "claude-code",
            },
            "claude-code-work": {
                "source": "claude_local",
                "name": "Work",
                "location": "C:/Users/alice/.claude-work",
                "origin": "detected",
                "enabled": False,
                "revision": 1,
                "accountDigest": "digest-work",
                "toolKey": "claude-code-work",
            },
        },
        "opencode_local": {
            "opencode-go-2": {
                "source": "opencode_local",
                "name": "Profile 2",
                "location": "C:/Users/alice/.opencode-go-2/opencode.db",
                "origin": "detected",
                "enabled": True,
                "revision": 1,
                "accountDigest": None,
                "toolKey": "opencode-go-2",
            }
        },
        "openai_admin": {
            "default": {
                "source": "openai_admin",
                "name": "Default",
                "location": "",
                "origin": "default",
                "enabled": True,
                "revision": 1,
                "accountDigest": None,
                "toolKey": None,
            }
        },
    }
    _create_meta_db(
        populated,
        {ps.KEY: ps.encode(v2_state), ps.MIGRATED_KEY: "1"},
    )

    enabled = ps.enabled_profiles(populated)
    assert [(p.provider, p.key, p.account_digest, p.tool_key) for p in enabled] == [
        ("claude_local", "default", "digest-default", "claude-code"),
        ("opencode_local", "opencode-go-2", None, "opencode-go-2"),
        ("openai_admin", "default", None, None),
    ]
    assert all(isinstance(p, ps.Profile) and p.enabled is True for p in enabled)


def test_profiles_accessor_filtering_and_shapes():
    state = {
        "version": 2,
        "profiles": {
            "claude_local": {
                "default": {
                    "name": "Default",
                    "location": "/a",
                    "origin": "default",
                    "enabled": True,
                    "revision": 1,
                    "accountDigest": "d1",
                    "toolKey": "claude-code",
                },
                "claude-code-work": {
                    "name": "Work",
                    "location": "/b",
                    "origin": "manual",
                    "enabled": False,
                    "revision": 3,
                    "accountDigest": "d2",
                    "toolKey": "claude-code-work",
                },
            },
            "openai_admin": {
                "default": {
                    "name": "OpenAI",
                    "location": "",
                    "origin": "default",
                    "enabled": True,
                    "revision": 2,
                    "accountDigest": None,
                    "toolKey": None,
                }
            },
        },
        "dismissed": {},
        "requests": {},
        "legacy": [],
        "cleanup": [],
    }

    all_profiles = ps.profiles(state)
    assert len(all_profiles) == 3
    claude_only = ps.profiles(state, provider="claude_local")
    assert [p.key for p in claude_only] == ["default", "claude-code-work"]
    assert claude_only[0].tool_key == "claude-code"
    assert claude_only[1].tool_key == "claude-code-work"
    assert claude_only[1].enabled is False
    api_only = ps.profiles(state, provider="openai_admin")
    assert api_only == [
        ps.Profile(
            provider="openai_admin",
            key="default",
            name="OpenAI",
            location="",
            origin="default",
            enabled=True,
            revision=2,
            account_digest=None,
            tool_key=None,
        )
    ]
    assert ps.profiles(state, provider="codex_local") == []


def test_readonly_helpers_prefer_v2_and_fallback_to_v1(tmp_path, monkeypatch):
    v1_state = _v1_payload(
        bindings={
            "codex_local": {
                "source": "codex_local",
                "enabled": True,
                "revision": 1,
                "location": "C:/v1/codex",
            },
            "grok_local": {
                "source": "grok_local",
                "enabled": True,
                "revision": 1,
                "location": "C:/v1/grok",
            },
        }
    )
    v2_state = ps.empty_state()
    v2_state["profiles"] = {
        "codex_local": {
            "default": {
                "source": "codex_local",
                "enabled": True,
                "revision": 5,
                "location": "C:/v2/codex",
                "toolKey": "codex",
            },
            "codex-work": {
                "source": "codex_local",
                "enabled": True,
                "revision": 1,
                "location": "C:/v2/codex-work",
                "toolKey": "codex-work",
            },
        }
    }

    # Empty DB -> None / {}
    empty_db = tmp_path / "empty-helpers.db"
    _create_meta_db(empty_db, {})
    with closing(sqlite3.connect(empty_db)) as conn:
        assert ps.load_config_if_present(conn) is None
        assert ps.load_bindings_map(conn) == {}
    assert ps.default_binding_from_records({}, "codex_local") is None

    # v1 only -> falls back cleanly without running filesystem discovery
    monkeypatch.setattr(
        "spend_app.claude_accounts.extra_accounts",
        lambda: (_ for _ in ()).throw(AssertionError("load_config_if_present must not discover")),
    )
    v1_db = tmp_path / "v1-helpers.db"
    _create_meta_db(v1_db, {ps.V1_KEY: json.dumps(v1_state)})
    with closing(sqlite3.connect(v1_db)) as conn:
        cfg_v1 = ps.load_config_if_present(conn)
        assert cfg_v1 is not None
        assert cfg_v1["bindings"]["codex_local"]["location"] == "C:/v1/codex"
        assert ps.load_bindings_map(conn)["grok_local"]["location"] == "C:/v1/grok"
    assert (
        ps.default_binding_from_records({ps.V1_KEY: json.dumps(v1_state)}, "codex_local")["location"]
        == "C:/v1/codex"
    )

    # Both v1 and v2 present -> prefers v2
    both_db = tmp_path / "both-helpers.db"
    _create_meta_db(
        both_db,
        {ps.V1_KEY: json.dumps(v1_state), ps.KEY: ps.encode(v2_state)},
    )
    with closing(sqlite3.connect(both_db)) as conn:
        cfg_v2 = ps.load_config_if_present(conn)
        assert cfg_v2 is not None
        assert cfg_v2["bindings"]["codex_local"]["location"] == "C:/v2/codex"
        assert "grok_local" not in cfg_v2["bindings"]
        bindings_map = ps.load_bindings_map(conn)
        assert set(bindings_map) == {"codex_local"}
        assert bindings_map["codex_local"]["revision"] == 5

    both_records = {ps.V1_KEY: json.dumps(v1_state), ps.KEY: ps.encode(v2_state)}
    assert ps.default_binding_from_records(both_records, "codex_local")["revision"] == 5
    assert ps.default_binding_from_records(both_records, "grok_local") is None


# ---------------------------------------------------------------------------
# 5. Store compatibility view & Service multi-profile support
# ---------------------------------------------------------------------------


def test_store_compatibility_view_syncs_default_bindings_without_persisting_bindings(tmp_path):
    db_path = tmp_path / "store-compat.db"
    spend_db.initialize(db_path)
    store = c.Store(db_path)

    with store.transaction() as state:
        state["profiles"]["codex_local"] = {
            "codex-work": {
                "source": "codex_local",
                "enabled": True,
                "revision": 1,
                "location": "C:/fake/.codex-work",
                "toolKey": "codex-work",
            }
        }
        state["bindings"]["codex_local"] = {
            "enabled": True,
            "revision": 1,
            "location": "C:/fake/.codex",
        }
        state["bindings"]["grok_local"] = {
            "enabled": True,
            "revision": 2,
            "location": "C:/fake/.grok",
        }

    read_back = store.read()
    assert read_back["bindings"]["codex_local"]["location"] == "C:/fake/.codex"
    assert read_back["profiles"]["codex_local"]["default"]["toolKey"] == "codex"
    assert read_back["profiles"]["codex_local"]["default"]["origin"] == "default"
    assert read_back["profiles"]["codex_local"]["codex-work"]["location"] == "C:/fake/.codex-work"
    assert "codex-work" not in read_back["bindings"]

    # Update one default binding and delete another via state["bindings"]
    with store.transaction() as state:
        state["bindings"]["grok_local"] = {
            "enabled": False,
            "revision": 3,
            "location": "C:/fake/.grok-2",
        }
        del state["bindings"]["codex_local"]

    final = store.read()
    assert "codex_local" not in final["bindings"]
    assert "default" not in final["profiles"]["codex_local"]
    assert "codex-work" in final["profiles"]["codex_local"]
    assert final["profiles"]["grok_local"]["default"]["revision"] == 3
    assert final["profiles"]["grok_local"]["default"]["enabled"] is False

    raw_v2 = json.loads(_read_app_meta(db_path)[ps.KEY])
    assert "bindings" not in raw_v2


def test_service_mutate_and_completion_multi_profile_independent_lifecycle(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    spend_db.initialize(service.settings.database_path)

    # Connect default and an extra profile independently
    default_res = service.mutate(_mutate_body("codex_local", location="C:/fake/.codex"))
    work_req = _mutate_body("codex_local", location="C:/fake/.codex-work", name="Work")
    work_res = service.mutate(work_req, profile="codex-work")

    assert default_res["connection"]["revision"] == 1
    assert default_res["connection"]["bindingId"] == "codex_local"
    assert default_res["connection"]["toolKey"] == "codex"

    assert work_res["connection"]["revision"] == 1
    assert work_res["connection"]["bindingId"] == "codex_local:codex-work"
    assert work_res["connection"]["toolKey"] == "codex-work"
    assert work_res["connection"]["name"] == "Work"

    # Receipt idempotency for the same profile vs conflict across profiles
    assert service.mutate(work_req, profile="codex-work") == work_res
    with pytest.raises(c.Conflict):
        service.mutate(work_req, profile="default")
    with pytest.raises(c.Conflict):
        service.mutate(work_req, profile="codex-personal")

    # Independent revision tracking: advance default to revision 2 while codex-work stays at 1
    default_res_2 = service.mutate(
        _mutate_body("codex_local", revision=1, location="C:/fake/.codex-new")
    )
    assert default_res_2["connection"]["revision"] == 2
    with pytest.raises(c.Conflict):
        service.mutate(
            _mutate_body("codex_local", revision=0, location="C:/fake/.codex-work-2"),
            profile="codex-work",
        )

    # Completion targets the specific profile and revision only
    service.completion(
        "codex_local", 1, {"status": "success", "eventsSeen": 7}, profile="codex-work"
    )
    state = service.store.read()
    work_record = state["profiles"]["codex_local"]["codex-work"]
    default_record = state["profiles"]["codex_local"]["default"]
    assert work_record["state"] == "receiving_usage"
    assert work_record["importRevision"] == 1
    assert work_record["lastImport"] is not None
    assert default_record["state"] == "awaiting_ingest"
    assert default_record["lastImport"] is None

    # Stale completion on codex-work is ignored
    service.completion(
        "codex_local", 99, {"status": "failed"}, profile="codex-work"
    )
    assert service.store.read()["profiles"]["codex_local"]["codex-work"]["state"] == "receiving_usage"

    # Disable extra profile via body["profile"] leaves default eligible
    service.mutate(
        _mutate_body("codex_local", revision=1, operation="disable", profile="codex-work")
    )
    after_disable = service.store.read()
    codex_spec = REGISTRY.get("codex_local")
    assert c.eligible(codex_spec, after_disable, profile="default") is True
    assert c.eligible(codex_spec, after_disable, profile="codex-work") is False
    assert after_disable["profiles"]["codex_local"]["codex-work"]["revision"] == 2

    raw_v2 = json.loads(_read_app_meta(service.settings.database_path)[ps.KEY])
    assert "bindings" not in raw_v2


@pytest.mark.parametrize(
    "source",
    ["traycer_local", "openai_admin", "anthropic_admin", "openrouter", "deepseek"],
)
def test_service_mutate_rejects_non_default_profiles_for_unsupported_sources(tmp_path, monkeypatch, source):
    service = _make_service(tmp_path, monkeypatch)
    with pytest.raises(LocationError, match="Multiple profiles are not supported"):
        service.mutate(
            _mutate_body(source, location="C:/fake/alt", secret="sk-test"),
            profile="alt-profile",
        )
    with pytest.raises(LocationError, match="Multiple profiles are not supported"):
        service.mutate(
            _mutate_body(source, location="C:/fake/alt", secret="sk-test", profile="alt-profile"),
        )
