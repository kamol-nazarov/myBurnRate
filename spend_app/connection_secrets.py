"""BURNRATE-owned credential-store references and explicit, bounded API tests."""
import hashlib
import logging
import os
import sys
import threading
import uuid
from pathlib import Path
from datetime import UTC, datetime, timedelta

import httpx

from spend_app.connection_paths import LocationError
from spend_app.integration_policy import assert_allowed_host, identity_headers

# Balance sources are checked against their own policy lane.
POLICY_LANES = {"openrouter": "openrouter_credits", "deepseek": "deepseek_balance"}
LOGGER = logging.getLogger(__name__)


def store_name() -> str:
    if os.name == "nt":
        return "Windows Credential Manager"
    if sys.platform == "darwin":
        return "The macOS Keychain"
    return "The desktop keyring (Secret Service)"


class Vault:
    def __init__(self, database):
        self.service = "BURNRATE/connections/" + hashlib.sha256(os.path.normcase(str(database)).encode()).hexdigest()[:24]

    def backend(self):
        # The operating system's own store, named explicitly; never keyring's
        # global backend selection, plaintext plugins or fallback stores.
        try:
            if os.name == "nt":
                from keyring.backends.Windows import WinVaultKeyring
                return WinVaultKeyring()
            if sys.platform == "darwin":
                from keyring.backends.macOS import Keyring
            else:
                from keyring.backends.SecretService import Keyring
            if Keyring.priority <= 0:  # raises when the store is not running
                raise RuntimeError()
            return Keyring()
        except Exception:
            raise LocationError(f"{store_name()} is unavailable. Local setup still works.") from None

    def available(self):
        try:
            self.backend()
            return True
        except LocationError:
            return False

    def put(self, secret, ref=None):
        ref = ref or "burnrate-" + uuid.uuid4().hex
        previous = None
        try:
            previous = self.get(ref)
        except Exception:
            previous = None
        try:
            self.backend().set_password(self.service, ref, secret)
            if self.get(ref) != secret:
                raise RuntimeError()
            return ref
        except Exception:
            if previous:
                try:
                    if self.get(ref) != previous:
                        self.backend().set_password(self.service, ref, previous)
                except Exception:
                    pass
            else:
                try:
                    self.delete(ref)
                except Exception:
                    pass
            raise LocationError("Secure credential storage failed; the existing connection was preserved.") from None

    def get(self, ref):
        if not isinstance(ref, str) or not ref.startswith("burnrate-"):
            raise LocationError("Invalid credential reference.")
        try:
            secret = self.backend().get_password(self.service, ref)
            if not secret:
                raise ValueError()
            return secret
        except Exception:
            raise LocationError("Saved credential unavailable. Replace it in Connect harness.") from None

    def delete(self, ref):
        if not ref.startswith("burnrate-"):
            raise LocationError("Not a BURNRATE-owned credential.")
        backend = self.backend()
        if backend.get_password(self.service, ref) is not None:
            backend.delete_password(self.service, ref)


OWNER_SESSION_VAULT_REF = "burnrate-owner-session"
MACHINE_TOKEN_VAULT_REF = "burnrate-machine-token"
PENDING_REVOKE_VAULT_REF = "burnrate-pending-revoke"

_IN_MEMORY_REVOKES_LOCK = threading.Lock()
_IN_MEMORY_PENDING_REVOKES: list[dict] = []
_REVOKE_RETENTION_WARNING = False

_VAULT_FACTORY = None
_DB_SECRET_LOCKS_GUARD = threading.Lock()
_DB_SECRET_LOCKS: dict[str, threading.RLock] = {}


def db_secret_lock(database_path: Path | str) -> threading.RLock:
    """Return the per-database lock serializing vault + setup state updates."""
    key = os.path.normcase(os.path.abspath(str(database_path)))
    with _DB_SECRET_LOCKS_GUARD:
        lock = _DB_SECRET_LOCKS.get(key)
        if lock is None:
            lock = threading.RLock()
            _DB_SECRET_LOCKS[key] = lock
        return lock


def set_vault_factory(factory):
    """Install (or clear with ``None``) the vault factory used by ``get_vault``."""
    global _VAULT_FACTORY
    previous = _VAULT_FACTORY
    _VAULT_FACTORY = factory
    return previous


def get_vault(database):
    """Return the active credential vault for ``database``."""
    if _VAULT_FACTORY is not None:
        return _VAULT_FACTORY(database)
    return Vault(database)


def read_vault_secret(database_path: Path | str | None, ref: str, *, vault=None) -> str | None:
    """Read a trimmed non-empty secret from the vault, or None if absent/unavailable."""
    if vault is None:
        if database_path is None:
            return None
        try:
            vault = get_vault(database_path)
        except Exception:
            return None
    if vault is None:
        return None
    try:
        val = vault.get(ref)
        return val.strip() if isinstance(val, str) and val.strip() else None
    except Exception:
        return None


def credential_origin_ref(token: str) -> str:
    return "burnrate-origin-" + hashlib.sha256(token.encode()).hexdigest()


def credential_origin_allowed(database_path, token: str, origin: str, spoke: dict, *, vault=None) -> bool:
    from spend_app.hub_origin import bound_hub_origin

    if not token:
        return False
    try:
        expected = bound_hub_origin(spoke, origin)
    except ValueError:
        return False
    recorded = read_vault_secret(database_path, credential_origin_ref(token), vault=vault)
    if recorded is None:
        return not bool(spoke.get("credentialOrigin"))
    return recorded == expected


def verified_vault_put(vault, secret: str, ref: str) -> str:
    """Write ``secret`` to ``vault`` at ``ref`` and verify readback matches."""
    if vault is None:
        raise LocationError("Secure credential storage is unavailable.")
    clean = str(secret or "")
    if not clean:
        raise LocationError("Cannot store an empty credential.")
    previous = None
    try:
        previous = vault.get(ref)
    except Exception:
        previous = None
    stored_ref = vault.put(clean, ref) or ref
    readback = vault.get(stored_ref)
    if readback != clean:
        if previous:
            try:
                vault.put(previous, stored_ref)
            except Exception:
                pass
        else:
            try:
                vault.delete(stored_ref)
            except Exception:
                pass
        raise LocationError("Secure credential readback failed.")
    return stored_ref


def _restore_vault_secret(vault, ref: str, previous: str | None) -> None:
    if vault is None:
        return
    if previous:
        try:
            verified_vault_put(vault, previous, ref)
        except Exception:
            pass
    else:
        try:
            vault.delete(ref)
        except Exception:
            pass


def _delete_vault_if_matches(vault, ref: str, expected_val: str | None) -> None:
    if vault is None or not expected_val:
        return
    try:
        current = vault.get(ref)
        if isinstance(current, str) and current.strip() == expected_val:
            vault.delete(ref)
    except Exception:
        pass


