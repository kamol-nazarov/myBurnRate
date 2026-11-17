"""BURNRATE-owned credential-store references and explicit, bounded API tests."""
import hashlib
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
    out: list[dict] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        tok = str(item.get("token") or "").strip()
        if not tok:
            continue
        entry: dict[str, str] = {
            "kind": str(item.get("kind") or "owner").strip() or "owner",
            "token": tok,
        }
        mid = str(item.get("machineId") or "").strip()
        if mid:
            entry["machineId"] = mid
        hub_url = str(item.get("hubUrl") or "").strip()
        if hub_url:
            entry["hubUrl"] = hub_url
        out.append(entry)
    return out


def _merge_pending_revokes(existing: list[dict], new_entries: list[dict]) -> list[dict]:
    merged: dict[tuple[str, str], dict] = {}
    order: list[tuple[str, str]] = []
    for item in [*existing, *new_entries]:
        if not isinstance(item, dict):
            continue
        tok = str(item.get("token") or "").strip()
        if not tok:
            continue
        hub_url = str(item.get("hubUrl") or "").strip()
        mid = str(item.get("machineId") or "").strip()
        key = (tok, hub_url)
        if key not in merged:
            entry: dict[str, str] = {
                "kind": str(item.get("kind") or "owner").strip() or "owner",
                "token": tok,
            }
            if mid:
                entry["machineId"] = mid
            if hub_url:
                entry["hubUrl"] = hub_url
            merged[key] = entry
            order.append(key)
        elif mid and not merged[key].get("machineId"):
            merged[key]["machineId"] = mid
    return [merged[k] for k in order]


def _write_pending_revokes(use_vault, entries: list[dict]) -> None:
    import json

    if use_vault is None:
        return
    if entries:
        try:
            verified_vault_put(
                use_vault,
                json.dumps(entries, separators=(",", ":"), sort_keys=True),
                PENDING_REVOKE_VAULT_REF,
            )
        except Exception:
            pass
    else:
        try:
            use_vault.delete(PENDING_REVOKE_VAULT_REF)
        except Exception:
            pass


def read_pending_revokes(database_path: Path | str | None, *, vault=None) -> list[dict]:
    """Read pending revoke entries from the vault only (never SQLite)."""
    raw = read_vault_secret(database_path, PENDING_REVOKE_VAULT_REF, vault=vault)
    return _parse_pending_revokes(raw)


def enqueue_pending_revokes(
    database_path: Path | str | None,
    entries: list[dict],
    *,
    vault=None,
) -> None:
    """Persist unrevoked tokens in vault-only ``burnrate-pending-revoke``."""
    clean_entries = [e for e in (entries or []) if isinstance(e, dict) and str(e.get("token") or "").strip()]
    if not clean_entries:
        return
    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None
    if lock_cm is not None:
        lock_cm.acquire()
    try:
        use_vault = vault
        if use_vault is None and db_file is not None:
            try:
                use_vault = get_vault(db_file)
            except Exception:
                use_vault = None
        if use_vault is None:
            return
        existing = _parse_pending_revokes(
            read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
        )
        merged = _merge_pending_revokes(existing, clean_entries)
        _write_pending_revokes(use_vault, merged)
    finally:
        if lock_cm is not None:
            lock_cm.release()


