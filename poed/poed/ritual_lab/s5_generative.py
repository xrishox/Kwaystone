"""S5: generative-background system.

Pure signal-processing localization: ACF pitch + fold phase over the
strongest periodic region (no line windows, no chrome), then the grid extent
is decided generatively — a cell belongs to the panel if it either matches
the modal empty-cell texture (the ornament pattern reconstructs it) or is
feature-occupied; the largest connected block of such cells, capped at 12x10,
is the grid.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from . import cover, estimate, identify, occupancy
from .s2_chrome import PITCH_HEIGHT_BOUNDS
from .stages import Lattice, PanelHypothesis, RitualScanOutput

PATTERN_CORR_MIN = 0.35
MAX_COLS = 12
MAX_ROWS = 10


def _pattern_membership(cells: np.ndarray, occupied: np.ndarray) -> np.ndarray:
    non_occ = ~occupied
    if non_occ.sum() < 4:
        return occupied.copy()
    modal = np.median(cells[non_occ], axis=0)
    modal_vec = modal.reshape(-1) - float(modal.mean())
    modal_norm = float(np.linalg.norm(modal_vec))
    member = occupied.copy()
    if modal_norm <= 1e-6:
        return member
    rows, cols = occupied.shape
    for row in range(rows):
        for col in range(cols):
            if member[row, col]:
                continue
            vec = cells[row, col].reshape(-1).astype(np.float64)
            vec -= vec.mean()
            norm = float(np.linalg.norm(vec))
            if norm <= 1e-6:
                continue
            corr = float(np.dot(vec, modal_vec) / (norm * modal_norm))
            member[row, col] = corr >= PATTERN_CORR_MIN
    return member


def _largest_block(member: np.ndarray) -> tuple[int, int, int, int] | None:
    count, labels = cv2.connectedComponents(member.astype(np.uint8), connectivity=4)
    best = None
    for label in range(1, count):
        ys, xs = np.nonzero(labels == label)
        if len(ys) < 12:
            continue
        area = len(ys)
        if best is None or area > best[0]:
            best = (area, int(xs.min()), int(ys.min()), int(xs.max()), int(ys.max()))
    if best is None:
        return None
    _, cx0, cy0, cx1, cy1 = best
    return cx0, cy0, cx1, cy1


class GenerativeSystem:
    id = "s5"

    def __init__(self) -> None:
        self._identifier: identify.Identifier | None = None
        self._identifier_rows: int | None = None

    def _get_identifier(self, rows: dict) -> identify.Identifier:
        if self._identifier is None or self._identifier_rows != id(rows):
            self._identifier = identify.Identifier(rows)
            self._identifier_rows = id(rows)
        return self._identifier

    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        timings: dict[str, float] = {}
        height = frame.shape[0]
        pitch_lo = height * PITCH_HEIGHT_BOUNDS[0]
        pitch_hi = height * PITCH_HEIGHT_BOUNDS[1]
        gray = estimate.to_gray(frame)

        started = time.perf_counter()
        region, quality = estimate.periodic_region(gray, pitch_lo, pitch_hi)
        timings["region"] = round((time.perf_counter() - started) * 1000.0, 2)
        if region is None:
            return RitualScanOutput(
                fired=False, evidence=[f"no-periodic-region q={quality:.2f}"],
                timings_ms=timings,
            )
        x0, y0, x1, y1 = region
        started = time.perf_counter()
        wide, _stats = estimate.lattice_from_region(
            gray[y0:y1, x0:x1], x0, y0, min_pitch=pitch_lo, max_pitch=pitch_hi
        )
        timings["lattice"] = round((time.perf_counter() - started) * 1000.0, 2)
        if wide is None:
            return RitualScanOutput(
                fired=False, evidence=["no-acf-lattice"], timings_ms=timings
            )

        started = time.perf_counter()
        occ_wide = occupancy.feature_occupancy(frame, wide)
        cells = occupancy.cell_stack(frame, wide)
        member = _pattern_membership(cells, occ_wide.occupied)
        block = _largest_block(member)
        timings["membership"] = round((time.perf_counter() - started) * 1000.0, 2)
        if block is None:
            return RitualScanOutput(
                fired=False, evidence=["no-member-block"], timings_ms=timings
            )
        cx0, cy0, cx1, cy1 = block
        cols = min(MAX_COLS, cx1 - cx0 + 1)
        rows_n = min(MAX_ROWS, cy1 - cy0 + 1)
        if cols < 4 or rows_n < 4:
            return RitualScanOutput(
                fired=False,
                evidence=[f"block-too-small {cols}x{rows_n}"],
                timings_ms=timings,
            )
        lattice = Lattice(
            x0=wide.x0 + cx0 * wide.pitch_x,
            y0=wide.y0 + cy0 * wide.pitch_y,
            pitch_x=wide.pitch_x,
            pitch_y=wide.pitch_y,
            cols=cols,
            rows=rows_n,
        )

        started = time.perf_counter()
        occ = occupancy.feature_occupancy(frame, lattice)
        legal = occupancy.legal_footprints(rows)
        candidates = occupancy.expansion_candidates(
            occupancy.cell_stack(frame, lattice), occ.occupied
        )
        timings["occupancy"] = round((time.perf_counter() - started) * 1000.0, 2)

        started = time.perf_counter()
        identifier = self._get_identifier(rows)
        footprints, matches = cover.refined_cover(
            frame, lattice, occ.occupied, legal, identifier, candidates=candidates
        )
        timings["cover+identify"] = round((time.perf_counter() - started) * 1000.0, 2)
        panel = PanelHypothesis(
            rect=lattice.frame_rect(),
            confidence=1.0,
            evidence=(f"generative-membership {cols}x{rows_n}",),
        )
        return RitualScanOutput(
            fired=True,
            matches=matches,
            panel=panel,
            lattice=lattice,
            occupancy=occ,
            footprints=footprints,
            evidence=list(panel.evidence),
            timings_ms=timings,
        )
