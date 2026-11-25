"""The always-on-top usage tab: its data, its bridge and its window.

The tab is a second frameless pywebview window next to the main one. It never
refreshes anything itself. The main bridge pushes every successful or failed
usage fetch into a :class:`TabModel`; the tab page either receives the new
payload through ``evaluate_js`` or reads it back with ``TabBridge.get_state``.
Which provider shows where, and how urgent it looks, comes from
``provider_urgency`` so the tab and the full window always agree.
"""

from __future__ import annotations

import json
import logging
import re
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

from spend_app import native_geometry
from spend_app.error_codes import ERROR_CATALOG
from spend_app.provider_urgency import (
    LEVEL_RANK,
    compute_urgency,
    display_order,
    focus_window,
    format_hours,
    format_reset,
    grade_window,
    window_has_current_reading,
    window_urgency,
)

LOGGER = logging.getLogger(__name__)

SURFACES = ("full", "tab")
TAB_PREFERENCE_KEY = "tabWindow"
SURFACE_KEY = "surface"
PILL_LIMIT_KEY = "pillLimit"

# Layout, in logical pixels. The page uses the same numbers (they ride along
# in every payload), so the corner pill is exactly as wide as its content.
TAB_HEIGHT = 40
TAB_RADIUS = 20
CARD_GAP = 6
CARD_RADIUS = 14
CARD_WIDTH = 356
#: How close the tab may come to the work area's edge, in logical pixels.
BUDGET_MARGIN = 16

#: Resting corner-pill slot widths (kept 1:1 with tab_ui.html CSS).
PILL_PAD = 10            # 5px left + 5px right inside .tab
HOME_WIDTH = 31          # 29px home + 2px right margin
TIGHT_HOME_WIDTH = 28    # 27px home + 1px right margin
SPEND_WIDTH = 50         # "$127" chip (46px) + 4px right margin
SPEND_WIDE_WIDTH = 58    # "$12.3K" chip (54px) + 4px right margin
DIVIDER_WIDTH = 6        # 1px divider + 5px right margin
PRIMARY_WIDTH = 94       # 20px logo + headline + compact runway ("54% ~1h")
PRIMARY_WIDE_WIDTH = 106 # balance with runway or idle provider name
PRIMARY_COMPACT_WIDTH = 72  # 20px logo + headline only ("54%")
MORE_PILL_WIDTH = 36     # "+9" pill (34px) + 2px left margin
MORE_PILL_WIDE_WIDTH = 42  # "+10" pill (40px) + 2px left margin
TIGHT_MORE_WIDTH = 32
REFRESH_WIDTH = 28       # 26px refresh button + 2px left margin
TIGHT_REFRESH_WIDTH = 25
CHIP_WIDTH = 196         # full error chip (error-only state)
CHIP_CODE_WIDTH = 78     # error code badge ("NET-001")
CHIP_ICON_WIDTH = 28     # warning icon only
LOADING_WIDTH = 92       # "Loading…"
EMPTY_WIDTH = 116        # "No limits yet"
COMPACT_EMPTY_WIDTH = 84 # "No limits"
#: Longer idle reasons show as "Awaiting reading" (the full reason is the tooltip).
IDLE_STATUS_LIMIT = 22
FOLDABLE_LEVELS = ("ok", "balance", "idle")
URGENT_LEVELS = ("out", "soon")

LIMIT_LABELS = {
    "5h": "5-hour",
    "weekly": "Weekly",
    "weekly-fable": "Fable weekly",
    "monthly": "Monthly",
    "gemini-5h": "Gemini 5-hour",
    "gemini-weekly": "Gemini weekly",
    "3p-5h": "3P 5-hour",
    "3p-weekly": "3P weekly",
    "balance": "Balance",
}

WINDOW_TAGS = {
    "5h": "5h",
    "weekly": "Weekly",
    "weekly-fable": "Fable",
    "monthly": "Monthly",
    "gemini-5h": "Gemini 5h",
    "gemini-weekly": "Gemini weekly",
    "3p-5h": "3P 5h",
    "3p-weekly": "3P weekly",
    "balance": "Balance",
}


def normalize_pill_limit(value) -> str | None:
    """Normalized ``provider::limit`` (or ``provider``) selection, or ``None`` for Auto."""
    if value is None:
        return None
    text = str(value).strip()
    if not text or text.lower() == "auto" or len(text) > 160 or any(ch in text for ch in "\r\n\t"):
        return None
    if "::" in text:
        provider, _, limit = text.partition("::")
        provider, limit = provider.strip(), limit.strip()
        if not provider:
            return None
        return f"{provider}::{limit}" if limit else provider
    return text


def split_pill_limit(value: str | None) -> tuple[str, str | None] | None:
    norm = normalize_pill_limit(value)
    if norm is None:
        return None
    if "::" in norm:
        provider, _, limit = norm.partition("::")
        return (provider, limit or None)
    return (norm, None)

_FIRST_SENTENCE = re.compile(r"^(.*?[.!?])(?:\s|$)")


def short_error(message: str, code: str | None) -> str:
    """The plain one-line chip text: first sentence, no full stop, plus the code."""
    text = str(message or "").strip()
    match = _FIRST_SENTENCE.match(text)
    if match:
        text = match.group(1)
    text = text.rstrip(".!? ").strip() or "Something went wrong"
    return f"{text} · {code}" if code else text


def window_label(window: dict) -> str:
    """``5-hour`` / ``Weekly`` / ``Fable weekly`` for a window, falling back to its own label."""
    key = str(window.get("limitKey") or "")
    if key in LIMIT_LABELS:
        return LIMIT_LABELS[key]
    if key.startswith("weekly-"):
        suffix = key[len("weekly-") :].replace("-", " ").strip().title()
        if suffix:
            return f"{suffix} weekly"
    label = str(window.get("label") or window.get("shortLabel") or "Window").strip()
    label = re.sub(r"\s+window$", "", label, flags=re.IGNORECASE)
    return label[:1].upper() + label[1:] if label else "Window"


def _pct(value) -> float | None:
    return float(value) if isinstance(value, (int, float)) else None


def _span(hours) -> str:
    """``1h`` / ``45m`` without the leading tilde."""
    return format_hours(hours).lstrip("~")


def _pace_line(window: dict, level: str, now: datetime) -> dict | None:
    pace = window.get("pace")
    if level == "out":
        reset = format_reset(window.get("resetsAt"), now)
        return {"level": "soon", "text": f"Out of quota{' · ' + reset if reset else ''}"}
    if not isinstance(pace, dict):
        return None
    state = pace.get("state")
    if state in ("tight", "dry"):
        text = f"At this pace: dry in {format_hours(pace.get('hoursToDry'))}"
        before = pace.get("hoursDryBeforeReset")
        if isinstance(before, (int, float)) and before > 0:
            text += f", {_span(before)} before it resets"
        return {"level": level if level in ("soon", "warn") else "warn", "text": text}
    if state == "safe":
        return {"level": "ok", "text": "On pace to last until it resets"}
    return None


def _card_rows(provider: dict, now: datetime) -> list[dict]:
    rows = []
    for window in provider.get("windows") or []:
        left = _pct(window.get("remainingPct"))
        label = window_label(window)
        if not window_has_current_reading(window, now):
            if window.get("unit") == "usd":
                continue
            rows.append(
                {
                    "label": label,
                    "muted": True,
                    "note": str(window.get("reason") or "Awaiting reading"),
                }
            )
            continue
        level = grade_window(window)
        reset = format_reset(window.get("resetsAt"), now)
        rows.append(
            {
                "label": label,
                "muted": False,
                "level": level,
                "leftPct": round(left),
                "usedPct": round(100 - left),
                "resets": reset[:1].upper() + reset[1:] if reset else "",
                "pace": _pace_line(window, level, now),
            }
        )
    return rows


def _balance_card(provider: dict) -> dict:
    runway = provider.get("fundsRunwayInfo") or {}
    days = runway.get("days") if isinstance(runway, dict) else None
    summary = str((provider.get("remaining") or {}).get("summary") or "")
    if isinstance(days, (int, float)) and runway.get("state") == "estimate":
        summary = f"About {round(days)} days left at your current spend"
    return {"amount": provider.get("fundsRemainingUsd"), "summary": summary}


#: How many characters a headline can hold before it would be drawn past its segment.
HEADLINE_CHARS = 9
COMPACT_HEADLINE_CHARS = 6
NAME_CHARS = 13
COMPACT_NAME_CHARS = 8


def abbreviate_money(amount: float, *, compact: bool = False) -> str:
    """A balance headline that fits its segment: exact up to $9,999.99, then
    ``$12.3K`` / ``$1.2M``. A compact segment is tighter: exact to $99.99, whole
    dollars to $999, then ``$2.0K``. The exact amount stays in the tooltip, the
    aria-label and the hover card."""
    amount = max(0.0, float(amount))
    if amount < (100 if compact else 10_000):
        return f"${amount:,.2f}"
    if compact and amount < 1_000:
        return f"${amount:,.0f}"
    thousands = round(amount / 1_000, 1)
    if thousands >= 1_000:
        return f"${amount / 1_000_000:.1f}M"
    return f"${thousands:.1f}K"


