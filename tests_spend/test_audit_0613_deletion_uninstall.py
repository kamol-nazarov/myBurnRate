"""Durable tests for Phase 3 of the 0.6.12 -> 0.6.13 Usage & Account Data Audit:
D-06 (secure_delete + VACUUM, opt-in backup, pricing_gaps/owner_sessions/tombstone cleanup),
D-02 (wipe_all_credentials, burnrate wipe CLI, NSIS & PowerShell uninstaller cleanup),
D-15 (burnrate hub delete-owner).
"""
from __future__ import annotations

import json
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import uuid4

from spend_app import cli
from spend_app.auth_model import Principal
from spend_app.connection_secrets import (
    MACHINE_TOKEN_VAULT_REF,
    OWNER_SESSION_VAULT_REF,
    PENDING_REVOKE_VAULT_REF,
    credential_origin_ref,
    publish_spoke_credentials,
    wipe_all_credentials,
)
from spend_app.db import UsageEvent, connect, initialize, record_pricing_gap, upsert_agent_run, upsert_usage_event
from spend_app.hub_update import create_hub_owner, delete_hub_owner
from spend_app.machine_data import delete_machine_data
from spend_app.sync import register_machine, revoke_machine

ROOT = Path(__file__).resolve().parents[1]


class _SpyVault:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}

    def available(self) -> bool:
        return True

    def get(self, ref: str) -> str | None:
        return self.values.get(ref)

    def put(self, secret: str, ref: str | None = None) -> str:
        key = ref or f"burnrate-{uuid4().hex}"
        self.values[key] = secret
        return key

    def delete(self, ref: str) -> None:
        self.values.pop(ref, None)


def _sample_row(raw_id: str, project: str, now_iso: str) -> UsageEvent:
    return UsageEvent(
        source="claude_local",
        tool_key="claude-code",
        model_key="anthropic:claude-sonnet-4-6",
        occurred_at=now_iso,
        session_id=raw_id,
        project=project,
        input_tokens=100,
        cached_input_tokens=0,
        cache_write_tokens=0,
        cache_write_1h_tokens=0,
        output_tokens=50,
        reasoning_tokens=None,
        cost_usd=None,
        computed_cost_usd=0.01,
        raw_id=raw_id,
        ingested_at=now_iso,
    )


