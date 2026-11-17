"""The usage tab: payload states, overflow, hover card data, window geometry."""

from datetime import UTC, datetime, timedelta

import pytest

from spend_app import native_geometry, tab_window
from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
from spend_app.native_geometry import Monitor, Rect, SavedGeometry
from spend_app.tab_window import TabModel, build_segment, short_error, split_overflow
from spend_app.wealthfront_window import merge_provider_entries

NOW = datetime(2026, 11, 17, 12, 0, tzinfo=UTC)


def iso(hours):
    return (NOW + timedelta(hours=hours)).isoformat().replace("+00:00", "Z")


def card(key, pct, forecast=None, resets=5 * 24.0, title=None, label=None, limit="weekly"):
    return {
        "providerKey": key,
        "title": title or key,
        "plan": "Max",
        "accountLabel": label or "",
        "rows": [
            {"limitKey": limit, "label": f"{limit} window", "pct": pct, "unit": "pct",
             "resetsAt": iso(resets), "forecast": forecast}
        ],
    }


def dry(to_dry, before, state="dry"):
    return {"state": state, "hoursRemaining": to_dry, "hoursBeforeReset": before,
            "projectedRunDry": iso(to_dry), "line": "x"}


def balance(key, usd, runway=None):
    return {"providerKey": key, "title": key, "isPayg": True, "fundsRemainingUsd": usd,
            "fundsRunway": runway, "rows": []}


def providers(*cards):
    return merge_provider_entries({"tools": []}, hud={"capacity": list(cards)})


def payload(*cards, error=None, loaded=True, dark=True):
    model = TabModel()
    if loaded:
        model.publish_report({"providers": providers(*cards)})
    if error:
        model.publish_error(*error)
    return model.payload(dark=dark, now=NOW)


# ---- the demo is the canvas -------------------------------------------

def demo_payload():
    report = DemoClient().snapshot("today", "all")
    model = TabModel()
    model.publish_report({"providers": merge_provider_entries(
        report, hud=demo_hud(), hud_usage=demo_hud_usage(), today_report=report, week_report=report)})
    return model.payload(dark=True)


def test_demo_tab_keeps_each_provider_s_profiles_together():
    data = demo_payload()
    keys = [s["key"] for s in data["segments"]]
    assert keys == ["claude-code", "claude-code-work", "codex", "opencode-go", "opencode-go-2", "grok"]
    by_key = {s["key"]: s for s in data["segments"]}
    assert (by_key["claude-code"]["big"], by_key["claude-code"]["unit"]) == ("54%", "Primary · 5-hour")
    assert by_key["claude-code"]["level"] == "soon" and by_key["claude-code"]["status"] == "dry in ~1h"
    assert by_key["claude-code-work"]["unit"] == "Work · weekly"
    assert (by_key["codex"]["level"], by_key["codex"]["status"]) == ("warn", "dry in ~14h")
    assert (by_key["opencode-go"]["level"], by_key["opencode-go"]["status"]) == ("warn", "dry in ~32h")
    assert data["urgentCount"] == 1
    assert data["state"] == "ready"
    assert data["fine"]["count"] == 4


def test_profiles_stay_adjacent_even_when_their_levels_differ():
    data = payload(
        card("claude-code", 20.0, dry(30.0, 40.0), title="Claude Code"),
        card("grok", 50.0, dry(10.0, 40.0)),
        card("claude-code-work", 5.0, None, title="Claude Code · Work", label="Work"),
    )
    assert [s["key"] for s in data["segments"]] == ["grok", "claude-code", "claude-code-work"]


# ---- states -------------------------------------------------------------

def test_loading_state_has_no_fake_values():
    data = payload(loaded=False)
    assert data["state"] == "loading"
    assert data["segments"] == []
    assert data["layout"]["width"] == tab_window.layout_width(0, False, status=tab_window.LOADING_WIDTH)


