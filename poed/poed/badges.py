"""Fullscreen transient scan overlay.

This is one real layer-shell surface with an empty input region. The badges are
visible, but pointer hover/clicks pass through to the game; the native host
dismisses the scan UI when game focus returns.
"""

import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gtk4LayerShell as LayerShell  # noqa: E402

from . import views

_BADGE_Y_OFFSETS = {
    "expedition": 51,
    "runeshape": 92,
}


def _empty_input_region() -> cairo.Region:
    return cairo.Region()


def _anchor_to_monitor(win: Gtk.Window, monitor_name: str | None) -> None:
    display = Gdk.Display.get_default()
    if display is None or not monitor_name:
        return
    monitors = display.get_monitors()
    for i in range(monitors.get_n_items()):
        mon = monitors.get_item(i)
        if mon is not None and mon.get_connector() == monitor_name:
            LayerShell.set_monitor(win, mon)
            break


class ScanOverlayLayer:
    """Fullscreen scan badge surface with no pointer targeting."""

    def __init__(self, application):
        self._win = Gtk.Window(application=application)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self._win, "waystone-scan-overlay")
        # Anchor all four edges: the surface covers the whole output.
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.LEFT,
                  LayerShell.Edge.BOTTOM, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self._win, e, True)
        # NONE: pointer events still work, but this surface must not compete for
        # keyboard focus with the result panel or the game.
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)

        self._fixed = Gtk.Fixed()
        self._fixed.set_hexpand(True)
        self._fixed.set_vexpand(True)
        self._fixed.set_can_target(False)
        self._fixed.add_css_class("poe-badges")
        self._win.set_child(self._fixed)
        self._win.add_css_class("poe-badges")

    def show(self, matches: list[dict], min_exalted: float,
             monitor_name: str | None, scanner_id: str | None = None) -> None:
        for child in list(self._fixed):
            self._fixed.remove(child)
        _anchor_to_monitor(self._win, monitor_name)
        for m in matches:
            try:
                x = int(m["x"])
                y = int(m["y"])
                w = int(m.get("w") or 1)
                h = int(m.get("h") or 1)
            except (KeyError, TypeError, ValueError):
                continue
            if m.get("markerOnly"):
                marker = Gtk.Box()
                marker.add_css_class("poe-scan-marker")
                marker.set_size_request(max(1, w), max(1, h))
                marker.set_tooltip_text(views.match_badge_label(m))
                # Marker-only boxes sit over the item body; keep them
                # pointer-transparent so in-game item hover still works.
                marker.set_can_target(False)
                self._fixed.put(marker, max(0, x), max(0, y))
                continue

            price = views.match_total_value(m)
            price_text = views.match_badge_price_label(m)
            scan_kind = str(m.get("scanKind") or "")
            expedition = scan_kind == "expedition"
            runeshape = scan_kind == "runeshape"
            name_text = views.match_badge_name_label(m)
            if not price_text and not name_text:
                continue
            badge = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=0)
            badge.add_css_class("poe-badge")
            if expedition:
                badge.add_css_class("poe-badge-expedition")
            if runeshape:
                badge.add_css_class("poe-badge-runeshape")
            badge.set_tooltip_text(views.match_badge_label(m))
            badge.set_can_target(False)

            if price_text:
                price_label = Gtk.Label(label=price_text, xalign=0.0)
                price_label.add_css_class("poe-badge-price")
                price_label.add_css_class(views.value_tier_css(price))
                badge.append(price_label)

            if name_text:
                name_label = Gtk.Label(label=name_text, xalign=0.0)
                name_label.add_css_class("poe-badge-name")
                if runeshape:
                    name_label.add_css_class("poe-badge-runeshape-name")
                badge.append(name_label)

            # Deterministic item-relative placement: never shift horizontally to
            # avoid overlap, because that can make a Ritual price look attached
            # to the wrong cell.
            badge_y = y - _BADGE_Y_OFFSETS.get(scan_kind, 34)
            badge_x = max(0, x)
            badge_y = max(0, badge_y)
            self._fixed.put(badge, badge_x, badge_y)
        self._add_route_badge(views.screen_scan_route_label(scanner_id, matches))
        self._win.set_visible(True)
        self._apply_clickthrough_input_region()
        GLib.idle_add(self._apply_clickthrough_input_region)

    def _apply_clickthrough_input_region(self):
        surface = self._win.get_surface()
        if surface is not None:
            surface.set_input_region(_empty_input_region())
        return GLib.SOURCE_REMOVE

    def _add_route_badge(self, text: str) -> None:
        if not text:
            return
        badge = Gtk.Box()
        badge.add_css_class("poe-route-badge")
        badge.set_can_target(False)
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("poe-route-badge-text")
        badge.append(label)
        x, y = 8, 8
        self._fixed.put(badge, x, y)

    def is_visible(self) -> bool:
        return self._win.get_visible()

    def hide(self) -> None:
        surface = self._win.get_surface()
        if surface is not None:
            surface.set_input_region(_empty_input_region())
        self._win.set_visible(False)
