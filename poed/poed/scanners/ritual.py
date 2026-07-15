from __future__ import annotations

import logging

from poed import uniquescan

from .common import (
    update_debug_manifest,
    write_debug_image,
)
from .types import Detection, ScanContext, ScanResult
from .unique_grid import scan_unique_grid

_LOG = logging.getLogger("waystone.scanners.ritual")


class RitualScanner:
    id = "ritual"
    title = "ritual rewards"
    priority = 10.0

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        # A visible Have panel can coexist with inventory grids. Its repeated
        # cards are more specific than a generic square lattice.
        if scene.have is not None:
            update_debug_manifest(ctx.debug_dir, ritual_probe="have-layout-present")
            return None
        layout = scene.ritual
        if layout is None:
            update_debug_manifest(ctx.debug_dir, ritual_probe="no-grid-layout")
            return None
        rect = layout.region
        crop = ctx.frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
        x0, y0 = rect.x, rect.y
        if ctx.debug_dir:
            write_debug_image(ctx.debug_dir / "10-ritual-probe-crop.jpg", crop)
        update_debug_manifest(
            ctx.debug_dir,
            ritual_probe={
                "accepted": True,
                "layout_score": layout.score,
                "cell": layout.cell,
                "evidence": list(layout.details),
            },
        )
        return Detection(
            self.id,
            layout.confidence,
            {
                "crop": crop,
                "x0": x0,
                "y0": y0,
                "cell": layout.cell,
            },
            region=rect,
            evidence=layout.details,
        )

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        result = scan_unique_grid(
            ctx,
            detection,
            scanner_id=self.id,
            title=self.title,
            include_unknown=True,
            stage_name="19-ritual-result.jpg",
            stage_label="ritual result",
            row_filter=uniquescan.filter_ritual_rows,
        )
        _LOG.info("ritual scan matches=%d", len(result.matches))
        return result

    def warm(self, brain, cfg: dict) -> None:
        uniquescan.warm(brain, cfg, row_filter=uniquescan.filter_ritual_rows)

    def stop(self) -> None:
        return
