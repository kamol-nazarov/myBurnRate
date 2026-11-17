"""Tab fade (Auto-fade), drag from anywhere, transactional surface switches, desktop log."""

import json
import logging

import pytest

from spend_app import native_geometry, wealthfront_window as ww
from spend_app.native_geometry import Monitor, Rect
from spend_app.wealthfront_window import FadeTarget, WindowOpacityController
from tests_spend.browser_runtime import CHROME
from tests_spend.test_tab_page import run_page
from tests_spend.test_tab_page_fit import datasets
from tests_spend.test_tab_review import BIG, placed  # noqa: F401 -- the "placed" fixture
from tests_spend.test_tab_surfaces import SurfaceController, make
from tests_spend.test_tab_window import NOW, FakeStore


# ---- the fade state machine ---------------------------------------------------------

class FakeApi:
    """user32, in memory: a cursor, a mouse button, windows, and a log of alpha calls."""

    def __init__(self):
        self.cursor = (500, 500)
        self.left_down = False
        self.visible = {}
        self.rects = {}
        self.layered = []
        self.alphas = []

    def is_window(self, hwnd):
        return True

    def is_iconic(self, hwnd):
        return False

    def is_visible(self, hwnd):
        return self.visible.get(hwnd, True)

    def cursor_pos(self):
        return self.cursor

    def window_rect(self, hwnd):
        return self.rects.get(hwnd)

    def left_button_down(self):
        return self.left_down

    def ensure_layered(self, hwnd):
        self.layered.append(hwnd)
        return True

    def set_alpha(self, hwnd, alpha):
        self.alphas.append((hwnd, alpha))


class FakeBridge:
    def __init__(self, auto_fade=True, idle=0.38, busy=False):
        self.state = (auto_fade, idle, busy)

    def opacity_state(self):
        return self.state


TAB = 7
STRIP = (100, 20, 900, 80)  # screen rectangle of the visible strip


def inside_strip(point):
    return STRIP[0] - 4 <= point[0] <= STRIP[2] + 4 and STRIP[1] - 4 <= point[1] <= STRIP[3] + 4


class Rig:
    def __init__(self, *, auto_fade=True, idle=0.38, hold=None):
        self.api = FakeApi()
        self.bridge = FakeBridge(auto_fade, idle)
        self.held = {"v": False}
        self.controller = WindowOpacityController(self.bridge, lambda: 0, api=self.api)
        self.controller.main.hwnd_getter = lambda: 0  # the full window is not under test here
        self.target = self.controller.add_target(
            FadeTarget(lambda: TAB, hit_test=inside_strip, hold=lambda: self.held["v"], leave_delay=1.5,
                       follows_ui_busy=False)
        )
        self.now = 0.0
        self.api.cursor = (500, 50)  # over the strip

    def run(self, seconds):
        end = self.now + seconds
        while self.now < end:
            self.controller.tick_target(self.target, self.now)
            self.now += 0.016

    @property
    def alpha(self):
        return self.target.alpha

    def leave(self):
        self.api.cursor = (1500, 900)

    def enter(self):
        self.api.cursor = (500, 50)


IDLE = round(0.38 * 255)


def test_the_tab_starts_opaque_and_stays_opaque_while_the_pointer_is_over_it():
    rig = Rig()
    rig.run(3)
    assert rig.alpha == 255
    assert rig.api.layered == [TAB]  # made layered once, never restyled again
    assert all(a == 255 for _, a in rig.api.alphas)  # and never dimmed


def test_it_fades_to_the_idle_opacity_only_after_the_1_5_s_delay():
    rig = Rig()
    rig.run(1)
    rig.leave()
    rig.run(1.4)
    assert rig.alpha == 255  # still inside the grace period
    rig.run(0.4)
    assert rig.alpha < 255  # the fade has begun
    rig.run(2.5)
    assert rig.alpha == IDLE
    assert rig.api.alphas[-1] == (TAB, IDLE)


