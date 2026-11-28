"""Regression tests for cloud bug fixes, performance optimizations, and security hardening."""
from __future__ import annotations

import sqlite3
import time
from collections import OrderedDict
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from spend_app import aggregate as aggregate_mod
from spend_app import plans_value_store as pv_mod
from spend_app import sync as sync_mod
from spend_app.auth_throttle import Throttle
from spend_app.cloud import backup as backup_mod
from spend_app.cloud.app import create_cloud_app
from spend_app.cloud.auth_store import CloudAuthError, CloudAuthStore, MAX_PASSWORD_LENGTH
from spend_app.cloud.email_sender import (
    EmailDeliveryError,
    MemoryEmailSender,
    OutboundEmail,
    ResendEmailSender,
)
from spend_app.cloud.tenant import TenantManager
from spend_app.config import ROOT, Settings
from spend_app.db import connect
from spend_app.pricing import PricingEngine


def _cloud_settings(tmp_path: Path) -> Settings:
    return Settings(
        database_path=tmp_path / "unused-local.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )


def test_password_length_cap_rejects_oversized_passwords_fast(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db")
    huge = "x" * 50_000
    over_by_one = "a" * (MAX_PASSWORD_LENGTH + 1)

    t0 = time.perf_counter()
    with pytest.raises(CloudAuthError) as exc_reg:
        store.start_registration("cap@example.com", huge)
    elapsed = time.perf_counter() - t0
    assert exc_reg.value.code == "AUTH-105"
    assert elapsed < 0.25, f"Oversized password should fail before PBKDF2, took {elapsed:.3f}s"

    # Provision a real account so we can exercise every other password-accepting entrypoint.
    _, code = store.start_registration("cap@example.com", "strong-valid-password-1")
    assert code is not None
    reg = store.verify_registration("cap@example.com", code, computer_name="Mac")
    principal = store.authenticate_session(reg["sessionToken"])
    assert principal is not None

    with pytest.raises(CloudAuthError) as exc_login:
        store.login("cap@example.com", over_by_one, computer_name="Mac")
    assert exc_login.value.code == "AUTH-101"

    _, reset_code = store.request_password_reset("cap@example.com")
    assert reset_code is not None
    with pytest.raises(CloudAuthError) as exc_reset:
        store.confirm_password_reset("cap@example.com", reset_code, over_by_one)
    assert exc_reset.value.code == "AUTH-105"

    with pytest.raises(CloudAuthError) as exc_chg:
        store.change_password(principal, "strong-valid-password-1", over_by_one)
    assert exc_chg.value.code == "AUTH-105"

    with pytest.raises(CloudAuthError) as exc_email:
        store.request_email_change(principal, over_by_one, "new@example.com")
    assert exc_email.value.code == "AUTH-101"

    with pytest.raises(CloudAuthError) as exc_del:
        store.delete_account(principal, over_by_one)
    assert exc_del.value.code == "AUTH-101"


def test_throttle_bounds_memory_under_high_cardinality_keys() -> None:
    throttle = Throttle(window=60, max_failures=5, lockout=60, max_keys=64)
    t = 1000.0
    for i in range(300):
        throttle.fail(f"ip:{i}", now=t + i * 0.01)
        assert len(throttle._failures) + len(throttle._locked_until) <= 64

    # A single repeated attacker still hits lockout reliably.
    locked_wait = 0
    for j in range(5):
        locked_wait = throttle.fail("attacker:1", now=t + 10.0 + j)
    assert locked_wait > 0
    assert throttle.retry_after("attacker:1", now=t + 15.0) > 0


def test_resend_sender_rejects_crlf_header_injection() -> None:
    class _ForbiddenClient:
        def post(self, *args, **kwargs):  # pragma: no cover - must never run
            raise AssertionError("HTTP client should not be called on header injection")

    client = _ForbiddenClient()
    fixture_key = "re_" + "unit_fixture"
    sender = ResendEmailSender(api_key=fixture_key, from_address="myBurnRate <no-reply@example.com>", client=client)  # type: ignore[arg-type]

    for bad_msg in (
        OutboundEmail(to="victim@example.com\r\nBcc: evil@example.com", subject="Hi", text="x", html="<p>x</p>"),
        OutboundEmail(to="victim@example.com", subject="Code\nX-Injected: yes", text="x", html="<p>x</p>"),
    ):
        with pytest.raises(EmailDeliveryError, match="control character"):
            sender.send(bad_msg)

    bad_from_sender = ResendEmailSender(api_key=fixture_key, from_address="ok@example.com\r\nX-Bad: 1", client=client)  # type: ignore[arg-type]
    with pytest.raises(EmailDeliveryError, match="control character"):
        bad_from_sender.send(OutboundEmail(to="ok@example.com", subject="Valid", text="x", html="<p>x</p>"))


def test_cloud_auth_store_read_connections_are_query_only_and_wal_persists(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db")
    with store._connect(write=False) as ro_conn:
        assert ro_conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert ro_conn.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError):
            ro_conn.execute(
                "INSERT INTO cloud_audit (at, event) VALUES ('2026-01-01T00:00:00Z', 'should_fail')"
            )


def test_cloud_sync_push_rate_limited_per_machine(tmp_path: Path) -> None:
    fixed_now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)
    mailer = MemoryEmailSender()
    app = create_cloud_app(
        _cloud_settings(tmp_path),
        data_root=tmp_path / "cloud",
        email_sender=mailer,
        now=fixed_now,
        enable_purge_loop=False,
    )
    client = TestClient(app)

    def _signup(email: str) -> tuple[str, str]:
        csrf = {"X-BURNRATE-Request": "1"}
        r1 = client.post(
            "/api/cloud/register",
            json={"email": email, "password": "strong-password-1234"},
            headers=csrf,
        )
        assert r1.status_code == 202
        code = mailer.last(to=email, tag="register_verify").subject.split()[0]  # type: ignore[union-attr]
        r2 = client.post(
            "/api/cloud/register/verify",
            json={"email": email, "code": code, "computerName": "Box"},
            headers=csrf,
        )
        assert r2.status_code in {200, 201}
        data = r2.json()
        return data["machineToken"], data["machine"]["id"]

    tok_a, mid_a = _signup("ratelimit-a@example.com")
    tok_b, mid_b = _signup("ratelimit-b@example.com")

    payload = {"machine": {"id": mid_a, "name": "Box"}, "events": [], "unpriced": [], "quotas": []}
    for i in range(60):
        resp = client.post("/api/sync/push", json=payload, headers={"Authorization": f"Bearer {tok_a}"})
        assert resp.status_code == 200, f"Push {i + 1} failed: {resp.text}"

    # 61st push within 60s returns 429 AUTH-102 with Retry-After
    throttled = client.post("/api/sync/push", json=payload, headers={"Authorization": f"Bearer {tok_a}"})
    assert throttled.status_code == 429
    assert throttled.json()["error"]["code"] == "AUTH-102"
    assert int(throttled.headers["Retry-After"]) >= 1

    # Second tenant/machine is completely unaffected by tenant A's sync push throttle
    payload_b = {"machine": {"id": mid_b, "name": "Box"}, "events": [], "unpriced": [], "quotas": []}
    ok_b = client.post("/api/sync/push", json=payload_b, headers={"Authorization": f"Bearer {tok_b}"})
    assert ok_b.status_code == 200


def test_tenant_deletion_evicts_aggregate_and_plans_value_caches(tmp_path: Path) -> None:
    cloud_root = tmp_path / "cloud"
    store = CloudAuthStore(cloud_root / "auth.db")
    tenants = TenantManager(cloud_root)
    pricing = PricingEngine.load(ROOT / "pricing")
    now = datetime(2026, 6, 15, 12, 0, tzinfo=UTC)

    _, code_a = store.start_registration("cache-a@example.com", "strong-password-alpha")
    reg_a = store.verify_registration("cache-a@example.com", code_a or "", computer_name="PC-A")
    uid_a = reg_a["user"]["id"]
    db_a = tenants.provision_user_db(uid_a)

    _, code_b = store.start_registration("cache-b@example.com", "strong-password-bravo")
    reg_b = store.verify_registration("cache-b@example.com", code_b or "", computer_name="PC-B")
    uid_b = reg_b["user"]["id"]
    db_b = tenants.provision_user_db(uid_b)

    for db_path in (db_a, db_b):
        aggregate_mod.aggregate_summary_cached(
            database_path=db_path,
            pricing=pricing,
            window_key="1d",
            tool="all",
            timezone="UTC",
            cache_threshold=0.75,
            cadence_seconds=15,
            now=now,
        )
        us = Settings(
            database_path=db_path,
            pricing_path=ROOT / "pricing",
            anthropic_admin_key=None,
            openai_admin_key=None,
            timezone="UTC",
            cache_hit_threshold=0.75,
            over_routing_token_ceiling=40000,
        )
        with connect(db_path) as conn:
            pv_mod.fetch_plans_value_report(
                connection=conn, settings=us, pricing=pricing, period="this_month", as_of=now
            )

    str_a, str_b = str(db_a), str(db_b)
    res_a, res_b = str(db_a.resolve()), str(db_b.resolve())
    assert any(str(k[0]) == str_a for b in aggregate_mod._MEMO.values() for k in b)
    assert any(str(k[0]) in {str_a, res_a} for k in pv_mod._CACHE)

    assert tenants.delete_user_storage(uid_a) is True

    # Tenant A's cached summaries & plans-value rows are completely evicted; Tenant B stays cached.
    assert not any(str(k[0]) == str_a for b in aggregate_mod._MEMO.values() for k in b)
    assert not any(str(k[0]) in {str_a, res_a} for k in pv_mod._CACHE)
    assert any(str(k[0]) == str_b for b in aggregate_mod._MEMO.values() for k in b)
    assert any(str(k[0]) in {str_b, res_b} for k in pv_mod._CACHE)


def test_backup_module_skips_invalid_dirs_and_symlinks(tmp_path: Path, monkeypatch) -> None:
    cloud_root = tmp_path / "cloud"
    store = CloudAuthStore(cloud_root / "auth.db")
    tenants = TenantManager(cloud_root)

    _, code_a = store.start_registration("bk-a@example.com", "strong-password-alpha")
    reg_a = store.verify_registration("bk-a@example.com", code_a or "", computer_name="PC-A")
    uid_a = reg_a["user"]["id"]
    tenants.provision_user_db(uid_a)

    _, code_b = store.start_registration("bk-b@example.com", "strong-password-bravo")
    reg_b = store.verify_registration("bk-b@example.com", code_b or "", computer_name="PC-B")
    uid_b = reg_b["user"]["id"]
    tenants.provision_user_db(uid_b)

    # Plant an invalid directory name under users/
    bogus = cloud_root / "users" / "not-a-valid-tenant"
    bogus.mkdir(parents=True)
    (bogus / "spend.db").write_bytes(b"not sqlite")

    # Simulate a symlinked tenant directory for uid_b without requiring Windows symlink privileges
    sim_symlink_dir = (cloud_root / "users" / uid_b).resolve()
    real_check = backup_mod.is_name_surrogate
    monkeypatch.setattr(
        backup_mod,
        "is_name_surrogate",
        lambda p: True if Path(p).resolve() == sim_symlink_dir else real_check(p),
    )

    summary = backup_mod.backup_cloud_root(cloud_root, tmp_path / "backups")
    assert summary["authBackedUp"] is True
    assert summary["tenantsBackedUp"] == 1
    assert summary["skippedEntries"] == 2
    snap = Path(summary["backupDir"])
    assert (snap / "users" / uid_a / "spend.db").is_file()
    assert not (snap / "users" / uid_b).exists()
    assert not (snap / "users" / "not-a-valid-tenant").exists()

    # Refuses symlinked auth.db outright
    auth_resolved = (cloud_root / "auth.db").resolve()
    monkeypatch.setattr(
        backup_mod,
        "is_name_surrogate",
        lambda p: True if Path(p).resolve() == auth_resolved else real_check(p),
    )
    with pytest.raises(PermissionError, match="auth.db"):
        backup_mod.backup_cloud_root(cloud_root, tmp_path / "backups-2")


def test_compose_hardening_volume_permissions_and_limits() -> None:
    compose = (ROOT / "deploy" / "cloud" / "compose.yaml").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "cloud-deployment.md").read_text(encoding="utf-8")

    # Mount cloud_data at /data (owned by non-root uid 10001 in Dockerfile), NOT /data/cloud
    assert "cloud_data:/data\n" in compose
    assert "cloud_data:/data/cloud" not in compose
    # Explicitly disable self-hosted central mode from Dockerfile ENV
    assert 'BURNRATE_CENTRAL_SERVER: "0"' in compose
    # Resource bounds + tmpfs on cloud-hub
    assert "pids_limit:" in compose
    assert "mem_limit:" in compose
    assert "/tmp" in compose
    # Docs invoke the packaged Python module that actually exists inside the container image
    assert "python -m spend_app.cloud.backup" in doc


