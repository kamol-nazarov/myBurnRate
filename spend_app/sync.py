"""Multi-machine sync hub, device registry, SSE live stream, and remote agent client."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import logging
import os
import platform
import re
import secrets
import sqlite3
import sys
import threading
import time
from collections import OrderedDict, deque
from contextlib import closing
from dataclasses import fields
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from anyio import CancelScope
from fastapi import APIRouter, HTTPException, Query, Request
from spend_app.error_codes import (
    HUB_PROTOCOL_VERSION,
    check_protocol_version,
    error_response,
    format_error,
    map_http_error,
    map_transport_error,
)
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from spend_app.db import (
    EXACT_USAGE_SOURCES,
    RETIRED_SOURCES,
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    cap_agent_run_name,
    prune_ingest_runs,
    prune_usage_changes,
    upsert_agent_run,
    upsert_quota,
    upsert_session,
    upsert_unpriced_event,
    upsert_usage_event,
    utc_now,
)
from spend_app.capacity_forecast import apply_balance_runway, record_balance_sample
from spend_app.ingest import IngestRun
from spend_app.organization import ORGANIZATION_SOURCES, admin_event_metadata, admin_row_pricing_args
from spend_app.pricing import PricingEngine, UnpricedModelError
from spend_app.provider_cost_sync import (
    COST_SEQ, apply_provider_costs, cost_acknowledgement, export_provider_costs, validate_provider_costs,
)
from spend_app.sync_continuity import (
    can_rebase,
    choose_usage_window,
    clear_legacy_conflicts,
    commit_usage_window,
    count_legacy_conflicts,
    journal_mark,
    received_revision,
    retain_legacy_conflict,
    verified_reply,
)
from spend_app.sync_validation import (
    MAX_COST, clean_quota_numbers, clean_usage_cost, finite_amount, reject_json_constant,
    validate_push,
)

LOGGER = logging.getLogger(__name__)

MACHINES_META_KEY = "machines.v1"
DEVICE_ID_META_KEY = "device_id.v1"
SYNC_CURSOR_PREFIX = "sync.cursor.v1:"
MACHINE_ONLINE_SECONDS = 90
_MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{7,127}$")
_MACHINE_BEARER_RE = re.compile(r"br_m_([a-z0-9][a-z0-9_-]{0,47})_[A-Za-z0-9_-]{43}")
_USAGE_EVENT_FIELDS = {field.name for event in (UsageEvent, UnpricedUsageEvent) for field in fields(event)}


class _RequestWindow:
    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.lock = threading.Lock()
        self.hits: OrderedDict[tuple[Path, str], deque[float]] = OrderedDict()

    def allow(self, key: tuple[Path, str], *, record: bool = False) -> bool:
        now = time.monotonic()
        with self.lock:
            bucket = self.hits.get(key)
            if bucket is not None:
                while bucket and bucket[0] <= now - 60:
                    bucket.popleft()
                if len(bucket) >= self.limit:
                    return False
            if record:
                if bucket is None:
                    bucket = deque()
                    self.hits[key] = bucket
                self.hits.move_to_end(key)
                bucket.append(now)
                if len(self.hits) > 4096:
                    self.hits.popitem(last=False)
        return True


_PUSH_WINDOW = _RequestWindow(60)  # A desktop can send 15 pages in one cycle.
_FAILED_BEARER_WINDOW = _RequestWindow(30)


def machine_bearer_allowed(database_path: Path, address: str, *, failed: bool = False) -> bool:
    return _FAILED_BEARER_WINDOW.allow((database_path, address), record=failed)


def _clean_device_id(value: object) -> str | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw or not _DEVICE_ID_RE.fullmatch(raw):
        return None
    return raw


def get_or_create_device_id(database_path: Path) -> str:
    initialize(database_path)
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key=?", (DEVICE_ID_META_KEY,)
        ).fetchone()
        if row and row[0]:
            existing = _clean_device_id(row[0])
            if existing:
                return existing
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key=?", (DEVICE_ID_META_KEY,)
        ).fetchone()
        if row and row[0]:
            existing = _clean_device_id(row[0])
            if existing:
                return existing
        created = f"dev-{secrets.token_hex(16)}"
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (DEVICE_ID_META_KEY, created),
        )
        return created
_PREFIX_RE = re.compile(r"^m:([a-z0-9][a-z0-9_-]{0,47}):(.+)$", re.DOTALL)


def row_machine_id(identifier: str | None) -> str:
    """Return the machine id encoded in a prefixed id, or ``'local'``."""
    if not identifier or not identifier.startswith("m:"):
        return "local"
    match = _PREFIX_RE.match(identifier)
    return match.group(1) if match else "local"


def strip_machine_prefix(identifier: str) -> str:
    if not identifier.startswith("m:"):
        return identifier
    match = _PREFIX_RE.match(identifier)
    return match.group(2) if match else identifier


def prefix_machine_id(machine_id: str, identifier: str | None) -> str | None:
    if identifier is None or not machine_id or machine_id == "local":
        return identifier
    prefix = f"m:{machine_id}:"
    if identifier.startswith(prefix):
        return identifier
    return prefix + strip_machine_prefix(identifier)


def _default_hub_name() -> str:
    node = (platform.node() or "").strip()
    return node or "This computer"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    return (slug[:28] or "machine").strip("-")


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _read_state(connection: sqlite3.Connection) -> dict:
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (MACHINES_META_KEY,)
    ).fetchone()
    if not row or not row[0]:
        return {"version": 1, "hub_name": _default_hub_name(), "machines": {}}
    try:
        data = json.loads(row[0])
    except ValueError:
        return {"version": 1, "hub_name": _default_hub_name(), "machines": {}}
    if not isinstance(data, dict) or not isinstance(data.get("machines"), dict):
        return {"version": 1, "hub_name": _default_hub_name(), "machines": {}}
    data.setdefault("version", 1)
    data.setdefault("hub_name", _default_hub_name())
    return data


def _write_state(connection: sqlite3.Connection, state: dict) -> None:
    connection.execute(
        "INSERT INTO app_meta(key, value) VALUES(?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (MACHINES_META_KEY, json.dumps(state, ensure_ascii=False)),
    )


def _preserve_audited_machine_names(connection, state: dict) -> None:
    """Keep a legacy rename when no later heartbeat could have replaced it."""
    if state.get("owner_names_migrated") or not connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='admin_audit'"
    ).fetchone():
        return
    renames = connection.execute(
        "SELECT target,timestamp FROM admin_audit WHERE id IN ("
        "SELECT MAX(id) FROM admin_audit WHERE action='machine.rename' AND outcome='success' GROUP BY target)"
    )
    for machine_id, timestamp in renames:
        record = state["machines"].get(machine_id)
        if not record or record.get("owner_name") or not record.get("name"):
            continue
        renamed = _parse_iso(timestamp)
        seen = _parse_iso(record.get("last_seen_at"))
        if renamed and (seen is None or renamed > seen):
            record["owner_name"] = record["name"]
    state["owner_names_migrated"] = True


def machines_fingerprint(connection: sqlite3.Connection) -> str:
    state = _read_state(connection)
    online_by_id = {m["id"]: bool(m.get("online")) for m in list_machines(connection)}
    items = [
        (
            mid,
            str(rec.get("name") or ""),
            str(rec.get("os") or ""),
            bool(rec.get("revoked")),
            int(rec.get("events_pushed") or 0),
            online_by_id.get(mid, False),
        )
        for mid, rec in sorted((state.get("machines") or {}).items())
        if isinstance(rec, dict)
    ]
    return json.dumps([state.get("hub_name"), items])


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


_MACHINE_LAST_SEEN_MEM: dict[str, str] = {}


def list_machines(
    connection: sqlite3.Connection, *, now: datetime | None = None
) -> list[dict]:
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    state = _read_state(connection)
    is_central = os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}
    has_remote_machines = any(
        isinstance(rec, dict) and not rec.get("revoked")
        for rec in state["machines"].values()
    )
    items: list[dict] = []
    if not (is_central and has_remote_machines):
        items.append(
            {
                "id": "local",
                "name": state.get("hub_name") or _default_hub_name(),
                "os": sys.platform,
                "isLocal": True,
                "online": True,
                "lastSeenAt": moment.isoformat().replace("+00:00", "Z"),
                "eventsPushed": 0,
            }
        )
    cutoff = moment - timedelta(seconds=MACHINE_ONLINE_SECONDS)
    for machine_id, rec in sorted(
        state["machines"].items(),
        key=lambda item: (str(item[1].get("name", "")).lower(), item[0]),
    ):
        if not isinstance(rec, dict) or rec.get("revoked"):
            continue
        raw_seen = rec.get("last_seen_at")
        is_realtime = abs((datetime.now(UTC) - moment).total_seconds()) < 300
        mem_seen = _MACHINE_LAST_SEEN_MEM.get(machine_id) if is_realtime else None
        db_dt = _parse_iso(raw_seen)
        mem_dt = _parse_iso(mem_seen)
        if mem_dt and (db_dt is None or mem_dt > db_dt):
            last_seen = mem_dt
            last_seen_iso = mem_seen
        else:
            last_seen = db_dt
            last_seen_iso = raw_seen
        online = bool(last_seen and last_seen >= cutoff)
        items.append(
            {
                "id": machine_id,
                "name": rec.get("name") or machine_id,
                "os": rec.get("os") or "",
                "isLocal": False,
                "online": online,
                "lastSeenAt": last_seen_iso,
                "eventsPushed": int(rec.get("events_pushed") or 0),
                "expiresAt": rec.get("expires_at"),
                "rotationRequired": bool(rec.get("rotation_required")),
                "rotationDeadline": rec.get("rotation_deadline"),
            }
        )
    return items


def _ensure_unique_active_machine_name(
    state: dict, name: str, *, exclude_machine_id: str | None = None
) -> None:
    target = (name or "").strip().lower()
    if not target:
        return
    if any(
        mid != exclude_machine_id
        and isinstance(rec, dict)
        and not rec.get("revoked")
        and str(rec.get("name", "")).strip().lower() == target
        for mid, rec in state["machines"].items()
    ):
        raise ValueError("A machine with that name already exists.")


def _register_in_connection(
    connection,
    *,
    name,
    os_name="",
    device_id: str | None = None,
    now=None,
    reconnect=False,
    machine_id=None,
    allow_new_id=False,
    actor=None,
):
    from spend_app.auth_model import MACHINE_DAYS, moment, registered_machine_session, stamp
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    clean_device_id: str | None = None
    if device_id is not None and str(device_id).strip():
        clean_device_id = _clean_device_id(device_id)
        if not clean_device_id:
            raise ValueError("Invalid device ID.")
    state = _read_state(connection)
    _preserve_audited_machine_names(connection, state)
    matched_id = machine_id
    matched_by_device_id = False
    if matched_id is not None and not allow_new_id and matched_id not in state["machines"]:
        raise ValueError("Unknown machine.")
    if matched_id is None and clean_device_id is not None:
        device_matches = [
            mid for mid, rec in state["machines"].items()
            if isinstance(rec, dict) and rec.get("device_id") == clean_device_id
        ]
        if device_matches:
            matched_id = next(
                (mid for mid in device_matches if not state["machines"][mid].get("revoked")),
                device_matches[0],
            )
            matched_by_device_id = True
        else:
            _ensure_unique_active_machine_name(state, clean_name)
    elif matched_id is None:
        matches = [
            mid for mid, rec in state["machines"].items()
            if isinstance(rec, dict) and str(rec.get("name", "")).strip().lower() == clean_name.lower()
        ]
        active = [mid for mid in matches if not state["machines"][mid].get("revoked")]
        if matches:
            if len(active) != 1:
                raise ValueError(
                    "Multiple or revoked machines match that name; specify the machine ID."
                )
            if reconnect:
                if state["machines"][active[0]].get("device_id"):
                    raise ValueError("A machine with that name already exists.")
                matched_id = active[0]
    if matched_id is None:
        matched_id = f"machine-{secrets.token_hex(8)}"
        while matched_id in state["machines"] or matched_id in {"local", "all"}:
            matched_id = f"machine-{secrets.token_hex(8)}"
    prev = state["machines"].get(matched_id) or {}
    display_name = prev.get("owner_name") or clean_name
    _ensure_unique_active_machine_name(state, display_name, exclude_machine_id=matched_id)
    token = f"br_m_{matched_id}_{secrets.token_urlsafe(32)}"
    if reconnect and prev.get("revoked") and not matched_by_device_id:
        raise ValueError("This machine is revoked; sign in again on that computer or specify its machine ID.")
    if actor is not None or prev.get("revoked"):
        registered_machine_session(
            connection, actor, machine_id=matched_id,
            revoked_at=(prev.get("revoked_at") or "") if prev.get("revoked") else None, now=now,
        )
    stored_device_id = clean_device_id or prev.get("device_id")
    record = {
        "id": matched_id, "name": display_name, "reported_name": clean_name,
        "os": (os_name or prev.get("os") or "").strip()[:32],
        "token_hash": _token_hash(token), "created_at": stamp(now),
        "registered_at": prev.get("registered_at") or prev.get("created_at") or stamp(now),
        "expires_at": stamp(moment(now) + timedelta(days=MACHINE_DAYS)), "last_used_at": None,
        "rotation_required": False, "last_seen_at": stamp(now),
        "events_pushed": int(prev.get("events_pushed") or 0),
    }
    if stored_device_id:
        record["device_id"] = stored_device_id
    if prev.get("owner_name"):
        record["owner_name"] = prev["owner_name"]
    state["machines"][matched_id] = record
    _write_state(connection, state)
    machines = list_machines(connection, now=now)
    created = next(item for item in machines if item["id"] == matched_id)
    return {"machine": created, "token": token, "machines": machines,
            "expiresAt": state["machines"][matched_id]["expires_at"]}


def register_or_reconnect_machine(
    database_path: Path, *, name: str, os_name: str = "", device_id: str | None = None, now=None, actor=None
) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _register_in_connection(
            connection, name=name, os_name=os_name, device_id=device_id, now=now, reconnect=True, actor=actor
        )
        if actor:
            from spend_app.auth_model import audit
            audit(connection, actor, "machine.register", result["machine"]["id"], now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": result["machine"]["id"], "at": utc_now()})
    return result


def register_machine(
    database_path: Path, *, name: str, os_name: str = "", device_id: str | None = None, now=None, actor=None
) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _register_in_connection(connection, name=name, os_name=os_name, device_id=device_id, now=now, actor=actor)
        if actor:
            from spend_app.auth_model import audit
            audit(connection, actor, "machine.register", result["machine"]["id"], now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": result["machine"]["id"], "at": utc_now()})
    return result


def rotate_machine_token(database_path: Path, token: str, *, now=None, actor=None) -> dict | None:
    from spend_app.auth_model import moment, parse
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        for mid, rec in state["machines"].items():
            if not isinstance(rec, dict) or rec.get("revoked"):
                continue
            expected = str(rec.get("token_hash") or "")
            if not expected or not hmac.compare_digest(_token_hash(token), expected):
                continue
            if rec.get("rotation_required"):
                deadline = parse(rec.get("rotation_deadline"))
                if not deadline or deadline <= moment(now):
                    return None
            expires = parse(rec.get("expires_at"))
            if not expires or expires <= moment(now):
                return None
            result = _register_in_connection(connection, name=rec["name"], os_name=rec.get("os", ""),
                                             machine_id=mid, now=now)
            if actor:
                from spend_app.auth_model import audit
                audit(connection, actor, "machine.rotate", mid, now=now)
            return result
    return None


def revoke_machine(
    database_path: Path, *, machine_id: str, now: datetime | None = None, actor=None
) -> dict:
    if not machine_id or machine_id in {"local", "all"}:
        raise ValueError("Cannot revoke the local hub machine.")
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        if machine_id not in state["machines"]:
            raise ValueError(f"Unknown machine: {machine_id}")
        from spend_app.auth_model import Principal, audit, revoke_machine_sessions, stamp

        state["machines"][machine_id].update(revoked=True, revoked_at=stamp(now), token_hash="")
        _write_state(connection, state)
        revoke_machine_sessions(connection, actor or Principal("system", "local"), machine_id, now=now)
        if actor:
            audit(connection, actor, "machine.revoke", machine_id, now=now)
        machines = list_machines(connection, now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": machine_id, "at": utc_now()})
    return {"machines": machines}


def rename_machine(
    database_path: Path, *, machine_id: str, name: str, now: datetime | None = None, actor=None
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        if machine_id != "local" and machine_id not in state["machines"]:
            raise ValueError(f"Unknown machine: {machine_id}")
        _ensure_unique_active_machine_name(state, clean_name, exclude_machine_id=machine_id)
        if machine_id == "local":
            state["hub_name"] = clean_name
        else:
            state["machines"][machine_id].update(owner_name=clean_name, name=clean_name)
        _write_state(connection, state)
        if actor:
            from spend_app.auth_model import audit
            audit(connection, actor, "machine.rename", machine_id, now=now)
        machines = list_machines(connection, now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": machine_id, "at": utc_now()})
    return {"machines": machines}


def authenticate_machine_token(database_path: Path, token: str, *, now=None) -> str | None:
    from spend_app.auth_model import moment, parse, stamp

    raw = (token or "").strip()
    match = _MACHINE_BEARER_RE.fullmatch(raw)
    if match is None or not database_path.is_file():
        return None
    machine_id = match[1]
    digest = _token_hash(raw)
    current_time = moment(now)

    def valid(record: object) -> bool:
        if not isinstance(record, dict) or record.get("revoked"):
            return False
        expected = str(record.get("token_hash") or "")
        if not expected or not hmac.compare_digest(digest, expected):
            return False
        expires = parse(record.get("expires_at"))
        if expires is None or expires <= current_time:
            return False
        if record.get("rotation_required"):
            deadline = parse(record.get("rotation_deadline"))
            return deadline is not None and deadline > current_time
        return True

    def usage_due(record: dict) -> bool:
        last = parse(record.get("last_used_at"))
        return last is None or current_time - last >= timedelta(seconds=60)

    uri = database_path.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as reader:
            match = reader.execute(
                "SELECT json_extract(value, ?) FROM app_meta WHERE key=? AND json_valid(value)",
                (f'$.machines."{machine_id}"', MACHINES_META_KEY),
            ).fetchone()
        record = json.loads(match[0]) if match and isinstance(match[0], str) else None
    except (sqlite3.DatabaseError, ValueError):
        return None
    if not valid(record):
        return None
    if usage_due(record):
        with connect(database_path) as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = _read_state(connection)
            record = state["machines"].get(machine_id)
            if not valid(record):
                return None
            if usage_due(record):
                record["last_used_at"] = stamp(current_time)
                _write_state(connection, state)
    _MACHINE_LAST_SEEN_MEM[machine_id] = stamp(current_time)
    return machine_id


class StreamBroker:
    """Thread-safe pub/sub notifier for SSE `/api/spend/stream` clients."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._revision = 0
        self._last_event: dict = {"type": "init", "revision": 0}
        self._waiters: set[tuple[asyncio.AbstractEventLoop, asyncio.Event]] = set()

    @property
    def revision(self) -> int:
        with self._lock:
            return self._revision

    def publish(self, payload: dict) -> dict:
        with self._lock:
            self._revision += 1
            event = {**payload, "revision": self._revision}
            self._last_event = event
            waiters = list(self._waiters)
        for loop, flag in waiters:
            try:
                loop.call_soon_threadsafe(flag.set)
            except RuntimeError:
                pass
        return event

    def subscribe(self) -> tuple[tuple[asyncio.AbstractEventLoop, asyncio.Event], int]:
        loop = asyncio.get_running_loop()
        flag = asyncio.Event()
        entry = (loop, flag)
        with self._lock:
            self._waiters.add(entry)
            return entry, self._revision

    def unsubscribe(self, entry: tuple[asyncio.AbstractEventLoop, asyncio.Event]) -> None:
        with self._lock:
            self._waiters.discard(entry)


