"""Drag-to-move for layer-shell surfaces.

Layer-shell windows have no compositor titlebar; we anchor a surface to
TOP+LEFT and treat its TOP/LEFT margins as (y, x). A grab-handle strip with a
Gtk.GestureDrag drives those margins live, then reports the final position so
callers can persist it.

Crucially, the drag position comes from the COMPOSITOR's absolute cursor
(`hyprctl cursorpos`), not the panel's own coordinate frame. Reading the
surface frame (GestureDrag offset OR get_point) is cursed: set_margin is async,
so the surface frame lags the actual commit, and any position formula with a
surface-frame term feeds that lag back in — halving speed (offset) or jumping
at speed (get_point). hyprctl cursorpos is frame-INDEPENDENT (global layout
coords, unaffected by our pending margin commits), so
    margin = cursor_global - output_origin - grab_local
has no surface term and therefore no feedback.
"""
import json
import subprocess

import gi

from poed.subproc import scrubbed_env

gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gdk, GLib, Gtk4LayerShell as LayerShell  # noqa: E402


def monitor_geometry() -> tuple[int, int]:
    """Union extent (width, height) across all monitors in px.

    Returns the far edge of the multi-monitor layout — max(geo.x + geo.width)
    and max(geo.y + geo.height) — not a single monitor. Returning only the
    first monitor walls off any wider/taller sibling screen during clamp (the
    game runs on the 3440-wide DP-3, not the 2560 first monitor). Any failure
    falls back to 1920x1080.
    """
    try:
        monitors = Gdk.Display.get_default().get_monitors()
        max_w, max_h = 0, 0
        for i in range(monitors.get_n_items()):
            mon = monitors.get_item(i)
            if mon is None:
                continue
            geo = mon.get_geometry()
            max_w = max(max_w, geo.x + geo.width)
            max_h = max(max_h, geo.y + geo.height)
        if max_w > 0 and max_h > 0:
            return max_w, max_h
    except Exception:
        pass
    return (1920, 1080)


def monitor_for_rect(rect):
    """Return the GDK monitor matching a compositor output rect, if known."""
    if rect is None:
        return None
    try:
        monitors = Gdk.Display.get_default().get_monitors()
        for i in range(monitors.get_n_items()):
            mon = monitors.get_item(i)
            if mon is None:
                continue
            geo = mon.get_geometry()
            if (
                int(geo.x) == int(rect.x)
                and int(geo.y) == int(rect.y)
                and int(geo.width) == int(rect.w)
                and int(geo.height) == int(rect.h)
            ):
                return mon
    except Exception:
        return None
    return None


def set_monitor_for_rect(win, rect) -> bool:
    """Pin a layer-shell window to the monitor matching a compositor rect.

    Layer-shell margins are relative to one output, not the whole workspace.
    Matching the game output before applying saved margins prevents a coordinate
    from a 2160p output from placing the panel off-screen on a 1440p output.
    """
    mon = monitor_for_rect(rect)
    if mon is None:
        return False
    try:
        LayerShell.set_monitor(win, mon)
        return True
    except Exception:
        return False


def window_monitor_size(win) -> tuple[int, int]:
    """Size of the output currently backing a layer-shell window."""
    try:
        mon = LayerShell.get_monitor(win)
        if mon is not None:
            geo = mon.get_geometry()
            if geo.width > 0 and geo.height > 0:
                return int(geo.width), int(geo.height)
    except Exception:
        pass
    return monitor_geometry()


def _parse_cursorpos(raw: str) -> tuple[int, int] | None:
    """Parse `hyprctl cursorpos` output ("x, y", e.g. "2314, 880"); None on garbage."""
    parts = raw.strip().split(",")
    if len(parts) != 2:
        return None
    try:
        return int(parts[0].strip()), int(parts[1].strip())
    except ValueError:
        return None


def cursor_pos() -> tuple[int, int] | None:
    """Compositor's absolute cursor in global layout coords, or None on failure."""
    try:
        out = subprocess.run(
            ["hyprctl", "cursorpos"],
            capture_output=True,
            text=True,
            timeout=0.5,
            env=scrubbed_env(),
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return _parse_cursorpos(out.stdout)


def monitor_origin_at(gx: int, gy: int) -> tuple[int, int]:
    """Top-left (x, y) of the monitor whose rect contains global point (gx, gy).

    Fallback (0, 0) on failure or no containing monitor.
    """
    try:
        out = subprocess.run(
            ["hyprctl", "monitors", "-j"],
            capture_output=True,
            text=True,
            timeout=0.5,
            env=scrubbed_env(),
        )
        for mon in json.loads(out.stdout):
            mx, my = mon["x"], mon["y"]
            if mx <= gx < mx + mon["width"] and my <= gy < my + mon["height"]:
                return mx, my
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, TypeError):
        pass
    return (0, 0)


def clamp_position(x, y, mon_w, mon_h, win_w, win_h):
    """Keep a window fully on-screen; window-larger-than-monitor pins to 0."""
    max_x = max(0, mon_w - win_w)
    max_y = max(0, mon_h - win_h)
    return (max(0, min(int(x), max_x)), max(0, min(int(y), max_y)))


