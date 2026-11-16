"""Unit and integration tests for P4 · Usage limits per profile."""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from contextlib import closing
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

import spend_app.limits as limits
from spend_app import claude_accounts, codex_quota, opencode_profiles, profile_store
from spend_app.db import UsageEvent, connect, initialize, upsert_usage_event
from spend_app.quotas import (
    LANE_CADENCE,
    QuotaLaneScheduler,
    QuotaSample,
    _DATABASE_LANES,
    _codex_quota_collector,
    _is_codex_profile_key,
    _limit_order,
    codex_quota_samples,
    default_quota_collectors,
    lane_is_active,
    nudge_quota_lanes,
    poll_quotas,
    shared_limit_lanes,
)

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


class FixedClock(datetime):
    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return NOW.replace(tzinfo=None)
        return NOW.astimezone(tz)


class StepMonoClock:
    def __init__(self, start: float = 1_000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


@pytest.fixture(autouse=True)
def _clean_p4_caches():
    with limits._CACHE_LOCK:
        limits._CACHE.clear()
    _DATABASE_LANES.clear()
    claude_accounts.reset_cache()
    opencode_profiles.reset_cache()
    yield
    with limits._CACHE_LOCK:
        limits._CACHE.clear()
    _DATABASE_LANES.clear()
    claude_accounts.reset_cache()
    opencode_profiles.reset_cache()


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _write_codex_session(
    sessions_dir: Path,
    *,
    used_pct: float,
    age_seconds: int = 30,
    reset_days: int = 4,
    plan_type: str = "pro",
    limit_id: str = "codex",
    filename: str = "rollout.jsonl",
    now: datetime = NOW,
) -> Path:
    sessions_dir.mkdir(parents=True, exist_ok=True)
    observed = now - timedelta(seconds=age_seconds)
    reset = now + timedelta(days=reset_days)
    record = {
        "timestamp": _iso(observed),
        "payload": {
            "rate_limits": {
                "plan_type": plan_type,
                "limit_id": limit_id,
                "primary": {
                    "used_percent": 15.0,
                    "window_minutes": 300,
                    "resets_at": (now + timedelta(hours=4)).timestamp(),
                },
                "secondary": {
                    "used_percent": used_pct,
                    "window_minutes": 10080,
                    "resets_at": reset.timestamp(),
                },
            }
        },
    }
    path = sessions_dir / filename
    path.write_text(json.dumps(record) + "\n", encoding="utf-8")
    return path


def _write_claude_state(
    directory: Path,
    account_uuid: str | None,
    *,
    five_pct: float | None = None,
    weekly_pct: float | None = None,
    tier: str = "default_claude_max_20x",
    fetched: datetime = NOW,
) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    doc: dict = {}
    if account_uuid:
        doc["oauthAccount"] = {
            "accountUuid": account_uuid,
            "organizationRateLimitTier": tier,
            "emailAddress": "hidden@example.invalid",
        }
    if five_pct is not None or weekly_pct is not None:
        utilization: dict = {}
        if five_pct is not None:
            utilization["five_hour"] = {
                "utilization": five_pct,
                "resets_at": _iso(fetched + timedelta(hours=3)),
            }
        if weekly_pct is not None:
            utilization["seven_day"] = {
                "utilization": weekly_pct,
                "resets_at": _iso(fetched + timedelta(days=4)),
            }
        doc["cachedUsageUtilization"] = {
            "fetchedAtMs": int(fetched.timestamp() * 1000),
            "accountUuid": account_uuid,
            "utilization": utilization,
        }
    state_path = directory / ".claude.json"
    state_path.write_text(json.dumps(doc), encoding="utf-8")
    return state_path


def _write_opencode_db(path: Path, console_key: str | None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path)) as conn, conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS credential ("
            "id TEXT, integration_id TEXT, label TEXT, value TEXT, "
            "connector_id TEXT, method_id TEXT, active INTEGER, "
            "time_created INTEGER, time_updated INTEGER)"
        )
        if console_key:
            conn.execute(
                "INSERT INTO credential(id, integration_id, value, active, time_created) "
                "VALUES(?,?,?,?,?)",
                ("cred-1", "opencode", json.dumps({"type": "key", "key": console_key}), 1, 1),
            )
    return path


def _profile_record(
    provider: str,
    key: str,
    *,
    name: str,
    location: str | None,
    enabled: bool = True,
    account_digest: str | None = None,
    tool_key: str | None = None,
    origin: str | None = None,
) -> dict:
    return {
        "source": provider,
        "bindingId": f"{provider}:{key}",
        "name": name,
        "location": location,
        "origin": origin or ("default" if key == "default" else "manual"),
        "mode": "detected",
        "enabled": enabled,
        "revision": 1,
        "state": "connected" if enabled else "disabled",
        "detail": None,
        "lastVerification": None,
        "lastImport": None,
        "importRevision": None,
        "capabilities": ["usage"],
        "accountDigest": account_digest,
        "toolKey": tool_key if tool_key is not None else profile_store.profile_tool_key(provider, key),
    }


def _seed_profiles_db(db_path: Path, profiles_by_provider: dict[str, dict[str, dict]]) -> Path:
    initialize(db_path)
    state = profile_store.empty_state()
    state["profiles"] = profiles_by_provider
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (profile_store.KEY, profile_store.encode(state)),
        )
        conn.execute(
            "INSERT INTO app_meta(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (profile_store.MIGRATED_KEY, "1"),
        )
    return db_path


def _quota_rows(db_path: Path) -> dict[tuple[str, str], dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT provider_key, limit_key, label, used, allowance, unit, pct, resets_at, source, polled_at "
            "FROM quotas"
        ).fetchall()
    return {(r[0], r[1]): dict(zip(r.keys(), tuple(r))) for r in rows}


def _freeze_codex_clock(monkeypatch) -> None:
    monkeypatch.setattr(limits, "datetime", FixedClock)
    monkeypatch.setattr(codex_quota, "datetime", FixedClock)


