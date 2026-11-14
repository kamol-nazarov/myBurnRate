"""Multi-machine sync hub, device registry, SSE live stream, and remote agent client."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import hmac
import json
import os
import platform
import re
import secrets
import sqlite3
import sys
import threading
import time
from dataclasses import asdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from anyio import CancelScope
from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool

from spend_app.connection_api import payload as read_trusted_json, trusted
from spend_app.db import (
    EXACT_USAGE_SOURCES,
    RETIRED_SOURCES,
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    prune_ingest_runs,
    upsert_agent_run,
    upsert_quota,
    upsert_session,
    upsert_unpriced_event,
    upsert_usage_event,
    utc_now,
)
from spend_app.capacity_forecast import apply_balance_runway, record_balance_sample
from spend_app.ingest import IngestRun
from spend_app.pricing import PricingEngine, UnpricedModelError

MACHINES_META_KEY = "machines.v1"
SYNC_CURSOR_PREFIX = "sync.cursor.v1:"
MACHINE_ONLINE_SECONDS = 90
_MACHINE_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,47}$")
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
            }
        )
    return items


def _register_in_connection(connection, *, name, os_name="", now=None, reconnect=False, machine_id=None):
    from spend_app.auth_model import MACHINE_DAYS, moment, stamp
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    state = _read_state(connection)
    matched_id = machine_id
    if matched_id is not None and matched_id not in state["machines"]:
        raise ValueError("Unknown machine.")
    if matched_id is None and reconnect:
        matched_id = next((mid for mid, rec in state["machines"].items()
                           if isinstance(rec, dict) and str(rec.get("name", "")).lower() == clean_name.lower()), None)
    if matched_id is None:
        matched_id = f"machine-{secrets.token_hex(8)}"
        while matched_id in state["machines"] or matched_id in {"local", "all"}:
            matched_id = f"machine-{secrets.token_hex(8)}"
    token = f"br_m_{matched_id}_{secrets.token_urlsafe(32)}"
    prev = state["machines"].get(matched_id) or {}
    state["machines"][matched_id] = {
        "id": matched_id, "name": clean_name, "os": (os_name or prev.get("os") or "").strip()[:32],
        "token_hash": _token_hash(token), "created_at": stamp(now),
        "registered_at": prev.get("registered_at") or prev.get("created_at") or stamp(now),
        "expires_at": stamp(moment(now) + timedelta(days=MACHINE_DAYS)), "last_used_at": None,
        "rotation_required": False, "last_seen_at": stamp(now),
        "events_pushed": int(prev.get("events_pushed") or 0),
    }
    _write_state(connection, state)
    machines = list_machines(connection, now=now)
    created = next(item for item in machines if item["id"] == matched_id)
    return {"machine": created, "token": token, "machines": machines,
            "expiresAt": state["machines"][matched_id]["expires_at"]}


def register_or_reconnect_machine(database_path: Path, *, name: str, os_name: str = "", now=None, actor=None) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _register_in_connection(connection, name=name, os_name=os_name, now=now, reconnect=True)
        if actor:
            from spend_app.auth_model import audit
            audit(connection, actor, "machine.register", result["machine"]["id"], now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": result["machine"]["id"], "at": utc_now()})
    return result


def register_machine(database_path: Path, *, name: str, os_name: str = "", now=None, actor=None) -> dict:
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        result = _register_in_connection(connection, name=name, os_name=os_name, now=now)
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
            if (rec.get("revoked") or not rec.get("token_hash") or not parse(rec.get("expires_at"))
                    or parse(rec["expires_at"]) <= moment(now)
                    or not hmac.compare_digest(_token_hash(token), rec["token_hash"])):
                continue
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
        state["machines"][machine_id].update(revoked=True, revoked_at=utc_now(), token_hash="")
        connection.execute("UPDATE pairing_grants SET used_at=? WHERE machine_id=? AND used_at IS NULL",
                           (utc_now(), machine_id))
        _write_state(connection, state)
        if actor:
            from spend_app.auth_model import audit
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
        if machine_id == "local":
            state["hub_name"] = clean_name
        elif machine_id in state["machines"]:
            state["machines"][machine_id]["name"] = clean_name
        else:
            raise ValueError(f"Unknown machine: {machine_id}")
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
    if not raw.startswith("br_m_"):
        return None
    digest = _token_hash(raw)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        state = _read_state(connection)
        for machine_id, rec in state["machines"].items():
            if not isinstance(rec, dict) or rec.get("revoked"):
                continue
            expected = str(rec.get("token_hash") or "")
            expires = parse(rec.get("expires_at"))
            if expected and hmac.compare_digest(digest, expected) and expires and expires > moment(now):
                rec["last_used_at"] = stamp(now)
                _write_state(connection, state)
                _MACHINE_LAST_SEEN_MEM[machine_id] = stamp(now)
                return machine_id
    return None


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
    if computed is not None:
        return float(computed), is_exact
    if item.get("cost_usd") is not None:
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
    )
    return float(cost), is_exact


def _retired_item(item) -> bool:
    return isinstance(item, dict) and (item.get("tool_key") == "cursor" or item.get("source") in RETIRED_SOURCES)


def _usage_change_batch(connection, machine_id: str, batch: object):
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
    if after > acknowledged:
        raise ValueError("Usage changes have a sequence gap; reset this hub's sync cursor.")
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
        if seq <= acknowledged:
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
    return usage, unpriced, deleted, key, max(acknowledged, through), skipped


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
) -> dict:
    if not _MACHINE_ID_RE.match(machine_id) or machine_id in {"local", "all"}:
        raise ValueError("Invalid machine identifier.")
    moment = (now or datetime.now(UTC)).astimezone(UTC)
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
    skipped_changes = 0
    with connect(database_path) as connection:
        # Serialise acknowledgement with event writes. Retried or concurrent
        # pushes must never replay an older correction over a newer one.
        connection.execute("BEGIN IMMEDIATE")
        try:
            if "usageChanges" in body:
                usage_items, unpriced_items, deleted, ack_key, change_ack, skipped_changes = _usage_change_batch(
                    connection, machine_id, body["usageChanges"])
                usage_changed = bool(usage_items or unpriced_items or deleted)
                for identifier in deleted:
                    raw_id = prefix_machine_id(machine_id, identifier)
                    for table in ("usage_events", "unpriced_usage_events", "coverage_gap_events", "pricing_gap_events"):
                        connection.execute(f"DELETE FROM {table} WHERE raw_id=?", (raw_id,))
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
                        telemetry_complete=bool(item.get("telemetry_complete", True)),
                        cost_usd=(
                            float(item["cost_usd"])
                            if item.get("cost_usd") is not None
                            else None
                        ),
                        raw_id=raw_id,
                        ingested_at=str(item.get("ingested_at") or stamp),
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
                    telemetry_complete=bool(item.get("telemetry_complete", True)),
                    cost_usd=(
                        float(item["cost_usd"])
                        if item.get("cost_usd") is not None
                        else None
                    ),
                    raw_id=raw_id,
                    ingested_at=str(item.get("ingested_at") or stamp),
                )
                if upsert_unpriced_event(connection, unpriced):
                    written += 1

            # A computer on an older version pushes each Grok model call and
            # its turn total; keep the turn and drop the calls it counts.
            grok_sessions = {
                prefix_machine_id(machine_id, str(item["session_id"]))
                for item in (*usage_items, *unpriced_items)
                if item.get("tool_key") == "grok" and item.get("session_id")
            }
            if grok_sessions:
                from spend_app.adapters.grok_native import drop_covered_calls

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
                        name=str(item["name"]),
                        model_key=item.get("model_key") or item.get("modelKey"),
                        state=str(item.get("state") or "LIVE"),
                        started_at=str(item.get("started_at") or item.get("startedAt") or stamp),
                        last_seen_at=str(item.get("last_seen_at") or item.get("lastSeenAt") or stamp),
                    ):
                        agents_changed = True
                if body.get("replaceAgentRuns", True):
                    prefix = f"m:{machine_id}:%"
                    existing_ids = [
                        row[0]
                        for row in connection.execute(
                            "SELECT id FROM agent_runs WHERE id LIKE ?", (prefix,)
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
                existing = connection.execute(
                    "SELECT id, label, used, allowance, unit, pct, resets_at, source, is_payg "
                    "FROM quotas WHERE provider_key=? AND limit_key=? ORDER BY polled_at DESC, id DESC LIMIT 1",
                    (provider_key, limit_key),
                ).fetchone()
                if unit_val == "unavailable":
                    if existing and existing["unit"] and existing["unit"] != "unavailable":
                        continue
                orig_source = str(item.get("source") or "").strip()
                sync_source = (
                    orig_source
                    if orig_source.startswith("sync:")
                    else f"sync:{machine_id}:{orig_source}"
                    if orig_source
                    else f"sync:{machine_id}"
                )
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
                polled_val = str(item.get("polled_at") or stamp)
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
                if existing is not None and tuple(existing)[1:] == fields:
                    cursor_upd = connection.execute(
                        "UPDATE quotas SET polled_at=? WHERE id=? AND polled_at<?",
                        (polled_val, existing["id"], polled_val),
                    )
                    if cursor_upd.rowcount:
                        quotas_changed = True
                    continue
                if upsert_quota(
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
                ):
                    quotas_changed = True

            state = _read_state(connection)
            rec = state["machines"].setdefault(
                machine_id,
                {
                    "id": machine_id,
                    "name": str(machine_meta.get("name") or machine_id)[:64],
                    "os": str(machine_meta.get("os") or "")[:32],
                    "token_hash": "",
                    "created_at": stamp,
                    "events_pushed": 0,
                },
            )
            if machine_meta.get("name"):
                rec["name"] = str(machine_meta["name"]).strip()[:64] or rec["name"]
            if machine_meta.get("os"):
                rec["os"] = str(machine_meta["os"]).strip()[:32]
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
        except Exception:
            raise

    if written > 0 or agents_changed or quotas_changed or usage_changed:
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
        **({"usageChangeSeq": change_ack, "usageChangesSkipped": skipped_changes} if change_ack is not None else {}),
    }


def export_sync_batch(
    database_path: Path,
    *,
    cursor: dict | None = None,
    limit: int = 500,
) -> tuple[dict, dict]:
    """Read local-only incremental rows from ``database_path`` for pushing to a hub."""
    cursor = cursor or {}
    last_usage_id = int(cursor.get("usage_id") or 0)
    last_unpriced_id = int(cursor.get("unpriced_id") or 0)
    last_change_seq = int(cursor.get("change_seq") or 0)
    if limit < 1:
        raise ValueError("Sync batch limit must be positive.")
    initialize(database_path)
    with connect(database_path) as connection:
        # Journal identities and current payloads must share a read snapshot.
        connection.execute("BEGIN")
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
            "SELECT seq,raw_id FROM usage_changes WHERE seq>? ORDER BY seq LIMIT ?",
            (last_change_seq, int(limit)),
        ).fetchall()
        next_change_seq = journal[-1]["seq"] if journal else last_change_seq
        # Multiple mutations of one identity can be represented by its latest
        # state. Keep the last sequence so acknowledgement remains monotonic.
        identities = {row["raw_id"]: row["seq"] for row in journal}
        changes = []
        for raw_id, seq in sorted(identities.items(), key=lambda item: item[1]):
            change = {"seq": seq, "raw_id": raw_id, "kind": "delete"}
            for table, kind, rows in (("usage_events", "usage", usage_rows),
                                      ("unpriced_usage_events", "unpriced", unpriced_rows)):
                row = connection.execute(f"SELECT * FROM {table} WHERE raw_id=?", (raw_id,)).fetchone()
                if row is not None:
                    event = dict(row)
                    if _parse_iso(str(event.get("occurred_at") or "")) is None:
                        change.update(kind="skip", reason="invalid_timestamp")
                        break
                    rows.append(event.copy())  # Upserts also work with legacy hubs.
                    event.pop("id", None)
                    event.pop("occurred_epoch_us", None)
                    change.update(kind=kind, event=event)
                    break
            changes.append(change)
        usage_rows = list({row["raw_id"]: row for row in usage_rows
                           if _parse_iso(str(row.get("occurred_at") or "")) is not None}.values())
        unpriced_rows = list({row["raw_id"]: row for row in unpriced_rows
                              if _parse_iso(str(row.get("occurred_at") or "")) is not None}.values())
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
                    SELECT provider_key, limit_key, MAX(id) AS max_id
                    FROM quotas
                    WHERE source NOT LIKE 'sync:%'
                      AND COALESCE(unit, '') != 'unavailable'
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
        quota_rows = [
            {
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
            }
            for r in verified_quotas
            if str(r.get("unit") or "") != "unavailable" and r.get("provider_key") != "cursor"
        ]
        state = _read_state(connection)
        hub_name = state.get("hub_name") or _default_hub_name()

    next_cursor = {
        "usage_id": next_usage_id,
        "unpriced_id": next_unpriced_id,
        "change_seq": next_change_seq,
    }
    for row in usage_rows:
        row.pop("id", None)
        row.pop("occurred_epoch_us", None)
    for row in unpriced_rows:
        row.pop("id", None)
        row.pop("occurred_epoch_us", None)

    payload = {
        "machine": {"name": hub_name, "os": sys.platform},
        "usageEvents": usage_rows,
        "unpricedEvents": unpriced_rows,
        "sessions": sessions,
        "agentRuns": agent_runs,
        "quotas": quota_rows,
        "replaceAgentRuns": True,
        "usageChanges": {"version": 1, "after": last_change_seq, "through": next_change_seq,
                         "changes": changes, "hasMore": len(journal) == limit},
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


def write_sync_cursor(database_path: Path, hub_url: str, cursor: dict, *, expected_generation=_ANY_GENERATION) -> None:
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        if expected_generation is not _ANY_GENERATION:
            row = connection.execute("SELECT value FROM app_meta WHERE key=?", (_cursor_key(hub_url),)).fetchone()
            current = json.loads(row[0]) if row else {}
            if current.get("generation") != expected_generation:
                raise RuntimeError("Pairing changed; retry sync with the current configuration.")
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_cursor_key(hub_url), json.dumps(cursor)),
        )


def prepare_pairing_sync(database_path: Path, hub_url: str, token: str) -> str:
    """Seed a fresh receiver from current rows without weakening journal order.

    Compacting acknowledged changes must not discard the initial snapshot.
    A generation and credential hash fence both in-flight and queued old pushes.
    """
    initialize(database_path)
    with connect(database_path) as connection:
        connection.execute("BEGIN IMMEDIATE")
        for table in ("usage_events", "unpriced_usage_events"):
            connection.execute(f"INSERT INTO usage_changes(raw_id) SELECT raw_id FROM {table} "
                               "WHERE raw_id NOT LIKE 'm:%' ORDER BY id")
        cursor = {"generation": secrets.token_hex(16), "credential_hash": _token_hash(token)}
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?) "
                           "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                           (_cursor_key(hub_url), json.dumps(cursor)))
    return cursor["generation"]


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

    cursor = read_sync_cursor(settings.database_path, hub_url)
    generation = cursor.get("generation")
    if cursor.get("credential_hash") and not hmac.compare_digest(cursor["credential_hash"], _token_hash(token.strip())):
        raise RuntimeError("Pairing changed; retry sync with the current configuration.")
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
        for batch_idx in range(15):
            batch, next_cursor = export_sync_batch(
                settings.database_path, cursor=cursor, limit=batch_limit
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
                raise RuntimeError(f"Sync push failed ({response.status_code}): {response.text}")
            last_result = response.json()
            total_written += int(last_result.get("eventsWritten") or 0)
            total_skipped += int(last_result.get("usageChangesSkipped") or 0)
            acknowledged = last_result.get("usageChangeSeq")
            if type(acknowledged) is int and acknowledged >= next_cursor["change_seq"]:
                # Only an explicit protocol acknowledgement consumes repair
                # records. A successful legacy HTTP response cannot acknowledge
                # deletions it did not understand.
                next_cursor["change_seq"] = acknowledged
            else:
                next_cursor["change_seq"] = int(cursor.get("change_seq") or 0)
                if batch["usageChanges"]["changes"]:
                    last_result = {**last_result, "usageChangesPending": True,
                                   "warning": "Upgrade the hub to synchronize usage corrections and deletions."}
            write_sync_cursor(settings.database_path, hub_url, next_cursor, expected_generation=generation)
            if type(acknowledged) is int and acknowledged > 0:
                try:
                    from spend_app.db import prune_usage_changes

                    with connect(settings.database_path) as connection:
                        prune_usage_changes(connection, acknowledged_seq=acknowledged)
                except Exception:
                    pass
            cursor = next_cursor
            if last_result.get("usageChangesPending"):
                break
            if (
                len(batch.get("usageEvents") or []) < batch_limit
                and len(batch.get("unpricedEvents") or []) < batch_limit
                and not batch["usageChanges"]["hasMore"]
            ):
                break

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


def clear_hub_proxy_cache() -> None:
    """Forget every Hub copy, e.g. after pairing changes."""
    with _HUB_PROXY_LOCK:
        _HUB_PROXY_CACHE.clear()


def expire_hub_proxy_cache() -> None:
    """Read the Hub again on next use, keeping the last copy as a fallback."""
    with _HUB_PROXY_LOCK:
        for key, (fetched_at, data, _fresh) in list(_HUB_PROXY_CACHE.items()):
            _HUB_PROXY_CACHE[key] = (fetched_at, data, False)


class HubMutationError(RuntimeError):
    """A paired computer must not turn a rejected Hub write into a local save."""

    def __init__(self, message: str, status_code: int = 503):
        super().__init__(message)
        self.status_code = status_code


def post_to_configured_hub(
    settings,
    path: str,
    body: dict,
    *,
    strict: bool = False,
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
    if state.get("role") != "spoke":
        return None
    spoke = state.get("spokeConfig") or {}
    if not spoke.get("enabled"):
        return None
    hub_url = str(spoke.get("hubUrl") or "").strip().rstrip("/")
    token = str(spoke.get("ownerSession") or "").strip()
    if path == "/api/machines" and not token:
        token = str(spoke.get("token") or "").strip()
    if not hub_url or not token:
        if strict:
            raise HubMutationError("Reconnect this computer to its hub before saving plans.")
        return None
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    try:
        if test_client is not None:
            response = test_client.post(path, json=body, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=5.0, trust_env=False) as http:
                response = http.post(f"{hub_url}{path}", json=body, headers=headers)
        if not 200 <= response.status_code < 300:
            if strict:
                code = response.status_code
                message = "The hub could not save this change. Retry with the same request identifier."
                if code in {400, 401, 403, 409, 413, 415, 422, 429}:
                    try:
                        error = response.json().get("error")
                        if isinstance(error, str) and error:
                            message = error
                    except (ValueError, AttributeError):
                        pass
                else:
                    code = 503
                raise HubMutationError(message, code)
            return None
        data = response.json()
        if not isinstance(data, dict):
            if strict:
                raise HubMutationError("The hub returned an invalid save response. Retry with the same request identifier.")
            return None
        clear_hub_proxy_cache()
        return data
    except HubMutationError:
        raise
    except Exception as exc:
        if strict:
            raise HubMutationError("Could not reach the hub. Retry with the same request identifier.") from exc
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

    A copy younger than ``ttl_seconds`` is served as is. With ``stale_seconds``
    any copy, of any age, is served at once while one background read renews
    it, and a cold cache returns immediately so the caller can paint local
    data. A slow or unreachable Hub therefore never holds up the HUD. A failed
    blocking read (``stale_seconds`` of 0) falls back to the last copy of any age.
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
    hub_url = str(spoke.get("hubUrl") or "").strip().rstrip("/")
    from spend_app.auth_model import digest, moment, parse
    token = str(spoke.get("ownerSession") or "").strip()
    expiry = parse(spoke.get("ownerSessionExpiresAt"))
    if expiry is not None and expiry <= moment():
        return None
    if digest(token) in _HUB_REJECTED_CREDENTIALS:
        return None
    if not hub_url or not token:
        return None

    clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
    cache_key = (hub_url, path, tuple(sorted(clean_params.items())), digest(token))

    def read() -> dict | None:
        return _read_hub_copy(cache_key, hub_url, token, path, clean_params, timeout=read_timeout)

    with _HUB_PROXY_LOCK:
        cached = _HUB_PROXY_CACHE.get(cache_key)
    if cached:
        fetched_at, payload, fresh = cached
        age = time.monotonic() - fetched_at
        if fresh and age < ttl_seconds:
            return _hub_copy_for_display(settings, payload)
        if stale_seconds > 0:
            _renew_hub_copy_in_background(cache_key, read)
            return _hub_copy_for_display(settings, payload)
    elif stale_seconds > 0:
        _renew_hub_copy_in_background(cache_key, read)
        return None
    data = read()
    if data is None:
        if digest(token) in _HUB_REJECTED_CREDENTIALS:
            return None
        return _hub_copy_for_display(settings, cached[1]) if cached else None
    return _hub_copy_for_display(settings, data)


_HUB_REJECTED_CREDENTIALS: set[str] = set()


def _read_hub_copy(cache_key: tuple, hub_url: str, token: str, path: str, params: dict,
                   timeout: float = 8.0) -> dict | None:
    headers = {"Authorization": f"Bearer {token}"}
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    try:
        if test_client is not None:
            response = test_client.get(path, params=params, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=timeout, trust_env=False) as http:
                response = http.get(f"{hub_url}{path}", params=params, headers=headers)
        if response.status_code in {401, 403}:
            from spend_app.auth_model import digest
            key = digest(token)
            with _HUB_PROXY_LOCK:
                _HUB_REJECTED_CREDENTIALS.add(key)
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
        try:
            from spend_app.setup_wizard import _bounded_json
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
                    return JSONResponse(
                        {"error": "A machine token can only rename or revoke its own machine."},
                        status_code=403,
                    )
            if action in {"revoke", "rename"}:
                hub_res = post_to_configured_hub(settings, "/api/machines", body)
                if isinstance(hub_res, dict) and isinstance(hub_res.get("machines"), list):
                    return JSONResponse(hub_res, headers={"Cache-Control": "no-store"})
            if action == "register":
                result = register_machine(
                    settings.database_path,
                    name=str(body.get("name") or ""),
                    os_name=str(body.get("os") or ""),
                    actor=request.state.principal,
                )
                request.state.audit_target = result["machine"]["id"]
                origin = f"{request.url.scheme}://{request.headers.get('host', '127.0.0.1:17331')}"
                result["syncCommand"] = (
                    f"python -m spend_app sync --hub {origin} --token {result['token']}"
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
            return JSONResponse({"error": "Unsupported machine action."}, status_code=422)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

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
                setup_state = public_setup_state(read_setup_state(connection))
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
        machine_id = authenticate_machine_token(settings.database_path, token)
        if not machine_id:
            return JSONResponse({"error": "Invalid or revoked machine token."}, status_code=401)
        try:
            raw = bytearray()
            async for chunk in request.stream():
                if len(raw) + len(chunk) > 8 * 1024 * 1024:
                    return JSONResponse({"error": "Sync payload too large."}, status_code=413)
                raw.extend(chunk)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            claimed = str(body.get("machineId") or (body.get("machine") or {}).get("id") or machine_id)
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
            from starlette.concurrency import run_in_threadpool

            result = await run_in_threadpool(
                lambda: apply_sync_push(
                    settings.database_path,
                    pricing,
                    machine_id=machine_id,
                    body=body,
                )
            )
            return JSONResponse(result, headers={"Cache-Control": "no-store"})
        except (ValueError, KeyError, TypeError) as exc:
            return JSONResponse({"error": f"Invalid sync payload: {exc}"}, status_code=422)

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