def test_d06_delete_machine_data_purges_freelist_bytes_and_missed_tables_without_default_backup(tmp_path):
    """Default delete creates no DB copy, scrubs missed tables/tombstone, and wipes SQLite freelist."""
    db_path = tmp_path / "spend.db"
    initialize(db_path)
    m1 = register_machine(db_path, name="SensitiveLaptop", os_name="Windows 11")["machine"]["id"]
    m2 = register_machine(db_path, name="KeepDesktop", os_name="macOS")["machine"]["id"]

    now_iso = datetime.now(UTC).isoformat().replace("+00:00", "Z")
    future_iso = (datetime.now(UTC) + timedelta(days=30)).isoformat().replace("+00:00", "Z")
    sentinel_project = "SENTINEL_SECRET_PROJ_994411"
    sentinel_title = "SENTINEL_RUN_TITLE_773322"

    with connect(db_path) as conn:
        upsert_usage_event(conn, _sample_row(f"m:{m1}:ev-1", sentinel_project, now_iso))
        upsert_usage_event(conn, _sample_row(f"m:{m2}:ev-1", "KeepProject", now_iso))
        upsert_agent_run(
            conn,
            id=f"m:{m1}:run-1",
            name=sentinel_title,
            model_key="claude-code",
            state="live",
            started_at=now_iso,
            last_seen_at=now_iso,
        )
        record_pricing_gap(
            conn,
            model_key="unknown:machine1-only",
            source=f"sync:{m1}",
            occurred_at=now_iso,
            raw_id=f"m:{m1}:gap-1",
        )
        record_pricing_gap(
            conn,
            model_key="unknown:shared-model",
            source=f"sync:{m2}",
            occurred_at=now_iso,
            raw_id=f"m:{m2}:gap-2",
        )
        record_pricing_gap(
            conn,
            model_key="unknown:shared-model",
            source=f"sync:{m1}",
            occurred_at=now_iso,
            raw_id=f"m:{m1}:gap-3",
        )
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, machine_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("sess-m1", "owner-1", "owner", "hash-m1", "SensitiveLaptop", now_iso, now_iso, now_iso, future_iso, m1),
        )
        conn.execute(
            "INSERT INTO owner_sessions(id, owner_id, role, token_hash, device_label, created_at, auth_time, last_used_at, expires_at, machine_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("sess-m2", "owner-1", "owner", "hash-m2", "KeepDesktop", now_iso, now_iso, now_iso, future_iso, m2),
        )

    revoke_machine(db_path, machine_id=m1)
    result = delete_machine_data(
        db_path,
        m1,
        actor=Principal("owner", "owner-1", "owner"),
        source="203.0.113.55",
    )

    # Default behavior: no unencrypted backup created
    assert result["ok"] is True
    assert result["backup"] is None
    assert not (tmp_path / "backups").exists()

    with connect(db_path) as conn:
        assert [r[0] for r in conn.execute("SELECT raw_id FROM usage_events")] == [f"m:{m2}:ev-1"]
        assert [r[0] for r in conn.execute("SELECT id FROM owner_sessions")] == ["sess-m2"]
        gaps = {r[0]: (r[1], r[2]) for r in conn.execute("SELECT model_key, occurrences, sample_raw_id FROM pricing_gaps")}
        assert "unknown:machine1-only" not in gaps
        assert gaps["unknown:shared-model"] == (1, f"m:{m2}:gap-2")
        machines = json.loads(conn.execute("SELECT value FROM app_meta WHERE key='machines.v1'").fetchone()[0])["machines"]
        assert machines[m1]["revoked"] is True
        assert machines[m1]["reported_name"] is None
        assert machines[m1]["os"] is None
        assert machines[m1]["name"] == "Deleted computer"
        audit_row = conn.execute(
            "SELECT action, target, source FROM admin_audit WHERE action='machine.data.delete' AND target=?",
            (m1,),
        ).fetchone()
        assert tuple(audit_row) == ("machine.data.delete", m1, "203.0.113.55")

    # Raw SQLite file bytes must NOT contain deleted project/title/hostname strings
    raw_bytes = db_path.read_bytes()
    assert sentinel_project.encode("utf-8") not in raw_bytes
    assert sentinel_title.encode("utf-8") not in raw_bytes
    assert b"SensitiveLaptop" not in raw_bytes


