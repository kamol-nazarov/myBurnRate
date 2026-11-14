"""Frameless Windows 11 WebView2 host for BURNRATE's Wealthfront iOS desktop UI."""

from __future__ import annotations

import base64
import importlib
import json
import logging
import math
import os
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from spend_app import __version__, native_geometry
from spend_app.native_alerts import QuotaWatcher
from spend_app.native_autoupdate import UpdateChecker
from spend_app.native_desktop import (
    PERIOD_LABELS,
    DemoClient,
    DesktopError,
    NativeClient,
    SourceMutation,
)
from spend_app.native_links import is_allowed_external_url
from spend_app.native_onboarding import build_onboarding_data
from spend_app.native_prefs import PreferenceStore, store_for
from spend_app.native_tray import TrayController, run_on_ui

LOGGER = logging.getLogger(__name__)


def open_in_browser(url: str) -> bool:
    """Hand an already validated URL to the default browser (never a shell)."""
    import webbrowser

    return bool(webbrowser.open(url))


EDGE_HITS = {
    "left": 10,
    "right": 11,
    "top": 12,
    "topleft": 13,
    "topright": 14,
    "bottom": 15,
    "bottomleft": 16,
    "bottomright": 17,
}

SHORT_WINDOW_LABELS = {
    "5h": "5h",
    "weekly": "7d",
    "monthly": "30d",
    "gemini-5h": "Gemini 5h",
    "gemini-weekly": "Gemini 7d",
    "3p-5h": "3P 5h",
    "3p-weekly": "3P 7d",
    "balance": "Balance",
}

WINDOW_TIE_ORDER = {
    "5h": 0,
    "gemini-5h": 1,
    "3p-5h": 2,
    "weekly": 3,
    "gemini-weekly": 4,
    "3p-weekly": 5,
    "monthly": 6,
}

SESSION_KEYS = ("5h", "gemini-5h", "3p-5h")
WEEKLY_KEYS = ("weekly", "gemini-weekly", "3p-weekly")
LABEL_SEPARATOR = " \u2014 "


def _compact_count(value: float | None) -> str:
    if value is None:
        return "0"
    num = float(value)
    if not math.isfinite(num):
        return "0"
    if abs(num) >= 1e9:
        return f"{num / 1e9:.1f}B"
    if abs(num) >= 1e6:
        return f"{num / 1e6:.1f}M"
    if abs(num) >= 1e3:
        return f"{num / 1e3:.1f}K"
    return f"{num:,.0f}"


def _clean_window_label(raw_label: str, limit_key: str) -> tuple[str, str]:
    text = str(raw_label or "").strip()
    reason = ""
    if LABEL_SEPARATOR in text:
        text, _, reason = text.partition(LABEL_SEPARATOR)
        text = text.strip()
        reason = reason.strip()
    text = (
        text.replace("Antigravity Gemini models · ", "Gemini · ")
        .replace("Antigravity Claude & GPT models · ", "Claude & GPT · ")
        .replace("Antigravity Claude/GPT models · ", "Claude / GPT · ")
        .replace(" remaining", "")
    )
    if not text:
        text = SHORT_WINDOW_LABELS.get(limit_key, limit_key or "Window")
    return text, reason


def _combine_tool_entries(tool_map: dict[str, dict], keys: tuple[str, ...]) -> dict:
    present = [tool_map[k] for k in keys if k in tool_map]
    if not present:
        return {}
    if len(present) == 1:
        return dict(present[0])
    tokens_sum = sum(int(item.get("tokens") or 0) for item in present)
    values = [
        float(item["valueUsd"])
        for item in present
        if isinstance(item.get("valueUsd"), (int, float))
    ]
    models: list[dict] = []
    for item in present:
        models.extend(m for m in (item.get("models") or []) if isinstance(m, dict))
    reasoning_vals = [
        int(item["reasoning"])
        for item in present
        if isinstance(item.get("reasoning"), (int, float))
    ]
    return {
        "key": keys[0],
        "name": present[0].get("name") or keys[0],
        "tokens": tokens_sum,
        "valueUsd": round(sum(values), 4) if values else None,
        "complete": all(item.get("complete", True) for item in present),
        "events": sum(int(item.get("events") or 0) for item in present),
        "unpricedEvents": sum(int(item.get("unpricedEvents") or 0) for item in present),
        "organization": any(bool(item.get("organization")) for item in present),
        "input": sum(int(item.get("input") or 0) for item in present),
        "cached": sum(int(item.get("cached") or 0) for item in present),
        "writes": sum(int(item.get("writes") or 0) for item in present),
        "output": sum(int(item.get("output") or 0) for item in present),
        "reasoning": sum(reasoning_vals) if reasoning_vals else None,
        "models": models,
    }


def _default_provider_rank(key: str) -> tuple[int, str]:
    k = key.lower()
    if k == "claude-code":
        return (10, k)
    if k.startswith("claude-code-"):
        return (11, k)
    if k == "opencode-go":
        return (20, k)
    if k.startswith("opencode-go-"):
        return (21, k)
    if k == "opencode":
        return (22, k)
    if k == "codex":
        return (30, k)
    if k == "antigravity":
        return (40, k)
    if k == "grok":
        return (50, k)
    if k == "openrouter":
        return (60, k)
    if k == "deepseek":
        return (70, k)
    if k == "muse":
        return (80, k)
    return (90, k)


