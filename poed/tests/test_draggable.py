from poed.draggable import clamp_position, _parse_cursorpos


def test_clamp_within_bounds_unchanged():
    assert clamp_position(100, 200, 2560, 1440, 400, 600) == (100, 200)


def test_clamp_negative_to_zero():
    assert clamp_position(-50, -10, 2560, 1440, 400, 600) == (0, 0)


def test_clamp_past_right_bottom_edges():
    # window must stay fully on-screen: max x = mon_w - win_w
    assert clamp_position(9999, 9999, 2560, 1440, 400, 600) == (2160, 840)


def test_clamp_window_larger_than_monitor_pins_zero():
    assert clamp_position(50, 50, 800, 600, 1000, 700) == (0, 0)


def test_parse_cursorpos_typical():
    assert _parse_cursorpos("2314, 880") == (2314, 880)


def test_parse_cursorpos_square_coords():
    assert _parse_cursorpos("1440, 1440") == (1440, 1440)


def test_parse_cursorpos_garbage_returns_none():
    assert _parse_cursorpos("") is None
    assert _parse_cursorpos("nonsense") is None
    assert _parse_cursorpos("5") is None