def test_the_pointer_coming_back_brings_it_to_full_in_about_120_ms():
    rig = Rig()
    rig.run(0.2)
    rig.leave()
    rig.run(4)
    assert rig.alpha == IDLE
    rig.enter()
    rig.run(0.13)
    assert rig.alpha > 0.85 * 255  # snappy
    rig.run(0.2)
    assert rig.alpha == 255


def test_a_leave_and_return_inside_the_delay_never_dims_at_all():
    rig = Rig()
    rig.run(1)
    rig.leave()
    rig.run(1.0)
    rig.enter()
    rig.run(2)
    assert rig.alpha == 255 and all(a == 255 for _, a in rig.api.alphas)


def test_an_open_hover_card_or_a_keyboard_focus_ring_keeps_it_opaque():
    rig = Rig()
    rig.run(0.5)
    rig.held["v"] = True  # card open / focus ring inside
    rig.leave()
    rig.run(6)
    assert rig.alpha == 255
    rig.held["v"] = False
    rig.run(1.4)
    assert rig.alpha == 255  # the delay starts when the hold ends...
    rig.run(1.0)
    assert rig.alpha < 255  # ...not before


def test_dragging_keeps_it_opaque_even_when_the_cursor_outruns_the_window():
    rig = Rig()
    rig.run(0.5)
    rig.api.left_down = True  # pressed on the strip
    rig.run(0.1)
    rig.leave()  # the cursor is no longer over the (lagging) window
    rig.run(5)
    assert rig.alpha == 255 and rig.target.dragging is True
    rig.api.left_down = False  # the drag ends
    rig.run(1.4)
    assert rig.alpha == 255
    rig.run(3)
    assert rig.alpha == IDLE


def test_a_press_that_starts_outside_the_tab_is_not_a_drag():
    rig = Rig()
    rig.leave()
    rig.api.left_down = True
    rig.run(4)
    assert rig.target.dragging is False and rig.alpha == IDLE


def test_with_auto_fade_off_the_tab_never_dims():
    rig = Rig(auto_fade=False)
    rig.leave()
    rig.run(8)
    assert rig.alpha == 255 and all(a == 255 for _, a in rig.api.alphas)
    rig.bridge.state = (True, 0.38, False)  # switching it on in Settings takes effect live
    rig.run(3)
    assert rig.alpha == IDLE
    rig.bridge.state = (False, 0.38, False)
    rig.run(1)
    assert rig.alpha == 255


def test_the_idle_opacity_setting_is_honoured_and_clamped():
    rig = Rig(idle=0.60)
    rig.leave()
    rig.run(6)
    assert rig.alpha == round(0.60 * 255)
    low = Rig(idle=0.01)
    low.leave()
    low.run(6)
    assert low.alpha == WindowOpacityController.FLOOR


def test_a_hidden_tab_gets_no_layered_calls_and_comes_back_fully_opaque():
    rig = Rig()
    rig.leave()
    rig.run(6)
    assert rig.alpha == IDLE
    rig.api.visible[TAB] = False
    calls = len(rig.api.alphas)
    rig.run(2)
    assert len(rig.api.alphas) == calls and rig.target.shown is False
    rig.api.visible[TAB] = True  # shown again, cursor still away
    rig.controller.tick_target(rig.target, rig.now)
    assert rig.api.alphas[-1] == (TAB, 255)  # no flash of the old faded value
    assert rig.alpha == 255


def test_waking_the_tab_holds_it_opaque_for_a_moment():
    rig = Rig()
    rig.leave()
    rig.controller.wake(2.5, rig.target)
    rig.target.awake_until = rig.now + 2.5
    rig.run(2.4)
    assert rig.alpha == 255
    rig.run(3)
    assert rig.alpha == IDLE


def test_the_tab_ignores_the_full_windows_busy_flag_but_the_full_window_does_not():
    api = FakeApi()
    api.rects[1] = (0, 0, 400, 400)
    controller = WindowOpacityController(FakeBridge(busy=True), lambda: 1, api=api)
    tab = controller.add_target(FadeTarget(lambda: TAB, hit_test=inside_strip, follows_ui_busy=False, leave_delay=1.5))
    api.cursor = (3000, 3000)
    now = 0.0
    for _ in range(600):
        controller.tick_target(controller.main, now)
        controller.tick_target(tab, now)
        now += 0.016
    assert controller.main.alpha == 255  # a dialog is up: the full window stays opaque
    assert tab.alpha == IDLE