def merge_provider_entries(
    period_report: dict,
    hud: dict | None = None,
    hud_usage: dict | None = None,
    today_report: dict | None = None,
    week_report: dict | None = None,
    provider_order: list[str] | None = None,
) -> list[dict]:
    """Merge live quota/balance lanes with recorded token telemetry across accounts."""
    capacity = (hud or {}).get("capacity") if isinstance(hud, dict) else []
    if not isinstance(capacity, list):
        capacity = []
    window_tokens_all = (
        (hud_usage or {}).get("windowTokens") if isinstance(hud_usage, dict) else {}
    )
    if not isinstance(window_tokens_all, dict):
        window_tokens_all = {}

    period_tools = {
        str(t["key"]): t
        for t in (period_report.get("tools") or [])
        if isinstance(t, dict) and t.get("key")
    }
    today_tools = {
        str(t["key"]): t
        for t in ((today_report or {}).get("tools") or [])
        if isinstance(t, dict) and t.get("key")
    }
    week_tools = {
        str(t["key"]): t
        for t in ((week_report or {}).get("tools") or [])
        if isinstance(t, dict) and t.get("key")
    }

    capacity_keys = {
        str(c.get("providerKey") or "").strip()
        for c in capacity
        if isinstance(c, dict) and c.get("providerKey")
    }
    has_multi_claude = "claude-code" in capacity_keys and any(
        k.startswith("claude-code-") for k in capacity_keys
    )
    has_multi_opencode_go = "opencode-go" in capacity_keys and any(
        k.startswith("opencode-go-") for k in capacity_keys
    )

    consumed_usage_keys: set[str] = set()
    providers: list[dict] = []

    for card in capacity:
        if not isinstance(card, dict):
            continue
        key = str(card.get("providerKey") or "").strip()
        if not key:
            continue

        if key == "opencode" and "zcode" not in capacity_keys:
            usage_keys: tuple[str, ...] = ("opencode", "zcode")
        else:
            usage_keys = (key,)
        consumed_usage_keys.update(usage_keys)

        period_entry = _combine_tool_entries(period_tools, usage_keys)
        today_entry = _combine_tool_entries(today_tools, usage_keys)
        week_entry = _combine_tool_entries(week_tools, usage_keys)

        raw_title = str(card.get("title") or period_entry.get("name") or key).strip()
        acct_label = str(card.get("accountLabel") or "").strip()
        if acct_label and raw_title.endswith(" · " + acct_label):
            display_name = raw_title[: -(len(acct_label) + 3)].strip()
            badge = acct_label
        else:
            display_name = raw_title
            badge = acct_label

        if (
            key == "claude-code"
            and has_multi_claude
            and not badge
            or key == "opencode-go"
            and has_multi_opencode_go
            and not badge
        ):
            badge = "Primary"
        elif key == "opencode" and not badge:
            if "z.ai" in display_name.lower():
                badge = "OpenCode"
            elif "opencode-go" in capacity_keys and card.get("plan"):
                badge = str(card["plan"])

        wt_provider = window_tokens_all.get(key)
        if not isinstance(wt_provider, dict):
            wt_provider = {}

        windows: list[dict] = []
        for row in card.get("rows") or []:
            if not isinstance(row, dict):
                continue
            limit_key = str(row.get("limitKey") or "").strip()
            clean_lbl, reason = _clean_window_label(
                str(row.get("label") or ""), limit_key
            )
            unit = str(row.get("unit") or "pct")
            unavailable = bool(row.get("unavailable"))
            raw_pct = row.get("pct")
            if (
                unit != "usd"
                and not unavailable
                and isinstance(raw_pct, (int, float))
                and math.isfinite(float(raw_pct))
            ):
                used_pct: float | None = max(0.0, min(100.0, float(raw_pct)))
                rem_pct: float | None = max(0.0, min(100.0, 100.0 - used_pct))
            else:
                used_pct = None
                rem_pct = None

            used_amt = (
                float(row["used"])
                if isinstance(row.get("used"), (int, float))
                and math.isfinite(float(row["used"]))
                else None
            )
            allowance = (
                float(row["allowance"])
                if isinstance(row.get("allowance"), (int, float))
                and math.isfinite(float(row["allowance"]))
                else None
            )
            rem_credits = (
                max(0.0, allowance - used_amt)
                if unit == "credits" and allowance is not None and used_amt is not None
                else None
            )
            rem_usd = used_amt if unit == "usd" else None

            wt_entry = wt_provider.get(limit_key)
            win_tok = (
                int(wt_entry["tokens"])
                if isinstance(wt_entry, dict)
                and isinstance(wt_entry.get("tokens"), (int, float))
                else None
            )
            forecast_line = (
                str(row["forecast"].get("line") or "")
                if isinstance(row.get("forecast"), dict)
                else ""
            )
            runway_line = (
                str(row["runway"].get("line") or "")
                if isinstance(row.get("runway"), dict)
                else ""
            )

            windows.append(
                {
                    "limitKey": limit_key,
                    "shortLabel": SHORT_WINDOW_LABELS.get(
                        limit_key, clean_lbl[:12] or "Window"
                    ),
                    "label": clean_lbl,
                    "reason": reason,
                    "unit": unit,
                    "usedPct": round(used_pct, 1) if used_pct is not None else None,
                    "remainingPct": round(rem_pct, 1) if rem_pct is not None else None,
                    "used": used_amt,
                    "allowance": allowance,
                    "remainingCredits": rem_credits,
                    "remainingUsd": rem_usd,
                    "resetsAt": row.get("resetsAt"),
                    "unavailable": unavailable,
                    "stale": bool(row.get("stale")),
                    "windowTokens": win_tok,
                    "forecast": forecast_line,
                    "runway": runway_line,
                }
            )

        # Hover token burn telemetry: Day, Session (5h), Weekly session
        has_session = any(w["limitKey"] in SESSION_KEYS for w in windows)
        session_readings = [
            int(wt_provider[lk]["tokens"])
            for lk in SESSION_KEYS
            if isinstance(wt_provider.get(lk), dict)
            and isinstance(wt_provider[lk].get("tokens"), (int, float))
        ]
        session_tokens = (
            sum(session_readings) if has_session and session_readings else None
        )

        day_from_windows: list[int] = []
        week_from_windows: list[int] = []
        for lk in WEEKLY_KEYS:
            entry = wt_provider.get(lk)
            if isinstance(entry, dict):
                if isinstance(entry.get("tokens"), (int, float)):
                    week_from_windows.append(int(entry["tokens"]))
                today_sub = entry.get("today")
                if isinstance(today_sub, dict) and isinstance(
                    today_sub.get("tokens"), (int, float)
                ):
                    day_from_windows.append(int(today_sub["tokens"]))

        day_candidates = [sum(day_from_windows)] if day_from_windows else []
        if isinstance(today_entry.get("tokens"), (int, float)):
            day_candidates.append(int(today_entry["tokens"]))
        day_tokens = max(day_candidates) if day_candidates else None
        if week_from_windows:
            # A quota window can have reset today, so it can legitimately be
            # smaller than the calendar-day total, including a measured zero.
            week_tokens = sum(week_from_windows)
            week_basis = "window"
        elif isinstance(week_entry.get("tokens"), (int, float)):
            week_tokens = int(week_entry["tokens"])
            week_basis = "rolling"
        else:
            week_tokens = None
            week_basis = None

        # Primary Remaining Usage headline
        funds_usd = card.get("fundsRemainingUsd")
        has_funds = isinstance(funds_usd, (int, float)) and math.isfinite(
            float(funds_usd)
        )
        measured_windows = [
            w for w in windows if w["remainingPct"] is not None and not w["unavailable"]
        ]

        if has_funds:
            bal = max(0.0, float(funds_usd))
            funds_runway = (
                str(card["fundsRunway"].get("line") or "")
                if isinstance(card.get("fundsRunway"), dict)
                else ""
            )
            tone = "danger" if bal < 1.0 else "warn" if bal < 5.0 else "mint"
            bar_pct = 100.0 if bal >= 25.0 else max(8.0, (bal / 25.0) * 100.0)
            remaining = {
                "kind": "balance",
                "headline": f"${bal:,.2f}",
                "sub": "balance left",
                "barPct": round(bar_pct, 1),
                "tone": tone,
                "resetsAt": None,
                "summary": funds_runway or "Prepaid API balance",
            }
        elif measured_windows:
            primary = min(
                measured_windows,
                key=lambda w: (
                    round(float(w["remainingPct"]), 1),
                    WINDOW_TIE_ORDER.get(w["limitKey"], 99),
                ),
            )
            rem_val = float(primary["remainingPct"])
            tone = (
                "danger" if rem_val <= 8.0 else "warn" if rem_val <= 20.0 else "accent"
            )
            if (
                primary["unit"] == "credits"
                and primary.get("remainingCredits") is not None
            ):
                summary = f"{_compact_count(primary['remainingCredits'])} cr left · {card.get('plan') or 'Plan'}"
            elif len(measured_windows) > 1:
                peers = [
                    f"{w['shortLabel']} {float(w['remainingPct']):.0f}%"
                    for w in measured_windows
                    if w is not primary
                ][:2]
                summary = " · ".join(peers)
                if card.get("plan") and len(peers) == 1:
                    summary += f" · {card['plan']}"
            else:
                summary = str(card.get("plan") or primary["label"] or "Active quota")

            remaining = {
                "kind": "quota",
                "headline": f"{rem_val:.0f}% left",
                "sub": primary["shortLabel"],
                "barPct": round(rem_val, 1),
                "tone": tone,
                "resetsAt": primary.get("resetsAt"),
                "summary": summary,
            }
        else:
            first_reason = next((w["reason"] for w in windows if w.get("reason")), "")
            lower_reason = first_reason.lower()
            if "not been used recently" in lower_reason:
                summary = "Meter wakes on next session"
            elif (
                "wait for a fresh" in lower_reason
                or "bounded local sample" in lower_reason
                or "reset" in lower_reason
            ):
                summary = "Waiting for fresh reading"
            elif first_reason:
                summary = (
                    first_reason[:44] + "…" if len(first_reason) > 44 else first_reason
                )
            else:
                summary = str(card.get("plan") or "Connected")

            remaining = {
                "kind": "idle",
                "headline": "No key" if card.get("isPayg") else "Idle",
                "sub": str(card.get("plan") or "meter idle"),
                "barPct": 0.0,
                "tone": "muted",
                "resetsAt": None,
                "summary": summary,
            }

        providers.append(
            {
                "key": key,
                "name": display_name,
                "badge": badge,
                "plan": str(card.get("plan") or ""),
                "inUse": bool(card.get("inUse")),
                "isPayg": bool(card.get("isPayg")),
                "fundsRemainingUsd": float(funds_usd) if has_funds else None,
                "organization": bool(period_entry.get("organization")),
                "remaining": remaining,
                "burn": {
                    "dayTokens": day_tokens,
                    "sessionTokens": session_tokens,
                    "hasSession": has_session,
                    "weekTokens": week_tokens,
                    "weekBasis": week_basis,
                },
                "windows": windows,
                "tokens": int(period_entry.get("tokens") or 0),
                "valueUsd": period_entry.get("valueUsd"),
                "complete": period_entry.get("complete", True),
                "input": int(period_entry.get("input") or 0),
                "cached": int(period_entry.get("cached") or 0),
                "writes": int(period_entry.get("writes") or 0),
                "output": int(period_entry.get("output") or 0),
                "reasoning": period_entry.get("reasoning"),
                "models": period_entry.get("models") or [],
            }
        )

    # Include any recorded-usage tools that do not have a quota card (e.g. Org API feeds)
    for key, tool in period_tools.items():
        if key in consumed_usage_keys:
            continue
        today_value = (today_tools.get(key) or {}).get("tokens")
        week_value = (week_tools.get(key) or {}).get("tokens")
        day_tok = int(today_value) if isinstance(today_value, (int, float)) else None
        week_tok = int(week_value) if isinstance(week_value, (int, float)) else None
        val_usd = tool.get("valueUsd")
        has_val = isinstance(val_usd, (int, float)) and math.isfinite(float(val_usd))
        headline = f"${float(val_usd):,.2f}" if has_val else "Tracked"
        providers.append(
            {
                "key": key,
                "name": str(tool.get("name") or key),
                "badge": "Org API" if tool.get("organization") else "",
                "plan": "API",
                "inUse": False,
                "isPayg": True,
                "fundsRemainingUsd": None,
                "organization": bool(tool.get("organization")),
                "remaining": {
                    "kind": "usage_only",
                    "headline": headline,
                    "sub": "API value" if has_val else "usage only",
                    "barPct": 100.0 if day_tok is not None and day_tok > 0 else 0.0,
                    "tone": "muted",
                    "resetsAt": None,
                    "summary": "Organization API feed"
                    if tool.get("organization")
                    else f"{len(tool.get('models') or [])} models recorded",
                },
                "burn": {
                    "dayTokens": day_tok,
                    "sessionTokens": None,
                    "hasSession": False,
                    "weekTokens": week_tok,
                    "weekBasis": "rolling" if week_tok is not None else None,
                },
                "windows": [],
                "tokens": int(tool.get("tokens") or 0),
                "valueUsd": val_usd,
                "complete": tool.get("complete", True),
                "input": int(tool.get("input") or 0),
                "cached": int(tool.get("cached") or 0),
                "writes": int(tool.get("writes") or 0),
                "output": int(tool.get("output") or 0),
                "reasoning": tool.get("reasoning"),
                "models": tool.get("models") or [],
            }
        )

    order_list = [
        str(k) for k in (provider_order or []) if isinstance(k, str) and k.strip()
    ]
    order_index = {k: idx for idx, k in enumerate(order_list)}

    providers.sort(
        key=lambda item: (
            0 if item["key"] in order_index else 1,
            order_index.get(item["key"], 0),
            *_default_provider_rank(item["key"]),
        )
    )
    return providers