def test_d02_wipe_all_credentials_and_cli_wipe_command(tmp_path, monkeypatch, capsys):
    """wipe_all_credentials clears all BURNRATE vault entries; burnrate wipe --data removes auth/temp dirs."""
    data_dir = tmp_path / "BURNRATE"
    data_dir.mkdir()
    db_path = data_dir / "spend.db"
    initialize(db_path)
    vault = _SpyVault()
    publish_spoke_credentials(
        db_path,
        owner_session="br_s_wipe_owner",
        machine_token="br_m_wipe_machine",
        authenticated_origin="https://hub.example.test",
        vault=vault,
    )
    vault.put("[]", PENDING_REVOKE_VAULT_REF)
    vault.put("sk-provider-secret", "burnrate-provider-1")
    with connect(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO app_meta(key, value) VALUES('connections.v2', ?)",
            (json.dumps({"version": 2, "profiles": {"openai_admin": {"default": {"credentialRef": "burnrate-provider-1"}}}, "cleanup": []}),),
        )

    assert OWNER_SESSION_VAULT_REF in vault.values
    assert credential_origin_ref("br_s_wipe_owner") in vault.values
    assert "burnrate-provider-1" in vault.values

    res = wipe_all_credentials(db_path, vault=vault)
    assert res["ok"] is True
    assert res["removed"] >= 5
    assert vault.values == {}

    # Now test CLI `burnrate wipe --credentials --data --yes`
    (data_dir / ".burnrate-auth").mkdir()
    (data_dir / ".burnrate-auth" / "access.key").write_text("a" * 64, encoding="utf-8")
    (data_dir / "logs").mkdir()
    (data_dir / "logs" / "dashboard.log").write_text("hi", encoding="utf-8")
    fake_temp = tmp_path / "temp"
    wv2 = fake_temp / "burnrate-wv2-sessions" / "pid-123"
    wv2.mkdir(parents=True)
    (wv2 / "state.dat").write_bytes(b"wv2")

    import tempfile

    monkeypatch.setenv("SPEND_DATABASE_PATH", str(db_path))
    monkeypatch.setenv("TEMP", str(fake_temp))
    monkeypatch.setenv("TMP", str(fake_temp))
    monkeypatch.setattr(tempfile, "tempdir", str(fake_temp))
    monkeypatch.setattr("spend_app.connection_secrets.get_vault", lambda _p: vault)
    monkeypatch.setattr(sys, "argv", ["burnrate", "wipe", "--credentials", "--data", "--yes"])
    assert cli.main() == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert not db_path.exists()
    assert not (data_dir / ".burnrate-auth").exists()
    assert not (fake_temp / "burnrate-wv2-sessions").exists()


def test_d02_nsis_and_powershell_uninstallers_purge_auth_temp_and_credentials():
    """Both Windows uninstallers must clean .burnrate-auth, burnrate-wv2-sessions, and OS credentials."""
    nsi = (ROOT / "packaging" / "burnrate-native.nsi").read_text(encoding="utf-8")
    uninstall = nsi.split('Section "Uninstall"', 1)[1]
    gate = uninstall.index("${If} $DeleteData == ${BST_CHECKED}")
    for expected in (
        'wipe --credentials --yes',
        'RMDir /r "$LOCALAPPDATA\\BURNRATE\\.burnrate-auth"',
        'RMDir /r "$LOCALAPPDATA\\BURNRATE\\native-python"',
        'RMDir /r "$TEMP\\burnrate-wv2-sessions"',
    ):
        assert expected in uninstall, expected
        assert uninstall.index(expected) > gate
    # Credential wipe runs BEFORE BURNRATE.exe is deleted
    assert uninstall.index('wipe --credentials --yes') < uninstall.index('Delete "$INSTDIR\\${EXE_NAME}"')

    ps = (ROOT / "scripts" / "BurnrateRuntime.ps1").read_text(encoding="utf-8")
    fn = ps.split("function Uninstall-BurnrateDashboard", 1)[1]
    assert "wipe --credentials --yes" in fn
    assert "burnrate-wv2-sessions" in fn


def test_d15_delete_hub_owner_removes_account_and_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("BURNRATE_CENTRAL_SERVER", "1")
    db_path = tmp_path / "hub.db"
    initialize(db_path)
    assert create_hub_owner(
        db_path,
        "owner@example.test",
        prompt_fn=lambda _p: "StrongPassword!1234",
        echo=lambda _m: None,
    ) == 0

    # Refuses without confirm=True
    assert delete_hub_owner(db_path, "owner@example.test", confirm=False, echo=lambda _m: None) == 2
    assert delete_hub_owner(db_path, "owner@example.test", confirm=True, echo=lambda _m: None) == 0

    with connect(db_path) as conn:
        accounts = json.loads(conn.execute("SELECT value FROM app_meta WHERE key='accounts.v1'").fetchone()[0])
        assert accounts["users"] == {}
        assert conn.execute("SELECT COUNT(*) FROM owner_sessions").fetchone()[0] == 0
        assert conn.execute("SELECT action FROM admin_audit WHERE action='account.delete'").fetchone()
    assert b"owner@example.test" not in db_path.read_bytes()