def test_one_controller_serves_both_windows_and_alpha_calls_are_lwa_alpha_only():
    api = FakeApi()
    api.rects[1] = (0, 0, 400, 400)
    controller = WindowOpacityController(FakeBridge(), lambda: 1, api=api)
    controller.add_target(FadeTarget(lambda: TAB, hit_test=inside_strip, leave_delay=1.5))
    api.cursor = (3000, 3000)
    for step in range(400):
        controller.tick(step * 0.016)
    assert {hwnd for hwnd, _ in api.alphas} == {1, TAB}
    assert sorted(api.layered) == [1, TAB]


def test_the_real_win32_wrapper_issues_lwa_alpha(monkeypatch):
    import sys
    import types

    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    calls = []

    class User32:
        def __init__(self):
            self.SetLayeredWindowAttributes = types.SimpleNamespace(argtypes=None)
            self.styles = {}

        def GetWindowLongW(self, hwnd, index):
            return self.styles.get(hwnd, 0)

        def SetWindowLongW(self, hwnd, index, value):
            self.styles[hwnd] = value

    user32 = User32()

    def set_layered(hwnd, key, alpha, flags):
        calls.append((hwnd, key, alpha, flags))
        return 1

    set_layered.argtypes = None
    user32.SetLayeredWindowAttributes = set_layered
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
    api = ww.Win32Fade()
    assert api.ensure_layered(77) is True
    assert user32.styles[77] & 0x00080000  # WS_EX_LAYERED
    api.set_alpha(77, 97)
    api.set_alpha(77, 999)
    assert calls[-2:] == [(77, 0, 97, 0x02), (77, 0, 255, 0x02)]  # LWA_ALPHA, clamped


# ---- the tab's visible region, for the pointer test ---------------------------------

def test_the_hit_test_covers_the_strip_and_the_card_but_not_the_gap_or_the_cut_away_area(placed):
    host, state = placed
    state["rect"] = Rect(100, 20, 1000, 80)
    host.set_base(900)
    assert host.visible_screen_rects() == [(100, 20, 1000, 80)]
    assert host.contains_screen_point((500, 50)) and host.contains_screen_point((1003, 50))
    assert not host.contains_screen_point((500, 300))
    host.expand(900, 330, (60, 300, 250))
    strip, card = host.visible_screen_rects()
    assert strip == (state["rect"].left, state["rect"].top, state["rect"].right, state["rect"].top + 60)
    assert card[0] == 160 and card[2] == 460 and card[1] == state["rect"].top + 66
    assert host.contains_screen_point((300, state["rect"].top + 150))  # inside the card
    assert not host.contains_screen_point((700, state["rect"].top + 150))  # right of the card: cut away
    assert not host.contains_screen_point((300, state["rect"].top + 63), margin=0)  # the 6 px gap


def test_the_tab_holds_opaque_while_a_card_is_open_or_focus_is_inside(placed):
    host, state = placed
    host.set_base(900)
    assert host.holds_opaque() is False
    host.expand(900, 330, (60, 300, 250))
    assert host.holds_opaque() is True
    host.collapse()
    assert host.holds_opaque() is False
    host.keyboard_focus = True
    assert host.holds_opaque() is True


def test_the_page_reports_keyboard_focus_to_the_host():
    from spend_app.tab_window import TabBridge

    bridge, _, tab, surfaces, *_ = make()
    tab_bridge = TabBridge(bridge, bridge._tab_model, surfaces)
    assert tab_bridge.set_focus(True) == {"ok": True} and tab.keyboard_focus is True
    tab_bridge.set_focus(False)
    assert tab.keyboard_focus is False