THEME_CHOICES = ("system", "light", "dark")
THEME_ATTRIBUTE = 'data-theme-pref="system"'
SERVICE_ALERT_RECHECK_SECONDS = 300.0
TRAY_HINT_KEY = "trayHintShown"
HOST_PAGE_METHODS = frozenset({"refresh", "openSettings", "notify", "setUpdate"})
LIGHT_BACKGROUND = "#F5F6FA"
DARK_BACKGROUND = "#101117"
# DWMWA_BORDER_COLOR takes a COLORREF, 0x00BBGGRR.
LIGHT_BORDER = 0x00EAE4E4  # #E4E4EA
DARK_BORDER = 0x00372C2A  # #2A2C37
PERSONALIZE_KEY = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"


def normalize_theme(value: object) -> str:
    text = str(value or "").strip().lower()
    return text if text in THEME_CHOICES else "system"


def system_prefers_dark() -> bool:
    """Windows' app theme (``AppsUseLightTheme`` == 0). Light when unreadable."""
    if sys.platform != "win32":
        return False
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, PERSONALIZE_KEY) as key:
            return winreg.QueryValueEx(key, "AppsUseLightTheme")[0] == 0
    except OSError:
        return False


def resolve_dark(theme_pref: str, system_dark: bool | None = None) -> bool:
    pref = normalize_theme(theme_pref)
    if pref == "system":
        return system_prefers_dark() if system_dark is None else bool(system_dark)
    return pref == "dark"


def background_for(dark: bool) -> str:
    return DARK_BACKGROUND if dark else LIGHT_BACKGROUND


def apply_theme_pref(html: str, theme_pref: str) -> str:
    """Stamp the saved theme onto the page's ``<html data-theme-pref>`` attribute."""
    pref = normalize_theme(theme_pref)
    if pref == "system":
        return html
    return html.replace(THEME_ATTRIBUTE, f'data-theme-pref="{pref}"', 1)


def load_ui_html(theme_pref: str = "system") -> str:
    """Load wealthfront_ui.html with offline Plus Jakarta Sans variable font embedded.

    ``theme_pref`` replaces the page's ``data-theme-pref="system"`` attribute so
    the saved choice is applied before first paint. A page without it is served
    unchanged.
    """
    html_path = Path(__file__).with_name("wealthfront_ui.html")
    html = html_path.read_text(encoding="utf-8")
    html = apply_theme_pref(html, theme_pref)
    font_path = Path(__file__).parent / "fonts" / "plus-jakarta-sans-latin.woff2"
    if font_path.is_file():
        b64 = base64.b64encode(font_path.read_bytes()).decode("ascii")
        font_css = (
            '@font-face{font-family:"Plus Jakarta Sans";font-style:normal;'
            "font-weight:200 800;font-display:block;"
            f'src:url("data:font/woff2;base64,{b64}") format("woff2");}}'
        )
        html = html.replace("/*__EMBEDDED_FONTS__*/", font_css, 1)
    return html


def _set_theme_chrome(hwnd: int, dark: bool) -> None:
    """Title-bar mode and hairline border for the theme. Safe from any thread."""
    if sys.platform != "win32" or not hwnd:
        return
    try:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        mode = ctypes.c_int(1 if dark else 0)
        setter(hwnd, 20, ctypes.byref(mode), ctypes.sizeof(mode))
        border = ctypes.c_int(DARK_BORDER if dark else LIGHT_BORDER)
        setter(hwnd, 34, ctypes.byref(border), ctypes.sizeof(border))
    except OSError:
        pass


def _apply_win11_chrome(window, icon_path: Path | None, dark: bool = False) -> int:
    """Enable native Windows 11 rounded corners, themed border, and taskbar icon."""
    if sys.platform != "win32":
        return 0
    hwnd = 0
    try:
        native = getattr(window, "native", None)
        if native is not None and hasattr(native, "Handle"):
            hwnd = int(native.Handle.ToInt64())
            if icon_path is not None and icon_path.is_file():

                def set_icon() -> None:
                    import System.Drawing  # type: ignore[import-not-found]

                    native.Icon = System.Drawing.Icon(str(icon_path))

                # Posted to the UI thread, never a blocking cross-thread set:
                # that deadlocks against the form's own first-show work.
                run_on_ui(native, set_icon)
    except (OSError, AttributeError, RuntimeError):
        hwnd = 0

    if not hwnd:
        return 0

    try:
        import ctypes
        from ctypes import wintypes

        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        rounded = ctypes.c_int(2)  # DWMWCP_ROUND
        setter(hwnd, 33, ctypes.byref(rounded), ctypes.sizeof(rounded))
    except OSError:
        pass
    _set_theme_chrome(hwnd, dark)
    return hwnd


def _set_window_topmost(window, hwnd_getter, enabled: bool) -> None:
    if sys.platform != "win32":
        window.on_top = enabled
        return

    import ctypes
    from ctypes import wintypes

    hwnd = hwnd_getter()
    if not hwnd:
        raise RuntimeError("The window is not ready to pin")
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    set_position = user32.SetWindowPos
    set_position.argtypes = [
        wintypes.HWND,
        wintypes.HWND,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        ctypes.c_int,
        wintypes.UINT,
    ]
    set_position.restype = wintypes.BOOL
    insert_after = ctypes.c_void_p(-1 if enabled else -2)  # TOPMOST / NOTOPMOST
    # The WebView bridge runs off the UI thread. Queue the change rather than
    # synchronously setting the WinForms TopMost property and deadlocking it.
    flags = 0x0001 | 0x0002 | 0x0010 | 0x4000  # NOSIZE, NOMOVE, NOACTIVATE, ASYNC
    if not set_position(hwnd, insert_after, 0, 0, 0, 0, flags):
        raise ctypes.WinError(ctypes.get_last_error())


