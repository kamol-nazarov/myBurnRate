"""Display-only speed preference from the default Codex config on this PC."""

import tomllib
from pathlib import Path

from spend_app.providers import default_codex_glob

_MAX_CONFIG_BYTES = 256 * 1024
_SPEEDS = {
    "default": "standard",
    "standard": "standard",
    "priority": "fast",
    "fast": "fast",
    "ultrafast": "ultrafast",
}


def configured_codex_speed(connections: list[dict] | None) -> str | None:
    """Read the requested default only for the matching local source binding."""
    codex = next(
        (
            row
            for row in connections or []
            if isinstance(row, dict) and row.get("source") == "codex_local"
        ),
        None,
    )
    if not codex:
        return None
    profiles = codex.get("profiles")
    if not isinstance(profiles, list):
        return None
    profile = next(
        (
            row
            for row in profiles
            if isinstance(row, dict) and row.get("key") == "default"
        ),
        None,
    )
    if not profile or profile.get("state") in {"disabled", "not_found"}:
        return None
    location = profile.get("location")
    if not isinstance(location, str) or not location:
        return None
    try:
        sessions = Path(default_codex_glob()).parents[1].resolve()
        if Path(location).expanduser().resolve() != sessions:
            return None
        config = sessions.parent / "config.toml"
        if not config.is_file() or config.resolve() != config:
            return None
        with config.open("rb") as stream:
            raw = stream.read(_MAX_CONFIG_BYTES + 1)
        if len(raw) > _MAX_CONFIG_BYTES:
            return None
        settings = tomllib.loads(raw.decode("utf-8-sig"))
    except (OSError, ValueError, RuntimeError):
        return None
    tier = settings.get("service_tier")
    if settings.get("model_provider", "openai") != "openai" or not isinstance(
        tier, str
    ):
        return None
    speed = _SPEEDS.get(tier)
    features = settings.get("features")
    if speed and isinstance(features, dict) and features.get(f"{speed}_mode") is False:
        return None
    return speed