def _compensate_publish_failure(
    db_file: Path,
    use_vault,
    *,
    snap_gen: int,
    snap_legacy_owner: str,
    snap_legacy_machine: str,
    prev_owner: str | None,
    prev_machine: str | None,
    wrote_owner: bool,
    wrote_machine: bool,
    written_owner_val: str | None,
    written_machine_val: str | None,
) -> None:
    """Fenced B4 compensation under a fresh SQLite BEGIN IMMEDIATE lock.

    Restores previous vault refs only when nobody else has published or cleared
    since the failed transaction's snapshot. If another process won, leaves its
    credentials intact and only deletes uncommitted refs still matching ours.
    """
    from spend_app.db import connect
    from spend_app.setup_wizard import read_setup_state

    try:
        with connect(db_file) as comp_conn:
            comp_conn.execute("BEGIN IMMEDIATE")
            cur_state = read_setup_state(comp_conn)
            cur_spoke = dict(cur_state.get("spokeConfig") or {})
            cur_gen = int(cur_spoke.get("credentialGeneration") or 0)
            cur_legacy_owner = str(cur_spoke.get("ownerSession") or "").strip()
            cur_legacy_machine = str(cur_spoke.get("token") or "").strip()
            cur_vault_owner = read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=use_vault)
            cur_vault_machine = read_vault_secret(db_file, MACHINE_TOKEN_VAULT_REF, vault=use_vault)

            owner_uncontested = (
                cur_vault_owner == written_owner_val
                if wrote_owner
                else cur_vault_owner == prev_owner
            )
            machine_uncontested = (
                cur_vault_machine == written_machine_val
                if wrote_machine
                else cur_vault_machine == prev_machine
            )
            nobody_else_won = (
                cur_gen == snap_gen
                and cur_legacy_owner == snap_legacy_owner
                and cur_legacy_machine == snap_legacy_machine
                and owner_uncontested
                and machine_uncontested
            )
            if nobody_else_won:
                if wrote_owner:
                    _restore_vault_secret(use_vault, OWNER_SESSION_VAULT_REF, prev_owner)
                if wrote_machine:
                    _restore_vault_secret(use_vault, MACHINE_TOKEN_VAULT_REF, prev_machine)
            else:
                if wrote_owner and written_owner_val != prev_owner:
                    _delete_vault_if_matches(use_vault, OWNER_SESSION_VAULT_REF, written_owner_val)
                if wrote_machine and written_machine_val != prev_machine:
                    _delete_vault_if_matches(use_vault, MACHINE_TOKEN_VAULT_REF, written_machine_val)
            try:
                comp_conn.commit()
            except Exception:
                pass
    except Exception:
        pass


MAX_PENDING_REVOKES = 64
MAX_FLUSH_BATCH = 8
PENDING_REVOKE_CALL_TIMEOUT = 3.0
PENDING_REVOKE_BASE_BACKOFF_SECONDS = 5
_PENDING_REVOKE_KINDS = frozenset({"owner_logout", "machine_revoke_owner", "machine_self_revoke", "owner"})


