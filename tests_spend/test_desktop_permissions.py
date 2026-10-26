"""The HUD may call the desktop app's update commands.

The HUD page comes from the local service, which Tauri treats as a remote
origin, so an app command answers it only when build.rs lists the command and
capabilities/default.json allows it. 0.5.0 and 0.5.1 shipped without either:
the HUD offered an update, and every click was refused with "Command
install_update not allowed by ACL".
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SRC_TAURI = Path(__file__).resolve().parents[1] / "src-tauri"
HUD_COMMANDS = {"pending_update", "install_update"}


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


def test_hud_update_commands_are_allowed_for_the_service_origin() -> None:
    capability = json.loads((SRC_TAURI / "capabilities" / "default.json").read_text(encoding="utf-8"))
    assert "hud" in capability["windows"]
    assert any(url.startswith("http://127.0.0.1:") for url in capability["remote"]["urls"])
    manifest = _manifest_commands()
    assert HUD_COMMANDS <= manifest
    assert manifest <= _handler_commands()
    for command in manifest:
        assert "allow-" + command.replace("_", "-") in capability["permissions"], command
