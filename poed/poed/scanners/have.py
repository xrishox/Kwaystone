from __future__ import annotations

import logging

import cv2

from poed import have_scan

from .common import (
    finalize_matches,
    update_debug_manifest,
    write_debug_image,
)
from . import ocr
from .types import Detection, ScanContext, ScanResult

_LOG = logging.getLogger("waystone.scanners.have")


class HaveScanner:
    id = "have"
    title = "have items"
    priority = 40.0

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        layout = scene.have
        if layout is None:
            update_debug_manifest(ctx.debug_dir, have_probe="no-layout")
            return None
        rect = layout.region
        crop = ctx.frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
        x0, y0 = rect.x, rect.y
        if ctx.debug_dir:
            write_debug_image(ctx.debug_dir / "20-have-probe-crop.jpg", crop)
        update_debug_manifest(
            ctx.debug_dir,
            have_probe={
                "accepted": True,
                "layout_score": layout.score,
                "evidence": list(layout.details),
            },
        )
        return Detection(
            self.id,
            layout.confidence,
            {"crop": crop, "x0": x0, "y0": y0},
            region=rect,
            evidence=layout.details,
        )

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        crop = detection.payload["crop"]
        matches, cards = have_scan.scan_have_image(
            crop, ctx.rows, debug_dir=ctx.debug_dir
        )
        if ctx.debug_dir:
            marked = crop.copy()
            for card in cards:
                color = (0, 255, 255) if card.synthesized else (0, 255, 0)
                cv2.rectangle(
                    marked,
                    (card.x, card.y),
                    (card.x + card.w, card.y + card.h),
                    color,
                    2,
                )
            write_debug_image(ctx.debug_dir / "23-have-cards.jpg", marked)
        matches = finalize_matches(
            ctx,
            matches,
            self.id,
            stage="29-have-result.jpg",
            title=f"have result: matches={len(matches)}",
            offset_x=int(detection.payload["x0"]),
            offset_y=int(detection.payload["y0"]),
        )
        _LOG.info("have scan matches=%d", len(matches))
        return ScanResult(self.id, self.title, matches)

    def warm(self, brain, cfg: dict) -> None:
        ocr.warm()

    def stop(self) -> None:
        ocr.stop()
