"""Phase 4 tests: deploy/cloud stack validation, create_app cloud dispatch, and online tenant backups."""
from __future__ import annotations

import importlib.util
from pathlib import Path

from fastapi.testclient import TestClient

from spend_app.api import create_app
from spend_app.cloud.auth_store import CloudAuthStore
from spend_app.cloud.tenant import TenantManager
from spend_app.config import ROOT, Settings

DEPLOY_CLOUD = ROOT / "deploy" / "cloud"


def test_deploy_cloud_configs_enforce_isolation_and_tls() -> None:
    compose = (DEPLOY_CLOUD / "compose.yaml").read_text(encoding="utf-8")
    caddy = (DEPLOY_CLOUD / "Caddyfile").read_text(encoding="utf-8")
    litestream = (DEPLOY_CLOUD / "litestream.yml").read_text(encoding="utf-8")
    env_ex = (DEPLOY_CLOUD / ".env.example").read_text(encoding="utf-8")
    doc = (ROOT / "docs" / "cloud-deployment.md").read_text(encoding="utf-8")

    # Cloud hub runs in cloud mode with dropped capabilities and no public host port
    assert 'BURNRATE_CLOUD_SERVER: "1"' in compose
    assert "no-new-privileges:true" in compose
    assert "cap_drop:" in compose
    assert "cloud-hub:" in compose
    # Only Caddy exposes ports; cloud-hub sits behind cloud_internal
    hub_section = compose.split("caddy:")[0]
    assert "ports:" not in hub_section

    # Caddy enforces HSTS, body cap, and proxies to cloud-hub:8788
    assert "Strict-Transport-Security" in caddy
    assert "max_size 18MB" in caddy
    assert "reverse_proxy cloud-hub:8788" in caddy

    # Litestream replicates central auth.db
    assert "/data/cloud/auth.db" in litestream
    assert "BURNRATE_RESEND_API_KEY" in env_ex
    assert "deploy/cloud" in doc


def test_create_app_switches_cleanly_between_cloud_and_local(tmp_path: Path, monkeypatch) -> None:
    settings = Settings(
        database_path=tmp_path / "local.db",
        pricing_path=ROOT / "pricing",
        anthropic_admin_key=None,
        openai_admin_key=None,
        timezone="UTC",
        cache_hit_threshold=0.75,
        over_routing_token_ceiling=40000,
    )

    # Local mode (default)
    monkeypatch.delenv("BURNRATE_CLOUD_SERVER", raising=False)
    monkeypatch.delenv("BURNRATE_CENTRAL_SERVER", raising=False)
    local_app = create_app(settings=settings, enable_scheduler=False)
    local_client = TestClient(local_app)
    local_info = local_client.get("/api/hub/info").json()
    assert local_info["service"] == "burnrate"
    assert "mode" not in local_info

    # Cloud mode
    monkeypatch.setenv("BURNRATE_CLOUD_SERVER", "1")
    monkeypatch.setenv("BURNRATE_CLOUD_DATA_ROOT", str(tmp_path / "cloud"))
    cloud_app = create_app(settings=settings, enable_scheduler=False)
    cloud_client = TestClient(cloud_app)
    cloud_info = cloud_client.get("/api/hub/info").json()
    assert cloud_info["mode"] == "cloud"
    assert cloud_client.get("/healthz").json()["mode"] == "cloud"


def test_online_tenant_backup_script_copies_auth_and_user_dbs(tmp_path: Path) -> None:
    cloud_root = tmp_path / "cloud"
    store = CloudAuthStore(cloud_root / "auth.db")
    tenants = TenantManager(cloud_root)

    _, code_a = store.start_registration("a@example.com", "strong-password-alpha")
    assert code_a is not None
    reg_a = store.verify_registration("a@example.com", code_a, computer_name="PC-A")
    tenants.provision_user_db(reg_a["user"]["id"])

    _, code_b = store.start_registration("b@example.com", "strong-password-bravo")
    assert code_b is not None
    reg_b = store.verify_registration("b@example.com", code_b, computer_name="PC-B")
    tenants.provision_user_db(reg_b["user"]["id"])

    spec = importlib.util.spec_from_file_location("backup_tenants", DEPLOY_CLOUD / "backup_tenants.py")
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    out_root = tmp_path / "backups"
    summary = mod.backup_cloud_root(cloud_root, out_root)
    assert summary["authBackedUp"] is True
    assert summary["tenantsBackedUp"] == 2
    snap = Path(summary["backupDir"])
    assert (snap / "auth.db").is_file()
    assert (snap / "users" / reg_a["user"]["id"] / "spend.db").is_file()
    assert (snap / "users" / reg_b["user"]["id"] / "spend.db").is_file()
