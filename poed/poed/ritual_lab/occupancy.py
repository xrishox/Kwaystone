"""Shared occupancy + footprint stages.

The Favours grid background is translucent, so any absolute or
background-model threshold breaks when a bright scene shows through empty
cells. Item content is different in kind: icon art is opaque, saturated, and
edge-dense, and the item footprint carries the game's navy tint. Occupancy
therefore combines per-cell features (saturation, edge energy, navy-tint
fraction) robust-z-normalized across the grid's own cells, split by 1D
2-means with a separation gate so all-empty and all-occupied panels do not
get hallucinated splits.
"""

from __future__ import annotations

import cv2
import numpy as np

from .stages import Footprint, Lattice, OccupancyMap

CELL_SIDE = 48
EMPTY_FLOOR_QUANTILE = 0.30
RESIDUAL_RATIO = 2.1
RESIDUAL_MIN = 5.0
# Deep inset: reward art overflows up to ~20% into neighbouring cells and must
# not inflate their occupancy features.
CELL_INSET = 0.22
NAVY_HUE = (100, 140)
SEPARATION_MIN = 1.35


def cell_stack(frame: np.ndarray, lattice: Lattice, side: int = CELL_SIDE) -> np.ndarray:
    cells = np.empty((lattice.rows, lattice.cols, side, side, 3), dtype=np.float32)
    for row in range(lattice.rows):
        for col in range(lattice.cols):
            rect = lattice.cell_rect(col, row)
            crop = frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
            if crop.size == 0:
                cells[row, col] = 0.0
                continue
            cells[row, col] = cv2.resize(
                crop, (side, side), interpolation=cv2.INTER_AREA
            ).astype(np.float32)
    return cells


def modal_residual_occupancy(frame: np.ndarray, lattice: Lattice) -> OccupancyMap:
    cells = cell_stack(frame, lattice)
    flat = cells.reshape(-1, *cells.shape[2:])
    modal = np.median(flat, axis=0)
    residual = np.abs(cells - modal[None, None]).mean(axis=(2, 3, 4))
    floor = float(np.quantile(residual, EMPTY_FLOOR_QUANTILE))
    threshold = max(RESIDUAL_MIN, floor * RESIDUAL_RATIO)
    occupied = residual > threshold
    return OccupancyMap(energy=residual, occupied=occupied)


def _robust_z(values: np.ndarray) -> np.ndarray:
    median = float(np.median(values))
    mad = float(np.median(np.abs(values - median))) * 1.4826
    scale = max(mad, float(values.std()) * 0.3, 1e-3)
    return (values - median) / scale


def _fill_holes(occupied: np.ndarray, passes: int = 2) -> np.ndarray:
    """Dark item art can leave interior holes; a cell surrounded on 3+ sides
    by occupied cells is part of an item."""
    out = occupied.copy()
    for _ in range(passes):
        padded = np.pad(out, 1, constant_values=False)
        neighbors = (
            padded[:-2, 1:-1].astype(int)
            + padded[2:, 1:-1].astype(int)
            + padded[1:-1, :-2].astype(int)
            + padded[1:-1, 2:].astype(int)
        )
        grown = out | (~out & (neighbors >= 3))
        if np.array_equal(grown, out):
            break
        out = grown
    return out


