"""Standalone layer-shell league selector (sibling of LoginBox).

TOP+LEFT anchored, drag handle, position persisted under "league".
Pointer-only — never grabs the keyboard from the game. Starts with just the
configured league; set_leagues() swaps in the full roster once the brain's
`leagues` cmd answers. Selection changes call on_change(league) exactly once
per user pick (programmatic model swaps are guarded).
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell as LayerShell  # noqa: E402

from . import draggable

_LOG = logging.getLogger("waystone.league")


class LeagueBox:
    def __init__(self, app: Gtk.Application, current_league: str, on_change, positions=None):
        self._on_change = on_change
        self._positions = positions
        self._updating = False
        self._names = [current_league]

        self._win = Gtk.Window(application=app)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)

        mon_w, _mon_h = draggable.monitor_geometry()
        saved = positions.get("league") if positions is not None else None
        self._pos = saved if saved is not None else (int(mon_w * 0.25), 0)
        draggable.anchor_top_left(self._win, *self._pos)

        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        box.add_css_class("poe-panel")
        box.append(draggable.make_drag_handle(self._win, self._get_pos, self._save_pos))

        self._dd = Gtk.DropDown.new_from_strings(self._names)
        self._dd.connect("notify::selected", self._on_selected)
        box.append(self._dd)
        self._win.set_child(box)

    # -- drag-to-move (mirrors LoginBox) ------------------------------------

    def _get_pos(self):
        return self._pos

    def _save_pos(self, x: int, y: int) -> None:
        w, h = self._win.get_width(), self._win.get_height()
        if w > 0 and h > 0:
            mon_w, mon_h = draggable.monitor_geometry()
            x, y = draggable.clamp_position(x, y, mon_w, mon_h, w, h)
        self._pos = (int(x), int(y))
        if self._positions is not None:
            self._positions.set("league", *self._pos)
        draggable.set_position(self._win, *self._pos)

    # -- public API (GLib main thread only) ----------------------------------

    def set_leagues(self, names: list[str], current: str) -> None:
        if current not in names:
            names = [current, *names]
        self._names = list(names)
        self._updating = True
        try:
            self._dd.set_model(Gtk.StringList.new(self._names))
            self._dd.set_selected(self._names.index(current))
        finally:
            self._updating = False

    def set_visible(self, visible: bool) -> None:
        if visible:
            self._win.present()
            draggable.set_position(self._win, *self._pos)
        else:
            self._win.set_visible(False)

    # -- internal -------------------------------------------------------------

    def _on_selected(self, dd, _pspec) -> None:
        if self._updating:
            return
        i = dd.get_selected()
        if 0 <= i < len(self._names):
            league = self._names[i]
            _LOG.info("selected: %s", league)
            self._on_change(league)
