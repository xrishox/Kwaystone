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


def active_game_output() -> str | None:
    """Name of the monitor showing the focused window (`hyprctl activewindow
    -j` monitor id -> `hyprctl monitors -j` name); None on any failure."""
    try:
        win = json.loads(_hyprctl_out("activewindow", "-j"))
        mons = json.loads(_hyprctl_out("monitors", "-j"))
        for m in mons:
            if m.get("id") == win.get("monitor"):
                return m.get("name")
    except (OSError, ValueError, KeyError, TypeError, AttributeError):
        pass
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
