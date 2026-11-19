"""Review round 1: ordering (D2-06/07), hover anchor (D2-02) and width budget (D2-03)."""

import math

import pytest

from spend_app import native_geometry, tab_window
from spend_app import provider_urgency as pu
from spend_app.native_geometry import Monitor, Rect
from spend_app.tab_window import TabBridge, TabHost, TabModel
from tests_spend.test_tab_surfaces import Win, make
from tests_spend.test_tab_window import (
    FakeStore,
    card,
    demo_payload,
    dry,
    iso,
    mon,
    payload,
    providers,
)

LEVEL_RANK = pu.LEVEL_RANK


# ---- D2-06 / D2-07: one order, stamped by Python -------------------------------

def forecast_helper(state, to_dry, before):
    return {"state": state, "hoursRemaining": to_dry, "hoursBeforeReset": before,
            "projectedRunDry": iso(to_dry) if to_dry is not None else None, "line": "x"}


def test_every_provider_gets_a_unique_display_rank_and_the_list_order_is_unchanged():
    from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
    from spend_app.wealthfront_window import merge_provider_entries

    report = DemoClient().snapshot("today", "all")
    items = merge_provider_entries(report, hud=demo_hud(), hud_usage=demo_hud_usage(),
                                   today_report=report, week_report=report)
    ranks = [p["displayRank"] for p in items]
    assert sorted(ranks) == list(range(len(items)))
    # the list keeps the user's order; only displayRank carries urgency
    assert [p["key"] for p in items][:3] == ["claude-code", "claude-code-work", "opencode-go"]
    by_rank = sorted(items, key=lambda p: p["displayRank"])
    levels = [LEVEL_RANK[p["urgency"]["level"]] for p in by_rank]
    assert levels == sorted(levels)
    assert [p["key"] for p in by_rank][:5] == ["claude-code", "codex", "opencode-go", "claude-code-work", "grok"]


def test_the_reviewers_out_ok_soon_case_orders_out_soon_ok():
    items = providers(
        card("claude-code", 100.0, dry(0.0, 40.0), resets=24.0),  # out
        card("claude-code-work", 5.0, None, label="Work", title="Claude Code · Work"),  # ok
        card("codex", 50.0, dry(1.5, 40.0)),  # soon
    )
    ordered = [p["key"] for p in sorted(items, key=lambda p: p["displayRank"])]
    assert ordered == ["claude-code", "codex", "claude-code-work"]
    levels = [p["urgency"]["level"] for p in sorted(items, key=lambda p: p["displayRank"])]
    assert levels == ["out", "soon", "ok"]


def test_safe_forecasts_sort_as_none_even_with_numeric_hours():
    # capacity_forecast._compared gives a safe forecast numeric hoursRemaining when the
    # projection falls after the reset; those hours are not urgency.
    grok = card("grok", 10.0, forecast_helper("safe", 2.0, None), resets=1.0)  # 90% left, 2h "dry"
    codex = card("codex", 80.0, forecast_helper("safe", 24.0, None), resets=12.0)  # 20% left, 24h
    items = providers(grok, codex)
    assert all(p["urgency"]["level"] == "ok" for p in items)
    ordered = [p["key"] for p in sorted(items, key=lambda p: p["displayRank"])]
    assert ordered == ["codex", "grok"]  # by remaining percentage, as for no forecast at all
    for p in items:
        assert pu._sort_hours(pu.focus_window(p)) == math.inf


def test_drying_windows_sort_by_their_hours_and_null_hours_sort_last():
    near = card("grok", 40.0, forecast_helper("dry", 6.0, 20.0))
    far = card("codex", 40.0, forecast_helper("dry", 30.0, 20.0))
    unknown = card("zcode", 40.0, {"state": "dry", "hoursRemaining": None, "hoursBeforeReset": 20.0,
                                   "projectedRunDry": None, "line": "x"})
    items = providers(far, unknown, near)
    ordered = [p["key"] for p in sorted(items, key=lambda p: p["displayRank"])]
    assert ordered == ["grok", "codex", "zcode"]