def _token_sha256(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _parse_pending_iso(value: object) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _format_pending_iso(moment: datetime) -> str:
    return moment.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _normalize_pending_kind(raw_kind: object, *, has_machine_id: bool = False) -> str:
    k = str(raw_kind or "").strip()
    if k in {"owner_logout", "machine_revoke_owner", "machine_self_revoke"}:
        return k
    if k == "owner":
        return "owner" if has_machine_id else "owner_logout"
    return "owner_logout"


def _deterministic_pending_id(kind: str, token_hash: str, hub_url: str, machine_id: str) -> str:
    payload = f"{kind}|{token_hash}|{hub_url}|{machine_id}"
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _normalize_pending_entry(
    item: dict,
    *,
    now_iso: str,
    generate_id: bool = False,
) -> dict | None:
    if not isinstance(item, dict):
        return None
    tok = str(item.get("token") or "").strip()
    if not tok:
        return None
    mid = str(item.get("machineId") or "").strip()
    hub_url = str(item.get("hubUrl") or "").strip()
    kind = _normalize_pending_kind(item.get("kind"), has_machine_id=bool(mid))
    token_hash = _token_sha256(tok)
    entry_id = str(item.get("id") or "").strip()
    if not entry_id:
        entry_id = uuid.uuid4().hex if generate_id else _deterministic_pending_id(kind, token_hash, hub_url, mid)
    created_at = str(item.get("createdAt") or "").strip() or now_iso
    raw_attempts = item.get("attempts")
    try:
        attempts = max(0, int(raw_attempts)) if raw_attempts is not None else 0
    except (TypeError, ValueError):
        attempts = 0
    next_attempt = str(item.get("nextAttemptAt") or "").strip() or None
    last_error = str(item.get("lastError") or "").strip() or None
    entry: dict = {
        "id": entry_id,
        "kind": kind,
        "tokenHash": token_hash,
        "token": tok,
        "createdAt": created_at,
        "attempts": attempts,
    }
    if mid:
        entry["machineId"] = mid
    if hub_url:
        entry["hubUrl"] = hub_url
    if next_attempt:
        entry["nextAttemptAt"] = next_attempt
    if last_error:
        entry["lastError"] = last_error
    return entry


def _pending_dedupe_key(entry: dict) -> tuple[str, str, str, str]:
    return (
        str(entry.get("kind") or "owner_logout"),
        str(entry.get("tokenHash") or ""),
        str(entry.get("hubUrl") or ""),
        str(entry.get("machineId") or ""),
    )


def _backoff_next_attempt(moment: datetime, attempts: int, *, retry_after: int | None = None) -> str:
    delay = PENDING_REVOKE_BASE_BACKOFF_SECONDS * (2 ** max(0, min(attempts - 1, 8)))
    if isinstance(retry_after, int) and retry_after > 0:
        delay = max(delay, min(retry_after, 3600))
    else:
        delay = min(delay, 1800)
    return _format_pending_iso(moment + timedelta(seconds=delay))


def is_terminal_revoke_response(
    kind: str,
    status_code: int,
    data: object = None,
    *,
    headers=None,
    err_code: str | None = None,
) -> bool:
    """Return True only when the hub proved success or that the target/credential is already invalid."""
    if 200 <= status_code < 300:
        return True
    if 300 <= status_code < 400:
        return False
    resolved_code = err_code
    if resolved_code is None:
        from spend_app.error_codes import extract_error_code

        resolved_code, _ = extract_error_code(data, headers=headers)
    norm_kind = "owner_logout" if kind == "owner" else kind
    if norm_kind == "owner_logout":
        if status_code == 401:
            return True
        if status_code == 403:
            return False
        return resolved_code == "AUTH-107" and status_code not in {400, 429} and status_code < 500
    if norm_kind == "machine_revoke_owner":
        if status_code in (401, 403):
            return False
        if status_code in (404, 410):
            return True
        return resolved_code in {"DEV-301", "DEV-302"} and status_code == 422
    if norm_kind == "machine_self_revoke":
        if status_code == 403:
            return False
        if status_code in (401, 404, 410):
            return True
        return resolved_code in {"DEV-301", "DEV-302"} and status_code == 422
    return False


def response_is_terminal_revoke(kind: str, res, call_err: dict | None = None) -> bool:
    """Classify an HTTP/TestClient response + optional _call_hub error dict for S9."""
    if res is None:
        return False
    status_code = getattr(res, "status_code", 0)
    try:
        status_code = int(status_code)
    except (TypeError, ValueError):
        return False
    data = None
    try:
        data = res.json() if getattr(res, "content", b"") else None
    except Exception:
        data = None
    err_code = (call_err or {}).get("code") if status_code != 403 else None
    return is_terminal_revoke_response(
        kind,
        status_code,
        data,
        headers=getattr(res, "headers", None),
        err_code=err_code,
    )


def _parse_pending_revokes(raw: str | None) -> list[dict]:
    import json

    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except ValueError:
        return []
    if not isinstance(parsed, list):
        return []
    now_iso = _format_pending_iso(datetime.now(UTC))
    out: list[dict] = []
    seen_keys: set[tuple[str, str, str, str]] = set()
    for item in parsed:
        norm = _normalize_pending_entry(item, now_iso=now_iso, generate_id=False)
        if norm is None:
            continue
        k = _pending_dedupe_key(norm)
        if k in seen_keys:
            continue
        seen_keys.add(k)
        out.append(norm)
    return out


def _merge_pending_revokes_with_ids(
    existing: list[dict], new_entries: list[dict]
) -> tuple[list[dict], list[dict]]:
    now_iso = _format_pending_iso(datetime.now(UTC))
    merged: dict[tuple[str, str, str, str], dict] = {}
    order: list[tuple[str, str, str, str]] = []
    matched_keys: list[tuple[str, str, str, str]] = []
    for item in existing:
        norm = _normalize_pending_entry(item, now_iso=now_iso, generate_id=False)
        if norm is None:
            continue
        k = _pending_dedupe_key(norm)
        if k not in merged:
            merged[k] = norm
            order.append(k)
    for item in new_entries:
        norm = _normalize_pending_entry(item, now_iso=now_iso, generate_id=True)
        if norm is None:
            continue
        k = _pending_dedupe_key(norm)
        if k not in merged:
            merged[k] = norm
            order.append(k)
        else:
            cur = merged[k]
            if norm.get("machineId") and not cur.get("machineId"):
                cur["machineId"] = norm["machineId"]
            if norm.get("hubUrl") and not cur.get("hubUrl"):
                cur["hubUrl"] = norm["hubUrl"]
        if k not in matched_keys:
            matched_keys.append(k)
    return [merged[k] for k in order], [dict(merged[k]) for k in matched_keys]


def _merge_pending_revokes(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    merged, _ = _merge_pending_revokes_with_ids(existing, new_entries)
    return merged


def _write_pending_revokes(use_vault, entries: list[dict], *, database_path: Path | str | None = None) -> None:
    import json
    from spend_app.error_codes import CodedError

    if entries:
        if use_vault is None:
            raise CodedError("APP-903", status_code=503)
        try:
            verified_vault_put(
                use_vault,
                json.dumps(entries, separators=(",", ":"), sort_keys=True),
                PENDING_REVOKE_VAULT_REF,
            )
        except Exception as exc:
            raise CodedError("APP-903", status_code=503) from exc
    else:
        if use_vault is None:
            return
        try:
            use_vault.delete(PENDING_REVOKE_VAULT_REF)
        except Exception as exc:
            if read_vault_secret(database_path, PENDING_REVOKE_VAULT_REF, vault=use_vault) is not None:
                raise CodedError("APP-903", status_code=503) from exc


def read_pending_revokes(database_path: Path | str | None, *, vault=None) -> list[dict]:
    """Read pending revoke entries from the vault only (never SQLite)."""
    raw = read_vault_secret(database_path, PENDING_REVOKE_VAULT_REF, vault=vault)
    return _parse_pending_revokes(raw)


def clear_in_memory_pending_revokes() -> None:
    """Reset process-local unpersisted revocation queue and warning (for tests)."""
    global _REVOKE_RETENTION_WARNING
    with _IN_MEMORY_REVOKES_LOCK:
        _IN_MEMORY_PENDING_REVOKES.clear()
        _REVOKE_RETENTION_WARNING = False


def retained_pending_revoke_count() -> int:
    with _IN_MEMORY_REVOKES_LOCK:
        return len(_IN_MEMORY_PENDING_REVOKES)


def has_revoke_retention_warning() -> bool:
    with _IN_MEMORY_REVOKES_LOCK:
        return bool(_REVOKE_RETENTION_WARNING or _IN_MEMORY_PENDING_REVOKES)


def _retain_in_memory_revoke(entry: dict) -> None:
    global _REVOKE_RETENTION_WARNING
    with _IN_MEMORY_REVOKES_LOCK:
        merged = _merge_pending_revokes(_IN_MEMORY_PENDING_REVOKES, [entry])
        if len(merged) > MAX_PENDING_REVOKES:
            merged = merged[-MAX_PENDING_REVOKES:]
        _IN_MEMORY_PENDING_REVOKES[:] = merged
        _REVOKE_RETENTION_WARNING = True


def pending_revoke_count(database_path: Path | str | None, *, vault=None) -> int:
    """Return the count of vault-stored and retained pending revokes (never exposes tokens or URLs)."""
    return len(read_pending_revokes(database_path, vault=vault)) + retained_pending_revoke_count()


def _resolve_vault_for_pending(db_file: Path | None, vault):
    if vault is not None:
        return vault
    if db_file is None:
        return None
    try:
        return get_vault(db_file)
    except Exception:
        return None


def enqueue_pending_revokes(
    database_path: Path | str | None,
    entries: list[dict],
    *,
    vault=None,
    hub_url: str | None = None,
    client=None,
    allow_flush_on_overflow: bool = True,
) -> None:
    """Atomically persist unrevoked tokens in vault-only ``burnrate-pending-revoke``.

    Uses both ``db_secret_lock`` and SQLite ``BEGIN IMMEDIATE`` (when ``database_path``
    is provided) so concurrent desktop and background processes never lose entries.
    Raises ``CodedError("APP-903", status_code=503)`` if the vault write fails or
    capacity (``MAX_PENDING_REVOKES``) remains exceeded after coalescing/flushing.
    """
    from spend_app.error_codes import CodedError

    clean_entries = [e for e in (entries or []) if isinstance(e, dict) and str(e.get("token") or "").strip()]
    if not clean_entries:
        return
    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None

    def _try_enqueue_locked() -> tuple[bool, object]:
        if lock_cm is not None:
            lock_cm.acquire()
        try:
            use_vault = _resolve_vault_for_pending(db_file, vault)
            if use_vault is None:
                raise CodedError("APP-903", status_code=503)
            if db_file is not None:
                from spend_app.db import connect, initialize

                initialize(db_file)
                with connect(db_file) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    existing = _parse_pending_revokes(
                        read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                    )
                    merged = _merge_pending_revokes(existing, clean_entries)
                    if len(merged) > MAX_PENDING_REVOKES:
                        conn.rollback()
                        return False, use_vault
                    _write_pending_revokes(use_vault, merged, database_path=db_file)
                    conn.commit()
                    return True, use_vault
            existing = _parse_pending_revokes(
                read_vault_secret(None, PENDING_REVOKE_VAULT_REF, vault=use_vault)
            )
            merged = _merge_pending_revokes(existing, clean_entries)
            if len(merged) > MAX_PENDING_REVOKES:
                return False, use_vault
            _write_pending_revokes(use_vault, merged, database_path=None)
            return True, use_vault
        finally:
            if lock_cm is not None:
                lock_cm.release()

    ok, used_vault = _try_enqueue_locked()
    if ok:
        return
    if allow_flush_on_overflow:
        try:
            flush_pending_revokes(
                db_file,
                hub_url,
                vault=used_vault,
                client=client,
                ignore_backoff=True,
            )
        except Exception:
            pass
        ok2, _ = _try_enqueue_locked()
        if ok2:
            return
    raise CodedError("APP-903", status_code=503)


def ack_pending_revokes(
    database_path: Path | str | None,
    acked_ids: set[str] | list[str] | tuple[str, ...],
    *,
    vault=None,
) -> None:
    """Atomically remove acknowledged pending revoke IDs under the shared lock."""
    from spend_app.error_codes import CodedError

    clean_ids = {str(i).strip() for i in (acked_ids or ()) if str(i).strip()}
    if not clean_ids:
        return
    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None
    if lock_cm is not None:
        lock_cm.acquire()
    try:
        use_vault = _resolve_vault_for_pending(db_file, vault)
        if use_vault is None:
            raise CodedError("APP-903", status_code=503)
        if db_file is not None:
            from spend_app.db import connect, initialize

            initialize(db_file)
            with connect(db_file) as conn:
                conn.execute("BEGIN IMMEDIATE")
                latest = _parse_pending_revokes(
                    read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                )
                remaining = [item for item in latest if str(item.get("id") or "") not in clean_ids]
                if len(remaining) != len(latest):
                    _write_pending_revokes(use_vault, remaining, database_path=db_file)
                conn.commit()
        else:
            latest = _parse_pending_revokes(
                read_vault_secret(None, PENDING_REVOKE_VAULT_REF, vault=use_vault)
            )
            remaining = [item for item in latest if str(item.get("id") or "") not in clean_ids]
            if len(remaining) != len(latest):
                _write_pending_revokes(use_vault, remaining, database_path=None)
    finally:
        if lock_cm is not None:
            lock_cm.release()


def _revoke_or_queue_token(
    database_path: Path | str | None,
    hub_url: str | None,
    token: str,
    *,
    kind: str,
    endpoint: str,
    machine_id: str | None = None,
    vault=None,
    client=None,
) -> bool:
    from spend_app.error_codes import HUB_PROTOCOL_VERSION, CodedError
    from spend_app.sync import _hub_post_with_cookie_isolation

    clean_token = str(token or "").strip()
    if not clean_token:
        return True
    clean_hub = str(hub_url or "").strip()
    revoke_ok = False
    if clean_hub:
        headers = {
            "Authorization": f"Bearer {clean_token}",
            "Content-Type": "application/json",
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        }
        try:
            res = _hub_post_with_cookie_isolation(
                clean_hub,
                endpoint,
                headers,
                json_body={},
                client=client,
                timeout=PENDING_REVOKE_CALL_TIMEOUT,
            )
            revoke_ok = response_is_terminal_revoke(kind, res)
        except Exception:
            revoke_ok = False
    if revoke_ok:
        return True
    entry: dict = {"kind": kind, "token": clean_token}
    if clean_hub:
        entry["hubUrl"] = clean_hub
    clean_mid = str(machine_id or "").strip()
    if clean_mid:
        entry["machineId"] = clean_mid
    try:
        enqueue_pending_revokes(
            database_path,
            [entry],
            vault=vault,
            hub_url=clean_hub or None,
            client=client,
        )
    except CodedError:
        from spend_app.redact import redact_hub_urls

        _retain_in_memory_revoke(entry)
        LOGGER.warning(
            "Could not persist pending credential revocation (%s); retained in memory for retry (%s)",
            kind,
            redact_hub_urls(clean_hub) if clean_hub else "no-hub",
        )
        raise
    return False


def revoke_or_queue_machine_token(
    database_path: Path | str | None,
    hub_url: str | None,
    token: str,
    *,
    machine_id: str | None = None,
    vault=None,
    client=None,
) -> bool:
    """Best-effort self-revoke a newly issued/fenced machine token, or queue it durably."""
    return _revoke_or_queue_token(
        database_path,
        hub_url,
        token,
        kind="machine_self_revoke",
        endpoint="/api/machines/self/revoke",
        machine_id=machine_id,
        vault=vault,
        client=client,
    )


def revoke_or_queue_owner_session(
    database_path: Path | str | None,
    hub_url: str | None,
    token: str,
    *,
    vault=None,
    client=None,
) -> bool:
    """Best-effort logout a newly issued/fenced owner session, or queue it durably."""
    return _revoke_or_queue_token(
        database_path,
        hub_url,
        token,
        kind="owner_logout",
        endpoint="/api/auth/logout",
        vault=vault,
        client=client,
    )


def flush_pending_revokes(
    database_path: Path | str | None,
    hub_url: str | None = None,
    *,
    vault=None,
    client=None,
    now: datetime | None = None,
    clock=None,
    ignore_backoff: bool = False,
) -> bool:
    """Retry up to ``MAX_FLUSH_BATCH`` due pending revokes against the hub.

    Snapshot and final reconciliation run under ``db_secret_lock`` + SQLite
    ``BEGIN IMMEDIATE`` while all HTTP calls execute strictly outside locks.
    Returns True only when no pending revokes remain in the queue.
    """
    from spend_app.error_codes import (
        HUB_PROTOCOL_VERSION,
        extract_error_code,
        map_http_error,
        map_transport_error,
    )
    from spend_app.hub_origin import hub_origin
    from spend_app.sync import _hub_post_with_cookie_isolation

    if clock is not None:
        def _now_utc() -> datetime:
            return clock().astimezone(UTC)
    elif now is not None:
        _fixed_now = now.astimezone(UTC)
        def _now_utc() -> datetime:
            return _fixed_now
    else:
        def _now_utc() -> datetime:
            return datetime.now(UTC)

    moment = _now_utc()
    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None

    with _IN_MEMORY_REVOKES_LOCK:
        mem_pending = [dict(item) for item in _IN_MEMORY_PENDING_REVOKES]

    use_vault = None
    vault_pending: list[dict] = []
    if lock_cm is not None:
        lock_cm.acquire()
    try:
        use_vault = _resolve_vault_for_pending(db_file, vault)
        if use_vault is None and not mem_pending:
            return True
        if use_vault is not None:
            try:
                if db_file is not None:
                    from spend_app.db import connect, initialize

                    initialize(db_file)
                    with connect(db_file) as conn:
                        conn.execute("BEGIN IMMEDIATE")
                        vault_pending = _parse_pending_revokes(
                            read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                        )
                        conn.commit()
                else:
                    vault_pending = _parse_pending_revokes(
                        read_vault_secret(None, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                    )
            except Exception:
                vault_pending = []
    finally:
        if lock_cm is not None:
            lock_cm.release()

    pending = _merge_pending_revokes(vault_pending, mem_pending)
    if not pending:
        return True

    due: list[dict] = []
    for item in pending:
        if ignore_backoff:
            due.append(item)
            continue
        due_at = _parse_pending_iso(item.get("nextAttemptAt"))
        if due_at is None or due_at <= moment:
            due.append(item)
    due.sort(key=lambda e: (int(e.get("attempts") or 0), str(e.get("createdAt") or "")))
    batch = due[:MAX_FLUSH_BATCH]
    if not batch:
        return False

    fallback_origin = None
    if hub_url:
        try:
            fallback_origin = hub_origin(hub_url)
        except ValueError:
            fallback_origin = None

    done_ids: set[str] = set()
    converted_to_owner_logout: set[str] = set()
    retry_updates: dict[str, tuple[str, int | None, datetime]] = {}
    any_failed = False

    def _call_revoke(target_origin: str, endpoint: str, headers: dict, json_body: dict, kind: str, use_client):
        try:
            res = _hub_post_with_cookie_isolation(
                target_origin,
                endpoint,
                headers,
                json_body=json_body,
                client=use_client,
                timeout=PENDING_REVOKE_CALL_TIMEOUT,
            )
        except Exception as exc:
            code, _ = map_transport_error(exc)
            return False, code, None
        try:
            res_data = res.json() if getattr(res, "content", b"") else None
        except Exception:
            res_data = None
        if is_terminal_revoke_response(
            kind,
            res.status_code,
            res_data,
            headers=getattr(res, "headers", None),
        ):
            return True, None, None
        if 300 <= res.status_code < 400:
            return False, "HUB-501", None
        mapped = map_http_error(
            res.status_code,
            res_data,
            headers=getattr(res, "headers", None),
            endpoint_path=endpoint,
            has_machine_token=(kind == "machine_self_revoke"),
        )
        _, retry_after = extract_error_code(res_data, headers=getattr(res, "headers", None))
        return False, mapped.code, mapped.retry_after or retry_after

    for item in batch:
        item_id = str(item["id"])
        kind = str(item.get("kind") or "owner_logout")
        tok = str(item.get("token") or "").strip()
        raw_item_hub = str(item.get("hubUrl") or "").strip()
        target_raw = raw_item_hub or fallback_origin or ""
        if not target_raw:
            retry_updates[item_id] = ("NET-001", None, _now_utc())
            any_failed = True
            continue
        try:
            target_origin = hub_origin(target_raw)
        except ValueError:
            err_c = "NET-003" if target_raw.lower().startswith("http://") else "NET-001"
            retry_updates[item_id] = (err_c, None, _now_utc())
            any_failed = True
            continue
        use_client = client if (client is not None and fallback_origin == target_origin) else None
        headers = {
            "Authorization": f"Bearer {tok}",
            "Content-Type": "application/json",
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        }
        mid = str(item.get("machineId") or "").strip()

        if kind == "machine_revoke_owner":
            if not mid:
                done_ids.add(item_id)
                continue
            ok_rev, err_c, retry_after = _call_revoke(
                target_origin,
                "/api/machines",
                headers,
                {"action": "revoke", "id": mid},
                "machine_revoke_owner",
                use_client,
            )
            if ok_rev:
                done_ids.add(item_id)
            else:
                retry_updates[item_id] = (err_c or "HUB-501", retry_after, _now_utc())
                any_failed = True
        elif kind == "machine_self_revoke":
            ok_rev, err_c, retry_after = _call_revoke(
                target_origin,
                "/api/machines/self/revoke",
                headers,
                {},
                "machine_self_revoke",
                use_client,
            )
            if ok_rev:
                done_ids.add(item_id)
            else:
                retry_updates[item_id] = (err_c or "HUB-501", retry_after, _now_utc())
                any_failed = True
        elif kind == "owner" and mid:
            ok_m, err_m, ra_m = _call_revoke(
                target_origin,
                "/api/machines",
                headers,
                {"action": "revoke", "id": mid},
                "machine_revoke_owner",
                use_client,
            )
            if not ok_m:
                retry_updates[item_id] = (err_m or "HUB-501", ra_m, _now_utc())
                any_failed = True
                continue
            ok_o, err_o, ra_o = _call_revoke(
                target_origin,
                "/api/auth/logout",
                headers,
                {},
                "owner_logout",
                use_client,
            )
            if ok_o:
                done_ids.add(item_id)
            else:
                converted_to_owner_logout.add(item_id)
                retry_updates[item_id] = (err_o or "HUB-501", ra_o, _now_utc())
                any_failed = True
        else:
            ok_o, err_o, ra_o = _call_revoke(
                target_origin,
                "/api/auth/logout",
                headers,
                {},
                "owner_logout",
                use_client,
            )
            if ok_o:
                done_ids.add(item_id)
            else:
                retry_updates[item_id] = (err_o or "HUB-501", ra_o, _now_utc())
                any_failed = True

    def _reconcile(latest: list[dict], reconcile_at: datetime) -> list[dict]:
        updated: list[dict] = []
        for cur in latest:
            cid = str(cur["id"])
            if cid in done_ids:
                continue
            item_copy = dict(cur)
            if cid in converted_to_owner_logout:
                item_copy["kind"] = "owner_logout"
                item_copy.pop("machineId", None)
            if cid in retry_updates:
                err_code, retry_after, observed_at = retry_updates[cid]
                new_attempts = int(item_copy.get("attempts") or 0) + 1
                item_copy["attempts"] = new_attempts
                item_copy["lastError"] = err_code
                cooldown_base = reconcile_at if reconcile_at >= observed_at else observed_at
                item_copy["nextAttemptAt"] = _backoff_next_attempt(
                    cooldown_base, new_attempts, retry_after=retry_after
                )
            updated.append(item_copy)
        return updated

    global _REVOKE_RETENTION_WARNING
    done_keys = {_pending_dedupe_key(item) for item in batch if str(item["id"]) in done_ids}
    reconcile_moment = _now_utc()
    with _IN_MEMORY_REVOKES_LOCK:
        mem_filtered = [
            cur
            for cur in _IN_MEMORY_PENDING_REVOKES
            if str(cur.get("id") or "") not in done_ids and _pending_dedupe_key(cur) not in done_keys
        ]
        updated_mem = _reconcile(mem_filtered, reconcile_moment)
        _IN_MEMORY_PENDING_REVOKES[:] = updated_mem
        if not updated_mem:
            _REVOKE_RETENTION_WARNING = False
        mem_snapshot = [dict(cur) for cur in updated_mem]

    updated_vault: list[dict] = []
    if use_vault is not None:
        if lock_cm is not None:
            lock_cm.acquire()
        try:
            def _reconcile_vault(latest: list[dict], db_arg: Path | None) -> list[dict]:
                global _REVOKE_RETENTION_WARNING
                rec = _reconcile(latest, _now_utc())
                if mem_snapshot:
                    promoted = _merge_pending_revokes(rec, mem_snapshot)
                    if len(promoted) <= MAX_PENDING_REVOKES:
                        try:
                            _write_pending_revokes(use_vault, promoted, database_path=db_arg)
                            promoted_ids = {str(e.get("id") or "") for e in mem_snapshot}
                            promoted_keys = {_pending_dedupe_key(e) for e in mem_snapshot}
                            with _IN_MEMORY_REVOKES_LOCK:
                                _IN_MEMORY_PENDING_REVOKES[:] = [
                                    cur
                                    for cur in _IN_MEMORY_PENDING_REVOKES
                                    if str(cur.get("id") or "") not in promoted_ids
                                    and _pending_dedupe_key(cur) not in promoted_keys
                                ]
                                if not _IN_MEMORY_PENDING_REVOKES:
                                    _REVOKE_RETENTION_WARNING = False
                            return promoted
                        except Exception:
                            pass
                if rec != latest:
                    _write_pending_revokes(use_vault, rec, database_path=db_arg)
                return rec

            if db_file is not None:
                from spend_app.db import connect

                with connect(db_file) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    latest = _parse_pending_revokes(
                        read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                    )
                    updated_vault = _reconcile_vault(latest, db_file)
                    conn.commit()
            else:
                latest = _parse_pending_revokes(
                    read_vault_secret(None, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                )
                updated_vault = _reconcile_vault(latest, None)
        finally:
            if lock_cm is not None:
                lock_cm.release()

    return not any_failed and len(updated_vault) == 0 and retained_pending_revoke_count() == 0


CLEANUP_PENDING_META_KEY = "secrets.cleanup_pending.v1"


def mark_cleanup_pending_in_tx(
    connection,
    *,
    token_migrated: bool = False,
    owner_migrated: bool = False,
    token_invalidated: bool = False,
    owner_invalidated: bool = False,
    state: dict | None = None,
) -> None:
    """Update or clear the durable cleanup-pending marker inside an active transaction."""
    import json
    from spend_app.db import utc_now

    row = connection.execute(
        "SELECT value FROM app_meta WHERE key=?",
        (CLEANUP_PENDING_META_KEY,),
    ).fetchone()
    marker: dict[str, object] | None = None
    if row and row[0]:
        try:
            parsed = json.loads(row[0])
            marker = dict(parsed) if isinstance(parsed, dict) else {"token": True, "ownerSession": True}
        except ValueError:
            marker = {"token": True, "ownerSession": True}

    if marker is None and not token_migrated and not owner_migrated:
        return

    cur = dict(marker or {})
    if token_migrated:
        cur["token"] = True
    if owner_migrated:
        cur["ownerSession"] = True
    if token_invalidated:
        cur["token"] = False
    if owner_invalidated:
        cur["ownerSession"] = False

    spoke = (state or {}).get("spokeConfig") or {}
    has_legacy_json = bool(str(spoke.get("token") or "").strip()) or bool(
        str(spoke.get("ownerSession") or "").strip()
    )
    if cur.get("token") or cur.get("ownerSession"):
        cur["at"] = utc_now()
        connection.execute(
            "INSERT INTO app_meta(key, value) VALUES(?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (CLEANUP_PENDING_META_KEY, json.dumps(cur, separators=(",", ":"), sort_keys=True)),
        )
    elif row is not None and not has_legacy_json:
        connection.execute("DELETE FROM app_meta WHERE key=?", (CLEANUP_PENDING_META_KEY,))


def publish_spoke_credentials(
    database_path: Path | str,
    *,
    owner_session: str | None = None,
    machine_token: str | None = None,
    vault=None,
    prepare_state=None,
    apply_state=None,
) -> dict:
    """Atomically publish spoke credentials to the verified vault and update setup metadata.

    Serializes via per-DB lock and SQLite BEGIN IMMEDIATE so concurrent migration
    or rotation cannot overwrite newer credentials. Never falls back to SQLite
    plaintext storage; raises CodedError("APP-903") on vault failure.
    """
    from spend_app.db import connect, initialize
    from spend_app.error_codes import CodedError
    from spend_app.hub_origin import hub_origin
    from spend_app.setup_wizard import read_setup_state, write_setup_state

    db_file = Path(database_path)
    clean_owner = str(owner_session).strip() if owner_session is not None else None
    clean_machine = str(machine_token).strip() if machine_token is not None else None

    with db_secret_lock(db_file):
        initialize(db_file)
        try:
            use_vault = vault if vault is not None else get_vault(db_file)
        except Exception as exc:
            raise CodedError("APP-903", status_code=503) from exc
        if use_vault is None:
            raise CodedError("APP-903", status_code=503)

        prev_machine = None
        prev_owner = None
        prev_gen = 0
        legacy_machine = ""
        legacy_owner = ""
        wrote_machine = False
        wrote_owner = False
        written_machine_val = None
        written_owner_val = None
        try:
            with connect(db_file) as connection:
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("BEGIN IMMEDIATE")
                state = read_setup_state(connection)
                if prepare_state is not None:
                    prepare_state(connection, state)

                orig_spoke = dict(state.get("spokeConfig") or {})
                prev_gen = int(orig_spoke.get("credentialGeneration") or 0)
                legacy_machine = str(orig_spoke.get("token") or "").strip()
                legacy_owner = str(orig_spoke.get("ownerSession") or "").strip()

                prev_machine = read_vault_secret(db_file, MACHINE_TOKEN_VAULT_REF, vault=use_vault)
                prev_owner = read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=use_vault)

                try:
                    if clean_machine:
                        verified_vault_put(use_vault, clean_machine, MACHINE_TOKEN_VAULT_REF)
                        wrote_machine = True
                        written_machine_val = clean_machine
                    if clean_owner:
                        verified_vault_put(use_vault, clean_owner, OWNER_SESSION_VAULT_REF)
                        wrote_owner = True
                        written_owner_val = clean_owner
                except Exception as exc:
                    if wrote_owner:
                        _restore_vault_secret(use_vault, OWNER_SESSION_VAULT_REF, prev_owner)
                        wrote_owner = False
                    if wrote_machine:
                        _restore_vault_secret(use_vault, MACHINE_TOKEN_VAULT_REF, prev_machine)
                        wrote_machine = False
                    raise CodedError("APP-903", status_code=503) from exc

                # B3: migrate untouched legacy counterparts in the same transaction
                # before removing them from SQLite; preserve them if migration fails.
                migrated_legacy_machine = False
                preserve_legacy_machine = False
                if not clean_machine and legacy_machine:
                    if prev_machine:
                        migrated_legacy_machine = True
                    else:
                        try:
                            verified_vault_put(use_vault, legacy_machine, MACHINE_TOKEN_VAULT_REF)
                            wrote_machine = True
                            written_machine_val = legacy_machine
                            migrated_legacy_machine = True
                        except Exception:
                            preserve_legacy_machine = True

                migrated_legacy_owner = False
                preserve_legacy_owner = False
                if not clean_owner and legacy_owner:
                    if prev_owner:
                        migrated_legacy_owner = True
                    else:
                        try:
                            verified_vault_put(use_vault, legacy_owner, OWNER_SESSION_VAULT_REF)
                            wrote_owner = True
                            written_owner_val = legacy_owner
                            migrated_legacy_owner = True
                        except Exception:
                            preserve_legacy_owner = True

                spoke = dict(state.get("spokeConfig") or {})
                if clean_machine or migrated_legacy_machine:
                    spoke.pop("token", None)
                    if migrated_legacy_machine and not clean_machine:
                        spoke["rotationRequired"] = True
                if clean_owner or migrated_legacy_owner:
                    spoke.pop("ownerSession", None)
                    if migrated_legacy_owner and not clean_owner:
                        spoke["ownerSessionRotationRequired"] = True
                if clean_owner:
                    spoke.pop("signedOut", None)
                state["spokeConfig"] = spoke

                if apply_state is not None:
                    apply_state(connection, state)

                spoke = dict(state.get("spokeConfig") or {})
                if (clean_owner or clean_machine) and spoke.get("hubUrl"):
                    new_origin = hub_origin(spoke["hubUrl"])
                    old_origin = orig_spoke.get("credentialOrigin") or orig_spoke.get("hubUrl")
                    issued = {value for value in (clean_owner, clean_machine) if value}
                    if old_origin and hub_origin(old_origin) == new_origin:
                        issued.update(value for value in (
                            prev_owner if not clean_owner else None,
                            prev_machine if not clean_machine else None,
                        ) if value)
                    for value in issued:
                        ref = credential_origin_ref(value)
                        existing = read_vault_secret(db_file, ref, vault=use_vault)
                        if existing and existing != new_origin:
                            raise CodedError("AUTH-107", status_code=401)
                        if existing is None:
                            verified_vault_put(use_vault, new_origin, ref)
                    spoke["credentialOrigin"] = new_origin
                    spoke.pop("requiresHubSignIn", None)
                if clean_machine or migrated_legacy_machine:
                    spoke.pop("token", None)
                    if migrated_legacy_machine and not clean_machine:
                        spoke["rotationRequired"] = True
                elif preserve_legacy_machine:
                    spoke["token"] = legacy_machine

                if clean_owner or migrated_legacy_owner:
                    spoke.pop("ownerSession", None)
                    if migrated_legacy_owner and not clean_owner:
                        spoke["ownerSessionRotationRequired"] = True
                elif preserve_legacy_owner:
                    spoke["ownerSession"] = legacy_owner

                if clean_owner:
                    spoke.pop("signedOut", None)

                spoke["credentialGeneration"] = max(prev_gen, int(spoke.get("credentialGeneration") or 0)) + 1
                state["spokeConfig"] = spoke

                if migrated_legacy_machine or migrated_legacy_owner or (clean_machine and legacy_machine) or (clean_owner and legacy_owner):
                    mark_cleanup_pending_in_tx(
                        connection,
                        token_migrated=bool(legacy_machine),
                        owner_migrated=bool(legacy_owner),
                        state=state,
                    )

                saved = write_setup_state(connection, state)
                connection.commit()
                return saved
        except Exception:
            if wrote_owner or wrote_machine:
                _compensate_publish_failure(
                    db_file,
                    use_vault,
                    snap_gen=prev_gen,
                    snap_legacy_owner=legacy_owner,
                    snap_legacy_machine=legacy_machine,
                    prev_owner=prev_owner,
                    prev_machine=prev_machine,
                    wrote_owner=wrote_owner,
                    wrote_machine=wrote_machine,
                    written_owner_val=written_owner_val,
                    written_machine_val=written_machine_val,
                )
            raise


def _build_clear_revoke_tasks(
    queue_revokes_mode: str | None,
    owner_tokens: list[str],
    machine_tokens: list[str],
    machine_id: str | None,
    effective_hub_url: str | None,
) -> list[dict]:
    if not queue_revokes_mode:
        return []
    tasks: list[dict] = []
    clean_hub = str(effective_hub_url or "").strip()
    clean_mid = str(machine_id or "").strip()
    if queue_revokes_mode == "full_sign_out":
        if machine_tokens:
            for mtok in machine_tokens:
                entry: dict = {"kind": "machine_self_revoke", "token": mtok}
                if clean_mid:
                    entry["machineId"] = clean_mid
                if clean_hub:
                    entry["hubUrl"] = clean_hub
                tasks.append(entry)
        elif clean_mid and owner_tokens:
            entry = {
                "kind": "machine_revoke_owner",
                "token": owner_tokens[0],
                "machineId": clean_mid,
            }
            if clean_hub:
                entry["hubUrl"] = clean_hub
            tasks.append(entry)
    for tok in owner_tokens:
        entry = {"kind": "owner_logout", "token": tok}
        if clean_hub:
            entry["hubUrl"] = clean_hub
        tasks.append(entry)
    return tasks


def clear_spoke_credentials(
    database_path: Path | str | None,
    *,
    clear_owner_session: bool = False,
    clear_machine_token: bool = False,
    mark_signed_out: bool = False,
    vault=None,
    prepare_state=None,
    apply_state=None,
    queue_revokes_mode: str | None = None,
    hub_url: str | None = None,
    extra_owner_tokens: list[str] | None = None,
    extra_machine_tokens: list[str] | None = None,
    client=None,
    allow_flush_on_overflow: bool = True,
) -> dict:
    """Clear vault credentials and SQLite spoke state under the shared DB lock.

    When ``queue_revokes_mode`` (``"owner_only"`` or ``"full_sign_out"``) is set,
    computes exact revocation tasks inside the same ``BEGIN IMMEDIATE`` transaction,
    merges & writes them to the verified pending-revoke vault entry FIRST, and
    only deletes local credential refs / commits after durable persistence succeeds.
    If capacity (``MAX_PENDING_REVOKES``) is exceeded or the vault write fails,
    aborts with ``CodedError("APP-903", status_code=503)`` leaving local
    credentials intact.
    """
    from spend_app.error_codes import CodedError

    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None
    clean_extra_owner = [str(t).strip() for t in (extra_owner_tokens or ()) if str(t).strip()]
    clean_extra_machine = [str(t).strip() for t in (extra_machine_tokens or ()) if str(t).strip()]

    def _try_clear_once() -> tuple[bool, dict | None, str | None, object]:
        if lock_cm is not None:
            lock_cm.acquire()
        removed_owner: list[str] = []
        removed_machine: list[str] = []
        machine_id: str | None = None
        stored_hub_url: str | None = None
        new_gen: int | None = None
        queued_tasks: list[dict] = []
        try:
            use_vault = vault
            if use_vault is None and db_file is not None:
                try:
                    use_vault = get_vault(db_file)
                except Exception:
                    use_vault = None

            if db_file is not None and db_file.is_file():
                from spend_app.db import connect
                from spend_app.setup_wizard import read_setup_state, write_setup_state

                with connect(db_file) as connection:
                    connection.execute("PRAGMA secure_delete = ON")
                    connection.execute("BEGIN IMMEDIATE")
                    state = read_setup_state(connection)
                    if prepare_state is not None:
                        prepare_state(connection, state)

                    orig_spoke = dict(state.get("spokeConfig") or {})
                    orig_account = dict(state.get("account") or {})
                    machine_id = str(orig_account.get("machineId") or "").strip() or None
                    stored_hub_url = str(orig_spoke.get("hubUrl") or "").strip() or None
                    effective_hub = str(hub_url or stored_hub_url or "").strip() or None

                    if clear_owner_session:
                        v_owner = read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=use_vault)
                        s_owner = str(orig_spoke.get("ownerSession") or "").strip() or None
                        removed_owner = list(
                            dict.fromkeys(t for t in (v_owner, s_owner, *clean_extra_owner) if t)
                        )
                    if clear_machine_token:
                        v_machine = read_vault_secret(db_file, MACHINE_TOKEN_VAULT_REF, vault=use_vault)
                        s_machine = str(orig_spoke.get("token") or "").strip() or None
                        removed_machine = list(
                            dict.fromkeys(t for t in (v_machine, s_machine, *clean_extra_machine) if t)
                        )

                    tasks_to_queue = _build_clear_revoke_tasks(
                        queue_revokes_mode,
                        removed_owner,
                        removed_machine,
                        machine_id,
                        effective_hub,
                    )
                    if tasks_to_queue:
                        if use_vault is None:
                            connection.rollback()
                            raise CodedError("APP-903", status_code=503)
                        existing = _parse_pending_revokes(
                            read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                        )
                        merged, queued_tasks = _merge_pending_revokes_with_ids(existing, tasks_to_queue)
                        if len(merged) > MAX_PENDING_REVOKES:
                            connection.rollback()
                            return False, None, effective_hub, use_vault
                        _write_pending_revokes(use_vault, merged, database_path=db_file)

                    if use_vault is not None:
                        if clear_owner_session:
                            try:
                                use_vault.delete(OWNER_SESSION_VAULT_REF)
                            except Exception:
                                pass
                        if clear_machine_token:
                            try:
                                use_vault.delete(MACHINE_TOKEN_VAULT_REF)
                            except Exception:
                                pass

                    spoke = dict(state.get("spokeConfig") or {})
                    prev_gen = int(spoke.get("credentialGeneration") or 0)
                    if clear_owner_session:
                        spoke.pop("ownerSession", None)
                        spoke.pop("ownerSessionExpiresAt", None)
                        spoke.pop("ownerSessionRotationRequired", None)
                    if clear_machine_token:
                        spoke.pop("token", None)
                        spoke.pop("tokenExpiresAt", None)
                        spoke.pop("rotationRequired", None)
                    if mark_signed_out:
                        spoke["signedOut"] = True
                    state["spokeConfig"] = spoke
                    if apply_state is not None:
                        apply_state(connection, state)
                    spoke = dict(state.get("spokeConfig") or {})
                    if mark_signed_out:
                        spoke["signedOut"] = True
                    new_gen = max(prev_gen, int(spoke.get("credentialGeneration") or 0)) + 1
                    spoke["credentialGeneration"] = new_gen
                    state["spokeConfig"] = spoke
                    write_setup_state(connection, state)
                    connection.commit()
            else:
                effective_hub = str(hub_url or "").strip() or None
                if clear_owner_session:
                    v_owner = (
                        read_vault_secret(None, OWNER_SESSION_VAULT_REF, vault=use_vault)
                        if use_vault is not None
                        else None
                    )
                    removed_owner = list(
                        dict.fromkeys(t for t in (v_owner, *clean_extra_owner) if t)
                    )
                if clear_machine_token:
                    v_machine = (
                        read_vault_secret(None, MACHINE_TOKEN_VAULT_REF, vault=use_vault)
                        if use_vault is not None
                        else None
                    )
                    removed_machine = list(
                        dict.fromkeys(t for t in (v_machine, *clean_extra_machine) if t)
                    )
                tasks_to_queue = _build_clear_revoke_tasks(
                    queue_revokes_mode,
                    removed_owner,
                    removed_machine,
                    None,
                    effective_hub,
                )
                if tasks_to_queue:
                    if use_vault is None:
                        raise CodedError("APP-903", status_code=503)
                    existing = _parse_pending_revokes(
                        read_vault_secret(None, PENDING_REVOKE_VAULT_REF, vault=use_vault)
                    )
                    merged, queued_tasks = _merge_pending_revokes_with_ids(existing, tasks_to_queue)
                    if len(merged) > MAX_PENDING_REVOKES:
                        return False, None, effective_hub, use_vault
                    _write_pending_revokes(use_vault, merged, database_path=None)

                if use_vault is not None:
                    if clear_owner_session:
                        try:
                            use_vault.delete(OWNER_SESSION_VAULT_REF)
                        except Exception:
                            pass
                    if clear_machine_token:
                        try:
                            use_vault.delete(MACHINE_TOKEN_VAULT_REF)
                        except Exception:
                            pass

            return True, {
                "ownerTokens": removed_owner,
                "machineTokens": removed_machine,
                "machineId": machine_id,
                "hubUrl": stored_hub_url,
                "credentialGeneration": new_gen,
                "queuedTasks": queued_tasks,
            }, None, use_vault
        finally:
            if lock_cm is not None:
                lock_cm.release()

    ok, result, overflow_hub, used_vault = _try_clear_once()
    if ok and result is not None:
        return result
    if allow_flush_on_overflow and overflow_hub:
        try:
            flush_pending_revokes(
                db_file,
                overflow_hub,
                vault=used_vault,
                client=client,
                ignore_backoff=True,
            )
        except Exception:
            pass
        ok2, result2, _, _ = _try_clear_once()
        if ok2 and result2 is not None:
            return result2
    raise CodedError("APP-903", status_code=503)


def test_provider(source, key):
    end = datetime.now(UTC)
    start = end - timedelta(hours=1)
    headers = identity_headers()
    method = "GET"
    params = {}
    body = None
    if source == "openai_admin":
        url = "https://api.openai.com/v1/organization/usage/completions"
        params = {"start_time": int(start.timestamp()), "end_time": int(end.timestamp()), "limit": 1}
        headers["Authorization"] = "Bearer " + key
    elif source == "anthropic_admin":
        url = "https://api.anthropic.com/v1/organizations/usage_report/messages"
        params = {"starting_at": start.isoformat(), "ending_at": end.isoformat(), "limit": 1}
        headers.update({"x-api-key": key, "anthropic-version": "2023-06-01"})
    elif source == "openrouter":
        url = "https://openrouter.ai/api/v1/credits"
        headers["Authorization"] = "Bearer " + key
    elif source == "deepseek":
        url = "https://api.deepseek.com/user/balance"
        headers["Authorization"] = "Bearer " + key
    else:
        raise LocationError("Unsupported documented provider.")
    from urllib.parse import urlsplit
    lane = POLICY_LANES.get(source, source)
    assert_allowed_host(lane, urlsplit(url).hostname)
    try:
        with httpx.Client(timeout=10, trust_env=False, follow_redirects=False) as client:
            # At most two fixed read endpoints (usage and cost), no pagination,
            # inference, redirects or retries. Stop at the first failure.
            urls = [url]
            if source == "openai_admin":
                urls.append("https://api.openai.com/v1/organization/costs")
            elif source == "anthropic_admin":
                urls.append("https://api.anthropic.com/v1/organizations/cost_report")
            for endpoint in urls:
                assert_allowed_host(lane, urlsplit(endpoint).hostname)
                with client.stream(method, endpoint, headers=headers, params=params, json=body) as response:
                    status = response.status_code
                    if status in {401, 403}:
                        raise LocationError("Key rejected or missing required admin/read scope. Check the key type and permissions.")
                    if status == 429:
                        raise LocationError("Provider rate limited the test. Wait before trying again.")
                    if status != 200:
                        raise LocationError("Provider test unavailable. Try again later; the previous connection is unchanged.")
                    # Never consume or expose a potentially large response body.
    except httpx.HTTPError:
        raise LocationError("Provider could not be reached. Check connectivity and retry later.") from None
