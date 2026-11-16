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

    # Connect default and add an extra profile independently
    default_res = service.mutate(_mutate_body("codex_local", location="C:/fake/.codex"))
    work_req = {
        "source": "codex_local",
        "operation": "add_profile",
        "location": "C:/fake/.codex-work",
        "name": "Work",
        "requestId": str(uuid4()),
    }
    work_res = service.mutate(work_req)

    assert default_res["connection"]["revision"] == 1
    assert default_res["connection"]["bindingId"] == "codex_local"
    assert default_res["connection"]["toolKey"] == "codex"

    assert work_res["connection"]["revision"] == 1
    assert work_res["connection"]["bindingId"] == "codex_local:codex-work"
    assert work_res["connection"]["toolKey"] == "codex-work"
    assert work_res["connection"]["name"] == "Work"

    # Receipt idempotency for the same profile vs conflict across profiles / LocationError for uncreated non-default connect
    assert service.mutate(work_req) == work_res
    with pytest.raises(c.Conflict):
        service.mutate(_mutate_body("codex_local", location="C:/fake/.codex-other", requestId=work_req["requestId"]), profile="default")
    with pytest.raises(c.LocationError):
        service.mutate(_mutate_body("codex_local", location="C:/fake/.codex-personal"), profile="codex-personal")

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


# ---------------------------------------------------------------------------
# 6. assert_folder_free & FolderInUse
# ---------------------------------------------------------------------------


def test_folder_in_use_is_value_error_and_blocks_same_and_cross_provider(tmp_path):
    assert issubclass(ps.FolderInUse, ValueError)
    claude_dir = tmp_path / ".claude-work"
    claude_dir.mkdir(parents=True)

    state = {
        "version": 2,
        "profiles": {
            "claude_local": {
                "claude-code-work": {
                    "source": "claude_local",
                    "location": str(claude_dir),
                }
            }
        },
    }

    # Same folder, same provider -> FolderInUse
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "claude_local", claude_dir)

    # Same folder across different providers -> FolderInUse
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "codex_local", claude_dir)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "muse_local", str(claude_dir))

    # Distinct folder passes
    other_dir = tmp_path / ".claude-personal"
    other_dir.mkdir(parents=True)
    ps.assert_folder_free(state, "claude_local", other_dir)


def test_assert_folder_free_subpath_vs_root_equivalence(tmp_path):
    claude_root = tmp_path / ".claude"
    (claude_root / "projects").mkdir(parents=True)
    (claude_root / "transcripts").mkdir(parents=True)

    codex_root = tmp_path / ".codex"
    (codex_root / "sessions").mkdir(parents=True)
    (codex_root / "archived_sessions").mkdir(parents=True)

    oc_flat = tmp_path / "oc-flat"
    oc_flat.mkdir(parents=True)
    oc_flat_db = oc_flat / "opencode.db"
    oc_flat_db.write_bytes(b"")

    oc_nested = tmp_path / ".opencode-work"
    oc_nested_db = oc_nested / "opencode" / "opencode.db"
    oc_nested_db.parent.mkdir(parents=True)
    oc_nested_db.write_bytes(b"")

    zcode_root = tmp_path / ".zcode"
    zcode_db = zcode_root / "cli" / "db" / "db.sqlite"
    zcode_db.parent.mkdir(parents=True)
    zcode_db.write_bytes(b"")

    state = {
        "version": 2,
        "profiles": {
            "claude_local": {"default": {"location": str(claude_root)}},
            "codex_local": {"default": {"location": str(codex_root)}},
            "opencode_local": {
                "default": {"location": str(oc_flat_db)},
                "opencode-go-work": {"location": str(oc_nested_db)},
            },
            "zcode_local": {"default": {"location": str(zcode_db)}},
        },
    }

    # Claude root <-> projects / transcripts (both directions)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "claude_local", claude_root / "projects")
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "claude_local", claude_root / "transcripts")

    state_sub = {
        "version": 2,
        "profiles": {
            "claude_local": {"default": {"location": str(claude_root / "projects")}},
            "codex_local": {"default": {"location": str(codex_root / "sessions")}},
            "opencode_local": {"default": {"location": str(oc_flat)}},
            "zcode_local": {"default": {"location": str(zcode_root)}},
        },
    }
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state_sub, "claude_local", claude_root)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state_sub, "codex_local", codex_root)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state_sub, "opencode_local", oc_flat_db)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state_sub, "zcode_local", zcode_db)

    # Codex root <-> sessions / archived_sessions
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "codex_local", codex_root / "sessions")
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "codex_local", codex_root / "archived_sessions")

    # OpenCode opencode.db <-> parent and .opencode-* nested layout
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "opencode_local", oc_flat)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "opencode_local", oc_nested)
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "opencode_local", oc_nested / "opencode")

    # ZCode cli/db/db.sqlite <-> .zcode root
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(state, "zcode_local", zcode_root)


