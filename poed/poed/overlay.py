"""Native Alt+X scan-result panel.

Alt+Z price checking is rendered by the embedded EE2 web UI. This module stays
native and only owns the small draggable list panel used by screen scans.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
gi.require_version("Pango", "1.0")
from gi.repository import Gtk, Gdk, GLib, Pango, Gtk4LayerShell as LayerShell  # noqa: E402

from . import draggable
from . import views

_LOG = logging.getLogger("waystone.overlay")
_PANEL_DEFAULT_SIZE = (620, 600)

_CSS = b"""
window.poe-overlay-window, .poe-overlay-window {
             background: transparent; }
.poe-panel { background: rgba(11,11,14,0.67); border: 1px solid #3d3a2f;
             border-radius: 8px; padding: 12px; color: #cfcfc4; }
.poe-panel-scroll, .poe-panel-scroll viewport,
list.poe-panel-list, list.poe-panel-list row,
list.poe-panel-list row:hover, list.poe-panel-list row:selected {
             background: transparent; }
.poe-row { padding: 4px 6px; border-top: 1px solid #22211b; }
.poe-dim { color: #7f7c6a; font-size: 11px; }
.poe-prop { color: #cfcfc4; font-size: 12px; }
.poe-name-unique { color: #af6025; font-weight: bold; }
.poe-drag { background: #1b1a14; border-bottom: 1px solid #3d3a2f; }
.poe-drag:hover { background: #2a2820; }
.poe-badges { background: transparent; }
.poe-badge { color: #cfcfc4; background: rgba(0,0,0,0.75); padding: 2px 6px;
             border-radius: 3px; font-size: 17px; }
.poe-badge-expedition { padding: 3px 9px; border-radius: 5px; font-size: 26px; }
.poe-badge-runeshape { padding: 6px 14px; border-radius: 6px; font-size: 42px; }
.poe-route-badge { color: #cfcfc4; background: rgba(0,0,0,0.72);
                   border: 1px solid rgba(255,210,80,0.45);
                   border-radius: 5px; padding: 4px 9px; font-size: 14px;
                   font-weight: bold; }
.poe-scan-marker { background: transparent; border: 2px solid rgba(255,210,80,0.90);
                   border-radius: 4px; }
.poe-badge-price { font-weight: bold; }
.poe-badge-name { color: #9a9786; font-size: 12px; }
.poe-badge-expedition .poe-badge-name { font-size: 18px; }
.poe-badge-runeshape-name { color: #cfcfc4; font-size: 42px; font-weight: bold; }
.poe-value-grey { color: #7f7c6a; }
.poe-value-blue { color: #68a8ff; font-weight: bold; }
.poe-value-red { color: #ff4d4d; font-weight: bold; }
.poe-value-orange { color: #ffb347; font-weight: bold; }
.poe-value-white { color: #ffffff; font-weight: bold; }
"""


def _esc(value) -> str:
    return GLib.markup_escape_text(str(value))


def _label(markup: str, css: str, xalign: float = 0.0, wrap: bool = True) -> Gtk.Label:
    label = Gtk.Label(xalign=xalign, wrap=wrap)
    label.set_markup(markup)
    label.add_css_class(css)
    return label


class OverlayPanel:
    def __init__(
        self,
        app: Gtk.Application,
        on_visibility=None,
        positions=None,
        desktop=None,
    ):
        self._on_visibility = on_visibility
        self._positions = positions
        self._desktop = desktop
        self._win = Gtk.Window(application=app)
        self._win.add_css_class("poe-overlay-window")
        self._win.set_decorated(False)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)

        self._sync_game_monitor()
        mon_w, _mon_h = draggable.window_monitor_size(self._win)
        saved = positions.get("panel") if positions is not None else None
        raw_pos = saved if saved is not None else (max(0, mon_w // 2 - 310), 80)
        self._pos = draggable.clamp_window_position(
            self._win, *raw_pos, default_size=_PANEL_DEFAULT_SIZE
        )
        draggable.anchor_top_left(self._win, *self._pos)

        provider = Gtk.CssProvider()
        provider.load_from_data(_CSS)
        display = Gdk.Display.get_default()
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display,
                provider,
                Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION,
            )

        self._header = Gtk.Box(spacing=6)
        self._list = Gtk.ListBox()
        self._list.add_css_class("poe-panel-list")
        self._list.set_selection_mode(Gtk.SelectionMode.NONE)
        self._scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._scroll.add_css_class("poe-panel-scroll")
        self._scroll.set_min_content_width(480)
        self._scroll.set_min_content_height(520)
        self._scroll.set_child(self._list)

        content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        content.append(self._header)
        content.append(self._scroll)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("poe-panel")
        root.append(
            draggable.make_drag_handle(
                self._win,
                self._get_pos,
                self._save_pos,
                desktop=self._desktop,
            )
        )
        root.append(content)
        self._win.set_child(root)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self._win.add_controller(keys)

    def _on_key(self, _ctl, keyval, *_):
        if keyval == Gdk.KEY_Escape:
            self.hide()

    def _get_pos(self):
        return self._pos

    def _sync_game_monitor(self) -> None:
        if self._desktop is None:
            return
        active_output_rect = getattr(self._desktop, "active_output_rect", None)
        if active_output_rect is None:
            return
        rect = active_output_rect()
        if rect is None:
            return
        if draggable.set_monitor_for_rect(self._win, rect):
            _LOG.debug("panel monitor synced to game output rect=%s", rect)

    def _clamp_pos(self) -> None:
        new_pos = draggable.clamp_window_position(
            self._win, *self._pos, default_size=_PANEL_DEFAULT_SIZE
        )
        if new_pos == self._pos:
            return
        old_pos = self._pos
        self._pos = new_pos
        if self._positions is not None:
            self._positions.set("panel", *self._pos)
        draggable.set_position(self._win, *self._pos)
        mon_w, mon_h = draggable.window_monitor_size(self._win)
        _LOG.info(
            "panel position clamped: old=%s new=%s output=%sx%s",
            old_pos,
            self._pos,
            mon_w,
            mon_h,
        )

    def _save_pos(self, x: int, y: int) -> None:
        self._sync_game_monitor()
        x, y = draggable.clamp_window_position(
            self._win, x, y, default_size=_PANEL_DEFAULT_SIZE
        )
        self._pos = (int(x), int(y))
        if self._positions is not None:
            self._positions.set("panel", *self._pos)
        draggable.set_position(self._win, *self._pos)

    def _clear(self):
        for child in list(self._header):
            self._header.remove(child)
        for child in list(self._list):
            self._list.remove(child)

    def show_screen_scan(
        self,
        title: str,
        matches: list[dict],
        min_exalted: float,
        elapsed: float | None = None,
    ) -> None:
        del min_exalted
        self._clear()
        rows = sorted(
            (m for m in matches if not m.get("markerOnly")),
            key=lambda m: -views.match_total_value(m),
        )
        timing = f" — {elapsed:.1f}s" if elapsed is not None else ""
        self._header.append(
            _label(f"{_esc(title)} ({len(rows)}){_esc(timing)}", "poe-dim", wrap=False)
        )
        if not rows:
            self._list.append(_label("nothing recognized", "poe-dim"))
        for result in rows:
            row = Gtk.Box(spacing=8)
            row.add_css_class("poe-row")
            css = "poe-name-unique" if result.get("kind") == "unique" else "poe-prop"
            name = _label(_esc(result.get("name", "")), css, wrap=False)
            name.set_ellipsize(Pango.EllipsizeMode.END)
            name.set_max_width_chars(34)
            name.set_hexpand(True)
            name.set_tooltip_text(str(result.get("name", "")))
            value = views.match_total_value(result)
            value_text = views.match_value_label(result)
            price = _label(_esc(value_text), views.value_tier_css(value), wrap=False)
            price.set_tooltip_text(value_text)
            row.append(name)
            trend = result.get("trend")
            arrow = "↗" if trend is not None and trend >= 0.10 else (
                "↘" if trend is not None and trend <= -0.10 else ""
            )
            if arrow:
                row.append(_label(_esc(arrow), "poe-dim", wrap=False))
            row.append(price)
            self._list.append(row)
        self._present()

    def show_scan_error(self, message: str) -> None:
        self._clear()
        self._list.append(_label(_esc(message), "poe-dim"))
        self._present()

    def is_visible(self) -> bool:
        return self._win.get_visible()

    def hide(self) -> None:
        self._win.set_visible(False)
        self._win.set_default_size(1, 1)
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)
        if self._on_visibility is not None:
            self._on_visibility(False)

    def _present(self):
        self._sync_game_monitor()
        self._clamp_pos()
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.ON_DEMAND)
        self._win.set_default_size(1, 1)
        self._win.present()
        self._clamp_pos()
        draggable.set_position(self._win, *self._pos)
        if self._on_visibility is not None:
            self._on_visibility(True)
        GLib.idle_add(self._settle_size)

    def _settle_size(self):
        self._win.set_default_size(1, 1)
        self._win.queue_resize()
        self._clamp_pos()
        draggable.set_position(self._win, *self._pos)
        return GLib.SOURCE_REMOVE
