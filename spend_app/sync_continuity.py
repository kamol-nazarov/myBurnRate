"""Immutable usage journal checkpoints shared by the spoke and hub."""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass


_MARK = re.compile(r"[0-9a-f]{32}")
_STREAM = re.compile(r"(?:dev-)?[0-9a-f]{32}")
_HUB_HEAD_PREFIX = "sync.received.usage_head.v1:"
_MAX_RECEIVED_MARKS = 100_000
_MARK_RETENTION_SECONDS = 90 * 24 * 60 * 60
_RECEIVED_TABLES = ("sync_usage_receipts", "sync_usage_conflicts")


def journal_mark(connection, seq: int) -> str | None:
    row = connection.execute("SELECT mark FROM usage_change_marks WHERE seq=?", (seq,)).fetchone()
    return row[0] if row else None


def _valid_mark(value: object) -> bool:
    return isinstance(value, str) and _MARK.fullmatch(value) is not None


def _valid_stream(value: object) -> bool:
    return isinstance(value, str) and _STREAM.fullmatch(value) is not None


def _valid_head(value: object) -> bool:
    return (
        isinstance(value, dict)
        and _valid_stream(value.get("stream"))
        and type(value.get("through")) is int
        and value["through"] >= 0
        and (value.get("mark") is None or _valid_mark(value["mark"]))
    )


@dataclass(frozen=True)
class UsageWindow:
    """Proposed transaction boundary, or the current head for a retry."""

    accepted: bool
    head: dict
    marks: dict[int, str]
    revisions: dict[int, tuple[int, str]]
    legacy_through: int = 0
    legacy_verification_through: int = 0
    imported_legacy: frozenset[int] = frozenset()


def hub_usage_head(connection, machine_id: str) -> dict | None:
    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?", (_HUB_HEAD_PREFIX + machine_id,)
    ).fetchone()
    return json.loads(row[0]) if row else None


def choose_usage_window(connection, machine_id: str, legacy_ack: int, batch: dict, proof: object) -> UsageWindow:
    """Check lineage and range under the same writer lock as the event updates."""
    if not isinstance(proof, dict) or not _valid_stream(proof.get("stream")):
        raise ValueError("Invalid usage continuation stream.")
    after, through = batch["after"], batch["through"]
    marks, revisions = {}, {}
    values = proof.get("marks")
    if not isinstance(values, list):
        raise ValueError("Invalid usage continuation checkpoints.")
    previous = after
    imported_legacy = set()
    for value in values:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("Invalid usage continuation checkpoint.")
        seq, mark, legacy = value
        if (
            type(seq) is not int or not previous < seq <= through
            or not _valid_mark(mark) or type(legacy) is not bool
        ):
            raise ValueError("Invalid usage continuation checkpoint.")
        marks[seq] = mark
        if legacy:
            imported_legacy.add(seq)
        previous = seq
    values = proof.get("revisions")
    if not isinstance(values, list):
        raise ValueError("Invalid usage continuation revisions.")
    for value in values:
        if not isinstance(value, list) or len(value) != 3:
            raise ValueError("Invalid usage continuation revision.")
        seq, newest, mark = value
        if (
            type(seq) is not int or seq not in marks or seq in revisions
            or type(newest) is not int or newest <= through or not _valid_mark(mark)
        ):
            raise ValueError("Usage revision lacks a later journal checkpoint.")
        revisions[seq] = (newest, mark)
    after_mark = proof.get("afterMark")
    if (after_mark is not None and not _valid_mark(after_mark)) or (
        through != after and through not in marks
    ):
        raise ValueError("Usage continuation has an incomplete range.")

    stream = proof["stream"]
    existing = hub_usage_head(connection, machine_id)
    legacy_through = 0
    legacy_verification_through = 0
    if existing is None:
        legacy_through = legacy_ack
        if after != legacy_ack:
            legacy_head = {"stream": None, "through": legacy_ack, "mark": None}
            if proof.get("rebase") != legacy_head or after > legacy_ack:
                return UsageWindow(False, legacy_head, {}, {})
            # A v1 hub knew only a number. Verify each overlapping change
            # against storage before adopting its first provable checkpoint.
            legacy_verification_through = legacy_ack
    elif stream == existing["stream"]:
        if after != existing["through"] or after_mark != existing.get("mark"):
            return UsageWindow(False, existing, {}, {})
        legacy_through = existing.get("legacyThrough", 0)
        legacy_verification_through = existing.get("legacyVerificationThrough", 0)
    else:
        # A restored spoke starts a new branch using a compare-and-swap
        # against the hub head. A delayed batch from the previous branch
        # cannot apply after this transition.
        if proof.get("rebase") != existing or after > existing["through"]:
            return UsageWindow(False, existing, {}, {})
        legacy_through = existing.get("legacyThrough", 0)
        legacy_verification_through = existing.get("legacyVerificationThrough", 0)

    head = {"stream": stream, "through": through, "mark": marks.get(through, after_mark)}
    if legacy_through:
        head["legacyThrough"] = legacy_through
    if through < legacy_verification_through:
        head["legacyVerificationThrough"] = legacy_verification_through
    return UsageWindow(
        True, head, marks, revisions, legacy_through, legacy_verification_through, frozenset(imported_legacy),
    )


