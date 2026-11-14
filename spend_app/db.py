from __future__ import annotations

import json
import os
import sqlite3
import threading
from contextlib import contextmanager, closing
from contextvars import ContextVar
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Iterator

from spend_app.subscriptions import (
    SUBSCRIPTION_SEED_VERSION_KEY,
    migrate_legacy_subscription_identities,
    migrate_legacy_zai_seed,
    seed_subscriptions,
)


SCHEMA_VERSION = 12

EXACT_USAGE_SOURCES = ("codex_local", "openai_admin", "claude_local", "anthropic_admin")

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS app_meta (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL CHECK (input_tokens >= 0),
    cached_input_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cached_input_tokens >= 0),
    cache_write_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_tokens >= 0),
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0 CHECK (cache_write_1h_tokens >= 0),
    output_tokens INTEGER NOT NULL CHECK (output_tokens >= 0),
    reasoning_tokens INTEGER CHECK (reasoning_tokens IS NULL OR reasoning_tokens >= 0),
    cost_usd REAL,
    computed_cost_usd REAL NOT NULL CHECK (computed_cost_usd >= 0),
    is_exact INTEGER NOT NULL DEFAULT 0 CHECK (is_exact IN (0, 1)),
    occurred_epoch_us INTEGER,
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_usage_events_occurred_at ON usage_events(occurred_at);
CREATE INDEX IF NOT EXISTS idx_usage_events_tool_model ON usage_events(tool_key, model_key);
CREATE INDEX IF NOT EXISTS idx_usage_events_session ON usage_events(session_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_project ON usage_events(project);

CREATE TABLE IF NOT EXISTS model_prices (
    model_key TEXT NOT NULL,
    input_per_mtok REAL NOT NULL CHECK (input_per_mtok >= 0),
    cached_input_per_mtok REAL NOT NULL CHECK (cached_input_per_mtok >= 0),
    cache_write_per_mtok REAL NOT NULL CHECK (cache_write_per_mtok >= 0),
    cache_write_1h_per_mtok REAL,
    output_per_mtok REAL NOT NULL CHECK (output_per_mtok >= 0),
    long_context_threshold INTEGER,
    long_input_multiplier REAL NOT NULL DEFAULT 1,
    long_output_multiplier REAL NOT NULL DEFAULT 1,
    effective_from TEXT NOT NULL,
    effective_to TEXT,
    source_url TEXT NOT NULL,
    PRIMARY KEY (model_key, effective_from)
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_key TEXT NOT NULL,
    name TEXT NOT NULL,
    amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
    cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'annual')),
    start_date TEXT NOT NULL,
    end_date TEXT
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id TEXT PRIMARY KEY,
    tool_key TEXT NOT NULL,
    project TEXT,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    model_key TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS ingest_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'skipped', 'partial')),
    events_written INTEGER NOT NULL DEFAULT 0,
    error TEXT
);

CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_id ON ingest_runs(source, id);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_status_finished ON ingest_runs(status, finished_at);
CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs(started_at);

