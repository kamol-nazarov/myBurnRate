from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
import math
import os
from pathlib import Path
import threading
import time as time_module

from spend_app.db import (
    connect,
    initialize,
    upsert_agent_run,
    upsert_quota,
    utc_now,
)
from spend_app.integration_policy import PolicyDenied
from spend_app.limits import (
    _antigravity_active_sessions,
    _antigravity_limits_uncached,
    _cached,
    _claude_active_sessions,
    _claude_desktop_limits_uncached,
    _claude_from_traycer_result,
    _claude_limits_uncached,
    _codex_active_sessions,
    _codex_limits,
    _deepseek_balance_uncached,
    _grok_active_sessions,
    _grok_limits_uncached,
    _muse_limits_uncached,
    _opencode_go_limits_uncached,
    _openrouter_credits_uncached,
    _traycer_activity,
    _traycer_profile_rate_limits,
    _zai_limits_uncached,
    _zcode_active_sessions,
)


UNAVAILABLE_UNIT = "unavailable"
LABEL_REASON_SEPARATOR = " \u2014 "
WEEKLY_MINUTES = 7 * 24 * 60
AGENT_LIVE_STATE = "live"
AGENT_NO_DATA_STATE = "no_data"
LIVE_STATE_ALIASES = frozenset({"live", "running"})
QUOTA_LIMIT_ORDER = {
    "claude-code": ("5h", "weekly"),
    "antigravity": ("gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"),
    "opencode": ("5h", "weekly"),
    "muse": ("5h", "weekly"),
    "opencode-go": ("5h", "weekly", "monthly"),
    "openrouter": ("balance",),
}


def _limit_order(provider_key: str) -> tuple[str, ...]:
    # Extra Claude account and OpenCode profile lanes order their windows like the default lane.
    if provider_key not in QUOTA_LIMIT_ORDER:
        for prefix, default in (("claude-code-", "claude-code"), ("opencode-go-", "opencode-go")):
            if str(provider_key).startswith(prefix):
                return QUOTA_LIMIT_ORDER[default]
    return QUOTA_LIMIT_ORDER.get(provider_key, ())


PRESSURE_GREEN = "#63c689"
PRESSURE_BLUE = "#78a8f8"
PRESSURE_AMBER = "#d9a441"
PRESSURE_RED = "#dc6c78"

PROVIDER_SOURCES = {
    "codex": "codex_local_telemetry",
    "claude-code": "traycer_profile",
    "grok": "traycer_profile",
    "opencode": "zai_quota_endpoint",
    "muse": "muse_quota_endpoint",
    "opencode-go": "opencode_go_usage_endpoint",
    "openrouter": "openrouter_credits_api",
    "deepseek": "deepseek_balance_api",
    "antigravity": "antigravity_local_rpc",
}
# Lanes that report an account balance from a key connected in BURNRATE.
BALANCE_SOURCES = ("openrouter", "deepseek")

REQUIRED_LIMITS: dict[str, tuple[tuple[str, str], ...]] = {
    "codex": (("weekly", "Codex weekly window"),),
    "claude-code": (("5h", "Claude 5-hour window"), ("weekly", "Claude weekly window")),
    "grok": (("weekly", "Grok Build weekly"),),
    "opencode": (("5h", "Z.AI 5-hour credits"), ("weekly", "Z.AI weekly credits")),
    "muse": (("5h", "Muse Code 5-hour window"), ("weekly", "Muse Code weekly window")),
    "openrouter": (("balance", "OpenRouter funds remaining"),),
    "antigravity": (
        ("gemini-weekly", "Antigravity Gemini models \u00b7 Weekly remaining"),
        ("gemini-5h", "Antigravity Gemini models \u00b7 Five-hour remaining"),
        ("3p-weekly", "Antigravity Claude/GPT models \u00b7 Weekly remaining"),
        ("3p-5h", "Antigravity Claude/GPT models \u00b7 Five-hour remaining"),
    ),
}


@dataclass(frozen=True)
class QuotaSample:
    provider_key: str
    limit_key: str
    label: str
    unit: str
    source: str
    used: float | None = None
    allowance: float | None = None
    pct: float | None = None
    resets_at: str | None = None
    is_payg: bool | None = None
    reason: str | None = None
    # Set when the provider answered 429: seconds the lane must wait (from
    # Retry-After when given). Never persisted; drives the lane back-off.
    throttle_seconds: float | None = None
    observed_at: str | None = None
    pool_id: str | None = None
    scope: str | None = None


# Per-lane cadence in seconds as (active, idle). A lane is "active" while its
# tool produced a usage event within QUOTA_ACTIVITY_WINDOW_SECONDS. Local
# lanes cost nothing and refresh every tick; external endpoints publish no
# rate limits, so the active cadence caps them at 120 calls per hour and the
# idle cadence at 12.
LANE_CADENCE: dict[str, tuple[int, int]] = {
    "codex": (15, 15),
    "openrouter": (60, 300),
    "deepseek": (60, 300),
    "claude-code": (90, 300),  # 40/h: the OAuth usage endpoint 429s near 120/h with a ~1 h lockout (2026-09-02)
    "opencode": (30, 300),
    "muse": (30, 300),
    "opencode-go": (30, 300),
    "grok": (30, 300),
    "antigravity": (90, 300),
}
QUOTA_ACTIVITY_WINDOW_SECONDS = 300
THROTTLE_BACKOFF_MIN_SECONDS = 60
THROTTLE_BACKOFF_MAX_SECONDS = 3600  # long enough to honour a full-hour Retry-After
THROTTLED_STATUS = 429
# A poll whose readings could not be saved (database locked) is retried this
# soon instead of waiting out the lane's whole interval.
RETRY_AFTER_FAILED_SAVE_SECONDS = 60


