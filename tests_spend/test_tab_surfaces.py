"""Switching between the full window and the tab: bridge, controller, host, launch."""

import json
import sys
import threading
import time
import types

import pytest

from spend_app import tab_window
from spend_app.native_desktop import DemoClient, DesktopError
from spend_app.native_prefs import PreferenceStore
from spend_app.tab_window import SurfaceController, TabBridge, TabHost
from spend_app import wealthfront_window as ww
from spend_app.wealthfront_window import DesktopBridge, WindowHost


def wait_for(predicate, timeout=3.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if predicate():
            return True
        time.sleep(0.01)
    return False


class Win:
    def __init__(self, visible=True):
        self.calls = []
        self.visible = visible

    def show(self):
        self.calls.append("show")
        self.visible = True

    def hide(self):
        self.calls.append("hide")
        self.visible = False

    def destroy(self):
        self.calls.append("destroy")

    def minimize(self):
        self.calls.append("minimize")

    def evaluate_js(self, script):
        self.calls.append(("js", script))


class Tracker:
    def flush(self):
        pass

    def disable(self):
        pass


class Opacity:
    def wake(self):
        pass

    def stop(self):
        pass


class SyncSurfaces(SurfaceController):
    """The real queue and worker, but every request returns once it is applied."""

    def _request(self, action):
        super()._request(action)
        self.flush()


def make(tmp_path=None, *, surface=None, demo=True, tray=True, controller=SyncSurfaces):
    prefs = None
    if tmp_path is not None:
        prefs = tmp_path / "native-window.json"
        if surface:
            prefs.write_text(json.dumps({"surface": surface}), encoding="utf-8")
    bridge = DesktopBridge(DemoClient(), preferences=prefs, demo=demo)
    main_win, tab_win = Win(), Win(visible=False)
    host = WindowHost(
        main_win, bridge, [0], opacity=Opacity(), tracker=Tracker(), saved_geometry=None,
        tray_enabled=False, alerts_enabled=False, store=PreferenceStore(prefs), icon_path=None,
    )
    host.tray = type("T", (), {"available": tray, "dispose": lambda self: None})() if tray else None
    host.is_visible = lambda: main_win.visible
    bridge.attach_host(host)
    tab = TabHost(tab_win, store=PreferenceStore(prefs), persist=False)
    surfaces = controller(host, tab, bridge, PreferenceStore(prefs))
    host.surfaces = surfaces
    bridge.attach_surfaces(surfaces)
    return bridge, host, tab, surfaces, main_win, tab_win, prefs


# ---- show_tab / show_full ---------------------------------------------

def test_show_tab_hides_the_full_window_and_shows_the_tab():
    bridge, _, tab, _, main_win, tab_win, _ = make()
    assert bridge.show_tab() == {"ok": True}
    assert main_win.calls == ["hide"] and tab_win.calls == ["show"]
    assert tab.visible is True and bridge.surface == "tab"


def test_open_full_shows_the_full_window_and_hides_the_tab():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make()
    bridge.show_tab()
    surfaces.show_full()
    assert main_win.calls == ["hide", "show"] and tab_win.calls == ["show", "hide"]
    assert tab.visible is False and bridge.surface == "full"


def test_show_tab_without_a_tab_falls_back_to_minimize():
    bridge = DesktopBridge(DemoClient(), demo=True)
    bridge._window = Win()
    assert bridge.show_tab() == {"ok": False, "fallback": "minimize"}
    assert bridge._window.calls == ["minimize"]


def test_the_last_surface_is_remembered_across_launches(tmp_path):
    bridge, _, _, surfaces, *_ , prefs = make(tmp_path)
    surfaces.show_tab()
    assert json.loads(prefs.read_text(encoding="utf-8"))["surface"] == "tab"
    assert DesktopBridge(DemoClient(), preferences=prefs, demo=True).surface == "tab"
    surfaces.show_full()
    assert json.loads(prefs.read_text(encoding="utf-8"))["surface"] == "full"
    assert DesktopBridge(DemoClient(), preferences=prefs, demo=True).surface == "full"


def test_restore_on_launch_shows_the_tab_without_stealing_focus(tmp_path):
    _, _, tab, surfaces, main_win, tab_win, _ = make(tmp_path, surface="tab")
    surfaces.restore_on_launch()
    assert tab.visible is True and tab_win.calls == ["show"]
    assert main_win.calls == []  # it was created hidden; nothing to show


def test_restore_on_launch_leaves_a_full_launch_alone(tmp_path):
    _, _, tab, surfaces, main_win, tab_win, _ = make(tmp_path, surface="full")
    surfaces.restore_on_launch()
    assert tab.visible is False and tab_win.calls == [] and main_win.calls == []


def test_tray_toggle_hides_whatever_is_up_and_brings_back_the_remembered_surface():
    bridge, host, _, surfaces, main_win, tab_win, _ = make()
    surfaces.show_tab()
    host.toggle()  # tab up -> hidden
    assert tab_win.calls[-1] == "hide"
    host.toggle()  # nothing up, remembered surface is the tab
    assert tab_win.calls[-1] == "show"
    surfaces.show_full()
    host.toggle()  # full up -> hidden
    assert main_win.calls[-1] == "hide"
    host.toggle()
    assert main_win.calls[-1] == "show"


def test_tray_open_and_show_tab_handlers(monkeypatch):
    bridge, host, _, surfaces, main_win, tab_win, _ = make()
    host.show_tab()
    assert tab_win.calls == ["show"]
    host.open_full()
    assert main_win.calls[-1] == "show" and tab_win.calls[-1] == "hide"
    host.open_settings("general")
    assert main_win.calls[-1] == "show"


def test_tray_menu_items_and_handlers():
    from spend_app.native_tray import TrayController

    fired = []
    tray = TrayController(
        None, None, on_toggle=lambda: fired.append("toggle"), on_show=lambda: fired.append("show"),
        on_refresh=lambda: None, on_settings=lambda: None, on_quit=lambda: None, on_tab=lambda: fired.append("tab"),
    )
    tray._fire("tab")
    tray._fire("show")
    assert fired == ["tab", "show"]
    plain = TrayController(None, None, on_toggle=lambda: None, on_show=lambda: fired.append("fallback"),
                           on_refresh=lambda: None, on_settings=lambda: None, on_quit=lambda: None)
    plain._fire("tab")
    assert fired[-1] == "fallback"
    import inspect

    from spend_app import native_tray

    source = inspect.getsource(native_tray.TrayController.create)
    assert '"Open BURNRATE"' in source and '"Show tab"' in source


def test_closing_the_tab_parks_in_the_tray_or_quits():
    _, host, tab, surfaces, main_win, tab_win, _ = make()
    host.can_hide_to_tray = lambda: True  # demo runs quit on close
    surfaces.show_tab()
    surfaces.close_tab()
    assert tab_win.calls[-1] == "hide"
    _, host2, _, surfaces2, main_win2, tab_win2, _ = make(tray=False)
    surfaces2.close_tab()
    assert wait_for(lambda: "destroy" in main_win2.calls)
    assert "destroy" in tab_win2.calls  # the tab goes with the app


def test_quit_takes_the_tab_down_too():
    bridge, host, tab, _, main_win, tab_win, _ = make()
    bridge.quit_app()
    assert wait_for(lambda: "destroy" in main_win.calls)
    assert tab_win.calls.count("destroy") == 1 and tab.quitting is True
    surfaces_after = host.surfaces
    surfaces_after.show_tab()  # nothing is shown once the app is closing
    assert tab_win.calls.count("show") == 0


# ---- the model follows the main bridge -------------------------------------

def test_a_refresh_feeds_the_tab_model():
    bridge, *_ = make()
    assert bridge._tab_model.loaded is False
    assert bridge.fetch_usage("today", "all")["ok"] is True
    assert bridge._tab_model.loaded is True
    data = bridge._tab_model.payload(dark=False)
    assert data["state"] == "ready" and data["segments"][0]["key"] == "claude-code"


def test_a_failed_refresh_keeps_the_values_and_reports_the_code():
    bridge, *_ = make()
    bridge.fetch_usage("today", "all")

    def boom(*_a, **_k):
        raise DesktopError("Can't reach your hub. Check that this computer is on your network.", code="NET-001")

    bridge._client.snapshot = boom
    result = bridge.fetch_usage("today", "all")
    assert result["ok"] is False and result["code"] == "NET-001"
    data = bridge._tab_model.payload(dark=False)
    assert data["stale"] is True and data["error"]["text"] == "Can't reach your hub · NET-001"
    assert data["segments"]


def test_an_uncoded_failure_has_no_code_in_the_chip():
    bridge, *_ = make()

    def boom(*_a, **_k):
        raise OSError("down")

    bridge._client.snapshot = boom
    bridge.fetch_usage("today", "all")
    error = bridge._tab_model.payload(dark=False)["error"]
    assert error["code"] is None and error["text"] == "Could not refresh usage"


def test_listeners_hear_about_every_change_and_survive_a_broken_one():
    model = tab_window.TabModel()
    heard = []
    model.subscribe(lambda: (_ for _ in ()).throw(RuntimeError("dead tab")))
    model.subscribe(lambda: heard.append(1))
    model.publish_report({"providers": []})
    model.publish_error("x", None)
    model.refresh()
    assert len(heard) == 3


def test_the_theme_the_page_reports_reaches_the_tab():
    bridge, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model)
    heard = []
    bridge._tab_model.subscribe(lambda: heard.append(tab_bridge.build_payload()["dark"]))
    bridge.report_effective_theme(True)
    bridge.report_effective_theme(False)
    assert heard == [True, False]