def test_empty_state_when_there_are_no_limits_yet():
    data = payload()
    assert data["state"] == "empty"
    assert data["segments"] == [] and data["fine"] is None
    only_usage = TabModel()
    only_usage.publish_report({"providers": [{"key": "x", "remaining": {"kind": "usage_only"}, "windows": []}]})
    assert only_usage.payload(dark=False)["state"] == "empty"


def test_error_keeps_the_last_values_and_marks_them_stale():
    data = payload(card("claude-code", 46.0, dry(1.17, 1.06, "tight"), resets=2.2),
                   error=("Can't reach your hub. Check that this computer is on your network.", "NET-001"))
    assert data["state"] == "ready"
    assert data["stale"] is True
    assert data["error"]["text"] == "Can't reach your hub · NET-001"
    assert data["segments"][0]["big"] == "54%"
    assert data["layout"]["width"] == tab_window.layout_width(1, False, chip=True)


def test_error_before_any_value_shows_only_the_chip():
    data = payload(loaded=False, error=("The background service isn't running.", "APP-901"))
    assert data["state"] == "error"
    assert data["stale"] is False
    assert data["layout"]["width"] == tab_window.layout_width(0, False, chip=True)


def test_a_new_report_clears_the_error():
    model = TabModel()
    model.publish_error("Can't reach your hub.", "NET-001")
    model.publish_report({"providers": providers(card("grok", 12.0))})
    assert model.payload(dark=False)["error"] is None


def test_error_text_helper():
    assert short_error("Something broke. Try again.", None) == "Something broke"
    assert short_error("", "APP-901") == "Something went wrong · APP-901"


def test_unavailable_provider_is_a_muted_segment():
    cards = [{"providerKey": "claude-code", "title": "Claude Code", "plan": "Max",
              "rows": [{"limitKey": "5h", "label": "5h", "unavailable": True}]}]
    seg = payload(*cards)["segments"][0]
    assert seg["muted"] is True and seg["level"] == "idle"
    assert seg["big"] == "Claude Code"
    assert seg["status"]


def test_a_long_idle_reason_shows_as_awaiting_reading_with_the_reason_as_tooltip():
    cards = [{"providerKey": "claude-code", "title": "Claude Code", "plan": "Max",
              "rows": [{"limitKey": "5h", "label": "5h window — Waiting for a fresh reading", "unavailable": True}]}]
    seg = payload(*cards)["segments"][0]
    assert seg["status"] == "Awaiting reading"
    assert seg["detail"]
    short = payload({"providerKey": "grok", "title": "Grok", "isPayg": True, "rows": []})["segments"][0]
    assert short["status"] == "No key"


def test_two_profiles_one_segment_each_with_the_profile_name():
    data = payload(
        card("claude-code", 46.0, dry(1.17, 1.06, "tight"), resets=2.2, limit="5h"),
        card("claude-code-work", 80.0, None, resets=2.2, limit="5h", title="Claude Code · Work", label="Work"),
    )
    segments = data["segments"]
    assert [s["key"] for s in segments] == ["claude-code", "claude-code-work"]
    assert segments[0]["unit"] == "Primary · 5-hour"
    assert segments[1]["unit"] == "Work · 5-hour"
    assert segments[1]["title"] == "Claude Code · Work"


def test_balance_segment():
    runway = {"state": "estimate", "days": 42.0, "runsOutAt": iso(1000), "line": "x"}
    seg = payload(balance("openrouter", 23.4, runway))["segments"][0]
    assert (seg["big"], seg["unit"], seg["level"], seg["status"]) == ("$23.40", "balance", "balance", "~42d runway")
    assert seg["card"]["balance"]["summary"] == "About 42 days left at your current spend"
    plain = payload(balance("deepseek", 54.8))["segments"][0]
    assert plain["status"] == "pay as you go"


# ---- overflow -----------------------------------------------------------

def many(n_ok, n_warn=0):
    cards = [card(f"warn{i}", 40.0, dry(20.0 + i, 50.0)) for i in range(n_warn)]
    cards += [card(f"ok{i}", 10.0 + i) for i in range(n_ok)]
    return cards


def test_up_to_six_providers_never_fold():
    data = payload(*many(6))
    assert len(data["segments"]) == 6 and data["fine"] is None