def test_the_tab_uses_the_stamped_rank_and_orders_a_hand_built_list_the_same_way():
    items = providers(card("grok", 10.0), card("codex", 90.0, dry(5.0, 20.0)))
    stamped = [p["key"] for p in tab_window.tab_providers(items)]
    for p in items:
        p.pop("displayRank")
    assert [p["key"] for p in tab_window.tab_providers(items)] == stamped == ["codex", "grok"]


# ---- D2-02: the hover card never drifts the tab ---------------------------------

BIG = [Monitor(Rect(0, 0, 3840, 2160), Rect(0, 0, 3840, 2120), primary=True)]


@pytest.fixture
def placed(monkeypatch):
    """A tab whose rectangle really changes after every placement."""
    host = TabHost(Win(), store=FakeStore(), hwnd_box=[77])
    state = {"rect": Rect(100, 0, 360, 40), "dpi": 96}

    def apply(x, y, width, height):
        left = state["rect"].left if x is None else x
        top = state["rect"].top if y is None else y
        state["rect"] = Rect(left, top, left + width, top + height)

    monkeypatch.setattr(host, "_apply", apply)
    monkeypatch.setattr(host, "_set_region", lambda rects: None)
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: state["rect"])
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: state["dpi"])
    monkeypatch.setattr(native_geometry, "enum_monitors", lambda: BIG)
    return host, state


def put(host, state, dpi, *, top):
    state["dpi"] = dpi
    scale = dpi / 96
    state["rect"] = Rect(100, top, 100 + round(260 * scale), top + round(40 * scale))
    host.set_base(260)
    return state["rect"].top, scale


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_repeated_expands_and_segment_switches_keep_one_anchor_upward(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=2100 - round(40 * scale_for(dpi)))
    base_h = round(40 * scale)
    result = host.expand(308, 256, (0, 308, 210))
    assert result == {"up": True}
    first = state["rect"]
    assert first.bottom == anchor_top + base_h  # the strip stays where it was
    assert host.expand(308, 256, (0, 308, 210)) == {"up": True}  # a push or poll
    assert state["rect"] == first
    assert host.expand(308, 340, (0, 308, 294)) == {"up": True}  # expanded rest list, taller card
    assert state["rect"].bottom == anchor_top + base_h
    assert state["rect"].height == round(340 * scale)
    assert host.expand(308, 256, (0, 308, 210)) == {"up": True}
    assert state["rect"] == first
    host.collapse()
    assert (state["rect"].top, state["rect"].height) == (anchor_top, base_h)


def scale_for(dpi):
    return dpi / 96


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_a_downward_card_keeps_the_anchor_across_repeated_expands(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=200)
    for height in (256, 256, 340, 256):
        assert host.expand(308, height, (0, 308, height - 46)) == {"up": False}
        assert state["rect"].top == anchor_top
    host.collapse()
    assert state["rect"].top == anchor_top


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_hiding_from_an_upward_card_restores_the_anchor_for_the_next_show(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=2100 - round(40 * scale_for(dpi)))
    host.expand(308, 256, (0, 308, 210))
    assert state["rect"].top != anchor_top  # displaced while the card is up
    host.hide()
    assert (state["rect"].top, state["rect"].height) == (anchor_top, round(40 * scale))
    host.window.calls.clear()
    host.show()
    assert state["rect"].top == anchor_top and host.window.calls == ["show"]
    # and the next card opens upward from the same spot again
    assert host.expand(308, 256, (0, 308, 210)) == {"up": True}
    assert state["rect"].bottom == anchor_top + round(40 * scale)


def test_a_refresh_that_changes_the_width_mid_card_keeps_the_anchor(placed):
    host, state = placed
    anchor_top, _ = put(host, state, 96, top=2100 - 40)
    host.expand(308, 256, (0, 308, 210))
    bottom = state["rect"].bottom
    host.set_base(340)
    assert state["rect"].bottom == bottom and state["rect"].width == 340
    host.collapse()
    assert (state["rect"].top, state["rect"].width) == (anchor_top, 340)