def received_revision(connection, machine_id: str, mark: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sync_usage_receipts WHERE machine_id=? AND mark=?", (machine_id, bytes.fromhex(mark))
    ).fetchone() is not None


def retain_legacy_conflict(connection, machine_id: str, mark: str, change: dict) -> None:
    connection.execute(
        "INSERT OR IGNORE INTO sync_usage_conflicts(machine_id,mark,raw_id,change,received_at) VALUES(?,?,?,?,?)",
        (machine_id, bytes.fromhex(mark), change["raw_id"], json.dumps(change), int(time.time())),
    )


def clear_legacy_conflicts(connection, machine_id: str, raw_id: str) -> None:
    connection.execute(
        "DELETE FROM sync_usage_conflicts WHERE machine_id=? AND raw_id=?", (machine_id, raw_id)
    )


def count_legacy_conflicts(connection, machine_id: str) -> int:
    return connection.execute(
        "SELECT COUNT(*) FROM sync_usage_conflicts WHERE machine_id=?", (machine_id,)
    ).fetchone()[0]


def commit_usage_window(connection, machine_id: str, window: UsageWindow) -> None:
    marks = set(window.marks.values()) | {mark for _, mark in window.revisions.values()}
    connection.executemany(
        "INSERT OR IGNORE INTO sync_usage_receipts(machine_id,mark,received_at) VALUES(?,?,?)",
        ((machine_id, bytes.fromhex(mark), int(time.time())) for mark in marks),
    )
    connection.execute(
        "INSERT INTO app_meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_HUB_HEAD_PREFIX + machine_id, json.dumps(window.head)),
    )
    prune_received_journal(connection, machine_id)


def prune_received_journal(connection, machine_id: str | None = None) -> None:
    """Bound retained deduplication and recovery evidence independently of clients."""
    now = int(time.time())
    for table in _RECEIVED_TABLES:
        if machine_id is None:
            columns = {row[1] for row in connection.execute(f"PRAGMA table_info({table})")}
            if "received_at" not in columns:
                continue
            connection.execute(
                f"DELETE FROM {table} WHERE received_at < ?",
                (now - _MARK_RETENTION_SECONDS,),
            )
            continue
        connection.execute(
            f"DELETE FROM {table} WHERE machine_id=? AND received_at < ?",
            (machine_id, now - _MARK_RETENTION_SECONDS),
        )
        connection.execute(
            f"DELETE FROM {table} WHERE machine_id=? AND mark IN ("
            f"SELECT mark FROM {table} WHERE machine_id=? "
            "ORDER BY received_at DESC, mark DESC LIMIT -1 OFFSET ?)",
            (machine_id, machine_id, _MAX_RECEIVED_MARKS),
        )


def verified_reply(connection, head: object, offered: dict, after: int) -> bool:
    if not _valid_head(head) or head["through"] < after:
        return False
    if head["through"] == after and head["stream"] == offered["stream"]:
        return head["mark"] == offered["afterMark"]
    return head["mark"] is not None and head["mark"] == journal_mark(connection, head["through"])


def can_rebase(head: object, after: int) -> bool:
    return _valid_head(head) and head["mark"] is not None and head["through"] >= after
