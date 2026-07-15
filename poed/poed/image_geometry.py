"""Coordinate helpers shared by capture and scanner implementations."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class Rect:
    x: int
    y: int
    w: int
    h: int

    def clipped(self, max_w: int, max_h: int) -> "Rect | None":
        x0 = max(0, min(int(self.x), int(max_w)))
        y0 = max(0, min(int(self.y), int(max_h)))
        x1 = max(0, min(int(self.x + self.w), int(max_w)))
        y1 = max(0, min(int(self.y + self.h), int(max_h)))
        if x1 <= x0 or y1 <= y0:
            return None
        return Rect(x0, y0, x1 - x0, y1 - y0)




def frame_source(
    shot: np.ndarray,
    game_rect: Rect | None = None,
) -> tuple[np.ndarray, int, int, str]:
    if game_rect is None:
        return shot, 0, 0, "output"
    rect = game_rect.clipped(shot.shape[1], shot.shape[0])
    if rect is None or rect.w < 300 or rect.h < 300:
        return shot, 0, 0, "output"
    return (
        shot[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w],
        rect.x,
        rect.y,
        "game",
    )