def shorten(text: str, limit: int) -> str:
    text = str(text)
    return text if len(text) <= limit else text[: max(1, limit - 1)].rstrip() + "\u2026"


def abbreviate_spend(amount: float) -> str:
    """Compact daily spend for the 40px corner pill: ``$0``, ``$4.20`` under $10,
    whole dollars ``$127`` up to $999, then ``$1.2K`` / ``$1.2M``."""
    amount = max(0.0, float(amount))
    if amount == 0:
        return "$0"
    if amount < 10:
        return f"${amount:.2f}"
    if amount < 1_000:
        return f"${round(amount):,d}"
    thousands = round(amount / 1_000, 1)
    if thousands >= 1_000:
        return f"${amount / 1_000_000:.1f}M"
    return f"${thousands:.1f}K"


def extract_spend(report, today_report=None) -> dict | None:
    """Today's spend, hourly burn rate, and cache hit % for the pill & hover card."""
    source = today_report if isinstance(today_report, dict) else (report if isinstance(report, dict) else None)
    total = None
    if isinstance(today_report, dict) and isinstance(today_report.get("total"), dict):
        total = today_report["total"]
    elif isinstance(report, dict):
        if isinstance(report.get("todayTotal"), dict):
            total = report["todayTotal"]
        elif isinstance(report.get("total"), dict) and report.get("period") in (None, "today"):
            total = report["total"]
    if not isinstance(total, dict) or not isinstance(total.get("valueUsd"), (int, float)):
        return None
    usd = max(0.0, float(total["valueUsd"]))
    complete = total.get("complete") is not False
    suffix = "" if complete else "+"
    compact_label = abbreviate_spend(usd) + suffix
    exact_label = f"${usd:,.2f}{suffix}"

    rate = (source or {}).get("rate") if isinstance(source, dict) else None
    rate_text = ""
    if isinstance(rate, dict) and isinstance(rate.get("perHourUsd"), (int, float)):
        per_h = max(0.0, float(rate["perHourUsd"]))
        rate_suffix = "+" if rate.get("complete") is False else ""
        rate_text = f"${per_h:,.2f}/h{rate_suffix}"

    tools = (source or {}).get("tools") if isinstance(source, dict) else None
    cache_pct: int | None = None
    if isinstance(tools, list):
        cached = fresh = writes = 0.0
        for tool in tools:
            if not isinstance(tool, dict) or tool.get("organization"):
                continue
            cached += max(0.0, float(tool.get("cached") or 0))
            fresh += max(0.0, float(tool.get("input") or 0))
            writes += max(0.0, float(tool.get("writes") or 0))
        prompt = cached + fresh + writes
        if prompt > 0:
            cache_pct = round(cached / prompt * 100)

    meta_parts = [p for p in (rate_text, f"{cache_pct}% cache" if cache_pct is not None else "") if p]
    meta = " · ".join(meta_parts)
    return {
        "usd": round(usd, 2),
        "compact": compact_label,
        "exact": exact_label,
        "complete": complete,
        "rateText": rate_text,
        "cachePct": cache_pct,
        "meta": meta,
        "aria": f"{exact_label} spent today" + (f", {meta}" if meta else ""),
    }


def window_tag(window: dict | None, *, is_balance: bool = False) -> str:
    """Short parenthetical tag for hover-card rows: ``5h`` / ``Weekly`` / ``Fable`` / ``Balance``."""
    if is_balance:
        return "Balance"
    if window is None:
        return ""
    key = str(window.get("limitKey") or "")
    if key in WINDOW_TAGS:
        return WINDOW_TAGS[key]
    if key.startswith("weekly-"):
        suffix = key[len("weekly-") :].replace("-", " ").strip().title()
        if suffix:
            return suffix
    return window_label(window)


def _short_status(level: str, status: str) -> str:
    """Ultra-compact runway/reset token for the 40px pill: ``~1h``, ``out``, ``~42d``, ``5d``."""
    if level == "out":
        return "out"
    if level == "idle":
        return ""
    text = str(status or "").strip()
    if text.startswith("dry in "):
        return text[len("dry in ") :].strip()
    if text.startswith("resets in "):
        return text[len("resets in ") :].strip()
    if text.endswith(" runway"):
        return text[: -len(" runway")].strip()
    return ""


def _card_status(level: str, exact_big: str, status: str, short_status: str) -> str:
    """Right-aligned metric in the overview hover card (e.g. ``54% · dry ~1h``)."""
    if level == "idle":
        return status or "Awaiting reading"
    if level == "out":
        return f"0% · {status}" if status and not status.startswith("out") else (status or "0% · out")
    if level in ("soon", "warn") and short_status and status.startswith("dry in "):
        return f"{exact_big} · dry {short_status}"
    if level == "balance":
        return f"{exact_big} · {short_status}" if short_status else f"{exact_big} · PAYG"
    return f"{exact_big} · {status}" if status else exact_big


def _headline(provider: dict, window: dict | None) -> tuple[str, str, float]:
    """``(exact text, small unit label, fuel percent)`` for a segment."""
    if (provider.get("remaining") or {}).get("kind") == "balance":
        balance = provider.get("fundsRemainingUsd")
        amount = max(0.0, float(balance)) if isinstance(balance, (int, float)) else 0.0
        fuel = 100.0 if amount >= 25 else max(6.0, amount / 25 * 100)
        return f"${amount:,.2f}", "balance", fuel
    if window is None:
        return "—", "", 0.0
    left = _pct(window.get("remainingPct")) or 0.0
    return f"{round(left)}%", window_label(window).lower(), left


def _headline_forms(provider: dict, exact: str, level: str, name: str) -> tuple[str, str]:
    """``(normal, compact)`` headline: abbreviated only when it would not fit."""
    if level == "idle":  # the headline of an idle segment is its name
        return shorten(name, NAME_CHARS), shorten(name, COMPACT_NAME_CHARS)
    if (provider.get("remaining") or {}).get("kind") == "balance":
        balance = provider.get("fundsRemainingUsd")
        amount = float(balance) if isinstance(balance, (int, float)) else 0.0
        return abbreviate_money(amount), abbreviate_money(amount, compact=True)
    return shorten(exact, HEADLINE_CHARS), shorten(exact, COMPACT_HEADLINE_CHARS)


def build_segment(provider: dict, now: datetime, *, limit_key: str | None = None) -> dict:
    is_balance_provider = (provider.get("remaining") or {}).get("kind") == "balance"
    is_balance = is_balance_provider if limit_key is None else (limit_key == "balance" and (is_balance_provider or provider.get("fundsRemainingUsd") is not None))
    if limit_key is None or (limit_key == "balance" and is_balance_provider):
        urgency = compute_urgency(provider, now)
        window = focus_window(provider, now)
    elif is_balance:
        urgency = compute_urgency(provider, now)
        window = None
    else:
        target = next(
            (w for w in (provider.get("windows") or []) if str(w.get("limitKey") or "") == limit_key),
            None,
        )
        if target is None:
            urgency = compute_urgency(provider, now)
            window = focus_window(provider, now)
            is_balance = is_balance_provider
        elif window_has_current_reading(target, now):
            window = target
            urgency = window_urgency(target, now)
        else:
            window = target
            reason = str(target.get("reason") or "Awaiting reading")
            urgency = {"level": "idle", "limitKey": limit_key, "text": reason}

    level = urgency.get("level") or "idle"
    if is_balance:
        balance = provider.get("fundsRemainingUsd")
        amount = max(0.0, float(balance)) if isinstance(balance, (int, float)) else 0.0
        fuel = 100.0 if amount >= 25 else max(6.0, amount / 25 * 100)
        big, unit = f"${amount:,.2f}", "balance"
    elif window is not None and level != "idle":
        left = _pct(window.get("remainingPct")) or 0.0
        big, unit, fuel = f"{round(left)}%", window_label(window).lower(), left
    else:
        big, unit, fuel = "—", window_label(window).lower() if window is not None else "", 0.0

    profile = provider.get("profile")
    unit_label = f"{profile} · {unit}" if profile and unit and unit != "balance" else unit
    if profile and unit == "balance":
        unit_label = f"{profile} · balance"
    status = detail = str(urgency.get("text") or "")
    exact_big = big
    if level == "idle":
        big, unit_label, fuel = str(provider.get("name") or provider.get("key") or ""), profile or "", 0.0
        exact_big = big
        status = status or "Awaiting reading"
        if len(status) > IDLE_STATUS_LIMIT:
            status = "Awaiting reading"
    if level == "balance":
        runway = provider.get("fundsRunwayInfo") or {}
        days = runway.get("days") if isinstance(runway, dict) else None
        status = f"~{round(days)}d runway" if isinstance(days, (int, float)) else "pay as you go"
    name = str(provider.get("name") or provider.get("key") or "")
    title = f"{name} · {profile}" if profile else name
    aria = f"{title}: {exact_big} {unit}".strip() + (f", {status}" if status else "")
    big_normal, big_compact = _headline_forms(provider, exact_big, level, name)
    short_st = _short_status(level, status)
    pill_big = big_compact if level in ("balance", "idle") else big_normal
    w_tag = window_tag(window, is_balance=is_balance)
    card_st = _card_status(level, exact_big, status, short_st)
    return {
        "key": provider.get("key"),
        "name": name,
        "profile": profile,
        "title": title,
        "plan": provider.get("plan") or "",
        "level": level,
        "limitKey": urgency.get("limitKey") or (limit_key if window is not None else ("balance" if is_balance else None)),
        "windowTag": w_tag,
        "big": big_normal,
        "bigCompact": big_compact,
        "pillBig": pill_big,
        "exact": exact_big,
        "unit": unit_label,
        "fuel": round(fuel, 1),
        "status": status,
        "shortStatus": short_st,
        "cardStatus": card_st,
        "detail": detail or status,
        "muted": level == "idle",
        "aria": aria,
        "card": {
            "rows": _card_rows(provider, now),
            "balance": _balance_card(provider) if provider.get("fundsRemainingUsd") is not None else None,
            "summary": str((provider.get("remaining") or {}).get("summary") or ""),
        },
    }


