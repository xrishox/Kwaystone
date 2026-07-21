"""EE2 overlay placement math, exercised without creating real GTK windows."""

import types

import pytest

try:
    from poed import ee2_overlay
    from poed.image_geometry import Rect
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on GTK stack
    pytest.skip(f"ee2 overlay unavailable: {exc}", allow_module_level=True)


def _capture_place(monkeypatch):
    margins = {}
    anchors = []
    monkeypatch.setattr(
        ee2_overlay.draggable, "set_monitor_for_rect", lambda _win, _rect: None
    )
    monkeypatch.setattr(
        ee2_overlay.draggable, "window_monitor_size", lambda _win: (2560, 1440)
    )
    monkeypatch.setattr(
        ee2_overlay.LayerShell,
        "set_anchor",
        lambda _win, edge, on: anchors.append((edge, bool(on))),
    )
    monkeypatch.setattr(
        ee2_overlay.LayerShell,
        "set_margin",
        lambda _win, edge, value: margins.__setitem__(edge, value),
    )
    return margins, anchors


def _place(side, output_rect, game_rect):
    fake = types.SimpleNamespace(_win=object())
    ee2_overlay.Ee2PriceOverlay._place(fake, side, output_rect, game_rect)


def test_place_right_side_margins_track_game_rect(monkeypatch):
    margins, _anchors = _capture_place(monkeypatch)

    _place("right", Rect(0, 0, 2560, 1440), Rect(100, 50, 2000, 1200))

    Edge = ee2_overlay.LayerShell.Edge
    assert margins[Edge.TOP] == 50
    assert margins[Edge.BOTTOM] == 1440 - 50 - 1200
    assert margins[Edge.RIGHT] == 2560 - 100 - 2000
    assert margins[Edge.LEFT] == 0


def test_place_left_side_margins_track_game_rect(monkeypatch):
    margins, _anchors = _capture_place(monkeypatch)

    _place("left", Rect(0, 0, 2560, 1440), Rect(100, 50, 2000, 1200))

    Edge = ee2_overlay.LayerShell.Edge
    assert margins[Edge.LEFT] == 100
    assert margins[Edge.RIGHT] == 0


def test_place_without_game_rect_covers_output(monkeypatch):
    margins, _anchors = _capture_place(monkeypatch)

    _place("right", Rect(0, 0, 2560, 1440), None)

    Edge = ee2_overlay.LayerShell.Edge
    assert margins[Edge.TOP] == 0
    assert margins[Edge.BOTTOM] == 0
    assert margins[Edge.RIGHT] == 0


def test_place_never_emits_negative_margins(monkeypatch):
    margins, _anchors = _capture_place(monkeypatch)

    # Game rect beyond the output edges: top/left pass through (the
    # compositor clamps them), bottom/right must never go negative.
    _place("right", Rect(0, 0, 2560, 1440), Rect(3000, 2000, 1000, 1000))

    Edge = ee2_overlay.LayerShell.Edge
    assert margins[Edge.TOP] == 2000
    assert margins[Edge.BOTTOM] == 0
    assert margins[Edge.RIGHT] == 0
    assert all(value >= 0 for value in margins.values())