def test_the_tab_is_made_layered_before_it_is_shown(monkeypatch):
    import sys
    import types

    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    log = []
    user32 = types.SimpleNamespace(
        GetWindowLongW=lambda hwnd, i: 0x50008,
        SetWindowLongW=lambda hwnd, i, v: log.append(("style", hwnd, i, v)),
        SetLayeredWindowAttributes=lambda hwnd, k, a, f: log.append(("alpha", hwnd, a, f)),
    )
    monkeypatch.setattr(ctypes, "windll", types.SimpleNamespace(user32=user32), raising=False)
    host = ww.TabHost(types.SimpleNamespace(), store=FakeStore(), hwnd_box=[88])
    host.prepare_layered()
    assert log == [("style", 88, -20, 0x50008 | 0x00080000), ("alpha", 88, 255, 0x02)]


# ---- staying on screen after a drag -------------------------------------------------

def test_a_tab_dragged_partly_off_screen_is_pulled_back_when_the_mouse_is_up(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    state["rect"] = Rect(-300, 40, 600, 100)  # dropped hanging off the left edge
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: False)
    host._settle()
    assert state["rect"].left == 0 and state["rect"].top == 40


def test_the_clamp_waits_while_the_button_is_still_down(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    state["rect"] = Rect(-300, 40, 600, 100)
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: True)
    again = []
    monkeypatch.setattr(host, "note_moved", lambda: again.append(1))
    host._settle()
    assert again == [1] and state["rect"].left == -300  # still dragging: look again later


def test_a_move_arms_one_debounced_settle(placed, monkeypatch):
    host, _ = placed
    created = []

    class FakeTimer:
        def __init__(self, delay, fn):
            self.delay, self.fn, self.cancelled = delay, fn, False
            created.append(self)
            self.daemon = False

        def start(self):
            pass

        def cancel(self):
            self.cancelled = True

    monkeypatch.setattr("spend_app.tab_window.threading.Timer", FakeTimer)
    host.note_moved()
    host.note_moved()
    assert len(created) == 2 and created[0].cancelled and not created[1].cancelled
    assert created[1].delay == 0.5


def test_the_dragged_position_is_saved_under_tabwindow(placed, monkeypatch):
    host, state = placed
    snap = native_geometry.SavedGeometry(300, 200, 900, 60, 96)
    monkeypatch.setattr(native_geometry, "snapshot", lambda hwnd: snap)
    assert host.tracker.flush() is True
    assert host._store.saved == {"tabWindow": snap.as_preference()}


# ---- a failed shrink never leaves the user without a window ------------------------------

def failing(window, name):
    original = getattr(window, name)

    def boom(*a, **k):
        original(*a, **k) if False else None
        raise RuntimeError(f"{name} failed")

    setattr(window, name, boom)


def test_a_tab_that_fails_to_show_leaves_the_full_window_up(caplog):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    failing(tab_win, "show")
    with caplog.at_level(logging.WARNING, logger="spend_app"):
        surfaces.show_tab()
        assert surfaces.flush(5)
    assert main_win.visible is True and tab_win.visible is False and tab.visible is False
    assert main_win.calls == ["show"]  # the full window was never hidden
    assert bridge.surface == "full"
    assert any("Tab show failed" in r.message for r in caplog.records)
    assert any("Shrink to tab failed" in r.message for r in caplog.records)


def test_a_tab_that_never_appears_is_rolled_back(caplog, monkeypatch):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    monkeypatch.setattr(tab, "is_shown", lambda: False)
    with caplog.at_level(logging.WARNING, logger="spend_app"):
        surfaces.show_tab()
        assert surfaces.flush(5)
    assert main_win.visible is True and tab.visible is False and bridge.surface == "full"
    assert any("did not appear" in (r.exc_text or "") or "did not appear" in str(r.exc_info) for r in caplog.records)


def test_a_full_window_that_cannot_be_hidden_rolls_the_tab_back(caplog):
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    failing(main_win, "hide")
    surfaces.show_tab()
    assert surfaces.flush(5)
    assert main_win.visible is True and tab_win.visible is False and bridge.surface == "full"
    assert tab_win.calls == ["show", "hide"]


def test_a_full_window_that_will_not_open_keeps_the_tab():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    surfaces.show_tab()
    assert surfaces.flush(5)
    failing(main_win, "show")
    surfaces.show_full()
    assert surfaces.flush(5)
    assert tab.visible is True and tab_win.visible is True and bridge.surface == "tab"


