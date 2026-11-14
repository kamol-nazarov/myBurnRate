"""Notification-area icon for the native window (Windows only).

Design: a WinForms ``NotifyIcon`` created on the form's own UI thread. The form
thread already runs the WinForms message loop that pywebview started, so the
icon gets its click, menu and balloon events with no second message window or
thread, no ``NOTIFYICONDATA`` plumbing, and the framework removes the icon in
``Dispose``. ``create`` must be called on the UI thread (the host does it from
pywebview's ``before_show``); every other method is safe from any thread.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path

LOGGER = logging.getLogger(__name__)

BALLOON_TIMEOUT_MS = 10_000
TOOLTIP_LIMIT = 63
TITLE_LIMIT = 63
MESSAGE_LIMIT = 255


def _clip(text: str, limit: int) -> str:
    text = str(text or "")
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def run_on_ui(native, work: Callable[[], None]) -> None:
    """Run ``work`` on ``native``'s UI thread without blocking the caller.

    Runs inline when already on that thread, or when the form has no live
    handle (before it is created, or after it closed).
    """
    try:
        if native is None or native.IsDisposed or not native.IsHandleCreated:
            work()
        elif native.InvokeRequired:
            from System import Func, Type  # type: ignore[import-not-found]

            native.BeginInvoke(Func[Type](work))
        else:
            work()
    except Exception:
        LOGGER.debug("UI-thread call skipped", exc_info=True)


class TrayController:
    def __init__(
        self,
        native,
        icon_path: Path | None,
        *,
        on_toggle: Callable[[], None],
        on_show: Callable[[], None],
        on_refresh: Callable[[], None],
        on_settings: Callable[[], None],
        on_quit: Callable[[], None],
        tooltip: str = "BURNRATE",
    ) -> None:
        self._native = native
        self._icon_path = icon_path
        self._handlers = {
            "toggle": on_toggle,
            "show": on_show,
            "refresh": on_refresh,
            "settings": on_settings,
            "quit": on_quit,
        }
        self._tooltip = _clip(tooltip, TOOLTIP_LIMIT)
        self._icon = None
        self._menu = None
        self._owned_icon = None
        self._disposed = False
        self._balloon_click: Callable[[], None] | None = None

    @property
    def available(self) -> bool:
        return self._icon is not None and not self._disposed

    def create(self) -> bool:
        """Build the icon. Call on the UI thread; returns whether it exists."""
        if sys.platform != "win32" or self._icon is not None:
            return self.available
        try:
            import System.Drawing  # type: ignore[import-not-found]
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            glyph = None
            if self._icon_path is not None and self._icon_path.is_file():
                glyph = self._owned_icon = System.Drawing.Icon(str(self._icon_path))
            elif getattr(self._native, "Icon", None) is not None:
                glyph = self._native.Icon
            if glyph is None:
                return False

            menu = WinForms.ContextMenuStrip()
            show_item = WinForms.ToolStripMenuItem("Show BURNRATE")
            show_item.Font = System.Drawing.Font(
                show_item.Font, System.Drawing.FontStyle.Bold
            )
            items = (
                (show_item, "show"),
                (WinForms.ToolStripMenuItem("Refresh now"), "refresh"),
                (WinForms.ToolStripMenuItem("Settings"), "settings"),
                (None, None),
                (WinForms.ToolStripMenuItem("Quit"), "quit"),
            )
            for item, name in items:
                if item is None:
                    menu.Items.Add(WinForms.ToolStripSeparator())
                    continue
                item.Click += lambda _s, _e, key=name: self._fire(key)
                menu.Items.Add(item)

            icon = WinForms.NotifyIcon()
            icon.Icon = glyph
            icon.Text = self._tooltip
            icon.ContextMenuStrip = menu
            icon.MouseClick += self._on_mouse_click
            icon.BalloonTipClicked += self._on_balloon_clicked
            icon.Visible = True
            self._menu, self._icon = menu, icon
            return True
        except Exception:
            LOGGER.warning("Could not create the notification-area icon", exc_info=True)
            self._release()
            return False

    def _fire(self, name: str) -> None:
        try:
            self._handlers[name]()
        except Exception:
            LOGGER.debug("Tray action %s failed", name, exc_info=True)

    def _on_mouse_click(self, _sender, event) -> None:
        import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

        if event.Button == WinForms.MouseButtons.Left:
            self._fire("toggle")

    def set_tooltip(self, text: str) -> None:
        self._tooltip = _clip(text, TOOLTIP_LIMIT)

        def apply() -> None:
            if self._icon is not None and not self._disposed:
                self._icon.Text = self._tooltip

        run_on_ui(self._native, apply)

    def _on_balloon_clicked(self, _sender, _event) -> None:
        action = self._balloon_click or self._handlers["show"]
        try:
            action()
        except Exception:
            LOGGER.debug("Balloon action failed", exc_info=True)

    def notify(
        self,
        title: str,
        message: str,
        *,
        critical: bool = False,
        on_click: Callable[[], None] | None = None,
    ) -> None:
        """Show a balloon; clicking it runs ``on_click`` (default: show the window)."""

        def show() -> None:
            if self._icon is None or self._disposed:
                return
            self._balloon_click = on_click
            import System.Windows.Forms as WinForms  # type: ignore[import-not-found]

            kind = (
                WinForms.ToolTipIcon.Error if critical else WinForms.ToolTipIcon.Warning
            )
            self._icon.ShowBalloonTip(
                BALLOON_TIMEOUT_MS,
                _clip(title, TITLE_LIMIT) or "BURNRATE",
                _clip(message, MESSAGE_LIMIT) or " ",
                kind,
            )

        run_on_ui(self._native, show)

    def dispose(self) -> None:
        """Remove the icon from the notification area. Idempotent."""
        if self._disposed:
            return
        self._disposed = True
        run_on_ui(self._native, self._release)

    def _release(self) -> None:
        icon, menu, owned = self._icon, self._menu, self._owned_icon
        self._icon = self._menu = self._owned_icon = None
        for resource, hide in ((icon, True), (menu, False), (owned, False)):
            if resource is None:
                continue
            try:
                if hide:
                    resource.Visible = False
                resource.Dispose()
                if hide:
                    LOGGER.info("Notification-area icon removed")
            except Exception:
                LOGGER.debug("Tray resource release skipped", exc_info=True)