def test_overflow_folds_only_ok_balance_and_idle():
    data = payload(*many(5, n_warn=3))
    assert [s["level"] for s in data["segments"]].count("warn") == 3
    assert len(data["segments"]) == 6
    assert data["fine"]["count"] == 2
    assert all(s["level"] in ("warn", "ok") for s in data["segments"])
    assert {item["level"] for item in data["fine"]["items"]} == {"ok"}


def test_urgent_segments_are_never_folded_even_past_the_cap():
    data = payload(*many(2, n_warn=8))
    assert len([s for s in data["segments"] if s["level"] == "warn"]) == 8
    assert data["fine"]["count"] == 2


def test_split_overflow_keeps_order():
    segs = [{"level": lv, "key": str(i)} for i, lv in enumerate(["soon", "ok", "ok", "ok", "ok", "ok", "balance", "idle"])]
    shown, fine = split_overflow(segs)
    assert [s["key"] for s in shown] == ["0", "1", "2", "3", "4", "5"]
    assert [s["key"] for s in fine] == ["6", "7"]


# ---- hover card ---------------------------------------------------------

def test_hover_card_lists_every_window_with_pace_lines():
    cc = card("claude-code", 46.0, dry(1.17, 1.06, "tight"), resets=2.2, limit="5h")
    cc["rows"].append({"limitKey": "weekly", "label": "Weekly window", "pct": 61.0, "unit": "pct",
                       "resetsAt": iso(78.0), "forecast": dry(27.0, 51.0)})
    seg = payload(cc)["segments"][0]
    rows = seg["card"]["rows"]
    assert [r["label"] for r in rows] == ["5-hour", "Weekly"]
    five, week = rows
    assert (five["leftPct"], five["usedPct"], five["resets"]) == (54, 46, "Resets in 2h")
    assert five["pace"] == {"level": "soon", "text": "At this pace: dry in ~1h, 1h before it resets"}
    assert week["leftPct"] == 39
    assert week["pace"] == {"level": "warn", "text": "At this pace: dry in ~27h, 51h before it resets"}


def test_hover_card_marks_a_safe_pace_and_hides_an_unforecast_one():
    seg = payload(card("grok", 12.0, {"state": "safe", "hoursRemaining": None}))["segments"][0]
    assert seg["card"]["rows"][0]["pace"]["level"] == "ok"
    seg = payload(card("grok", 12.0))["segments"][0]
    assert seg["card"]["rows"][0]["pace"] is None


def test_hover_card_for_an_unavailable_window_is_muted():
    cc = {"providerKey": "claude-code", "title": "CC", "rows": [
        {"limitKey": "5h", "label": "5h window — Waiting for a fresh reading", "unavailable": True},
        {"limitKey": "weekly", "label": "w", "pct": 20.0, "unit": "pct", "resetsAt": iso(100)}]}
    rows = payload(cc)["segments"][0]["card"]["rows"]
    assert rows[0] == {"label": "5-hour", "muted": True, "note": "Waiting for a fresh reading"}


def test_segment_values_are_data_not_markup():
    seg = build_segment(providers(card("x", 10.0, title="<b>Evil</b>"))[0], NOW)
    assert seg["name"] == "<b>Evil</b>"  # the page escapes with esc()


# ---- the page and its shared logos --------------------------------------

def test_the_page_gets_the_real_logo_sprite_and_brand_table():
    html = tab_window.load_tab_html("dark")
    assert 'data-theme-pref="dark"' in html
    for symbol in ("claude", "codex", "opencode", "grok", "openrouter", "zai", "deepseek"):
        assert f'<symbol id="{symbol}"' in html
    assert "const BRAND_STYLES" in html and "function resolveBrand" in html and "function logoHtml" in html
    assert "__BRAND_SPRITE__" not in html and "__BRAND_JS__" not in html and "__EMBEDDED_FONTS__" not in html


