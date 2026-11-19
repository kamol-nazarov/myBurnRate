"""How urgent a provider is, computed once so the tab and the full window agree.

The rules live in the D refined build contract: pick one focus window per
provider, then grade it ``out``, ``soon``, ``warn``, ``ok``, ``balance`` or
``idle``. Everything here is a pure function of the merged provider dict.
"""

from __future__ import annotations

import math
from datetime import UTC, datetime

from spend_app.timeutil import parse_utc

LEVELS = ("out", "soon", "warn", "ok", "balance", "idle")
LEVEL_RANK = {level: rank for rank, level in enumerate(LEVELS)}
PACE_STATES = ("tight", "dry", "safe")
RUNWAY_STATES = ("learning", "idle", "estimate")
DRYING_STATES = ("tight", "dry")

#: Providers that can have several profiles; a profile's key is the family
#: key plus ``-<name>`` (``claude-code-work``).
PROFILE_FAMILIES = (
    "claude-code",
    "opencode-go",
    "zcode",
    "codex",
    "antigravity",
    "grok",
    "muse",
)

SOON_HOURS = 2.0
SOON_PCT = 10.0
SOON_BALANCE = 1.0
WARN_BALANCE = 5.0


def profile_family(key) -> str:
    """``claude-code`` for ``claude-code`` and ``claude-code-work``; else the key."""
    text = str(key or "")
    for family in PROFILE_FAMILIES:
        if text == family or text.startswith(family + "-"):
            return family
    return text


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def build_pace(forecast) -> dict | None:
    """Map a hub capacity row's ``forecast`` dict onto the window ``pace`` shape.

    A missing forecast and a ``stale`` one (no fresh reading to project from)
    both give ``None``, because neither says anything about running dry.
    """
    if not isinstance(forecast, dict):
        return None
    state = forecast.get("state")
    if state not in PACE_STATES:
        return None
    return {
        "state": state,
        "hoursToDry": _number(forecast.get("hoursRemaining")),
        "hoursDryBeforeReset": _number(forecast.get("hoursBeforeReset")),
        "runsDryAt": forecast.get("projectedRunDry") or None,
    }


def build_funds_runway(card_runway) -> dict | None:
    """The structured runway of a prepaid balance, or ``None`` without one."""
    if not isinstance(card_runway, dict):
        return None
    state = card_runway.get("state")
    if state not in RUNWAY_STATES:
        return None
    return {
        "state": state,
        "days": _number(card_runway.get("days")),
        "runsOutAt": card_runway.get("runsOutAt") or None,
    }


def window_has_current_reading(window: dict, now: datetime) -> bool:
    if window.get("unavailable") or window.get("stale") or _number(window.get("remainingPct")) is None:
        return False
    reset = parse_utc(window.get("resetsAt"))
    return reset is None or now < reset


def _is_drying(window: dict) -> bool:
    pace = window.get("pace")
    return isinstance(pace, dict) and pace.get("state") in DRYING_STATES


def _hours_to_dry(window: dict) -> float:
    pace = window.get("pace")
    hours = _number(pace.get("hoursToDry")) if isinstance(pace, dict) else None
    return math.inf if hours is None else hours


def _remaining_pct(window: dict) -> float:
    pct = _number(window.get("remainingPct"))
    return math.inf if pct is None else pct


def focus_window(provider: dict, now: datetime | None = None) -> dict | None:
    """The window the provider is graded on.

    A currently exhausted window takes precedence. Among the other windows,
    choose the one predicted to run dry soonest, then the one with least left.
    """
    now = now or datetime.now(UTC)
    measured = [w for w in provider.get("windows") or [] if window_has_current_reading(w, now)]
    if not measured:
        return None
    least_remaining = min(measured, key=_remaining_pct)
    if _remaining_pct(least_remaining) <= 0:
        return least_remaining
    drying = [w for w in measured if _is_drying(w)]
    if drying:
        return min(drying, key=_hours_to_dry)
    return least_remaining


def _balance(provider: dict) -> float | None:
    if provider.get("isPayg") or (provider.get("remaining") or {}).get("kind") == "balance":
        return _number(provider.get("fundsRemainingUsd"))
    return None


def _balance_level(balance: float) -> str:
    if balance <= 0:
        return "out"
    if balance < SOON_BALANCE:
        return "soon"
    if balance < WARN_BALANCE:
        return "warn"
    return "balance"


def format_hours(hours: float | None) -> str:
    """``~45m`` under an hour, ``~14h`` after that, never a decimal."""
    if hours is None or hours <= 0:
        return "~0m"
    if hours < 1:
        return f"~{max(1, int(round(hours * 60)))}m"
    return f"~{max(1, int(round(hours)))}h"


