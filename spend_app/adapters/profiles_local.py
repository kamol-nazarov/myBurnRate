"""Collect usage from enabled non-default provider profiles.

The default collectors (``key == "default"``) run on the registry pass and read
only each provider's default profile. This step runs each enabled non-default
profile through its provider's own adapter ``ingest()``, confined to that
profile's location and tagged with the profile's ``tool_key`` via
``TOOL_KEY_OVERRIDES`` so per-card burn, run-out forecasts and the Hub can tell
profiles apart while model keys and prices remain unchanged.

Runs at the start of a pass, before the default collectors, so each default
source's own run stays its latest health record.
"""

from __future__ import annotations

from pathlib import Path

NAME = "profiles_local"


def _profile_folder_and_patterns(
    provider: str, path: Path, meta
) -> tuple[str, list[str] | None, str | Path]:
    from spend_app.connection_paths import transcript_patterns

    if path.is_file():
        folder = str(path.parent)
        patterns = [path.name] if meta.argument == "session_glob" else None
        arg_value: str | Path = (
            str(path) if meta.argument in {"session_glob", "database_glob"} else path
        )
        return folder, patterns, arg_value

    folder = str(path)
    if provider in {"codex_local", "claude_local"}:
        patterns = transcript_patterns(provider, path, meta.suffix or "**/*.jsonl")
    elif provider == "muse_local":
        patterns = (
            ["sessions/**/*.jsonl"] if (path / "sessions").is_dir() else ["**/*.jsonl"]
        )
    else:
        patterns = None

    if meta.suffix and meta.argument != "import_path":
        arg_value = str(path / meta.suffix)
    elif meta.argument in {"session_glob", "database_glob"}:
        arg_value = str(path)
    else:
        arg_value = path
    return folder, patterns, arg_value


def ingest(*, database_path: Path, pricing) -> dict:
    from spend_app.adapters.file_cache import merge_results
    from spend_app.connection_paths import MissingLocation, approved, normalize
    from spend_app.db import TOOL_KEY_OVERRIDES
    from spend_app.profile_store import (
        DEFAULT_TOOL_KEYS,
        enabled_profiles,
        supports_profiles,
    )
    from spend_app.providers import REGISTRY

    results: list[dict] = []
    for profile in enabled_profiles(database_path):
        if profile.key == "default" or not supports_profiles(profile.provider):
            continue
        if not profile.location or not profile.tool_key:
            continue
        spec = REGISTRY.get(profile.provider)
        if spec is None or spec.ingest is None or spec.connection is None:
            continue
        default_tool_key = DEFAULT_TOOL_KEYS.get(profile.provider)
        if not default_tool_key:
            continue

        try:
            normalized = normalize(profile.location, spec.connection)
            folder, patterns, arg_value = _profile_folder_and_patterns(
                profile.provider, Path(normalized), spec.connection
            )
            kwargs = {
                "database_path": database_path,
                "pricing": pricing,
                spec.connection.argument: arg_value,
            }
            token = TOOL_KEY_OVERRIDES.set({default_tool_key: profile.tool_key})
            try:
                with approved(folder, profile.revision, patterns):
                    results.append(spec.ingest(**kwargs))
            finally:
                TOOL_KEY_OVERRIDES.reset(token)
        except MissingLocation:
            continue
        except Exception as exc:
            results.append(
                {
                    "source": spec.key,
                    "status": "failed",
                    "eventsSeen": 0,
                    "eventsWritten": 0,
                    "unpricedEventsWritten": 0,
                    "issues": [f"{profile.key}:{type(exc).__name__}"],
                }
            )

    active = [result for result in results if result.get("status") != "skipped"]
    if not active:
        # Nothing to read: no run is recorded for a step that is not a source.
        return {"status": "skipped", "eventsWritten": 0, "unpricedEventsWritten": 0}
    return merge_results(active)
