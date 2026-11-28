"""Phase 3 tests: DesktopBridge Cloud flows, Keychain pinning, redaction, and non-regression."""
from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from spend_app.cloud.app import create_cloud_app
from spend_app.cloud.auth_store import CloudAuthStore
from spend_app.cloud.email_sender import MemoryEmailSender
from spend_app.cloud.tenant import TenantManager
from spend_app.config import ROOT, Settings
from spend_app.db import connect, initialize
from spend_app.redact import redact
from spend_app.setup_wizard import read_setup_state
from spend_app.sync import _HUB_TEST_CLIENTS
from spend_app.wealthfront_window import DesktopBridge


class _MemVault:
    def __init__(self) -> None:
        self.data: dict[str, str] = {}

    def get(self, ref: str) -> str | None:
        return self.data.get(ref)

    def put(self, secret: str, ref: str = "") -> str:
        key = ref or f"mem-{len(self.data)}"
        self.data[key] = secret
        return key

    def delete(self, ref: str) -> None:
        self.data.pop(ref, None)


def _make_bridge(tmp_path: Path, name: str) -> tuple[DesktopBridge, _MemVault, Path]:
    db_path = tmp_path / f"{name}.db"
    initialize(db_path)
    settings = Settings(
        database_path=db_path,
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    fake_client = SimpleNamespace(settings=settings, pricing=None, ensure_service=lambda: None, request=lambda *a, **kw: {})
    vault = _MemVault()
    bridge = DesktopBridge(fake_client, demo=False)
    bridge._vault = vault
    return bridge, vault, db_path


def _extract_otp(text: str) -> str:
    m = re.search(r"\b(\d{6})\b", text)
    assert m is not None
    return m.group(1)


def test_desktop_bridge_cloud_register_pair_reset_email_and_delete(tmp_path: Path, monkeypatch) -> None:
    cloud_root = tmp_path / "cloud_server"
    store = CloudAuthStore(cloud_root / "auth.db")
    tenants = TenantManager(cloud_root)
    mailer = MemoryEmailSender()
    server_settings = Settings(
        database_path=tmp_path / "unused.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )
    app = create_cloud_app(
        settings=server_settings,
        data_root=cloud_root,
        auth_store=store,
        tenant_manager=tenants,
        email_sender=mailer,
        now=datetime(2026, 11, 28, 12, 0, tzinfo=UTC),
        enable_purge_loop=False,
    )
    cloud_origin = "https://cloud.test"
    monkeypatch.setenv("BURNRATE_CLOUD_URL", cloud_origin)
    monkeypatch.delenv("BURNRATE_HUB_URL", raising=False)
    monkeypatch.setenv("BURNRATE_ALLOWED_HOSTS", "cloud.test")

    test_client = TestClient(app, base_url=cloud_origin)
    _HUB_TEST_CLIENTS[cloud_origin] = test_client
    try:
        pc1, vault1, db1 = _make_bridge(tmp_path, "pc1")
        pc2, vault2, db2 = _make_bridge(tmp_path, "pc2")

        # 1. PC1 registers via 2-step OTP
        start = pc1.cloud_register_start("kamol@example.com", "super-secret-pass-123")
        assert start["ok"] is True and start["verificationRequired"] is True
        otp_mail = mailer.last(to="kamol@example.com", tag="register_verify")
        assert otp_mail is not None
        verified = pc1.cloud_register_verify("kamol@example.com", _extract_otp(otp_mail.text), computer_name="Office PC")
        assert verified["ok"] is True
        assert verified["mode"] == "cloud"
        assert verified["passwordAuthenticated"] is True

        # Tokens are stored ONLY in Keychain vault with br_cs_ / br_cm_ prefixes (not plaintext in DB!)
        assert vault1.get(pc1.OWNER_SESSION_VAULT_REF).startswith("br_cs_")
        assert vault1.get(pc1.MACHINE_TOKEN_VAULT_REF).startswith("br_cm_")
        with connect(db1) as conn:
            sp1 = read_setup_state(conn)["spokeConfig"]
        assert sp1.get("hubMode") == "cloud"
        assert sp1.get("hubUrl") == cloud_origin
        assert not sp1.get("token") and not sp1.get("ownerSession")

        # 2. PC1 generates 8-char pairing code; PC2 redeems it with NO password
        pair_gen = pc1.cloud_create_pairing_code()
        assert pair_gen["ok"] is True and len(pair_gen["code"]) == 9
        paired = pc2.cloud_redeem_pairing_code(pair_gen["code"], computer_name="Travel Laptop")
        assert paired["ok"] is True
        assert paired["paired"] is True
        assert paired["passwordAuthenticated"] is False
        assert vault2.get(pc2.OWNER_SESSION_VAULT_REF).startswith("br_cs_")
        assert vault2.get(pc2.MACHINE_TOKEN_VAULT_REF).startswith("br_cm_")

        # Paired PC2 CANNOT delete the account
        blocked_del = pc2.cloud_delete_account("super-secret-pass-123", "DELETE")
        assert blocked_del["ok"] is False

        # 3. PC1 changes email via 2-step OTP
        ec_req = pc1.cloud_email_change_request("super-secret-pass-123", "kamol.new@example.com")
        assert ec_req["ok"] is True
        ec_mail = mailer.last(to="kamol.new@example.com", tag="email_change_verify")
        assert ec_mail is not None
        ec_conf = pc1.cloud_email_change_confirm(_extract_otp(ec_mail.text))
        assert ec_conf["ok"] is True and ec_conf["email"] == "kamol.new@example.com"

        # 4. PC1 changes password (routes automatically to /api/cloud/password/change for br_cs_!)
        pw_ok = pc1.change_password("super-secret-pass-123", "brand-new-pass-456")
        assert pw_ok["ok"] is True

        # 5. PC1 deletes cloud account -> wipes cloud storage AND returns PC1 to local-only with empty Keychain
        deleted = pc1.cloud_delete_account("brand-new-pass-456", "DELETE")
        assert deleted["ok"] is True and deleted["deleted"] is True
        assert vault1.get(pc1.OWNER_SESSION_VAULT_REF) is None
        assert vault1.get(pc1.MACHINE_TOKEN_VAULT_REF) is None
        with connect(db1) as conn:
            st1 = read_setup_state(conn)
        assert st1["role"] == "local"
        assert st1["spokeConfig"].get("enabled") is False
    finally:
        _HUB_TEST_CLIENTS.pop(cloud_origin, None)


def test_cloud_tokens_and_resend_key_are_redacted() -> None:
    sample = (
        "session=br_cs_0123456789abcdef0123456789abcdef_secretTOKEN123 "
        "machine=br_cm_0123456789abcdef_secretMACHINE456 "
        "resend=re_LiveSecretKey987654321"
    )
    cleaned = redact(sample)
    assert "br_cs_" not in cleaned
    assert "br_cm_" not in cleaned
    assert "re_LiveSecretKey" not in cleaned