def format_reset(resets_at, now: datetime | None = None) -> str:
    """``resets in 2h`` or ``resets in 5d``; empty when there is no reset."""
    reset = parse_utc(resets_at)
    if reset is None:
        return ""
    now = now or datetime.now(UTC)
    hours = (reset - now).total_seconds() / 3600
    if hours <= 0:
        return "resets now"
    if hours < 1:
        return f"resets in {max(1, int(round(hours * 60)))}m"
    if hours < 48:
        return f"resets in {max(1, int(round(hours)))}h"
    return f"resets in {int(round(hours / 24))}d"


def window_urgency(window: dict, now: datetime | None = None) -> dict:
    """``{"level", "limitKey", "text"}`` for one measured window."""
    now = now or datetime.now(UTC)
    limit_key = str(window.get("limitKey") or "") or None
    level = grade_window(window)
    left = _remaining_pct(window)
    reset_text = format_reset(window.get("resetsAt"), now)
    if level == "out":
        text = f"out · {reset_text}" if reset_text else "out of quota"
    elif _is_drying(window):
        hours = _hours_to_dry(window)
        if math.isinf(hours):
            text = "dry before reset"
        elif hours <= 0:
            text = "dry now"
        else:
            text = f"dry in {format_hours(hours)}"
    elif level == "soon":
        text = f"{left:.0f}% left" + (f" · {reset_text}" if reset_text else "")
    else:
        text = reset_text or "on pace"
    return {"level": level, "limitKey": limit_key, "text": text}


def compute_urgency(provider: dict, now: datetime | None = None) -> dict:
    """``{"level", "limitKey", "text"}`` for one merged provider."""
    balance = _balance(provider)
    if balance is not None:
        return {
            "level": _balance_level(balance),
            "limitKey": None,
            "text": f"${max(0.0, balance):,.2f} balance",
        }

    now = now or datetime.now(UTC)
    window = focus_window(provider, now)
    if window is None:
        remaining = provider.get("remaining") or {}
        reason = "No key" if provider.get("isPayg") else str(remaining.get("summary") or "")
        return {
            "level": "idle",
            "limitKey": None,
            "text": reason or "Awaiting reading",
        }

    return window_urgency(window, now)


def grade_window(window: dict) -> str:
    """``out``, ``soon``, ``warn`` or ``ok`` for one measured window."""
    left = _remaining_pct(window)
    if left <= 0:
        return "out"
    if _is_drying(window):
        hours = _hours_to_dry(window)
        return "soon" if hours <= SOON_HOURS or left <= SOON_PCT else "warn"
    return "soon" if left <= SOON_PCT else "ok"


def _sort_hours(window: dict | None) -> float:
    """Hours to dry for ordering: only a window that really runs dry before it
    resets (pace ``tight``/``dry``) has any. A ``safe`` forecast's projected
    hours say nothing about urgency, so it sorts as none."""
    if window is None or not _is_drying(window):
        return math.inf
    return _hours_to_dry(window)


def sort_key(provider: dict, now: datetime | None = None) -> tuple:
    """The contract's order: level, then soonest dry (``tight``/``dry`` pace
    only), then least left, then smallest balance."""
    window = focus_window(provider, now)
    balance = _balance(provider)
    if balance is not None:
        level = _balance_level(balance)
    else:
        level = grade_window(window) if window else "idle"
    return (
        LEVEL_RANK.get(level, len(LEVELS)),
        _sort_hours(window),
        _remaining_pct(window) if window else math.inf,
        balance if balance is not None else math.inf,
    )


def display_order(providers, now: datetime | None = None) -> list[dict]:
    """Providers most urgent first, the one order every surface shows.

    Sorted by ``sort_key``. Only when two providers tie exactly do the profiles
    of one family sit together (at the spot of the family's first member in
    the given order); a healthy profile never precedes a more urgent provider.
    """
    now = now or datetime.now(UTC)
    items = [p for p in providers if isinstance(p, dict)]
    first_seen: dict[str, int] = {}
    for index, provider in enumerate(items):
        first_seen.setdefault(profile_family(provider.get("key")), index)
    indexed = list(enumerate(items))
    indexed.sort(
        key=lambda pair: (
            *sort_key(pair[1], now),
            first_seen[profile_family(pair[1].get("key"))],
            pair[0],
        )
    )
    return [provider for _, provider in indexed]


def assign_display_rank(providers: list[dict], now: datetime | None = None) -> None:
    """Stamp ``provider["displayRank"]`` (0..n-1) from :func:`display_order`."""
    for rank, provider in enumerate(display_order(providers, now)):
        provider["displayRank"] = rank
