from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Protocol

from poed.image_geometry import Rect  # noqa: F401 - re-exported for backends


@dataclass(frozen=True)
class Shortcut:
    sid: str
    description: str
    trigger: str




class DesktopBackend(Protocol):
    name: str
    uses_portal_shortcuts: bool

    def portal_shortcuts(self) -> list[tuple[str, str, str]]:
        ...

    def start(self, on_activated: Callable[[str], None]) -> None:
        ...

    def stop(self) -> None:
        ...

    def on_shortcuts_bound(self) -> None:
        ...

    def set_panel_visible(self, visible: bool) -> None:
        ...

    def is_game_focused(self) -> bool:
        ...

    def active_game_output(self) -> str | None:
        ...

    def active_game_rect(self, output: str, frame_size: tuple[int, int]) -> Rect | None:
        ...

    def active_output_rect(self) -> Rect | None:
        ...

    def capture_output(self, output: str):
        ...

    def cursor_pos(self) -> tuple[int, int] | None:
        ...

    def monitor_origin_at(self, gx: int, gy: int) -> tuple[int, int]:
        ...
