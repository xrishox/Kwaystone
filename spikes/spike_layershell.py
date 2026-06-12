#!/usr/bin/env python3
"""Spike: layer-shell overlay panel, keyboard on-demand, Esc closes."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell as LayerShell

def on_activate(app):
    win = Gtk.Window(application=app)
    LayerShell.init_for_window(win)
    LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
    LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)
    LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
    win.set_default_size(420, 600)
    win.set_child(Gtk.Label(label="overlay spike — press Esc"))
    ctl = Gtk.EventControllerKey()
    def on_key(_c, keyval, *_):
        if keyval == 65307:  # Esc
            win.close()
    ctl.connect("key-pressed", on_key)
    win.add_controller(ctl)
    win.present()

app = Gtk.Application(application_id="io.github.kriskruse.waystone.spike")
app.connect("activate", on_activate)
app.run(None)

## Findings
# Environment: Arch, Hyprland 0.55.2, gtk4 4.22.4, gtk4-layer-shell 1.3.0,
# python-gobject 3.56.3, Wayland session (WAYLAND_DISPLAY=wayland-1).
#
# REQUIRED ENV VAR: LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so
#   Running plain `python spikes/spike_layershell.py` FAILS the layer surface:
#     "WARNING: Failed to initialize layer surface, GTK4 Layer Shell may have
#      been linked after libwayland. Move gtk4-layer-shell before
#      libwayland-client in the linker options."
#     "GtkWindow is not a layer surface. Make sure you called
#      gtk_layer_init_for_window()"
#   The window then falls back to a normal toplevel and does NOT appear on the
#   overlay layer. `gi.require_version` ordering alone is NOT sufficient on this
#   setup (the Python/GI path loads libwayland before the layer-shell lib).
#   With `LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so` set, startup is CLEAN:
#   zero bytes on stdout/stderr, no warnings, no Wayland protocol errors.
#   (gtk4-layer-shell also ships /usr/lib/liblayer-shell-preload.so; the main
#    .so preloaded above works and is what was verified here.)
#
# hyprctl layers (with LD_PRELOAD, spike running) shows the surface on the
# overlay layer:
#     Layer level 3 (overlay):
#       Layer ...: xywh: 4460 1873 420 600, namespace: gtk4-layer-shell, pid: 84155
#   - Layer level 3 = "overlay" (above fullscreen) -> matches Layer.OVERLAY.
#   - Size 420x600 matches set_default_size(420, 600).
#   - x=4460 is the far-right of the multi-monitor span -> RIGHT anchor honored.
#   - namespace shown by Hyprland is "gtk4-layer-shell" (NOT the app id).
#
# VERIFIED HEADLESS:
#   - Typelibs import OK (Gtk 4.0 + Gtk4LayerShell 1.0).
#   - Clean startup with LD_PRELOAD (no errors on stderr).
#   - Real overlay-layer surface present in `hyprctl layers` at correct
#     size/anchor on the overlay (level 3) layer.
#   - timeout exit code 124 is expected: the GUI stays open until `timeout`
#     kills it; it is not a crash.
#
# NOT VERIFIED (needs human, no screen access here):
#   - Visually renders ABOVE a real fullscreen PoE2 game.
#   - Esc actually closes the window (keyval 65307 handler).
#   - Keyboard focus returns to the game after close (ON_DEMAND keyboard mode).
#
# Reference for Task 10 (poed/overlay.py): the overlay process must be launched
# with LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so (set it in the launcher/env
# before exec), or set it from within Python before GTK loads. Plain import
# ordering did not suffice here.
#
# Production note (Task 10): use Gdk.KEY_Escape instead of raw 65307.
#
# LD_PRELOAD must be in the process environment BEFORE the process starts —
# setting it from inside Python after any GI import is too late. The poed
# launcher must set it, not overlay.py.
#
# Only RIGHT anchor tested; y position (1873) was compositor-chosen since no
# vertical anchors set. Task 10: set TOP+BOTTOM anchors too if full-height
# panel wanted, or accept compositor placement.
