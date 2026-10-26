"""Background commands never open a window on Windows.

The installed service has no console (PyInstaller ``console=False``), so a
console program it starts without CREATE_NO_WINDOW opens a visible terminal
window that flashes and closes. ``tailscale serve status`` did that on every
setup-state read. Every subprocess call in code the service runs must pass
``creationflags`` or ``startupinfo``.
"""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1] / "spend_app"
SPAWN = {"run", "Popen", "check_output", "check_call", "call"}
# The command line, and `burnrate hub update` it runs, work in the user's own terminal.
EXEMPT_FILES = {"cli.py", "hub_update.py"}
# POSIX-only process listings (the Windows branch beside each is checked).
POSIX_ONLY_COMMANDS = {"ps"}


def _is_subprocess_attr(node: ast.AST | None) -> bool:
    return isinstance(node, ast.Attribute) and node.attr in SPAWN and isinstance(node.value, ast.Name) \
        and node.value.id == "subprocess"


def _injected_runners(function: ast.FunctionDef) -> set[str]:
    """Parameters that default to a subprocess function, like ``run=subprocess.run``."""
    arguments = function.args
    positional = arguments.posonlyargs + arguments.args
    pairs = list(zip(positional[len(positional) - len(arguments.defaults):], arguments.defaults))
    pairs += [(arg, default) for arg, default in zip(arguments.kwonlyargs, arguments.kw_defaults) if default]
    return {arg.arg for arg, default in pairs if _is_subprocess_attr(default)}


def _spawn_calls(path: Path):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and _is_subprocess_attr(node.func):
            yield node
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            runners = _injected_runners(node)
            for call in ast.walk(node):
                if isinstance(call, ast.Call) and isinstance(call.func, ast.Name) and call.func.id in runners:
                    yield call


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


def test_every_shell_out_in_the_scan_is_found() -> None:
    # The scan itself must see both call shapes, or a clean result means nothing.
    found = {call.lineno for call in _spawn_calls(ROOT / "notify.py")} | \
        {call.lineno for call in _spawn_calls(ROOT / "watchdog.py")}
    assert len(found) >= 2


@pytest.mark.skipif(os.name != "nt", reason="the in-process process list is Windows-only")
def test_process_discovery_starts_no_program(monkeypatch) -> None:
    # Antigravity discovery runs on every collection pass; it once started
    # PowerShell each time (about every six seconds).
    from spend_app import platform_paths

    def refuse(*args, **kwargs):
        raise AssertionError(f"started a program: {args[0] if args else kwargs}")

    monkeypatch.setattr(platform_paths.subprocess, "run", refuse)
    monkeypatch.setattr(platform_paths.subprocess, "Popen", refuse)
    own_image = Path(sys.executable).stem
    assert platform_paths.process_running(own_image) is True
    assert platform_paths.process_running("no-such-program-burnrate") is False
    lines = platform_paths.process_command_lines(own_image)
    assert any(str(os.getpid()) in line or own_image.lower() in line.lower() for line in lines)


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