def build_pill_options(providers: list[dict], now: datetime) -> list[dict]:
    """Selectable usage-limit targets for pinning to the compact corner pill."""
    options: list[dict] = []
    for provider in providers:
        key = str(provider.get("key") or "").strip()
        if not key:
            continue
        name = str(provider.get("name") or key)
        profile = provider.get("profile")
        title = f"{name} · {profile}" if profile else name
        windows = [
            w
            for w in (provider.get("windows") or [])
            if isinstance(w, dict) and w.get("unit") != "usd" and str(w.get("limitKey") or "").strip()
        ]
        for w in windows:
            lk = str(w.get("limitKey") or "").strip()
            w_lbl = window_label(w)
            w_tag = window_tag(w)
            if window_has_current_reading(w, now):
                urg = window_urgency(w, now)
                lvl = urg["level"]
                left = round(_pct(w.get("remainingPct")) or 0.0)
                st = _short_status(lvl, urg["text"])
                stat = f"{left}% · {st}" if st else f"{left}%"
            else:
                lvl = "idle"
                stat = "Awaiting"
            options.append(
                {
                    "id": f"{key}::{lk}",
                    "providerKey": key,
                    "limitKey": lk,
                    "key": key,
                    "title": title,
                    "windowLabel": w_lbl,
                    "windowTag": w_tag,
                    "level": lvl,
                    "stat": stat,
                }
            )
        is_bal = (provider.get("remaining") or {}).get("kind") == "balance"
        if is_bal or (provider.get("fundsRemainingUsd") is not None and not windows):
            bal = provider.get("fundsRemainingUsd")
            amt = max(0.0, float(bal)) if isinstance(bal, (int, float)) else None
            stat = f"${amt:,.2f}" if amt is not None else "No key"
            lvl = "balance" if amt is not None else "idle"
            options.append(
                {
                    "id": f"{key}::balance",
                    "providerKey": key,
                    "limitKey": "balance",
                    "key": key,
                    "title": title,
                    "windowLabel": "Balance",
                    "windowTag": "Balance",
                    "level": lvl,
                    "stat": stat,
                }
            )
    return options


def tab_providers(providers, now: datetime | None = None) -> list[dict]:
    """Providers the tab can say something about, most urgent first.

    The same rule as the full window applies. A render-time clock lets a
    cached reading expire between refreshes.
    """
    usable = [
        p
        for p in providers or []
        if isinstance(p, dict) and (p.get("remaining") or {}).get("kind") != "usage_only"
    ]
    if now is None and usable and all(isinstance(p.get("displayRank"), int) for p in usable):
        return sorted(usable, key=lambda p: p["displayRank"])
    return display_order(usable, now)


class _Step:
    """One rung of the Option 4B pill fit ladder."""

    def __init__(self, chip, *, show_spend: bool, show_status: bool, compact: bool, tight: bool):
        self.chip = chip
        self.show_spend = show_spend
        self.show_status = show_status
        self.compact = compact
        self.tight = tight


#: Tried in order until the corner pill fits the monitor budget:
#: shorten error chip -> drop runway tag -> hide spend chip on pill (stays in card) -> tighten chrome.
FIT_LADDER = (
    _Step("auto", show_spend=True, show_status=True, compact=False, tight=False),
    _Step("short", show_spend=True, show_status=True, compact=False, tight=False),
    _Step("icon", show_spend=True, show_status=True, compact=False, tight=False),
    _Step("icon", show_spend=True, show_status=False, compact=True, tight=False),
    _Step("icon", show_spend=False, show_status=False, compact=True, tight=False),
    _Step("icon", show_spend=False, show_status=False, compact=True, tight=True),
)
_CHIP_WIDTHS = {"full": CHIP_WIDTH, "code": CHIP_CODE_WIDTH, "icon": CHIP_ICON_WIDTH}


def _resolve_chip_mode(step_chip: str, *, state: str, has_error: bool, has_code: bool) -> str:
    if not has_error:
        return "none"
    if state == "error":  # error-only (no primary provider): full -> code -> icon
        if step_chip == "auto":
            return "full"
        if step_chip == "short":
            return "code" if has_code else "icon"
        return "icon"
    # Stale (error alongside live provider): code -> icon to keep the corner pill sleek
    if step_chip == "auto":
        return "code" if has_code else "icon"
    return "icon"


def _more_info(items: list[dict]) -> dict | None:
    if not items:
        return None
    urgent_count = sum(1 for s in items if s["level"] in URGENT_LEVELS)
    warn_count = sum(1 for s in items if s["level"] == "warn")
    severity = "soon" if urgent_count > 0 else ("warn" if warn_count > 0 else "ok")
    return {
        "count": len(items),
        "severity": severity,
        "urgentCount": urgent_count,
        "items": [
            {
                "key": s["key"],
                "title": s["title"],
                "level": s["level"],
                "windowTag": s.get("windowTag", ""),
                "text": s.get("cardStatus") or f"{s.get('exact', s['big'])} {s['unit']}".strip(),
            }
            for s in items
        ],
    }


def _build_overview(segments: list[dict]) -> dict:
    """Structured rows for the Option 4B hover-to-peek overview card."""
    urgent_warn = [s for s in segments if s["level"] not in FOLDABLE_LEVELS]
    if urgent_warn:
        highlighted = urgent_warn
        rest = [s for s in segments if s["level"] in FOLDABLE_LEVELS]
    else:
        highlighted = segments[:3]
        rest = segments[3:]

    n_rest = len(rest)
    n_ok = sum(1 for s in rest if s["level"] == "ok")
    n_bal = sum(1 for s in rest if s["level"] == "balance")
    n_idle = sum(1 for s in rest if s["level"] == "idle")
    if n_rest == 0:
        rest_summary, rest_status = "", ""
    elif n_ok > 0 and n_bal > 0:
        rest_summary = f"{n_rest} other {'provider' if n_rest == 1 else 'providers'} healthy / balances"
        rest_status = "All fine"
    elif n_bal == n_rest:
        rest_summary = f"{n_rest} pay-as-you-go {'balance' if n_rest == 1 else 'balances'}"
        rest_status = "All fine"
    elif n_idle == n_rest:
        rest_summary = f"{n_rest} other {'provider' if n_rest == 1 else 'providers'} awaiting reading"
        rest_status = "Idle"
    else:
        rest_summary = f"{n_rest} other {'provider' if n_rest == 1 else 'providers'} healthy"
        rest_status = "All fine"

    return {
        "single": len(segments) == 1,
        "highlighted": highlighted,
        "rest": [
            {
                "key": s["key"],
                "title": s["title"],
                "windowTag": s.get("windowTag", ""),
                "level": s["level"],
                "text": s.get("cardStatus") or s.get("status") or "",
            }
            for s in rest
        ],
        "restSummary": rest_summary,
        "restStatus": rest_status,
    }


