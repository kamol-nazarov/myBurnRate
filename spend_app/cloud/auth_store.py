"""Central auth.db for myBurnRate Cloud: users, OTPs, pairing codes, sessions, machines, audit."""
from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from spend_app.cloud.tenant import DEFAULT_MAX_DB_BYTES, DEFAULT_MAX_DEVICES
from spend_app.local_access import protect_file
from spend_app.setup_wizard import (
    MIN_PASSWORD_LENGTH,
    PBKDF2_ITERATIONS,
    _hash_password,
    _verify_password,
    normalize_email,
)

CLOUD_SESSION_PREFIX = "br_cs_"
CLOUD_MACHINE_PREFIX = "br_cm_"
MAX_PASSWORD_LENGTH = 512
SESSION_DAYS = 30
SESSION_ABSOLUTE_DAYS = 90
MACHINE_DAYS = 90
OTP_TTL_MINUTES = 15
OTP_MAX_ATTEMPTS = 5
PENDING_PURGE_HOURS = 24
PAIRING_TTL_SECONDS = 300
PAIRING_MAX_ACTIVE = 3
# Crockford Base32 without ambiguous 0/O/1/I/L
_PAIRING_ALPHABET = "23456789ABCDEFGHJKMNPQRSTVWXYZ"
_DEVICE_ID_RE = re.compile(r"^[A-Za-z0-9._-]{8,128}$")
_MACHINE_ID_RE = re.compile(r"^[a-f0-9]{16}$")


class CloudAuthError(Exception):
    def __init__(self, code: str, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True)
class CloudPrincipal:
    kind: str = "unauthenticated"  # "owner" | "machine" | "unauthenticated"
    user_id: str | None = None
    email: str | None = None
    session_id: str | None = None
    machine_id: str | None = None
    password_authenticated: bool = False
    max_devices: int = DEFAULT_MAX_DEVICES
    max_db_bytes: int = DEFAULT_MAX_DB_BYTES

    @property
    def id(self) -> str | None:
        return self.machine_id if self.kind == "machine" else self.user_id

    @property
    def role(self) -> str:
        return "owner" if self.kind == "owner" else self.kind

    def permits(self, permission: str) -> bool:
        if self.kind == "owner" and self.user_id:
            if permission == "account.sensitive":
                return self.password_authenticated
            return permission in {"hub.read", "admin.write", "credentials.manage"}
        return False


def _moment(now: datetime | None = None) -> datetime:
    return (now or datetime.now(UTC)).astimezone(UTC)


def _stamp(now: datetime | None = None) -> str:
    return _moment(now).isoformat().replace("+00:00", "Z")