STREAM_BROKER = StreamBroker()


def _db_live_signature(connection: sqlite3.Connection) -> int:
    # This read-only connection lives for the entire stream. data_version
    # changes when another connection commits, including in-place updates and
    # deletes, without scanning usage history for every viewer every 2 seconds.
    # Values are comparable only across reads on the SAME connection.
    return connection.execute("PRAGMA data_version").fetchone()[0]


def _price_pushed_event(pricing: PricingEngine, item: dict) -> tuple[float, bool]:
    computed = item.get("computed_cost_usd")
    source = str(item.get("source") or "remote")
    is_exact = bool(item.get("is_exact", source in EXACT_USAGE_SOURCES))
    if computed is not None and source not in ORGANIZATION_SOURCES:
        return float(computed), is_exact
    if item.get("cost_usd") is not None and source not in ORGANIZATION_SOURCES:
        return float(item["cost_usd"]), is_exact
    occurred = _parse_iso(str(item.get("occurred_at") or "")) or datetime.now(UTC)
    cost = pricing.compute(
        model_key=str(item["model_key"]),
        occurred_at=occurred,
        input_tokens=int(item.get("input_tokens") or 0),
        cached_input_tokens=int(item.get("cached_input_tokens") or 0),
        cache_write_tokens=int(item.get("cache_write_tokens") or 0),
        cache_write_1h_tokens=int(item.get("cache_write_1h_tokens") or 0),
        output_tokens=int(item.get("output_tokens") or 0),
        **admin_row_pricing_args(item),
    )
    if source in ORGANIZATION_SOURCES:
        is_exact = pricing.resolve(str(item["model_key"]), occurred).is_exact
    value = float(cost)
    if not finite_amount(value, 0, MAX_COST):
        raise ValueError("Invalid computed sync usage cost.")
    return value, is_exact


def _pushed_telemetry_complete(item: dict) -> bool:
    original = item.get("admin_telemetry_complete")
    if item.get("source") in ORGANIZATION_SOURCES and type(original) is bool:
        return original
    return bool(item.get("telemetry_complete", True))


def _wire_unpriced_admin_event(item: dict) -> dict:
    # A pre-evidence hub uses telemetry_complete to decide whether to
    # reprice a staged row. Keep that reader conservative without losing
    # the distinction between missing tokens and missing pricing evidence.
    if item.get("source") in ORGANIZATION_SOURCES:
        return {
            **item,
            "admin_telemetry_complete": bool(item.get("telemetry_complete", True)),
            "telemetry_complete": False,
        }
    return item


def _retired_item(item) -> bool:
    return isinstance(item, dict) and (item.get("tool_key") == "cursor" or item.get("source") in RETIRED_SOURCES)


def _legacy_usage_matches(connection, machine_id: str, change: dict) -> bool:
    """Compare the evidence a legacy hub actually stored, independent of price recomputation."""
    kind, item = change["kind"], change.get("event")
    if kind == "skip" or _retired_item(item):
        return True
    raw_id = prefix_machine_id(machine_id, change["raw_id"])
    stored = None
    for table in ("usage_events", "unpriced_usage_events"):
        row = connection.execute(f"SELECT * FROM {table} WHERE raw_id=?", (raw_id,)).fetchone()
        if row is not None:
            stored = dict(row)
            break
    if kind == "delete":
        return stored is None
    if stored is None:
        return False
    counts = ("input_tokens", "cached_input_tokens", "cache_write_tokens",
              "cache_write_1h_tokens", "output_tokens", "reasoning_tokens", "unclassified_tokens")
    return (
        stored["source"] == str(item.get("source") or "remote")
        and all(stored[key] == item.get(key) for key in ("tool_key", "model_key", "project", "cost_usd"))
        and stored["session_id"] == prefix_machine_id(machine_id, item.get("session_id"))
        and _parse_iso(stored["occurred_at"]) == _parse_iso(str(item.get("occurred_at") or ""))
        and all(int(stored.get(key) or 0) == int(item.get(key) or 0) for key in counts)
    )


def _usage_change_batch(connection, machine_id: str, batch: object, proof: object = None):
    """Validate and select unacknowledged changes while holding the writer lock."""
    if not isinstance(batch, dict) or batch.get("version") != 1:
        raise ValueError("Unsupported usage change protocol.")
    after, through, changes = batch.get("after"), batch.get("through"), batch.get("changes")
    if (type(after) is not int or type(through) is not int or not 0 <= after <= through
            or not isinstance(changes, list)):
        raise ValueError("Invalid usage change sequence.")
    key = "sync.received.usage_changes.v1:" + machine_id
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
    acknowledged = int(row[0]) if row else 0
    window = choose_usage_window(connection, machine_id, acknowledged, batch, proof) if proof is not None else None
    if window is not None:
        if not window.accepted:
            return [], [], [], key, acknowledged, 0, window
        # The lineage was proved, or this is a recovery transition. Numeric
        # positions from the previous branch cannot acknowledge this branch.
        acknowledged = after
    if after > acknowledged:
        raise ValueError("Usage changes have a sequence gap; reset this hub's sync cursor.")
    if window is None and after < acknowledged and through != acknowledged:
        raise ValueError("Legacy usage changes overlap accepted history; upgrade the spoke to recover.")
    usage, unpriced, deleted = [], [], []
    skipped = 0
    previous = after
    seen = set()
    for change in changes:
        if not isinstance(change, dict):
            raise ValueError("Invalid usage change.")
        seq, raw_id, kind = change.get("seq"), change.get("raw_id"), change.get("kind")
        if (type(seq) is not int or not previous < seq <= through
                or not isinstance(raw_id, str) or not raw_id or raw_id.startswith("m:")
                or raw_id in seen or kind not in {"usage", "unpriced", "delete", "skip"}):
            raise ValueError("Invalid usage change.")
        previous = seq
        seen.add(raw_id)
        if kind == "skip" and change.get("reason") != "invalid_timestamp":
            raise ValueError("Unsupported skipped usage reason.")
        if kind in {"usage", "unpriced"}:
            event = change.get("event")
            if not isinstance(event, dict) or event.get("raw_id") != raw_id:
                raise ValueError("Usage change identity does not match its event.")
        if window is not None:
            if seq not in window.marks:
                raise ValueError("Usage change lacks its checkpoint.")
            if received_revision(connection, machine_id, window.marks[seq]):
                continue
            revision = window.revisions.get(seq)
            if revision and received_revision(connection, machine_id, revision[1]):
                continue
            verify_legacy = (
                seq <= window.legacy_verification_through
                or (seq <= window.legacy_through and seq in window.imported_legacy)
            )
            if verify_legacy:
                if revision:
                    # The exported body is from a later mutation outside this
                    # page. Leave it unacknowledged until its own turn.
                    window.revisions.pop(seq)
                    continue
                if not _legacy_usage_matches(connection, machine_id, change):
                    conflict = {"raw_id": raw_id, "seq": seq, "kind": kind}
                    if kind in {"usage", "unpriced"}:
                        conflict["event"] = {
                            key: value for key, value in event.items()
                            if key in _USAGE_EVENT_FIELDS
                            and (value is None or isinstance(value, (str, int, float)))
                        }
                    retain_legacy_conflict(connection, machine_id, window.marks[seq], conflict)
                continue
            if kind != "skip":
                clear_legacy_conflicts(connection, machine_id, raw_id)
        if seq <= acknowledged:
            if window is None and not _legacy_usage_matches(connection, machine_id, change):
                raise ValueError("Legacy replay differs from accepted history; upgrade the spoke to recover.")
            continue
        if kind == "delete":
            deleted.append(raw_id)
        elif kind == "skip":
            # A malformed local observation is quarantined, not a deletion of
            # a previously valid remote event. Its later repair gets a new seq.
            skipped += 1
        elif not _retired_item(event):
            (usage if kind == "usage" else unpriced).append(event)
    if previous != through:
        raise ValueError("Usage change sequence is incomplete.")
    return usage, unpriced, deleted, key, max(acknowledged, through), skipped, window


def _validate_usage_timestamp(item: dict) -> None:
    if not isinstance(item, dict) or _parse_iso(str(item.get("occurred_at") or "")) is None:
        raise ValueError("Usage event requires a valid occurred_at timestamp.")


