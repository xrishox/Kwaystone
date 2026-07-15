from __future__ import annotations

import logging

from poed import runeshape
from poed.match_fields import match_row_fields

from .common import (
    normalize_matches,
    update_debug_manifest,
    write_result_stage,
)
from .types import Detection, ScanContext, ScanResult

_LOG = logging.getLogger("waystone.scanners.runeshape")


class RuneshapeScanner:
    id = "runeshape"
    title = "expedition runeshape"

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        detections = runeshape.detect_all(ctx.frame)
        if not detections:
            update_debug_manifest(
                ctx.debug_dir,
                runeshape_probe={"accepted": False},
            )
            return None
        primary = sorted(
            detections,
            key=lambda item: (
                item.confidence,
                item.avg_score,
                len(item.runes),
            ),
            reverse=True,
        )[0]
        update_debug_manifest(
            ctx.debug_dir,
            runeshape_probe={
                "accepted": True,
                "count": len(detections),
                "name": primary.entry.get("name"),
                "stackSize": primary.entry.get("stackSize"),
                "level": primary.entry.get("level"),
                "runes": list(primary.runes),
                "avgScore": round(primary.avg_score, 4),
                "avgMargin": round(primary.avg_margin, 4),
                "alternatives": len(primary.alternatives),
                "detections": [
                    _detection_manifest(detected)
                    for detected in detections
                ],
            },
        )
        return Detection(
            self.id,
            max(detected.confidence for detected in detections),
            {"detections": detections, "detected": primary},
            region=primary.rect,
            evidence=tuple(
                f"{detected.entry.get('name')} x{detected.entry.get('stackSize', 1)} "
                f"{','.join(detected.runes)}"
                for detected in detections
            ),
        )

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        detections = tuple(detection.payload.get("detections") or ())
        if not detections and detection.payload.get("detected") is not None:
            detections = (detection.payload["detected"],)
        matches = normalize_matches(
            [_match_for_detection(ctx, detected) for detected in detections],
            self.id,
        )
        write_result_stage(
            ctx.debug_dir,
            "29-runeshape-result.jpg",
            ctx.shot,
            matches,
            f"runeshape result: matches={len(matches)}",
        )
        _LOG.info("runeshape scan matches=%d", len(matches))
        return ScanResult(self.id, self.title, matches)

    def warm(self, brain, cfg: dict) -> None:
        return

    def stop(self) -> None:
        return


def _detection_manifest(detected: runeshape.RuneshapeDetection) -> dict:
    return {
        "name": detected.entry.get("name"),
        "stackSize": detected.entry.get("stackSize"),
        "level": detected.entry.get("level"),
        "runes": list(detected.runes),
        "avgScore": round(detected.avg_score, 4),
        "avgMargin": round(detected.avg_margin, 4),
        "alternatives": len(detected.alternatives),
        "rect": {
            "x": detected.rect.x,
            "y": detected.rect.y,
            "w": detected.rect.w,
            "h": detected.rect.h,
        },
    }


def _match_for_detection(
    ctx: ScanContext,
    detected: runeshape.RuneshapeDetection,
) -> dict:
    entry = detected.entry
    name = str(entry.get("name") or "Runeshape reward")
    row = ctx.rows.get(name) if isinstance(ctx.rows, dict) else None
    price_available = bool(row) and row.get("priceAvailable", True) is not False
    price = float((row or {}).get("price") or 0)
    stack = max(1, int(entry.get("stackSize") or 1))
    return {
        **match_row_fields(row, kind_default="runeshape"),
        "name": name,
        "price": price,
        "unitPrice": price,
        "priceAvailable": price_available,
        "stackSize": stack,
        "x": ctx.frame_x + detected.rect.x,
        "y": ctx.frame_y + detected.rect.y,
        "w": detected.rect.w,
        "h": detected.rect.h,
        "runeshapeLevel": entry.get("level"),
        "runeshapeRunes": list(detected.runes),
        "ambiguous": len({
            (
                alt.get("name"),
                alt.get("stackSize"),
                alt.get("level"),
            )
            for alt in detected.alternatives
        }) > 1,
    }
