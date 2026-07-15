import pytest


try:
    from poed import badges
except (ImportError, ValueError) as exc:  # pragma: no cover - depends on GTK stack
    pytest.skip(f"GTK badge overlay unavailable: {exc}", allow_module_level=True)


def test_badge_input_region_is_empty_for_clickthrough():
    region = badges._empty_input_region()

    assert not region.contains_point(0, 0)
    assert not region.contains_point(100, 100)