def apply_sync_push(
    database_path: Path,
    pricing: PricingEngine,
    *,
    machine_id: str,
    body: dict,
    now: datetime | None = None,
    expected_token_hash: str | None = None,
) -> dict:
    from spend_app.adapters.grok_native import drop_covered_calls, pushed_turn_elapsed_ms

    if not _MACHINE_ID_RE.fullmatch(machine_id) or machine_id in {"local", "all"}:
        raise ValueError("Invalid machine identifier.")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
    validate_push(body, moment)
    provider_cost_batch = validate_provider_costs(body["providerCosts"], moment) if "providerCosts" in body else None
    stamp = moment.isoformat().replace("+00:00", "Z")
    initialize(database_path)

    usage_items = body.get("usageEvents") or []
    unpriced_items = body.get("unpricedEvents") or []
    session_items = body.get("sessions") or []
    agent_items = body.get("agentRuns")
    quota_items = body.get("quotas") or []
    machine_meta = body.get("machine") if isinstance(body.get("machine"), dict) else {}

    if not all(
        isinstance(seq, list)
        for seq in (usage_items, unpriced_items, session_items, quota_items)
    ) or (agent_items is not None and not isinstance(agent_items, list)):
        raise ValueError("Sync collections must be JSON arrays.")

    # A computer still on an older BURNRATE may push Cursor usage, which
    # duplicates SuperGrok usage and is not counted.
    usage_items = [item for item in usage_items if not _retired_item(item)]
    unpriced_items = [item for item in unpriced_items if not _retired_item(item)]
    session_items = [item for item in session_items if not _retired_item(item)]
    if agent_items is not None:
        agent_items = [item for item in agent_items if not (
            isinstance(item, dict) and str(item.get("model_key") or item.get("modelKey") or "").startswith("cursor:"))]

    written = 0
    agents_changed = False
    quotas_changed = False
    usage_changed = False
    change_ack = None
    usage_window = None
    cost_ack = None
    costs_changed = False
    skipped_changes = 0
    legacy_conflicts = 0
    with connect(database_path) as connection:
        # Serialise acknowledgement with event writes. Retried or concurrent
        # pushes must never replay an older correction over a newer one.
        connection.execute("BEGIN IMMEDIATE")
        try:
            if expected_token_hash is not None:
                machine = _read_state(connection)["machines"].get(machine_id) or {}
                if machine.get("revoked") or not hmac.compare_digest(
                    str(machine.get("token_hash") or ""), expected_token_hash,
                ):
                    raise PermissionError("Machine credentials changed before synchronization.")
            if "usageChanges" in body:
                (usage_items, unpriced_items, deleted, ack_key, change_ack,
                 skipped_changes, usage_window) = _usage_change_batch(
                    connection, machine_id, body["usageChanges"], body.get("usageContinuity"))
                if usage_window is not None and not usage_window.accepted:
                    return {
                        "ok": True,
                        "machineId": machine_id,
                        "eventsWritten": 0,
                        "usageChangeSeq": change_ack,
                        "usageContinuity": usage_window.head,
                        "usageRetry": True,
                        "usageLegacyConflicts": count_legacy_conflicts(connection, machine_id),
                    }
                usage_changed = bool(usage_items or unpriced_items or deleted)
                for identifier in deleted:
                    raw_id = prefix_machine_id(machine_id, identifier)
                    for table in ("usage_events", "unpriced_usage_events", "coverage_gap_events", "pricing_gap_events"):
                        connection.execute(f"DELETE FROM {table} WHERE raw_id=?", (raw_id,))
            if provider_cost_batch is not None:
                cost_ack, costs_changed = apply_provider_costs(
                    connection, machine_id, provider_cost_batch, now=moment,
                )
            for item in usage_items:
                _validate_usage_timestamp(item)
                raw_id = prefix_machine_id(machine_id, str(item["raw_id"]))
                session_id = prefix_machine_id(
                    machine_id,
                    str(item["session_id"]) if item.get("session_id") else None,
                )
                try:
                    computed_cost, is_exact = _price_pushed_event(pricing, item)
                except UnpricedModelError:
                    unpriced = UnpricedUsageEvent(
                        source=str(item.get("source") or "remote"),
                        tool_key=str(item["tool_key"]),
                        model_key=str(item["model_key"]),
                        occurred_at=str(item["occurred_at"]),
                        session_id=session_id,
                        project=item.get("project"),
                        input_tokens=int(item.get("input_tokens") or 0),
                        cached_input_tokens=int(item.get("cached_input_tokens") or 0),
                        cache_write_tokens=int(item.get("cache_write_tokens") or 0),
                        cache_write_1h_tokens=int(item.get("cache_write_1h_tokens") or 0),
                        output_tokens=int(item.get("output_tokens") or 0),
                        reasoning_tokens=(
                            int(item["reasoning_tokens"])
                            if item.get("reasoning_tokens") is not None
                            else None
                        ),
                        unclassified_tokens=int(item.get("unclassified_tokens") or 0),
                        telemetry_complete=_pushed_telemetry_complete(item),
                        cost_usd=(
                            float(item["cost_usd"])
                            if item.get("cost_usd") is not None
                            else None
                        ),
                        raw_id=raw_id,
                        ingested_at=str(item.get("ingested_at") or stamp),
                        grok_turn_elapsed_ms=pushed_turn_elapsed_ms(connection, item, raw_id),
                        **admin_event_metadata(item),
                    )
                    if upsert_unpriced_event(connection, unpriced):
                        written += 1
                    continue

                event = UsageEvent(
                    source=str(item.get("source") or "remote"),
                    tool_key=str(item["tool_key"]),
                    model_key=str(item["model_key"]),
                    occurred_at=str(item["occurred_at"]),
                    session_id=session_id,
                    project=item.get("project"),
                    input_tokens=int(item.get("input_tokens") or 0),
                    cached_input_tokens=int(item.get("cached_input_tokens") or 0),
                    cache_write_tokens=int(item.get("cache_write_tokens") or 0),
                    cache_write_1h_tokens=int(item.get("cache_write_1h_tokens") or 0),
                    output_tokens=int(item.get("output_tokens") or 0),
                    reasoning_tokens=(
                        int(item["reasoning_tokens"])
                        if item.get("reasoning_tokens") is not None
                        else None
                    ),
                    cost_usd=(
                        float(item["cost_usd"])
                        if item.get("cost_usd") is not None
                        else None
                    ),
                    computed_cost_usd=computed_cost,
                    raw_id=raw_id,
                    ingested_at=str(item.get("ingested_at") or stamp),
                    is_exact=is_exact,
                    grok_turn_elapsed_ms=pushed_turn_elapsed_ms(connection, item, raw_id),
                    **admin_event_metadata(item),
                )
                if upsert_usage_event(connection, event):
                    written += 1

            for item in unpriced_items:
                _validate_usage_timestamp(item)
                raw_id = prefix_machine_id(machine_id, str(item["raw_id"]))
                session_id = prefix_machine_id(
                    machine_id,
                    str(item["session_id"]) if item.get("session_id") else None,
                )
                unpriced = UnpricedUsageEvent(
                    source=str(item.get("source") or "remote"),
                    tool_key=str(item["tool_key"]),
                    model_key=str(item["model_key"]),
                    occurred_at=str(item["occurred_at"]),
                    session_id=session_id,
                    project=item.get("project"),
                    input_tokens=int(item.get("input_tokens") or 0),
                    cached_input_tokens=int(item.get("cached_input_tokens") or 0),
                    cache_write_tokens=int(item.get("cache_write_tokens") or 0),
                    cache_write_1h_tokens=int(item.get("cache_write_1h_tokens") or 0),
                    output_tokens=int(item.get("output_tokens") or 0),
                    reasoning_tokens=(
                        int(item["reasoning_tokens"])
                        if item.get("reasoning_tokens") is not None
                        else None
                    ),
                    unclassified_tokens=int(item.get("unclassified_tokens") or 0),
                    telemetry_complete=_pushed_telemetry_complete(item),
                    cost_usd=(
                        float(item["cost_usd"])
                        if item.get("cost_usd") is not None
                        else None
                    ),
                    raw_id=raw_id,
                    ingested_at=str(item.get("ingested_at") or stamp),
                    grok_turn_elapsed_ms=pushed_turn_elapsed_ms(connection, item, raw_id),
                    **admin_event_metadata(item),
                )
                if upsert_unpriced_event(connection, unpriced):
                    written += 1

            # A Grok turn and its component calls can arrive out of order.
            grok_sessions = {
                prefix_machine_id(machine_id, str(item["session_id"]))
                for item in (*usage_items, *unpriced_items)
                if item.get("source") == "grok_local" and item.get("session_id")
            }
            if grok_sessions:
                drop_covered_calls(connection, grok_sessions)

            for item in session_items:
                sid = prefix_machine_id(machine_id, str(item["session_id"]))
                upsert_session(
                    connection,
                    session_id=sid,
                    tool_key=str(item["tool_key"]),
                    project=item.get("project"),
                    started_at=str(item["started_at"]),
                    ended_at=item.get("ended_at"),
                    model_key=str(item["model_key"]),
                )

            if agent_items is not None:
                seen_ids: set[str] = set()
                for item in agent_items:
                    rid = prefix_machine_id(machine_id, str(item["id"]))
                    seen_ids.add(rid)
                    if upsert_agent_run(
                        connection,
                        id=rid,
                        name=cap_agent_run_name(str(item["name"])),
                        model_key=item.get("model_key") or item.get("modelKey"),
                        state=str(item.get("state") or "LIVE"),
                        started_at=str(item.get("started_at") or item.get("startedAt") or stamp),
                        last_seen_at=str(item.get("last_seen_at") or item.get("lastSeenAt") or stamp),
                    ):
                        agents_changed = True
                if body.get("replaceAgentRuns", True):
                    prefix = f"m:{machine_id}:*"
                    existing_ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM agent_runs WHERE id GLOB ?", (prefix,)
                        )
                    ]
                    for old_id in existing_ids:
                        if old_id not in seen_ids:
                            connection.execute("DELETE FROM agent_runs WHERE id=?", (old_id,))
                            agents_changed = True

            for item in quota_items:
                provider_key = str(item["provider_key"])
                if provider_key == "cursor":
                    continue
                limit_key = str(item["limit_key"])
                unit_val = str(item.get("unit") or "pct")
                orig_source = str(item.get("source") or "").strip()
                if orig_source.startswith("sync:"):
                    # Authenticated machines can only update their own origins.
                    orig_source = orig_source.split(":", 2)[-1] if orig_source.count(":") >= 2 else ""
                sync_source = f"sync:{machine_id}:{orig_source}" if orig_source else f"sync:{machine_id}"
                polled_at = _parse_iso(str(item.get("polled_at") or stamp))
                if polled_at is None:
                    raise ValueError("Quota reading requires a valid timestamp.")
                polled_at = min(polled_at, moment + timedelta(minutes=5))
                polled_val = polled_at.isoformat(timespec="microseconds").replace("+00:00", "Z")
                existing = connection.execute(
                    "SELECT id, label, used, allowance, unit, pct, resets_at, source, is_payg, polled_at "
                    "FROM quotas WHERE provider_key=? AND limit_key=? AND source=? "
                    "AND julianday(polled_at) <= julianday(?) "
                    "ORDER BY julianday(polled_at) DESC, id DESC LIMIT 1",
                    (provider_key, limit_key, sync_source, (moment + timedelta(minutes=5)).isoformat()),
                ).fetchone()
                previous_poll = _parse_iso(existing["polled_at"]) if existing else None
                if previous_poll is not None and (
                    polled_at < previous_poll
                    or (polled_at == previous_poll and existing["unit"] == "unavailable")
                ):
                    continue
                label_val = str(item["label"])
                used_val = float(item["used"]) if item.get("used") is not None else None
                allowance_val = (
                    float(item["allowance"])
                    if item.get("allowance") is not None
                    else None
                )
                pct_val = float(item["pct"]) if item.get("pct") is not None else None
                resets_val = item.get("resets_at")
                payg_val = item.get("is_payg")
                fields = (
                    label_val,
                    used_val,
                    allowance_val,
                    unit_val,
                    pct_val,
                    resets_val,
                    sync_source,
                    int(bool(payg_val)) if payg_val is not None else None,
                )
                if limit_key == "balance" and unit_val == "usd" and used_val is not None:
                    record_balance_sample(connection, provider_key=provider_key, source=sync_source,
                                          usd=used_val, sampled_at=polled_val)
                if existing is not None and tuple(existing)[1:-1] == fields:
                    cursor_upd = connection.execute(
                        "UPDATE quotas SET polled_at=? WHERE id=? AND polled_at<>?",
                        (polled_val, existing["id"], polled_val),
                    )
                    if cursor_upd.rowcount:
                        quotas_changed = True
                    continue
                upsert_quota(
                    connection,
                    provider_key=provider_key,
                    limit_key=limit_key,
                    label=label_val,
                    unit=unit_val,
                    source=sync_source,
                    polled_at=polled_val,
                    used=used_val,
                    allowance=allowance_val,
                    pct=pct_val,
                    resets_at=resets_val,
                    is_payg=payg_val,
                )
                quotas_changed = True

            state = _read_state(connection)
            _preserve_audited_machine_names(connection, state)
            candidate_name = str(machine_meta.get("name") or "").strip()[:64]
            current = state["machines"].get(machine_id) or {}
            if candidate_name and not current.get("owner_name"):
                _ensure_unique_active_machine_name(state, candidate_name, exclude_machine_id=machine_id)
            rec = state["machines"].setdefault(
                machine_id,
                {
                    "id": machine_id,
                    "name": candidate_name or machine_id,
                    "os": str(machine_meta.get("os") or "")[:32],
                    "token_hash": "",
                    "created_at": stamp,
                    "events_pushed": 0,
                },
            )
            if candidate_name:
                rec["reported_name"] = candidate_name
                if not rec.get("owner_name"):
                    rec["name"] = candidate_name
            if machine_meta.get("os"):
                rec["os"] = str(machine_meta["os"]).strip()[:32]
            pushed_dev_id = _clean_device_id(machine_meta.get("deviceId"))
            if pushed_dev_id and not rec.get("device_id"):
                taken = any(
                    mid != machine_id
                    and isinstance(other, dict)
                    and not other.get("revoked")
                    and other.get("device_id") == pushed_dev_id
                    for mid, other in state["machines"].items()
                )
                if not taken:
                    rec["device_id"] = pushed_dev_id
            token_expires_at = rec.get("expires_at")
            rotation_required = bool(rec.get("rotation_required"))
            rec["last_seen_at"] = stamp
            rec["events_pushed"] = int(rec.get("events_pushed") or 0) + written
            _write_state(connection, state)
            run = IngestRun.start(connection, f"sync:{machine_id}")
            run.events_written = written
            run.finish(status="partial" if skipped_changes else "success", error=(
                f"{skipped_changes} usage observations have invalid timestamps and were skipped."
                if skipped_changes else None))
            if now is not None:
                connection.execute(
                    "UPDATE ingest_runs SET started_at=?, finished_at=? WHERE id=?",
                    (stamp, stamp, run.id),
                )
            prune_ingest_runs(connection, now=stamp)
            if change_ack is not None:
                connection.execute(
                    "INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                    (ack_key, str(change_ack)),
                )
            if usage_window is not None:
                commit_usage_window(connection, machine_id, usage_window)
                legacy_conflicts = count_legacy_conflicts(connection, machine_id)
        except Exception:
            raise

    if written > 0 or agents_changed or quotas_changed or usage_changed or costs_changed:
        STREAM_BROKER.publish(
            {
                "type": "sync",
                "machineId": machine_id,
                "eventsWritten": written,
                "at": stamp,
            }
        )
    return {
        "ok": True,
        "machineId": machine_id,
        "eventsWritten": written,
        "receivedAt": stamp,
        "tokenExpiresAt": token_expires_at,
        "rotationRequired": rotation_required,
        **({"providerCostsAck": cost_ack} if cost_ack is not None else {}),
        **({"usageChangeSeq": change_ack, "usageChangesSkipped": skipped_changes} if change_ack is not None else {}),
        **({"usageContinuity": usage_window.head} if usage_window is not None else {}),
        **({"usageLegacyConflicts": legacy_conflicts} if legacy_conflicts else {}),
    }


