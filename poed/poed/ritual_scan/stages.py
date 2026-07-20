"""Geometry and occupancy types for the ritual scanning pipeline.

Every candidate system produces the same artifacts (panel hypothesis, lattice,
occupancy, footprints, matches) so systems can be scored uniformly and their
stages recombined during ablation.
"""

from __future__ import annotations

from dataclasses import dataclass
import numpy as np

from poed.image_geometry import Rect


@dataclass(frozen=True)
class PanelHypothesis:
    """Localized Favours panel; `rect` is the grid interior in frame coords."""

    rect: Rect
    plaque_rect: Rect | None = None
    confidence: float = 0.0
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class Lattice:
    """Subpixel cell lattice. Cell rect edges derive from the float origin and
    pitch so rounding error never accumulates across columns/rows."""

    x0: float
    y0: float
    pitch_x: float
    pitch_y: float
    cols: int
    rows: int

    def cell_rect(self, col: int, row: int, w: int = 1, h: int = 1) -> Rect:
        left = int(round(self.x0 + col * self.pitch_x))
        top = int(round(self.y0 + row * self.pitch_y))
        right = int(round(self.x0 + (col + w) * self.pitch_x))
        bottom = int(round(self.y0 + (row + h) * self.pitch_y))
        return Rect(left, top, right - left, bottom - top)

    def frame_rect(self) -> Rect:
        return self.cell_rect(0, 0, self.cols, self.rows)

    def cell_at(self, x: float, y: float) -> tuple[int, int]:
        return (
            int((x - self.x0) // self.pitch_x),
            int((y - self.y0) // self.pitch_y),
        )


@dataclass
class OccupancyMap:
    """Per-cell item-content energy and thresholded occupancy."""

    energy: np.ndarray
    occupied: np.ndarray

    @property
    def rows(self) -> int:
        return int(self.occupied.shape[0])

    @property
    def cols(self) -> int:
        return int(self.occupied.shape[1])


@dataclass(frozen=True)
class Footprint:
    col: int
    row: int
    w: int
    h: int

    def rect(self, lattice: Lattice) -> Rect:
        return lattice.cell_rect(self.col, self.row, self.w, self.h)
