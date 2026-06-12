"""Per-panel window positions, persisted to XDG state.

Drag-to-move writes {panel_name: {x, y}} here so panels reopen where the
user left them. Read/write are defensive: a missing or corrupt file behaves
as 'no saved positions', never raises into the UI.
"""
import json
import os
from pathlib import Path


def default_path() -> Path:
    state = os.environ.get("XDG_STATE_HOME") or (Path.home() / ".local/state")
    return Path(state) / "poe2-overlay" / "positions.json"


class PositionStore:
    def __init__(self, path: Path | None = None):
        self._path = Path(path) if path else default_path()
        self._data: dict = {}
        try:
            self._data = json.loads(self._path.read_text())
            if not isinstance(self._data, dict):
                self._data = {}
        except (OSError, ValueError):
            self._data = {}

    def get(self, name: str) -> tuple[int, int] | None:
        e = self._data.get(name)
        if isinstance(e, dict) and "x" in e and "y" in e:
            return int(e["x"]), int(e["y"])
        return None

    def set(self, name: str, x: int, y: int) -> None:
        self._data[name] = {"x": int(x), "y": int(y)}
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data))
        except OSError:
            pass  # best-effort; position memory is a nicety, not critical
