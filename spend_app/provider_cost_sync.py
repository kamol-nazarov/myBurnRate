"""Acknowledged transfer of organization billing observations."""

from __future__ import annotations

import hashlib
import json
import math

from spend_app.db import ProviderCostBucket, connect, upsert_cost_bucket
from spend_app.organization import ORGANIZATION_SOURCES, clean_scope
from spend_app.timeutil import iso_utc, parse_utc

COST_SEQ = "provider_cost_seq"
_MARKER = "provider_cost_changes.initialized.v1"
_SOURCE_FILTER = (
    "source IN ('openai_admin','anthropic_admin') AND raw_id NOT LIKE 'm:%'"
)
_FIELDS = (
    "source",
    "starting_at",
    "ending_at",
    "project_id",
    "line_item",
    "model_key",
    "cost_usd",
    "raw_id",
    "ingested_at",
    "organization_scope",
)


def ensure_provider_cost_journal(path) -> None:
    # Keep one latest change per local bucket, bounded by retained cost
    # observations. AUTOINCREMENT gives in-place corrections a new position.
    with connect(path) as connection:
        if connection.execute(
            "SELECT 1 FROM app_meta WHERE key=?", (_MARKER,)
        ).fetchone():
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute(
            "SELECT 1 FROM app_meta WHERE key=?", (_MARKER,)
        ).fetchone():
            return
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info('provider_cost_buckets')")
        }
        if "organization_scope" not in columns:
            connection.execute(
                "ALTER TABLE provider_cost_buckets ADD COLUMN organization_scope TEXT"
            )
        connection.execute(
            "CREATE TABLE IF NOT EXISTS provider_cost_changes ("
            "seq INTEGER PRIMARY KEY AUTOINCREMENT, raw_id TEXT NOT NULL UNIQUE)"
        )
        for operation, row in (("INSERT", "NEW"), ("UPDATE", "NEW"), ("DELETE", "OLD")):
            condition = f"{row}.source IN ('openai_admin','anthropic_admin') AND {row}.raw_id NOT LIKE 'm:%'"
            if operation == "DELETE":
                action = "DELETE FROM provider_cost_changes WHERE raw_id=OLD.raw_id"
            else:
                action = (
                    "DELETE FROM provider_cost_changes WHERE raw_id=NEW.raw_id; "
                    "INSERT INTO provider_cost_changes(raw_id) VALUES(NEW.raw_id)"
                )
            connection.execute(
                f"CREATE TRIGGER IF NOT EXISTS provider_cost_journal_{operation.lower()} "
                f"AFTER {operation} ON provider_cost_buckets WHEN {condition} BEGIN {action}; END"
            )
        connection.execute(
            "INSERT OR IGNORE INTO provider_cost_changes(raw_id) SELECT raw_id "
            f"FROM provider_cost_buckets WHERE {_SOURCE_FILTER} ORDER BY id"
        )
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?, '1')", (_MARKER,))


def export_provider_costs(connection, after: int, limit: int) -> dict:
    fields = ", ".join(f"bucket.{field}" for field in _FIELDS)
    rows = connection.execute(
        f"SELECT change.seq, {fields} FROM provider_cost_changes AS change "
        "JOIN provider_cost_buckets AS bucket ON bucket.raw_id=change.raw_id "
        "WHERE change.seq>? ORDER BY change.seq LIMIT ?",
        (after, limit + 1),
    ).fetchall()
    changes = [dict(row) for row in rows[:limit]]
    return {
        "after": after,
        "through": changes[-1]["seq"] if changes else after,
        "changes": changes,
        "hasMore": len(rows) > limit,
    }


def cost_acknowledgement(batch: dict) -> dict:
    encoded = json.dumps(
        batch["changes"], sort_keys=True, separators=(",", ":"), allow_nan=False
    )
    return {
        "through": batch["through"],
        "digest": hashlib.sha256(encoded.encode("utf-8")).hexdigest(),
    }


def _nullable_text(value: object) -> bool:
    return value is None or (isinstance(value, str) and len(value) <= 1024)


def apply_provider_costs(connection, machine_id: str, batch: dict) -> tuple[dict, bool]:
    if not isinstance(batch, dict):
        raise ValueError("Invalid provider cost batch.")
    after, through, changes = (
        batch.get("after"),
        batch.get("through"),
        batch.get("changes"),
    )
    if (
        type(after) is not int
        or type(through) is not int
        or not 0 <= after <= through
        or not isinstance(changes, list)
        or len(changes) > 1_000
    ):
        raise ValueError("Invalid provider cost batch.")
    previous = after
    seen: set[str] = set()
    changed = False
    for item in changes:
        if not isinstance(item, dict) or not all(field in item for field in _FIELDS):
            raise ValueError("Invalid provider cost observation.")
        seq, raw_id, source = item.get("seq"), item["raw_id"], item["source"]
        if (
            type(seq) is not int
            or not previous < seq <= through
            or not isinstance(source, str)
            or source not in ORGANIZATION_SOURCES
            or not isinstance(raw_id, str)
            or not raw_id
            or len(raw_id) > 256
            or raw_id.startswith("m:")
            or raw_id in seen
            or not all(
                _nullable_text(item[key])
                for key in ("project_id", "line_item", "model_key")
            )
        ):
            raise ValueError("Invalid provider cost observation.")
        previous = seq
        seen.add(raw_id)
        starting = parse_utc(item["starting_at"])
        ending = parse_utc(item["ending_at"])
        observed = parse_utc(item["ingested_at"])
        amount = item["cost_usd"]
        if (
            starting is None
            or ending is None
            or ending <= starting
            or observed is None
            or type(amount) not in (int, float)
            or not 0 <= amount <= 1e15
            or not math.isfinite(amount)
        ):
            raise ValueError("Invalid provider cost period or amount.")
        imported_id = f"m:{machine_id}:{raw_id}"
        existing = connection.execute(
            "SELECT ingested_at FROM provider_cost_buckets WHERE raw_id=?",
            (imported_id,),
        ).fetchone()
        if (
            existing is not None
            and (stored := parse_utc(existing[0])) is not None
            and stored >= observed
        ):
            continue
        before = connection.total_changes
        upsert_cost_bucket(
            connection,
            ProviderCostBucket(
                source=source,
                raw_id=imported_id,
                starting_at=iso_utc(starting),
                ending_at=iso_utc(ending),
                project_id=item["project_id"],
                line_item=item["line_item"],
                model_key=item["model_key"],
                cost_usd=float(amount),
                ingested_at=iso_utc(observed),
                organization_scope=clean_scope(source, item["organization_scope"]),
            ),
        )
        changed |= connection.total_changes != before
    if previous != through:
        raise ValueError("Invalid provider cost sequence.")
    return cost_acknowledgement(batch), changed
