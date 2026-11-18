"""Frameless Windows 11 WebView2 host for BURNRATE's Wealthfront iOS desktop UI."""

from __future__ import annotations

import base64
import importlib
import json
import logging
import math
import os
import re
import shutil
import struct
import sqlite3
import sys
from datetime import UTC, datetime, timedelta
from uuid import uuid4
import zlib
import tempfile
import threading
import time
from pathlib import Path
from urllib.parse import urlencode

from spend_app import __version__, native_geometry
from spend_app.codex_speed import configured_codex_speed
from spend_app.error_codes import (
    HUB_PROTOCOL_VERSION,
    CodedError,
    error_message,
    format_error,
    map_http_error,
    map_transport_error,
)
from spend_app.redact import redact_hub_urls
from spend_app.native_alerts import QuotaWatcher
from spend_app.native_activation import ActivationListener, signal_running_instance
from spend_app.native_autoupdate import UpdateChecker
from spend_app.native_desktop import (
    PERIOD_LABELS,
    DemoClient,
    DesktopError,
    NativeClient,
    SourceMutation,
)
from spend_app.native_links import is_allowed_external_url
from spend_app.native_onboarding import build_onboarding_data, build_setup_scan_data
from spend_app.native_prefs import PreferenceStore, store_for
from spend_app.native_tray import TrayController, run_on_ui
from spend_app.opencode_providers import provider_tool_name
from spend_app.provider_urgency import (
    PROFILE_FAMILIES,
    assign_display_rank,
    build_funds_runway,
    build_pace,
    compute_urgency,
    window_has_current_reading,
)
from spend_app.tab_window import (
    SURFACE_KEY,
    SURFACES,
    TAB_HEIGHT,
    TAB_PREFERENCE_KEY,
    SurfaceController,
    TabBridge,
    TabHost,
    TabModel,
    load_tab_html,
)

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


_SOURCE_DISPLAY_NAMES = {
    "claude_local": ("claude-code", "Claude Code"),
    "codex_local": ("codex", "Codex"),
    "opencode_local": ("opencode-go", "OpenCode Go"),
    "zcode_local": ("zcode", "Z.AI Coding Plan"),
    "grok_local": ("grok", "Grok Build"),
    "antigravity_local": ("antigravity", "Antigravity"),
    "muse_local": ("muse", "Muse Code"),
    "openrouter": ("openrouter", "OpenRouter"),
    "deepseek": ("deepseek", "DeepSeek"),
}
_MULTI_PRIMARY_KEYS = PROFILE_FAMILIES


def _store_profile_index(connections: list[dict] | None) -> dict[str, dict]:
    """Map profile tool_keys (default and extra) to their store profile names."""
    index: dict[str, dict] = {}
    if not isinstance(connections, list):
        return index
    for conn in connections:
        if not isinstance(conn, dict):
            continue
        source = str(conn.get("source") or "").strip()
        default_key, default_display = _SOURCE_DISPLAY_NAMES.get(
            source, ("", str(conn.get("name") or source))
        )
        profiles = conn.get("profiles")
        if not isinstance(profiles, list):
            continue
        for prof in profiles:
            if not isinstance(prof, dict):
                continue
            pkey = str(prof.get("key") or "").strip()
            if not pkey:
                continue
            tkey = default_key if pkey == "default" else pkey
            if not tkey:
                continue
            index[tkey] = {
                "source": source,
                "profileKey": pkey,
                "name": str(prof.get("name") or "").strip(),
                "providerName": default_display,
                "plan": prof.get("plan"),
                "state": prof.get("state"),
            }
    return index


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
    if k.startswith("opencode-provider:"):
        return (23, k)
    if k == "zcode":
        return (24, k)
    if k.startswith("zcode-"):
        return (25, k)
    if k == "codex":
        return (30, k)
    if k.startswith("codex-"):
        return (31, k)
    if k == "antigravity":
        return (40, k)
    if k.startswith("antigravity-"):
        return (41, k)
    if k == "grok":
        return (50, k)
    if k.startswith("grok-"):
        return (51, k)
    if k == "openrouter":
        return (60, k)
    if k == "deepseek":
        return (70, k)
    if k == "muse":
        return (80, k)
    if k.startswith("muse-"):
        return (81, k)
    return (90, k)


