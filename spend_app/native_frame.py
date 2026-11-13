"""Client-only Windows frame, retaining the native taskbar and window manager.

WM_NCCALCSIZE makes the complete window a client area. Keep Tk's managed
window (not override-redirect), so minimize, restore, Alt+F4 and snapping work.
Unprocessed messages always continue through the original Tk window procedure.
"""

from __future__ import annotations

import logging
import sys

CLIENT = 1
CAPTION = 2
LEFT, RIGHT, TOP, TOPLEFT, TOPRIGHT, BOTTOM, BOTTOMLEFT, BOTTOMRIGHT = range(10, 18)
WS_CAPTION = 0x00C00000


def edge_hit(rect, x, y, border, *, maximized=False):
    left, top, right, bottom = rect
    if not left <= x < right or not top <= y < bottom:
        return 0
    if maximized:
        return CLIENT
    west, east = x < left + border, x >= right - border
    north, south = y < top + border, y >= bottom - border
    if north:
        return TOPLEFT if west else TOPRIGHT if east else TOP
    if south:
        return BOTTOMLEFT if west else BOTTOMRIGHT if east else BOTTOM
    return LEFT if west else RIGHT if east else CLIENT


def screen_point(value):
    # GET_X/Y_LPARAM uses signed 16-bit coordinates, including other monitors.
    def signed(number):
        return (number & 0x7FFF) - (number & 0x8000)

    return signed(value & 0xFFFF), signed((value >> 16) & 0xFFFF)


