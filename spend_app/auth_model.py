"""Principals, expiring human sessions, one-use pairing grants and admin audit.

Only credential hashes live on the hub. Transactions serialize credential
replacement and grant redemption; no process-local authentication cache exists.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from spend_app.db import connect

MACHINE_DAYS = 90
LEGACY_MACHINE_DAYS = 180
SESSION_DAYS = 30
PAIRING_MINUTES = 15
COOKIE = "burnrate_access"
MARKER = "auth_model.v1"


def moment(now=None):
    return (now or datetime.now(UTC)).astimezone(UTC)


def stamp(now=None):
    return moment(now).isoformat().replace("+00:00", "Z")


def parse(value):
    try:
        result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return result.replace(tzinfo=UTC) if result.tzinfo is None else result.astimezone(UTC)
    except (ValueError, TypeError):
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


def audit(connection, actor, action, target, outcome="success", *, now=None):
    """Callers pass only fixed action/target names or server-issued opaque IDs."""
    connection.execute(
        "INSERT INTO admin_audit(actor_type,actor_id,action,target,outcome,timestamp) "
        "VALUES(?,?,?,?,?,?)",
        (actor.kind, actor.id, action, target, outcome, stamp(now)),
    )


def ensure_schema(path):
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (MARKER,)).fetchone():
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (MARKER,)).fetchone():
            return
        for sql in (
            "CREATE TABLE IF NOT EXISTS owner_sessions (id TEXT PRIMARY KEY, owner_id TEXT NOT NULL, "
            "role TEXT NOT NULL, token_hash TEXT NOT NULL, created_at TEXT NOT NULL, "
            "last_used_at TEXT, expires_at TEXT NOT NULL, revoked_at TEXT, device_label TEXT NOT NULL)",
            "CREATE TABLE IF NOT EXISTS pairing_grants (id TEXT PRIMARY KEY, secret_hash TEXT NOT NULL, "
            "hub_origin TEXT NOT NULL, machine_id TEXT, machine_name TEXT NOT NULL, os_name TEXT NOT NULL, "
            "created_at TEXT NOT NULL, expires_at TEXT NOT NULL, used_at TEXT)",
            "CREATE TABLE IF NOT EXISTS admin_audit (id INTEGER PRIMARY KEY AUTOINCREMENT, "
            "actor_type TEXT NOT NULL, actor_id TEXT, action TEXT NOT NULL, target TEXT NOT NULL, "
            "outcome TEXT NOT NULL, timestamp TEXT NOT NULL)",
            "CREATE TRIGGER IF NOT EXISTS admin_audit_no_update BEFORE UPDATE ON admin_audit "
            "BEGIN SELECT RAISE(ABORT, 'admin audit is append-only'); END",
            "CREATE TRIGGER IF NOT EXISTS admin_audit_no_delete BEFORE DELETE ON admin_audit "
            "BEGIN SELECT RAISE(ABORT, 'admin audit is append-only'); END",
        ):
            connection.execute(sql)
        changed = False
        for key in ("machines.v1", "accounts.v1"):
            row = connection.execute("SELECT value FROM app_meta WHERE key=?", (key,)).fetchone()
            if not row:
                continue
            data = json.loads(row[0])  # Corrupt auth state fails closed, never reset it.
            records = data["machines" if key == "machines.v1" else "users"]
            for rec in records.values():
                if key == "machines.v1":
                    created = parse(rec.get("created_at"))
                    rec.setdefault("expires_at", stamp(created + timedelta(days=LEGACY_MACHINE_DAYS))
                                   if created else stamp(datetime(1970, 1, 1, tzinfo=UTC)))
                    rec.setdefault("last_used_at", None)
                    rec.setdefault("rotation_required", True)
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


def create_session(connection, actor, device_label="Browser", *, subject_id=None, now=None):
    sid = secrets.token_hex(16)
    token = f"br_s_{sid}_{secrets.token_urlsafe(32)}"
    expiry = stamp(moment(now) + timedelta(days=SESSION_DAYS))
    connection.execute(
        "INSERT INTO owner_sessions VALUES(?,?,?,?,?,?,?,?,?)",
        (sid, subject_id or actor.id, actor.role, digest(token), stamp(now), None, expiry, None, str(device_label)[:80]),
    )
    audit(connection, actor, "session.create", sid, now=now)
    return {"sessionToken": token, "sessionId": sid, "expiresAt": expiry}


def authenticate_session(path, token, *, via="bearer", now=None):
    if not token.startswith("br_s_"):
        return None
    sid = token[5:].split("_", 1)[0]
    with connect(path) as connection:
        row = connection.execute("SELECT * FROM owner_sessions WHERE id=?", (sid,)).fetchone()
        if (not row or not hmac.compare_digest(digest(token), row["token_hash"])
                or row["revoked_at"] or not parse(row["expires_at"])
                or parse(row["expires_at"]) <= moment(now) or row["role"] != "owner"):
            return None
        connection.execute("UPDATE owner_sessions SET last_used_at=? WHERE id=?", (stamp(now), sid))
        return Principal("owner", row["owner_id"], row["role"], sid, via)


def revoke_session(connection, actor, sid, *, now=None):
    result = connection.execute("UPDATE owner_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                                (stamp(now), sid))
    if result.rowcount:
        audit(connection, actor, "session.revoke", sid, now=now)
    return bool(result.rowcount)


def break_glass_matches(path, candidate, configured):
    if not candidate:
        return False
    with connect(path) as connection:
        row = connection.execute("SELECT value FROM app_meta WHERE key='auth.break_glass.v1'").fetchone()
    expected = row[0] if row else (digest(configured) if configured else "")
    return bool(expected and hmac.compare_digest(digest(candidate), expected))


def set_session_cookie(response, request, data):
    response.set_cookie(COOKIE, data["sessionToken"], httponly=True, samesite="lax", path="/api",
                        secure=request.url.scheme == "https", max_age=SESSION_DAYS * 86400)
    return response


def scoped_machine_read(settings, pricing, principal, request):
    """A deliberately small read projection; no hub settings, plans or registry."""
    from spend_app.native_usage import usage_report
    report = usage_report(settings, pricing, now=moment(), period="all", machine=principal.id)
    report.pop("plans", None)
    report.pop("machines", None)
    report["scope"] = "computer"
    return report
