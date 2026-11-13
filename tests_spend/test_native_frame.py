"""Window-manager behavior and signed multi-monitor edge coordinates."""

import sys
import time

import pytest

from spend_app.native_frame import NativeFrame, edge_hit, screen_point


@pytest.mark.parametrize(
    ("point", "expected"),
    [
        ((-400, -200), 13),
        ((-200, -200), 12),
        ((-1, -200), 14),
        ((-400, 0), 10),
        ((-200, 0), 1),
        ((-1, 0), 11),
        ((-400, 199), 16),
        ((-200, 199), 15),
        ((-1, 199), 17),
        ((-401, 0), 0),
        ((0, 0), 0),
        ((-200, 200), 0),
    ],
)
def test_resize_edges_use_screen_coordinates(point, expected):
    assert edge_hit((-400, -200, 0, 200), *point, 7) == expected


def test_maximized_window_has_no_resize_edges():
    assert edge_hit((-400, -200, 0, 200), -400, -200, 7, maximized=True) == 1


@pytest.mark.parametrize(
    "point", [(-1200, -900), (1200, -900), (-1200, 900), (1200, 900)]
)
def test_native_pointer_message_decodes_negative_monitor_positions(point):
    x, y = point
    assert screen_point(((y & 0xFFFF) << 16) | (x & 0xFFFF)) == point


def test_native_move_and_maximize_wait_for_the_tcl_callback_to_return():
    from types import SimpleNamespace

    queued = []

    class Api:
        def ReleaseCapture(self):
            pass

        def PostMessageW(self, *message):
            queued.append(message)

        def SendMessageW(self, *args):
            pytest.fail(
                "Synchronous modal dispatch reenters Tk with no Tcl thread state"
            )

        def IsZoomed(self, handle):
            return False

    frame = object.__new__(NativeFrame)
    frame.api, frame.handle = Api(), 17
    assert frame._begin(2, SimpleNamespace(x_root=-123, y_root=70)) == "break"
    assert queued[0][:3] == (17, 0xA1, 2)
    assert screen_point(queued[0][3]) == (-123, 70)
    assert frame._maximize() == "break"
    assert queued[1] == (17, 0x112, 0xF030, 0)


@pytest.mark.skipif(sys.platform != "win32", reason="Win32 window-manager integration")
def test_managed_frameless_window_minimizes_restores_and_closes():
    import ctypes as c
    import tkinter as tk
    from ctypes import wintypes as w

    from spend_app.native_desktop import DemoClient, UsageWindow

    root = tk.Tk()
    root.withdraw()
    root.attributes("-alpha", 0)
    app = UsageWindow(root, DemoClient(), demo=True)
    chrome = app.chrome

    def pump():
        root.update()
        time.sleep(0.03)
        root.update()

    try:
        root.deiconify()
        pump()
        assert chrome.enabled
        assert not root.overrideredirect()
        assert chrome.get_long(chrome.handle, -16) & 0x00C00000 == 0
        assert chrome.get_long(chrome.handle, -20) & 0x40000  # Taskbar app window.
        assert chrome.get_long(chrome.handle, -20) & 0x80 == 0
        rect = w.RECT()
        assert chrome.api.GetWindowRect(chrome.handle, c.byref(rect))
        point = ((rect.top & 0xFFFF) << 16) | (rect.left & 0xFFFF)
        assert chrome.api.SendMessageW(chrome.handle, 0x84, 0, point) == 13

        app.minimize_button.invoke()
        pump()
        assert chrome.api.IsIconic(chrome.handle)
        root.deiconify()
        pump()
        assert not chrome.api.IsIconic(chrome.handle)
        assert root.winfo_viewable()
        assert chrome.get_long(chrome.handle, -16) & 0x00C00000 == 0

        root.attributes("-topmost", True)
        chrome.refresh()
        pump()
        root.attributes("-topmost", False)
        chrome.refresh()
        pump()
        assert chrome.enabled
        assert chrome.get_long(chrome.handle, -16) & 0x00C00000 == 0

        # Maximize must use the work area, including its taskbar exclusion.
        root.state("zoomed")
        pump()
        info = chrome.MonitorInfo()
        info.size = c.sizeof(info)
        assert chrome.api.GetMonitorInfoW(
            chrome.api.MonitorFromWindow(chrome.handle, 2), c.byref(info)
        )
        origin, client = w.POINT(), w.RECT()
        chrome.api.ClientToScreen.argtypes = [w.HWND, c.POINTER(w.POINT)]
        chrome.api.ClientToScreen.restype = w.BOOL
        chrome.api.GetClientRect.argtypes = [w.HWND, c.POINTER(w.RECT)]
        chrome.api.GetClientRect.restype = w.BOOL
        assert chrome.api.ClientToScreen(chrome.handle, c.byref(origin))
        assert chrome.api.GetClientRect(chrome.handle, c.byref(client))
        assert (
            origin.x,
            origin.y,
            origin.x + client.right,
            origin.y + client.bottom,
        ) == (info.work.left, info.work.top, info.work.right, info.work.bottom)
        root.state("normal")
        pump()
        assert chrome.get_long(chrome.handle, -16) & 0x00C00000 == 0

        app.close_button.invoke()
        assert app.closed
        assert (
            not chrome._hooks
        )  # No native callback remains after Tk destroys the window.
    finally:
        if not app.closed:
            app.close()
