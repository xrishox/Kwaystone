from __future__ import annotations

import logging

import cv2
import numpy as np

from poed import expeditionscan

from .common import (
    finalize_matches,
    update_debug_manifest,
    write_debug_image,
    write_ocr_stage,
)
from . import ocr
from .types import Detection, ScanContext, ScanResult

_LOG = logging.getLogger("waystone.scanners.expedition")


class ExpeditionScanner:
    id = "expedition"
    title = "expedition rewards"
    priority = 20.0

    def should_probe(
        self,
        ctx: ScanContext,
        scene,
        *,
        additive_detected: bool,
        primary_detected: bool,
    ) -> bool:
        del scene, primary_detected
        if not additive_detected:
            return True
        crop, _x0, _y0 = expeditionscan.expedition_crop(ctx.frame)
        visual_gate = _visual_ocr_gate(crop)
        if visual_gate["reason"] == "parchment":
            return True
        update_debug_manifest(
            ctx.debug_dir,
            expedition_visual_gate=visual_gate,
            expedition_probe={
                "accepted": False,
                "reason": "additive-without-expedition-panel",
                **visual_gate,
            },
        )
        return False

    def probe(self, ctx: ScanContext, scene) -> Detection | None:
        del scene
        crop, x0, y0 = expeditionscan.expedition_crop(ctx.frame)
        visual_gate = _visual_ocr_gate(crop)
        if ctx.debug_dir:
            write_debug_image(ctx.debug_dir / "30-expedition-probe-crop.jpg", crop)
        update_debug_manifest(ctx.debug_dir, expedition_visual_gate=visual_gate)
        if not visual_gate["accepted"]:
            update_debug_manifest(
                ctx.debug_dir,
                expedition_probe={
                    "accepted": False,
                    "reason": "visual-gate",
                    **visual_gate,
                },
            )
            return None
        if ctx.debug_dir:
            write_debug_image(
                ctx.debug_dir / "31-expedition-probe-preprocessed.jpg",
                expeditionscan._preprocess(crop),
            )
        lines = _read_lines_for_gate(crop, visual_gate)
        grouped = expeditionscan._row_groups(lines, crop.shape[0])
        panel_present = expeditionscan._expedition_panel_present(lines)
        hits = [
            line for line in grouped
            if expeditionscan._match_name(line.text, ctx.rows) is not None
        ]
        hit_count = len(hits)
        accepted = panel_present or hit_count >= 4
        write_ocr_stage(
            ctx.debug_dir,
            "32-expedition-probe-ocr.jpg",
            crop,
            lines,
            (
                "expedition probe "
                f"{'accepted' if accepted else 'rejected'}: "
                f"title={panel_present} hits={hit_count}"
            ),
        )
        write_ocr_stage(
            ctx.debug_dir,
            "33-expedition-grouped-rows.jpg",
            crop,
            grouped,
            f"expedition grouped rows={len(grouped)} hits={len(hits)}",
        )
        update_debug_manifest(
            ctx.debug_dir,
            expedition_probe={
                "accepted": accepted,
                "title": panel_present,
                "ocr_lines": len(lines),
                "grouped_rows": len(grouped),
                "hits": hit_count,
            },
        )
        if not accepted:
            return None
        confidence = 1.0 if panel_present else min(0.95, 0.60 + hit_count * 0.08)
        return Detection(
            self.id,
            confidence,
            {
                "crop": crop,
                "x0": x0,
                "y0": y0,
                "lines": lines,
                "grouped": grouped,
            },
        )

    def scan(self, ctx: ScanContext, detection: Detection) -> ScanResult:
        crop = detection.payload["crop"]
        lines = detection.payload["lines"]
        matches, grouped = expeditionscan.scan_image(
            crop, ctx.rows, raw_lines=lines
        )
        matches = finalize_matches(
            ctx,
            matches,
            self.id,
            stage="39-expedition-result.jpg",
            title=f"expedition result: matches={len(matches)}",
            offset_x=int(detection.payload["x0"]),
            offset_y=int(detection.payload["y0"]),
        )
        _LOG.info("expedition scan matches=%d", len(matches))
        return ScanResult(self.id, self.title, matches)

    def warm(self, brain, cfg: dict) -> None:
        return

    def stop(self) -> None:
        return