def export_sync_batch(
    database_path: Path,
    *,
    cursor: dict | None = None,
    limit: int = 500,
    pricing: PricingEngine | None = None,
) -> tuple[dict, dict]:
    """Read local-only incremental rows from ``database_path`` for pushing to a hub."""
    cursor = cursor or {}
    last_usage_id = int(cursor.get("usage_id") or 0)
    last_unpriced_id = int(cursor.get("unpriced_id") or 0)
    last_change_seq = int(cursor.get("change_seq") or 0)
    last_cost_seq = int(cursor.get(COST_SEQ) or 0)
    if limit < 1:
        raise ValueError("Sync batch limit must be positive.")
    initialize(database_path)
    device_id = get_or_create_device_id(database_path)
    usage_stream = cursor.get("usage_stream") or device_id
    with connect(database_path) as connection:
        # Journal identities and current payloads must share a read snapshot.
        connection.execute("BEGIN")
        cost_batch = export_provider_costs(connection, last_cost_seq, limit)
        usage_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM usage_events WHERE id > ? AND raw_id NOT LIKE 'm:%' ORDER BY id LIMIT ?",
                (last_usage_id, int(limit)),
            )
        ]
        unpriced_rows = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM unpriced_usage_events WHERE id > ? AND raw_id NOT LIKE 'm:%' ORDER BY id LIMIT ?",
                (last_unpriced_id, int(limit)),
            )
        ]
        next_usage_id = max([last_usage_id, *[int(r["id"]) for r in usage_rows]])
        next_unpriced_id = max([last_unpriced_id, *[int(r["id"]) for r in unpriced_rows]])
        journal = connection.execute(
            "SELECT seq,raw_id,mark,legacy FROM usage_change_marks WHERE seq>? ORDER BY seq LIMIT ?",
            (last_change_seq, int(limit)),
        ).fetchall()
        next_change_seq = journal[-1]["seq"] if journal else last_change_seq
        # Multiple mutations of one identity can be represented by its latest
        # state. Keep the last sequence so acknowledgement remains monotonic.
        identities = {row["raw_id"]: row["seq"] for row in journal}
        changes = []
        revisions = []
        for raw_id, seq in sorted(identities.items(), key=lambda item: item[1]):
            change = {"seq": seq, "raw_id": raw_id, "kind": "delete"}
            for table, kind, rows in (("usage_events", "usage", usage_rows),
                                      ("unpriced_usage_events", "unpriced", unpriced_rows)):
                row = connection.execute(f"SELECT * FROM {table} WHERE raw_id=?", (raw_id,)).fetchone()
                if row is not None:
                    event = clean_usage_cost(dict(row))
                    if _parse_iso(str(event.get("occurred_at") or "")) is None:
                        change.update(kind="skip", reason="invalid_timestamp")
                        break
                    rows.append(event.copy())  # Upserts also work with legacy hubs.
                    event.pop("id", None)
                    event.pop("occurred_epoch_us", None)
                    change.update(kind=kind, event=event)
                    break
            changes.append(change)
            revision = connection.execute(
                "SELECT seq,mark FROM usage_change_marks WHERE raw_id=? AND seq>? ORDER BY seq DESC LIMIT 1",
                (raw_id, seq),
            ).fetchone()
            if revision:
                revisions.append([seq, revision["seq"], revision["mark"]])
        usage_proof = {
            "stream": usage_stream,
            "afterMark": journal_mark(connection, last_change_seq),
            "marks": [[row["seq"], row["mark"], bool(row["legacy"])] for row in journal],
            "revisions": revisions,
        }
        if cursor.get("usage_rebase"):
            usage_proof["rebase"] = cursor["usage_rebase"]
        usage_rows = list({row["raw_id"]: row for row in usage_rows
                           if _parse_iso(str(row.get("occurred_at") or "")) is not None}.values())
        unpriced_rows = list({row["raw_id"]: row for row in unpriced_rows
                              if _parse_iso(str(row.get("occurred_at") or "")) is not None}.values())
        for row in (*usage_rows, *unpriced_rows):
            clean_usage_cost(row)
        uncertain_admin_ids: set[str] = set()
        if pricing is not None:
            for row in usage_rows:
                if row.get("source") not in ORGANIZATION_SOURCES:
                    continue
                try:
                    _price_pushed_event(pricing, row)
                except UnpricedModelError:
                    uncertain_admin_ids.add(row["raw_id"])
        if uncertain_admin_ids:
            unpriced_rows.extend(row for row in usage_rows if row["raw_id"] in uncertain_admin_ids)
            usage_rows = [row for row in usage_rows if row["raw_id"] not in uncertain_admin_ids]
        unpriced_rows = [_wire_unpriced_admin_event(row) for row in unpriced_rows]
        for change in changes:
            if change["raw_id"] in uncertain_admin_ids and change["kind"] == "usage":
                change["kind"] = "unpriced"
            if change["kind"] == "unpriced":
                change["event"] = _wire_unpriced_admin_event(change["event"])
        session_ids = {
            row["session_id"]
            for row in (*usage_rows, *unpriced_rows)
            if row.get("session_id")
        }
        sessions: list[dict] = []
        if session_ids:
            placeholders = ",".join("?" for _ in session_ids)
            sessions = [
                dict(row)
                for row in connection.execute(
                    f"SELECT * FROM sessions WHERE session_id IN ({placeholders})",
                    tuple(session_ids),
                )
            ]
        agent_runs = [
            dict(row)
            for row in connection.execute(
                "SELECT * FROM agent_runs WHERE id NOT LIKE 'm:%'"
            )
        ]
        raw_quotas = [
            dict(row)
            for row in connection.execute(
                """
                SELECT q.*
                FROM quotas q
                JOIN (
                    SELECT id, ROW_NUMBER() OVER (
                        PARTITION BY provider_key, limit_key, source
                        ORDER BY julianday(polled_at) DESC, id DESC
                    ) AS rank
                    FROM quotas
                    WHERE source NOT LIKE 'sync:%'
                ) latest ON q.id = latest.id AND latest.rank = 1
                """
            )
        ]
        try:
            from spend_app.codex_quota import read_rows as verify_codex_rows

            verified_quotas = verify_codex_rows(connection, rows=raw_quotas)
        except Exception:
            verified_quotas = raw_quotas
        quota_rows = [
            clean_quota_numbers({
                "provider_key": r["provider_key"],
                "limit_key": r["limit_key"],
                "label": r["label"],
                "used": r.get("used"),
                "allowance": r.get("allowance"),
                "unit": r.get("unit") or "pct",
                "pct": r.get("pct"),
                "resets_at": r.get("resets_at"),
                "source": r.get("source") or "",
                "polled_at": r.get("polled_at"),
                "is_payg": r.get("is_payg"),
            })
            for r in verified_quotas
            if r.get("provider_key") != "cursor"
        ]
        state = _read_state(connection)
        hub_name = state.get("hub_name") or _default_hub_name()

    next_cursor = {
        "usage_id": next_usage_id,
        "unpriced_id": next_unpriced_id,
        "change_seq": next_change_seq,
        "usage_stream": usage_stream,
    }
    cost_resume_at = cost_batch.pop("resumeAt")
    if cost_resume_at:
        next_cursor[COST_SEQ] = cost_resume_at
    for row in usage_rows:
        row.pop("id", None)
        row.pop("occurred_epoch_us", None)
    for row in unpriced_rows:
        row.pop("id", None)
        row.pop("occurred_epoch_us", None)

    payload = {
        "machine": {"name": hub_name, "os": sys.platform, "deviceId": device_id},
        "usageEvents": usage_rows,
        "unpricedEvents": unpriced_rows,
        "sessions": sessions,
        "agentRuns": agent_runs,
        "quotas": quota_rows,
        "replaceAgentRuns": True,
        "usageChanges": {"version": 1, "after": last_change_seq, "through": next_change_seq,
                         "changes": changes, "hasMore": len(journal) == limit},
        "usageContinuity": usage_proof,
        **({"providerCosts": cost_batch} if cost_batch["changes"] else {}),
    }
    return payload, next_cursor


def _cursor_key(hub_url: str) -> str:
    digest = hashlib.sha256(hub_url.strip().rstrip("/").encode("utf-8")).hexdigest()[:16]
    return f"{SYNC_CURSOR_PREFIX}{digest}"


def read_sync_cursor(database_path: Path, hub_url: str) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        row = connection.execute(
            "SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)
        ).fetchone()
    if not row or not row[0]:
        return {"usage_id": 0, "unpriced_id": 0}
    try:
        return json.loads(row[0])
    except ValueError:
        return {"usage_id": 0, "unpriced_id": 0}


_ANY_GENERATION = object()


def write_sync_cursor(
    database_path: Path,
    hub_url: str,
    cursor: dict,
    *,
    expected_generation=_ANY_GENERATION,
    expected_usage: dict | None = None,
    confirmed_usage: tuple[dict, dict] | None = None,
) -> None:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if expected_generation is not _ANY_GENERATION or expected_usage is not None:
            row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)).fetchone()
            current = json.loads(row[0]) if row else {}
            if expected_generation is not _ANY_GENERATION and current.get("generation") != expected_generation:
                raise RuntimeError("Hub connection changed; retry sync with the current configuration.")
            if expected_usage is not None and any(
                current.get(key) != expected_usage.get(key)
                for key in ("change_seq", "usage_stream", "usage_rebase")
            ):
                raise RuntimeError("Usage cursor changed; retry sync from the current checkpoint.")
        if confirmed_usage is not None:
            head, offered = confirmed_usage
            if not verified_reply(connection, head, offered, int(expected_usage.get("change_seq") or 0)):
                raise RuntimeError("Usage journal changed; retry sync from the current checkpoint.")
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_cursor_key(hub_url), json.dumps(cursor)),
        )
        if confirmed_usage is not None:
            prune_usage_changes(connection)


def prepare_spoke_sync_tx(connection, hub_url: str, token: str) -> str:
    """Seed a fresh receiver inside an open BEGIN IMMEDIATE transaction."""
    for table in ("usage_events", "unpriced_usage_events"):
        connection.execute(
            f"INSERT INTO usage_changes(raw_id) SELECT raw_id FROM {table} "
            "WHERE raw_id NOT LIKE 'm:%' ORDER BY id"
        )
    cursor = {"generation": secrets.token_hex(16), "credential_hash": _token_hash(token)}
    connection.execute(
        "INSERT INTO app_meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_cursor_key(hub_url), json.dumps(cursor)),
    )
    return cursor["generation"]


def prepare_spoke_sync(database_path: Path, hub_url: str, token: str) -> str:
    """Seed a fresh receiver from current rows without weakening journal order.

    Compacting acknowledged changes must not discard the initial snapshot.
    A generation and credential hash fence both in-flight and queued old pushes.
    """
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        return prepare_spoke_sync_tx(connection, hub_url, token)


def rotate_spoke_sync_credential(connection, hub_url: str, token: str) -> None:
    row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)).fetchone()
    cursor = json.loads(row[0]) if row else {}
    cursor.update(generation=secrets.token_hex(16), credential_hash=_token_hash(token))
    connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                       (_cursor_key(hub_url), json.dumps(cursor)))