class WindowOpacityController:
    """Smoothly fades the Win32 frameless window when the cursor leaves its bounds."""

    def __init__(self, bridge: DesktopBridge, hwnd_getter) -> None:
        self._bridge = bridge
        self._hwnd_getter = hwnd_getter
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._layered_hwnd = 0
        self._current_alpha = 255.0
        self._awake_until = 0.0

    def wake(self, seconds: float = 2.5) -> None:
        """Hold full opacity for a moment, e.g. right after a tray click shows
        the window while the cursor is still over the taskbar."""
        self._awake_until = time.monotonic() + seconds

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="burnrate-opacity", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _ensure_layered(self, user32, hwnd: int) -> bool:
        if self._layered_hwnd == hwnd:
            return True
        try:
            gwl_exstyle = -20
            ws_ex_layered = 0x00080000
            style = user32.GetWindowLongW(hwnd, gwl_exstyle)
            if not (style & ws_ex_layered):
                user32.SetWindowLongW(hwnd, gwl_exstyle, style | ws_ex_layered)
            user32.SetLayeredWindowAttributes(hwnd, 0, 255, 0x02)
            self._layered_hwnd = hwnd
            self._current_alpha = 255.0
            return True
        except OSError:
            return False

    def _run(self) -> None:
        try:
            import ctypes
            from ctypes import wintypes
        except ImportError:
            return

        user32 = ctypes.windll.user32
        pt = wintypes.POINT()
        rect = wintypes.RECT()
        last_inside_at = time.monotonic()
        dragging_inside = False

        while not self._stop.is_set():
            hwnd = int(self._hwnd_getter() or 0)
            if not hwnd or not user32.IsWindow(hwnd):
                self._stop.wait(0.08)
                continue
            if user32.IsIconic(hwnd) or not user32.IsWindowVisible(hwnd):
                # Minimized, or hidden to the tray: nothing to fade, and no
                # layered-window calls against a window that is not on screen.
                self._stop.wait(0.15)
                continue
            if not self._ensure_layered(user32, hwnd):
                self._stop.wait(0.25)
                continue

            hovered = True
            if user32.GetCursorPos(ctypes.byref(pt)) and user32.GetWindowRect(
                hwnd, ctypes.byref(rect)
            ):
                inside = (rect.left - 4) <= pt.x <= (rect.right + 4) and (
                    rect.top - 4
                ) <= pt.y <= (rect.bottom + 4)
                lmb_down = bool(user32.GetAsyncKeyState(0x01) & 0x8000)
                if inside and lmb_down:
                    dragging_inside = True
                elif not lmb_down:
                    dragging_inside = False

                now = time.monotonic()
                if inside or dragging_inside:
                    last_inside_at = now
                    hovered = True
                else:
                    # Brief 160ms grace window prevents edge jitter when brushing border
                    hovered = (now - last_inside_at) < 0.16

            auto_fade, idle_opacity, ui_busy = self._bridge.opacity_state()
            if (
                not auto_fade
                or ui_busy
                or hovered
                or time.monotonic() < self._awake_until
            ):
                target = 255.0
            else:
                target = max(46.0, min(235.0, round(idle_opacity * 255.0)))

            diff = target - self._current_alpha
            if abs(diff) >= 0.75:
                # Snappy fade-in on hover (~130ms), silky fade-out on leave (~250ms)
                rate = 0.30 if diff > 0 else 0.15
                step = diff * rate
                if abs(step) < 1.1:
                    self._current_alpha = target
                else:
                    self._current_alpha += step
                alpha_byte = max(35, min(255, round(self._current_alpha)))
                try:
                    user32.SetLayeredWindowAttributes(hwnd, 0, alpha_byte, 0x02)
                except OSError:
                    pass
                self._stop.wait(0.016)
            else:
                if self._current_alpha != target:
                    self._current_alpha = target
                    try:
                        user32.SetLayeredWindowAttributes(hwnd, 0, round(target), 0x02)
                    except OSError:
                        pass
                self._stop.wait(0.055)


