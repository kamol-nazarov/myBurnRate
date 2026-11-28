"""Owner-operated removal of one revoked computer's imported data."""

from __future__ import annotations

import json
import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from spend_app.auth_model import Principal, audit
from spend_app.db import backup_database, connect, initialize, prune_automatic_backups, utc_now


def _scrub_pricing_gaps(connection: sqlite3.Connection, machine_id: str) -> int:
    """Recompute or drop pricing_gaps rows whose sample or counts came from ``machine_id``."""
    prefix = f"m:{machine_id}:*"
    sync_source = f"sync:{machine_id}"
    rows = connection.execute(
        "SELECT model_key, source, sample_raw_id FROM pricing_gaps "
        "WHERE sample_raw_id GLOB ? OR source = ? OR source GLOB ?",
        (prefix, sync_source, f"{sync_source}:*"),
    ).fetchall()
    touched = 0
    for row in rows:
        model_key = row[0]
        remaining = connection.execute(
            "SELECT COUNT(*), MIN(occurred_at), MAX(occurred_at), MAX(raw_id) "
            "FROM pricing_gap_events WHERE model_key=?",
            (model_key,),
        ).fetchone()
        count = int(remaining[0] or 0) if remaining else 0
        if count == 0:
            touched += int(
                connection.execute("DELETE FROM pricing_gaps WHERE model_key=?", (model_key,)).rowcount or 0
            )
        else:
            sample_row = connection.execute(
                "SELECT source, raw_id FROM pricing_gap_events WHERE model_key=? ORDER BY occurred_at DESC LIMIT 1",
                (model_key,),
            ).fetchone()
            connection.execute(
                "UPDATE pricing_gaps SET source=?, first_seen_at=?, last_seen_at=?, occurrences=?, sample_raw_id=? "
                "WHERE model_key=?",
                (sample_row[0], remaining[1], remaining[2], count, sample_row[1], model_key),
            )
            touched += 1
    return touched


def delete_machine_data(
    database_path: Path,
    machine_id: str,
    *,
    actor: Principal,
    create_backup: bool = False,
    source: str | None = None,
) -> dict:
    from spend_app.local_access import protect_directory_tree
    from spend_app.sync import _MACHINE_ID_RE, _read_state

    # The local CLI is an operating-system administrator of this database.
    if not actor.permits("admin.write") and (actor.kind, actor.id) != ("system", "local-cli"):
        raise PermissionError("Owner authorization is required.")
    if not _MACHINE_ID_RE.fullmatch(machine_id) or machine_id in {"local", "all"}:
        raise ValueError("Expected a remote computer.")

    initialize(database_path)
    backup_name: str | None = None
    with connect(database_path) as connection:
        connection.execute("PRAGMA secure_delete = ON")
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        machine = state["machines"].get(machine_id)
        if not isinstance(machine, dict) or not machine.get("revoked"):
            raise ValueError("Revoke this computer before removing its data.")

        if create_backup:
            parent = database_path.parent / "backups"
            parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            if parent.resolve(strict=True) != parent.absolute():
                raise ValueError("The backup directory is redirected.")
            backup_dir = Path(tempfile.mkdtemp(prefix="before-machine-data-delete-", dir=parent))
            backup_path = backup_dir / "spend.db"
            backup_database(database_path, backup_path)
            with closing(sqlite3.connect(backup_path.resolve().as_uri() + "?mode=ro", uri=True)) as backup:
                if backup.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
                    raise ValueError("The pre-removal backup did not pass its integrity check.")
            try:
                protect_directory_tree(backup_dir)
            except OSError:
                pass
            prune_automatic_backups(parent)
            backup_name = backup_dir.name

        names = (
            ("usage_events", "raw_id"), ("unpriced_usage_events", "raw_id"),
            ("provider_cost_buckets", "raw_id"), ("sessions", "session_id"),
            ("agent_runs", "id"), ("pricing_gap_events", "raw_id"),
            ("coverage_gap_events", "raw_id"),
        )
        removed = 0
        for table, column in names:
            removed += connection.execute(
                f"DELETE FROM {table} WHERE {column} GLOB ?", (f"m:{machine_id}:*",),
            ).rowcount
        for table in ("quotas", "balance_samples", "ingest_runs"):
            removed += connection.execute(
                f"DELETE FROM {table} WHERE source=? OR source GLOB ?",
                (f"sync:{machine_id}", f"sync:{machine_id}:*"),
            ).rowcount
        for table in ("sync_usage_receipts", "sync_usage_conflicts"):
            removed += connection.execute(
                f"DELETE FROM {table} WHERE machine_id=?", (machine_id,),
            ).rowcount
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='owner_sessions'"
        ).fetchone():
            removed += connection.execute(
                "DELETE FROM owner_sessions WHERE machine_id=?", (machine_id,),
            ).rowcount
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quota_pace_samples'"
        ).fetchone():
            pace_cols = {row[1] for row in connection.execute("PRAGMA table_info('quota_pace_samples')")}
            if "machine_id" in pace_cols:
                removed += connection.execute(
                    "DELETE FROM quota_pace_samples WHERE machine_id=?", (machine_id,),
                ).rowcount
        removed += _scrub_pricing_gaps(connection, machine_id)
        connection.execute(
            "DELETE FROM app_meta WHERE key IN (?,?)",
            (f"sync.received.usage_changes.v1:{machine_id}", f"sync.received.usage_head.v1:{machine_id}"),
        )
        # Keep minimal revocation tombstone while scrubbing display metadata.
        machine["name"] = "Deleted computer"
        machine["reported_name"] = None
        machine["os"] = None
        machine["data_deleted_at"] = utc_now()
        state["machines"][machine_id] = machine
        connection.execute(
            "UPDATE app_meta SET value=? WHERE key='machines.v1'",
            (json.dumps(state, separators=(",", ":")),),
        )
        audit(connection, actor, "machine.data.delete", machine_id, source=source)

    # Zero freed pages and compact freelist so deleted usage rows are not recoverable.
    with closing(sqlite3.connect(database_path, timeout=30, isolation_level=None)) as compact:
        compact.execute("PRAGMA secure_delete = ON")
        compact.execute("VACUUM")
    return {"ok": True, "machineId": machine_id, "recordsRemoved": removed, "backup": backup_name}
