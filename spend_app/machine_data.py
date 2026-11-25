"""Owner-operated removal of one revoked computer's imported data."""

from __future__ import annotations

import sqlite3
import tempfile
from contextlib import closing
from pathlib import Path

from spend_app.auth_model import Principal, audit
from spend_app.db import backup_database, connect, initialize


def delete_machine_data(database_path: Path, machine_id: str, *, actor: Principal) -> dict:
    from spend_app.sync import _MACHINE_ID_RE, _read_state

    # The local CLI is an operating-system administrator of this database.
    if not actor.permits("admin.write") and (actor.kind, actor.id) != ("system", "local-cli"):
        raise PermissionError("Owner authorization is required.")
    if not _MACHINE_ID_RE.fullmatch(machine_id) or machine_id in {"local", "all"}:
        raise ValueError("Expected a remote computer.")

    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        machine = _read_state(connection)["machines"].get(machine_id)
        if not isinstance(machine, dict) or not machine.get("revoked"):
            raise ValueError("Revoke this computer before removing its data.")

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
        connection.execute(
            "DELETE FROM app_meta WHERE key IN (?,?)",
            (f"sync.received.usage_changes.v1:{machine_id}", f"sync.received.usage_head.v1:{machine_id}"),
        )
        audit(connection, actor, "machine.data.delete", machine_id)
    return {"ok": True, "machineId": machine_id, "recordsRemoved": removed, "backup": backup_dir.name}
