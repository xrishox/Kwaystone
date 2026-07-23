"""Docked GTK panel for screen-captured Currency Exchange arbitrage sessions."""

from __future__ import annotations

import math
import time
from collections.abc import Callable
from datetime import datetime

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gdk", "4.0")
gi.require_version("Pango", "1.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gdk, GLib, Gtk, Pango, Gtk4LayerShell as LayerShell  # noqa: E402

from poed import draggable  # noqa: E402

_PANEL_WIDTH = 560
_CSS = """
.arb-root { background: rgba(13, 14, 16, 0.97); color: #e7e2d5; }
.arb-body { padding: 14px; }
.arb-title { font-size: 18px; font-weight: 700; }
.arb-kicker { color: #b7ad98; font-size: 12px; }
.arb-percent { color: #68d391; font-size: 30px; font-weight: 800; }
.arb-loop-percent { color: #68d391; font-size: 16px; font-weight: 700; }
.arb-negative { color: #e57373; }
.arb-path { font-size: 17px; font-weight: 650; }
.arb-section { color: #cbbf9f; font-size: 12px; font-weight: 700; margin-top: 10px; }
.arb-dim { color: #918b80; }
.arb-stale { color: #d9a441; }
.arb-live { color: #79c0ff; }
.arb-estimate { color: #d9a441; }
.arb-monitor { padding: 8px; border-radius: 4px; background: rgba(121,192,255,0.10); }
.arb-safe { color: #68d391; font-weight: 700; }
.arb-unsafe { color: #e57373; font-weight: 700; }
.arb-quantity-result { font-size: 16px; font-weight: 700; }
.arb-choice { padding: 12px; border-radius: 4px; background: rgba(255,255,255,0.055); }
.arb-choice:hover { background: rgba(255,255,255,0.11); }
.arb-loop-choice { padding: 8px; border-radius: 4px; background: rgba(255,255,255,0.035); }
.arb-loop-choice:hover { background: rgba(255,255,255,0.09); }
.arb-row { padding: 6px 0; }
.arb-divider { background: rgba(255,255,255,0.12); min-height: 1px; }
"""


def _register_css() -> None:
    provider = Gtk.CssProvider()
    provider.load_from_string(_CSS)
    display = Gdk.Display.get_default()
    if display is not None:
        Gtk.StyleContext.add_provider_for_display(
            display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
        )


def _label(text: str, css: str | None = None, xalign: float = 0.0) -> Gtk.Label:
    widget = Gtk.Label(label=text, xalign=xalign)
    widget.set_wrap(True)
    widget.set_wrap_mode(Pango.WrapMode.WORD_CHAR)
    if css:
        widget.add_css_class(css)
    return widget


def _age(ms: int | float | None) -> str:
    if not ms:
        return "now"
    seconds = max(0, int(time.time() - float(ms) / 1000.0))
    return f"{seconds}s" if seconds < 60 else f"{seconds // 60}m {seconds % 60}s"


def _rate(value: object) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if number >= 1000:
        return f"{number:,.0f}"
    if number >= 10:
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    return f"{number:,.4f}".rstrip("0").rstrip(".")


def _market_ratio(value: object) -> str:
    """Format the larger side against one, matching the in-game ratio label."""
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return "- : 1"
    if not math.isfinite(rate) or rate <= 0:
        return "- : 1"
    normalized = rate if rate >= 1 else 1 / rate
    amount = f"{normalized:,.2f}"
    if amount.endswith(".00"):
        amount = amount[:-3]
    return f"{amount} : 1"


def _snapshot_label(value: object) -> str:
    raw = str(value or "").strip()
    if not raw:
        return "latest snapshot"
    try:
        timestamp = float(raw)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp).astimezone().strftime("%b %-d, %-I:%M %p")
    except (OSError, OverflowError, ValueError):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed.astimezone().strftime("%b %-d, %-I:%M %p")
        except ValueError:
            return raw