def fit_pill_layout(
    segments: list[dict],
    *,
    state: str,
    spend: dict | None,
    has_error: bool,
    has_code: bool,
    budget: float | None,
) -> dict:
    """Plan the Option 4B corner pill for ``budget`` logical pixels (``None``: no limit).

    The resting pill always stays ultra-compact: brand mark (+ urgent badge),
    optional daily spend chip, #1 most-urgent limit, optional ``+N`` overflow
    pill, optional error indicator, and quick refresh button.
    """
    primary = segments[0] if segments else None
    rest = segments[1:] if len(segments) > 1 else []
    spend_text = spend["compact"] if isinstance(spend, dict) else ""
    spend_w = (SPEND_WIDE_WIDTH if len(spend_text) >= 5 else SPEND_WIDTH) if spend_text else 0
    more_base_w = (MORE_PILL_WIDE_WIDTH if len(rest) >= 10 else MORE_PILL_WIDTH) if rest else 0
    primary_normal_w = (
        PRIMARY_WIDE_WIDTH
        if primary is not None and (primary["level"] in ("balance", "idle") or len(primary.get("pillBig", "")) >= 5)
        else PRIMARY_WIDTH
    )

    chosen = None
    for step in FIT_LADDER:
        chip_mode = _resolve_chip_mode(step.chip, state=state, has_error=has_error, has_code=has_code)
        home_w = TIGHT_HOME_WIDTH if step.tight else HOME_WIDTH
        refresh_w = TIGHT_REFRESH_WIDTH if step.tight else REFRESH_WIDTH
        show_spend = bool(spend_text) and step.show_spend and state in ("ready", "empty")
        show_divider = show_spend and (primary is not None or state == "empty")
        cur_spend_w = spend_w if show_spend else 0
        cur_div_w = DIVIDER_WIDTH if show_divider else 0
        chip_w = _CHIP_WIDTHS.get(chip_mode, 0) if has_error else 0

        if state == "loading":
            body_w = LOADING_WIDTH
            primary_w = 0
            more_w = 0
        elif state == "empty":
            body_w = COMPACT_EMPTY_WIDTH if step.compact else EMPTY_WIDTH
            primary_w = 0
            more_w = 0
        elif state == "error":
            body_w = 0
            primary_w = 0
            more_w = 0
        else:  # ready
            body_w = 0
            has_short = bool(primary and primary.get("shortStatus")) and step.show_status
            primary_w = (
                PRIMARY_COMPACT_WIDTH
                if (step.compact or not has_short) and primary is not None and primary["level"] not in ("balance", "idle")
                else (PRIMARY_COMPACT_WIDTH if step.compact else primary_normal_w)
            )
            more_w = (TIGHT_MORE_WIDTH if step.tight else more_base_w) if rest else 0

        width = PILL_PAD + home_w + cur_spend_w + cur_div_w + body_w + primary_w + more_w + chip_w + refresh_w
        chosen = {
            "shown": [primary] if primary is not None else [],
            "primary": primary,
            "rest": rest,
            "width": width,
            "spendWidth": (cur_spend_w - 4) if show_spend else 0,
            "primaryWidth": primary_w,
            "moreWidth": (more_w - 2) if more_w else 0,
            "mode": {
                "chip": chip_mode if has_error else "none",
                "showSpend": show_spend,
                "showStatus": step.show_status,
                "compact": step.compact,
                "tight": step.tight,
            },
        }
        if budget is None or width <= budget:
            break
    return chosen


class TabModel:
    """Last known usage, daily spend, and last error; the single source for the tab."""

    def __init__(self, *, pill_limit: str | None = None) -> None:
        self._lock = threading.Lock()
        self._providers: list[dict] = []
        self._spend: dict | None = None
        self._loaded = False
        self._error: dict | None = None
        self._pill_limit: str | None = normalize_pill_limit(pill_limit)
        self._listeners: list = []
        self._updated = 0.0

    def subscribe(self, listener) -> None:
        self._listeners.append(listener)

    @property
    def pill_limit(self) -> str | None:
        with self._lock:
            return self._pill_limit

    def set_pill_limit(self, value: str | None) -> str | None:
        normalized = normalize_pill_limit(value)
        with self._lock:
            changed = normalized != self._pill_limit
            self._pill_limit = normalized
        if changed:
            self._notify()
        return normalized

    @property
    def loaded(self) -> bool:
        return self._loaded

    @property
    def age(self) -> float:
        """Seconds since the last successful fetch (``inf`` before the first)."""
        return time.monotonic() - self._updated if self._loaded else float("inf")

    def publish_report(self, report, *, today_report=None) -> None:
        providers = report.get("providers") if isinstance(report, dict) else None
        if not isinstance(providers, list):
            return
        quota_failed = report.get("quotaStatus") in ("stale", "unavailable")
        spend = extract_spend(report, today_report=today_report)
        with self._lock:
            self._providers = providers
            self._spend = spend
            self._loaded = True
            self._error = (
                {"message": "Quota refresh failed.", "code": None, "text": "Quota refresh failed"}
                if quota_failed else None
            )
            self._updated = time.monotonic()
        self._notify()

    def publish_error(self, message: str, code: str | None, *, retain_providers: bool = True) -> None:
        with self._lock:
            if not retain_providers:
                self._providers = []
                self._spend = None
            self._error = {"message": str(message or ""), "code": code, "text": short_error(message, code)}
        self._notify()

    def refresh(self) -> None:
        """Re-render with the data already held (the theme changed, say)."""
        self._notify()

    def _notify(self) -> None:
        for listener in list(self._listeners):
            try:
                listener()
            except Exception:  # noqa: BLE001 -- a dead tab must not break a refresh
                LOGGER.debug("Tab listener failed", exc_info=True)

    def payload(
        self,
        *,
        dark: bool,
        demo: bool = False,
        now: datetime | None = None,
        budget: float | None = None,
    ) -> dict:
        """What the tab page draws. ``budget`` is the logical width the current
        monitor can spare; the corner pill shrinks along ``FIT_LADDER`` so
        nothing is ever cut off."""
        now = now or datetime.now(UTC)
        with self._lock:
            providers = list(self._providers)
            spend = dict(self._spend) if self._spend else None
            loaded = self._loaded
            error = dict(self._error) if self._error else None
            pill_limit = self._pill_limit
        ordered = tab_providers(providers, now)
        segments = [build_segment(p, now) for p in ordered]
        pill_options = build_pill_options(ordered, now)

        pinned_pair = split_pill_limit(pill_limit)
        matched_provider = None
        if pinned_pair is not None:
            p_key, p_limit = pinned_pair
            matched_provider = next((p for p in ordered if str(p.get("key") or "") == p_key), None)
        if matched_provider is not None:
            pinned_seg = build_segment(matched_provider, now, limit_key=pinned_pair[1])
            pinned_seg["pinned"] = True
            pinned_seg["aria"] = f"Pinned · {pinned_seg['aria']}"
            pill_segments = [pinned_seg] + [s for s in segments if s["key"] != matched_provider.get("key")]
            tag = pinned_seg.get("windowTag") or ""
            pill_label = f"{pinned_seg['title']} ({tag})" if tag else pinned_seg["title"]
        else:
            pill_segments = segments
            pill_label = "Auto"

        if not loaded and error is None:
            state = "loading"
        elif not segments:
            state = "error" if error else "empty"
        else:
            state = "ready"
        plan = fit_pill_layout(
            pill_segments,
            state=state,
            spend=spend,
            has_error=bool(error),
            has_code=bool(error and error.get("code")),
            budget=budget,
        )
        urgent = sum(1 for s in segments if s["level"] in URGENT_LEVELS)
        card_width = CARD_WIDTH if budget is None else int(max(1, min(CARD_WIDTH, budget)))
        return {
            "state": state,
            "dark": bool(dark),
            "demo": bool(demo),
            "stale": bool(error) and bool(segments),
            "error": error,
            "spend": spend,
            "primary": plan["primary"],
            "segments": plan["shown"],
            "more": _more_info(plan["rest"]),
            "overview": _build_overview(segments),
            "urgentCount": urgent,
            "pillLimit": pill_limit,
            "pillPinned": matched_provider is not None,
            "pillLabel": pill_label,
            "pillOptions": pill_options,
            "layout": {
                "height": TAB_HEIGHT,
                "width": plan["width"],
                "budget": budget,
                "mode": plan["mode"],
                "spendWidth": plan["spendWidth"],
                "primaryWidth": plan["primaryWidth"],
                "moreWidth": plan["moreWidth"],
                "cardWidth": card_width,
                "cardGap": CARD_GAP,
            },
        }


# --- the page ---------------------------------------------------------------

SPRITE_START = "<!-- Official Vector Provider Brand Symbols -->"
BRAND_JS_START = "const MONO_DARK"
BRAND_JS_END_FUNCTION = "function logoHtml("


def extract_brand_assets(full_html: str) -> tuple[str, str]:
    """The provider logo sprite and the brand-colour JS, read out of the full
    window's page so the two pages share one source and never drift."""
    start = full_html.find(SPRITE_START)
    end = full_html.find("</svg>", start)
    if start < 0 or end < 0:
        raise ValueError("The logo sprite is missing from wealthfront_ui.html")
    sprite = full_html[start : end + len("</svg>")]
    js_start = full_html.find(BRAND_JS_START)
    fn_start = full_html.find(BRAND_JS_END_FUNCTION, js_start)
    fn_end = full_html.find("\n}\n", fn_start)
    if js_start < 0 or fn_start < 0 or fn_end < 0:
        raise ValueError("The brand table is missing from wealthfront_ui.html")
    return sprite, full_html[js_start : fn_end + 2]


def load_tab_html(theme_pref: str = "system") -> str:
    """tab_ui.html with the shared logos, the embedded font and the saved theme."""
    from spend_app.wealthfront_window import apply_theme_pref, embedded_font_css

    root = Path(__file__).parent
    html = (root / "tab_ui.html").read_text(encoding="utf-8")
    sprite, brand_js = extract_brand_assets((root / "wealthfront_ui.html").read_text(encoding="utf-8"))
    html = html.replace("<!--__BRAND_SPRITE__-->", sprite, 1).replace("/*__BRAND_JS__*/", brand_js, 1)
    html = html.replace("/*__EMBEDDED_FONTS__*/", embedded_font_css(), 1)
    return apply_theme_pref(html, theme_pref)


# --- the page-facing bridge ------------------------------------------------