def sync_once(
    settings,
    pricing: PricingEngine,
    *,
    hub_url: str,
    token: str,
    machine_name: str | None = None,
    collect_local: bool = True,
    client=None,
) -> dict:
    """Collect local usage/activity and push incremental rows to ``hub_url``."""
    from spend_app.hub_origin import hub_origin

    hub_url = hub_origin(hub_url)
    if collect_local:
        from spend_app.providers import REGISTRY
        from spend_app.quotas import poll_activity
        from spend_app.scheduler import _run_ingest_specs

        try:
            _run_ingest_specs(
                [(spec, {}) for spec in REGISTRY if spec.kind in {"local", "manual"}],
                settings,
                pricing,
            )
        except Exception:
            pass
        try:
            poll_activity(database_path=settings.database_path)
        except Exception:
            pass
        try:
            from spend_app.quotas import poll_quotas

            poll_quotas(database_path=settings.database_path, settings=settings)
        except Exception:
            pass

    try:
        from spend_app.connection_secrets import flush_pending_revokes
        flush_pending_revokes(settings.database_path, hub_url, client=client)
    except Exception:
        pass
    cursor = read_sync_cursor(settings.database_path, hub_url)
    generation = cursor.get("generation")
    if cursor.get("credential_hash") and not hmac.compare_digest(cursor["credential_hash"], _token_hash(token.strip())):
        raise RuntimeError("Hub connection changed; retry sync with the current configuration.")
    total_written = 0
    total_skipped = 0
    last_result: dict = {"ok": True, "eventsWritten": 0}
    endpoint = hub_url.strip().rstrip("/") + "/api/sync/push"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Content-Type": "application/json",
    }
    batch_limit = 1000

    def _run_batches(post_fn) -> None:
        nonlocal cursor, total_written, total_skipped, last_result
        legacy_after = None
        for batch_idx in range(15):
            wire_cursor = cursor if legacy_after is None else {**cursor, "change_seq": legacy_after}
            batch, next_cursor = export_sync_batch(
                settings.database_path, cursor=wire_cursor, limit=batch_limit, pricing=pricing
            )
            for key in ("generation", "credential_hash"):
                if key in cursor:
                    next_cursor[key] = cursor[key]
            if machine_name:
                batch["machine"]["name"] = machine_name
            if batch_idx > 0:
                batch["agentRuns"] = []
                batch["quotas"] = []
                batch["replaceAgentRuns"] = False
            response = post_fn(endpoint, json=batch, headers=headers)
            if response.status_code >= 400:
                from spend_app.redact import redact
                raise RuntimeError(f"Sync push failed ({response.status_code}): {redact(response.text)}")
            last_result = response.json()
            cost_batch = batch.get("providerCosts")
            costs_pending = cost_batch is not None and last_result.get("providerCostsAck") != cost_acknowledgement(cost_batch)
            if costs_pending:
                next_cursor[COST_SEQ] = int(cursor.get(COST_SEQ) or 0)
                last_result = {
                    **last_result,
                    "providerCostsPending": True,
                    "providerCostsWarning": "Upgrade the hub to synchronize provider-reported charges.",
                }
            total_written += int(last_result.get("eventsWritten") or 0)
            total_skipped += int(last_result.get("usageChangesSkipped") or 0)
            head = last_result.get("usageContinuity")
            offered = batch["usageContinuity"]
            after = int(cursor.get("change_seq") or 0)
            acknowledged = last_result.get("usageChangeSeq")
            with connect(settings.database_path) as connection:
                proved = verified_reply(connection, head, offered, after)
                legacy_mark = journal_mark(connection, acknowledged) if (
                    type(acknowledged) is int and acknowledged > after
                ) else None
            legacy_rebase = (
                isinstance(head, dict)
                and head == {"stream": None, "through": acknowledged, "mark": None}
                and type(acknowledged) is int
                and acknowledged > after
            )
            if proved and (head["through"] >= next_cursor["change_seq"] or last_result.get("usageRetry")):
                if last_result.get("usageRetry"):
                    # This request did not apply. Move only the position
                    # already proven by the hub before attempting the rest.
                    next_cursor["usage_id"] = int(cursor.get("usage_id") or 0)
                    next_cursor["unpriced_id"] = int(cursor.get("unpriced_id") or 0)
                next_cursor.update(usage_stream=head["stream"], change_seq=head["through"])
                write_sync_cursor(
                    settings.database_path, hub_url, next_cursor, expected_generation=generation,
                    expected_usage=cursor, confirmed_usage=(head, offered),
                )
                cursor = next_cursor
                if last_result.get("usageRetry"):
                    continue
            elif (
                last_result.get("usageRetry") and (can_rebase(head, after) or legacy_rebase)
                and head != cursor.get("usage_rebase")
            ):
                # The hub's position cannot be found in this copy of the
                # journal. Preserve the restored cursor and persist a fenced
                # recovery request so a network interruption can retry it.
                recovering = {**cursor, "usage_stream": secrets.token_hex(16), "usage_rebase": head}
                write_sync_cursor(
                    settings.database_path, hub_url, recovering,
                    expected_generation=generation, expected_usage=cursor,
                )
                cursor = recovering
                continue
            else:
                last_result = {
                    **last_result,
                    "usageChangesPending": bool(
                        batch["usageChanges"]["changes"] or head or cursor.get("usage_rebase")
                        or cursor.get("usage_legacy_seq")
                    ),
                    "warning": "The hub could not verify this usage checkpoint. Pending changes have been retained.",
                }
                # Support legacy upserts without treating their response as
                # acknowledgement of repair history.
                legacy_cursor = {
                    **cursor,
                    "change_seq": after,
                }
                if head is None:
                    legacy_cursor.update(
                        usage_id=next_cursor["usage_id"], unpriced_id=next_cursor["unpriced_id"],
                    )
                # A legacy hub can still consume ordinary batches. Its
                # numeric position is only a paging hint. Keep every pending
                # change and probe the confirmed prefix again on the next call.
                legacy_floor = legacy_after if legacy_after is not None else after
                if head is None and legacy_mark and acknowledged > legacy_floor and (
                    acknowledged == batch["usageChanges"]["through"]
                    or acknowledged >= int(cursor.get("usage_legacy_seq") or 0)
                ):
                    legacy_cursor.update(usage_legacy_seq=acknowledged, usage_legacy_mark=legacy_mark)
                    legacy_after = acknowledged
                else:
                    legacy_after = None
                write_sync_cursor(
                    settings.database_path, hub_url, legacy_cursor,
                    expected_generation=generation, expected_usage=cursor,
                )
                cursor = legacy_cursor
                if legacy_after is not None and (
                    batch["usageChanges"]["hasMore"]
                    or batch["usageChanges"]["through"] > legacy_after
                ):
                    continue
                break
            if last_result.get("usageChangesPending"):
                break
            if (
                len(batch.get("usageEvents") or []) < batch_limit
                and len(batch.get("unpricedEvents") or []) < batch_limit
                and not batch["usageChanges"]["hasMore"]
                and (cost_batch is None or costs_pending or not cost_batch["hasMore"])
            ):
                break
        if last_result.get("usageRetry") and not last_result.get("usageChangesPending"):
            last_result = {
                **last_result, "usageChangesPending": True,
                "warning": "Usage synchronization is waiting for a compatible checkpoint.",
            }
        if last_result.get("usageLegacyConflicts"):
            last_result = {
                **last_result, "usageChangesPending": True,
                "warning": "The hub preserved newer usage and retained conflicting legacy changes for recovery.",
            }

    if client is not None:
        _run_batches(client.post)
    else:
        import httpx

        with httpx.Client(timeout=30.0, trust_env=False) as http:
            _run_batches(http.post)
    return {**last_result, "eventsWritten": total_written,
            "cursor": {key: value for key, value in cursor.items() if key != "credential_hash"},
            **({"usageChangesSkipped": total_skipped,
                "invalidUsageWarning": "Some usage observations have invalid timestamps and were not synchronized."}
               if total_skipped else {})}


_HUB_PROXY_LOCK = threading.Lock()
# (hub, path, params) -> (monotonic read time, Hub payload, fresh). A payload
# marked stale is read again on next use but still serves while that read runs.
_HUB_PROXY_CACHE: dict[tuple, tuple[float, dict, bool]] = {}
_HUB_PROXY_RENEWING: set[tuple] = set()
_HUB_TEST_CLIENTS: dict[str, object] = {}


def register_hub_test_client(hub_url: str, client: object) -> None:
    """Allow in-process unit tests to route spoke read-through calls to a TestClient."""
    _HUB_TEST_CLIENTS[hub_url.strip().rstrip("/")] = client


def _vault_get_secret(database_path: Path, ref: str) -> str | None:
    try:
        from spend_app.connection_secrets import get_vault

        vault = get_vault(database_path)
        if vault is None:
            return None
        val = vault.get(ref)
        return str(val).strip() if isinstance(val, str) and val.strip() else None
    except Exception:
        return None


def _vault_put_secret(database_path: Path, ref: str, value: str) -> bool:
    try:
        from spend_app.connection_secrets import get_vault, verified_vault_put

        vault = get_vault(database_path)
        verified_vault_put(vault, value, ref)
        return True
    except Exception:
        return False


_OWNER_ROTATION_LOCKS_GUARD = threading.Lock()
_OWNER_ROTATION_LOCKS: dict[str, threading.Lock] = {}


def _owner_rotation_lock(database_path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(str(database_path)))
    with _OWNER_ROTATION_LOCKS_GUARD:
        lock = _OWNER_ROTATION_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _OWNER_ROTATION_LOCKS[key] = lock
        return lock


def migrate_legacy_spoke_secrets(database_path: Path, *, vault=None) -> dict[str, bool]:
    """Move legacy SQLite ``spokeConfig.token`` / ``ownerSession`` into the vault.

    Serializes with all credential publishers via ``db_secret_lock`` and
    ``BEGIN IMMEDIATE``, marks migrated credentials for rotation, persists a
    durable cleanup-pending marker before sanitation, and scrubs SQLite/WAL
    remnants via verified ``PRAGMA wal_checkpoint(TRUNCATE)`` + ``VACUUM``.
    """
    outcome = {"token": False, "ownerSession": False}
    if database_path is None:
        return outcome
    db_file = Path(database_path)
    if not db_file.is_file():
        return outcome

    from spend_app.connection_secrets import (
        CLEANUP_PENDING_META_KEY,
        MACHINE_TOKEN_VAULT_REF,
        OWNER_SESSION_VAULT_REF,
        db_secret_lock,
        get_vault,
        mark_cleanup_pending_in_tx,
        verified_vault_put,
    )
    from spend_app.setup_wizard import read_setup_state, write_setup_state

    with db_secret_lock(db_file):
        try:
            with connect(db_file) as connection:
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("BEGIN IMMEDIATE")
                latest = read_setup_state(connection)
                latest_spoke = dict(latest.get("spokeConfig") or {})
                legacy_token = str(latest_spoke.get("token") or "").strip()
                legacy_session = str(latest_spoke.get("ownerSession") or "").strip()
                marker_row = connection.execute(
                    "SELECT value FROM app_meta WHERE key=?",
                    (CLEANUP_PENDING_META_KEY,),
                ).fetchone()
                has_marker = marker_row is not None

                if not legacy_token and not legacy_session and not has_marker:
                    return outcome

                use_vault = None
                if legacy_token or legacy_session:
                    try:
                        use_vault = vault if vault is not None else get_vault(db_file)
                    except Exception:
                        use_vault = None
                    if use_vault is None:
                        return outcome

                changed = False
                token_migrated = False
                owner_migrated = False
                prev_gen = int(latest_spoke.get("credentialGeneration") or 0)

                if legacy_token:
                    try:
                        verified_vault_put(use_vault, legacy_token, MACHINE_TOKEN_VAULT_REF)
                        outcome["token"] = True
                        latest_spoke.pop("token", None)
                        latest_spoke["rotationRequired"] = True
                        token_migrated = True
                        changed = True
                    except Exception:
                        outcome["token"] = False

                if legacy_session:
                    try:
                        verified_vault_put(use_vault, legacy_session, OWNER_SESSION_VAULT_REF)
                        outcome["ownerSession"] = True
                        latest_spoke.pop("ownerSession", None)
                        latest_spoke["ownerSessionRotationRequired"] = True
                        owner_migrated = True
                        changed = True
                    except Exception:
                        outcome["ownerSession"] = False

                if changed:
                    latest_spoke["credentialGeneration"] = prev_gen + 1
                    latest["spokeConfig"] = latest_spoke
                    write_setup_state(connection, latest)
                    mark_cleanup_pending_in_tx(
                        connection,
                        token_migrated=token_migrated,
                        owner_migrated=owner_migrated,
                        state=latest,
                    )
                    connection.commit()
                else:
                    connection.rollback()

                if not changed and not has_marker:
                    return outcome

                prev_timeout_row = connection.execute("PRAGMA busy_timeout").fetchone()
                prev_timeout = int(prev_timeout_row[0]) if prev_timeout_row else 30000
                try:
                    connection.execute("PRAGMA busy_timeout = 100")
                    cp1 = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if not cp1 or int(cp1[0]) != 0 or int(cp1[1]) != int(cp1[2]):
                        raise RuntimeError(
                            f"WAL checkpoint incomplete or busy: {tuple(cp1) if cp1 else None}"
                        )
                    connection.execute(f"PRAGMA busy_timeout = {prev_timeout}")
                    connection.execute("VACUUM")
                    connection.execute("PRAGMA busy_timeout = 100")
                    cp2 = connection.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
                    if not cp2 or int(cp2[0]) != 0 or int(cp2[1]) != int(cp2[2]):
                        raise RuntimeError(
                            f"Post-VACUUM WAL checkpoint incomplete or busy: {tuple(cp2) if cp2 else None}"
                        )
                except Exception as exc:
                    LOGGER.warning(
                        "Legacy credential SQLite sanitation incomplete; keeping cleanup marker (%s)",
                        type(exc).__name__,
                    )
                    return outcome
                finally:
                    try:
                        connection.execute(f"PRAGMA busy_timeout = {prev_timeout}")
                    except Exception:
                        pass

                connection.execute("BEGIN IMMEDIATE")
                cur_state = read_setup_state(connection)
                cur_spoke = cur_state.get("spokeConfig") or {}
                if (
                    not str(cur_spoke.get("token") or "").strip()
                    and not str(cur_spoke.get("ownerSession") or "").strip()
                ):
                    connection.execute(
                        "DELETE FROM app_meta WHERE key=?",
                        (CLEANUP_PENDING_META_KEY,),
                    )
                connection.commit()
                try:
                    connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
                except Exception:
                    pass
        except Exception as exc:
            LOGGER.warning(
                "Legacy credential migration failed (%s)",
                type(exc).__name__,
            )
            return outcome
    return outcome


def _hub_post_with_cookie_isolation(hub_url: str, path: str, headers: dict, *, json_body: dict | None = None, client=None, timeout: float = 8.0):
    resolved_client = client if client is not None else _HUB_TEST_CLIENTS.get(hub_url)
    if resolved_client is not None:
        cookies = getattr(resolved_client, "cookies", None)
        saved_jar = list(cookies.jar) if cookies is not None and hasattr(cookies, "jar") else None
        if cookies is not None:
            cookies.clear()
        try:
            payload = json_body if json_body is not None else {}
            try:
                return resolved_client.post(path, json=payload, headers=headers, follow_redirects=False)
            except TypeError:
                return resolved_client.post(path, json=payload, headers=headers)
        finally:
            if cookies is not None and saved_jar is not None:
                cookies.clear()
                for cookie in saved_jar:
                    cookies.jar.set_cookie(cookie)
    import httpx
    from spend_app.hub_response import bounded_request
    with httpx.Client(timeout=timeout, trust_env=False, follow_redirects=False) as http:
        return bounded_request(http, "POST", f"{hub_url}{path}",
                               json=(json_body if json_body is not None else {}), headers=headers)


