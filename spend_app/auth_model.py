"""Principals, expiring human sessions and admin audit.

Only credential hashes live on the hub. Transactions serialize credential
replacement; no process-local authentication cache exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import secrets
import sqlite3
from dataclasses import dataclass
from contextlib import closing
from datetime import UTC, datetime, timedelta

from spend_app.db import connect

MACHINE_DAYS = 90
LEGACY_MACHINE_DAYS = 180
ROTATION_GRACE_DAYS = 14
SESSION_DAYS = 30
SESSION_ABSOLUTE_DAYS = 90
COOKIE = "__Host-burnrate_access"
LEGACY_COOKIE = "burnrate_access"
MARKER = "auth_model.v1"


def moment(now=None):
    return (now or datetime.now(UTC)).astimezone(UTC)


def stamp(now=None):
    return moment(now).isoformat().replace("+00:00", "Z")


def parse(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def digest(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class Principal:
    kind: str = "unauthenticated"
    id: str | None = None
    role: str | None = None
    session_id: str | None = None
    via: str = "none"

    def permits(self, permission: str) -> bool:
        # Add a member permission set here when the product supports members.
        return self.kind == "owner" and self.role == "owner" and permission in {
            "hub.read", "admin.write", "credentials.manage",
        }


def audit(connection, actor, action, target, outcome="success", *, now=None, source=None):
    """Callers pass only fixed action/target names or server-issued opaque IDs."""
    connection.execute(
        "INSERT INTO admin_audit(actor_type,actor_id,action,target,outcome,timestamp,source) "
        "VALUES(?,?,?,?,?,?,?)",
        (actor.kind, actor.id, action, target, outcome, stamp(now), source),
    )


def _stamp_missing_rotation_deadlines(data, *, deadline=None):
    target = deadline or stamp(moment() + timedelta(days=ROTATION_GRACE_DAYS))
    changed = False
    for rec in data.get("machines", {}).values():
        if isinstance(rec, dict) and rec.get("rotation_required") and not rec.get("rotation_deadline"):
            rec["rotation_deadline"] = target
            changed = True
    return changed


def _ensure_session_binding_schema(connection):
    columns = {
        "admin_audit": {"source": "TEXT"},
        "owner_sessions": {
            "machine_id": "TEXT",
            "auth_time": "TEXT",
            "password_authenticated": "INTEGER NOT NULL DEFAULT 0",
        },
    }
    if all(names.keys() <= {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
           for table, names in columns.items()):
        return
    connection.execute("BEGIN IMMEDIATE")
    for table, definitions in columns.items():
        existing = {row["name"] for row in connection.execute(f"PRAGMA table_info({table})")}
        for name, definition in definitions.items():
            if name not in existing:
                connection.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")
    connection.execute("UPDATE owner_sessions SET auth_time=created_at WHERE auth_time IS NULL")
    connection.execute("CREATE INDEX IF NOT EXISTS owner_sessions_machine ON owner_sessions(machine_id)")
    row = connection.execute("SELECT value FROM app_meta WHERE key='machines.v1'").fetchone()
    if row:
        by_label = {}
        for machine_id, machine in json.loads(row[0]).get("machines", {}).items():
            if isinstance(machine, dict):
                for label in (machine.get("name"), machine.get("reported_name")):
                    if isinstance(label, str) and label:
                        by_label.setdefault(label, set()).add(machine_id)
        for label, candidates in by_label.items():
            if len(candidates) == 1:
                connection.execute(
                    "UPDATE owner_sessions SET machine_id=? WHERE device_label=? AND machine_id IS NULL",
                    (next(iter(candidates)), label),
                )
        # A pre-upgrade session tied to an already revoked machine must not survive.
        for machine_id, machine in json.loads(row[0]).get("machines", {}).items():
            if isinstance(machine, dict) and machine.get("revoked"):
                connection.execute(
                    "UPDATE owner_sessions SET revoked_at=? WHERE machine_id=? AND revoked_at IS NULL",
                    (machine.get("revoked_at") or stamp(), machine_id),
                )
    connection.commit()


def ensure_schema(path):
    with connect(path) as connection:
        # admin_audit and append-only triggers must be ensured unconditionally so
        # existing databases recover missing audit guards.
        for sql in (
            "CREATE TABLE IF NOT EXISTS admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL, target TEXT NOT NULL, "
            "outcome TEXT NOT NULL, timestamp TEXT NOT NULL)",
            "CREATE TRIGGER IF NOT EXISTS admin_audit_no_update BEFORE UPDATE ON admin_audit "
            "BEGIN SELECT RAISE(ABORT, 'admin audit is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS admin_audit_no_delete BEFORE DELETE ON admin_audit "
            "BEGIN SELECT RAISE(ABORT, 'admin audit is append-only'); END",
            "CREATE TABLE IF NOT EXISTS owner_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
            "role TEXT NOT NULL, token_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
            "last_used_at TEXT, expires_at TEXT NOT NULL, revoked_at TEXT, device_label TEXT NOT NULL)",
        ):
            connection.execute(sql)
        _ensure_session_binding_schema(connection)
        if connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='pairing_grants'"
        ).fetchone():
            connection.commit()
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DROP TABLE IF EXISTS pairing_grants")
            connection.commit()
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (MARKER,)).fetchone():
            row = connection.execute("SELECT value FROM app_meta WHERE key='machines.v1'").fetchone()
            if row:
                data = json.loads(row[0])
                if isinstance(data, dict) and any(
                    isinstance(rec, dict) and rec.get("rotation_required") and not rec.get("rotation_deadline")
                    for rec in data.get("machines", {}).values()
                ):
                    connection.execute("BEGIN IMMEDIATE")
                    row = connection.execute("SELECT value FROM app_meta WHERE key='machines.v1'").fetchone()
                    if row:
                        data = json.loads(row[0])
                        if isinstance(data, dict) and _stamp_missing_rotation_deadlines(data):
                            connection.execute("UPDATE app_meta SET value=? WHERE key='machines.v1'", (json.dumps(data),))
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (MARKER,)).fetchone():
            return
        changed = False
        for key in ("machines.v1", "accounts.v1"):
            row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            data = json.loads(row[0])  # Corrupt auth state fails closed, never reset it.
            records = data["machines" if key == "machines.v1" else "users"]
            deadline = stamp(moment() + timedelta(days=ROTATION_GRACE_DAYS)) if key == "machines.v1" else None
            for rec in records.values():
                if key == "machines.v1":
                    created = parse(rec.get("created_at"))
                    rec.setdefault("expires_at", stamp(created + timedelta(days=LEGACY_MACHINE_DAYS))
                                   if created else stamp(datetime(1970, 1, 1, tzinfo=UTC)))
                    rec.setdefault("last_used_at", None)
                    rec.setdefault("rotation_required", True)
                    if rec.get("rotation_required") and not rec.get("rotation_deadline"):
                        rec["rotation_deadline"] = deadline
                else:
                    rec.setdefault("id", secrets.token_hex(16))
                    rec.setdefault("role", "owner")
            connection.execute("UPDATE app_meta SET value=? WHERE key=?", (json.dumps(data), key))
            changed = True
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?,?)", (MARKER, stamp()))
        if changed:
            audit(connection, Principal("system", "migration"), "auth.migrate", MARKER)


def owner_id(connection):
    row = connection.execute("SELECT value FROM app_meta WHERE key='accounts.v1'").fetchone()
    if row:
        for rec in json.loads(row[0]).get("users", {}).values():
            if rec.get("role", "owner") == "owner":
                return rec.get("id") or "bootstrap"
    return "bootstrap"


def find_machine_by_label(connection, device_label):
    """Associate compatible session-only sign-ins with an unambiguous device."""
    row = connection.execute("SELECT value FROM app_meta WHERE key='machines.v1'").fetchone()
    if not row:
        return None
    matches = [
        machine_id for machine_id, machine in json.loads(row[0]).get("machines", {}).items()
        if isinstance(machine, dict) and device_label in (machine.get("name"), machine.get("reported_name"))
    ]
    return matches[0] if len(matches) == 1 else None


def create_session(connection, actor, device_label="Browser", *, subject_id=None, now=None,
                   auth_time=None, machine_id=None, password_authenticated=False, source=None):
    sid = secrets.token_hex(16)
    token = f"br_s_{sid}_{secrets.token_urlsafe(32)}"
    created = moment(now)
    authenticated = auth_time or created
    expiry = stamp(min(created + timedelta(days=SESSION_DAYS),
                       authenticated + timedelta(days=SESSION_ABSOLUTE_DAYS)))
    connection.execute(
        "INSERT INTO owner_sessions(id,owner_id,role,token_hash,created_at,last_used_at,expires_at,"
        "revoked_at,device_label,machine_id,auth_time,password_authenticated) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (sid, subject_id or actor.id, actor.role, digest(token), stamp(created), None, expiry, None,
         str(device_label)[:80], machine_id, stamp(authenticated), password_authenticated),
    )
    audit(connection, actor, "session.create", sid, now=now, source=source)
    return {"sessionToken": token, "sessionId": sid, "expiresAt": expiry}


def session_is_live(row, now=None):
    if row is None or row["revoked_at"] or row["role"] != "owner":
        return False
    expires = parse(row["expires_at"])
    authenticated = parse(row["auth_time"])
    return bool(expires and authenticated and authenticated <= moment(now)
                < min(expires, authenticated + timedelta(days=SESSION_ABSOLUTE_DAYS)))


def authenticate_session(path, token, *, via="bearer", now=None):
    if not token.startswith("br_s_"):
        return None
    sid = token[5:].split("_", 1)[0]
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM owner_sessions WHERE id=?", (sid,)).fetchone()
        if not session_is_live(row, now) or not hmac.compare_digest(digest(token), row["token_hash"]):
            return None
        # Read traffic should not contend on the SQLite writer for every GET.
        last = parse(row["last_used_at"])
        if last is None or (moment(now) - last) >= timedelta(seconds=60):
            connection.execute("UPDATE owner_sessions SET last_used_at=? WHERE id=?", (stamp(now), sid))
        return Principal("owner", row["owner_id"], row["role"], sid, via)


def registered_machine_session(connection, actor, *, machine_id, revoked_at=None, now=None):
    session_id = getattr(actor, "session_id", None)
    row = connection.execute("SELECT * FROM owner_sessions WHERE id=?", (session_id,)).fetchone() if session_id else None
    if session_id and not session_is_live(row, now):
        raise ValueError("This machine registration requires a current owner session.")
    if revoked_at is not None:
        revoked = parse(revoked_at)
        if not (row and row["password_authenticated"] and revoked and parse(row["auth_time"]) > revoked):
            raise ValueError("This machine is revoked; sign in again with the account password.")
    if row:
        # Keep the first machine association for this session chain. An owner
        # managing another computer must not transfer their existing binding.
        connection.execute(
            "UPDATE owner_sessions SET machine_id=? WHERE id=? AND machine_id IS NULL", (machine_id, session_id)
        )


def revoke_machine_sessions(connection, actor, machine_id, *, now=None, source=None):
    for row in connection.execute(
        "SELECT id FROM owner_sessions WHERE machine_id=? AND revoked_at IS NULL", (machine_id,)
    ).fetchall():
        revoke_session(connection, actor, row["id"], now=now, source=source)


def revoke_session(connection, actor, sid, *, now=None, source=None):
    result = connection.execute("UPDATE owner_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (stamp(now), sid))
    if result.rowcount:
        audit(connection, actor, "session.revoke", sid, now=now, source=source)
    return bool(result.rowcount)


def break_glass_matches(path, candidate, configured):
    if not candidate:
        return False
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=5)) as connection:
            row = connection.execute("SELECT value FROM app_meta WHERE key='auth.break_glass.v1'").fetchone()
    except sqlite3.DatabaseError:
        # A missing or unreadable credential store cannot restore an old token.
        return False
    expected = row[0] if row else (digest(configured) if configured else "")
    return bool(expected and hmac.compare_digest(digest(candidate), expected))


def extract_cookie(response, name=COOKIE):
    cookies = getattr(response, "cookies", None)
    if cookies is not None:
        try:
            value = cookies.get(name)
        except Exception:
            value = None
        if value:
            return str(value)
    headers = getattr(response, "headers", None)
    if headers is not None:
        if hasattr(headers, "get_list"):
            raw_list = headers.get_list("set-cookie")
        else:
            raw = headers.get("set-cookie")
            raw_list = [raw] if raw else []
        if raw_list:
            from http.cookies import SimpleCookie
            for raw in raw_list:
                if not raw:
                    continue
                parsed = SimpleCookie()
                parsed.load(raw)
                if name in parsed and parsed[name].value:
                    return parsed[name].value
    return extract_cookie(response, LEGACY_COOKIE) if name == COOKIE else ""


def set_session_cookie(response, request, data):
    max_age = max(0, int((parse(data["expiresAt"]) - moment()).total_seconds()))
    response.set_cookie(COOKIE, data["sessionToken"], httponly=True, samesite="lax", path="/",
                        secure=True, max_age=max_age)
    # Central HTTPS hubs emit only __Host-burnrate_access unless an operator
    # explicitly opts into BURNRATE_LEGACY_COOKIE=1 for older clients.
    from spend_app.connection_api import central_server
    legacy_opt_in = os.getenv("BURNRATE_LEGACY_COOKIE", "").strip().lower() in {"1", "true", "yes"}
    if not central_server() or (request.url.scheme == "https" and legacy_opt_in):
        response.set_cookie(LEGACY_COOKIE, data["sessionToken"], httponly=True, samesite="lax", path="/",
                            secure=request.url.scheme == "https", max_age=max_age)
    return response


def scoped_machine_read(settings, pricing, principal, request):
    """A deliberately small read projection; no hub settings, plans or registry."""
    from spend_app.native_usage import usage_report
    report = usage_report(settings, pricing, now=moment(), period="all", machine=principal.id)
    report.pop("plans", None)
    report.pop("machines", None)
    report["scope"] = "computer"
    return report