class TabBridge:
    """``js_api`` of the tab window. Every public method is callable from JS.

    The tab never refreshes anything: the full window's page owns refreshing
    (it keeps polling while hidden), and every result lands in the model that
    this bridge only reads.
    """

    #: Longest a getter waits for a refresh the main window already started.
    REFRESH_WAIT = 3.0

    def __init__(self, bridge, model: TabModel, controller=None) -> None:
        self._bridge = bridge
        self._model = model
        self._controller = controller
        self._payload_lock = threading.Lock()
        self._revision = 0

    def _attach_controller(self, controller) -> None:
        self._controller = controller

    def _dark(self) -> bool:
        from spend_app.wealthfront_window import resolve_dark

        effective = getattr(self._bridge, "effective_dark", None)
        return bool(effective) if effective is not None else resolve_dark(self._bridge.theme)

    def _build_payload(self) -> dict:
        # Push callbacks and page polls share one snapshot order, including changes
        # to layout, time-sensitive readings and theme. A new page has a new bridge.
        with self._payload_lock:
            budget = self._controller.tab.logical_budget() if self._controller is not None else None
            payload = self._model.payload(dark=self._dark(), demo=self._bridge.demo, budget=budget)
            self._revision += 1
            payload["revision"] = self._revision
            return payload

    def get_state(self) -> dict:
        """The current payload. If a refresh is already in flight, wait for its
        result instead of reading stale data; never start one."""
        self._bridge._wait_for_refresh(self.REFRESH_WAIT)
        return self._build_payload()

    def open_full(self) -> dict:
        if self._controller is not None:
            self._controller.show_full()
        return {"ok": True}

    def refresh_now(self) -> dict:
        """Ask the main window's page to trigger an immediate usage & quota refresh."""
        if self._controller is not None and hasattr(self._controller.main, "refresh_page"):
            self._controller.main.refresh_page()
        elif hasattr(self._bridge, "call_page"):
            self._bridge.call_page("refresh")
        return {"ok": True}

    def set_pill_limit(self, value: str | None = None) -> dict:
        """Pin a specific provider/limit to the corner pill (or ``None`` for Auto)."""
        if hasattr(self._bridge, "set_pill_limit"):
            result = self._bridge.set_pill_limit(value)
            chosen = result.get("pillLimit") if isinstance(result, dict) else normalize_pill_limit(value)
        else:
            chosen = self._model.set_pill_limit(value)
        return {"ok": True, "pillLimit": chosen, "state": self._build_payload()}

    def begin_drag(self) -> None:
        if self._controller is not None:
            self._controller.tab.begin_drag()

    def press(self) -> dict:
        """The page saw the primary button go down: remember where the pointer and
        the window were, so a drag that starts a few pixels later does not lag."""
        if self._controller is not None:
            self._controller.tab.note_press()
        return {"ok": True}

    def set_focus(self, inside: bool) -> dict:
        """The page reports whether a keyboard focus ring is inside the tab (the
        tab stays fully opaque while it is)."""
        if self._controller is not None:
            self._controller.tab.keyboard_focus = bool(inside)
        return {"ok": True}

    def expand(
        self,
        width: float,
        height: float,
        card_x: float | None = None,
        card_width: float | None = None,
        card_height: float | None = None,
    ) -> dict:
        """Grow for a hover card; ``card_*`` is the card's rectangle in CSS pixels
        so only the strip and the card stay visible."""
        if self._controller is None:
            return {"ok": False}
        card = None
        if None not in (card_x, card_width, card_height):
            card = (
                clamp_number(card_x, 0, 2400),
                clamp_number(card_width, 1, 2400),
                clamp_number(card_height, 1, 1200),
            )
        return {"ok": True, **self._controller.tab.expand(width, height, card)}

    def collapse(self, instant: bool = False) -> dict:
        if self._controller is not None:
            self._controller.tab.collapse(instant=bool(instant))
        return {"ok": True}


# --- the window ------------------------------------------------------------


def _scaled(value: float, dpi: int) -> int:
    return int(round(float(value) * max(dpi, 1) / 96))


def shape_rects(
    size: tuple[int, int],
    scale: float,
    card: tuple[float, float, float] | None = None,
    up: bool = False,
    strip: tuple[float, float] | None = None,
) -> list[tuple[int, int, int, int, int]]:
    """The visible parts of the tab window as ``(left, top, right, bottom, radius)``
    in physical pixels: the pill strip, plus the hover card while one is open.

    ``card`` is ``(x, width, height)`` in logical pixels. ``strip`` is the pill's
    ``(x, width)`` in logical pixels (defaults to spanning the full window width).
    Everything outside these rectangles is cut away by the window region, so it
    is invisible and clicks go through to whatever is underneath.
    """
    width, height = size
    tab_h = min(height, int(round(TAB_HEIGHT * scale)))
    tab_radius = int(round(TAB_RADIUS * scale))
    if card is None:
        return [(0, 0, width, height, tab_radius)]
    card_x, card_w, card_h = card
    tab_top = height - tab_h if up else 0
    gap_px = int(round(CARD_GAP * scale))
    card_h_px = int(round(card_h * scale))
    if up:
        card_bottom = max(0, tab_top - gap_px)
        card_top = max(0, card_bottom - card_h_px)
    else:
        card_top = tab_h + gap_px
        card_bottom = min(height, card_top + card_h_px)
    if strip is None:
        strip_left, strip_right = 0, width
    else:
        strip_x, strip_w = strip
        strip_left = max(0, min(width, int(round(strip_x * scale))))
        strip_right = max(strip_left, min(width, strip_left + int(round(strip_w * scale))))
    rects = [(strip_left, tab_top, strip_right, tab_top + tab_h, tab_radius)]
    if card_h_px > 0 and card_bottom > card_top:
        rects.append(
            (
                int(round(card_x * scale)),
                card_top,
                min(width, int(round((card_x + card_w) * scale))),
                card_bottom,
                int(round(CARD_RADIUS * scale)),
            )
        )
    return rects


def clamp_number(value, low: float, high: float) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return low
    if number != number:
        return low
    return max(low, min(number, high))


