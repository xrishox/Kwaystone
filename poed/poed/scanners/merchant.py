from __future__ import annotations

import logging
import re

import cv2
import numpy as np

from poed import ocr_worker, uniquescan
from poed.image_geometry import Rect

from . import ocr
from .common import update_debug_manifest, write_debug_image, write_ocr_stage
from .scene import LayoutEvidence
from .types import Detection, ScanContext, ScanResult
from .unique_grid import scan_unique_grid

_LOG = logging.getLogger("waystone.scanners.merchant")


class MerchantScanner:
    id = "merchant"
    title = "merchant stock"
    priority = 30.0

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        # A visible Have panel is more specific than a vendor-like grid and
        # should not be reinterpreted as stock.
        if scene.have is not None:
            update_debug_manifest(ctx.debug_dir, merchant_probe="have-layout-present")
            return None
        candidates = sorted(scene.grid_candidates, key=lambda item: item.region.x)
        title_reads = []
        layout = None
        title_crop = None
        title_lines = []
        for index, candidate in enumerate(candidates):
            crop = _header_crop(ctx.frame, candidate)
            if crop.size == 0:
                continue
            plaque_box = _buy_sell_title_plaque_box(
                crop,
                float(candidate.cell or 0.0),
            )
            title_read = {
                "candidate": index,
                "visual_title_plaque": plaque_box is not None,
                "grid": {
                    "x": candidate.region.x,
                    "y": candidate.region.y,
                    "w": candidate.region.w,
                    "h": candidate.region.h,
                    "cell": candidate.cell,
                },
            }
            title_reads.append(title_read)
            if plaque_box is None:
                continue
            try:
                # The plaque gate already isolated the title strip;
                # recognition-only on that strip replaces the full
                # detection+recognition pass in the common accept case.
                # If it does not read as Buy/Sell, the full read decides,
                # so recall is unchanged.
                text, lines = _read_plaque_title(crop, plaque_box)
                title_read["plaque_text"] = text
                if not _is_buy_or_sell_title(text):
                    lines = ocr.read_lines(crop)
                    text = " ".join(line.text for line in lines)
            except ocr.OcrUnavailable as e:
                update_debug_manifest(
                    ctx.debug_dir,
                    merchant_probe={
                        "accepted": False,
                        "reason": f"ocr-unavailable: {e}",
                        "grid_candidates": len(candidates),
                    },
                )
                return None
            title_read["text"] = text
            if _is_buy_or_sell_title(text):
                layout = candidate
                title_crop = crop
                title_lines = lines
                break

        if layout is None:
            update_debug_manifest(
                ctx.debug_dir,
                merchant_probe={
                    "accepted": False,
                    "reason": "no-buy-or-sell-title",
                    "grid_candidates": len(candidates),
                    "title_reads": title_reads,
                },
            )
            return None
        rect = _stock_scan_region(ctx.frame, layout)
        crop = ctx.frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
        if ctx.debug_dir:
            write_ocr_stage(
                ctx.debug_dir,
                "11-merchant-title-ocr.jpg",
                title_crop,
                title_lines,
                "merchant title accepted: BUY OR SELL",
            )
            write_debug_image(ctx.debug_dir / "12-merchant-probe-crop.jpg", crop)
        update_debug_manifest(
            ctx.debug_dir,
            merchant_probe={
                "accepted": True,
                "layout_score": layout.score,
                "cell": layout.cell,
                "evidence": list(layout.details),
                "grid_region": _rect_json(layout.region),
                "scan_region": _rect_json(rect),
                "title_reads": title_reads,
            },
        )
        return Detection(
            self.id,
            layout.confidence,
            {
                "crop": crop,
                "x0": rect.x,
                "y0": rect.y,
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
            include_unknown=False,
            stage_name="18-merchant-result.jpg",
            stage_label="merchant result",
            matching_mode="shared",
            gray_thresh=0.70,
            color_thresh=0.73,
        )
        _LOG.info("merchant scan matches=%d", len(result.matches))
        return result

    def warm(self, brain, cfg: dict) -> None:
        uniquescan.warm(brain, cfg)

    def stop(self) -> None:
        return


def _header_crop(frame: np.ndarray, layout: LayoutEvidence) -> np.ndarray:
    cell = float(layout.cell or 0.0)
    if cell <= 0:
        return frame[0:0, 0:0]
    height, width = frame.shape[:2]
    rect = layout.region
    left = max(0, int(round(rect.x - cell * 0.5)))
    right = min(width, int(round(rect.x + rect.w + cell * 0.5)))
    top = max(0, int(round(rect.y - cell * 1.8)))
    bottom = min(height, int(round(rect.y + cell * 0.35)))
    if right <= left or bottom <= top:
        return frame[0:0, 0:0]
    return frame[top:bottom, left:right]


def _stock_scan_region(frame: np.ndarray, layout: LayoutEvidence) -> Rect:
    """Return the item-matching region for an accepted merchant stock grid.

    The grid-line detector localizes the stock panel from visible line
    evidence. In merchant windows, occupied edge columns often hide the outer
    grid lines, so the evidence rectangle can start inside the first stock
    column. Matching against that clipped crop drops tall/thin edge items. Use
    cell-relative padding around the accepted Buy/Sell grid so item art at any
    panel edge is fully present without adding item-specific exceptions.
    """

    cell = float(layout.cell or 0.0)
    if cell <= 0:
        return layout.region
    height, width = frame.shape[:2]
    rect = layout.region

    left = int(round(rect.x - cell * 2.0))
    top = int(round(rect.y - cell * 0.35))
    right = int(round(rect.x + rect.w + cell * 0.50))
    bottom = int(round(rect.y + rect.h + cell * 0.35))

    clipped = Rect(left, top, right - left, bottom - top).clipped(width, height)
    return clipped or layout.region


def _rect_json(rect: Rect) -> dict[str, int]:
    return {"x": rect.x, "y": rect.y, "w": rect.w, "h": rect.h}


def _is_buy_or_sell_title(text: str) -> bool:
    normalized = re.sub(r"[^A-Z]+", "", text.upper())
    if "BUYORSELL" in normalized:
        return True
    return "BUY" in normalized and "SELL" in normalized


def _read_plaque_title(
    crop: np.ndarray, box: tuple[int, int, int, int]
) -> tuple[str, list]:
    """Recognition-only read of the located title plaque strip."""

    x, y, w, h = box
    pad = max(2, h // 8)
    strip = crop[
        max(0, y - pad) : min(crop.shape[0], y + h + pad),
        max(0, x - pad) : min(crop.shape[1], x + w + pad),
    ]
    if strip.size == 0:
        return "", []
    reads = ocr_worker.recognize_arrays([strip])
    text = str(reads[0].get("text") or "").strip() if reads else ""
    line = ocr.OcrLine(
        text,
        float(reads[0].get("score") or 0.0) if reads else 0.0,
        (x, y, x + w, y + h),
    )
    return text, [line] if text else []


def _buy_sell_title_plaque_box(
    crop: np.ndarray, cell: float
) -> tuple[int, int, int, int] | None:
    """Locate the gold title plaque that warrants Merchant OCR, if any.

    The Buy/Sell merchant header has a wide, full-height gold title plaque
    above the stock grid. Ritual grids have gold/brown chrome too, but their
    title strip is clipped/thin and their central header is the dark tribute
    counter. This is only an OCR gate; text recognition remains the authority.
    """

    if crop.size == 0 or cell <= 0:
        return None
    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, (10, 25, 95), (45, 255, 255))
    gold = cv2.morphologyEx(
        gold,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (13, 5)),
    )
    contours, _hierarchy = cv2.findContours(
        gold,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    for contour in contours:
        _x, y, width, height = cv2.boundingRect(contour)
        width_cells = width / cell
        height_cells = height / cell
        y_cells = y / cell
        if (
            3.2 <= width_cells <= 6.4
            and 0.45 <= height_cells <= 1.25
            and y_cells <= 0.55
        ):
            return (_x, y, width, height)
    return None