def test_assert_folder_free_exclude_key_and_bindings_fallback(tmp_path):
    claude_dir = tmp_path / ".claude-work"
    (claude_dir / "projects").mkdir(parents=True)
    default_dir = tmp_path / ".codex"
    (default_dir / "sessions").mkdir(parents=True)

    state = {
        "version": 2,
        "profiles": {
            "claude_local": {
                "claude-code-work": {"location": str(claude_dir)},
            }
        },
        "bindings": {
            "codex_local": {"location": str(default_dir / "sessions")},
        },
    }

    # Same profile key may reuse/update its own folder (including subpath form)
    ps.assert_folder_free(
        state, "claude_local", claude_dir / "projects", exclude_key="claude-code-work"
    )
    ps.assert_folder_free(
        state, "codex_local", default_dir, exclude_key="default"
    )

    # Different profile key on the same provider still raises FolderInUse
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(
            state, "claude_local", claude_dir, exclude_key="claude-code-other"
        )

    # Same exclude_key string on a DIFFERENT provider does not bypass the check
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(
            state, "codex_local", claude_dir, exclude_key="claude-code-work"
        )
    with pytest.raises(ps.FolderInUse):
        ps.assert_folder_free(
            state, "claude_local", default_dir, exclude_key="default"
        )


# ---------------------------------------------------------------------------
# 7. Review regression tests: fresh install, alt cutover, v1 rollback
# ---------------------------------------------------------------------------


def _write_claude_sample(
    path: Path,
    *,
    session_id: str,
    message_id: str,
    timestamp: str = "2026-11-12T12:00:00Z",
    output_tokens: int = 25,
) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "type": "assistant",
        "timestamp": timestamp,
        "sessionId": session_id,
        "cwd": "C:/work/demo",
        "uuid": f"uuid-{message_id}",
        "message": {
            "id": message_id,
            "model": "claude-opus-5",
            "usage": {
                "input_tokens": 100,
                "cache_read_input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "output_tokens": output_tokens,
            },
        },
    }
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def test_fresh_install_does_not_autoapprove_extra_profiles(tmp_path, monkeypatch):
    from spend_app import claude_accounts, opencode_profiles, profile_detect
    from spend_app.adapters import profiles_local
    from spend_app.config import Settings
    from spend_app.pricing import PricingEngine

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_DATA_HOME",
        "OPENCODE_DB",
        "BURNRATE_CLAUDE_ACCOUNT_DIRS",
        "BURNRATE_OPENCODE_PROFILE_DIRS",
    ):
        monkeypatch.delenv(name, raising=False)

    extra = home / ".claude-work"
    _write_claude_sample(extra / "projects" / "p" / "one.jsonl", session_id="work-session", message_id="work-message")
    (extra / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "work-account", "billingType": "claude_max"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: claude_accounts.discover(home=home, environ={}))
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())
    profile_detect.reset_cache()

    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=root / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    spend_db.initialize(settings.database_path)

    state = c.Store(settings.database_path).read()
    extras = [p for p in ps.profiles(state) if p.key != "default" and p.enabled]
    assert extras == []
    assert state["profiles"] == {}

    # Extra folder is not collected without approval, and surfaces as detected for approval
    ingest_res = profiles_local.ingest(
        database_path=settings.database_path,
        pricing=PricingEngine.load(settings.pricing_path),
    )
    assert ingest_res["status"] == "skipped"
    with spend_db.connect(settings.database_path) as db:
        assert db.execute("SELECT COUNT(*) FROM usage_events").fetchone()[0] == 0

    svc = c.Service(settings, vault=_FakeVault())
    claude_row = next(row for row in svc.list()["connections"] if row["source"] == "claude_local")
    assert any("claude-work" in d["folder"] or d["candidateId"] for d in claude_row["detected"])
    profile_detect.reset_cache()


