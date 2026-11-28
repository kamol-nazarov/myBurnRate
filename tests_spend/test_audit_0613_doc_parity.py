"""Durable doc-vs-code parity tests for Phase 6 (D-14, D-05/D-12/D-15 docs)."""
from __future__ import annotations

import logging
from pathlib import Path

from spend_app import exposure

ROOT = Path(__file__).resolve().parents[1]


def test_d14_exposure_warning_no_longer_claims_registration_needs_no_setup_token(caplog):
    with caplog.at_level(logging.WARNING, logger="burnrate.exposure"):
        exposure.validate_bind_host("203.0.113.10", central=True, allow_public=True)
        exposure.validate_publish_host("203.0.113.10", allow_public=True)
    joined = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "requires no setup token" not in joined
    assert "BURNRATE_SETUP_TOKEN" in joined


def test_d14_privacy_and_security_docs_match_0613_behavior():
    privacy = (ROOT / "docs" / "privacy.md").read_text(encoding="utf-8")
    security = (ROOT / "SECURITY.md").read_text(encoding="utf-8")

    # Stale claim removed
    assert "`GET /api/spend/limits` can fire every quota collector" not in privacy
    assert "persisted quota snapshots" in privacy

    # Required disclosures present
    for token in (
        "glm-acp-agent/credentials.json",
        "Z_AI_API_KEY",
        "POST /muse-code/key",
        "BURNRATE_ENABLE_TRAYCER_CLI",
        "BURNRATE_HIDE_LOCAL_TITLES",
        "~/.codex*/auth.json",
        "burnrate wipe --credentials --data --yes",
        "burnrate-wv2-sessions",
        "PRAGMA secure_delete = ON",
        "burnrate hub delete-owner",
    ):
        assert token in privacy, f"Missing disclosure in docs/privacy.md: {token}"

    for token in (
        "BURNRATE_ENABLE_TRAYCER_CLI=0",
        "BURNRATE_HIDE_LOCAL_TITLES=1",
        "HMAC-SHA256",
        "recursively applies private",
    ):
        assert token in security, f"Missing security statement in SECURITY.md: {token}"
