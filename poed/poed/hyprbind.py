"""Consume the price-check hotkey only while the game window exists.

A static Hyprland `bind` would swallow Ctrl+D system-wide (terminal EOF, ...).
Instead poed adds the bind via `hyprctl keyword bind` when the PoE2 window
appears and removes it when the window closes or poed exits. Window presence
comes from Hyprland's socket2 event stream.
"""
import json
import os
import socket
import subprocess

from poed.image_geometry import Rect


def _norm(addr: str) -> str:
    """Normalize a Hyprland window address to bare hex (no 0x prefix, no whitespace).

    hyprctl clients -j returns addresses as "0x560e297adf80"; socket2
    openwindow events may also carry the prefix, while closewindow always uses
    bare hex.  Storing and comparing the bare form everywhere avoids mismatches.
    """
    return addr.strip().removeprefix("0x")


def _hyprctl(*args: str) -> bool:
    try:
        r = subprocess.run(["hyprctl", *args], capture_output=True, timeout=2.0)
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False  # compositor gone; nothing sensible to do


def resolve_shortcut_name(shortcut_id: str, _raw: str | None = None) -> str | None:
    """Full 'appid:shortcut' name as the portal actually registered it.

    xdph derives the appid from the caller's systemd scope (terminal launch ->
    'xdg-terminal-exec', etc.), ignoring our DBus app_id — so the bind arg
    must be queried, never assumed.

    `_raw` is injectable for tests; production runs `hyprctl globalshortcuts -j`
    (2s timeout, any failure -> None). Returns the registered `name` whose
    suffix after the colon equals `shortcut_id`, else None.
    """
    if _raw is None:
        try:
            _raw = subprocess.run(
                ["hyprctl", "globalshortcuts", "-j"],
                capture_output=True, text=True, timeout=2.0,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            return None
    try:
        entries = json.loads(_raw)
    except (ValueError, TypeError):
        return None
    if not isinstance(entries, list):
        return None
    for entry in entries:
        name = entry.get("name") if isinstance(entry, dict) else None
        if isinstance(name, str) and name.rsplit(":", 1)[-1] == shortcut_id:
            return name
    return None


class BindManager:
    def __init__(self, game_class: str, mods: str, key: str, shortcut_id: str,
                 _ctl=_hyprctl, _resolve=resolve_shortcut_name):
        self._game_class = game_class
        self._mods = mods
        self._key = key
        self._shortcut_id = shortcut_id
        self._unbind_arg = f"{mods},{key}"
        self._ctl = _ctl
        self._resolve = _resolve
        self._resolved_name: str | None = None
        self._game_windows: set[str] = set()
        self._bound = False

    # -- socket2 line protocol: "event>>data" ------------------------------

    def handle_line(self, line: str) -> None:
        event, _, data = line.partition(">>")
        if event == "openwindow":
            # data: ADDR,WORKSPACE,CLASS,TITLE (title may contain commas)
            parts = data.split(",", 3)
            if len(parts) >= 3:
                addr, _, klass = parts[0], parts[1], parts[2]
                if klass == self._game_class:
                    self._game_windows.add(_norm(addr))
                    self._sync()
        elif event == "closewindow":
            self._game_windows.discard(_norm(data))
            self._sync()

    def _sync(self) -> None:
        want = bool(self._game_windows)
        if want and not self._bound:
            if self._resolved_name is None:
                # Resolve lazily; the portal may not have registered yet.
                self._resolved_name = self._resolve(self._shortcut_id)
                if self._resolved_name is None:
                    return  # defer: next _sync (or notify_registered) retries
            bind_arg = f"{self._mods},{self._key},global,{self._resolved_name}"
            ok = self._ctl("keyword", "bind", bind_arg)
            if ok:
                self._bound = True
        elif not want and self._bound:
            ok = self._ctl("keyword", "unbind", self._unbind_arg)
            if ok:
                self._bound = False

    def notify_registered(self) -> None:
        """Portal BindShortcuts completed; retry a deferred bind if needed."""
        self._sync()

    # -- lifecycle ----------------------------------------------------------

    def prime(self) -> None:
        """Bind immediately if the game is already running at poed startup."""
        try:
            out = subprocess.run(
                ["hyprctl", "clients", "-j"],
                capture_output=True, text=True, timeout=2.0,
            ).stdout
            for c in json.loads(out):
                if c.get("class") == self._game_class:
                    self._game_windows.add(_norm(c.get("address", "?")))
        except (OSError, subprocess.SubprocessError, ValueError):
            pass
        self._sync()

    def stop(self) -> None:
        if self._bound:
            ok = self._ctl("keyword", "unbind", self._unbind_arg)
            if ok:
                self._bound = False

    def socket_path(self) -> str:
        runtime = os.environ["XDG_RUNTIME_DIR"]
        sig = os.environ["HYPRLAND_INSTANCE_SIGNATURE"]
        return f"{runtime}/hypr/{sig}/.socket2.sock"

    def connect_events(self) -> socket.socket:
        s = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        s.connect(self.socket_path())
        s.setblocking(False)
        return s


class MultiBindManager:
    """Fan one socket2 event stream out to several BindManagers (one per
    hotkey). Window tracking stays per-manager; the socket is shared."""

    def __init__(self, managers: list[BindManager]):
        self._managers = managers

    @classmethod
    def create(cls, game_class: str, binds: list[tuple[str, str, str]],
               _ctl=_hyprctl, _resolve=resolve_shortcut_name) -> "MultiBindManager":
        return cls([
            BindManager(game_class, mods, key, sid, _ctl=_ctl, _resolve=_resolve)
            for mods, key, sid in binds
        ])

    def handle_line(self, line: str) -> None:
        for m in self._managers:
            m.handle_line(line)

    def notify_registered(self) -> None:
        for m in self._managers:
            m.notify_registered()

    def prime(self) -> None:
        for m in self._managers:
            m.prime()

    def stop(self) -> None:
        for m in self._managers:
            m.stop()

    def socket_path(self) -> str:
        return self._managers[0].socket_path()

    def connect_events(self) -> socket.socket:
        return self._managers[0].connect_events()


def _is_game_window(win: dict, game_class: str) -> bool:
    klass = win.get("class") or win.get("initialClass") or ""
    title = win.get("title") or ""
    return klass == game_class or title == "Path of Exile 2"


def _active_or_known_game_window(game_class: str) -> dict | None:
    active = json.loads(_hyprctl_out("activewindow", "-j"))
    if isinstance(active, dict) and _is_game_window(active, game_class):
        return active

    clients = json.loads(_hyprctl_out("clients", "-j"))
    if not isinstance(clients, list):
        return None
    matches = [
        c for c in clients if isinstance(c, dict) and _is_game_window(c, game_class)
    ]
    if not matches:
        return None
    matches.sort(key=lambda c: c.get("focusHistoryID", 999999))
    return matches[0]


def active_game_output(game_class: str | None = None) -> str | None:
    """Name of the monitor showing the game window. With no game class, retain
    the older focused-window behaviour for callers/tests that only need it."""
    try:
        win = (
            _active_or_known_game_window(game_class)
            if game_class
            else json.loads(_hyprctl_out("activewindow", "-j"))
        )
        if not isinstance(win, dict):
            return None
        mons = json.loads(_hyprctl_out("monitors", "-j"))
        for m in mons:
            if m.get("id") == win.get("monitor"):
                return m.get("name")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
    return None


def active_game_rect(
    game_class: str,
    output: str,
    frame_size: tuple[int, int],
) -> Rect | None:
    """PoE window rectangle in captured-output pixels, or None on failure."""
    try:
        win = _active_or_known_game_window(game_class)
        if not isinstance(win, dict):
            return None
        mons = json.loads(_hyprctl_out("monitors", "-j"))
        mon = next(
            (m for m in mons if isinstance(m, dict) and m.get("name") == output),
            None,
        )
        if mon is None:
            return None

        at = win.get("at")
        size = win.get("size")
        if not (
            isinstance(at, list)
            and len(at) >= 2
            and isinstance(size, list)
            and len(size) >= 2
        ):
            return None

        mx, my = float(mon["x"]), float(mon["y"])
        mw, mh = float(mon["width"]), float(mon["height"])
        wx, wy = float(at[0]), float(at[1])
        ww, wh = float(size[0]), float(size[1])
        frame_w, frame_h = frame_size
        if mw <= 0 or mh <= 0 or frame_w <= 0 or frame_h <= 0:
            return None

        ix0 = max(wx, mx)
        iy0 = max(wy, my)
        ix1 = min(wx + ww, mx + mw)
        iy1 = min(wy + wh, my + mh)
        if ix1 <= ix0 or iy1 <= iy0:
            return None

        sx = frame_w / mw
        sy = frame_h / mh
        return Rect(
            int(round((ix0 - mx) * sx)),
            int(round((iy0 - my) * sy)),
            int(round((ix1 - ix0) * sx)),
            int(round((iy1 - iy0) * sy)),
        ).clipped(frame_w, frame_h)
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def active_output_rect(game_class: str) -> Rect | None:
    try:
        output = active_game_output(game_class)
        if not output:
            return None
        mons = json.loads(_hyprctl_out("monitors", "-j"))
        mon = next(
            (m for m in mons if isinstance(m, dict) and m.get("name") == output),
            None,
        )
        if mon is None:
            return None
        return Rect(
            int(mon["x"]),
            int(mon["y"]),
            int(mon["width"]),
            int(mon["height"]),
        )
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        return None


def _hyprctl_out(*args: str) -> str:
    r = subprocess.run(["hyprctl", *args], capture_output=True, text=True,
                       timeout=2.0)
    return r.stdout


class EscBind:
    """Consume Esc ONLY while the overlay panel is visible.

    A static Esc bind would swallow Esc system-wide (the game loses Esc); bind
    on show, unbind on hide/exit. Resolution of the portal-registered shortcut
    name is lazy and cached (same xdph appid quirk as BindManager).
    """

    def __init__(self, shortcut_id: str, _ctl=_hyprctl, _resolve=resolve_shortcut_name):
        self._shortcut_id = shortcut_id
        self._ctl = _ctl
        self._resolve = _resolve
        self._resolved_name: str | None = None
        self._bound = False

    def show(self) -> None:
        if self._bound:
            return
        if self._resolved_name is None:
            self._resolved_name = self._resolve(self._shortcut_id)
            if self._resolved_name is None:
                return  # defer silently: next show() retries
        bind_arg = f",Escape,global,{self._resolved_name}"
        if self._ctl("keyword", "bind", bind_arg):
            self._bound = True

    def hide(self) -> None:
        if not self._bound:
            return
        if self._ctl("keyword", "unbind", ",Escape"):
            self._bound = False

    stop = hide
