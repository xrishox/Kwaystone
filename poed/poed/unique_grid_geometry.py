"""Geometry helpers for item-grid based scanners.

These functions know about visible grid structure and occupied cells, but not
about icon templates, prices, overlays, or scanner routing.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

RITUAL_GRID_REGION = (0.085, 0.972, 0.065, 0.792)
RITUAL_COLS = 12
RITUAL_ROWS = 10


@dataclass(frozen=True)
class Grid:
    x0: float
    y0: float
    cell: float
    cols: int = RITUAL_COLS
    rows: int = RITUAL_ROWS


def ritual_grid(crop: np.ndarray) -> Grid | None:
    h, w = crop.shape[:2]
    rx0, rx1, ry0, ry1 = RITUAL_GRID_REGION
    x0 = w * rx0
    x1 = w * rx1
    y0 = h * ry0
    y1 = h * ry1
    cell_x = (x1 - x0) / RITUAL_COLS
    cell_y = (y1 - y0) / RITUAL_ROWS
    if cell_x < 35 or cell_y < 35:
        return None
    if abs(cell_x - cell_y) / max(cell_x, cell_y) > 0.20:
        return None
    return Grid(x0=x0, y0=y0, cell=(cell_x + cell_y) / 2.0)


def cell_rect(
    grid: Grid,
    col: int,
    row: int,
    slots_w: int = 1,
    slots_h: int = 1,
    pad: float = 0.0,
) -> tuple[int, int, int, int]:
    p = grid.cell * pad
    x0 = int(round(grid.x0 + col * grid.cell - p))
    y0 = int(round(grid.y0 + row * grid.cell - p))
    x1 = int(round(grid.x0 + (col + slots_w) * grid.cell + p))
    y1 = int(round(grid.y0 + (row + slots_h) * grid.cell + p))
    return x0, y0, x1, y1


def cluster_positions(positions: list[float], tolerance: float = 4.0) -> list[float]:
    if not positions:
        return []
    groups: list[list[float]] = []
    for position in sorted(positions):
        if not groups or position - groups[-1][-1] > tolerance:
            groups.append([position])
        else:
            groups[-1].append(position)
    return [float(np.mean(group)) for group in groups]


def best_grid_origin(
    positions: list[float],
    pitch: float,
    count: int,
    limit: int,
) -> float | None:
    """Recover the first grid boundary from noisy Hough line positions."""
    if not positions:
        return None
    tolerance = max(6.0, pitch * 0.08)
    best: tuple[tuple[int, float, float], float] | None = None
    for position in positions:
        for step in range(count + 1):
            candidate = position - step * pitch
            if candidate < -pitch or candidate > limit:
                continue
            origins: list[float] = []
            error = 0.0
            for index in range(count + 1):
                target = candidate + index * pitch
                nearby = [line for line in positions if abs(line - target) <= tolerance]
                if not nearby:
                    continue
                closest = min(nearby, key=lambda line: abs(line - target))
                origins.append(closest - index * pitch)
                error += abs(closest - target)
            if len(origins) < 4:
                continue
            refined = float(np.median(origins))
            key = (len(origins), -error, -abs(refined))
            if best is None or key > best[0]:
                best = (key, refined)
    return None if best is None else best[1]


def axis_cell_count(
    positions: list[float],
    origin: float,
    pitch: float,
    max_count: int,
    limit: int,
) -> int:
    """Return the visible cell count supported by detected grid boundaries.

    ``best_grid_origin`` deliberately assumes the largest known Ritual grid so
    it can recover an origin even when item art hides the first/last boundary.
    After that origin is known, the usable extent should come from confirmed
    boundary lines rather than the maximum grid size.  This keeps windowed or
    clipped Ritual panels from generating cells over unrelated game-world UI.
    """

    tolerance = max(6.0, pitch * 0.08)
    supported_steps: list[int] = []
    for step in range(max_count + 1):
        target = origin + step * pitch
        if target < -tolerance or target > limit + tolerance:
            continue
        if any(abs(position - target) <= tolerance for position in positions):
            supported_steps.append(step)
    if not supported_steps:
        return max_count
    return max(1, min(max_count, max(supported_steps)))


def dynamic_ritual_grid(crop: np.ndarray, cell: float | None) -> Grid | None:
    """Find a Ritual-like grid inside a dynamically localized crop."""
    if cell is None or cell < 35:
        return None
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 40, 100)
    lines = cv2.HoughLinesP(
        edges,
        1,
        np.pi / 180,
        threshold=50,
        minLineLength=max(70, int(crop.shape[0] * 0.12)),
        maxLineGap=12,
    )
    if lines is None:
        return None

    vertical: list[float] = []
    horizontal: list[float] = []
    for x0, y0, x1, y1 in lines[:, 0]:
        if abs(int(x1) - int(x0)) <= 3:
            top, bottom = sorted((float(y0), float(y1)))
            if bottom - top > crop.shape[0] * 0.45:
                vertical.append((float(x0) + float(x1)) / 2.0)
        if abs(int(y1) - int(y0)) <= 3:
            left, right = sorted((float(x0), float(x1)))
            if right - left > crop.shape[1] * 0.35:
                horizontal.append((float(y0) + float(y1)) / 2.0)

    xs = cluster_positions(vertical)
    ys = cluster_positions(horizontal)
    origin_x = best_grid_origin(xs, cell, RITUAL_COLS, crop.shape[1])
    origin_y = best_grid_origin(ys, cell, RITUAL_ROWS, crop.shape[0])
    if origin_x is None or origin_y is None:
        return None
    cols = axis_cell_count(xs, origin_x, cell, RITUAL_COLS, crop.shape[1])
    rows = axis_cell_count(ys, origin_y, cell, RITUAL_ROWS, crop.shape[0])
    return Grid(origin_x, origin_y, cell, cols=cols, rows=rows)


def occupied_cells(crop: np.ndarray, grid: Grid) -> np.ndarray:
    occ = np.zeros((grid.rows, grid.cols), dtype=bool)
    for row in range(grid.rows):
        for col in range(grid.cols):
            x0, y0, x1, y1 = cell_rect(grid, col, row, pad=-0.08)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(crop.shape[1], x1), min(crop.shape[0], y1)
            if x1 <= x0 or y1 <= y0:
                continue
            roi = crop[y0:y1, x0:x1]
            hsv = cv2.cvtColor(roi, cv2.COLOR_BGR2HSV)
            val = float(hsv[:, :, 2].mean())
            sat = float(hsv[:, :, 1].mean())
            std = float(roi.std(axis=(0, 1)).mean())
            # The game tints an item's whole footprint navy, including cells
            # where the art is nearly invisible (dark weapon shafts). Empty
            # cells measure sat ~0, footprint cells sat 120+, so the tint is
            # decisive even when the texture tests fail.
            blue = float((
                (hsv[:, :, 0] >= 100)
                & (hsv[:, :, 0] <= 140)
                & (hsv[:, :, 1] >= 40)
            ).mean())
            occ[row, col] = (
                (val > 18 and sat > 35 and std > 10)
                or (val > 28 and std > 16)
                or (val > 12 and sat > 80 and blue > 0.40)
            )
    return occ


def rect_intersection_area(
    a: tuple[int, int, int, int],
    b: tuple[int, int, int, int],
) -> int:
    ix = max(0, min(a[2], b[2]) - max(a[0], b[0]))
    iy = max(0, min(a[3], b[3]) - max(a[1], b[1]))
    return ix * iy


def cell_has_match(
    cell: tuple[int, int, int, int],
    matches: list[dict],
) -> bool:
    cell_area = max(1, (cell[2] - cell[0]) * (cell[3] - cell[1]))
    for match in matches:
        try:
            x, y, w, h = (int(match[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        match_rect = (x, y, x + w, y + h)
        if rect_intersection_area(cell, match_rect) >= cell_area * 0.30:
            return True
        cx = x + w / 2.0
        cy = y + h / 2.0
        if cell[0] <= cx <= cell[2] and cell[1] <= cy <= cell[3]:
            return True
    return False


def unknown_occupied_cells(
    crop: np.ndarray,
    grid: Grid,
    matches: list[dict],
) -> list[dict]:
    occupied = occupied_cells(crop, grid)
    unexplained = np.zeros_like(occupied)
    rects: dict[tuple[int, int], tuple[int, int, int, int]] = {}
    for row in range(grid.rows):
        for col in range(grid.cols):
            if not bool(occupied[row, col]):
                continue
            x0, y0, x1, y1 = cell_rect(grid, col, row, pad=0.0)
            x0, y0 = max(0, x0), max(0, y0)
            x1, y1 = min(crop.shape[1], x1), min(crop.shape[0], y1)
            if x1 <= x0 or y1 <= y0:
                continue
            if cell_has_match((x0, y0, x1, y1), matches):
                continue
            unexplained[row, col] = True
            rects[(row, col)] = (x0, y0, x1, y1)

    # One marker per 4-connected component: an unrecognized multi-cell item
    # is one thing on screen and reads as one thing in the results, not as
    # a scatter of per-cell squares.
    count, labels = cv2.connectedComponents(
        unexplained.astype(np.uint8), connectivity=4
    )
    unknown: list[dict] = []
    for component in range(1, count):
        cells = [key for key in rects if labels[key[0], key[1]] == component]
        if not cells:
            continue
        row0 = min(c[0] for c in cells)
        row1 = max(c[0] for c in cells)
        col0 = min(c[1] for c in cells)
        col1 = max(c[1] for c in cells)
        rectangular = len(cells) == (row1 - row0 + 1) * (col1 - col0 + 1)
        if rectangular:
            # A rectangle-filling component is one item footprint (or one
            # contiguous unknown region): one marker. A partial bbox would
            # cover cells the component does not own and get suppressed
            # against real neighbours, so L-shaped scatter stays per-cell.
            groups: list[list[tuple[int, int]]] = [cells]
        else:
            groups = [[cell] for cell in cells]
        for group in groups:
            x0 = min(rects[c][0] for c in group)
            y0 = min(rects[c][1] for c in group)
            x1 = max(rects[c][2] for c in group)
            y1 = max(rects[c][3] for c in group)
            top_left = min(group, key=lambda c: (c[0], c[1]))
            unknown.append({
                "name": "Unrecognized reward",
                "kind": "unknown",
                "price": 0,
                "quantity": 0,
                "priceAvailable": False,
                "markerOnly": True,
                "ambiguous": False,
                "trend": None,
                "score": 0.0,
                "x": x0,
                "y": y0,
                "w": x1 - x0,
                "h": y1 - y0,
                "cell": (top_left[1], top_left[0]),
                "cells": len(group),
            })
    return unknown


def candidate_slots(occ: np.ndarray, slots_w: int, slots_h: int):
    rows, cols = occ.shape
    sparse_art_allowed = sparse_item_art_allowed(slots_w, slots_h)
    required = candidate_required_cells(slots_w, slots_h)
    # Multi-slot item art can be visually sparse in owned cells when the icon is
    # diagonal, asymmetric, or mostly decoration/background. Generate sparse
    # candidates only for shapes with enough geometric evidence, then let
    # template matching decide. Small 2x2 items are intentionally stricter than
    # larger footprints: three occupied cells are required so two adjacent 1x1
    # rewards do not get merged.
    for row in range(rows - slots_h + 1):
        for col in range(cols - slots_w + 1):
            if not sparse_art_allowed and not occ[row, col]:
                continue
            cells = occ[row:row + slots_h, col:col + slots_w]
            filled = int(np.count_nonzero(cells))
            if filled < required:
                continue
            if sparse_art_allowed and not _large_slot_has_spread(cells, slots_w, slots_h):
                continue
            yield col, row


def sparse_item_art_allowed(slots_w: int, slots_h: int) -> bool:
    area = slots_w * slots_h
    return area >= 6 or max(slots_w, slots_h) >= 3 or (slots_w, slots_h) == (2, 2)


def candidate_required_cells(slots_w: int, slots_h: int) -> int:
    area = slots_w * slots_h
    if not sparse_item_art_allowed(slots_w, slots_h):
        return area
    if (slots_w, slots_h) == (2, 2):
        return 3
    return max(2, int(np.ceil(area * 0.5)))


def _large_slot_has_spread(cells: np.ndarray, slots_w: int, slots_h: int) -> bool:
    """Return whether sparse large-item evidence spans the expected footprint.

    A large reward can have empty-looking owned cells because the icon art is
    diagonal or concentrated on one side. Requiring half the rectangle to be
    occupied recovers those cases, but without a spread check a vertical stack
    of unrelated 1x1 rewards could accidentally produce a wide candidate. Keep
    the rule geometric: multi-column items need evidence in multiple columns;
    multi-row items need evidence in multiple rows.
    """

    occupied_rows = np.flatnonzero(cells.any(axis=1))
    occupied_cols = np.flatnonzero(cells.any(axis=0))
    if slots_w > 1 and len(occupied_cols) < 2:
        return False
    if slots_h > 1 and len(occupied_rows) < min(2, slots_h):
        return False
    return True


# Compatibility aliases for historical private tests/callers.
_ritual_grid = ritual_grid
_cell_rect = cell_rect
_cluster_positions = cluster_positions
_best_grid_origin = best_grid_origin
_axis_cell_count = axis_cell_count
_dynamic_ritual_grid = dynamic_ritual_grid
_occupied_cells = occupied_cells
_rect_intersection_area = rect_intersection_area
_cell_has_match = cell_has_match
_unknown_occupied_cells = unknown_occupied_cells
_candidate_slots = candidate_slots
