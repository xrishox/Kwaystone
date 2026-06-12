"""Click-through price badges over scanned unique items.

A fullscreen overlay-layer surface on the game's monitor whose INPUT REGION is
empty, so all pointer/keyboard input passes through to the game. Badges are
labels positioned by Gtk.Fixed at the match coordinates (grim captures monitor
pixels; with scale-1 monitors those ARE layout coordinates relative to the
monitor origin — revisit if a fractional-scale monitor enters the setup).
Dismissed together with the panel (Esc) or replaced by the next scan.
"""
import cairo
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, Gtk4LayerShell as LayerShell  # noqa: E402


class BadgeLayer:
    def __init__(self, application):
        self._win = Gtk.Window(application=application)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self._win, "poe2-overlay-badges")
        # Anchor all four edges: the surface covers the whole output.
        for e in (LayerShell.Edge.TOP, LayerShell.Edge.LEFT,
                  LayerShell.Edge.BOTTOM, LayerShell.Edge.RIGHT):
            LayerShell.set_anchor(self._win, e, True)
        # NONE, never ON_DEMAND/EXCLUSIVE: this surface must not compete for
        # the seat with the panel (iteration-5 lesson: sibling starvation).
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)
        self._fixed = Gtk.Fixed()
        self._win.set_child(self._fixed)
        self._win.add_css_class("poe-badges")
        self._win.connect("realize", self._on_realize)

    @staticmethod
    def _on_realize(win):
        # Empty input region: every event passes through to the game beneath.
        surface = win.get_surface()
        if surface is not None:
            surface.set_input_region(cairo.Region())

    def show(self, matches: list[dict], min_exalted: float,
             monitor_name: str | None) -> None:
        for child in list(self._fixed):
            self._fixed.remove(child)
        if monitor_name:
            monitors = Gdk.Display.get_default().get_monitors()
            for i in range(monitors.get_n_items()):
                mon = monitors.get_item(i)
                if mon is not None and mon.get_connector() == monitor_name:
                    LayerShell.set_monitor(self._win, mon)
                    break
        for m in matches:
            price = m.get("price") or 0
            text = f"{round(price)} ex" if price >= 10 else f"{price:.1f} ex"
            if m.get("ambiguous"):
                text += "?"
            label = Gtk.Label(label=text)
            label.add_css_class(
                "poe-badge-good" if price >= min_exalted else "poe-badge")
            # Float the badge just above the item's matched box.
            self._fixed.put(label, m["x"], max(0, m["y"] - 18))
        self._win.set_visible(True)

    def hide(self) -> None:
        self._win.set_visible(False)
