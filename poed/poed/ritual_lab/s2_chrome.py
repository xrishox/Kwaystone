"""S2: chrome-anchored top-down localization of the Favours panel.

The panel is found by its own chrome: a gold text band (FAVOURS title or
TRIBUTE counter) verified by a recognition-only OCR read, then the grid is
localized from bounded gridline periodicity below the band. The grid
background is translucent in-game, so darkness is never used as evidence —
only the periodic gridlines, which survive any scene behind the panel. Grids
without Favours chrome (inventory, merchant) are structurally rejected.
"""

from __future__ import annotations

import re
import time

import cv2
import numpy as np

from poed.image_geometry import Rect

from . import cover, estimate, identify, occupancy
from .stages import Lattice, PanelHypothesis, RitualScanOutput

CHROME_ANALYSIS_WIDTH = 1600
GOLD_LOW = (10, 25, 95)
GOLD_HIGH = (45, 255, 255)
BAND_MIN_ASPECT = 2.2
TITLE_PATTERN = re.compile(r"TRIBUTE|FAVOURS|RITUAL")


def _gold_bands(image: np.ndarray) -> list[Rect]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, GOLD_LOW, GOLD_HIGH)
    height, width = image.shape[:2]
    kernel_w = max(9, int(width * 0.012))
    gold = cv2.morphologyEx(
        gold, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3))
    )
    contours, _ = cv2.findContours(gold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bands = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if h <= 0 or w / h < BAND_MIN_ASPECT:
            continue
        if not (height * 0.006 <= h <= height * 0.05):
            continue
        if not (width * 0.02 <= w <= width * 0.25):
            continue
        density = cv2.countNonZero(gold[y:y + h, x:x + w]) / float(w * h)
        if density < 0.25:
            continue
        bands.append(Rect(x, y, w, h))
    bands.sort(key=lambda rect: rect.w, reverse=True)
    return bands


def _read_band_text(frame: np.ndarray, band: Rect) -> tuple[str | None, str]:
    """Recognition-only OCR of the band strip; None text means OCR unavailable."""
    from poed import ocr_worker

    pad = max(3, band.h // 4)
    strip = frame[
        max(0, band.y - pad):min(frame.shape[0], band.y + band.h + pad),
        max(0, band.x - pad):min(frame.shape[1], band.x + band.w + pad),
    ]
    if strip.size == 0:
        return None, ""
    try:
        reads = ocr_worker.recognize_arrays([strip])
    except ocr_worker.OcrUnavailable as e:
        return None, f"ocr-unavailable: {e}"
    text = str(reads[0].get("text") or "") if reads else ""
    return re.sub(r"[^A-Z]+", "", text.upper()), text


# The game UI scales with frame height, so the cell pitch is a fixed fraction
# of it (measured 105/2160 = 0.0486 at 4K). A tight prior structurally
# excludes the half-pitch octave error regardless of which chrome band
# anchored the panel.
PITCH_HEIGHT_BOUNDS = (0.038, 0.060)
GRID_QUALITY_MIN = 0.22
GRID_QUALITY_DECISIVE = 0.60


def _grid_below_band(frame: np.ndarray, band: Rect) -> tuple[Lattice | None, dict]:
    height, width = frame.shape[:2]
    pitch_lo = height * PITCH_HEIGHT_BOUNDS[0]
    pitch_hi = height * PITCH_HEIGHT_BOUNDS[1]
    cx = band.x + band.w / 2.0
    span = pitch_hi * 8.0
    x0 = max(0, int(cx - span))
    x1 = min(width, int(cx + span))
    y0 = min(height - 1, band.y + band.h)
    y1 = min(height, int(y0 + pitch_hi * 12.5))
    gray = estimate.to_gray(frame[y0:y1, x0:x1])
    return estimate.grid_from_roi(
        gray,
        x0,
        y0,
        cx - x0,
        min_pitch=pitch_lo,
        max_pitch=pitch_hi,
    )


def locate_panel(
    frame: np.ndarray,
) -> tuple[PanelHypothesis | None, Lattice | None, list[str]]:
    height, width = frame.shape[:2]
    scale = min(1.0, CHROME_ANALYSIS_WIDTH / max(1, width))
    small = (
        cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else frame
    )
    notes = []
    best: tuple[float, PanelHypothesis, Lattice] | None = None
    for band in _gold_bands(small)[:12]:
        band_frame = Rect(
            int(band.x / scale), int(band.y / scale),
            int(band.w / scale), int(band.h / scale),
        )
        # Geometry gate first: OCR only runs on bands that actually have a
        # qualifying grid below them, so negative frames cost zero OCR calls.
        lattice, stats = _grid_below_band(frame, band_frame)
        if lattice is None:
            notes.append(f"no-grid-below-band ({stats})")
            continue
        quality = float(stats.get("score_x", 0.0)) + float(stats.get("score_y", 0.0))
        if quality < GRID_QUALITY_MIN:
            notes.append(f"weak-grid-below-band (quality={quality:.2f})")
            continue
        normalized, raw = _read_band_text(frame, band_frame)
        ocr_ok = normalized is not None
        if ocr_ok and not TITLE_PATTERN.search(normalized):
            notes.append(f"band-rejected: {raw!r}")
            continue
        evidence = (
            "plaque+gridlines",
            f"title={raw!r}" if ocr_ok else "ocr-unavailable",
            f"pitch={lattice.pitch_x:.1f} cols={lattice.cols} rows={lattice.rows}",
            f"quality={quality:.2f}",
        )
        panel = PanelHypothesis(
            rect=lattice.frame_rect(),
            plaque_rect=band_frame,
            confidence=1.0,
            evidence=evidence,
        )
        if best is None or quality > best[0]:
            best = (quality, panel, lattice)
        if quality >= GRID_QUALITY_DECISIVE:
            break
    if best is not None:
        return best[1], best[2], notes
    return None, None, notes


class ChromeSystem:
    id = "s2"

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
        started = time.perf_counter()
        panel, lattice, notes = locate_panel(frame)
        timings["panel"] = round((time.perf_counter() - started) * 1000.0, 2)
        if panel is None or lattice is None:
            return RitualScanOutput(fired=False, evidence=notes, timings_ms=timings)

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

        return RitualScanOutput(
            fired=True,
            matches=matches,
            panel=panel,
            lattice=lattice,
            occupancy=occ,
            footprints=footprints,
            evidence=[*panel.evidence, *notes],
            timings_ms=timings,
        )
