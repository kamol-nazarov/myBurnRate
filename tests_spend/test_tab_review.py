"""Review round 1: ordering (D2-06/07), hover anchor (D2-02) and width budget (D2-03)."""

import math

import pytest

from spend_app import native_geometry, tab_window
from spend_app import provider_urgency as pu
from spend_app.native_geometry import Monitor, Rect
from spend_app.tab_window import TabBridge, TabHost, TabModel, plan_segments
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
    state = {"rect": Rect(100, 0, 1000, 60), "dpi": 96}

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
    state["rect"] = Rect(100, top, 100 + round(900 * scale), top + round(60 * scale))
    host.set_base(900)
    return state["rect"].top, scale


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_repeated_expands_and_segment_switches_keep_one_anchor_upward(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=2100 - round(60 * scale_for(dpi)))
    base_h = round(60 * scale)
    result = host.expand(900, 330, (60, 300, 250))
    assert result == {"up": True}
    first = state["rect"]
    assert first.bottom == anchor_top + base_h  # the strip stays where it was
    assert host.expand(900, 330, (60, 300, 250)) == {"up": True}  # a push or poll
    assert state["rect"] == first
    assert host.expand(900, 250, (180, 300, 170)) == {"up": True}  # another segment, shorter card
    assert state["rect"].bottom == anchor_top + base_h
    assert state["rect"].height == round(250 * scale)
    assert host.expand(900, 330, (60, 300, 250)) == {"up": True}
    assert state["rect"] == first
    host.collapse()
    assert (state["rect"].top, state["rect"].height) == (anchor_top, base_h)


def scale_for(dpi):
    return dpi / 96


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_a_downward_card_keeps_the_anchor_across_repeated_expands(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=200)
    for height in (330, 330, 220, 330):
        assert host.expand(900, height, (60, 300, height - 66)) == {"up": False}
        assert state["rect"].top == anchor_top
    host.collapse()
    assert state["rect"].top == anchor_top


@pytest.mark.parametrize("dpi", [96, 144, 192])
def test_hiding_from_an_upward_card_restores_the_anchor_for_the_next_show(placed, dpi):
    host, state = placed
    anchor_top, scale = put(host, state, dpi, top=2100 - round(60 * scale_for(dpi)))
    host.expand(900, 330, (60, 300, 250))
    assert state["rect"].top != anchor_top  # displaced while the card is up
    host.hide()
    assert (state["rect"].top, state["rect"].height) == (anchor_top, round(60 * scale))
    host.window.calls.clear()
    host.show()
    assert state["rect"].top == anchor_top and host.window.calls == ["show"]
    # and the next card opens upward from the same spot again
    assert host.expand(900, 330, (60, 300, 250)) == {"up": True}
    assert state["rect"].bottom == anchor_top + round(60 * scale)


def test_a_refresh_that_changes_the_width_mid_card_keeps_the_anchor(placed):
    host, state = placed
    anchor_top, _ = put(host, state, 96, top=2100 - 60)
    host.expand(900, 330, (60, 300, 250))
    bottom = state["rect"].bottom
    host.set_base(1100)
    assert state["rect"].bottom == bottom and state["rect"].width == 1100
    host.collapse()
    assert (state["rect"].top, state["rect"].width) == (anchor_top, 1100)


def test_the_tracker_never_saves_a_displaced_position(placed, monkeypatch):
    host, state = placed
    put(host, state, 96, top=2100 - 60)
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: "SNAP")
    host.expand(900, 330, (60, 300, 250))
    assert host._snapshot(77) is None
    host.collapse()
    assert host._snapshot(77) == "SNAP"


# ---- D2-03: the tab always fits the monitor ----------------------------------

def many_urgent(count):
    return [card(f"claude-code-{i}", 40.0, dry(20.0 + i, 50.0), title=f"Provider {i}", label=f"P{i}")
            for i in range(count)]


def test_the_budget_folds_calm_providers_until_the_strip_fits():
    model = TabModel()
    model.publish_report({"providers": providers(*[card(f"k{i}", 10.0 + i, resets=100.0, title=f"P{i}") for i in range(5)])})
    roomy = model.payload(dark=False, budget=2000)
    assert roomy["layout"]["width"] > 600 and roomy["fine"] is None
    fitted = model.payload(dark=False, budget=560)
    assert fitted["layout"]["width"] <= 560
    assert len(fitted["segments"]) < 5 and fitted["fine"]["count"] == 5 - len(fitted["segments"])
    assert fitted["urgentMore"] is None