# ---- the tab's own bridge ------------------------------------------------

def test_tab_bridge_state_and_actions():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make()
    bridge.fetch_usage("today", "all")
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    state = tab_bridge.get_state()
    assert state["state"] == "ready" and state["demo"] is True
    surfaces.show_tab()
    assert tab_bridge.open_full() == {"ok": True}
    assert main_win.calls[-1] == "show" and tab_win.calls[-1] == "hide"
    assert tab_bridge.collapse() == {"ok": True}


def test_tab_push_and_poll_share_a_revision_including_theme_changes():
    bridge, _, _, surfaces, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    pushed = []
    bridge._tab_model.subscribe(lambda: pushed.append(tab_bridge.build_payload()))
    initial = tab_bridge.get_state()
    bridge.fetch_usage("today", "all")
    bridge.report_effective_theme(not initial["dark"])
    latest = tab_bridge.get_state()
    assert [item["state"] for item in pushed] == ["ready", "ready"]
    revisions = [initial["revision"], *(item["revision"] for item in pushed), latest["revision"]]
    assert revisions == sorted(set(revisions))
    assert latest["dark"] == pushed[-1]["dark"] != initial["dark"]
    # A re-opened desktop has a fresh page and bridge and starts its own sequence.
    assert TabBridge(bridge, bridge._tab_model, surfaces).get_state()["revision"] == 1


