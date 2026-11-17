"""The D refined data contract: per-window pace and per-provider urgency."""

from datetime import UTC, datetime, timedelta

import pytest

from spend_app import provider_urgency as pu
from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
from spend_app.wealthfront_window import merge_provider_entries

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def iso(hours: float) -> str:
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def forecast(state="dry", to_dry=10.0, before=20.0):
    return {
        "state": state,
        "projectedRunDry": iso(to_dry) if to_dry is not None else None,
        "hoursRemaining": to_dry,
        "hoursBeforeReset": before,
        "burnPctPerHour": 3.0,
        "line": "pace → dry",
    }


def row(limit="5h", pct=50.0, fc=None, resets=3.0, **extra):
    return {
        "limitKey": limit,
        "label": f"{limit} window",
        "pct": pct,
        "unit": "pct",
        "resetsAt": iso(resets),
        "forecast": fc,
        **extra,
    }


def merged(*cards, **kw):
    hud = {"capacity": [dict({"plan": "Max"}, **c) for c in cards]}
    report = {"tools": []}
    return merge_provider_entries(report, hud=hud, **kw)


def provider(key="claude-code", rows=(), **card):
    return merged({"providerKey": key, "title": "Claude Code", "rows": list(rows), **card})[0]


def urgency(p):
    return pu.compute_urgency(p, NOW)


# ---- pace -------------------------------------------------------------

def test_pace_maps_forecast_fields():
    pace = pu.build_pace(forecast("tight", to_dry=1.17, before=1.06))
    assert pace == {
        "state": "tight",
        "hoursToDry": 1.17,
        "hoursDryBeforeReset": 1.06,
        "runsDryAt": iso(1.17),
    }


@pytest.mark.parametrize("raw", [None, {}, "x", {"state": "stale"}, {"state": "bogus"}])
def test_pace_is_none_without_a_usable_forecast(raw):
    assert pu.build_pace(raw) is None


def test_safe_pace_keeps_a_missing_run_dry_time_as_none():
    pace = pu.build_pace({"state": "safe", "hoursRemaining": None, "projectedRunDry": None})
    assert pace == {
        "state": "safe",
        "hoursToDry": None,
        "hoursDryBeforeReset": None,
        "runsDryAt": None,
    }


def test_window_carries_pace_and_keeps_text_lines():
    p = provider(rows=[row("5h", 54.0, forecast("tight", 1.17, 1.06))])
    window = p["windows"][0]
    assert window["pace"]["state"] == "tight"
    assert window["pace"]["hoursToDry"] == 1.17
    assert window["forecast"] == "pace → dry"
    assert window["runway"] == ""


def test_window_pace_is_none_when_no_forecast():
    assert provider(rows=[row("5h", 10.0)])["windows"][0]["pace"] is None


# ---- funds runway and profile ----------------------------------------

def test_funds_runway_info():
    p = merged(
        {
            "providerKey": "openrouter",
            "title": "OpenRouter",
            "isPayg": True,
            "fundsRemainingUsd": 23.4,
            "fundsRunway": {
                "state": "estimate",
                "days": 42.0,
                "runsOutAt": iso(1000),
                "line": "about 42 days",
            },
            "rows": [],
        }
    )[0]
    assert p["fundsRunwayInfo"] == {"state": "estimate", "days": 42.0, "runsOutAt": iso(1000)}
    assert p["remaining"]["summary"] == "about 42 days"


def test_funds_runway_info_is_none_for_a_text_only_runway_or_no_balance():
    text_only = merged(
        {
            "providerKey": "deepseek",
            "isPayg": True,
            "fundsRemainingUsd": 5.0,
            "fundsRunway": {"line": "Prepaid"},
            "rows": [],
        }
    )[0]
    assert text_only["fundsRunwayInfo"] is None
    assert provider(rows=[row()])["fundsRunwayInfo"] is None


def test_profile_is_set_only_when_a_provider_has_several_profiles():
    both = merged(
        {"providerKey": "claude-code", "title": "Claude Code", "rows": [row()]},
        {
            "providerKey": "claude-code-work",
            "title": "Claude Code · Work",
            "accountLabel": "Work",
            "rows": [row()],
        },
    )
    by_key = {p["key"]: p for p in both}
    assert by_key["claude-code"]["profile"] == "Primary"
    assert by_key["claude-code-work"]["profile"] == "Work"
    assert by_key["claude-code-work"]["badge"] == "Work"
    assert provider(rows=[row()])["profile"] is None
    org = merged({"providerKey": "openrouter", "isPayg": True, "fundsRemainingUsd": 9.0, "rows": []})[0]
    assert org["profile"] is None


# ---- focus window -----------------------------------------------------

def windows(*specs):
    return {"windows": [{"limitKey": k, "remainingPct": pct, "pace": pace} for k, pct, pace in specs]}


def drying(hours, state="dry"):
    return {"state": state, "hoursToDry": hours}


