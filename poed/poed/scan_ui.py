"""Alt+X native scan-result UI lifecycle."""

from __future__ import annotations

import logging
import time

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from poed.badges import ScanOverlayLayer
from poed.overlay import OverlayPanel

_LOG = logging.getLogger("waystone.scan_ui")


class ScanResultController:
    """Owns the native Alt+X result panel and pointer-transparent badges."""

    def __init__(self, application, cfg, positions, desktop, on_visibility_changed):
        self._cfg = cfg
        self._desktop = desktop
        self._on_visibility_changed = on_visibility_changed
        self._panel = OverlayPanel(
            application,
            on_visibility=self._on_panel_visibility,
            positions=positions,
            desktop=desktop,
        )
        self._badges = ScanOverlayLayer(application)
        self._focus_poll = None
        self._seen_game_unfocused = False

    def show_result(self, result, output, t0: float, timings=None) -> None:
        ui_started = time.perf_counter()
        started = time.perf_counter()
        self._badges.show(
            result.matches,
            self._cfg["unique_min_exalted"],
            output,
            scanner_id=result.scanner_id,
        )
        overlay_ms = (time.perf_counter() - started) * 1000.0
        started = time.perf_counter()
        self._panel.show_screen_scan(
            result.title,
            result.matches,
            self._cfg["unique_min_exalted"],
            elapsed=time.monotonic() - t0,
        )
        self._seen_game_unfocused = not self._desktop.is_game_focused()
        self._start_focus_poll()
        panel_ms = (time.perf_counter() - started) * 1000.0
        total_ms = (time.perf_counter() - ui_started) * 1000.0
        _LOG.info(
            "screenscan-ui: scanner=%s overlay=%.2fms panel=%.2fms total=%.2fms",
            result.scanner_id,
            overlay_ms,
            panel_ms,
            total_ms,
        )
        if isinstance(timings, dict):
            _LOG.debug("screenscan timings: %s", timings)

    def show_scan_error(self, message: str) -> None:
        self.hide_badges()
        self._panel.show_scan_error(message)
        self._seen_game_unfocused = not self._desktop.is_game_focused()
        self._start_focus_poll()

    def dismiss(self) -> None:
        if self._panel.is_visible():
            self._panel.hide()
        else:
            self.hide_badges()
        self._stop_focus_poll()

    def hide_badges(self) -> None:
        if self._badges.is_visible():
            self._badges.hide()
            self._on_visibility_changed()

    def is_visible(self) -> bool:
        return self.is_panel_visible() or bool(self._badges.is_visible())

    def is_panel_visible(self) -> bool:
        return bool(self._panel.is_visible())

    def _on_panel_visibility(self, visible: bool) -> None:
        if not visible:
            self._stop_focus_poll()
            if self._badges.is_visible():
                self._badges.hide()
        self._on_visibility_changed()

    def _start_focus_poll(self) -> None:
        if self._focus_poll is None:
            self._focus_poll = GLib.timeout_add(200, self._poll_focus)

    def _stop_focus_poll(self, *, remove_source: bool = True) -> None:
        source = self._focus_poll
        self._focus_poll = None
        self._seen_game_unfocused = False
        if remove_source and source is not None:
            try:
                GLib.source_remove(source)
            except (RuntimeError, ValueError):
                pass

    def _poll_focus(self):
        if not self.is_visible():
            self._stop_focus_poll(remove_source=False)
            return GLib.SOURCE_REMOVE
        game_focused = self._desktop.is_game_focused()
        if not game_focused:
            self._seen_game_unfocused = True
        if self._seen_game_unfocused and game_focused:
            _LOG.info("hiding Alt+X scan UI after game focus returned")
            self._stop_focus_poll(remove_source=False)
            self.dismiss()
            return GLib.SOURCE_REMOVE
        return GLib.SOURCE_CONTINUE