class QuotaLaneScheduler:
    """Decides per lane whether a tick should poll it, with 429 back-off."""

    def __init__(self, cadence: dict[str, tuple[int, int]] | None = None, *, clock=None) -> None:
        self.cadence = dict(cadence or LANE_CADENCE)
        self._clock = clock or time_module.monotonic
        self._next_due: dict[str, float] = {}
        self._last_polled: dict[str, float] = {}
        self._backoff: dict[str, float] = {}
        self._lock = threading.Lock()

    def lane_cadence(self, provider_key: str) -> tuple[int, int]:
        from spend_app import claude_accounts, opencode_profiles

        if provider_key not in self.cadence:
            if claude_accounts.is_account_key(provider_key):
                return self.cadence.get(claude_accounts.PRIMARY_KEY, (90, 300))
            if opencode_profiles.is_profile_key(provider_key):
                return self.cadence.get(opencode_profiles.PRIMARY_KEY, (30, 300))
        return self.cadence.get(provider_key, (15, 15))

    def due(self, provider_key: str, *, active: bool = False) -> bool:
        with self._lock:
            now = self._clock()
            due_at = self._next_due.get(provider_key, float("-inf"))
            if active and provider_key not in self._backoff and provider_key in self._last_polled:
                active_interval, _ = self.lane_cadence(provider_key)
                due_at = min(due_at, self._last_polled[provider_key] + float(active_interval))
            return now >= due_at

    def backoff_seconds(self, provider_key: str) -> float:
        with self._lock:
            return self._backoff.get(provider_key, 0.0)

    def record(self, provider_key: str, samples: Sequence[QuotaSample], *, active: bool) -> None:
        active_interval, idle_interval = self.lane_cadence(provider_key)
        interval = float(active_interval if active else idle_interval)
        throttles = [s.throttle_seconds for s in samples if s.throttle_seconds is not None]
        with self._lock:
            now = self._clock()
            self._last_polled[provider_key] = now
            if throttles:
                previous = self._backoff.get(provider_key, 0.0)
                wait = max(max(throttles), previous * 2, THROTTLE_BACKOFF_MIN_SECONDS)
                wait = max(max(throttles), min(wait, THROTTLE_BACKOFF_MAX_SECONDS))
                self._backoff[provider_key] = wait
                self._next_due[provider_key] = now + wait
            else:
                self._backoff.pop(provider_key, None)
                self._next_due[provider_key] = now + interval

    def retry_soon(self, provider_key: str, seconds: float = RETRY_AFTER_FAILED_SAVE_SECONDS) -> None:
        """Make a lane due again shortly; used when its readings were not saved."""
        with self._lock:
            now = self._clock()
            self._last_polled[provider_key] = now
            self._next_due[provider_key] = now + float(seconds)

    def nudge(self, provider_key: str | None = None) -> None:
        targets = [provider_key] if provider_key else list(self.cadence)
        with self._lock:
            for key in targets:
                if not key or key in self._backoff:
                    continue
                if key in {"codex", "grok", "antigravity"}:
                    self._next_due[key] = float("-inf")
                elif key in self._last_polled:
                    active_interval, _ = self.lane_cadence(key)
                    self._next_due[key] = min(
                        self._next_due.get(key, float("-inf")),
                        self._last_polled[key] + float(active_interval),
                    )


_DEFAULT_LANES = QuotaLaneScheduler()
_DATABASE_LANES: dict[str, QuotaLaneScheduler] = {}


def nudge_quota_lanes(database_path: Path | str, provider_key: str | None = None) -> None:
    """Wake due quota lanes and clear local telemetry caches when new activity/usage lands."""
    try:
        from spend_app.limits import _CACHE, _CACHE_LOCK

        with _CACHE_LOCK:
            if provider_key in {None, "codex"}:
                for k in [k for k in _CACHE if k.startswith("codex_quota_poll:")]:
                    _CACHE.pop(k, None)
            if provider_key in {None, "grok"}:
                _CACHE.pop("grok_quota_poll", None)
            if provider_key in {None, "antigravity"}:
                _CACHE.pop("antigravity_quota_poll", None)
            if provider_key in {None, "muse"}:
                _CACHE.pop("muse_quota_poll", None)
            if provider_key in {None, "opencode-go"}:
                _CACHE.pop("opencode_go_quota_poll", None)
    except Exception:
        pass
    scheduler = _DATABASE_LANES.get(str(Path(database_path).resolve()))
    if scheduler is not None:
        scheduler.nudge(provider_key)


def lane_is_active(database_path: Path | str, provider_key: str, *, now: datetime | None = None) -> bool:
    """True when the lane's tool produced usage or has a live agent within the activity window."""
    moment = now or datetime.now(UTC)
    from spend_app.claude_accounts import account_for_key, is_account_key
    from spend_app.opencode_profiles import is_profile_key, profile_for_key

    if is_profile_key(provider_key):
        # OpenCode writes the profile's own database while that profile is in use.
        profile = profile_for_key(provider_key)
        stamps = []
        for path in ((profile.database, profile.database.with_name(profile.database.name + "-wal")) if profile else ()):
            try:
                stamps.append(path.stat().st_mtime)
            except OSError:
                pass
        modified = max(stamps, default=None)
        return modified is not None and moment.timestamp() - modified <= QUOTA_ACTIVITY_WINDOW_SECONDS
    if is_account_key(provider_key):
        # Usage is not attributed per account; Claude Code rewrites the
        # profile's state file while that account is in use.
        account = account_for_key(provider_key)
        try:
            modified = account.state_path.stat().st_mtime if account else None
        except OSError:
            modified = None
        return modified is not None and moment.timestamp() - modified <= QUOTA_ACTIVITY_WINDOW_SECONDS
    since = (moment - timedelta(seconds=QUOTA_ACTIVITY_WINDOW_SECONDS)).isoformat().replace("+00:00", "Z")
    tool_keys = ("opencode", "zcode") if provider_key == "opencode" else (provider_key,)
    placeholders = ",".join("?" for _ in tool_keys)
    run_prefixes = {
        "codex": ("codex:%",),
        "claude-code": ("claude:%",),
        "grok": ("grok:%",),
        "opencode": ("zcode:%", "opencode:%"),
        "antigravity": ("antigravity:%",),
    }.get(provider_key, ())
    with connect(Path(database_path)) as connection:
        row = connection.execute(
            f"SELECT 1 FROM usage_events WHERE occurred_at >= ? AND tool_key IN ({placeholders}) LIMIT 1",
            (since, *tool_keys),
        ).fetchone()
        if row is None:
            row = connection.execute(
                f"SELECT 1 FROM unpriced_usage_events WHERE occurred_at >= ? AND tool_key IN ({placeholders}) LIMIT 1",
                (since, *tool_keys),
            ).fetchone()
        if row is None and run_prefixes:
            like_clauses = " OR ".join("id LIKE ?" for _ in run_prefixes)
            row = connection.execute(
                f"SELECT 1 FROM agent_runs WHERE lower(state) IN ('live', 'running') AND last_seen_at >= ? AND ({like_clauses}) LIMIT 1",
                (since, *run_prefixes),
            ).fetchone()
    return row is not None


