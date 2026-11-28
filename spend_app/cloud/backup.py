"""Safe, symlink-checked online SQLite backup helper for myBurnRate Cloud."""
from __future__ import annotations

import argparse
import sqlite3
from contextlib import closing
from datetime import UTC, datetime
from pathlib import Path

from spend_app.cloud.tenant import validate_user_id
from spend_app.connection_paths import is_name_surrogate


def _backup_sqlite(source: Path, destination: Path) -> None:
    if is_name_surrogate(source):
        raise PermissionError(f"Refusing to back up symlink or junction: {source}")
    destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    with closing(sqlite3.connect(source.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=30)) as src:
        with closing(sqlite3.connect(destination, timeout=30)) as dst:
            src.backup(dst)
    with closing(sqlite3.connect(destination.resolve(strict=True).as_uri() + "?mode=ro", uri=True, timeout=10)) as check:
        if check.execute("PRAGMA quick_check").fetchall() != [("ok",)]:
            raise RuntimeError(f"Integrity check failed for backup {destination}")


def backup_cloud_root(cloud_root: Path, backup_dir: Path) -> dict:
    """Copy auth.db and every valid users/<uid>/spend.db safely via SQLite's online backup API."""
    root = Path(cloud_root)
    if root.exists() and is_name_surrogate(root):
        raise PermissionError("Cloud root directory must not be a symlink or junction.")
    out_base = Path(backup_dir)
    if out_base.exists() and is_name_surrogate(out_base):
        raise PermissionError("Backup output directory must not be a symlink or junction.")

    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out_dir = out_base / f"cloud-backup-{stamp}"
    out_dir.mkdir(mode=0o700, parents=True, exist_ok=True)

    auth_db = root / "auth.db"
    auth_backed_up = False
    if auth_db.exists() or auth_db.is_symlink():
        if is_name_surrogate(auth_db):
            raise PermissionError("auth.db must not be a symlink or junction.")
        if auth_db.is_file():
            _backup_sqlite(auth_db, out_dir / "auth.db")
            auth_backed_up = True

    backed_up_tenants = 0
    skipped_entries = 0
    users_dir = root / "users"
    if users_dir.exists() or users_dir.is_symlink():
        if is_name_surrogate(users_dir):
            raise PermissionError("Cloud users directory must not be a symlink or junction.")
        if users_dir.is_dir():
            for child in sorted(users_dir.iterdir()):
                if is_name_surrogate(child):
                    skipped_entries += 1
                    continue
                try:
                    uid = validate_user_id(child.name)
                except ValueError:
                    skipped_entries += 1
                    continue
                if not child.is_dir():
                    skipped_entries += 1
                    continue
                spend_db = child / "spend.db"
                if spend_db.exists() or spend_db.is_symlink():
                    if is_name_surrogate(spend_db):
                        skipped_entries += 1
                        continue
                    if spend_db.is_file():
                        _backup_sqlite(spend_db, out_dir / "users" / uid / "spend.db")
                        backed_up_tenants += 1

    return {
        "backupDir": str(out_dir),
        "authBackedUp": auth_backed_up,
        "tenantsBackedUp": backed_up_tenants,
        "skippedEntries": skipped_entries,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Safe online backup for myBurnRate Cloud SQLite files.")
    parser.add_argument("--cloud-root", required=True, type=Path, help="Path to cloud data root")
    parser.add_argument("--out", required=True, type=Path, help="Destination backups directory")
    args = parser.parse_args(argv)
    summary = backup_cloud_root(args.cloud_root, args.out)
    print(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
