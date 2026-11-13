"""BURNRATE's graphite/ember design system for native desktop widgets."""

from __future__ import annotations

import sys
from pathlib import Path

BG = "#08080a"
SURFACE = "#111114"
RAISED = "#19191d"
HOVER = "#252529"
LINE = "#29292e"
TEXT = "#f2f0eb"
MUTED = "#a6a29c"
ACCENT = "#ff6b22"
GREEN = "#5ed3a2"
WARNING = "#f0b88c"
FONT = "Segoe UI Variable Text" if sys.platform == "win32" else "Helvetica"
DISPLAY = "Segoe UI Variable Display" if sys.platform == "win32" else FONT

TOOL_COLORS = {
    "claude-code": "#b596ff",
    "claude": "#b596ff",
    "codex": "#7ea6ff",
    "opencode": "#5ed3a2",
    "grok": "#5fd0e3",
    "muse": "#f29bd0",
    "cursor": "#e3c26b",
    "zcode": "#f28b82",
    "antigravity": "#a6e36f",
}


def dark_titlebar(window):
    """Let Windows draw its own caption/buttons with the matching dark surface."""
    if sys.platform != "win32":
        return
    try:
        import ctypes
        from ctypes import wintypes

        window.update_idletasks()
        user32 = ctypes.windll.user32
        user32.GetParent.argtypes = [wintypes.HWND]
        user32.GetParent.restype = wintypes.HWND
        handle = user32.GetParent(window.winfo_id()) or window.winfo_id()
        value = ctypes.c_int(1)
        setter = ctypes.windll.dwmapi.DwmSetWindowAttribute
        setter.argtypes = [
            wintypes.HWND,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        # Windows 10 used 19 before the attribute settled on 20.
        if setter(handle, 20, ctypes.byref(value), ctypes.sizeof(value)) != 0:
            setter(handle, 19, ctypes.byref(value), ctypes.sizeof(value))
    except (OSError, AttributeError):
        pass


def install(root):
    from tkinter import ttk

    root.configure(background=BG)
    root.option_add("*TCombobox*Listbox.font", (FONT, 10))
    root.option_add("*TCombobox*Listbox.background", RAISED)
    root.option_add("*TCombobox*Listbox.foreground", TEXT)
    root.option_add("*TCombobox*Listbox.selectBackground", HOVER)
    root.option_add("*TCombobox*Listbox.selectForeground", TEXT)
    root.option_add("*TCombobox*Listbox.highlightThickness", 0)
    style = ttk.Style(root)
    # Platform themes leave inputs and headers bright on Windows. Clam allows
    # every native Tk control to share this palette, including Settings.
    style.theme_use("clam")
    style.configure(
        ".", background=BG, foreground=TEXT, font=(FONT, 10), bordercolor=LINE
    )
    style.configure("TFrame", background=BG)
    style.configure("TLabel", background=BG, foreground=TEXT)
    style.configure("Muted.TLabel", foreground=MUTED)
    style.configure("Caption.TLabel", foreground=MUTED, font=(FONT, 9))
    style.configure("Title.TLabel", font=(DISPLAY, 21, "bold"))
    style.configure("Value.TLabel", font=(DISPLAY, 44, "bold"))
    style.configure("Metric.TLabel", font=(DISPLAY, 18, "bold"))
    style.configure("Section.TLabel", font=(FONT, 11, "bold"))
    style.configure("Live.TLabel", foreground=GREEN, font=(FONT, 9))
    style.configure("Notice.TLabel", foreground=WARNING, font=(FONT, 9))
    style.configure(
        "TButton",
        background=RAISED,
        foreground=TEXT,
        bordercolor=LINE,
        lightcolor=RAISED,
        darkcolor=RAISED,
        padding=(13, 8),
        focuscolor=ACCENT,
        focusthickness=1,
        borderwidth=1,
    )
    style.map(
        "TButton",
        background=[("disabled", SURFACE), ("pressed", HOVER), ("active", HOVER)],
        foreground=[("disabled", MUTED)],
        bordercolor=[("focus", ACCENT), ("active", "#45454b")],
    )
    style.configure(
        "Quiet.TButton",
        background=BG,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        foreground=MUTED,
        padding=(10, 7),
    )
    style.map(
        "Quiet.TButton",
        background=[("active", RAISED), ("pressed", HOVER)],
        foreground=[("active", TEXT)],
        bordercolor=[("focus", ACCENT), ("active", RAISED)],
    )
    style.configure(
        "Accent.TButton",
        background=ACCENT,
        foreground=BG,
        bordercolor=ACCENT,
        lightcolor=ACCENT,
        darkcolor=ACCENT,
        font=(FONT, 10, "bold"),
    )
    style.map(
        "Accent.TButton",
        background=[("active", "#ff8245"), ("pressed", "#e95b15"), ("disabled", HOVER)],
        foreground=[("disabled", MUTED)],
        bordercolor=[("focus", TEXT)],
    )
    style.configure(
        "Segment.TRadiobutton",
        background=SURFACE,
        foreground=MUTED,
        padding=(12, 8),
        font=(FONT, 9),
        focuscolor=ACCENT,
        borderwidth=0,
    )
    style.layout(
        "Segment.TRadiobutton",
        [
            (
                "Radiobutton.padding",
                {
                    "sticky": "nswe",
                    "children": [
                        (
                            "Radiobutton.focus",
                            {
                                "sticky": "nswe",
                                "children": [("Radiobutton.label", {"sticky": "nswe"})],
                            },
                        )
                    ],
                },
            )
        ],
    )
    style.map(
        "Segment.TRadiobutton",
        background=[("selected", "#303035"), ("active", HOVER)],
        foreground=[("selected", TEXT), ("active", TEXT)],
    )
    style.configure("Segment.TFrame", background=SURFACE)
    style.configure(
        "TCombobox",
        fieldbackground=SURFACE,
        background=SURFACE,
        foreground=TEXT,
        arrowcolor=MUTED,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=(10, 8),
        arrowsize=12,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", SURFACE)],
        foreground=[("readonly", TEXT)],
        selectbackground=[("readonly", SURFACE)],
        selectforeground=[("readonly", TEXT)],
        bordercolor=[("focus", ACCENT)],
    )
    style.configure(
        "TEntry",
        fieldbackground=SURFACE,
        foreground=TEXT,
        insertcolor=TEXT,
        bordercolor=LINE,
        lightcolor=LINE,
        darkcolor=LINE,
        padding=9,
    )
    style.map(
        "TEntry",
        bordercolor=[("focus", ACCENT)],
        fieldbackground=[("readonly", SURFACE)],
    )
    style.configure(
        "TCheckbutton",
        background=BG,
        foreground=TEXT,
        indicatorbackground=RAISED,
        indicatorforeground=ACCENT,
        focuscolor=ACCENT,
        padding=(0, 7),
    )
    style.map(
        "TCheckbutton",
        background=[("active", BG)],
        indicatorbackground=[("selected", ACCENT)],
    )
    style.configure(
        "Treeview",
        background=BG,
        fieldbackground=BG,
        foreground=TEXT,
        borderwidth=0,
        relief="flat",
        rowheight=42,
        font=(FONT, 10),
    )
    style.map(
        "Treeview",
        background=[("selected", "#30231e")],
        foreground=[("selected", TEXT)],
    )
    style.configure(
        "Treeview.Heading",
        background=BG,
        foreground=MUTED,
        borderwidth=0,
        relief="flat",
        font=(FONT, 9),
        padding=(8, 10),
    )
    style.map("Treeview.Heading", background=[("active", BG)])
    style.layout("Treeview", [("Treeview.treearea", {"sticky": "nswe"})])
    style.configure("Usage.Treeview", rowheight=46, font=(FONT, 11))
    style.configure(
        "Vertical.TScrollbar",
        background=HOVER,
        troughcolor=BG,
        borderwidth=0,
        arrowcolor=MUTED,
        arrowsize=11,
    )
    style.map("Vertical.TScrollbar", background=[("active", "#414147")])
    style.layout(
        "Vertical.TScrollbar",
        [
            (
                "Vertical.Scrollbar.trough",
                {
                    "sticky": "ns",
                    "children": [
                        ("Vertical.Scrollbar.thumb", {"sticky": "nswe", "expand": True})
                    ],
                },
            )
        ],
    )
    style.configure("Vertical.TScrollbar", width=6)
    style.configure(
        "TNotebook",
        background=BG,
        borderwidth=0,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        tabmargins=(0, 0, 0, 15),
    )
    style.configure(
        "TNotebook.Tab",
        background=BG,
        foreground=MUTED,
        padding=(18, 11),
        borderwidth=0,
        bordercolor=BG,
        lightcolor=BG,
        darkcolor=BG,
        focuscolor=ACCENT,
    )
    style.map(
        "TNotebook.Tab",
        background=[("selected", RAISED), ("active", SURFACE)],
        foreground=[("selected", TEXT)],
    )
    style.configure(
        "TSeparator", background=LINE, lightcolor=LINE, darkcolor=LINE, bordercolor=LINE
    )
    style.configure("Treeview", rowheight=round(42 * ui_scale(root)))
    style.configure("Usage.Treeview", rowheight=round(46 * ui_scale(root)))
    root.after(0, lambda: dark_titlebar(root))


def brand(parent):
    import tkinter as tk
    from tkinter import ttk

    row = ttk.Frame(parent)
    mark = tk.Canvas(row, width=32, height=32, background=BG, highlightthickness=0)
    mark.create_polygon(
        6,
        0,
        23,
        0,
        29,
        6,
        29,
        23,
        23,
        29,
        6,
        29,
        0,
        23,
        0,
        6,
        smooth=True,
        splinesteps=12,
        fill=ACCENT,
        outline="",
    )
    mark.create_line(9, 20, 20, 9, fill=BG, width=1.8)
    mark.create_line(13, 9, 20, 9, 20, 16, fill=BG, width=1.8)
    resources = Path(__file__).resolve().parents[1]
    for path in (
        resources / "32x32.png",
        resources / "burnrate-assets/32x32.png",
        resources / "src-tauri/icons/32x32.png",
    ):
        if path.is_file():
            mark.image = tk.PhotoImage(master=parent, file=str(path))
            mark.delete("all")
            mark.create_image(0, 0, anchor="nw", image=mark.image)
            break
    mark.pack(side="left", padx=(0, 11))
    ttk.Label(row, text="B U R N R A T E", font=(FONT, 10, "bold")).pack(side="left")
    return row


def ui_scale(root):
    return max(1.0, float(root.tk.call("tk", "scaling")) / (96 / 72))


def size_window(root, width, height, minimum_width, minimum_height):
    # Fonts use points; geometry uses pixels. Fit both high-DPI displays and
    # smaller work areas without making the footer unreachable.
    scale = ui_scale(root)
    available_width = max(320, root.winfo_screenwidth() - 80)
    available_height = max(360, root.winfo_screenheight() - 90)
    root.geometry(
        f"{min(round(width * scale), available_width)}x{min(round(height * scale), available_height)}"
    )
    root.minsize(
        min(round(minimum_width * scale), available_width),
        min(round(minimum_height * scale), available_height),
    )


def notice(parent):
    from tkinter import ttk

    class Notice(ttk.Label):
        def configure(self, cnf=None, **kwargs):
            result = super().configure(cnf, **kwargs)
            if "text" in kwargs:
                if kwargs["text"]:
                    self.pack(fill="x", pady=(0, 8))
                else:
                    self.pack_forget()
            return result

    return Notice(parent, text="", wraplength=580, style="Notice.TLabel")


def tool_icon(root, key):
    """A tiny vector-like tile, drawn in memory; no image files or web assets."""
    import tkinter as tk

    name = str(key)
    family = next(
        (
            family
            for family in TOOL_COLORS
            if name == family or name.startswith(family + "-")
        ),
        "",
    )
    color = TOOL_COLORS.get(family, MUTED)
    image = tk.PhotoImage(master=root, width=40, height=29)
    base = "#212125"
    for y in range(29):
        inset = 4 if y in (0, 28) else 2 if y in (1, 27) else 1 if y in (2, 26) else 0
        image.put(base, to=(inset, y, 29 - inset, y + 1))
    # Simple deliberately geometric provider marks keep the native table crisp.
    pixels = set()
    if "claude" in str(key):
        for t in range(-8, 9):
            pixels.update(
                ((14 + t, 14), (14, 14 + t), (14 + t, 14 + t), (14 + t, 14 - t))
            )
    elif "codex" in str(key):
        for t in range(7):
            pixels.update(((8 + t, 8 + t), (8 + t, 20 - t)))
        pixels.update((x, 21) for x in range(16, 23))
    elif "opencode" in str(key):
        for t in range(9, 21):
            pixels.update(((t, 9), (t, 20), (9, t), (20, t)))
    elif "muse" in str(key):
        for y in range(9, 21):
            pixels.update(((8, y), (21, y)))
        for t in range(7):
            pixels.update(((8 + t, 9 + t), (21 - t, 9 + t)))
    elif "grok" in name:
        for t in range(9, 21):
            pixels.update(((t, 9), (t, 20), (9, t)))
        for t in range(14, 21):
            pixels.update(((t, 15), (20, t)))
    elif "zcode" in name:
        for t in range(9, 21):
            pixels.update(((t, 9), (t, 20), (29 - t, t)))
    elif "antigravity" in name:
        for t in range(13):
            pixels.update(((14 - t // 2, 8 + t), (14 + t // 2, 8 + t)))
        pixels.update((x, 16) for x in range(10, 19))
    else:
        for t in range(7):
            pixels.update(
                ((14 - t, 8 + t), (14 + t, 8 + t), (14 - t, 20 - t), (14 + t, 20 - t))
            )
    for x, y in pixels:
        image.put(color, to=(x, y, x + 1, y + 1))
    zoom = max(1, round(ui_scale(root)))
    return image.zoom(zoom) if zoom > 1 else image