def test_focus_prefers_the_window_that_dries_soonest():
    p = windows(("5h", 20.0, None), ("weekly", 70.0, drying(30.0)), ("monthly", 60.0, drying(9.0)))
    assert pu.focus_window(p)["limitKey"] == "monthly"


def test_focus_falls_back_to_the_lowest_remaining():
    p = windows(("5h", 90.0, None), ("weekly", 35.0, {"state": "safe", "hoursToDry": 99.0}), ("monthly", 60.0, None))
    assert pu.focus_window(p)["limitKey"] == "weekly"


def test_focus_ignores_unavailable_and_unmeasured_windows():
    p = {
        "windows": [
            {"limitKey": "5h", "remainingPct": 1.0, "unavailable": True, "pace": drying(0.1)},
            {"limitKey": "weekly", "remainingPct": None},
            {"limitKey": "monthly", "remainingPct": 55.0},
        ]
    }
    assert pu.focus_window(p)["limitKey"] == "monthly"
    assert pu.focus_window({"windows": []}) is None


# ---- every urgency level ----------------------------------------------

def test_level_out_when_the_window_is_empty():
    p = provider(rows=[row("5h", 100.0, forecast("dry", 0.0, 2.0), resets=2.0)])
    assert urgency(p) == {"level": "out", "limitKey": "5h", "text": "out · resets in 2h"}


def test_level_out_for_an_empty_balance():
    p = provider("openrouter", isPayg=True, fundsRemainingUsd=0.0, rows=[])
    assert urgency(p) == {"level": "out", "limitKey": None, "text": "$0.00 balance"}


def test_level_soon_for_a_tight_pace_within_two_hours():
    p = provider(rows=[row("5h", 46.0, forecast("tight", 1.17, 1.06))])
    assert urgency(p) == {"level": "soon", "limitKey": "5h", "text": "dry in ~1h"}


def test_level_soon_for_a_pace_of_exactly_two_hours():
    p = provider(rows=[row("5h", 46.0, forecast("tight", 2.0, 1.0))])
    assert urgency(p)["level"] == "soon"


def test_level_soon_for_ten_percent_left_without_a_pace():
    p = provider(rows=[row("weekly", 90.0, None, resets=30.0)])
    assert urgency(p) == {"level": "soon", "limitKey": "weekly", "text": "10% left · resets in 30h"}


def test_level_soon_for_a_balance_under_a_dollar():
    p = provider("openrouter", isPayg=True, fundsRemainingUsd=0.8, rows=[])
    assert urgency(p)["level"] == "soon"


def test_level_warn_when_it_dries_before_refill():
    p = provider(rows=[row("weekly", 38.0, forecast("dry", 14.0, 96.0))])
    assert urgency(p) == {"level": "warn", "limitKey": "weekly", "text": "dry in ~14h"}


def test_level_warn_for_a_balance_under_five_dollars():
    p = provider("openrouter", isPayg=True, fundsRemainingUsd=4.99, rows=[])
    assert urgency(p)["level"] == "warn"


def test_level_ok_when_measured_and_on_pace():
    p = provider(rows=[row("weekly", 12.0, forecast("safe", None, None), resets=5 * 24.0)])
    assert urgency(p) == {"level": "ok", "limitKey": "weekly", "text": "resets in 5d"}


def test_level_balance_for_a_healthy_prepaid_balance():
    p = provider("openrouter", isPayg=True, fundsRemainingUsd=23.4, rows=[])
    assert urgency(p) == {"level": "balance", "limitKey": None, "text": "$23.40 balance"}


def test_level_idle_with_the_existing_plain_reason():
    p = provider(rows=[{"limitKey": "5h", "label": "5h", "unavailable": True}], plan="Max")
    result = urgency(p)
    assert result["level"] == "idle"
    assert result["limitKey"] is None
    assert result["text"]


def test_level_idle_for_a_payg_provider_without_a_key():
    p = provider("openrouter", isPayg=True, rows=[])
    assert urgency(p) == {"level": "idle", "limitKey": None, "text": "No key"}


def test_every_provider_gets_a_complete_urgency_dict():
    for p in merged({"providerKey": "claude-code", "rows": [row()]}, {"providerKey": "grok", "rows": []}):
        assert set(p["urgency"]) == {"level", "limitKey", "text"}
        assert p["urgency"]["level"] in pu.LEVELS


def test_usage_only_tools_are_idle():
    report = {"tools": [{"key": "opencode-provider:x", "name": "X", "tokens": 5, "valueUsd": 1.0, "models": []}]}
    p = merge_provider_entries(report, hud={"capacity": []})[0]
    assert p["remaining"]["kind"] == "usage_only"
    assert p["urgency"]["level"] == "idle"
    assert p["fundsRunwayInfo"] is None


# ---- sort key ---------------------------------------------------------

