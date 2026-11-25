"""Approved display labels for activity shared with a paired hub."""

from pathlib import Path

from spend_app.db import connect


ACTIVITY_TOOLS = {
    "antigravity": "Antigravity",
    "claude": "Claude Code",
    "codex": "Codex",
    "grok": "Grok Build",
    "opencode": "OpenCode",
    "traycer": "Traycer",
    "zcode": "ZCode",
}


def synced_activity_name(identifier: str | None) -> str:
    if not identifier:
        return "Coding tool"
    raw_id = identifier.split(":", 2)[-1] if identifier.startswith("m:") else identifier
    return ACTIVITY_TOOLS.get(raw_id.partition(":")[0], "Coding tool")


def scrub_synced_activity_titles(path: Path) -> None:
    """Replace historical imported titles without modifying local activity."""
    marker = "sync.activity_tool_labels.v1"
    with connect(path) as connection:
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        connection.execute("BEGIN IMMEDIATE")
        if connection.execute("SELECT 1 FROM app_meta WHERE key=?", (marker,)).fetchone():
            return
        rows = connection.execute("SELECT id, name FROM agent_runs WHERE id GLOB 'm:*'").fetchall()
        connection.executemany(
            "UPDATE agent_runs SET name=? WHERE id=?",
            ((label, row["id"]) for row in rows if (label := synced_activity_name(row["id"])) != row["name"]),
        )
        connection.execute("INSERT INTO app_meta(key,value) VALUES(?, '1')", (marker,))
