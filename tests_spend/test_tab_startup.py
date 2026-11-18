"""The startup bug report: a tab that starts off-screen, a shrink that fails, a second launch."""

import logging

import pytest

from spend_app import native_geometry
from spend_app.native_geometry import Monitor, Rect
from tests_spend.test_tab_review import placed  # noqa: F401 -- the "placed" fixture
from tests_spend.test_tab_surfaces import SurfaceController, make
from tests_spend.test_tab_transitions import wait_for  # noqa: F401


OWNER_MONITOR = [Monitor(Rect(0, 0, 2560, 1080), Rect(0, 0, 2560, 1032), primary=True)]  # one 2560x1080 screen


@pytest.fixture
def owner_screen(placed, monkeypatch):  # noqa: F811
    host, state = placed
    monkeypatch.setattr(native_geometry, "enum_monitors", lambda: OWNER_MONITOR)
    return host, state


def test_a_tab_that_starts_off_screen_is_pulled_onto_the_monitor_before_it_is_shown(owner_screen):
    # The tab had been placed for a wider (3440 px) screen, which then became 2560 px:
    # x=2574 is past the right edge. Showing it must clamp it first.
    host, state = owner_screen
    state["rect"] = Rect(2574, 16, 2574 + 866, 76)
    host.set_base(866)
    host.show()
    assert state["rect"].left == 2560 - 866 and state["rect"].top == 16
    assert state["rect"].right <= 2560 and host.visible is True


def test_a_saved_position_from_another_screen_layout_is_clamped_at_launch(owner_screen):
    host, state = owner_screen
    host.set_base(866)
    host.restore_position({"x": 2574, "y": 16, "width": 866, "height": 60, "dpi": 96})
    assert state["rect"].left == 2560 - 866
    host.restore_position({"x": 9000, "y": 9000, "width": 866, "height": 60, "dpi": 96})
    assert state["rect"].right <= 2560 and state["rect"].bottom <= 1032


def test_launch_with_the_owners_prefs_shows_the_full_window_and_keeps_the_tab_hidden(tmp_path):
    import json

    prefs = tmp_path / "native-window.json"
    prefs.write_text(json.dumps({
        "period": "today", "topmost": True, "autoFade": True, "idleOpacity": 0.38, "theme": "light",
        "closeToTray": True, "lowQuotaAlerts": True, "providerOrder": [], "surface": "full",
        "window": {"x": 1334, "y": 203, "width": 440, "height": 640, "dpi": 96},
    }), encoding="utf-8")
    bridge, host, tab, surfaces, main_win, tab_win, _ = make(tmp_path, controller=SurfaceController)
    surfaces.restore_on_launch()
    assert surfaces.flush(3)
    assert tab.visible is False and tab_win.calls == [] and bridge.surface == "full"
    surfaces.show_tab()
    assert surfaces.flush(3)
    assert tab.visible is True and main_win.visible is False  # the click works with these prefs
    assert json.loads(prefs.read_text(encoding="utf-8"))["surface"] == "tab"
    assert json.loads(prefs.read_text(encoding="utf-8"))["topmost"] is True  # other prefs are untouched


def test_a_failed_shrink_is_logged_with_its_reason_and_leaves_one_window(caplog):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)

    def explode():
        raise OSError("no window station")

    tab_win.show = explode
    with caplog.at_level(logging.INFO, logger="spend_app"):
        bridge._surfaces = surfaces
        assert bridge.show_tab() == {"ok": True}
        assert surfaces.flush(5)
    text = "\n".join(f"{r.levelname} {r.getMessage()} {r.exc_text or ''}" for r in caplog.records)
    assert "Shrink to tab requested" in text
    assert "Tab show failed" in text and "Shrink to tab failed; keeping the full window" in text
    assert main_win.visible is True and tab_win.visible is False and tab.visible is False
    assert bridge.surface == "full"


@pytest.mark.parametrize("raises", [False, True])
def test_saved_tab_restore_falls_back_to_full_and_next_launch_recovers(tmp_path, raises):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(tmp_path, surface="tab")
    main_win.visible = False

    def failed_show():
        if raises:
            raise OSError("tab show failed")

    tab_win.show = failed_show
    # Windows can report that a show request returned while the form remains hidden.
    tab.is_shown = lambda: tab_win.visible
    surfaces.restore_on_launch()
    assert main_win.visible and not tab_win.visible and not tab.visible
    assert bridge.surface == "full"

    next_bridge, _, _, next_surfaces, next_main, next_tab, _ = make(tmp_path)
    next_surfaces.restore_on_launch()
    assert next_bridge.surface == "full"
    assert next_main.visible and not next_tab.visible and next_tab.calls == []


def test_invisible_full_fallback_never_commits_and_tray_can_retry(tmp_path):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(tmp_path, surface="tab")
    main_win.visible = False
    show_main, show_tab = main_win.show, tab_win.show
    main_win.show = tab_win.show = lambda: None
    tab.is_shown = lambda: tab_win.visible

    surfaces.restore_on_launch()
    assert not main_win.visible and not tab_win.visible
    assert bridge.surface == "tab"

    main_win.show, tab_win.show = show_main, show_tab
    surfaces.toggle()
    assert tab_win.visible and not main_win.visible
    surfaces.show_full()
    assert main_win.visible and not tab_win.visible and bridge.surface == "full"


def test_a_second_launch_is_logged_so_a_surprise_full_window_can_be_explained(caplog, monkeypatch):
    import sys
    import types

    from spend_app import native_activation, wealthfront_window as ww
    from spend_app.native_desktop import DemoClient

    fake = types.ModuleType("webview")
    fake.windows = []
    created = []

    class Event:
        def __iadd__(self, handler):
            return self

    def create_window(title, **kwargs):
        window = types.SimpleNamespace(
            title=title, native=None,
            events=types.SimpleNamespace(before_show=Event(), shown=Event(), loaded=Event(), closed=Event(),
                                         moved=Event(), resized=Event()),
        )
        created.append(window)
        fake.windows.append(window)
        return window

    captured = {}

    class Listener:
        def __init__(self, on_activate, **kwargs):
            captured["callback"] = on_activate

        def start(self):
            return True

        def stop(self):
            pass

    fake.create_window = create_window
    fake.start = lambda **kw: None
    monkeypatch.setitem(sys.modules, "webview", fake)
    monkeypatch.setattr(ww, "ActivationListener", Listener)
    monkeypatch.setattr(ww, "_focus_existing_window", lambda title: False)
    monkeypatch.setattr(ww, "configure_desktop_logging", lambda *a, **k: None)
    monkeypatch.setattr(ww.DesktopBridge, "_migrate_legacy_secrets", lambda self: None)
    ww.run_webview(DemoClient(), demo=False)
    assert "callback" in captured
    with caplog.at_level(logging.INFO, logger="spend_app"):
        captured["callback"]()
    assert any("A second launch asked this process to open the full window" in r.getMessage() for r in caplog.records)