@pytest.mark.parametrize(
    "work_px, dpi",
    [(1920, 96), (1366, 144), (1920, 192), (1366, 192), (1024, 96), (2560, 192), (3440, 96)],
)
def test_the_demo_with_an_error_chip_fits_every_monitor_and_scale(work_px, dpi):
    model = TabModel()
    model.publish_report({"providers": demo_providers_list()})
    model.publish_error("Can't reach your hub. Check that this computer is on your network.", "NET-001")
    budget = work_px / (dpi / 96) - tab_window.BUDGET_MARGIN
    data = model.payload(dark=True, budget=budget)
    limit = max(budget, tab_window.MIN_BUDGET)
    assert data["layout"]["width"] <= limit
    assert data["error"]["code"] == "NET-001" and data["stale"] is True
    # nothing urgent is ever lost: every urgent provider is shown or listed in the pill
    urgent_keys = {"claude-code", "codex", "opencode-go"}
    shown = {s["key"] for s in data["segments"]}
    listed = {i["key"] for i in (data["urgentMore"] or {"items": []})["items"]}
    assert urgent_keys <= shown | listed


def demo_providers_list():
    from spend_app.native_desktop import DemoClient, demo_hud, demo_hud_usage
    from spend_app.wealthfront_window import merge_provider_entries

    report = DemoClient().snapshot("today", "all")
    return merge_provider_entries(report, hud=demo_hud(), hud_usage=demo_hud_usage(),
                                  today_report=report, week_report=report)


def test_urgent_providers_that_alone_do_not_fit_become_a_red_urgent_pill():
    model = TabModel()
    model.publish_report({"providers": providers(*many_urgent(9))})
    data = model.payload(dark=False, budget=700)
    shown = data["segments"]
    more = data["urgentMore"]
    assert data["layout"]["width"] <= 700
    assert shown and more and len(shown) + more["count"] == 9
    assert [s["key"] for s in shown] + [i["key"] for i in more["items"]] == [f"claude-code-{i}" for i in range(9)]
    assert all(i["level"] == "warn" for i in more["items"])
    assert data["fine"] is None
    assert data["urgentCount"] == 0  # warn is "not about to run out"; the badge counts out and soon


def test_out_and_soon_hidden_in_the_pill_still_count_on_the_badge():
    cards = [card(f"claude-code-{i}", 100.0, dry(0.0, 40.0), title=f"Provider {i}", resets=24.0, label=f"P{i}")
             for i in range(8)]
    model = TabModel()
    model.publish_report({"providers": providers(*cards)})
    data = model.payload(dark=False, budget=640)
    assert data["urgentMore"] and data["urgentCount"] == 8
    assert data["layout"]["width"] <= 640


def test_calm_providers_fold_before_any_urgent_one_is_dropped():
    cards = many_urgent(4) + [card(f"k{i}", 10.0, resets=100.0, title=f"Calm {i}") for i in range(3)]
    model = TabModel()
    model.publish_report({"providers": providers(*cards)})
    data = model.payload(dark=False, budget=800)
    assert data["layout"]["width"] <= 800
    assert [s["level"] for s in data["segments"]].count("warn") == 4
    assert data["urgentMore"] is None and data["fine"]["count"] >= 1


def test_plan_segments_without_a_budget_keeps_the_cap_rule():
    segs = [{"level": lv, "key": str(i), "width": 124} for i, lv in enumerate(["soon", "ok", "ok", "ok", "ok", "ok"])]
    shown, folded, hidden = plan_segments(segs)
    assert len(shown) == 5 and len(folded) == 1 and hidden == []


def test_the_pill_is_never_left_empty_and_the_window_is_never_wider_than_its_plan():
    for count in range(1, 12):
        model = TabModel()
        model.publish_report({"providers": providers(*many_urgent(count))})
        data = model.payload(dark=False, budget=700)
        assert data["layout"]["width"] <= 700
        if data["urgentMore"]:
            assert data["urgentMore"]["count"] >= 1


def host_with_monitor(monkeypatch, work_width, dpi, rect=Rect(100, 20, 900, 80)):
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