def test_the_tracker_never_saves_a_displaced_position(placed, monkeypatch):
    host, state = placed
    put(host, state, 96, top=2100 - 40)
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: "SNAP")
    host.expand(308, 256, (0, 308, 210))
    assert host._snapshot(77) is None
    host.collapse()
    assert host._snapshot(77) == "SNAP"


# ---- D2-03: the corner pill always fits the monitor --------------------------

def many_urgent(count):
    return [card(f"claude-code-{i}", 40.0, dry(20.0 + i, 50.0), title=f"Provider {i}", label=f"P{i}")
            for i in range(count)]


def error_model(providers_list, *, code="NET-001", spend_report=None):
    model = TabModel()
    report = dict(spend_report) if isinstance(spend_report, dict) else {}
    report["providers"] = providers_list
    model.publish_report(report, today_report=spend_report)
    model.publish_error("Can't reach your hub. Check that this computer is on your network.", code)
    return model


FIT_MATRIX = [
    (1920, 96), (1366, 144), (1920, 192), (1366, 192), (1024, 96), (2560, 192), (3440, 96),
    (1280, 288), (800, 192), (1024, 192), (640, 96),
]


def demo_report_and_providers():
    from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
    from spend_app.wealthfront_window import merge_provider_entries

    report = DemoClient().snapshot("today", "all")
    items = merge_provider_entries(report, hud=demo_hud(), hud_usage=demo_hud_usage(),
                                   today_report=report, week_report=report)
    return report, items


@pytest.mark.parametrize("work_px, dpi", FIT_MATRIX)
def test_the_demo_with_an_error_chip_fits_every_monitor_and_scale(work_px, dpi):
    report, items = demo_report_and_providers()
    model = error_model(items, spend_report=report)
    scale = dpi / 96
    budget = work_px / scale - tab_window.BUDGET_MARGIN
    assert budget >= 240
    data = model.payload(dark=True, budget=budget)
    assert data["layout"]["width"] <= budget
    assert data["layout"]["width"] * scale <= work_px
    assert data["error"]["code"] == "NET-001" and data["stale"] is True
    # nothing is lost: #1 primary + remaining in more + every urgent in overview.highlighted
    urgent_keys = {"claude-code", "codex", "opencode-go"}
    shown = {data["primary"]["key"]} if data["primary"] else set()
    listed = {i["key"] for i in (data["more"] or {"items": []})["items"]}
    highlighted = {s["key"] for s in data["overview"]["highlighted"]}
    assert urgent_keys <= highlighted
    assert len(shown | listed) == len(tab_window.tab_providers(items))


def sweep_sets():
    from tests_spend.test_tab_window import card as make_card

    report, items = demo_report_and_providers()
    idle = [
        {"providerKey": "claude-code", "title": "Claude Code", "plan": "Max",
         "rows": [{"limitKey": "5h", "label": "5h window \u2014 Waiting", "unavailable": True}]},
        make_card("codex", 40.0, None, resets=100.0, title="Codex"),
        {"providerKey": "openrouter", "title": "OpenRouter", "isPayg": True, "fundsRemainingUsd": 23.4, "rows": []},
    ]
    return {
        "demo+error": lambda: error_model(items, spend_report=report),
        "demo+error without code": lambda: error_model(items, code=None, spend_report=report),
        "many urgent+error": lambda: error_model(providers(*many_urgent(9))),
        "idle+error": lambda: error_model(providers(*idle)),
        "demo": lambda: _healthy(items, spend_report=report),
        "empty": lambda: _healthy([]),
        "loading": lambda: TabModel(),
        "error first": lambda: _error_only(),
        "error first without code": lambda: _error_only(code=None),
    }


def _healthy(providers_list, spend_report=None):
    model = TabModel()
    report = dict(spend_report) if isinstance(spend_report, dict) else {}
    report["providers"] = providers_list
    model.publish_report(report, today_report=spend_report)
    return model