def _isolate_default_poll_sources(tmp_path: Path, monkeypatch) -> Path:
    """Point default local paths into tmp_path and stub external/RPC pollers."""
    home = tmp_path / "home"
    home.mkdir(exist_ok=True)
    monkeypatch.setattr(limits.Path, "home", lambda: home)
    monkeypatch.setattr("spend_app.providers.Path.home", lambda: home)
    monkeypatch.setattr("spend_app.claude_accounts.Path.home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    monkeypatch.delenv("OPENROUTER_MANAGEMENT_KEY", raising=False)
    monkeypatch.delenv("DEEPSEEK_BALANCE_KEY", raising=False)
    monkeypatch.delenv("Z_AI_API_KEY", raising=False)
    monkeypatch.setenv("MUSE_AUTH_PATH", str(tmp_path / "missing-muse-auth.json"))
    monkeypatch.setattr("spend_app.quotas._antigravity_limits_uncached", lambda: {"status": "error", "windows": []})
    monkeypatch.setattr("spend_app.quotas._grok_limits_uncached", lambda: {"status": "unavailable", "windows": []})
    monkeypatch.setattr("spend_app.quotas._claude_desktop_limits_uncached", lambda: (_ for _ in ()).throw(RuntimeError("no desktop")))
    monkeypatch.setattr("spend_app.quotas._traycer_profile_rate_limits", lambda _tool: (_ for _ in ()).throw(RuntimeError("no traycer")))
    monkeypatch.setattr("spend_app.quotas._zai_limits_uncached", lambda: {"status": "unavailable", "windows": []})
    monkeypatch.setattr("spend_app.quotas._muse_limits_uncached", lambda: {"status": "unavailable", "windows": []})
    monkeypatch.setattr("spend_app.quotas._sync_after_update", lambda *a, **k: None)
    return home


# ---------------------------------------------------------------------------
# 1. limits._codex_limits(root=None, key="codex")
# ---------------------------------------------------------------------------


def test_codex_limits_no_args_preserves_default_behavior(tmp_path: Path, monkeypatch) -> None:
    _freeze_codex_clock(monkeypatch)
    home = tmp_path / "home"
    default_sessions = home / ".codex" / "sessions"
    _write_codex_session(default_sessions, used_pct=27.5, plan_type="pro")
    monkeypatch.setattr("spend_app.providers.Path.home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    result = limits._codex_limits()
    assert result["status"] == "exact"
    assert result["key"] == "codex"
    assert result["name"] == "Codex"
    assert result["poolId"] == "codex"
    assert result["plan"] == "ChatGPT Pro"
    assert result["scope"] == codex_quota.scope(default_sessions)
    assert len(result["windows"]) == 1
    assert result["windows"][0]["key"] == "weekly"
    assert result["windows"][0]["usedPct"] == 27.5
    assert result["windows"][0]["remainingPct"] == 72.5


def test_codex_limits_custom_root_directory_and_glob_keep_default_intact(tmp_path: Path, monkeypatch) -> None:
    _freeze_codex_clock(monkeypatch)
    home = tmp_path / "home"
    default_sessions = home / ".codex" / "sessions"
    second_sessions = tmp_path / "codex-2" / "sessions"
    work_sessions = tmp_path / "codex-work" / "sessions"

    _write_codex_session(default_sessions, used_pct=19.0, plan_type="plus")
    _write_codex_session(second_sessions, used_pct=64.0, plan_type="pro")
    _write_codex_session(work_sessions, used_pct=82.5, plan_type="team")

    monkeypatch.setattr("spend_app.providers.Path.home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    res2 = limits._codex_limits(second_sessions, key="codex-2")
    assert res2["status"] == "exact"
    assert res2["key"] == "codex-2"
    assert res2["name"] == "Codex \u00b7 Profile 2"
    assert res2["scope"] == codex_quota.scope(second_sessions)
    assert res2["windows"][0]["usedPct"] == 64.0
    assert res2["plan"] == "ChatGPT Pro"

    # Both POSIX and backslash `/**/*.jsonl` forms are accepted.
    glob_root = Path(f"{work_sessions.as_posix()}/**/*.jsonl")
    res_work = limits._codex_limits(glob_root, key="codex-work")
    assert res_work["status"] == "exact"
    assert res_work["key"] == "codex-work"
    assert res_work["name"] == "Codex \u00b7 Work"
    assert res_work["scope"] == codex_quota.scope(work_sessions)
    assert res_work["windows"][0]["usedPct"] == 82.5
    assert res_work["scope"] != codex_quota.scope(default_sessions)
    assert res2["scope"] != res_work["scope"]

    # Backslash glob string form as stored on Windows.
    win_glob_root = Path(str(work_sessions) + "\\**\\*.jsonl")
    res_win = limits._codex_limits(win_glob_root, key="codex-work")
    assert res_win["status"] == "exact"
    assert res_win["scope"] == codex_quota.scope(work_sessions)

    # Default profile remains unchanged.
    default_res = limits._codex_limits()
    assert default_res["key"] == "codex"
    assert default_res["name"] == "Codex"
    assert default_res["scope"] == codex_quota.scope(default_sessions)
    assert default_res["windows"][0]["usedPct"] == 19.0


def test_codex_limits_custom_root_missing_empty_and_stale(tmp_path: Path, monkeypatch) -> None:
    _freeze_codex_clock(monkeypatch)

    missing_root = tmp_path / "does-not-exist" / "sessions"
    missing_res = limits._codex_limits(missing_root, key="codex-2")
    assert missing_res["status"] == "unavailable"
    assert missing_res["key"] == "codex-2"
    assert missing_res["name"] == "Codex \u00b7 Profile 2"
    assert missing_res["windows"] == []

    # Existing root with only non-main pool data.
    alt_pool_root = tmp_path / "codex-work" / "sessions"
    _write_codex_session(alt_pool_root, used_pct=10.0, limit_id="codex_bengalfox")
    alt_res = limits._codex_limits(alt_pool_root, key="codex-work")
    assert alt_res["status"] == "unavailable"
    assert alt_res["key"] == "codex-work"
    assert alt_res["name"] == "Codex \u00b7 Work"
    assert alt_res["windows"] == []

    # Existing root with a stale (> 6h) snapshot.
    stale_root = tmp_path / "codex-3" / "sessions"
    _write_codex_session(stale_root, used_pct=45.0, age_seconds=7 * 3600)
    stale_res = limits._codex_limits(stale_root, key="codex-3")
    assert stale_res["status"] == "unavailable"
    assert stale_res["key"] == "codex-3"
    assert stale_res["name"] == "Codex \u00b7 Profile 3"
    assert stale_res["windows"] == []
    assert "six hours" in stale_res["detail"]


# ---------------------------------------------------------------------------
# 2. quotas profile-store iteration, Codex collectors, caching, cadence & nudge
# ---------------------------------------------------------------------------


def test_default_quota_collectors_iterates_enabled_store_profiles_and_skips_disabled(
    tmp_path: Path,
) -> None:
    codex2 = tmp_path / "codex-2" / "sessions"
    codex2.mkdir(parents=True, exist_ok=True)
    codex_off = tmp_path / "codex-off" / "sessions"
    claude_work = tmp_path / "claude-work"
    claude_off = tmp_path / "claude-off"
    oc_2_db = _write_opencode_db(tmp_path / "oc-2" / "opencode" / "opencode.db", "oc-key-2")
    oc_off_db = _write_opencode_db(tmp_path / "oc-off" / "opencode" / "opencode.db", "oc-key-off")

    _write_claude_state(claude_work, "uuid-work", five_pct=12, weekly_pct=34)
    _write_claude_state(claude_off, "uuid-off", five_pct=5, weekly_pct=10)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "codex_local": {
                "default": _profile_record("codex_local", "default", name="Default", location=None, enabled=True),
                "codex-2": _profile_record("codex_local", "codex-2", name="Profile 2", location=str(codex2), enabled=True),
                "codex-off": _profile_record("codex_local", "codex-off", name="Disabled", location=str(codex_off), enabled=False),
            },
            "claude_local": {
                "default": _profile_record("claude_local", "default", name="Default", location=None, enabled=True),
                "claude-code-work": _profile_record(
                    "claude_local",
                    "claude-code-work",
                    name="Work",
                    location=str(claude_work / "projects"),
                    enabled=True,
                    account_digest=_sha256("uuid-work"),
                ),
                "claude-code-off": _profile_record(
                    "claude_local",
                    "claude-code-off",
                    name="Off",
                    location=str(claude_off / "projects"),
                    enabled=False,
                    account_digest=_sha256("uuid-off"),
                ),
            },
            "opencode_local": {
                "default": _profile_record("opencode_local", "default", name="Default", location=None, enabled=True),
                "opencode-go-2": _profile_record(
                    "opencode_local",
                    "opencode-go-2",
                    name="Profile 2",
                    location=str(oc_2_db),
                    enabled=True,
                    account_digest=_sha256("oc-key-2"),
                ),
                "opencode-go-off": _profile_record(
                    "opencode_local",
                    "opencode-go-off",
                    name="Off",
                    location=str(oc_off_db),
                    enabled=False,
                    account_digest=_sha256("oc-key-off"),
                ),
            },
        },
    )

    collectors = default_quota_collectors(db_path)
    assert {"codex-2", "claude-code-work", "opencode-go-2"} <= set(collectors)
    assert "codex-off" not in collectors
    assert "claude-code-off" not in collectors
    assert "opencode-go-off" not in collectors


def test_poll_quotas_persists_second_codex_profile_alongside_default(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)
    home = _isolate_default_poll_sources(tmp_path, monkeypatch)

    default_sessions = home / ".codex" / "sessions"
    second_sessions = tmp_path / "codex-2" / "sessions"
    _write_codex_session(default_sessions, used_pct=15.0)
    _write_codex_session(second_sessions, used_pct=72.0)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "codex_local": {
                "codex-2": _profile_record(
                    "codex_local",
                    "codex-2",
                    name="Profile 2",
                    location=str(second_sessions),
                    enabled=True,
                    account_digest=_sha256("acct-second"),
                    tool_key="codex-2",
                ),
            }
        },
    )

    result = poll_quotas(db_path, now=lambda: _iso(NOW))
    assert "codex" in result["polledProviders"]
    assert "codex-2" in result["polledProviders"]

    rows = _quota_rows(db_path)
    assert ("codex", "weekly") in rows
    assert ("codex-2", "weekly") in rows
    assert rows[("codex", "weekly")]["pct"] == 15.0
    assert rows[("codex-2", "weekly")]["pct"] == 72.0
    assert rows[("codex-2", "weekly")]["source"] == "codex_local_telemetry"

    snap = {p["key"]: p for p in limits.snapshot_limits(db_path, now=NOW)["providers"]}
    assert snap["codex"]["status"] == "exact"
    assert snap["codex"]["windows"][0]["usedPct"] == 15.0
    assert snap["codex-2"]["status"] == "exact"
    assert snap["codex-2"]["windows"][0]["usedPct"] == 72.0


def test_claude_extra_profile_maps_by_folder_without_raw_uuid_in_store(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(limits, "datetime", FixedClock)

    raw_uuid = "raw-secret-claude-account-uuid"
    folder = tmp_path / "profiles" / ".claude-work"
    _write_claude_state(folder, raw_uuid, five_pct=22.0, weekly_pct=48.0, fetched=NOW)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "default": _profile_record("claude_local", "default", name="Default", location=None, enabled=True),
                "claude-code-work": _profile_record(
                    "claude_local",
                    "claude-code-work",
                    name="Work",
                    # Folder path with trailing /projects as detected by P2.
                    location=str(folder / "projects"),
                    enabled=True,
                    account_digest=_sha256(raw_uuid),
                    tool_key="claude-code-work",
                ),
            }
        },
    )

    # Confirm the store never persists the raw UUID.
    with connect(db_path) as conn:
        raw_v2 = conn.execute("SELECT value FROM app_meta WHERE key=?", (profile_store.KEY,)).fetchone()[0]
    assert raw_uuid not in raw_v2

    result = poll_quotas(db_path, now=lambda: _iso(NOW))
    assert "claude-code-work" in result["polledProviders"]

    rows = _quota_rows(db_path)
    assert rows[("claude-code-work", "5h")]["pct"] == 22.0
    assert rows[("claude-code-work", "5h")]["source"] == "claude_code_cache"
    assert rows[("claude-code-work", "weekly")]["pct"] == 48.0
    assert rows[("claude-code-work", "weekly")]["source"] == "claude_code_cache"


