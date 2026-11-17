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
        wrote_machine = False
        wrote_owner = False
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
                    if clean_owner:
                        verified_vault_put(use_vault, clean_owner, OWNER_SESSION_VAULT_REF)
                        wrote_owner = True
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
            if wrote_owner:
                _restore_vault_secret(use_vault, OWNER_SESSION_VAULT_REF, prev_owner)
            if wrote_machine:
                _restore_vault_secret(use_vault, MACHINE_TOKEN_VAULT_REF, prev_machine)
            raise


def clear_spoke_credentials(
    database_path: Path | str | None,
    *,
    clear_owner_session: bool = False,
    clear_machine_token: bool = False,
    vault=None,
    prepare_state=None,
    apply_state=None,
) -> None:
    """Clear vault credentials and SQLite spoke state under the shared DB lock."""
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

        if db_file is not None and db_file.is_file():
            from spend_app.db import connect
            from spend_app.setup_wizard import read_setup_state, write_setup_state

            with connect(db_file) as connection:
                connection.execute("PRAGMA secure_delete = ON")
                connection.execute("BEGIN IMMEDIATE")
                state = read_setup_state(connection)
                if prepare_state is not None:
                    prepare_state(connection, state)

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
                state["spokeConfig"] = spoke
                if apply_state is not None:
                    apply_state(connection, state)
                spoke = dict(state.get("spokeConfig") or {})
                spoke["credentialGeneration"] = max(prev_gen, int(spoke.get("credentialGeneration") or 0)) + 1
                state["spokeConfig"] = spoke
                write_setup_state(connection, state)
                connection.commit()
        elif use_vault is not None:
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
    finally:
        if lock_cm is not None:
            lock_cm.release()


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