CREATE TABLE IF NOT EXISTS pricing_gaps (
    model_key TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    occurrences INTEGER NOT NULL DEFAULT 1,
    sample_raw_id TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS pricing_gap_events (
    raw_id TEXT PRIMARY KEY,
    model_key TEXT NOT NULL,
    source TEXT NOT NULL,
    occurred_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS provider_cost_buckets (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    starting_at TEXT NOT NULL,
    ending_at TEXT NOT NULL,
    project_id TEXT,
    line_item TEXT,
    model_key TEXT,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_provider_cost_buckets_time
ON provider_cost_buckets(starting_at, ending_at);

CREATE TABLE IF NOT EXISTS subscription_daily_costs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    subscription_id INTEGER NOT NULL,
    tool_key TEXT NOT NULL,
    date TEXT NOT NULL,
    cost_usd REAL NOT NULL CHECK (cost_usd >= 0),
    raw_id TEXT NOT NULL UNIQUE,
    FOREIGN KEY(subscription_id) REFERENCES subscriptions(id)
);

CREATE INDEX IF NOT EXISTS idx_subscription_daily_costs_date
ON subscription_daily_costs(date, tool_key);

CREATE TABLE IF NOT EXISTS unpriced_usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    session_id TEXT,
    project TEXT,
    input_tokens INTEGER NOT NULL,
    cached_input_tokens INTEGER NOT NULL,
    cache_write_tokens INTEGER NOT NULL,
    cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL,
    reasoning_tokens INTEGER,
    unclassified_tokens INTEGER NOT NULL DEFAULT 0,
    telemetry_complete INTEGER NOT NULL DEFAULT 1,
    cost_usd REAL,
    occurred_epoch_us INTEGER,
    raw_id TEXT NOT NULL UNIQUE,
    ingested_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_unpriced_usage_time
ON unpriced_usage_events(occurred_at, tool_key, model_key);

CREATE TABLE IF NOT EXISTS coverage_gap_events (
    raw_id TEXT PRIMARY KEY,
    source TEXT NOT NULL,
    tool_key TEXT NOT NULL,
    model_key TEXT NOT NULL,
    occurred_at TEXT NOT NULL,
    issue TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_coverage_gap_time
ON coverage_gap_events(occurred_at, tool_key, model_key);

CREATE TABLE IF NOT EXISTS opencode_session_progress (
    session_id TEXT NOT NULL,
    provider_id TEXT NOT NULL,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    cached_input_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    reasoning_tokens INTEGER,
    cost_usd REAL,
    model_id TEXT,
    observed_at TEXT NOT NULL,
    PRIMARY KEY (session_id, provider_id)
);
"""

ACTIVITY_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quotas (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    label TEXT NOT NULL,
    used REAL,
    allowance REAL,
    unit TEXT NOT NULL,
    pct REAL,
    resets_at TEXT,
    source TEXT NOT NULL,
    is_payg BOOLEAN CHECK (is_payg IS NULL OR is_payg IN (0, 1)),
    polled_at TEXT NOT NULL,
    UNIQUE(provider_key, limit_key, polled_at)
);

CREATE INDEX IF NOT EXISTS idx_quotas_provider ON quotas(provider_key, polled_at);
CREATE INDEX IF NOT EXISTS idx_quotas_lane_id ON quotas(provider_key, limit_key, id);

CREATE TABLE IF NOT EXISTS agent_runs (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    model_key TEXT,
    state TEXT NOT NULL,
    started_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_agent_runs_last_seen ON agent_runs(last_seen_at);
"""

PACE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS quota_pace_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    limit_key TEXT NOT NULL,
    pct REAL NOT NULL,
    resets_at TEXT,
    sampled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_quota_pace_samples_window
    ON quota_pace_samples(provider_key, limit_key, sampled_at);

CREATE TABLE IF NOT EXISTS balance_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    provider_key TEXT NOT NULL,
    source TEXT NOT NULL,
    usd REAL NOT NULL,
    sampled_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_balance_samples_source
    ON balance_samples(provider_key, source, sampled_at);
"""

ACTIVITY_SCHEMA_SQL += PACE_SCHEMA_SQL


@dataclass(frozen=True)
class UsageEvent:
    source: str
    tool_key: str
    model_key: str
    occurred_at: str
    session_id: str | None
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    cost_usd: float | None
    computed_cost_usd: float
    raw_id: str
    ingested_at: str
    is_exact: bool = False


@dataclass(frozen=True)
class ProviderCostBucket:
    source: str
    starting_at: str
    ending_at: str
    project_id: str | None
    line_item: str | None
    model_key: str | None
    cost_usd: float
    raw_id: str
    ingested_at: str


@dataclass(frozen=True)
class UnpricedUsageEvent:
    source: str
    tool_key: str
    model_key: str
    occurred_at: str
    session_id: str | None
    project: str | None
    input_tokens: int
    cached_input_tokens: int
    cache_write_tokens: int
    cache_write_1h_tokens: int
    output_tokens: int
    reasoning_tokens: int | None
    unclassified_tokens: int
    telemetry_complete: bool
    cost_usd: float | None
    raw_id: str
    ingested_at: str


def utc_now() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@contextmanager
def connect(path: Path) -> Iterator[sqlite3.Connection]:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=30)
    if os.name != "nt" and str(path) != ":memory:":
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA synchronous=NORMAL")
    connection.execute("PRAGMA foreign_keys=ON")
    try:
        yield connection
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()


def backup_database(src: Path, dst: Path) -> Path:
    """Copy ``src`` to ``dst`` using SQLite's Online Backup API.

    Safe while the source is in WAL mode: pages are read from the live
    connection, so the destination is a consistent standalone database
    without copying ``-wal``/``-shm`` sidecars. Not called by initialize.
    """
    source_path = Path(src)
    dest_path = Path(dst)
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(source_path, timeout=30)) as source:
        with closing(sqlite3.connect(dest_path, timeout=30)) as dest:
            source.backup(dest)
    return dest_path


def rebuild_subscriptions_for_quarterly(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='subscriptions'"
    ).fetchone()
    if row is None or "quarterly" in (row[0] or ""):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE IF EXISTS subscriptions_rebuild")
        connection.execute(
            """
            CREATE TABLE subscriptions_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                tool_key TEXT NOT NULL,
                name TEXT NOT NULL,
                amount_usd REAL NOT NULL CHECK (amount_usd >= 0),
                cadence TEXT NOT NULL CHECK (cadence IN ('monthly', 'quarterly', 'annual')),
                start_date TEXT NOT NULL,
                end_date TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO subscriptions_rebuild(id, tool_key, name, amount_usd, cadence, start_date, end_date)
            SELECT id, tool_key, name, amount_usd, cadence, start_date, end_date FROM subscriptions
            """
        )
        connection.execute("DROP TABLE subscriptions")
        connection.execute("ALTER TABLE subscriptions_rebuild RENAME TO subscriptions")
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError("subscriptions rebuild produced foreign key violations")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def rebuild_ingest_runs_for_partial(connection: sqlite3.Connection) -> None:
    row = connection.execute(
        "SELECT sql FROM sqlite_master WHERE type='table' AND name='ingest_runs'"
    ).fetchone()
    if row is None or "partial" in (row[0] or ""):
        return
    connection.commit()
    connection.execute("PRAGMA foreign_keys=OFF")
    connection.execute("BEGIN IMMEDIATE")
    try:
        connection.execute("DROP TABLE IF EXISTS ingest_runs_rebuild")
        connection.execute(
            """
            CREATE TABLE ingest_runs_rebuild (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                status TEXT NOT NULL CHECK (status IN ('running', 'success', 'failed', 'skipped', 'partial')),
                events_written INTEGER NOT NULL DEFAULT 0,
                error TEXT
            )
            """
        )
        connection.execute(
            """
            INSERT INTO ingest_runs_rebuild(id, source, started_at, finished_at, status, events_written, error)
            SELECT id, source, started_at, finished_at, status, events_written, error FROM ingest_runs
            """
        )
        connection.execute("DROP TABLE ingest_runs")
        connection.execute("ALTER TABLE ingest_runs_rebuild RENAME TO ingest_runs")
        connection.execute("COMMIT")
    except Exception:
        connection.execute("ROLLBACK")
        raise
    finally:
        connection.execute("PRAGMA foreign_keys=ON")


def ensure_quota_pace_table(connection: sqlite3.Connection) -> None:
    connection.executescript(PACE_SCHEMA_SQL)


def ensure_activity_table_shapes(connection: sqlite3.Connection) -> None:
    quota_columns = {row[1] for row in connection.execute("PRAGMA table_info('quotas')")}
    agent_columns = {row[1] for row in connection.execute("PRAGMA table_info('agent_runs')")}
    stale_quotas = bool(quota_columns) and "provider_key" not in quota_columns
    stale_agent_runs = bool(agent_columns) and "run_id" in agent_columns
    if stale_quotas or stale_agent_runs:
        connection.commit()
        connection.execute("PRAGMA foreign_keys=OFF")
        connection.execute("BEGIN IMMEDIATE")
        try:
            if stale_quotas:
                connection.execute("DROP TABLE quotas")
            if stale_agent_runs:
                connection.execute("DROP TABLE agent_runs")
            connection.execute("COMMIT")
        except Exception:
            connection.execute("ROLLBACK")
            raise
        finally:
            connection.execute("PRAGMA foreign_keys=ON")
    connection.executescript(ACTIVITY_SCHEMA_SQL)


REQUIRED_SCHEMA_OBJECTS = frozenset(
    {
        "app_meta",
        "usage_events",
        "unpriced_usage_events",
        "model_prices",
        "subscriptions",
        "subscription_plans",
        "subscription_mutations",
        "subscription_daily_costs",
        "sessions",
        "ingest_runs",
        "pricing_gaps",
        "pricing_gap_events",
        "provider_cost_buckets",
        "coverage_gap_events",
        "opencode_session_progress",
        "quotas",
        "quota_pace_samples",
        "balance_samples",
        "agent_runs",
        "idx_quota_pace_samples_window",
        "idx_balance_samples_source",
        "idx_ingest_runs_source_id",
        "idx_ingest_runs_status_finished",
        "idx_ingest_runs_started_at",
        "idx_usage_events_epoch",
        "idx_unpriced_usage_epoch",
    }
)

INGEST_RUN_RETENTION_HOURS = 24
INGEST_RUN_KEEP_PER_SOURCE = 200


def schema_is_current(connection: sqlite3.Connection) -> bool:
    """True when the database already carries the current schema and seeds.

    Every scheduler job and poller calls ``initialize`` defensively. Running
    the full migration script each time cost a write transaction every few
    seconds (the schema-version upsert) and contended with ingest writers.
    A current database is recognised by its version stamp, seed stamp and the
    presence of every required table and index, so a partially migrated or
    externally modified file still takes the full path.
    """
    try:
        rows = connection.execute("SELECT key, value FROM app_meta").fetchall()
    except sqlite3.OperationalError:
        return False
    meta = {row[0]: row[1] for row in rows}
    if meta.get("schema_version") != str(SCHEMA_VERSION):
        return False
    if SUBSCRIPTION_SEED_VERSION_KEY not in meta:
        return False
    present = {
        row[0]
        for row in connection.execute(
            "SELECT name FROM sqlite_master WHERE type IN ('table', 'index')"
        )
    }
    return REQUIRED_SCHEMA_OBJECTS <= present


def prune_ingest_runs(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    retention_hours: int = INGEST_RUN_RETENTION_HOURS,
    keep_per_source: int = INGEST_RUN_KEEP_PER_SOURCE,
) -> int:
    """Drop finished ingest-run history older than the retention window.

    The 15-second local cadence writes five run rows per cycle (about 29k per
    day). Nothing reads history beyond the latest run per source and the
    latest success, so old rows only slowed those lookups and grew the file.
    The newest ``keep_per_source`` rows of every source are always kept, so
    the latest status and last success survive even for dormant sources.
    """
    stamp = now or utc_now()
    cutoff = (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(hours=retention_hours)
    ).isoformat().replace("+00:00", "Z")
    table = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='ingest_runs'"
    ).fetchone()
    if table is None:
        return 0
    cursor = connection.execute(
        """
        DELETE FROM ingest_runs
        WHERE finished_at IS NOT NULL
          AND started_at < ?
          AND id NOT IN (
              SELECT r.id FROM ingest_runs AS r
              WHERE r.source = ingest_runs.source
              ORDER BY r.id DESC LIMIT ?
          )
          AND id NOT IN (
              SELECT MAX(id) FROM ingest_runs
              WHERE status IN ('success', 'partial') GROUP BY source
          )
        """,
        (cutoff, int(keep_per_source)),
    )
    return int(cursor.rowcount or 0)


QUOTA_RETENTION_DAYS = 30
QUOTA_KEEP_PER_LANE = 5


def prune_quotas(
    connection: sqlite3.Connection,
    *,
    now: str | None = None,
    retention_days: int = QUOTA_RETENTION_DAYS,
    keep_per_lane: int = QUOTA_KEEP_PER_LANE,
) -> int:
    """Drop quota snapshots and pace samples older than ``retention_days``.

    Always retains the newest ``keep_per_lane`` snapshots per ``(provider_key,
    limit_key, sync-origin)`` so dormant or synced providers keep their last
    known status.
    """
    stamp = now or utc_now()
    cutoff = (
        datetime.fromisoformat(stamp.replace("Z", "+00:00")) - timedelta(days=retention_days)
    ).isoformat().replace("+00:00", "Z")
    removed = 0
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quotas'"
    ).fetchone():
        connection.execute(
            "CREATE INDEX IF NOT EXISTS idx_quotas_lane_id ON quotas(provider_key, limit_key, id)"
        )
        cur = connection.execute(
            """
            DELETE FROM quotas
            WHERE polled_at < ?
              AND id NOT IN (
                  SELECT q.id FROM quotas AS q
                  WHERE q.provider_key = quotas.provider_key
                    AND q.limit_key = quotas.limit_key
                    AND (CASE WHEN q.source LIKE 'sync:%' THEN q.source ELSE '' END)
                        = (CASE WHEN quotas.source LIKE 'sync:%' THEN quotas.source ELSE '' END)
                  ORDER BY q.id DESC LIMIT ?
              )
            """,
            (cutoff, int(keep_per_lane)),
        )
        removed += int(cur.rowcount or 0)
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='quota_pace_samples'"
    ).fetchone():
        cur_pace = connection.execute(
            "DELETE FROM quota_pace_samples WHERE sampled_at < ?",
            (cutoff,),
        )
        removed += int(cur_pace.rowcount or 0)
    return removed


def prune_usage_changes(
    connection: sqlite3.Connection,
    *,
    acknowledged_seq: int | None = None,
) -> int:
    """Compact ``usage_changes`` by removing Hub-acknowledged rows and superseded duplicates.

    - When one or more ``sync.cursor.v1:*`` entries exist in ``app_meta``, any
      journal entry ``seq <= min(cursor.change_seq)`` has already been received
      by every paired Hub and is deleted.
    - Superseded earlier entries for the same ``raw_id`` are also compacted to
      their latest ``seq``, matching ``export_sync_batch``'s identity collapse.
    """
    if not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='usage_changes'"
    ).fetchone():
        return 0
    removed = 0
    safe_seq: int | None = None
    if connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'"
    ).fetchone():
        rows = connection.execute(
            "SELECT value FROM app_meta WHERE key >= 'sync.cursor.v1:' AND key < 'sync.cursor.v1;'"
        ).fetchall()
        seqs: list[int] = []
        for (raw_val,) in rows:
            try:
                parsed = json.loads(raw_val)
                seqs.append(int(parsed.get("change_seq") or 0))
            except (ValueError, TypeError, AttributeError):
                seqs.append(0)
        if seqs:
            safe_seq = min(seqs)
    if safe_seq is None and acknowledged_seq is not None and acknowledged_seq > 0:
        safe_seq = int(acknowledged_seq)
    if safe_seq is not None and safe_seq > 0:
        cur = connection.execute("DELETE FROM usage_changes WHERE seq <= ?", (safe_seq,))
        removed += int(cur.rowcount or 0)
    cur_dedup = connection.execute(
        """
        DELETE FROM usage_changes
        WHERE seq NOT IN (
            SELECT MAX(seq) FROM usage_changes GROUP BY raw_id
        )
        """
    )
    removed += int(cur_dedup.rowcount or 0)
    return removed


_USAGE_CACHE_HOUR_US = 3_600_000_000


def _usage_cache_bucket_sql(reference: str) -> str:
    # SQLite integer division truncates toward zero; keep pre-epoch hours
    # aligned with Python's floor division as well.
    epoch = f"{reference}.occurred_epoch_us" if reference else "occurred_epoch_us"
    return (
        f"CASE WHEN {epoch} >= 0 THEN {epoch} / {_USAGE_CACHE_HOUR_US} "
        f"ELSE ({epoch} - {_USAGE_CACHE_HOUR_US - 1}) / {_USAGE_CACHE_HOUR_US} END"
    )


def ensure_usage_cache_revisions(path: Path) -> None:
    """Track changed usage hours independently of the compacted sync journal.

    Revisions include local and imported rows, and persist deletion
    tombstones. A recent insert must not invalidate cached settled history.
    The additive table, triggers, seed and readiness marker commit together.
    """
    marker = "cache.usage-revisions.v1"
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        connection.execute(
            "CREATE TABLE IF NOT EXISTS usage_cache_revisions ("
            "bucket INTEGER PRIMARY KEY, revision INTEGER NOT NULL) WITHOUT ROWID"
        )

        def touch(reference: str, condition: str) -> str:
            return (
                "INSERT INTO usage_cache_revisions(bucket, revision) "
                f"SELECT {_usage_cache_bucket_sql(reference)}, 1 WHERE {condition} "
                "ON CONFLICT(bucket) DO UPDATE SET revision=usage_cache_revisions.revision+1;"
            )

        for table in ("usage_events", "unpriced_usage_events"):
            for action, reference in (("INSERT", "NEW"), ("DELETE", "OLD")):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS cache_{table}_{action.lower()} "
                    f"AFTER {action} ON {table} "
                    f"BEGIN {touch(reference, reference + '.occurred_epoch_us IS NOT NULL')} END"
                )
            new_bucket = _usage_cache_bucket_sql("NEW")
            old_bucket = _usage_cache_bucket_sql("OLD")
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS cache_{table}_update AFTER UPDATE ON {table} "
                f"BEGIN {touch('OLD', 'OLD.occurred_epoch_us IS NOT NULL')} "
                + touch(
                    "NEW",
                    "NEW.occurred_epoch_us IS NOT NULL AND "
                    f"(OLD.occurred_epoch_us IS NULL OR ({new_bucket}) != ({old_bucket}))",
                )
                + " END"
            )
            connection.execute(
                "INSERT OR IGNORE INTO usage_cache_revisions(bucket, revision) "
                f"SELECT {_usage_cache_bucket_sql('')}, 1 FROM {table} "
                "WHERE occurred_epoch_us IS NOT NULL GROUP BY 1"
            )
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?, '1')", (marker,))


def usage_cache_revision(
    connection: sqlite3.Connection, start: datetime, end: datetime
) -> tuple[tuple[int, int], ...]:
    """Revisions for hours intersecting half-open ``[start, end)``.

    Model/tool/machine corrections can leave token sums and ingest stamps
    unchanged. Hour-scoped revisions catch them without a history scan or
    invalidating settled rows for activity in the recent live slice.
    """
    from spend_app.timeutil import epoch_micros

    start_us, end_us = epoch_micros(start), epoch_micros(end)
    if end_us <= start_us:
        return ()
    return tuple(
        tuple(row)
        for row in connection.execute(
            "SELECT bucket, revision FROM usage_cache_revisions "
            "WHERE bucket >= ? AND bucket <= ? ORDER BY bucket",
            (start_us // _USAGE_CACHE_HOUR_US, (end_us - 1) // _USAGE_CACHE_HOUR_US),
        )
    )


def initialize(path: Path) -> None:
    from spend_app.subscription_schema import upgrade_database
    upgrade_database(path, _initialize_base)
    ensure_usage_change_journal(path)
    ensure_usage_cache_revisions(path)
    migrate_usage_keys(path)
    retire_cursor_usage(path)
    dedupe_grok_calls(path)
    retire_muse_rollup_events(path)


def ensure_usage_change_journal(path: Path) -> None:
    """Capture every local usage mutation, including SQL-based parser repairs.

    Trigger writes commit or roll back with their event mutation. The journal
    stores identities, not another copy of usage payloads; exporters resolve
    the latest state in one read transaction. Imported machine rows never echo.
    Existing rows seed the first sequence so legacy ID cursors can be upgraded.
    """
    marker = "usage_changes.initialized.v1"
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        connection.execute(
            "CREATE TABLE IF NOT EXISTS usage_changes ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, raw_id TEXT NOT NULL)"
        )
        for table in ("usage_events", "unpriced_usage_events"):
            for action, reference in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
                connection.execute(
                    f"CREATE TRIGGER IF NOT EXISTS journal_{table}_{action.lower()} "
                    f"AFTER {action} ON {table} WHEN {reference}.raw_id NOT LIKE 'm:%' "
                    f"BEGIN INSERT INTO usage_changes(raw_id) VALUES({reference}.raw_id); END"
                )
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS journal_{table}_rename AFTER UPDATE OF raw_id ON {table} "
                "WHEN OLD.raw_id != NEW.raw_id AND OLD.raw_id NOT LIKE 'm:%' "
                "BEGIN INSERT INTO usage_changes(raw_id) VALUES(OLD.raw_id); END"
            )
            connection.execute(
                f"INSERT INTO usage_changes(raw_id) SELECT raw_id FROM {table} "
                "WHERE raw_id NOT LIKE 'm:%' ORDER BY id"
            )
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?, '1')", (marker,))


# Event identity and alias records (one or more per usage event) live in their
# own compact table: a 32-byte digest instead of a 90-character text key that
# app_meta stored twice (row and index), and large values compressed. Keys keep
# their app_meta spelling at the call sites; usage_repair.read_meta/write_meta
# route them here. Records still in app_meta move over in small background
# steps (migrate_usage_keys_step), and reads fall back to app_meta until then.
USAGE_KEYS_SQL = """
CREATE TABLE IF NOT EXISTS usage_keys (
    kind INTEGER NOT NULL,
    source TEXT NOT NULL,
    digest BLOB NOT NULL,
    value NOT NULL,
    PRIMARY KEY (kind, source, digest)
) WITHOUT ROWID;
"""
USAGE_KEY_PREFIXES = {"usage.alias.v1:": 0, "usage.identity.v1:": 1}
USAGE_KEYS_MARKER = "usage_keys.migrated.v1"
USAGE_KEYS_STEP_ROWS = 10000
# JSON at least this long is stored zlib-compressed (identity records compress
# about 2.7x; short alias values would not shrink).
USAGE_VALUE_COMPRESS_AT = 160
_USAGE_KEYS_READY: set[str] = set()
_USAGE_KEYS_DONE: set[str] = set()
_USAGE_KEYS_LOCK = threading.Lock()


def split_usage_key(key: str) -> tuple[int, str, bytes] | None:
    """``usage.alias.v1:<source>:<sha256 hex>`` -> (kind, source, digest), else None."""
    for prefix, kind in USAGE_KEY_PREFIXES.items():
        if key.startswith(prefix):
            source, _, digest = key[len(prefix):].partition(":")
            if len(digest) == 64:
                try:
                    return kind, source, bytes.fromhex(digest)
                except ValueError:
                    pass
            return kind, source, digest.encode("utf-8")
    return None


def encode_usage_value(value) -> str | bytes:
    import zlib

    text = json.dumps(value, sort_keys=True, separators=(",", ":"))
    return zlib.compress(text.encode("utf-8"), 6) if len(text) >= USAGE_VALUE_COMPRESS_AT else text


def decode_usage_value(stored):
    import zlib

    if isinstance(stored, bytes):
        stored = zlib.decompress(stored).decode("utf-8")
    return json.loads(stored)


def migrate_usage_keys(path: Path) -> None:
    """Create the identity table once per database and process (cheap)."""
    marker = str(Path(path).resolve())
    if marker in _USAGE_KEYS_READY:
        return
    with _USAGE_KEYS_LOCK:
        if marker in _USAGE_KEYS_READY:
            return
        with connect(Path(path)) as connection:
            connection.executescript(USAGE_KEYS_SQL)
        _USAGE_KEYS_READY.add(marker)


def migrate_usage_keys_step(path: Path, limit: int = USAGE_KEYS_STEP_ROWS) -> int:
    """Move up to ``limit`` identity records out of app_meta in one short transaction.

    Returns how many moved; 0 once nothing is left (then the marker is written
    and later calls are free). A record already rewritten in usage_keys keeps
    its newer value.
    """
    path = Path(path)
    marker = str(path.resolve())
    if marker in _USAGE_KEYS_DONE or not path.exists():
        return 0
    moved = 0
    with connect(path) as connection:
        if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='app_meta'").fetchone():
            return 0  # not initialized yet; nothing to move
        connection.executescript(USAGE_KEYS_SQL)
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (USAGE_KEYS_MARKER,)).fetchone():
            _USAGE_KEYS_DONE.add(marker)
            return 0
        connection.execute("BEGIN IMMEDIATE")
        for prefix in USAGE_KEY_PREFIXES:
            # A key range (not LIKE) so the primary-key index is used.
            low, high = prefix, prefix[:-1] + chr(ord(prefix[-1]) + 1)
            batch = connection.execute(
                "SELECT key, value FROM app_meta WHERE key >= ? AND key < ? ORDER BY key LIMIT ?",
                (low, high, limit - moved),
            ).fetchall()
            if not batch:
                continue
            rows = sorted((*split_usage_key(key), encode_usage_value(json.loads(value))) for key, value in batch)
            connection.executemany(
                "INSERT OR IGNORE INTO usage_keys(kind, source, digest, value) VALUES(?,?,?,?)", rows)
            connection.execute("DELETE FROM app_meta WHERE key >= ? AND key <= ?", (low, batch[-1][0]))
            moved += len(batch)
            if moved >= limit:
                break
        if moved == 0:
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO NOTHING",
                (USAGE_KEYS_MARKER, json.dumps({"at": utc_now()})),
            )
            _USAGE_KEYS_DONE.add(marker)
    return moved


# Cursor usage duplicates SuperGrok usage, so BURNRATE neither collects nor
# counts it. Sources whose old collection runs and rows are retired:
RETIRED_SOURCES = frozenset({"cursor_local", "cursor_usage_service", "cursor_admin", "cursor_csv"})
RETIRED_USAGE_KEY = "retired_usage.cursor.v1"
_RETIRED_USAGE_CHECKED: set[str] = set()
_RETIRED_USAGE_LOCK = threading.Lock()


def retire_cursor_usage(path: Path) -> int:
    """Remove recorded Cursor usage once per database, after a verified backup.

    Runs on every database (local or Hub, including rows synced from other
    computers) and records a marker so later calls are a single lookup. The
    schema version is unchanged, so an older BURNRATE can still open the file.
    """
    path = Path(path)
    marker = str(path.resolve())
    if marker in _RETIRED_USAGE_CHECKED:
        return 0
    with _RETIRED_USAGE_LOCK:
        return _retire_cursor_usage(path, marker)


def _retire_cursor_usage(path: Path, marker: str) -> int:
    if marker in _RETIRED_USAGE_CHECKED:
        return 0
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (RETIRED_USAGE_KEY,)).fetchone():
            _RETIRED_USAGE_CHECKED.add(marker)
            return 0
        recorded = connection.execute(
            "SELECT EXISTS(SELECT 1 FROM usage_events WHERE tool_key='cursor') "
            "OR EXISTS(SELECT 1 FROM unpriced_usage_events WHERE tool_key='cursor')"
        ).fetchone()[0]
    if recorded:
        target = path.parent / "backups" / (
            "pre-cursor-removal-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".db"
        )
        backup_database(path, target)
        with closing(sqlite3.connect(target)) as backup:
            if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                raise RuntimeError("Backup before removing Cursor usage failed; nothing was removed")
    sources = sorted(RETIRED_SOURCES)
    marks = ",".join("?" for _ in sources)
    removed = 0
    with connect(path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("usage_events", "unpriced_usage_events"):
            removed += connection.execute(f"DELETE FROM {table} WHERE tool_key='cursor'").rowcount
        connection.execute("DELETE FROM sessions WHERE tool_key='cursor'")
        connection.execute("DELETE FROM coverage_gap_events WHERE tool_key='cursor'")
        connection.execute("DELETE FROM pricing_gaps WHERE model_key LIKE 'cursor:%'")
        connection.execute("DELETE FROM pricing_gap_events WHERE model_key LIKE 'cursor:%'")
        connection.execute(f"DELETE FROM provider_cost_buckets WHERE source IN ({marks})", sources)
        connection.execute(f"DELETE FROM ingest_runs WHERE source IN ({marks})", sources)
        connection.execute("DELETE FROM quotas WHERE provider_key='cursor'")
        connection.execute("DELETE FROM quota_pace_samples WHERE provider_key='cursor'")
        connection.execute("DELETE FROM agent_runs WHERE model_key LIKE 'cursor:%'")
        # Ingest identity bookkeeping for the retired sources.
        connection.execute(
            "DELETE FROM app_meta WHERE key LIKE 'usage.alias.v1:cursor%' OR key LIKE 'usage.identity.v1:cursor%'"
        )
        connection.execute(
            "DELETE FROM usage_keys WHERE source LIKE 'cursor%'"
        )
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (RETIRED_USAGE_KEY, json.dumps({"removedEvents": removed, "at": utc_now()})),
        )
    _RETIRED_USAGE_CHECKED.add(marker)
    return removed


# Grok Build usage used to be stored twice: once per model call (unified.jsonl)
# and once per completed turn (updates.jsonl). The repair keeps the turn and
# drops the calls it covers, on every database including a hub.
GROK_DEDUPE_KEY = "repair.grok_call_dedupe.v1"
_GROK_DEDUPE_CHECKED: set[str] = set()
_GROK_DEDUPE_LOCK = threading.Lock()


def dedupe_grok_calls(path: Path) -> int:
    """Remove per-call Grok rows a stored turn already counts, once per database.

    Takes a verified backup first when there is anything to remove, and
    records a marker so later calls are a single lookup.
    """
    path = Path(path)
    marker = str(path.resolve())
    if marker in _GROK_DEDUPE_CHECKED:
        return 0
    with _GROK_DEDUPE_LOCK:
        if marker in _GROK_DEDUPE_CHECKED:
            return 0
        with connect(path) as connection:
            if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (GROK_DEDUPE_KEY,)).fetchone():
                _GROK_DEDUPE_CHECKED.add(marker)
                return 0
            recorded = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM unpriced_usage_events WHERE tool_key='grok') "
                "OR EXISTS(SELECT 1 FROM usage_events WHERE tool_key='grok')"
            ).fetchone()[0]
        if recorded:
            target = path.parent / "backups" / (
                "pre-grok-dedupe-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".db"
            )
            backup_database(path, target)
            with closing(sqlite3.connect(target)) as backup:
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup before removing duplicate Grok usage failed; nothing was removed")
        from spend_app.adapters.grok_native import drop_covered_calls

        with connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            removed = drop_covered_calls(connection) if recorded else 0
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (GROK_DEDUPE_KEY, json.dumps({"removedEvents": removed, "at": utc_now()})),
            )
        _GROK_DEDUPE_CHECKED.add(marker)
        return removed


MUSE_ROLLUP_CLEANUP_KEY = "repair.muse_rollup_cleanup.v1"
_MUSE_ROLLUP_CHECKED: set[str] = set()
_MUSE_ROLLUP_LOCK = threading.Lock()


def retire_muse_rollup_events(path: Path) -> int:
    """Remove non-turn ``muse:unknown`` rollup rows ingested by older Muse parsers.

    Muse Code ``runtime.session`` checkpoints and child-lifecycle summaries
    carried cumulative usage without a model identifier. Removing those stale
    ``muse:unknown`` rows once per database (local and Hub) clears false
    ``Partial`` badges while ``muse-session-4`` re-parses real turns in place.
    """
    path = Path(path)
    marker = str(path.resolve())
    if marker in _MUSE_ROLLUP_CHECKED:
        return 0
    with _MUSE_ROLLUP_LOCK:
        if marker in _MUSE_ROLLUP_CHECKED:
            return 0
        with connect(path) as connection:
            if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (MUSE_ROLLUP_CLEANUP_KEY,)).fetchone():
                _MUSE_ROLLUP_CHECKED.add(marker)
                return 0
            recorded = connection.execute(
                "SELECT EXISTS(SELECT 1 FROM unpriced_usage_events "
                "WHERE tool_key='muse' AND model_key='muse:unknown' AND (source='muse_local' OR source LIKE 'sync:%'))"
            ).fetchone()[0]
        if recorded:
            target = path.parent / "backups" / (
                "pre-muse-rollup-cleanup-" + datetime.now(UTC).strftime("%Y%m%dT%H%M%S") + ".db"
            )
            backup_database(path, target)
            with closing(sqlite3.connect(target)) as backup:
                if backup.execute("PRAGMA integrity_check").fetchone()[0] != "ok":
                    raise RuntimeError("Backup before removing Muse rollup events failed; nothing was removed")
        removed = 0
        with connect(path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if recorded:
                removed = connection.execute(
                    "DELETE FROM unpriced_usage_events "
                    "WHERE tool_key='muse' AND model_key='muse:unknown' AND (source='muse_local' OR source LIKE 'sync:%')"
                ).rowcount
                connection.execute(
                    "DELETE FROM coverage_gap_events "
                    "WHERE tool_key='muse' AND model_key='muse:unknown' AND (source='muse_local' OR source LIKE 'sync:%')"
                )
                connection.execute(
                    "DELETE FROM pricing_gap_events "
                    "WHERE model_key='muse:unknown' AND (source='muse_local' OR source LIKE 'sync:%')"
                )
                connection.execute(
                    "DELETE FROM pricing_gaps "
                    "WHERE model_key='muse:unknown' "
                    "AND NOT EXISTS (SELECT 1 FROM unpriced_usage_events WHERE model_key='muse:unknown')"
                )
            connection.execute(
                "INSERT INTO app_meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (MUSE_ROLLUP_CLEANUP_KEY, json.dumps({"removedEvents": removed, "at": utc_now()})),
            )
        _MUSE_ROLLUP_CHECKED.add(marker)
        return removed


def _initialize_base(path: Path) -> None:
    with connect(path) as connection:
        if schema_is_current(connection):
            # Cheap row-level migrations stay on every call: they only touch
            # the handful of subscription rows and write nothing when there
            # is nothing legacy to rename.
            migrate_legacy_zai_seed(connection)
            migrate_legacy_subscription_identities(connection)
            seed_subscriptions(connection)
            return
        try:
            previous_row = connection.execute(
                "SELECT value FROM app_meta WHERE key='schema_version'"
            ).fetchone()
            previous_version = int(previous_row[0]) if previous_row else 0
        except sqlite3.OperationalError:
            previous_version = 0
        connection.executescript(SCHEMA_SQL)
        ensure_activity_table_shapes(connection)
        ensure_quota_pace_table(connection)
        rebuild_subscriptions_for_quarterly(connection)
        rebuild_ingest_runs_for_partial(connection)
        # The rebuild above recreates ingest_runs without its indexes.
        connection.executescript(
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_source_id ON ingest_runs(source, id);"
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_status_finished ON ingest_runs(status, finished_at);"
            "CREATE INDEX IF NOT EXISTS idx_ingest_runs_started_at ON ingest_runs(started_at);"
        )
        usage_columns = {row[1] for row in connection.execute("PRAGMA table_info('usage_events')")}
        if "cache_write_1h_tokens" not in usage_columns:
            connection.execute(
                "ALTER TABLE usage_events ADD COLUMN cache_write_1h_tokens INTEGER NOT NULL DEFAULT 0"
            )
        price_columns = {row[1] for row in connection.execute("PRAGMA table_info('model_prices')")}
        if "cache_write_1h_per_mtok" not in price_columns:
            connection.execute("ALTER TABLE model_prices ADD COLUMN cache_write_1h_per_mtok REAL")
        unpriced_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('unpriced_usage_events')")
        }
        if "unclassified_tokens" not in unpriced_columns:
            connection.execute(
                "ALTER TABLE unpriced_usage_events ADD COLUMN unclassified_tokens INTEGER NOT NULL DEFAULT 0"
            )
        if "telemetry_complete" not in unpriced_columns:
            connection.execute(
                "ALTER TABLE unpriced_usage_events ADD COLUMN telemetry_complete INTEGER NOT NULL DEFAULT 1"
            )
        if "is_exact" not in usage_columns:
            connection.execute(
                "ALTER TABLE usage_events ADD COLUMN is_exact INTEGER NOT NULL DEFAULT 0"
            )
            placeholders = ",".join("?" for _ in EXACT_USAGE_SOURCES)
            connection.execute(
                f"UPDATE usage_events SET is_exact = CASE WHEN source IN ({placeholders}) "
                "THEN 1 ELSE 0 END",
                EXACT_USAGE_SOURCES,
            )
        if previous_version < 5:
            connection.execute("DELETE FROM pricing_gaps")
            connection.execute("DELETE FROM pricing_gap_events")
        migrate_release_a(connection)
        backfill_event_epochs(connection)
        migrate_legacy_zai_seed(connection)
        migrate_legacy_subscription_identities(connection)
        seed_subscriptions(connection)
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES('schema_version', ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (str(SCHEMA_VERSION),),
        )


def _same_content(existing: sqlite3.Row | None, values: dict) -> bool:
    """True when the stored row already carries these values (ignoring ingested_at)."""
    if existing is None:
        return False
    for column, value in values.items():
        if column == "ingested_at":
            continue
        stored = existing[column]
        if isinstance(value, bool):
            value = int(value)
        if stored == value:
            continue
        if (
            isinstance(stored, (int, float))
            and isinstance(value, (int, float))
            and float(stored) == float(value)
        ):
            continue
        return False
    return True


def _event_epoch(value: str | None) -> int | None:
    """Canonical integer epoch microseconds for an event timestamp, or None.

    Query ordering and window filtering use this fixed-width column: ISO TEXT
    ordering silently drops a within-second event when source precision is
    mixed. Rows whose timestamp cannot be parsed stay queryable by text but
    fall outside epoch windows (they were unplaceable anyway).
    """
    if value is None:
        return None
    from spend_app.timeutil import epoch_micros, parse_utc

    parsed = parse_utc(value)
    return None if parsed is None else epoch_micros(parsed)


def migrate_release_a(connection: sqlite3.Connection) -> None:
    """Schema 11: canonical event epoch column (backfilled) and progress state.

    Additive only: existing ids, ISO timestamps and Decimal-backed costs are
    untouched, the backfill is derivable (idempotent), and a failure rolls
    back with the caller's transaction so the verified old database remains.
    """
    for table in ("usage_events", "unpriced_usage_events"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")}
        if not columns:
            continue
        if "occurred_epoch_us" not in columns:
            connection.execute(f"ALTER TABLE {table} ADD COLUMN occurred_epoch_us INTEGER")
    connection.executescript(
        """
        CREATE INDEX IF NOT EXISTS idx_usage_events_epoch ON usage_events(occurred_epoch_us);
        CREATE INDEX IF NOT EXISTS idx_unpriced_usage_epoch ON unpriced_usage_events(occurred_epoch_us);
        CREATE TABLE IF NOT EXISTS opencode_session_progress (
            session_id TEXT NOT NULL,
            provider_id TEXT NOT NULL,
            input_tokens INTEGER NOT NULL DEFAULT 0,
            cached_input_tokens INTEGER NOT NULL DEFAULT 0,
            cache_write_tokens INTEGER NOT NULL DEFAULT 0,
            output_tokens INTEGER NOT NULL DEFAULT 0,
            reasoning_tokens INTEGER,
            cost_usd REAL,
            model_id TEXT,
            observed_at TEXT NOT NULL,
            PRIMARY KEY (session_id, provider_id)
        );
        """
    )


def backfill_event_epochs(connection: sqlite3.Connection, *, batch_size: int = 5000) -> int:
    """Populate occurred_epoch_us from the ISO timestamps; idempotent.

    Returns the number of rows updated. Safe to run repeatedly: only rows
    with a NULL epoch are visited, and repeated runs after an interrupted
    upgrade finish the remaining rows.
    """
    if batch_size < 1:
        raise ValueError("batch_size must be positive")
    updated = 0
    invalid = {}
    for table in ("usage_events", "unpriced_usage_events"):
        columns = {row[1] for row in connection.execute(f"PRAGMA table_info('{table}')")}
        if "occurred_epoch_us" not in columns:
            continue
        last_id = 0
        invalid_count = 0
        while True:
            rows = connection.execute(
                f"SELECT id, occurred_at FROM {table} WHERE occurred_epoch_us IS NULL AND id>? ORDER BY id LIMIT ?",
                (last_id, batch_size),
            ).fetchall()
            if not rows:
                break
            for row in rows:
                last_id = row["id"]
                epoch = _event_epoch(row["occurred_at"])
                if epoch is None:
                    invalid_count += 1
                    continue
                connection.execute(
                    f"UPDATE {table} SET occurred_epoch_us=? WHERE id=?",
                    (epoch, row["id"]),
                )
                updated += 1
        if invalid_count:
            invalid[table] = invalid_count
    # Keep a bounded diagnostic, without retaining bad source text. Some unit
    # callers use minimal in-memory tables with no app_meta.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE name='app_meta' AND type='table'").fetchone():
        if invalid:
            connection.execute(
                "INSERT INTO app_meta(key,value) VALUES('migration.invalid_event_timestamps', ?) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                (json.dumps(invalid, sort_keys=True),),
            )
        else:
            connection.execute("DELETE FROM app_meta WHERE key='migration.invalid_event_timestamps'")
    return updated


def upsert_usage_event(connection: sqlite3.Connection, event: UsageEvent) -> bool:
    values = asdict(event)
    values["occurred_epoch_us"] = _event_epoch(event.occurred_at)
    columns = tuple(values)
    existing = connection.execute(
        f"SELECT {','.join(columns)} FROM usage_events WHERE raw_id = ?", (event.raw_id,)
    ).fetchone()
    existed = existing is not None
    if _same_content(existing, values):
        # Read-only local stores (OpenCode, Traycer) are re-read every
        # cycle; rewriting unchanged rows only churned the WAL and made
        # ingested_at meaningless as a change marker.
        connection.execute("DELETE FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,))
        return False
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO usage_events({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    connection.execute("DELETE FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,))
    return not existed


def upsert_cost_bucket(connection: sqlite3.Connection, bucket: ProviderCostBucket) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM provider_cost_buckets WHERE raw_id = ?", (bucket.raw_id,)
    ).fetchone() is not None
    values = asdict(bucket)
    columns = tuple(values)
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO provider_cost_buckets({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    return not existed


def upsert_unpriced_event(connection: sqlite3.Connection, event: UnpricedUsageEvent) -> bool:
    values = asdict(event)
    values["occurred_epoch_us"] = _event_epoch(event.occurred_at)
    columns = tuple(values)
    existing = connection.execute(
        f"SELECT {','.join(columns)} FROM unpriced_usage_events WHERE raw_id=?", (event.raw_id,)
    ).fetchone()
    existed = existing is not None
    if _same_content(existing, values):
        connection.execute("DELETE FROM usage_events WHERE raw_id=?", (event.raw_id,))
        return False
    placeholders = ",".join("?" for _ in columns)
    updates = ",".join(f"{column}=excluded.{column}" for column in columns if column != "raw_id")
    connection.execute(
        f"INSERT INTO unpriced_usage_events({','.join(columns)}) VALUES({placeholders}) "
        f"ON CONFLICT(raw_id) DO UPDATE SET {updates}",
        tuple(values[column] for column in columns),
    )
    connection.execute("DELETE FROM usage_events WHERE raw_id=?", (event.raw_id,))
    return not existed


def upsert_quota(
    connection: sqlite3.Connection,
    *,
    provider_key: str,
    limit_key: str,
    label: str,
    unit: str,
    source: str,
    polled_at: str,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
    resets_at: str | None = None,
    is_payg: bool | None = None,
) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM quotas WHERE provider_key=? AND limit_key=? AND polled_at=?",
        (provider_key, limit_key, polled_at),
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO quotas(
            provider_key, limit_key, label, used, allowance, unit, pct, resets_at,
            source, is_payg, polled_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(provider_key, limit_key, polled_at) DO UPDATE SET
            label=excluded.label,
            used=excluded.used,
            allowance=excluded.allowance,
            unit=excluded.unit,
            pct=excluded.pct,
            resets_at=excluded.resets_at,
            source=excluded.source,
            is_payg=excluded.is_payg
        """,
        (
            provider_key,
            limit_key,
            label,
            used,
            allowance,
            unit,
            pct,
            resets_at,
            source,
            is_payg,
            polled_at,
        ),
    )
    return not existed


def upsert_agent_run(
    connection: sqlite3.Connection,
    *,
    id: str,
    name: str,
    model_key: str | None,
    state: str,
    started_at: str,
    last_seen_at: str,
) -> bool:
    existed = connection.execute(
        "SELECT 1 FROM agent_runs WHERE id = ?", (id,)
    ).fetchone() is not None
    connection.execute(
        """
        INSERT INTO agent_runs(id, name, model_key, state, started_at, last_seen_at)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(id) DO UPDATE SET
            name=excluded.name,
            model_key=excluded.model_key,
            state=excluded.state,
            started_at=excluded.started_at,
            last_seen_at=excluded.last_seen_at
        WHERE name IS NOT excluded.name
           OR model_key IS NOT excluded.model_key
           OR state IS NOT excluded.state
           OR started_at IS NOT excluded.started_at
           OR last_seen_at IS NOT excluded.last_seen_at
        """,
        (id, name, model_key, state, started_at, last_seen_at),
    )
    return not existed


def record_coverage_gap(
    connection: sqlite3.Connection,
    *,
    raw_id: str,
    source: str,
    tool_key: str,
    model_key: str,
    occurred_at: str,
    issue: str,
) -> None:
    connection.execute(
        """
        INSERT INTO coverage_gap_events(raw_id,source,tool_key,model_key,occurred_at,issue)
        VALUES(?,?,?,?,?,?)
        ON CONFLICT(raw_id) DO UPDATE SET
            source=excluded.source,
            tool_key=excluded.tool_key,
            model_key=excluded.model_key,
            occurred_at=excluded.occurred_at,
            issue=excluded.issue
        """,
        (raw_id, source, tool_key, model_key, occurred_at, issue),
    )


# While an extra profile is collected, its rows and sessions are recorded under
# that profile's card key (see adapters.profiles_local): {"claude-code": "claude-code-work"}.
TOOL_KEY_OVERRIDES: ContextVar[dict | None] = ContextVar("tool_key_overrides", default=None)


def upsert_session(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    tool_key: str,
    project: str | None,
    started_at: str,
    ended_at: str | None,
    model_key: str,
) -> None:
    overrides = TOOL_KEY_OVERRIDES.get()
    if overrides:
        tool_key = overrides.get(tool_key, tool_key)
    connection.execute(
        """
        INSERT INTO sessions(session_id, tool_key, project, started_at, ended_at, model_key)
        VALUES(?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id) DO UPDATE SET
            tool_key=excluded.tool_key,
            project=COALESCE(excluded.project, sessions.project),
            started_at=MIN(sessions.started_at, excluded.started_at),
            ended_at=MAX(COALESCE(sessions.ended_at, excluded.ended_at), excluded.ended_at),
            model_key=excluded.model_key
        """,
        (session_id, tool_key, project, started_at, ended_at, model_key),
    )


def record_pricing_gap(
    connection: sqlite3.Connection,
    *,
    model_key: str,
    source: str,
    occurred_at: str,
    raw_id: str,
) -> None:
    inserted = connection.execute(
        "INSERT OR IGNORE INTO pricing_gap_events(raw_id,model_key,source,occurred_at) VALUES(?,?,?,?)",
        (raw_id, model_key, source, occurred_at),
    ).rowcount
    if not inserted:
        return
    connection.execute(
        """
        INSERT INTO pricing_gaps(model_key, source, first_seen_at, last_seen_at, occurrences, sample_raw_id)
        VALUES(?, ?, ?, ?, 1, ?)
        ON CONFLICT(model_key) DO UPDATE SET
            last_seen_at=MAX(pricing_gaps.last_seen_at, excluded.last_seen_at),
            occurrences=pricing_gaps.occurrences + 1,
            sample_raw_id=excluded.sample_raw_id
        """,
        (model_key, source, occurred_at, occurred_at, raw_id),
    )


def read_opencode_progress(
    connection: sqlite3.Connection, *, session_id: str, provider_id: str
) -> dict | None:
    row = connection.execute(
        "SELECT input_tokens, cached_input_tokens, cache_write_tokens, output_tokens, "
        "reasoning_tokens, cost_usd, model_id, observed_at "
        "FROM opencode_session_progress WHERE session_id=? AND provider_id=?",
        (session_id, provider_id),
    ).fetchone()
    if row is None:
        return None
    return {
        "input_tokens": row[0],
        "cached_input_tokens": row[1],
        "cache_write_tokens": row[2],
        "output_tokens": row[3],
        "reasoning_tokens": row[4],
        "cost_usd": row[5],
        "model_id": row[6],
        "observed_at": row[7],
    }


def write_opencode_progress(
    connection: sqlite3.Connection,
    *,
    session_id: str,
    provider_id: str,
    input_tokens: int,
    cached_input_tokens: int,
    cache_write_tokens: int,
    output_tokens: int,
    reasoning_tokens: int | None,
    cost_usd: float | None,
    model_id: str | None,
    observed_at: str,
) -> None:
    connection.execute(
        """
        INSERT INTO opencode_session_progress(
            session_id, provider_id, input_tokens, cached_input_tokens, cache_write_tokens,
            output_tokens, reasoning_tokens, cost_usd, model_id, observed_at
        ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(session_id, provider_id) DO UPDATE SET
            input_tokens=excluded.input_tokens,
            cached_input_tokens=excluded.cached_input_tokens,
            cache_write_tokens=excluded.cache_write_tokens,
            output_tokens=excluded.output_tokens,
            reasoning_tokens=excluded.reasoning_tokens,
            cost_usd=excluded.cost_usd,
            model_id=excluded.model_id,
            observed_at=excluded.observed_at
        """,
        (
            session_id,
            provider_id,
            input_tokens,
            cached_input_tokens,
            cache_write_tokens,
            output_tokens,
            reasoning_tokens,
            cost_usd,
            model_id,
            observed_at,
        ),
    )


def sync_model_prices(connection: sqlite3.Connection, prices: object) -> int:
    written = 0
    for price in getattr(prices, "prices", ()):  # PricingEngine without a circular import.
        connection.execute(
            """
            INSERT INTO model_prices(
                model_key, input_per_mtok, cached_input_per_mtok, cache_write_per_mtok,
                cache_write_1h_per_mtok, output_per_mtok, long_context_threshold, long_input_multiplier,
                long_output_multiplier, effective_from, effective_to, source_url
            ) VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(model_key, effective_from) DO UPDATE SET
                input_per_mtok=excluded.input_per_mtok,
                cached_input_per_mtok=excluded.cached_input_per_mtok,
                cache_write_per_mtok=excluded.cache_write_per_mtok,
                cache_write_1h_per_mtok=excluded.cache_write_1h_per_mtok,
                output_per_mtok=excluded.output_per_mtok,
                long_context_threshold=excluded.long_context_threshold,
                long_input_multiplier=excluded.long_input_multiplier,
                long_output_multiplier=excluded.long_output_multiplier,
                effective_to=excluded.effective_to,
                source_url=excluded.source_url
            WHERE input_per_mtok IS NOT excluded.input_per_mtok
               OR cached_input_per_mtok IS NOT excluded.cached_input_per_mtok
               OR cache_write_per_mtok IS NOT excluded.cache_write_per_mtok
               OR cache_write_1h_per_mtok IS NOT excluded.cache_write_1h_per_mtok
               OR output_per_mtok IS NOT excluded.output_per_mtok
               OR long_context_threshold IS NOT excluded.long_context_threshold
               OR long_input_multiplier IS NOT excluded.long_input_multiplier
               OR long_output_multiplier IS NOT excluded.long_output_multiplier
               OR effective_to IS NOT excluded.effective_to
               OR source_url IS NOT excluded.source_url
            """,
            (
                price.model_key,
                float(price.input_per_mtok),
                float(price.cached_input_per_mtok),
                float(price.cache_write_per_mtok),
                float(price.cache_write_1h_per_mtok) if price.cache_write_1h_per_mtok else None,
                float(price.output_per_mtok),
                price.long_context_threshold,
                float(price.long_input_multiplier),
                float(price.long_output_multiplier),
                price.effective_from.isoformat(),
                price.effective_to.isoformat() if price.effective_to else None,
                price.source_url,
            ),
        )
        written += 1
    return written