def _read_lines_for_gate(crop: np.ndarray, visual_gate: dict) -> list:
    """OCR the probe crop, restricted to the detected label bands.

    The parchment panel has text everywhere and keeps the full crop; the
    in-world loot-label case only has text inside the detected bands, and
    PaddleOCR cost scales with input area. Line boxes are shifted back into
    full-crop coordinates so downstream grouping is unchanged.
    """

    spans = visual_gate.get("labelSpans") or []
    if visual_gate.get("reason") != "prominent-labels" or not spans:
        return ocr.read_lines(crop)
    pad = 30
    y_lo = max(0, min(y0 for y0, _y1 in spans) - pad)
    y_hi = min(crop.shape[0], max(y1 for _y0, y1 in spans) + pad)
    if y_hi - y_lo >= crop.shape[0] * 0.85:
        return ocr.read_lines(crop)
    lines = ocr.read_lines(crop[y_lo:y_hi])
    return [
        type(line)(
            line.text,
            line.score,
            (line.box[0], line.box[1] + y_lo, line.box[2], line.box[3] + y_lo),
        )
        for line in lines
    ]


def _visual_ocr_gate(crop: np.ndarray) -> dict:
    """Cheap high-recall gate for Expedition OCR.

    The OCR fallback is intentionally broad: it handles both the parchment
    Runeshape Combinations panel and in-world reward/loot labels.  This gate
    only rejects clear negatives.  Ambiguous cases still run OCR so routing
    correctness remains controlled by text matching.
    """

    if crop.size == 0:
        return {
            "accepted": False,
            "reason": "empty-crop",
            "parchmentRatio": 0.0,
            "prominentLabelRows": 0,
        }

    hsv = cv2.cvtColor(crop, cv2.COLOR_BGR2HSV)
    hue, sat, val = cv2.split(hsv)
    parchment = (sat <= 100) & (val >= 120)
    parchment_ratio = float(np.mean(parchment))

    prominent_rows, label_spans = _prominent_label_rows(
        hue, sat, val, crop.shape[1]
    )
    accepted = parchment_ratio >= 0.30 or prominent_rows >= 3
    reason = "parchment" if parchment_ratio >= 0.30 else (
        "prominent-labels" if prominent_rows >= 3 else "no-expedition-visual"
    )
    return {
        "accepted": bool(accepted),
        "reason": reason,
        "parchmentRatio": round(parchment_ratio, 4),
        "prominentLabelRows": int(prominent_rows),
        "labelSpans": label_spans,
    }


def _prominent_label_rows(
    hue: np.ndarray,
    sat: np.ndarray,
    val: np.ndarray,
    crop_width: int,
) -> tuple[int, list[tuple[int, int]]]:
    """Large horizontal text-label bands (count, y-spans), no recognition."""

    if crop_width <= 0:
        return 0, []
    bright_neutral = (sat <= 135) & (val >= 135)
    warm_text = (hue >= 5) & (hue <= 45) & (sat >= 35) & (val >= 90)
    magic_text = (hue >= 90) & (hue <= 150) & (sat >= 35) & (val >= 95)
    green_text = (hue >= 35) & (hue <= 95) & (sat >= 35) & (val >= 95)
    mask = (bright_neutral | warm_text | magic_text | green_text).astype(np.uint8) * 255
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)),
    )
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (25, 3)),
    )
    contours, _hierarchy = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    rows = []
    for contour in contours:
        x, y, width, height = cv2.boundingRect(contour)
        if width < max(42, crop_width * 0.09):
            continue
        if not 5 <= height <= 72:
            continue
        aspect = width / max(1, height)
        if aspect < 2.1:
            continue
        area = float(cv2.contourArea(contour))
        fill = area / max(1, width * height)
        if fill < 0.08:
            continue
        rows.append((x, y, width, height))

    centers: list[float] = []
    spans: list[tuple[int, int]] = []
    for _x, y, _width, height in sorted(rows, key=lambda item: item[1]):
        center = y + height / 2.0
        if centers and abs(center - centers[-1]) <= max(8.0, height * 0.70):
            spans[-1] = (min(spans[-1][0], y), max(spans[-1][1], y + height))
            continue
        centers.append(center)
        spans.append((y, y + height))
    return len(centers), spans