def ensure_fresh_owner_session(
    database_path: Path | None,
    hub_url: str,
    token: str,
    *,
    vault=None,
    client=None,
) -> str:
    """Rotate a migrated legacy owner session before the next hub call.

    Captures ``(token, credentialGeneration)`` before the network call and fences
    both success publication and failure clearing inside ``BEGIN IMMEDIATE``.
    Never substitutes an unrelated stored legacy session when a distinct fresh
    session token is passed.
    """
    clean_token = str(token or "").strip()
    if not clean_token or database_path is None:
        return clean_token
    db_file = Path(database_path)
    if not db_file.is_file():
        return clean_token

    from spend_app.auth_model import extract_cookie
    from spend_app.connection_secrets import (
        OWNER_SESSION_VAULT_REF,
        clear_spoke_credentials,
        credential_origin_allowed,
        mark_cleanup_pending_in_tx,
        publish_spoke_credentials,
        read_vault_secret,
        revoke_or_queue_owner_session,
    )
    from spend_app.error_codes import CodedError, HUB_PROTOCOL_VERSION, map_transport_error
    from spend_app.setup_wizard import read_setup_state

    with connect(db_file) as connection:
        spoke = (read_setup_state(connection).get("spokeConfig") or {})
    if not spoke.get("ownerSessionRotationRequired"):
        return clean_token
    initial_stored = (
        read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=vault)
        or str(spoke.get("ownerSession") or "").strip()
    )
    if not initial_stored or clean_token != initial_stored:
        return clean_token

    with _owner_rotation_lock(db_file):
        with connect(db_file) as connection:
            spoke = (read_setup_state(connection).get("spokeConfig") or {})
        vault_tok = read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=vault)
        stored_tok = vault_tok or str(spoke.get("ownerSession") or "").strip()
        captured_gen = int(spoke.get("credentialGeneration") or 0)
        if not spoke.get("ownerSessionRotationRequired"):
            if stored_tok and stored_tok != clean_token:
                if not credential_origin_allowed(db_file, stored_tok, hub_url, spoke, vault=vault):
                    raise CodedError("AUTH-107", status_code=401)
                return stored_tok
            if not stored_tok:
                raise CodedError("AUTH-107", status_code=401)
            return clean_token
        if not stored_tok or stored_tok != clean_token:
            return clean_token

        captured_token = stored_tok
        headers = {
            "Authorization": f"Bearer {captured_token}",
            "Content-Type": "application/json",
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        }
        try:
            response = _hub_post_with_cookie_isolation(
                hub_url, "/api/auth/session/rotate", headers, client=client
            )
        except Exception as exc:
            err_code, err_status = map_transport_error(exc)
            raise CodedError(err_code, status_code=err_status) from exc

        new_token = extract_cookie(response) if response.status_code == 200 else None
        try:
            data = response.json() if getattr(response, "content", b"") else {}
        except ValueError:
            data = None

        class _StaleOwnerLifecycle(Exception):
            pass

        def _check_lifecycle_fence(_conn, current_state) -> None:
            sp = current_state.get("spokeConfig") or {}
            cur_gen = int(sp.get("credentialGeneration") or 0)
            cur_tok = (
                read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=vault)
                or str(sp.get("ownerSession") or "").strip()
            )
            if (
                cur_gen != captured_gen
                or cur_tok != captured_token
                or not sp.get("ownerSessionRotationRequired")
                or sp.get("signedOut")
            ):
                raise _StaleOwnerLifecycle()

        def _fence_clear_on_err(_conn, current_state) -> None:
            sp = current_state.get("spokeConfig") or {}
            cur_gen = int(sp.get("credentialGeneration") or 0)
            cur_tok = (
                read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=vault)
                or str(sp.get("ownerSession") or "").strip()
            )
            if cur_gen != captured_gen or cur_tok != captured_token:
                raise _StaleOwnerLifecycle()

        if response.status_code != 200 or not new_token or not isinstance(data, dict):
            q_err: CodedError | None = None
            if response.status_code != 401:
                try:
                    revoke_or_queue_owner_session(
                        db_file, hub_url, captured_token, vault=vault, client=client
                    )
                except CodedError as qe:
                    q_err = qe
            try:
                clear_spoke_credentials(
                    db_file,
                    clear_owner_session=True,
                    vault=vault,
                    prepare_state=_check_lifecycle_fence,
                )
                clear_hub_proxy_cache()
            except _StaleOwnerLifecycle:
                pass
            if q_err is not None:
                raise q_err
            raise CodedError("AUTH-107", status_code=401)

        expires_at = data.get("expiresAt")

        def _apply(conn, st):
            sp = dict(st.get("spokeConfig") or {})
            sp["ownerSessionExpiresAt"] = expires_at
            sp["ownerSessionRotationRequired"] = False
            st["spokeConfig"] = sp
            mark_cleanup_pending_in_tx(conn, owner_invalidated=True, state=st)

        try:
            publish_spoke_credentials(
                db_file,
                owner_session=new_token,
                vault=vault,
                prepare_state=_check_lifecycle_fence,
                apply_state=_apply,
            )
        except _StaleOwnerLifecycle:
            revoke_or_queue_owner_session(
                db_file, hub_url, new_token, vault=vault, client=client
            )
            raise CodedError("AUTH-107", status_code=401) from None
        except CodedError:
            try:
                clear_spoke_credentials(
                    db_file,
                    clear_owner_session=True,
                    vault=vault,
                    prepare_state=_fence_clear_on_err,
                )
                clear_hub_proxy_cache()
            except (_StaleOwnerLifecycle, CodedError):
                pass
            revoke_or_queue_owner_session(
                db_file, hub_url, new_token, vault=vault, client=client
            )
            raise
        except Exception as exc:
            try:
                revoke_or_queue_owner_session(
                    db_file, hub_url, new_token, vault=vault, client=client
                )
            except CodedError as q_exc:
                raise q_exc from exc
            raise
        clear_hub_proxy_cache()
        return new_token


def clear_hub_proxy_cache() -> None:
    """Forget every Hub copy, e.g. after hub connection changes."""
    with _HUB_PROXY_LOCK:
        _HUB_PROXY_CACHE.clear()


def expire_hub_proxy_cache() -> None:
    """Read the Hub again on next use, keeping the last copy as a fallback."""
    with _HUB_PROXY_LOCK:
        for key, (fetched_at, data, _fresh) in list(_HUB_PROXY_CACHE.items()):
            _HUB_PROXY_CACHE[key] = (fetched_at, data, False)


class HubMutationError(RuntimeError):
    """A paired computer must not turn a rejected Hub write into a local save."""

    def __init__(
        self,
        message: str,
        status_code: int = 503,
        *,
        code: str | None = None,
        retry_after: int | None = None,
    ):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retry_after = retry_after


def hub_write_scope(state: dict, hub_url: str) -> tuple[str, str, int, str | None]:
    """Identify the destination captured by a setup confirmation."""
    account = state.get("account") or {}
    spoke = state.get("spokeConfig") or {}
    return (
        hub_url,
        str(account.get("machineId") or ""),
        int(spoke.get("credentialGeneration") or 0),
        account.get("connectedAt"),
    )


def post_to_configured_hub(
    settings,
    path: str,
    body: dict,
    *,
    strict: bool = False,
    cookie_out: dict | None = None,
    expected_plan_authority: str | None = None,
    expected_scope: tuple[str, str, int, str | None] | None = None,
) -> dict | None:
    """Forward a write mutation from a paired Spoke to the central Hub.

    With ``strict`` only an unpaired instance returns None; an enabled Spoke
    raises on failure so its caller cannot silently commit a local mutation.
    """
    try:
        from spend_app.setup_wizard import read_setup_state

        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
    except Exception as exc:
        if strict:
            raise HubMutationError("Could not read the hub configuration. Retry the change.") from exc
        return None
    if expected_plan_authority is not None:
        from spend_app.plan_service import plan_authority

        if expected_plan_authority != plan_authority(state):
            raise HubMutationError("The plan account changed. Reload plans.", 409)
    if state.get("role") != "spoke":
        return None
    spoke = state.get("spokeConfig") or {}
    if not spoke.get("enabled"):
        return None
    from spend_app.config import resolve_hub_url
    from spend_app.connection_secrets import credential_origin_allowed
    from spend_app.hub_origin import bound_hub_origin

    try:
        hub_url = bound_hub_origin(spoke, resolve_hub_url())
    except ValueError as exc:
        if strict:
            raise HubMutationError(str(exc), 422) from exc
        return None
    scope_changed = "The hub connection changed. Reload setup before changing plans."
    if expected_scope is not None and hub_write_scope(state, hub_url) != expected_scope:
        raise HubMutationError(scope_changed, 409)
    owner_tok = str(spoke.get("ownerSession") or "").strip() or (
        _vault_get_secret(settings.database_path, "burnrate-owner-session") or ""
    )
    token = owner_tok
    using_owner_session = bool(owner_tok)
    if path == "/api/machines" and not token:
        token = str(spoke.get("token") or "").strip() or (
            _vault_get_secret(settings.database_path, "burnrate-machine-token") or ""
        )
        using_owner_session = False
    if not credential_origin_allowed(settings.database_path, token, hub_url, spoke):
        if strict:
            raise HubMutationError("Reconnect this computer to its hub before saving plans.")
        return None
    try:
        from spend_app.connection_secrets import flush_pending_revokes
        flush_pending_revokes(settings.database_path, hub_url)
    except Exception:
        pass
    if using_owner_session:
        try:
            token = ensure_fresh_owner_session(settings.database_path, hub_url, token)
        except Exception as exc:
            if strict:
                err_code = getattr(exc, "code", None) or "AUTH-107"
                err_status = getattr(exc, "status_code", 401)
                raise HubMutationError(format_error(err_code), err_status, code=err_code) from exc
            return None
    if expected_scope is not None:
        from spend_app.connection_secrets import db_secret_lock

        with db_secret_lock(settings.database_path):
            with connect(settings.database_path) as connection:
                current = read_setup_state(connection)
            current_spoke = current["spokeConfig"]
            try:
                current_url = bound_hub_origin(current_spoke, resolve_hub_url())
            except ValueError as exc:
                raise HubMutationError(scope_changed, 409) from exc
            current_owner = str(current_spoke.get("ownerSession") or "").strip() or (
                _vault_get_secret(settings.database_path, "burnrate-owner-session") or ""
            )
            if (
                current.get("role") != "spoke"
                or not current_spoke.get("enabled")
                or hub_write_scope(current, current_url) != expected_scope
                or current_owner != token
            ):
                raise HubMutationError(scope_changed, 409)
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "X-BURNRATE-Request": "1",
        "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
    }
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    try:
        if test_client is not None:
            response = test_client.post(path, json=body, headers=headers)
        else:
            import httpx
            from spend_app.hub_response import bounded_request

            with httpx.Client(timeout=5.0, trust_env=False) as http:
                response = bounded_request(http, "POST", f"{hub_url}{path}", json=body, headers=headers)
        if not 200 <= response.status_code < 300:
            if strict:
                status = response.status_code
                message = "The hub could not save this change. Retry with the same request identifier."
                err_code = None
                retry_after = None
                try:
                    payload = response.json()
                except (ValueError, AttributeError):
                    payload = None
                if isinstance(payload, dict) and isinstance(payload.get("error"), dict):
                    mapped = map_http_error(
                        status,
                        payload,
                        headers=getattr(response, "headers", None),
                        endpoint_path=path,
                    )
                    err_code = mapped.code
                    retry_after = mapped.retry_after
                    message = format_error(err_code, retry_after=retry_after)
                    status = mapped.status_code
                elif status in {400, 401, 403, 409, 413, 415, 422, 429}:
                    if isinstance(payload, dict):
                        error = payload.get("error")
                        if isinstance(error, str) and error:
                            message = error
                    if status in {401, 403, 429}:
                        mapped = map_http_error(
                            status,
                            payload,
                            headers=getattr(response, "headers", None),
                            endpoint_path=path,
                        )
                        err_code = mapped.code
                        retry_after = mapped.retry_after
                else:
                    status = 503
                    err_code = "HUB-501"
                raise HubMutationError(message, status, code=err_code, retry_after=retry_after)
            return None
        data = response.json()
        if not isinstance(data, dict):
            if strict:
                raise HubMutationError("The hub returned an invalid save response. Retry with the same request identifier.")
            return None
        if cookie_out is not None:
            from spend_app.auth_model import extract_cookie
            cookie_out["session"] = extract_cookie(response)
        clear_hub_proxy_cache()
        return data
    except HubMutationError:
        raise
    except Exception as exc:
        if strict:
            err_code, err_status = map_transport_error(exc)
            raise HubMutationError(
                "Could not reach the hub. Retry with the same request identifier.",
                err_status,
                code=err_code,
            ) from exc
        return None


_EXPIRED_HUB_QUOTA_REASON = "Synced quota window reset; wait for a fresh reading. No new zero was inferred."


