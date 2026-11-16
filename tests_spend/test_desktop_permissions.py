"""Desktop IPC permissions: remote origins may call only explicit HUD/window commands.

The HUD and dashboard pages come from the local service, which Tauri treats
as a remote origin, so an app command answers them only when build.rs lists
the command, lib.rs registers it in generate_handler![], and
capabilities/default.json allows it. Update installation is restricted to the
native tray menu in Rust so remote loopback origins cannot query or trigger
updates over Tauri IPC.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from spend_app.hud import render_hud

SRC_TAURI = Path(__file__).resolve().parents[1] / "src-tauri"
DISALLOWED_REMOTE_COMMANDS = {"pending_update", "install_update"}


def _manifest_commands() -> set[str]:
    build = (SRC_TAURI / "build.rs").read_text(encoding="utf-8")
    listed = re.search(r"const COMMANDS: &\[&str\] = &\[(.*?)\];", build, re.S)
    assert listed, "build.rs must declare its app commands"
    assert ".app_manifest(" in build and ".commands(COMMANDS)" in build
    return set(re.findall(r'"([a-z_]+)"', listed.group(1)))


def _handler_commands() -> set[str]:
    lib = (SRC_TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    handler = re.search(r"generate_handler!\[(.*?)\]", lib, re.S)
    assert handler
    return set(re.findall(r"[a-z_]+", handler.group(1)))


def test_remote_ipc_commands_match_manifest_and_exclude_updates() -> None:
    capability = json.loads((SRC_TAURI / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert "hud" in capability["windows"]
    assert "main" in capability["windows"]
    assert any(url.startswith("http://127.0.0.1:") for url in capability["remote"]["urls"])
    assert any(url.startswith("http://localhost:") for url in capability["remote"]["urls"])

    manifest = _manifest_commands()
    handlers = _handler_commands()
    permissions = set(capability["permissions"])

    assert DISALLOWED_REMOTE_COMMANDS.isdisjoint(manifest)
    assert DISALLOWED_REMOTE_COMMANDS.isdisjoint(handlers)
    for command in DISALLOWED_REMOTE_COMMANDS:
        assert "allow-" + command.replace("_", "-") not in permissions, command

    assert manifest == handlers
    for command in manifest:
        assert "allow-" + command.replace("_", "-") in permissions, command


def test_updates_remain_tray_internal_and_absent_from_hud() -> None:
    lib = (SRC_TAURI / "src" / "lib.rs").read_text(encoding="utf-8")
    assert "async fn install_update(app: AppHandle)" in lib
    assert "install_update(app).await" in lib
    assert not re.search(
        r"#\[tauri::command\]\s*(?:pub\s+)?(?:async\s+)?fn\s+(?:install_update|pending_update)\b",
        lib,
    )

    for dashboard in (False, True):
        html = render_hud(dashboard=dashboard)
        assert "install_update" not in html
        assert "pending_update" not in html
        assert 'id="hud-update"' not in html