class ArbPanel:
    def __init__(
        self,
        app: Gtk.Application,
        *,
        on_visibility: Callable[[], None] | None = None,
        on_close: Callable[[], None] | None = None,
        on_recalculate: Callable[[], None] | None = None,
        on_threshold: Callable[[float], None] | None = None,
        on_buffer: Callable[[float], None] | None = None,
        on_concession: Callable[[float], None] | None = None,
        on_show_losing: Callable[[bool], None] | None = None,
        on_selection: Callable[[str, int, bool], None] | None = None,
        min_percent: float = 5.0,
        safety_buffer_percent: float = 5.0,
        execution_concession_percent: float = 5.0,
        show_losing_candidates: bool = False,
        desktop=None,
    ):
        _register_css()
        self._on_visibility = on_visibility
        self._on_close_callback = on_close
        self._on_recalculate = on_recalculate
        self._on_threshold = on_threshold
        self._on_buffer = on_buffer
        self._on_concession = on_concession
        self._on_show_losing = on_show_losing
        self._on_selection = on_selection
        self._min_percent = min_percent
        self._safety_buffer_percent = safety_buffer_percent
        self._execution_concession_percent = execution_concession_percent
        self._show_losing_candidates = bool(show_losing_candidates)
        self._desktop = desktop
        self._side = "right"
        self._quantity = 1
        self._quantity_max = 25
        self._analysis: dict | None = None
        self._selected_loop_id: str | None = None
        self._selection_is_manual = False
        self._monitor_state = "off"
        self._monitor_detail = ""
        self._expiry_source = 0

        self._win = Gtk.Window(application=app)
        self._win.set_title("Kwaystone Currency Exchange arbitrage")
        self._win.set_decorated(False)
        self._win.connect("close-request", self._on_close)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self._win, "waystone-arb-session")
        LayerShell.set_anchor(self._win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(self._win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(self._win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.ON_DEMAND)
        self._win.set_default_size(_PANEL_WIDTH, 1)

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.add_css_class("arb-root")
        self._header = Gtk.Box(spacing=8)
        self._header.add_css_class("arb-body")
        self._header.append(_label("Currency arbitrage", "arb-title"))
        spacer = Gtk.Box(hexpand=True)
        self._header.append(spacer)
        self._settings = Gtk.MenuButton(icon_name="emblem-system-symbolic")
        self._settings.set_tooltip_text("Arbitrage settings")
        self._settings.set_popover(self._settings_popover())
        self._header.append(self._settings)
        self._recalculate = Gtk.Button(icon_name="view-refresh-symbolic")
        self._recalculate.set_has_frame(False)
        self._recalculate.set_sensitive(False)
        self._recalculate.set_tooltip_text("Recalculate optimal loop")
        self._recalculate.connect("clicked", self._recalculate_clicked)
        self._header.append(self._recalculate)
        close = Gtk.Button(icon_name="window-close-symbolic")
        close.set_has_frame(False)
        close.set_tooltip_text("Close arbitrage session")
        close.connect("clicked", lambda *_: self._on_close())
        self._header.append(close)
        root.append(self._header)
        root.append(Gtk.Separator())

        scroll = Gtk.ScrolledWindow(vexpand=True, hexpand=True)
        self._content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._content.add_css_class("arb-body")
        scroll.set_child(self._content)
        root.append(scroll)
        self._win.set_child(root)

        keys = Gtk.EventControllerKey()
        keys.connect("key-pressed", self._on_key)
        self._win.add_controller(keys)

    def _settings_popover(self) -> Gtk.Popover:
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_margin_top(10)
        box.set_margin_bottom(10)
        box.set_margin_start(10)
        box.set_margin_end(10)
        box.append(_label("Minimum arbitrage", "arb-kicker"))
        scale = Gtk.Scale.new_with_range(Gtk.Orientation.HORIZONTAL, 0, 20, 0.5)
        scale.set_value(self._min_percent)
        scale.set_digits(1)
        scale.set_size_request(220, -1)
        scale.connect("value-changed", self._threshold_changed)
        box.append(scale)
        box.append(_label("Faster-fill concession per market", "arb-kicker"))
        concession_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 15, 0.5
        )
        concession_scale.set_value(self._execution_concession_percent)
        concession_scale.set_digits(1)
        concession_scale.set_size_request(220, -1)
        self._concession_summary = _label("", "arb-dim")
        concession_scale.connect("value-changed", self._concession_changed)
        box.append(concession_scale)
        box.append(self._concession_summary)
        box.append(_label("Total loop safety buffer", "arb-kicker"))
        buffer_scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 0, 15, 0.5
        )
        buffer_scale.set_value(self._safety_buffer_percent)
        buffer_scale.set_digits(1)
        buffer_scale.set_size_request(220, -1)
        self._buffer_summary = _label("", "arb-dim")
        buffer_scale.connect("value-changed", self._buffer_changed)
        box.append(buffer_scale)
        box.append(self._buffer_summary)
        self._show_losing_toggle = Gtk.CheckButton(label="Show losing candidates")
        self._show_losing_toggle.set_active(self._show_losing_candidates)
        self._show_losing_toggle.connect("toggled", self._show_losing_changed)
        box.append(self._show_losing_toggle)
        self._update_concession_summary()
        self._update_buffer_summary()
        popover.set_child(box)
        return popover

    def _threshold_changed(self, scale: Gtk.Scale) -> None:
        self._min_percent = float(scale.get_value())
        if self._on_threshold is not None:
            self._on_threshold(self._min_percent)

    def _buffer_changed(self, scale: Gtk.Scale) -> None:
        self._safety_buffer_percent = round(float(scale.get_value()) * 2) / 2
        self._update_buffer_summary()
        if self._on_buffer is not None:
            self._on_buffer(self._safety_buffer_percent)

    def _concession_changed(self, scale: Gtk.Scale) -> None:
        self._execution_concession_percent = round(float(scale.get_value()) * 2) / 2
        self._update_concession_summary()
        if self._on_concession is not None:
            self._on_concession(self._execution_concession_percent)

    def _update_concession_summary(self) -> None:
        loop_cost = (
            1 - (1 - self._execution_concession_percent / 100) ** 3
        ) * 100
        self._concession_summary.set_text(
            f"{self._execution_concession_percent:.1f}% fewer units per leg · "
            f"{loop_cost:.1f}% across 3 legs"
        )

    def _update_buffer_summary(self) -> None:
        per_leg = (1 - (1 - self._safety_buffer_percent / 100) ** (1 / 3)) * 100
        self._buffer_summary.set_text(
            f"{self._safety_buffer_percent:.1f}% total · {per_leg:.1f}% modeled per leg"
        )

    def _show_losing_changed(self, toggle: Gtk.CheckButton) -> None:
        self._show_losing_candidates = toggle.get_active()
        visible = self._visible_loops(list((self._analysis or {}).get("loops") or []))
        visible_ids = {str(loop.get("id") or "") for loop in visible}
        if self._selected_loop_id not in visible_ids:
            self._selection_is_manual = False
            preferred = self._preferred_loop(visible)
            self._selected_loop_id = str((preferred or {}).get("id") or "") or None
        self._notify_selection()
        if self._on_show_losing is not None:
            self._on_show_losing(self._show_losing_candidates)
        if self._analysis is not None:
            self._render_analysis()

    def _recalculate_clicked(self, _button: Gtk.Button) -> None:
        self._selection_is_manual = False
        self._selected_loop_id = None
        if self._on_recalculate is not None:
            self._on_recalculate()

    def _on_key(self, _ctl, keyval, *_args):
        if keyval == Gdk.KEY_Escape:
            self._on_close()
            return True
        return False

    def _on_close(self, *_args):
        if self._on_close_callback is not None:
            self._on_close_callback()
        else:
            self.hide()
        return True

    def is_visible(self) -> bool:
        return self._win.get_visible()

    def selected_loop(self) -> dict | None:
        loops = list((self._analysis or {}).get("loops") or [])
        return next(
            (loop for loop in loops if loop.get("id") == self._selected_loop_id),
            None,
        )

    def selected_quantity(self) -> int:
        return self._quantity

    def monitor_active(self) -> bool:
        return self._monitor_state != "off"

    def set_monitor_state(self, state: str, detail: str = "") -> None:
        self._monitor_state = state
        self._monitor_detail = detail
        if self._analysis is not None and self.is_visible():
            self._render_analysis()

    def alert(self) -> None:
        display = Gdk.Display.get_default()
        if display is not None:
            display.beep()

    def hide(self) -> None:
        self._cancel_expiry()
        was_visible = self.is_visible()
        self._win.set_visible(False)
        if was_visible and self._on_visibility is not None:
            self._on_visibility()

    def _place(self, side: str) -> None:
        self._side = "left" if side == "left" else "right"
        LayerShell.set_anchor(self._win, LayerShell.Edge.LEFT, self._side == "left")
        LayerShell.set_anchor(self._win, LayerShell.Edge.RIGHT, self._side == "right")
        output_rect = None
        game_rect = None
        if self._desktop is not None:
            try:
                output_rect = self._desktop.active_output_rect()
                output = self._desktop.active_game_output()
                if output and output_rect is not None:
                    game_rect = self._desktop.active_game_rect(
                        output, (int(output_rect.w), int(output_rect.h))
                    )
            except (AttributeError, OSError, RuntimeError, TypeError, ValueError):
                output_rect = None
                game_rect = None
        if output_rect is not None:
            draggable.set_monitor_for_rect(self._win, output_rect)
        out_w = int(getattr(output_rect, "w", 0) or 0)
        out_h = int(getattr(output_rect, "h", 0) or 0)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = draggable.window_monitor_size(self._win)
        top = max(0, int(getattr(game_rect, "y", 0) or 0)) if game_rect else 0
        height = max(1, int(getattr(game_rect, "h", out_h) or out_h)) if game_rect else out_h
        bottom = max(0, out_h - top - height)
        left = max(0, int(getattr(game_rect, "x", 0) or 0)) if game_rect else 0
        width = max(1, int(getattr(game_rect, "w", out_w) or out_w)) if game_rect else out_w
        right = max(0, out_w - left - width)
        LayerShell.set_margin(self._win, LayerShell.Edge.TOP, top)
        LayerShell.set_margin(self._win, LayerShell.Edge.BOTTOM, bottom)
        LayerShell.set_margin(self._win, LayerShell.Edge.LEFT, left if self._side == "left" else 0)
        LayerShell.set_margin(self._win, LayerShell.Edge.RIGHT, right if self._side == "right" else 0)

    def _show(self, side: str | None = None) -> None:
        self._place(side or self._side)
        self._win.set_visible(True)
        self._win.present()
        if self._on_visibility is not None:
            self._on_visibility()

    def _clear(self) -> None:
        child = self._content.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            self._content.remove(child)
            child = next_child

    @staticmethod
    def _clear_box(box: Gtk.Box) -> None:
        child = box.get_first_child()
        while child is not None:
            next_child = child.get_next_sibling()
            box.remove(child)
            child = next_child

    def show_choice(
        self,
        pair: dict,
        on_select: Callable[[str], None],
        side: str,
        *,
        on_restore: Callable[[], None] | None = None,
        restore_target_name: str = "",
    ) -> None:
        self._cancel_expiry()
        self._analysis = None
        self._selected_loop_id = None
        self._selection_is_manual = False
        self._recalculate.set_sensitive(False)
        self._quantity = 1
        self._quantity_max = 25
        self._monitor_state = "off"
        self._clear()
        observation = pair["observation"]
        self._content.append(_label("Choose arbitrage target", "arb-kicker"))
        self._content.append(self._choice("I WANT", observation["want"], on_select))
        self._content.append(self._choice("I HAVE", observation["have"], on_select))
        if on_restore is not None:
            self._content.append(
                self._restore_choice(restore_target_name, on_restore)
            )
        ratio = f'{_rate(observation["wantAmount"])} : {_rate(observation["haveAmount"])}'
        self._content.append(_label(f"Market ratio  {ratio}", "arb-dim"))
        self._show(side)

    def _choice(self, role: str, item: dict, on_select: Callable[[str], None]) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("arb-choice")
        row = Gtk.Box(spacing=10)
        role_label = _label(role, "arb-kicker")
        role_label.set_size_request(70, -1)
        row.append(role_label)
        name = _label(str(item.get("name") or item.get("apiId") or ""))
        name.set_hexpand(True)
        row.append(name)
        button.set_child(row)
        button.connect("clicked", lambda *_: on_select(str(item.get("apiId") or "")))
        return button

    def _restore_choice(
        self, target_name: str, on_restore: Callable[[], None]
    ) -> Gtk.Button:
        button = Gtk.Button()
        button.add_css_class("arb-choice")
        row = Gtk.Box(spacing=10)
        role = _label("RESTORE LATEST ARB", "arb-kicker")
        role.set_size_request(150, -1)
        row.append(role)
        name = _label(target_name or "Previous arbitrage")
        name.set_hexpand(True)
        row.append(name)
        button.set_child(row)
        button.connect("clicked", lambda *_: on_restore())
        return button

    def show_loading(self, target_name: str, side: str | None = None) -> None:
        self._cancel_expiry()
        self._recalculate.set_sensitive(False)
        self._clear()
        self._content.append(_label(target_name, "arb-title"))
        self._content.append(_label("Calculating loops", "arb-dim"))
        self._show(side)

    def show_error(
        self,
        message: str,
        side: str | None = None,
        *,
        can_recalculate: bool = False,
    ) -> None:
        self._cancel_expiry()
        self._recalculate.set_sensitive(can_recalculate)
        self._clear()
        self._content.append(_label("Currency Exchange unavailable", "arb-title"))
        self._content.append(_label(message, "arb-negative"))
        self._show(side)

    @staticmethod
    def _loop_stale(loop: dict) -> bool:
        valid_until = loop.get("validUntil")
        try:
            expired = float(valid_until) < time.time() * 1000.0
        except (TypeError, ValueError):
            expired = False
        return bool(loop.get("stale")) or expired

    @staticmethod
    def _quantity_outcome(loop: dict, quantity: int) -> dict | None:
        outcomes = list(loop.get("quantityOutcomes") or [])
        if not outcomes:
            return None
        index = max(0, min(int(quantity), len(outcomes)) - 1)
        outcome = outcomes[index]
        return outcome if isinstance(outcome, dict) else None

    @staticmethod
    def _positive_loop(loop: dict) -> bool:
        try:
            return float(loop.get("bufferedPercent")) > 0
        except (TypeError, ValueError):
            return False

    def _visible_loops(self, loops: list[dict]) -> list[dict]:
        if self._show_losing_candidates:
            return loops
        return [
            loop
            for loop in loops
            if self._positive_loop(loop)
            or (
                self.monitor_active()
                and str(loop.get("id") or "") == self._selected_loop_id
            )
        ]

    def _ranked_loops(
        self, loops: list[dict], status: str, confidence: str | None = None
    ) -> list[dict]:
        candidates = [
            loop
            for loop in loops
            if loop.get("status") == status
            and (confidence is None or loop.get("estimateConfidence") == confidence)
        ]

        def key(loop: dict):
            outcome = self._quantity_outcome(loop, self._quantity) or {}
            complete = bool(outcome.get("bufferedComplete"))
            try:
                fractional_return = float(loop.get("bufferedPercent"))
            except (TypeError, ValueError):
                fractional_return = -100.0
            value = (
                outcome.get("bufferedReturnPercent")
                if complete
                else fractional_return
            )
            try:
                buffered_return = float(value)
            except (TypeError, ValueError):
                buffered_return = -100.0
            return (
                -int(complete),
                -buffered_return,
                -fractional_return,
                str(loop.get("id") or ""),
            )

        return sorted(candidates, key=key)

    def _preferred_loop(self, loops: list[dict]) -> dict | None:
        eligible = [
            loop
            for loop in loops
            if loop.get("status") == "verified"
            or (
                loop.get("status") == "estimate"
                and loop.get("estimateConfidence") == "reliable"
            )
        ]
        if not eligible:
            eligible = list(loops)

        def key(loop: dict):
            outcome = self._quantity_outcome(loop, self._quantity) or {}
            complete = bool(outcome.get("bufferedComplete"))
            try:
                fractional_return = float(loop.get("bufferedPercent"))
            except (TypeError, ValueError):
                fractional_return = -100.0
            value = (
                outcome.get("bufferedReturnPercent")
                if complete
                else fractional_return
            )
            try:
                score = float(value)
            except (TypeError, ValueError):
                score = -100.0
            return (
                -int(complete),
                -score,
                -int(loop.get("status") == "verified"),
                str(loop.get("id") or ""),
            )

        return min(eligible, key=key) if eligible else None

    def _notify_selection(self) -> None:
        if self._on_selection is not None and self._selected_loop_id:
            self._on_selection(
                self._selected_loop_id,
                self._quantity,
                self._selection_is_manual,
            )

    def _cancel_expiry(self) -> None:
        if self._expiry_source:
            GLib.source_remove(self._expiry_source)
            self._expiry_source = 0

    def _schedule_expiry(self, loops: list[dict]) -> None:
        self._cancel_expiry()
        now = time.time() * 1000.0
        deadlines = []
        for loop in loops:
            try:
                deadline = float(loop.get("validUntil"))
            except (TypeError, ValueError):
                continue
            if deadline > now:
                deadlines.append(deadline)
        if deadlines:
            delay = max(1, int(min(deadlines) - now) + 25)
            self._expiry_source = GLib.timeout_add(delay, self._expire_loops)

    def _expire_loops(self):
        self._expiry_source = 0
        if self._analysis is not None and self.is_visible():
            self._render_analysis()
        return GLib.SOURCE_REMOVE

    def show_analysis(self, data: dict, side: str | None = None) -> None:
        self._analysis = data
        self._side = side or self._side
        loops = self._visible_loops(list(data.get("loops") or []))
        loop_ids = {str(loop.get("id") or "") for loop in loops}
        if self._selected_loop_id not in loop_ids:
            self._selection_is_manual = False
        if not self._selection_is_manual:
            preferred = self._preferred_loop(loops)
            self._selected_loop_id = str((preferred or {}).get("id") or "") or None
        self._notify_selection()
        self._render_analysis()

    def _render_analysis(self) -> None:
        data = self._analysis or {}
        self._recalculate.set_sensitive(True)
        self._clear()
        target = data.get("target") or {}
        target_name = str(target.get("name") or "Arbitrage target")
        self._content.append(_label(target_name, "arb-title"))
        epoch = _snapshot_label(data.get("ratesEpoch"))
        age = int(data.get("ratesAgeMs") or 0)
        analyzed_at = int(data.get("analyzedAt") or 0)
        if analyzed_at:
            age += max(0, int(time.time() * 1000) - analyzed_at)
        rate_text = f"Poe2Scout {epoch} · {max(0, age // 60000)}m old"
        rates_status = str(data.get("ratesStatus") or "fresh")
        if rates_status == "degraded":
            rate_text += " · degraded, estimates disabled"
        elif rates_status == "stale":
            rate_text += " · stale, estimates disabled"
        self._content.append(_label(rate_text, "arb-estimate"))

        all_loops = list(data.get("loops") or [])
        loops = self._visible_loops(all_loops)
        captures = list(data.get("captures") or [])
        loops_evaluated = int(data.get("loopsEvaluated") or len(loops))
        currency_count = int(data.get("capturedCurrencyCount") or 0)
        loop_summary = (
            f"{loops_evaluated} loop{'s' if loops_evaluated != 1 else ''} evaluated"
            if self._show_losing_candidates
            else f"{len(loops)} positive · {loops_evaluated} evaluated"
        )
        self._content.append(
            _label(
                f"{loop_summary} · "
                f"{currency_count} currenc{'ies' if currency_count != 1 else 'y'}",
                "arb-dim",
            )
        )
        if self.monitor_active():
            css = "arb-safe" if self._monitor_state == "safe" else (
                "arb-unsafe" if self._monitor_state in {"unsafe", "unavailable"} else "arb-live"
            )
            status = {
                "starting": "LIVE MONITOR STARTING",
                "paused": "LIVE MONITOR PAUSED",
                "verifying": "LIVE VERIFYING",
                "tracking": "LIVE RECALCULATING",
                "safe": "LIVE LOOP SAFE",
                "unsafe": "LIVE LOOP UNSAFE",
                "unavailable": "LIVE MONITOR UNAVAILABLE",
            }.get(self._monitor_state, "LIVE MONITOR")
            box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
            box.add_css_class("arb-monitor")
            box.append(_label(status, css))
            if self._monitor_detail:
                box.append(_label(self._monitor_detail, "arb-dim"))
            self._content.append(box)
        selected = next(
            (loop for loop in loops if loop.get("id") == self._selected_loop_id),
            None,
        )
        if selected:
            status = str(selected.get("status") or "estimate")
            estimate_confidence = str(
                selected.get("estimateConfidence") or "reliable"
            )
            stale = self._loop_stale(selected)
            selected_outcome = self._quantity_outcome(selected, self._quantity) or {}
            best = self._preferred_loop(loops)
            best_id = str((best or {}).get("id") or "")
            if self.monitor_active() and self._monitor_state not in {"safe", "unsafe"}:
                heading = "LIVE DATA NOT YET VERIFIED"
            elif status == "estimate" and estimate_confidence == "thin":
                heading = "THIN HISTORICAL SIGNAL · VERIFY FIRST"
            elif status == "estimate":
                heading = "BUFFERED ESTIMATE · NOT VERIFIED PROFIT"
            elif stale:
                heading = "STALE VERIFIED LOOP"
            elif selected.get("id") == best_id:
                heading = f"BEST BUFFERED LOOP AT {self._quantity}"
            else:
                heading = "SELECTED LOOP"
            self._content.append(_label(heading, "arb-section"))
            self._append_loop(selected, headline=True)
            if status == "estimate":
                estimated_legs = [
                    leg
                    for leg in selected.get("legs") or []
                    if leg.get("source") == "poe2scout"
                ]
                warning = (
                    "This uses a thin completed-hour pair and is not a ranked "
                    "candidate. Verify it with Alt+A before relying on it."
                    if estimate_confidence == "thin"
                    else "This percentage uses a historical currency bridge. "
                    "Do not treat it as executable profit."
                )
                self._content.append(_label(warning, "arb-estimate"))
                for leg in estimated_legs:
                    from_name = str((leg.get("from") or {}).get("name") or "?")
                    to_name = str((leg.get("to") or {}).get("name") or "?")
                    self._content.append(
                        _label(
                            f"Verify {from_name} → {to_name}: open that market and press Alt+A",
                            "arb-estimate",
                        )
                    )
            elif stale:
                self._content.append(_label("Captured ratios expired", "arb-stale"))
            elif self._monitor_state == "unsafe":
                self._content.append(_label("Live buffered outcome is unsafe", "arb-unsafe"))
            elif not selected_outcome.get("actionable"):
                self._content.append(
                    _label("Buffered outcome below actionable threshold", "arb-dim")
                )
            else:
                self._content.append(
                    _label("Verified live ratios · market depth not verified", "arb-live")
                )
        else:
            empty_text = (
                "No positive arbitrage candidates"
                if all_loops and not self._show_losing_candidates
                else "No complete directed loop"
            )
            self._content.append(_label(empty_text, "arb-kicker"))
            if currency_count >= 2:
                self._content.append(
                    _label(
                        "Currency Exchange prices are directional. Capture each "
                        "missing I HAVE → I WANT market; reverse prices are never inferred.",
                        "arb-estimate",
                    )
                )

        if currency_count == 1:
            self._content.append(
                _label("Add another currency for cross-currency comparison", "arb-dim")
            )

        other = [loop for loop in loops if loop.get("id") != self._selected_loop_id]
        captured = self._ranked_loops(other, "verified")
        candidates = self._ranked_loops(other, "estimate", "reliable")
        thin_candidates = self._ranked_loops(other, "estimate", "thin")
        if captured:
            self._content.append(_label("VERIFIED LOOPS", "arb-section"))
            for loop in captured:
                self._content.append(self._loop_button(loop))
        if candidates:
            self._content.append(_label("CANDIDATES TO VERIFY", "arb-section"))
            for loop in candidates:
                self._content.append(self._loop_button(loop))
        if thin_candidates:
            self._content.append(_label("THIN HISTORICAL SIGNALS", "arb-section"))
            for loop in thin_candidates:
                self._content.append(self._loop_button(loop))

        self._content.append(_label("CAPTURED MARKETS", "arb-section"))
        for capture in captures:
            self._append_capture(capture)

        bridges = list(data.get("bridges") or [])
        if bridges:
            self._content.append(_label("LIVE CURRENCY PRICES", "arb-section"))
            for bridge in bridges:
                self._append_bridge(bridge)

        if data.get("unavailable"):
            missing = ", ".join(str(item) for item in data["unavailable"])
            self._content.append(_label(f"Missing directed market: {missing}", "arb-dim"))
        self._schedule_expiry([*all_loops, *captures, *bridges])
        self._show(self._side)

    def _path_text(self, path: list[dict]) -> str:
        names = []
        for item in path:
            name = str(item.get("name") or item.get("apiId") or "?")
            if not names or names[-1] != name:
                names.append(name)
        return " → ".join(names) + " ↻"

    def _append_loop(self, loop: dict, headline: bool = False) -> None:
        stale = self._loop_stale(loop)
        path = self._path_text(list(loop.get("path") or []))
        if headline:
            self._content.append(_label(path, "arb-path"))
        if stale:
            self._content.append(_label("Stale capture", "arb-stale"))
        if headline:
            self._append_quantity_tool(loop)
            nominal = float(loop.get("nominalPercent") or loop.get("percent") or 0)
            execution = float(loop.get("executionPercent") or 0)
            buffered = float(loop.get("bufferedPercent") or 0)
            self._content.append(
                _label(
                    f"Fractional model  {buffered:+.1f}% buffered · "
                    f"{execution:+.1f}% faster fill · {nominal:+.1f}% market",
                    "arb-dim",
                )
            )
        for leg in loop.get("legs") or []:
            source = str(leg.get("source") or "")
            from_name = str((leg.get("from") or {}).get("name") or "?")
            to_name = str((leg.get("to") or {}).get("name") or "?")
            if source == "capture":
                detail = "captured ratio"
            elif source == "capture-bridge":
                detail = "live currency ratio"
            else:
                evidence = leg.get("scoutEvidence") or {}
                confidence = str(evidence.get("confidence") or "thin")
                from_volume = int(evidence.get("fromVolume") or 0)
                to_volume = int(evidence.get("toVolume") or 0)
                liquidity = float(evidence.get("liquidityExalted") or 0)
                detail = (
                    f"Poe2Scout {confidence} · {from_volume:,}/{to_volume:,} units · "
                    f"{liquidity:,.0f} ex"
                )
            row = Gtk.Box(spacing=8)
            execution_rate = leg.get("executionRate")
            if execution_rate is None:
                execution_rate = float(leg.get("rate") or 0) * (
                    1 - self._execution_concession_percent / 100
                )
            text = _label(
                f"Market  1 {from_name} = {_rate(leg.get('rate'))} {to_name} "
                f"({_market_ratio(leg.get('rate'))})\n"
                f"Faster fill  1 {from_name} = {_rate(execution_rate)} {to_name} "
                f"({_market_ratio(execution_rate)})"
            )
            text.set_hexpand(True)
            row.append(text)
            css = (
                "arb-live"
                if source in {"capture", "capture-bridge"}
                else "arb-estimate"
            )
            row.append(_label(detail, css, 1.0))
            self._content.append(row)

    def _notch_color(
        self, buffered_return: float | None
    ) -> tuple[float, float, float]:
        if buffered_return is None:
            return (0.37, 0.37, 0.37)
        red = (0.85, 0.42, 0.42)
        amber = (0.85, 0.64, 0.25)
        green = (0.41, 0.83, 0.57)
        threshold = max(1.0, self._min_percent)
        if buffered_return < 0:
            mix = max(0.0, min(1.0, 1 + buffered_return / threshold))
            start, end = red, amber
        else:
            mix = max(0.0, min(1.0, buffered_return / threshold))
            start, end = amber, green
        return tuple(start[index] + (end[index] - start[index]) * mix for index in range(3))

    def _append_quantity_tool(self, loop: dict) -> None:
        all_points = list(loop.get("quantityOutcomes") or [])
        if not all_points:
            return
        points = all_points[: self._quantity_max]

        header = Gtk.Box(spacing=8)
        title = _label("WHOLE-UNIT OUTCOME", "arb-section")
        title.set_hexpand(True)
        header.append(title)
        first_range: Gtk.ToggleButton | None = None
        range_buttons: list[tuple[Gtk.ToggleButton, int]] = []
        for maximum in (25, 50, 100):
            button = Gtk.ToggleButton(label=str(maximum))
            button.set_tooltip_text(f"Show quantities through {maximum}")
            if first_range is None:
                first_range = button
            else:
                button.set_group(first_range)
            button.set_active(maximum == self._quantity_max)
            button.set_sensitive(not self.monitor_active())
            range_buttons.append((button, maximum))
            header.append(button)
        self._content.append(header)

        scale = Gtk.Scale.new_with_range(
            Gtk.Orientation.HORIZONTAL, 1, self._quantity_max, 1
        )
        scale.set_digits(0)
        scale.set_draw_value(True)
        scale.set_hexpand(True)
        scale.set_value(min(self._quantity, self._quantity_max))
        scale.set_sensitive(not self.monitor_active())
        self._content.append(scale)

        notches = Gtk.DrawingArea()
        notches.set_size_request(-1, 18)
        notches.set_can_target(False)
        self._content.append(notches)

        outcome_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self._content.append(outcome_box)
        state: dict[str, object] = {"points": points, "selected": self._quantity}

        def draw_notches(_area, context, width: int, height: int) -> None:
            current = list(state["points"])
            if not current:
                return
            selected = int(state["selected"])
            count = len(current)
            left = 5.0
            usable = max(1.0, width - left * 2)
            bar_width = max(1.0, min(4.0, usable / count * 0.65))
            for index, point in enumerate(current):
                x = left + (usable * index / max(1, count - 1))
                bar_height = 4.0
                if point.get("localPeak"):
                    bar_height = 10.0
                if point.get("budgetBest"):
                    bar_height = 16.0
                raw_return = point.get("bufferedReturnPercent")
                buffered_return = (
                    float(raw_return)
                    if point.get("bufferedComplete") and raw_return is not None
                    else None
                )
                red, green, blue = self._notch_color(buffered_return)
                context.set_source_rgba(red, green, blue, 0.95)
                context.rectangle(x - bar_width / 2, height - bar_height, bar_width, bar_height)
                context.fill()
                if int(point.get("quantity") or 0) == selected:
                    context.set_source_rgba(0.93, 0.91, 0.84, 1.0)
                    context.set_line_width(1.0)
                    context.rectangle(
                        x - bar_width / 2 - 1,
                        max(0.5, height - bar_height - 1),
                        bar_width + 2,
                        bar_height + 1,
                    )
                    context.stroke()

        notches.set_draw_func(draw_notches)

        def render_outcome(quantity: int) -> None:
            current = list(state["points"])
            if not current:
                return
            quantity = max(1, min(quantity, len(current)))
            point = current[quantity - 1]
            self._quantity = quantity
            state["selected"] = quantity
            self._clear_box(outcome_box)
            legs = list(loop.get("legs") or [])
            start_name = str(((legs[0] if legs else {}).get("from") or {}).get("name") or "?")
            finish_name = str(((legs[-1] if legs else {}).get("to") or {}).get("name") or "?")
            buffered_complete = bool(point.get("bufferedComplete"))
            execution_complete = bool(point.get("executionComplete"))
            nominal_complete = bool(point.get("nominalComplete"))
            buffered_return = (
                float(point.get("bufferedReturnPercent"))
                if buffered_complete and point.get("bufferedReturnPercent") is not None
                else None
            )
            nominal_return = (
                float(point.get("nominalReturnPercent"))
                if nominal_complete and point.get("nominalReturnPercent") is not None
                else None
            )
            execution_return = (
                float(point.get("executionReturnPercent"))
                if execution_complete
                and point.get("executionReturnPercent") is not None
                else None
            )
            if loop.get("status") == "estimate":
                result_css = "arb-estimate"
            else:
                result_css = "arb-live" if point.get("actionable") else (
                    "arb-estimate"
                    if buffered_return is None or buffered_return >= 0
                    else "arb-negative"
                )
            result_kind = (
                "Buffered faster-fill estimate"
                if loop.get("status") == "estimate"
                else "Buffered faster fill"
            )
            if buffered_complete and buffered_return is not None:
                result_text = (
                    f"{result_kind}: {quantity} {start_name} → "
                    f"{int(point.get('bufferedFinalUnits') or 0)} {finish_name} "
                    f"({buffered_return:+.1f}%)"
                )
            else:
                blocked_step = int(point.get("bufferedBlockedStep") or 0)
                blocked_units = int(point.get("bufferedBlockedUnits") or 0)
                blocked_name = str(
                    ((legs[blocked_step] if blocked_step < len(legs) else {}).get("from") or {}).get(
                        "name"
                    )
                    or "intermediate currency"
                )
                result_text = (
                    f"{result_kind}: cannot complete · "
                    f"{blocked_units} {blocked_name} retained"
                )
            result = _label(result_text, "arb-quantity-result")
            result.add_css_class(result_css)
            outcome_box.append(result)
            status = " · low-budget best" if point.get("budgetBest") else (
                " · local peak" if point.get("localPeak") else ""
            )
            if execution_complete and execution_return is not None:
                execution_text = (
                    f"Faster fill: {quantity} {start_name} → "
                    f"{int(point.get('executionFinalUnits') or 0)} {finish_name} "
                    f"({execution_return:+.1f}%){status}"
                )
            else:
                blocked_step = int(point.get("executionBlockedStep") or 0)
                blocked_units = int(point.get("executionBlockedUnits") or 0)
                blocked_name = str(
                    ((legs[blocked_step] if blocked_step < len(legs) else {}).get("from") or {}).get(
                        "name"
                    )
                    or "intermediate currency"
                )
                execution_text = (
                    f"Faster fill: cannot complete · "
                    f"{blocked_units} {blocked_name} retained{status}"
                )
            outcome_box.append(_label(execution_text, "arb-dim"))
            if nominal_complete and nominal_return is not None:
                nominal_text = (
                    f"Market: {quantity} {start_name} → "
                    f"{int(point.get('nominalFinalUnits') or 0)} {finish_name} "
                    f"({nominal_return:+.1f}%){status}"
                )
            else:
                blocked_step = int(point.get("nominalBlockedStep") or 0)
                blocked_units = int(point.get("nominalBlockedUnits") or 0)
                blocked_name = str(
                    ((legs[blocked_step] if blocked_step < len(legs) else {}).get("from") or {}).get(
                        "name"
                    )
                    or "intermediate currency"
                )
                nominal_text = (
                    f"Market: cannot complete · {blocked_units} {blocked_name} retained"
                )
            outcome_box.append(_label(nominal_text, "arb-dim"))
            buffer_percent = float((self._analysis or {}).get("safetyBufferBps") or 0) / 100
            per_leg_percent = float(
                (self._analysis or {}).get("perLegSafetyBufferBps") or 0
            ) / 100
            outcome_box.append(
                _label(
                    f"{self._execution_concession_percent:.1f}% fewer output units "
                    "accepted at every market",
                    "arb-dim",
                )
            )
            outcome_box.append(
                _label(
                    f"{buffer_percent:.1f}% total adverse loop move · "
                    f"{per_leg_percent:.1f}% modeled per leg",
                    "arb-dim",
                )
            )
            for index, step in enumerate(point.get("steps") or []):
                leg = legs[index] if index < len(legs) else {}
                from_name = str((leg.get("from") or {}).get("name") or "?")
                to_name = str((leg.get("to") or {}).get("name") or "?")
                headroom = float(step.get("boundaryHeadroomPercent") or 0)
                headroom_css = (
                    "arb-live" if headroom >= per_leg_percent else "arb-negative"
                )
                outcome_box.append(
                    _label(
                        f"{int(step.get('nominalInputUnits') or 0)} {from_name} → "
                        f"{int(step.get('nominalOutputUnits') or 0)} {to_name} market · "
                        f"{int(step.get('executionOutputUnits') or 0)} faster fill · "
                        f"{int(step.get('bufferedOutputUnits') or 0)} buffered · "
                        f"{headroom:.1f}% headroom before losing 1"
                    )
                )
                last = outcome_box.get_last_child()
                if last is not None:
                    last.add_css_class(headroom_css)
            notches.queue_draw()

        def quantity_changed(widget: Gtk.Scale) -> None:
            quantity = int(round(widget.get_value()))
            if abs(widget.get_value() - quantity) > 1e-6:
                widget.set_value(quantity)
                return
            previous = self._quantity
            render_outcome(quantity)
            if quantity != previous:
                if not self._selection_is_manual:
                    preferred = self._preferred_loop(
                        self._visible_loops(
                            list((self._analysis or {}).get("loops") or [])
                        )
                    )
                    self._selected_loop_id = (
                        str((preferred or {}).get("id") or "") or None
                    )
                self._notify_selection()
                GLib.idle_add(self._render_analysis)

        def range_changed(button: Gtk.ToggleButton, maximum: int) -> None:
            if not button.get_active():
                return
            self._quantity_max = maximum
            state["points"] = all_points[:maximum]
            scale.set_range(1, maximum)
            if self._quantity > maximum:
                scale.set_value(maximum)
            else:
                render_outcome(self._quantity)

        scale.connect("value-changed", quantity_changed)
        for button, maximum in range_buttons:
            button.connect("toggled", range_changed, maximum)
        render_outcome(int(round(scale.get_value())))

    def _select_loop(self, loop_id: str) -> None:
        if self.monitor_active():
            return
        self._selected_loop_id = loop_id
        self._selection_is_manual = True
        self._notify_selection()
        self._render_analysis()

    def _loop_button(self, loop: dict) -> Gtk.Widget:
        button = Gtk.Button()
        button.add_css_class("arb-loop-choice")
        row = Gtk.Box(spacing=8)
        path = _label(self._path_text(list(loop.get("path") or [])))
        path.set_hexpand(True)
        row.append(path)
        status = str(loop.get("status") or "estimate")
        stale = self._loop_stale(loop)
        outcome = self._quantity_outcome(loop, self._quantity) or {}
        complete = bool(outcome.get("bufferedComplete"))
        percent = (
            float(outcome.get("bufferedReturnPercent"))
            if complete and outcome.get("bufferedReturnPercent") is not None
            else float(loop.get("bufferedPercent") or 0)
        )
        css = "arb-estimate" if status == "estimate" or stale else (
            "arb-loop-percent" if outcome.get("actionable") else (
                "arb-estimate" if percent >= 0 else "arb-negative"
            )
        )
        suffix = " thin est." if (
            status == "estimate" and loop.get("estimateConfidence") == "thin"
        ) else (" est." if status == "estimate" else " buffered")
        final_units = int(outcome.get("bufferedFinalUnits") or 0)
        result = (
            f"{self._quantity} → {final_units} · {percent:+.1f}%{suffix}"
            if complete
            else f"{self._quantity} → incomplete · {percent:+.1f}% fractional{suffix}"
        )
        row.append(
            _label(result, css, 1.0)
        )
        button.set_child(row)
        button.set_sensitive(not self.monitor_active())
        button.connect("clicked", lambda *_: self._select_loop(str(loop.get("id") or "")))
        return button

    def _append_capture(self, capture: dict) -> None:
        row = Gtk.Box(spacing=8)
        have = str((capture.get("have") or {}).get("name") or "?")
        want = str((capture.get("want") or {}).get("name") or "?")
        text = _label(
            f"1 {have} = {_rate(capture.get('rate'))} {want} "
            f"({_market_ratio(capture.get('rate'))})"
        )
        text.set_hexpand(True)
        row.append(text)
        stale = self._loop_stale(capture)
        row.append(_label(_age(capture.get("observedAt")), "arb-stale" if stale else "arb-live", 1.0))
        row.add_css_class("arb-row")
        self._content.append(row)

    def _append_bridge(self, bridge: dict) -> None:
        row = Gtk.Box(spacing=8)
        have = str((bridge.get("have") or {}).get("name") or "?")
        want = str((bridge.get("want") or {}).get("name") or "?")
        text = _label(f"{have} → {want}")
        text.set_hexpand(True)
        row.append(text)
        row.append(
            _label(
                f"1 {have} = {_rate(bridge.get('rate'))} {want} "
                f"({_market_ratio(bridge.get('rate'))})",
                xalign=1.0,
            )
        )
        stale = self._loop_stale(bridge)
        row.append(
            _label(
                _age(bridge.get("observedAt")),
                "arb-stale" if stale else "arb-live",
                1.0,
            )
        )
        row.add_css_class("arb-row")
        self._content.append(row)