def test_sort_key_orders_levels_then_pace_then_remaining():
    cards = [
        {"providerKey": "grok", "title": "Grok", "rows": [row("weekly", 12.0, None)]},
        {"providerKey": "codex", "title": "Codex", "rows": [row("weekly", 38.0, forecast("dry", 14.0, 96.0))]},
        {"providerKey": "claude-code", "title": "CC", "rows": [row("5h", 46.0, forecast("tight", 1.17, 1.06))]},
        {"providerKey": "openrouter", "title": "OR", "isPayg": True, "fundsRemainingUsd": 30.0, "rows": []},
        {"providerKey": "deepseek", "title": "DS", "isPayg": True, "fundsRemainingUsd": 20.0, "rows": []},
        {"providerKey": "muse", "title": "Muse", "rows": [{"limitKey": "weekly", "unavailable": True}]},
        {"providerKey": "zcode", "title": "Z", "rows": [row("weekly", 99.0, None, resets=4.0)]},
        {"providerKey": "antigravity", "title": "AG", "rows": [row("weekly", 40.0, None)]},
        {"providerKey": "opencode-go", "title": "OG", "rows": [row("weekly", 44.0, forecast("dry", 32.0, 60.0))]},
    ]
    ordered = sorted(merged(*cards), key=pu.sort_key)
    assert [p["key"] for p in ordered] == [
        "claude-code",  # soon, dry in 1h
        "zcode",  # soon, 1% left with no pace
        "codex",  # warn, 14h
        "opencode-go",  # warn, 32h
        "antigravity",  # ok, 60% left
        "grok",  # ok, 88% left
        "deepseek",  # balance, smaller balance first
        "openrouter",
        "muse",  # idle last
    ]


def test_sort_key_breaks_ties_inside_a_level_by_remaining():
    first = provider("a", rows=[row("weekly", 60.0)])
    second = provider("b", rows=[row("weekly", 20.0)])
    assert pu.sort_key(first)[0] == pu.sort_key(second)[0] == pu.LEVEL_RANK["ok"]
    assert pu.sort_key(first) < pu.sort_key(second)  # 40% left sorts before 80% left


# ---- formatting -------------------------------------------------------

@pytest.mark.parametrize(
    "hours, text",
    [(0.4, "~24m"), (1.17, "~1h"), (13.6, "~14h"), (32.0, "~32h"), (0.0, "~0m"), (None, "~0m")],
)
def test_format_hours(hours, text):
    assert pu.format_hours(hours) == text


@pytest.mark.parametrize(
    "ahead, text",
    [(0.5, "resets in 30m"), (2.2, "resets in 2h"), (47.0, "resets in 47h"), (110.0, "resets in 5d"), (-1, "resets now")],
)
def test_format_reset(ahead, text):
    assert pu.format_reset(iso(ahead), NOW) == text


def test_format_reset_without_a_time():
    assert pu.format_reset(None, NOW) == ""
    assert pu.format_reset("nonsense", NOW) == ""


# ---- the demo is the canvas state -------------------------------------

def demo_providers():
    report = DemoClient().snapshot("today", "all")
    providers = merge_provider_entries(
        report,
        hud=demo_hud(),
        hud_usage=demo_hud_usage(),
        today_report=report,
        week_report=report,
    )
    return {p["key"]: p for p in providers}


@pytest.mark.parametrize(
    "key, limit, left, level, text",
    [
        ("claude-code", "5h", 54.0, "soon", "dry in ~1h"),
        ("codex", "weekly", 62.0, "warn", "dry in ~14h"),
        ("opencode-go", "weekly", 56.0, "warn", "dry in ~32h"),
        ("grok", "weekly", 88.0, "ok", "resets in 5d"),
    ],
)
def test_demo_matches_the_canvas(key, limit, left, level, text):
    p = demo_providers()[key]
    assert p["urgency"] == {"level": level, "limitKey": limit, "text": text}
    window = next(w for w in p["windows"] if w["limitKey"] == limit)
    assert window["remainingPct"] == left


def test_demo_canvas_pace_values():
    claude = next(w for w in demo_providers()["claude-code"]["windows"] if w["limitKey"] == "5h")
    assert claude["pace"]["hoursToDry"] == pytest.approx(1.17)
    assert claude["pace"]["hoursDryBeforeReset"] == pytest.approx(1.06, abs=0.01)
    assert claude["pace"]["state"] == "tight"
    assert demo_providers()["grok"]["windows"][0]["pace"] is None


def test_demo_openrouter_is_the_balance_segment():
    p = demo_providers()["openrouter"]
    assert p["urgency"] == {"level": "balance", "limitKey": None, "text": "$23.40 balance"}
    assert p["fundsRunwayInfo"]["state"] == "estimate"
    assert p["fundsRunwayInfo"]["days"] == 42.0


def test_demo_tab_order_starts_with_the_canvas_order():
    providers = sorted(demo_providers().values(), key=pu.sort_key)
    assert [p["key"] for p in providers[:3]] == ["claude-code", "codex", "opencode-go"]
    levels = [p["urgency"]["level"] for p in providers]
    assert levels == sorted(levels, key=pu.LEVEL_RANK.get)
