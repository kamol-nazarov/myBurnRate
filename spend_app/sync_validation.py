"""Validation shared by the hub ingress and the desktop sync exporter."""

from __future__ import annotations

import math
import re
from datetime import UTC, datetime, timedelta

from spend_app.timeutil import parse_utc

MAX_COST = 1_000_000
MAX_COUNT = 10**12
MAX_SQLITE_INT = 2**63 - 1
MAX_PAGE = 2_000
MAX_ROWS = 24_000
MIN_TIMESTAMP = datetime(2015, 1, 1, tzinfo=UTC)
QUOTA_KEY = re.compile(r"[a-z0-9_.:-]{1,64}")
TEXT_LIMITS = {
    "source": 128, "tool_key": 128, "model_key": 128, "modelKey": 128,
    "project": 256, "raw_id": 512, "session_id": 512, "id": 512,
    "label": 512, "unit": 32, "os": 128, "deviceId": 128,
    "machineId": 48, "admin_context_window": 32, "organization_scope": 64,
    "state": 32,
}
COUNT_FIELDS = {
    "input_tokens", "cached_input_tokens", "cache_write_tokens", "cache_write_1h_tokens",
    "output_tokens", "reasoning_tokens", "unclassified_tokens", "admin_request_count",
    "grok_turn_elapsed_ms",
}
COST_FIELDS = ("cost_usd", "computed_cost_usd")


def finite_amount(value: object, low: float, high: float) -> bool:
    # Check range before float conversion so a giant integer is safely rejected.
    return type(value) in (float, int) and low <= value <= high and math.isfinite(value)


def read_timestamp(value: object, now: datetime, *, upper: timedelta | None = timedelta(days=1)) -> datetime:
    if not isinstance(value, str) or len(value) > 64:
        raise ValueError("Invalid sync timestamp.")
    stamp = parse_utc(value)
    if stamp is None or stamp < MIN_TIMESTAMP or (upper is not None and stamp > now + upper):
        raise ValueError("Sync timestamp outside the supported range.")
    return stamp


def _row(item: object, *, required: tuple[str, ...] = ()) -> dict:
    if not isinstance(item, dict):
        raise ValueError("Sync rows must be JSON objects.")
    for key in required:
        if not isinstance(item.get(key), str) or not item[key].strip():
            raise ValueError("A required sync text field is missing.")
    for key, limit in TEXT_LIMITS.items():
        value = item.get(key)
        if value is not None and (not isinstance(value, str) or len(value) > limit):
            raise ValueError("Invalid sync text field.")
    return item


def _optional_times(item: dict, now: datetime, keys: tuple[str, ...]) -> None:
    for name in keys:
        if item.get(name) is not None:
            read_timestamp(item[name], now)


def validate_usage(item: object, now: datetime) -> None:
    row = _row(item, required=("raw_id", "tool_key", "model_key", "occurred_at"))
    read_timestamp(row["occurred_at"], now)
    _optional_times(row, now, ("ingested_at",))
    for field in COUNT_FIELDS.intersection(row):
        value = row[field]
        if value is not None and (type(value) is not int or not 0 <= value <= MAX_COUNT):
            raise ValueError("Invalid sync usage count.")
    for field in COST_FIELDS:
        value = row.get(field)
        if value is not None and not finite_amount(value, 0, MAX_COST):
            raise ValueError("Invalid sync usage cost.")


def _quota_range(unit: str, field: str) -> tuple[float, float]:
    if field == "pct":
        # Providers can report overage. Keep that information but bound it.
        return 0, 10_000
    if unit in {"usd", "credits"}:
        # An account running on overdraft can report a negative balance.
        return (-MAX_COST if field == "used" else 0), MAX_COUNT
    return 0, MAX_COUNT