def merge_provider_entries(
    period_report: dict,
    hud: dict | None = None,
    hud_usage: dict | None = None,
    today_report: dict | None = None,
    week_report: dict | None = None,
    provider_order: list[str] | None = None,
    connections: list[dict] | None = None,
    local_machine_id: str = "local",
    machine: str = "all",
    *,
    now: datetime | None = None,
) -> list[dict]:
    """Merge live quota/balance lanes with recorded token telemetry across accounts."""
    now = now or datetime.now(UTC)
    capacity = (hud or {}).get("capacity") if isinstance(hud, dict) else []
    if not isinstance(capacity, list):
        capacity = []
    window_tokens_all = (
        (hud_usage or {}).get("windowTokens") if isinstance(hud_usage, dict) else {}
    )
    if not isinstance(window_tokens_all, dict):
        window_tokens_all = {}

    store_profiles = _store_profile_index(connections)
    hidden = set()
    if machine in ("all", local_machine_id) or period_report.get("scope") == "local-fallback":
        hidden = {key for key, profile in store_profiles.items() if profile["state"] == "disabled"}
    if hidden and machine == "all" and period_report.get("scope") != "local-fallback":
        # Keep readings from other computers even when this computer stopped tracking the provider.
        scoped_capacity = []
        for card in capacity:
            key = str(card.get("providerKey") or "").strip() if isinstance(card, dict) else ""
            if key not in hidden:
                scoped_capacity.append(card)
                continue
            rows = []
            for row in card.get("rows") or []:
                source = str(row.get("source") or "") if isinstance(row, dict) else ""
                kind, _, origin = source.partition(":")
                origin = origin.partition(":")[0]
                if kind == "sync" and origin and origin != local_machine_id:
                    rows.append(row)
            if rows:
                hidden.discard(key)
                balance = next((row.get("used") for row in rows if row.get("limitKey") == "balance"), None)
                scoped_capacity.append({**card, "rows": rows, "fundsRemainingUsd": balance, "fundsRunway": None})
        capacity = scoped_capacity

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
        if isinstance(c, dict) and c.get("providerKey") and c["providerKey"] not in hidden
    }
    multi_primary_keys = {
        pk
        for pk in _MULTI_PRIMARY_KEYS
        if pk in capacity_keys and any(k.startswith(pk + "-") for k in capacity_keys)
    }

    consumed_usage_keys: set[str] = set()
    providers: list[dict] = []

    for card in capacity:
        if not isinstance(card, dict):
            continue
        key = str(card.get("providerKey") or "").strip()
        if not key or key in hidden:
            continue

        if key == "opencode" and "zcode" not in capacity_keys:
            usage_keys: tuple[str, ...] = ("opencode", "zcode")
        else:
            usage_keys = (key,)
        consumed_usage_keys.update(usage_keys)

        period_entry = _combine_tool_entries(period_tools, usage_keys)
        today_entry = _combine_tool_entries(today_tools, usage_keys)
        week_entry = _combine_tool_entries(week_tools, usage_keys)

        raw_title = str(
            card.get("title")
            or card.get("providerName")
            or period_entry.get("name")
            or key
        ).strip()
        acct_label = str(card.get("accountLabel") or "").strip()
        if acct_label and raw_title.endswith(" · " + acct_label):
            display_name = raw_title[: -(len(acct_label) + 3)].strip()
            badge = acct_label
        elif " · " in raw_title and any(
            key.startswith(pk + "-") for pk in _MULTI_PRIMARY_KEYS
        ):
            display_name, _, tail_label = raw_title.partition(" · ")
            display_name = display_name.strip()
            badge = acct_label or tail_label.strip()
        else:
            display_name = raw_title
            badge = acct_label

        store_prof = store_profiles.get(key)
        if store_prof and store_prof.get("name"):
            if store_prof["profileKey"] != "default":
                badge = store_prof["name"]
                if display_name == key or display_name == raw_title:
                    display_name = store_prof["providerName"]
            elif store_prof["name"] != "Default":
                badge = store_prof["name"]

        if key in multi_primary_keys and not badge:
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
                    "remainingPct": rem_pct,
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
                    "pace": build_pace(row.get("forecast")),
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
        measured_windows = [w for w in windows if window_has_current_reading(w, now)]

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

        is_profile_member = key in multi_primary_keys or any(
            key.startswith(pk + "-") for pk in _MULTI_PRIMARY_KEYS
        )
        provider = {
            "key": key,
            "name": display_name,
            "badge": badge,
            "profile": badge if is_profile_member and badge else None,
            "plan": str(card.get("plan") or ""),
            "inUse": bool(card.get("inUse")),
            "isPayg": bool(card.get("isPayg")),
            "fundsRemainingUsd": float(funds_usd) if has_funds else None,
            "fundsRunwayInfo": build_funds_runway(card.get("fundsRunway"))
            if has_funds
            else None,
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
        provider["urgency"] = compute_urgency(provider, now)
        providers.append(provider)

    # Include any recorded-usage tools that do not have a quota card (e.g. Org API feeds)
    for key, tool in period_tools.items():
        if key in consumed_usage_keys or key in hidden:
            continue
        opencode_provider = key.startswith("opencode-provider:")
        is_extra_profile = any(key.startswith(pk + "-") for pk in _MULTI_PRIMARY_KEYS)
        store_prof = store_profiles.get(key)
        raw_tool_name = str(tool.get("name") or key)
        badge = "Org API" if tool.get("organization") else ""
        plan_label = "Recorded usage" if opencode_provider else "API"
        is_payg = not opencode_provider
        if (
            store_prof
            and store_prof.get("name")
            and (
                store_prof["profileKey"] != "default"
                or store_prof["name"] != "Default"
            )
        ):
            name = store_prof["providerName"]
            if not badge:
                badge = store_prof["name"]
            if is_extra_profile:
                plan_label = str(store_prof.get("plan") or "Recorded usage")
                is_payg = False
        elif is_extra_profile and " · " in raw_tool_name:
            base_name, _, tail_label = raw_tool_name.partition(" · ")
            name = base_name.strip()
            if not badge:
                badge = tail_label.strip()
            plan_label = "Recorded usage"
            is_payg = False
        else:
            name = provider_tool_name(key) or raw_tool_name
        today_value = (today_tools.get(key) or {}).get("tokens")
        week_value = (week_tools.get(key) or {}).get("tokens")
        day_tok = int(today_value) if isinstance(today_value, (int, float)) else None
        week_tok = int(week_value) if isinstance(week_value, (int, float)) else None
        val_usd = tool.get("valueUsd")
        has_val = isinstance(val_usd, (int, float)) and math.isfinite(float(val_usd))
        headline = f"${float(val_usd):,.2f}" if has_val else "Tracked"
        provider = {
            "key": key,
            "name": name,
            "badge": badge,
            "profile": badge if is_extra_profile and badge else None,
            "plan": plan_label,
            "inUse": False,
            "isPayg": is_payg,
            "fundsRemainingUsd": None,
            "fundsRunwayInfo": None,
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
        provider["urgency"] = compute_urgency(provider, now)
        providers.append(provider)

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
    assign_display_rank(providers, now)
    return providers


# H9 analytics
ANALYTICS_WINDOW_ALIASES = {
    "today": "1d",
    "week": "1w",
    "month": "mtd",
    "7d": "1w",
    "30d": "1mo",
    "mtd": "mtd",
    "MTD": "mtd",
    "ytd": "ytd",
    "YTD": "ytd",
    "all": "all",
    "All": "all",
}
ANALYTICS_WINDOWS = (
    "15m",
    "30m",
    "1h",
    "3h",
    "6h",
    "12h",
    "1d",
    "1w",
    "1mo",
    "mtd",
    "ytd",
    "all",
)


def normalize_analytics_window(value: object) -> str:
    raw = str(value or "").strip()
    if raw in ANALYTICS_WINDOWS:
        return raw
    if raw in ANALYTICS_WINDOW_ALIASES:
        return ANALYTICS_WINDOW_ALIASES[raw]
    low = raw.lower()
    if low in ANALYTICS_WINDOWS:
        return low
    if low in ANALYTICS_WINDOW_ALIASES:
        return ANALYTICS_WINDOW_ALIASES[low]
    return "1d"


def period_for_analytics_window(window_key: str) -> str:
    win = normalize_analytics_window(window_key)
    if win in {"15m", "30m", "1h", "3h", "6h", "12h", "1d"}:
        return "today"
    if win == "1w":
        return "week"
    if win in {"1mo", "mtd"}:
        return "month"
    return "all"


THEME_CHOICES = ("system", "light", "dark")
THEME_ATTRIBUTE = 'data-theme-pref="system"'
SERVICE_ALERT_RECHECK_SECONDS = 300.0
TRAY_HINT_KEY = "trayHintShown"
TAB_WINDOW_TITLE = "BURNRATE tab"
#: How long the tab stays opaque after the pointer leaves it (Auto-fade).
TAB_FADE_DELAY = 1.5
TAB_LIGHT_BACKGROUND = "#FFFFFF"
TAB_DARK_BACKGROUND = "#18172B"
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


def embedded_font_css() -> str:
    """The offline Manrope variable font as an ``@font-face`` rule ("" if absent)."""
    font_path = Path(__file__).parent / "fonts" / "manrope-latin.woff2"
    if not font_path.is_file():
        return ""
    b64 = base64.b64encode(font_path.read_bytes()).decode("ascii")
    return (
        '@font-face{font-family:"Manrope";font-style:normal;'
        "font-weight:200 800;font-display:block;"
        f'src:url("data:font/woff2;base64,{b64}") format("woff2");}}'
    )


def load_ui_html(theme_pref: str = "system") -> str:
    """Load wealthfront_ui.html with offline Manrope variable font embedded.

    ``theme_pref`` replaces the page's ``data-theme-pref="system"`` attribute so
    the saved choice is applied before first paint. A page without it is served
    unchanged.
    """
    html_path = Path(__file__).with_name("wealthfront_ui.html")
    html = html_path.read_text(encoding="utf-8")
    html = apply_theme_pref(html, theme_pref)
    return html.replace("/*__EMBEDDED_FONTS__*/", embedded_font_css(), 1)


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


class Win32Fade:
    """The few user32 calls the fader makes, behind one seam so tests can fake them."""

    LWA_ALPHA = 0x02
    WS_EX_LAYERED = 0x00080000
    GWL_EXSTYLE = -20

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._wintypes = wintypes
        self._u = ctypes.windll.user32
        self._u.SetLayeredWindowAttributes.argtypes = [
            ctypes.c_void_p, ctypes.c_uint32, ctypes.c_ubyte, ctypes.c_uint32,
        ]

    def is_window(self, hwnd: int) -> bool:
        return bool(self._u.IsWindow(hwnd))

    def is_iconic(self, hwnd: int) -> bool:
        return bool(self._u.IsIconic(hwnd))

    def is_visible(self, hwnd: int) -> bool:
        return bool(self._u.IsWindowVisible(hwnd))

    def cursor_pos(self):
        point = self._wintypes.POINT()
        if not self._u.GetCursorPos(self._ctypes.byref(point)):
            return None
        return point.x, point.y

    def window_rect(self, hwnd: int):
        rect = self._wintypes.RECT()
        if not self._u.GetWindowRect(hwnd, self._ctypes.byref(rect)):
            return None
        return rect.left, rect.top, rect.right, rect.bottom

    def left_button_down(self) -> bool:
        return bool(self._u.GetAsyncKeyState(0x01) & 0x8000)

    def ensure_layered(self, hwnd: int) -> bool:
        try:
            style = self._u.GetWindowLongW(hwnd, self.GWL_EXSTYLE)
            if not (style & self.WS_EX_LAYERED):
                self._u.SetWindowLongW(hwnd, self.GWL_EXSTYLE, style | self.WS_EX_LAYERED)
            return bool(self._u.SetLayeredWindowAttributes(hwnd, 0, 255, self.LWA_ALPHA))
        except OSError:
            return False

    def set_alpha(self, hwnd: int, alpha: int) -> None:
        self._u.SetLayeredWindowAttributes(hwnd, 0, max(0, min(255, int(alpha))), self.LWA_ALPHA)


class FadeTarget:
    """One window the opacity controller fades.

    ``hit_test(point)`` says whether the cursor is over the window's visible
    part (default: its rectangle plus a 4 px margin); ``hold()`` keeps it
    opaque for reasons the cursor cannot tell (a hover card, keyboard focus);
    ``leave_delay`` is how long it stays opaque after the cursor leaves.
    """

    def __init__(self, hwnd_getter, *, hit_test=None, hold=None, leave_delay: float = 0.16,
                 follows_ui_busy: bool = True) -> None:
        self.hwnd_getter = hwnd_getter
        self.hit_test = hit_test
        self.hold = hold
        self.leave_delay = leave_delay
        self.follows_ui_busy = follows_ui_busy
        self.layered_hwnd = 0
        self.alpha = 255.0
        self.shown = False
        self.last_inside_at = 0.0
        self.dragging = False
        self.awake_until = 0.0


class WindowOpacityController:
    """Fades the app's frameless windows when the cursor leaves them (Auto-fade).

    One loop serves every window (the full window and the tab), so there is a
    single set of rules and a single owner of the layered-window calls.
    """

    MARGIN = 4
    FLOOR = 46.0
    CEILING = 235.0

    def __init__(self, bridge: DesktopBridge, hwnd_getter, *, api=None) -> None:
        self._bridge = bridge
        self._api = api
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._targets: list[FadeTarget] = [FadeTarget(hwnd_getter)]

    @property
    def main(self) -> FadeTarget:
        return self._targets[0]

    def add_target(self, target: FadeTarget) -> FadeTarget:
        self._targets.append(target)
        return target

    def wake(self, seconds: float = 2.5, target: FadeTarget | None = None) -> None:
        """Hold full opacity for a moment, e.g. right after a tray click shows
        the window while the cursor is still over the taskbar."""
        until = time.monotonic() + seconds
        for item in ([target] if target is not None else self._targets[:1]):
            item.awake_until = until

    def start(self) -> None:
        if sys.platform != "win32" or self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="burnrate-opacity", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _win32(self):
        if self._api is None:
            self._api = Win32Fade()
        return self._api

    def _inside(self, target: FadeTarget, api, point) -> bool:
        if target.hit_test is not None:
            return bool(target.hit_test(point))
        rect = api.window_rect(int(target.hwnd_getter() or 0))
        if rect is None:
            return False
        margin = self.MARGIN
        return (rect[0] - margin) <= point[0] <= (rect[2] + margin) and (
            rect[1] - margin
        ) <= point[1] <= (rect[3] + margin)

    def tick_target(self, target: FadeTarget, now: float) -> bool:
        """One pass for one window; True while it is still mid-fade."""
        api = self._win32()
        hwnd = int(target.hwnd_getter() or 0)
        if not hwnd or not api.is_window(hwnd):
            return False
        if api.is_iconic(hwnd) or not api.is_visible(hwnd):
            # Minimized, or hidden to the tray: nothing to fade, no layered
            # calls against a window that is not on screen, and the next time
            # it appears it starts fully opaque.
            target.shown = False
            return False
        if target.layered_hwnd != hwnd:
            if not api.ensure_layered(hwnd):
                return False
            target.layered_hwnd = hwnd
            target.alpha = 255.0
        if not target.shown:
            target.shown = True
            target.alpha = 255.0
            target.last_inside_at = now
            api.set_alpha(hwnd, 255)

        held = bool(target.hold()) if target.hold is not None else False
        if held:
            target.last_inside_at = now  # the fade delay starts when the hold ends
        hovered = True
        point = api.cursor_pos()
        if point is not None:
            inside = self._inside(target, api, point)
            left_down = api.left_button_down()
            if inside and left_down:
                target.dragging = True
            elif not left_down:
                target.dragging = False
            if inside or target.dragging:
                target.last_inside_at = now
                hovered = True
            else:
                hovered = (now - target.last_inside_at) < target.leave_delay

        auto_fade, idle_opacity, ui_busy = self._bridge.opacity_state()
        if (
            not auto_fade
            or (ui_busy and target.follows_ui_busy)
            or hovered
            or held
            or now < target.awake_until
        ):
            goal = 255.0
        else:
            goal = max(self.FLOOR, min(self.CEILING, round(idle_opacity * 255.0)))

        diff = goal - target.alpha
        if abs(diff) >= 0.75:
            # Snappy fade-in on hover (~130ms), silky fade-out on leave (~250ms)
            rate = 0.30 if diff > 0 else 0.15
            step = diff * rate
            if abs(step) < 1.1:
                target.alpha = goal
            else:
                target.alpha += step
            try:
                api.set_alpha(hwnd, max(35, min(255, round(target.alpha))))
            except OSError:
                pass
            return True
        if target.alpha != goal:
            target.alpha = goal
            try:
                api.set_alpha(hwnd, round(goal))
            except OSError:
                pass
        return False

    def tick(self, now: float | None = None) -> float:
        """One pass over every window; returns how long to sleep before the next."""
        now = time.monotonic() if now is None else now
        fading = False
        for target in list(self._targets):
            try:
                fading = self.tick_target(target, now) or fading
            except OSError:
                LOGGER.debug("Fade pass skipped", exc_info=True)
        return 0.016 if fading else 0.055

    def _run(self) -> None:
        try:
            import ctypes  # noqa: F401 -- Win32Fade needs it; stop quietly where it is missing
        except ImportError:
            return
        while not self._stop.is_set():
            self._stop.wait(self.tick())


_CREDENTIAL_UNSET = object()


class _StaleOwnerLifecycle(Exception):
    """Raised inside a credential transaction when a newer lifecycle took over."""


class DesktopBridge:
    """In-process Python bridge exposed to the local WebView2 UI."""

    OWNER_SESSION_VAULT_REF = "burnrate-owner-session"
    MACHINE_TOKEN_VAULT_REF = "burnrate-machine-token"
    PENDING_REVOKE_VAULT_REF = "burnrate-pending-revoke"

    def __init__(
        self,
        client: NativeClient | DemoClient,
        *,
        preferences: Path | None = None,
        demo: bool = False,
        smoke_test: bool = False,
        vault=None,
    ) -> None:
        self._client = client
        self._preferences = preferences
        self._smoke_test = smoke_test
        self._vault = vault
        self._owner_session_lock = threading.RLock()
        self._owner_session: str | None = None
        self._owner_session_expires_at: str | None = None
        self._owner_session_gen: int | None = None
        self._machine_token: str | None = None
        self._machine_token_gen: int | None = None
        self._sync_status: str = "idle"
        self._sync_error_code: str | None = None
        self._sync_error_message: str | None = None
        self._sync_gen: int = 0
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
        self._analytics_window = normalize_analytics_window(saved.get("analyticsWindow") or self._period)
        # Which window was last up. The old in-page mini bar becomes the tab.
        surface = str(saved.get(SURFACE_KEY) or "").strip().lower()
        if surface not in SURFACES:
            surface = "tab" if saved.get("hudMini") else "full"
        self._surface = surface
        if "hudMini" in saved:
            self._prefs.update({SURFACE_KEY: surface, "hudMini": None})
        self._tab_model = TabModel()
        self._quota_auth_scope: tuple | None = None
        self._quota_capacity: dict[tuple[str, str], list[dict]] = {}
        self._published_usage_machine: str | None = None
        self._surfaces: SurfaceController | None = None
        # How many fetch_usage calls are running; the tab waits on this rather
        # than starting a refresh of its own.
        self._refresh_state = threading.Lock()
        self._refreshes_running = 0
        self._usage_request = 0
        self._refresh_idle = threading.Event()
        self._refresh_idle.set()
        raw_density = str(saved.get("hudDensity") or "compact").strip().lower()
        self._hud_density = raw_density if raw_density in {"compact", "comfortable"} else "compact"
        if not self._demo:
            self._migrate_legacy_secrets()

    def attach(self, window, hwnd_getter) -> None:
        self._window = window
        self._hwnd_getter = hwnd_getter

    def attach_host(self, host: WindowHost) -> None:
        self._host = host

    def attach_surfaces(self, surfaces: SurfaceController) -> None:
        self._surfaces = surfaces

    @property
    def surface(self) -> str:
        return self._surface

    @property
    def effective_dark(self) -> bool | None:
        return self._effective_dark

    def _remember_surface(self, surface: str) -> None:
        if surface in SURFACES and surface != self._surface:
            self._surface = surface
            self._prefs.update({SURFACE_KEY: surface})

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
                "analyticsWindow": self._analytics_window,
                "hudMini": None,
                SURFACE_KEY: self._surface,
                "hudDensity": self._hud_density,
            }
        )

    def get_initial_state(self) -> dict:
        return {
            "period": self._period,
            "analyticsWindow": self._analytics_window,
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
            "hudMini": False,
            "surface": self._surface,
            "hudDensity": self._hud_density,
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
        self._tab_model.refresh()
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
            "kind": kind if kind in {"installer", "portable", "source", "wheel"} else "source",
            "current": str(status.get("current") or __version__),
            "canAutoUpdate": bool(status.get("canAutoUpdate")),
            **({"lastError": str(status["lastError"])} if status.get("lastError") else {}),
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
            data = build_onboarding_data(self._client, demo=self._demo)
            return self._enrich_account_payload(data, fetch_remote_machines=False)
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
        with self._refresh_state:
            self._usage_request += 1
            request = self._usage_request
            self._refreshes_running += 1
            self._refresh_idle.clear()
        try:
            with self._lock:
                return self._fetch_usage(period, machine, restart, request)
        finally:
            with self._refresh_state:
                self._refreshes_running -= 1
                if self._refreshes_running == 0:
                    self._refresh_idle.set()

    def _wait_for_refresh(self, timeout: float) -> bool:
        """Block until no refresh is running (True), or ``timeout`` passes (False)."""
        return self._refresh_idle.wait(timeout)

    def _fetch_usage(
        self, period: str, machine: str, restart: bool, request: int
    ) -> dict:
        use_period = period if period in PERIOD_LABELS else "today"
        use_machine = machine or "all"
        auth_scope = self._quota_session_scope()
        if auth_scope != self._quota_auth_scope:
            self._quota_auth_scope = auth_scope
            self._quota_capacity.clear()
            self._published_usage_machine = None
        try:
            if restart or not self._service_ready:
                self._client.ensure_service()
                self._service_ready = True
                if auth_scope is None:
                    auth_scope = self._quota_session_scope()
                    self._quota_auth_scope = auth_scope
            if restart and not self._demo:
                self._safe_request("/api/spend/quotas/refresh", {})
            report = self._client.snapshot(use_period, use_machine)
            today_report = report if use_period == "today" else self._safe_snapshot("today", use_machine)
            week_report = report if use_period == "week" else self._safe_snapshot("week", use_machine)
            query = urlencode({"machine": use_machine})
            hud = self._safe_request("/api/spend/hud?" + query)
            hud_usage = self._safe_request("/api/spend/hud/usage?" + query)
            conns_res = self._safe_request("/api/connections") if not self._demo else None
            connections = (conns_res or {}).get("connections")
            if not isinstance(connections, list):
                connections = None
            if auth_scope != self._quota_session_scope():
                raise DesktopError("Computer connection changed. Refresh to load current usage.")
            capacity, quota_status = self._resolve_quota_capacity(
                use_machine, report.get("scope", ""), hud, can_reuse=auth_scope is not None
            )
            report["quotaStatus"] = quota_status
            report["providers"] = merge_provider_entries(
                report,
                hud={"capacity": capacity},
                hud_usage=hud_usage,
                today_report=today_report,
                week_report=week_report,
                provider_order=self._provider_order,
                connections=connections,
                local_machine_id=report.get("localMachineId") or "local",
                machine=use_machine,
            )
            report["providersAuthoritative"] = True
            if not self._demo and (
                use_machine in ("all", report.get("localMachineId") or "local")
                or report.get("scope") == "local-fallback"
            ):
                codex = next((row for row in report["providers"] if row["key"] == "codex"), None)
                if codex is not None:
                    speed = configured_codex_speed(connections)
                    if speed is not None:
                        codex["configuredSpeed"] = speed
            activity = (hud or {}).get("activity")
            report["activity"] = activity if isinstance(activity, list) else (_demo_activity() if self._demo else [])
            today_total = (today_report or {}).get("total")
            if isinstance(today_total, dict):
                report["todayTotal"] = today_total
            if request == self._usage_request:
                self._tab_model.publish_report(report)
                self._published_usage_machine = use_machine
            return {"ok": True, "report": report}
        except DesktopError as exc:
            message, code = str(exc), exc.code
            error = {"ok": False, "error": message, "code": code}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            message, code = "Could not refresh usage. Retry shortly.", None
            error = {"ok": False, "error": message}
        if request == self._usage_request:
            same_scope = use_machine == self._published_usage_machine and auth_scope == self._quota_session_scope()
            if not same_scope:
                self._published_usage_machine = None
            self._tab_model.publish_error(message, code, retain_providers=same_scope)
        return error

    def _quota_session_scope(self) -> tuple | None:
        """Use persisted connection metadata so a changed hub cannot inherit capacity."""
        db_path = self._db_path()
        if self._demo or db_path is None:
            return ("local",)
        if not db_path.is_file():
            return None
        from spend_app.config import resolve_hub_url
        from spend_app.db import connect
        from spend_app.setup_wizard import read_setup_state

        try:
            with connect(db_path) as conn:
                state = read_setup_state(conn)
            spoke = state.get("spokeConfig") or {}
            account = state.get("account") or {}
            hub = (resolve_hub_url() or spoke.get("hubUrl")) if spoke.get("enabled") else None
            return (
                state.get("role"),
                hub,
                spoke.get("credentialGeneration"),
                account.get("machineId"),
                account.get("connectedAt"),
            )
        except (OSError, RuntimeError, ValueError, TypeError, sqlite3.Error):
            return None

    def _resolve_quota_capacity(
        self, machine: str, scope: str, hud: dict | None, *, can_reuse: bool
    ) -> tuple[list[dict], str]:
        hud = hud or {}
        capacity = hud.get("capacity")
        hud_scope = hud.get("scope")
        if scope == "unavailable":
            return [], "unavailable"
        key = (machine, scope)
        if (
            isinstance(capacity, list)
            and hud.get("ok") is not False
            and (hud_scope == scope or (not hud_scope and scope != "local-fallback"))
        ):
            if can_reuse:
                self._quota_capacity[key] = capacity
            return capacity, "fresh"
        if can_reuse and self._quota_capacity.get(key):
            return self._quota_capacity[key], "stale"
        return [], "unavailable"

    def _clear_quota_cache(self) -> None:
        with self._lock:
            self._quota_capacity.clear()
            self._published_usage_machine = None
            self._tab_model.publish_report({"providers": [], "quotaStatus": "unavailable"})


    # H9 analytics
    def save_analytics_window(self, window: str) -> dict:
        win = normalize_analytics_window(window)
        self._analytics_window = win
        mapped_period = period_for_analytics_window(win)
        if mapped_period in PERIOD_LABELS:
            self._period = mapped_period
        self._write_preferences()
        return {"ok": True, "analyticsWindow": self._analytics_window, "period": self._period}

    def fetch_analytics_summary(
        self, window: str = "1d", tool: str = "all", machine: str = "all"
    ) -> dict:
        use_window = normalize_analytics_window(window)
        use_tool = str(tool or "all").strip() or "all"
        use_machine = str(machine or "all").strip() or "all"
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                query = urlencode(
                    {"window": use_window, "tool": use_tool, "machine": use_machine}
                )
                summary = self._client.request("/api/spend/summary?" + query)
                health = self._safe_request("/api/spend/health")
            return {"ok": True, "summary": summary, "health": health}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load analytics summary."}

    def fetch_entity_detail(
        self, kind: str, key: str, window: str = "1d", machine: str = "all", tool: str = ""
    ) -> dict:
        use_kind = str(kind or "").strip().lower()
        use_key = str(key or "").strip()
        if use_kind not in {"model", "tool"} or not use_key:
            return {"ok": False, "error": "Invalid analytics entity."}
        use_window = normalize_analytics_window(window)
        use_machine = str(machine or "all").strip() or "all"
        use_tool = str(tool or "").strip() if use_kind == "model" else ""
        if use_tool == "all":
            use_tool = ""
        params = {"kind": use_kind, "key": use_key, "window": use_window, "machine": use_machine}
        if use_tool:
            params["tool"] = use_tool
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                entity = self._client.request("/api/spend/entity?" + urlencode(params))
            if use_tool and (not isinstance(entity, dict) or entity.get("selectedTool") != use_tool):
                return {"ok": False, "error": "Detail for this tool profile is unavailable."}
            return {"ok": True, "entity": entity}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load detail view."}

    def get_settings_data(self) -> dict:
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                connections = self._client.request("/api/connections")
                machines = self._client.request("/api/machines")
                setup = self._client.request("/api/setup/state")
            payload = {
                "ok": True,
                "connections": connections.get("connections") or [],
                "machines": machines.get("machines") or [],
                "setup": setup.get("setup") or {},
                "hubAvailable": bool(setup.get("hubAvailable", False)),
            }
            return self._enrich_account_payload(payload, fetch_remote_machines=True)
        except DesktopError as exc:
            out = {"ok": False, "error": str(exc)}
            if getattr(exc, "code", None):
                out["code"] = exc.code
            return out
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

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

    def _fresh_profile_revision(self, source: str, profile: str) -> int | None:
        fresh = self._client.request("/api/connections")
        item = next(
            (
                c
                for c in (fresh.get("connections") or [])
                if c.get("source") == source
            ),
            None,
        )
        if item is None:
            return None
        profiles = item.get("profiles")
        if isinstance(profiles, list):
            prof = next(
                (p for p in profiles if isinstance(p, dict) and p.get("key") == profile),
                None,
            )
            if prof is not None:
                return int(prof.get("revision") or 0)
            if profile != "default":
                return None
        return int(item.get("revision") or 0)

    def _run_connection_mutation(
        self,
        source: str,
        mutation_key: str,
        body: dict,
        *,
        profile: str | None = None,
        retry_409: bool = True,
        fallback_error: str = "Could not update connection.",
    ) -> tuple[dict | None, dict | None]:
        if self._demo:
            return None, {
                "ok": False,
                "error": "Settings changes are disabled in demo mode.",
            }
        mutation = self._mutations.setdefault(
            mutation_key, SourceMutation(self._client)
        )
        affects_capacity = body.get("operation") in {"connect", "disable", "add_profile", "remove_profile"}
        try:
            res = mutation.send(body)
            self._mutations.pop(mutation_key, None)
            if affects_capacity:
                self._clear_quota_cache()
            return (res if isinstance(res, dict) else {}), None
        except DesktopError as exc:
            if retry_409 and exc.status_code == 409 and profile is not None:
                try:
                    fresh_rev = self._fresh_profile_revision(source, profile)
                except DesktopError as reload_exc:
                    self._mutations.pop(mutation_key, None)
                    return None, {"ok": False, "error": str(reload_exc)}
                except (
                    OSError,
                    AttributeError,
                    RuntimeError,
                    ValueError,
                    KeyError,
                    TypeError,
                ) as reload_exc:
                    LOGGER.debug("Optional UI call skipped: %s", reload_exc)
                    fresh_rev = None
                if fresh_rev is not None:
                    retry_body = dict(body)
                    retry_body["revision"] = fresh_rev
                    try:
                        res = mutation.send(retry_body)
                        self._mutations.pop(mutation_key, None)
                        if affects_capacity:
                            self._clear_quota_cache()
                        return (res if isinstance(res, dict) else {}), None
                    except DesktopError as retry_exc:
                        if (
                            retry_exc.status_code is not None
                            and 400 <= retry_exc.status_code < 500
                        ):
                            self._mutations.pop(mutation_key, None)
                        return None, {"ok": False, "error": str(retry_exc)}
                    except (OSError, RuntimeError, ValueError, KeyError, TypeError):
                        return None, {"ok": False, "error": fallback_error}
            if exc.status_code is not None and 400 <= exc.status_code < 500:
                self._mutations.pop(mutation_key, None)
            return None, {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return None, {"ok": False, "error": fallback_error}

    def connect_source(
        self,
        source: str,
        is_api: bool,
        value: str,
        revision: int = 0,
        profile: str = "default",
    ) -> dict:
        target_profile = str(profile or "default")
        body = {
            "source": source,
            "profile": target_profile,
            "operation": "connect",
            "revision": int(revision or 0),
            "mode": "api" if is_api else "manual",
            ("secret" if is_api else "location"): value,
        }
        _res, err = self._run_connection_mutation(
            source,
            f"{source}:{target_profile}",
            body,
            profile=target_profile,
            retry_409=True,
            fallback_error="Could not connect source.",
        )
        return err if err is not None else {"ok": True}

    def disconnect_source(
        self, source: str, revision: int = 0, profile: str = "default"
    ) -> dict:
        target_profile = str(profile or "default")
        body = {
            "source": source,
            "profile": target_profile,
            "operation": "disable",
            "revision": int(revision or 0),
        }
        _res, err = self._run_connection_mutation(
            source,
            f"{source}:{target_profile}",
            body,
            profile=target_profile,
            retry_409=True,
            fallback_error="Could not disconnect source.",
        )
        return err if err is not None else {"ok": True}

    def add_profile(
        self,
        source: str,
        candidate_id: str = "",
        location: str = "",
        name: str = "",
    ) -> dict:
        body: dict = {"source": source, "operation": "add_profile"}
        clean_cand = str(candidate_id or "").strip()
        if clean_cand:
            body["candidateId"] = clean_cand
        else:
            body["location"] = str(location or "")
        clean_name = str(name or "").strip()
        if clean_name:
            body["name"] = clean_name
        res, err = self._run_connection_mutation(
            source,
            f"{source}:add",
            body,
            profile=None,
            retry_409=False,
            fallback_error="Could not add profile.",
        )
        if err is not None:
            return err
        return {"ok": True, "connection": (res or {}).get("connection") or {}}

    def rename_profile(
        self, source: str, profile: str, name: str, revision: int = 0
    ) -> dict:
        target_profile = str(profile or "default")
        body = {
            "source": source,
            "profile": target_profile,
            "operation": "rename_profile",
            "name": str(name or "").strip(),
            "revision": int(revision or 0),
        }
        res, err = self._run_connection_mutation(
            source,
            f"{source}:{target_profile}",
            body,
            profile=target_profile,
            retry_409=True,
            fallback_error="Could not rename profile.",
        )
        if err is not None:
            return err
        return {"ok": True, "connection": (res or {}).get("connection") or {}}

    def remove_profile(
        self, source: str, profile: str, revision: int = 0
    ) -> dict:
        target_profile = str(profile or "default")
        body = {
            "source": source,
            "profile": target_profile,
            "operation": "remove_profile",
            "revision": int(revision or 0),
        }
        res, err = self._run_connection_mutation(
            source,
            f"{source}:{target_profile}",
            body,
            profile=target_profile,
            retry_409=True,
            fallback_error="Could not remove profile.",
        )
        if err is not None:
            return err
        return {
            "ok": True,
            "disabled": bool((res or {}).get("disabled")),
            "removed": bool((res or {}).get("removed")),
        }

    def dismiss_candidate(self, source: str, candidate_id: str) -> dict:
        body = {
            "source": source,
            "operation": "dismiss_candidate",
            "candidateId": str(candidate_id or "").strip(),
        }
        res, err = self._run_connection_mutation(
            source,
            f"{source}:dismiss",
            body,
            profile=None,
            retry_409=False,
            fallback_error="Could not dismiss candidate.",
        )
        if err is not None:
            return err
        return {"ok": True, "dismissed": bool((res or {}).get("dismissed", True))}

    def connect_hub_account(
        self,
        username: str,
        password: str,
        computer_name: str = "",
        mode: str = "login",
        setup_token: str = "",
    ) -> dict:
        if self._demo:
            return {"ok": False, "error": "Hub sign-in is disabled in demo mode."}
        clean_user = (username or "").strip()
        if not clean_user:
            return {
                "ok": False,
                "code": "AUTH-106",
                "error": format_error("AUTH-106"),
            }
        auth_mode = (
            "register" if str(mode or "").strip().lower() == "register" else "login"
        )
        if auth_mode == "register":
            from spend_app.setup_wizard import MIN_PASSWORD_LENGTH, normalize_email

            normalized = normalize_email(clean_user)
            if normalized is None:
                return {
                    "ok": False,
                    "code": "AUTH-106",
                    "error": format_error("AUTH-106"),
                }
            clean_user = normalized
            if len(password) < MIN_PASSWORD_LENGTH:
                return {
                    "ok": False,
                    "code": "AUTH-105",
                    "error": format_error("AUTH-105"),
                }
        elif not password:
            return {
                "ok": False,
                "code": "AUTH-101",
                "error": format_error("AUTH-101"),
            }
        clean_comp = (computer_name or "").strip()
        try:
            res = self._client.request(
                "/api/setup/account",
                {
                    "mode": auth_mode,
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
            with self._owner_session_lock:
                self._owner_session = None
                self._owner_session_expires_at = None
                self._owner_session_gen = None
                self._machine_token = None
                self._machine_token_gen = None
            self._get_owner_session_info()
            self._get_machine_token()
            self._clear_quota_cache()
            out = {
                "ok": True,
                "account": res.get("account") or {},
                "setup": res.get("setup") or {},
            }
            sync_res = res.get("syncResult")
            if isinstance(sync_res, dict) and sync_res.get("ok") is False:
                warn_code = sync_res.get("code") or "SYNC-201"
                out["warningCode"] = warn_code
                out["warning"] = format_error(warn_code)
                out["syncResult"] = sync_res
            if res.get("subscriptionHistoryPending"):
                from spend_app.setup_wizard import PLAN_HISTORY_PENDING

                out["warning"] = PLAN_HISTORY_PENDING
            return out
        except DesktopError as exc:
            code = getattr(exc, "code", None)
            if code:
                return {"ok": False, "code": code, "error": str(exc)}
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}

    def set_account_email(self, email: str, password: str) -> dict:
        if self._demo:
            return {"ok": False, "error": "Account changes are disabled in demo mode."}
        from spend_app.setup_wizard import normalize_email

        normalized = normalize_email(email)
        if normalized is None:
            return {
                "ok": False,
                "code": "AUTH-106",
                "error": format_error("AUTH-106"),
            }
        if not password:
            return {
                "ok": False,
                "code": "AUTH-101",
                "error": format_error("AUTH-101"),
            }
        try:
            res = self._client.request(
                "/api/setup/account/email",
                {"email": normalized, "password": password},
            )
            return {
                "ok": True,
                "account": res.get("account") or {},
                "setup": res.get("setup") or {},
            }
        except DesktopError as exc:
            code = getattr(exc, "code", None)
            if code:
                return {"ok": False, "code": code, "error": str(exc)}
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}

    def dismiss_email_prompt(self) -> dict:
        if self._demo:
            return {"ok": True}
        try:
            res = self._client.request("/api/setup/account/dismiss-email-prompt", {})
            return {"ok": True, "setup": res.get("setup") or {}}
        except DesktopError as exc:
            code = getattr(exc, "code", None)
            if code:
                return {"ok": False, "code": code, "error": str(exc)}
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "HUB-501", "error": format_error("HUB-501")}


    def _db_path(self) -> Path | None:
        settings = getattr(self._client, "settings", None)
        path = getattr(settings, "database_path", None)
        return Path(path) if path is not None else None

    def _resolve_vault(self):
        if self._demo:
            return None
        if self._vault is not None:
            return self._vault
        db_path = self._db_path()
        if db_path is None:
            return None
        from spend_app.connection_secrets import get_vault

        return get_vault(db_path)

    def _migrate_legacy_secrets(self) -> None:
        if self._demo or self._vault is not None:
            return
        db_path = self._db_path()
        if db_path is None or not db_path.is_file():
            return
        try:
            from spend_app.sync import migrate_legacy_spoke_secrets

            migrate_legacy_spoke_secrets(db_path)
        except Exception:
            LOGGER.debug("Legacy spoke secret migration skipped", exc_info=True)

    def _capture_owner_session_with_gen(self) -> tuple[str | None, str | None, int | None]:
        db_path = self._db_path()
        db_token: str | None = None
        db_expires_at: str | None = None
        db_gen: int | None = None
        if db_path is not None and db_path.is_file():
            try:
                from spend_app.db import connect
                from spend_app.setup_wizard import read_setup_state

                with connect(db_path) as conn:
                    state = read_setup_state(conn)
                spoke = state.get("spokeConfig") or {}
                db_gen = int(spoke.get("credentialGeneration") or 0)
                db_token = str(spoke.get("ownerSession") or "").strip() or None
                db_expires_at = spoke.get("ownerSessionExpiresAt")
            except Exception:
                db_token = None
                db_expires_at = None
                db_gen = None

        with self._owner_session_lock:
            if (
                db_gen is not None
                and self._owner_session_gen is not None
                and db_gen != self._owner_session_gen
            ):
                self._owner_session = None
                self._owner_session_expires_at = None
                self._owner_session_gen = None
            if self._owner_session and (db_gen is None or self._owner_session_gen == db_gen):
                return (
                    self._owner_session,
                    self._owner_session_expires_at or db_expires_at,
                    self._owner_session_gen,
                )

            vault = self._resolve_vault()
            if vault is not None:
                try:
                    vault_token = vault.get(self.OWNER_SESSION_VAULT_REF)
                    if isinstance(vault_token, str) and vault_token.strip():
                        self._owner_session = vault_token.strip()
                        self._owner_session_expires_at = db_expires_at
                        self._owner_session_gen = db_gen
                        return self._owner_session, self._owner_session_expires_at, self._owner_session_gen
                except Exception:
                    pass

            if db_token:
                self._owner_session = db_token
                self._owner_session_expires_at = db_expires_at
                self._owner_session_gen = db_gen
                return db_token, db_expires_at, db_gen

            self._owner_session_gen = db_gen
        return None, None, db_gen

    def _get_owner_session_info(self) -> tuple[str | None, str | None]:
        token, expires_at, _gen = self._capture_owner_session_with_gen()
        return token, expires_at

    def _get_owner_session_token(self) -> str | None:
        token, _ = self._get_owner_session_info()
        return token

    def _clear_owner_session(
        self,
        *,
        expected_token: object = _CREDENTIAL_UNSET,
        expected_gen: int | None = None,
    ) -> None:
        with self._owner_session_lock:
            vault = self._resolve_vault()
            db_path = self._db_path()
            if db_path is not None and db_path.is_file():
                try:
                    from spend_app.connection_secrets import clear_spoke_credentials, read_vault_secret
                    from spend_app.sync import clear_hub_proxy_cache

                    def _prepare(_conn, current_state):
                        sp = current_state.get("spokeConfig") or {}
                        cur_gen = int(sp.get("credentialGeneration") or 0)
                        if expected_gen is not None and cur_gen != expected_gen:
                            raise _StaleOwnerLifecycle()
                        if expected_token is not _CREDENTIAL_UNSET:
                            cur_tok = (
                                read_vault_secret(db_path, self.OWNER_SESSION_VAULT_REF, vault=vault)
                                or str(sp.get("ownerSession") or "").strip()
                            )
                            if cur_tok != str(expected_token or "").strip():
                                raise _StaleOwnerLifecycle()

                    prepare_fn = (
                        _prepare
                        if (expected_token is not _CREDENTIAL_UNSET or expected_gen is not None)
                        else None
                    )
                    clear_spoke_credentials(
                        db_path,
                        clear_owner_session=True,
                        vault=vault,
                        prepare_state=prepare_fn,
                    )
                    clear_hub_proxy_cache()
                    if expected_token is _CREDENTIAL_UNSET or (self._owner_session or "") == str(
                        expected_token or ""
                    ).strip():
                        self._owner_session = None
                        self._owner_session_expires_at = None
                        self._owner_session_gen = None
                    return
                except _StaleOwnerLifecycle:
                    return
                except Exception:
                    pass
            if expected_token is not _CREDENTIAL_UNSET and (self._owner_session or "") != str(
                expected_token or ""
            ).strip():
                return
            if vault is not None:
                try:
                    vault.delete(self.OWNER_SESSION_VAULT_REF)
                except Exception:
                    pass
            self._owner_session = None
            self._owner_session_expires_at = None
            self._owner_session_gen = None

    def _resolve_hub_origin(self) -> tuple[str | None, dict | None]:
        from spend_app.config import resolve_hub_url
        from spend_app.db import connect
        from spend_app.hub_origin import hub_origin
        from spend_app.setup_wizard import read_setup_state

        raw_hub_url = ""
        try:
            raw_hub_url = resolve_hub_url()
        except ValueError as exc:
            code = "NET-003" if "HTTPS" in str(exc) else "NET-001"
            return None, {"ok": False, "code": code, "error": format_error(code)}

        if not raw_hub_url:
            db_path = self._db_path()
            if db_path is not None and db_path.is_file():
                try:
                    with connect(db_path) as conn:
                        state = read_setup_state(conn)
                    raw_hub_url = str((state.get("spokeConfig") or {}).get("hubUrl") or "").strip()
                except Exception:
                    raw_hub_url = ""

        if not raw_hub_url:
            return None, {"ok": False, "code": "NET-001", "error": format_error("NET-001")}

        try:
            return hub_origin(raw_hub_url), None
        except ValueError as exc:
            code = "NET-003" if "HTTPS" in str(exc) else "NET-001"
            return None, {"ok": False, "code": code, "error": format_error(code)}

    def _call_hub(
        self,
        hub_url: str,
        method: str,
        path: str,
        *,
        json_body: dict | None = None,
        token: str | None = None,
        handshake: bool = False,
        fresh_session: bool = False,
        flush_pending: bool = True,
    ) -> tuple[dict | None, object | None, dict | None]:
        import httpx
        from spend_app.setup_wizard import _hub_protocol_handshake
        from spend_app.sync import _HUB_TEST_CLIENTS

        test_client = _HUB_TEST_CLIENTS.get(hub_url)
        headers = {
            "X-BURNRATE-Request": "1",
            "X-BURNRATE-Protocol": str(HUB_PROTOCOL_VERSION),
        }
        if json_body is not None:
            headers["Content-Type"] = "application/json"

        try:
            if handshake:
                _hub_protocol_handshake(hub_url, test_client)
            if flush_pending and path != "/api/auth/logout":
                try:
                    from spend_app.connection_secrets import flush_pending_revokes

                    flush_pending_revokes(
                        self._db_path(),
                        hub_url,
                        vault=self._resolve_vault(),
                        client=test_client,
                    )
                except Exception:
                    pass
            if token and not fresh_session and path not in {"/api/auth/session/rotate", "/api/auth/logout"}:
                db_path = self._db_path()
                if db_path is not None and db_path.is_file():
                    from spend_app.sync import ensure_fresh_owner_session

                    fresh_token = ensure_fresh_owner_session(
                        db_path,
                        hub_url,
                        token,
                        vault=self._resolve_vault(),
                        client=test_client,
                    )
                    if fresh_token != token:
                        with self._owner_session_lock:
                            if self._owner_session == token:
                                self._owner_session = fresh_token
                                self._owner_session_gen = None
                        token = fresh_token
            if token:
                headers["Authorization"] = f"Bearer {token}"
            if test_client is not None:
                cookies = getattr(test_client, "cookies", None)
                saved_jar = (
                    list(cookies.jar)
                    if cookies is not None and hasattr(cookies, "jar")
                    else None
                )
                if cookies is not None:
                    cookies.clear()
                try:
                    if method == "GET":
                        try:
                            res = test_client.get(path, headers=headers, follow_redirects=False)
                        except TypeError:
                            res = test_client.get(path, headers=headers)
                    else:
                        try:
                            res = test_client.post(path, json=json_body, headers=headers, follow_redirects=False)
                        except TypeError:
                            res = test_client.post(path, json=json_body, headers=headers)
                finally:
                    if cookies is not None and saved_jar is not None:
                        cookies.clear()
                        for cookie in saved_jar:
                            cookies.jar.set_cookie(cookie)
            else:
                with httpx.Client(timeout=10.0, trust_env=False, follow_redirects=False) as http:
                    url = f"{hub_url}{path}"
                    if method == "GET":
                        res = http.get(url, headers=headers)
                    else:
                        res = http.post(url, json=json_body, headers=headers)
        except CodedError as exc:
            if token and not fresh_session and exc.code == "AUTH-107":
                with self._owner_session_lock:
                    if self._owner_session == token:
                        self._owner_session = None
                        self._owner_session_expires_at = None
                        self._owner_session_gen = None
            msg = redact_hub_urls(error_message(exc.code, retry_after=exc.retry_after))
            return None, None, {
                "ok": False,
                "code": exc.code,
                "message": msg,
                "error": redact_hub_urls(str(exc)),
                "retryAfter": exc.retry_after,
            }
        except Exception as exc:
            code, _ = map_transport_error(exc)
            return None, None, {
                "ok": False,
                "code": code,
                "message": redact_hub_urls(error_message(code)),
                "error": redact_hub_urls(format_error(code)),
            }

        try:
            data = res.json() if getattr(res, "content", b"") else {}
        except ValueError:
            data = None

        if 300 <= res.status_code < 400:
            return None, res, {
                "ok": False,
                "code": "HUB-501",
                "message": error_message("HUB-501"),
                "error": format_error("HUB-501"),
            }

        if res.status_code >= 400:
            mapped = map_http_error(
                res.status_code,
                data,
                headers=getattr(res, "headers", None),
                endpoint_path=path,
            )
            if token and not fresh_session and mapped.code == "AUTH-107":
                self._clear_owner_session(expected_token=token)
            return None, res, {
                "ok": False,
                "code": mapped.code,
                "message": redact_hub_urls(error_message(mapped.code, retry_after=mapped.retry_after)),
                "error": redact_hub_urls(str(mapped)),
                "retryAfter": mapped.retry_after,
            }

        if not isinstance(data, dict):
            return None, res, {
                "ok": False,
                "code": "HUB-501",
                "message": error_message("HUB-501"),
                "error": format_error("HUB-501"),
            }

        return data, res, None

    # -- H5 desktop account & computers -----------------------------------

    @staticmethod
    def _default_computer_name() -> str:
        import socket

        for candidate in (
            socket.gethostname() or "",
            os.environ.get("COMPUTERNAME", ""),
        ):
            clean = str(candidate).strip()
            if clean:
                return clean[:64]
        return "My computer"

    @staticmethod
    def _h5_err(
        code: str,
        *,
        retry_after: int | None = None,
        message: str | None = None,
    ) -> dict:
        resolved = code if code else "HUB-501"
        raw_msg = message if message is not None else error_message(resolved, retry_after=retry_after)
        return {
            "ok": False,
            "code": resolved,
            "message": redact_hub_urls(str(raw_msg)),
        }

    @classmethod
    def _h5_err_from_call(cls, call_err: dict) -> dict:
        code = str(call_err.get("code") or "HUB-501")
        retry_after = call_err.get("retryAfter")
        msg = call_err.get("message")
        return cls._h5_err(
            code,
            retry_after=retry_after if isinstance(retry_after, int) else None,
            message=str(msg) if msg else None,
        )

    def _get_machine_token(self) -> str | None:
        db_path = self._db_path()
        db_tok: str | None = None
        db_gen: int | None = None
        if db_path is not None and db_path.is_file():
            try:
                from spend_app.db import connect
                from spend_app.setup_wizard import read_setup_state

                with connect(db_path) as conn:
                    spoke = (read_setup_state(conn).get("spokeConfig") or {})
                    db_gen = int(spoke.get("credentialGeneration") or 0)
                    db_tok = str(spoke.get("token") or "").strip() or None
            except Exception:
                db_tok = None
                db_gen = None
        with self._owner_session_lock:
            if (
                db_gen is not None
                and self._machine_token_gen is not None
                and db_gen != self._machine_token_gen
            ):
                self._machine_token = None
                self._machine_token_gen = None
            if self._machine_token and (db_gen is None or self._machine_token_gen == db_gen):
                return self._machine_token
            vault = self._resolve_vault()
            if vault is not None:
                try:
                    token = vault.get(self.MACHINE_TOKEN_VAULT_REF)
                    if isinstance(token, str) and token.strip():
                        self._machine_token = token.strip()
                        self._machine_token_gen = db_gen
                        return self._machine_token
                except Exception:
                    pass
            if db_tok:
                self._machine_token = db_tok
                self._machine_token_gen = db_gen
                return db_tok
            self._machine_token_gen = db_gen
        return None

    def _local_machine_id(self) -> str:
        db_path = self._db_path()
        if db_path is not None and db_path.is_file():
            try:
                from spend_app.db import connect
                from spend_app.setup_wizard import read_setup_state

                with connect(db_path) as conn:
                    state = read_setup_state(conn)
                return str((state.get("account") or {}).get("machineId") or "").strip()
            except Exception:
                return ""
        return ""

    @staticmethod
    def _annotate_machines(
        machines: list[dict] | None, current_machine_id: str = ""
    ) -> list[dict]:
        rows: list[dict] = []
        for item in machines or []:
            if not isinstance(item, dict):
                continue
            mid = str(item.get("id") or "").strip()
            if not mid:
                continue
            is_cur = bool(item.get("isCurrent") or (current_machine_id and mid == current_machine_id))
            rows.append(
                {
                    "id": mid,
                    "name": str(item.get("name") or mid),
                    "os": str(item.get("os") or ""),
                    "online": item.get("online", True),
                    "lastSeenAt": item.get("lastSeenAt"),
                    "registeredAt": item.get("registeredAt"),
                    "isCurrent": is_cur,
                }
            )
        rows.sort(key=lambda r: (0 if r["isCurrent"] else 1))
        return rows

    def _enrich_account_payload(
        self, payload: dict, *, fetch_remote_machines: bool = False
    ) -> dict:
        if not isinstance(payload, dict):
            return payload
        if self._demo:
            payload.setdefault("hasOwnerSession", False)
            payload.setdefault("accountEmail", "")
            payload.setdefault("computerName", "Sample Desktop")
            payload.setdefault("syncState", {"status": "idle", "code": None, "message": None})
            return payload

        current_mid = self._local_machine_id()
        has_session = bool(self._owner_session_status().get("hasOwnerSession"))

        if fetch_remote_machines and has_session:
            listed = self.list_computers()
            if listed.get("ok") and isinstance(listed.get("machines"), list):
                payload["machines"] = listed["machines"]
            else:
                has_session = bool(self._owner_session_status().get("hasOwnerSession"))

        setup = dict(payload.get("setup") or {}) if isinstance(payload.get("setup"), dict) else {}
        account = dict(setup.get("account") or {}) if isinstance(setup.get("account"), dict) else {}
        spoke = dict(setup.get("spokeConfig") or {}) if isinstance(setup.get("spokeConfig"), dict) else {}

        spoke["hasOwnerSession"] = has_session
        spoke["hasToken"] = bool(self._get_machine_token() or spoke.get("hasToken"))
        if self._owner_session_expires_at and not spoke.get("ownerSessionExpiresAt"):
            spoke["ownerSessionExpiresAt"] = self._owner_session_expires_at
        setup["spokeConfig"] = spoke
        setup["account"] = account

        comp_name = str(
            setup.get("computerName")
            or payload.get("computerName")
            or self._default_computer_name()
        ).strip()
        setup["computerName"] = comp_name

        acct_email = str(
            account.get("email")
            or account.get("username")
            or payload.get("accountEmail")
            or ""
        ).strip()

        with self._owner_session_lock:
            sync_state = {
                "status": self._sync_status,
                "code": self._sync_error_code,
                "message": self._sync_error_message,
            }

        payload["setup"] = setup
        payload["hasOwnerSession"] = has_session
        payload["accountEmail"] = acct_email
        payload["computerName"] = comp_name
        payload["syncState"] = sync_state
        if "machines" in payload and isinstance(payload["machines"], list):
            payload["machines"] = self._annotate_machines(payload["machines"], current_mid)
        return payload

    def _run_background_first_sync(
        self,
        *,
        sync_gen: int,
        hub_url: str,
        machine_token: str,
        machine_name: str,
        db_path: Path | None,
    ) -> None:
        try:
            from spend_app.config import load_settings
            from spend_app.pricing import PricingEngine
            from spend_app.sync import _HUB_TEST_CLIENTS, clear_hub_proxy_cache, sync_once

            client_settings = getattr(self._client, "settings", None)
            if client_settings is None and db_path is not None:
                client_settings = load_settings(database_path=db_path)
            if client_settings is not None:
                pricing = getattr(self._client, "pricing", None)
                if pricing is None:
                    pricing_dir = getattr(client_settings, "pricing_path", None)
                    pricing = (
                        PricingEngine.load(pricing_dir)
                        if pricing_dir is not None and Path(pricing_dir).is_dir()
                        else PricingEngine([])
                    )
                sync_token = None
                vault = self._resolve_vault()
                if vault is not None:
                    try:
                        val = vault.get(self.MACHINE_TOKEN_VAULT_REF)
                        if isinstance(val, str) and val.strip():
                            sync_token = val.strip()
                    except Exception:
                        sync_token = None
                if not sync_token:
                    sync_token = machine_token
                test_client = _HUB_TEST_CLIENTS.get(hub_url)
                sync_once(
                    client_settings,
                    pricing,
                    hub_url=hub_url,
                    token=sync_token,
                    machine_name=machine_name,
                    collect_local=False,
                    client=test_client,
                )
                clear_hub_proxy_cache()
            with self._owner_session_lock:
                if self._sync_gen == sync_gen:
                    self._sync_status = "ok"
                    self._sync_error_code = None
                    self._sync_error_message = None
        except Exception:
            LOGGER.debug("Background first sync failed (SYNC-201)", exc_info=True)
            with self._owner_session_lock:
                if self._sync_gen == sync_gen:
                    self._sync_status = "error"
                    self._sync_error_code = "SYNC-201"
                    self._sync_error_message = redact_hub_urls(error_message("SYNC-201"))

    def _authenticate_and_register(
        self,
        *,
        endpoint: str,
        email: str,
        password: str,
        computer_name: str = "",
    ) -> dict:
        if self._demo:
            return self._h5_err("HUB-501", message="Account actions are disabled in demo mode.")

        from spend_app.plan_service import PlanError
        from spend_app.setup_wizard import (
            MIN_PASSWORD_LENGTH,
            PLAN_HISTORY_PENDING,
            complete_deferred_history,
            normalize_email,
            receive_sign_in_history,
        )
        from spend_app.subscription_transfer import attach_sign_in_history, export_history

        raw_email = (email or "").strip()
        if endpoint == "/api/account/register":
            normalized = normalize_email(raw_email)
            if normalized is None:
                return self._h5_err("AUTH-106")
            clean_user = normalized
            if len(str(password or "")) < MIN_PASSWORD_LENGTH:
                return self._h5_err("AUTH-105")
        else:
            if not raw_email:
                return self._h5_err("AUTH-106")
            if "@" in raw_email:
                normalized = normalize_email(raw_email)
                if normalized is None:
                    return self._h5_err("AUTH-106")
                clean_user = normalized
            else:
                clean_user = raw_email
            if not password:
                return self._h5_err("AUTH-101")

        hub_url, err = self._resolve_hub_origin()
        if err is not None:
            return self._h5_err(str(err.get("code") or "NET-001"))

        db_path = self._db_path()
        initial_gen: int | None = None
        initial_signed_out = False
        mem_initial_gen = self._owner_session_gen
        local_history = None
        settings = getattr(self._client, "settings", None)
        if db_path is not None:
            try:
                from zoneinfo import ZoneInfo

                from spend_app.db import connect, initialize
                from spend_app.setup_wizard import read_setup_state

                initialize(db_path)
                with connect(db_path) as conn:
                    conn.execute("BEGIN IMMEDIATE")
                    sp_init = read_setup_state(conn)["spokeConfig"]
                    today = datetime.now(UTC).astimezone(ZoneInfo(settings.timezone)).date()
                    local_history = export_history(conn, today, native_only=True)
                initial_gen = int(sp_init.get("credentialGeneration") or 0)
                initial_signed_out = bool(sp_init.get("signedOut"))
            except Exception:
                LOGGER.debug("Could not read setup state snapshot before sign-in", exc_info=True)
                return self._h5_err("APP-903")

        clean_comp = (computer_name or "").strip() or self._default_computer_name()
        auth_body = {
            "username": clean_user,
            "password": password,
            "computerName": clean_comp,
            "deviceLabel": clean_comp,
        }
        deferred_history = attach_sign_in_history(auth_body, local_history) if local_history is not None else False
        auth_data, auth_res, call_err = self._call_hub(
            hub_url,
            "POST",
            endpoint,
            json_body=auth_body,
            handshake=True,
        )
        if call_err is not None:
            return self._h5_err_from_call(call_err)
        remote_history = auth_data.get("subscriptionHistory")

        from spend_app.auth_model import extract_cookie
        from spend_app.connection_secrets import (
            revoke_or_queue_machine_token,
            revoke_or_queue_owner_session,
        )
        from spend_app.sync import _HUB_TEST_CLIENTS, HubMutationError

        session_token = extract_cookie(auth_res)
        if not session_token:
            return self._h5_err("HUB-501")

        hub_client = _HUB_TEST_CLIENTS.get(hub_url)
        vault_for_cleanup = self._resolve_vault()

        device_id: str | None = None
        if db_path is not None:
            try:
                from spend_app.sync import get_or_create_device_id

                device_id = get_or_create_device_id(db_path)
            except Exception:
                device_id = None

        reg_body: dict[str, object] = {
            "action": "register",
            "name": clean_comp,
            "os": sys.platform,
        }
        if device_id:
            reg_body["deviceId"] = device_id

        reg_data, _, reg_err = self._call_hub(
            hub_url,
            "POST",
            "/api/machines",
            json_body=reg_body,
            token=session_token,
            fresh_session=True,
        )
        if reg_err is not None:
            try:
                revoke_or_queue_owner_session(
                    db_path,
                    hub_url,
                    session_token,
                    vault=vault_for_cleanup,
                    client=hub_client,
                )
            except CodedError as q_exc:
                return self._h5_err(q_exc.code)
            return self._h5_err_from_call(reg_err)

        machine = reg_data.get("machine") if isinstance(reg_data.get("machine"), dict) else {}
        machine_id = str(machine.get("id") or "").strip()
        machine_token = str(reg_data.get("token") or "").strip()
        if not machine_token:
            try:
                revoke_or_queue_owner_session(
                    db_path,
                    hub_url,
                    session_token,
                    vault=vault_for_cleanup,
                    client=hub_client,
                )
            except CodedError as q_exc:
                return self._h5_err(q_exc.code)
            return self._h5_err("HUB-501")

        expires_at = auth_data.get("expiresAt")
        acct_raw = dict(auth_data.get("account") or {}) if isinstance(auth_data.get("account"), dict) else {}
        acct_email = str(acct_raw.get("email") or acct_raw.get("username") or clean_user).strip()
        account_meta = {
            "username": str(acct_raw.get("username") or acct_email),
            "email": acct_email,
            "needsEmail": bool(acct_raw.get("needsEmail", False)),
            "machineId": machine_id,
        }

        def _cleanup_issued_pair() -> str | None:
            q_code: str | None = None
            try:
                revoke_or_queue_machine_token(
                    db_path,
                    hub_url,
                    machine_token,
                    machine_id=machine_id or None,
                    vault=vault_for_cleanup,
                    client=hub_client,
                )
            except CodedError as qe:
                q_code = qe.code
            try:
                revoke_or_queue_owner_session(
                    db_path,
                    hub_url,
                    session_token,
                    vault=vault_for_cleanup,
                    client=hub_client,
                )
            except CodedError as qe:
                q_code = q_code or qe.code
            return q_code

        with self._owner_session_lock:
            vault = vault_for_cleanup
            try:
                if db_path is not None:
                    from spend_app.connection_secrets import publish_spoke_credentials
                    from spend_app.sync import clear_hub_proxy_cache, prepare_spoke_sync_tx

                    def _prepare_auth_state(_conn, state):
                        sp = state.get("spokeConfig") or {}
                        cur_gen = int(sp.get("credentialGeneration") or 0)
                        if initial_gen is None or cur_gen != initial_gen or (
                            not initial_signed_out and sp.get("signedOut")
                        ):
                            raise _StaleOwnerLifecycle()

                    def _apply_auth_state(conn, state):
                        receive_sign_in_history(
                            conn, settings.timezone, hub_url, remote_history, auth_data.get("subscriptions")
                        )
                        state["role"] = "spoke"
                        state["computerName"] = clean_comp
                        state["account"] = account_meta
                        spoke = dict(state.get("spokeConfig") or {})
                        spoke.pop("token", None)
                        spoke.pop("ownerSession", None)
                        spoke.pop("hubUrl", None)
                        spoke["enabled"] = True
                        spoke["hubConfigured"] = True
                        spoke["ownerSessionExpiresAt"] = expires_at
                        spoke["ownerSessionRotationRequired"] = False
                        spoke["rotationRequired"] = False
                        state["spokeConfig"] = spoke
                        prepare_spoke_sync_tx(conn, hub_url, machine_token)

                    saved_auth = publish_spoke_credentials(
                        db_path,
                        owner_session=session_token,
                        machine_token=machine_token,
                        vault=vault,
                        prepare_state=_prepare_auth_state,
                        apply_state=_apply_auth_state,
                    )
                    clear_hub_proxy_cache()
                    saved_gen = int(
                        ((saved_auth.get("spokeConfig") or {}).get("credentialGeneration")) or 0
                    )
                else:
                    from spend_app.connection_secrets import _restore_vault_secret, verified_vault_put

                    if self._owner_session_gen != mem_initial_gen:
                        raise _StaleOwnerLifecycle()
                    prev_owner = None
                    if vault is not None:
                        try:
                            raw_prev = vault.get(self.OWNER_SESSION_VAULT_REF)
                            if isinstance(raw_prev, str) and raw_prev.strip():
                                prev_owner = raw_prev.strip()
                        except Exception:
                            prev_owner = None
                    verified_vault_put(vault, session_token, self.OWNER_SESSION_VAULT_REF)
                    try:
                        verified_vault_put(vault, machine_token, self.MACHINE_TOKEN_VAULT_REF)
                    except Exception:
                        _restore_vault_secret(vault, self.OWNER_SESSION_VAULT_REF, prev_owner)
                        raise
                    saved_gen = (self._owner_session_gen or 0) + 1
            except _StaleOwnerLifecycle:
                q_code = _cleanup_issued_pair()
                return self._h5_err(q_code or "AUTH-107")
            except CodedError as exc:
                LOGGER.debug("Credential vault publication failed (%s)", exc.code, exc_info=True)
                q_code = _cleanup_issued_pair()
                return self._h5_err(q_code or exc.code)
            except Exception:
                LOGGER.debug("Credential vault publication failed (APP-903)", exc_info=True)
                q_code = _cleanup_issued_pair()
                return self._h5_err(q_code or "APP-903")

            self._owner_session = session_token
            self._owner_session_expires_at = expires_at
            self._owner_session_gen = saved_gen
            self._machine_token = machine_token
            self._machine_token_gen = saved_gen

        self._clear_quota_cache()
        pending_history = bool(local_history and local_history["plans"]) and (remote_history is None or deferred_history)
        if deferred_history and remote_history is not None:
            try:
                complete_deferred_history(settings, saved_auth, hub_url, local_history, remote_history["store"])
                pending_history = False
            except (PlanError, HubMutationError, KeyError, OSError):
                pass

        with self._owner_session_lock:
            self._sync_gen += 1
            sync_gen = self._sync_gen
            self._sync_status = "syncing"
            self._sync_error_code = None
            self._sync_error_message = None

        threading.Thread(
            target=self._run_background_first_sync,
            kwargs={
                "sync_gen": sync_gen,
                "hub_url": hub_url,
                "machine_token": machine_token,
                "machine_name": clean_comp,
                "db_path": db_path,
            },
            name="burnrate-first-sync",
            daemon=True,
        ).start()

        safe_machines = self._annotate_machines(reg_data.get("machines") or [], machine_id)
        result = {
            "ok": True,
            "account": account_meta,
            "computerName": clean_comp,
            "machines": safe_machines,
            "syncStarted": True,
            "syncState": {
                "status": "syncing",
                "code": None,
                "message": None,
            },
        }
        if pending_history:
            result["warning"] = PLAN_HISTORY_PENDING
        return result

    def sign_in(
        self,
        email: str,
        password: str,
        computer_name: str = "",
    ) -> dict:
        return self._authenticate_and_register(
            endpoint="/api/account/login",
            email=email,
            password=password,
            computer_name=computer_name,
        )

    def create_account(
        self,
        email: str,
        password: str,
        computer_name: str = "",
    ) -> dict:
        return self._authenticate_and_register(
            endpoint="/api/account/register",
            email=email,
            password=password,
            computer_name=computer_name,
        )

    def sign_out(self) -> dict:
        if self._demo:
            return self._h5_err("HUB-501", message="Account actions are disabled in demo mode.")

        from spend_app.connection_secrets import (
            ack_pending_revokes,
            clear_spoke_credentials,
            flush_pending_revokes,
            read_pending_revokes,
            response_is_terminal_revoke,
        )
        from spend_app.sync import _HUB_TEST_CLIENTS, clear_hub_proxy_cache

        db_path = self._db_path()
        vault = self._resolve_vault()

        def _apply_signout(_conn, state):
            if state.get("role") == "spoke":
                state["role"] = "local"
            state["account"] = {}
            spoke = dict(state.get("spokeConfig") or {})
            spoke.pop("lastSyncError", None)
            spoke["enabled"] = False
            state["spokeConfig"] = spoke

        with self._owner_session_lock:
            mem_owner = self._owner_session
            mem_machine = self._machine_token
            pre_hub, pre_err = self._resolve_hub_origin()
            pre_hub_url = pre_hub if pre_err is None else None
            pre_client = _HUB_TEST_CLIENTS.get(pre_hub_url) if pre_hub_url else None
            try:
                cleared = clear_spoke_credentials(
                    db_path,
                    clear_owner_session=True,
                    clear_machine_token=True,
                    mark_signed_out=True,
                    vault=vault,
                    apply_state=_apply_signout,
                    queue_revokes_mode="full_sign_out",
                    hub_url=pre_hub_url,
                    extra_owner_tokens=[mem_owner] if mem_owner else None,
                    extra_machine_tokens=[mem_machine] if mem_machine else None,
                    client=pre_client,
                    allow_flush_on_overflow=True,
                )
            except CodedError as exc:
                return self._h5_err(exc.code)
            except Exception:
                LOGGER.debug("Could not clear local spoke state", exc_info=True)
                return self._h5_err("APP-903")

            if db_path is not None and db_path.is_file():
                clear_hub_proxy_cache()

            self._owner_session = None
            self._owner_session_expires_at = None
            self._owner_session_gen = None
            self._machine_token = None
            self._machine_token_gen = None
            self._sync_gen += 1
            self._sync_status = "idle"
            self._sync_error_code = None
            self._sync_error_message = None

        self._clear_quota_cache()
        owner_tokens = list(cleared.get("ownerTokens") or [])
        local_mid = str(cleared.get("machineId") or "").strip()
        queued_tasks = list(cleared.get("queuedTasks") or [])
        existing_pending = read_pending_revokes(db_path, vault=vault)
        if not queued_tasks and not existing_pending:
            return {"ok": True}

        hub_url, err = self._resolve_hub_origin()
        if err is not None:
            return self._h5_err(str(err.get("code") or "NET-001"))

        test_client = _HUB_TEST_CLIENTS.get(hub_url)
        acked_ids: list[str] = []
        first_err: dict | None = None

        machine_tasks = [
            t for t in queued_tasks if t.get("kind") in {"machine_self_revoke", "machine_revoke_owner"}
        ]
        owner_tasks = [t for t in queued_tasks if t.get("kind") == "owner_logout"]

        machine_revoked = False
        machine_err: dict | None = None
        if local_mid and owner_tokens:
            for tok in owner_tokens:
                _, rev_res, rev_err = self._call_hub(
                    hub_url,
                    "POST",
                    "/api/machines",
                    json_body={"action": "revoke", "id": local_mid},
                    token=tok,
                    fresh_session=True,
                    flush_pending=False,
                )
                if response_is_terminal_revoke("machine_revoke_owner", rev_res, rev_err):
                    machine_revoked = True
                    break
                machine_err = rev_err or self._h5_err("HUB-501")

        if machine_revoked:
            acked_ids.extend(str(t["id"]) for t in machine_tasks if t.get("id"))
        else:
            self_tasks = [t for t in machine_tasks if t.get("kind") == "machine_self_revoke"]
            if self_tasks:
                for task in self_tasks:
                    _, self_res, self_err = self._call_hub(
                        hub_url,
                        "POST",
                        "/api/machines/self/revoke",
                        json_body={},
                        token=str(task.get("token") or ""),
                        fresh_session=True,
                        flush_pending=False,
                    )
                    if response_is_terminal_revoke("machine_self_revoke", self_res, self_err):
                        if task.get("id"):
                            acked_ids.append(str(task["id"]))
                    elif first_err is None:
                        first_err = self_err or self._h5_err("HUB-501")
            elif any(t.get("kind") == "machine_revoke_owner" for t in machine_tasks):
                if first_err is None:
                    first_err = machine_err or self._h5_err("HUB-501")

        for task in owner_tasks:
            _, res, call_err = self._call_hub(
                hub_url,
                "POST",
                "/api/auth/logout",
                json_body={},
                token=str(task.get("token") or ""),
                fresh_session=True,
                flush_pending=False,
            )
            if response_is_terminal_revoke("owner_logout", res, call_err):
                if task.get("id"):
                    acked_ids.append(str(task["id"]))
            elif first_err is None:
                first_err = call_err or self._h5_err("HUB-501")

        if acked_ids:
            try:
                ack_pending_revokes(db_path, acked_ids, vault=vault)
            except CodedError as exc:
                return self._h5_err(exc.code)

        if first_err is not None:
            return self._h5_err_from_call(first_err)

        if read_pending_revokes(db_path, vault=vault):
            try:
                flushed = flush_pending_revokes(
                    db_path,
                    hub_url,
                    vault=vault,
                    client=test_client,
                    ignore_backoff=True,
                )
            except CodedError as exc:
                return self._h5_err(exc.code)
            if not flushed:
                return self._h5_err("NET-001")

        return {"ok": True}

    def change_password(self, current_password: str, new_password: str) -> dict:
        if self._demo:
            return self._h5_err("HUB-501", message="Account actions are disabled in demo mode.")
        if not current_password:
            return self._h5_err("AUTH-101")
        from spend_app.setup_wizard import MIN_PASSWORD_LENGTH

        if len(str(new_password or "")) < MIN_PASSWORD_LENGTH:
            return self._h5_err("AUTH-105")
        hub_url, err = self._resolve_hub_origin()
        if err is not None:
            return self._h5_err(str(err.get("code") or "NET-001"))
        token = self._get_owner_session_token()
        if not token:
            return self._h5_err("AUTH-107")
        _, _, call_err = self._call_hub(
            hub_url,
            "POST",
            "/api/account/password",
            json_body={
                "currentPassword": current_password,
                "newPassword": str(new_password),
            },
            token=token,
        )
        if call_err is not None:
            return self._h5_err_from_call(call_err)
        return {"ok": True}

    def list_computers(self) -> dict:
        if self._demo:
            try:
                res = self._client.request("/api/machines")
                return {
                    "ok": True,
                    "machines": self._annotate_machines(res.get("machines") or [], ""),
                }
            except Exception:
                return {"ok": True, "machines": []}
        hub_url, err = self._resolve_hub_origin()
        if err is not None:
            return self._h5_err(str(err.get("code") or "NET-001"))
        token = self._get_owner_session_token()
        if not token:
            return self._h5_err("AUTH-107")
        data, _, call_err = self._call_hub(
            hub_url,
            "GET",
            "/api/machines",
            token=token,
        )
        if call_err is not None:
            return self._h5_err_from_call(call_err)
        return {
            "ok": True,
            "machines": self._annotate_machines(
                data.get("machines") or [], self._local_machine_id()
            ),
        }

    def remove_computer(self, machine_id: str) -> dict:
        if self._demo:
            return self._h5_err("HUB-501", message="Account actions are disabled in demo mode.")
        clean_id = str(machine_id or "").strip()
        if not clean_id:
            return self._h5_err("DEV-302", message="Choose a computer to remove.")
        local_mid = self._local_machine_id()
        if clean_id in {"local", "all"} or (local_mid and clean_id == local_mid):
            return self._h5_err("DEV-302", message="Use Sign out to disconnect this computer.")
        hub_url, err = self._resolve_hub_origin()
        if err is not None:
            return self._h5_err(str(err.get("code") or "NET-001"))
        token = self._get_owner_session_token()
        if not token:
            return self._h5_err("AUTH-107")
        data, _, call_err = self._call_hub(
            hub_url,
            "POST",
            "/api/machines",
            json_body={"action": "revoke", "id": clean_id},
            token=token,
        )
        if call_err is not None:
            return self._h5_err_from_call(call_err)
        return {
            "ok": True,
            "machines": self._annotate_machines(data.get("machines") or [], local_mid),
        }

    def _owner_session_status(self) -> dict:
        if self._demo:
            return {"ok": True, "hasOwnerSession": False, "expiresAt": None}
        token, expires_at, captured_gen = self._capture_owner_session_with_gen()
        if not token:
            return {"ok": True, "hasOwnerSession": False, "expiresAt": None}
        if expires_at:
            from spend_app.auth_model import moment, parse

            parsed = parse(expires_at)
            if parsed is not None and parsed <= moment():
                self._clear_owner_session(expected_token=token, expected_gen=captured_gen)
                return {"ok": True, "hasOwnerSession": False, "expiresAt": None}
        return {"ok": True, "hasOwnerSession": True, "expiresAt": expires_at}

    def minimize_window(self) -> None:
        if self._window is not None:
            self._window.minimize()

    def show_tab(self) -> dict:
        """Shrink to the tab: hide this window and show the always-on-top tab."""
        if self._surfaces is None:
            LOGGER.warning("Shrink to tab was asked for but no tab is attached; minimizing instead")
            self.minimize_window()
            return {"ok": False, "fallback": "minimize"}
        LOGGER.info("Shrink to tab requested")
        self._surfaces.show_tab()
        return {"ok": True}

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

    # -- H11 settings ------------------------------------------------------

    def _ensure_ready(self) -> None:
        if self._demo or self._service_ready:
            return
        with self._lock:
            if not self._service_ready:
                self._client.ensure_service()
                self._service_ready = True

    @staticmethod
    def _h11_error(exc: BaseException, fallback_code: str = "APP-901") -> dict:
        code = getattr(exc, "code", None) or fallback_code
        msg = str(exc) if isinstance(exc, DesktopError) and str(exc) else format_error(code)
        return {"ok": False, "code": code, "error": msg}

    def get_setup_scan(self) -> dict:
        try:
            self._ensure_ready()
            return build_setup_scan_data(self._client, demo=self._demo)
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def run_setup_scan(self) -> dict:
        if self._demo:
            data = build_setup_scan_data(self._client, demo=True)
            data["started"] = data.get("checkedAt")
            return data
        try:
            self._ensure_ready()
            scan_res = self._client.request("/api/setup/scan", {})
            data = build_setup_scan_data(self._client, demo=False)
            if isinstance(scan_res, dict):
                data["started"] = scan_res.get("started")
                data["harnesses"] = scan_res.get("harnesses") or []
            return data
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def get_setup_progress(self) -> dict:
        if self._demo:
            progress = build_setup_scan_data(self._client, demo=True).get("progress") or {}
            return {
                "ok": True,
                "heartbeat": progress.get("heartbeat") or {},
                "sources": progress.get("sources") or [],
            }
        try:
            self._ensure_ready()
            res = self._client.request("/api/setup/progress")
            return {
                "ok": True,
                "heartbeat": res.get("heartbeat") or {},
                "sources": res.get("sources") or [],
            }
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def set_setup_consent(self, key: str, enabled: bool) -> dict:
        if self._demo:
            return {"ok": False, "error": "Settings changes are disabled in demo mode."}
        clean_key = str(key or "").strip()
        if not clean_key:
            return {"ok": False, "error": "Choose a setting to change."}
        try:
            self._ensure_ready()
            res = self._client.request(
                "/api/setup/consent", {"switches": {clean_key: bool(enabled)}}
            )
            return {
                "ok": True,
                "consent": res.get("consent") or [],
                "warning": res.get("warning"),
            }
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def complete_onboarding(self, computer_name: str = "") -> dict:
        if self._demo:
            return {"ok": True, "setup": {"completed": True}}
        body: dict = {}
        clean_name = str(computer_name or "").strip()
        if clean_name:
            body["computerName"] = clean_name
        try:
            self._ensure_ready()
            res = self._client.request("/api/setup/complete", body)
            return {"ok": True, "setup": res.get("setup") or {}}
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def defer_onboarding(self) -> dict:
        if self._demo:
            return {"ok": True, "setup": {}}
        try:
            self._ensure_ready()
            res = self._client.request("/api/setup/defer", {})
            return {"ok": True, "setup": res.get("setup") or {}}
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def get_notification_settings(self) -> dict:
        if self._demo:
            from spend_app.notify import KINDS

            return {
                "ok": True,
                "available": False,
                "kinds": [{"key": k, "label": lbl, "on": True} for k, lbl in KINDS],
            }
        try:
            self._ensure_ready()
            res = self._client.request("/api/setup/notifications")
            return {
                "ok": True,
                "available": bool(res.get("available")),
                "kinds": res.get("kinds") or [],
            }
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def set_notification_kind(self, key: str, enabled: bool) -> dict:
        if self._demo:
            return {"ok": False, "error": "Notification changes are disabled in demo mode."}
        clean_key = str(key or "").strip()
        if not clean_key:
            return {"ok": False, "error": "Choose a notification to change."}
        try:
            self._ensure_ready()
            res = self._client.request(
                "/api/setup/notifications", {"kinds": {clean_key: bool(enabled)}}
            )
            self._service_alerts = None
            kinds = res.get("kinds") or []
            if clean_key in {"threshold", "dry"}:
                plan_on = any(
                    bool(item.get("on"))
                    for item in kinds
                    if isinstance(item, dict) and item.get("key") in {"threshold", "dry"}
                )
                self._low_quota_alerts = plan_on
                self._write_preferences()
            return {
                "ok": True,
                "available": bool(res.get("available")),
                "kinds": kinds,
                "lowQuotaAlerts": self._low_quota_alerts,
            }
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def send_test_notification(self) -> dict:
        if self._demo:
            return {"ok": False, "error": "Test notifications are disabled in demo mode."}
        try:
            self._ensure_ready()
            self._client.request("/api/setup/notifications/test", {})
            return {"ok": True}
        except DesktopError as exc:
            if exc.status_code == 502 and getattr(exc, "code", None) == "HUB-501":
                return {
                    "ok": False,
                    "error": "Windows did not show the test notification. Check Windows notification settings for BURNRATE.",
                }
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def get_diagnostics_report(self) -> dict:
        if self._demo:
            return {"ok": False, "error": "Diagnostics are disabled in demo mode."}
        try:
            self._ensure_ready()
            res = self._client.request("/api/setup/diagnostics", {})
            return {"ok": True, "text": str(res.get("text") or "")}
        except DesktopError as exc:
            return self._h11_error(exc)
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "APP-901", "error": format_error("APP-901")}

    def trigger_sync(self) -> dict:
        if self._demo:
            return {"ok": False, "error": "Sync is disabled in demo mode."}
        try:
            self._ensure_ready()
            res = self._client.request("/api/sync/trigger", {})
            sync_res = res.get("syncResult") if isinstance(res.get("syncResult"), dict) else None
            sync_failed = res.get("ok") is False or (
                sync_res is not None and sync_res.get("ok") is False
            )
            if sync_failed:
                raw_err = str((sync_res or {}).get("error") or res.get("error") or "sync_failed")
                code = (sync_res or {}).get("code") or (
                    "NET-001"
                    if raw_err == "hub_unreachable"
                    else "AUTH-101"
                    if raw_err == "hub_rejected"
                    else "SYNC-201"
                )
                return {
                    "ok": False,
                    "code": code,
                    "error": format_error(code),
                    "rawError": raw_err,
                    "triggeredAt": res.get("triggeredAt"),
                    "setup": res.get("setup") or {},
                    "machines": res.get("machines") or [],
                    "syncResult": sync_res,
                }
            with self._owner_session_lock:
                self._sync_gen += 1
                self._sync_status = "ok"
                self._sync_error_code = None
                self._sync_error_message = None
            return {
                "ok": True,
                "triggeredAt": res.get("triggeredAt"),
                "setup": res.get("setup") or {},
                "machines": res.get("machines") or [],
                "syncResult": sync_res,
            }
        except DesktopError as exc:
            return self._h11_error(exc, fallback_code="SYNC-201")
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "code": "SYNC-201", "error": format_error("SYNC-201")}


    # H10 ops/hud

    def set_hud_mini(self, enabled: bool) -> dict:
        """Retired with the in-page mini bar; kept so a stale page never throws."""
        return {"ok": True, "hudMini": False}

    def set_hud_density(self, density: str) -> dict:
        val = str(density or "").strip().lower()
        self._hud_density = val if val in {"compact", "comfortable"} else "compact"
        self._write_preferences()
        return {"ok": True, "hudDensity": self._hud_density}

    def copy_image_to_clipboard(self, data_url: str) -> dict:
        if not isinstance(data_url, str) or not data_url.startswith("data:image/png;base64,"):
            return {"ok": False, "error": "Expected a PNG data URL."}
        b64 = data_url.split(",", 1)[1].strip()
        if not b64 or len(b64) > 16_000_000:
            return {"ok": False, "error": "Image payload size out of bounds."}
        try:
            png_bytes = base64.b64decode(b64, validate=True)
            dib_bytes = _png_to_cf_dib(png_bytes)
            _write_win32_clipboard(png_bytes, dib_bytes)
        except Exception as exc:  # noqa: BLE001 -- surfaced cleanly to UI
            LOGGER.debug("Copy image to clipboard failed", exc_info=True)
            return {"ok": False, "error": str(exc) or "Could not copy image to clipboard."}
        return {"ok": True}

    def get_diagnostics_data(self) -> dict:
        if self._demo:
            return {
                "ok": True,
                "health": _demo_health(),
                "freshness": _demo_freshness(),
                "pendingRevokeCount": 0,
            }
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                health = self._safe_request("/api/spend/health")
                freshness = self._safe_request("/api/spend/freshness")
            if health is None and freshness is None:
                return {"ok": False, "error": "Diagnostics unavailable."}
            out = {"ok": True, "health": health, "freshness": freshness}
            db_path = self._db_path()
            from spend_app.connection_secrets import (
                has_revoke_retention_warning,
                pending_revoke_count,
            )

            if db_path is not None or self._vault is not None:
                out["pendingRevokeCount"] = pending_revoke_count(
                    db_path, vault=self._resolve_vault()
                )
            if has_revoke_retention_warning():
                out["pendingRevokeRetentionWarning"] = True
            return out
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load diagnostics."}

    def get_week_share_data(self, machine: str = "all") -> dict:
        if self._demo:
            return {"ok": True, "week": _demo_week_share()}
        use_machine = machine or "all"
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                data = self._client.request(
                    "/api/spend/share/week?" + urlencode({"machine": use_machine})
                )
            return {"ok": True, "week": data}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not build weekly share card."}

    def get_setup_check(self) -> dict:
        if self._demo:
            return {"ok": True, "check": {"plans": {"active": [], "suggestions": []}}}
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                data = self._safe_request("/api/setup/check")
            if not isinstance(data, dict):
                return {"ok": False, "error": "Setup check unavailable."}
            return {"ok": True, "check": data}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not check suggested plans."}

    def _request_plan_settings(self, path: str, body: dict | None = None) -> dict:
        if self._demo:
            return {"ok": False, "error": "Plan settings are unavailable in sample data."}
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                response = self._client.request(path) if body is None else self._client.request(path, body)
            if body is None and (
                not isinstance(response.get("plans"), list)
                or not isinstance(response.get("tools"), (list, tuple))
                or not isinstance(response.get("cadences"), (list, tuple))
                or not response.get("today")
                or not response.get("authority")
            ):
                return {"ok": False, "error": "Could not read the plan settings from this service."}
            return {"ok": True, "result": response}
        except DesktopError as exc:
            if exc.status_code == 409:
                return {"ok": False, "error": "These plans changed. Reload plans before trying again."}
            if body is None and exc.status_code == 503:
                return {"ok": False, "error": "Connect to your hub and sign in to load plans."}
            if body is not None and exc.status_code == 422:
                return {"ok": False, "error": "Check the plan price and dates. Plan terms must have valid dates and cannot overlap."}
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not load or save plans."}

    def get_plan_settings(self) -> dict:
        return self._request_plan_settings("/api/subscriptions?manage=true")

    def change_plan(self, authority: str, mutation: dict) -> dict:
        if not isinstance(authority, str) or not re.fullmatch(r"local|hub-[0-9a-f]{64}", authority):
            return {"ok": False, "error": "Reload plans before saving."}
        if not isinstance(mutation, dict) or mutation.get("operation") not in ("add", "schedule", "end", "correct"):
            return {"ok": False, "error": "Invalid plan change."}
        return self._request_plan_settings("/api/subscriptions?" + urlencode({"authority": authority}), mutation)

    def confirm_suggested_plan(self, suggestion: dict) -> dict:
        if self._demo:
            return {"ok": False, "error": "Plan changes are disabled in demo mode."}
        if not isinstance(suggestion, dict):
            return {"ok": False, "error": "Invalid plan suggestion."}
        name = str(suggestion.get("name") or "").strip()
        tool_key = str(suggestion.get("tool_key") or "").strip()
        cadence = str(suggestion.get("cadence") or "monthly").strip() or "monthly"
        try:
            amount = float(suggestion.get("amount_usd"))
        except (TypeError, ValueError):
            amount = 0.0
        if not name or not tool_key or not math.isfinite(amount) or amount <= 0:
            return {"ok": False, "error": "Invalid plan amount or provider."}
        now_utc = datetime.now(UTC)
        month_start = f"{now_utc.year:04d}-{now_utc.month:02d}-01"
        body = {
            "operation": "add",
            "request_id": f"desktop-hud-plan-{uuid4()}",
            "values": {
                "name": name,
                "tool_key": tool_key,
                "amount_usd": f"{amount:.2f}",
                "cadence": cadence,
                "start_date": month_start,
                "end_date": None,
            },
        }
        try:
            with self._lock:
                if not self._service_ready:
                    self._client.ensure_service()
                    self._service_ready = True
                res = self._client.request("/api/subscriptions", body)
            return {"ok": True, "result": res}
        except DesktopError as exc:
            return {"ok": False, "error": str(exc)}
        except (OSError, RuntimeError, ValueError, KeyError, TypeError):
            return {"ok": False, "error": "Could not save plan."}


