from __future__ import annotations

import importlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from dotenv import dotenv_values, load_dotenv


ROOT = Path(__file__).resolve().parents[1]
APP_DIR_NAME = "BURNRATE"
BUILTIN_HUB_MODULE = "spend_app._build_config"
HUB_URL_ENV = "BURNRATE_HUB_URL"
_SECURITY_ENV = {
    HUB_URL_ENV, "BURNRATE_ALLOWED_HOSTS", "BURNRATE_ALLOWED_ORIGINS",
    "BURNRATE_ALLOW_ADMIN_BEARER", "BURNRATE_TRUSTED_PROXIES", "SPEND_DATABASE_PATH",
    "BURNRATE_REQUIRE_SIGNED_NATIVE", "BURNRATE_ACCESS_TOKEN", "BURNRATE_SETUP_TOKEN",
    "BURNRATE_CENTRAL_SERVER", "BURNRATE_DATA_ROOT", "BURNRATE_TEST_HOSTS",
}

LOCAL_INGEST_INTERVAL_SECONDS = 15
ADMIN_INGEST_INTERVAL_MINUTES = 15
# Backward-compatible name for callers that only configure remote/admin cadence.
INGEST_INTERVAL_MINUTES = ADMIN_INGEST_INTERVAL_MINUTES
# Quota job tick. Each lane decides whether it is due on a tick (see
# spend_app.quotas.LANE_CADENCE); external endpoints are never called on
# every tick.
QUOTA_POLL_SECONDS = 15
ACTIVITY_POLL_SECONDS = 4


def local_timezone_name() -> str:
    """IANA name of this computer's timezone, or "UTC" when undetectable.

    Every "today" surface counts since local midnight, so an unset
    SPEND_TIMEZONE must resolve to the machine's own zone. Defaulting to
    UTC instead shifted the day boundary by the user's offset: an evening
    in New York showed only the usage after 00:00Z (20:00 local).
    """
    try:
        from tzlocal import get_localzone_name

        name = get_localzone_name()
    except (ImportError, KeyError, OSError, RuntimeError, ValueError):
        return "UTC"
    if not isinstance(name, str) or not name:
        return "UTC"
    try:
        ZoneInfo(name)
    except (KeyError, ValueError):
        return "UTC"
    return name


def default_timezone() -> str:
    """Configured IANA timezone: SPEND_TIMEZONE when set, else this machine's."""
    configured = os.getenv("SPEND_TIMEZONE", "").strip()
    return configured if configured else local_timezone_name()


def dashboard_enabled() -> bool:
    """The HUD is the product; the full dashboard returns with BURNRATE_ENABLE_DASHBOARD=1."""
    return os.getenv("BURNRATE_ENABLE_DASHBOARD", "").strip().lower() in {"1", "true", "yes"}


def demo_mode() -> bool:
    """``burnrate demo``: made-up data, and nothing on this computer is read or moved."""
    return os.getenv("BURNRATE_DEMO") == "1"


def local_app_data() -> Path:
    configured = os.getenv("LOCALAPPDATA")
    if configured:
        return Path(configured)
    if os.name == "nt":
        return Path.home() / "AppData" / "Local"
    xdg = os.getenv("XDG_DATA_HOME")
    if xdg:
        return Path(xdg)
    return Path.home() / ".local" / "share"


def default_app_dir() -> Path:
    """Where this computer's database, settings and logs live (BURNRATE_DATA_ROOT overrides)."""
    override = os.getenv("BURNRATE_DATA_ROOT", "").strip()
    return Path(override) if override else local_app_data() / APP_DIR_NAME


def default_database_path() -> Path:
    override = os.getenv("SPEND_DATABASE_PATH")
    if override:
        return Path(override)
    if os.getenv("BURNRATE_DEV") == "1":
        return ROOT / "data" / "spend" / "spend.db"
    return default_app_dir() / "spend.db"


def default_logs_path() -> Path:
    if os.getenv("BURNRATE_DEV") == "1":
        return ROOT / "logs"
    return default_app_dir() / "logs"


