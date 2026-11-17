"""Review round 1: window lifecycle (D2-01, D2-04, D2-05) for the tab and full window."""

import json
import sys
import threading
import time
import types

import pytest

from spend_app import wealthfront_window as ww
from spend_app.tab_window import SurfaceController, TabHost
from tests_spend.test_tab_surfaces import make, wait_for


# ---- D2-04: transitions are serialized and the latest request wins -------------

def test_opposite_transitions_never_leave_both_windows_up():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    entered, release = threading.Event(), threading.Event()
    hide = main_win.hide

    def slow_hide():
        hide()
        entered.set()
        release.wait(5)  # the worker is between hiding the full window and showing the tab

    main_win.hide = slow_hide
    surfaces.show_tab()
    assert entered.wait(3)
    surfaces.show_full()  # the tray's Open BURNRATE arrives mid-transition; it never blocks
    release.set()
    assert surfaces.flush(5)
    assert main_win.visible is True and tab_win.visible is False
    assert tab.visible is False and bridge.surface == "full"


def test_the_last_absolute_request_wins():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    gate = threading.Event()
    show = main_win.show

    def slow_show():
        gate.wait(5)
        show()

    main_win.show = slow_show
    surfaces.show_full()  # the worker blocks inside the first transition
    time.sleep(0.1)
    for _ in range(3):
        surfaces.show_tab()
        surfaces.show_full()
    surfaces.show_tab()
    gate.set()
    assert surfaces.flush(5)
    assert tab.visible is True and main_win.visible is False and bridge.surface == "tab"


def test_toggles_keep_their_order_after_an_absolute_request():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    surfaces.show_tab()
    surfaces.toggle()  # hides the tab
    surfaces.toggle()  # brings it back
    assert surfaces.flush(5)
    assert tab.visible is True and main_win.visible is False


def test_a_transition_that_re_enters_the_controller_cannot_deadlock():
    bridge, _, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    show = tab_win.show
    seen = []

    def reentrant_show():
        show()
        if not seen:
            seen.append(1)
            surfaces.toggle()  # e.g. a handler fired by the GUI call itself

    tab_win.show = reentrant_show
    surfaces.show_tab()
    assert surfaces.flush(5)
    assert tab.visible is False  # the queued toggle ran after the transition


def test_shutdown_drops_queued_transitions_and_blocks_new_ones():
    bridge, host, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    gate = threading.Event()
    hide = main_win.hide
    main_win.hide = lambda: (gate.wait(5), hide())
    surfaces.show_tab()
    time.sleep(0.1)
    surfaces.show_full()
    surfaces.shutdown()
    gate.set()
    assert surfaces.flush(5)
    surfaces.show_tab()
    assert surfaces.flush(5)
    assert "destroy" in tab_win.calls and tab.quitting is True
    assert tab_win.calls.count("destroy") == 1


# ---- D2-01: every way of closing the full window takes the tab with it ----------

class FakeEvent:
    def __init__(self):
        self.handlers = []

    def __iadd__(self, handler):
        self.handlers.append(handler)
        return self

    def fire(self, sender, args):
        for handler in list(self.handlers):
            handler(sender, args)


class FakeForm:
    def __init__(self):
        self.FormClosing = FakeEvent()
        self.FormClosed = FakeEvent()
        self.DpiChanged = FakeEvent()
        self.Handle = types.SimpleNamespace(ToInt64=lambda: 4242)


class CloseArgs:
    def __init__(self, reason):
        self.CloseReason = reason
        self.Cancel = False


@pytest.fixture
def winforms(monkeypatch):
    reasons = types.SimpleNamespace(
        UserClosing="user", TaskManagerClosing="task", WindowsShutDown="shutdown",
        ApplicationExitCall="exit", FormOwnerClosing="owner",
    )
    forms = types.ModuleType("System.Windows.Forms")
    forms.CloseReason = reasons
    windows = types.ModuleType("System.Windows")
    windows.Forms = forms
    system = types.ModuleType("System")
    system.Windows = windows
    for name, module in (("System", system), ("System.Windows", windows), ("System.Windows.Forms", forms)):
        monkeypatch.setitem(sys.modules, name, module)
    monkeypatch.setattr(ww, "_apply_win11_chrome", lambda *a, **k: 4242)
    monkeypatch.setattr(TabHost, "hide_taskbar_button", lambda self: None)
    monkeypatch.setattr(TabHost, "drop_dwm_frame", lambda self: None)
    return reasons


def native_pair(*, can_hide):
    bridge, host, tab, surfaces, main_win, tab_win, _ = make(controller=SurfaceController)
    host.can_hide_to_tray = lambda: can_hide
    main_win.native = FakeForm()
    tab_win.native = FakeForm()
    host._install_form_hooks(main_win.native)
    tab.on_before_show(on_close_request=surfaces.close_tab, dark=False)
    return bridge, host, tab, surfaces, main_win, tab_win


