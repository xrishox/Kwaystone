from __future__ import annotations

import logging
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
        self._shortcuts = [
            Shortcut("price-check", "PoE2 price check", cfg["hotkey_price"]),
            Shortcut("unique-scan", "Scan current PoE2 screen", "ALT+x"),
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
        bind_mgr = MultiBindManager.create(
            self.cfg["game_window_class"],
            [
                (price_mods, price_key, "price-check"),
                (unique_mods, unique_key, "unique-scan"),
            ],
        )
        self._bind_mgr = bind_mgr
        bind_mgr.prime()
        sock = bind_mgr.connect_events()
        self._sock = sock

        buf = b""

        def on_hypr_event(_fd, _cond, _data):
            nonlocal buf
            try:
                chunk = sock.recv(4096)
            except BlockingIOError:
                return GLib.SOURCE_CONTINUE
            if not chunk:
                _LOG.warning("event socket closed; dynamic bind disabled until restart")
                bind_mgr.stop()
                return GLib.SOURCE_REMOVE
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                bind_mgr.handle_line(line.decode("utf-8", "replace"))
            return GLib.SOURCE_CONTINUE

        self._source_id = GLibUnix.fd_add_full(
            GLib.PRIORITY_DEFAULT,
            sock.fileno(),
            GLib.IOCondition.IN,
            on_hypr_event,
            None,
        )

    def stop(self) -> None:
        if self._source_id:
            GLib.source_remove(self._source_id)
            self._source_id = 0
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
        if visible:
            self._esc_bind.show()
        else:
            self._esc_bind.hide()

    def is_game_focused(self) -> bool:
        from poed import clipboard

        return clipboard.is_game_focused(self.cfg["game_window_class"])

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

        return draggable.cursor_pos()

    def monitor_origin_at(self, gx: int, gy: int) -> tuple[int, int]:
        from poed import draggable

        return draggable.monitor_origin_at(gx, gy)