def _mask_expired_hub_quotas(data: dict, now: datetime) -> dict:
    """Hub-proxy counterpart of ``codex_quota.mask_expired_synced``.

    Every Hub quota row is another machine's last reading, and an older Hub
    still serves rows whose window already reset. Mask those on the Spoke
    (capacity rows and /api/spend/limits windows) without inferring a zero.
    Returns a copy; the cached Hub payload is never mutated.
    """
    from spend_app.codex_quota import timestamp

    def expired(value) -> bool:
        reset = timestamp(value)
        return reset is not None and reset <= now

    result = dict(data)
    if isinstance(data.get("capacity"), list):
        cards = []
        for card in data["capacity"]:
            rows = card.get("rows") if isinstance(card, dict) else None
            if not isinstance(rows, list) or not any(
                isinstance(row, dict) and not row.get("isPayg") and row.get("unit") != "unavailable" and expired(row.get("resetsAt"))
                for row in rows
            ):
                cards.append(card)
                continue
            new_rows = []
            for row in rows:
                if isinstance(row, dict) and not row.get("isPayg") and row.get("unit") != "unavailable" and expired(row.get("resetsAt")):
                    name = str(row.get("label") or row.get("limitKey") or "Quota window").split(" — ", 1)[0]
                    row = {
                        **row,
                        "label": f"{name} — {_EXPIRED_HUB_QUOTA_REASON}",
                        "pct": None,
                        "used": None,
                        "allowance": None,
                        "unit": "unavailable",
                        "resetsAt": None,
                        "etaLabel": None,
                        "forecast": None,
                    }
                new_rows.append(row)
            updated = {**card, "rows": new_rows}
            if not updated.get("isPayg"):
                primary = next((r for r in new_rows if isinstance(r, dict) and r.get("isPrimary")), None)
                if primary is not None:
                    updated["primaryPct"] = primary.get("pct")
                    updated["peakPct"] = primary.get("pct")
                    if primary.get("pct") is None:
                        updated["forecast"] = None
            cards.append(updated)
        result["capacity"] = cards
    if isinstance(data.get("providers"), list):
        providers = []
        for provider in data["providers"]:
            windows = provider.get("windows") if isinstance(provider, dict) else None
            if not isinstance(windows, list):
                providers.append(provider)
                continue
            # snapshot_limits lists only windows with a value, so a masked
            # window is dropped rather than shown as an empty reading.
            kept = [w for w in windows if not (isinstance(w, dict) and expired(w.get("resetAt")))]
            if len(kept) == len(windows):
                providers.append(provider)
                continue
            providers.append({**provider, "windows": kept, **({} if kept else {"status": "unavailable"})})
        result["providers"] = providers
    return result


def _overlay_fresher_local_quotas(database_path: Path, capacity: list[dict]) -> list[dict]:
    """Overlay fresher local measured quota rows onto Hub capacity cards for immediate Spoke display.

    Only the newest local observation per limit counts. When that observation
    is "unavailable", or its window has already reset, an older reading is not
    resurrected in its place, and a local reading never replaces a newer Hub one.
    A lane the Hub has no card for yet (an older Hub, or one that has not
    received this computer's first sync) is shown from the local readings.
    """
    from spend_app.codex_quota import timestamp

    try:
        with connect(database_path) as connection:
            raw_quotas = [
                dict(row)
                for row in connection.execute(
                    """
                    SELECT q.*
                    FROM quotas q
                    JOIN (
                        SELECT provider_key, limit_key, MAX(id) AS max_id
                        FROM quotas
                        WHERE source NOT LIKE 'sync:%'
                        GROUP BY provider_key, limit_key
                    ) latest ON q.id = latest.max_id
                    """
                )
            ]
            try:
                from spend_app.codex_quota import read_rows as verify_codex_rows

                verified_quotas = verify_codex_rows(connection, rows=raw_quotas)
            except Exception:
                verified_quotas = raw_quotas
    except Exception:
        return capacity

    now_utc = datetime.now(UTC)
    local_by_key: dict[tuple[str, str], dict] = {}
    for row in verified_quotas:
        if str(row.get("unit") or "") == "unavailable" or row.get("provider_key") == "cursor":
            continue
        reset = timestamp(row.get("resets_at"))
        if reset is not None and reset <= now_utc:
            continue
        local_by_key[(str(row["provider_key"]), str(row["limit_key"]))] = row

    if not local_by_key:
        return capacity

    def not_older(local_polled, hub_polled) -> bool:
        hub_at = timestamp(hub_polled)
        local_at = timestamp(local_polled)
        return hub_at is None or (local_at is not None and local_at >= hub_at)

    def balance(limit_key: str, used, unit) -> bool:
        # An account balance is a dollar amount with no percentage.
        return limit_key == "balance" and used is not None and unit == "usd"

    from spend_app.aggregate import _capacity_from_rows, _capacity_limit_rank, _eta_label, _money, provider_meta

    merged_cards: list[dict] = []
    # Balances read on this computer get their runway from its own history.
    local_balances: set[str] = set()
    for card in capacity:
        if not isinstance(card, dict) or not isinstance(card.get("rows"), list):
            if isinstance(card, dict):
                merged_cards.append(card)
            continue
        provider_key = str(card.get("providerKey") or "")
        updated_card = dict(card)
        meta = provider_meta(provider_key)
        if meta:
            updated_card["providerName"] = meta.get("providerName") or updated_card.get("providerName")
            if not updated_card.get("plan") and meta.get("plan"):
                updated_card["plan"] = meta["plan"]
            updated_card["isPayg"] = bool(meta.get("isPayg", updated_card.get("isPayg")))
            for name in ("shortName", "accountLabel", "toolKey"):
                if meta.get(name):
                    updated_card[name] = meta[name]
        new_rows: list[dict] = []
        for row_item in card["rows"]:
            if not isinstance(row_item, dict):
                continue
            limit_key = str(row_item.get("limitKey") or "")
            local_row = local_by_key.get((provider_key, limit_key))
            hub_has_reading = row_item.get("unit") != "unavailable" and (
                row_item.get("pct") is not None or balance(limit_key, row_item.get("used"), row_item.get("unit")))
            local_balance = local_row is not None and balance(limit_key, local_row.get("used"), local_row.get("unit"))
            if (
                local_row is not None
                and (local_row.get("pct") is not None or local_balance)
                and (not hub_has_reading or not_older(local_row.get("polled_at"), row_item.get("polledAt")))
            ):
                updated_row = dict(row_item)
                updated_row["label"] = local_row.get("label") or row_item.get("label")
                updated_row["used"] = float(local_row["used"]) if local_row.get("used") is not None else None
                updated_row["allowance"] = float(local_row["allowance"]) if local_row.get("allowance") is not None else None
                updated_row["unit"] = local_row.get("unit") or row_item.get("unit") or "pct"
                updated_row["pct"] = float(local_row["pct"]) if local_row.get("pct") is not None else None
                updated_row["resetsAt"] = local_row.get("resets_at")
                updated_row["etaLabel"] = "available" if local_balance else _eta_label(local_row.get("resets_at"), now_utc)
                updated_row["source"] = local_row.get("source") or row_item.get("source")
                updated_row["polledAt"] = local_row.get("polled_at") or row_item.get("polledAt")
                if local_balance:
                    local_balances.add(provider_key)
                new_rows.append(updated_row)
            else:
                new_rows.append(row_item)
        for idx, r in enumerate(new_rows):
            if isinstance(r, dict):
                r["order"] = _capacity_limit_rank(provider_key, str(r.get("limitKey") or ""))
        new_rows.sort(key=lambda r: (int(r.get("order", 100)), r.get("pct") is None))
        for idx, r in enumerate(new_rows):
            if isinstance(r, dict):
                r["isPrimary"] = idx == 0
        updated_card["rows"] = new_rows
        polled_candidates = [str(r["polledAt"]) for r in new_rows if isinstance(r, dict) and r.get("polledAt")]
        if polled_candidates:
            updated_card["polledAt"] = max(polled_candidates)
        if not updated_card.get("isPayg") and new_rows:
            primary_row = next((r for r in new_rows if r.get("isPrimary")), new_rows[0])
            primary_pct = primary_row.get("pct")
            updated_card["primaryPct"] = primary_pct
            updated_card["peakPct"] = primary_pct
        funds = next((r["used"] for r in new_rows if isinstance(r, dict) and r.get("limitKey") == "balance"
                      and r.get("used") is not None and r.get("unit") == "usd"), None)
        if funds is not None:
            updated_card["fundsRemainingUsd"] = _money(Decimal(str(funds)))
        merged_cards.append(updated_card)
    hub_keys = {str(card.get("providerKey") or "") for card in merged_cards if isinstance(card, dict)}
    local_only = [
        row for (provider_key, _limit_key), row in local_by_key.items()
        if provider_key not in hub_keys and provider_meta(provider_key)
    ]
    if local_only:
        keys = {str(row["provider_key"]) for row in local_only}
        # The builder also adds placeholders for every built-in provider; keep only these lanes.
        merged_cards.extend(
            card for card in _capacity_from_rows(local_only, now=now_utc, month_to_date={})
            if card["providerKey"] in keys
        )
        local_balances |= keys
    if local_balances:
        try:
            with connect(database_path) as connection:
                apply_balance_runway(merged_cards, connection, now=now_utc, providers=local_balances, local_only=True)
        except Exception:
            pass
    return merged_cards


def fetch_from_configured_hub(
    settings,
    path: str,
    params: dict | None = None,
    *,
    ttl_seconds: float = 1.0,
    stale_seconds: float = 0.0,
    read_timeout: float = 8.0,
) -> dict | None:
    """When this instance is a paired Spoke, read unified dashboard data from the Hub.

    A copy younger than ``ttl_seconds`` is served as is (bounded by
    ``stale_seconds`` when positive). With positive ``stale_seconds``, a copy
    younger than ``stale_seconds`` is served at once while one background read
    renews it, and a cold cache returns immediately so the caller can paint
    local data; copies at or past ``stale_seconds`` require a synchronous read
    and are never served on failure. A failed blocking read (``stale_seconds``
    of 0) falls back to the last copy of any age unless rejected (401/403).
    """
    try:
        from spend_app.setup_wizard import read_setup_state

        with connect(settings.database_path) as connection:
            state = read_setup_state(connection)
    except Exception:
        return None
    if state.get("role") != "spoke":
        return None
    spoke = state.get("spokeConfig") or {}
    if not spoke.get("enabled"):
        return None
    from spend_app.config import resolve_hub_url
    from spend_app.connection_secrets import credential_origin_allowed
    from spend_app.hub_origin import bound_hub_origin

    try:
        hub_url = bound_hub_origin(spoke, resolve_hub_url())
    except ValueError:
        return None
    from spend_app.auth_model import digest, moment, parse
    token = str(spoke.get("ownerSession") or "").strip() or (
        _vault_get_secret(settings.database_path, "burnrate-owner-session") or ""
    )
    expiry = parse(spoke.get("ownerSessionExpiresAt"))
    if expiry is not None and expiry <= moment():
        return None
    if not credential_origin_allowed(settings.database_path, token, hub_url, spoke):
        return None
    try:
        from spend_app.connection_secrets import flush_pending_revokes
        flush_pending_revokes(settings.database_path, hub_url)
    except Exception:
        pass
    try:
        token = ensure_fresh_owner_session(settings.database_path, hub_url, token)
    except Exception:
        return None
    token_digest = digest(token)

    clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
    cache_key = (hub_url, path, tuple(sorted(clean_params.items())), token_digest)

    def read() -> dict | None:
        return _read_hub_copy(cache_key, hub_url, token, path, clean_params, timeout=read_timeout)

    with _HUB_PROXY_LOCK:
        if token_digest in _HUB_REJECTED_CREDENTIALS:
            return None
        start_gen = _HUB_CREDENTIAL_GENERATIONS.get(token_digest, 0)
        cached = _HUB_PROXY_CACHE.get(cache_key)

    def _fenced_display(payload: dict) -> dict | None:
        display = _hub_copy_for_display(settings, payload)
        with _HUB_PROXY_LOCK:
            if token_digest in _HUB_REJECTED_CREDENTIALS or _HUB_CREDENTIAL_GENERATIONS.get(token_digest, 0) != start_gen:
                return None
        return display

    if cached:
        fetched_at, payload, fresh = cached
        age = time.monotonic() - fetched_at
        if fresh and age < ttl_seconds and (stale_seconds <= 0 or age < stale_seconds):
            return _fenced_display(payload)
        if stale_seconds > 0 and age < stale_seconds:
            _renew_hub_copy_in_background(cache_key, read)
            return _fenced_display(payload)
    elif stale_seconds > 0:
        _renew_hub_copy_in_background(cache_key, read)
        return None
    data = read()
    if data is None:
        if cached and (stale_seconds <= 0 or (time.monotonic() - cached[0]) < stale_seconds):
            return _fenced_display(cached[1])
        return None
    return _fenced_display(data)


_HUB_REJECTED_CREDENTIALS: set[str] = set()
_HUB_CREDENTIAL_GENERATIONS: dict[str, int] = {}


def _read_hub_copy(cache_key: tuple, hub_url: str, token: str, path: str, params: dict,
                   timeout: float = 8.0) -> dict | None:
    cred_key = cache_key[3]
    with _HUB_PROXY_LOCK:
        if cred_key in _HUB_REJECTED_CREDENTIALS:
            return None
        start_gen = _HUB_CREDENTIAL_GENERATIONS.get(cred_key, 0)
    from spend_app.hub_origin import hub_origin

    try:
        hub_url = hub_origin(hub_url)
    except ValueError:
        return None
    headers = {"Authorization": f"Bearer {token}"}
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    try:
        if test_client is not None:
            response = test_client.get(path, params=params, headers=headers)
        else:
            import httpx
            from spend_app.hub_response import bounded_request

            with httpx.Client(timeout=timeout, trust_env=False) as http:
                response = bounded_request(http, "GET", f"{hub_url}{path}", params=params, headers=headers)
        if response.status_code in {401, 403}:
            from spend_app.auth_model import digest
            key = digest(token)
            with _HUB_PROXY_LOCK:
                _HUB_REJECTED_CREDENTIALS.add(key)
                _HUB_CREDENTIAL_GENERATIONS[key] = _HUB_CREDENTIAL_GENERATIONS.get(key, 0) + 1
                for old_key in list(_HUB_PROXY_CACHE):
                    if len(old_key) > 3 and old_key[3] == key:
                        _HUB_PROXY_CACHE.pop(old_key, None)
            return None
        if response.status_code != 200:
            return None
        data = response.json()
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    # An older Hub may still report Cursor, which BURNRATE no longer tracks.
    if isinstance(data.get("capacity"), list):
        data["capacity"] = [
            item for item in data["capacity"]
            if isinstance(item, dict) and item.get("providerKey") != "cursor"
        ]
    if isinstance(data.get("providers"), list):
        data["providers"] = [
            item for item in data["providers"]
            if isinstance(item, dict) and item.get("key") != "cursor" and item.get("providerKey") != "cursor"
        ]
    with _HUB_PROXY_LOCK:
        if cred_key in _HUB_REJECTED_CREDENTIALS or _HUB_CREDENTIAL_GENERATIONS.get(cred_key, 0) != start_gen:
            return None
        _HUB_PROXY_CACHE[cache_key] = (time.monotonic(), data, True)
    return data