class TabHost:
    """Owns the tab's pywebview window: placement, size, drag, show and hide."""

    def __init__(self, window, *, store, hwnd_box: list[int] | None = None, icon_path: Path | None = None, persist: bool = True) -> None:
        self.window = window
        self.hwnd_box = hwnd_box if hwnd_box is not None else [0]
        self._store = store
        self._icon_path = icon_path
        self._persist = persist
        self.tracker = native_geometry.GeometryTracker(
            store,
            lambda: self.hwnd_box[0],
            snapshotter=self._snapshot,
            key=TAB_PREFERENCE_KEY,
        )
        if not persist:
            self.tracker.disable()
        self._base = (PILL_PAD + HOME_WIDTH + LOADING_WIDTH + REFRESH_WIDTH, TAB_HEIGHT)  # logical (w, h)
        self._expanded: tuple[float, float] | None = None
        self._card: tuple[float, float, float] | None = None
        self._strip_x = 0.0
        self._up = False
        #: Generation token & current logical card height for smooth Win32 region reveal/retract.
        self._anim_gen = 0
        self._anim_card_h = 0.0
        self.animate_region = True
        #: Physical top-left of the collapsed tab for the whole expansion session.
        self._anchor: tuple[int, int] | None = None
        self.on_reflow = None
        #: Called after the tab has been shown (the fader starts it fully opaque).
        self.on_shown = None
        #: Whether a keyboard focus ring is inside the page (reported by the page).
        self.keyboard_focus = False
        self._dragging = False
        self._press: tuple | None = None
        self._settle_timer: threading.Timer | None = None
        self._last_budget: float | None = None
        self._reflowing = False
        self._layout_revision = 0
        self.visible = False
        self.quitting = False
        self._lock = threading.RLock()

    @property
    def hwnd(self) -> int:
        return int(self.hwnd_box[0] or 0)

    # -- fading support (read by WindowOpacityController) ------------------------

    def _strip_spec(self) -> tuple[float, float] | None:
        if self._expanded is None:
            return None
        return (self._strip_x, self._base[0])

    def visible_screen_rects(self) -> list[tuple[int, int, int, int]]:
        """The visible parts (strip, plus the card while one is open) as screen
        rectangles ``(left, top, right, bottom)``; everything else is cut away."""
        rect = native_geometry.window_rect(self.hwnd)
        if rect is None:
            return []
        scale = max(self._dpi(), 1) / 96
        return [
            (rect.left + left, rect.top + top, rect.left + right, rect.top + bottom)
            for left, top, right, bottom, _radius in shape_rects(
                (rect.width, rect.height), scale, self._card, self._up, self._strip_spec()
            )
        ]

    def contains_screen_point(self, point, margin: int = 4) -> bool:
        x, y = point
        return any(
            left - margin <= x <= right + margin and top - margin <= y <= bottom + margin
            for left, top, right, bottom in self.visible_screen_rects()
        )

    def holds_opaque(self) -> bool:
        """The tab stays fully opaque while a hover card is open or the keyboard
        focus ring is inside it."""
        return self._expanded is not None or self.keyboard_focus

    def prepare_layered(self) -> None:
        """Make the (still hidden) form a layered window at full alpha, so the
        first fade later is only an alpha change: no restyle while it is on
        screen, and no flash."""
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            style = user32.GetWindowLongW(self.hwnd, -20)
            if not (style & 0x00080000):
                user32.SetWindowLongW(self.hwnd, -20, style | 0x00080000)
            user32.SetLayeredWindowAttributes(self.hwnd, 0, 255, 0x02)
        except (OSError, AttributeError):
            LOGGER.warning("Could not make the tab a layered window", exc_info=True)

    # -- staying on screen after a drag -------------------------------------------

    def note_moved(self) -> None:
        """The window moved (a drag, a monitor change): once the mouse is up and
        it has been still for a moment, pull it back inside the work area."""
        timer = self._settle_timer
        if timer is not None:
            timer.cancel()
        timer = threading.Timer(0.5, self._settle)
        timer.daemon = True
        self._settle_timer = timer
        timer.start()

    def _settle(self) -> None:
        if self.quitting or not self.visible or self._expanded is not None:
            return
        if native_geometry.left_button_down():
            self.note_moved()  # still dragging: look again later
            return
        try:
            self.hide_taskbar_button()
            with self._lock:
                self.fit()
            self.tracker.notify_changed()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Could not settle the tab after a move", exc_info=True)

    def _snapshot(self, hwnd: int):
        """Never save while a hover card has stretched (or shifted) the window."""
        if self._expanded is not None:
            return None
        return native_geometry.snapshot(hwnd)

    # -- physical placement --------------------------------------------------

    def _dpi(self) -> int:
        return native_geometry.window_dpi(self.hwnd)

    def _apply(self, x: int | None, y: int | None, width: int, height: int) -> None:
        """SetWindowPos without activating or showing (pywebview's resize shows)."""
        if sys.platform != "win32" or not self.hwnd:
            return
        import ctypes

        flags = 0x0004 | 0x0010 | 0x4000  # NOZORDER | NOACTIVATE | ASYNCWINDOWPOS
        if x is None or y is None:
            flags |= 0x0002  # SWP_NOMOVE
            x = y = 0
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, x, y, width, height, flags)

    def _set_region(self, rects: list[tuple[int, int, int, int, int]]) -> None:
        """Cut the window down to ``rects`` (rounded), invisible outside them."""
        if sys.platform != "win32" or not self.hwnd or not rects:
            return
        import ctypes

        gdi32, user32 = ctypes.windll.gdi32, ctypes.windll.user32
        gdi32.CreateRoundRectRgn.restype = ctypes.c_void_p
        gdi32.CombineRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        user32.SetWindowRgn.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_int]
        region = None
        for left, top, right, bottom, radius in rects:
            part = gdi32.CreateRoundRectRgn(left, top, right + 1, bottom + 1, radius * 2, radius * 2)
            if region is None:
                region = part
            else:
                gdi32.CombineRgn(region, region, part, 2)  # RGN_OR
                gdi32.DeleteObject(part)
        if not user32.SetWindowRgn(self.hwnd, region, True):
            gdi32.DeleteObject(region)  # the window only owns a region it accepted

    def _apply_shape(self) -> None:
        width, height = self._target_size()
        self._set_region(
            shape_rects((width, height), self._dpi() / 96, self._card, self._up, self._strip_spec())
        )

    def _target_size(self) -> tuple[int, int]:
        dpi = self._dpi()
        width, height = self._expanded or self._base
        return _scaled(width, dpi), _scaled(height, dpi)

    def _current(self) -> native_geometry.Rect | None:
        return native_geometry.window_rect(self.hwnd)

    def _base_size(self) -> tuple[int, int]:
        dpi = self._dpi()
        return _scaled(self._base[0], dpi), _scaled(self._base[1], dpi)

    def _place_session(self) -> tuple[int, int, bool]:
        """Position of the grown window, always computed from the collapsed anchor."""
        width, height = self._target_size()
        base_w, base_h = self._base_size()
        return native_geometry.place_expanded(
            (self._anchor[0], self._anchor[1], base_w, base_h),
            (width, height),
            native_geometry.enum_monitors(),
        )

    def _update_strip_offset(self, expanded_x: int, new_w: int) -> None:
        if self._anchor is None:
            self._strip_x = 0.0
            return
        scale = max(self._dpi(), 1) / 96
        base_w, _ = self._base_size()
        shift_px = max(0, min(max(0, new_w - base_w), self._anchor[0] - expanded_x))
        self._strip_x = round(shift_px / scale, 2) if shift_px else 0.0

    def fit(self, *, keep_visible: bool = True) -> None:
        """Re-apply the wanted size (DPI changes rescale it) and pull on screen."""
        self._anim_gen += 1
        self._anim_card_h = float(self._card[2]) if self._card is not None else 0.0
        width, height = self._target_size()
        if self._anchor is not None and self._expanded is not None:
            x, y, self._up = self._place_session()
            self._update_strip_offset(x, width)
            self._apply(x, y, width, height)
            self._apply_shape()
            self.check_budget()
            return
        rect = self._current()
        if rect is None:
            self._apply(None, None, width, height)
            self._apply_shape()
            return
        x, y = rect.left, rect.top
        if keep_visible:
            x, y = native_geometry.keep_on_screen(
                (x, y, width, height), native_geometry.enum_monitors()
            )
        self._apply(x, y, width, height)
        self._apply_shape()
        self.check_budget()

    def set_base(self, width: float, height: float = TAB_HEIGHT, *, revision: int | None = None) -> None:
        """The window's content width changed (a refresh added or dropped a segment)."""
        width = clamp_number(width, 120, 2400)
        with self._lock:
            if revision is not None:
                if revision <= self._layout_revision:
                    return
                self._layout_revision = revision
            self._base = (width, float(height))
            if self._expanded is not None:
                self._expanded = (max(width, self._expanded[0]), self._expanded[1])
            self.fit()

    # -- the monitor's width budget ------------------------------------------

    def _budget_now(self) -> float | None:
        monitors = native_geometry.enum_monitors()
        if not monitors:
            return None
        rect = self._current()
        if self._anchor is not None:
            base_w, base_h = self._base_size()
            rect = native_geometry.Rect(
                self._anchor[0], self._anchor[1], self._anchor[0] + base_w, self._anchor[1] + base_h
            )
        if rect is not None:
            monitor = native_geometry.monitor_for(rect, monitors)
        else:
            monitor = next((m for m in monitors if m.primary), None) or monitors[0]
        scale = max(self._dpi(), 1) / 96
        return monitor.work.width / scale - BUDGET_MARGIN

    def logical_budget(self) -> float | None:
        """Logical width the tab may use on its current monitor (None: unknown)."""
        budget = self._budget_now()
        self._last_budget = budget
        return budget

    def check_budget(self) -> None:
        """A move or DPI change may give the tab a different monitor budget:
        ask for a fresh layout when it did."""
        callback = self.on_reflow
        if callback is None or self._reflowing:
            return
        budget = self._budget_now()
        if budget is None or (self._last_budget is not None and abs(budget - self._last_budget) < 1):
            return
        self._reflowing = True
        try:
            callback()
        except Exception:  # noqa: BLE001 -- a stale layout beats a crashed handler
            LOGGER.debug("Tab reflow failed", exc_info=True)
        finally:
            self._reflowing = False

    OPEN_ANIM_DURATION = 0.175
    RESIZE_ANIM_DURATION = 0.160
    CLOSE_ANIM_DURATION = 0.115
    ANIM_FRAME_SEC = 0.008  # ~120 Hz eased SetWindowRgn ticks

    def _should_animate_region(self) -> bool:
        return bool(
            self.animate_region
            and sys.platform == "win32"
            and self.hwnd
            and getattr(self.window, "native", None) is not None
        )

    def _run_region_anim(
        self,
        gen: int,
        *,
        size: tuple[int, int],
        scale: float,
        card_x: float,
        card_w: float,
        start_h: float,
        end_h: float,
        up: bool,
        strip: tuple[float, float] | None,
        duration: float,
        ease_in: bool = False,
        on_done=None,
    ) -> None:
        started = time.monotonic()
        while True:
            with self._lock:
                if self.quitting or self._anim_gen != gen:
                    return
            elapsed = time.monotonic() - started
            t = 1.0 if duration <= 0 else min(1.0, elapsed / duration)
            eased = (t * t) if ease_in else (1.0 - (1.0 - t) ** 3)
            cur_h = start_h + (end_h - start_h) * eased
            with self._lock:
                if self.quitting or self._anim_gen != gen:
                    return
                self._anim_card_h = max(0.0, cur_h)
                rects = shape_rects(
                    size,
                    scale,
                    (card_x, card_w, self._anim_card_h) if self._anim_card_h > 0.5 else None,
                    up,
                    strip,
                )
                if self._anim_card_h <= 0.5:
                    # Only the pill stays visible on the final retraction frame
                    tab_h = min(size[1], int(round(TAB_HEIGHT * scale)))
                    tab_top = size[1] - tab_h if up else 0
                    tab_r = int(round(TAB_RADIUS * scale))
                    sl = max(0, min(size[0], int(round((strip[0] if strip else 0.0) * scale))))
                    sr = max(sl, min(size[0], sl + int(round((strip[1] if strip else size[0] / scale) * scale))))
                    rects = [(sl, tab_top, sr, tab_top + tab_h, tab_r)]
                self._set_region(rects)
            if t >= 1.0:
                if on_done is not None:
                    on_done(gen)
                return
            time.sleep(self.ANIM_FRAME_SEC)

    def _finish_collapse(self, gen: int) -> None:
        with self._lock:
            if self.quitting or self._anim_gen != gen or self._expanded is None:
                return
            anchor = self._anchor
            self._expanded = None
            self._card = None
            self._strip_x = 0.0
            self._up = False
            self._anchor = None
            self._anim_card_h = 0.0
            width, height = self._target_size()
            if anchor is None:
                self._apply(None, None, width, height)
            else:
                self._apply(anchor[0], anchor[1], width, height)
            self._apply_shape()

    def expand(
        self,
        width: float,
        height: float,
        card: tuple[float, float, float] | None = None,
    ) -> dict:
        """Grow downward (or upward near the bottom edge) to hold a hover card.

        ``card`` is the card's ``(x, width, height)`` in logical pixels; the
        window region then shows only the strip and the card. Every expansion
        in one session is placed from the same collapsed anchor, so repeated or
        switching cards never drift the tab.
        """
        width = clamp_number(width, self._base[0], 2400)
        height = clamp_number(height, self._base[1], 1200)
        with self._lock:
            self._anim_gen += 1
            gen = self._anim_gen
            prev_expanded = self._expanded
            prev_h = self._anim_card_h
            if self._anchor is None:
                rect = self._current()
                if rect is not None:
                    self._anchor = (rect.left, rect.top)
            self._expanded = (width, height)
            self._card = card
            new_w, new_h = self._target_size()
            if self._anchor is None:
                self._up = False
                self._strip_x = 0.0
                self._anim_card_h = float(card[2]) if card is not None else 0.0
                self._apply(None, None, new_w, new_h)
                self._apply_shape()
                return {"up": False}
            x, y, self._up = self._place_session()
            self._update_strip_offset(x, new_w)
            scale = max(self._dpi(), 1) / 96
            strip = self._strip_spec()
            if card is not None and self._should_animate_region():
                target_h = float(card[2])
                start_h = prev_h if prev_h > 4.0 else min(target_h, 18.0)
                if abs(target_h - start_h) >= 4.0:
                    shrinking = (
                        prev_expanded is not None
                        and height < prev_expanded[1]
                        and prev_h > target_h
                    )
                    duration = self.RESIZE_ANIM_DURATION if prev_h > 4.0 else self.OPEN_ANIM_DURATION
                    if shrinking:
                        # Keep current taller window bounds while smoothly retracting the card height,
                        # then snap the final window height cleanly once retracted.
                        old_w, old_h = _scaled(prev_expanded[0], self._dpi()), _scaled(prev_expanded[1], self._dpi())
                        self._anim_card_h = start_h

                        def finalize_shrink(done_gen: int) -> None:
                            with self._lock:
                                if self.quitting or self._anim_gen != done_gen:
                                    return
                                self._anim_card_h = target_h
                                self._apply(x, y, new_w, new_h)
                                self._apply_shape()

                        threading.Thread(
                            target=self._run_region_anim,
                            args=(gen,),
                            kwargs={
                                "size": (old_w, old_h),
                                "scale": scale,
                                "card_x": card[0],
                                "card_w": card[1],
                                "start_h": start_h,
                                "end_h": target_h,
                                "up": self._up,
                                "strip": strip,
                                "duration": duration,
                                "ease_in": False,
                                "on_done": finalize_shrink,
                            },
                            name="burnrate-tab-anim",
                            daemon=True,
                        ).start()
                    else:
                        self._anim_card_h = start_h
                        self._apply(x, y, new_w, new_h)
                        self._set_region(shape_rects((new_w, new_h), scale, (card[0], card[1], start_h), self._up, strip))
                        threading.Thread(
                            target=self._run_region_anim,
                            args=(gen,),
                            kwargs={
                                "size": (new_w, new_h),
                                "scale": scale,
                                "card_x": card[0],
                                "card_w": card[1],
                                "start_h": start_h,
                                "end_h": target_h,
                                "up": self._up,
                                "strip": strip,
                                "duration": duration,
                                "ease_in": False,
                            },
                            name="burnrate-tab-anim",
                            daemon=True,
                        ).start()
                    result: dict = {"up": self._up}
                    if self._strip_x:
                        result["stripX"] = self._strip_x
                    return result
            self._anim_card_h = float(card[2]) if card is not None else 0.0
            self._apply(x, y, new_w, new_h)
            self._apply_shape()
            result = {"up": self._up}
            if self._strip_x:
                result["stripX"] = self._strip_x
            return result

    def collapse(self, *, instant: bool = False) -> None:
        """End the session: smoothly retract the hover card back into the pill."""
        with self._lock:
            if self._expanded is None:
                return
            self._anim_gen += 1
            gen = self._anim_gen
            if (
                not instant
                and self._card is not None
                and self._should_animate_region()
                and self._anim_card_h > 4.0
            ):
                size = self._target_size()
                scale = max(self._dpi(), 1) / 96
                card_x, card_w, _ = self._card
                start_h = self._anim_card_h
                up = self._up
                strip = self._strip_spec()
                threading.Thread(
                    target=self._run_region_anim,
                    args=(gen,),
                    kwargs={
                        "size": size,
                        "scale": scale,
                        "card_x": card_x,
                        "card_w": card_w,
                        "start_h": start_h,
                        "end_h": 0.0,
                        "up": up,
                        "strip": strip,
                        "duration": self.CLOSE_ANIM_DURATION,
                        "ease_in": True,
                        "on_done": self._finish_collapse,
                    },
                    name="burnrate-tab-anim",
                    daemon=True,
                ).start()
                return
            self._finish_collapse(gen)

    def drop_dwm_frame(self) -> None:
        """No DWM border or corner rounding: the window region draws the shape.

        Otherwise Windows outlines the whole (stretched) window rectangle.
        """
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes
            from ctypes import wintypes

            setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
            setter.argtypes = [wintypes.HWND, wintypes.DWORD, ctypes.c_void_p, wintypes.DWORD]
            for attribute, value in ((33, 1), (34, 0xFFFFFFFE)):  # DONOTROUND, no border
                number = ctypes.c_uint(value)
                setter(self.hwnd, attribute, ctypes.byref(number), ctypes.sizeof(number))
        except (OSError, AttributeError):
            LOGGER.debug("Could not drop the tab's DWM frame", exc_info=True)

    def hide_taskbar_button(self) -> None:
        """Make the tab a tool window (no taskbar button, no Alt+Tab entry).

        ``Form.ShowInTaskbar`` recreates the window handle, which aborts
        WebView2's start-up, so the extended style is changed directly.
        """
        if sys.platform != "win32" or not self.hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            gwl_exstyle, tool_window, app_window = -20, 0x00000080, 0x00040000
            style = user32.GetWindowLongW(self.hwnd, gwl_exstyle)
            desired = (style | tool_window) & ~app_window
            if desired != style:
                user32.SetWindowLongW(self.hwnd, gwl_exstyle, desired)
                # SWP_NOSIZE | SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE | SWP_FRAMECHANGED
                user32.SetWindowPos(self.hwnd, 0, 0, 0, 0, 0, 0x0037)
        except OSError:
            LOGGER.debug("Could not hide the tab's taskbar button", exc_info=True)

    DRAG_POLL = 0.008
    PRESS_FRESH = 5.0

    def note_press(self) -> None:
        self._press = (native_geometry.cursor_pos(), time.monotonic())

    def begin_drag(self) -> None:
        """Drag the tab with the pointer until the left button is released.

        Done by polling the native cursor position (not by the page's mouse
        events), so a fast move that leaves the window, or a lagging WebView,
        never drops the drag. Any open hover card is put away first.
        """
        if self.quitting or not self.visible or self._dragging:
            return
        self.collapse(instant=True)
        start = native_geometry.cursor_pos()
        press = self._press
        if press is not None and press[0] is not None and time.monotonic() - press[1] < self.PRESS_FRESH:
            start = press[0]  # where the button went down, before the pointer travelled to the threshold
        rect = self._current()
        if start is None or rect is None or not native_geometry.left_button_down():
            return
        self._dragging = True
        threading.Thread(
            target=self._drag_loop, args=(start, (rect.left, rect.top)),
            name="burnrate-tab-drag", daemon=True,
        ).start()

    def _drag_loop(self, start_cursor, start_window) -> None:
        try:
            while native_geometry.left_button_down() and not self.quitting and self.visible:
                position = native_geometry.cursor_pos()
                if position is not None:
                    self._move_to(
                        start_window[0] + position[0] - start_cursor[0],
                        start_window[1] + position[1] - start_cursor[1],
                    )
                time.sleep(self.DRAG_POLL)
        except Exception:  # noqa: BLE001 -- a failed drag must not take the app down
            LOGGER.warning("Tab drag failed", exc_info=True)
        finally:
            self._dragging = False
            self._press = None
            self.tracker.notify_changed()  # save the new spot under "tabWindow"
            self.note_moved()  # and clamp it back on screen once it has settled

    def _move_to(self, x: int, y: int) -> None:
        """Move without resizing, activating or restacking."""
        if sys.platform != "win32" or not self.hwnd:
            return
        import ctypes

        flags = 0x0001 | 0x0004 | 0x0010  # NOSIZE | NOZORDER | NOACTIVATE
        ctypes.windll.user32.SetWindowPos(self.hwnd, 0, x, y, 0, 0, flags)

    # -- show / hide ---------------------------------------------------------

    def restore_position(self, saved) -> None:
        """Place the tab at its saved spot (or the top-right of the work area)."""
        width, height = self._target_size()
        monitors = native_geometry.enum_monitors()
        position = native_geometry.plan_tab_position(
            native_geometry.parse_saved(saved), monitors, self._dpi(), (width, height)
        )
        if position is None:
            self._apply(None, None, width, height)
        else:
            self._apply(position[0], position[1], width, height)

    def is_shown(self) -> bool:
        """Whether the tab is really on screen (not only flagged visible)."""
        if not self.visible:
            return False
        if sys.platform != "win32" or not self.hwnd:
            return True
        import ctypes

        return bool(ctypes.windll.user32.IsWindowVisible(self.hwnd))

    def show(self, *, activate: bool = True, strict: bool = False) -> None:
        """Show the tab. With ``strict`` a failure is raised (after being logged)
        instead of swallowed, so the caller can fall back to the full window."""
        if self.quitting:
            return
        try:
            self.hide_taskbar_button()
            with self._lock:
                self.fit()
            if activate:
                self.window.show()
            elif sys.platform == "win32" and self.hwnd:
                import ctypes

                ctypes.windll.user32.ShowWindow(self.hwnd, 4)  # SW_SHOWNOACTIVATE
            else:
                self.window.show()
            self.visible = True
            if self.on_shown is not None:
                self.on_shown()
        except Exception:  # noqa: BLE001
            self.visible = False
            LOGGER.warning("Tab show failed", exc_info=True)
            if strict:
                raise

    def hide(self) -> None:
        if self.quitting:
            return
        self.collapse(instant=True)  # back to the anchor, so the next show is not displaced
        self.tracker.flush()
        with self._lock:
            self.visible = False
        try:
            self.window.hide()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Tab hide failed", exc_info=True)

    def destroy(self) -> None:
        self.quitting = True
        self._anim_gen += 1
        self.tracker.disable()
        try:
            self.window.destroy()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Tab destroy failed", exc_info=True)

    def push(self, payload: dict) -> None:
        """Hand a payload to the page on a worker (never on the UI thread)."""
        script = (
            "window.burnrateTab && window.burnrateTab.update && "
            f"window.burnrateTab.update({json.dumps(payload)})"
        )

        def run() -> None:
            try:
                self.window.evaluate_js(script)
            except Exception as exc:  # noqa: BLE001 -- the page may be loading or gone
                LOGGER.debug("Tab push skipped: %s", exc)

        threading.Thread(target=run, name="burnrate-tab-push", daemon=True).start()

    # -- form events (UI thread) ---------------------------------------------

    def on_before_show(self, *, on_close_request, dark: bool) -> None:
        from spend_app.wealthfront_window import _apply_win11_chrome

        native = getattr(self.window, "native", None)
        try:
            self.hwnd_box[0] = int(native.Handle.ToInt64())
        except (AttributeError, OSError, RuntimeError):
            return
        self.hide_taskbar_button()
        _apply_win11_chrome(self.window, self._icon_path, dark)
        self.drop_dwm_frame()
        self.prepare_layered()

        def on_closing(_sender, args) -> None:
            if self.quitting:
                return
            if args.CloseReason in user_closing:
                args.Cancel = True  # Alt+F4 or Close: park the tab (or quit) instead
                on_close_request()
                return
            # Windows shutdown, application exit, owner close: let it through.
            self.quitting = True
            self.tracker.flush()
            self.tracker.disable()

        try:
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            user_closing = (
                WinForms.CloseReason.UserClosing,
                WinForms.CloseReason.TaskManagerClosing,
            )
            native.FormClosing += on_closing
            native.DpiChanged += lambda *_args: self.fit()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Tab form hooks failed", exc_info=True)