# H10 ops/hud helpers


def _paeth_predictor(a: int, b: int, c: int) -> int:
    p = a + b - c
    pa = abs(p - a)
    pb = abs(p - b)
    pc = abs(p - c)
    if pa <= pb and pa <= pc:
        return a
    if pb <= pc:
        return b
    return c


def _png_to_cf_dib(png_bytes: bytes) -> bytes:
    """Decode an 8-bit RGB/RGBA PNG into Windows CF_DIB (32-bit BGRA)."""
    if len(png_bytes) < 24 or png_bytes[:8] != b"\x89PNG\r\n\x1a\n":
        raise ValueError("Invalid PNG signature.")
    pos = 8
    width = height = bit_depth = color_type = 0
    saw_ihdr = False
    idat_parts: list[bytes] = []
    while pos + 8 <= len(png_bytes):
        length, ctype = struct.unpack(">I4s", png_bytes[pos : pos + 8])
        pos += 8
        if pos + length + 4 > len(png_bytes):
            raise ValueError("Truncated PNG chunk.")
        cdata = png_bytes[pos : pos + length]
        pos += length + 4  # skip CRC
        if ctype == b"IHDR":
            if length != 13:
                raise ValueError("Invalid IHDR chunk.")
            width, height, bit_depth, color_type, comp, filt, interlace = struct.unpack(
                ">IIBBBBB", cdata
            )
            if width <= 0 or height <= 0 or bit_depth != 8 or color_type not in (2, 6):
                raise ValueError("Unsupported PNG format.")
            if comp != 0 or filt != 0 or interlace != 0:
                raise ValueError("Unsupported PNG compression or interlacing.")
            saw_ihdr = True
        elif ctype == b"IDAT":
            idat_parts.append(cdata)
        elif ctype == b"IEND":
            break
    if not saw_ihdr or not idat_parts:
        raise ValueError("Missing PNG image data.")

    raw = zlib.decompress(b"".join(idat_parts))
    bpp = 4 if color_type == 6 else 3
    stride = width * bpp
    expected = height * (1 + stride)
    if len(raw) != expected:
        raise ValueError("Corrupt PNG scanline length.")

    rows: list[bytes] = []
    prev = bytes(stride)
    idx = 0
    for _ in range(height):
        ftype = raw[idx]
        idx += 1
        scan = bytearray(raw[idx : idx + stride])
        idx += stride
        if ftype == 0:
            pass
        elif ftype == 1:
            for i in range(bpp, stride):
                scan[i] = (scan[i] + scan[i - bpp]) & 0xFF
        elif ftype == 2:
            for i in range(stride):
                scan[i] = (scan[i] + prev[i]) & 0xFF
        elif ftype == 3:
            for i in range(stride):
                left = scan[i - bpp] if i >= bpp else 0
                scan[i] = (scan[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ftype == 4:
            for i in range(stride):
                left = scan[i - bpp] if i >= bpp else 0
                up = prev[i]
                up_left = prev[i - bpp] if i >= bpp else 0
                scan[i] = (scan[i] + _paeth_predictor(left, up, up_left)) & 0xFF
        else:
            raise ValueError(f"Unsupported PNG filter type {ftype}.")
        row_bytes = bytes(scan)
        rows.append(row_bytes)
        prev = row_bytes

    pixels = bytearray(width * height * 4)
    out_pos = 0
    for y in range(height - 1, -1, -1):
        row = rows[y]
        if bpp == 4:
            for x in range(0, stride, 4):
                pixels[out_pos] = row[x + 2]
                pixels[out_pos + 1] = row[x + 1]
                pixels[out_pos + 2] = row[x]
                pixels[out_pos + 3] = row[x + 3]
                out_pos += 4
        else:
            for x in range(0, stride, 3):
                pixels[out_pos] = row[x + 2]
                pixels[out_pos + 1] = row[x + 1]
                pixels[out_pos + 2] = row[x]
                pixels[out_pos + 3] = 255
                out_pos += 4

    header = struct.pack(
        "<IiiHHIIiiII",
        40,
        width,
        height,
        1,
        32,
        0,
        len(pixels),
        0,
        0,
        0,
        0,
    )
    return header + bytes(pixels)


def _alloc_hglobal(kernel32, data: bytes) -> int:
    import ctypes

    hglobal = kernel32.GlobalAlloc(0x0042, len(data))  # GMEM_MOVEABLE | GMEM_ZEROINIT
    if not hglobal:
        raise OSError("GlobalAlloc failed for clipboard image.")
    ptr = kernel32.GlobalLock(hglobal)
    if not ptr:
        kernel32.GlobalFree(hglobal)
        raise OSError("GlobalLock failed for clipboard image.")
    try:
        ctypes.memmove(ptr, data, len(data))
    finally:
        kernel32.GlobalUnlock(hglobal)
    return hglobal


def _write_win32_clipboard(png_bytes: bytes, dib_bytes: bytes) -> None:
    if sys.platform != "win32":
        raise RuntimeError("Clipboard image copy is only supported on Windows.")
    import ctypes

    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32

    kernel32.GlobalAlloc.argtypes = [ctypes.c_uint, ctypes.c_size_t]
    kernel32.GlobalAlloc.restype = ctypes.c_void_p
    kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalLock.restype = ctypes.c_void_p
    kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    kernel32.GlobalUnlock.restype = ctypes.c_int
    kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
    kernel32.GlobalFree.restype = ctypes.c_void_p

    user32.OpenClipboard.argtypes = [ctypes.c_void_p]
    user32.OpenClipboard.restype = ctypes.c_int
    user32.CloseClipboard.argtypes = []
    user32.CloseClipboard.restype = ctypes.c_int
    user32.EmptyClipboard.argtypes = []
    user32.EmptyClipboard.restype = ctypes.c_int
    user32.SetClipboardData.argtypes = [ctypes.c_uint, ctypes.c_void_p]
    user32.SetClipboardData.restype = ctypes.c_void_p
    user32.RegisterClipboardFormatW.argtypes = [ctypes.c_wchar_p]
    user32.RegisterClipboardFormatW.restype = ctypes.c_uint

    opened = False
    for _ in range(5):
        if user32.OpenClipboard(None):
            opened = True
            break
        time.sleep(0.01)
    if not opened:
        raise OSError("Could not open Windows clipboard.")
    try:
        user32.EmptyClipboard()
        h_dib = _alloc_hglobal(kernel32, dib_bytes)
        if not user32.SetClipboardData(8, h_dib):  # CF_DIB
            kernel32.GlobalFree(h_dib)
            raise OSError("SetClipboardData(CF_DIB) failed.")
        png_fmt = user32.RegisterClipboardFormatW("PNG")
        if png_fmt:
            h_png = _alloc_hglobal(kernel32, png_bytes)
            if not user32.SetClipboardData(png_fmt, h_png):
                kernel32.GlobalFree(h_png)
    finally:
        user32.CloseClipboard()


def _demo_activity() -> list[dict]:
    now = datetime.now(UTC)
    return [
        {
            "id": "demo-claude-1",
            "name": "Refactor HUD bridge and clipboard export",
            "state": "LIVE",
            "modelKey": "claude-opus-4-1",
            "machineId": "local",
            "machineName": "Desktop",
            "startedAt": (now - timedelta(minutes=34)).isoformat().replace("+00:00", "Z"),
        },
        {
            "id": "demo-codex-2",
            "name": "Run integration verification suite",
            "state": "LIVE",
            "modelKey": "gpt-5-codex",
            "machineId": "example-laptop",
            "machineName": "Laptop",
            "startedAt": (now - timedelta(minutes=14)).isoformat().replace("+00:00", "Z"),
        },
        {
            "id": "demo-gemini-3",
            "name": "Audit diagnostics pricing coverage",
            "state": "LIVE",
            "modelKey": "gemini-2.5-pro",
            "machineId": "local",
            "machineName": "Desktop",
            "startedAt": (now - timedelta(minutes=5)).isoformat().replace("+00:00", "Z"),
        },
    ]


def _demo_health() -> dict:
    now = datetime.now(UTC)
    stamp = now.isoformat().replace("+00:00", "Z")
    return {
        "generatedAt": stamp,
        "ingest": [
            {
                "source": "claude_local",
                "status": "success",
                "eventsLast24h": 142,
                "lastSuccess": stamp,
            },
            {
                "source": "codex_local",
                "status": "success",
                "eventsLast24h": 98,
                "lastSuccess": stamp,
            },
        ],
        "quotas": [
            {
                "providerKey": "claude-code",
                "status": "ok",
                "lastPoll": stamp,
            },
            {
                "providerKey": "codex",
                "status": "ok",
                "lastPoll": stamp,
            },
        ],
        "sources": [],
        "pricingGaps": [],
        "providerVsComputedVariancePct": 1.4,
    }


def _demo_freshness() -> dict:
    now = datetime.now(UTC)
    stamp = now.isoformat().replace("+00:00", "Z")
    return {
        "generatedAt": stamp,
        "version": __version__,
        "status": "ok",
        "issues": [],
        "ingest": {"lastPassSeconds": 1.2, "lastPassAgeSeconds": 12},
        "machines": [
            {"key": "Desktop", "state": "online", "ageSeconds": 12, "lastSeenAt": stamp},
            {"key": "Laptop", "state": "online", "ageSeconds": 45, "lastSeenAt": stamp},
        ],
    }


def _demo_week_share() -> dict:
    now = datetime.now(UTC)
    start = now - timedelta(days=7)
    return {
        "from": start.isoformat().replace("+00:00", "Z"),
        "to": now.isoformat().replace("+00:00", "Z"),
        "days": 7,
        "usd": 127.34,
        "complete": True,
        "planUsd": 60.0,
        "tokens": 8_200_000,
        "sessions": 50,
        "cacheReusePct": 68.0,
        "tools": [
            {"name": "Claude Code", "usd": 52.10, "sharePct": 40.9},
            {"name": "Codex", "usd": 41.82, "sharePct": 32.8},
            {"name": "OpenCode", "usd": 18.60, "sharePct": 14.6},
            {"name": "Muse Code", "usd": 9.72, "sharePct": 7.6},
        ],
    }

def _find_icon() -> Path | None:
    resource_root = Path(__file__).resolve().parents[1]
    for candidate in (
        resource_root / "burnrate.ico",
        resource_root / "burnrate-assets" / "burnrate.ico",
        resource_root / "assets" / "icons" / "icon.ico",
    ):
        if candidate.is_file():
            return candidate
    return None


def _focus_existing_window(title: str) -> bool:
    """Bring a running BURNRATE window forward, un-hiding it from the tray.

    The running process is asked to open its full window through its own
    surface controller (it remembers which window is up); only a running build
    without that listener falls back to poking the windows directly.
    """
    if sys.platform != "win32":
        return False
    if signal_running_instance():
        return True
    try:
        import ctypes

        user32 = ctypes.windll.user32
        hwnd = user32.FindWindowW(None, title)
        if hwnd:
            cls = ctypes.create_unicode_buffer(128)
            user32.GetClassNameW(hwnd, cls, len(cls))
            if cls.value == "Windows.Internal.Shell.TabProxyWindow":
                return False
            tab_hwnd = user32.FindWindowW(None, TAB_WINDOW_TITLE)
            if tab_hwnd:
                user32.GetClassNameW(tab_hwnd, cls, len(cls))
                if cls.value != "Windows.Internal.Shell.TabProxyWindow":
                    user32.ShowWindow(tab_hwnd, 0)  # SW_HIDE: the full window takes over
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
        #: Set by ``run_webview`` once the tab window exists.
        self.surfaces: SurfaceController | None = None

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

    def open_full(self) -> None:
        """Bring up the full window, putting the tab away."""
        if self.surfaces is not None:
            self.surfaces.show_full()
        else:
            self.show()

    def show_tab(self) -> None:
        if self.surfaces is not None:
            self.surfaces.show_tab()

    def toggle(self) -> None:
        if self.surfaces is not None:
            self.surfaces.toggle()
        elif self.is_visible():
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
        self._shutdown_surfaces()
        try:
            self.window.destroy()
        except Exception:
            LOGGER.debug("Destroy skipped", exc_info=True)

    def _shutdown_surfaces(self) -> None:
        """Drop queued transitions and destroy the tab (idempotent). Every way of
        closing the full window ends here, so the tab can never be orphaned."""
        if self.surfaces is not None:
            self.surfaces.shutdown()

    def _stop_workers(self) -> None:
        self.tracker.disable()
        if self.surfaces is not None:
            self.surfaces.tab.tracker.flush()
            self.surfaces.tab.tracker.disable()
        self.opacity.stop()
        if self.watcher is not None:
            self.watcher.stop()
        if self.updater is not None:
            self.updater.stop()

    def shutdown(self) -> None:
        """Idempotent final cleanup, also reached when the window died early."""
        self.quitting = True
        self._stop_workers()
        self._shutdown_surfaces()
        if self.tray is not None:
            self.tray.dispose()

    # -- page <-> host -----------------------------------------------------

    def refresh_page(self) -> None:
        self.bridge.call_page("refresh")

    def open_settings(self, tab: str | None = None) -> None:
        self.open_full()
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
        if self._plan is None:
            try:
                native_geometry.apply_default_size(native)
            except Exception:
                LOGGER.debug("Could not set the default window size", exc_info=True)
        if self._tray_enabled:
            self._create_tray(native)
        self._install_form_hooks(native)

    def _create_tray(self, native) -> None:
        tooltip = "BURNRATE" + (" · Sample data" if self.bridge.demo else "")
        tray = TrayController(
            native,
            self._icon_path,
            on_toggle=self.toggle,
            on_show=self.open_full,
            on_tab=self.show_tab,
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
            self._shutdown_surfaces()  # the tab goes with the window

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


DESKTOP_LOG_NAME = "desktop.log"
_DESKTOP_LOG_MARK = "_burnrate_desktop_log"


def configure_desktop_logging(logs_dir: Path | None = None) -> Path | None:
    """Write the desktop app's own log (warnings, window transitions) to
    ``desktop.log`` next to the service's ``dashboard.log``, redacted like it.

    pythonw has no console, so without this a failure in the window code
    leaves no trace. Idempotent; returns the log path (``None`` if unwritable).
    """
    import logging.handlers

    from spend_app.config import default_logs_path
    from spend_app.redact import RedactingFormatter

    try:
        directory = Path(logs_dir) if logs_dir is not None else default_logs_path()
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / DESKTOP_LOG_NAME
        logger = logging.getLogger("spend_app")
        for handler in list(logger.handlers):
            if getattr(handler, _DESKTOP_LOG_MARK, False):
                logger.removeHandler(handler)
                handler.close()
        handler = logging.handlers.RotatingFileHandler(
            path, maxBytes=512_000, backupCount=3, encoding="utf-8"
        )
        setattr(handler, _DESKTOP_LOG_MARK, True)
        handler.setFormatter(
            RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
        )
        logger.addHandler(handler)
        logger.setLevel(logging.INFO)
        webview_logger = logging.getLogger("pywebview")
        if not any(getattr(h, _DESKTOP_LOG_MARK, False) for h in webview_logger.handlers):
            webview_handler = logging.handlers.RotatingFileHandler(
                path, maxBytes=512_000, backupCount=3, encoding="utf-8"
            )
            setattr(webview_handler, _DESKTOP_LOG_MARK, True)
            webview_handler.setFormatter(
                RedactingFormatter("%(asctime)s %(levelname)s %(name)s %(message)s")
            )
            webview_handler.setLevel(logging.WARNING)
            webview_logger.addHandler(webview_handler)
        return path
    except OSError:
        return None


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
    if not demo and not smoke_test:
        configure_desktop_logging()

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
        hidden=bridge.surface == "tab" and not smoke_test,
        background_color=background_for(resolve_dark(bridge.theme)),
    )
    # The tab: a second frameless, always-on-top window, created hidden and
    # shown only by "Shrink to tab" (or a launch that remembers the tab).
    tab_bridge = TabBridge(bridge, bridge._tab_model)
    tab_window_obj = webview.create_window(
        TAB_WINDOW_TITLE,
        html=load_tab_html(bridge.theme),
        js_api=tab_bridge,
        width=360,
        height=TAB_HEIGHT,
        min_size=(120, 40),
        resizable=False,
        frameless=True,
        easy_drag=False,
        on_top=True,
        hidden=True,
        # No DWM frame: the tab cuts its own rounded shape with a window region.
        shadow=False,
        background_color=TAB_DARK_BACKGROUND
        if resolve_dark(bridge.theme)
        else TAB_LIGHT_BACKGROUND,
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
    tab_host = TabHost(
        tab_window_obj,
        store=store,
        icon_path=_find_icon(),
        persist=persist,
    )
    surfaces = SurfaceController(host, tab_host, bridge, store)
    # The tab fades with the same Auto-fade rules as the full window, driven by the
    # one controller: it is "hovered" while the cursor is over the strip or its
    # card, and stays opaque while a card is open or the keyboard focus is in it.
    fade_target = opacity_ctl.add_target(
        FadeTarget(
            lambda: tab_host.hwnd,
            hit_test=tab_host.contains_screen_point,
            hold=tab_host.holds_opaque,
            leave_delay=TAB_FADE_DELAY,
            follows_ui_busy=False,
        )
    )
    tab_host.on_shown = lambda: opacity_ctl.wake(2.5, fade_target)
    host.surfaces = surfaces
    bridge.attach_surfaces(surfaces)
    tab_bridge.attach_controller(surfaces)
    saved_tab = store.read().get(TAB_PREFERENCE_KEY) if persist else None

    def push_tab() -> None:
        payload = tab_bridge.build_payload()
        tab_host.set_base(payload["layout"]["width"], revision=payload["revision"])
        tab_host.push(payload)

    bridge._tab_model.subscribe(push_tab)
    tab_host.on_reflow = push_tab  # a new monitor or DPI means a new width budget
    # A second launch asks this process (not the raw windows) to open the full one.
    def on_second_launch() -> None:
        LOGGER.info("A second launch asked this process to open the full window")
        surfaces.show_full()

    activation = ActivationListener(on_second_launch)
    if not demo and not smoke_test:
        activation.start()
    result = [0]

    def on_shown():
        host.on_shown()
        if not smoke_test:
            opacity_ctl.start()
            # Off the event thread: showing the tab waits for its own page.
            threading.Thread(
                target=surfaces.restore_on_launch, name="burnrate-surface", daemon=True
            ).start()

    def on_tab_before_show():
        tab_host.on_before_show(
            on_close_request=surfaces.close_tab, dark=resolve_dark(bridge.theme)
        )
        tab_host.restore_position(saved_tab)

    def on_tab_moved(*_position):
        tab_host.tracker.notify_changed()
        tab_host.check_budget()
        tab_host.note_moved()

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
                    # The second window must exist and have loaded its page.
                    ok = len(webview.windows) == 2 and bool(
                        tab_window_obj.evaluate_js("!!document.getElementById('tab')")
                    )
                except (OSError, AttributeError, RuntimeError, ValueError):
                    ok = False
            if ok:
                try:
                    window.resize(*native_geometry.MIN_SIZE)
                    time.sleep(0.15)
                    count_after = window.evaluate_js(
                        "document.querySelectorAll('#providerList .provider-item').length"
                    )
                    ok = isinstance(count_after, int) and count_after >= 8
                except (OSError, AttributeError, RuntimeError, ValueError):
                    ok = False
            result[0] = 0 if ok else 1
            host.quitting = True  # a direct destroy must never be cancelled
            tab_host.destroy()
            window.destroy()

        threading.Thread(target=verify, daemon=True).start()

    window.events.before_show += host.on_before_show
    tab_window_obj.events.before_show += on_tab_before_show
    tab_window_obj.events.moved += on_tab_moved
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
        activation.stop()
        host.shutdown()
        _remove_tree(storage_dir)
    return result[0]
