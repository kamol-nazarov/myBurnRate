"""Background commands never open a window on Windows.

The installed service has no console (PyInstaller ``console=False``), so a
console program it starts without CREATE_NO_WINDOW opens a visible terminal
window that flashes and closes. ``tailscale serve status`` did that on every
setup-state read. Every subprocess call in code the service runs must pass
``creationflags`` or ``startupinfo``.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "spend_app"
SPAWN = {"run", "Popen", "check_output", "check_call", "call"}
# The command line runs in the user's own terminal.
EXEMPT_FILES = {"cli.py"}
# POSIX-only process listings (the Windows branch beside each is checked).
POSIX_ONLY_COMMANDS = {"ps"}


def _spawn_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in SPAWN and isinstance(func.value, ast.Name) \
                and func.value.id == "subprocess":
            yield node


def _command_name(call: ast.Call) -> str | None:
    if call.args and isinstance(call.args[0], ast.List) and call.args[0].elts:
        first = call.args[0].elts[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            return first.value
    return None


def test_every_background_command_is_started_without_a_window() -> None:
    missing = []
    for path in sorted(ROOT.rglob("*.py")):
        if path.name in EXEMPT_FILES:
            continue
        for call in _spawn_calls(path):
            if _command_name(call) in POSIX_ONLY_COMMANDS:
                continue
            keywords = {keyword.arg for keyword in call.keywords}
            if not keywords & {"creationflags", "startupinfo"}:
                missing.append(f"{path.relative_to(ROOT.parent)}:{call.lineno}")
    assert not missing, "subprocess calls that would flash a window on Windows: " + ", ".join(missing)


@pytest.mark.parametrize("call", ["status", "serve"])
def test_tailscale_runs_without_a_window(monkeypatch, call: str) -> None:
    from spend_app import setup_wizard
    from spend_app.platform_paths import NO_WINDOW

    seen = []

    class Done:
        returncode = 1
        stdout = ""

    monkeypatch.setattr(setup_wizard.shutil, "which", lambda name: "tailscale")
    monkeypatch.setattr(setup_wizard.subprocess, "run", lambda *a, **k: seen.append(k) or Done())
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    if call == "status":
        setup_wizard.detect_network_addresses(17331)
    else:
        setup_wizard.ensure_tailscale_hub_proxy(17331)
    assert seen and all(kwargs.get("creationflags") == NO_WINDOW for kwargs in seen)
