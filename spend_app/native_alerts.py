"""Low-quota alerts for the native window, independent of the page's timers.

``evaluate_low_quota`` is a pure function: merged provider entries plus the
previous alert state in, notifications plus the new state out. The thresholds
are the ones the window's tones already use (``merge_provider_entries``):
quota windows warn at 20% left and turn critical at 8% left, prepaid balances
warn under $5 and turn critical under $1.

Each (provider, window, level) notifies once per reset cycle. A lane re-arms
when its window resets (``resetsAt`` moves later) or its remaining value
recovers above the level's threshold. The state is plain JSON and is stored in
the preferences file, so a restart does not repeat a notification.
"""

from __future__ import annotations

import logging
import math
import threading
from collections.abc import Callable
from datetime import UTC, datetime

from spend_app.native_prefs import PreferenceStore
from spend_app.timeutil import parse_utc

LOGGER = logging.getLogger(__name__)

WARN_PCT = 20.0
CRITICAL_PCT = 8.0
WARN_USD = 5.0
CRITICAL_USD = 1.0
#: A rolling window's reset time drifts by seconds; a real reset moves it by
#: at least minutes. Only a later ``resetsAt`` beyond this counts as a reset.
RESET_SHIFT_SECONDS = 600.0
STATE_KEY = "alertState"
TOOLTIP_LIMIT = 63  # NotifyIcon.Text rejects 64+ characters on .NET Framework

_TIE_ORDER = {
    "5h": 0,
    "gemini-5h": 1,
    "3p-5h": 2,
    "weekly": 3,
    "gemini-weekly": 4,
    "3p-weekly": 5,
    "monthly": 6,
}

State = dict
Notification = dict


def _number(value) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _display_name(provider: dict) -> str:
    name = str(provider.get("name") or provider.get("key") or "Provider")
    badge = str(provider.get("badge") or "").strip()
    return f"{name} · {badge}" if badge and badge not in name else name


def _percent_level(remaining: float) -> str | None:
    if remaining <= CRITICAL_PCT:
        return "critical"
    if remaining <= WARN_PCT:
        return "warning"
    return None


def _balance_level(balance: float) -> str | None:
    if balance < CRITICAL_USD:
        return "critical"
    if balance < WARN_USD:
        return "warning"
    return None


def provider_lane(provider: dict) -> dict | None:
    """The one lane that represents ``provider``: its prepaid balance, else its
    tightest measured window. ``None`` when nothing is measured yet."""
    funds = _number(provider.get("fundsRemainingUsd"))
    if funds is not None:
        balance = max(0.0, funds)
        return {
            "kind": "balance",
            "window": "balance",
            "label": "balance",
            "value": balance,
            "level": _balance_level(balance),
            "resetsAt": None,
        }
    best = None
    for window in provider.get("windows") or []:
        if not isinstance(window, dict) or window.get("unavailable"):
            continue
        remaining = _number(window.get("remainingPct"))
        if remaining is None:
            continue
        rank = (round(remaining, 1), _TIE_ORDER.get(window.get("limitKey"), 99))
        if best is None or rank < best[0]:
            best = (rank, window, remaining)
    if best is None:
        return None
    _, window, remaining = best
    label = str(window.get("shortLabel") or window.get("limitKey") or "window")
    return {
        "kind": "quota",
        "window": str(window.get("limitKey") or label),
        "label": label,
        "value": remaining,
        "level": _percent_level(remaining),
        "resetsAt": window.get("resetsAt") or None,
    }


def _reset_moved(old, new) -> bool:
    if not old or not new or old == new:
        return False
    old_at, new_at = parse_utc(old), parse_utc(new)
    if old_at is None or new_at is None:
        return True
    return (new_at - old_at).total_seconds() > RESET_SHIFT_SECONDS


def _resets_in(resets_at, now: datetime) -> str:
    stamp = parse_utc(resets_at) if resets_at else None
    if stamp is None:
        return ""
    seconds = int((stamp - now).total_seconds())
    if seconds <= 0:
        return ""
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes = rest // 60
    if days:
        text = f"{days}d {hours}h"
    elif hours:
        text = f"{hours}h {minutes}m"
    else:
        text = f"{max(minutes, 1)}m"
    return f" · resets in {text}"


def _notification(provider: dict, lane: dict, level: str, now: datetime) -> dict:
    name = _display_name(provider)
    critical = level == "critical"
    if lane["kind"] == "balance":
        title = (
            f"{name} balance is almost empty" if critical else f"{name} balance is low"
        )
        message = f"${lane['value']:,.2f} left"
    else:
        title = f"{name} is almost out" if critical else f"{name} is running low"
        message = (
            f"{lane['value']:.0f}% left on the {lane['label']} window"
            + _resets_in(lane["resetsAt"], now)
        )
    return {
        "id": f"{provider['key']}|{lane['window']}|{level}",
        "provider": provider["key"],
        "window": lane["window"],
        "level": level,
        "title": title,
        "message": message,
        "remaining": lane["value"],
    }