def test_opencode_extra_profile_maps_from_store_location(
    tmp_path: Path, monkeypatch
) -> None:
    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "1")

    profile_dir = tmp_path / ".opencode-work"
    _write_opencode_db(profile_dir / "opencode" / "opencode.db", "oc-work-console-key")

    sent_auths: list[str] = []

    def fake_get(url, *, headers=None, **_kwargs):
        sent_auths.append((headers or {}).get("Authorization", ""))
        req = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=req,
            json={
                "usage": {
                    "rolling": {"percent": 11, "resetsAt": "2026-10-26T16:00:00Z"},
                    "weekly": {"percent": 29, "resetsAt": "2026-10-30T00:00:00Z"},
                    "monthly": {"percent": 7, "resetsAt": "2026-11-20T00:00:00Z"},
                }
            },
        )

    monkeypatch.setattr(limits.httpx, "get", fake_get)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "opencode_local": {
                "default": _profile_record("opencode_local", "default", name="Default", location=None, enabled=True),
                "opencode-go-work": _profile_record(
                    "opencode_local",
                    "opencode-go-work",
                    name="Work",
                    # Pass directory location (resolved to opencode/opencode.db).
                    location=str(profile_dir),
                    enabled=True,
                    account_digest=_sha256("oc-work-console-key"),
                    tool_key="opencode-go-work",
                ),
            }
        },
    )

    result = poll_quotas(db_path, now=lambda: _iso(NOW))
    assert "opencode-go-work" in result["polledProviders"]
    assert sent_auths == ["Bearer oc-work-console-key"]

    rows = _quota_rows(db_path)
    assert rows[("opencode-go-work", "5h")]["pct"] == 11.0
    assert rows[("opencode-go-work", "weekly")]["pct"] == 29.0
    assert rows[("opencode-go-work", "monthly")]["pct"] == 7.0


def test_codex_cache_isolation_cadence_and_nudge(tmp_path: Path, monkeypatch) -> None:
    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)

    home = tmp_path / "home"
    default_sessions = home / ".codex" / "sessions"
    second_sessions = tmp_path / "codex-2" / "sessions"
    _write_codex_session(default_sessions, used_pct=14.0)
    _write_codex_session(second_sessions, used_pct=66.0)
    monkeypatch.setattr("spend_app.providers.Path.home", lambda: home)
    monkeypatch.delenv("CODEX_HOME", raising=False)

    db_path = tmp_path / "spend.db"
    initialize(db_path)

    # Cadence for codex-* matches codex cadence.
    assert QuotaLaneScheduler().lane_cadence("codex-2") == LANE_CADENCE["codex"]
    assert QuotaLaneScheduler(cadence={"codex": (120, 120)}).lane_cadence("codex-work") == (120, 120)

    # Distinct cache keys per provider_key (even if same root/db, and across roots).
    s1 = _codex_quota_collector(db_path, provider_key="codex")
    s2 = _codex_quota_collector(db_path, root=second_sessions, provider_key="codex-2")
    s3 = _codex_quota_collector(db_path, root=second_sessions, provider_key="codex-3")
    assert s1[0].provider_key == "codex" and s1[0].pct == 14.0
    assert s2[0].provider_key == "codex-2" and s2[0].pct == 66.0
    assert s3[0].provider_key == "codex-3" and s3[0].pct == 66.0

    with limits._CACHE_LOCK:
        codex_keys = [k for k in limits._CACHE if k.startswith("codex_quota_poll:")]
        limits._CACHE["claude_account_poll:claude-code-work"] = (1.0, ({"status": "exact"}, "claude_code_cache"))
        limits._CACHE["opencode_go_profile_poll:opencode-go-2"] = (1.0, {"status": "exact"})
    assert len(codex_keys) == 3
    assert any(k.startswith(f"codex_quota_poll:codex:{db_path.resolve()}:") for k in codex_keys)
    assert any(k.startswith(f"codex_quota_poll:codex-2:{db_path.resolve()}:") for k in codex_keys)
    assert any(k.startswith(f"codex_quota_poll:codex-3:{db_path.resolve()}:") for k in codex_keys)

    # Verify cache hit uses stored cache rather than re-reading disk.
    _write_codex_session(second_sessions, used_pct=99.0)
    assert _codex_quota_collector(db_path, root=second_sessions, provider_key="codex-2")[0].pct == 66.0

    # Scheduler setup under _DATABASE_LANES.
    mono = StepMonoClock(1_000.0)
    scheduler = QuotaLaneScheduler(clock=mono)
    _DATABASE_LANES[str(db_path.resolve())] = scheduler
    scheduler.record("codex", s1, active=False)
    scheduler.record("codex-2", s2, active=False)
    scheduler.record("claude-code-work", [QuotaSample("claude-code-work", "5h", "5h", "pct", "s", pct=1.0)], active=False)
    scheduler.record("opencode-go-2", [QuotaSample("opencode-go-2", "5h", "5h", "pct", "s", pct=1.0)], active=False)
    assert not scheduler.due("codex")
    assert not scheduler.due("codex-2")

    # Targeted nudge for codex-2 clears only codex-2 cache & resets only codex-2 due time.
    nudge_quota_lanes(db_path, "codex-2")
    assert scheduler.due("codex-2") is True
    assert scheduler.due("codex") is False
    with limits._CACHE_LOCK:
        assert not any(k.startswith("codex_quota_poll:codex-2:") for k in limits._CACHE)
        assert any(k.startswith("codex_quota_poll:codex:") for k in limits._CACHE)
        assert any(k.startswith("codex_quota_poll:codex-3:") for k in limits._CACHE)
        assert "claude_account_poll:claude-code-work" in limits._CACHE
        assert "opencode_go_profile_poll:opencode-go-2" in limits._CACHE

    # After nudge, codex-2 reads the updated 99.0 snapshot on disk.
    assert _codex_quota_collector(db_path, root=second_sessions, provider_key="codex-2")[0].pct == 99.0

    # Targeted nudge for claude & opencode extra lanes.
    nudge_quota_lanes(db_path, "claude-code-work")
    with limits._CACHE_LOCK:
        assert "claude_account_poll:claude-code-work" not in limits._CACHE
        assert "opencode_go_profile_poll:opencode-go-2" in limits._CACHE

    nudge_quota_lanes(db_path, "opencode-go-2")
    with limits._CACHE_LOCK:
        assert "opencode_go_profile_poll:opencode-go-2" not in limits._CACHE

    # Re-seed caches and test provider_key=None global nudge.
    scheduler.record("codex", s1, active=False)
    scheduler.record("codex-2", s2, active=False)
    with limits._CACHE_LOCK:
        limits._CACHE["claude_account_poll:claude-code-work"] = (1.0, ({"status": "exact"}, "claude_code_cache"))
        limits._CACHE["opencode_go_profile_poll:opencode-go-2"] = (1.0, {"status": "exact"})

    nudge_quota_lanes(db_path, None)
    assert scheduler.due("codex") is True
    assert scheduler.due("codex-2") is True
    with limits._CACHE_LOCK:
        assert not any(
            k.startswith(("codex_quota_poll:", "claude_account_poll:", "opencode_go_profile_poll:"))
            for k in limits._CACHE
        )


# ---------------------------------------------------------------------------
# 3. Account digest sharing
# ---------------------------------------------------------------------------


def test_default_and_extra_sharing_account_digest_produces_single_lane(tmp_path: Path) -> None:
    digest = _sha256("shared-account-id")

    claude_extra = tmp_path / ".claude-work"
    _write_claude_state(claude_extra, "shared-account-id", five_pct=10, weekly_pct=20)
    codex_extra = tmp_path / ".codex-work" / "sessions"
    _write_codex_session(codex_extra, used_pct=33.0)
    oc_extra = _write_opencode_db(tmp_path / ".opencode-work" / "opencode" / "opencode.db", "shared-account-id")

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "default": _profile_record(
                    "claude_local", "default", name="Personal", location=None, enabled=True, account_digest=digest
                ),
                "claude-code-work": _profile_record(
                    "claude_local",
                    "claude-code-work",
                    name="Work",
                    location=str(claude_extra / "projects"),
                    enabled=True,
                    account_digest=digest,
                ),
            },
            "codex_local": {
                "default": _profile_record(
                    "codex_local", "default", name="Default", location=None, enabled=True, account_digest=digest
                ),
                "codex-work": _profile_record(
                    "codex_local",
                    "codex-work",
                    name="Work",
                    location=str(codex_extra),
                    enabled=True,
                    account_digest=digest,
                ),
            },
            "opencode_local": {
                "default": _profile_record(
                    "opencode_local", "default", name="Default", location=None, enabled=True, account_digest=digest
                ),
                "opencode-go-work": _profile_record(
                    "opencode_local",
                    "opencode-go-work",
                    name="Work",
                    location=str(oc_extra),
                    enabled=True,
                    account_digest=digest,
                ),
            },
        },
    )

    collectors = default_quota_collectors(db_path)
    assert "claude-code" in collectors and "claude-code-work" not in collectors
    assert "codex" in collectors and "codex-work" not in collectors
    assert "opencode-go" in collectors and "opencode-go-work" not in collectors

    shared = shared_limit_lanes(db_path)
    assert shared == {
        "claude-code-work": {
            "ownerKey": "claude-code",
            "ownerName": "Personal",
            "label": "Shares limits with Personal",
        },
        "codex-work": {
            "ownerKey": "codex",
            "ownerName": "Default",
            "label": "Shares limits with Default",
        },
        "opencode-go-work": {
            "ownerKey": "opencode-go",
            "ownerName": "Default",
            "label": "Shares limits with Default",
        },
    }

    claude_meta = claude_accounts.provider_meta("claude-code-work", database_path=db_path)
    assert claude_meta is not None
    assert claude_meta["sharesLimitsWith"] == "claude-code"
    assert claude_meta["sharesLimitsLabel"] == "Shares limits with Personal"

    oc_meta = opencode_profiles.provider_meta("opencode-go-work", database_path=db_path)
    assert oc_meta is not None
    assert oc_meta["sharesLimitsWith"] == "opencode-go"
    assert oc_meta["sharesLimitsLabel"] == "Shares limits with Default"


