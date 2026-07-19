"""S3: item-first bottom-up system.

No gridline or chrome evidence at all: saturated opaque art blobs are
detected across the frame, the lattice phase/pitch is voted by folding blob
center coordinates, and the 12x10 window holding the most blob mass becomes
the grid. Occupancy is blob coverage; partition and identity come from the
shared identification cover.
"""

from __future__ import annotations

import time

import cv2
import numpy as np

from . import cover, identify, occupancy
from .s2_chrome import PITCH_HEIGHT_BOUNDS
from .stages import Lattice, PanelHypothesis, RitualScanOutput

MIN_BLOBS = 4


def _art_blobs(frame: np.ndarray, pitch_mid: float) -> list[tuple[int, int, int, int]]:
    hsv = cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    sat_thr = max(70.0, float(cv2.threshold(sat, 0, 255, cv2.THRESH_OTSU)[0]))
    mask = ((sat > sat_thr) & (val > 40)).astype(np.uint8)
    kernel = max(3, int(round(pitch_mid * 0.04)))
    mask = cv2.morphologyEx(
        mask, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel, kernel))
    )
    count, labels, stats, _ = cv2.connectedComponentsWithStats(mask, connectivity=8)
    blobs = []
    lo = pitch_mid * 0.35
    hi = pitch_mid * 4.6
    for label in range(1, count):
        x, y, w, h, area = stats[label]
        if not (lo <= w <= hi and lo <= h <= hi):
            continue
        if area < (pitch_mid * 0.30) ** 2:
            continue
        blobs.append((int(x), int(y), int(w), int(h)))
    return blobs


def _fold_vote(values: np.ndarray, pitch: float) -> tuple[float, float]:
    bins = max(6, int(round(pitch / 4)))
    hist = np.zeros(bins)
    idx = np.floor((values % pitch) / pitch * bins).astype(int) % bins
    np.add.at(hist, idx, 1.0)
    smooth = hist + np.roll(hist, 1) + np.roll(hist, -1)
    best = int(np.argmax(smooth))
    return (best + 0.5) / bins * pitch, float(smooth[best]) / max(1, len(values))


class ItemFirstSystem:
    id = "s3"

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
        height, width = frame.shape[:2]
        pitch_lo = height * PITCH_HEIGHT_BOUNDS[0]
        pitch_hi = height * PITCH_HEIGHT_BOUNDS[1]

        started = time.perf_counter()
        blobs = _art_blobs(frame, (pitch_lo + pitch_hi) / 2.0)
        timings["blobs"] = round((time.perf_counter() - started) * 1000.0, 2)
        if len(blobs) < MIN_BLOBS:
            return RitualScanOutput(
                fired=False, evidence=[f"too-few-blobs {len(blobs)}"], timings_ms=timings
            )

        started = time.perf_counter()
        lefts = np.array([b[0] for b in blobs], dtype=np.float64)
        tops = np.array([b[1] for b in blobs], dtype=np.float64)
        best = None
        for pitch in np.linspace(pitch_lo, pitch_hi, 28):
            phase_x, conf_x = _fold_vote(lefts, pitch)
            phase_y, conf_y = _fold_vote(tops, pitch)
            confidence = conf_x + conf_y
            if best is None or confidence > best[0]:
                best = (confidence, pitch, phase_x, phase_y)
        _, pitch, phase_x, phase_y = best
        margin = pitch * 0.09
        phase_x = (phase_x - margin) % pitch
        phase_y = (phase_y - margin) % pitch

        cols_total = int((width - phase_x) // pitch)
        rows_total = int((height - phase_y) // pitch)
        if cols_total < 4 or rows_total < 4:
            return RitualScanOutput(
                fired=False, evidence=["degenerate-lattice"], timings_ms=timings
            )
        counts = np.zeros((rows_total, cols_total))
        for x, y, w, h in blobs:
            col = int((x + w / 2 - phase_x) // pitch)
            row = int((y + h / 2 - phase_y) // pitch)
            if 0 <= col < cols_total and 0 <= row < rows_total:
                counts[row, col] += 1
        window_cols = min(12, cols_total)
        window_rows = min(10, rows_total)
        best_window = None
        integral = counts.cumsum(axis=0).cumsum(axis=1)

        def window_sum(r0, c0):
            r1, c1 = r0 + window_rows - 1, c0 + window_cols - 1
            total = integral[r1, c1]
            if r0 > 0:
                total -= integral[r0 - 1, c1]
            if c0 > 0:
                total -= integral[r1, c0 - 1]
            if r0 > 0 and c0 > 0:
                total += integral[r0 - 1, c0 - 1]
            return total

        for r0 in range(rows_total - window_rows + 1):
            for c0 in range(cols_total - window_cols + 1):
                total = window_sum(r0, c0)
                if best_window is None or total > best_window[0]:
                    best_window = (total, r0, c0)
        timings["lattice"] = round((time.perf_counter() - started) * 1000.0, 2)
        if best_window is None or best_window[0] < MIN_BLOBS:
            return RitualScanOutput(
                fired=False, evidence=["no-blob-window"], timings_ms=timings
            )
        _, r0, c0 = best_window
        lattice = Lattice(
            x0=phase_x + c0 * pitch,
            y0=phase_y + r0 * pitch,
            pitch_x=pitch,
            pitch_y=pitch,
            cols=window_cols,
            rows=window_rows,
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
            evidence=(f"blob-window mass={best_window[0]:.0f}", f"pitch={pitch:.1f}"),
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
