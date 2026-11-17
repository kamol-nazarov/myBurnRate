"""The native drag loop: the tab follows the cursor by polling, not by page mouse events."""

from spend_app import native_geometry
from spend_app.native_geometry import Rect
from tests_spend.test_tab_review import placed  # noqa: F401 -- the "placed" fixture


class ScriptedMouse:
    """A cursor path with the left button held for the first N reads."""

    def __init__(self, path, down_reads):
        self.path, self.reads, self.down_reads, self.index = list(path), 0, down_reads, 0

    def left_button_down(self):
        self.reads += 1
        return self.reads <= self.down_reads

    def cursor_pos(self):
        point = self.path[min(self.index, len(self.path) - 1)]
        self.index += 1
        return point


def test_the_native_drag_follows_the_cursor_and_ends_with_the_button(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    state["rect"] = Rect(500, 100, 1400, 160)
    mouse = ScriptedMouse([(700, 120), (710, 125), (760, 150), (900, 170)], down_reads=4)
    moves = []
    monkeypatch.setattr(native_geometry, "left_button_down", mouse.left_button_down)
    monkeypatch.setattr(native_geometry, "cursor_pos", mouse.cursor_pos)
    monkeypatch.setattr(host, "_move_to", lambda x, y: moves.append((x, y)))
    settled = []
    monkeypatch.setattr(host, "note_moved", lambda: settled.append(1))
    monkeypatch.setattr("spend_app.tab_window.time.sleep", lambda s: None)
    host._drag_loop((700, 120), (500, 100))
    assert moves[:3] == [(500, 100), (510, 105), (560, 130)]  # the window keeps its offset from the cursor
    assert host._dragging is False and settled == [1]  # and is pulled back on screen afterwards


def test_a_drag_closes_an_open_card_first_and_starts_only_with_the_button_down(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    state["rect"] = Rect(500, 100, 1400, 160)
    host.expand(900, 330, (60, 300, 250))
    assert host._expanded is not None
    started = []

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            started.append((target.__name__, args))

        def start(self):
            pass

    monkeypatch.setattr("spend_app.tab_window.threading.Thread", FakeThread)
    monkeypatch.setattr(native_geometry, "cursor_pos", lambda: (700, 120))
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: False)
    host.begin_drag()
    assert host._expanded is None and started == []  # the card is gone; no button, no drag
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: True)
    host.begin_drag()
    assert len(started) == 1 and started[0][0] == "_drag_loop" and started[0][1][0] == (700, 120)
    assert host._dragging is True
    host.begin_drag()
    assert len(started) == 1  # a drag already running is not started twice


def test_the_drag_starts_from_where_the_press_happened_not_where_the_threshold_was_crossed(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    state["rect"] = Rect(500, 100, 1400, 160)
    started = []

    class FakeThread:
        def __init__(self, target, args=(), name=None, daemon=None):
            started.append(args)

        def start(self):
            pass

    monkeypatch.setattr("spend_app.tab_window.threading.Thread", FakeThread)
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: True)
    monkeypatch.setattr(native_geometry, "cursor_pos", lambda: (700, 120))
    host.note_press()  # the press at (700, 120)...
    monkeypatch.setattr(native_geometry, "cursor_pos", lambda: (706, 123))  # ...6 px later the drag begins
    host.begin_drag()
    assert started == [((700, 120), (500, 100))]


def test_a_finished_drag_saves_the_position_and_clamps_it(placed, monkeypatch):
    host, state = placed
    host.set_base(900)
    host.visible = True
    notified, settled = [], []
    monkeypatch.setattr(host.tracker, "notify_changed", lambda: notified.append(1))
    monkeypatch.setattr(host, "note_moved", lambda: settled.append(1))
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: False)
    host._press = ((1, 1), 0.0)
    host._drag_loop((1, 1), (0, 0))
    assert notified == [1] and settled == [1] and host._press is None
    state["rect"] = Rect(-300, 40, 600, 100)
    host._settle()
    assert state["rect"].left == 0 and notified == [1, 1]  # the clamped spot is saved too


def test_a_hidden_or_closing_tab_never_starts_a_drag(placed, monkeypatch):
    host, state = placed
    monkeypatch.setattr(native_geometry, "left_button_down", lambda: True)
    monkeypatch.setattr(native_geometry, "cursor_pos", lambda: (1, 1))
    host.visible = False
    host.begin_drag()
    assert host._dragging is False
    host.visible, host.quitting = True, True
    host.begin_drag()
    assert host._dragging is False