class DesktopBridge:
    """In-process Python bridge exposed to the local WebView2 UI."""

    def __init__(
        self,
        client: NativeClient | DemoClient,
        *,
        preferences: Path | None = None,
        demo: bool = False,
        smoke_test: bool = False,
    ) -> None:
        self._client = client
        self._preferences = preferences
        self._smoke_test = smoke_test
        # (checked_at, owned); None = never checked. A 0.0 timestamp would
        # read as fresh on a machine that booted within the recheck window.
        self._service_alerts: tuple[float, bool] | None = None
        self._last_update_info: dict | None = None
        self._prefs: PreferenceStore = store_for(preferences)
        self._demo = demo
        self._window = None
        self._hwnd = 0
        self._service_ready = demo
        self._lock = threading.Lock()
        self._mutations: dict[str, SourceMutation] = {}
        self._ui_busy = False
        self._host: WindowHost | None = None
        self._tray_available = False
        self._effective_dark: bool | None = None

        saved = self._read_preferences()
        period = saved.get("period", "today")
        self._period = period if period in PERIOD_LABELS else "today"
        self._topmost = bool(saved.get("topmost", False))
        self._auto_fade = bool(saved.get("autoFade", True))
        raw_opacity = saved.get("idleOpacity", 0.38)
        try:
            parsed_opacity = float(raw_opacity)
        except (TypeError, ValueError):
            parsed_opacity = 0.38
        self._idle_opacity = (
            max(0.20, min(0.85, parsed_opacity))
            if math.isfinite(parsed_opacity)
            else 0.38
        )
        raw_order = saved.get("providerOrder")
        self._provider_order: list[str] = (
            [str(k) for k in raw_order if isinstance(k, str) and k.strip()]
            if isinstance(raw_order, list)
            else []
        )
        self._theme = normalize_theme(saved.get("theme"))
        self._close_to_tray = bool(saved.get("closeToTray", True))
        self._low_quota_alerts = bool(saved.get("lowQuotaAlerts", True))

    def attach(self, window, hwnd_getter) -> None:
        self._window = window
        self._hwnd_getter = hwnd_getter

    def attach_host(self, host: WindowHost) -> None:
        self._host = host

    def opacity_state(self) -> tuple[bool, float, bool]:
        # Pinning controls z-order; fading is an independent preference.
        return (self._auto_fade, self._idle_opacity, self._ui_busy)

    @property
    def theme(self) -> str:
        return self._theme

    @property
    def close_to_tray(self) -> bool:
        return self._close_to_tray

    @property
    def low_quota_alerts(self) -> bool:
        return self._low_quota_alerts

    @property
    def demo(self) -> bool:
        return self._demo

    def set_tray_available(self, available: bool) -> None:
        self._tray_available = bool(available)

    def _read_preferences(self) -> dict:
        return self._prefs.read()

    def _write_preferences(self) -> None:
        # Merges into the shared store so the geometry and alert-state writers
        # (and keys this build does not know) are never overwritten.
        self._prefs.update(
            {
                "period": self._period,
                "topmost": self._topmost,
                "autoFade": self._auto_fade,
                "idleOpacity": round(self._idle_opacity, 2),
                "providerOrder": self._provider_order,
                "theme": self._theme,
                "closeToTray": self._close_to_tray,
                "lowQuotaAlerts": self._low_quota_alerts,
            }
        )

    def get_initial_state(self) -> dict:
        return {
            "period": self._period,
            "topmost": self._topmost,
            "autoFade": self._auto_fade,
            "idleOpacity": round(self._idle_opacity, 2),
            "providerOrder": list(self._provider_order),
            "demo": self._demo,
            "theme": self._theme,
            "closeToTray": self._close_to_tray,
            "lowQuotaAlerts": self._low_quota_alerts,
            "trayAvailable": self._tray_available,
            "version": __version__,
            "update": self.update_summary(),
        }

    # -- theme -------------------------------------------------------------

    def set_theme(self, theme: str) -> dict:
        self._theme = normalize_theme(theme)
        self._write_preferences()
        # The page reports the effective result next; until then keep the
        # window chrome in step with what the choice resolves to.
        if self._host is not None:
            self._host.apply_theme(resolve_dark(self._theme))
        return {"ok": True, "theme": self._theme}

    def report_effective_theme(self, dark: bool) -> dict:
        self._effective_dark = bool(dark)
        if self._host is not None:
            self._host.apply_theme(self._effective_dark)
        return {"ok": True}

    # -- tray / lifecycle --------------------------------------------------

    def set_close_to_tray(self, enabled: bool) -> dict:
        self._close_to_tray = bool(enabled)
        self._write_preferences()
        return {"ok": True, "closeToTray": self._close_to_tray}

    def set_low_quota_alerts(self, enabled: bool) -> dict:
        self._low_quota_alerts = bool(enabled)
        self._write_preferences()
        if not self._demo:
            # One switch for the user: mirror it to the service notifier's
            # plan-window kinds. Best effort; the native preference is saved.
            try:
                self._client.request(
                    "/api/setup/notifications",
                    {
                        "kinds": {
                            "threshold": self._low_quota_alerts,
                            "dry": self._low_quota_alerts,
                        }
                    },
                )
                self._service_alerts = None  # force a re-ask
            except Exception:
                LOGGER.debug("Service notification mirror skipped", exc_info=True)
        return {"ok": True, "lowQuotaAlerts": self._low_quota_alerts}

    def service_owns_plan_alerts(self) -> bool:
        """Whether the service notifier raises plan-window alerts right now.

        Asked of ``GET /api/setup/notifications`` and cached briefly: it is
        cheap, but the answer can change (service restarted, toasts enabled).
        A failed or unavailable answer means this window alerts as the fallback.
        """
        if self._demo:
            return False
        cached = self._service_alerts
        if (
            cached is not None
            and time.monotonic() - cached[0] < SERVICE_ALERT_RECHECK_SECONDS
        ):
            return cached[1]
        try:
            data = self._client.request("/api/setup/notifications")
            owned = bool(isinstance(data, dict) and data.get("available") is True)
        except Exception:  # noqa: BLE001 -- any failure means fallback
            owned = False
        self._service_alerts = (time.monotonic(), owned)
        return owned

    def open_external(self, url: str) -> dict:
        """Open a pinned-repository https URL in the default browser."""
        if self._smoke_test:
            return {"ok": False, "error": "Links are disabled in the smoke test."}
        if not is_allowed_external_url(url):
            return {"ok": False, "error": "That link is not allowed."}
        try:
            opened = open_in_browser(url)
        except Exception:
            LOGGER.debug("Opening a link failed", exc_info=True)
            return {"ok": False, "error": "Could not open the browser."}
        if not opened:
            return {"ok": False, "error": "Could not open the browser."}
        return {"ok": True}

    def quit_app(self) -> dict:
        if self._host is not None:
            self._host.quit()
        elif self._window is not None:
            self._window.destroy()
        return {"ok": True}

    # -- host -> page ------------------------------------------------------

    def call_page(self, method: str, *args) -> None:
        """Fire-and-forget ``window.burnrateHost.<method>(...)`` on a worker.

        Guarded so a page without the hook (or still loading) is a no-op.
        ``evaluate_js`` must not run on the WinForms UI thread, so this never
        blocks the caller.
        """
        if method not in HOST_PAGE_METHODS or self._window is None:
            return
        call = ", ".join(json.dumps(arg) for arg in args)
        script = (
            f"window.burnrateHost && window.burnrateHost.{method} && "
            f"window.burnrateHost.{method}({call})"
        )

        def run() -> None:
            try:
                self._window.evaluate_js(script)
            except Exception as exc:  # noqa: BLE001 -- the page may be gone already
                LOGGER.debug("Host call %s skipped: %s", method, exc)

        threading.Thread(target=run, name="burnrate-host-call", daemon=True).start()

    # -- updates -----------------------------------------------------------

    @staticmethod
    def _update_module():
        try:
            return importlib.import_module("spend_app.native_update")
        except ImportError:
            return None

    def update_summary(self) -> dict:
        fallback = {"kind": "source", "current": __version__, "canAutoUpdate": False}
        module = self._update_module()
        if module is None or not hasattr(module, "update_status"):
            return fallback
        try:
            status = module.update_status()
        except Exception:
            LOGGER.debug("Update status unavailable", exc_info=True)
            return fallback
        if not isinstance(status, dict):
            return fallback
        kind = status.get("kind")
        return {
            "kind": kind if kind in {"installer", "portable", "source"} else "source",
            "current": str(status.get("current") or __version__),
            "canAutoUpdate": bool(status.get("canAutoUpdate")),
        }

    def check_for_update(self) -> dict:
        unavailable = {
            "ok": False,
            "error": "Updates are not available in this build.",
            "current": __version__,
            "available": False,
            "version": "",
            "notes": "",
            "pageUrl": "",
            "canInstall": False,
        }
        module = self._update_module()
        if module is None or not hasattr(module, "check_for_update"):
            return unavailable
        try:
            info = module.check_for_update(timeout=10.0)
        except Exception as exc:  # noqa: BLE001 -- shown as text
            return {**unavailable, "error": str(exc) or "Could not check for updates."}
        if not isinstance(info, dict):
            return {**unavailable, "error": "Could not check for updates."}
        result = {
            "ok": info.get("ok", True) is not False,
            "current": str(info.get("current") or __version__),
            "available": bool(info.get("available")),
            "version": str(info.get("version") or ""),
            "notes": str(info.get("notes") or ""),
            "pageUrl": str(info.get("pageUrl") or ""),
            "canInstall": bool(info.get("canInstall")),
        }
        if info.get("error"):
            result["ok"] = False
            result["error"] = str(info["error"])
        self._last_update_info = dict(result)
        return result

    @staticmethod
    def _progress_payload(args: tuple, kwargs: dict) -> dict:
        raw = args[0] if args and isinstance(args[0], dict) else kwargs
        phase = (
            raw.get("phase")
            or raw.get("state")
            or (args[0] if args and isinstance(args[0], str) else "downloading")
        )
        percent = raw.get("percent")
        if percent is None and len(args) > 1:
            percent = args[1]
        if (
            isinstance(percent, bool)
            or not isinstance(percent, (int, float))
            or not math.isfinite(percent)
        ):
            percent = None
        return {"phase": str(phase), "percent": percent}

    def install_update(self) -> dict:
        module = self._update_module()
        if module is None or not hasattr(module, "install_update"):
            return {"ok": False, "error": "Updates are not available in this build."}

        def progress(*args, **kwargs) -> None:
            # setUpdate always receives a check_for_update-shaped dict; progress
            # rides along under its own key.
            base = self._last_update_info or {
                "ok": True,
                "current": __version__,
                "available": True,
                "version": "",
                "notes": "",
                "pageUrl": "",
                "canInstall": True,
            }
            self.call_page(
                "setUpdate",
                {**base, "progress": self._progress_payload(args, kwargs)},
            )

        try:
            outcome = module.install_update(progress=progress)
        except Exception as exc:  # noqa: BLE001 -- shown as text
            return {"ok": False, "error": str(exc) or "Could not install the update."}
        if outcome is False or (
            isinstance(outcome, dict) and outcome.get("ok") is False
        ):
            error = outcome.get("error") if isinstance(outcome, dict) else ""
            return {"ok": False, "error": str(error or "Could not install the update.")}
        # The installer replaces this process: leave once the reply is sent.
        threading.Timer(0.4, self.quit_app).start()
        return {"ok": True, "restarting": True}

    # -- onboarding --------------------------------------------------------

    def get_onboarding_data(self) -> dict:
        try:
            return build_onboarding_data(self._client, demo=self._demo)
        except Exception:
            LOGGER.debug("Onboarding data failed", exc_info=True)
            return {
                "ok": False,
                "error": "Could not read connections.",
                "tools": [],
                "apiSources": [],
                "hubConnected": False,
            }

    # -- alerts ------------------------------------------------------------

    def fetch_alert_providers(self) -> list[dict] | None:
        """Merged providers for the alert watcher; ``None`` while unavailable.

        Reads only the quota capacity feed: alerts need remaining percentages
        and balances, not token telemetry, and must not start the service.
        """
        if self._demo:
            return None
        try:
            hud = self._client.request(
                "/api/spend/hud?" + urlencode({"machine": "all"})
            )
        except (DesktopError, OSError, RuntimeError, ValueError):
            return None
        return merge_provider_entries({"tools": []}, hud=hud)

    def save_period(self, period: str) -> dict:
        if period in PERIOD_LABELS:
            self._period = period
            self._write_preferences()
        return {"ok": True}

    def save_provider_order(self, order: list[str]) -> dict:
        if isinstance(order, list):
            cleaned: list[str] = []
            seen: set[str] = set()
            for item in order:
                key = str(item or "").strip()
                if key and key not in seen:
                    seen.add(key)
                    cleaned.append(key)
            self._provider_order = cleaned
            self._write_preferences()
        return {"ok": True, "providerOrder": list(self._provider_order)}

    def set_topmost(self, enabled: bool) -> dict:
        requested = bool(enabled)
        if self._window is not None:
            try:
                _set_window_topmost(self._window, self._hwnd_getter, requested)
            except (OSError, AttributeError, RuntimeError) as exc:
                LOGGER.debug("Topmost update skipped: %s", exc)
                return {
                    "ok": False,
                    "topmost": self._topmost,
                    "error": "Could not change the window pin.",
                }
        self._topmost = requested
        self._write_preferences()
        return {"ok": True, "topmost": self._topmost}

    def set_auto_fade(self, enabled: bool) -> dict:
        self._auto_fade = bool(enabled)
        self._write_preferences()
        return {"ok": True, "autoFade": self._auto_fade}

    def set_idle_opacity(self, opacity: float) -> dict:
        try:
            val = float(opacity)
        except (TypeError, ValueError):
            val = 0.38
        if math.isfinite(val):
            self._idle_opacity = max(0.20, min(0.85, val))
            self._write_preferences()
        return {"ok": True, "idleOpacity": round(self._idle_opacity, 2)}

    def set_ui_busy(self, busy: bool) -> dict:
        self._ui_busy = bool(busy)
        return {"ok": True}

    def _safe_snapshot(self, period: str, machine: str) -> dict | None:
        try:
            return self._client.snapshot(period, machine)
        except (DesktopError, OSError, RuntimeError, ValueError, KeyError, TypeError):
            return None

    def _safe_request(self, path: str, body: dict | None = None) -> dict | None:
        try:
            res = self._client.request(path, body)
            return res if isinstance(res, dict) else None
        except (DesktopError, OSError, RuntimeError, ValueError, KeyError, TypeError):
            return None

    def fetch_usage(
        self, period: str = "today", machine: str = "all", restart: bool = False
    ) -> dict:
        use_period = period if period in PERIOD_LABELS else "today"
        use_machine = machine or "all"
        try:
            with self._lock:
                if restart or not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                if restart and not self._demo:
                    self._safe_request("/api/spend/quotas/refresh", {})
                report = self._client.snapshot(use_period, use_machine)
                today_report = (
                    report
                    if use_period == "today"
                    else self._safe_snapshot("today", use_machine)
                )
                week_report = (
                    report
                    if use_period == "week"
                    else self._safe_snapshot("week", use_machine)
                )
                query = urlencode({"machine": use_machine})
                hud = self._safe_request("/api/spend/hud?" + query)
                hud_usage = self._safe_request("/api/spend/hud/usage?" + query)
                report["providers"] = merge_provider_entries(
                    report,
                    hud=hud,
                    hud_usage=hud_usage,
                    today_report=today_report,
                    week_report=week_report,
                    provider_order=self._provider_order,
                )
            return {"ok": True, "report": report}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not refresh usage. Retry shortly."}

    def get_settings_data(self) -> dict:
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                connections = self._client.request("/api/connections")
                machines = self._client.request("/api/machines")
                setup = self._client.request("/api/setup/state")
            return {
                "ok": True,
                "connections": connections.get("connections") or [],
                "machines": machines.get("machines") or [],
                "setup": setup.get("setup") or {},
                "centralServerUrl": setup.get("centralServerUrl") or "",
            }
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load settings."}

    def browse_location(self, kind: str = "") -> str:
        if self._window is None:
            return ""
        prev_busy = self._ui_busy
        self._ui_busy = True
        try:
            import webview

            dialog_type = (
                webview.FileDialog.OPEN
                if kind in {"sqlite", "opencode", "zcode"}
                else webview.FileDialog.FOLDER
            )
            selected = self._window.create_file_dialog(dialog_type)
            if selected and len(selected) > 0:
                return str(selected[0])
        except (OSError, AttributeError, RuntimeError) as exc:
            LOGGER.debug("File dialog skipped: %s", exc)
        finally:
            self._ui_busy = prev_busy
        return ""

    def connect_source(
        self, source: str, is_api: bool, value: str, revision: int = 0
    ) -> dict:
        if self._demo:
            return {"ok": False, "error": "Settings changes are disabled in demo mode."}
        mutation = self._mutations.setdefault(source, SourceMutation(self._client))
        body = {
            "source": source,
            "operation": "connect",
            "revision": int(revision or 0),
            "mode": "api" if is_api else "manual",
            ("secret" if is_api else "location"): value,
        }
        try:
            mutation.send(body)
            return {"ok": True}
        except DesktopError as exc:
            if exc.status_code == 409:
                try:
                    fresh = self._client.request("/api/connections")
                    item = next(
                        (
                            c
                            for c in (fresh.get("connections") or [])
                            if c.get("source") == source
                        ),
                        None,
                    )
                    if item is not None:
                        body["revision"] = int(item.get("revision") or 0)
                        mutation.send(body)
                        return {"ok": True}
                except (OSError, AttributeError, RuntimeError) as exc:
                    LOGGER.debug("Optional UI call skipped: %s", exc)
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not connect source."}

    def disconnect_source(self, source: str, revision: int = 0) -> dict:
        if self._demo:
            return {"ok": False, "error": "Settings changes are disabled in demo mode."}
        mutation = self._mutations.setdefault(source, SourceMutation(self._client))
        body = {
            "source": source,
            "operation": "disable",
            "revision": int(revision or 0),
        }
        try:
            mutation.send(body)
            return {"ok": True}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not disconnect source."}

    def connect_hub_account(
        self,
        server_url: str,
        username: str,
        password: str,
        computer_name: str = "",
        mode: str = "login",
    ) -> dict:
        if self._demo:
            return {"ok": False, "error": "Hub sign-in is disabled in demo mode."}
        raw_url = (server_url or "").strip().rstrip("/")
        if raw_url and "://" not in raw_url:
            raw_url = "https://" + raw_url
        parsed = urlsplit(raw_url)
        if parsed.scheme not in {"https", "http"} or not parsed.hostname:
            return {
                "ok": False,
                "error": "Enter your VPS hub URL (for example https://your-hub.ts.net:8449).",
            }
        clean_user = (username or "").strip()
        if not clean_user or not password:
            return {
                "ok": False,
                "error": "Enter your hub account username and password.",
            }
        auth_mode = (
            "register" if str(mode or "").strip().lower() == "register" else "login"
        )
        clean_comp = (computer_name or "").strip()
        try:
            res = self._client.request(
                "/api/setup/account",
                {
                    "mode": auth_mode,
                    "serverUrl": raw_url,
                    "username": clean_user,
                    "password": password,
                    "computerName": clean_comp,
                    "testSync": True,
                },
            )
            try:
                self._client.request("/api/setup/defer", {})
            except DesktopError:
                pass
            return {
                "ok": True,
                "account": res.get("account") or {},
                "setup": res.get("setup") or {},
            }
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not sign in to hub account."}

    def create_pairing_code(self, hub_url: str, name: str) -> dict:
        if self._demo:
            return {"ok": False, "error": "Pairing is disabled in demo mode."}
        parsed = urlsplit(hub_url.strip())
        if (
            parsed.scheme not in {"https", "http"}
            or not parsed.hostname
            or parsed.hostname in {"localhost", "127.0.0.1", "::1"}
        ):
            return {
                "ok": False,
                "error": "Enter a reachable hub address, not localhost.",
            }
        try:
            res = self._client.request(
                "/api/setup/pair-hub", {"hubUrl": hub_url.strip(), "name": name.strip()}
            )
            return {"ok": True, "pairingCode": res.get("pairingCode", "")}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not generate pairing code."}

    def join_with_code(self, code: str) -> dict:
        if self._demo:
            return {"ok": False, "error": "Pairing is disabled in demo mode."}
        try:
            self._client.request(
                "/api/setup/connect-spoke",
                {"pairingCode": code.strip(), "testSync": True},
            )
            return {"ok": True}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not pair computer."}

    def minimize_window(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def close_window(self) -> dict:
        """The X button: hide to the tray when allowed, otherwise quit."""
        if self._host is not None:
            return {"ok": True, "hidden": self._host.close()}
        if self._window is not None:
            self._window.destroy()
        return {"ok": True, "hidden": False}

    def begin_resize(self, edge: str) -> None:
        if sys.platform != "win32":
            return
        hit = EDGE_HITS.get(str(edge or "").lower())
        hwnd = self._hwnd_getter() if hasattr(self, "_hwnd_getter") else 0
        if not hit or not hwnd:
            return
        try:
            import ctypes

            user32 = ctypes.windll.user32
            user32.ReleaseCapture()
            user32.PostMessageW(hwnd, 0xA1, hit, 0)  # WM_NCLBUTTONDOWN
        except OSError:
            pass


def _find_icon() -> Path | None:
    resource_root = Path(__file__).resolve().parents[1]
    for candidate in (
        resource_root / "burnrate.ico",
        resource_root / "burnrate-assets" / "burnrate.ico",
        resource_root / "src-tauri" / "icons" / "icon.ico",
    ):
        if candidate.is_file():
            return candidate
    return None


def _focus_existing_window(title: str) -> bool:
    """Bring a running BURNRATE window forward, un-hiding it from the tray."""
    if sys.platform != "win32":
        return False
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            if not user32.IsWindowVisible(hwnd):
                user32.ShowWindow(hwnd, 5)  # SW_SHOW: it was hidden to the tray
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, 9)  # SW_RESTORE
            user32.SetForegroundWindow(hwnd)
            return True
    except OSError:
        return False
    return False


