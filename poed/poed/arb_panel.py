"""Native layer-shell panel for the Alt+S currency-arbitrage view.

Layout (top to bottom, nothing important below the fold):
  verdict banner  -> color-coded buy-with / no-arb call
  liquid pair     -> the item's price in its most-liquid market
  per-currency    -> direct pair price + anchor conversion + exact delta
  listings        -> equipment mode: count + median, normalized
  exchange matrix -> top real currencies only, CLICK a row to anchor

All rate math happens in the brain; re-anchoring is pure client-side
division against the cached answer, so it is instant.
"""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, Gtk4LayerShell as LayerShell  # noqa: E402

from poed import draggable  # noqa: E402

_LOG = logging.getLogger("waystone.arb_panel")

_PANEL_DEFAULT_SIZE = (460, 420)

_CSS = """
.poe-verdict-good { color: #7ee787; font-weight: bold; }
.poe-verdict-none { color: #a0a0a0; }
.poe-arb-best { color: #7ee787; }
.poe-arb-bad { color: #f47067; }
.poe-cur-row { padding: 2px 6px; }
.poe-cur-row:hover { background: rgba(255, 255, 255, 0.08); }
.poe-selected { background: rgba(120, 170, 255, 0.18); border-radius: 4px; }
.poe-anchor-tag { color: #8ab4f8; }
"""

_MAJOR_ORDER = ("exalted", "chaos", "divine")


def _fmt(value: float) -> str:
    if value >= 1000:
        return f"{value:,.0f}"
    if value >= 10:
        return f"{value:,.1f}"
    return f"{value:,.2f}"


def _register_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