def test_legacy_upgrade_grandfathers_known_tool_key_without_v1(tmp_path, monkeypatch):
    from spend_app import opencode_profiles

    db_path = tmp_path / "legacy-no-v1.db"
    spend_db.initialize(db_path)
    with spend_db.connect(db_path) as db:
        # Simulate pre-v2 DB with historical usage_events for claude-code-work, no v1 metadata
        db.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        db.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, is_exact,
                occurred_epoch_us, raw_id, ingested_at
            ) VALUES('claude_local', 'claude-code-work', 'claude-opus-5', '2026-10-01T00:00:00Z',
                     's-old', 'demo', 10, 0, 0, 0, 5, NULL, NULL, 0.01, 1, 1, 'old-raw-1', '2026-10-01T00:00:01Z')
            """
        )

    work_acc = ClaudeAccount(
        key="claude-code-work",
        label="Work",
        config_dir=tmp_path / ".claude-work",
        state_path=tmp_path / ".claude-work" / ".claude.json",
        account_uuid="work-uuid",
        plan="Claude Max",
    )
    unseen_acc = ClaudeAccount(
        key="claude-code-unseen",
        label="Unseen",
        config_dir=tmp_path / ".claude-unseen",
        state_path=tmp_path / ".claude-unseen" / ".claude.json",
        account_uuid="unseen-uuid",
        plan="Claude Pro",
    )
    monkeypatch.setattr("spend_app.claude_accounts.extra_accounts", lambda: (work_acc, unseen_acc))
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())

    c.Store(db_path).bootstrap()
    state = c.Store(db_path).read()
    claude_profiles = state["profiles"].get("claude_local") or {}
    assert "claude-code-work" in claude_profiles
    assert claude_profiles["claude-code-work"]["enabled"] is True
    assert "claude-code-unseen" not in claude_profiles


def test_alt_migration_preserves_old_usage_tool_key(tmp_path, monkeypatch):
    from spend_app import claude_accounts, opencode_profiles
    from spend_app.adapters import claude_local, profiles_local
    from spend_app.config import Settings
    from spend_app.pricing import PricingEngine

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())

    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=root / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    spend_db.initialize(settings.database_path)
    pricing = PricingEngine.load(settings.pricing_path)

    folder = home / ".claude-2"
    transcript = _write_claude_sample(
        folder / "projects" / "p" / "one.jsonl",
        session_id="old-session",
        message_id="old-message",
        timestamp="2026-11-12T12:00:00Z",
    )
    claude_local.reset_file_cache()
    claude_local.ingest(database_path=settings.database_path, pricing=pricing, session_glob=str(transcript))

    v1 = _v1_payload(
        bindings={
            "claude_local_alt": {
                "source": "claude_local_alt",
                "location": str(folder / "projects"),
                "enabled": True,
                "revision": 3,
                "mode": "manual",
            }
        }
    )
    with spend_db.connect(settings.database_path) as db:
        db.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        db.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", (ps.V1_KEY, json.dumps(v1)))

    c.Store(settings.database_path).bootstrap()
    migrated = c.Store(settings.database_path).read()
    alt_rec = migrated["profiles"]["claude_local"]["claude-code-2"]
    assert alt_rec["cutoverAt"]

    profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    with spend_db.connect(settings.database_path) as db:
        rows = [tuple(r) for r in db.execute("SELECT session_id, tool_key FROM usage_events ORDER BY id")]
        sess_rows = [tuple(r) for r in db.execute("SELECT session_id, tool_key FROM sessions")]
    assert rows == [("old-session", "claude-code")]
    assert sess_rows == [("old-session", "claude-code")]

    # New events added after cutover ARE recorded under claude-code-2 while old row stays claude-code
    _write_claude_sample(
        folder / "projects" / "p" / "two.jsonl",
        session_id="new-session",
        message_id="new-message",
        timestamp="2026-11-12T12:05:00Z",
    )
    profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    claude_local.reset_file_cache()
    with spend_db.connect(settings.database_path) as db:
        rows_after = [tuple(r) for r in db.execute("SELECT session_id, tool_key FROM usage_events ORDER BY id")]
        sess_after = dict(db.execute("SELECT session_id, tool_key FROM sessions").fetchall())
    assert rows_after == [("old-session", "claude-code"), ("new-session", "claude-code-2")]
    assert sess_after == {"old-session": "claude-code", "new-session": "claude-code-2"}


def test_v1_rollback_keeps_vault_reference_reachable(tmp_path, monkeypatch):
    service = _make_service(tmp_path, monkeypatch)
    spend_db.initialize(service.settings.database_path)
    v1 = _v1_payload(
        bindings={
            "openai_admin": {
                "source": "openai_admin",
                "enabled": True,
                "revision": 1,
                "location": "",
                "mode": "api",
                "credentialRef": "legacy-ref",
            }
        }
    )
    with spend_db.connect(service.settings.database_path) as db:
        db.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        db.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", (ps.V1_KEY, json.dumps(v1)))

    c.Store(service.settings.database_path).bootstrap()
    service.vault.put("synthetic-key", "legacy-ref")

    # 1. Replacing the key in v2 must NOT delete the frozen v1 credentialRef
    service.mutate(
        {
            "source": "openai_admin",
            "operation": "connect",
            "mode": "api",
            "revision": 1,
            "secret": "sk-new-v2-key",
            "requestId": str(uuid4()),
        }
    )
    assert "legacy-ref" in service.vault.values
    v2_ref = service.store.read()["profiles"]["openai_admin"]["default"]["credentialRef"]
    assert v2_ref and v2_ref != "legacy-ref"
    assert v2_ref in service.vault.values

    # 2. Disabling in v2 deletes the v2-only ref on cleanup, but still preserves legacy-ref
    service.mutate(
        {
            "source": "openai_admin",
            "operation": "disable",
            "revision": 2,
            "requestId": str(uuid4()),
        }
    )
    assert v2_ref not in service.vault.values
    assert "legacy-ref" in service.vault.values
    assert service.store.read()["cleanup"] == []

    # 3. connections.v1 still names legacy-ref and resolves cleanly in the Vault
    with spend_db.connect(service.settings.database_path) as db:
        old = json.loads(db.execute("SELECT value FROM app_meta WHERE key=?", (ps.V1_KEY,)).fetchone()[0])
    ref = old["bindings"]["openai_admin"]["credentialRef"]
    assert ref == "legacy-ref"
    assert service.vault.get(ref) == "synthetic-key"


def test_remote_history_does_not_approve_local_folder(tmp_path, monkeypatch):
    from spend_app import claude_accounts, opencode_profiles, profile_detect
    from spend_app.config import Settings

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    for name in (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_DATA_HOME",
        "OPENCODE_DB",
        "BURNRATE_CLAUDE_ACCOUNT_DIRS",
        "BURNRATE_OPENCODE_PROFILE_DIRS",
    ):
        monkeypatch.delenv(name, raising=False)

    folder = home / ".claude-work"
    _write_claude_sample(
        folder / "projects" / "p" / "one.jsonl",
        session_id="local-private",
        message_id="private-message",
    )
    (folder / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "local-private-account"}}),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        claude_accounts,
        "extra_accounts",
        lambda: claude_accounts.discover(home=home, environ={}),
    )
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())
    profile_detect.reset_cache()

    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=root / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    db_path = settings.database_path
    spend_db.initialize(db_path)

    # Seed remote-only history across usage_events, unpriced_usage_events, sessions, quotas, and ingest_runs
    with spend_db.connect(db_path) as db:
        db.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        db.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, is_exact,
                occurred_epoch_us, raw_id, ingested_at
            ) VALUES('claude_local', 'claude-code-work', 'claude-opus-5', '2026-10-01T00:00:00Z',
                     'm:another-computer:s1', 'demo', 10, 0, 0, 0, 5, NULL, NULL, 0.01, 1, 1,
                     'm:another-computer:remote-message', '2026-10-01T00:00:01Z')
            """
        )
        db.execute(
            """
            INSERT INTO unpriced_usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, unclassified_tokens, telemetry_complete, cost_usd,
                occurred_epoch_us, raw_id, ingested_at
            ) VALUES('claude_local', 'claude-code-work', 'unknown-model', '2026-10-01T00:00:00Z',
                     'm:another-computer:s1', 'demo', 10, 0, 0, 0, 5, NULL, 0, 1, NULL, 1,
                     'm:another-computer:remote-unpriced', '2026-10-01T00:00:01Z')
            """
        )
        db.execute(
            """
            INSERT INTO sessions(session_id, tool_key, project, started_at, ended_at, model_key)
            VALUES('m:another-computer:s1', 'claude-code-work', 'demo', '2026-10-01T00:00:00Z',
                   '2026-10-01T00:01:00Z', 'claude-opus-5')
            """
        )
        db.execute(
            """
            INSERT INTO quotas(provider_key, limit_key, label, unit, source, polled_at)
            VALUES('claude-code-work', 'weekly', 'Claude weekly', 'pct',
                   'sync:another-computer:claude_local_accounts', '2026-10-01T00:00:00Z')
            """
        )
        db.execute(
            """
            INSERT INTO ingest_runs(source, started_at, finished_at, status, events_written)
            VALUES('sync:another-computer', '2026-10-01T00:00:00Z', '2026-10-01T00:00:01Z', 'success', 1),
                  ('claude_local', '2026-10-01T00:00:00Z', '2026-10-01T00:00:01Z', 'success', 1)
            """
        )

    c.Store(db_path).bootstrap()
    state = c.Store(db_path).read()
    extras = [p for p in ps.profiles(state) if p.key != "default" and p.enabled]
    assert extras == [], extras
    assert "sync:another-computer" not in state["legacy"]

    # Folder still surfaces as detected rather than approved
    svc = c.Service(settings, vault=_FakeVault())
    claude_row = next(row for row in svc.list()["connections"] if row["source"] == "claude_local")
    assert any("claude-work" in d["folder"] for d in claude_row["detected"])
    profile_detect.reset_cache()


