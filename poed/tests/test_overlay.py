import pytest


try:
    from poed import overlay
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on GTK stack
    pytest.skip(f"GTK overlay unavailable: {exc}", allow_module_level=True)


def test_panel_background_is_thirty_percent_more_transparent():
    assert b"window.poe-overlay-window, .poe-overlay-window" in overlay._CSS
    assert b"background: transparent; }" in overlay._CSS
    assert b".poe-panel { background: rgba(11,11,14,0.67);" in overlay._CSS


def test_screen_scan_list_uses_transparent_internal_backgrounds():
    assert b".poe-panel-scroll, .poe-panel-scroll viewport," in overlay._CSS
    assert b"list.poe-panel-list, list.poe-panel-list row," in overlay._CSS
    assert b"background: transparent;" in overlay._CSS


def test_scan_route_badge_has_css():
    assert b".poe-route-badge" in overlay._CSS
    assert b"rgba(0,0,0,0.72)" in overlay._CSS


def test_expedition_scan_badge_is_larger():
    assert b".poe-badge-expedition" in overlay._CSS
    assert b"font-size: 26px;" in overlay._CSS
    assert b".poe-badge-expedition .poe-badge-name { font-size: 18px;" in overlay._CSS