# --- switching ---------------------------------------------------------------


class SurfaceController:
    """Decides which of the two windows is up and remembers it.

    Every transition (shrink to tab, open full, tray toggle, launch restore,
    closing the tab) is a request on one queue that a single worker applies in
    order. The latest absolute request (tab/full) supersedes everything queued
    before it, so the last click wins and the windows can never end up both
    visible. Callers never block and no lock is held across a GUI call, so a
    request made from the UI thread cannot deadlock against the worker.
    """

    ABSOLUTE = ("tab", "full")

    def __init__(self, main_host, tab: TabHost, bridge, store) -> None:
        self.main = main_host
        self.tab = tab
        self.bridge = bridge
        self.store = store
        self._lock = threading.Lock()
        self._pending: list[str] = []
        self._worker_alive = False
        self._closing = False
        self._idle = threading.Event()
        self._idle.set()

    @property
    def surface(self) -> str:
        return self.bridge.surface

    # -- requests (any thread, never blocking) ---------------------------------

    def show_tab(self) -> None:
        self._request("tab")

    def show_full(self) -> None:
        self._request("full")

    def toggle(self) -> None:
        """Tray click: hide whatever is up, or bring the remembered surface back."""
        self._request("toggle")

    def restore_on_launch(self) -> None:
        """The main window is already up for a ``full`` launch; bring the tab up
        (without stealing focus) for a ``tab`` one."""
        self._request("restore")

    def close_tab(self) -> None:
        """Alt+F4 on the tab: park in the tray, or quit when there is none."""
        self._request("close-tab")

    def flush(self, timeout: float = 5.0) -> bool:
        """Wait until every queued transition has been applied (tests, shutdown)."""
        return self._idle.wait(timeout)

    def shutdown(self) -> None:
        """Drop queued transitions and take the tab down with the app."""
        with self._lock:
            self._closing = True
            self._pending.clear()
        self.tab.destroy()

    # -- the queue --------------------------------------------------------------

    def _request(self, action: str) -> None:
        with self._lock:
            if self._closing:
                return
            self._pending.append(action)
            self._idle.clear()
            if self._worker_alive:
                return
            self._worker_alive = True
        threading.Thread(target=self._drain, name="burnrate-surface", daemon=True).start()

    def _next(self) -> str | None:
        with self._lock:
            if self._closing or not self._pending:
                self._worker_alive = False
                self._idle.set()
                return None
            last_absolute = max(
                (i for i, a in enumerate(self._pending) if a in self.ABSOLUTE), default=-1
            )
            if last_absolute > 0:
                del self._pending[:last_absolute]  # the later absolute request wins
            return self._pending.pop(0)

    def _drain(self) -> None:
        while True:
            action = self._next()
            if action is None:
                return
            try:
                self._apply(action)
            except Exception:  # noqa: BLE001 -- one bad transition must not stop the rest
                LOGGER.warning("Surface transition %s failed", action, exc_info=True)

    def _apply(self, action: str) -> None:
        if action == "tab":
            self._to_tab()
        elif action == "full":
            self._to_full()
        elif action == "toggle":
            if self.tab.visible:
                self.tab.hide()
            elif self.main.is_visible():
                self.main.hide()
            elif self.surface == "tab":
                self.tab.show()
            else:
                self.main.show()
        elif action == "restore":
            if self.surface == "tab":
                self._restore_tab_on_launch()
        elif action == "close-tab":
            if self.main.can_hide_to_tray():
                self.tab.hide()
            else:
                self.main.quit()
        LOGGER.info(
            "Surface %s -> surface=%s main_visible=%s tab_visible=%s",
            action, self.surface, self._safe(self.main.is_visible), self.tab.visible,
        )

    @staticmethod
    def _safe(call):
        try:
            return call()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Window call failed", exc_info=True)
            return None

    def _show_tab_verified(self, *, activate: bool = True) -> None:
        self.tab.show(activate=activate, strict=True)
        if not self.tab.is_shown():
            self.tab.visible = False
            raise RuntimeError("the tab window did not appear")

    def _show_full_verified(self) -> None:
        self.main.show()
        if not self.main.is_visible():
            raise RuntimeError("the full window did not appear")

    def _restore_tab_on_launch(self) -> None:
        try:
            self._show_tab_verified(activate=False)
        except Exception:  # noqa: BLE001 -- native show or visibility check failed
            LOGGER.warning("Saved tab could not be restored; opening the full window", exc_info=True)
            try:
                self._show_full_verified()
            except Exception:  # noqa: BLE001 -- keep the saved surface available for retry
                LOGGER.warning("Full window fallback failed", exc_info=True)
            else:
                self.bridge._remember_surface("full")
                self._safe(self.tab.hide)

    def _to_tab(self) -> None:
        """Shrink to the tab. The tab comes up first and the full window only
        goes once the tab is really showing, so a failure can never leave the
        user with no window: it is rolled back to the full window."""
        self.bridge._remember_surface("tab")
        try:
            self._show_tab_verified()
            self.main.hide()
            if self._safe(self.main.is_visible):
                raise RuntimeError("the full window is still visible")
        except Exception:  # noqa: BLE001 -- anything: keep the full window
            LOGGER.warning("Shrink to tab failed; keeping the full window", exc_info=True)
            self.bridge._remember_surface("full")
            self._safe(self.tab.hide)
            self._safe(self.main.show)

    def _to_full(self) -> None:
        self.bridge._remember_surface("full")
        try:
            self._show_full_verified()
            self.tab.hide()
        except Exception:  # noqa: BLE001
            LOGGER.warning("Opening the full window failed; keeping the tab", exc_info=True)
            self.bridge._remember_surface("tab")
            self._safe(self.tab.show)