def test_cutover_still_applies_legitimate_usage_revisions(tmp_path, monkeypatch):
    from spend_app import claude_accounts, opencode_profiles
    from spend_app.adapters import claude_local, profiles_local
    from spend_app.config import Settings
    from spend_app.pricing import PricingEngine

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())

    root = Path(__file__).resolve().parents[1]
    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=root / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    spend_db.initialize(settings.database_path)
    pricing = PricingEngine.load(settings.pricing_path)

    folder = home / ".claude-2"
    transcript = _write_claude_sample(
        folder / "projects" / "p" / "one.jsonl",
        session_id="streaming",
        message_id="message",
        output_tokens=25,
    )
    claude_local.reset_file_cache()
    claude_local.ingest(database_path=settings.database_path, pricing=pricing, session_glob=str(transcript))

    v1 = _v1_payload(
        bindings={
            "claude_local_alt": {
                "source": "claude_local_alt",
                "location": str(folder / "projects"),
                "enabled": True,
                "revision": 3,
                "mode": "manual",
            }
        }
    )
    with spend_db.connect(settings.database_path) as db:
        db.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        db.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", (ps.V1_KEY, json.dumps(v1)))

    c.Store(settings.database_path).bootstrap()
    first = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert first["eventsAccepted"] == 1, first

    # A newer revision of the pre-cutover message arrives (25 -> 125 output tokens):
    # revised fields must apply while preserving the stored "claude-code" tool_key.
    _write_claude_sample(
        transcript,
        session_id="streaming",
        message_id="message",
        output_tokens=125,
        timestamp="2026-11-12T12:01:00Z",
    )
    result = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert result["eventsAccepted"] == 1, result

    # Unchanged re-run hits file cache and preserves accepted counts & stored revision
    cached = profiles_local.ingest(database_path=settings.database_path, pricing=pricing)
    assert cached["eventsAccepted"] == 1, cached
    claude_local.reset_file_cache()

    with spend_db.connect(settings.database_path) as db:
        rows = [tuple(r) for r in db.execute("SELECT tool_key, output_tokens FROM usage_events")]
        sess = [tuple(r) for r in db.execute("SELECT session_id, tool_key FROM sessions")]
    assert rows == [("claude-code", 125)], (result, rows)
    assert sess == [("streaming", "claude-code")], sess
