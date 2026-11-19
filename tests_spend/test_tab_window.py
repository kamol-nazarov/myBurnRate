"""The usage tab: payload states, overflow, hover card data, window geometry."""

from datetime import UTC, datetime, timedelta

import pytest

from spend_app import native_geometry, tab_window
from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
from spend_app.native_geometry import Monitor, Rect, SavedGeometry
from spend_app.tab_window import TabModel, abbreviate_spend, build_segment, extract_spend, short_error
from spend_app.wealthfront_window import merge_provider_entries

# Some host/browser tests consume these fixtures with the live render clock.
NOW = datetime.now(UTC)


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
    report = dict(report)
    report["providers"] = merge_provider_entries(
        report, hud=demo_hud(), hud_usage=demo_hud_usage(), today_report=report, week_report=report
    )
    model = TabModel()
    model.publish_report(report, today_report=report)
    return model.payload(dark=True)


def test_demo_tab_is_an_option_4b_corner_pill_with_daily_spend_and_overview():
    data = demo_payload()
    assert data["state"] == "ready"
    assert data["layout"]["height"] == 40
    assert data["layout"]["width"] <= 280  # ultra-compact corner pill (~260px)
    # Live daily spend chip + hover card summary
    assert data["spend"]["compact"] == "$127"
    assert data["spend"]["exact"] == "$127.34"
    assert "$12.73/h" in data["spend"]["meta"] and "56% cache" in data["spend"]["meta"]
    # Resting pill shows #1 most-urgent limit + "+9" overflow pill
    primary = data["primary"]
    assert primary["key"] == "claude-code"
    assert (primary["big"], primary["shortStatus"], primary["level"]) == ("54%", "~1h", "soon")
    assert [s["key"] for s in data["segments"]] == ["claude-code"]
    assert data["more"]["count"] == 9
    assert data["more"]["severity"] == "warn"  # Codex + OpenCode Go are warn; no 2nd out/soon
    assert data["urgentCount"] == 1
    # Hover overview lists urgent/warn rows first and summarizes the 7 calm/balance providers
    ov = data["overview"]
    assert [s["key"] for s in ov["highlighted"]] == ["claude-code", "codex", "opencode-go"]
    assert ov["highlighted"][0]["cardStatus"] == "54% · dry ~1h"
    assert ov["highlighted"][1]["cardStatus"] == "62% · dry ~14h"
    assert ov["highlighted"][2]["cardStatus"] == "56% · dry ~32h"
    assert len(ov["rest"]) == 7
    assert ov["restSummary"] == "7 other providers healthy / balances"
    assert ov["restStatus"] == "All fine"


LEVEL_RANK_OF = {"out": 0, "soon": 1, "warn": 2, "ok": 3, "balance": 4, "idle": 5}


def test_a_healthy_profile_never_precedes_a_more_urgent_provider():
    data = payload(
        card("claude-code", 100.0, dry(0.0, 40.0), title="Claude Code", resets=24.0),  # out
        card("claude-code-work", 5.0, None, title="Claude Code \u00b7 Work", label="Work"),  # ok
        card("codex", 50.0, dry(1.5, 40.0)),  # soon
    )
    assert data["primary"]["key"] == "claude-code"
    ordered = [s["key"] for s in data["overview"]["highlighted"]] + [r["key"] for r in data["overview"]["rest"]]
    assert ordered == ["claude-code", "codex", "claude-code-work"]
    assert data["more"]["count"] == 2 and data["more"]["severity"] == "soon"


def test_profiles_sit_together_only_among_equal_sort_keys():
    data = payload(
        card("claude-code", 50.0, None, title="Claude Code"),
        card("grok", 50.0, None),
        card("claude-code-work", 50.0, None, title="Claude Code \u00b7 Work", label="Work"),
    )
    # all three tie exactly: the Claude profiles sit together, at the first one's place
    assert [s["key"] for s in data["overview"]["highlighted"]] == ["claude-code", "claude-code-work", "grok"]


def test_spend_abbreviation_and_extraction():
    assert abbreviate_spend(0) == "$0"
    assert abbreviate_spend(4.2) == "$4.20"
    assert abbreviate_spend(127.34) == "$127"
    assert abbreviate_spend(1420.0) == "$1.4K"
    assert abbreviate_spend(1_500_000.0) == "$1.5M"
    assert extract_spend({"providers": []}) is None  # never invents $0 without a total
    week = {"period": "week", "total": {"valueUsd": 800.0, "complete": True}}
    today = {"period": "today", "total": {"valueUsd": 42.5, "complete": False},
             "rate": {"perHourUsd": 5.25, "complete": True},
             "tools": [{"cached": 60, "input": 40, "writes": 0, "organization": False}]}
    sp = extract_spend(week, today_report=today)
    assert sp["compact"] == "$42+" and sp["exact"] == "$42.50+"
    assert sp["meta"] == "$5.25/h · 60% cache"


