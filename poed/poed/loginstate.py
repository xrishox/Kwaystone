"""Persist whether the user was logged in, to XDG state.

Stores ONLY a boolean flag — never the POESESSID value. On startup poed
reads this flag and, if set, re-runs the same login resolution the Login
button does (config poesessid, else Firefox auto-detect); a fresh session
value is resolved live, never read from here.

Read/write are defensive: a missing or corrupt file behaves as 'anonymous',
never raises into the UI — same posture as positions.py.
"""
import json
import os
from pathlib import Path

from poed import config


def default_path() -> Path:
    state = Path(os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state"))
    config.migrate_dir(state / "poe2-overlay", state / "waystone")
    return state / "waystone" / "login.json"


class LoginState:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else default_path()
        self._flag = False
        try:
            data = json.loads(self._path.read_text())
            # Only a real JSON `true` counts; anything else is anonymous.
            self._flag = isinstance(data, dict) and data.get("logged_in") is True
        except (OSError, ValueError):
            self._flag = False

    def logged_in(self) -> bool:
        return self._flag

    def set(self, value: bool) -> None:
        self._flag = bool(value)
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps({"logged_in": self._flag}))
        except OSError:
            pass  # best-effort; the flag is a convenience, not critical