def test_sync_machine_last_seen_cache_is_bounded() -> None:
    assert isinstance(sync_mod._MACHINE_LAST_SEEN_MEM, OrderedDict)
    assert sync_mod._MACHINE_LAST_SEEN_MAX == 4096


def test_desktop_bridge_clears_stale_last_sync_error_on_cloud_login(tmp_path: Path, monkeypatch) -> None:
    from types import SimpleNamespace

    from spend_app.db import initialize
    from spend_app.setup_wizard import read_setup_state, write_setup_state
    from spend_app.sync import _HUB_TEST_CLIENTS
    from spend_app.wealthfront_window import DesktopBridge

    class _MemVault:
        def __init__(self) -> None:
            self.data: dict[str, str] = {}

        def put(self, secret: str, ref: str = "") -> str:
            key = ref or f"mem-{len(self.data)}"
            self.data[key] = secret
            return key

        def get(self, ref: str) -> str | None:
            return self.data.get(ref)

        def delete(self, ref: str) -> None:
            self.data.pop(ref, None)

    cloud_origin = "https://cloud-hardening.test"
    mailer = MemoryEmailSender()
    cloud_app = create_cloud_app(
        _cloud_settings(tmp_path),
        data_root=tmp_path / "cloud",
        email_sender=mailer,
        enable_purge_loop=False,
    )
    cloud_client = TestClient(cloud_app, base_url=cloud_origin)
    monkeypatch.setenv("BURNRATE_CLOUD_URL", cloud_origin)
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "cloud-hardening.test")
    _HUB_TEST_CLIENTS[cloud_origin] = cloud_client
    try:
        local_db = tmp_path / "local.db"
        initialize(local_db)
        with connect(local_db) as conn:
            write_setup_state(
                conn,
                {
                    "role": "local",
                    "computerName": "Laptop",
                    "spokeConfig": {"enabled": False, "lastSyncError": "Old stale connection error"},
                },
            )
            conn.commit()
        local_settings = Settings(
            database_path=local_db,
            pricing_path=ROOT / "pricing",
            anthropic_admin_key=None,
            openai_admin_key=None,
            timezone="UTC",
            cache_hit_threshold=0.75,
            over_routing_token_ceiling=40000,
        )
        fake_client = SimpleNamespace(
            settings=local_settings,
            pricing=None,
            ensure_service=lambda: None,
            request=lambda *a, **kw: {},
        )
        vault = _MemVault()
        bridge = DesktopBridge(fake_client, demo=False)
        bridge._vault = vault

        r1 = bridge.cloud_register_start("stale@example.com", "strong-password-1234")
        assert r1["ok"] is True
        code = mailer.last(to="stale@example.com", tag="register_verify").subject.split()[0]  # type: ignore[union-attr]
        r2 = bridge.cloud_register_verify("stale@example.com", code, "Laptop")
        assert r2["ok"] is True

        with connect(local_db) as conn:
            persisted = read_setup_state(conn)
        assert (persisted.get("spokeConfig") or {}).get("lastSyncError") is None
    finally:
        _HUB_TEST_CLIENTS.pop(cloud_origin, None)
