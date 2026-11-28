"""Phase 2 tests: Cloud API endpoints, multi-user tenant isolation, quotas, pairing, reset, deletion."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.cloud.app import create_cloud_app
from spend_app.cloud.auth_store import CloudAuthStore
from spend_app.cloud.email_sender import MemoryEmailSender
from spend_app.cloud.tenant import TenantManager
from spend_app.config import ROOT, Settings

HEADERS = {
    "X-BURNRATE-Request": "1",
    "Content-Type": "application/json",
}


def _make_cloud(tmp_path: Path, *, max_db_bytes: int = 250 * 1024 * 1024, max_devices: int = 5):
    cloud_root = tmp_path / "cloud"
    store = CloudAuthStore(cloud_root / "auth.db", default_max_devices=max_devices, default_max_db_bytes=max_db_bytes)
    tenants = TenantManager(cloud_root, default_max_db_bytes=max_db_bytes)
    mailer = MemoryEmailSender()
    settings = Settings(
        database_path=tmp_path / "unused.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    fixed_now = datetime(2026, 11, 28, 12, 0, tzinfo=UTC)
    app = create_cloud_app(
        settings=settings,
        data_root=cloud_root,
        auth_store=store,
        tenant_manager=tenants,
        email_sender=mailer,
        now=fixed_now,
        enable_purge_loop=False,
    )
    return TestClient(app), store, tenants, mailer, cloud_root


def _extract_otp(text: str) -> str:
    match = re.search(r"\b(\d{6})\b", text)
    assert match is not None, f"No 6-digit OTP found in: {text}"
    return match.group(1)


def _register_user(client: TestClient, mailer: MemoryEmailSender, email: str, password: str, pc_name: str, dev_id: str) -> dict:
    resp = client.post(
        "/api/cloud/register",
        json={"email": email, "password": password},
        headers=HEADERS,
    )
    assert resp.status_code == 202
    sent = mailer.last(to=email, tag="register_verify")
    assert sent is not None
    code = _extract_otp(sent.text)

    verify = client.post(
        "/api/cloud/register/verify",
        json={"email": email, "code": code, "computerName": pc_name, "os": "Windows", "deviceId": dev_id},
        headers=HEADERS,
    )
    assert verify.status_code == 200, verify.text
    return verify.json()


def _sample_push(raw_id: str, input_tokens: int = 1000) -> dict:
    return {
        "machine": {"name": "PC", "os": "Windows"},
        "usageEvents": [
            {
                "raw_id": raw_id,
                "source": "claude_code",
                "tool_key": "claude_code",
                "model_key": "claude-sonnet-4-20250514",
                "occurred_at": "2026-11-28T11:30:00Z",
                "input_tokens": input_tokens,
                "output_tokens": 250,
                "cost_usd": 0.015,
            }
        ],
    }


def test_multi_tenant_isolation_and_pairing_restrictions(tmp_path: Path) -> None:
    client, store, tenants, mailer, cloud_root = _make_cloud(tmp_path)

    # Hub info reports cloud mode
    info = client.get("/api/hub/info")
    assert info.status_code == 200
    assert info.json()["mode"] == "cloud"

    # Before verification, zero user DBs exist on disk
    r0 = client.post(
        "/api/cloud/register",
        json={"email": "unverified@example.com", "password": "password-very-strong-1"},
        headers=HEADERS,
    )
    assert r0.status_code == 202
    users_dir = cloud_root / "users"
    assert not users_dir.exists() or list(users_dir.iterdir()) == []

    # Register User A and User B via OTP
    alice_pass = "alicepassword1111"
    alice = _register_user(client, mailer, "alice@example.com", alice_pass, "Alice Desktop", "alice-pc-0001")
    bob = _register_user(client, mailer, "bob@example.com", "bobpassword2222", "Bob MacBook", "bob-mac-0001")

    alice_uid = alice["user"]["id"]
    bob_uid = bob["user"]["id"]
    assert alice_uid != bob_uid
    assert (cloud_root / "users" / alice_uid / "spend.db").is_file()
    assert (cloud_root / "users" / bob_uid / "spend.db").is_file()

    # Duplicate register returns generic 202 and sends alert to existing user (no enumeration)
    dup = client.post(
        "/api/cloud/register",
        json={"email": "alice@example.com", "password": "attacker-password-999"},
        headers=HEADERS,
    )
    assert dup.status_code == 202
    assert mailer.last(to="alice@example.com", tag="register_existing_alert") is not None

    # Alice pairs a 2nd computer via 8-char code (no master password on Laptop)
    pair_req = client.post(
        "/api/cloud/pairing-code",
        json={},
        headers={**HEADERS, "Authorization": f"Bearer {alice['sessionToken']}"},
    )
    assert pair_req.status_code == 200
    pair_code = pair_req.json()["code"]

    paired = client.post(
        "/api/cloud/pair",
        json={"code": pair_code, "computerName": "Alice Laptop", "os": "macOS", "deviceId": "alice-pc-0002"},
        headers=HEADERS,
    )
    assert paired.status_code == 200
    alice_laptop = paired.json()

    # Paired session CAN read Alice's analytics/machines, CANNOT delete account or change password
    me_paired = client.get("/api/cloud/me", headers={"Authorization": f"Bearer {alice_laptop['sessionToken']}"})
    assert me_paired.status_code == 200
    assert me_paired.json()["passwordAuthenticated"] is False

    del_blocked = client.post(
        "/api/cloud/account/delete",
        json={"password": alice_pass, "confirm": "DELETE"},
        headers={**HEADERS, "Authorization": f"Bearer {alice_laptop['sessionToken']}"},
    )
    assert del_blocked.status_code == 403

    # Alice pushes usage from Desktop + Laptop; Bob pushes usage from Bob MacBook
    p_a1 = client.post(
        "/api/sync/push",
        json=_sample_push("evt-alice-1", input_tokens=1111),
        headers={**HEADERS, "Authorization": f"Bearer {alice['machineToken']}"},
    )
    assert p_a1.status_code == 200 and p_a1.json()["eventsWritten"] == 1

    p_a2 = client.post(
        "/api/sync/push",
        json=_sample_push("evt-alice-2", input_tokens=2222),
        headers={**HEADERS, "Authorization": f"Bearer {alice_laptop['machineToken']}"},
    )
    assert p_a2.status_code == 200 and p_a2.json()["eventsWritten"] == 1

    p_b1 = client.post(
        "/api/sync/push",
        json=_sample_push("evt-bob-1", input_tokens=99999),
        headers={**HEADERS, "Authorization": f"Bearer {bob['machineToken']}"},
    )
    assert p_b1.status_code == 200 and p_b1.json()["eventsWritten"] == 1

    # Strict cross-tenant isolation: Alice sees ONLY Alice's 2 machines & tokens; Bob sees ONLY Bob's
    alice_machines = client.get("/api/machines", headers={"Authorization": f"Bearer {alice['sessionToken']}"}).json()["machines"]
    bob_machines = client.get("/api/machines", headers={"Authorization": f"Bearer {bob['sessionToken']}"}).json()["machines"]
    assert {m["id"] for m in alice_machines} == {alice["machine"]["id"], alice_laptop["machine"]["id"]}
    assert {m["id"] for m in bob_machines} == {bob["machine"]["id"]}

    alice_usage = client.get("/api/spend/usage?period=all", headers={"Authorization": f"Bearer {alice['sessionToken']}"}).json()
    bob_usage = client.get("/api/spend/usage?period=all", headers={"Authorization": f"Bearer {bob['sessionToken']}"}).json()
    assert alice_usage["total"]["tokens"] == (1111 + 250) + (2222 + 250)
    assert bob_usage["total"]["tokens"] == 99999 + 250

    # Alice CANNOT rename/revoke/delete Bob's machine
    cross_revoke = client.post(
        "/api/machines",
        json={"action": "revoke", "machineId": bob["machine"]["id"]},
        headers={**HEADERS, "Authorization": f"Bearer {alice['sessionToken']}"},
    )
    assert cross_revoke.status_code == 404

    # Machine token CANNOT read another machine's data (even within the same user!)
    cross_machine_read = client.get(
        f"/api/spend/usage?machine={alice_laptop['machine']['id']}",
        headers={"Authorization": f"Bearer {alice['machineToken']}"},
    )
    assert cross_machine_read.status_code == 403
    own_machine_read = client.get(
        f"/api/spend/usage?machine={alice['machine']['id']}",
        headers={"Authorization": f"Bearer {alice['machineToken']}"},
    )
    assert own_machine_read.status_code == 200
    assert own_machine_read.json()["total"]["tokens"] == 1111 + 250

    # Account deletion physically removes Alice's DB & auth rows while Bob remains completely untouched
    del_ok = client.post(
        "/api/cloud/account/delete",
        json={"password": alice_pass, "confirm": "DELETE"},
        headers={**HEADERS, "Authorization": f"Bearer {alice['sessionToken']}"},
    )
    assert del_ok.status_code == 200
    assert not (cloud_root / "users" / alice_uid).exists()
    assert mailer.last(to="alice@example.com", tag="account_deleted") is not None

    # Alice's tokens are dead immediately
    assert client.get("/api/cloud/me", headers={"Authorization": f"Bearer {alice['sessionToken']}"}).status_code == 401
    assert client.post(
        "/api/sync/push",
        json=_sample_push("evt-after-delete"),
        headers={**HEADERS, "Authorization": f"Bearer {alice['machineToken']}"},
    ).status_code == 401

    # Bob is completely unaffected
    bob_still = client.get("/api/spend/usage?period=all", headers={"Authorization": f"Bearer {bob['sessionToken']}"})
    assert bob_still.status_code == 200
    assert bob_still.json()["total"]["tokens"] == 99999 + 250


def test_password_reset_email_change_and_storage_quota_413(tmp_path: Path) -> None:
    client, _store, _tenants, mailer, _root = _make_cloud(tmp_path, max_db_bytes=1)

    user = _register_user(client, mailer, "dave@example.com", "dave-initial-pass-123", "Dave PC", "dave-dev-0001")

    # Quota exceeded (max_db_bytes=1) returns 413 CLOUD-413 before writing
    over = client.post(
        "/api/sync/push",
        json=_sample_push("evt-over-quota"),
        headers={**HEADERS, "Authorization": f"Bearer {user['machineToken']}"},
    )
    assert over.status_code == 413
    assert over.json()["error"]["code"] == "CLOUD-413"

    # Password reset on unknown email still returns generic 202 (no enumeration)
    unknown = client.post(
        "/api/cloud/password-reset/request",
        json={"email": "nobody@example.com"},
        headers=HEADERS,
    )
    assert unknown.status_code == 202
    assert mailer.last(to="nobody@example.com") is None

    # Password reset for Dave with disconnectDevices=False kills session, keeps machine token
    req_r = client.post(
        "/api/cloud/password-reset/request",
        json={"email": "dave@example.com"},
        headers=HEADERS,
    )
    assert req_r.status_code == 202
    reset_mail = mailer.last(to="dave@example.com", tag="password_reset")
    assert reset_mail is not None
    reset_code = _extract_otp(reset_mail.text)

    confirm_r = client.post(
        "/api/cloud/password-reset/confirm",
        json={
            "email": "dave@example.com",
            "code": reset_code,
            "newPassword": "dave-new-password-999",
            "disconnectDevices": False,
        },
        headers=HEADERS,
    )
    assert confirm_r.status_code == 200
    assert confirm_r.json()["revokedSessions"] == 1
    assert confirm_r.json()["revokedDevices"] == 0
    assert mailer.last(to="dave@example.com", tag="password_changed") is not None

    # Old session rejected; login with new password works
    assert client.get("/api/cloud/me", headers={"Authorization": f"Bearer {user['sessionToken']}"}).status_code == 401
    login_r = client.post(
        "/api/cloud/login",
        json={
            "email": "dave@example.com",
            "password": "dave-new-password-999",
            "computerName": "Dave PC",
            "deviceId": "dave-dev-0001",
        },
        headers=HEADERS,
    )
    assert login_r.status_code == 200
    new_session = login_r.json()["sessionToken"]

    # Verified email change sends code to new email and alerts both old and new on confirm
    ec_req = client.post(
        "/api/cloud/email-change/request",
        json={"password": "dave-new-password-999", "newEmail": "dave.updated@example.com"},
        headers={**HEADERS, "Authorization": f"Bearer {new_session}"},
    )
    assert ec_req.status_code == 202
    ec_mail = mailer.last(to="dave.updated@example.com", tag="email_change_verify")
    assert ec_mail is not None
    ec_code = _extract_otp(ec_mail.text)

    ec_conf = client.post(
        "/api/cloud/email-change/confirm",
        json={"code": ec_code},
        headers={**HEADERS, "Authorization": f"Bearer {new_session}"},
    )
    assert ec_conf.status_code == 200
    assert ec_conf.json()["email"] == "dave.updated@example.com"
    assert mailer.last(to="dave@example.com", tag="email_changed") is not None
    assert mailer.last(to="dave.updated@example.com", tag="email_changed") is not None
