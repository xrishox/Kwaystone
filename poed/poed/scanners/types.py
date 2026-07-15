from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

import numpy as np

from poed.image_geometry import Rect


@dataclass(frozen=True)
class Detection:
    scanner_id: str
    confidence: float
    payload: dict[str, Any] = field(default_factory=dict)
    region: Rect | None = None
    evidence: tuple[str, ...] = ()


@dataclass(frozen=True)
class ScanContext:
    cfg: dict
    output: str
    shot: np.ndarray
    frame: np.ndarray
    frame_x: int
    frame_y: int
    source: str
    rows: dict
    game_rect: Rect | None = None
    debug_dir: Path | None = None


@dataclass(frozen=True)
class ScanResult:
    scanner_id: str
    title: str
    matches: list[dict]


class Scanner(Protocol):
    id: str
    title: str

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        ...

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        ...

    def warm(self, brain, cfg: dict) -> None:
        ...

    def stop(self) -> None:
        ...