# ---- states -------------------------------------------------------------

def test_loading_state_has_no_fake_values():
    data = payload(loaded=False)
    assert data["state"] == "loading"
    assert data["segments"] == [] and data["primary"] is None and data["spend"] is None
    assert data["layout"]["width"] == (
        tab_window.PILL_PAD + tab_window.HOME_WIDTH + tab_window.LOADING_WIDTH + tab_window.REFRESH_WIDTH
    )


def test_empty_state_when_there_are_no_limits_yet():
    data = payload()
    assert data["state"] == "empty"
    assert data["segments"] == [] and data["more"] is None
    only_usage = TabModel()
    only_usage.publish_report({"providers": [{"key": "x", "remaining": {"kind": "usage_only"}, "windows": []}]})
    assert only_usage.payload(dark=False)["state"] == "empty"


def test_error_keeps_the_last_values_and_marks_them_stale():
    data = payload(card("claude-code", 46.0, dry(1.17, 1.06, "tight"), resets=2.2),
                   error=("Can't reach your hub. Check that this computer is on your network.", "NET-001"))
    assert data["state"] == "ready"
    assert data["stale"] is True
    assert data["error"]["text"] == "Can't reach your hub · NET-001"
    assert data["primary"]["big"] == "54%"
    assert data["layout"]["mode"]["chip"] == "code"


def test_error_before_any_value_shows_only_the_chip():
    data = payload(loaded=False, error=("The background service isn't running.", "APP-901"))
    assert data["state"] == "error"
    assert data["stale"] is False
    assert data["layout"]["mode"]["chip"] == "full"
    assert data["layout"]["width"] == (
        tab_window.PILL_PAD + tab_window.HOME_WIDTH + tab_window.CHIP_WIDTH + tab_window.REFRESH_WIDTH
    )


def test_a_new_report_clears_the_error():
    model = TabModel()
    model.publish_error("Can't reach your hub.", "NET-001")
    model.publish_report({"providers": providers(card("grok", 12.0))})
    assert model.payload(dark=False)["error"] is None


def test_failed_quota_report_keeps_supplied_stale_values_and_fresh_empty_is_empty():
    model = TabModel()
    limits = providers(card("claude-code", 100.0))
    model.publish_report({"providers": [], "quotaStatus": "unavailable"})
    assert model.payload(dark=False, now=NOW)["state"] == "error"

    model.publish_report({"providers": limits, "quotaStatus": "fresh"})
    model.publish_report({"providers": limits, "quotaStatus": "stale"})
    stale = model.payload(dark=False, now=NOW)
    assert stale["state"] == "ready" and stale["stale"] is True
    assert stale["segments"][0]["big"] == "0%" and stale["urgentCount"] == 1
    assert stale["error"]["text"] == "Quota refresh failed"

    model.publish_report({"providers": [], "quotaStatus": "fresh"})
    fresh = model.payload(dark=False, now=NOW)
    assert fresh["state"] == "empty" and fresh["stale"] is False and fresh["error"] is None


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


def test_two_profiles_keep_profile_names_in_pill_and_overview():
    data = payload(
        card("claude-code", 46.0, dry(1.17, 1.06, "tight"), resets=2.2, limit="5h"),
        card("claude-code-work", 80.0, None, resets=2.2, limit="5h", title="Claude Code · Work", label="Work"),
    )
    assert data["primary"]["key"] == "claude-code"
    assert data["primary"]["unit"] == "Primary · 5-hour"
    assert data["more"]["count"] == 1
    assert data["more"]["items"][0]["title"] == "Claude Code · Work"


def test_balance_segment():
    runway = {"state": "estimate", "days": 42.0, "runsOutAt": iso(1000), "line": "x"}
    seg = payload(balance("openrouter", 23.4, runway))["segments"][0]
    assert (seg["big"], seg["unit"], seg["level"], seg["status"], seg["shortStatus"]) == (
        "$23.40", "balance", "balance", "~42d runway", "~42d"
    )
    assert seg["card"]["balance"]["summary"] == "About 42 days left at your current spend"
    plain = payload(balance("deepseek", 54.8))["segments"][0]
    assert plain["status"] == "pay as you go" and plain["shortStatus"] == ""