def _apply_throttle(samples: list[QuotaSample], payload: dict) -> list[QuotaSample]:
    if not isinstance(payload, dict) or payload.get("httpStatus") != THROTTLED_STATUS:
        return samples
    wait = float(payload.get("retryAfterSeconds") or THROTTLE_BACKOFF_MIN_SECONDS)
    throttled = []
    for sample in samples:
        reason = sample.reason or "Quota source is unavailable."
        throttled.append(
            replace(
                sample,
                reason=f"{reason} Provider rate-limited this lane (HTTP 429); polling backs off.",
                throttle_seconds=wait,
            )
        )
    return throttled


@dataclass(frozen=True)
class AgentRunRecord:
    id: str
    name: str
    model_key: str | None
    state: str
    started_at: str
    last_seen_at: str


def _number(value: object) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        return None
    return float(value)


def _window_reset(window: dict) -> str | None:
    reset = window.get("resetAt")
    return normalize_reset(reset) if reset else None


def normalize_reset(value: object) -> str | None:
    """Keep a provider reset time at whole-second precision.

    Anthropic's OAuth usage endpoint returns ``resets_at`` with a different
    microsecond fraction on every call (server clock noise around a fixed
    minute boundary). Persisting that jitter made every 90-second poll look
    like a changed value, so the dedup never matched and a new quota row was
    written each poll. The reset instant itself is unchanged.
    """
    if not value:
        return None
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    # Round to the nearest second: the observed jitter straddles the second
    # boundary (\u2026:59.9 and \u2026:00.1 for the same reset), so flooring would
    # still flap and would display a :59 reset as the previous minute.
    rounded = (parsed.astimezone(UTC) + timedelta(microseconds=500_000)).replace(microsecond=0)
    return rounded.isoformat().replace("+00:00", "Z")


def _windows_by_key(payload: dict) -> dict[str, dict]:
    return {
        str(window.get("key")): window
        for window in payload.get("windows") or []
        if isinstance(window, dict) and window.get("key")
    }


def _unavailable(
    provider_key: str,
    limit_key: str,
    label: str,
    source: str,
    reason: str | None,
) -> QuotaSample:
    return QuotaSample(
        provider_key=provider_key,
        limit_key=limit_key,
        label=label,
        unit=UNAVAILABLE_UNIT,
        source=source,
        reason=reason or "Quota source is unavailable.",
    )


def unavailable_samples(
    provider_key: str,
    reason: str,
    *,
    source: str | None = None,
) -> list[QuotaSample]:
    tag = source or PROVIDER_SOURCES.get(provider_key, "unavailable")
    return [
        _unavailable(provider_key, limit_key, label, tag, reason)
        for limit_key, label in REQUIRED_LIMITS.get(provider_key, ())
    ]


def claude_quota_samples(
    payload: dict, *, source: str, provider_key: str = "claude-code", name: str = "Claude"
) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Claude provider quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label, missing_reason in (
        ("5h", f"{name} 5-hour window", "Claude quota omitted the 5-hour percentage."),
        ("weekly", f"{name} weekly window", "Claude quota omitted the weekly percentage."),
    ):
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    provider_key,
                    limit_key,
                    label,
                    source,
                    detail if not exact else missing_reason,
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key=provider_key,
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def grok_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Grok Build provider quota is unavailable.")
    window = _windows_by_key(payload).get("weekly") if exact else None
    used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
    if used_pct is None:
        return [
            _unavailable(
                "grok",
                "weekly",
                "Grok Build weekly",
                source,
                detail if not exact else "Grok Build quota omitted the weekly percentage.",
            )
        ]
    return [
        QuotaSample(
            provider_key="grok",
            limit_key="weekly",
            label="Grok Build weekly",
            unit="pct",
            source=source,
            pct=max(0.0, used_pct),
            resets_at=_window_reset(window),
        )
    ]


