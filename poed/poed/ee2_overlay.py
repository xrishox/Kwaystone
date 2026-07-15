"""Native Wayland host window for the EE2 price-check UI."""

import logging

import gi

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, Gtk4LayerShell as LayerShell  # noqa: E402

from poed import draggable  # noqa: E402

try:  # WebKitGTK 6 is GTK4-compatible. WebKit2GTK 4.x is GTK3 and must not be used here.
    gi.require_version("WebKit", "6.0")
    from gi.repository import WebKit  # type: ignore[attr-defined]  # noqa: E402
except (ImportError, ValueError):
    WebKit = None  # type: ignore[assignment]


_LOG = logging.getLogger("waystone.ee2_overlay")
_PANEL_WIDTH = 460


class Ee2PriceOverlay:
    """Layer-shell side surface containing the upstream EE2 price-check UI."""

    def __init__(self, application: Gtk.Application):
        if WebKit is None:
            raise RuntimeError(
                "WebKitGTK 6 is required for the EE2 price-check UI "
                "(Debian package: gir1.2-webkit-6.0 / libwebkitgtk-6.0)."
            )
        self._loaded_url: str | None = None
        self._win = Gtk.Window(application=application)
        self._win.set_title("Kwaystone price check")
        self._win.set_decorated(False)
        self._win.connect("close-request", self._on_close)
        LayerShell.init_for_window(self._win)
        LayerShell.set_layer(self._win, LayerShell.Layer.OVERLAY)
        LayerShell.set_namespace(self._win, "kwaystone-ee2-price-check")
        LayerShell.set_anchor(self._win, LayerShell.Edge.TOP, True)
        LayerShell.set_anchor(self._win, LayerShell.Edge.BOTTOM, True)
        LayerShell.set_anchor(self._win, LayerShell.Edge.RIGHT, True)
        LayerShell.set_keyboard_mode(self._win, LayerShell.KeyboardMode.ON_DEMAND)
        self._win.set_default_size(_PANEL_WIDTH, 1)

        self._view = WebKit.WebView()
        self._view.set_hexpand(True)
        self._view.set_vexpand(True)
        self._view.set_size_request(_PANEL_WIDTH, -1)
        self._view.connect("load-changed", self._on_load_changed)
        self._view.connect("load-failed", self._on_load_failed)
        settings = self._view.get_settings()
        if settings is not None:
            settings.set_enable_developer_extras(True)
        rgba = Gdk.RGBA()
        rgba.parse("rgba(0,0,0,0)")
        self._view.set_background_color(rgba)
        self._win.set_child(self._view)

    def load(self, url: str) -> None:
        if self._loaded_url == url:
            return
        self._loaded_url = url
        _LOG.info("loading EE2 price-check UI: %s", url)
        self._view.load_uri(url)

    def show(self, url: str, side: str = "right", output_rect=None, game_rect=None) -> None:
        self.load(url)
        self._place(side, output_rect, game_rect)
        self._win.set_visible(True)
        self._win.present()

    def hide(self) -> None:
        self._win.set_visible(False)

    def is_visible(self) -> bool:
        return self._win.get_visible()

    def _on_close(self, *_args):
        self.hide()
        return True

    def _place(self, side: str, output_rect, game_rect) -> None:
        if output_rect is not None:
            draggable.set_monitor_for_rect(self._win, output_rect)

        side = "left" if side == "left" else "right"
        LayerShell.set_anchor(self._win, LayerShell.Edge.LEFT, side == "left")
        LayerShell.set_anchor(self._win, LayerShell.Edge.RIGHT, side == "right")

        out_w = int(getattr(output_rect, "w", 0) or 0)
        out_h = int(getattr(output_rect, "h", 0) or 0)
        if out_w <= 0 or out_h <= 0:
            out_w, out_h = draggable.window_monitor_size(self._win)

        if game_rect is None:
            top = 0
            bottom = 0
            left = 0
            right = 0
        else:
            top = max(0, int(getattr(game_rect, "y", 0) or 0))
            height = max(1, int(getattr(game_rect, "h", out_h) or out_h))
            bottom = max(0, out_h - top - height)
            left = max(0, int(getattr(game_rect, "x", 0) or 0))
            width = max(1, int(getattr(game_rect, "w", out_w) or out_w))
            right = max(0, out_w - left - width)

        LayerShell.set_margin(self._win, LayerShell.Edge.TOP, top)
        LayerShell.set_margin(self._win, LayerShell.Edge.BOTTOM, bottom)
        LayerShell.set_margin(self._win, LayerShell.Edge.LEFT, left if side == "left" else 0)
        LayerShell.set_margin(self._win, LayerShell.Edge.RIGHT, right if side == "right" else 0)
        _LOG.info(
            "EE2 price-check placement: side=%s width=%s top=%s bottom=%s left=%s right=%s",
            side,
            _PANEL_WIDTH,
            top,
            bottom,
            left if side == "left" else 0,
            right if side == "right" else 0,
        )

    def _on_load_changed(self, _view, event) -> None:
        name = getattr(event, "value_nick", None) or getattr(event, "value_name", None) or str(event)
        _LOG.info("EE2 webview load: %s", name)

    def _on_load_failed(self, _view, event, failing_uri, error) -> bool:
        name = getattr(event, "value_nick", None) or getattr(event, "value_name", None) or str(event)
        message = getattr(error, "message", str(error))
        _LOG.warning("EE2 webview load failed: event=%s uri=%s error=%s", name, failing_uri, message)
        return False
