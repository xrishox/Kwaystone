"""Alt+Z price-check lifecycle.

This module is intentionally separate from the native scan-result UI. Alt+Z
captures hovered item text and hands it to the embedded EE2 price-check app; it
must not fall back to Kwaystone's GTK scan panel.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from poed import clipboard, config
from poed.ee2_overlay import Ee2PriceOverlay
from poed.shortcuts import shortcut_modifiers

_LOG = logging.getLogger("waystone.price_check")


class PriceCheckController:
    """Owns the EE2-backed Alt+Z UI and host state."""

    def __init__(self, application, cfg, brain, desktop, on_visibility_changed):
        self._application = application
        self._cfg = cfg
        self._brain = brain
        self._desktop = desktop
        self._on_visibility_changed = on_visibility_changed
        self._price_hotkey_modifiers = shortcut_modifiers(cfg["hotkey_price"])
        self._overlay = None
        self._url = None
        self._hide_seq = 0
        self._state_poll = None
        self._side = "right"
        self._output_rect = None
        self._game_rect = None
        self._seen_game_unfocused = False

    def run(self, gen: int, is_current: Callable[[int], bool]) -> None:
        """Capture item text and show the EE2 side panel for the active generation."""
        try:
            text = clipboard.grab_item_text(
                self._desktop.is_game_focused,
                self._price_hotkey_modifiers,
            )
            if not is_current(gen):
                return
            if text is None:
                _LOG.info("price copy failed: clipboard did not contain item text")
                return

            _LOG.info(
                "price copy captured: chars=%s first=%r",
                len(text),
                text.splitlines()[0] if text.splitlines() else "",
            )
            url = self._ensure_host()
            side, output_rect, game_rect = self._overlay_geometry()
            self._side = side
            self._output_rect = output_rect
            self._game_rect = game_rect
            self._brain.request(
                {
                    "cmd": "ee2itemtext",
                    "clipboard": text,
                    "x": 400 if side == "left" else 1,
                    "y": 1,
                    "focusOverlay": False,
                }
            )
            # Baseline read here (worker thread): a synchronous brain request
            # must never sit on the GTK main loop in _show_overlay.
            self._sync_state_baseline()
            GLib.idle_add(self._show_overlay, gen, is_current, url)
        except (RuntimeError, OSError, TimeoutError, KeyError, TypeError, ValueError) as e:
            _LOG.warning("EE2 price-check failed: %s", e)

    def prewarm(self) -> None:
        """Create the WebKit overlay at startup so first Alt+Z is instant and
        any WebKit environment problem surfaces at launch, not mid-game."""
        if self._overlay is not None:
            return
        try:
            self._overlay = Ee2PriceOverlay(self._application)
            _LOG.info("EE2 overlay prewarmed")
        except RuntimeError as e:
            _LOG.warning("EE2 overlay unavailable: %s", e)

    def hide(self) -> None:
        self._hide_overlay()

    def is_visible(self) -> bool:
        return bool(self._overlay is not None and self._overlay.is_visible())

    def sync_config(self) -> None:
        """Pull league/account changes made inside the EE2 settings UI."""
        try:
            summary = self._brain.request({"cmd": "ee2config"}, timeout=5.0)
        except (RuntimeError, OSError, TimeoutError) as e:
            _LOG.debug("could not sync EE2 config: %s", e)
            return
        league = str(summary.get("league") or self._cfg["league"])
        if league != self._cfg["league"]:
            self._cfg["league"] = league
            _LOG.info("league changed from EE2 settings to %s", league)
            try:
                config.save_league(None, league)
            except OSError as e:
                _LOG.warning("could not persist league: %s", e)

    def _ensure_host(self) -> str:
        result = self._brain.request(
            {
                "cmd": "ee2host",
                "league": self._cfg["league"],
                "accountName": self._cfg["account_name"],
                "sessionId": self._cfg["poesessid"],
            },
            timeout=10.0,
        )
        url = str(result["url"])
        self._url = url
        return url

    def _overlay_geometry(self):
        side = "right"
        output_rect = None
        game_rect = None
        try:
            output_rect = self._desktop.active_output_rect()
        except (RuntimeError, OSError, AttributeError, TypeError, ValueError) as e:
            _LOG.debug("could not read active output rect for EE2 overlay: %s", e)
        if output_rect is not None:
            try:
                output = self._desktop.active_game_output()
                if output:
                    game_rect = self._desktop.active_game_rect(
                        output, (int(output_rect.w), int(output_rect.h))
                    )
            except (RuntimeError, OSError, AttributeError, TypeError, ValueError) as e:
                _LOG.debug("could not read active game rect for EE2 overlay: %s", e)
        try:
            cursor = self._desktop.cursor_pos()
        except (RuntimeError, OSError, AttributeError, TypeError, ValueError) as e:
            _LOG.debug("could not read cursor position for EE2 overlay: %s", e)
            cursor = None
        if cursor is not None and output_rect is not None:
            cursor_x = int(cursor[0]) - int(output_rect.x)
            basis_x = int(game_rect.x) if game_rect is not None else 0
            basis_w = int(game_rect.w) if game_rect is not None else int(output_rect.w)
            side = "left" if cursor_x >= basis_x + basis_w / 2 else "right"
        _LOG.info(
            "EE2 price-check target geometry: side=%s cursor=%s output=%s game=%s",
            side,
            cursor,
            output_rect,
            game_rect,
        )
        return side, output_rect, game_rect

    def _show_overlay(self, gen: int, is_current: Callable[[int], bool], url: str):
        if not is_current(gen):
            return GLib.SOURCE_REMOVE
        if self._overlay is None:
            try:
                self._overlay = Ee2PriceOverlay(self._application)
            except RuntimeError as e:
                _LOG.warning("could not create EE2 price overlay: %s", e)
                return GLib.SOURCE_REMOVE
        self._overlay.show(url, self._side, self._output_rect, self._game_rect)
        self._seen_game_unfocused = not self._desktop.is_game_focused()
        self._on_visibility_changed()
        self._start_state_poll()
        return GLib.SOURCE_REMOVE

    def _sync_state_baseline(self) -> None:
        try:
            state = self._brain.request({"cmd": "ee2state"}, timeout=1.0)
            self._hide_seq = int(state.get("hideRequestSeq") or 0)
        except (RuntimeError, OSError, TimeoutError, ValueError) as e:
            _LOG.debug("could not read EE2 state baseline: %s", e)

    def _start_state_poll(self) -> None:
        if self._state_poll is not None:
            return
        stop = threading.Event()
        self._state_poll = stop
        threading.Thread(target=self._poll_loop, args=(stop,), daemon=True).start()

    def _stop_state_poll(self) -> None:
        stop = self._state_poll
        self._state_poll = None
        self._seen_game_unfocused = False
        if stop is not None:
            stop.set()

    def _poll_loop(self, stop: threading.Event) -> None:
        # Worker thread: the brain round-trip (0.5s timeout) must never sit
        # on the GTK main loop — a stalled brain would freeze all UI.
        while not stop.wait(0.2):
            try:
                state = self._brain.request({"cmd": "ee2state"}, timeout=0.5)
            except (RuntimeError, OSError, TimeoutError) as e:
                _LOG.debug("could not poll EE2 state: %s", e)
                continue
            GLib.idle_add(self._apply_state, state, stop)

    def _apply_state(self, state, stop: threading.Event):
        # Main thread: visibility/focus decisions and GTK hide live here.
        if stop.is_set() or self._state_poll is not stop:
            return GLib.SOURCE_REMOVE
        if not self.is_visible():
            self._stop_state_poll()
            return GLib.SOURCE_REMOVE
        game_focused = self._desktop.is_game_focused()
        if not game_focused:
            self._seen_game_unfocused = True
        if self._seen_game_unfocused and game_focused:
            _LOG.info(
                "hiding EE2 price-check after game focus returned: game_focused=%s",
                game_focused,
            )
            self._hide_overlay()
            return GLib.SOURCE_REMOVE
        try:
            seq = int(state.get("hideRequestSeq") or 0)
        except (TypeError, ValueError) as e:
            _LOG.debug("could not parse EE2 state: %s", e)
            return GLib.SOURCE_REMOVE
        if seq > self._hide_seq:
            reason = str(state.get("hideReason") or "")
            _LOG.info("EE2 requested native hide: seq=%s reason=%s", seq, reason)
            self._hide_seq = seq
            self._hide_overlay()
        return GLib.SOURCE_REMOVE

    def _hide_overlay(self) -> None:
        was_visible = self.is_visible()
        if self._overlay is not None:
            self._overlay.hide()
        self._stop_state_poll()
        if was_visible:
            self._on_visibility_changed()
