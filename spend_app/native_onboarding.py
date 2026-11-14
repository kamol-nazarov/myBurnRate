"""First-run data for the native window's onboarding screen.

Combines the service's ``/api/connections`` rows with a read-only look at the
three headline command-line tools. The look is existence-only: whether the
program is on ``PATH`` and whether its default data folder exists. No file under
those folders is opened, listed or read, and no credential location is touched.
"""

from __future__ import annotations

import os
import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

#: Connection states in which BURNRATE is already collecting from a source.
ACTIVE_STATES = frozenset(
    {"already_monitored", "awaiting_ingest", "waiting_activity", "receiving_usage"}
)
#: States in which an API (balance) source holds a working credential.
API_CONNECTED_STATES = frozenset(
    {
        "awaiting_ingest",
        "waiting_activity",
        "receiving_usage",
        "balance_available",
        "connected",
    }
)
STATE_LABELS = {
    "already_monitored": "Monitored on this PC",
    "awaiting_ingest": "Connected, waiting for the first import",
    "waiting_activity": "Connected, waiting for activity",
    "receiving_usage": "Receiving usage",
}

#: (connection source, display name, executables, data-folder resolver)
HEADLINE_TOOLS = (
    ("claude_local", "Claude Code", ("claude",), "claude"),
    ("codex_local", "Codex", ("codex",), "codex"),
    ("opencode_local", "OpenCode", ("opencode",), "opencode"),
)

_BRANDS = {
    "claude_local": "claude",
    "claude_local_alt": "claude",
    "codex_local": "codex",
    "opencode_local": "opencode-go",
    "zcode_local": "zcode",
    "grok_local": "grok",
    "muse_local": "muse",
    "antigravity_local": "antigravity",
    "cursor_local": "cursor",
    "traycer_local": "traycer",
    "openrouter": "openrouter",
    "deepseek": "deepseek",
    "openai_admin": "openai",
    "anthropic_admin": "anthropic",
}


def brand_for(source: str) -> str:
    """The logo key the page's ``resolveBrand`` understands for ``source``."""
    key = str(source or "").lower().strip()
    if key in _BRANDS:
        return _BRANDS[key]
    for needle, brand in (
        ("deepseek", "deepseek"),
        ("anthropic", "anthropic"),
        ("claude", "claude"),
        ("openai", "openai"),
        ("codex", "codex"),
        ("opencode", "opencode-go"),
        ("muse", "muse"),
        ("grok", "grok"),
        ("zcode", "zcode"),
        ("antigravity", "antigravity"),
        ("cursor", "cursor"),
        ("traycer", "traycer"),
        ("gemini", "gemini"),
        ("openrouter", "openrouter"),
    ):
        if needle in key:
            return brand
    return "custom"


def _data_folder(kind: str, env: Mapping[str, str], home: Path) -> Path:
    if kind == "claude":
        return Path(env.get("CLAUDE_CONFIG_DIR") or home / ".claude")
    if kind == "codex":
        return Path(env.get("CODEX_HOME") or home / ".codex")
    base = env.get("XDG_DATA_HOME") or home / ".local" / "share"
    return Path(base) / "opencode"


def probe_tools(
    *,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict[str, bool]:
    """``{source: detected}`` from PATH and default-folder existence only."""
    env = os.environ if env is None else env
    home = home or Path.home()
    found: dict[str, bool] = {}
    for source, _name, programs, kind in HEADLINE_TOOLS:
        on_path = any(which(program) for program in programs)
        try:
            has_folder = _data_folder(kind, env, home).is_dir()
        except OSError:
            has_folder = False
        found[source] = bool(on_path or has_folder)
    return found


def _tool_detail(row: dict | None, detected: bool, active: bool) -> str:
    state = str((row or {}).get("state") or "")
    if active:
        return STATE_LABELS.get(state, "Collecting usage")
    if state == "needs_attention":
        return str(row.get("detail") or "Needs attention")
    if state == "disabled":
        return "Disconnected. Existing history is kept."
    if state == "externally_managed":
        return "Configured outside BURNRATE"
    if detected:
        return "Found on this PC, not connected yet"
    return "Not found on this PC"


def demo_onboarding_data() -> dict:
    """Representative sample data for ``--demo``; nothing on this PC is probed."""
    tools = [
        ("claude_local", "Claude Code", True, True),
        ("codex_local", "Codex", True, True),
        ("opencode_local", "OpenCode", True, False),
    ]
    apis = [
        ("openrouter", "OpenRouter balance", True),
        ("deepseek", "DeepSeek balance", True),
        ("openai_admin", "OpenAI Admin", False),
        ("anthropic_admin", "Anthropic Admin", False),
    ]
    return {
        "ok": True,
        "tools": [
            {
                "source": source,
                "name": name,
                "brand": brand_for(source),
                "detected": detected,
                "active": active,
                "detail": _tool_detail(
                    {"state": "receiving_usage"} if active else None, detected, active
                ),
            }
            for source, name, detected, active in tools
        ],
        "apiSources": [
            {
                "source": source,
                "name": name,
                "brand": brand_for(source),
                "connected": connected,
                "credentialHelp": "Sample data: connections are disabled in the demo.",
                "revision": 0,
            }
            for source, name, connected in apis
        ],
        "hubConnected": True,
    }


def build_onboarding_data(
    client,
    *,
    demo: bool = False,
    which: Callable[[str], str | None] = shutil.which,
    env: Mapping[str, str] | None = None,
    home: Path | None = None,
) -> dict:
    """The ``get_onboarding_data`` payload. Never raises; a service failure
    still returns the local probe with ``ok`` False and an ``error``."""
    if demo:
        return demo_onboarding_data()
    detected = probe_tools(which=which, env=env, home=home)

    rows: list[dict] = []
    error = ""
    hub_connected = False
    try:
        data = client.request("/api/connections")
        rows = [r for r in (data.get("connections") or []) if isinstance(r, dict)]
    except Exception as exc:  # noqa: BLE001 -- surfaced to the page as text
        error = str(exc) or "Could not read connections."
    if not error:
        try:
            setup = client.request("/api/setup/state").get("setup") or {}
            spoke = setup.get("spokeConfig") or {}
            hub_connected = bool(spoke.get("enabled") and spoke.get("hubUrl"))
        except Exception:  # noqa: BLE001 -- the hub flag alone degrades to False
            hub_connected = False

    by_source = {str(r.get("source")): r for r in rows}
    tools = []
    for source, name, _programs, _kind in HEADLINE_TOOLS:
        row = by_source.get(source)
        active = bool(row and str(row.get("state") or "") in ACTIVE_STATES)
        is_detected = bool(detected.get(source) or active)
        tools.append(
            {
                "source": source,
                "name": name,
                "brand": brand_for(source),
                "detected": is_detected,
                "active": active,
                "detail": _tool_detail(row, is_detected, active),
            }
        )

    api_sources = [
        {
            "source": str(row.get("source")),
            "name": str(row.get("name") or row.get("source")),
            "brand": brand_for(str(row.get("source"))),
            "connected": str(row.get("state") or "") in API_CONNECTED_STATES,
            "credentialHelp": str(row.get("credentialHelp") or ""),
            "revision": int(row.get("revision") or 0),
        }
        for row in rows
        if row.get("kind") == "api"
    ]
    payload = {
        "ok": not error,
        "tools": tools,
        "apiSources": api_sources,
        "hubConnected": hub_connected,
    }
    if error:
        payload["error"] = error
    return payload
