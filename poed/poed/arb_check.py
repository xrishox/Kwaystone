"""Alt+S currency-arbitrage lifecycle.

Mirrors PriceCheckController's shape: clipboard grab on a worker, brain does
all rate math (arbquote), the panel shows the instant aggregate answer, and a
state poll (arbstate) applies live-verified rows as the refinement queue lands
them. Hidden by Esc, game-focus return, or a newer press.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import Callable

import gi

gi.require_version("GLib", "2.0")
from gi.repository import GLib  # noqa: E402

from poed import clipboard  # noqa: E402
from poed.arb_panel import ArbPanel  # noqa: E402

_LOG = logging.getLogger("waystone.arb_check")


class ArbCheckController:
    """Owns the Alt+S arbitrage panel and its refinement polling."""

    def __init__(self, application, cfg, brain, desktop, on_visibility_changed):
        self._application = application
        self._cfg = cfg
        self._brain = brain
        self._desktop = desktop
        self._on_visibility_changed = on_visibility_changed
        self._panel: ArbPanel | None = None
        self._poll_stop: threading.Event | None = None
        self._refresh_id = 0
        self._seen_game_unfocused = False

    def is_visible(self) -> bool:
        return bool(self._panel is not None and self._panel.is_visible())

    def hide(self) -> None:
        was_visible = self.is_visible()
        self._stop_poll()
        if self._panel is not None:
            self._panel.hide()
        if was_visible:
            self._on_visibility_changed()

    def run(self, gen: int, is_current: Callable[[int], bool]) -> None:
        """Worker-thread entry: grab the item, get the stage-1 answer, show it."""
        try:
            text = clipboard.grab_item_text(self._desktop.is_game_focused) or ""
            if not is_current(gen):
                return
            answer = self._brain.request(
                {
                    "cmd": "arbquote",
                    "clipboard": text,
                    "league": self._cfg["league"],
                    "accountName": self._cfg["account_name"],
                    "sessionId": self._cfg["poesessid"],
                },
                timeout=30.0,
            )
            if not is_current(gen):
                return
            GLib.idle_add(self._show_panel, gen, is_current, answer)
        except (RuntimeError, OSError, TimeoutError) as e:
            _LOG.warning("arbitrage check failed: %s", e)

    # --- panel + refinement poll (main thread) --------------------------------

    def _show_panel(self, gen: int, is_current, answer: dict):
        if not is_current(gen):
            return GLib.SOURCE_REMOVE
        if self._panel is None:
            self._panel = ArbPanel(
                self._application,
                on_visibility=self._on_visibility_changed,
                desktop=self._desktop,
            )
        self._refresh_id = int(answer.get("refreshId") or 0)
        self._panel.show_answer(answer, str(answer.get("league") or self._cfg["league"]))
        self._seen_game_unfocused = not self._desktop.is_game_focused()
        self._start_poll(gen, is_current)
        return GLib.SOURCE_REMOVE

    def _start_poll(self, gen: int, is_current) -> None:
        self._stop_poll()
        stop = threading.Event()
        self._poll_stop = stop

        def poll():
            while not stop.wait(0.5):
                if not is_current(gen):
                    return
                try:
                    state = self._brain.request(
                        {"cmd": "arbstate", "refreshId": self._refresh_id},
                        timeout=2.0,
                    )
                except (RuntimeError, OSError, TimeoutError) as e:
                    _LOG.debug("arbstate poll failed: %s", e)
                    continue
                GLib.idle_add(self._apply_state, gen, is_current, state, stop)

        threading.Thread(target=poll, daemon=True).start()

    def _stop_poll(self) -> None:
        stop, self._poll_stop = self._poll_stop, None
        if stop is not None:
            stop.set()

    def _apply_state(self, gen: int, is_current, state: dict, stop: threading.Event):
        if stop.is_set() or not is_current(gen):
            return GLib.SOURCE_REMOVE
        if self._panel is None or not self._panel.is_visible():
            self._stop_poll()
            return GLib.SOURCE_REMOVE
        game_focused = self._desktop.is_game_focused()
        if not game_focused:
            self._seen_game_unfocused = True
        if self._seen_game_unfocused and game_focused:
            _LOG.info("hiding arbitrage panel after game focus returned")
            self.hide()
            return GLib.SOURCE_REMOVE
        self._panel.update_state(state, str(state.get("league") or self._cfg["league"]))
        if state.get("done"):
            self._stop_poll()
        return GLib.SOURCE_REMOVE