def _renew_hub_copy_in_background(cache_key: tuple, read) -> None:
    with _HUB_PROXY_LOCK:
        if cache_key in _HUB_PROXY_RENEWING:
            return
        _HUB_PROXY_RENEWING.add(cache_key)

    def run() -> None:
        try:
            read()
        finally:
            with _HUB_PROXY_LOCK:
                _HUB_PROXY_RENEWING.discard(cache_key)

    threading.Thread(target=run, name="burnrate-hub-read", daemon=True).start()


def _hub_copy_for_display(settings, data: dict) -> dict:
    # Mask before the overlay so a fresher local reading still wins. Both run
    # on every read, so a new local reading shows without a new Hub read.
    data = _mask_expired_hub_quotas(data, datetime.now(UTC))
    if isinstance(data.get("capacity"), list):
        data["capacity"] = _overlay_fresher_local_quotas(settings.database_path, copy.deepcopy(data["capacity"]))
    return data


def _resolve_stream_machines(settings) -> list[dict]:
    hub_data = fetch_from_configured_hub(settings, "/api/machines", ttl_seconds=1.0)
    if hub_data is not None and isinstance(hub_data.get("machines"), list):
        return hub_data["machines"]
    with connect(settings.database_path) as connection:
        return list_machines(connection)


def sync_router(settings, pricing: PricingEngine) -> APIRouter:
    router = APIRouter()

    @router.get("/api/machines")
    def get_machines(request: Request):
        if request.state.principal.kind == "machine":
            with connect(settings.database_path) as connection:
                machines = [m for m in list_machines(connection) if m["id"] == request.state.principal.id]
            return JSONResponse({"machines": machines})
        hub_data = fetch_from_configured_hub(settings, "/api/machines")
        if hub_data is not None and isinstance(hub_data.get("machines"), list):
            return JSONResponse(hub_data, headers={"Cache-Control": "no-store"})
        with connect(settings.database_path) as connection:
            machines = list_machines(connection)
        return JSONResponse({"machines": machines}, headers={"Cache-Control": "no-store"})

    @router.post("/api/machines")
    async def mutate_machines(request: Request):
        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        from spend_app.setup_wizard import RequestTooLargeError, _bounded_json
        try:
            body = await _bounded_json(request)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            action = str(body.get("action") or "register").strip().lower()
            request.state.audit_action = "machine." + (action if action in {"register", "rename", "revoke"} else "change")
            if action in {"rename", "revoke"}:
                with connect(settings.database_path) as connection:
                    if str(body.get("id") or "") in _read_state(connection)["machines"]:
                        request.state.audit_target = str(body["id"])
            if getattr(request.state, "auth_kind", "anonymous") == "machine":
                own_id = getattr(request.state, "machine_id", None)
                if action not in {"rename", "revoke"} or str(body.get("id") or "") != str(own_id):
                    return error_response("AUTH-107", status_code=403)
            if action in {"revoke", "rename"}:
                hub_res = post_to_configured_hub(settings, "/api/machines", body)
                if isinstance(hub_res, dict) and isinstance(hub_res.get("machines"), list):
                    return JSONResponse(hub_res, headers={"Cache-Control": "no-store"})
            if action == "register":
                raw_device_id = body.get("deviceId")
                device_id = str(raw_device_id).strip() if raw_device_id is not None else None
                if device_id == "":
                    device_id = None
                result = register_or_reconnect_machine(
                    settings.database_path,
                    name=str(body.get("name") or ""),
                    os_name=str(body.get("os") or ""),
                    device_id=device_id,
                    actor=request.state.principal,
                )
                request.state.audit_target = result["machine"]["id"]
                result["syncCommand"] = (
                    "python -m spend_app sync --token-file <path-to-token-file>"
                )
                result["syncHint"] = (
                    "Write the token to a private file first; the token is shown once in this response."
                )
                request.state.audit_recorded = True
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "revoke":
                result = revoke_machine(
                    settings.database_path,
                    machine_id=str(body.get("id") or ""),
                    actor=request.state.principal,
                )
                if request.state.principal.kind == "machine":
                    result["machines"] = []
                request.state.audit_recorded = True
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "rename":
                result = rename_machine(
                    settings.database_path,
                    machine_id=str(body.get("id") or ""),
                    actor=request.state.principal,
                    name=str(body.get("name") or ""),
                )
                if request.state.principal.kind == "machine":
                    result["machines"] = [m for m in result["machines"] if m["id"] == request.state.principal.id]
                request.state.audit_recorded = True
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            return error_response("APP-901", status_code=422)
        except RequestTooLargeError:
            return error_response("REQ-401", status_code=422)
        except HTTPException as exc:
            if exc.status_code == 403:
                return error_response("NET-002", status_code=403)
            return error_response("APP-901", status_code=exc.status_code)
        except ValueError as exc:
            low = str(exc).lower()
            if "unknown machine" in low or "cannot revoke" in low or ("revoked" in low and "multiple or revoked" not in low):
                return error_response("DEV-301", status_code=422)
            return error_response("DEV-302", status_code=422)

    @router.post("/api/machines/{machine_id}/data/delete")
    async def remove_machine_data(request: Request, machine_id: str, confirm: str = Query(default="")):
        from spend_app.connection_api import central_server
        from spend_app.machine_data import delete_machine_data

        actor = getattr(request.state, "principal", None)
        if not central_server() or actor is None or not actor.permits("admin.write"):
            return error_response("AUTH-107", status_code=403)
        if confirm != machine_id:
            return error_response("APP-901", status_code=422)
        try:
            result = await run_in_threadpool(delete_machine_data, settings.database_path, machine_id, actor=actor)
        except ValueError:
            return error_response("APP-901", status_code=422)
        return JSONResponse(result, headers={"Cache-Control": "no-store"})

    @router.post("/api/machines/self/revoke")
    async def self_revoke_machine(request: Request):
        proto_err = check_protocol_version(request.headers.get("x-burnrate-protocol"))
        if proto_err is not None:
            return error_response(proto_err, status_code=426)
        principal = getattr(getattr(request, "state", None), "principal", None)
        if principal is None or principal.kind != "machine" or not principal.id:
            return error_response("AUTH-107", status_code=403)
        if request.headers.get("x-burnrate-request") != "1":
            return error_response("NET-002", status_code=403)
        from spend_app.setup_wizard import RequestTooLargeError

        try:
            limit = 65536
            declared = request.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > limit:
                raise RequestTooLargeError("Request too large.")
            chunks = bytearray()
            async for chunk in request.stream():
                if len(chunks) + len(chunk) > limit:
                    raise RequestTooLargeError("Request too large.")
                chunks.extend(chunk)
            body: dict = {}
            if chunks.strip():
                content_type = request.headers.get("content-type", "").split(";")[0].strip().lower()
                if content_type != "application/json":
                    return error_response("APP-901", status_code=415)
                parsed = json.loads(
                    chunks, parse_constant=lambda _: (_ for _ in ()).throw(ValueError())
                )
                if not isinstance(parsed, dict):
                    raise ValueError("Expected a JSON object.")
                body = parsed
            target_id = str(body.get("id") or "").strip() if body.get("id") is not None else ""
            if target_id and target_id != str(principal.id):
                request.state.audit_action = "machine.revoke"
                request.state.audit_target = target_id
                return error_response("AUTH-107", status_code=403)
            request.state.audit_action = "machine.revoke"
            request.state.audit_target = principal.id
            revoke_machine(
                settings.database_path,
                machine_id=principal.id,
                actor=principal,
            )
            request.state.audit_recorded = True
            return JSONResponse(
                {"ok": True, "machineId": principal.id},
                headers={"Cache-Control": "no-store"},
            )
        except RequestTooLargeError:
            return error_response("REQ-401", status_code=422)
        except ValueError as exc:
            low = str(exc).lower()
            if "unknown machine" in low or "cannot revoke" in low or "revoked" in low:
                return error_response("DEV-301", status_code=422)
            return error_response("APP-901", status_code=422)

    @router.post("/api/sync/trigger")
    async def sync_trigger():
        from starlette.concurrency import run_in_threadpool
        from spend_app import aggregate
        from spend_app.quotas import nudge_quota_lanes, poll_quotas
        from spend_app.setup_wizard import (
            maybe_run_configured_spoke_sync,
            public_setup_state,
            read_setup_state,
        )

        def _do_trigger() -> dict:
            initialize(settings.database_path)
            try:
                nudge_quota_lanes(database_path=settings.database_path, now=datetime.now(UTC))
            except Exception:
                pass
            try:
                poll_quotas(database_path=settings.database_path, settings=settings)
            except Exception:
                pass
            sync_res = maybe_run_configured_spoke_sync(settings, pricing)
            expire_hub_proxy_cache()
            aggregate.reset_memo()
            STREAM_BROKER.publish({"type": "sync_trigger", "at": utc_now()})
            with connect(settings.database_path) as connection:
                setup_state = public_setup_state(read_setup_state(connection), database_path=settings.database_path)
                machines = list_machines(connection)
            return {
                "ok": bool(sync_res.get("ok", True)) if isinstance(sync_res, dict) else True,
                "triggeredAt": utc_now(),
                "setup": setup_state,
                "machines": machines,
                "syncResult": sync_res,
            }

        res = await run_in_threadpool(_do_trigger)
        return JSONResponse(res, headers={"Cache-Control": "no-store"})

    @router.post("/api/sync/push")
    async def sync_push(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        principal = getattr(request.state, "principal", None)
        if principal is not None and principal.kind == "machine":
            machine_id = principal.id
        else:
            machine_id = await run_in_threadpool(authenticate_machine_token, settings.database_path, token)
        if not machine_id:
                return error_response("DEV-301", status_code=401)
        if not _PUSH_WINDOW.allow((settings.database_path, machine_id), record=True):
            return JSONResponse(
                {"error": "Sync rate limit exceeded."}, status_code=429, headers={"Retry-After": "60"},
            )
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 8 * 1024 * 1024:
                    return JSONResponse({"error": "Sync payload too large."}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw, parse_constant=reject_json_constant)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            machine = body.get("machine", {})
            if not isinstance(machine, dict):
                raise ValueError("Expected machine metadata to be an object.")
            for identifier in (body.get("machineId"), machine.get("id")):
                if identifier is not None and not isinstance(identifier, str):
                    raise ValueError("Expected a string computer identifier.")
            claimed = str(body.get("machineId") or machine.get("id") or machine_id)
            if claimed != machine_id:
                return JSONResponse({"error": "Cannot push as another machine."}, status_code=403)

            def foreign_identifier(value, key=""):
                if isinstance(value, dict):
                    return any(foreign_identifier(item, field) for field, item in value.items())
                if isinstance(value, list):
                    return any(foreign_identifier(item, key) for item in value)
                return (key in {"id", "raw_id", "session_id", "sessionId", "machineId"}
                        and isinstance(value, str) and value.startswith("m:")
                        and row_machine_id(value) != machine_id)

            if foreign_identifier(body):
                return JSONResponse({"error": "Cannot write another machine's identifiers."}, status_code=403)
            result = await run_in_threadpool(
                lambda: apply_sync_push(
                    settings.database_path,
                    pricing,
                    machine_id=machine_id,
                    body=body,
                    expected_token_hash=_token_hash(token),
                )
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except PermissionError:
            return error_response("DEV-301", status_code=401)
        except (ValueError, KeyError, TypeError, OverflowError, RecursionError) as exc:
            if "already exists" in str(exc).lower():
                return error_response("DEV-302", status_code=422)
            return error_response("HUB-501", status_code=422)

    @router.get("/api/spend/stream")
    async def spend_stream(request: Request, once: int = Query(default=0)):
        async def event_generator():
            sub, last_rev = STREAM_BROKER.subscribe()
            _, flag = sub
            observer = None
            try:
                observer = await run_in_threadpool(
                    sqlite3.connect,
                    settings.database_path.resolve().as_uri() + "?mode=ro",
                    uri=True,
                    check_same_thread=False,
                )
                last_sig = await run_in_threadpool(_db_live_signature, observer)
                machines = await run_in_threadpool(_resolve_stream_machines, settings)
                last_machines_sig = json.dumps(
                    [(m.get("id"), m.get("online"), m.get("eventsPushed")) for m in machines]
                )
                ready = json.dumps(
                    {
                        "type": "ready",
                        "revision": last_rev,
                        "machines": machines,
                    }
                )
                yield f"event: ready\ndata: {ready}\n\n"
                if once:
                    return
                while not await request.is_disconnected():
                    try:
                        await asyncio.wait_for(flag.wait(), timeout=2.0)
                        flag.clear()
                    except TimeoutError:
                        pass
                    cur_rev = STREAM_BROKER.revision
                    # SQLite reads and a spoke's hub HTTP request can both
                    # block. Neither may run on the web server's event loop.
                    cur_sig = await run_in_threadpool(_db_live_signature, observer)
                    machines = await run_in_threadpool(_resolve_stream_machines, settings)
                    cur_machines_sig = json.dumps(
                        [(m.get("id"), m.get("online"), m.get("eventsPushed")) for m in machines]
                    )
                    if (
                        cur_rev != last_rev
                        or cur_sig != last_sig
                        or cur_machines_sig != last_machines_sig
                    ):
                        last_rev = cur_rev
                        last_sig = cur_sig
                        last_machines_sig = cur_machines_sig
                        expire_hub_proxy_cache()
                        payload = json.dumps(
                            {
                                "type": "update",
                                "revision": last_rev,
                                "machines": machines,
                                "at": utc_now(),
                            }
                        )
                        yield f"event: update\ndata: {payload}\n\n"
                    else:
                        yield ": keepalive\n\n"
            finally:
                STREAM_BROKER.unsubscribe(sub)
                if observer is not None:
                    # StreamingResponse cancels its task group on disconnect.
                    # Release the observer even inside that cancelled scope.
                    with CancelScope(shield=True):
                        await run_in_threadpool(observer.close)

        return StreamingResponse(
            event_generator(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    return router