class NativeFrame:
    def __init__(self, window):
        self.window = window
        self.enabled = False
        self.handle = 0
        self._hooks = {}
        self._refreshing = False
        self._edge_cursor = False
        if sys.platform != "win32":
            return
        self._load_api()
        self.refresh()
        window.bind("<Map>", self._on_map, add="+")
        window.bind("<Configure>", self._on_configure, add="+")
        # Tk's child windows can receive edge clicks before their wrapper.
        window.bind("<ButtonPress-1>", self._resize, add="+")
        window.bind("<Motion>", self._motion, add="+")

    def _load_api(self):
        import ctypes as c
        from ctypes import wintypes as w

        self.c, self.w = c, w
        self.api = api = c.WinDLL("user32", use_last_error=True)
        pointer = c.c_ssize_t
        self._proc_type = c.WINFUNCTYPE(pointer, w.HWND, w.UINT, w.WPARAM, w.LPARAM)
        self._callback = self._proc_type(self._procedure)
        self._callback_address = c.cast(self._callback, c.c_void_p).value
        get_name = (
            "GetWindowLongPtrW" if c.sizeof(c.c_void_p) == 8 else "GetWindowLongW"
        )
        set_name = (
            "SetWindowLongPtrW" if c.sizeof(c.c_void_p) == 8 else "SetWindowLongW"
        )
        self.get_long, self.set_long = getattr(api, get_name), getattr(api, set_name)
        self.get_long.argtypes, self.get_long.restype = [w.HWND, c.c_int], pointer
        self.set_long.argtypes, self.set_long.restype = (
            [w.HWND, c.c_int, pointer],
            pointer,
        )
        api.GetParent.argtypes, api.GetParent.restype = [w.HWND], w.HWND
        api.CallWindowProcW.argtypes = [c.c_void_p, w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        api.CallWindowProcW.restype = pointer
        api.DefWindowProcW.argtypes = [w.HWND, w.UINT, w.WPARAM, w.LPARAM]
        api.DefWindowProcW.restype = pointer
        api.SetWindowPos.argtypes = [
            w.HWND,
            w.HWND,
            c.c_int,
            c.c_int,
            c.c_int,
            c.c_int,
            w.UINT,
        ]
        api.SetWindowPos.restype = w.BOOL
        api.GetWindowRect.argtypes, api.GetWindowRect.restype = (
            [w.HWND, c.POINTER(w.RECT)],
            w.BOOL,
        )
        api.IsZoomed.argtypes, api.IsZoomed.restype = [w.HWND], w.BOOL
        api.IsIconic.argtypes, api.IsIconic.restype = [w.HWND], w.BOOL
        api.SendMessageW.argtypes, api.SendMessageW.restype = (
            [w.HWND, w.UINT, w.WPARAM, w.LPARAM],
            pointer,
        )
        api.PostMessageW.argtypes, api.PostMessageW.restype = (
            [w.HWND, w.UINT, w.WPARAM, w.LPARAM],
            w.BOOL,
        )
        api.ReleaseCapture.argtypes, api.ReleaseCapture.restype = [], w.BOOL
        api.ShowWindow.argtypes, api.ShowWindow.restype = [w.HWND, c.c_int], w.BOOL
        api.MonitorFromWindow.argtypes, api.MonitorFromWindow.restype = (
            [w.HWND, w.DWORD],
            w.HANDLE,
        )

        class MonitorInfo(c.Structure):
            _fields_ = [
                ("size", w.DWORD),
                ("monitor", w.RECT),
                ("work", w.RECT),
                ("flags", w.DWORD),
            ]

        self.MonitorInfo = MonitorInfo
        api.GetMonitorInfoW.argtypes, api.GetMonitorInfoW.restype = (
            [w.HANDLE, c.POINTER(MonitorInfo)],
            w.BOOL,
        )
        if hasattr(api, "GetDpiForWindow"):
            api.GetDpiForWindow.argtypes, api.GetDpiForWindow.restype = [w.HWND], w.UINT
        api.LoadCursorW.argtypes, api.LoadCursorW.restype = (
            [w.HINSTANCE, c.c_void_p],
            w.HANDLE,
        )
        api.SetCursor.argtypes, api.SetCursor.restype = [w.HANDLE], w.HANDLE

    def refresh(self):
        if sys.platform != "win32" or self._refreshing:
            return
        self._refreshing = True
        try:
            if not self.window.winfo_exists():
                return
            self.window.update_idletasks()
            handle = (
                self.api.GetParent(self.window.winfo_id()) or self.window.winfo_id()
            )
            if handle not in self._hooks:
                old = self.get_long(handle, -4)  # GWLP_WNDPROC
                self._hooks[handle] = old
                self.c.set_last_error(0)
                result = self.set_long(handle, -4, self._callback_address)
                if not result and self.c.get_last_error():
                    self._hooks.pop(handle, None)
                    raise self.c.WinError(self.c.get_last_error())
            self.handle = handle
            self.set_long(handle, -16, self.get_long(handle, -16) & ~WS_CAPTION)
            # Keep the main app in Alt+Tab and the taskbar; no tool-window trick.
            self.set_long(handle, -20, (self.get_long(handle, -20) | 0x40000) & ~0x80)
            self.api.SetWindowPos(
                handle, None, 0, 0, 0, 0, 0x37
            )  # frame changed, no move/size/activation
            self.enabled = True
        finally:
            self._refreshing = False

    def _on_map(self, event):
        if event.widget == self.window:
            self.refresh()

    def _on_configure(self, event):
        if event.widget == self.window and not self._refreshing:
            handle = (
                self.api.GetParent(self.window.winfo_id()) or self.window.winfo_id()
            )
            if handle != self.handle:
                self.refresh()

    def _hit(self, x, y):
        rect = self.w.RECT()
        if not self.handle or not self.api.GetWindowRect(
            self.handle, self.c.byref(rect)
        ):
            return CLIENT
        dpi = (
            self.api.GetDpiForWindow(self.handle)
            if hasattr(self.api, "GetDpiForWindow")
            else 96
        )
        return edge_hit(
            (rect.left, rect.top, rect.right, rect.bottom),
            x,
            y,
            max(6, round(7 * dpi / 96)),
            maximized=bool(self.api.IsZoomed(self.handle)),
        )

    def _procedure(self, handle, message, wparam, lparam):
        old = self._hooks.get(handle)
        try:
            if message == 0x83:  # WM_NCCALCSIZE: the whole window is app content.
                if (
                    lparam
                    and self.api.IsZoomed(handle)
                    and not self.api.IsIconic(handle)
                ):
                    monitor = self.MonitorInfo()
                    monitor.size = self.c.sizeof(monitor)
                    if self.api.GetMonitorInfoW(
                        self.api.MonitorFromWindow(handle, 2), self.c.byref(monitor)
                    ):
                        rect = self.w.RECT.from_address(lparam)
                        rect.left, rect.top = monitor.work.left, monitor.work.top
                        rect.right, rect.bottom = (
                            monitor.work.right,
                            monitor.work.bottom,
                        )
                return 0
            if message == 0x84:  # WM_NCHITTEST: native edge/corner resizing.
                return self._hit(*screen_point(lparam))
            if (
                message == 0x82
            ):  # WM_NCDESTROY: remove the subclass before Tk tears down.
                if old and self.get_long(handle, -4) == self._callback_address:
                    self.set_long(handle, -4, old)
                self._hooks.pop(handle, None)
                if self.handle == handle:
                    self.handle, self.enabled = 0, False
        except Exception:
            logging.getLogger(__name__).exception("Native frame message failed")
        if old:
            return self.api.CallWindowProcW(old, handle, message, wparam, lparam)
        return self.api.DefWindowProcW(handle, message, wparam, lparam)

    def drag_region(self, widget):
        if not self.enabled:
            return
        if widget.winfo_class() in {
            "Button",
            "TButton",
            "TCombobox",
            "Entry",
            "TEntry",
        }:
            return
        widget.bind("<ButtonPress-1>", self._drag, add="+")
        widget.bind("<Double-Button-1>", self._maximize, add="+")
        for child in widget.winfo_children():
            self.drag_region(child)

    def _begin(self, hit, event):
        self.api.ReleaseCapture()
        point = ((event.y_root & 0xFFFF) << 16) | (event.x_root & 0xFFFF)
        # Enter Windows' modal move loop after this Tcl/Python event callback
        # returns. Synchronous dispatch can reenter Tk with no Tcl thread state.
        self.api.PostMessageW(self.handle, 0xA1, hit, point)  # WM_NCLBUTTONDOWN
        return "break"

    def _drag(self, event):
        hit = self._hit(event.x_root, event.y_root)
        return self._begin(hit if LEFT <= hit <= BOTTOMRIGHT else CAPTION, event)

    def _resize(self, event):
        if self.enabled:
            hit = self._hit(event.x_root, event.y_root)
            if LEFT <= hit <= BOTTOMRIGHT:
                return self._begin(hit, event)
        return None

    def _motion(self, event):
        if not self.enabled:
            return
        hit = self._hit(event.x_root, event.y_root)
        cursor = {
            LEFT: 32644,
            RIGHT: 32644,
            TOP: 32645,
            BOTTOM: 32645,
            TOPLEFT: 32642,
            BOTTOMRIGHT: 32642,
            TOPRIGHT: 32643,
            BOTTOMLEFT: 32643,
        }.get(hit)
        if cursor or self._edge_cursor:
            self.api.SetCursor(self.api.LoadCursorW(None, cursor or 32512))
        self._edge_cursor = bool(cursor)

    def _maximize(self, _event=None):
        command = 0xF120 if self.api.IsZoomed(self.handle) else 0xF030
        self.api.PostMessageW(self.handle, 0x112, command, 0)  # WM_SYSCOMMAND
        return "break"

    def minimize(self):
        self.window.iconify()
