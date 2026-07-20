"""Ritual (Favours) reward-grid scanning domain package.

Pipeline: chrome-anchored panel localization (gold band verified by
recognition-only OCR + quality-ranked gridline lattice), per-cell feature
occupancy with expansion candidates, identification-driven footprint cover,
and masked-ZNCC identification against the market icon corpus. Selected as
the winner of the ritual-rewrite lab (see docs/ritual-scanner.md): 0/112
false fires on the negative suite, full corpus at verification Level 3.

This package must not import poed.scanners or poed.desktop internals.
"""

from __future__ import annotations

import numpy as np

from . import cover, identify, occupancy
from .panel import locate_panel
from .stages import Footprint, Lattice, OccupancyMap, PanelHypothesis

__all__ = [
    "Footprint",
    "Lattice",
    "OccupancyMap",
    "PanelHypothesis",
    "extract",
    "locate",
    "identifier",
    "locate_panel",
    "warm_index",
]

_identifier_cache: tuple[int, dict, identify.Identifier] | None = None


def identifier(rows: dict) -> identify.Identifier:
    global _identifier_cache
    cached = _identifier_cache
    if cached is not None and cached[1] is rows:
        return cached[2]
    built = identify.Identifier(rows)
    _identifier_cache = (id(rows), rows, built)
    return built


def locate(
    frame: np.ndarray,
) -> tuple[PanelHypothesis | None, Lattice | None, list[str]]:
    return locate_panel(frame)


def extract(
    frame: np.ndarray,
    lattice: Lattice,
    rows: dict,
) -> tuple[list[Footprint], list[dict], OccupancyMap]:
    """Find, partition, and identify the rewards on a located lattice.

    Matches are raw (frame coordinates, un-normalized); the scanner layer owns
    coordinate finalization and normalization."""
    occ = occupancy.feature_occupancy(frame, lattice)
    legal = occupancy.legal_footprints(rows)
    candidates = occupancy.expansion_candidates(
        occupancy.cell_stack(frame, lattice), occ.occupied
    )
    footprints, matches = cover.refined_cover(
        frame, lattice, occ.occupied, legal, identifier(rows), candidates=candidates
    )
    return footprints, matches, occ


def warm_index(rows: dict) -> None:
    identifier(rows)._half_prefilter_matrix()