def test_page_escapes_every_value():
    source = (tab_window.Path(tab_window.__file__).with_name("tab_ui.html")).read_text(encoding="utf-8")
    assert "function esc(" in source
    assert "innerHTML" in source
    # Every dynamic piece in the markup builders goes through esc() or Number().
    for needle in ("s.name", "s.status", "s.unit", "s.aria", "r.label", "r.note", "r.resets", "seg.title", "seg.plan", "it.title", "next.error.text"):
        assert f"esc({needle})" in source


def test_no_hub_address_in_the_page():
    html = tab_window.load_tab_html("system")
    assert "http://" not in html.split("<style>", 1)[-1].split("</style>")[0]


# ---- geometry -----------------------------------------------------------

def mon(left, top, right, bottom, primary=True, bar=40):
    return Monitor(Rect(left, top, right, bottom), Rect(left, top, right, bottom - bar), primary)


MAIN = [mon(0, 0, 1920, 1080)]
TWO = [mon(0, 0, 1920, 1080), mon(1920, 0, 3840, 1080, primary=False)]


def test_default_tab_position_is_top_right_of_the_work_area():
    assert native_geometry.plan_tab_position(None, MAIN, 96, (800, 60)) == (1920 - 800 - 16, 16)
    assert native_geometry.plan_tab_position(None, [], 96, (800, 60)) is None


def test_saved_tab_position_is_kept_and_rescaled_across_dpi():
    saved = SavedGeometry(300, 200, 800, 60, 96)
    assert native_geometry.plan_tab_position(saved, MAIN, 96, (800, 60)) == (300, 200)
    assert native_geometry.plan_tab_position(saved, MAIN, 144, (1200, 90)) == (450, 300)


def test_saved_tab_position_off_screen_comes_back():
    saved = SavedGeometry(5000, 4000, 800, 60, 96)
    assert native_geometry.plan_tab_position(saved, MAIN, 96, (800, 60)) == (1920 - 800, 1040 - 60)
    gone = SavedGeometry(2500, 100, 800, 60, 96)  # second monitor unplugged
    assert native_geometry.plan_tab_position(gone, MAIN, 96, (800, 60)) == (1120, 100)
    assert native_geometry.plan_tab_position(gone, TWO, 96, (800, 60)) == (2500, 100)


def test_keep_on_screen_clamps_each_edge():
    assert native_geometry.keep_on_screen((-300, -20, 800, 60), MAIN) == (0, 0)
    assert native_geometry.keep_on_screen((1500, 1000, 800, 60), MAIN) == (1120, 980)
    assert native_geometry.keep_on_screen((100, 100, 800, 60), MAIN) == (100, 100)


def test_expanded_tab_grows_down_then_up_near_the_bottom():
    assert native_geometry.place_expanded((100, 100, 800, 60), (800, 330), MAIN) == (100, 100, False)
    assert native_geometry.place_expanded((100, 900, 800, 60), (800, 330), MAIN) == (100, 630, True)
    assert native_geometry.place_expanded((1400, 100, 800, 60), (816, 330), MAIN) == (1104, 100, False)


def test_defaults_for_the_full_window():
    assert native_geometry.DEFAULT_SIZE == (440, 640)
    assert native_geometry.MIN_SIZE == (380, 520)
    assert native_geometry.default_size_for(144) == (660, 960)
    assert native_geometry.min_size_for(96) == (380, 520)


def test_a_saved_old_size_is_raised_to_the_new_minimum():
    saved = SavedGeometry(100, 100, 336, 440, 96)
    plan = native_geometry.plan_restore(saved, MAIN, 96)
    assert (plan.width, plan.height) == (380, 520)


# ---- the tab host (win32 calls faked) -----------------------------------

class FakeWindow:
    def __init__(self):
        self.calls = []

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def destroy(self):
        self.calls.append("destroy")

    def evaluate_js(self, script):
        self.calls.append(("js", script))


class FakeStore:
    def __init__(self):
        self.saved = {}

    def update(self, changes):
        self.saved.update(changes)

    def read(self):
        return dict(self.saved)


