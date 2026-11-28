"""Durable tests for Phase 2 of the 0.6.12 -> 0.6.13 Usage & Account Data Audit:
D-03 (legacy connections.v1 key deletion on disconnect/rotation),
D-04 (cleanup of superseded burnrate-origin-* vault entries),
D-05 (keyed HMAC for OpenCode API-key digests & opaque candidateId).
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from uuid import uuid4

from spend_app import connections as c
from spend_app import db as spend_db
from spend_app import profile_detect
from spend_app import profile_store as ps
from spend_app.config import Settings, load_settings
from spend_app.connection_secrets import (
    MACHINE_TOKEN_VAULT_REF,
    OWNER_SESSION_VAULT_REF,
    clear_spoke_credentials,
    credential_origin_ref,
    publish_spoke_credentials,
)
from spend_app.setup_wizard import SETUP_META_KEY


class _MemoryVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.fail_next_delete: set[str] = set()

    def available(self) -> bool:
        return True

    def get(self, ref: str) -> str | None:
        return self.values.get(ref)

    def put(self, secret: str, ref: str | None = None) -> str:
        key = ref or f"burnrate-{uuid4().hex}"
        self.values[key] = secret
        return key

    def delete(self, ref: str) -> None:
        if ref in self.fail_next_delete:
            self.fail_next_delete.remove(ref)
            raise RuntimeError("transient vault error")
        self.values.pop(ref, None)


def _v1_payload(bindings: dict) -> dict:
    return {"version": 1, "legacy": [], "bindings": bindings, "requests": {}, "cleanup": []}


def test_d03_disconnect_and_replace_delete_legacy_v1_provider_key_from_vault(tmp_path, monkeypatch):
    """Explicit disconnect or key replacement removes legacy connections.v1 key from Vault."""
    db_path = tmp_path / "spend.db"
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("BURNRATE_DB_PATH", str(db_path))
    spend_db.initialize(db_path)
    vault = _MemoryVault()
    vault.put("sk-legacy-secret", "burnrate-legacy-openai")
    vault.put("sk-untouched-secret", "burnrate-legacy-anthropic")

    v1 = _v1_payload(
        bindings={
            "openai_admin": {
                "source": "openai_admin",
                "enabled": True,
                "revision": 1,
                "location": "",
                "mode": "api",
                "credentialRef": "burnrate-legacy-openai",
            },
            "anthropic_admin": {
                "source": "anthropic_admin",
                "enabled": True,
                "revision": 1,
                "location": "",
                "mode": "api",
                "credentialRef": "burnrate-legacy-anthropic",
            },
        }
    )
    with spend_db.connect(db_path) as conn:
        conn.execute("DELETE FROM app_meta WHERE key IN (?, ?)", (ps.KEY, ps.MIGRATED_KEY))
        conn.execute("INSERT INTO app_meta(key, value) VALUES(?, ?)", (ps.V1_KEY, json.dumps(v1)))

    c.Store(db_path).bootstrap()
    settings = load_settings()
    service = c.Service(settings, vault=vault)

    # 1. Transient vault delete failure keeps ref queued until cleanup succeeds
    vault.fail_next_delete.add("burnrate-legacy-openai")
    service.mutate(
        {
            "source": "openai_admin",
            "operation": "disable",
            "revision": 1,
            "requestId": str(uuid4()),
        }
    )
    assert "burnrate-legacy-openai" in vault.values
    assert "burnrate-legacy-openai" in service.store.read()["cleanup"]

    # 2. Retry cleanup -> deletes the legacy v1 key from Vault and clears queue
    service.cleanup()
    assert "burnrate-legacy-openai" not in vault.values
    assert service.store.read()["cleanup"] == []

    # 3. connections.v1 cleared openai_admin ref, while untouched anthropic_admin stays intact
    with spend_db.connect(db_path) as conn:
        stored_v1 = json.loads(conn.execute("SELECT value FROM app_meta WHERE key=?", (ps.V1_KEY,)).fetchone()[0])
    assert stored_v1["bindings"]["openai_admin"]["credentialRef"] is None
    assert stored_v1["bindings"]["anthropic_admin"]["credentialRef"] == "burnrate-legacy-anthropic"
    assert vault.get("burnrate-legacy-anthropic") == "sk-untouched-secret"


def test_d04_origin_refs_deleted_on_rotation_clear_and_hub_url_migration(tmp_path, monkeypatch):
    """Rotating, clearing, or switching hub URLs deletes stale burnrate-origin-* entries."""
    db_path = tmp_path / "spend.db"
    spend_db.initialize(db_path)
    vault = _MemoryVault()
    origin = "https://hub.example.test"

    # Initial publish (login + pair)
    publish_spoke_credentials(
        db_path,
        owner_session="br_s_owner_v1",
        machine_token="br_m_machine_v1",
        authenticated_origin=origin,
        vault=vault,
    )
    assert credential_origin_ref("br_s_owner_v1") in vault.values
    assert credential_origin_ref("br_m_machine_v1") in vault.values

    # Rotate owner session -> old owner origin ref deleted, machine origin ref kept
    publish_spoke_credentials(
        db_path,
        owner_session="br_s_owner_v2",
        authenticated_origin=origin,
        vault=vault,
    )
    assert credential_origin_ref("br_s_owner_v1") not in vault.values
    assert credential_origin_ref("br_s_owner_v2") in vault.values
    assert credential_origin_ref("br_m_machine_v1") in vault.values

    # Rotate machine token -> old machine origin ref deleted
    publish_spoke_credentials(
        db_path,
        machine_token="br_m_machine_v2",
        authenticated_origin=origin,
        vault=vault,
    )
    assert credential_origin_ref("br_m_machine_v1") not in vault.values
    assert credential_origin_ref("br_m_machine_v2") in vault.values
    assert credential_origin_ref("br_s_owner_v2") in vault.values

    # Clear spoke credentials -> zero burnrate-origin-* entries remain
    clear_spoke_credentials(
        db_path,
        clear_owner_session=True,
        clear_machine_token=True,
        queue_revokes_mode="none",
        vault=vault,
    )
    assert OWNER_SESSION_VAULT_REF not in vault.values
    assert MACHINE_TOKEN_VAULT_REF not in vault.values
    origin_leftovers = [k for k in vault.values if k.startswith("burnrate-origin-")]
    assert origin_leftovers == []

    # Hub URL migration also clears origin refs
    publish_spoke_credentials(
        db_path,
        owner_session="br_s_owner_v3",
        machine_token="br_m_machine_v3",
        authenticated_origin=origin,
        vault=vault,
    )
    with spend_db.connect(db_path) as conn:
        state = json.loads(conn.execute("SELECT value FROM app_meta WHERE key=?", (SETUP_META_KEY,)).fetchone()[0])
        state["spokeConfig"]["hubUrl"] = origin
        conn.execute("UPDATE app_meta SET value=? WHERE key=?", (json.dumps(state), SETUP_META_KEY))
    monkeypatch.setenv("BURNRATE_HUB_URL", "https://new-hub.example.test")
    monkeypatch.setattr("spend_app.connection_secrets.get_vault", lambda _p: vault)
    assert spend_db.migrate_setup_hub_url(db_path) is True
    assert [k for k in vault.values if k.startswith("burnrate-origin-")] == []


def test_d05_opencode_api_key_never_stored_or_exposed_as_unsalted_sha256(tmp_path, monkeypatch):
    """Raw OpenCode API keys and their unsalted SHA-256 never appear in SQLite or GET /api/connections."""
    home = tmp_path / "home"
    xdg = tmp_path / "xdg"
    db_path = tmp_path / "data" / "spend.db"
    db_path.parent.mkdir(parents=True)
    spend_db.initialize(db_path)

    secret_default = "sk-opencode-default-secret-998877"
    secret_extra = "sk-opencode-extra-secret-112233"
    raw_sha_default = hashlib.sha256(secret_default.encode("utf-8")).hexdigest()
    raw_sha_extra = hashlib.sha256(secret_extra.encode("utf-8")).hexdigest()

    def _mk_oc_db(target: Path, api_key: str) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(target) as conn:
            conn.execute(
                "CREATE TABLE credential (id TEXT, integration_id TEXT, label TEXT, value TEXT, "
                "connector_id TEXT, method_id TEXT, active INTEGER, time_created INTEGER, time_updated INTEGER)"
            )
            conn.execute(
                "INSERT INTO credential(id, integration_id, value, active, time_created) VALUES(?,?,?,?,?)",
                ("cred-1", "opencode", json.dumps({"type": "key", "key": api_key}), 1, 1),
            )
            conn.commit()

    _mk_oc_db(xdg / "opencode" / "opencode.db", secret_default)
    extra_dir = home / ".opencode-work"
    _mk_oc_db(extra_dir / "opencode" / "opencode.db", secret_extra)

    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg))
    monkeypatch.setenv("SPEND_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("BURNRATE_DB_PATH", str(db_path))
    monkeypatch.delenv("OPENCODE_DB", raising=False)
    monkeypatch.delenv("OPENCODE_API_KEY", raising=False)
    profile_detect.reset_cache()

    candidates = profile_detect.detect("opencode_local", home=home, environ={"XDG_DATA_HOME": str(xdg)})
    assert len(candidates) == 2
    for cand in candidates:
        assert cand.account_digest not in {raw_sha_default, raw_sha_extra}
        assert cand.candidate_id != cand.account_digest
        assert cand.candidate_id not in {raw_sha_default, raw_sha_extra}

    # Verify duplicate detection still works deterministically within the same install
    assert profile_detect.opencode_key_digest(secret_extra) == candidates[1].account_digest
    assert candidates[1].account_digest != raw_sha_extra

    # Verify GET /api/connections (service.list) + dismiss never leak key or unsalted SHA-256
    settings = load_settings()
    service = c.Service(settings, vault=_MemoryVault())
    listed = service.list()
    listed_json = json.dumps(listed)
    for forbidden in (secret_default, secret_extra, raw_sha_default, raw_sha_extra, candidates[1].account_digest):
        assert forbidden not in listed_json

    oc_row = next(r for r in listed["connections"] if r["source"] == "opencode_local")
    assert len(oc_row["detected"]) == 1
    cid = oc_row["detected"][0]["candidateId"]
    service.mutate(
        {
            "source": "opencode_local",
            "operation": "dismiss_candidate",
            "candidateId": cid,
            "requestId": str(uuid4()),
        }
    )

    db_bytes = db_path.read_bytes()
    for forbidden in (secret_default, secret_extra, raw_sha_default, raw_sha_extra):
        assert forbidden.encode("utf-8") not in db_bytes