def test_tab_bridge_expand_reports_the_growth_direction(monkeypatch):
    bridge, _, tab, surfaces, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    seen = []
    monkeypatch.setattr(tab, "expand", lambda w, h, card=None: seen.append((w, h, card)) or {"up": True})
    assert tab_bridge.expand(316, 330, 8, 300, 250) == {"ok": True, "up": True}
    assert seen == [(316, 330, (8.0, 300.0, 250.0))]
    tab_bridge.expand(316, 330)
    assert seen[-1][2] is None  # no card rectangle: the whole window shows
    assert TabBridge(bridge, bridge._tab_model).expand(1, 2) == {"ok": False}


def test_the_tab_getter_never_starts_a_refresh():
    bridge, _, _, surfaces, *_ = make()
    started = []
    bridge.fetch_usage = lambda *a, **k: started.append(a) or {"ok": True}
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    state = tab_bridge.get_state()
    assert state["state"] == "loading"  # nothing loaded yet, and the getter only reads
    assert tab_bridge.get_state()["state"] == "loading"
    assert started == []


def test_the_tab_getter_waits_for_a_main_refresh_already_in_flight():
    bridge, _, _, surfaces, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    entered, release = threading.Event(), threading.Event()
    callers = []
    original = bridge._client.snapshot

    def slow_snapshot(period, machine):
        callers.append(threading.current_thread().name)
        entered.set()
        release.wait(5)
        return original(period, machine)

    bridge._client.snapshot = slow_snapshot
    main = threading.Thread(target=lambda: bridge.fetch_usage("today", "all"), name="main-refresh")
    main.start()
    assert entered.wait(3)
    results = []
    getter = threading.Thread(target=lambda: results.append(tab_bridge.get_state()), name="tab-getter")
    getter.start()
    time.sleep(0.2)
    assert getter.is_alive() and results == []  # waiting for the main refresh, not racing it
    release.set()
    main.join(5)
    getter.join(5)
    assert results and results[0]["state"] == "ready"  # it got the main refresh's result
    assert set(callers) == {"main-refresh"}  # the getter never fetched anything itself