@pytest.fixture
def host(monkeypatch):
    applied = []
    host = tab_window.TabHost(FakeWindow(), store=FakeStore(), hwnd_box=[77])
    monkeypatch.setattr(host, "_apply", lambda x, y, w, h: applied.append((x, y, w, h)))
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: 96)
    monkeypatch.setattr(native_geometry, "enum_monitors", lambda: MAIN)
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(100, 100, 900, 160))
    host.applied = applied
    return host


def test_set_base_sizes_the_window_to_its_content(host):
    host.set_base(800)
    assert host.applied[-1] == (100, 100, 800, 60)


def test_expand_grows_the_window_and_collapse_restores_it(host):
    host.set_base(800)
    assert host.expand(316, 330) == {"up": False}
    assert host.applied[-1] == (100, 100, 800, 330)
    host.collapse()
    assert host.applied[-1] == (100, 100, 800, 60)


def test_expand_is_wide_enough_for_the_card_and_grows_up_at_the_bottom(host, monkeypatch):
    host.set_base(250)
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(100, 960, 350, 1020))
    result = host.expand(316, 330)
    assert result == {"up": True}
    assert host.applied[-1] == (100, 690, 316, 330)
    host.collapse()
    assert host.applied[-1] == (100, 960, 250, 60)


def test_expand_scales_with_dpi(host, monkeypatch):
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: 144)
    host.set_base(800)
    assert host.applied[-1][2:] == (1200, 90)


def test_tab_is_not_saved_while_a_card_stretches_it(host, monkeypatch):
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: "SNAP")
    assert host._snapshot(77) == "SNAP"
    host.set_base(800)
    host.expand(316, 330)
    assert host._snapshot(77) is None
    host.collapse()
    assert host._snapshot(77) == "SNAP"


def test_tab_position_saves_under_its_own_key(host, monkeypatch):
    snap = SavedGeometry(10, 20, 800, 60, 96)
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: snap)
    assert host.tracker.flush() is True
    assert host._store.saved == {"tabWindow": snap.as_preference()}


def test_show_and_hide(host):
    host.show()
    assert host.visible is True and host.window.calls[-1] == "show"
    host.hide()
    assert host.visible is False and host.window.calls[-1] == "hide"


def test_hide_drops_a_open_card(host):
    host.set_base(800)
    host.expand(316, 330)
    host.hide()
    assert host._expanded is None


def test_push_evaluates_the_payload_on_a_worker(host):
    import threading

    host.push({"state": "ready"})
    for thread in threading.enumerate():
        if thread.name == "burnrate-tab-push":
            thread.join(2)
    script = host.window.calls[-1][1]
    assert "burnrateTab.update" in script and '"state": "ready"' in script


def test_restore_position_uses_the_saved_spot(host):
    host.set_base(800)
    host.restore_position({"x": 300, "y": 200, "width": 800, "height": 60, "dpi": 96})
    assert host.applied[-1] == (300, 200, 800, 60)
    host.restore_position(None)
    assert host.applied[-1] == (1920 - 800 - 16, 16, 800, 60)


def test_destroy_stops_saving(host):
    host.destroy()
    assert host.quitting is True and host.window.calls[-1] == "destroy"


def test_the_form_gets_its_true_default_size(monkeypatch):
    import sys
    import types

    sizes = []
    drawing = types.ModuleType("System.Drawing")
    drawing.Size = lambda w, h: sizes.append((w, h)) or (w, h)
    system = types.ModuleType("System")
    system.Drawing = drawing
    monkeypatch.setitem(sys.modules, "System", system)
    monkeypatch.setitem(sys.modules, "System.Drawing", drawing)
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: 144)
    native = types.SimpleNamespace(Handle=types.SimpleNamespace(ToInt64=lambda: 9))
    native_geometry.apply_default_size(native)
    assert sizes == [(660, 960)] and native.Size == (660, 960)


def test_a_refresh_that_widens_the_tab_widens_an_open_card_window_too(host):
    host.set_base(400)
    host.expand(316, 330)
    host.set_base(900)
    assert host.applied[-1][2:] == (900, 330)
    host.collapse()
    assert host.applied[-1][2:] == (900, 60)
