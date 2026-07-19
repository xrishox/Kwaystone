"""s0: the current production ritual pipeline, wrapped for reference scores."""

from __future__ import annotations

import time

import numpy as np

from poed.scanners.ritual import RitualScanner
from poed.scanners.scene import SceneAnalysis
from poed.scanners.types import ScanContext

from .stages import PanelHypothesis, RitualScanOutput


class BaselineSystem:
    id = "s0"

    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        ctx = ScanContext(
            cfg={"unique_scan_min_price": 0.0},
            output="ritual-lab",
            shot=frame,
            frame=frame,
            frame_x=0,
            frame_y=0,
            source="output",
            rows=rows,
        )
        scanner = RitualScanner()
        scene = SceneAnalysis(frame)
        timings: dict[str, float] = {}
        started = time.perf_counter()
        detection = scanner.probe(ctx, scene)
        timings["probe"] = round((time.perf_counter() - started) * 1000.0, 2)
        if detection is None:
            return RitualScanOutput(fired=False, timings_ms=timings)
        started = time.perf_counter()
        result = scanner.scan(ctx, detection)
        timings["extract"] = round((time.perf_counter() - started) * 1000.0, 2)
        panel = None
        if detection.region is not None:
            panel = PanelHypothesis(
                rect=detection.region,
                confidence=detection.confidence,
                evidence=tuple(detection.evidence),
            )
        return RitualScanOutput(
            fired=True,
            matches=list(result.matches),
            panel=panel,
            evidence=list(detection.evidence),
            timings_ms=timings,
        )