def test_a_successful_shrink_shows_the_tab_before_hiding_the_full_window():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    order = []
    show, hide = tab_win.show, main_win.hide
    tab_win.show = lambda: (order.append("tab shown"), show())
    main_win.hide = lambda: (order.append("main hidden"), hide())
    surfaces.show_tab()
    assert surfaces.flush(5)
    assert order == ["tab shown", "main hidden"]  # never a moment with no window


# ---- the desktop log ------------------------------------------------------------------

def test_the_desktop_log_records_warnings_and_redacts_secrets(tmp_path):
    path = ww.configure_desktop_logging(tmp_path)
    try:
        assert path == tmp_path / "desktop.log"
        logging.getLogger("spend_app.tab_window").warning(
            "Shrink to tab failed in C:\\Users\\kamol\\secret with Bearer abcdefghijklmnop1234567890"
        )
        try:
            raise RuntimeError("boom")
        except RuntimeError:
            logging.getLogger("spend_app.tab_window").warning("Tab show failed", exc_info=True)
        for handler in logging.getLogger("spend_app").handlers:
            handler.flush()
        text = path.read_text(encoding="utf-8")
        assert "WARNING spend_app.tab_window Shrink to tab failed" in text
        assert "abcdefghijklmnop1234567890" not in text and "[redacted]" in text
        assert "C:\\Users\\[user]\\secret" in text
        assert "Traceback" in text and "RuntimeError: boom" in text
        ww.configure_desktop_logging(tmp_path)  # idempotent: no duplicate handlers
        marked = [h for h in logging.getLogger("spend_app").handlers if getattr(h, ww._DESKTOP_LOG_MARK, False)]
        assert len(marked) == 1
    finally:
        logger = logging.getLogger("spend_app")
        for handler in list(logger.handlers):
            if getattr(handler, ww._DESKTOP_LOG_MARK, False):
                logger.removeHandler(handler)
                handler.close()
        webview_logger = logging.getLogger("pywebview")
        for handler in list(webview_logger.handlers):
            if getattr(handler, ww._DESKTOP_LOG_MARK, False):
                webview_logger.removeHandler(handler)
                handler.close()


def test_transitions_are_logged_at_info_level(tmp_path):
    path = ww.configure_desktop_logging(tmp_path)
    try:
        bridge, _, tab, surfaces, *_ = make(controller=SurfaceController)
        surfaces.show_tab()
        assert surfaces.flush(5)
        for handler in logging.getLogger("spend_app").handlers:
            handler.flush()
        assert "Surface tab -> surface=tab" in path.read_text(encoding="utf-8")
    finally:
        logger = logging.getLogger("spend_app")
        for handler in list(logger.handlers):
            if getattr(handler, ww._DESKTOP_LOG_MARK, False):
                logger.removeHandler(handler)
                handler.close()


def test_run_webview_sets_up_the_log_for_real_runs_only(monkeypatch, tmp_path):
    import sys
    import types

    seen = []
    monkeypatch.setattr(ww, "configure_desktop_logging", lambda *a, **k: seen.append(1))
    monkeypatch.setattr(ww, "_focus_existing_window", lambda title: False)
    fake = types.ModuleType("webview")
    fake.windows = []
    fake.create_window = lambda *a, **k: (_ for _ in ()).throw(RuntimeError("stop here"))
    monkeypatch.setitem(sys.modules, "webview", fake)
    from spend_app.native_desktop import DemoClient

    for demo, smoke, expected in ((True, False, 0), (True, True, 0)):
        seen.clear()
        with pytest.raises(RuntimeError):
            ww.run_webview(DemoClient(), demo=demo, smoke_test=smoke)
        assert len(seen) == expected
    monkeypatch.setattr(ww.DesktopBridge, "_migrate_legacy_secrets", lambda self: None)
    seen.clear()
    with pytest.raises(RuntimeError):
        ww.run_webview(DemoClient(), demo=False, smoke_test=False)
    assert seen == [1]


# ---- drag from anywhere, in real Chrome ---------------------------------------------------