def test_a_getter_does_not_wait_when_no_refresh_is_running():
    bridge, _, _, surfaces, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    began = time.monotonic()
    tab_bridge.get_state()
    assert time.monotonic() - began < 1.0
    assert bridge._wait_for_refresh(0) is True


def test_tab_bridge_only_exposes_the_methods_the_page_uses():
    import inspect

    public = {n for n, v in inspect.getmembers(TabBridge) if not n.startswith("_") and callable(v)}
    assert public == {
        "attach_controller", "begin_drag", "build_payload", "collapse", "expand",
        "get_state", "open_full", "press", "refresh_now", "set_focus", "set_pill_limit",
    }


def test_tab_bridge_refresh_now_triggers_main_page_refresh_without_opening_full():
    bridge, host, tab, surfaces, main_win, tab_win, _ = make()
    refreshed = []
    host.refresh_page = lambda: refreshed.append(1)
    surfaces.show_tab()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    assert tab_bridge.refresh_now() == {"ok": True}
    assert refreshed == [1]
    assert tab.visible is True and main_win.visible is False


# ---- run_webview with a fake pywebview -----------------------------------

class Event:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, *args):
        for handler in list(self.handlers):
            handler(*args)


class FakeWebviewWindow:
    def __init__(self, kwargs, title):
        self.kwargs = kwargs
        self.title = title
        self.calls = []
        self.native = None
        self.destroyed = threading.Event()
        self.events = types.SimpleNamespace(
            before_show=Event(), shown=Event(), loaded=Event(), closed=Event(), moved=Event(), resized=Event()
        )

    def show(self):
        self.calls.append("show")

    def hide(self):
        self.calls.append("hide")

    def resize(self, *size):
        self.calls.append(("resize", size))

    def destroy(self):
        self.calls.append("destroy")
        self.destroyed.set()

    def evaluate_js(self, script):
        if "provider-item" in script:
            return 10
        if "heroValue" in script:
            return "$12.00"
        if "fonts.check" in script:
            return True
        if "getElementById('tab')" in script:
            return True
        return None


def install_fake_webview(monkeypatch, *, tab_page_ok=True):
    fake = types.ModuleType("webview")
    fake.windows = []

    def create_window(title, **kwargs):
        window = FakeWebviewWindow(kwargs, title)
        fake.windows.append(window)
        return window

    def start(**_kwargs):
        main = fake.windows[0]
        for window in fake.windows:
            window.events.before_show.fire()
        main.events.shown.fire()
        main.events.loaded.fire()
        main.destroyed.wait(15)
        time.sleep(0.05)

    fake.create_window = create_window
    fake.start = start
    if not tab_page_ok:
        original = FakeWebviewWindow.evaluate_js
        monkeypatch.setattr(
            FakeWebviewWindow, "evaluate_js",
            lambda self, s: False if "getElementById('tab')" in s else original(self, s),
        )
    monkeypatch.setitem(sys.modules, "webview", fake)
    return fake


