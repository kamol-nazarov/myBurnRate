"""Multi-machine sync hub, device registry, SSE live stream, and remote agent client."""

from __future__ import annotations

import asyncio
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

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, StreamingResponse

from spend_app.connection_api import payload as read_trusted_json, trusted
from spend_app.db import (
    EXACT_USAGE_SOURCES,
    UnpricedUsageEvent,
    UsageEvent,
    connect,
    initialize,
    upsert_agent_run,
    upsert_quota,
    upsert_session,
    upsert_unpriced_event,
    upsert_usage_event,
    utc_now,
)
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
    items = [
        (
            mid,
            str(rec.get("name") or ""),
            str(rec.get("os") or ""),
            bool(rec.get("revoked")),
            int(rec.get("events_pushed") or 0),
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
        mem_seen = _MACHINE_LAST_SEEN_MEM.get(machine_id) if now is None else None
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
            }
        )
    return items


def register_or_reconnect_machine(
    database_path: Path,
    *,
    name: str,
    os_name: str = "",
    now: datetime | None = None,
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    stamp = ((now or datetime.now(UTC)).astimezone(UTC)).isoformat().replace("+00:00", "Z")
    initialize(database_path)
    with connect(database_path) as connection:
        state = _read_state(connection)
        matched_id = None
        for existing_id, rec in state["machines"].items():
            if not isinstance(rec, dict) or rec.get("revoked"):
                continue
            if str(rec.get("name") or "").strip().lower() == clean_name.lower():
                matched_id = existing_id
                break
        if matched_id is None:
            base = _slugify(clean_name)
            matched_id = f"{base}-{secrets.token_hex(2)}"
            while matched_id in state["machines"] or matched_id in {"local", "all"}:
                matched_id = f"{base}-{secrets.token_hex(2)}"
        token = f"br_m_{matched_id}_{secrets.token_urlsafe(24)}"
        prev = state["machines"].get(matched_id) if isinstance(state["machines"].get(matched_id), dict) else {}
        state["machines"][matched_id] = {
            "id": matched_id,
            "name": clean_name,
            "os": (os_name or prev.get("os") or "").strip()[:32],
            "token_hash": _token_hash(token),
            "created_at": prev.get("created_at") or stamp,
            "last_seen_at": stamp,
            "events_pushed": int(prev.get("events_pushed") or 0),
        }
        _write_state(connection, state)
        machines = list_machines(connection, now=now)
    created = next(item for item in machines if item["id"] == matched_id)
    STREAM_BROKER.publish({"type": "machines", "machineId": matched_id, "at": stamp})
    return {"machine": created, "token": token, "machines": machines}


def register_machine(
    database_path: Path,
    *,
    name: str,
    os_name: str = "",
    now: datetime | None = None,
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    stamp = ((now or datetime.now(UTC)).astimezone(UTC)).isoformat().replace("+00:00", "Z")
    initialize(database_path)
    with connect(database_path) as connection:
        state = _read_state(connection)
        base = _slugify(clean_name)
        machine_id = f"{base}-{secrets.token_hex(2)}"
        while machine_id in state["machines"] or machine_id in {"local", "all"}:
            machine_id = f"{base}-{secrets.token_hex(2)}"
        token = f"br_m_{machine_id}_{secrets.token_urlsafe(24)}"
        state["machines"][machine_id] = {
            "id": machine_id,
            "name": clean_name,
            "os": (os_name or "").strip()[:32],
            "token_hash": _token_hash(token),
            "created_at": stamp,
            "last_seen_at": None,
            "events_pushed": 0,
        }
        _write_state(connection, state)
        machines = list_machines(connection, now=now)
    created = next(item for item in machines if item["id"] == machine_id)
    STREAM_BROKER.publish({"type": "machines", "machineId": machine_id, "at": stamp})
    return {"machine": created, "token": token, "machines": machines}


def revoke_machine(
    database_path: Path, *, machine_id: str, now: datetime | None = None
) -> dict:
    if not machine_id or machine_id in {"local", "all"}:
        raise ValueError("Cannot revoke the local hub machine.")
    initialize(database_path)
    with connect(database_path) as connection:
        state = _read_state(connection)
        if machine_id not in state["machines"]:
            raise ValueError(f"Unknown machine: {machine_id}")
        del state["machines"][machine_id]
        _write_state(connection, state)
        machines = list_machines(connection, now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": machine_id, "at": utc_now()})
    return {"machines": machines}


def rename_machine(
    database_path: Path, *, machine_id: str, name: str, now: datetime | None = None
) -> dict:
    clean_name = (name or "").strip()
    if not clean_name or len(clean_name) > 64:
        raise ValueError("Machine name must be between 1 and 64 characters.")
    initialize(database_path)
    with connect(database_path) as connection:
        state = _read_state(connection)
        if machine_id == "local":
            state["hub_name"] = clean_name
        elif machine_id in state["machines"]:
            state["machines"][machine_id]["name"] = clean_name
        else:
            raise ValueError(f"Unknown machine: {machine_id}")
        _write_state(connection, state)
        machines = list_machines(connection, now=now)
    STREAM_BROKER.publish({"type": "machines", "machineId": machine_id, "at": utc_now()})
    return {"machines": machines}


def authenticate_machine_token(database_path: Path, token: str) -> str | None:
    raw = (token or "").strip()
    if not raw.startswith("br_m_"):
        return None
    digest = _token_hash(raw)
    try:
        with connect(database_path) as connection:
            state = _read_state(connection)
    except Exception:
        return None
    for machine_id, rec in state["machines"].items():
        if not isinstance(rec, dict) or rec.get("revoked"):
            continue
        expected = str(rec.get("token_hash") or "")
        if expected and hmac.compare_digest(digest, expected):
            _MACHINE_LAST_SEEN_MEM[machine_id] = datetime.now(UTC).isoformat().replace("+00:00", "Z")
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


def _db_live_signature(database_path: Path) -> tuple:
    try:
        with connect(database_path) as connection:
            u = connection.execute("SELECT COUNT(*), MAX(id), MAX(ingested_at) FROM usage_events").fetchone()
            un = connection.execute("SELECT COUNT(*), MAX(id) FROM unpriced_usage_events").fetchone()
            ar = connection.execute("SELECT COUNT(*), MAX(last_seen_at) FROM agent_runs").fetchone()
            ir = connection.execute("SELECT MAX(id), MAX(finished_at) FROM ingest_runs").fetchone()
            mf = machines_fingerprint(connection)
            return (tuple(u), tuple(un), tuple(ar), tuple(ir), mf)
    except Exception:
        return ()


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

    written = 0
    with connect(database_path) as connection:
        try:
            for item in usage_items:
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
                    upsert_agent_run(
                        connection,
                        id=rid,
                        name=str(item["name"]),
                        model_key=item.get("model_key") or item.get("modelKey"),
                        state=str(item.get("state") or "LIVE"),
                        started_at=str(item.get("started_at") or item.get("startedAt") or stamp),
                        last_seen_at=str(item.get("last_seen_at") or item.get("lastSeenAt") or stamp),
                    )
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

            for item in quota_items:
                unit_val = str(item.get("unit") or "pct")
                if unit_val == "unavailable":
                    existing = connection.execute(
                        "SELECT unit FROM quotas WHERE provider_key=? AND limit_key=? ORDER BY polled_at DESC, id DESC LIMIT 1",
                        (str(item["provider_key"]), str(item["limit_key"])),
                    ).fetchone()
                    if existing and existing[0] and existing[0] != "unavailable":
                        continue
                orig_source = str(item.get("source") or "").strip()
                sync_source = (
                    orig_source
                    if orig_source.startswith("sync:")
                    else f"sync:{machine_id}:{orig_source}"
                    if orig_source
                    else f"sync:{machine_id}"
                )
                upsert_quota(
                    connection,
                    provider_key=str(item["provider_key"]),
                    limit_key=str(item["limit_key"]),
                    label=str(item["label"]),
                    unit=unit_val,
                    source=sync_source,
                    polled_at=str(item.get("polled_at") or stamp),
                    used=float(item["used"]) if item.get("used") is not None else None,
                    allowance=(
                        float(item["allowance"])
                        if item.get("allowance") is not None
                        else None
                    ),
                    pct=float(item["pct"]) if item.get("pct") is not None else None,
                    resets_at=item.get("resets_at"),
                    is_payg=item.get("is_payg"),
                )

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
            if written > 0:
                run = IngestRun.start(connection, f"sync:{machine_id}")
                run.events_written = written
                run.finish(status="success")
        except Exception:
            raise

    if written > 0:
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
    initialize(database_path)
    with connect(database_path) as connection:
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
            if str(r.get("unit") or "") != "unavailable"
        ]
        state = _read_state(connection)
        hub_name = state.get("hub_name") or _default_hub_name()

    next_cursor = {
        "usage_id": max([last_usage_id, *[int(r["id"]) for r in usage_rows]]),
        "unpriced_id": max([last_unpriced_id, *[int(r["id"]) for r in unpriced_rows]]),
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


def write_sync_cursor(database_path: Path, hub_url: str, cursor: dict) -> None:
    with connect(database_path) as connection:
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (_cursor_key(hub_url), json.dumps(cursor)),
        )


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
    total_written = 0
    last_result: dict = {"ok": True, "eventsWritten": 0}
    endpoint = hub_url.strip().rstrip("/") + "/api/sync/push"
    headers = {
        "Authorization": f"Bearer {token.strip()}",
        "Content-Type": "application/json",
    }
    batch_limit = 1000

    def _run_batches(post_fn) -> None:
        nonlocal cursor, total_written, last_result
        for batch_idx in range(15):
            batch, next_cursor = export_sync_batch(
                settings.database_path, cursor=cursor, limit=batch_limit
            )
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
            write_sync_cursor(settings.database_path, hub_url, next_cursor)
            cursor = next_cursor
            if (
                len(batch.get("usageEvents") or []) < batch_limit
                and len(batch.get("unpricedEvents") or []) < batch_limit
            ):
                break

    if client is not None:
        _run_batches(client.post)
    else:
        import httpx

        with httpx.Client(timeout=30.0) as http:
            _run_batches(http.post)
    return {**last_result, "eventsWritten": total_written, "cursor": cursor}


_HUB_PROXY_LOCK = threading.Lock()
_HUB_PROXY_CACHE: dict[tuple, tuple[float, dict]] = {}
_HUB_TEST_CLIENTS: dict[str, object] = {}


def register_hub_test_client(hub_url: str, client: object) -> None:
    """Allow in-process unit tests to route spoke read-through calls to a TestClient."""
    _HUB_TEST_CLIENTS[hub_url.strip().rstrip("/")] = client


def clear_hub_proxy_cache() -> None:
    with _HUB_PROXY_LOCK:
        _HUB_PROXY_CACHE.clear()


def post_to_configured_hub(
    settings,
    path: str,
    body: dict,
) -> dict | None:
    """Forward a write mutation from a paired Spoke to the central Hub."""
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
    token = str(spoke.get("token") or "").strip()
    if not hub_url or not token:
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

            with httpx.Client(timeout=5.0) as http:
                response = http.post(f"{hub_url}{path}", json=body, headers=headers)
        clear_hub_proxy_cache()
        if response.status_code >= 400:
            return None
        data = response.json()
        return data if isinstance(data, dict) else None
    except Exception:
        return None


def fetch_from_configured_hub(
    settings,
    path: str,
    params: dict | None = None,
    *,
    ttl_seconds: float = 2.0,
) -> dict | None:
    """When this instance is a paired Spoke, read unified dashboard data from the Hub."""
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
    token = str(spoke.get("token") or "").strip()
    if not hub_url or not token:
        return None

    clean_params = {k: str(v) for k, v in (params or {}).items() if v is not None}
    cache_key = (hub_url, path, tuple(sorted(clean_params.items())))
    now_mono = time.monotonic()
    with _HUB_PROXY_LOCK:
        cached = _HUB_PROXY_CACHE.get(cache_key)
        if cached and (now_mono - cached[0]) < ttl_seconds:
            return cached[1]

    headers = {"Authorization": f"Bearer {token}"}
    test_client = _HUB_TEST_CLIENTS.get(hub_url)
    try:
        if test_client is not None:
            response = test_client.get(path, params=clean_params, headers=headers)
        else:
            import httpx

            with httpx.Client(timeout=8.0) as http:
                response = http.get(f"{hub_url}{path}", params=clean_params, headers=headers)
        if response.status_code != 200:
            return cached[1] if cached else None
        data = response.json()
        if not isinstance(data, dict):
            return cached[1] if cached else None
        with _HUB_PROXY_LOCK:
            _HUB_PROXY_CACHE[cache_key] = (now_mono, data)
        return data
    except Exception:
        return cached[1] if cached else None


def sync_router(settings, pricing: PricingEngine) -> APIRouter:
    router = APIRouter()

    @router.get("/api/machines")
    def get_machines():
        hub_data = fetch_from_configured_hub(settings, "/api/machines")
        if hub_data is not None and isinstance(hub_data.get("machines"), list):
            return JSONResponse(hub_data, headers={"Cache-Control": "no-store"})
        with connect(settings.database_path) as connection:
            machines = list_machines(connection)
        return JSONResponse({"machines": machines}, headers={"Cache-Control": "no-store"})

    @router.post("/api/machines")
    async def mutate_machines(request: Request):
        try:
            body = await request.json()
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
            action = str(body.get("action") or "register").strip().lower()
            if action in {"revoke", "rename"}:
                hub_res = post_to_configured_hub(settings, "/api/machines", body)
                if isinstance(hub_res, dict) and isinstance(hub_res.get("machines"), list):
                    return JSONResponse(hub_res, headers={"Cache-Control": "no-store"})
            if action == "register":
                result = register_machine(
                    settings.database_path,
                    name=str(body.get("name") or ""),
                    os_name=str(body.get("os") or ""),
                )
                origin = f"{request.url.scheme}://{request.headers.get('host', '127.0.0.1:17331')}"
                result["syncCommand"] = (
                    f"python -m spend_app sync --hub {origin} --token {result['token']}"
                )
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "revoke":
                result = revoke_machine(
                    settings.database_path,
                    machine_id=str(body.get("id") or ""),
                )
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            if action == "rename":
                result = rename_machine(
                    settings.database_path,
                    machine_id=str(body.get("id") or ""),
                    name=str(body.get("name") or ""),
                )
                return JSONResponse(result, headers={"Cache-Control": "no-store"})
            return JSONResponse({"error": "Unsupported machine action."}, status_code=422)
        except ValueError as exc:
            return JSONResponse({"error": str(exc)}, status_code=422)

    @router.post("/api/sync/push")
    async def sync_push(request: Request):
        auth = request.headers.get("authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else ""
        machine_id = authenticate_machine_token(settings.database_path, token)
        if not machine_id:
            return JSONResponse({"error": "Invalid or revoked machine token."}, status_code=401)
        try:
            raw = await request.body()
            if len(raw) > 8 * 1024 * 1024:
                return JSONResponse({"error": "Sync payload too large."}, status_code=413)
            body = json.loads(raw)
            if not isinstance(body, dict):
                raise ValueError("Expected a JSON object.")
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
            try:
                last_sig = _db_live_signature(settings.database_path)
                with connect(settings.database_path) as connection:
                    machines = list_machines(connection)
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
                    cur_sig = _db_live_signature(settings.database_path)
                    if cur_rev != last_rev or cur_sig != last_sig:
                        last_rev = cur_rev
                        last_sig = cur_sig
                        with connect(settings.database_path) as connection:
                            machines = list_machines(connection)
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