def clamp_window_position(
    win,
    x: int,
    y: int,
    *,
    default_size: tuple[int, int] = (400, 300),
    monitor_size: tuple[int, int] | None = None,
) -> tuple[int, int]:
    """Clamp a layer-shell margin position to its actual output.

    GTK reports 0/1px dimensions before a surface has been measured. In that
    phase, use a caller-provided conservative default size so stale saved
    coordinates still get moved back onto the visible output before present().
    """
    try:
        win_w, win_h = int(win.get_width()), int(win.get_height())
    except Exception:
        win_w, win_h = 0, 0
    if win_w <= 1:
        win_w = int(default_size[0])
    if win_h <= 1:
        win_h = int(default_size[1])
    mon_w, mon_h = monitor_size if monitor_size is not None else window_monitor_size(win)
    return clamp_position(x, y, mon_w, mon_h, win_w, win_h)


def anchor_top_left(win, x: int, y: int) -> None:
    """Anchor the surface to TOP+LEFT and place it via margins (x=LEFT, y=TOP)."""
    LayerShell.set_anchor(win, LayerShell.Edge.TOP, True)
    LayerShell.set_anchor(win, LayerShell.Edge.LEFT, True)
    LayerShell.set_anchor(win, LayerShell.Edge.BOTTOM, False)
    LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, False)
    set_position(win, x, y)


def set_position(win, x: int, y: int) -> None:
    LayerShell.set_margin(win, LayerShell.Edge.LEFT, int(x))
    LayerShell.set_margin(win, LayerShell.Edge.TOP, int(y))


def make_drag_handle(win, get_xy, on_moved, desktop=None) -> Gtk.Widget:
    """A grab strip; dragging it moves `win` via margins.

    get_xy() -> (x, y) current committed position.
    on_moved(x, y) called on drag-end with the final clamped position.
    """
    handle = Gtk.Box()
    handle.add_css_class("poe-drag")
    handle.set_size_request(-1, 14)
    grip = Gtk.Label(label="⠿⠿⠿")
    grip.add_css_class("poe-dim")
    handle.append(grip)

    drag = Gtk.GestureDrag()
    live = {
        "x": 0, "y": 0, "ox": 0, "oy": 0, "gx": 0.0, "gy": 0.0,
        # Captured once at drag-begin: monitor union + window size are
        # invariant for the duration of a drag, and a hyprctl/GDK walk per
        # motion event is pure overhead.
        "mon": (1920, 1080), "wh": (0, 0),
        # Last cursor poll, GLib monotonic µs — throttles the per-motion
        # `hyprctl cursorpos` subprocess to at most one per 10ms.
        "last_poll": 0,
    }

    def on_begin(_g, sx, sy):
        live["x"], live["y"] = get_xy()      # committed margin (for fallback + final)
        live["gx"], live["gy"] = sx, sy      # grab point in panel-local coords
        live["mon"] = monitor_geometry()
        live["wh"] = (win.get_width(), win.get_height())
        live["last_poll"] = 0
        c = desktop.cursor_pos() if desktop is not None else cursor_pos()
        if c is None:
            live["ox"] = None                # no compositor cursor; will fall back
            return
        # output origin = top-left of the monitor the surface sits on (the one
        # under the cursor at grab). margin is relative to that origin.
        if desktop is not None:
            live["ox"], live["oy"] = desktop.monitor_origin_at(*c)
        else:
            live["ox"], live["oy"] = monitor_origin_at(*c)

    def on_update(_g, off_x, off_y):
        now = GLib.get_monotonic_time()
        if now - live["last_poll"] < 10_000:  # µs — cap polls at ~100/s
            return
        live["last_poll"] = now
        if live["ox"] is None:
            # Fallback: no hyprctl cursor — use the (laggy) offset so drag still
            # half-works on non-Hypr compositors.
            nx, ny = live["x"] + off_x, live["y"] + off_y
        else:
            c = desktop.cursor_pos() if desktop is not None else cursor_pos()
            if c is None:
                return
            # Absolute frame: panel's top-left should sit at cursor minus where
            # on the panel we grabbed, expressed in the surface's output origin.
            # No surface-frame term -> no feedback through the async margin
            # commits that halved/jumped the offset & get_point approaches.
            nx = c[0] - live["ox"] - live["gx"]
            ny = c[1] - live["oy"] - live["gy"]
        mon_w, mon_h = live["mon"]
        w, h = live["wh"]
        if w > 0 and h > 0:
            nx, ny = clamp_position(nx, ny, mon_w, mon_h, w, h)
        live["x"], live["y"] = int(nx), int(ny)
        set_position(win, live["x"], live["y"])

    def on_end(_g, _ox, _oy):
        on_moved(live["x"], live["y"])

    drag.connect("drag-begin", on_begin)
    drag.connect("drag-update", on_update)
    drag.connect("drag-end", on_end)
    handle.add_controller(drag)
    return handle
