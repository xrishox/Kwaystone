#!/usr/bin/env python3
"""Spike: does a TOP+LEFT-margined layer-shell button receive clicks?

Mirrors LoginBox's exact layer config. Prints on hover and click.
Run: LD_PRELOAD=/usr/lib/libgtk4-layer-shell.so python spikes/spike_loginbox_input.py
"""
import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell as LayerShell  # noqa: E402


def on_activate(app):
    win = Gtk.Window(application=app)
    LayerShell.init_for_window(win)
    LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
    LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.NONE)
    LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
    LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
    LayerShell.set_margin(win, LayerShell.Edge.LEFT, 800)

    btn = Gtk.Button(label="CLICK ME (spike)")
    btn.connect("clicked", lambda *_: print("spike: CLICK received", flush=True))
    motion = Gtk.EventControllerMotion()
    motion.connect("enter", lambda *_: print("spike: hover enter", flush=True))
    btn.add_controller(motion)
    win.set_child(btn)
    win.present()


app = Gtk.Application(application_id="io.github.kriskruse.waystone.spikelogin")
app.connect("activate", on_activate)
app.run(None)
