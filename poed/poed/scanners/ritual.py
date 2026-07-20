from __future__ import annotations

import logging

from poed import ritual_scan, uniquescan

from .common import (
    finalize_matches,
    update_debug_manifest,
    write_debug_image,
)
from .types import Detection, ScanContext, ScanResult

_LOG = logging.getLogger("waystone.scanners.ritual")


class RitualScanner:
    id = "ritual"
    title = "ritual rewards"
    priority = 10.0

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        panel, lattice, notes = ritual_scan.locate(ctx.frame)
        if panel is None or lattice is None:
            update_debug_manifest(
                ctx.debug_dir,
                ritual_probe={"accepted": False, "notes": notes[:6]},
            )
            return None
        rect = panel.rect.clipped(ctx.frame.shape[1], ctx.frame.shape[0])
        if rect is None:
            update_debug_manifest(ctx.debug_dir, ritual_probe="degenerate-panel")
            return None
        if ctx.debug_dir:
            crop = ctx.frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
            write_debug_image(ctx.debug_dir / "10-ritual-probe-crop.jpg", crop.copy())
        update_debug_manifest(
            ctx.debug_dir,
            ritual_probe={
                "accepted": True,
                "evidence": list(panel.evidence),
                "pitch": round(lattice.pitch_x, 2),
                "cols": lattice.cols,
                "rows": lattice.rows,
            },
        )
        return Detection(
            self.id,
            panel.confidence,
            {"lattice": lattice},
            region=rect,
            evidence=panel.evidence,
        )

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        rows = uniquescan.filter_rows(
            ctx.rows, ctx.cfg.get("unique_scan_min_price", 0.0)
        )
        rows = uniquescan.filter_ritual_rows(rows)
        _footprints, matches, _occupancy = ritual_scan.extract(
            ctx.frame, detection.payload["lattice"], rows
        )
        matches = finalize_matches(
            ctx,
            matches,
            self.id,
            stage="19-ritual-result.jpg",
            title=f"ritual result: matches={len(matches)}",
        )
        _LOG.info("ritual scan matches=%d", len(matches))
        return ScanResult(self.id, self.title, matches)

    def warm(self, brain, cfg: dict) -> None:
        uniquescan.warm(brain, cfg, row_filter=uniquescan.filter_ritual_rows)

    def stop(self) -> None:
        return
