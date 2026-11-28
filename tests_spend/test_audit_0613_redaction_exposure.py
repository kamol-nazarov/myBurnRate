"""Durable tests for Phase 4 of the 0.6.12 -> 0.6.13 Usage & Account Data Audit:
D-07 (local agent_runs.name redaction, BURNRATE_HIDE_LOCAL_TITLES, and 24h TTL on no_data rows),
D-09 (desktop.log HubUrlRedactingFilter & Meta EAA... token redaction),
D-11 (GET /api/connections top-level sourceIdentity stripping & ~ path normalization),
D-12 (BURNRATE_ENABLE_TRAYCER_CLI opt-out toggle).
"""
from __future__ import annotations

import logging
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spend_app import connections as c
from spend_app import limits
from spend_app.config import load_settings
from spend_app.db import connect, initialize, upsert_agent_run
from spend_app.quotas import _clear_unseen_live_runs
from spend_app.redact import redact
from spend_app.wealthfront_window import configure_desktop_logging


def test_d07_local_agent_run_name_redacts_secrets_and_prunes_stale_no_data(tmp_path, monkeypatch):
    db_path = tmp_path / "spend.db"
    initialize(db_path)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    now_iso = now.isoformat().replace("+00:00", "Z")
    old_48h = (now - timedelta(hours=48)).isoformat().replace("+00:00", "Z")
    recent_2h = (now - timedelta(hours=2)).isoformat().replace("+00:00", "Z")

    monkeypatch.delenv("BURNRATE_HIDE_LOCAL_TITLES", raising=False)
    synthetic_key = "-".join(("sk", "ant", "api03", "verysecretvalue1234567890"))
    with connect(db_path) as conn:
        upsert_agent_run(
            conn,
            id="codex:1",
            name=f"Rotate {synthetic_key} for prod",
            model_key="openai:gpt-5",
            state="live",
            started_at=now_iso,
            last_seen_at=now_iso,
        )
        upsert_agent_run(
            conn,
            id="traycer:old",
            name="Old stale task title",
            model_key="traycer",
            state="no_data",
            started_at=old_48h,
            last_seen_at=old_48h,
        )
        upsert_agent_run(
            conn,
            id="traycer:recent",
            name="Recent unmetered task",
            model_key="traycer",
            state="no_data",
            started_at=recent_2h,
            last_seen_at=recent_2h,
        )
        stored_live = conn.execute("SELECT name FROM agent_runs WHERE id='codex:1'").fetchone()[0]
        assert synthetic_key not in stored_live
        assert "[redacted]" in stored_live.lower()

        # Stale (>24h) no_data row pruned; recent (<24h) no_data row kept
        removed = _clear_unseen_live_runs(conn, {"codex:1"}, now=now_iso)
        assert removed == 1
        ids = {r[0] for r in conn.execute("SELECT id FROM agent_runs").fetchall()}
        assert ids == {"codex:1", "traycer:recent"}

    # Opt-in BURNRATE_HIDE_LOCAL_TITLES=1 replaces local titles with fixed tool labels
    monkeypatch.setenv("BURNRATE_HIDE_LOCAL_TITLES", "1")
    with connect(db_path) as conn:
        upsert_agent_run(
            conn,
            id="claude:2",
            name="Confidential customer contract review",
            model_key="anthropic:claude-sonnet-4-6",
            state="live",
            started_at=now_iso,
            last_seen_at=now_iso,
        )
        hidden = conn.execute("SELECT name FROM agent_runs WHERE id='claude:2'").fetchone()[0]
        assert "Confidential" not in hidden
        assert hidden == "Claude Code"


def test_d09_desktop_log_redacts_hub_url_and_meta_tokens(tmp_path, monkeypatch):
    secret_hub = "https://private-hub.tail99999.ts.net:8443"
    meta_token = "EAABsbCS1iHgBAPrivateSyntheticMetaToken1234567890"
    monkeypatch.setenv("BURNRATE_HUB_URL", secret_hub)

    # Direct redact() check for bare Meta EAA... tokens
    scrubbed = redact(f"Muse error with token {meta_token} on {secret_hub}")
    assert meta_token not in scrubbed

    log_path = configure_desktop_logging(tmp_path)
    assert log_path is not None
    try:
        logging.getLogger("spend_app.test_d09").warning(
            "Desktop failed calling %s with %s", secret_hub, meta_token
        )
        logging.getLogger("pywebview").warning(
            "Webview redirect to %s carrying %s", secret_hub, meta_token
        )
        for name in ("spend_app", "pywebview"):
            for h in logging.getLogger(name).handlers:
                h.flush()
        text = log_path.read_text(encoding="utf-8")
        assert "private-hub.tail99999.ts.net" not in text
        assert meta_token not in text
    finally:
        for name in ("spend_app", "pywebview"):
            lgr = logging.getLogger(name)
            for h in list(lgr.handlers):
                if getattr(h, "_burnrate_desktop_log", False):
                    lgr.removeHandler(h)
                    h.close()


def test_d11_connections_top_level_strips_source_identity_and_normalizes_home_path(tmp_path, monkeypatch):
    home = tmp_path / "home"
    claude_dir = home / ".claude" / "projects"
    claude_dir.mkdir(parents=True)
    db_path = tmp_path / "spend.db"
    initialize(db_path)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(db_path))
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)

    settings = load_settings()
    service = c.Service(settings)
    with service.store.transaction() as state:
        state["profiles"].setdefault("claude_local", {})["default"] = {
            "source": "claude_local",
            "bindingId": "claude_local",
            "name": "Default",
            "location": str(claude_dir),
            "origin": "default",
            "mode": "manual",
            "enabled": True,
            "revision": 1,
            "state": "connected",
            "detail": "ok",
            "capabilities": ["usage"],
            "toolKey": "claude-code",
            "sourceIdentity": "secret-internal-folder-id",
        }

    listed = service.list()
    claude_row = next(r for r in listed["connections"] if r["source"] == "claude_local")
    assert "sourceIdentity" not in claude_row
    assert claude_row["location"].startswith("~")
    assert str(home) not in claude_row["location"]


def test_d12_traycer_cli_opt_out_blocks_subprocess_spawn(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_ENABLE_TRAYCER_CLI", "0")
    assert limits.traycer_cli_enabled() is False

    spawned = []
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: spawned.append(a))
    monkeypatch.setattr(limits, "_traycer_sender", lambda: ("epic-1", "agent-1"))

    with pytest.raises(RuntimeError, match="BURNRATE_ENABLE_TRAYCER_CLI"):
        limits._traycer_profile_rate_limits_uncached("claude")
    with pytest.raises(RuntimeError, match="BURNRATE_ENABLE_TRAYCER_CLI"):
        limits._traycer_active_agents_uncached("epic-1", "agent-1")
    assert spawned == []