# ---- overflow & overview ------------------------------------------------

def many(n_ok, n_warn=0):
    cards = [card(f"warn{i}", 40.0, dry(20.0 + i, 50.0)) for i in range(n_warn)]
    cards += [card(f"ok{i}", 10.0 + i) for i in range(n_ok)]
    return cards


def test_single_provider_has_no_more_pill_and_uses_single_detail_card():
    data = payload(*many(1))
    assert data["more"] is None
    assert data["overview"]["single"] is True
    assert len(data["overview"]["highlighted"]) == 1


def test_multiple_providers_fold_into_more_pill_and_overview_groups_urgent_first():
    data = payload(*many(5, n_warn=3))
    assert data["primary"]["level"] == "warn"
    assert data["more"]["count"] == 7
    assert data["more"]["severity"] == "warn"
    assert [s["level"] for s in data["overview"]["highlighted"]] == ["warn", "warn", "warn"]
    assert len(data["overview"]["rest"]) == 5
    assert data["overview"]["restSummary"] == "5 other providers healthy"


def test_all_calm_providers_highlight_top_three_in_hover_overview():
    data = payload(*many(6))
    assert data["more"]["count"] == 5 and data["more"]["severity"] == "ok"
    assert len(data["overview"]["highlighted"]) == 3
    assert len(data["overview"]["rest"]) == 3


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
    for needle in ("p.shortStatus", "r.label", "r.note", "r.resets", "seg.title", "seg.plan", "it.title", "item.title", "chipText", "s.spend.exact"):
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
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(100, 100, 360, 140))
    host.applied = applied
    return host


def test_set_base_sizes_the_window_to_its_content(host):
    host.set_base(260)
    assert host.applied[-1] == (100, 100, 260, 40)


def test_expand_grows_the_window_and_collapse_restores_it(host):
    host.set_base(260)
    assert host.expand(308, 256) == {"up": False}
    assert host.applied[-1] == (100, 100, 308, 256)
    host.collapse()
    assert host.applied[-1] == (100, 100, 260, 40)


def test_expand_is_wide_enough_for_the_card_and_grows_up_at_the_bottom(host, monkeypatch):
    host.set_base(260)
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(100, 980, 360, 1020))
    result = host.expand(308, 256)
    assert result == {"up": True}
    assert host.applied[-1] == (100, 764, 308, 256)
    host.collapse()
    assert host.applied[-1] == (100, 980, 260, 40)


def test_expand_near_right_edge_returns_stripX_so_the_pill_stays_anchored(host, monkeypatch):
    host.set_base(260)
    # Default top-right parking spot: 1920 - 260 - 16 = 1644
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(1644, 16, 1904, 56))
    regions = []
    monkeypatch.setattr(host, "_set_region", lambda rects: regions.append(rects))
    result = host.expand(308, 256, (0, 308, 210))
    assert result == {"up": False, "stripX": 32.0}
    assert host.applied[-1] == (1612, 16, 308, 256)
    # Strip region stays at screen x = 1612 + 32 = 1644..1904 (zero jump!)
    assert regions[-1][0] == (32, 0, 292, 40, 20)
    assert regions[-1][1] == (0, 46, 308, 256, 14)
    host.collapse()
    assert host.applied[-1] == (1644, 16, 260, 40)


def test_expand_scales_with_dpi(host, monkeypatch):
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: 144)
    host.set_base(260)
    assert host.applied[-1][2:] == (390, 60)


def test_tab_is_not_saved_while_a_card_stretches_it(host, monkeypatch):
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: "SNAP")
    assert host._snapshot(77) == "SNAP"
    host.set_base(260)
    host.expand(308, 256)
    assert host._snapshot(77) is None
    host.collapse()
    assert host._snapshot(77) == "SNAP"


def test_tab_position_saves_under_its_own_key(host, monkeypatch):
    snap = SavedGeometry(10, 20, 260, 40, 96)
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: snap)
    assert host.tracker.flush() is True
    assert host._store.saved == {"tabWindow": snap.as_preference()}


def test_show_and_hide(host):
    host.show()
    assert host.visible is True and host.window.calls[-1] == "show"
    host.hide()
    assert host.visible is False and host.window.calls[-1] == "hide"