def test_two_extra_profiles_sharing_digest_without_default_match(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)
    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "1")

    default_digest = _sha256("default-acct")
    shared_extra_digest = _sha256("shared-extra-acct")

    claude_1 = tmp_path / ".claude-team-a"
    claude_2 = tmp_path / ".claude-team-b"
    _write_claude_state(claude_1, "shared-extra-acct", five_pct=18, weekly_pct=42, fetched=NOW)
    _write_claude_state(claude_2, "shared-extra-acct", five_pct=99, weekly_pct=99, fetched=NOW)

    codex_1 = tmp_path / ".codex-team-a" / "sessions"
    codex_2 = tmp_path / ".codex-team-b" / "sessions"
    _write_codex_session(codex_1, used_pct=31.0)
    _write_codex_session(codex_2, used_pct=88.0)

    oc_1 = _write_opencode_db(tmp_path / ".opencode-team-a" / "opencode.db", "extra-oc-key")
    oc_2 = _write_opencode_db(tmp_path / ".opencode-team-b" / "opencode.db", "extra-oc-key")

    def fake_get(url, **_kwargs):
        req = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=req,
            json={
                "usage": {
                    "rolling": {"percent": 5, "resetsAt": "2026-10-26T16:00:00Z"},
                    "weekly": {"percent": 15, "resetsAt": "2026-10-30T00:00:00Z"},
                    "monthly": {"percent": 25, "resetsAt": "2026-11-20T00:00:00Z"},
                }
            },
        )

    monkeypatch.setattr(limits.httpx, "get", fake_get)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "default": _profile_record(
                    "claude_local", "default", name="Default", location=None, enabled=True, account_digest=default_digest
                ),
                "claude-code-team-a": _profile_record(
                    "claude_local",
                    "claude-code-team-a",
                    name="Team Alpha",
                    location=str(claude_1),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
                "claude-code-team-b": _profile_record(
                    "claude_local",
                    "claude-code-team-b",
                    name="Team Beta",
                    location=str(claude_2),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
            },
            "codex_local": {
                "default": _profile_record(
                    "codex_local", "default", name="Default", location=None, enabled=True, account_digest=default_digest
                ),
                "codex-team-a": _profile_record(
                    "codex_local",
                    "codex-team-a",
                    name="Codex Alpha",
                    location=str(codex_1),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
                "codex-team-b": _profile_record(
                    "codex_local",
                    "codex-team-b",
                    name="Codex Beta",
                    location=str(codex_2),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
            },
            "opencode_local": {
                "default": _profile_record(
                    "opencode_local", "default", name="Default", location=None, enabled=True, account_digest=default_digest
                ),
                "opencode-go-team-a": _profile_record(
                    "opencode_local",
                    "opencode-go-team-a",
                    name="Go Alpha",
                    location=str(oc_1),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
                "opencode-go-team-b": _profile_record(
                    "opencode_local",
                    "opencode-go-team-b",
                    name="Go Beta",
                    location=str(oc_2),
                    enabled=True,
                    account_digest=shared_extra_digest,
                ),
            },
        },
    )

    collectors = default_quota_collectors(db_path)
    assert "claude-code-team-a" in collectors and "claude-code-team-b" not in collectors
    assert "codex-team-a" in collectors and "codex-team-b" not in collectors
    assert "opencode-go-team-a" in collectors and "opencode-go-team-b" not in collectors

    shared = shared_limit_lanes(db_path)
    assert shared == {
        "claude-code-team-b": {
            "ownerKey": "claude-code-team-a",
            "ownerName": "Team Alpha",
            "label": "Shares limits with Team Alpha",
        },
        "codex-team-b": {
            "ownerKey": "codex-team-a",
            "ownerName": "Codex Alpha",
            "label": "Shares limits with Codex Alpha",
        },
        "opencode-go-team-b": {
            "ownerKey": "opencode-go-team-a",
            "ownerName": "Go Alpha",
            "label": "Shares limits with Go Alpha",
        },
    }

    assert "sharesLimitsWith" not in claude_accounts.provider_meta("claude-code-team-a", database_path=db_path)
    claude_b_meta = claude_accounts.provider_meta("claude-code-team-b", database_path=db_path)
    assert claude_b_meta["sharesLimitsWith"] == "claude-code-team-a"
    assert claude_b_meta["sharesLimitsLabel"] == "Shares limits with Team Alpha"

    assert "sharesLimitsWith" not in opencode_profiles.provider_meta("opencode-go-team-a", database_path=db_path)
    oc_b_meta = opencode_profiles.provider_meta("opencode-go-team-b", database_path=db_path)
    assert oc_b_meta["sharesLimitsWith"] == "opencode-go-team-a"
    assert oc_b_meta["sharesLimitsLabel"] == "Shares limits with Go Alpha"

    poll_quotas(db_path, now=lambda: _iso(NOW))
    rows = _quota_rows(db_path)
    persisted_providers = {prov for prov, _ in rows}
    assert {"claude-code-team-a", "codex-team-a", "opencode-go-team-a"} <= persisted_providers
    assert "claude-code-team-b" not in persisted_providers
    assert "codex-team-b" not in persisted_providers
    assert "opencode-go-team-b" not in persisted_providers
    assert rows[("claude-code-team-a", "5h")]["pct"] == 18.0
    assert rows[("codex-team-a", "weekly")]["pct"] == 31.0
    assert rows[("opencode-go-team-a", "5h")]["pct"] == 5.0


# ---------------------------------------------------------------------------
# 4. Profiles without sign-in or limit data & provider_meta / is_*_key
# ---------------------------------------------------------------------------


def test_profiles_without_signin_or_limit_data_produce_no_lane(
    tmp_path: Path, monkeypatch
) -> None:
    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)
    _isolate_default_poll_sources(tmp_path, monkeypatch)

    # Claude 1: not signed in (no account_digest and no accountUuid in .claude.json).
    claude_unsigned = tmp_path / ".claude-unsigned"
    _write_claude_state(claude_unsigned, None)
    # Claude 2: signed in, but no cachedUsageUtilization.
    claude_nocache = tmp_path / ".claude-nocache"
    _write_claude_state(claude_nocache, "uuid-nocache")
    # Codex extra 1: missing directory -> skipped (no lane).
    codex_missing = tmp_path / ".codex-missing" / "sessions"
    # Codex extra 2: existing empty directory -> lane returns [] samples.
    codex_empty = tmp_path / ".codex-empty" / "sessions"
    codex_empty.mkdir(parents=True, exist_ok=True)
    # OpenCode extra 1: db present with no Go keys and no digest -> skipped in _resolve_profile_lanes.
    oc_empty = _write_opencode_db(tmp_path / ".opencode-empty" / "opencode.db", None)
    # OpenCode extra 2: digest present, db missing or no Go keys / Go quota disabled -> returns [] samples.
    oc_nokeys = _write_opencode_db(tmp_path / ".opencode-nokeys" / "opencode.db", None)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "claude-code-unsigned": _profile_record(
                    "claude_local",
                    "claude-code-unsigned",
                    name="Unsigned",
                    location=str(claude_unsigned),
                    enabled=True,
                    account_digest="",
                ),
                "claude-code-nocache": _profile_record(
                    "claude_local",
                    "claude-code-nocache",
                    name="No Cache",
                    location=str(claude_nocache),
                    enabled=True,
                    account_digest=_sha256("uuid-nocache"),
                ),
            },
            "codex_local": {
                "codex-missing": _profile_record(
                    "codex_local",
                    "codex-missing",
                    name="Missing Codex",
                    location=str(codex_missing),
                    enabled=True,
                    account_digest=_sha256("codex-missing-acct"),
                ),
                "codex-empty": _profile_record(
                    "codex_local",
                    "codex-empty",
                    name="Empty Codex",
                    location=str(codex_empty),
                    enabled=True,
                    account_digest=_sha256("codex-empty-acct"),
                ),
            },
            "opencode_local": {
                "opencode-go-empty": _profile_record(
                    "opencode_local",
                    "opencode-go-empty",
                    name="Empty OC",
                    location=str(oc_empty),
                    enabled=True,
                    account_digest="",
                ),
                "opencode-go-nokeys": _profile_record(
                    "opencode_local",
                    "opencode-go-nokeys",
                    name="No Go Keys",
                    location=str(oc_nokeys),
                    enabled=True,
                    account_digest=_sha256("oc-no-keys-acct"),
                ),
            },
        },
    )

    collectors = default_quota_collectors(db_path)
    assert "claude-code-unsigned" not in collectors
    assert "opencode-go-empty" not in collectors
    assert "codex-missing" not in collectors
    assert collectors["claude-code-nocache"]() == []
    assert collectors["codex-empty"]() == []
    assert collectors["opencode-go-nokeys"]() == []

    # Default codex retains its existing unavailable behavior while extra codex does not.
    assert codex_quota_samples({"status": "unavailable", "windows": []}, source="codex_local_telemetry", provider_key="codex")[0].unit == "unavailable"
    assert codex_quota_samples({"status": "unavailable", "windows": []}, source="codex_local_telemetry", provider_key="codex-missing") == []

    poll_quotas(db_path, now=lambda: _iso(NOW))
    persisted_providers = {prov for prov, _ in _quota_rows(db_path)}
    assert "codex" in persisted_providers  # default codex still writes unavailable row
    for extra in (
        "claude-code-unsigned",
        "claude-code-nocache",
        "codex-missing",
        "codex-empty",
        "opencode-go-empty",
        "opencode-go-nokeys",
    ):
        assert extra not in persisted_providers