class WindowHost:
    """Window lifecycle shared by the X button, Alt+F4, the tray and shutdown.

    All of pywebview's WinForms calls that touch the form go through its own
    ``Invoke``, so these methods are safe from the JS bridge, the tray (UI
    thread) and worker threads alike.
    """

    def __init__(
        self,
        window,
        bridge: DesktopBridge,
        hwnd_box: list[int],
        *,
        opacity: WindowOpacityController,
        tracker: native_geometry.GeometryTracker,
        saved_geometry: dict | None,
        tray_enabled: bool,
        alerts_enabled: bool,
        updates_enabled: bool = False,
        store: PreferenceStore,
        icon_path: Path | None,
    ) -> None:
        self.window = window
        self.bridge = bridge
        self.hwnd_box = hwnd_box
        self.opacity = opacity
        self.tracker = tracker
        self.tray: TrayController | None = None
        self.watcher: QuotaWatcher | None = None
        self.quitting = False
        self._saved_geometry = saved_geometry
        self._plan: native_geometry.RestorePlan | None = None
        self._tray_enabled = tray_enabled
        self._alerts_enabled = alerts_enabled
        self._updates_enabled = updates_enabled
        self.updater: UpdateChecker | None = None
        self._store = store
        self._icon_path = icon_path
        self._quit_lock = threading.Lock()

    # -- state -------------------------------------------------------------

    @property
    def hwnd(self) -> int:
        return int(self.hwnd_box[0] or 0)

    def tray_ready(self) -> bool:
        return self.tray is not None and self.tray.available

    def can_hide_to_tray(self) -> bool:
        """Close hides (rather than quits) only for a real, tray-backed run.

        Demo quits on close so sample windows are never stranded in the tray.
        """
        return (
            not self.quitting
            and not self.bridge.demo
            and self.bridge.close_to_tray
            and self.tray_ready()
        )

    def is_visible(self) -> bool:
        if sys.platform != "win32" or not self.hwnd:
            return False
        import ctypes

        user32 = ctypes.windll.user32
        return bool(
            user32.IsWindowVisible(self.hwnd) and not user32.IsIconic(self.hwnd)
        )

    # -- show / hide / quit ------------------------------------------------

    def show(self) -> None:
        try:
            self.window.show()
            if sys.platform == "win32" and self.hwnd:
                import ctypes

                user32 = ctypes.windll.user32
                if user32.IsIconic(self.hwnd):
                    user32.ShowWindow(self.hwnd, 9)  # SW_RESTORE
                user32.SetForegroundWindow(self.hwnd)
        except Exception:
            LOGGER.debug("Show skipped", exc_info=True)
        self.opacity.wake()

    def hide(self) -> None:
        self.tracker.flush()  # last chance: a hidden window is never saved
        try:
            self.window.hide()
        except Exception:
            LOGGER.debug("Hide skipped", exc_info=True)

    def toggle(self) -> None:
        if self.is_visible():
            self.hide()
        else:
            self.show()

    def close(self) -> bool:
        """X button / ``close_window``: True when the window was hidden."""
        if self.can_hide_to_tray():
            self.hide_for_close()
            return True
        self.quit()
        return False

    def hide_for_close(self) -> None:
        """Hide because the user closed the window; hint once, ever, why it lives on."""
        self.hide()
        self._show_tray_hint()

    def _show_tray_hint(self) -> None:
        if self.bridge.demo or not self._tray_enabled or self.tray is None:
            return
        if self._store.read().get(TRAY_HINT_KEY):
            return
        self._store.update({TRAY_HINT_KEY: True})  # persist first: never repeats
        self.tray.notify(
            "BURNRATE is still running",
            "It keeps tracking in the notification area. Right-click the icon to quit.",
        )

    def quit(self) -> None:
        """Exit cleanly: save, stop workers, remove the tray icon, close."""
        with self._quit_lock:
            if self.quitting:
                return
            self.quitting = True
        threading.Thread(
            target=self._finish_quit, name="burnrate-quit", daemon=True
        ).start()

    def _finish_quit(self) -> None:
        self.tracker.flush()
        self._stop_workers()
        try:
            self.window.destroy()
        except Exception:
            LOGGER.debug("Destroy skipped", exc_info=True)

    def _stop_workers(self) -> None:
        self.tracker.disable()
        self.opacity.stop()
        if self.watcher is not None:
            self.watcher.stop()
        if self.updater is not None:
            self.updater.stop()

    def shutdown(self) -> None:
        """Idempotent final cleanup, also reached when the window died early."""
        self.quitting = True
        self._stop_workers()
        if self.tray is not None:
            self.tray.dispose()

    # -- page <-> host -----------------------------------------------------

    def refresh_page(self) -> None:
        self.bridge.call_page("refresh")

    def open_settings(self, tab: str | None = None) -> None:
        self.show()
        self.bridge.call_page("openSettings", tab)

    def apply_theme(self, dark: bool) -> None:
        """Recolour the frame, hairline border and form background for ``dark``."""
        _set_theme_chrome(self.hwnd, dark)
        native = getattr(self.window, "native", None)
        if native is None:
            return

        def recolour() -> None:
            import System.Drawing  # type: ignore[import-not-found]

            native.BackColor = System.Drawing.ColorTranslator.FromHtml(
                background_for(dark)
            )

        run_on_ui(native, recolour)

    # -- pywebview events --------------------------------------------------

    def on_before_show(self) -> None:
        """Runs on the form's UI thread, after construction and before ``Show``."""
        native = self.window.native
        try:
            self.hwnd_box[0] = int(native.Handle.ToInt64())
        except (AttributeError, OSError, RuntimeError):
            return
        # Themed chrome before the first paint, inline on this (UI) thread.
        _apply_win11_chrome(
            self.window, self._icon_path, resolve_dark(self.bridge.theme)
        )
        if self._saved_geometry is not None:
            try:
                plan = native_geometry.plan_restore(
                    native_geometry.parse_saved(self._saved_geometry),
                    native_geometry.enum_monitors(),
                    native_geometry.window_dpi(self.hwnd),
                )
                native_geometry.apply_before_show(native, plan)
                self._plan = plan
            except Exception:
                LOGGER.warning("Could not restore the window position", exc_info=True)
        if self._tray_enabled:
            self._create_tray(native)
        self._install_form_hooks(native)

    def _create_tray(self, native) -> None:
        tooltip = "BURNRATE" + (" · Sample data" if self.bridge.demo else "")
        tray = TrayController(
            native,
            self._icon_path,
            on_toggle=self.toggle,
            on_show=self.show,
            on_refresh=self.refresh_page,
            on_settings=self.open_settings,
            on_quit=self.quit,
            tooltip=tooltip,
        )
        if tray.create():
            LOGGER.info("Notification-area icon created")
            self.tray = tray
            self.bridge.set_tray_available(True)

    def _install_form_hooks(self, native) -> None:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

        # WinForms reports a WM_CLOSE sent by another process (the taskbar's
        # Close window, Task Manager) as TaskManagerClosing, so both count as the
        # user closing the window. Shutdown, app exit and owner closes do not.
        user_closing = (
            WinForms.CloseReason.UserClosing,
            WinForms.CloseReason.TaskManagerClosing,
        )

        def on_closing(_sender, args) -> None:
            # Only a user close (X, Alt+F4, taskbar) may hide to the tray;
            # Windows shutdown and application exit must go through.
            LOGGER.debug(
                "Closing: reason=%s hide=%s", args.CloseReason, self.can_hide_to_tray()
            )
            if args.CloseReason in user_closing and self.can_hide_to_tray():
                args.Cancel = True
                self.hide_for_close()
                return
            self.quitting = True
            self.tracker.flush()  # still visible here: the last rectangle
            self._stop_workers()

        def on_closed(_sender, _args) -> None:
            # On the UI thread, so the icon is removed before the loop exits.
            if self.tray is not None:
                self.tray.dispose()

        native.FormClosing += on_closing
        native.FormClosed += on_closed

    def on_shown(self) -> None:
        self.hwnd_box[0] = _apply_win11_chrome(
            self.window, self._icon_path, resolve_dark(self.bridge.theme)
        )
        self._settle_geometry()
        if self._alerts_enabled and self.tray_ready() and self.watcher is None:
            self.watcher = QuotaWatcher(
                self.bridge.fetch_alert_providers,
                self._store,
                self._notify,
                self.tray.set_tooltip,
                alerts_enabled=lambda: self.bridge.low_quota_alerts,
                plan_windows_service_owned=self.bridge.service_owns_plan_alerts,
            )
            self.watcher.start()
        if self._updates_enabled and self.updater is None:
            self.updater = UpdateChecker(
                status=self.bridge.update_summary,
                check=self.bridge.check_for_update,
                store=self._store,
                push=lambda result: self.bridge.call_page("setUpdate", result),
                notify=self._notify_update,
            )
            self.updater.start()

    def _notify_update(self, version: str) -> None:
        if self.tray is not None:
            self.tray.notify(
                f"BURNRATE {version} is available",
                "Open Settings to install it. Nothing installs until you choose to.",
                on_click=lambda: self.open_settings("general"),
            )

    def _notify(self, item: dict) -> None:
        if self.tray is not None:
            self.tray.notify(
                item["title"], item["message"], critical=item["level"] == "critical"
            )

    def _settle_geometry(self) -> None:
        """Re-assert the restored rectangle if WinForms moved or rescaled it
        while first showing (the pre-show placement is the normal path)."""
        plan = self._plan
        if plan is None or sys.platform != "win32" or not self.hwnd:
            return
        current = native_geometry.window_rect(self.hwnd)
        if current is None:
            return
        wanted_position = plan.position or (current.left, current.top)
        if (current.left, current.top, current.width, current.height) != (
            *wanted_position,
            plan.width,
            plan.height,
        ):
            import ctypes

            ctypes.windll.user32.SetWindowPos(
                self.hwnd,
                0,
                wanted_position[0],
                wanted_position[1],
                plan.width,
                plan.height,
                0x0004 | 0x0010,  # SWP_NOZORDER | SWP_NOACTIVATE
            )


