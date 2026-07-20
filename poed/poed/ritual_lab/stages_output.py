"""Lab-side result container and system protocol.

Production types (PanelHypothesis, Lattice, OccupancyMap, Footprint) live in
poed.ritual_scan.stages; this module only defines what candidate systems
return to the lab scorer.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import numpy as np

from poed.ritual_scan.stages import Footprint, Lattice, OccupancyMap, PanelHypothesis


@dataclass
class RitualScanOutput:
    fired: bool
    matches: list[dict] = field(default_factory=list)
    panel: PanelHypothesis | None = None
    lattice: Lattice | None = None
    occupancy: OccupancyMap | None = None
    footprints: list[Footprint] = field(default_factory=list)
    evidence: list[str] = field(default_factory=list)
    timings_ms: dict[str, float] = field(default_factory=dict)
    extra: dict[str, Any] = field(default_factory=dict)


class RitualSystem(Protocol):
    id: str

    def analyze(self, frame: np.ndarray, rows: dict) -> RitualScanOutput:
        ...