def flush_pending_revokes(
    database_path: Path | str | None,
    hub_url: str | None = None,
    *,
    vault=None,
    client=None,
) -> bool:
    """Retry any vault-stored pending revokes against the hub.

    Returns True when no pending revokes remain (or all retried cleanly),
    False if any revoke failed due to network/hub error.
    """
    from spend_app.error_codes import HUB_PROTOCOL_VERSION
    from spend_app.hub_origin import hub_origin
    from spend_app.sync import _hub_post_with_cookie_isolation

    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None
    if lock_cm is not None:
        lock_cm.acquire()
    try:
        use_vault = vault
        if use_vault is None and db_file is not None:
            try:
                use_vault = get_vault(db_file)
            except Exception:
                use_vault = None
        if use_vault is None:
            return True
        pending = _parse_pending_revokes(
            read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
        )
    finally:
        if lock_cm is not None:
            lock_cm.release()

    if not pending:
        return True

    fallback_origin = None
    if hub_url:
        try:
            fallback_origin = hub_origin(hub_url)
        except ValueError:
            fallback_origin = None

    done_keys: set[tuple[str, str]] = set()
    cleared_machine_keys: set[tuple[str, str]] = set()
    any_failed = False

    for item in pending:
        tok = str(item.get("token") or "").strip()
        raw_item_hub = str(item.get("hubUrl") or "").strip()
        key = (tok, raw_item_hub)
        target_raw = raw_item_hub or fallback_origin or ""
        if not target_raw:
            any_failed = True
            continue
        try:
            target_origin = hub_origin(target_raw)
        except ValueError:
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
        if mid:
            try:
                rev_res = _hub_post_with_cookie_isolation(
                    target_origin,
                    "/api/machines",
                    headers,
                    json_body={"action": "revoke", "id": mid},
                    client=use_client,
                    timeout=6.0,
                )
            except Exception:
                any_failed = True
                continue
            if rev_res.status_code >= 500:
                any_failed = True
                continue
            cleared_machine_keys.add(key)
        try:
            res = _hub_post_with_cookie_isolation(
                target_origin,
                "/api/auth/logout",
                headers,
                client=use_client,
                timeout=6.0,
            )
        except Exception:
            any_failed = True
            continue
        if res.status_code >= 500:
            any_failed = True
            continue
        done_keys.add(key)

    if lock_cm is not None:
        lock_cm.acquire()
    try:
        latest = _parse_pending_revokes(
            read_vault_secret(db_file, PENDING_REVOKE_VAULT_REF, vault=use_vault)
        )
        updated: list[dict] = []
        for item in latest:
            k = (str(item.get("token") or "").strip(), str(item.get("hubUrl") or "").strip())
            if k in done_keys:
                continue
            if k in cleared_machine_keys and "machineId" in item:
                item = {ik: iv for ik, iv in item.items() if ik != "machineId"}
            updated.append(item)
        _write_pending_revokes(use_vault, updated)
    finally:
        if lock_cm is not None:
            lock_cm.release()

    return not any_failed


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


def clear_spoke_credentials(
    database_path: Path | str | None,
    *,
    clear_owner_session: bool = False,
    clear_machine_token: bool = False,
    mark_signed_out: bool = False,
    vault=None,
    prepare_state=None,
    apply_state=None,
) -> dict:
    """Clear vault credentials and SQLite spoke state under the shared DB lock.

    Reads all stored owner/machine credentials (vault + SQLite state) inside the
    same BEGIN IMMEDIATE transaction, deletes them from the vault, bumps
    credentialGeneration, optionally sets ``signedOut``, and returns the exact
    removed tokens and metadata so sign-out can revoke whatever won.
    """
    db_file = Path(database_path) if database_path is not None else None
    lock_cm = db_secret_lock(db_file) if db_file is not None else None
    if lock_cm is not None:
        lock_cm.acquire()
    removed_owner: list[str] = []
    removed_machine: list[str] = []
    machine_id: str | None = None
    hub_url: str | None = None
    new_gen: int | None = None
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
                hub_url = str(orig_spoke.get("hubUrl") or "").strip() or None

                if clear_owner_session:
                    v_owner = read_vault_secret(db_file, OWNER_SESSION_VAULT_REF, vault=use_vault)
                    s_owner = str(orig_spoke.get("ownerSession") or "").strip() or None
                    removed_owner = list(dict.fromkeys(t for t in (v_owner, s_owner) if t))
                if clear_machine_token:
                    v_machine = read_vault_secret(db_file, MACHINE_TOKEN_VAULT_REF, vault=use_vault)
                    s_machine = str(orig_spoke.get("token") or "").strip() or None
                    removed_machine = list(dict.fromkeys(t for t in (v_machine, s_machine) if t))

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
        elif use_vault is not None:
            if clear_owner_session:
                v_owner = read_vault_secret(None, OWNER_SESSION_VAULT_REF, vault=use_vault)
                if v_owner:
                    removed_owner = [v_owner]
                try:
                    use_vault.delete(OWNER_SESSION_VAULT_REF)
                except Exception:
                    pass
            if clear_machine_token:
                v_machine = read_vault_secret(None, MACHINE_TOKEN_VAULT_REF, vault=use_vault)
                if v_machine:
                    removed_machine = [v_machine]
                try:
                    use_vault.delete(MACHINE_TOKEN_VAULT_REF)
                except Exception:
                    pass
    finally:
        if lock_cm is not None:
            lock_cm.release()
    return {
        "ownerTokens": removed_owner,
        "machineTokens": removed_machine,
        "machineId": machine_id,
        "hubUrl": hub_url,
        "credentialGeneration": new_gen,
    }


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