def _remove_tree(path: Path, attempts: int = 16, pause: float = 0.25) -> bool:
    """Delete the per-run WebView2 profile. Its helper processes release their
    files a moment after the window closes, so retry briefly before giving up."""
    for attempt in range(attempts):
        shutil.rmtree(path, ignore_errors=True)
        if not path.exists():
            return True
        if attempt + 1 < attempts:
            time.sleep(pause)
    return False


def run_webview(
    client: NativeClient | DemoClient,
    *,
    preferences: Path | None = None,
    demo: bool = False,
    smoke_test: bool = False,
) -> int:
    """Run the window. ``preferences`` is the one switch for persistence: demo
    passes ``None`` (nothing saved); a test may pass a temporary file."""
    import webview

    if not demo and not smoke_test and _focus_existing_window("BURNRATE"):
        return 0

    bridge = DesktopBridge(
        client, preferences=preferences, demo=demo, smoke_test=smoke_test
    )
    store = store_for(preferences)
    html = load_ui_html(bridge.theme)
    title = "BURNRATE" + (" · Sample data" if demo else "")
    window = webview.create_window(
        title,
        html=html,
        js_api=bridge,
        width=native_geometry.DEFAULT_SIZE[0],
        height=native_geometry.DEFAULT_SIZE[1],
        min_size=native_geometry.MIN_SIZE,
        frameless=True,
        easy_drag=False,
        on_top=bridge._topmost,
        background_color=background_for(resolve_dark(bridge.theme)),
    )

    hwnd_box = [0]
    bridge.attach(window, lambda: hwnd_box[0])
    opacity_ctl = WindowOpacityController(bridge, lambda: hwnd_box[0])
    persist = preferences is not None and not smoke_test
    tracker = native_geometry.GeometryTracker(store, lambda: hwnd_box[0])
    if not persist:
        tracker.disable()
    host = WindowHost(
        window,
        bridge,
        hwnd_box,
        opacity=opacity_ctl,
        tracker=tracker,
        saved_geometry=store.read().get(native_geometry.PREFERENCE_KEY)
        if persist
        else None,
        tray_enabled=not smoke_test,
        alerts_enabled=not smoke_test and not demo,
        updates_enabled=not smoke_test and not demo,
        store=store,
        icon_path=_find_icon(),
    )
    bridge.attach_host(host)
    result = [0]

    def on_shown():
        host.on_shown()
        if not smoke_test:
            opacity_ctl.start()

    def on_moved(*_position):
        tracker.notify_changed()

    def on_resized(*_size):
        tracker.notify_changed()

    def on_closed():
        opacity_ctl.stop()

    def on_loaded():
        if not hwnd_box[0]:
            hwnd_box[0] = _apply_win11_chrome(
                window, _find_icon(), resolve_dark(bridge.theme)
            )
        if not smoke_test:
            opacity_ctl.start()
            return

        def verify():
            deadline = time.monotonic() + 8.0
            ok = False
            while time.monotonic() < deadline:
                try:
                    count = window.evaluate_js(
                        "document.querySelectorAll('#providerList .provider-item').length"
                    )
                    hero = window.evaluate_js(
                        "document.getElementById('heroValue')?.textContent || ''"
                    )
                    font_ok = window.evaluate_js(
                        "document.fonts.check('700 30px \"Plus Jakarta Sans\"')"
                    )
                    if (
                        isinstance(count, int)
                        and count >= 8
                        and hero.startswith("$")
                        and font_ok
                    ):
                        ok = True
                        break
                except (OSError, AttributeError, RuntimeError) as exc:
                    LOGGER.debug("Optional UI call skipped: %s", exc)
                time.sleep(0.15)
            if ok:
                try:
                    window.resize(336, 440)
                    time.sleep(0.15)
                    count_after = window.evaluate_js(
                        "document.querySelectorAll('#providerList .provider-item').length"
                    )
                    ok = isinstance(count_after, int) and count_after >= 8
                except (OSError, AttributeError, RuntimeError, ValueError):
                    ok = False
            result[0] = 0 if ok else 1
            host.quitting = True  # a direct destroy must never be cancelled
            window.destroy()

        threading.Thread(target=verify, daemon=True).start()

    window.events.before_show += host.on_before_show
    window.events.shown += on_shown
    window.events.loaded += on_loaded
    window.events.closed += on_closed
    window.events.moved += on_moved
    window.events.resized += on_resized

    cache_root = Path(tempfile.gettempdir()) / "burnrate-wv2-sessions"
    cache_root.mkdir(parents=True, exist_ok=True)
    storage_dir = cache_root / f"pid-{os.getpid()}"
    storage_dir.mkdir(parents=True, exist_ok=True)

    gui_backend = "edgechromium" if sys.platform == "win32" else None
    try:
        webview.start(
            gui=gui_backend,
            private_mode=False,
            storage_path=str(storage_dir),
        )
    finally:
        host.shutdown()
        _remove_tree(storage_dir)
    return result[0]
