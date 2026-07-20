"""S4: retrieval + geometric voting.

Identity-first localization: art blobs propose candidate item positions, but
only blobs that RETRIEVE against the icon index (quick masked ZNCC over the
descriptor short-list) may vote for the lattice phase; the densest 12x10
window of identified voters becomes the grid. Scenery, chrome, and foreign UI
cannot vote because they match no known icon. (A raw descriptor-tile sweep
was measured useless: matched cells and world tiles both score ~0.3.)
"""

from __future__ import annotations

import time

import numpy as np

from poed import ritual_scan
from poed.ritual_scan.panel import PITCH_HEIGHT_BOUNDS
from poed.ritual_scan.stages import Lattice, PanelHypothesis

from .s3_items import _art_blobs
from .stages_output import RitualScanOutput

VOTE_SCORE = 0.30
MIN_VOTES = 4
MAX_BLOBS = 150


class VotingSystem:
    id = "s4"


    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        timings: dict[str, float] = {}
        height, width = frame.shape[:2]
        pitch_lo = height * PITCH_HEIGHT_BOUNDS[0]
        pitch_hi = height * PITCH_HEIGHT_BOUNDS[1]
        pitch = (pitch_lo + pitch_hi) / 2.0
        identifier = ritual_scan.identifier(rows)
        if not identifier.groups.get((1, 1)):
            return RitualScanOutput(fired=False, evidence=["no-1x1-index"])

        started = time.perf_counter()
        blobs = _art_blobs(frame, pitch)[:MAX_BLOBS]
        votes = []
        pad = int(round(pitch * 0.30))
        for x, y, w, h in blobs:
            cx, cy = x + w // 2, y + h // 2
            half = int(round(pitch / 2)) + pad
            window = frame[
                max(0, cy - half):min(height, cy + half),
                max(0, cx - half):min(width, cx + half),
            ]
            if window.size == 0:
                continue
            score = identifier.quick_footprint_score(window, (1, 1), pitch)
            if score >= VOTE_SCORE:
                votes.append((cx - pitch / 2, cy - pitch / 2))
        timings["voting"] = round((time.perf_counter() - started) * 1000.0, 2)
        if len(votes) < MIN_VOTES:
            return RitualScanOutput(
                fired=False, evidence=[f"votes={len(votes)}"], timings_ms=timings
            )

        started = time.perf_counter()
        xs = np.array([v[0] for v in votes], dtype=np.float64)
        ys = np.array([v[1] for v in votes], dtype=np.float64)
        best = None
        for candidate in np.linspace(pitch_lo, pitch_hi, 28):
            bins = max(6, int(round(candidate / 4)))
            hx = np.zeros(bins)
            np.add.at(hx, np.floor((xs % candidate) / candidate * bins).astype(int) % bins, 1.0)
            hy = np.zeros(bins)
            np.add.at(hy, np.floor((ys % candidate) / candidate * bins).astype(int) % bins, 1.0)
            score = float(hx.max() + hy.max()) / max(1, len(votes))
            if best is None or score > best[0]:
                phase_x = (int(np.argmax(hx)) + 0.5) / bins * candidate
                phase_y = (int(np.argmax(hy)) + 0.5) / bins * candidate
                best = (score, candidate, phase_x, phase_y)
        _, vote_pitch, phase_x, phase_y = best
        margin = vote_pitch * 0.05
        phase_x = (phase_x - margin) % vote_pitch
        phase_y = (phase_y - margin) % vote_pitch
        cols_total = int((width - phase_x) // vote_pitch)
        rows_total = int((height - phase_y) // vote_pitch)
        if cols_total < 4 or rows_total < 4:
            return RitualScanOutput(
                fired=False, evidence=["degenerate-vote-lattice"], timings_ms=timings
            )
        counts = np.zeros((rows_total, cols_total))
        for x, y in votes:
            col = int((x + vote_pitch / 2 - phase_x) // vote_pitch)
            row = int((y + vote_pitch / 2 - phase_y) // vote_pitch)
            if 0 <= col < cols_total and 0 <= row < rows_total:
                counts[row, col] += 1
        window_cols = min(12, cols_total)
        window_rows = min(10, rows_total)
        integral = counts.cumsum(axis=0).cumsum(axis=1)
        best_window = None
        for r0 in range(rows_total - window_rows + 1):
            for c0 in range(cols_total - window_cols + 1):
                r1, c1 = r0 + window_rows - 1, c0 + window_cols - 1
                total = integral[r1, c1]
                if r0 > 0:
                    total -= integral[r0 - 1, c1]
                if c0 > 0:
                    total -= integral[r1, c0 - 1]
                if r0 > 0 and c0 > 0:
                    total += integral[r0 - 1, c0 - 1]
                if best_window is None or total > best_window[0]:
                    best_window = (total, r0, c0)
        timings["consensus"] = round((time.perf_counter() - started) * 1000.0, 2)
        if best_window is None or best_window[0] < MIN_VOTES:
            return RitualScanOutput(
                fired=False, evidence=["no-vote-window"], timings_ms=timings
            )
        _, r0, c0 = best_window
        lattice = Lattice(
            x0=phase_x + c0 * vote_pitch,
            y0=phase_y + r0 * vote_pitch,
            pitch_x=vote_pitch,
            pitch_y=vote_pitch,
            cols=window_cols,
            rows=window_rows,
        )

        started = time.perf_counter()
        footprints, matches, occ = ritual_scan.extract(frame, lattice, rows)
        timings["extract"] = round((time.perf_counter() - started) * 1000.0, 2)
        panel = PanelHypothesis(
            rect=lattice.frame_rect(),
            confidence=1.0,
            evidence=(
                f"votes={len(votes)} window={best_window[0]:.0f}",
                f"pitch={vote_pitch:.1f}",
            ),
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