def _error_only(code="APP-901"):
    model = TabModel()
    model.publish_error("BURNRATE's background service isn't running. Restart the app.", code)
    return model


def test_nothing_is_ever_cut_off_at_any_budget_of_240_or_more():
    for name, build in sweep_sets().items():
        model = build()
        for budget in range(240, 2001, 10):
            data = model.payload(dark=False, budget=budget)
            assert data["layout"]["width"] <= budget, (name, budget, data["layout"])
            assert data["layout"]["cardWidth"] <= budget, (name, budget)


def test_the_fit_ladder_shrinks_chip_then_status_then_spend_on_narrow_budgets():
    report, items = demo_report_and_providers()
    model = error_model(items, spend_report=report)
    roomy = model.payload(dark=False, budget=2000)
    assert roomy["layout"]["mode"] == {
        "chip": "code", "showSpend": True, "showStatus": True, "compact": False, "tight": False
    }
    # Narrower than code chip allows -> chip becomes icon first while spend & status remain
    mid = model.payload(dark=False, budget=roomy["layout"]["width"] - 20)
    assert mid["layout"]["mode"]["chip"] == "icon" and mid["layout"]["mode"]["showSpend"] is True
    # Tight 240px budget drops spend chip from the resting pill (stays in hover card!)
    narrow = model.payload(dark=False, budget=240)
    assert narrow["layout"]["width"] <= 240
    assert narrow["layout"]["mode"]["chip"] == "icon"
    assert narrow["layout"]["mode"]["showSpend"] is False
    assert narrow["spend"]["exact"] == "$127.34"  # still available in the hover card
    assert narrow["primary"]["key"] == "claude-code" and narrow["more"]["count"] == 9


def test_a_chip_without_a_code_shows_only_its_icon_when_stale():
    report, items = demo_report_and_providers()
    model = error_model(items, code=None, spend_report=report)
    data = model.payload(dark=False, budget=700)
    assert data["layout"]["mode"]["chip"] == "icon"
    assert data["error"]["text"]


def test_the_hover_card_never_outgrows_the_budget():
    report, items = demo_report_and_providers()
    model = error_model(items, spend_report=report)
    assert model.payload(dark=False, budget=2000)["layout"]["cardWidth"] == tab_window.CARD_WIDTH
    assert model.payload(dark=False, budget=250)["layout"]["cardWidth"] == 250


def demo_providers_list():
    _, items = demo_report_and_providers()
    return items


def test_out_and_soon_hidden_in_more_pill_still_count_on_the_badge_and_turn_pill_red():
    cards = [card(f"claude-code-{i}", 100.0, dry(0.0, 40.0), title=f"Provider {i}", label=f"P{i}", resets=24.0)
             for i in range(8)]
    model = TabModel()
    model.publish_report({"providers": providers(*cards)})
    data = model.payload(dark=False, budget=640)
    assert data["more"]["count"] == 7 and data["more"]["severity"] == "soon"
    assert data["urgentCount"] == 8
    assert data["layout"]["width"] <= 640


def host_with_monitor(monkeypatch, work_width, dpi, rect=Rect(100, 20, 360, 60)):
    host = TabHost(Win(), store=FakeStore(), hwnd_box=[77])
    monitors = [Monitor(Rect(0, 0, work_width, 1200), Rect(0, 0, work_width, 1160), primary=True)]
    monkeypatch.setattr(native_geometry, "enum_monitors", lambda: monitors)
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: dpi)
    monkeypatch.setattr(native_geometry, "window_rect", lambda hwnd: rect)
    monkeypatch.setattr(host, "_apply", lambda *a: None)
    monkeypatch.setattr(host, "_set_region", lambda rects: None)
    return host, monitors


@pytest.mark.parametrize("work, dpi, logical", [(1920, 96, 1904), (1366, 144, 894), (1920, 192, 944), (1366, 192, 667)])
def test_the_host_budget_is_the_monitors_logical_work_width(monkeypatch, work, dpi, logical):
    host, _ = host_with_monitor(monkeypatch, work, dpi)
    assert host.logical_budget() == pytest.approx(logical, abs=1)