def antigravity_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Antigravity local model quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["antigravity"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "antigravity",
                    limit_key,
                    label,
                    source,
                    detail if not exact else f"Antigravity RPC omitted the {label} pool.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="antigravity",
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def codex_quota_samples(payload: dict, *, source: str, now: datetime | None = None) -> list[QuotaSample]:
    label = "Codex weekly window"
    if payload.get("status") != "exact" or payload.get("poolId") != "codex":
        return [
            _unavailable(
                "codex",
                "weekly",
                label,
                source,
                str(payload.get("detail") or "No local rate-limit snapshot was found."),
            )
        ]
    weekly = None
    for window in payload.get("windows") or []:
        minutes = window.get("windowMinutes") if isinstance(window, dict) else None
        if isinstance(minutes, (int, float)) and not isinstance(minutes, bool) and float(minutes) == WEEKLY_MINUTES:
            weekly = window
            break
    used_pct = _number(weekly.get("usedPct")) if isinstance(weekly, dict) else None
    from spend_app.codex_quota import freshness_reason
    reason = freshness_reason(payload.get("observedAt"), _window_reset(weekly) if weekly else None, now or datetime.now(UTC))
    if used_pct is None or not 0 <= used_pct <= 100 or reason or not payload.get("scope"):
        return [
            _unavailable(
                "codex",
                "weekly",
                label,
                source,
                reason or "The local Codex snapshot had no verified weekly rate-limit window/scope.",
            )
        ]
    return [
        QuotaSample(
            provider_key="codex",
            limit_key="weekly",
            label=label,
            unit="pct",
            source=source,
            pct=used_pct,
            resets_at=_window_reset(weekly),
            observed_at=payload["observedAt"],
            pool_id="codex",
            scope=payload["scope"],
        )
    ]


def muse_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Muse Code quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["muse"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "muse",
                    limit_key,
                    label,
                    source,
                    detail if not exact else f"Muse Code quota endpoint omitted the {label} window.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="muse",
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


# Not a required lane: a computer without a Go subscription shows no Go card.
OPENCODE_GO_WINDOWS = (
    ("5h", "OpenCode Go 5-hour window"),
    ("weekly", "OpenCode Go weekly window"),
    ("monthly", "OpenCode Go monthly window"),
)


def opencode_go_quota_samples(payload: dict, *, source: str, provider_key: str = "opencode-go") -> list[QuotaSample]:
    status = payload.get("status")
    # No consent, no Go subscription, or nothing reported: no card at all.
    if status in {"absent", "unavailable"}:
        return []
    exact = status == "exact"
    detail = str(payload.get("detail") or "OpenCode Go usage is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in OPENCODE_GO_WINDOWS:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(_unavailable(provider_key, limit_key, label, source,
                                        detail if not exact else f"OpenCode Go omitted the {label}."))
            continue
        samples.append(
            QuotaSample(
                provider_key=provider_key,
                limit_key=limit_key,
                label=label,
                unit="pct",
                source=source,
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def zai_quota_samples(payload: dict, *, source: str) -> list[QuotaSample]:
    exact = payload.get("status") == "exact"
    detail = str(payload.get("detail") or "Z.AI quota is unavailable.")
    windows = _windows_by_key(payload)
    samples: list[QuotaSample] = []
    for limit_key, label in REQUIRED_LIMITS["opencode"]:
        window = windows.get(limit_key) if exact else None
        used_pct = _number(window.get("usedPct")) if isinstance(window, dict) else None
        if used_pct is None:
            samples.append(
                _unavailable(
                    "opencode",
                    limit_key,
                    label,
                    source,
                    detail if not exact else f"The Z.AI quota endpoint omitted the {label} window.",
                )
            )
            continue
        samples.append(
            QuotaSample(
                provider_key="opencode",
                limit_key=limit_key,
                label=label,
                unit="credits",
                source=source,
                used=_number(window.get("used")),
                allowance=_number(window.get("limit")),
                pct=max(0.0, used_pct),
                resets_at=_window_reset(window),
            )
        )
    return samples


def openrouter_quota_samples(
    *,
    collector: Callable[[], dict] | None = None,
) -> list[QuotaSample]:
    payload = (collector or _openrouter_credits_uncached)()
    windows = _windows_by_key(payload)
    balance = windows.get("balance") if payload.get("status") == "exact" else None
    remaining = _number(balance.get("remainingUsd")) if balance else None
    if remaining is None:
        return [
            QuotaSample(
                provider_key="openrouter",
                limit_key="balance",
                label="OpenRouter funds remaining",
                unit=UNAVAILABLE_UNIT,
                source="openrouter_credits_api",
                is_payg=True,
                reason=str(payload.get("detail") or "OpenRouter account balance is unavailable."),
            )
        ]
    return [
        QuotaSample(
            provider_key="openrouter",
            limit_key="balance",
            label="OpenRouter funds remaining",
            unit="usd",
            source="openrouter_credits_api",
            used=remaining,
            is_payg=True,
        )
    ]


DEEPSEEK_BALANCE_LABEL = "DeepSeek funds remaining"


def deepseek_quota_samples(payload: dict, *, source: str = "deepseek_balance_api") -> list[QuotaSample]:
    # No connected key: no card at all, as for a computer without DeepSeek.
    if payload.get("status") == "absent":
        return []
    windows = _windows_by_key(payload)
    balance = windows.get("balance") if payload.get("status") == "exact" else None
    remaining = _number(balance.get("remainingUsd")) if balance else None
    if remaining is None:
        return [
            QuotaSample(
                provider_key="deepseek",
                limit_key="balance",
                label=DEEPSEEK_BALANCE_LABEL,
                unit=UNAVAILABLE_UNIT,
                source=source,
                is_payg=True,
                reason=str(payload.get("detail") or "DeepSeek account balance is unavailable."),
            )
        ]
    return [
        QuotaSample(
            provider_key="deepseek",
            limit_key="balance",
            label=DEEPSEEK_BALANCE_LABEL,
            unit="usd",
            source=source,
            used=remaining,
            is_payg=True,
        )
    ]


def _copy_quota_resets(primary: dict, donor: dict | None) -> dict:
    """Copy a real reset onto a matching 5h or weekly window.

    Desktop history has no reset of its own. A timestamp is attached only
    when another source already read in this collection has one for the same
    window. Percentages stay with ``primary``. Nothing is invented when the
    donor has no reset, or when desktop is the only source.
    """
    if not isinstance(donor, dict) or donor.get("status") != "exact":
        return primary
    donated: dict[str, str] = {}
    for window in donor.get("windows") or []:
        if not isinstance(window, dict):
            continue
        key = window.get("key")
        reset = window.get("resetAt")
        if key in {"5h", "weekly"} and reset:
            donated[str(key)] = reset
    if not donated:
        return primary
    windows = []
    for window in primary.get("windows") or []:
        if not isinstance(window, dict):
            windows.append(window)
            continue
        key = window.get("key")
        if window.get("resetAt") or key not in donated:
            windows.append(window)
            continue
        windows.append({**window, "resetAt": donated[str(key)]})
    return {**primary, "windows": windows}


def _claude_reset_donor() -> dict | None:
    """Traycer, then Claude Code's cached usage.

    Used only to borrow resets. A successful Traycer payload is not replaced
    by the cache, even when Traycer's resets are empty.
    """
    try:
        return _claude_from_traycer_result(_traycer_profile_rate_limits("claude"))
    except Exception:
        payload = _claude_limits_uncached()
    if isinstance(payload, dict) and payload.get("status") == "exact":
        return payload
    return None


def _claude_quota_collector() -> list[QuotaSample]:
    def load() -> tuple[dict, str]:
        # Desktop history still wins the percentages. It does not win by
        # erasing a reset another source in this same collection already has.
        try:
            desktop = _claude_desktop_limits_uncached()
        except Exception:
            desktop = None
        # Claude Code's own cached reading (no network, no sign-in) wins when
        # it is newer than Claude Desktop's history, which has no resets.
        cached = _claude_limits_uncached()
        cached_at = str(cached.get("observedAt") or "")
        if cached.get("status") == "exact" and cached_at and (
                desktop is None or cached_at >= str(desktop.get("observedAt") or "")):
            return cached, "claude_code_cache"
        if desktop is None:
            try:
                return _claude_from_traycer_result(_traycer_profile_rate_limits("claude")), "traycer_profile"
            except Exception:
                return cached, "claude_code_cache"
        return _copy_quota_resets(desktop, _claude_reset_donor()), "claude_desktop_history"

    # The lane scheduler owns the cadence; the short cache only coalesces a
    # poll with a concurrent compatibility /limits read.
    payload, source = _cached("claude_quota_poll", 10, load)
    return _apply_throttle(claude_quota_samples(payload, source=source), payload)


def _claude_account_collector(account) -> Callable[[], list[QuotaSample]]:
    """Quota for one extra Claude Code account: its own cached usage, its own lane."""
    from spend_app.limits import claude_account_limits

    def collect() -> list[QuotaSample]:
        payload, source = _cached(f"claude_account_poll:{account.key}", 10, lambda: claude_account_limits(account))
        # Window labels match the default lane; the card names the account.
        samples = claude_quota_samples(payload, source=source, provider_key=account.key)
        return _apply_throttle(samples, payload)

    return collect


def _grok_quota_collector() -> list[QuotaSample]:
    # Local CLI billing snapshot first (no network); Traycer profile otherwise.
    payload = _cached("grok_quota_poll", 10, _grok_limits_uncached)
    source = str(payload.get("source") or "traycer_profile")
    return _apply_throttle(grok_quota_samples(payload, source=source), payload)


def _collect(name: str, ttl: int, loader: Callable[[], dict], builder, source: str) -> list[QuotaSample]:
    payload = _cached(name, ttl, loader)
    return _apply_throttle(builder(payload, source=source), payload)


def _opencode_profile_collector(profile) -> Callable[[], list[QuotaSample]]:
    """Go quota for one extra OpenCode profile: its own stored key, its own lane."""

    def collect() -> list[QuotaSample]:
        # Window labels match the default lane; the card names the profile.
        return _collect(
            f"opencode_go_profile_poll:{profile.key}",
            10,
            lambda: _opencode_go_limits_uncached(profile.database, provider_key=profile.key),
            lambda payload, *, source: opencode_go_quota_samples(payload, source=source, provider_key=profile.key),
            "opencode_go_usage_endpoint",
        )

    return collect


def _codex_quota_collector(database_path):
    from spend_app.codex_quota import current_scope
    key = f"codex_quota_poll:{Path(database_path).resolve()}:{current_scope()}"
    return _collect(key, 10, _codex_limits, codex_quota_samples, "codex_local_telemetry")


def default_quota_collectors(
    database_path: Path | str,
) -> dict[str, Callable[[], list[QuotaSample]]]:
    result = {
        "antigravity": lambda: _collect(
            "antigravity_quota_poll",
            10,
            _antigravity_limits_uncached,
            antigravity_quota_samples,
            "antigravity_local_rpc",
        ),
        "codex": lambda: _codex_quota_collector(database_path),
        "claude-code": _claude_quota_collector,
        "grok": _grok_quota_collector,
        "opencode": lambda: _collect("zai_quota_poll", 10, _zai_limits_uncached, zai_quota_samples, "zai_quota_endpoint"),
        "muse": lambda: _collect("muse_quota_poll", 10, _muse_limits_uncached, muse_quota_samples, "muse_quota_endpoint"),
        "opencode-go": lambda: _collect("opencode_go_quota_poll", 10, _opencode_go_limits_uncached,
                                        opencode_go_quota_samples, "opencode_go_usage_endpoint"),
        "openrouter": openrouter_quota_samples,
        "deepseek": lambda: _collect("deepseek_balance_poll", 10, _deepseek_balance_uncached,
                                     deepseek_quota_samples, "deepseek_balance_api"),
    }
    from spend_app.claude_accounts import extra_accounts
    for account in extra_accounts():
        result[account.key] = _claude_account_collector(account)
    from spend_app.opencode_profiles import extra_profiles
    for profile in extra_profiles():
        result[profile.key] = _opencode_profile_collector(profile)
    from spend_app.integration_policy import authorize
    if authorize("claude_statusline_snapshot")[0]:
        from spend_app.claude_statusline import read_snapshot
        result["claude-code"] = lambda: claude_quota_samples(read_snapshot(database_path), source="claude_statusline_snapshot")

    from spend_app.connections import Service, Store, lock_for, external_key
    from spend_app.config import load_settings
    from spend_app.providers import REGISTRY
    from dataclasses import replace
    settings = replace(load_settings(), database_path=Path(database_path))
    sources = {"codex": "codex_local", "claude-code": "claude_local", "grok": "grok_local", "opencode": "opencode_local", "opencode-go": "opencode_local", "muse": "muse_local", "antigravity": "antigravity_local", "openrouter": "openrouter", "deepseek": "deepseek"}
    def guarded(provider, reader):
        def collect():
            key = None
            with lock_for(database_path):
                state = Store(database_path).read()
                binding = state["bindings"].get(sources.get(provider))
                if binding and provider not in BALANCE_SOURCES:
                    if suppresses_default_profile(sources.get(provider), binding):
                        return unavailable_samples(provider, "Managed location grants usage only. Activity/quota at custom locations are not supported; no default profile was read.")
                    binding = None  # the default location: read the default profile as usual
                if binding:
                    if not binding["enabled"]:
                        if provider == "deepseek":
                            return deepseek_quota_samples({"status": "error", "detail": "Balance connection is disconnected."})
                        return unavailable_samples(provider, "Balance connection is disconnected.")
                    service = Service(settings)
                    key = external_key(REGISTRY.get(provider), settings) or service.vault.get(binding["credentialRef"])
            if binding and provider == "openrouter":
                return openrouter_quota_samples(collector=lambda: _openrouter_credits_uncached(key))
            if binding and provider == "deepseek":
                payload = _deepseek_balance_uncached(key)
                return _apply_throttle(deepseek_quota_samples(payload), payload)
            return reader()
        return collect
    return {provider: guarded(provider, reader) for provider, reader in result.items()}


def _persist_fields(sample: QuotaSample) -> tuple:
    label = sample.label
    if sample.reason:
        label = f"{label}{LABEL_REASON_SEPARATOR}{sample.reason}"
    is_payg = None if sample.is_payg is None else int(bool(sample.is_payg))
    return (
        label,
        sample.used,
        sample.allowance,
        sample.unit,
        sample.pct,
        sample.resets_at,
        sample.source,
        is_payg,
    )


from spend_app.connections import lock_for, serialize_collection, suppresses_default_profile
from spend_app.capacity_forecast import record_balance_sample, record_pace_samples


def poll_quotas(
    database_path: Path | str,
    *,
    collectors: dict[str, Callable[[], list[QuotaSample]]] | None = None,
    now: Callable[[], str] | None = None,
    lanes: QuotaLaneScheduler | None = None,
    activity: Callable[[str], bool] | None = None,
    settings=None,
) -> dict:
    """Poll the lanes that are due on this tick and persist their samples.

    With explicit ``collectors`` (tests, backfills) every collector runs each
    call unless ``lanes`` is given. The scheduled job uses the default lane
    scheduler: local lanes every tick, external lanes on their active/idle
    cadence, and any lane that answered 429 waits out its back-off.
    """
    path = Path(database_path)
    polled_at = (now or utc_now)()
    initialize(path)
    if collectors is None and os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "polledAt": polled_at,
            "written": 0,
            "skipped": 0,
            "providers": [],
            "polledProviders": [],
            "deferredProviders": [],
        }
    resolved = collectors if collectors is not None else default_quota_collectors(path)
    scheduler = lanes if lanes is not None else (_DATABASE_LANES.setdefault(str(path.resolve()), QuotaLaneScheduler()) if collectors is None else None)
    is_active = activity or (lambda provider: lane_is_active(path, provider))
    managed_completion: dict[str, dict] = {}
    if collectors is None:
        from spend_app.connections import Store
        with lock_for(path):
            bindings = Store(path).read()["bindings"]
            managed_completion = {source: bindings[source] for source in BALANCE_SOURCES if bindings.get(source)}
    balance_ok: dict[str, bool] = {}
    written = 0
    skipped = 0
    polled: list[str] = []
    deferred: list[str] = []
    paced = []
    polled_samples: list[tuple[str, list[QuotaSample], bool, bool]] = []
    for provider_key in sorted(resolved):
        lane_active = bool(is_active(provider_key)) if scheduler is not None else False
        if scheduler is not None and not scheduler.due(provider_key, active=lane_active):
            deferred.append(provider_key)
            continue
        polled.append(provider_key)
        try:
            samples = list(resolved[provider_key]())
        except PolicyDenied as exc:
            # Actionable, secret-free denial copy \u2014 never a class name.
            samples = unavailable_samples(provider_key, str(exc))
        except Exception:
            samples = unavailable_samples(
                provider_key,
                "Quota collection failed for this lane; it will retry on the next tick.",
            )
        if provider_key in BALANCE_SOURCES:
            balance_ok[provider_key] = any(sample.used is not None for sample in samples)
        throttled = any(sample.throttle_seconds is not None for sample in samples)
        if scheduler is not None and throttled:
            # A provider back-off holds even if saving fails below.
            scheduler.record(provider_key, samples, active=lane_active)
        present = {sample.limit_key for sample in samples}
        for limit_key, label in REQUIRED_LIMITS.get(provider_key, ()):
            if limit_key not in present:
                samples.append(
                    _unavailable(
                        provider_key,
                        limit_key,
                        label,
                        PROVIDER_SOURCES.get(provider_key, "unavailable"),
                        "No sample was produced for this limit during the poll.",
                    )
                )
        polled_samples.append((provider_key, samples, lane_active, throttled))

    sources_by_provider = {"codex": "codex_local", "claude-code": "claude_local", "grok": "grok_local", "opencode": "opencode_local", "opencode-go": "opencode_local", "antigravity": "antigravity_local", "openrouter": "openrouter"}
    try:
        with lock_for(path):
            current_bindings = {}
            if collectors is None:
                from spend_app.connections import Store
                current_bindings = Store(path).read()["bindings"]
            with connect(path) as connection:
                for provider_key, samples, _active, _throttled in polled_samples:
                    source_key = sources_by_provider.get(provider_key)
                    if (collectors is None and provider_key not in BALANCE_SOURCES
                            and suppresses_default_profile(source_key, current_bindings.get(source_key))):
                        samples = unavailable_samples(
                            provider_key,
                            "Managed location grants usage only. Activity/quota at custom locations are not supported; no default profile was read.",
                        )
                    for sample in samples:
                        from spend_app.codex_quota import save_metadata
                        save_metadata(connection, sample, polled_at)
                        fields = _persist_fields(sample)
                        if sample.limit_key == "balance" and sample.unit == "usd" and sample.used is not None:
                            record_balance_sample(connection, provider_key=sample.provider_key, source=sample.source,
                                                  usd=sample.used, sampled_at=polled_at)
                        latest = connection.execute(
                            "SELECT id, label, used, allowance, unit, pct, resets_at, source, is_payg "
                            "FROM quotas WHERE provider_key=? AND limit_key=? "
                            "ORDER BY polled_at DESC, id DESC LIMIT 1",
                            (sample.provider_key, sample.limit_key),
                        ).fetchone()
                        if sample.pct is not None and not sample.is_payg:
                            paced.append(sample)
                        if (
                            sample.unit == "unavailable"
                            and latest is not None
                            and latest["unit"] != "unavailable"
                            and (
                                str(latest["source"] or "").startswith("sync:")
                                or (
                                    collectors is None
                                    and latest["resets_at"]
                                    and str(latest["resets_at"]) > polled_at
                                )
                            )
                        ):
                            skipped += 1
                            continue
                        if latest is not None and tuple(latest)[1:] == fields:
                            # Same observation: no new history row, but the row now
                            # states the most recent poll that confirmed the value so
                            # "last poll" stays truthful.
                            connection.execute(
                                "UPDATE quotas SET polled_at=? WHERE id=? AND polled_at<?",
                                (polled_at, latest["id"], polled_at),
                            )
                            skipped += 1
                            continue
                        upsert_quota(
                            connection,
                            provider_key=sample.provider_key,
                            limit_key=sample.limit_key,
                            label=fields[0],
                            used=sample.used,
                            allowance=sample.allowance,
                            unit=sample.unit,
                            pct=sample.pct,
                            resets_at=sample.resets_at,
                            source=sample.source,
                            polled_at=polled_at,
                            is_payg=sample.is_payg,
                        )
                        written += 1
                record_pace_samples(connection, paced, polled_at)
            completed = [source for source in managed_completion if source in polled]
            if completed:
                from spend_app.connections import Service
                from spend_app.config import load_settings
                from dataclasses import replace
                service = Service(replace(load_settings(), database_path=path))
                for source in completed:
                    service.completion(source, managed_completion[source]["revision"],
                                       {"status": "success" if balance_ok.get(source) else "failed", "eventsSeen": 0})
    except Exception:
        if scheduler is not None:
            for provider_key, _samples, _active, throttled in polled_samples:
                if not throttled:
                    scheduler.retry_soon(provider_key)
        raise
    # Only saved readings move a lane to its next interval; see retry_soon above.
    if scheduler is not None:
        for provider_key, samples, lane_active, throttled in polled_samples:
            if not throttled:
                scheduler.record(provider_key, samples, active=lane_active)
    if written > 0:
        if collectors is None:
            try:
                from dataclasses import replace as _replace
                from spend_app.config import load_settings as _load_settings
                from spend_app.pricing import PricingEngine as _PricingEngine
                from spend_app.setup_wizard import maybe_run_configured_spoke_sync
                _s = settings if settings is not None else _replace(_load_settings(), database_path=path)
                maybe_run_configured_spoke_sync(_s, _PricingEngine.default())
            except Exception:
                pass
        try:
            from spend_app.sync import STREAM_BROKER, expire_hub_proxy_cache
            expire_hub_proxy_cache()
            STREAM_BROKER.publish({"type": "quotas", "written": written, "at": polled_at})
        except Exception:
            pass
    return {
        "polledAt": polled_at,
        "written": written,
        "skipped": skipped,
        "providers": sorted(resolved),
        "polledProviders": polled,
        "deferredProviders": deferred,
    }


def agent_run_records(activity: dict) -> list[AgentRunRecord]:
    records: list[AgentRunRecord] = []
    for agent in activity.get("activeAgents") or []:
        record = _run_record(agent, state=AGENT_LIVE_STATE, last_seen_field="startedAt")
        if record:
            records.append(record)
    for agent in activity.get("unmeteredTurns") or []:
        record = _run_record(agent, state=AGENT_NO_DATA_STATE, last_seen_field="stoppedAt")
        if record:
            records.append(record)
    for session in activity.get("grokSessions") or []:
        record = _grok_run_record(session)
        if record:
            records.append(record)
    for session in activity.get("codexSessions") or []:
        record = _codex_run_record(session)
        if record:
            records.append(record)
    for key, prefix in (
        ("claudeSessions", "claude"),
        ("zcodeSessions", "zcode"),
        ("antigravitySessions", "antigravity"),
    ):
        for session in activity.get(key) or []:
            record = _local_run_record(session, prefix=prefix)
            if record:
                records.append(record)
    return records


def _grok_run_record(session: dict) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    if not session_id or not started_at:
        return None
    return AgentRunRecord(
        id=f"grok:{session_id}",
        name=str(session.get("title") or "Grok Build session"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(started_at),
    )


def _codex_run_record(session: dict) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    last_seen_at = session.get("lastSeenAt")
    if not session_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"codex:{session_id}",
        name=str(session.get("title") or "Codex task"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def _local_run_record(session: dict, *, prefix: str) -> AgentRunRecord | None:
    if not isinstance(session, dict):
        return None
    session_id = str(session.get("sessionId") or "").strip()
    started_at = session.get("startedAt")
    last_seen_at = session.get("lastSeenAt")
    if not session_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"{prefix}:{session_id}",
        name=str(session.get("title") or f"{prefix.title()} session"),
        model_key=str(session["model"]) if session.get("model") else None,
        state=AGENT_LIVE_STATE,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def _run_record(agent: dict, *, state: str, last_seen_field: str) -> AgentRunRecord | None:
    if not isinstance(agent, dict):
        return None
    chat_id = str(agent.get("chatId") or "").strip()
    started_at = agent.get("startedAt")
    last_seen_at = agent.get(last_seen_field)
    if not chat_id or not started_at or not last_seen_at:
        return None
    return AgentRunRecord(
        id=f"traycer:{chat_id}",
        name=str(agent.get("title") or "Untitled agent"),
        model_key=str(agent["model"]) if agent.get("model") else None,
        state=state,
        started_at=str(started_at),
        last_seen_at=str(last_seen_at),
    )


def is_live_run(state: str | None) -> bool:
    return str(state or "").strip().lower() in LIVE_STATE_ALIASES


def _clear_unseen_live_runs(connection, seen_ids: set[str]) -> int:
    if seen_ids:
        placeholders = ",".join("?" for _ in seen_ids)
        cursor = connection.execute(
            "DELETE FROM agent_runs "
            f"WHERE lower(state) IN ('live','running') AND id NOT LIKE 'm:%' AND id NOT IN ({placeholders})",
            tuple(seen_ids),
        )
    else:
        cursor = connection.execute(
            "DELETE FROM agent_runs WHERE lower(state) IN ('live','running') AND id NOT LIKE 'm:%'"
        )
    return int(cursor.rowcount or 0)


def default_activity_collector(database_path=None) -> dict:
    bindings = {}
    if database_path is not None:
        from spend_app.connections import Store
        with lock_for(database_path):
            bindings = Store(database_path).read()["bindings"]
    # Existing readers have no custom-root contract. Do not read another
    # default profile for a custom or disabled usage binding; a binding of the
    # default location reads that same profile anyway.
    activity = {} if suppresses_default_profile("traycer_local", bindings.get("traycer_local")) else _traycer_activity()
    readers = {
        "grokSessions": ("grok_local", _grok_active_sessions),
        "codexSessions": ("codex_local", _codex_active_sessions),
        "zcodeSessions": ("zcode_local", _zcode_active_sessions),
        "antigravitySessions": ("antigravity_local", _antigravity_active_sessions),
        "claudeSessions": ("claude_local", _claude_active_sessions),
    }
    return {**activity, **{field: [] if suppresses_default_profile(source, bindings.get(source)) else reader()
                           for field, (source, reader) in readers.items()}}


def poll_activity(
    database_path: Path | str,
    *,
    collector: Callable[[], dict] | None = None,
    now: Callable[[], str] | None = None,
) -> dict:
    path = Path(database_path)
    polled_at = (now or utc_now)()
    initialize(path)
    if collector is None and os.getenv("BURNRATE_CENTRAL_SERVER", "").strip().lower() in {"1", "true", "yes"}:
        return {
            "polledAt": polled_at,
            "seen": 0,
            "new": 0,
            "cleared": 0,
            "live": 0,
            "noData": 0,
        }
    activity = collector() if collector else default_activity_collector(path)
    records = agent_run_records(activity)
    new = 0
    with lock_for(path):
        with connect(path) as connection:
            for record in records:
                if upsert_agent_run(
                    connection,
                    id=record.id,
                    name=record.name,
                    model_key=record.model_key,
                    state=record.state,
                    started_at=record.started_at,
                    last_seen_at=record.last_seen_at,
                ):
                    new += 1
            cleared = _clear_unseen_live_runs(connection, {record.id for record in records})
    if new > 0 or cleared > 0:
        if collector is None:
            if cleared > 0:
                nudge_quota_lanes(path)
            try:
                from dataclasses import replace as _replace
                from spend_app.config import load_settings as _load_settings
                from spend_app.pricing import PricingEngine as _PricingEngine
                from spend_app.setup_wizard import maybe_run_configured_spoke_sync
                _s = _replace(_load_settings(), database_path=path)
                maybe_run_configured_spoke_sync(_s, _PricingEngine.default())
            except Exception:
                pass
        try:
            from spend_app.sync import STREAM_BROKER
            STREAM_BROKER.publish({"type": "activity", "new": new, "cleared": cleared, "at": polled_at})
        except Exception:
            pass
    return {
        "polledAt": polled_at,
        "seen": len(records),
        "new": new,
        "cleared": cleared,
        "live": sum(1 for record in records if is_live_run(record.state)),
        "noData": sum(1 for record in records if not is_live_run(record.state)),
    }


def pressure_color(pct: float | None) -> str:
    if pct is None:
        return "unavailable"
    if pct < 30:
        return PRESSURE_GREEN
    if pct < 60:
        return PRESSURE_BLUE
    if pct < 85:
        return PRESSURE_AMBER
    return PRESSURE_RED


def order_capacity_rows(rows: Sequence[QuotaSample]) -> list[QuotaSample]:
    grouped: dict[str, list[QuotaSample]] = {}
    for row in rows:
        grouped.setdefault(row.provider_key, []).append(row)
    primary_values: dict[str, float] = {}
    for provider_key, provider_rows in grouped.items():
        order = _limit_order(provider_key)
        primary = next(
            (row for row in provider_rows if order and row.limit_key == order[0]),
            max(provider_rows, key=lambda row: row.pct if row.pct is not None else float("-inf")),
        )
        primary_values[provider_key] = primary.pct if primary.pct is not None else float("-inf")

    def provider_rank(provider_key: str) -> tuple[int, float]:
        if provider_key in BALANCE_SOURCES:
            return (1, 0.0)
        return (0, -primary_values.get(provider_key, float("-inf")))

    def row_key(row: QuotaSample) -> tuple[int, float, int, str]:
        group, provider_primary = provider_rank(row.provider_key)
        order = _limit_order(row.provider_key)
        try:
            limit_rank = order.index(row.limit_key)
        except ValueError:
            limit_rank = len(order) + 100
        return (group, provider_primary, limit_rank, row.limit_key)

    return sorted(rows, key=row_key)


def split_quota_label(label: str) -> tuple[str, str | None]:
    if LABEL_REASON_SEPARATOR in label:
        name, reason = label.split(LABEL_REASON_SEPARATOR, 1)
        return name, reason
    return label, None


def derived_used(
    *,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
) -> float | None:
    if pct is not None and allowance not in (None, 0):
        return (pct / 100.0) * allowance
    return used


def quota_note(
    unit: str,
    *,
    used: float | None = None,
    allowance: float | None = None,
    pct: float | None = None,
    label: str | None = None,
) -> str | None:
    if unit == UNAVAILABLE_UNIT:
        if label:
            _, reason = split_quota_label(label)
            return reason
        return None
    quantity = derived_used(used=used, allowance=allowance, pct=pct)
    if unit == "usd":
        if quantity is None:
            return None
        if allowance is None:
            return f"${quantity:,.2f} spent"
        return f"${quantity:,.2f} of ${allowance:,.2f} spent"
    if unit == "credits":
        if quantity is not None and allowance is not None:
            return f"{quantity:,.0f} of {allowance:,.0f} credits used"
        if pct is not None:
            return f"{pct:g}% of credits used"
        return None
    if unit == "pct":
        if pct is not None:
            return f"{pct:g}% used"
        return None
    return None