def test_custom_non_prefix_store_keys_and_provider_meta(tmp_path: Path, monkeypatch) -> None:
    claude_dir = tmp_path / "studio-claude"
    _write_claude_state(claude_dir, "studio-claude-uuid", tier="default_claude_pro")
    claude_alt_dir = tmp_path / "studio-claude-alt"
    _write_claude_state(claude_alt_dir, "studio-claude-uuid", tier="default_claude_pro")

    oc_db = _write_opencode_db(tmp_path / "studio-oc" / "opencode.db", "studio-oc-key")
    oc_alt_db = _write_opencode_db(tmp_path / "studio-oc-alt" / "opencode.db", "studio-oc-key")

    codex_dir = tmp_path / "studio-codex" / "sessions"
    _write_codex_session(codex_dir, used_pct=21.0)

    shared_claude = _sha256("studio-claude-uuid")
    shared_oc = _sha256("studio-oc-key")

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "studio-claude": _profile_record(
                    "claude_local",
                    "studio-claude",
                    name="Studio Claude",
                    location=str(claude_dir),
                    enabled=True,
                    account_digest=shared_claude,
                    tool_key="studio-claude",
                ),
                "studio-claude-alt": _profile_record(
                    "claude_local",
                    "studio-claude-alt",
                    name="Studio Claude Alt",
                    location=str(claude_alt_dir),
                    enabled=True,
                    account_digest=shared_claude,
                    tool_key="studio-claude-alt",
                ),
            },
            "opencode_local": {
                "studio-oc": _profile_record(
                    "opencode_local",
                    "studio-oc",
                    name="Studio Go",
                    location=str(oc_db),
                    enabled=True,
                    account_digest=shared_oc,
                    tool_key="studio-oc",
                ),
                "studio-oc-alt": _profile_record(
                    "opencode_local",
                    "studio-oc-alt",
                    name="Studio Go Alt",
                    location=str(oc_alt_db),
                    enabled=True,
                    account_digest=shared_oc,
                    tool_key="studio-oc-alt",
                ),
            },
            "codex_local": {
                "studio-codex": _profile_record(
                    "codex_local",
                    "studio-codex",
                    name="Studio Codex",
                    location=str(codex_dir),
                    enabled=True,
                    account_digest=_sha256("studio-codex-acct"),
                    tool_key="studio-codex",
                ),
            },
        },
    )

    assert claude_accounts.is_account_key("studio-claude", database_path=db_path) is True
    assert claude_accounts.is_account_key("studio-claude") is False
    c_meta = claude_accounts.provider_meta("studio-claude", database_path=db_path)
    assert c_meta == {
        "providerKey": "studio-claude",
        "providerName": "Claude Code \u00b7 Studio Claude",
        "shortName": "Claude Studio Claude",
        "accountLabel": "Studio Claude",
        "plan": "Claude Pro",
        "isPayg": False,
        "toolKey": "claude-code",
    }
    c_alt_meta = claude_accounts.provider_meta("studio-claude-alt", database_path=db_path)
    assert c_alt_meta["sharesLimitsWith"] == "studio-claude"
    assert c_alt_meta["sharesLimitsLabel"] == "Shares limits with Studio Claude"

    assert opencode_profiles.is_profile_key("studio-oc", database_path=db_path) is True
    assert opencode_profiles.is_profile_key("studio-oc") is False
    o_meta = opencode_profiles.provider_meta("studio-oc", database_path=db_path)
    assert o_meta == {
        "providerKey": "studio-oc",
        "providerName": "OpenCode Go \u00b7 Studio Go",
        "shortName": "OpenCode Go Studio Go",
        "accountLabel": "Studio Go",
        "plan": "Go",
        "isPayg": False,
        "toolKey": "opencode-go",
    }
    o_alt_meta = opencode_profiles.provider_meta("studio-oc-alt", database_path=db_path)
    assert o_alt_meta["sharesLimitsWith"] == "studio-oc"
    assert o_alt_meta["sharesLimitsLabel"] == "Shares limits with Studio Go"

    assert _is_codex_profile_key("studio-codex", database_path=db_path) is True
    assert _is_codex_profile_key("studio-codex") is False

    # With SPEND_DATABASE_PATH pointing to db_path, order & cadence helpers resolve custom keys too.
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(db_path))
    assert _limit_order("studio-claude") == ("5h", "weekly")
    assert _limit_order("studio-oc") == ("5h", "weekly", "monthly")
    scheduler = QuotaLaneScheduler()
    assert scheduler.lane_cadence("studio-claude") == LANE_CADENCE["claude-code"]
    assert scheduler.lane_cadence("studio-oc") == LANE_CADENCE["opencode-go"]
    assert scheduler.lane_cadence("studio-codex") == LANE_CADENCE["codex"]


def test_lane_is_active_for_store_backed_keys(tmp_path: Path) -> None:
    claude_dir = tmp_path / "work-claude"
    claude_state = _write_claude_state(claude_dir, "work-claude-uuid")
    oc_db = _write_opencode_db(tmp_path / "work-oc" / "opencode.db", "work-oc-key")
    codex_dir = tmp_path / "work-codex" / "sessions"
    codex_dir.mkdir(parents=True, exist_ok=True)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "custom-claude": _profile_record(
                    "claude_local",
                    "custom-claude",
                    name="Custom Claude",
                    location=str(claude_dir / "projects"),
                    enabled=True,
                    account_digest=_sha256("work-claude-uuid"),
                    tool_key="custom-claude",
                ),
            },
            "opencode_local": {
                "custom-oc": _profile_record(
                    "opencode_local",
                    "custom-oc",
                    name="Custom OC",
                    location=str(oc_db),
                    enabled=True,
                    account_digest=_sha256("work-oc-key"),
                    tool_key="custom-oc",
                ),
            },
            "codex_local": {
                "codex-2": _profile_record(
                    "codex_local",
                    "codex-2",
                    name="Profile 2",
                    location=str(codex_dir),
                    enabled=True,
                    account_digest=_sha256("codex-2-uuid"),
                    tool_key="codex-2",
                ),
            },
        },
    )

    old_ts = (NOW - timedelta(hours=1)).timestamp()
    fresh_ts = (NOW - timedelta(seconds=30)).timestamp()

    # 1) Recent file mtime makes Claude & OpenCode store-backed lanes active.
    os.utime(claude_state, (fresh_ts, fresh_ts))
    os.utime(oc_db, (fresh_ts, fresh_ts))
    assert lane_is_active(db_path, "custom-claude", now=NOW) is True
    assert lane_is_active(db_path, "custom-oc", now=NOW) is True
    assert lane_is_active(db_path, "codex-2", now=NOW) is False

    # 2) Stale file mtime falls back to usage_events in the database.
    os.utime(claude_state, (old_ts, old_ts))
    os.utime(oc_db, (old_ts, old_ts))
    assert lane_is_active(db_path, "custom-claude", now=NOW) is False
    assert lane_is_active(db_path, "custom-oc", now=NOW) is False

    recent_iso = _iso(NOW - timedelta(minutes=1))
    with connect(db_path) as conn:
        for idx, tool_key in enumerate(("custom-claude", "custom-oc", "codex-2"), start=1):
            upsert_usage_event(
                conn,
                UsageEvent(
                    source="local",
                    tool_key=tool_key,
                    model_key="claude-sonnet-4-6" if "claude" in tool_key else "gpt-5.6",
                    occurred_at=recent_iso,
                    session_id=f"sess-{idx}",
                    project="proj",
                    input_tokens=10,
                    cached_input_tokens=0,
                    cache_write_tokens=0,
                    cache_write_1h_tokens=0,
                    output_tokens=5,
                    reasoning_tokens=None,
                    cost_usd=None,
                    computed_cost_usd=0.0,
                    raw_id=f"raw-{idx}",
                    ingested_at=recent_iso,
                ),
            )

    assert lane_is_active(db_path, "custom-claude", now=NOW) is True
    assert lane_is_active(db_path, "custom-oc", now=NOW) is True
    assert lane_is_active(db_path, "codex-2", now=NOW) is True


def test_missing_database_returns_no_discovered_extra_lanes(tmp_path: Path, monkeypatch) -> None:
    """When the DB does not exist, _enabled_store_profiles returns [] without auto-discovering extras."""
    extra_claude = claude_accounts.ClaudeAccount(
        key="claude-code-extra",
        label="Extra",
        config_dir=tmp_path / ".claude-extra",
        state_path=tmp_path / ".claude-extra" / ".claude.json",
        account_uuid="extra-uuid",
        plan="Claude Max",
    )
    extra_oc = opencode_profiles.OpenCodeProfile(
        key="opencode-go-extra",
        label="Extra OC",
        data_dir=tmp_path / ".opencode-extra",
        database=tmp_path / ".opencode-extra" / "opencode" / "opencode.db",
    )
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: (extra_claude,))
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: (extra_oc,))

    missing_db = tmp_path / "missing-spend.db"
    assert not missing_db.exists()
    collectors = default_quota_collectors(missing_db)
    assert "claude-code-extra" not in collectors
    assert "opencode-go-extra" not in collectors