class ArbPanel:
    """Layer-shell arbitrage panel with drag, saved position, anchor clicks."""

    def __init__(self, app: Gtk.Application, on_visibility=None,
                 positions=None, desktop=None):
        self._on_visibility = on_visibility
        self._positions = positions
        self._desktop = desktop
        self.anchor = "exalted"
        self._last: dict | None = None
        self._last_league = ""
        self._matrix_rows: dict[str, Gtk.Widget] = {}
        _register_css()

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
                self._win, self._get_pos, self._save_pos, desktop=self._desktop
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

    # --- lifecycle -----------------------------------------------------------

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

    # --- public API -----------------------------------------------------------

    def set_anchor(self, api_id: str) -> None:
        """Force/release the normalization anchor, re-rendering instantly."""
        self.anchor = "exalted" if api_id == self.anchor else api_id
        if self._last is not None:
            self._render(self._last, self._last_league)

    def show_answer(self, answer: dict, league: str) -> None:
        self._last = answer
        self._last_league = league
        self._render(answer, league)
        self._show()

    def update_state(self, state: dict, league: str) -> None:
        # Keep stage-1 fields the state payload doesn't carry.
        merged = dict(self._last or {})
        merged.update(state)
        self._last = merged
        self._last_league = league
        self._render(merged, league)

    # --- rendering -------------------------------------------------------------

    def _clear(self) -> None:
        for child in list(self._header):
            self._header.remove(child)
        for child in list(self._content):
            self._content.remove(child)
        self._matrix_rows = {}

    def _render(self, data: dict, league: str) -> None:
        self._clear()
        item_name = data.get("itemName")

        title = Gtk.Label(xalign=0.0)
        title.add_css_class("poe-title")
        title.set_text(
            f"Currency arbitrage — {item_name}" if item_name else "Currency arbitrage"
        )
        self._header.append(title)
        league_label = Gtk.Label(label=league, xalign=1.0)
        league_label.add_css_class("poe-dim")
        league_label.set_hexpand(True)
        self._header.append(league_label)

        verdict = data.get("verdict")
        if verdict:
            self._content.append(self._verdict_banner(verdict))
        liquid = data.get("liquidPair")
        if liquid:
            self._content.append(self._liquid_line(liquid))
        if data.get("perCurrency"):
            self._content.append(self._section("price across currencies"))
            self._append_per_currency(data)
        if data.get("listings"):
            self._content.append(self._section("listings by currency"))
            self._append_listings(data)
        note = data.get("note") or data.get("listingsNote")
        if note:
            self._content.append(self._dim_line(str(note)))

        self._content.append(self._section("exchange rates — click to anchor"))
        self._append_matrix(data)

    def _verdict_banner(self, verdict: dict) -> Gtk.Widget:
        label = Gtk.Label(label=str(verdict.get("text") or ""), xalign=0.0)
        label.add_css_class(
            "poe-verdict-good"
            if verdict.get("kind") == "opportunity"
            else "poe-verdict-none"
        )
        return label

    def _liquid_line(self, liquid: dict) -> Gtk.Widget:
        currency = str(liquid.get("currency") or "")
        price = liquid.get("price")
        price_text = f"{_fmt(price)} {currency}" if price else "—"
        detail = (
            f"liquid pair · vol {_fmt(float(liquid.get('liquidity') or 0))}"
            f" · stock {_fmt(float(liquid.get('stock') or 0))}"
        )
        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        name = Gtk.Label(label=f"best market: {price_text}", xalign=0.0)
        name.set_hexpand(True)
        box.append(name)
        box.append(self._dim(detail))
        return box

    def _anchor_price(self, data: dict) -> float:
        prices = data.get("exaltedPrices") or {}
        if self.anchor == "exalted":
            return 1.0
        value = prices.get(self.anchor)
        try:
            value = float(value)
        except (TypeError, ValueError):
            return 1.0
        return value if value > 0 else 1.0

    def _append_per_currency(self, data: dict) -> None:
        anchor_price = self._anchor_price(data)
        live_rows = {
            row.get("key"): row for row in (data.get("itemRows") or [])
        }
        # Cheapest direct conversion anchors the delta coloring.
        direct = [
            float(row.get("exaltedPrice") or 0)
            for row in data["perCurrency"]
            if row.get("direct")
        ]
        best = min(direct) if direct else 0.0
        for row in data["perCurrency"]:
            currency = str(row.get("currency") or "")
            amount = row.get("amount")
            live = live_rows.get(
                f"item:{self._find_api_id(data)}:{currency}", {}
            )
            is_live = live.get("source") == "live"
            price = row.get("exaltedPrice") or 0
            delta = (price - best) / best if best > 0 else 0.0
            converted = price / anchor_price if price else None
            amount_text = (
                f"{_fmt(amount)} {currency}" if amount is not None else "—"
            )
            conv_text = (
                f"≈ {_fmt(converted)} {self.anchor}" if converted else "—"
            )
            delta_text = (
                "best" if best and price == best
                else f"+{delta * 100:.1f}%" if delta >= 0.005
                else f"{delta * 100:+.1f}%"
            )
            row_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=currency, xalign=0.0)
            label.set_hexpand(True)
            if not row.get("direct"):
                label.add_css_class("poe-dim")
            row_box.append(label)
            price_label = Gtk.Label(label=amount_text, xalign=1.0)
            if not is_live:
                price_label.add_css_class("poe-dim")
            row_box.append(price_label)
            row_box.append(Gtk.Label(label=conv_text, xalign=1.0))
            delta_label = Gtk.Label(label=delta_text, xalign=1.0)
            if best and price == best and row.get("direct"):
                delta_label.add_css_class("poe-arb-best")
            elif delta >= 0.05:
                delta_label.add_css_class("poe-arb-bad")
            else:
                delta_label.add_css_class("poe-dim")
            row_box.append(delta_label)
            self._content.append(row_box)

    def _find_api_id(self, data: dict) -> str:
        for row in data.get("itemRows") or []:
            key = str(row.get("key") or "")
            parts = key.split(":")
            if len(parts) == 3:
                return parts[1]
        return ""

    def _append_listings(self, data: dict) -> None:
        anchor_price = self._anchor_price(data)
        for entry in data.get("listings") or []:
            currency = str(entry.get("currency") or "")
            exalted_median = float(entry.get("exaltedMedian") or 0)
            converted = exalted_median / anchor_price if exalted_median else 0
            delta = float(entry.get("deltaVsBest") or 0)
            detail = f'{int(entry.get("count") or 0)} listed'
            detail += f" · +{delta * 100:.1f}%" if delta >= 0.005 else " · best"
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=f"median {currency}", xalign=0.0)
            label.set_hexpand(True)
            box.append(label)
            box.append(Gtk.Label(
                label=f"{entry.get('median'):g} {currency} (≈ {_fmt(converted)} {self.anchor})",
                xalign=1.0,
            ))
            delta_label = Gtk.Label(label=detail, xalign=1.0)
            if entry.get("flagged"):
                delta_label.add_css_class("poe-arb-bad")
            elif delta < 0.005:
                delta_label.add_css_class("poe-arb-best")
            else:
                delta_label.add_css_class("poe-dim")
            box.append(delta_label)
            self._content.append(box)

    def _append_matrix(self, data: dict) -> None:
        for row in data.get("matrix") or []:
            key = str(row.get("key") or "")
            api_id = key.split(":", 1)[1] if ":" in key else key
            button = Gtk.Button()
            button.add_css_class("poe-cur-row")
            button.set_has_frame(False)
            if api_id == self.anchor:
                button.add_css_class("poe-selected")
            box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            label = Gtk.Label(label=str(row.get("label") or ""), xalign=0.0)
            label.set_hexpand(True)
            box.append(label)
            price = Gtk.Label(label=str(row.get("priceText") or ""), xalign=1.0)
            if row.get("source") != "live":
                price.add_css_class("poe-dim")
            box.append(price)
            if api_id == self.anchor:
                tag = Gtk.Label(label="anchor", xalign=1.0)
                tag.add_css_class("poe-anchor-tag")
                box.append(tag)
            else:
                box.append(self._dim(str(row.get("detail") or "")))
            button.set_child(box)
            button.connect("clicked", self._on_currency_clicked, api_id)
            self._content.append(button)

    def _on_currency_clicked(self, _button, api_id: str) -> None:
        self.set_anchor(api_id)

    # --- small widgets ----------------------------------------------------------

    def _section(self, title: str) -> Gtk.Widget:
        label = Gtk.Label(label=title, xalign=0.0)
        label.add_css_class("poe-section")
        return label

    def _dim(self, text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text, xalign=1.0)
        label.add_css_class("poe-dim")
        return label

    def _dim_line(self, text: str) -> Gtk.Widget:
        label = Gtk.Label(label=text, xalign=0.0)
        label.add_css_class("poe-dim")
        return label
