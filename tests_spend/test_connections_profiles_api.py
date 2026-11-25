"""Comprehensive tests for P5: multi-profile Connections API, contracts, mutations, and callers."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from spend_app import (
    claude_accounts,
    connections as c,
    db as spend_db,
    profile_detect,
    profile_store as ps,
)
from spend_app.api import create_app
from spend_app.config import Settings
from spend_app.connection_api import connection_router
from spend_app.native_desktop import DemoClient
from spend_app.native_onboarding import build_onboarding_data
from spend_app.providers import REGISTRY
from spend_app.redact import redact
from spend_app.setup_wizard import _api_connections

ROOT = Path(__file__).resolve().parents[1]
API_HEADERS = {"X-BURNRATE-Request": "1", "Origin": "http://testserver"}


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _iso(dt: datetime) -> str:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _make_claude_folder(folder: Path, *, account_uuid: str | None = None, plan: str | None = None) -> Path:
    proj = folder / "projects" / "demo"
    proj.mkdir(parents=True, exist_ok=True)
    (proj / "session.jsonl").write_text(
        json.dumps(
            {
                "type": "assistant",
                "timestamp": "2026-10-01T00:00:00Z",
                "message": {"model": "claude-sonnet-4-5", "usage": {"input_tokens": 10, "output_tokens": 5}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if account_uuid is not None or plan is not None:
        oauth = {}
        if account_uuid is not None:
            oauth["accountUuid"] = account_uuid
        if plan is not None:
            oauth["subscriptionType"] = plan
        (folder / ".claude.json").write_text(
            json.dumps({"oauthAccount": oauth}),
            encoding="utf-8",
        )
    return folder


def _make_codex_folder(folder: Path, *, account_id: str | None = None) -> Path:
    sessions = folder / "sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout.jsonl").write_text(
        json.dumps(
            {
                "type": "event_msg",
                "timestamp": "2026-10-01T00:00:00Z",
                "payload": {"type": "token_count", "info": {"total_token_usage": {"input_tokens": 10}}},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    if account_id is not None:
        (folder / "auth.json").write_text(
            json.dumps({"tokens": {"account_id": account_id}}),
            encoding="utf-8",
        )
    return folder


def _insert_usage_event(db_path: Path, tool_key: str, ingested_at: datetime, *, unpriced: bool = False) -> None:
    table = "unpriced_usage_events" if unpriced else "usage_events"
    raw_id = f"raw-{uuid4().hex}"
    ts = _iso(ingested_at)
    with spend_db.connect(db_path) as conn:
        if unpriced:
            conn.execute(
                f"INSERT INTO {table}(source, tool_key, model_key, occurred_at, session_id, project, "
                "input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, raw_id, ingested_at) "
                "VALUES('claude_local', ?, 'unknown-model', ?, 's1', 'p1', 10, 0, 0, 5, ?, ?)",
                (tool_key, ts, raw_id, ts),
            )
        else:
            conn.execute(
                f"INSERT INTO {table}(source, tool_key, model_key, occurred_at, session_id, project, "
                "input_tokens, output_tokens, computed_cost_usd, raw_id, ingested_at) "
                "VALUES('claude_local', ?, 'claude-sonnet-4-5', ?, 's1', 'p1', 10, 5, 0.01, ?, ?)",
                (tool_key, ts, raw_id, ts),
            )


@pytest.fixture
def isolated_env(tmp_path: Path, monkeypatch):
    home = tmp_path / "home"
    home.mkdir(parents=True, exist_ok=True)
    for var in (
        "OPENAI_ADMIN_KEY",
        "ANTHROPIC_ADMIN_KEY",
        "OPENROUTER_MANAGEMENT_KEY",
        "DEEPSEEK_BALANCE_KEY",
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "CLAUDE_EXTRA_DIRS",
        "CODEX_EXTRA_DIRS",
        "OPENCODE_EXTRA_DIRS",
        "OPENCODE_DB",
        "OPENCODE_CONFIG_DIR",
        "OPENCODE_API_KEY",
        "GROK_HOME",
        "BURNRATE_CENTRAL_SERVER",
        "BURNRATE_ALLOWED_HOSTS",
        "BURNRATE_ALLOWED_ORIGINS",
        "BURNRATE_ENABLE_DASHBOARD",
    ):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(home / ".local" / "share"))
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    profile_detect.reset_cache()
    yield home
    profile_detect.reset_cache()


@pytest.fixture
def settings(tmp_path: Path, isolated_env: Path) -> Settings:
    value = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    spend_db.initialize(value.database_path)
    return value


@pytest.fixture
def client(settings: Settings) -> TestClient:
    from tests_spend.local_client_fixture import local_client

    return local_client(create_app(settings, enable_scheduler=False), settings.database_path)


# ---------------------------------------------------------------------------
# 1. GET /api/connections contract test (pinning response shape for P6)
# ---------------------------------------------------------------------------


def test_get_connections_contract_shape_dedup_and_grouping(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    """Contract test pinning the P5 ``GET /api/connections`` response shape for P6.

    Decision documented for P6 UI consumers:
    - ``sharesLimitsWith`` on ``profiles[]`` holds the matched primary profile's ``key``:
      ``None`` (JSON ``null``) on ``"default"`` if present, or on the first profile by sort
      order when sharing an ``accountDigest``; and holds that primary profile's ``key`` on
      any subsequent duplicate profiles that share the same ``accountDigest``.
    - ``detected[]`` excludes already-added folders (matched via folder identity) and
      dismissed candidates, while still including non-added, non-dismissed candidates
      that share an ``accountDigest`` with an added profile; for those candidates,
      ``sameAccountAs`` is set to the matched added profile's ``key`` (otherwise ``None``).
    """
    home = isolated_env

    # Default ~/.claude exists (persisted with acct-A; no home/.claude.json so
    # profile_detect still discovers ~/.claude-dup-default as a separate candidate)
    default_claude = _make_claude_folder(home / ".claude")

    # Secondary added profiles sharing account A and account B, plus an added detected folder (acct-E)
    work_dir = _make_claude_folder(tmp_path / "manual-claude-work", account_uuid="acct-A", plan="max")
    team_dir = _make_claude_folder(tmp_path / "manual-claude-team", account_uuid="acct-B", plan="pro")
    added_cand_dir = _make_claude_folder(home / ".claude-added-cand", account_uuid="acct-E", plan="pro")

    # Detected candidates on disk:
    # - .claude-added-cand: already added -> excluded from detected[]
    # - .claude-dup-default: shares account A with default -> sameAccountAs == "default"
    # - .claude-dup-team: shares account B with added team profile -> sameAccountAs == "claude-code-team"
    # - .claude-fresh: distinct account C -> sameAccountAs is None
    # - .claude-dismissed: dismissed -> excluded from detected[]
    dup_default_dir = _make_claude_folder(home / ".claude-dup-default", account_uuid="acct-A", plan="max")
    dup_team_dir = _make_claude_folder(home / ".claude-dup-team", account_uuid="acct-B", plan="pro")
    fresh_dir = _make_claude_folder(home / ".claude-fresh", account_uuid="acct-C", plan="pro")
    dismissed_dir = _make_claude_folder(home / ".claude-dismissed", account_uuid="acct-D", plan="pro")

    # Two Codex profiles (with NO default folder on disk) both sharing digest "codex-shared"
    # to verify non-default sort-order sharesLimitsWith fallback when default has no digest.
    codex_alpha = _make_codex_folder(home / ".codex-alpha", account_id="codex-shared")
    codex_beta = _make_codex_folder(home / ".codex-beta", account_id="codex-shared")

    with c.Store(settings.database_path).transaction() as state:
        state["profiles"]["claude_local"] = {
            "default": {
                "source": "claude_local",
                "bindingId": "claude_local",
                "name": "Default",
                "location": str(default_claude),
                "origin": "default",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("acct-A"),
                "plan": "Claude Max",
                "toolKey": "claude-code",
            },
            "claude-code-added-cand": {
                "source": "claude_local",
                "bindingId": "claude_local:claude-code-added-cand",
                "name": "Added Cand",
                "location": str(added_cand_dir),
                "origin": "detected",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("acct-E"),
                "plan": "Claude Pro",
                "toolKey": "claude-code-added-cand",
            },
            "claude-code-work": {
                "source": "claude_local",
                "bindingId": "claude_local:claude-code-work",
                "name": "Work",
                "location": str(work_dir),
                "origin": "manual",
                "enabled": True,
                "revision": 2,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("acct-A"),
                "plan": "Claude Max",
                "toolKey": "claude-code-work",
            },
            "claude-code-team": {
                "source": "claude_local",
                "bindingId": "claude_local:claude-code-team",
                "name": "Team",
                "location": str(team_dir),
                "origin": "detected",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("acct-B"),
                "plan": "Claude Pro",
                "toolKey": "claude-code-team",
            },
        }
        state["profiles"]["codex_local"] = {
            "codex-alpha": {
                "source": "codex_local",
                "bindingId": "codex_local:codex-alpha",
                "name": "Alpha",
                "location": str(codex_alpha),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("codex-shared"),
                "toolKey": "codex-alpha",
            },
            "codex-beta": {
                "source": "codex_local",
                "bindingId": "codex_local:codex-beta",
                "name": "Beta",
                "location": str(codex_beta),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "accountDigest": _sha256("codex-shared"),
                "toolKey": "codex-beta",
            },
        }
        # Dismiss .claude-dismissed candidate
        dismissed_cand = next(
            cand
            for cand in profile_detect.detect("claude_local")
            if Path(cand.folder) == dismissed_dir
        )
        state["dismissed"]["claude_local"] = {dismissed_cand.candidate_id: "2026-10-01T00:00:00Z"}

    profile_detect.reset_cache()
    resp = client.get("/api/connections")
    assert resp.status_code == 200
    payload = resp.json()

    # Exact top-level keys
    assert set(payload.keys()) == {"connections", "vaultAvailable", "computerScope"}
    assert isinstance(payload["connections"], list)
    assert isinstance(payload["vaultAvailable"], bool)
    assert isinstance(payload["computerScope"], str) and payload["computerScope"]

    by_source = {row["source"]: row for row in payload["connections"]}

    # Verify legacy fields + new fields on every connection row
    required_legacy = {
        "source",
        "name",
        "kind",
        "experimental",
        "revision",
        "location",
        "capabilities",
        "capabilityNote",
        "credentialHelp",
        "externallyManaged",
        "state",
        "detail",
        "lastVerification",
        "lastImport",
        "importRevision",
    }
    required_profile_keys = {
        "key",
        "name",
        "location",
        "origin",
        "state",
        "revision",
        "plan",
        "sharesLimitsWith",
    }
    required_detected_keys = {"candidateId", "folder", "plan", "sameAccountAs"}

    for row in payload["connections"]:
        assert required_legacy <= set(row.keys()), row["source"]
        assert {"supportsProfiles", "profiles", "detected"} <= set(row.keys()), row["source"]
        assert isinstance(row["supportsProfiles"], bool)
        assert isinstance(row["profiles"], list)
        assert isinstance(row["detected"], list)
        for prof in row["profiles"]:
            assert set(prof.keys()) == required_profile_keys
        for det in row["detected"]:
            assert set(det.keys()) == required_detected_keys

    # supportsProfiles values: False for traycer_local & API sources, True for supported local providers
    assert by_source["traycer_local"]["supportsProfiles"] is False
    for api_src in ("openrouter", "deepseek", "openai_admin", "anthropic_admin"):
        assert by_source[api_src]["supportsProfiles"] is False
    for local_src in (
        "claude_local",
        "codex_local",
        "opencode_local",
        "zcode_local",
        "grok_local",
        "antigravity_local",
        "muse_local",
    ):
        assert by_source[local_src]["supportsProfiles"] is True

    # Claude profiles & sharesLimitsWith
    claude_row = by_source["claude_local"]
    claude_profs = {p["key"]: p for p in claude_row["profiles"]}
    assert [p["key"] for p in claude_row["profiles"]] == [
        "default",
        "claude-code-added-cand",
        "claude-code-team",
        "claude-code-work",
    ]
    assert claude_profs["default"]["sharesLimitsWith"] is None
    assert claude_profs["claude-code-work"]["sharesLimitsWith"] == "default"
    assert claude_profs["claude-code-team"]["sharesLimitsWith"] is None

    # Claude detected[]: excludes already-added (.claude-added-cand) and dismissed (.claude-dismissed)
    detected_by_folder = {d["folder"]: d for d in claude_row["detected"]}
    assert "~/.claude-added-cand" not in detected_by_folder
    assert "~/.claude-dismissed" not in detected_by_folder
    assert set(detected_by_folder) == {
        "~/.claude-dup-default",
        "~/.claude-dup-team",
        "~/.claude-fresh",
    }
    assert detected_by_folder["~/.claude-dup-default"]["sameAccountAs"] == "default"
    assert detected_by_folder["~/.claude-dup-team"]["sameAccountAs"] == "claude-code-team"
    assert detected_by_folder["~/.claude-fresh"]["sameAccountAs"] is None
    assert dup_default_dir.exists() and dup_team_dir.exists() and fresh_dir.exists()

    # Codex sharesLimitsWith when default has no digest -> first added profile by sort order is primary
    codex_row = by_source["codex_local"]
    codex_profs = {p["key"]: p for p in codex_row["profiles"]}
    assert [p["key"] for p in codex_row["profiles"]] == ["default", "codex-alpha", "codex-beta"]
    assert codex_profs["default"]["state"] == "not_found"
    assert codex_profs["default"]["sharesLimitsWith"] is None
    assert codex_profs["codex-alpha"]["sharesLimitsWith"] is None
    assert codex_profs["codex-beta"]["sharesLimitsWith"] == "codex-alpha"


def test_get_connections_profile_states_and_receiving_query_budget(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path, monkeypatch
):
    """Profile states cover receiving/added/not_found/error/externally_managed/disabled
    with a single bounded SQL query over usage_events AND unpriced_usage_events (<24h)."""
    home = isolated_env
    _make_claude_folder(home / ".claude")
    work_dir = _make_claude_folder(tmp_path / "ext-claude-work")
    err_dir = _make_claude_folder(tmp_path / "ext-claude-err")
    dis_dir = _make_claude_folder(tmp_path / "ext-claude-dis")
    stale_dir = _make_claude_folder(tmp_path / "ext-claude-stale")

    with c.Store(settings.database_path).transaction() as state:
        state["profiles"]["claude_local"] = {
            "claude-code-work": {
                "source": "claude_local",
                "name": "Work Unpriced",
                "location": str(work_dir),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
                "toolKey": "claude-code-work",
            },
            "claude-code-err": {
                "source": "claude_local",
                "name": "Errored",
                "location": str(err_dir),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "needs_attention",
                "toolKey": "claude-code-err",
            },
            "claude-code-dis": {
                "source": "claude_local",
                "name": "Disabled",
                "location": str(dis_dir),
                "origin": "manual",
                "enabled": False,
                "revision": 2,
                "state": "disabled",
                "toolKey": "claude-code-dis",
            },
            "claude-code-stale": {
                "source": "claude_local",
                "name": "Stale >24h",
                "location": str(stale_dir),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "receiving_usage",
                "toolKey": "claude-code-stale",
            },
        }
        # External env var makes traycer_local / anthropic_admin externally_managed
        state["profiles"]["traycer_local"] = {
            "default": {
                "source": "traycer_local",
                "name": "Default",
                "location": str(home / ".traycer"),
                "origin": "default",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
            }
        }

    now = datetime.now(UTC)
    # Default claude-code receives priced usage within last 24h
    _insert_usage_event(settings.database_path, "claude-code", now - timedelta(hours=2), unpriced=False)
    # Work profile receives unpriced usage within last 24h
    _insert_usage_event(settings.database_path, "claude-code-work", now - timedelta(hours=1), unpriced=True)
    # Stale profile ingested > 24h ago -> state must be "added", NOT "receiving"
    _insert_usage_event(settings.database_path, "claude-code-stale", now - timedelta(hours=25), unpriced=False)
    _insert_usage_event(settings.database_path, "claude-code-stale", now - timedelta(hours=26), unpriced=True)

    # Externally managed provider via env var
    monkeypatch.setenv("ANTHROPIC_ADMIN_KEY", "sk-ant-admin-external")
    # Also test disabled non-profile source & needs_attention on traycer_local via separate check
    # Count SQL statements executed during Service.list()
    executed_sql: list[str] = []
    real_connect = sqlite3.connect

    class _CountingConn:
        def __init__(self, inner):
            self._inner = inner

        def execute(self, sql, *args, **kwargs):
            executed_sql.append(sql)
            return self._inner.execute(sql, *args, **kwargs)

        def close(self):
            return self._inner.close()

        def __getattr__(self, name):
            return getattr(self._inner, name)

    monkeypatch.setattr(c.sqlite3, "connect", lambda *a, **kw: _CountingConn(real_connect(*a, **kw)))

    # Check Service.list() directly for exact SQL query budget (store.read + 1 bounded ingest query)
    direct = c.Service(settings).list()
    assert "connections" in direct
    direct_usage_queries = [q for q in executed_sql if "usage_events" in q]
    assert len(direct_usage_queries) == 1
    assert "unpriced_usage_events" in direct_usage_queries[0]
    assert len(executed_sql) <= 4

    executed_sql.clear()
    resp = client.get("/api/connections")
    assert resp.status_code == 200
    by_source = {row["source"]: row for row in resp.json()["connections"]}

    # Verify single bounded SQL query over usage_events AND unpriced_usage_events on HTTP GET too
    usage_queries = [q for q in executed_sql if "usage_events" in q]
    assert len(usage_queries) == 1
    assert "unpriced_usage_events" in usage_queries[0]
    assert len(executed_sql) <= 10

    claude_profs = {p["key"]: p for p in by_source["claude_local"]["profiles"]}
    assert claude_profs["default"]["state"] == "receiving"
    assert claude_profs["claude-code-work"]["state"] == "receiving"
    assert claude_profs["claude-code-err"]["state"] == "error"
    assert claude_profs["claude-code-dis"]["state"] == "disabled"
    assert claude_profs["claude-code-stale"]["state"] == "added"

    # Codex default folder missing -> not_found
    assert by_source["codex_local"]["profiles"][0]["state"] == "not_found"
    # Anthropic Admin external -> externally_managed
    assert by_source["anthropic_admin"]["state"] == "externally_managed"
    assert by_source["anthropic_admin"]["profiles"][0]["state"] == "externally_managed"


@pytest.mark.parametrize(
    "raw_state,enabled,external_key,expected_prof_state,expected_row_state",
    [
        ("receiving_usage", True, False, "receiving", "receiving_usage"),
        ("balance_available", True, False, "receiving", "balance_available"),
        ("connected", True, False, "receiving", "connected"),
        ("awaiting_ingest", True, False, "added", "awaiting_ingest"),
        ("waiting_activity", True, False, "added", "waiting_activity"),
        ("receiving_usage", False, False, "disabled", "receiving_usage"),
        ("needs_attention", True, False, "error_preserved", "needs_attention"),
        ("not_connected", True, False, "not_connected", "not_connected"),
        ("not_connected", True, True, "externally_managed", "externally_managed"),
    ],
)
def test_api_profile_state_mapping_preserves_top_level_state(
    settings: Settings,
    isolated_env: Path,
    monkeypatch,
    raw_state: str,
    enabled: bool,
    external_key: bool,
    expected_prof_state: str,
    expected_row_state: str,
):
    if external_key:
        monkeypatch.setenv("OPENAI_ADMIN_KEY", "sk-ext")
    if raw_state != "not_connected":
        with c.Store(settings.database_path).transaction() as state:
            state["profiles"]["openai_admin"] = {
                "default": {
                    "source": "openai_admin",
                    "name": "Default",
                    "location": "",
                    "origin": "default",
                    "mode": "api",
                    "enabled": enabled,
                    "revision": 1,
                    "state": raw_state,
                }
            }

    row = next(r for r in c.Service(settings).list()["connections"] if r["source"] == "openai_admin")
    expected_profile = "needs_attention" if expected_prof_state == "error_preserved" else expected_prof_state
    assert row["state"] == expected_row_state
    assert row["profiles"][0]["state"] == expected_profile
    assert row["supportsProfiles"] is False
    assert row["detected"] == []


def test_get_connections_never_writes_missing_db_or_v2_meta(
    tmp_path: Path, isolated_env: Path
):
    home = isolated_env
    # Default claude folder exists; codex default folder does NOT exist
    _make_claude_folder(home / ".claude")

    db_path = tmp_path / "uncreated.db"
    assert not db_path.exists()
    cfg = Settings(
        database_path=db_path,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )

    app = FastAPI()
    app.include_router(connection_router(cfg))
    router_client = TestClient(app)

    resp = router_client.get("/api/connections")
    assert resp.status_code == 200
    # Missing DB must NOT be created by GET
    assert not db_path.exists()

    by_source = {r["source"]: r for r in resp.json()["connections"]}
    claude_default = by_source["claude_local"]["profiles"][0]
    assert claude_default["key"] == "default"
    assert claude_default["origin"] == "default"
    assert claude_default["revision"] == 0
    assert claude_default["state"] == "added"

    codex_default = by_source["codex_local"]["profiles"][0]
    assert codex_default["key"] == "default"
    assert codex_default["origin"] == "default"
    assert codex_default["revision"] == 0
    assert codex_default["state"] == "not_found"

    # Also verify when DB exists with tables but no connections.v2 entry, GET writes NO connections.v2
    empty_meta_db = tmp_path / "no-v2.db"
    with spend_db.connect(empty_meta_db) as conn:
        conn.execute("CREATE TABLE IF NOT EXISTS app_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    cfg2 = Settings(
        database_path=empty_meta_db,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    app2 = FastAPI()
    app2.include_router(connection_router(cfg2))
    assert TestClient(app2).get("/api/connections").status_code == 200
    with sqlite3.connect(empty_meta_db.absolute().as_uri() + "?mode=ro", uri=True) as ro:
        keys = {r[0] for r in ro.execute("SELECT key FROM app_meta").fetchall()}
    assert ps.KEY not in keys


def test_get_connections_path_formatting_home_tilde_vs_outside_home(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    home = isolated_env
    inside_added = _make_claude_folder(home / "profiles" / "claude-inside")
    inside_cand = _make_claude_folder(home / ".claude-cand", account_uuid="cand-in")
    outside_dir = tmp_path / "outside-home" / "claude-outside"
    _make_claude_folder(outside_dir)

    with c.Store(settings.database_path).transaction() as state:
        state["profiles"]["claude_local"] = {
            "default": {
                "source": "claude_local",
                "name": "Default",
                "location": str(inside_added),
                "origin": "default",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
            },
            "claude-code-outside": {
                "source": "claude_local",
                "name": "Outside",
                "location": str(outside_dir),
                "origin": "manual",
                "enabled": True,
                "revision": 1,
                "state": "awaiting_ingest",
            },
        }

    resp = client.get("/api/connections")
    assert resp.status_code == 200
    claude_row = next(r for r in resp.json()["connections"] if r["source"] == "claude_local")

    # Top-level legacy row["location"] remains unchanged (full raw path)
    assert claude_row["location"] == str(inside_added)

    profs = {p["key"]: p for p in claude_row["profiles"]}
    assert profs["default"]["location"] == "~/profiles/claude-inside"
    assert profs["claude-code-outside"]["location"] == str(outside_dir)

    assert len(claude_row["detected"]) == 1
    assert claude_row["detected"][0]["folder"] == "~/.claude-cand"
    assert inside_cand.exists()


# ---------------------------------------------------------------------------
# 2. POST /api/connections operations, validation, security & error codes
# ---------------------------------------------------------------------------


def test_post_add_profile_candidate_manual_names_and_errors(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    home = isolated_env
    cand_dir = _make_claude_folder(home / ".claude-work", account_uuid="acct-cand-1", plan="max")
    manual_dir = _make_codex_folder(tmp_path / "manual-codex-alpha", account_id="acct-manual-1")

    # 1. Add via candidateId (omit name -> folder-derived label, NO revision required)
    listing = client.get("/api/connections").json()
    claude_row = next(r for r in listing["connections"] if r["source"] == "claude_local")
    cand = next(d for d in claude_row["detected"] if d["folder"] == "~/.claude-work")

    add_cand_body = {
        "source": "claude_local",
        "operation": "add_profile",
        "candidateId": cand["candidateId"],
        "requestId": str(uuid4()),
    }
    res1 = client.post("/api/connections", json=add_cand_body, headers=API_HEADERS)
    assert res1.status_code == 200, res1.text
    conn1 = res1.json()["connection"]
    assert conn1["key"] == "claude-code-work"
    assert conn1["name"] == "Work"
    assert conn1["origin"] == "detected"
    assert conn1["revision"] == 1

    # Idempotency replay returns identical response; mismatched body with same requestId -> 409
    assert client.post("/api/connections", json=add_cand_body, headers=API_HEADERS).json() == res1.json()
    mismatch = {**add_cand_body, "name": "Different"}
    assert client.post("/api/connections", json=mismatch, headers=API_HEADERS).status_code == 409

    # Candidate no longer appears in detected[], now appears in profiles[]
    after_listing = client.get("/api/connections").json()
    claude_after = next(r for r in after_listing["connections"] if r["source"] == "claude_local")
    assert all(d["candidateId"] != cand["candidateId"] for d in claude_after["detected"])
    assert any(p["key"] == "claude-code-work" for p in claude_after["profiles"])

    # 2. Add via manual location with explicit trimmed name NOT passed through redact()
    secret_looking_name = "  alice@work.example  "
    assert redact(secret_looking_name.strip()) != secret_looking_name.strip()
    res2 = client.post(
        "/api/connections",
        json={
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(manual_dir),
            "name": secret_looking_name,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert res2.status_code == 200, res2.text
    conn2 = res2.json()["connection"]
    assert conn2["origin"] == "manual"
    assert conn2["name"] == "alice@work.example"
    assert conn2["revision"] == 1

    # 3. Validation failures -> 422
    extra_dir = _make_claude_folder(tmp_path / "extra-valid-claude")
    for bad_name in ("", "   ", "bad\x00name", "bad\nname", "a" * 41):
        r = client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "add_profile",
                "location": str(extra_dir),
                "name": bad_name,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        )
        assert r.status_code == 422, f"Expected 422 for name={bad_name!r}, got {r.status_code}"

    # Unsupported providers -> 422
    for unsupported in ("traycer_local", "openrouter", "deepseek", "openai_admin", "anthropic_admin"):
        r = client.post(
            "/api/connections",
            json={
                "source": unsupported,
                "operation": "add_profile",
                "location": str(extra_dir),
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        )
        assert r.status_code == 422, unsupported

    # 4. FolderInUse -> 409 (already added folder, subpath, and virtual existing default)
    r_dup_folder = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(cand_dir / "projects"),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_dup_folder.status_code == 409

    # Virtual default folder exists on disk -> adding that folder -> 409 FolderInUse
    default_claude = _make_claude_folder(home / ".claude")
    r_virtual_def = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(default_claude),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_virtual_def.status_code == 409

    # 5. Duplicate account_digest -> 409
    dup_acct_dir = _make_claude_folder(tmp_path / "claude-same-account", account_uuid="acct-cand-1", plan="max")
    r_dup_acct = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(dup_acct_dir),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_dup_acct.status_code == 409


def test_post_rename_profile_added_virtual_default_and_validation(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    home = isolated_env
    _make_claude_folder(home / ".claude")
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "def-uuid", "subscriptionType": "max"}}),
        encoding="utf-8",
    )
    work_dir = _make_claude_folder(tmp_path / "claude-work-ren", account_uuid="work-uuid")

    add_res = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(work_dir),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    ).json()
    work_key = add_res["connection"]["key"]

    # Rename added profile (revision 1 -> 2), verifying no redaction
    rename_body = {
        "source": "claude_local",
        "operation": "rename_profile",
        "profile": work_key,
        "revision": 1,
        "name": "  team@company.example  ",
        "requestId": str(uuid4()),
    }
    r1 = client.post("/api/connections", json=rename_body, headers=API_HEADERS)
    assert r1.status_code == 200
    assert r1.json()["connection"]["name"] == "team@company.example"
    assert r1.json()["connection"]["revision"] == 2

    # Revision conflict -> 409
    r_conflict = client.post(
        "/api/connections",
        json={**rename_body, "revision": 1, "name": "Stale", "requestId": str(uuid4())},
        headers=API_HEADERS,
    )
    assert r_conflict.status_code == 409

    # Invalid / control-char / >40 char names -> 422
    for bad in ("", "   ", "bad\x07name", "x" * 41, None):
        r_bad = client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "rename_profile",
                "profile": work_key,
                "revision": 2,
                "name": bad,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        )
        assert r_bad.status_code == 422

    # Materialize virtual "default" at revision 0 -> revision 1
    r_def = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "rename_profile",
            "profile": "default",
            "revision": 0,
            "name": "Personal Account",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_def.status_code == 200
    assert r_def.json()["connection"]["key"] == "default"
    assert r_def.json()["connection"]["revision"] == 1
    assert r_def.json()["connection"]["name"] == "Personal Account"
    assert r_def.json()["connection"]["origin"] == "default"


def test_post_remove_profile_non_default_and_default_preserve_history(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    home = isolated_env
    _make_claude_folder(home / ".claude")
    work_dir = _make_claude_folder(tmp_path / "claude-work-rem")

    add_res = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(work_dir),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    ).json()
    work_key = add_res["connection"]["key"]
    work_tool_key = add_res["connection"]["toolKey"]

    now = datetime.now(UTC)
    _insert_usage_event(settings.database_path, "claude-code", now - timedelta(minutes=10))
    _insert_usage_event(settings.database_path, work_tool_key, now - timedelta(minutes=10))

    # Revision conflict on non-default remove -> 409
    assert (
        client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "remove_profile",
                "profile": work_key,
                "revision": 0,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        ).status_code
        == 409
    )

    # Remove non-default profile -> removes profile entry, preserves usage_events
    rem_non_def = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "remove_profile",
            "profile": work_key,
            "revision": 1,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert rem_non_def.status_code == 200
    assert rem_non_def.json()["removed"] is True

    # Removing virtual/non-existent default with bad revision -> 409
    assert (
        client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "remove_profile",
                "profile": "default",
                "revision": 1,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        ).status_code
        == 409
    )

    # Removing "default" profile at revision 0 ONLY disables and preserves usage_events
    rem_def = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "remove_profile",
            "profile": "default",
            "revision": 0,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert rem_def.status_code == 200
    assert rem_def.json()["disabled"] is True
    assert rem_def.json()["connection"]["enabled"] is False
    assert rem_def.json()["connection"]["state"] == "disabled"
    assert rem_def.json()["connection"]["revision"] == 1

    # Verify via GET /api/connections
    claude_row = next(
        r for r in client.get("/api/connections").json()["connections"] if r["source"] == "claude_local"
    )
    assert [p["key"] for p in claude_row["profiles"]] == ["default"]
    assert claude_row["profiles"][0]["state"] == "disabled"
    assert claude_row["profiles"][0]["revision"] == 1
    assert claude_row["removedProfileKeys"] == [work_key]

    from spend_app.wealthfront_window import merge_provider_entries

    assert merge_provider_entries(
        {"tools": [{"key": "claude-code"}, {"key": work_tool_key}]},
        hud={"capacity": [{"providerKey": "claude-code"}, {"providerKey": work_tool_key}]},
        connections=[claude_row],
    ) == []

    # Verify BOTH usage_events rows are preserved untouched
    with spend_db.connect(settings.database_path) as conn:
        counts = dict(
            conn.execute("SELECT tool_key, COUNT(*) FROM usage_events GROUP BY tool_key").fetchall()
        )
    assert counts.get("claude-code") == 1
    assert counts.get(work_tool_key) == 1


def test_post_dismiss_candidate_existing_ops_and_security_guards(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    home = isolated_env
    _make_claude_folder(home / ".claude-cand", account_uuid="cand-acct")
    work_dir = _make_claude_folder(tmp_path / "claude-ops-work", account_uuid="ops-acct-1")
    other_dir = _make_claude_folder(tmp_path / "claude-ops-other", account_uuid="ops-acct-2")
    dup_acct_dir = _make_claude_folder(tmp_path / "claude-ops-dup-acct", account_uuid="ops-acct-2")

    # 1. dismiss_candidate
    claude_row = next(
        r for r in client.get("/api/connections").json()["connections"] if r["source"] == "claude_local"
    )
    cand_id = claude_row["detected"][0]["candidateId"]
    dismiss_req = {
        "source": "claude_local",
        "operation": "dismiss_candidate",
        "candidateId": cand_id,
        "requestId": str(uuid4()),
    }
    res_dis = client.post("/api/connections", json=dismiss_req, headers=API_HEADERS)
    assert res_dis.status_code == 200
    assert res_dis.json()["dismissed"] is True
    # Idempotent replay
    assert client.post("/api/connections", json=dismiss_req, headers=API_HEADERS).json() == res_dis.json()
    # Hidden from detected[]
    claude_after = next(
        r for r in client.get("/api/connections").json()["connections"] if r["source"] == "claude_local"
    )
    assert claude_after["detected"] == []

    # Missing / blank candidateId -> 422
    for bad_cid in (None, "", "   ", "x" * 129):
        r_bad = client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "dismiss_candidate",
                "candidateId": bad_cid,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        )
        assert r_bad.status_code == 422

    # 2. Existing ops (connect, recheck, disable, reconnect) require an existing non-default profile
    for op_name in ("connect", "reconnect", "recheck", "disable"):
        missing_payload = {
            "source": "claude_local",
            "operation": op_name,
            "profile": "claude-code-missing",
            "revision": 0,
            "requestId": str(uuid4()),
        }
        if op_name in {"connect", "reconnect"}:
            missing_payload["location"] = str(work_dir)
        assert client.post("/api/connections", json=missing_payload, headers=API_HEADERS).status_code == 422

    # Create profiles via add_profile (revision=1)
    r_conn = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(work_dir),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_conn.status_code == 200
    ops_key = r_conn.json()["connection"]["key"]
    assert r_conn.json()["connection"]["revision"] == 1

    r_other = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(other_dir),
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_other.status_code == 200

    # connect / reconnect pointing ops_key to other_dir's folder -> 409 FolderInUse
    assert (
        client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "reconnect",
                "profile": ops_key,
                "location": str(other_dir),
                "revision": 1,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        ).status_code
        == 409
    )

    # connect / reconnect pointing to a different folder with duplicate account_digest -> 409
    assert (
        client.post(
            "/api/connections",
            json={
                "source": "claude_local",
                "operation": "connect",
                "profile": ops_key,
                "location": str(dup_acct_dir),
                "revision": 1,
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        ).status_code
        == 409
    )

    # recheck -> revision 2; disable -> revision 3; reconnect -> revision 4
    r_recheck = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "recheck",
            "profile": ops_key,
            "revision": 1,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_recheck.status_code == 200 and r_recheck.json()["connection"]["revision"] == 2

    r_disable = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "disable",
            "profile": ops_key,
            "revision": 2,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_disable.status_code == 200
    assert r_disable.json()["connection"]["enabled"] is False
    assert r_disable.json()["connection"]["revision"] == 3

    r_reconn = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "reconnect",
            "profile": ops_key,
            "location": str(work_dir),
            "revision": 3,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_reconn.status_code == 200
    assert r_reconn.json()["connection"]["enabled"] is True
    assert r_reconn.json()["connection"]["revision"] == 4

    # 3. Security & HTTP boundary guards
    # Cross-site Origin -> 403
    assert (
        client.post(
            "/api/connections",
            json=dismiss_req,
            headers={"X-BURNRATE-Request": "1", "Origin": "https://evil.example"},
        ).status_code
        == 403
    )
    # Sec-Fetch-Site: cross-site -> 403
    assert (
        client.post(
            "/api/connections",
            json=dismiss_req,
            headers={**API_HEADERS, "Sec-Fetch-Site": "cross-site"},
        ).status_code
        == 403
    )
    # Missing X-BURNRATE-Request: 1 -> 403
    assert (
        client.post(
            "/api/connections",
            json=dismiss_req,
            headers={"Origin": "http://testserver"},
        ).status_code
        == 403
    )
    # Bad Content-Type -> 422
    assert (
        client.post(
            "/api/connections",
            content=json.dumps(dismiss_req),
            headers={**API_HEADERS, "Content-Type": "text/plain"},
        ).status_code
        == 422
    )


# ---------------------------------------------------------------------------
# 3. POST /api/connections/discover & Service.discover()
# ---------------------------------------------------------------------------


def test_post_discover_resets_cache_and_service_discover_keeps_legacy_shape(
    client: TestClient, settings: Settings, isolated_env: Path, monkeypatch
):
    home = isolated_env

    # Warm cache with NO detected folders
    initial = client.get("/api/connections").json()
    claude_init = next(r for r in initial["connections"] if r["source"] == "claude_local")
    assert claude_init["detected"] == []

    # Create a new profile folder AFTER cache was populated
    _make_claude_folder(home / ".claude-newly-added", account_uuid="new-cand-uuid", plan="max")

    # Without reset_cache(), cache still holds empty list; POST /api/connections/discover resets it
    reset_calls = []
    orig_reset = profile_detect.reset_cache

    def _spy_reset():
        reset_calls.append(1)
        return orig_reset()

    monkeypatch.setattr(profile_detect, "reset_cache", _spy_reset)

    disc_resp = client.post("/api/connections/discover", json={}, headers=API_HEADERS)
    assert disc_resp.status_code == 200
    assert len(reset_calls) >= 1

    body = disc_resp.json()
    assert set(body.keys()) == {"connections", "vaultAvailable", "computerScope"}
    claude_after = next(r for r in body["connections"] if r["source"] == "claude_local")
    assert [d["folder"] for d in claude_after["detected"]] == ["~/.claude-newly-added"]

    # Service.discover() also resets profile_detect cache and keeps its existing return shape
    reset_calls.clear()
    svc_disc = c.Service(settings).discover()
    assert len(reset_calls) == 1
    assert set(svc_disc.keys()) == {"connections"}
    assert all("candidates" in row for row in svc_disc["connections"])
    assert all(
        row["kind"] not in {"api", "external"} and not REGISTRY.get(row["source"]).connection.manual_only
        for row in svc_disc["connections"]
    )


# ---------------------------------------------------------------------------
# 4. Updated callers: setup_wizard, native_onboarding, DemoClient
# ---------------------------------------------------------------------------


def test_setup_wizard_api_connections_handles_grouped_rows_and_never_exposes_secrets(
    settings: Settings, monkeypatch
):
    fake_listing = {
        "vaultAvailable": True,
        "connections": [
            {
                "source": "claude_local",
                "name": "Claude Code",
                "kind": "local",
                "state": "already_monitored",
                "profiles": [{"key": "default", "state": "added", "revision": 0}],
            },
            {
                "source": "openrouter",
                "name": "OpenRouter balance",
                "kind": "api",
                "state": None,
                "detail": "Connected",
                "enabled": None,
                "revision": None,
                "capabilities": ["balance"],
                "credentialHelp": "Management key",
                "externallyManaged": False,
                "credentialConfigured": True,
                "credentialRef": "burnrate-secret-ref-should-leak-never",
                "secret": "sk-or-v1-must-never-leak",
                "lastImport": "2026-10-01T00:00:00Z",
                "profiles": [
                    {
                        "key": "default",
                        "name": "Default",
                        "location": "",
                        "origin": "default",
                        "state": "receiving",
                        "revision": 3,
                        "plan": None,
                        "sharesLimitsWith": None,
                        "credentialRef": "burnrate-nested-secret",
                    }
                ],
                "detected": [],
            },
            {
                "source": "deepseek",
                "name": "DeepSeek balance",
                "kind": "api",
                "state": None,
                "detail": "Disabled",
                "enabled": None,
                "revision": None,
                "capabilities": ["balance"],
                "credentialHelp": "Key",
                "externallyManaged": False,
                "credentialConfigured": False,
                "lastImport": None,
                "profiles": [
                    {
                        "key": "default",
                        "state": "disabled",
                        "revision": 2,
                    }
                ],
            },
        ],
    }
    monkeypatch.setattr(c.Service, "list", lambda self: fake_listing)

    res = _api_connections(settings)
    assert res["vaultAvailable"] is True
    assert [row["source"] for row in res["connections"]] == ["openrouter", "deepseek"]

    or_row, ds_row = res["connections"]
    assert or_row["revision"] == 3
    assert or_row["state"] == "receiving"
    assert or_row["enabled"] is True
    assert ds_row["revision"] == 2
    assert ds_row["state"] == "disabled"
    assert ds_row["enabled"] is False

    serialized = json.dumps(res)
    assert "credentialRef" not in serialized
    assert "burnrate-secret-ref" not in serialized
    assert "sk-or-v1-must-never-leak" not in serialized


def test_native_onboarding_reflects_extra_profiles_detected_candidates_and_flat_rows(tmp_path: Path):
    # 1. Grouped rows: non-default active profile makes tool active; detected candidate makes tool detected
    class _GroupedClient:
        def request(self, path: str):
            if path == "/api/connections":
                return {
                    "connections": [
                        {
                            "source": "claude_local",
                            "name": "Claude Code",
                            "kind": "local",
                            "state": "not_connected",
                            "profiles": [
                                {"key": "default", "state": "not_found", "revision": 0},
                                {"key": "claude-code-work", "state": "receiving", "revision": 1},
                            ],
                            "detected": [],
                        },
                        {
                            "source": "codex_local",
                            "name": "Codex CLI",
                            "kind": "local",
                            "state": "not_connected",
                            "profiles": [{"key": "default", "state": "not_found", "revision": 0}],
                            "detected": [
                                {"candidateId": "c1", "folder": "~/.codex-work", "plan": None, "sameAccountAs": None}
                            ],
                        },
                        {
                            "source": "openrouter",
                            "name": "OpenRouter balance",
                            "kind": "api",
                            "state": None,
                            "profiles": [{"key": "default", "state": "receiving", "revision": 4}],
                        },
                    ]
                }
            return {"setup": {}}

    grouped = build_onboarding_data(
        _GroupedClient(), which=lambda _cmd: None, env={}, home=tmp_path
    )
    assert grouped["ok"] is True
    tools = {t["source"]: t for t in grouped["tools"]}
    assert tools["claude_local"]["active"] is True
    assert tools["claude_local"]["detected"] is True
    assert tools["codex_local"]["active"] is False
    assert tools["codex_local"]["detected"] is True
    apis = {a["source"]: a for a in grouped["apiSources"]}
    assert apis["openrouter"]["connected"] is True
    assert apis["openrouter"]["revision"] == 4

    # 2. Compatibility with legacy flat mock rows (no profiles / detected keys)
    class _FlatClient:
        def request(self, path: str):
            if path == "/api/connections":
                return {
                    "connections": [
                        {"source": "claude_local", "kind": "local", "state": "receiving_usage", "revision": 2},
                        {"source": "openrouter", "name": "OpenRouter", "kind": "api", "state": "balance_available", "revision": 5},
                    ]
                }
            return {"setup": {}}

    flat = build_onboarding_data(_FlatClient(), which=lambda _cmd: None, env={}, home=tmp_path)
    assert flat["ok"] is True
    flat_tools = {t["source"]: t for t in flat["tools"]}
    assert flat_tools["claude_local"]["active"] is True
    assert flat_tools["claude_local"]["detected"] is True
    flat_apis = {a["source"]: a for a in flat["apiSources"]}
    assert flat_apis["openrouter"]["connected"] is True
    assert flat_apis["openrouter"]["revision"] == 5


def test_demo_client_connections_include_profile_fields():
    data = DemoClient().request("/api/connections")
    assert "connections" in data and len(data["connections"]) >= 1
    for row in data["connections"]:
        assert {"supportsProfiles", "profiles", "detected"} <= set(row.keys())
        assert isinstance(row["supportsProfiles"], bool)
        assert isinstance(row["profiles"], list) and len(row["profiles"]) >= 1
        assert isinstance(row["detected"], list)
        for prof in row["profiles"]:
            assert {
                "key",
                "name",
                "location",
                "origin",
                "state",
                "revision",
                "plan",
                "sharesLimitsWith",
            } <= set(prof.keys())


# ---------------------------------------------------------------------------
# 5. Review probes: live account digest tracking & non-default connect guard
# ---------------------------------------------------------------------------


def test_add_rechecks_current_account_of_existing_profile(
    client: TestClient, settings: Settings, isolated_env: Path
):
    home = isolated_env
    work = _make_codex_folder(home / ".codex-work", account_id="account-A")
    other = _make_codex_folder(home / ".codex-other", account_id="account-B")

    service = c.Service(settings)
    added = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(work),
            "name": "Work",
            "requestId": str(uuid4()),
        }
    )["connection"]
    work_key = added["key"]

    # Switch Work's auth.json from account-A to account-B (without even resetting cache)
    (work / "auth.json").write_text(json.dumps({"tokens": {"account_id": "account-B"}}), encoding="utf-8")

    # Adding another folder signed in to account-B is refused (409 / Conflict)
    with pytest.raises(c.Conflict):
        service.mutate(
            {
                "source": "codex_local",
                "operation": "add_profile",
                "location": str(other),
                "name": "Other",
                "requestId": str(uuid4()),
            }
        )

    # Same via HTTP POST returns 409
    r_dup = client.post(
        "/api/connections",
        json={
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(other),
            "name": "Other",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_dup.status_code == 409

    # GET /api/connections remains read-only (stored accountDigest stays account-A),
    # while reporting live sameAccountAs -> work_key after cache reset
    profile_detect.reset_cache()
    codex_row = next(r for r in client.get("/api/connections").json()["connections"] if r["source"] == "codex_local")
    assert len(codex_row["detected"]) == 1
    assert codex_row["detected"][0]["sameAccountAs"] == work_key

    stored_before = c.Store(settings.database_path).read()["profiles"]["codex_local"][work_key]
    assert stored_before["accountDigest"] == _sha256("account-A")

    # Recheck (a write) refreshes the stored accountDigest to account-B
    r_recheck = client.post(
        "/api/connections",
        json={
            "source": "codex_local",
            "operation": "recheck",
            "profile": work_key,
            "revision": 1,
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_recheck.status_code == 200
    stored_after = c.Store(settings.database_path).read()["profiles"]["codex_local"][work_key]
    assert stored_after["accountDigest"] == _sha256("account-B")

    # A third folder with account-A is now allowed since Work switched to account-B
    third = _make_codex_folder(home / ".codex-third", account_id="account-A")
    r_third = client.post(
        "/api/connections",
        json={
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(third),
            "name": "Third",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_third.status_code == 200


def test_limits_deduplicate_after_account_switch(
    client: TestClient, settings: Settings, isolated_env: Path
):
    from spend_app import quotas

    home = isolated_env
    first = _make_codex_folder(home / ".codex-first", account_id="account-A")
    second = _make_codex_folder(home / ".codex-second", account_id="account-B")
    service = c.Service(settings)
    res_first = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(first),
            "name": "First",
            "requestId": str(uuid4()),
        }
    )["connection"]
    res_second = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(second),
            "name": "Second",
            "requestId": str(uuid4()),
        }
    )["connection"]

    # Switch second profile from account-B to account-A on disk
    (second / "auth.json").write_text(json.dumps({"tokens": {"account_id": "account-A"}}), encoding="utf-8")
    profile_detect.reset_cache()

    collectors, shared = quotas._resolve_profile_lanes(settings.database_path)
    assert len(collectors) == 1, (list(collectors), shared)
    assert res_first["key"] in collectors
    assert res_second["key"] in shared
    assert shared[res_second["key"]]["ownerKey"] == res_first["key"]

    # GET /api/connections also reflects sharesLimitsWith using current account digest
    codex_row = next(r for r in client.get("/api/connections").json()["connections"] if r["source"] == "codex_local")
    by_key = {p["key"]: p for p in codex_row["profiles"]}
    assert by_key[res_first["key"]]["sharesLimitsWith"] is None
    assert by_key[res_second["key"]]["sharesLimitsWith"] == res_first["key"]


def test_connect_requires_existing_nondefault_profile(
    client: TestClient, settings: Settings, isolated_env: Path
):
    home = isolated_env
    folder = _make_codex_folder(home / ".codex-custom", account_id="account-A")
    service = c.Service(settings)

    # connect/reconnect/recheck/disable for non-existent non-default profile (including built-in tool key "codex") must fail
    for op_name in ("connect", "reconnect", "recheck", "disable"):
        payload = {
            "source": "codex_local",
            "operation": op_name,
            "profile": "codex",
            "revision": 0,
            "requestId": str(uuid4()),
        }
        if op_name in {"connect", "reconnect"}:
            payload["location"] = str(folder)
        with pytest.raises(c.LocationError):
            service.mutate(payload)
        assert client.post("/api/connections", json={**payload, "requestId": str(uuid4())}, headers=API_HEADERS).status_code == 422

    # add_profile refuses reserved built-in default keys, "default", and cross-provider keys
    for bad_key in ("default", *ps.DEFAULT_TOOL_KEYS.values(), "claude-code-work", "opencode-go-team"):
        r_bad = client.post(
            "/api/connections",
            json={
                "source": "codex_local",
                "operation": "add_profile",
                "profile": bad_key,
                "location": str(folder),
                "requestId": str(uuid4()),
            },
            headers=API_HEADERS,
        )
        assert r_bad.status_code == 422

    # profile_key_for_folder never produces "default" or any DEFAULT_TOOL_KEYS value
    for prov in ps.DEFAULT_TOOL_KEYS:
        generated = ps.profile_key_for_folder(prov, home / "some-folder")
        assert generated != "default"
        assert generated not in set(ps.DEFAULT_TOOL_KEYS.values())

def test_default_record_at_custom_claude_folder_uses_its_identity(
    client: TestClient, settings: Settings, isolated_env: Path
):
    home = isolated_env
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "personal-A", "organizationRateLimitTier": "default_claude_max_20x"}}),
        encoding="utf-8",
    )
    custom = _make_claude_folder(home / ".claude-custom")
    (custom / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "work-B", "organizationRateLimitTier": "default_claude_pro"}}),
        encoding="utf-8",
    )

    service = c.Service(settings)
    service.mutate(
        {
            "source": "claude_local",
            "operation": "connect",
            "location": str(custom),
            "revision": 0,
            "requestId": str(uuid4()),
        }
    )

    rec = c.Store(settings.database_path).read()["profiles"]["claude_local"]["default"]
    assert rec["accountDigest"] == _sha256("work-B"), rec
    assert rec["plan"] == "Claude Pro"

    # GET /api/connections reflects custom folder's plan and keeps personal-A separate
    claude_row = next(r for r in client.get("/api/connections").json()["connections"] if r["source"] == "claude_local")
    by_key = {p["key"]: p for p in claude_row["profiles"]}
    assert by_key["default"]["plan"] == "Claude Pro"
    assert by_key["default"]["sharesLimitsWith"] is None

    # Adding another folder signed in to personal-A succeeds because default is on work-B
    personal_extra = _make_claude_folder(
        home / ".claude-personal-copy", account_uuid="personal-A", plan="max"
    )
    res = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(personal_extra),
            "name": "Personal Copy",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert res.status_code == 200


def test_duplicate_of_custom_default_account_is_refused(
    client: TestClient, settings: Settings, isolated_env: Path
):
    home = isolated_env
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude.json").write_text(
        json.dumps({"oauthAccount": {"accountUuid": "personal-A"}}),
        encoding="utf-8",
    )
    custom = _make_claude_folder(home / ".claude-custom", account_uuid="work-B")
    other = _make_claude_folder(home / ".claude-other", account_uuid="work-B")

    service = c.Service(settings)
    service.mutate(
        {
            "source": "claude_local",
            "operation": "connect",
            "location": str(custom),
            "revision": 0,
            "requestId": str(uuid4()),
        }
    )

    with pytest.raises(c.Conflict):
        service.mutate(
            {
                "source": "claude_local",
                "operation": "add_profile",
                "location": str(other),
                "name": "Other",
                "requestId": str(uuid4()),
            }
        )

    r_dup = client.post(
        "/api/connections",
        json={
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(other),
            "name": "Other",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_dup.status_code == 409


def test_warm_digest_cache_cannot_bypass_write_check(
    client: TestClient, settings: Settings, isolated_env: Path
):
    home = isolated_env
    work = _make_codex_folder(home / ".codex-work", account_id="account-A")
    other = _make_codex_folder(home / ".codex-other", account_id="account-B")

    service = c.Service(settings)
    service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(work),
            "name": "Work",
            "requestId": str(uuid4()),
        }
    )

    # Warm the 60s digest cache with account-A via GET
    assert client.get("/api/connections").status_code == 200

    # Switch Work's auth.json to account-B while cache for `work` is still warm
    (work / "auth.json").write_text(
        json.dumps({"tokens": {"account_id": "account-B"}}), encoding="utf-8"
    )
    assert profile_detect.current_account_digest("codex_local", work) == _sha256("account-A")

    # Write path must bypass warm digest cache and reject adding other (account-B)
    r_dup = client.post(
        "/api/connections",
        json={
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(other),
            "name": "Other",
            "requestId": str(uuid4()),
        },
        headers=API_HEADERS,
    )
    assert r_dup.status_code == 409


def test_retargeted_root_keeps_stored_digest_on_get(
    client: TestClient, settings: Settings, isolated_env: Path, tmp_path: Path
):
    import os
    import subprocess
    from spend_app import connection_paths

    if os.name != "nt":
        pytest.skip("Windows junctions require os.name == 'nt'")

    home = isolated_env
    work = _make_codex_folder(home / ".codex-work", account_id="account-A")
    other = _make_codex_folder(home / ".codex-other", account_id="account-B")

    service = c.Service(settings)
    work_key = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(work),
            "name": "Work",
            "requestId": str(uuid4()),
        }
    )["connection"]["key"]
    other_key = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(other),
            "name": "Other",
            "requestId": str(uuid4()),
        }
    )["connection"]["key"]

    evil_target = _make_codex_folder(tmp_path / "evil-target", account_id="account-B")
    moved = tmp_path / "work-moved"
    work.rename(moved)
    res = subprocess.run(
        ["cmd", "/c", "mklink", "/J", str(work), str(evil_target)],
        capture_output=True,
        text=True,
        check=False,
    )
    if res.returncode != 0 or not connection_paths.is_junction(work):
        pytest.skip("Windows junction creation not supported here")

    profile_detect.reset_cache()
    assert profile_detect.current_account_digest("codex_local", work / "sessions") is None

    codex_row = next(r for r in client.get("/api/connections").json()["connections"] if r["source"] == "codex_local")
    by_key = {p["key"]: p for p in codex_row["profiles"]}
    assert by_key[work_key]["sharesLimitsWith"] is None
    assert by_key[other_key]["sharesLimitsWith"] is None
    stored = c.Store(settings.database_path).read()["profiles"]["codex_local"][work_key]
    assert stored["accountDigest"] == _sha256("account-A")