def test_codex_read_rows_respects_default_vs_custom_or_disabled_binding(
    tmp_path: Path, monkeypatch
) -> None:
    from spend_app import connections
    from spend_app.providers import REGISTRY

    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)
    home = _isolate_default_poll_sources(tmp_path, monkeypatch)

    default_sessions = home / ".codex" / "sessions"
    _write_codex_session(default_sessions, used_pct=37.0)
    default_loc = connections.default_location(REGISTRY.get("codex_local"), None)

    # 1) Persisted default binding pointing at the default location keeps the codex lane.
    db_default = _seed_profiles_db(
        tmp_path / "default-loc.db",
        {
            "codex_local": {
                "default": _profile_record(
                    "codex_local",
                    "default",
                    name="Default",
                    location=default_loc,
                    enabled=True,
                )
            }
        },
    )
    poll_quotas(db_default, now=lambda: _iso(NOW))
    with connect(db_default) as conn:
        rows = {(r["provider_key"], r["limit_key"]): r for r in codex_quota.read_rows(conn, now=NOW)}
    assert rows[("codex", "weekly")]["pct"] == 37.0
    assert rows[("codex", "weekly")]["unit"] == "pct"
    snap = {p["key"]: p for p in limits.snapshot_limits(db_default, now=NOW)["providers"]}
    assert snap["codex"]["status"] == "exact"
    assert snap["codex"]["windows"][0]["usedPct"] == 37.0

    # 2) Persisted default binding at a custom location suppresses the codex lane.
    custom_loc = str(tmp_path / "custom-codex" / "sessions")
    db_custom = _seed_profiles_db(
        tmp_path / "custom-loc.db",
        {
            "codex_local": {
                "default": _profile_record(
                    "codex_local",
                    "default",
                    name="Default",
                    location=custom_loc,
                    enabled=True,
                )
            }
        },
    )
    with connect(db_custom) as conn:
        sample = QuotaSample(
            provider_key="codex",
            limit_key="weekly",
            label="Codex weekly window",
            unit="pct",
            source="codex_local_telemetry",
            pct=37.0,
            resets_at=_iso(NOW + timedelta(days=4)),
            observed_at=_iso(NOW),
            pool_id="codex",
            scope=codex_quota.current_scope(),
        )
        codex_quota.save_metadata(conn, sample, _iso(NOW))
        conn.execute(
            "INSERT INTO quotas(provider_key, limit_key, label, unit, pct, resets_at, source, polled_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("codex", "weekly", sample.label, "pct", 37.0, sample.resets_at, sample.source, _iso(NOW)),
        )
    with connect(db_custom) as conn:
        custom_rows = {(r["provider_key"], r["limit_key"]): r for r in codex_quota.read_rows(conn, now=NOW)}
    assert custom_rows[("codex", "weekly")]["pct"] is None
    assert custom_rows[("codex", "weekly")]["unit"] == "unavailable"
    assert "usage only" in custom_rows[("codex", "weekly")]["label"]

    # 3) Persisted disabled default binding suppresses the codex lane.
    db_disabled = _seed_profiles_db(
        tmp_path / "disabled.db",
        {
            "codex_local": {
                "default": _profile_record(
                    "codex_local",
                    "default",
                    name="Default",
                    location=default_loc,
                    enabled=False,
                )
            }
        },
    )
    with connect(db_disabled) as conn:
        codex_quota.save_metadata(conn, sample, _iso(NOW))
        conn.execute(
            "INSERT INTO quotas(provider_key, limit_key, label, unit, pct, resets_at, source, polled_at) "
            "VALUES(?,?,?,?,?,?,?,?)",
            ("codex", "weekly", sample.label, "pct", 37.0, sample.resets_at, sample.source, _iso(NOW)),
        )
    with connect(db_disabled) as conn:
        disabled_rows = {(r["provider_key"], r["limit_key"]): r for r in codex_quota.read_rows(conn, now=NOW)}
    assert disabled_rows[("codex", "weekly")]["pct"] is None
    assert disabled_rows[("codex", "weekly")]["unit"] == "unavailable"
    assert "disabled" in disabled_rows[("codex", "weekly")]["label"]


def test_extra_codex_quota_expires_at_read_time(tmp_path: Path) -> None:
    """Ported review probe: expired extra Codex quota is masked at read time even when later poll returns []."""
    work_sessions = tmp_path / "codex-work" / "sessions"
    work_sessions.mkdir(parents=True, exist_ok=True)
    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "codex_local": {
                "codex-work": _profile_record(
                    "codex_local",
                    "codex-work",
                    name="Work",
                    location=str(work_sessions),
                    enabled=True,
                    tool_key="codex-work",
                ),
            }
        },
    )
    sample = QuotaSample(
        provider_key="codex-work",
        limit_key="weekly",
        label="Codex weekly window",
        unit="pct",
        source="codex_local_telemetry",
        pct=42.0,
        resets_at=_iso(NOW + timedelta(hours=2)),
        observed_at=_iso(NOW),
        pool_id="codex",
        scope=codex_quota.scope(work_sessions),
    )
    poll_quotas(db_path, collectors={"codex-work": lambda: [sample]}, now=lambda: _iso(NOW))

    with connect(db_path) as conn:
        fresh_rows = codex_quota.read_rows(conn, now=NOW)
    fresh_work = next(r for r in fresh_rows if r["provider_key"] == "codex-work")
    assert fresh_work["pct"] == 42.0

    # Later poll after reset passes returns no fresh sample (extra lanes return []).
    poll_quotas(db_path, collectors={"codex-work": lambda: []}, now=lambda: _iso(NOW + timedelta(days=1)))
    with connect(db_path) as conn:
        expired_rows = codex_quota.read_rows(conn, now=NOW + timedelta(days=1))
    work = next(r for r in expired_rows if r["provider_key"] == "codex-work")
    assert work["pct"] is None
    assert work["unit"] == "unavailable"
    assert work["resets_at"] is None
    assert "expired" in work["label"]

    snap = {p["key"]: p for p in limits.snapshot_limits(db_path, now=NOW + timedelta(days=1))["providers"]}
    assert snap["codex-work"]["status"] == "unavailable"
    assert snap["codex-work"]["windows"] == []


def test_extra_codex_quota_validates_stale_root_disabled_and_removed(
    tmp_path: Path, monkeypatch
) -> None:
    from spend_app.aggregate import _capacity_from_rows, _load_quota_rows

    _freeze_codex_clock(monkeypatch)
    monkeypatch.setattr("spend_app.quotas.datetime", FixedClock)
    home = _isolate_default_poll_sources(tmp_path, monkeypatch)

    default_sessions = home / ".codex" / "sessions"
    work_sessions = tmp_path / "codex-work" / "sessions"
    work_moved = tmp_path / "codex-work-moved" / "sessions"
    studio_sessions = tmp_path / "studio-codex" / "sessions"
    _write_codex_session(default_sessions, used_pct=15.0)
    _write_codex_session(work_sessions, used_pct=42.0)
    _write_codex_session(studio_sessions, used_pct=68.0)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "codex_local": {
                "codex-work": _profile_record(
                    "codex_local",
                    "codex-work",
                    name="Work",
                    location=str(work_sessions),
                    enabled=True,
                    account_digest=_sha256("acct-work"),
                    tool_key="codex-work",
                ),
                "studio": _profile_record(
                    "codex_local",
                    "studio",
                    name="Studio",
                    location=str(studio_sessions),
                    enabled=True,
                    account_digest=_sha256("acct-studio"),
                    tool_key="studio-codex",
                ),
            }
        },
    )

    poll_quotas(db_path, now=lambda: _iso(NOW))

    with connect(db_path) as conn:
        meta_keys = {
            row[0]: json.loads(row[1])
            for row in conn.execute(
                "SELECT key, value FROM app_meta WHERE key LIKE ?",
                (f"{codex_quota.KEY}%",),
            )
        }
        assert codex_quota.KEY in meta_keys
        assert f"{codex_quota.KEY}:codex-work" in meta_keys
        assert f"{codex_quota.KEY}:studio-codex" in meta_keys
        assert meta_keys[codex_quota.KEY]["scope"] == codex_quota.scope(default_sessions)
        assert meta_keys[f"{codex_quota.KEY}:codex-work"]["scope"] == codex_quota.scope(work_sessions)
        assert meta_keys[f"{codex_quota.KEY}:studio-codex"]["scope"] == codex_quota.scope(studio_sessions)

        # Fresh read keeps all three lanes exact.
        fresh = {r["provider_key"]: r for r in codex_quota.read_rows(conn, now=NOW)}
        assert fresh["codex"]["pct"] == 15.0
        assert fresh["codex-work"]["pct"] == 42.0
        assert fresh["studio-codex"]["pct"] == 68.0

        # 1) Stale (>6h) masks extra Codex rows even before reset passes.
        stale = {r["provider_key"]: r for r in codex_quota.read_rows(conn, now=NOW + timedelta(hours=7))}
        assert stale["codex"]["pct"] is None
        assert stale["codex-work"]["pct"] is None
        assert stale["codex-work"]["unit"] == "unavailable"
        assert "six hours" in stale["codex-work"]["label"]
        assert stale["studio-codex"]["pct"] is None
        assert "six hours" in stale["studio-codex"]["label"]

        cap_rows = _load_quota_rows(conn, now=NOW + timedelta(hours=7))
        cap_cards = {c["providerKey"]: c for c in _capacity_from_rows(cap_rows, now=NOW + timedelta(hours=7), month_to_date={})}
        assert cap_cards["codex-work"]["primaryPct"] is None

        # 2) Root change on codex-work hides codex-work only; codex & studio-codex remain exact.
        state = profile_store.load(conn)
        state["profiles"]["codex_local"]["codex-work"]["location"] = str(work_moved)
        conn.execute("UPDATE app_meta SET value=? WHERE key=?", (profile_store.encode(state), profile_store.KEY))
        root_changed = {r["provider_key"]: r for r in codex_quota.read_rows(conn, now=NOW)}
        assert root_changed["codex"]["pct"] == 15.0
        assert root_changed["studio-codex"]["pct"] == 68.0
        assert root_changed["codex-work"]["pct"] is None
        assert root_changed["codex-work"]["unit"] == "unavailable"
        assert "unverified" in root_changed["codex-work"]["label"]

        # 3) Disabling codex-work masks it.
        state["profiles"]["codex_local"]["codex-work"]["location"] = str(work_sessions)
        state["profiles"]["codex_local"]["codex-work"]["enabled"] = False
        conn.execute("UPDATE app_meta SET value=? WHERE key=?", (profile_store.encode(state), profile_store.KEY))
        disabled = {r["provider_key"]: r for r in codex_quota.read_rows(conn, now=NOW)}
        assert disabled["codex"]["pct"] == 15.0
        assert disabled["studio-codex"]["pct"] == 68.0
        assert disabled["codex-work"]["pct"] is None
        assert disabled["codex-work"]["unit"] == "unavailable"
        assert "disabled" in disabled["codex-work"]["label"]

        # 4) Removing codex-work and studio-codex masks both while default codex remains exact.
        state["profiles"]["codex_local"].pop("codex-work")
        state["profiles"]["codex_local"].pop("studio")
        conn.execute("UPDATE app_meta SET value=? WHERE key=?", (profile_store.encode(state), profile_store.KEY))
        removed = {r["provider_key"]: r for r in codex_quota.read_rows(conn, now=NOW)}
        assert removed["codex"]["pct"] == 15.0
        assert removed["codex-work"]["pct"] is None
        assert removed["codex-work"]["unit"] == "unavailable"
        assert "removed" in removed["codex-work"]["label"]
        assert removed["studio-codex"]["pct"] is None
        assert removed["studio-codex"]["unit"] == "unavailable"
        assert "removed" in removed["studio-codex"]["label"]