def _parse(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (ValueError, TypeError, OverflowError):
        return None


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _new_otp() -> str:
    return f"{secrets.randbelow(1_000_000):06d}"


def _new_pairing_code() -> str:
    raw = "".join(secrets.choice(_PAIRING_ALPHABET) for _ in range(8))
    return f"{raw[:4]}-{raw[4:]}"


def _normalize_pairing_code(raw: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9]", "", str(raw or "")).upper()
    if len(cleaned) != 8 or any(ch not in _PAIRING_ALPHABET for ch in cleaned):
        return ""
    return cleaned


def _clean_device_id(raw: str | None) -> str | None:
    if raw is None:
        return None
    val = str(raw).strip()
    if not val:
        return None
    if not _DEVICE_ID_RE.fullmatch(val):
        raise CloudAuthError("DEV-302", "Invalid device identifier.", status_code=422)
    return val


def _clean_computer_name(raw: str | None) -> str:
    val = (str(raw or "").strip() or "Connected Computer")[:64]
    return val


_SCHEMA_STATEMENTS: tuple[str, ...] = (
    """CREATE TABLE IF NOT EXISTS users (
        id TEXT PRIMARY KEY,
        email TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        iterations INTEGER NOT NULL,
        status TEXT NOT NULL DEFAULT 'active',
        max_devices INTEGER NOT NULL DEFAULT 5,
        max_db_bytes INTEGER NOT NULL DEFAULT 262144000,
        created_at TEXT NOT NULL,
        password_changed_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS pending_registrations (
        email TEXT PRIMARY KEY,
        password_hash TEXT NOT NULL,
        salt TEXT NOT NULL,
        iterations INTEGER NOT NULL,
        code_hash TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT NOT NULL,
        purge_at TEXT NOT NULL,
        created_ip TEXT,
        created_at TEXT NOT NULL
    )""",
    """CREATE TABLE IF NOT EXISTS email_otps (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        purpose TEXT NOT NULL,
        target_email TEXT NOT NULL,
        code_hash TEXT NOT NULL,
        attempts INTEGER NOT NULL DEFAULT 0,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        created_ip TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_email_otps_user_purpose ON email_otps(user_id, purpose, created_at)",
    """CREATE TABLE IF NOT EXISTS device_pairing_codes (
        code_hash TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        created_by_session_id TEXT,
        expires_at TEXT NOT NULL,
        consumed_at TEXT,
        consumed_machine_id TEXT,
        created_at TEXT NOT NULL
    )""",
    "CREATE INDEX IF NOT EXISTS idx_pairing_user ON device_pairing_codes(user_id, expires_at)",
    """CREATE TABLE IF NOT EXISTS user_sessions (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        token_hash TEXT NOT NULL,
        machine_id TEXT,
        device_label TEXT NOT NULL,
        password_authenticated INTEGER NOT NULL DEFAULT 0,
        auth_time TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_used_at TEXT,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_sessions_user ON user_sessions(user_id, revoked_at)",
    "CREATE INDEX IF NOT EXISTS idx_user_sessions_machine ON user_sessions(machine_id)",
    """CREATE TABLE IF NOT EXISTS user_machines (
        id TEXT PRIMARY KEY,
        user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        device_id TEXT,
        name TEXT NOT NULL,
        reported_name TEXT,
        os TEXT,
        token_hash TEXT NOT NULL,
        created_at TEXT NOT NULL,
        last_seen_at TEXT,
        expires_at TEXT NOT NULL,
        revoked_at TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_user_machines_user ON user_machines(user_id, revoked_at)",
    """CREATE UNIQUE INDEX IF NOT EXISTS idx_user_machines_user_device
        ON user_machines(user_id, device_id) WHERE device_id IS NOT NULL AND revoked_at IS NULL""",
    """CREATE TABLE IF NOT EXISTS cloud_audit (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        user_id TEXT,
        actor_type TEXT NOT NULL,
        actor_id TEXT,
        action TEXT NOT NULL,
        target TEXT NOT NULL,
        outcome TEXT NOT NULL,
        source TEXT,
        timestamp TEXT NOT NULL
    )""",
    """CREATE TRIGGER IF NOT EXISTS cloud_audit_no_update BEFORE UPDATE ON cloud_audit
        BEGIN SELECT RAISE(ABORT, 'cloud audit is append-only'); END""",
    """CREATE TRIGGER IF NOT EXISTS cloud_audit_no_delete BEFORE DELETE ON cloud_audit
        BEGIN SELECT RAISE(ABORT, 'cloud audit is append-only'); END""",
)


class CloudAuthStore:
    """Thread/process-safe SQLite WAL backing store for central cloud auth."""

    def __init__(
        self,
        db_path: Path,
        *,
        default_max_devices: int = DEFAULT_MAX_DEVICES,
        default_max_db_bytes: int = DEFAULT_MAX_DB_BYTES,
    ) -> None:
        self.path = Path(db_path)
        self.default_max_devices = int(default_max_devices)
        self.default_max_db_bytes = int(default_max_db_bytes)
        self.initialize()

    @contextmanager
    def _connect(self, *, write: bool = False):
        if write:
            self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        try:
            conn.execute("PRAGMA busy_timeout=15000" if not write else "PRAGMA busy_timeout=30000")
            if write:
                conn.execute("PRAGMA foreign_keys=ON")
                conn.execute("PRAGMA secure_delete=ON")
                conn.execute("BEGIN IMMEDIATE")
            else:
                conn.execute("PRAGMA query_only=ON")
            yield conn
            if write and conn.in_transaction:
                conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()

    def initialize(self) -> None:
        self.path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        conn = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        try:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA secure_delete=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("BEGIN IMMEDIATE")
            for stmt in _SCHEMA_STATEMENTS:
                conn.execute(stmt)
            conn.execute("COMMIT")
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        finally:
            conn.close()
        try:
            protect_file(self.path)
        except OSError:
            pass

    def _audit(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str | None,
        actor_type: str,
        actor_id: str | None,
        action: str,
        target: str,
        outcome: str = "success",
        source: str | None = None,
        now: datetime | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO cloud_audit(user_id,actor_type,actor_id,action,target,outcome,source,timestamp) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (user_id, actor_type, actor_id, action, target, outcome, source, _stamp(now)),
        )

    def _issue_session(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        device_label: str,
        machine_id: str | None,
        password_authenticated: bool,
        auth_time: datetime | None = None,
        source: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        created = _moment(now)
        authenticated = auth_time or created
        expires = min(
            created + timedelta(days=SESSION_DAYS),
            authenticated + timedelta(days=SESSION_ABSOLUTE_DAYS),
        )
        sid = secrets.token_hex(16)
        token = f"{CLOUD_SESSION_PREFIX}{sid}_{secrets.token_urlsafe(32)}"
        conn.execute(
            "INSERT INTO user_sessions(id,user_id,token_hash,machine_id,device_label,"
            "password_authenticated,auth_time,created_at,last_used_at,expires_at,revoked_at) "
            "VALUES(?,?,?,?,?,?,?,?,NULL,?,NULL)",
            (
                sid,
                user_id,
                _sha256(token),
                machine_id,
                _clean_computer_name(device_label),
                1 if password_authenticated else 0,
                _stamp(authenticated),
                _stamp(created),
                _stamp(expires),
            ),
        )
        self._audit(
            conn,
            user_id=user_id,
            actor_type="owner",
            actor_id=user_id,
            action="session.create",
            target=sid,
            source=source,
            now=created,
        )
        return {"sessionToken": token, "sessionId": sid, "expiresAt": _stamp(expires)}

    def _register_or_reconnect_machine_in_tx(
        self,
        conn: sqlite3.Connection,
        *,
        user_id: str,
        max_devices: int,
        computer_name: str,
        os_name: str,
        device_id: str | None,
        source: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        moment_now = _moment(now)
        clean_name = _clean_computer_name(computer_name)
        clean_os = str(os_name or "").strip()[:48]
        clean_dev = _clean_device_id(device_id)

        existing = None
        if clean_dev is not None:
            existing = conn.execute(
                "SELECT * FROM user_machines WHERE user_id=? AND device_id=? AND revoked_at IS NULL",
                (user_id, clean_dev),
            ).fetchone()

        expires = moment_now + timedelta(days=MACHINE_DAYS)
        if existing is not None:
            mid = existing["id"]
            token = f"{CLOUD_MACHINE_PREFIX}{mid}_{secrets.token_urlsafe(32)}"
            conn.execute(
                "UPDATE user_machines SET token_hash=?, reported_name=?, os=?, last_seen_at=?, expires_at=? "
                "WHERE id=?",
                (_sha256(token), clean_name, clean_os or existing["os"], _stamp(moment_now), _stamp(expires), mid),
            )
            name_out = existing["name"]
        else:
            active_count = conn.execute(
                "SELECT COUNT(*) FROM user_machines WHERE user_id=? AND revoked_at IS NULL",
                (user_id,),
            ).fetchone()[0]
            if active_count >= max_devices:
                raise CloudAuthError(
                    "CLOUD-409",
                    f"Device limit reached ({max_devices} active computers). Revoke an unused computer first.",
                    status_code=409,
                )
            mid = secrets.token_hex(8)
            token = f"{CLOUD_MACHINE_PREFIX}{mid}_{secrets.token_urlsafe(32)}"
            conn.execute(
                "INSERT INTO user_machines(id,user_id,device_id,name,reported_name,os,token_hash,"
                "created_at,last_seen_at,expires_at,revoked_at) VALUES(?,?,?,?,?,?,?,?,?,?,NULL)",
                (
                    mid,
                    user_id,
                    clean_dev,
                    clean_name,
                    clean_name,
                    clean_os,
                    _sha256(token),
                    _stamp(moment_now),
                    _stamp(moment_now),
                    _stamp(expires),
                ),
            )
            name_out = clean_name
        self._audit(
            conn,
            user_id=user_id,
            actor_type="owner",
            actor_id=user_id,
            action="machine.register",
            target=mid,
            source=source,
            now=moment_now,
        )
        return {
            "machine": {
                "id": mid,
                "name": name_out,
                "reportedName": clean_name,
                "os": clean_os,
                "deviceId": clean_dev,
                "expiresAt": _stamp(expires),
            },
            "token": token,
            "expiresAt": _stamp(expires),
        }

    # -- Registration (2-step 6-digit email OTP) --------------------------------

    def start_registration(
        self,
        email: str,
        password: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str | None]:
        """Stage a pending registration and return ``('pending', otp)`` or ``('existing', None)``."""
        clean_email = normalize_email(email)
        if clean_email is None:
            raise CloudAuthError("AUTH-106", "Enter a valid email address.", status_code=422)
        if not isinstance(password, str) or len(password) < MIN_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        if len(password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        moment_now = _moment(now)
        salt_hex, hash_hex = _hash_password(password, iterations=PBKDF2_ITERATIONS)
        code = _new_otp()

        with self._connect(write=True) as conn:
            # Opportunistically purge stale unverified rows
            conn.execute("DELETE FROM pending_registrations WHERE purge_at <= ?", (_stamp(moment_now),))
            existing = conn.execute(
                "SELECT id FROM users WHERE email=? AND status='active'", (clean_email,)
            ).fetchone()
            if existing is not None:
                self._audit(
                    conn,
                    user_id=existing["id"],
                    actor_type="unauthenticated",
                    actor_id=None,
                    action="account.register_duplicate",
                    target=existing["id"],
                    outcome="ignored",
                    source=client_ip,
                    now=moment_now,
                )
                return "existing", None
            conn.execute(
                "INSERT INTO pending_registrations(email,password_hash,salt,iterations,code_hash,"
                "attempts,expires_at,purge_at,created_ip,created_at) VALUES(?,?,?,?,?,0,?,?,?,?) "
                "ON CONFLICT(email) DO UPDATE SET password_hash=excluded.password_hash, salt=excluded.salt, "
                "iterations=excluded.iterations, code_hash=excluded.code_hash, attempts=0, "
                "expires_at=excluded.expires_at, purge_at=excluded.purge_at, "
                "created_ip=excluded.created_ip, created_at=excluded.created_at",
                (
                    clean_email,
                    hash_hex,
                    salt_hex,
                    PBKDF2_ITERATIONS,
                    _sha256(code),
                    _stamp(moment_now + timedelta(minutes=OTP_TTL_MINUTES)),
                    _stamp(moment_now + timedelta(hours=PENDING_PURGE_HOURS)),
                    client_ip,
                    _stamp(moment_now),
                ),
            )
            self._audit(
                conn,
                user_id=None,
                actor_type="unauthenticated",
                actor_id=None,
                action="account.register_start",
                target=_sha256(clean_email)[:16],
                source=client_ip,
                now=moment_now,
            )
        return "pending", code

    def verify_registration(
        self,
        email: str,
        code: str,
        *,
        computer_name: str = "Connected Computer",
        os_name: str = "",
        device_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        clean_email = normalize_email(email)
        clean_code = str(code or "").strip()
        if clean_email is None or not re.fullmatch(r"\d{6}", clean_code):
            raise CloudAuthError("AUTH-108", "Invalid or expired verification code.", status_code=401)
        moment_now = _moment(now)
        rejection: CloudAuthError | None = None
        result: dict | None = None
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM pending_registrations WHERE email=?", (clean_email,)
            ).fetchone()
            if row is None:
                rejection = CloudAuthError("AUTH-108", "Invalid or expired verification code.", status_code=401)
            else:
                expires = _parse(row["expires_at"])
                if expires is None or moment_now >= expires or int(row["attempts"]) >= OTP_MAX_ATTEMPTS:
                    conn.execute("DELETE FROM pending_registrations WHERE email=?", (clean_email,))
                    rejection = CloudAuthError(
                        "AUTH-108", "Verification code expired. Request a new code.", status_code=401
                    )
                elif not hmac.compare_digest(_sha256(clean_code), row["code_hash"]):
                    new_attempts = int(row["attempts"]) + 1
                    if new_attempts >= OTP_MAX_ATTEMPTS:
                        conn.execute("DELETE FROM pending_registrations WHERE email=?", (clean_email,))
                    else:
                        conn.execute(
                            "UPDATE pending_registrations SET attempts=? WHERE email=?",
                            (new_attempts, clean_email),
                        )
                    rejection = CloudAuthError("AUTH-108", "Invalid or expired verification code.", status_code=401)
                elif conn.execute("SELECT 1 FROM users WHERE email=?", (clean_email,)).fetchone():
                    conn.execute("DELETE FROM pending_registrations WHERE email=?", (clean_email,))
                    rejection = CloudAuthError("AUTH-103", "Account already exists.", status_code=409)
                else:
                    uid = secrets.token_hex(16)
                    stamp_now = _stamp(moment_now)
                    conn.execute(
                        "INSERT INTO users(id,email,password_hash,salt,iterations,status,max_devices,"
                        "max_db_bytes,created_at,password_changed_at) VALUES(?,?,?,?,?,'active',?,?,?,?)",
                        (
                            uid,
                            clean_email,
                            row["password_hash"],
                            row["salt"],
                            int(row["iterations"]),
                            self.default_max_devices,
                            self.default_max_db_bytes,
                            stamp_now,
                            stamp_now,
                        ),
                    )
                    conn.execute("DELETE FROM pending_registrations WHERE email=?", (clean_email,))
                    self._audit(
                        conn,
                        user_id=uid,
                        actor_type="owner",
                        actor_id=uid,
                        action="account.register",
                        target=uid,
                        source=client_ip,
                        now=moment_now,
                    )
                    paired = self._register_or_reconnect_machine_in_tx(
                        conn,
                        user_id=uid,
                        max_devices=self.default_max_devices,
                        computer_name=computer_name,
                        os_name=os_name,
                        device_id=device_id,
                        source=client_ip,
                        now=moment_now,
                    )
                    session = self._issue_session(
                        conn,
                        user_id=uid,
                        device_label=computer_name,
                        machine_id=paired["machine"]["id"],
                        password_authenticated=True,
                        source=client_ip,
                        now=moment_now,
                    )
                    result = {
                        "user": {"id": uid, "email": clean_email},
                        "sessionToken": session["sessionToken"],
                        "sessionId": session["sessionId"],
                        "sessionExpiresAt": session["expiresAt"],
                        "machineToken": paired["token"],
                        "machineExpiresAt": paired["expiresAt"],
                        "machine": paired["machine"],
                    }
        if rejection is not None:
            raise rejection
        assert result is not None
        return result

    # -- Password Login ---------------------------------------------------------

    def login(
        self,
        email: str,
        password: str,
        *,
        computer_name: str = "Connected Computer",
        os_name: str = "",
        device_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        clean_email = normalize_email(email)
        if clean_email is None or not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            _verify_password("invalid", "00" * 16, "00" * 32, PBKDF2_ITERATIONS)
            raise CloudAuthError("AUTH-101", "Invalid email or password.", status_code=401)
        moment_now = _moment(now)
        rejection: CloudAuthError | None = None
        result: dict | None = None
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM users WHERE email=? AND status='active'", (clean_email,)
            ).fetchone()
            if row is None:
                _verify_password(password, "00" * 16, "00" * 32, PBKDF2_ITERATIONS)
                rejection = CloudAuthError("AUTH-101", "Invalid email or password.", status_code=401)
            else:
                ok = _verify_password(password, row["salt"], row["password_hash"], int(row["iterations"]))
                if not ok:
                    self._audit(
                        conn,
                        user_id=row["id"],
                        actor_type="unauthenticated",
                        actor_id=None,
                        action="login",
                        target=row["id"],
                        outcome="failure",
                        source=client_ip,
                        now=moment_now,
                    )
                    rejection = CloudAuthError("AUTH-101", "Invalid email or password.", status_code=401)
                else:
                    uid = row["id"]
                    paired = self._register_or_reconnect_machine_in_tx(
                        conn,
                        user_id=uid,
                        max_devices=int(row["max_devices"]),
                        computer_name=computer_name,
                        os_name=os_name,
                        device_id=device_id,
                        source=client_ip,
                        now=moment_now,
                    )
                    session = self._issue_session(
                        conn,
                        user_id=uid,
                        device_label=computer_name,
                        machine_id=paired["machine"]["id"],
                        password_authenticated=True,
                        source=client_ip,
                        now=moment_now,
                    )
                    self._audit(
                        conn,
                        user_id=uid,
                        actor_type="owner",
                        actor_id=uid,
                        action="login",
                        target=uid,
                        source=client_ip,
                        now=moment_now,
                    )
                    result = {
                        "user": {"id": uid, "email": clean_email},
                        "sessionToken": session["sessionToken"],
                        "sessionId": session["sessionId"],
                        "sessionExpiresAt": session["expiresAt"],
                        "machineToken": paired["token"],
                        "machineExpiresAt": paired["expiresAt"],
                        "machine": paired["machine"],
                    }
        if rejection is not None:
            raise rejection
        assert result is not None
        return result

    # -- One-Time 8-Char Device Pairing Code ------------------------------------

    def create_pairing_code(
        self,
        user_id: str,
        session_id: str | None,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        moment_now = _moment(now)
        expires = moment_now + timedelta(seconds=PAIRING_TTL_SECONDS)
        code = _new_pairing_code()
        raw_norm = _normalize_pairing_code(code)
        with self._connect(write=True) as conn:
            conn.execute(
                "DELETE FROM device_pairing_codes WHERE user_id=? AND (expires_at <= ? OR consumed_at IS NOT NULL)",
                (user_id, _stamp(moment_now)),
            )
            active = conn.execute(
                "SELECT code_hash FROM device_pairing_codes WHERE user_id=? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
            if len(active) >= PAIRING_MAX_ACTIVE:
                for old in active[: len(active) - PAIRING_MAX_ACTIVE + 1]:
                    conn.execute("DELETE FROM device_pairing_codes WHERE code_hash=?", (old["code_hash"],))
            conn.execute(
                "INSERT INTO device_pairing_codes(code_hash,user_id,created_by_session_id,expires_at,"
                "consumed_at,consumed_machine_id,created_at) VALUES(?,?,?,?,NULL,NULL,?)",
                (_sha256(raw_norm), user_id, session_id, _stamp(expires), _stamp(moment_now)),
            )
            self._audit(
                conn,
                user_id=user_id,
                actor_type="owner",
                actor_id=user_id,
                action="pairing_code.create",
                target=user_id,
                source=client_ip,
                now=moment_now,
            )
        return {
            "code": code,
            "expiresAt": _stamp(expires),
            "expiresInSeconds": PAIRING_TTL_SECONDS,
        }

    def redeem_pairing_code(
        self,
        code: str,
        *,
        computer_name: str = "Connected Computer",
        os_name: str = "",
        device_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        norm = _normalize_pairing_code(code)
        if not norm:
            raise CloudAuthError("AUTH-108", "Invalid or expired pairing code.", status_code=401)
        moment_now = _moment(now)
        code_hash = _sha256(norm)
        rejection: CloudAuthError | None = None
        result: dict | None = None
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM device_pairing_codes WHERE code_hash=?", (code_hash,)
            ).fetchone()
            if row is None or row["consumed_at"] is not None:
                rejection = CloudAuthError("AUTH-108", "Invalid or expired pairing code.", status_code=401)
            else:
                expires = _parse(row["expires_at"])
                if expires is None or moment_now >= expires:
                    conn.execute("DELETE FROM device_pairing_codes WHERE code_hash=?", (code_hash,))
                    rejection = CloudAuthError("AUTH-108", "Invalid or expired pairing code.", status_code=401)
                else:
                    user = conn.execute(
                        "SELECT * FROM users WHERE id=? AND status='active'", (row["user_id"],)
                    ).fetchone()
                    if user is None:
                        rejection = CloudAuthError("AUTH-108", "Invalid or expired pairing code.", status_code=401)
                    else:
                        uid = user["id"]
                        paired = self._register_or_reconnect_machine_in_tx(
                            conn,
                            user_id=uid,
                            max_devices=int(user["max_devices"]),
                            computer_name=computer_name,
                            os_name=os_name,
                            device_id=device_id,
                            source=client_ip,
                            now=moment_now,
                        )
                        conn.execute(
                            "UPDATE device_pairing_codes SET consumed_at=?, consumed_machine_id=? WHERE code_hash=?",
                            (_stamp(moment_now), paired["machine"]["id"], code_hash),
                        )
                        # Paired device gets a device-bound session (password_authenticated=0) so it can
                        # view All computers & sync, but cannot change password/email/delete account.
                        session = self._issue_session(
                            conn,
                            user_id=uid,
                            device_label=computer_name,
                            machine_id=paired["machine"]["id"],
                            password_authenticated=False,
                            source=client_ip,
                            now=moment_now,
                        )
                        self._audit(
                            conn,
                            user_id=uid,
                            actor_type="owner",
                            actor_id=uid,
                            action="pairing_code.redeem",
                            target=paired["machine"]["id"],
                            source=client_ip,
                            now=moment_now,
                        )
                        result = {
                            "user": {"id": uid, "email": user["email"]},
                            "sessionToken": session["sessionToken"],
                            "sessionId": session["sessionId"],
                            "sessionExpiresAt": session["expiresAt"],
                            "machineToken": paired["token"],
                            "machineExpiresAt": paired["expiresAt"],
                            "machine": paired["machine"],
                        }
        if rejection is not None:
            raise rejection
        assert result is not None
        return result

    # -- Authentication & Rotation ----------------------------------------------

    def authenticate_session(self, token: str, *, now: datetime | None = None) -> CloudPrincipal | None:
        raw = str(token or "").strip()
        if not raw.startswith(CLOUD_SESSION_PREFIX):
            return None
        sid = raw[len(CLOUD_SESSION_PREFIX) :].split("_", 1)[0]
        if not sid:
            return None
        moment_now = _moment(now)
        with self._connect(write=False) as conn:
            row = conn.execute(
                "SELECT s.*, u.email, u.status, u.max_devices, u.max_db_bytes "
                "FROM user_sessions s JOIN users u ON u.id = s.user_id WHERE s.id=?",
                (sid,),
            ).fetchone()
            if row is None or row["status"] != "active" or row["revoked_at"] is not None:
                return None
            if not hmac.compare_digest(_sha256(raw), row["token_hash"]):
                return None
            expires = _parse(row["expires_at"])
            auth_time = _parse(row["auth_time"])
            if expires is None or auth_time is None:
                return None
            if moment_now >= min(expires, auth_time + timedelta(days=SESSION_ABSOLUTE_DAYS)):
                return None
            last_used = _parse(row["last_used_at"])
        if last_used is None or (moment_now - last_used) >= timedelta(seconds=60):
            with self._connect(write=True) as wconn:
                wconn.execute("UPDATE user_sessions SET last_used_at=? WHERE id=?", (_stamp(moment_now), sid))
        return CloudPrincipal(
            kind="owner",
            user_id=row["user_id"],
            email=row["email"],
            session_id=sid,
            machine_id=row["machine_id"],
            password_authenticated=bool(row["password_authenticated"]),
            max_devices=int(row["max_devices"]),
            max_db_bytes=int(row["max_db_bytes"]),
        )

    def authenticate_machine(self, token: str, *, now: datetime | None = None) -> CloudPrincipal | None:
        raw = str(token or "").strip()
        if not raw.startswith(CLOUD_MACHINE_PREFIX):
            return None
        mid = raw[len(CLOUD_MACHINE_PREFIX) :].split("_", 1)[0]
        if not _MACHINE_ID_RE.fullmatch(mid):
            return None
        moment_now = _moment(now)
        with self._connect(write=False) as conn:
            row = conn.execute(
                "SELECT m.*, u.email, u.status, u.max_devices, u.max_db_bytes "
                "FROM user_machines m JOIN users u ON u.id = m.user_id WHERE m.id=?",
                (mid,),
            ).fetchone()
            if row is None or row["status"] != "active" or row["revoked_at"] is not None:
                return None
            if not hmac.compare_digest(_sha256(raw), row["token_hash"]):
                return None
            expires = _parse(row["expires_at"])
            if expires is None or moment_now >= expires:
                return None
            last_seen = _parse(row["last_seen_at"])
        if last_seen is None or (moment_now - last_seen) >= timedelta(seconds=60):
            with self._connect(write=True) as wconn:
                wconn.execute("UPDATE user_machines SET last_seen_at=? WHERE id=?", (_stamp(moment_now), mid))
        return CloudPrincipal(
            kind="machine",
            user_id=row["user_id"],
            email=row["email"],
            machine_id=mid,
            max_devices=int(row["max_devices"]),
            max_db_bytes=int(row["max_db_bytes"]),
        )

    def rotate_session(
        self,
        principal: CloudPrincipal,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        if principal.kind != "owner" or not principal.user_id or not principal.session_id:
            return None
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM user_sessions WHERE id=? AND user_id=?",
                (principal.session_id, principal.user_id),
            ).fetchone()
            if row is None or row["revoked_at"] is not None:
                return None
            expires = _parse(row["expires_at"])
            auth_time = _parse(row["auth_time"])
            if expires is None or auth_time is None or moment_now >= min(
                expires, auth_time + timedelta(days=SESSION_ABSOLUTE_DAYS)
            ):
                return None
            conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE id=? AND revoked_at IS NULL",
                (_stamp(moment_now), principal.session_id),
            )
            self._audit(
                conn,
                user_id=principal.user_id,
                actor_type="owner",
                actor_id=principal.user_id,
                action="session.revoke",
                target=principal.session_id,
                source=client_ip,
                now=moment_now,
            )
            return self._issue_session(
                conn,
                user_id=principal.user_id,
                device_label=row["device_label"],
                machine_id=row["machine_id"],
                password_authenticated=bool(row["password_authenticated"]),
                auth_time=auth_time,
                source=client_ip,
                now=moment_now,
            )

    def rotate_machine(
        self,
        principal: CloudPrincipal,
        raw_token: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict | None:
        if principal.kind != "machine" or not principal.user_id or not principal.machine_id:
            return None
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            row = conn.execute(
                "SELECT * FROM user_machines WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (principal.machine_id, principal.user_id),
            ).fetchone()
            if row is None or not hmac.compare_digest(_sha256(raw_token), row["token_hash"]):
                return None
            expires = _parse(row["expires_at"])
            if expires is None or moment_now >= expires:
                return None
            new_token = f"{CLOUD_MACHINE_PREFIX}{principal.machine_id}_{secrets.token_urlsafe(32)}"
            new_expires = moment_now + timedelta(days=MACHINE_DAYS)
            conn.execute(
                "UPDATE user_machines SET token_hash=?, last_seen_at=?, expires_at=? WHERE id=?",
                (_sha256(new_token), _stamp(moment_now), _stamp(new_expires), principal.machine_id),
            )
            self._audit(
                conn,
                user_id=principal.user_id,
                actor_type="machine",
                actor_id=principal.machine_id,
                action="machine.rotate",
                target=principal.machine_id,
                source=client_ip,
                now=moment_now,
            )
        return {
            "ok": True,
            "machineId": principal.machine_id,
            "token": new_token,
            "expiresAt": _stamp(new_expires),
        }

    # -- Sessions & Machines Management -----------------------------------------

    def get_user_profile(self, user_id: str) -> dict | None:
        with self._connect(write=False) as conn:
            row = conn.execute(
                "SELECT id, email, status, max_devices, max_db_bytes, created_at, password_changed_at "
                "FROM users WHERE id=? AND status='active'",
                (user_id,),
            ).fetchone()
            if row is None:
                return None
            active_devices = int(
                conn.execute(
                    "SELECT COUNT(*) FROM user_machines WHERE user_id=? AND revoked_at IS NULL",
                    (user_id,),
                ).fetchone()[0]
                or 0
            )
            active_sessions = int(
                conn.execute(
                    "SELECT COUNT(*) FROM user_sessions WHERE user_id=? AND revoked_at IS NULL",
                    (user_id,),
                ).fetchone()[0]
                or 0
            )
        return {
            "id": row["id"],
            "email": row["email"],
            "createdAt": row["created_at"],
            "passwordChangedAt": row["password_changed_at"],
            "activeDevices": active_devices,
            "maxDevices": int(row["max_devices"]),
            "activeSessions": active_sessions,
            "maxDbBytes": int(row["max_db_bytes"]),
        }

    def register_machine(
        self,
        user_id: str,
        *,
        computer_name: str,
        os_name: str = "",
        device_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT max_devices FROM users WHERE id=? AND status='active'", (user_id,)
            ).fetchone()
            if user is None:
                raise CloudAuthError("AUTH-107", "Account unavailable.", status_code=401)
            return self._register_or_reconnect_machine_in_tx(
                conn,
                user_id=user_id,
                max_devices=int(user["max_devices"]),
                computer_name=computer_name,
                os_name=os_name,
                device_id=device_id,
                source=client_ip,
                now=now,
            )

    def list_sessions(self, user_id: str) -> list[dict]:
        with self._connect(write=False) as conn:
            rows = conn.execute(
                "SELECT id, machine_id, device_label, password_authenticated, auth_time, "
                "created_at, last_used_at, expires_at, revoked_at "
                "FROM user_sessions WHERE user_id=? ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def list_machines(self, user_id: str, *, now: datetime | None = None) -> list[dict]:
        moment_now = _moment(now)
        with self._connect(write=False) as conn:
            rows = conn.execute(
                "SELECT id, name, reported_name, os, device_id, created_at, last_seen_at, expires_at, revoked_at "
                "FROM user_machines WHERE user_id=? ORDER BY created_at ASC",
                (user_id,),
            ).fetchall()
        out = []
        for r in rows:
            last_seen = _parse(r["last_seen_at"])
            online = bool(r["revoked_at"] is None and last_seen and (moment_now - last_seen) < timedelta(minutes=5))
            out.append(
                {
                    "id": r["id"],
                    "name": r["name"],
                    "reportedName": r["reported_name"],
                    "os": r["os"],
                    "isLocal": False,
                    "revoked": r["revoked_at"] is not None,
                    "revokedAt": r["revoked_at"],
                    "online": online,
                    "lastSeenAt": r["last_seen_at"],
                    "expiresAt": r["expires_at"],
                }
            )
        return out

    def revoke_session(
        self,
        user_id: str,
        session_id: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "UPDATE user_sessions SET revoked_at=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (_stamp(moment_now), session_id, user_id),
            )
            if cur.rowcount:
                self._audit(
                    conn,
                    user_id=user_id,
                    actor_type="owner",
                    actor_id=user_id,
                    action="session.revoke",
                    target=session_id,
                    source=client_ip,
                    now=moment_now,
                )
            return bool(cur.rowcount)

    def revoke_machine(
        self,
        user_id: str,
        machine_id: str,
        *,
        actor_type: str = "owner",
        actor_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> bool:
        moment_now = _moment(now)
        stamp_now = _stamp(moment_now)
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "UPDATE user_machines SET revoked_at=?, token_hash='' WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (stamp_now, machine_id, user_id),
            )
            if not cur.rowcount:
                return False
            # Also revoke every owner session bound to this device (S-04 invariant)
            bound = conn.execute(
                "SELECT id FROM user_sessions WHERE user_id=? AND machine_id=? AND revoked_at IS NULL",
                (user_id, machine_id),
            ).fetchall()
            for s in bound:
                conn.execute("UPDATE user_sessions SET revoked_at=? WHERE id=?", (stamp_now, s["id"]))
                self._audit(
                    conn,
                    user_id=user_id,
                    actor_type=actor_type,
                    actor_id=actor_id or user_id,
                    action="session.revoke",
                    target=s["id"],
                    source=client_ip,
                    now=moment_now,
                )
            self._audit(
                conn,
                user_id=user_id,
                actor_type=actor_type,
                actor_id=actor_id or user_id,
                action="machine.revoke",
                target=machine_id,
                source=client_ip,
                now=moment_now,
            )
        return True

    def rename_machine(
        self,
        user_id: str,
        machine_id: str,
        new_name: str,
        *,
        actor_type: str = "owner",
        actor_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        clean = str(new_name or "").strip()
        if not clean or len(clean) > 64:
            raise CloudAuthError("DEV-302", "Machine name must be between 1 and 64 characters.", status_code=422)
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            cur = conn.execute(
                "UPDATE user_machines SET name=? WHERE id=? AND user_id=? AND revoked_at IS NULL",
                (clean, machine_id, user_id),
            )
            if not cur.rowcount:
                raise CloudAuthError("DEV-301", "Unknown or revoked computer.", status_code=404)
            self._audit(
                conn,
                user_id=user_id,
                actor_type=actor_type,
                actor_id=actor_id or user_id,
                action="machine.rename",
                target=machine_id,
                source=client_ip,
                now=moment_now,
            )
        return {"ok": True, "id": machine_id, "name": clean}

    # -- Password Reset, Password Change, Email Change, Account Deletion --------

    def request_password_reset(
        self,
        email: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str | None, str | None]:
        clean = normalize_email(email)
        if clean is None:
            return None, None
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE email=? AND status='active'", (clean,)
            ).fetchone()
            if user is None:
                return None, None
            uid = user["id"]
            # Invalidate prior unconsumed password_reset codes for this user
            conn.execute(
                "UPDATE email_otps SET consumed_at=? WHERE user_id=? AND purpose='password_reset' AND consumed_at IS NULL",
                (_stamp(moment_now), uid),
            )
            code = _new_otp()
            conn.execute(
                "INSERT INTO email_otps(id,user_id,purpose,target_email,code_hash,attempts,"
                "expires_at,consumed_at,created_ip,created_at) VALUES(?,?,?,?,?,0,?,NULL,?,?)",
                (
                    secrets.token_hex(16),
                    uid,
                    "password_reset",
                    clean,
                    _sha256(code),
                    _stamp(moment_now + timedelta(minutes=OTP_TTL_MINUTES)),
                    client_ip,
                    _stamp(moment_now),
                ),
            )
            self._audit(
                conn,
                user_id=uid,
                actor_type="unauthenticated",
                actor_id=None,
                action="account.password_reset_request",
                target=uid,
                source=client_ip,
                now=moment_now,
            )
        return uid, code

    def confirm_password_reset(
        self,
        email: str,
        code: str,
        new_password: str,
        *,
        disconnect_devices: bool = False,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        clean_email = normalize_email(email)
        clean_code = str(code or "").strip()
        if clean_email is None or not re.fullmatch(r"\d{6}", clean_code):
            raise CloudAuthError("AUTH-108", "Invalid or expired reset code.", status_code=401)
        if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        if len(new_password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        moment_now = _moment(now)
        stamp_now = _stamp(moment_now)
        rejection: CloudAuthError | None = None
        result: dict | None = None
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT id FROM users WHERE email=? AND status='active'", (clean_email,)
            ).fetchone()
            if user is None:
                rejection = CloudAuthError("AUTH-108", "Invalid or expired reset code.", status_code=401)
            else:
                uid = user["id"]
                otp = conn.execute(
                    "SELECT * FROM email_otps WHERE user_id=? AND purpose='password_reset' AND consumed_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (uid,),
                ).fetchone()
                if otp is None:
                    rejection = CloudAuthError("AUTH-108", "Invalid or expired reset code.", status_code=401)
                else:
                    expires = _parse(otp["expires_at"])
                    if expires is None or moment_now >= expires or int(otp["attempts"]) >= OTP_MAX_ATTEMPTS:
                        conn.execute("UPDATE email_otps SET consumed_at=? WHERE id=?", (stamp_now, otp["id"]))
                        rejection = CloudAuthError(
                            "AUTH-108", "Reset code expired. Request a new code.", status_code=401
                        )
                    elif not hmac.compare_digest(_sha256(clean_code), otp["code_hash"]):
                        new_attempts = int(otp["attempts"]) + 1
                        if new_attempts >= OTP_MAX_ATTEMPTS:
                            conn.execute(
                                "UPDATE email_otps SET attempts=?, consumed_at=? WHERE id=?",
                                (new_attempts, stamp_now, otp["id"]),
                            )
                        else:
                            conn.execute("UPDATE email_otps SET attempts=? WHERE id=?", (new_attempts, otp["id"]))
                        rejection = CloudAuthError("AUTH-108", "Invalid or expired reset code.", status_code=401)
                    else:
                        salt_hex, hash_hex = _hash_password(new_password, iterations=PBKDF2_ITERATIONS)
                        conn.execute(
                            "UPDATE users SET password_hash=?, salt=?, iterations=?, password_changed_at=? WHERE id=?",
                            (hash_hex, salt_hex, PBKDF2_ITERATIONS, stamp_now, uid),
                        )
                        conn.execute("UPDATE email_otps SET consumed_at=? WHERE id=?", (stamp_now, otp["id"]))
                        # Always revoke ALL owner sessions on reset
                        revoked_sessions = int(
                            conn.execute(
                                "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                                (stamp_now, uid),
                            ).rowcount
                            or 0
                        )
                        revoked_devices = 0
                        if disconnect_devices:
                            revoked_devices = int(
                                conn.execute(
                                    "UPDATE user_machines SET revoked_at=?, token_hash='' "
                                    "WHERE user_id=? AND revoked_at IS NULL",
                                    (stamp_now, uid),
                                ).rowcount
                                or 0
                            )
                        # Invalidate any open pairing codes
                        conn.execute("DELETE FROM device_pairing_codes WHERE user_id=?", (uid,))
                        self._audit(
                            conn,
                            user_id=uid,
                            actor_type="owner",
                            actor_id=uid,
                            action="account.password_reset",
                            target=uid,
                            source=client_ip,
                            now=moment_now,
                        )
                        result = {
                            "ok": True,
                            "userId": uid,
                            "email": clean_email,
                            "revokedSessions": revoked_sessions,
                            "revokedDevices": revoked_devices,
                        }
        if rejection is not None:
            raise rejection
        assert result is not None
        return result

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        *,
        current_session_id: str | None = None,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> dict:
        if not isinstance(new_password, str) or len(new_password) < MIN_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at least {MIN_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        if len(new_password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError(
                "AUTH-105",
                f"Password must be at most {MAX_PASSWORD_LENGTH} characters.",
                status_code=422,
            )
        if not isinstance(current_password, str) or not current_password or len(current_password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError("AUTH-101", "Current password is incorrect.", status_code=401)
        moment_now = _moment(now)
        stamp_now = _stamp(moment_now)
        rejection: CloudAuthError | None = None
        email_out = ""
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND status='active'", (user_id,)
            ).fetchone()
            if user is None or not _verify_password(
                str(current_password or ""), user["salt"], user["password_hash"], int(user["iterations"])
            ):
                self._audit(
                    conn,
                    user_id=user_id,
                    actor_type="owner",
                    actor_id=user_id,
                    action="account.password_change",
                    target=user_id,
                    outcome="failure",
                    source=client_ip,
                    now=moment_now,
                )
                rejection = CloudAuthError("AUTH-101", "Current password is incorrect.", status_code=401)
            else:
                email_out = user["email"]
                salt_hex, hash_hex = _hash_password(new_password, iterations=PBKDF2_ITERATIONS)
                conn.execute(
                    "UPDATE users SET password_hash=?, salt=?, iterations=?, password_changed_at=? WHERE id=?",
                    (hash_hex, salt_hex, PBKDF2_ITERATIONS, stamp_now, user_id),
                )
                if current_session_id:
                    conn.execute(
                        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND id != ? AND revoked_at IS NULL",
                        (stamp_now, user_id, current_session_id),
                    )
                else:
                    conn.execute(
                        "UPDATE user_sessions SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                        (stamp_now, user_id),
                    )
                self._audit(
                    conn,
                    user_id=user_id,
                    actor_type="owner",
                    actor_id=user_id,
                    action="account.password_change",
                    target=user_id,
                    source=client_ip,
                    now=moment_now,
                )
        if rejection is not None:
            raise rejection
        return {"ok": True, "email": email_out}

    def request_email_change(
        self,
        user_id: str,
        password: str,
        new_email: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        clean_new = normalize_email(new_email)
        if clean_new is None:
            raise CloudAuthError("AUTH-106", "Enter a valid new email address.", status_code=422)
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError("AUTH-101", "Password is incorrect.", status_code=401)
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND status='active'", (user_id,)
            ).fetchone()
            if user is None or not _verify_password(
                str(password or ""), user["salt"], user["password_hash"], int(user["iterations"])
            ):
                raise CloudAuthError("AUTH-101", "Password is incorrect.", status_code=401)
            if user["email"] == clean_new:
                raise CloudAuthError("AUTH-106", "New email matches current email.", status_code=422)
            if conn.execute("SELECT 1 FROM users WHERE email=? AND id != ?", (clean_new, user_id)).fetchone():
                raise CloudAuthError("AUTH-103", "That email address is already in use.", status_code=409)
            conn.execute(
                "UPDATE email_otps SET consumed_at=? WHERE user_id=? AND purpose='email_change' AND consumed_at IS NULL",
                (_stamp(moment_now), user_id),
            )
            code = _new_otp()
            conn.execute(
                "INSERT INTO email_otps(id,user_id,purpose,target_email,code_hash,attempts,"
                "expires_at,consumed_at,created_ip,created_at) VALUES(?,?,?,?,?,0,?,NULL,?,?)",
                (
                    secrets.token_hex(16),
                    user_id,
                    "email_change",
                    clean_new,
                    _sha256(code),
                    _stamp(moment_now + timedelta(minutes=OTP_TTL_MINUTES)),
                    client_ip,
                    _stamp(moment_now),
                ),
            )
            self._audit(
                conn,
                user_id=user_id,
                actor_type="owner",
                actor_id=user_id,
                action="account.email_change_request",
                target=user_id,
                source=client_ip,
                now=moment_now,
            )
        return clean_new, code

    def confirm_email_change(
        self,
        user_id: str,
        code: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> tuple[str, str]:
        clean_code = str(code or "").strip()
        if not re.fullmatch(r"\d{6}", clean_code):
            raise CloudAuthError("AUTH-108", "Invalid or expired verification code.", status_code=401)
        moment_now = _moment(now)
        stamp_now = _stamp(moment_now)
        rejection: CloudAuthError | None = None
        pair_out: tuple[str, str] | None = None
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND status='active'", (user_id,)
            ).fetchone()
            if user is None:
                rejection = CloudAuthError("AUTH-107", "Account unavailable.", status_code=401)
            else:
                otp = conn.execute(
                    "SELECT * FROM email_otps WHERE user_id=? AND purpose='email_change' AND consumed_at IS NULL "
                    "ORDER BY created_at DESC LIMIT 1",
                    (user_id,),
                ).fetchone()
                if otp is None:
                    rejection = CloudAuthError("AUTH-108", "Invalid or expired verification code.", status_code=401)
                else:
                    expires = _parse(otp["expires_at"])
                    if expires is None or moment_now >= expires or int(otp["attempts"]) >= OTP_MAX_ATTEMPTS:
                        conn.execute("UPDATE email_otps SET consumed_at=? WHERE id=?", (stamp_now, otp["id"]))
                        rejection = CloudAuthError("AUTH-108", "Verification code expired.", status_code=401)
                    elif not hmac.compare_digest(_sha256(clean_code), otp["code_hash"]):
                        new_attempts = int(otp["attempts"]) + 1
                        if new_attempts >= OTP_MAX_ATTEMPTS:
                            conn.execute(
                                "UPDATE email_otps SET attempts=?, consumed_at=? WHERE id=?",
                                (new_attempts, stamp_now, otp["id"]),
                            )
                        else:
                            conn.execute("UPDATE email_otps SET attempts=? WHERE id=?", (new_attempts, otp["id"]))
                        rejection = CloudAuthError(
                            "AUTH-108", "Invalid or expired verification code.", status_code=401
                        )
                    else:
                        old_email = user["email"]
                        new_email = otp["target_email"]
                        if conn.execute(
                            "SELECT 1 FROM users WHERE email=? AND id != ?", (new_email, user_id)
                        ).fetchone():
                            rejection = CloudAuthError(
                                "AUTH-103", "That email address is already in use.", status_code=409
                            )
                        else:
                            conn.execute("UPDATE users SET email=? WHERE id=?", (new_email, user_id))
                            conn.execute("UPDATE email_otps SET consumed_at=? WHERE id=?", (stamp_now, otp["id"]))
                            self._audit(
                                conn,
                                user_id=user_id,
                                actor_type="owner",
                                actor_id=user_id,
                                action="account.email_set",
                                target=user_id,
                                source=client_ip,
                                now=moment_now,
                            )
                            pair_out = (old_email, new_email)
        if rejection is not None:
            raise rejection
        assert pair_out is not None
        return pair_out

    def delete_account(
        self,
        user_id: str,
        password: str,
        *,
        client_ip: str | None = None,
        now: datetime | None = None,
    ) -> str:
        if not isinstance(password, str) or not password or len(password) > MAX_PASSWORD_LENGTH:
            raise CloudAuthError("AUTH-101", "Password is incorrect.", status_code=401)
        moment_now = _moment(now)
        with self._connect(write=True) as conn:
            user = conn.execute(
                "SELECT * FROM users WHERE id=? AND status='active'", (user_id,)
            ).fetchone()
            if user is None or not _verify_password(
                str(password or ""), user["salt"], user["password_hash"], int(user["iterations"])
            ):
                raise CloudAuthError("AUTH-101", "Password is incorrect.", status_code=401)
            email = user["email"]
            # CASCADE removes sessions, machines, email_otps, and device_pairing_codes
            conn.execute("DELETE FROM users WHERE id=?", (user_id,))
            self._audit(
                conn,
                user_id=None,
                actor_type="owner",
                actor_id=user_id,
                action="account.delete",
                target=user_id,
                source=client_ip,
                now=moment_now,
            )
        return email

    def purge_expired(self, *, now: datetime | None = None) -> dict[str, int]:
        moment_now = _moment(now)
        stamp_now = _stamp(moment_now)
        otp_cutoff = _stamp(moment_now - timedelta(days=7))
        session_cutoff = _stamp(moment_now - timedelta(days=30))
        with self._connect(write=True) as conn:
            pending = int(
                conn.execute("DELETE FROM pending_registrations WHERE purge_at <= ?", (stamp_now,)).rowcount or 0
            )
            otps = int(
                conn.execute(
                    "DELETE FROM email_otps WHERE expires_at <= ? OR (consumed_at IS NOT NULL AND consumed_at <= ?)",
                    (otp_cutoff, otp_cutoff),
                ).rowcount
                or 0
            )
            pairings = int(
                conn.execute(
                    "DELETE FROM device_pairing_codes WHERE expires_at <= ? OR consumed_at IS NOT NULL",
                    (stamp_now,),
                ).rowcount
                or 0
            )
            sessions = int(
                conn.execute(
                    "DELETE FROM user_sessions WHERE (revoked_at IS NOT NULL AND revoked_at <= ?) OR expires_at <= ?",
                    (session_cutoff, session_cutoff),
                ).rowcount
                or 0
            )
        return {"pending": pending, "otps": otps, "pairings": pairings, "sessions": sessions}
