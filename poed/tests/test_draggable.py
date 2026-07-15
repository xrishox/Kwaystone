from poed.draggable import clamp_position, clamp_window_position, _parse_cursorpos


class _FakeWindow:
    def __init__(self, width=0, height=0):
        self._width = width
        self._height = height

    def get_width(self):
        return self._width

    def get_height(self):
        return self._height


def test_clamp_within_bounds_unchanged():
    assert clamp_position(100, 200, 2560, 1440, 400, 600) == (100, 200)


def test_clamp_negative_to_zero():
    assert clamp_position(-50, -10, 2560, 1440, 400, 600) == (0, 0)


def test_clamp_past_right_bottom_edges():
    # window must stay fully on-screen: max x = mon_w - win_w
    assert clamp_position(9999, 9999, 2560, 1440, 400, 600) == (2160, 840)


def test_clamp_window_larger_than_monitor_pins_zero():
    assert clamp_position(50, 50, 800, 600, 1000, 700) == (0, 0)


def test_clamp_window_position_uses_default_size_before_gtk_measures():
    win = _FakeWindow(width=1, height=1)

    assert clamp_window_position(
        win,
        457,
        1799,
        default_size=(880, 600),
        monitor_size=(2560, 1440),
    ) == (457, 840)


def test_clamp_window_position_uses_measured_window_size():
    win = _FakeWindow(width=900, height=700)

    assert clamp_window_position(
        win,
        9999,
        9999,
        default_size=(880, 600),
        monitor_size=(2560, 1440),
    ) == (1660, 740)


def test_parse_cursorpos_typical():
    assert _parse_cursorpos("2314, 880") == (2314, 880)


def test_parse_cursorpos_square_coords():
    assert _parse_cursorpos("1440, 1440") == (1440, 1440)


def test_parse_cursorpos_garbage_returns_none():
    assert _parse_cursorpos("") is None
    assert _parse_cursorpos("nonsense") is None
    assert _parse_cursorpos("5") is None