# ---------------------------------------------------------------------------
# 6. Retargeted root guards (R1)
# ---------------------------------------------------------------------------


def _create_junction(target: Path, link: Path) -> None:
    try:
        import _winapi

        _winapi.CreateJunction(str(target), str(link))
    except (ImportError, AttributeError, NotImplementedError, OSError) as exc:
        pytest.skip(f"Windows junction creation not permitted: {exc}")


class _FakeVault:
    def available(self) -> bool:
        return True

    def get(self, _ref: str) -> str | None:
        return None


def test_location_unchanged_helper(tmp_path: Path, monkeypatch) -> None:
    from spend_app.providers import CONNECTION_SPECS

    codex_dir = tmp_path / ".codex-work" / "sessions"
    codex_dir.mkdir(parents=True)
    target_dir = tmp_path / "unapproved-codex" / "sessions"
    target_dir.mkdir(parents=True)
    oc_db = _write_opencode_db(tmp_path / ".opencode-work" / "opencode" / "opencode.db", "k1")

    assert profile_store.location_unchanged(
        profile_store.Profile("codex_local", "codex-work", "Work", str(codex_dir), "manual", True, 1, None, "codex-work"),
        CONNECTION_SPECS["codex_local"],
    ) is True
    assert profile_store.location_unchanged(
        profile_store.Profile("opencode_local", "opencode-go-work", "Work", str(oc_db), "manual", True, 1, None, "opencode-go-work"),
        CONNECTION_SPECS["opencode_local"],
    ) is True

    # Empty, missing, relative, wildcard, or missing spec all return False.
    for bad in (None, "", "relative/path", str(tmp_path / "missing"), str(codex_dir / "**/*.jsonl")):
        assert profile_store.location_unchanged(
            profile_store.Profile("codex_local", "codex-work", "Work", bad, "manual", True, 1, None, "codex-work"),
            CONNECTION_SPECS["codex_local"],
        ) is False
    assert profile_store.location_unchanged(
        profile_store.Profile("codex_local", "codex-work", "Work", str(codex_dir), "manual", True, 1, None, "codex-work"),
        None,
    ) is False

    # ~ home path matches expanded identity.
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))
    assert profile_store.location_unchanged(
        profile_store.Profile("codex_local", "codex-work", "Work", "~/.codex-work/sessions", "manual", True, 1, None, "codex-work"),
        CONNECTION_SPECS["codex_local"],
    ) is True

    # Windows junction retargeting is detected as changed.
    chosen = tmp_path / ".codex-junc"
    chosen.mkdir()
    chosen.rename(tmp_path / ".codex-junc-orig")
    _create_junction(target_dir.parent, chosen)
    assert profile_store.location_unchanged(
        profile_store.Profile("codex_local", "codex-junc", "Junc", str(chosen), "manual", True, 1, None, "codex-junc"),
        CONNECTION_SPECS["codex_local"],
    ) is False


def test_quota_reader_rejects_retargeted_extra_root(tmp_path: Path, monkeypatch) -> None:
    """Ported R1 probe: retargeting a Codex profile root via junction yields no lane, no sample, and masks stored rows."""
    from uuid import uuid4
    from spend_app import connections, quotas
    from spend_app.config import Settings

    hermetic = tmp_path / "hermetic"
    hermetic.mkdir()
    monkeypatch.setenv("HOME", str(hermetic))
    monkeypatch.setenv("USERPROFILE", str(hermetic))
    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(limits, "datetime", datetime)
    monkeypatch.setattr(codex_quota, "datetime", datetime)
    monkeypatch.setattr("spend_app.quotas.datetime", datetime)

    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    initialize(settings.database_path)

    chosen = hermetic / ".codex-work"
    target = hermetic / "private-codex"
    now = datetime.now(UTC)
    for root, pct in ((chosen, 17.0), (target, 83.0)):
        session = _write_codex_session(root / "sessions", used_pct=pct, now=now)
        doc = json.loads(session.read_text(encoding="utf-8"))
        doc["type"] = "event_msg"
        doc["payload"]["type"] = "token_count"
        session.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        (root / "auth.json").write_text(json.dumps({"tokens": {"account_id": f"account-{pct}"}}), encoding="utf-8")

    service = connections.Service(settings, vault=_FakeVault())
    added = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(chosen),
            "name": "Work",
            "requestId": str(uuid4()),
        }
    )["connection"]
    key = added["key"]

    # Before retargeting: lane exists and reads 17.0%.
    pre_collectors, _ = quotas._resolve_profile_lanes(settings.database_path)
    assert key in pre_collectors
    pre_collector = pre_collectors[key]
    quotas.poll_quotas(settings.database_path)
    with connect(settings.database_path) as db:
        rows = codex_quota.read_rows(db)
    assert next(r for r in rows if r["provider_key"] == key)["pct"] == 17.0

    # Retarget approved path via Windows junction to unapproved target (83.0%).
    chosen.rename(hermetic / "chosen-original")
    _create_junction(target, chosen)

    # 1) _resolve_profile_lanes produces no lane.
    post_collectors, _ = quotas._resolve_profile_lanes(settings.database_path)
    assert key not in post_collectors

    # 2) Even a pre-swap collector callable returns no sample.
    assert pre_collector() == []

    # 3) Direct _codex_limits refuses the retargeted root.
    assert limits._codex_limits(chosen, key=key)["status"] == "unavailable"

    # 4) Default poll_quotas + read_rows masks the prior row (never surfaces 17% or 83%).
    quotas.poll_quotas(settings.database_path)
    with connect(settings.database_path) as db:
        rows = codex_quota.read_rows(db)
    visible = next(r for r in rows if r["provider_key"] == key)
    assert visible["pct"] is None

    # 5) Even if a sample carrying target's resolved scope is fed into poll_quotas,
    #    save_metadata and read_rows reject it because approved scope uses the unresolved path.
    forged = QuotaSample(
        provider_key=key,
        limit_key="weekly",
        label="Codex weekly window",
        unit="pct",
        source="codex_local_telemetry",
        pct=83.0,
        resets_at=_iso(now + timedelta(days=4)),
        observed_at=_iso(now),
        pool_id="codex",
        scope=codex_quota.scope(target),
    )
    quotas.poll_quotas(settings.database_path, collectors={key: lambda: [forged]})
    with connect(settings.database_path) as db:
        rows = codex_quota.read_rows(db)
    visible = next(r for r in rows if r["provider_key"] == key)
    assert visible["pct"] is None


