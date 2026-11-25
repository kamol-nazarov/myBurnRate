import json
import os
from pathlib import Path
import stat
import urllib.request

from spend_app import native_launch, watchdog
from spend_app.child_environment import child_environment
from spend_app.db import backup_database, connect, initialize
from spend_app.redact import redact
from spend_app.setup_wizard import (
    SETUP_META_KEY,
    default_setup_state,
    write_setup_state,
)
from spend_app.sync import migrate_legacy_spoke_secrets
from spend_app.tab_window import TabBridge
from spend_app.wealthfront_window import DesktopBridge


def test_window_policy_and_bridge_contract():
    module = Path(__file__).resolve().parents[1] / "spend_app"
    for name in ("wealthfront_ui.html", "tab_ui.html"):
        html = (module / name).read_text(encoding="utf-8")
        head = html.split("<script>", 1)[0]
        assert 'http-equiv="Content-Security-Policy"' in head
        assert "default-src 'none'" in head and "connect-src 'none'" in head
        assert "img-src data:" in head and "font-src data:" in head
    helpers = {
        "attach",
        "attach_host",
        "attach_surfaces",
        "set_tray_available",
        "report_effective_theme",
        "service_owns_plan_alerts",
        "fetch_alert_providers",
    }
    assert not any(hasattr(DesktopBridge, helper) for helper in helpers)
    assert hasattr(DesktopBridge, "publish_effective_theme")
    assert not hasattr(TabBridge, "attach_controller") and not hasattr(
        TabBridge, "build_payload"
    )


def test_signing_enrollment_blocks_other_publishers(monkeypatch, tmp_path):
    monkeypatch.setattr(native_launch, "DEFAULT_THUMBPRINTS", ("ad" * 20,))
    monkeypatch.delenv(native_launch.REQUIRE_SIGNED_ENV, raising=False)
    import itertools

    statuses = itertools.cycle(
        (("Valid", "ac" * 20, "fixture"), ("Valid", "ad" * 20, "fixture"))
    )
    monkeypatch.setattr(native_launch, "_authenticode", lambda _: next(statuses))
    path = tmp_path / "fixture.exe"
    assert native_launch.evaluate(path) == (False, native_launch.FOREIGN)
    assert native_launch.evaluate(path) == (True, native_launch.VALID)


def test_customary_credential_formats_are_removed():
    # Construct synthetic placeholders at run time to keep credentials out of source.
    samples = [
        ("AI" + "za") + "0" * 35,
        "ts" + "key-" + "synthetic-only",
        "0" * 32 + "." + "synthetic-only",
    ]
    for value in samples:
        assert redact("redaction fixture " + value) == "redaction fixture [redacted]"
    assert (
        redact("https://example-user:fixture:password@example.invalid/a")
        == "https://[redacted]@example.invalid/a"
    )


def test_system_child_does_not_inherit_application_secrets(monkeypatch):
    for name in ("BURNRATE_ACCESS_TOKEN", "OPENAI_API_KEY", "BURNRATE_SETUP_TOKEN"):
        monkeypatch.setenv(name, "synthetic")
    passed = child_environment(TRAYCER_AGENT_ID="fixture")
    assert passed["TRAYCER_AGENT_ID"] == "fixture"
    assert (
        not {"BURNRATE_ACCESS_TOKEN", "OPENAI_API_KEY", "BURNRATE_SETUP_TOKEN"}
        & passed.keys()
    )


def test_watchdog_redirect_handler_declines_forwarding():
    request = urllib.request.Request(
        "http://127.0.0.1/", headers={"Authorization": "Bearer synthetic"}
    )
    assert (
        watchdog._NoRedirect().redirect_request(
            request, None, 302, "Found", {}, "https://example.invalid/"
        )
        is None
    )


def test_vault_failure_leaves_no_plaintext_and_requires_login(tmp_path):
    path = tmp_path / "source.db"
    initialize(path)
    state = default_setup_state()
    state["role"] = "spoke"
    state["spokeConfig"].update(
        token="synthetic-machine", ownerSession="synthetic-owner"
    )
    with connect(path) as conn:
        write_setup_state(conn, state)
    assert migrate_legacy_spoke_secrets(path, vault=object()) == {
        "token": False,
        "ownerSession": False,
    }
    with connect(path) as conn:
        result = json.loads(
            conn.execute(
                "SELECT value FROM app_meta WHERE key=?", (SETUP_META_KEY,)
            ).fetchone()[0]
        )
    assert result["spokeConfig"]["signedOut"] is True
    assert (
        "token" not in result["spokeConfig"]
        and "ownerSession" not in result["spokeConfig"]
    )
    output = backup_database(path, tmp_path / "backup.db")
    if os.name != "nt":
        assert stat.S_IMODE(output.stat().st_mode) == 0o600