def test_preserve_existing_tool_keys_ignores_other_source_collisions(tmp_path):
    from datetime import UTC, datetime
    from spend_app.adapters import common

    db_path = tmp_path / "spend.db"
    spend_db.initialize(db_path)

    with spend_db.connect(db_path) as db:
        db.execute(
            """
            INSERT INTO usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, cost_usd, computed_cost_usd, is_exact,
                occurred_epoch_us, raw_id, ingested_at
            ) VALUES
                ('codex_local', 'codex', 'gpt-5', '2026-11-12T12:00:00Z',
                 's-codex', 'demo', 10, 0, 0, 0, 5, NULL, NULL, 0.01, 1, 1,
                 'shared-priced-id', '2026-11-12T12:00:01Z'),
                ('claude_local', 'claude-code', 'claude-opus-5', '2026-11-12T12:00:00Z',
                 's-claude', 'demo', 10, 0, 0, 0, 5, NULL, NULL, 0.01, 1, 1,
                 'same-source-id', '2026-11-12T12:00:01Z')
            """
        )
        db.execute(
            """
            INSERT INTO unpriced_usage_events(
                source, tool_key, model_key, occurred_at, session_id, project,
                input_tokens, cached_input_tokens, cache_write_tokens, cache_write_1h_tokens,
                output_tokens, reasoning_tokens, unclassified_tokens, telemetry_complete, cost_usd,
                occurred_epoch_us, raw_id, ingested_at
            ) VALUES(
                'opencode_local', 'opencode-go', 'unknown-model', '2026-11-12T12:00:00Z',
                's-oc', 'demo', 10, 0, 0, 0, 5, NULL, 0, 1, NULL, 1,
                'shared-unpriced-id', '2026-11-12T12:00:01Z'
            )
            """
        )

        occurred = datetime(2026, 11, 12, 12, 5, tzinfo=UTC)
        incoming_priced = common.UsageRow(
            source="claude_local",
            tool_key="claude-code-2",
            model_key="claude-opus-5",
            occurred_at=occurred,
            session_id="s-claude-2",
            project="demo",
            input_tokens=20,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=10,
            reasoning_tokens=None,
            cost_usd=None,
            raw_id="shared-priced-id",
        )
        incoming_unpriced = common.UsageRow(
            source="claude_local",
            tool_key="claude-code-2",
            model_key="unknown-model",
            occurred_at=occurred,
            session_id="s-claude-2",
            project="demo",
            input_tokens=20,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=10,
            reasoning_tokens=None,
            cost_usd=None,
            raw_id="shared-unpriced-id",
        )
        incoming_same_source = common.UsageRow(
            source="claude_local",
            tool_key="claude-code-2",
            model_key="claude-opus-5",
            occurred_at=occurred,
            session_id="s-claude",
            project="demo",
            input_tokens=20,
            cached_input_tokens=0,
            cache_write_tokens=0,
            cache_write_1h_tokens=0,
            output_tokens=30,
            reasoning_tokens=None,
            cost_usd=None,
            raw_id="same-source-id",
        )

        resolved = common._preserve_existing_tool_keys(
            db, (incoming_priced, incoming_unpriced, incoming_same_source)
        )
        assert resolved[0] == incoming_priced
        assert resolved[0].tool_key == "claude-code-2"
        assert resolved[1] == incoming_unpriced
        assert resolved[1].tool_key == "claude-code-2"
        assert resolved[2].tool_key == "claude-code"

        stored_priced = [
            tuple(r)
            for r in db.execute(
                "SELECT raw_id, source, tool_key FROM usage_events ORDER BY raw_id"
            )
        ]
        stored_unpriced = [
            tuple(r)
            for r in db.execute(
                "SELECT raw_id, source, tool_key FROM unpriced_usage_events ORDER BY raw_id"
            )
        ]
        assert stored_priced == [
            ("same-source-id", "claude_local", "claude-code"),
            ("shared-priced-id", "codex_local", "codex"),
        ]
        assert stored_unpriced == [
            ("shared-unpriced-id", "opencode_local", "opencode-go"),
        ]