@dataclass(frozen=True)
class Settings:
    database_path: Path
    pricing_path: Path
    anthropic_admin_key: str | None
    openai_admin_key: str | None
    timezone: str
    cache_hit_threshold: float
    over_routing_token_ceiling: int
    local_ingest_interval_seconds: int = LOCAL_INGEST_INTERVAL_SECONDS
    admin_ingest_interval_minutes: int = ADMIN_INGEST_INTERVAL_MINUTES
    quota_poll_seconds: int = QUOTA_POLL_SECONDS
    activity_poll_seconds: int = ACTIVITY_POLL_SECONDS
    logs_path: Path = field(default_factory=default_logs_path)


def load_user_env() -> None:
    """Load per-user settings from the data root; called by the service and CLI entrypoints."""
    if "PYTEST_CURRENT_TEST" in os.environ:
        return
    env_file = default_app_dir() / ".env"
    if not env_file.exists():
        try:
            env_file.parent.mkdir(parents=True, exist_ok=True)
            env_file.write_text(
                "BURNRATE_ENABLE_ZAI_QUOTA=1\n"
                "BURNRATE_ENABLE_OPENCODE_GO_QUOTA=1\n"
                "BURNRATE_ENABLE_MUSE_QUOTA=1\n",
                encoding="utf-8",
            )
        except OSError:
            pass
    if os.name != "nt":
        try:
            os.chmod(env_file.parent, 0o700)
            os.chmod(env_file, 0o600)
        except OSError:
            pass
    load_dotenv(env_file, override=False)
    for key in (
        "BURNRATE_ENABLE_ZAI_QUOTA",
        "BURNRATE_ENABLE_OPENCODE_GO_QUOTA",
        "BURNRATE_ENABLE_MUSE_QUOTA",
    ):
        os.environ.setdefault(key, "1")


def builtin_hub_url() -> str:
    """Return the build-time hub origin from ``spend_app._build_config``, or ``""``."""
    try:
        mod = importlib.import_module(BUILTIN_HUB_MODULE)
    except ImportError:
        return ""
    raw = str(getattr(mod, "BUILTIN_HUB_URL", "") or "").strip()
    if not raw:
        return ""
    parsed = urlsplit(raw)
    if (
        parsed.scheme.lower() != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("Built-in hub address must be an https:// origin.")
    return f"https://{parsed.netloc.lower()}"


def resolve_hub_url() -> str:
    """Return the configured hub origin: ``BURNRATE_HUB_URL`` override, then built-in."""
    load_user_env()
    override = os.getenv(HUB_URL_ENV, "").strip()
    if override:
        from spend_app.hub_origin import hub_origin

        return hub_origin(override)
    return builtin_hub_url()


def load_settings(env_path: Path | None = None) -> Settings:
    load_user_env()
    for key, value in dotenv_values(env_path or ROOT / ".env").items():
        if value is not None and key.upper() not in _SECURITY_ENV:
            os.environ.setdefault(key, value)
    return Settings(
        database_path=default_database_path(),
        pricing_path=Path(os.getenv("SPEND_PRICING_PATH", ROOT / "pricing")),
        anthropic_admin_key=os.getenv("ANTHROPIC_ADMIN_KEY") or None,
        openai_admin_key=os.getenv("OPENAI_ADMIN_KEY") or None,
        timezone=default_timezone(),
        cache_hit_threshold=float(os.getenv("CACHE_HIT_THRESHOLD", "0.75")),
        over_routing_token_ceiling=int(os.getenv("OVER_ROUTING_TOKEN_CEILING", "40000")),
        local_ingest_interval_seconds=int(
            os.getenv(
                "SPEND_LOCAL_INGEST_INTERVAL_SECONDS",
                str(LOCAL_INGEST_INTERVAL_SECONDS),
            )
        ),
        admin_ingest_interval_minutes=int(
            os.getenv(
                "SPEND_ADMIN_INGEST_INTERVAL_MINUTES",
                os.getenv(
                    "SPEND_INGEST_INTERVAL_MINUTES",
                    str(ADMIN_INGEST_INTERVAL_MINUTES),
                ),
            )
        ),
        quota_poll_seconds=int(os.getenv("SPEND_QUOTA_POLL_SECONDS", str(QUOTA_POLL_SECONDS))),
        activity_poll_seconds=int(
            os.getenv("SPEND_ACTIVITY_POLL_SECONDS", str(ACTIVITY_POLL_SECONDS))
        ),
        logs_path=default_logs_path(),
    )
