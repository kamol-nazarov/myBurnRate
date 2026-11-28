"""Physical per-user SQLite tenant storage with strict path isolation and quotas."""
from __future__ import annotations

import re
import shutil
import threading
import time
from collections import OrderedDict
from pathlib import Path

from spend_app.connection_paths import is_name_surrogate
from spend_app.db import (
    connect,
    initialize,
    prune_ingest_runs,
    prune_quotas,
    prune_retention_tables,
    prune_usage_changes,
)
from spend_app.local_access import protect_data_tree

USER_ID_RE = re.compile(r"^[0-9a-f]{32}$")
DEFAULT_MAX_DB_BYTES = 250 * 1024 * 1024  # 250 MB per user
DEFAULT_MAX_DEVICES = 5
_MAX_CACHED_LOCKS = 2048
_LAZY_PRUNE_INTERVAL_SECONDS = 86400


class StorageQuotaExceeded(Exception):
    """Raised when a user's SQLite database reaches its configured cloud size cap."""


def validate_user_id(user_id: str) -> str:
    clean = str(user_id or "").strip().lower()
    if not USER_ID_RE.fullmatch(clean):
        raise ValueError("Invalid tenant identifier.")
    return clean


class TenantManager:
    """Resolve, provision, quota-check, lazily prune, and delete per-user SQLite DBs."""

    def __init__(
        self,
        root: Path,
        *,
        default_max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
        prune_interval_seconds: int = _LAZY_PRUNE_INTERVAL_SECONDS,
    ) -> None:
        self.root = Path(root)
        self.users_dir = self.root / "users"
        self.default_max_db_bytes = int(default_max_db_bytes)
        self.prune_interval_seconds = int(prune_interval_seconds)
        self._guard = threading.Lock()
        self._locks: OrderedDict[str, threading.RLock] = OrderedDict()
        self._last_pruned: dict[str, float] = {}
        self._resolved_users_dir: Path | None = None

    def _ensure_users_dir(self) -> Path:
        cached = self._resolved_users_dir
        if cached is not None and cached.is_dir():
            return cached
        self.users_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        if is_name_surrogate(self.users_dir):
            raise PermissionError("Cloud tenant storage directory must not be redirected.")
        resolved = self.users_dir.resolve(strict=True)
        self._resolved_users_dir = resolved
        return resolved

    def lock_for(self, user_id: str) -> threading.RLock:
        uid = validate_user_id(user_id)
        with self._guard:
            lock = self._locks.get(uid)
            if lock is not None:
                self._locks.move_to_end(uid)
                return lock
            lock = threading.RLock()
            self._locks[uid] = lock
            while len(self._locks) > _MAX_CACHED_LOCKS:
                self._locks.popitem(last=False)
            return lock

    def user_dir(self, user_id: str) -> Path:
        uid = validate_user_id(user_id)
        base = self._ensure_users_dir()
        candidate = base / uid
        if candidate.exists() and is_name_surrogate(candidate):
            raise PermissionError("Tenant directory must not be a symlink or junction.")
        resolved = candidate.resolve(strict=False)
        if resolved.parent != base:
            raise ValueError("Resolved tenant path escaped users directory.")
        return candidate

    def user_db_path(self, user_id: str) -> Path:
        folder = self.user_dir(user_id)
        db_path = folder / "spend.db"
        if db_path.exists() and is_name_surrogate(db_path):
            raise PermissionError("Tenant database must not be a symlink or junction.")
        return db_path

    def provision_user_db(self, user_id: str) -> Path:
        """Create and initialize a verified user's isolated SQLite database."""
        uid = validate_user_id(user_id)
        with self.lock_for(uid):
            folder = self.user_dir(uid)
            folder.mkdir(mode=0o700, parents=True, exist_ok=True)
            db_path = self.user_db_path(uid)
            initialize(db_path)
            try:
                protect_data_tree(db_path, protect_root=True)
            except OSError:
                pass
            return db_path

    def existing_user_db(self, user_id: str) -> Path:
        """Return an already-provisioned user DB; never creates one implicitly."""
        uid = validate_user_id(user_id)
        db_path = self.user_db_path(uid)
        if not db_path.is_file():
            raise FileNotFoundError(f"Tenant database does not exist for {uid}")
        return db_path

    def db_size_bytes(self, user_id: str) -> int:
        uid = validate_user_id(user_id)
        db_path = self.user_db_path(uid)
        total = 0
        for candidate in (db_path, Path(str(db_path) + "-wal"), Path(str(db_path) + "-shm")):
            try:
                if candidate.is_file() and not is_name_surrogate(candidate):
                    total += candidate.stat().st_size
            except OSError:
                continue
        return total

    def check_storage_quota(self, user_id: str, max_bytes: int | None = None) -> int:
        limit = self.default_max_db_bytes if max_bytes is None else int(max_bytes)
        size = self.db_size_bytes(user_id)
        if limit > 0 and size >= limit:
            raise StorageQuotaExceeded(
                f"Cloud storage quota reached ({size // (1024 * 1024)} MB / {limit // (1024 * 1024)} MB)."
            )
        return size

    def maybe_prune_user_db(self, user_id: str, *, now_monotonic: float | None = None) -> bool:
        """Lazily prune retention tables at most once per prune interval per user."""
        uid = validate_user_id(user_id)
        tick = time.monotonic() if now_monotonic is None else float(now_monotonic)
        with self._guard:
            last = self._last_pruned.get(uid)
            if last is not None and (tick - last) < self.prune_interval_seconds:
                return False
            self._last_pruned[uid] = tick
        try:
            db_path = self.existing_user_db(uid)
        except FileNotFoundError:
            return False
        with self.lock_for(uid):
            with connect(db_path) as conn:
                prune_ingest_runs(conn)
                prune_quotas(conn)
                prune_usage_changes(conn)
                prune_retention_tables(conn)
        return True

    def delete_user_storage(self, user_id: str) -> bool:
        """Permanently delete a user's isolated folder and SQLite database files."""
        from spend_app.aggregate import evict_database_memo
        from spend_app.plans_value_store import evict_plans_value_cache

        uid = validate_user_id(user_id)
        with self.lock_for(uid):
            base = self._ensure_users_dir()
            folder = base / uid
            db_path = folder / "spend.db"
            evict_database_memo(db_path)
            evict_plans_value_cache(db_path)
            if not folder.exists():
                removed = False
            elif is_name_surrogate(folder):
                raise PermissionError("Refusing to delete redirected tenant directory.")
            else:
                for attempt in range(4):
                    try:
                        shutil.rmtree(folder)
                        break
                    except PermissionError:
                        if attempt == 3:
                            raise
                        time.sleep(0.05 * (attempt + 1))
                removed = True
        with self._guard:
            self._last_pruned.pop(uid, None)
            self._locks.pop(uid, None)
        return removed
