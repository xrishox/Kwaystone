"""Native layer-shell panel for the Alt+S currency-arbitrage view.

Sectioned content (exchange matrix, item view, listings) rebuilt from brain
rows; the panel itself stays dumb — all rate math lives in the brain. Chrome
(layer-shell, drag, saved position, clamp) mirrors OverlayPanel deliberately
so panel behavior is uniform across features.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, Gtk4LayerShell as LayerShell  # noqa: E402

from poed import draggable  # noqa: E402

_LOG = logging.getLogger("waystone.arb_panel")

_PANEL_DEFAULT_SIZE = (460, 420)


def _row(label: str, price: str, detail: str = "", *, live: bool = False,
         flagged: bool = False, dim: bool = False) -> Gtk.Widget:
    box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
    name = Gtk.Label(label=label, xalign=0.0)
    name.set_hexpand(True)
    if dim:
        name.add_css_class("poe-dim")
    box.append(name)
    price_label = Gtk.Label(label=price, xalign=1.0)
    if live:
        price_label.add_css_class("poe-live")
    if flagged:
        price_label.add_css_class("poe-flag")
    box.append(price_label)
    if detail:
        detail_label = Gtk.Label(label=detail, xalign=1.0)
        detail_label.add_css_class("poe-dim")
        box.append(detail_label)
    return box


def _section(title: str) -> Gtk.Widget:
    label = Gtk.Label(label=title, xalign=0.0)
    label.add_css_class("poe-section")
    return label


class ArbPanel:
    """Layer-shell arbitrage panel with drag-to-move and saved position."""

    def __init__(self, app: Gtk.Application, on_visibility=None,
                 positions=None, desktop=None):
        self._on_visibility = on_visibility
        self._positions = positions
        self._desktop = desktop
        self._win = Gtk.Window(application=app)
        self._win.add_css_class("poe-overlay-window")
        self._win.set_decorated(False)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self._win, "waystone-arb-panel")
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.NONE)

        self._sync_game_monitor()
        mon_w, _mon_h = draggable.window_monitor_size(self._win)
        saved = positions.get("arb") if positions is not None else None
        raw_pos = saved if saved is not None else (max(0, mon_w // 2 - 230), 120)
        self._pos = draggable.clamp_window_position(
            self._win, *raw_pos, default_size=_PANEL_DEFAULT_SIZE
        )
        draggable.anchor_top_left(self._win, *self._pos)

        self._header = Gtk.Box(spacing=6)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        scroll.add_css_class("poe-panel-scroll")
        scroll.set_min_content_width(430)
        scroll.set_min_content_height(300)
        scroll.set_child(self._content)

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
        body = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        body.append(self._header)
        body.append(scroll)
        root.append(body)
        self._win.set_child(root)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self._win.add_controller(keys)

    def _on_key(self, _ctl, keyval, *_):
        if keyval == Gdk.KEY_Escape:
            self.hide()

    def _get_pos(self):
        return self._pos

    def _save_pos(self, x: int, y: int) -> None:
        self._sync_game_monitor()
        x, y = draggable.clamp_window_position(
            self._win, x, y, default_size=_PANEL_DEFAULT_SIZE
        )
        self._pos = (int(x), int(y))
        if self._positions is not None:
            self._positions.set("arb", *self._pos)
        draggable.set_position(self._win, *self._pos)

    def _sync_game_monitor(self) -> None:
        if self._desktop is None:
            return
        active_output_rect = getattr(self._desktop, "active_output_rect", None)
        if active_output_rect is None:
            return
        rect = active_output_rect()
        if rect is not None:
            draggable.set_monitor_for_rect(self._win, rect)

    def is_visible(self) -> bool:
        return self._win.get_visible()

    def hide(self) -> None:
        if self._win.get_visible():
            self._win.set_visible(False)
            if self._on_visibility is not None:
                self._on_visibility(False)

    def _show(self) -> None:
        self._sync_game_monitor()
        self._win.set_visible(True)
        self._win.present()
        if self._on_visibility is not None:
            self._on_visibility(True)

    def _clear(self) -> None:
        for child in list(self._header):
            self._header.remove(child)
        for child in list(self._content):
            self._content.remove(child)

    def show_answer(self, answer: dict, league: str) -> None:
        """Render the immediate stage-1 answer from the brain."""
        self._clear()
        self._build_header(answer, league)
        self._append_matrix_rows(answer)
        note = answer.get("note")
        if note:
            self._content.append(_row(note, "", dim=True))
        self._show()

    def update_state(self, state: dict, league: str) -> None:
        """Re-render as stage-2 refinement lands (aggregate -> live rows)."""
        self._clear()
        self._build_header({"league": league}, league)
        self._append_matrix_rows(state)
        listings = state.get("listings") or []
        if listings:
            self._content.append(_section("listings by currency"))
            for entry in listings:
                delta = float(entry.get("deltaVsBest") or 0.0)
                detail = (
                    f'{int(entry.get("count") or 0)} listed'
                    + (f" · +{delta * 100:.1f}% vs best" if delta >= 0.005 else " · best")
                )
                self._content.append(_row(
                    f'median {entry.get("currency")}',
                    f'{entry.get("median"):g} '
                    f'(≈ {float(entry.get("exaltedMedian") or 0):.1f} ex)',
                    detail,
                    flagged=bool(entry.get("flagged")),
                ))
        note = state.get("listingsNote")
        if note:
            self._content.append(_row(note, "", dim=True))

    # --- internals -----------------------------------------------------------

    def _build_header(self, answer: dict, league: str) -> None:
        title = Gtk.Label(xalign=0.0)
        title.add_css_class("poe-title")
        item = answer.get("itemName")
        title.set_text(f"Currency arbitrage — {item}" if item else "Currency arbitrage")
        self._header.append(title)
        league_label = Gtk.Label(label=league, xalign=1.0)
        league_label.add_css_class("poe-dim")
        league_label.set_hexpand(True)
        self._header.append(league_label)

    def _append_matrix_rows(self, answer: dict) -> None:
        matrix = answer.get("matrix") or []
        if matrix:
            self._content.append(_section("exchange rates"))
            for row in matrix:
                self._content.append(_row(
                    str(row.get("label") or ""),
                    str(row.get("priceText") or ""),
                    str(row.get("detail") or ""),
                    live=row.get("source") == "live",
                    dim=row.get("source") != "live",
                ))
        item_rows = answer.get("itemRows") or []
        if item_rows:
            self._content.append(_section("item across currencies"))
            for row in item_rows:
                self._content.append(_row(
                    str(row.get("label") or ""),
                    str(row.get("priceText") or ""),
                    str(row.get("detail") or ""),
                    live=row.get("source") == "live",
                    dim=row.get("source") != "live",
                ))