def test_retargeted_claude_and_opencode_lanes_return_no_lane_and_no_sample(
    tmp_path: Path, monkeypatch
) -> None:
    from spend_app import quotas

    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(limits, "datetime", FixedClock)
    monkeypatch.setenv("BURNRATE_ENABLE_OPENCODE_GO_QUOTA", "1")

    claude_chosen = tmp_path / ".claude-work"
    claude_target = tmp_path / "private-claude"
    _write_claude_state(claude_chosen, "claude-chosen-uuid", five_pct=12.0, weekly_pct=24.0, fetched=NOW)
    _write_claude_state(claude_target, "claude-target-uuid", five_pct=91.0, weekly_pct=95.0, fetched=NOW)

    oc_chosen = tmp_path / ".opencode-work"
    oc_target = tmp_path / "private-opencode"
    _write_opencode_db(oc_chosen / "opencode" / "opencode.db", "oc-chosen-key")
    _write_opencode_db(oc_target / "opencode" / "opencode.db", "oc-target-key")

    sent_auths: list[str] = []

    def fake_get(url, *, headers=None, **_kwargs):
        sent_auths.append((headers or {}).get("Authorization", ""))
        req = httpx.Request("GET", url)
        return httpx.Response(
            200,
            request=req,
            json={
                "usage": {
                    "rolling": {"percent": 19, "resetsAt": "2026-10-26T16:00:00Z"},
                    "weekly": {"percent": 28, "resetsAt": "2026-10-30T00:00:00Z"},
                    "monthly": {"percent": 37, "resetsAt": "2026-11-20T00:00:00Z"},
                }
            },
        )

    monkeypatch.setattr(limits.httpx, "get", fake_get)

    db_path = _seed_profiles_db(
        tmp_path / "spend.db",
        {
            "claude_local": {
                "claude-code-work": _profile_record(
                    "claude_local",
                    "claude-code-work",
                    name="Work Claude",
                    location=str(claude_chosen),
                    enabled=True,
                    account_digest=_sha256("claude-chosen-uuid"),
                    tool_key="claude-code-work",
                ),
            },
            "opencode_local": {
                "opencode-go-work": _profile_record(
                    "opencode_local",
                    "opencode-go-work",
                    name="Work Go",
                    location=str(oc_chosen),
                    enabled=True,
                    account_digest=_sha256("oc-chosen-key"),
                    tool_key="opencode-go-work",
                ),
            },
        },
    )

    pre_collectors, _ = quotas._resolve_profile_lanes(db_path)
    assert "claude-code-work" in pre_collectors
    assert "opencode-go-work" in pre_collectors
    pre_claude = pre_collectors["claude-code-work"]
    pre_oc = pre_collectors["opencode-go-work"]
    assert pre_claude()[0].pct == 12.0
    assert pre_oc()[0].pct == 19.0
    assert sent_auths == ["Bearer oc-chosen-key"]

    # Retarget both approved roots via Windows junctions.
    claude_chosen.rename(tmp_path / "claude-chosen-orig")
    oc_chosen.rename(tmp_path / "oc-chosen-orig")
    _create_junction(claude_target, claude_chosen)
    _create_junction(oc_target, oc_chosen)

    # Clear caches to ensure pre-built collectors also refuse without cache hits.
    nudge_quota_lanes(db_path, None)

    post_collectors, _ = quotas._resolve_profile_lanes(db_path)
    assert "claude-code-work" not in post_collectors
    assert "opencode-go-work" not in post_collectors

    assert pre_claude() == []
    assert pre_oc() == []
    assert sent_auths == ["Bearer oc-chosen-key"]


def test_distinct_windows_directories_have_distinct_unresolved_scopes(tmp_path: Path) -> None:
    first = tmp_path / "Straße"
    second = tmp_path / "Strasse"
    first.mkdir()
    second.mkdir()
    if first.samefile(second):
        pytest.skip("Filesystem folds sharp-s and ss")
    assert codex_quota.unresolved_scope(first) != codex_quota.unresolved_scope(second)
    assert codex_quota.scope(first) != codex_quota.scope(second)


def test_unicode_casefold_junction_cannot_redirect_usage(tmp_path: Path, monkeypatch) -> None:
    from uuid import uuid4
    from spend_app import connections, profile_detect
    from spend_app.adapters import profiles_local
    from spend_app.config import Settings
    from spend_app.pricing import PricingEngine
    from spend_app.providers import REGISTRY
    from tests_spend.test_p3_profile_collection import _write_claude_transcript

    hermetic = tmp_path / "hermetic"
    hermetic.mkdir()
    monkeypatch.setenv("HOME", str(hermetic))
    monkeypatch.setenv("USERPROFILE", str(hermetic))
    for name in (
        "CODEX_HOME",
        "CLAUDE_CONFIG_DIR",
        "XDG_DATA_HOME",
        "OPENCODE_DB",
        "OPENCODE_API_KEY",
        "BURNRATE_CLAUDE_ACCOUNT_DIRS",
        "BURNRATE_OPENCODE_PROFILE_DIRS",
        "BURNRATE_CODEX_PROFILE_DIRS",
    ):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())
    profile_detect.reset_cache()

    chosen = hermetic / ".claude-straße"
    target = hermetic / ".claude-strasse"
    _write_claude_transcript(
        chosen / "projects" / "p" / "one.jsonl",
        session_id="chosen-session",
        message_id="chosen-message",
    )
    _write_claude_transcript(
        target / "projects" / "p" / "one.jsonl",
        session_id="private-session",
        message_id="private-message",
    )
    if chosen.samefile(target):
        pytest.skip("Filesystem folds sharp-s and ss")

    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    initialize(settings.database_path)

    service = connections.Service(settings, vault=_FakeVault())
    added = service.mutate(
        {
            "source": "claude_local",
            "operation": "add_profile",
            "location": str(chosen),
            "name": "Chosen",
            "requestId": str(uuid4()),
        }
    )["connection"]

    chosen.rename(hermetic / "original-chosen")
    _create_junction(target, chosen)

    profile = next(
        p for p in profile_store.enabled_profiles(settings.database_path) if p.key == added["key"]
    )
    assert profile_store.location_unchanged(profile, REGISTRY.get("claude_local")) is False
    profiles_local.ingest(
        database_path=settings.database_path,
        pricing=PricingEngine.load(settings.pricing_path),
    )
    with connect(settings.database_path) as db:
        rows = [tuple(r) for r in db.execute("SELECT session_id,tool_key FROM usage_events")]
    assert rows == []


def test_unicode_casefold_junction_cannot_redirect_quota(tmp_path: Path, monkeypatch) -> None:
    from uuid import uuid4
    from spend_app import connections, profile_detect, quotas
    from spend_app.config import Settings

    hermetic = tmp_path / "hermetic"
    hermetic.mkdir()
    monkeypatch.setenv("HOME", str(hermetic))
    monkeypatch.setenv("USERPROFILE", str(hermetic))
    _isolate_default_poll_sources(tmp_path, monkeypatch)
    monkeypatch.setattr(limits, "datetime", datetime)
    monkeypatch.setattr(codex_quota, "datetime", datetime)
    monkeypatch.setattr("spend_app.quotas.datetime", datetime)
    monkeypatch.setattr(claude_accounts, "extra_accounts", lambda: ())
    monkeypatch.setattr(opencode_profiles, "extra_profiles", lambda: ())
    profile_detect.reset_cache()

    chosen = hermetic / ".codex-straße"
    target = hermetic / ".codex-strasse"
    now = datetime.now(UTC)
    for root, pct in ((chosen, 17.0), (target, 83.0)):
        session = _write_codex_session(root / "sessions", used_pct=pct, now=now)
        doc = json.loads(session.read_text(encoding="utf-8"))
        doc["type"] = "event_msg"
        doc["payload"]["type"] = "token_count"
        session.write_text(json.dumps(doc) + "\n", encoding="utf-8")
        (root / "auth.json").write_text(
            json.dumps({"tokens": {"account_id": f"account-{pct}"}}), encoding="utf-8"
        )
    if chosen.samefile(target):
        pytest.skip("Filesystem folds sharp-s and ss")

    settings = Settings(
        database_path=tmp_path / "spend.db",
        pricing_path=Path(__file__).resolve().parents[1] / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
        logs_path=tmp_path / "logs",
    )
    initialize(settings.database_path)

    service = connections.Service(settings, vault=_FakeVault())
    added = service.mutate(
        {
            "source": "codex_local",
            "operation": "add_profile",
            "location": str(chosen),
            "name": "Chosen",
            "requestId": str(uuid4()),
        }
    )["connection"]

    chosen.rename(hermetic / "original-chosen")
    _create_junction(target, chosen)

    collectors, _ = quotas._resolve_profile_lanes(settings.database_path)
    reader = collectors.get(added["key"])
    samples = reader() if reader is not None else []
    quotas.poll_quotas(settings.database_path, collectors={added["key"]: lambda: samples})
    with connect(settings.database_path) as db:
        rows = codex_quota.read_rows(db)
    visible = next((r for r in rows if r["provider_key"] == added["key"]), None)
    assert visible is None or visible["pct"] is None


def test_ascii_case_and_dotdot_aliases_are_consistent(tmp_path: Path) -> None:
    from spend_app.providers import REGISTRY

    root = tmp_path / "Profile"
    root.mkdir()
    intermediate = tmp_path / "intermediate"
    intermediate.mkdir()
    aliases = [intermediate / ".." / "Profile"]
    if os.name == "nt":
        aliases.append(tmp_path / "PROFILE")
    for alias in aliases:
        assert alias.samefile(root)
        assert codex_quota.unresolved_scope(alias) == codex_quota.unresolved_scope(root)
        assert profile_store.location_unchanged(str(alias), REGISTRY.get("codex_local"))
    distinct = tmp_path / "Unrelated"
    distinct.mkdir()
    assert codex_quota.unresolved_scope(root) != codex_quota.unresolved_scope(distinct)