def validate_quota(item: object, now: datetime) -> None:
    row = _row(item, required=("provider_key", "limit_key", "label"))
    if not all(QUOTA_KEY.fullmatch(row[key]) for key in ("provider_key", "limit_key")):
        raise ValueError("Invalid quota identifier.")
    unit = row.get("unit") or "pct"
    for field in ("used", "allowance", "pct"):
        value = row.get(field)
        if value is not None and not finite_amount(value, *_quota_range(unit, field)):
            raise ValueError("Invalid quota value.")
    # The poll timestamp is clamped on import. Weekly and monthly reset
    # timestamps may legitimately be more than a day in the future.
    for key in ("polled_at", "resets_at"):
        if row.get(key) is not None:
            read_timestamp(row[key], now, upper=None)
    if row.get("is_payg") is not None and row["is_payg"] not in (False, True):
        raise ValueError("Invalid quota billing flag.")


def _validate_tree(body: dict) -> None:
    visited = 0

    def visit(value: object, depth: int) -> None:
        nonlocal visited
        visited += 1
        if depth > 12 or visited > MAX_ROWS * 30:
            raise ValueError("Sync structure exceeds supported complexity.")
        if isinstance(value, dict):
            for child in value.values():
                visit(child, depth + 1)
        elif isinstance(value, list):
            for child in value:
                visit(child, depth + 1)
        elif type(value) is int and not -MAX_SQLITE_INT - 1 <= value <= MAX_SQLITE_INT:
            raise ValueError("Sync integer outside supported range.")
        elif type(value) is float and not math.isfinite(value):
            raise ValueError("Sync number must be finite.")

    visit(body, 0)


def validate_push(body: object, now: datetime) -> None:
    if not isinstance(body, dict):
        raise ValueError("Expected a JSON object.")
    row_count = 0
    if "machine" in body:
        _row(body["machine"])
    limits = {
        # The desktop can send 1,000 old-client rows plus 1,000
        # mirrored journal rows per stream, and their related sessions.
        "usageEvents": 4_000, "unpricedEvents": 4_000, "sessions": 8_000,
        "agentRuns": 8_000, "quotas": 8_000,
    }
    for name, limit in limits.items():
        rows = body.get(name)
        if rows is None:
            continue
        if not isinstance(rows, list) or len(rows) > limit:
            raise ValueError("Invalid or oversized sync collection.")
        row_count += len(rows)
        for row in rows:
            if name in ("usageEvents", "unpricedEvents"):
                validate_usage(row, now)
            elif name == "quotas":
                validate_quota(row, now)
            elif name == "sessions":
                item = _row(row, required=("session_id", "tool_key", "model_key", "started_at"))
                _optional_times(item, now, ("started_at", "ended_at"))
            else:
                item = _row(row, required=("id", "name"))
                _optional_times(item, now, ("started_at", "last_seen_at", "startedAt", "lastSeenAt"))

    for name in ("usageChanges", "usageContinuity", "providerCosts"):
        if name not in body:
            continue
        batch = _row(body[name])
        fields = ("marks", "revisions") if name == "usageContinuity" else ("changes",)
        for field in fields:
            rows = batch.get(field)
            if not isinstance(rows, list) or len(rows) > MAX_PAGE:
                raise ValueError("Invalid or oversized journal collection.")
            row_count += len(rows)
        if name == "usageChanges":
            for change in rows:
                item = _row(change, required=("raw_id",))
                if item.get("kind") in ("usage", "unpriced"):
                    validate_usage(item.get("event"), now)
    if row_count > MAX_ROWS:
        raise ValueError("Too many sync rows.")
    _validate_tree(body)


def reject_json_constant(_: str) -> None:
    raise ValueError("Sync number must be finite.")


def clean_usage_cost(row: dict) -> dict:
    for field in COST_FIELDS:
        if row.get(field) is not None and not finite_amount(row[field], 0, MAX_COST):
            row[field] = None
    return row


def clean_quota_numbers(row: dict) -> dict:
    for field in ("used", "allowance", "pct"):
        if row.get(field) is not None and not finite_amount(row[field], *_quota_range(row["unit"], field)):
            row[field] = None
    return row