def cell_features(frame: np.ndarray, lattice: Lattice) -> dict[str, np.ndarray]:
    hsv = None
    grad = None
    sat = np.zeros((lattice.rows, lattice.cols), dtype=np.float32)
    edge = np.zeros_like(sat)
    navy = np.zeros_like(sat)
    bounds = lattice.frame_rect()
    region = frame[
        max(0, bounds.y):bounds.y + bounds.h,
        max(0, bounds.x):bounds.x + bounds.w,
    ]
    if region.size == 0:
        return {"sat": sat, "edge": edge, "navy": navy}
    hsv = cv2.cvtColor(region, cv2.COLOR_BGR2HSV)
    gray = cv2.cvtColor(region, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad = cv2.magnitude(
        cv2.Scharr(gray, cv2.CV_32F, 1, 0), cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    )
    ox, oy = max(0, bounds.x), max(0, bounds.y)
    inset_x = int(round(lattice.pitch_x * CELL_INSET))
    inset_y = int(round(lattice.pitch_y * CELL_INSET))
    for row in range(lattice.rows):
        for col in range(lattice.cols):
            rect = lattice.cell_rect(col, row)
            x0 = rect.x - ox + inset_x
            y0 = rect.y - oy + inset_y
            x1 = rect.x - ox + rect.w - inset_x
            y1 = rect.y - oy + rect.h - inset_y
            x0, y0 = max(0, x0), max(0, y0)
            x1 = min(hsv.shape[1], x1)
            y1 = min(hsv.shape[0], y1)
            if x1 <= x0 or y1 <= y0:
                continue
            cell_hsv = hsv[y0:y1, x0:x1]
            sat[row, col] = float(cell_hsv[:, :, 1].mean())
            edge[row, col] = float(grad[y0:y1, x0:x1].mean())
            hue = cell_hsv[:, :, 0]
            navy_mask = (
                (hue >= NAVY_HUE[0])
                & (hue <= NAVY_HUE[1])
                & (cell_hsv[:, :, 1] >= 60)
                & (cell_hsv[:, :, 2] >= 25)
            )
            navy[row, col] = float(navy_mask.mean())
    return {"sat": sat, "edge": edge, "navy": navy}


def _two_means_high(flat: np.ndarray) -> np.ndarray | None:
    """1D 2-means; returns the high-cluster membership or None when the
    distribution is not separably bimodal."""
    if len(flat) < 4:
        return None
    centers = np.array([float(flat.min()), float(flat.max())])
    for _ in range(12):
        assignment = np.abs(flat[:, None] - centers[None, :]).argmin(axis=1)
        for index in range(2):
            members = flat[assignment == index]
            if len(members):
                centers[index] = members.mean()
    assignment = np.abs(flat[:, None] - centers[None, :]).argmin(axis=1)
    spread = float(flat.std()) or 1.0
    separation = abs(centers[1] - centers[0]) / spread
    high_cluster = int(np.argmax(centers))
    fraction = float((assignment == high_cluster).mean())
    if separation < SEPARATION_MIN or not (0.0 < fraction < 0.95):
        return None
    return assignment == high_cluster


def expansion_candidates(cells: np.ndarray, core: np.ndarray) -> np.ndarray:
    """Cells that MIGHT be backdrop-tinted footprint cells without art (the
    corners of sparse diagonal items): they decorrelate from the modal empty
    ornament texture and touch the art core. They are only hypotheses — the
    identification cover decides whether an item actually spans them."""
    rows, cols = core.shape
    non_core = ~core
    if core.all() or not core.any() or non_core.sum() < 4:
        return np.zeros_like(core)
    modal_empty = np.median(cells[non_core], axis=0)
    modal_vec = modal_empty.reshape(-1) - float(modal_empty.mean())
    modal_norm = float(np.linalg.norm(modal_vec))
    if modal_norm <= 1e-6:
        return np.zeros_like(core)
    corr = np.ones((rows, cols), dtype=np.float64)
    for row in range(rows):
        for col in range(cols):
            if core[row, col]:
                continue
            vec = cells[row, col].reshape(-1).astype(np.float64)
            vec -= vec.mean()
            norm = float(np.linalg.norm(vec))
            corr[row, col] = (
                float(np.dot(vec, modal_vec) / (norm * modal_norm)) if norm > 1e-6 else 1.0
            )
    split = _two_means_high(-corr[non_core].reshape(-1))
    if split is None:
        return np.zeros_like(core)
    candidate_map = np.zeros((rows, cols), dtype=bool)
    candidate_map[non_core] = split
    reachable = np.zeros_like(core)
    frontier = core.copy()
    for _ in range(3):
        padded = np.pad(frontier | reachable, 1, constant_values=False)
        neighbor = (
            padded[:-2, 1:-1] | padded[2:, 1:-1] | padded[1:-1, :-2] | padded[1:-1, 2:]
        )
        grown = candidate_map & neighbor & ~reachable
        if not grown.any():
            break
        reachable |= grown
    return reachable


def feature_occupancy(frame: np.ndarray, lattice: Lattice) -> OccupancyMap:
    features = cell_features(frame, lattice)
    combined = (
        0.35 * _robust_z(features["sat"])
        + 0.35 * _robust_z(np.log1p(features["edge"]))
        + 0.30 * _robust_z(features["navy"])
    )
    occupied = np.zeros_like(combined, dtype=bool)
    high = _two_means_high(combined.reshape(-1).astype(np.float64))
    if high is not None:
        occupied = _fill_holes(high.reshape(combined.shape))
    return OccupancyMap(energy=combined, occupied=occupied)


def legal_footprints(rows: dict) -> set[tuple[int, int]]:
    legal = {(1, 1)}
    for row in rows.values():
        w = int(row.get("w") or 1)
        h = int(row.get("h") or 1)
        if 1 <= w <= 2 and 1 <= h <= 4:
            legal.add((w, h))
    return legal


def _components(occupied: np.ndarray) -> list[list[tuple[int, int]]]:
    count, labels = cv2.connectedComponents(occupied.astype(np.uint8), connectivity=4)
    components = []
    for label in range(1, count):
        ys, xs = np.nonzero(labels == label)
        components.append([(int(col), int(row)) for row, col in zip(ys, xs)])
    return components


def _carve_component(
    cells: set[tuple[int, int]],
    legal: set[tuple[int, int]],
) -> list[Footprint]:
    """Partition an occupied component into legal footprints, largest first."""
    ordered = sorted(legal, key=lambda wh: (wh[0] * wh[1], wh[1]), reverse=True)
    out = []
    remaining = set(cells)
    while remaining:
        col, row = min(remaining, key=lambda cr: (cr[1], cr[0]))
        placed = False
        for w, h in ordered:
            block = {(col + dc, row + dr) for dc in range(w) for dr in range(h)}
            if block <= remaining:
                out.append(Footprint(col, row, w, h))
                remaining -= block
                placed = True
                break
        if not placed:
            out.append(Footprint(col, row, 1, 1))
            remaining.discard((col, row))
    return out


def footprints_from_occupancy(
    occupancy: OccupancyMap,
    legal: set[tuple[int, int]],
) -> list[Footprint]:
    out = []
    for component in _components(occupancy.occupied):
        cols = [c for c, _ in component]
        rows = [r for _, r in component]
        w = max(cols) - min(cols) + 1
        h = max(rows) - min(rows) + 1
        if len(component) == w * h and (w, h) in legal:
            out.append(Footprint(min(cols), min(rows), w, h))
            continue
        out.extend(_carve_component(set(component), legal))
    return sorted(out, key=lambda f: (f.row, f.col))