STUB = """
window.calls = [];
window.pywebview = { api: {
  begin_drag: function () { calls.push('begin_drag'); },
  open_full: function () { calls.push('open_full'); return Promise.resolve({ok: true}); },
  expand: function () { calls.push('expand'); return Promise.resolve({ok: true, up: false}); },
  collapse: function () { calls.push('collapse'); return Promise.resolve({ok: true}); },
  press: function () { calls.push('press'); },
  set_focus: function (v) { calls.push('focus:' + v); return Promise.resolve({ok: true}); },
  get_state: function () { return Promise.resolve(null); }
} };
function S(i) { return document.querySelectorAll('.seg')[i]; }
function mouse(type, el, x, y, buttons, detail) {
  if (typeof el === 'function') el = el();
  el.dispatchEvent(new MouseEvent(type, { bubbles: true, cancelable: true, clientX: x, clientY: y,
                                           button: 0, buttons: buttons, detail: detail || 0, view: window }));
}
function count(name) { return calls.filter(function (c) { return c === name; }).length; }
"""


def demo_json():
    model = datasets()["demo"]
    return json.dumps(model.payload(dark=False, now=NOW, budget=2000))


def fit_json():
    model = datasets()["demo+error"]
    return json.dumps(model.payload(dark=False, now=NOW, budget=700))


needs_chrome = pytest.mark.skipif(not CHROME.exists(), reason="Google Chrome is needed for the tab page checks")


@needs_chrome
@pytest.mark.browser
def test_a_press_that_moves_more_than_4_px_starts_a_native_drag_and_no_click_follows(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({demo_json()});
      var seg = function () {{ return S(1); }};
      mouse('mousedown', seg, 300, 30, 1);
      mouse('mousemove', seg, 303, 32, 1);          // 3 px: still a click
      report.presses = count('press');
      report.afterSmall = count('begin_drag');
      mouse('mousemove', seg, 306, 30, 1);          // 6 px: a drag
      report.afterBig = count('begin_drag');
      mouse('mousemove', seg, 340, 30, 1);          // more movement does not start a second drag
      report.afterMore = count('begin_drag');
      mouse('mouseup', seg, 340, 30, 0);
      mouse('click', seg, 340, 30, 0, 1);           // the click the browser fires after the drag
      report.clickAfterDrag = count('open_full');
      mouse('click', seg, 340, 30, 0, 0);           // a keyboard click is still honoured
      report.keyboardClick = count('open_full');
      """)
    assert "error" not in out, out.get("error")
    assert out["presses"] == 1  # the host is told where the press happened
    assert out["afterSmall"] == 0 and out["afterBig"] == 1 and out["afterMore"] == 1
    assert out["clickAfterDrag"] == 0 and out["keyboardClick"] == 1


@needs_chrome
@pytest.mark.browser
def test_a_press_and_release_without_moving_stays_a_click(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({demo_json()});
      var seg = function () {{ return S(0); }}, fine = document.getElementById('fine'),
          open = document.getElementById('open');
      [seg, fine, open].forEach(function (el) {{
        mouse('mousedown', el, 50, 30, 1); mouse('mousemove', el, 52, 31, 1);
        mouse('mouseup', el, 52, 31, 0); mouse('click', el, 52, 31, 0, 1);
      }});
      report.opened = count('open_full'); report.drags = count('begin_drag');
      // a drag, then later a normal click works again
      mouse('mousedown', seg, 50, 30, 1); mouse('mousemove', seg, 90, 30, 1);
      mouse('mousedown', seg, 50, 30, 1); mouse('mouseup', seg, 50, 30, 0); mouse('click', seg, 50, 30, 0, 1);
      report.openedAfter = count('open_full');
      """)
    assert "error" not in out, out.get("error")
    assert out["opened"] == 3 and out["drags"] == 0
    assert out["openedAfter"] == 4  # the click after a later press is not swallowed