@pytest.mark.parametrize("reason", ["user", "task", "shutdown", "exit"])
def test_a_native_close_of_the_full_window_destroys_the_tab_too(winforms, reason):
    # Alt+F4 or the taskbar Close with close-to-tray off / no tray: not hidden, so it closes.
    bridge, host, tab, surfaces, main_win, tab_win = native_pair(can_hide=False)
    args = CloseArgs(reason)
    main_win.native.FormClosing.fire(main_win.native, args)
    assert args.Cancel is False  # the full window is allowed to close...
    assert host.quitting is True
    assert "destroy" in tab_win.calls and tab.quitting is True  # ...and the tab goes with it
    surfaces.show_tab()
    assert surfaces.flush(3) and tab_win.calls.count("show") == 0


def test_a_user_close_that_hides_to_the_tray_keeps_the_tab(winforms):
    bridge, host, tab, surfaces, main_win, tab_win = native_pair(can_hide=True)
    args = CloseArgs("user")
    main_win.native.FormClosing.fire(main_win.native, args)
    assert args.Cancel is True and host.quitting is False
    assert "destroy" not in tab_win.calls and tab.quitting is False


def test_the_tab_lets_windows_shutdown_and_app_exit_through(winforms):
    for reason in ("shutdown", "exit", "owner"):
        bridge, host, tab, surfaces, main_win, tab_win = native_pair(can_hide=True)
        args = CloseArgs(reason)
        tab_win.native.FormClosing.fire(tab_win.native, args)
        assert args.Cancel is False and tab.quitting is True


def test_a_user_close_of_the_tab_parks_it_instead_of_closing(winforms):
    bridge, host, tab, surfaces, main_win, tab_win = native_pair(can_hide=True)
    surfaces.show_tab()
    assert surfaces.flush(3)
    args = CloseArgs("user")
    tab_win.native.FormClosing.fire(tab_win.native, args)
    assert args.Cancel is True
    assert surfaces.flush(3)
    assert tab.visible is False and tab.quitting is False and tab_win.calls[-1] == "hide"


def test_a_user_close_of_the_tab_with_no_tray_quits_everything(winforms):
    bridge, host, tab, surfaces, main_win, tab_win = native_pair(can_hide=False)
    args = CloseArgs("user")
    tab_win.native.FormClosing.fire(tab_win.native, args)
    assert args.Cancel is True
    assert wait_for(lambda: "destroy" in main_win.calls and "destroy" in tab_win.calls)


# ---- D2-05: a second launch goes through the running surface controller ---------

class FakeEvents:
    """kernel32's named events, in memory."""

    def __init__(self):
        self.named = {}

    def create(self, name):
        return self.named.setdefault(name, threading.Event())

    def open(self, name):
        return self.named.get(name)

    def signal(self, handle):
        handle.set()
        return True

    def wait(self, handle, milliseconds):
        if handle.wait(milliseconds / 1000):
            handle.clear()
            return True
        return False

    def close(self, handle):
        pass


def test_a_second_launch_reaches_the_running_process_and_keeps_its_state_in_sync(tmp_path):
    from spend_app.native_activation import ActivationListener, signal_running_instance

    bridge, host, tab, surfaces, main_win, tab_win, prefs = make(tmp_path, controller=SurfaceController)
    events = FakeEvents()
    assert signal_running_instance(events=events) is False  # nobody listening yet
    listener = ActivationListener(surfaces.show_full, events=events)
    assert listener.start() is True
    try:
        surfaces.show_tab()
        assert surfaces.flush(3) and tab.visible is True
        assert signal_running_instance(events=events) is True  # the owner opens the shortcut again
        assert wait_for(lambda: bridge.surface == "full")
        assert surfaces.flush(3)
        assert main_win.visible is True and tab_win.visible is False and tab.visible is False
        assert json.loads(prefs.read_text(encoding="utf-8"))["surface"] == "full"
        host.toggle()  # the next tray click toggles the full window, not a stale tab
        assert surfaces.flush(3) and main_win.visible is False
        assert tab_win.calls.count("show") == 1
    finally:
        listener.stop()


def test_the_listener_stops_cleanly_and_survives_a_failing_handler():
    from spend_app.native_activation import ActivationListener, signal_running_instance

    events = FakeEvents()
    calls = []

    def handler():
        calls.append(1)
        raise RuntimeError("boom")

    listener = ActivationListener(handler, events=events)
    assert listener.start()
    signal_running_instance(events=events)
    assert wait_for(lambda: calls)
    signal_running_instance(events=events)
    assert wait_for(lambda: len(calls) == 2)
    listener.stop()
    listener.stop()


def test_focus_existing_window_asks_the_running_process_first(monkeypatch):
    if sys.platform != "win32":
        pytest.skip("Windows only")
    import ctypes

    monkeypatch.setattr(ww, "signal_running_instance", lambda: True)

    class Boom:
        def __getattr__(self, name):
            raise AssertionError("raw Win32 calls must not run when the owner was signalled")

    monkeypatch.setattr(ctypes, "windll", type("W", (), {"user32": Boom()})())
    assert ww._focus_existing_window("BURNRATE") is True