def test_the_bridge_builds_its_payload_against_the_hosts_budget(monkeypatch):
    bridge, _, tab, surfaces, *_ = make()
    host, _ = host_with_monitor(monkeypatch, 1366, 144)
    monkeypatch.setattr(surfaces, "tab", host)
    bridge.fetch_usage("today", "all")
    data = TabBridge(bridge, bridge._tab_model, surfaces).build_payload()
    assert data["layout"]["budget"] == pytest.approx(894, abs=1)
    assert data["layout"]["width"] <= 894


def test_moving_to_a_narrower_monitor_asks_for_a_new_layout(monkeypatch):
    host, monitors = host_with_monitor(monkeypatch, 3440, 96)
    calls = []
    host.on_reflow = lambda: calls.append(host.logical_budget())
    assert host.logical_budget() > 3000
    host.check_budget()
    assert calls == []  # same monitor: nothing to redo
    monitors[0] = Monitor(Rect(0, 0, 1280, 1024), Rect(0, 0, 1280, 984), primary=True)
    host.check_budget()  # dragged to a 1280 px monitor
    assert len(calls) == 1 and calls[0] < 1280
    host.check_budget()
    assert len(calls) == 1  # the new budget is now the known one


def test_a_dpi_change_reflows_and_does_not_recurse(monkeypatch):
    host, _ = host_with_monitor(monkeypatch, 1920, 96)
    seen = []

    def reflow():
        seen.append(1)
        host.fit()  # a reflow that triggers another fit must not loop

    host.on_reflow = reflow
    host.logical_budget()
    monkeypatch.setattr(native_geometry, "window_dpi", lambda hwnd: 192)
    host.fit()
    assert seen == [1]


@pytest.mark.parametrize("work_px, dpi", FIT_MATRIX)
def test_the_physical_window_never_exceeds_the_physical_work_area(monkeypatch, work_px, dpi):
    bridge, _, tab, surfaces, *_ = make()
    host, monitors = host_with_monitor(monkeypatch, work_px, dpi, rect=Rect(0, 20, 260, 60))
    monkeypatch.setattr(surfaces, "tab", host)
    report, items = demo_report_and_providers()
    report = dict(report, providers=items)
    bridge._tab_model.publish_report(report, today_report=report)
    bridge._tab_model.publish_error("Can't reach your hub. Check that this computer is on your network.", "NET-001")
    data = TabBridge(bridge, bridge._tab_model, surfaces).build_payload()
    host.set_base(data["layout"]["width"])
    physical_width, _ = host._target_size()
    assert physical_width <= monitors[0].work.width  # compared with the real work area
    host.expand(max(data["layout"]["width"], data["layout"]["cardWidth"]), 256, (0, data["layout"]["cardWidth"], 210))
    assert host._target_size()[0] <= monitors[0].work.width


# ---- the "+N" pill severity follows what is inside it -----------------------

def out_cards(count):
    return [card(f"claude-code-{i}", 100.0, dry(0.0, 40.0), title=f"Provider {i}", label=f"P{i}", resets=24.0)
            for i in range(count)]


def test_more_pill_severity_is_red_only_when_another_out_or_soon_is_hidden():
    warn_model = TabModel()
    warn_model.publish_report({"providers": providers(*many_urgent(5))})
    assert warn_model.payload(dark=False)["more"]["severity"] == "warn"
    red_model = TabModel()
    red_model.publish_report({"providers": providers(*out_cards(4))})
    assert red_model.payload(dark=False)["more"]["severity"] == "soon"
    ok_model = TabModel()
    ok_model.publish_report({"providers": providers(*[card(f"k{i}", 10.0 + i, resets=100.0, title=f"P{i}") for i in range(5)])})
    assert ok_model.payload(dark=False)["more"]["severity"] == "ok"
