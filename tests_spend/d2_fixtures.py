"""Contract fixtures for the D2 full window: the demo state the canvas shows, as the service sends it.

``pace``, ``urgency``, ``profile`` and ``fundsRunwayInfo`` are the fields the hub-side contract adds to
``merge_provider_entries``. The page must also cope when they are missing (an older service).
"""

from __future__ import annotations

import copy
from datetime import UTC, datetime, timedelta

from tests_spend.test_wealthfront_ui import demo_report


def _iso(hours: float) -> str:
    return (datetime.now(UTC) + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def _window(key, label, left, reset_h, *, pace=None, unit="pct", **extra):
    win = {
        "limitKey": key,
        "shortLabel": key,
        "label": label,
        "reason": "",
        "unit": unit,
        "usedPct": None if left is None else 100.0 - left,
        "remainingPct": left,
        "used": None,
        "allowance": None,
        "remainingCredits": None,
        "remainingUsd": None,
        "resetsAt": None if reset_h is None else _iso(reset_h),
        "unavailable": False,
        "stale": False,
        "windowTokens": None,
        "forecast": "",
        "runway": "",
        "pace": pace,
    }
    win.update(extra)
    return win


def _pace(state, hours, before_reset):
    return {
        "state": state,
        "hoursToDry": hours,
        "hoursDryBeforeReset": before_reset,
        "runsDryAt": None if hours is None else _iso(hours),
    }


def _provider(key, name, plan, windows, *, level, text, profile=None, badge="", **extra):
    first = next((w for w in windows if w.get("remainingPct") is not None), None)
    prov = {
        "key": key,
        "name": name,
        "badge": badge,
        "plan": plan,
        "inUse": False,
        "isPayg": False,
        "fundsRemainingUsd": None,
        "organization": False,
        "remaining": {
            "kind": "quota",
            "headline": f"{int(first['remainingPct'])}% left" if first else "—",
            "sub": first["shortLabel"] if first else "",
            "barPct": first["remainingPct"] if first else 0,
            "tone": "accent",
            "resetsAt": first["resetsAt"] if first else None,
            "summary": plan,
        },
        "burn": {"dayTokens": 2900000, "sessionTokens": 640000, "hasSession": True, "weekTokens": 9400000, "weekBasis": "window"},
        "windows": windows,
        "tokens": 2900000,
        "valueUsd": 52.1,
        "complete": True,
        "input": 1160000,
        "cached": 1450000,
        "writes": 0,
        "output": 290000,
        "reasoning": None,
        "models": [
            {"key": f"{key}:sample", "name": "Example model", "tokens": 2900000, "valueUsd": 52.1, "complete": True}
        ],
        "profile": profile,
        "fundsRunwayInfo": None,
        "urgency": {"level": level, "limitKey": None, "text": text},
    }
    prov.update(extra)
    return prov


def contract_providers() -> list[dict]:
    """The five providers of the canvas, as the service sends them with the contract fields."""
    claude = _provider(
        "claude-code",
        "Claude Code",
        "Claude Max",
        [
            _window("5h", "5-hour window", 54.0, 2.23, pace=_pace("tight", 1.17, 1.06)),
            _window("weekly", "Weekly window", 39.0, 76.0, pace=_pace("dry", 27.0, 49.0)),
        ],
        level="soon",
        text="dry in ~1h",
        inUse=True,
    )
    codex = _provider(
        "codex",
        "Codex",
        "ChatGPT Pro",
        [_window("weekly", "Weekly", 62.0, 100.0, pace=_pace("dry", 14.0, 86.0))],
        level="warn",
        text="dry in ~14h",
    )
    go = _provider(
        "opencode-go",
        "OpenCode Go",
        "Go",
        [
            _window("5h", "5-hour window", 69.0, 1.0, pace=_pace("safe", None, None)),
            _window("weekly", "Weekly window", 56.0, 50.0, pace=_pace("dry", 32.0, 18.0)),
            _window("monthly", "Monthly window", 46.0, 288.0, pace=_pace("safe", None, None)),
        ],
        level="warn",
        text="dry in ~32h",
    )
    grok = _provider(
        "grok",
        "Grok Build",
        "SuperGrok Heavy",
        [_window("weekly", "Weekly", 88.0, 120.0, pace=_pace("safe", None, None))],
        level="ok",
        text="resets in 5d",
    )
    router = _provider(
        "openrouter",
        "OpenRouter",
        "PAYG",
        [
            _window(
                "balance",
                "Balance",
                None,
                None,
                unit="usd",
                remainingUsd=23.40,
                used=23.40,
                runway="Runway ~42d at recent pace",
            )
        ],
        level="balance",
        text="$23.40 balance",
        isPayg=True,
        fundsRemainingUsd=23.40,
        fundsRunwayInfo={"state": "estimate", "days": 42.0, "runsOutAt": _iso(42 * 24)},
    )
    router["remaining"] = {
        "kind": "balance",
        "headline": "$23.40",
        "sub": "balance left",
        "barPct": 93.6,
        "tone": "mint",
        "resetsAt": None,
        "summary": "Runway ~42d at recent pace",
    }
    router["burn"] = {"dayTokens": None, "sessionTokens": None, "hasSession": False, "weekTokens": None, "weekBasis": None}
    router["tokens"] = 0
    router["valueUsd"] = None
    router["models"] = []
    return [claude, codex, go, grok, router]


def live_activity() -> list[dict]:
    return [
        {"state": "live", "name": "northwind-web", "modelKey": "claude-opus-5-5", "machineName": "Studio PC", "toolKey": "claude-code", "startedAt": _iso(-0.4)},
        {"state": "live", "name": "contoso-api", "modelKey": "gpt-6.1-sol", "machineName": "Travel Laptop", "toolKey": "codex", "startedAt": _iso(-0.2)},
    ]


def d2_report(variant: str = "default") -> dict:
    """``default`` is the canvas state; the other variants exercise one state each."""
    report = demo_report()
    report["providers"] = contract_providers()
    report["activity"] = live_activity()
    if variant == "profiles":
        work = copy.deepcopy(report["providers"][0])
        work["key"] = "claude-code-work"
        work["profile"] = "Work"
        work["inUse"] = False
        work["urgency"] = {"level": "ok", "limitKey": "weekly", "text": "resets in 5d"}
        for win in work["windows"]:
            win["remainingPct"], win["usedPct"], win["pace"] = 88.0, 12.0, _pace("safe", None, None)
        report["providers"][0]["profile"] = "Personal"
        report["providers"].insert(1, work)
    elif variant == "old-service":
        for prov in report["providers"]:
            for key in ("profile", "fundsRunwayInfo", "urgency"):
                prov.pop(key, None)
            for win in prov["windows"]:
                win.pop("pace", None)
        report["providers"][0]["windows"][0]["forecast"] = "Runs dry in about 1h at this pace"
    elif variant in ("no-limits", "summary-only"):
        for prov in report["providers"]:
            prov["windows"] = []
            if variant == "no-limits":
                prov["urgency"] = {"level": "idle", "limitKey": None, "text": "Waiting for fresh reading"}
                prov["fundsRemainingUsd"] = None
                prov["fundsRunwayInfo"] = None
                prov["isPayg"] = False
                prov["remaining"] = {
                    "kind": "idle",
                    "headline": "Idle",
                    "sub": prov["plan"],
                    "barPct": 0.0,
                    "tone": "muted",
                    "resetsAt": None,
                    "summary": "Waiting for fresh reading",
                }
            else:
                for key in ("urgency", "profile", "fundsRunwayInfo"):
                    prov.pop(key, None)
    elif variant == "unavailable":
        report["providers"][1]["windows"][0].update(
            {"unavailable": True, "remainingPct": None, "usedPct": None, "reason": "Sign in to Codex to read limits", "pace": None}
        )
        report["providers"][1]["urgency"] = {"level": "idle", "limitKey": None, "text": "Sign in to Codex to read limits"}
    return report
