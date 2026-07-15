"""Desktop/compositor backends for Kwaystone's Wayland-facing side."""

import os

from .base import DesktopBackend, Rect, Shortcut


def _desktop_env() -> str:
    return (
        os.environ.get("WAYSTONE_DESKTOP_BACKEND")
        or os.environ.get("XDG_CURRENT_DESKTOP")
        or os.environ.get("XDG_SESSION_DESKTOP")
        or ""
    ).lower()


def create_backend(cfg: dict) -> DesktopBackend:
    forced = os.environ.get("WAYSTONE_DESKTOP_BACKEND", "").lower()
    desktop = _desktop_env()
    if forced in {"kwin", "kde"} or (
        "kde" in desktop and os.environ.get("WAYLAND_DISPLAY")
    ):
        from .kwin import KWinBackend

        return KWinBackend(cfg)
    if forced in {"hypr", "hyprland"} or os.environ.get("HYPRLAND_INSTANCE_SIGNATURE"):
        from .hyprland import HyprlandBackend

        return HyprlandBackend(cfg)
    raise SystemExit(
        "unsupported Wayland compositor: Kwaystone currently supports "
        "Hyprland and KDE Plasma Wayland"
    )


__all__ = ["DesktopBackend", "Rect", "Shortcut", "create_backend"]