def test_run_webview_creates_the_hidden_always_on_top_tab(monkeypatch, tmp_path):
    fake = install_fake_webview(monkeypatch)
    assert ww.run_webview(DemoClient(), demo=True, smoke_test=True) == 0
    main, tab = fake.windows
    assert main.title.startswith("BURNRATE") and tab.title == "BURNRATE tab"
    assert tab.kwargs["hidden"] is True and tab.kwargs["on_top"] is True
    assert tab.kwargs["frameless"] is True and tab.kwargs["resizable"] is False
    assert tab.kwargs["height"] == tab_window.TAB_HEIGHT
    assert '<symbol id="claude"' in tab.kwargs["html"]
    assert main.kwargs["width"] == 440 and main.kwargs["height"] == 640
    assert main.kwargs["min_size"] == (380, 520)
    assert ("resize", (380, 520)) in main.calls
    assert "destroy" in tab.calls and "destroy" in main.calls


def test_the_smoke_test_fails_when_the_tab_page_is_missing(monkeypatch):
    install_fake_webview(monkeypatch, tab_page_ok=False)
    assert ww.run_webview(DemoClient(), demo=True, smoke_test=True) == 1


def test_a_tab_launch_starts_the_full_window_hidden_and_raises_the_tab(monkeypatch, tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(json.dumps({"surface": "tab"}), encoding="utf-8")
    fake = install_fake_webview(monkeypatch)

    def start(**_kwargs):
        for window in fake.windows:
            window.events.before_show.fire()
        fake.windows[0].events.shown.fire()
        assert wait_for(lambda: "show" in fake.windows[1].calls)
        fake.windows[0].destroy()

    fake.start = start
    # Demo keeps the tray and update checks off; persistence still needs prefs.
    ww.run_webview(DemoClient(), preferences=prefs, demo=True)
    main, tab = fake.windows
    assert main.kwargs["hidden"] is True
    assert "show" in tab.calls and "hide" not in tab.calls


def test_a_tab_launch_recovers_when_the_native_tab_cannot_be_shown(monkeypatch, tmp_path):
    prefs = tmp_path / "native-window.json"
    prefs.write_text(json.dumps({"surface": "tab"}), encoding="utf-8")
    fake = install_fake_webview(monkeypatch)
    monkeypatch.setattr(ww.WindowHost, "is_visible", lambda host: "show" in host.window.calls)

    def fail_to_show():
        raise OSError("native tab unavailable")

    def start(**_kwargs):
        main, tab = fake.windows
        assert main.kwargs["hidden"] and tab.kwargs["hidden"]
        tab.show = fail_to_show
        for window in fake.windows:
            window.events.before_show.fire()
        main.events.shown.fire()
        assert wait_for(lambda: json.loads(prefs.read_text(encoding="utf-8"))["surface"] == "full")
        assert "show" in main.calls
        main.destroy()

    fake.start = start
    ww.run_webview(DemoClient(), preferences=prefs, demo=True)


def test_a_full_launch_creates_the_main_window_visible(monkeypatch, tmp_path):
    fake = install_fake_webview(monkeypatch)

    def start(**_kwargs):
        fake.windows[0].destroy()

    fake.start = start
    ww.run_webview(DemoClient(), demo=True)
    assert fake.windows[0].kwargs["hidden"] is False
    assert fake.windows[1].calls == ["destroy"]  # never shown; torn down with the app


def test_the_second_launch_puts_the_tab_away(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    hidden = []
    tab_is_proxy = False

    class User32:
        @staticmethod
        def FindWindowW(_c, title):
            return 7 if title == ww.TAB_WINDOW_TITLE else 42

        IsWindowVisible = staticmethod(lambda _h: True)
        IsIconic = staticmethod(lambda _h: False)
        SetForegroundWindow = staticmethod(lambda _h: 1)

        @staticmethod
        def GetClassNameW(handle, buffer, _size):
            buffer.value = (
                "Windows.Internal.Shell.TabProxyWindow"
                if handle == 7 and tab_is_proxy else "WindowsForms10.Window.8.app.0"
            )
            return len(buffer.value)

        @staticmethod
        def ShowWindow(handle, command):
            hidden.append((handle, command))

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": User32})())
    assert ww._focus_existing_window("BURNRATE") is True
    assert hidden == [(7, 0)]

    tab_is_proxy = True
    hidden.clear()
    assert ww._focus_existing_window("BURNRATE") is True
    assert hidden == []
