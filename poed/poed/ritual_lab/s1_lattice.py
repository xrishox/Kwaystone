"""S1: rebuilt lattice-first system (the current concept, from scratch).

No chrome anchor: the strongest grid-periodic region anywhere in the frame is
found by coarse tile scan, then the 12x10 line-window lattice is fit inside
it. The height-relative pitch prior (0.038-0.060 of frame height) is the only
structural guard against other grids — the FP suite measures whether that is
enough without S2's plaque gate (the inventory grid pitch sits below the
band).
"""

from __future__ import annotations

import time

import numpy as np

from . import cover, estimate, identify, occupancy
from .s2_chrome import PITCH_HEIGHT_BOUNDS
from .stages import RitualScanOutput, PanelHypothesis

GRID_QUALITY_MIN = 0.30


class LatticeSystem:
    id = "s1"

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
        lattice, stats = estimate.grid_from_roi(
            gray[y0:y1, x0:x1],
            x0,
            y0,
            (x1 - x0) / 2.0,
            min_pitch=pitch_lo,
            max_pitch=pitch_hi,
        )
        timings["lattice"] = round((time.perf_counter() - started) * 1000.0, 2)
        lattice_quality = float(stats.get("score_x", 0)) + float(stats.get("score_y", 0))
        if lattice is None or lattice_quality < GRID_QUALITY_MIN:
            return RitualScanOutput(
                fired=False,
                evidence=[f"weak-lattice q={lattice_quality:.2f}"],
                timings_ms=timings,
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
            evidence=(f"periodic-region q={quality:.2f}", f"lattice q={lattice_quality:.2f}"),
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
