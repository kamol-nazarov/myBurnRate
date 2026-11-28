"""Phase 1 tests: CloudAuthStore, TenantManager, and ResendEmailSender."""
from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from spend_app.cloud.auth_store import (
    CLOUD_MACHINE_PREFIX,
    CLOUD_SESSION_PREFIX,
    OTP_MAX_ATTEMPTS,
    PAIRING_MAX_ACTIVE,
    CloudAuthError,
    CloudAuthStore,
)
from spend_app.cloud.email_sender import (
    RESEND_API_URL,
    EmailDeliveryError,
    MemoryEmailSender,
    ResendEmailSender,
    verification_email,
)
from spend_app.cloud.tenant import StorageQuotaExceeded, TenantManager, validate_user_id


def test_registration_otp_lifecycle_and_duplicate_silence(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db")
    t0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

    # Short password rejected
    with pytest.raises(CloudAuthError) as exc:
        store.start_registration("alice@example.com", "short", now=t0)
    assert exc.value.code == "AUTH-105"

    # Stage pending registration
    state, code = store.start_registration("Alice@Example.com", "correct horse battery staple", now=t0)
    assert state == "pending"
    assert code is not None and len(code) == 6 and code.isdigit()

    # Wrong OTP increments attempts and auto-invalidates on 5th miss
    for _ in range(OTP_MAX_ATTEMPTS):
        bad = "000000" if code != "000000" else "999999"
        with pytest.raises(CloudAuthError) as bad_exc:
            store.verify_registration("alice@example.com", bad, now=t0 + timedelta(minutes=1))
        assert bad_exc.value.status_code == 401

    # Even the real code is now dead because the pending row was purged on attempt 5
    with pytest.raises(CloudAuthError):
        store.verify_registration("alice@example.com", code, now=t0 + timedelta(minutes=1))

    # Re-stage and test expiry
    state2, code2 = store.start_registration("alice@example.com", "correct horse battery staple", now=t0)
    assert state2 == "pending" and code2 is not None
    with pytest.raises(CloudAuthError):
        store.verify_registration("alice@example.com", code2, now=t0 + timedelta(minutes=16))

    # Re-stage and verify successfully
    _, code3 = store.start_registration("alice@example.com", "correct horse battery staple", now=t0)
    assert code3 is not None
    verified = store.verify_registration(
        "alice@example.com",
        code3,
        computer_name="Alice MacBook",
        os_name="macOS",
        device_id="device-alice-0001",
        now=t0 + timedelta(minutes=2),
    )
    assert verified["user"]["email"] == "alice@example.com"
    assert verified["sessionToken"].startswith(CLOUD_SESSION_PREFIX)
    assert verified["machineToken"].startswith(CLOUD_MACHINE_PREFIX)

    # Session & machine tokens authenticate cleanly
    sp = store.authenticate_session(verified["sessionToken"], now=t0 + timedelta(minutes=3))
    assert sp is not None and sp.kind == "owner" and sp.password_authenticated is True
    assert sp.permits("account.sensitive") is True

    mp = store.authenticate_machine(verified["machineToken"], now=t0 + timedelta(minutes=3))
    assert mp is not None and mp.kind == "machine" and mp.user_id == sp.user_id
    assert mp.machine_id == verified["machine"]["id"]

    # Duplicate registration returns ("existing", None) without touching Alice's account
    dup_state, dup_code = store.start_registration("alice@example.com", "different password 1234", now=t0)
    assert dup_state == "existing"
    assert dup_code is None


def test_pairing_code_single_use_device_limit_and_cascade_revoke(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db", default_max_devices=2)
    t0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

    _, code = store.start_registration("bob@example.com", "super-strong-password-123", now=t0)
    assert code is not None
    reg = store.verify_registration(
        "bob@example.com", code, computer_name="Bob Desktop", device_id="bob-dev-0001", now=t0
    )
    uid = reg["user"]["id"]

    # Creating > PAIRING_MAX_ACTIVE prunes oldest active code
    codes = [store.create_pairing_code(uid, reg["sessionId"], now=t0 + timedelta(seconds=i)) for i in range(PAIRING_MAX_ACTIVE + 1)]
    assert len(codes[0]["code"]) == 9 and "-" in codes[0]["code"]
    # First code was evicted when 4th was generated
    with pytest.raises(CloudAuthError):
        store.redeem_pairing_code(codes[0]["code"], computer_name="Laptop", now=t0 + timedelta(seconds=10))

    # Latest code redeems cleanly (even without hyphen / lowercase)
    raw_pair = codes[-1]["code"].replace("-", "").lower()
    paired = store.redeem_pairing_code(
        raw_pair,
        computer_name="Bob Laptop",
        os_name="Windows",
        device_id="bob-dev-0002",
        now=t0 + timedelta(seconds=20),
    )
    assert paired["user"]["id"] == uid
    # Replay of same code fails immediately
    with pytest.raises(CloudAuthError):
        store.redeem_pairing_code(codes[-1]["code"], computer_name="Other", now=t0 + timedelta(seconds=25))

    # Paired session is password_authenticated=False (cannot do sensitive account actions)
    p_sess = store.authenticate_session(paired["sessionToken"], now=t0 + timedelta(seconds=30))
    assert p_sess is not None
    assert p_sess.password_authenticated is False
    assert p_sess.permits("hub.read") is True
    assert p_sess.permits("account.sensitive") is False

    # Third distinct device hits max_devices=2
    code3 = store.create_pairing_code(uid, reg["sessionId"], now=t0 + timedelta(seconds=40))
    with pytest.raises(CloudAuthError) as limit_exc:
        store.redeem_pairing_code(
            code3["code"], computer_name="Bob Third", device_id="bob-dev-0003", now=t0 + timedelta(seconds=45)
        )
    assert limit_exc.value.status_code == 409

    # Revoking Bob Laptop kills BOTH its machine token AND its device-bound session
    assert store.revoke_machine(uid, paired["machine"]["id"], now=t0 + timedelta(seconds=50)) is True
    assert store.authenticate_machine(paired["machineToken"], now=t0 + timedelta(seconds=55)) is None
    assert store.authenticate_session(paired["sessionToken"], now=t0 + timedelta(seconds=55)) is None
    # Desktop session still alive
    assert store.authenticate_session(reg["sessionToken"], now=t0 + timedelta(seconds=55)) is not None


def test_password_reset_email_change_delete_and_append_only_audit(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db")
    t0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)

    _, otp = store.start_registration("carol@example.com", "initial-password-1234", now=t0)
    assert otp is not None
    reg = store.verify_registration("carol@example.com", otp, computer_name="Carol PC", device_id="carol-dev-0001", now=t0)
    uid = reg["user"]["id"]

    # Password reset without disconnect_devices: revokes sessions, keeps machine token alive
    reset_uid, reset_otp = store.request_password_reset("carol@example.com", now=t0 + timedelta(minutes=1))
    assert reset_uid == uid and reset_otp is not None
    res = store.confirm_password_reset(
        "carol@example.com",
        reset_otp,
        "updated-password-5678",
        disconnect_devices=False,
        now=t0 + timedelta(minutes=2),
    )
    assert res["revokedSessions"] == 1
    assert res["revokedDevices"] == 0
    assert store.authenticate_session(reg["sessionToken"], now=t0 + timedelta(minutes=3)) is None
    assert store.authenticate_machine(reg["machineToken"], now=t0 + timedelta(minutes=3)) is not None

    # Login with new password succeeds; old password fails and logs audit
    with pytest.raises(CloudAuthError):
        store.login("carol@example.com", "initial-password-1234", now=t0 + timedelta(minutes=4))
    logged = store.login(
        "carol@example.com", "updated-password-5678", computer_name="Carol PC", device_id="carol-dev-0001", now=t0 + timedelta(minutes=5)
    )

    # Password reset WITH disconnect_devices revokes BOTH sessions and machines
    _, reset_otp2 = store.request_password_reset("carol@example.com", now=t0 + timedelta(minutes=6))
    assert reset_otp2 is not None
    res2 = store.confirm_password_reset(
        "carol@example.com",
        reset_otp2,
        "third-password-999999",
        disconnect_devices=True,
        now=t0 + timedelta(minutes=7),
    )
    assert res2["revokedSessions"] == 1
    assert res2["revokedDevices"] == 1
    assert store.authenticate_machine(logged["machineToken"], now=t0 + timedelta(minutes=8)) is None

    # Verified email change
    target_email, ec_otp = store.request_email_change(
        uid, "third-password-999999", "carol.new@example.com", now=t0 + timedelta(minutes=9)
    )
    assert target_email == "carol.new@example.com"
    old_e, new_e = store.confirm_email_change(uid, ec_otp, now=t0 + timedelta(minutes=10))
    assert (old_e, new_e) == ("carol@example.com", "carol.new@example.com")

    # Account deletion cascades user rows, while cloud_audit remains append-only
    deleted_email = store.delete_account(uid, "third-password-999999", now=t0 + timedelta(minutes=11))
    assert deleted_email == "carol.new@example.com"
    assert store.get_user_profile(uid) is None

    conn = sqlite3.connect(tmp_path / "auth.db")
    try:
        audit_count = conn.execute("SELECT COUNT(*) FROM cloud_audit").fetchone()[0]
        assert audit_count >= 5
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("DELETE FROM cloud_audit")
        with pytest.raises(sqlite3.IntegrityError, match="append-only"):
            conn.execute("UPDATE cloud_audit SET outcome='tampered'")
    finally:
        conn.close()


def test_pending_purge_and_tenant_manager_isolation(tmp_path: Path) -> None:
    store = CloudAuthStore(tmp_path / "auth.db")
    t0 = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    store.start_registration("ghost@example.com", "ghost-password-1234", now=t0)
    purged = store.purge_expired(now=t0 + timedelta(hours=25))
    assert purged["pending"] == 1

    mgr = TenantManager(tmp_path / "cloud_root", default_max_db_bytes=1024 * 1024, prune_interval_seconds=60)
    # Path traversal / malformed IDs rejected
    for bad_id in ("../escape", "abc", "Z" * 32, "0" * 31, "0" * 33):
        with pytest.raises(ValueError):
            validate_user_id(bad_id)

    uid = "a" * 32
    # Never implicitly creates a DB
    with pytest.raises(FileNotFoundError):
        mgr.existing_user_db(uid)

    db_path = mgr.provision_user_db(uid)
    assert db_path.is_file()
    assert mgr.existing_user_db(uid) == db_path
    assert mgr.db_size_bytes(uid) > 0

    # Storage quota enforcement
    with pytest.raises(StorageQuotaExceeded):
        mgr.check_storage_quota(uid, max_bytes=1)

    # Lazy prune runs once per interval
    assert mgr.maybe_prune_user_db(uid, now_monotonic=100.0) is True
    assert mgr.maybe_prune_user_db(uid, now_monotonic=120.0) is False
    assert mgr.maybe_prune_user_db(uid, now_monotonic=161.0) is True

    # Deletion removes folder cleanly
    assert mgr.delete_user_storage(uid) is True
    assert not db_path.parent.exists()
    with pytest.raises(FileNotFoundError):
        mgr.existing_user_db(uid)


def test_resend_email_sender_and_memory_sender() -> None:
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        return httpx.Response(200, json={"id": "re_123"})

    client = httpx.Client(transport=httpx.MockTransport(handler))
    fixture_key = "re_" + "unit_fixture"
    sender = ResendEmailSender(api_key=fixture_key, from_address="myBurnRate <no-reply@myburnrate.io>", client=client)
    msg = verification_email("user@example.com", "123456")
    sender.send(msg)
    assert len(captured) == 1
    assert str(captured[0].url) == RESEND_API_URL
    assert captured[0].headers["authorization"] == f"Bearer {fixture_key}"

    # Error handling
    err_client = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(502, text="bad gateway")))
    err_sender = ResendEmailSender(api_key=fixture_key, client=err_client)
    with pytest.raises(EmailDeliveryError):
        err_sender.send(msg)

    # Missing API key fails cleanly
    with pytest.raises(EmailDeliveryError):
        ResendEmailSender(api_key="", client=client).send(msg)

    # Memory sink
    mem = MemoryEmailSender()
    mem.send(msg)
    assert mem.last(to="user@example.com", tag="register_verify") == msg