def test_hide_drops_a_open_card(host):
    host.set_base(260)
    host.expand(308, 256)
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
    host.set_base(260)
    host.restore_position({"x": 300, "y": 200, "width": 260, "height": 40, "dpi": 96})
    assert host.applied[-1] == (300, 200, 260, 40)
    host.restore_position(None)
    assert host.applied[-1] == (1920 - 260 - 16, 16, 260, 40)


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
    host.set_base(260)
    host.expand(308, 256)
    host.set_base(340)
    assert host.applied[-1][2:] == (340, 256)
    host.collapse()
    assert host.applied[-1][2:] == (340, 40)


# ---- the window region: only the strip and the card are visible --------------

def test_a_delayed_push_cannot_resize_the_host_for_an_older_payload(host):
    host.set_base(320, revision=2)
    host.set_base(240, revision=1)
    assert host._base == (320, tab_window.TAB_HEIGHT)


def test_collapsed_tab_is_one_rounded_rectangle():
    assert tab_window.shape_rects((260, 40), 1.0) == [(0, 0, 260, 40, 20)]
    assert tab_window.shape_rects((390, 60), 1.5) == [(0, 0, 390, 60, 30)]


def test_expanded_tab_shows_the_strip_and_the_card_only():
    rects = tab_window.shape_rects((308, 256), 1.0, card=(0, 308, 210), strip=(32, 260))
    assert rects == [(32, 0, 292, 40, 20), (0, 46, 308, 256, 14)]
    assert all(r[2] <= 308 and r[3] <= 256 for r in rects)


def test_expanded_tab_growing_upward_puts_the_strip_at_the_bottom():
    strip, card = tab_window.shape_rects((308, 256), 1.0, card=(0, 308, 210), up=True, strip=(32, 260))
    assert strip == (32, 216, 292, 256, 20)
    assert card == (0, 0, 308, 210, 14)


def test_region_scales_with_dpi_and_never_leaves_the_window():
    strip, card = tab_window.shape_rects((462, 384), 1.5, card=(0, 308, 210), strip=(32, 260))
    assert strip == (48, 0, 438, 60, 30)
    assert card == (0, 69, 462, 384, 21)
    wide = tab_window.shape_rects((400, 256), 1.0, card=(300, 300, 200))
    assert wide[1][2] == 400  # clipped to the window


def test_hover_sets_the_region_and_collapse_restores_the_strip(host, monkeypatch):
    regions = []
    monkeypatch.setattr(host, "_set_region", lambda rects: regions.append(rects))
    host.set_base(260)
    assert regions[-1] == [(0, 0, 260, 40, 20)]
    host.expand(308, 256, (0, 308, 210))
    assert regions[-1] == [(0, 0, 260, 40, 20), (0, 46, 308, 256, 14)]
    host.collapse()
    assert regions[-1] == [(0, 0, 260, 40, 20)]


def test_region_follows_the_growth_direction(host, monkeypatch):
    regions = []
    monkeypatch.setattr(host, "_set_region", lambda rects: regions.append(rects))
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: Rect(100, 980, 360, 1020))
    host.set_base(260)
    host.expand(308, 256, (0, 308, 210))
    assert regions[-1][0] == (0, 216, 260, 256, 20)
    assert regions[-1][1][1] == 0


def test_hiding_drops_the_card_shape(host, monkeypatch):
    regions = []
    monkeypatch.setattr(host, "_set_region", lambda rects: regions.append(rects))
    host.set_base(260)
    host.expand(308, 256, (0, 308, 210))
    host.hide()
    host.show()
    assert regions[-1] == [(0, 0, 260, 40, 20)]


def test_an_error_keeps_exactly_the_last_known_segments_dimmed():
    healthy = demo_payload()
    report = DemoClient().snapshot("today", "all")
    report = dict(report)
    report["providers"] = merge_provider_entries(
        report, hud=demo_hud(), hud_usage=demo_hud_usage(), today_report=report, week_report=report
    )
    model = TabModel()
    model.publish_report(report, today_report=report)
    model.publish_error("Can't reach your hub. Check that this computer is on your network.", "NET-001")
    failed = model.payload(dark=True)
    assert failed["primary"]["key"] == healthy["primary"]["key"]
    assert failed["primary"]["big"] == healthy["primary"]["big"]
    assert failed["more"] == healthy["more"]
    assert failed["spend"] == healthy["spend"]
    assert failed["stale"] is True and failed["error"]["code"] == "NET-001"
