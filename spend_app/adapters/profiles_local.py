"""Usage from extra Claude Code accounts and OpenCode profiles.

The default collectors read the default profile only. This step runs the same
collectors on each extra profile's own transcripts or database, confined to
that profile's folder. Its rows carry the profile's card key as their tool
(``claude-code-work``, ``opencode-go-2``) so per-card burn, run-out forecasts
and the Hub can tell them apart; model keys, and so prices, are unchanged.

Runs at the start of a pass, before the default collectors, so each default
source's own run stays its latest health record.
"""

from __future__ import annotations

from pathlib import Path

NAME = "profiles_local"


def ingest(*, database_path: Path, pricing) -> dict:
    from spend_app.adapters import claude_local, opencode_local
    from spend_app.adapters.file_cache import merge_results
    from spend_app.claude_accounts import extra_accounts
    from spend_app.connection_paths import approved
    from spend_app.db import TOOL_KEY_OVERRIDES
    from spend_app.opencode_profiles import extra_profiles

    results = []
    for account in extra_accounts():
        projects = account.config_dir / "projects"
        if not projects.is_dir() or next(projects.glob("*/*.jsonl"), None) is None:
            continue  # this account has no transcripts yet
        token = TOOL_KEY_OVERRIDES.set({"claude-code": account.key})
        try:
            with approved(str(projects)):
                results.append(claude_local.ingest(database_path=database_path, pricing=pricing,
                                                   session_glob=str(projects / "**" / "*.jsonl")))
        finally:
            TOOL_KEY_OVERRIDES.reset(token)
    for profile in extra_profiles():
        token = TOOL_KEY_OVERRIDES.set({"opencode-go": profile.key})
        try:
            with approved(str(profile.database.parent)):
                results.append(opencode_local.ingest(database_path=database_path, pricing=pricing,
                                                     source_database=profile.database))
        finally:
            TOOL_KEY_OVERRIDES.reset(token)
    if not results:
        # Nothing to read: no run is recorded for a step that is not a source.
        return {"status": "skipped", "eventsWritten": 0, "unpricedEventsWritten": 0}
    return merge_results(results)