@needs_chrome
@pytest.mark.browser
def test_a_drag_can_start_on_segments_pills_the_brand_the_open_button_and_empty_space(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({fit_json()});
      var targets = {{ seg: document.querySelector('.seg'), fine: document.getElementById('fine'),
        urgent: document.getElementById('urgentPill'), home: document.getElementById('home'),
        brand: document.querySelector('.brand'), open: document.getElementById('open'),
        strip: document.getElementById('tab'), chip: document.getElementById('chip') }};
      report.started = {{}};
      Object.keys(targets).forEach(function (name) {{
        var el = targets[name];
        if (!el || el.hidden) return;
        var before = count('begin_drag');
        mouse('mousedown', el, 20, 30, 1); mouse('mousemove', el, 40, 30, 1); mouse('mouseup', el, 40, 30, 0);
        report.started[name] = count('begin_drag') - before;
      }});
      """)
    assert "error" not in out, out.get("error")
    assert out["started"] and all(v == 1 for v in out["started"].values()), out["started"]
    assert {"seg", "fine", "home", "brand", "open", "strip"} <= set(out["started"])


@needs_chrome
@pytest.mark.browser
def test_no_hover_card_opens_during_a_drag_and_an_open_one_closes_when_it_starts(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({demo_json()});
      var segs = [0, 1, 2, 3].map(function (i) {{ return function () {{ return S(i); }}; }});
      mouse('mouseover', segs[0], 100, 30, 0);
      await tick(300);
      report.cardOpened = count('expand') > 0 && !document.getElementById('cardWrap').hidden;
      var collapsesBefore = count('collapse');
      mouse('mousedown', segs[0], 100, 30, 1);
      mouse('mousemove', segs[0], 120, 30, 1);                      // the drag starts
      report.cardClosed = document.getElementById('cardWrap').hidden;
      report.collapsedOnStart = count('collapse') - collapsesBefore;
      var expands = count('expand');
      mouse('mouseover', segs[2], 300, 30, 1);                      // pointer sweeps over another segment
      await tick(400);
      report.expandsDuringDrag = count('expand') - expands;
      report.cardDuringDrag = !document.getElementById('cardWrap').hidden;
      mouse('mouseup', segs[2], 300, 30, 0);
      mouse('mouseover', segs[3], 400, 30, 0);
      await tick(400);
      report.cardAfterDrag = !document.getElementById('cardWrap').hidden;
      """)
    assert "error" not in out, out.get("error")
    assert out["cardOpened"] is True and out["cardClosed"] is True and out["collapsedOnStart"] == 1
    assert out["expandsDuringDrag"] == 0 and out["cardDuringDrag"] is False
    assert out["cardAfterDrag"] is True  # hovering works again once the drag is over


@needs_chrome
@pytest.mark.browser
def test_a_mouse_press_does_not_pop_the_card_before_it_becomes_a_drag(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({demo_json()});
      mouse('mousedown', function () {{ return S(1); }}, 100, 30, 1);
      S(1).dispatchEvent(new FocusEvent('focusin', {{ bubbles: true }}));   // the mouse focuses the button
      await tick(300);
      report.expandsOnPress = count('expand');
      """)
    assert "error" not in out, out.get("error")
    assert out["expandsOnPress"] == 0


@needs_chrome
@pytest.mark.browser
def test_keyboard_behaviour_is_unchanged_and_focus_is_reported_to_the_host(tmp_path):
    out = run_page(tmp_path, f"""{STUB}
      window.burnrateTab.update({demo_json()});
      S(0).focus();
      await tick(100);
      report.cardOnFocus = !document.getElementById('cardWrap').hidden;
      report.focusTrue = count('focus:true');
      document.activeElement.dispatchEvent(new KeyboardEvent('keydown', {{ key: 'Escape', bubbles: true }}));
      report.escapeClosed = document.getElementById('cardWrap').hidden;
      document.activeElement.click();                                 // Enter on a focused button: detail 0
      report.enterOpens = count('open_full');
      document.activeElement.blur();
      await tick(50);
      report.focusFalse = count('focus:false');
      """)
    assert "error" not in out, out.get("error")
    assert out["cardOnFocus"] is True and out["escapeClosed"] is True and out["enterOpens"] == 1
    assert out["focusTrue"] >= 1 and out["focusFalse"] >= 1
