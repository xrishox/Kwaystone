from __future__ import annotations

import logging
import threading
from typing import Callable

import gi

gi.require_version("GLib", "2.0")
gi.require_version("GLibUnix", "2.0")
from gi.repository import GLib, GLibUnix  # noqa: E402

from poed import hyprbind
from poed.hyprbind import EscBind, MultiBindManager
from poed.shortcuts import hypr_bind

from .base import Shortcut
from .capture import grim_output

_LOG = logging.getLogger("waystone.desktop.hyprland")


class HyprlandBackend:
    name = "hyprland"
    uses_portal_shortcuts = True
    # The dynamic hyprctl binds route to portal-registered shortcut names, so
    # a portal failure leaves the backend with no hotkeys at all.
    portal_required = True

    def __init__(self, cfg: dict):
        self.cfg = cfg
        self._on_activated: Callable[[str], None] | None = None
        self._bind_mgr: MultiBindManager | None = None
        self._esc_bind = EscBind("panel-close")
        self._sock = None
        self._source_id = 0
        self._reconnect_source = 0
        self._reconnect_delay = 1.0
        self._focused = False
        self._events_dead = False
        self._panel_visible = False
        self._cursor: tuple[int, int] | None = None
        self._cursor_stop: threading.Event | None = None
        self._shortcuts = [
            Shortcut("price-check", "PoE2 price check", cfg["hotkey_price"]),
            Shortcut("unique-scan", "Scan current PoE2 screen", "ALT+x"),
            Shortcut("arb-check", "Currency arbitrage", cfg["hotkey_arb"]),
            Shortcut("panel-close", "Close PoE2 overlay panel", "ESC"),
        ]

    def portal_shortcuts(self) -> list[tuple[str, str, str]]:
        return [(s.sid, s.description, s.trigger) for s in self._shortcuts]

    def portal_session_token(self) -> str | None:
        # xdph names shortcuts from the caller's systemd scope, not the session
        # token; keep a fresh token per run (see hyprbind.resolve_shortcut_name).
        return None

    def start(self, on_activated: Callable[[str], None]) -> None:
        self._on_activated = on_activated
        price_mods, price_key = hypr_bind(self.cfg["hotkey_price"])
        unique_mods, unique_key = hypr_bind("ALT+x")
        arb_mods, arb_key = hypr_bind(self.cfg["hotkey_arb"])
        bind_mgr = MultiBindManager.create(
            self.cfg["game_window_class"],
            [
                (price_mods, price_key, "price-check"),
                (unique_mods, unique_key, "unique-scan"),
                (arb_mods, arb_key, "arb-check"),
            ],
        )
        self._bind_mgr = bind_mgr
        bind_mgr.prime()
        # Initial focus state comes from one query; afterwards the socket2
        # activewindow stream keeps it current (no per-poll hyprctl spawns).
        from poed import clipboard

        self._focused = clipboard.is_game_focused(self.cfg["game_window_class"])
        self._connect_events()

    def _connect_events(self) -> None:
        """(Re)connect the socket2 event stream; EOF/failure retries with backoff."""
        try:
            sock = self._bind_mgr.connect_events()
        except OSError as e:
            _LOG.warning("hyprland events connect failed: %s; retrying", e)
            self._schedule_reconnect()
            return
        self._events_dead = False
        self._reconnect_delay = 1.0
        self._sock = sock
        buf = b""

        def on_hypr_event(_fd, _cond, _data):
            nonlocal buf
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                return GLib.SOURCE_CONTINUE
            if not chunk:
                _LOG.warning("event socket closed; reconnecting")
                self._events_dead = True
                self._sock = None
                try:
                    sock.close()
                except OSError:
                    pass
                self._schedule_reconnect()
                return GLib.SOURCE_REMOVE
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                text = line.decode("utf-8", "replace")
                self._track_focus(text)
                self._bind_mgr.handle_line(text)
            return GLib.SOURCE_CONTINUE

        self._source_id = GLibUnix.fd_add_full(
            GLib.PRIORITY_DEFAULT,
            sock.fileno(),
            GLib.IOCondition.IN,
            on_hypr_event,
            None,
        )

    def _schedule_reconnect(self) -> None:
        if self._reconnect_source:
            return
        delay = self._reconnect_delay
        self._reconnect_delay = min(self._reconnect_delay * 2, 15.0)

        def run():
            self._reconnect_source = 0
            # Runtime binds and their portal-name targets may not have
            # survived whatever killed the stream: reset, re-resolve, and
            # re-prime from live window state before listening again.
            if self._bind_mgr is not None:
                self._bind_mgr.reset_binds()
                self._bind_mgr.prime()
            self._esc_bind.reset_bind()
            if self._panel_visible:
                self._esc_bind.show()
            self._connect_events()
            return GLib.SOURCE_REMOVE

        self._reconnect_source = GLib.timeout_add_seconds(int(delay), run)

    def _track_focus(self, line: str) -> None:
        # "activewindow>>CLASS,TITLE" (title may contain commas; empty data
        # means nothing is focused). activewindowv2 carries only an address
        # and is ignored — the classful event accompanies it.
        event, _, data = line.partition(">>")
        if event != "activewindow":
            return
        klass, _, _title = data.partition(",")
        self._focused = bool(klass) and klass == self.cfg["game_window_class"]

    def stop(self) -> None:
        if self._source_id:
            GLib.source_remove(self._source_id)
            self._source_id = 0
        if self._reconnect_source:
            GLib.source_remove(self._reconnect_source)
            self._reconnect_source = 0
        self._stop_cursor_tracker()
        if self._bind_mgr is not None:
            self._bind_mgr.stop()
        self._esc_bind.stop()
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def on_shortcuts_bound(self) -> None:
        if self._bind_mgr is not None:
            self._bind_mgr.notify_registered()

    def set_panel_visible(self, visible: bool) -> None:
        self._panel_visible = bool(visible)
        if visible:
            self._esc_bind.show()
            self._start_cursor_tracker()
        else:
            self._esc_bind.hide()
            self._stop_cursor_tracker()

    def _start_cursor_tracker(self) -> None:
        # Panel drags read the compositor cursor at up to 100/s; spawning
        # hyprctl per read on the GTK loop would hitch the drag. Poll on a
        # worker while any panel is visible and serve reads from the cache.
        if self._cursor_stop is not None:
            return
        stop = threading.Event()
        self._cursor_stop = stop

        def poll():
            # Lazy import: draggable pulls in the layer-shell typelib, which
            # must not be required at backend-import time (tests, non-GTK).
            from poed import draggable

            while not stop.wait(0.02):
                pos = draggable.cursor_pos()
                if pos is not None:
                    self._cursor = pos

        threading.Thread(target=poll, daemon=True).start()

    def _stop_cursor_tracker(self) -> None:
        stop, self._cursor_stop = self._cursor_stop, None
        if stop is not None:
            stop.set()

    def is_game_focused(self) -> bool:
        if self._events_dead:
            # Socket2 is gone: degrade to the old subprocess probe rather
            # than trusting a stale flag.
            from poed import clipboard

            return clipboard.is_game_focused(self.cfg["game_window_class"])
        return self._focused

    def active_game_output(self) -> str | None:
        return hyprbind.active_game_output(self.cfg["game_window_class"])

    def active_game_rect(self, output: str, frame_size: tuple[int, int]):
        return hyprbind.active_game_rect(
            self.cfg["game_window_class"], output, frame_size
        )

    def active_output_rect(self):
        return hyprbind.active_output_rect(self.cfg["game_window_class"])

    def capture_output(self, output: str):
        return grim_output(output)

    def cursor_pos(self) -> tuple[int, int] | None:
        from poed import draggable

        if self._cursor is not None:
            return self._cursor
        # Cold start (panel just shown, tracker hasn't polled yet).
        return draggable.cursor_pos()

    def monitor_origin_at(self, gx: int, gy: int) -> tuple[int, int]:
        from poed import draggable

        return draggable.monitor_origin_at(gx, gy)
