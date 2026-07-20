"""S2: the chrome-anchored system — a thin wrapper over poed.ritual_scan.

This composition won the lab (0/112 FP fires, corpus 4/4 L3) and graduated
into the production package; the wrapper remains so the lab can keep scoring
it against the other candidates.
"""

from __future__ import annotations

import time

import numpy as np

from poed import ritual_scan

from .stages_output import RitualScanOutput


class ChromeSystem:
    id = "s2"

    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        timings: dict[str, float] = {}
        started = time.perf_counter()
        panel, lattice, notes = ritual_scan.locate(frame)
        timings["panel"] = round((time.perf_counter() - started) * 1000.0, 2)
        if panel is None or lattice is None:
            return RitualScanOutput(fired=False, evidence=notes, timings_ms=timings)

        started = time.perf_counter()
        footprints, matches, occ = ritual_scan.extract(frame, lattice, rows)
        timings["extract"] = round((time.perf_counter() - started) * 1000.0, 2)
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
