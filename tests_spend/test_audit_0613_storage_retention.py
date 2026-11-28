"""Durable tests for Phase 1 of the 0.6.12 -> 0.6.13 Usage & Account Data Audit:
D-01 (recursive Windows/POSIX private ACLs), D-10 (bounded backups, wheel staging,
WebView2 temp sessions, and SQLite retention tables).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from spend_app.db import (
    backup_database,
    connect,
    initialize,
    prune_automatic_backups,
    prune_retention_tables,
)
from spend_app.local_access import ensure_local_key, protect_data_tree
from spend_app.native_update import Updater
from spend_app.wealthfront_window import sweep_stale_webview_sessions


@pytest.mark.skipif(os.name != "nt", reason="Windows NTFS ACL enforcement test")
def test_d01_protect_data_tree_strips_inherited_aces_from_preexisting_backups_and_logs(tmp_path):
    """Upgraded Windows installs must strip inherited read ACEs from backups/ and logs/."""
    data_dir = tmp_path / "BURNRATE"
    backups = data_dir / "backups" / "before-upgrade"
    logs = data_dir / "logs"
    backups.mkdir(parents=True)
    logs.mkdir(parents=True)
    db_path = data_dir / "spend.db"
    initialize(db_path)
    backup_file = backups / "old.db"
    backup_file.write_bytes(b"backup-data")
    log_file = logs / "dashboard.log"
    log_file.write_text("log line\n", encoding="utf-8")

    # Simulate an upgraded install where BUILTIN\Users (analogous to sandbox groups)
    # holds an inherited read ACE on backups/ and logs/.
    for target in (data_dir / "backups", logs):
        res = subprocess.run(
            ["icacls", str(target), "/grant", "BUILTIN\\Users:(OI)(CI)(RX)"],
            capture_output=True,
            text=True,
            check=False,
        )
        assert res.returncode == 0, res.stderr

    protect_data_tree(db_path, protect_root=True)

    for inspected in (data_dir, db_path, data_dir / "backups", backup_file, logs, log_file):
        out = subprocess.run(
            ["icacls", str(inspected)],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
        assert "BUILTIN\\Users" not in out, f"Unexpected Users ACE left on {inspected}:\n{out}"
        assert "(I)" not in out, f"Inherited ACE still present on {inspected}:\n{out}"


def test_d01_ensure_local_key_protects_custom_db_subfolders(tmp_path):
    """ensure_local_key hardens managed subfolders even for non-default DB paths."""
    db_path = tmp_path / "spend.db"
    initialize(db_path)
    (tmp_path / "backups").mkdir()
    backup_copy = tmp_path / "backups" / "pre-wheel-update-20260101.db"
    backup_database(db_path, backup_copy)
    (tmp_path / "logs").mkdir()
    log_file = tmp_path / "logs" / "dashboard.log"
    log_file.write_text("ok\n", encoding="utf-8")

    token = ensure_local_key(db_path)
    assert len(token) == 64
    if os.name == "nt":
        out = subprocess.run(["icacls", str(backup_copy)], capture_output=True, text=True, check=True).stdout
        assert "(I)" not in out
    else:
        assert (backup_copy.stat().st_mode & 0o777) == 0o600
        assert ((tmp_path / "backups").stat().st_mode & 0o777) == 0o700


def test_d10_prune_automatic_backups_keeps_newest_and_preserves_manual(tmp_path):
    """Automatic BURNRATE backups are capped while user-named backups are untouched."""
    backups = tmp_path / "backups"
    backups.mkdir()
    manual_dir = backups / "my-manual-snapshot"
    manual_dir.mkdir()
    (manual_dir / "spend.db").write_bytes(b"keep-me")
    manual_file = backups / "custom-export.db"
    manual_file.write_bytes(b"keep-me-too")

    created = []
    base_time = time.time() - 1000
    for idx in range(8):
        p = backups / f"pre-wheel-update-2026110{idx}.db"
        p.write_bytes(b"auto")
        os.utime(p, (base_time + idx * 10, base_time + idx * 10))
        created.append(p)
    del_dir = backups / "before-machine-data-delete-old"
    del_dir.mkdir()
    (del_dir / "spend.db").write_bytes(b"old-del")
    os.utime(del_dir, (base_time - 50, base_time - 50))

    removed = prune_automatic_backups(backups, keep=5)
    assert removed == 4
    assert manual_dir.is_dir() and manual_file.is_file()
    assert not del_dir.exists()
    remaining_auto = sorted(p.name for p in backups.iterdir() if p.name.startswith("pre-wheel-update-"))
    assert len(remaining_auto) == 5
    assert remaining_auto == [f"pre-wheel-update-2026110{i}.db" for i in range(3, 8)]


def test_d10_cleanup_stale_wheel_jobs_respects_active_lock(tmp_path):
    updates = tmp_path / "updates"
    updates.mkdir()
    stale = updates / "wheel-abc123"
    stale.mkdir()
    (stale / "burnrate-0.6.12-py3-none-any.whl").write_bytes(b"whl")
    other = updates / "myBurnRate_0.6.12_x64-setup.exe"
    other.write_bytes(b"exe")

    removed = Updater.cleanup_stale_wheel_jobs(updates)
    assert removed == 1
    assert not stale.exists()
    assert other.exists()


def test_d02_d10_sweep_stale_webview_sessions_only_removes_dead_pids(tmp_path):
    root = tmp_path / "burnrate-wv2-sessions"
    root.mkdir()
    dead_a = root / "pid-999991"
    dead_b = root / "pid-999992"
    live_other = root / "pid-4242"
    live_self = root / f"pid-{os.getpid()}"
    unrelated = root / "keep-other-folder"
    for d in (dead_a, dead_b, live_other, live_self, unrelated):
        d.mkdir()
        (d / "cache.bin").write_bytes(b"x")

    removed = sweep_stale_webview_sessions(
        root,
        current_pid=os.getpid(),
        is_alive=lambda pid: pid == 4242,
    )
    assert removed == 2
    assert not dead_a.exists()
    assert not dead_b.exists()
    assert live_other.exists()
    assert live_self.exists()
    assert unrelated.exists()


def test_d10_prune_retention_tables_prunes_aged_rows_and_preserves_recent(tmp_path):
    db_path = tmp_path / "spend.db"
    initialize(db_path)
    now = datetime(2026, 11, 27, 12, 0, 0, tzinfo=UTC)
    now_iso = now.isoformat().replace("+00:00", "Z")
    old_120d = (now - timedelta(days=120)).isoformat().replace("+00:00", "Z")
    recent_5d = (now - timedelta(days=5)).isoformat().replace("+00:00", "Z")
    old_45d = (now - timedelta(days=45)).isoformat().replace("+00:00", "Z")
    future_10d = (now + timedelta(days=10)).isoformat().replace("+00:00", "Z")
    old_10d = (now - timedelta(days=10)).isoformat().replace("+00:00", "Z")

    with connect(db_path) as conn:
        # 8 old balance samples + 2 recent -> keep 5 newest total (2 recent + 3 old)
        for i in range(8):
            ts = (now - timedelta(days=120 - i)).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO balance_samples(provider_key, source, usd, sampled_at) VALUES(?,?,?,?)",
                ("deepseek", "deepseek_balance", 10.0, ts),
            )
        for i in range(2):
            ts = (now - timedelta(days=5 - i)).isoformat().replace("+00:00", "Z")
            conn.execute(
                "INSERT INTO balance_samples(provider_key, source, usd, sampled_at) VALUES(?,?,?,?)",
                ("deepseek", "deepseek_balance", 9.0, ts),
            )

        # owner_sessions: 1 old revoked, 1 old expired, 1 recent revoked, 1 active
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s-old-rev", "owner-1", "owner", "h1", "PC", old_120d, old_120d, old_120d, future_10d, old_45d),
        )
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s-old-exp", "owner-1", "owner", "h2", "PC", old_120d, old_120d, old_120d, old_45d, None),
        )
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s-rec-rev", "owner-1", "owner", "h3", "PC", recent_5d, recent_5d, recent_5d, future_10d, recent_5d),
        )
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("s-active", "owner-1", "owner", "h4", "PC", recent_5d, recent_5d, recent_5d, future_10d, None),
        )

        # attention.v1.request.*: 1 expired, 1 recent
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            ("attention.v1.request.old", json.dumps({"digest": "abc", "at": old_10d})),
        )
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?)",
            ("attention.v1.request.fresh", json.dumps({"digest": "def", "at": recent_5d})),
        )

        # connections.v2 requests: 125 entries -> trimmed to 100
        conn_state = {"version": 2, "requests": {f"req-{i:03d}": {"fingerprint": "x", "result": {}} for i in range(125)}}
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES('connections.v2', ?)",
            (json.dumps(conn_state),),
        )

        # Ensure accounting cursor key is never deleted by retention pruning
        conn.execute(
            "INSERT INTO app_meta(key, value) VALUES('codex.counter-ranges.v1:sess-1', '{}')"
        )

        counts = prune_retention_tables(conn, now=now_iso)

        assert counts["balance_samples"] == 5
        assert conn.execute("SELECT COUNT(*) FROM balance_samples").fetchone()[0] == 5
        assert counts["owner_sessions"] == 2
        remaining_sessions = {r[0] for r in conn.execute("SELECT id FROM owner_sessions").fetchall()}
        assert remaining_sessions == {"s-rec-rev", "s-active"}
        assert counts["attention_requests"] == 1
        assert conn.execute("SELECT 1 FROM app_meta WHERE key='attention.v1.request.fresh'").fetchone()
        assert not conn.execute("SELECT 1 FROM app_meta WHERE key='attention.v1.request.old'").fetchone()
        assert counts["connection_requests"] == 25
        v2 = json.loads(conn.execute("SELECT value FROM app_meta WHERE key='connections.v2'").fetchone()[0])
        assert len(v2["requests"]) == 100
        assert "req-124" in v2["requests"] and "req-000" not in v2["requests"]
        # Accounting cursor key untouched
        assert conn.execute("SELECT 1 FROM app_meta WHERE key='codex.counter-ranges.v1:sess-1'").fetchone()