def evaluate_low_quota(
    providers: list[dict],
    state: State | None,
    now: datetime | None = None,
    *,
    plan_windows_service_owned: bool = False,
) -> tuple[list[Notification], State]:
    """Decide which notifications to raise and the state to remember.

    Only each provider's tightest lane is considered. Crossing straight into
    critical notifies once (critical), and also marks the warning level as
    delivered. The input state is never mutated.

    Alert ownership: while the local service's notifier is active
    (``plan_windows_service_owned``) it alone raises plan-window alerts, so only
    prepaid-balance lanes are evaluated here and plan-window state is left
    untouched. Otherwise (fallback) both kinds are. A lane is never evaluated in
    both modes, so one lane can never notify twice.
    """
    now = now or datetime.now(UTC)
    lanes: dict[str, dict] = {}
    raw = (state or {}).get("lanes") if isinstance(state, dict) else None
    if isinstance(raw, dict):
        for key, entry in raw.items():
            if isinstance(entry, dict) and isinstance(key, str):
                levels = [
                    lv
                    for lv in entry.get("levels") or []
                    if lv in {"warning", "critical"}
                ]
                lanes[key] = {"resetsAt": entry.get("resetsAt"), "levels": levels}

    notifications: list[Notification] = []
    for provider in providers or []:
        if not isinstance(provider, dict) or not provider.get("key"):
            continue
        lane = provider_lane(provider)
        if lane is None:
            continue  # unmeasured: neither alert nor re-arm
        if plan_windows_service_owned and lane["kind"] != "balance":
            continue
        key = f"{provider['key']}|{lane['window']}"
        entry = lanes.get(key) or {"resetsAt": None, "levels": []}
        delivered = set(entry["levels"])
        if _reset_moved(entry["resetsAt"], lane["resetsAt"]):
            delivered.clear()
        level = lane["level"]
        if level is None:
            delivered.clear()
        elif level == "warning":
            delivered.discard("critical")
        if level and level not in delivered:
            notifications.append(_notification(provider, lane, level, now))
            delivered.add("warning")
            if level == "critical":
                delivered.add("critical")
        if delivered:
            lanes[key] = {
                "resetsAt": lane["resetsAt"] or entry["resetsAt"],
                "levels": sorted(delivered),
            }
        else:
            lanes.pop(key, None)
    return notifications, {"lanes": lanes}


def tooltip_text(providers: list[dict], *, prefix: str = "BURNRATE") -> str:
    """Tray tooltip naming the tightest lane, e.g. ``BURNRATE - Claude Code 12% left (5h)``."""
    best_pct = None
    best_balance = None
    for provider in providers or []:
        if not isinstance(provider, dict):
            continue
        lane = provider_lane(provider)
        if lane is None:
            continue
        if lane["kind"] == "balance":
            if best_balance is None or lane["value"] < best_balance[0]["value"]:
                best_balance = (lane, provider)
        elif best_pct is None or lane["value"] < best_pct[0]["value"]:
            best_pct = (lane, provider)
    # A balance only outranks the quota lanes once it is itself running low and
    # no quota window is at its warning level.
    low_balance = best_balance and best_balance[0]["value"] < WARN_USD
    pct_is_urgent = best_pct is not None and best_pct[0]["value"] <= WARN_PCT
    if best_pct is not None and not (low_balance and not pct_is_urgent):
        lane, provider = best_pct
        detail = (
            f"{_display_name(provider)} {lane['value']:.0f}% left ({lane['label']})"
        )
    elif best_balance is not None:
        lane, provider = best_balance
        detail = f"{_display_name(provider)} ${lane['value']:,.2f} left"
    else:
        return prefix
    text = f"{prefix} - {detail}"
    if len(text) > TOOLTIP_LIMIT:
        text = text[: TOOLTIP_LIMIT - 1].rstrip() + "…"
    return text


class QuotaWatcher:
    """Polls on its own thread so alerts work while the window is hidden."""

    def __init__(
        self,
        fetch_providers: Callable[[], list[dict] | None],
        store: PreferenceStore,
        notify: Callable[[Notification], None],
        set_tooltip: Callable[[str], None],
        *,
        alerts_enabled: Callable[[], bool],
        plan_windows_service_owned: Callable[[], bool] = lambda: False,
        interval: float = 60.0,
        first_delay: float = 8.0,
    ) -> None:
        self._fetch = fetch_providers
        self._store = store
        self._notify = notify
        self._set_tooltip = set_tooltip
        self._alerts_enabled = alerts_enabled
        self._service_owns_plan = plan_windows_service_owned
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="burnrate-quota-watch", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def run_once(self) -> list[Notification]:
        try:
            providers = self._fetch()
        except Exception:
            LOGGER.debug("Quota poll failed", exc_info=True)
            return []
        if providers is None:
            return []
        try:
            self._set_tooltip(tooltip_text(providers))
        except Exception:
            LOGGER.debug("Tray tooltip update skipped", exc_info=True)
        if not self._alerts_enabled():
            return []
        previous = self._store.read().get(STATE_KEY)
        try:
            service_owned = bool(self._service_owns_plan())
        except Exception:  # noqa: BLE001 -- unknown means fall back
            service_owned = False  # unknown: fall back to alerting here
        notifications, state = evaluate_low_quota(
            providers, previous, plan_windows_service_owned=service_owned
        )
        if state != previous:
            # Persist first: a crash after this point drops an alert rather
            # than repeating it on the next launch.
            self._store.update({STATE_KEY: state})
        for item in notifications:
            try:
                self._notify(item)
            except Exception:
                LOGGER.debug("Notification skipped", exc_info=True)
        return notifications

    def _run(self) -> None:
        if self._stop.wait(self._first_delay):
            return
        while True:
            self.run_once()
            if self._stop.wait(self._interval):
                return
