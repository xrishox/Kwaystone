"""OCR Expedition vendor rows and Have trade-panel rows.

This module keeps the historical public/private API used by tests and older
callers, but the implementation is now split into focused modules:

- :mod:`poed.ocr_worker` owns the persistent PaddleOCR helper process.
- :mod:`poed.expedition_text` owns OCR row grouping and catalog matching.
- :mod:`poed.have_scan` owns the Have trade-panel scanner pipeline.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from poed import expedition_text as _text
from poed.match_fields import match_row_fields
from poed import have_scan as _have
from poed import ocr_worker as _ocr

_LOG = logging.getLogger("waystone.expeditionscan")

OcrLine = _ocr.OcrLine
OcrUnavailable = _ocr.OcrUnavailable
MATCH_FLOOR = _text.MATCH_FLOOR
HAVE_MATCH_FLOOR = _text.HAVE_MATCH_FLOOR
_PaddleWorker = _ocr.PaddleOcrWorker
_helper_python = _ocr._helper_python
_worker_for_helper = _ocr._worker_for_helper
_preprocess = _ocr.preprocess
_ocr_lines = _ocr.read_lines
warm = _ocr.warm
stop = _ocr.stop
_normalize = _text.normalize
_stack_size = _text.stack_size
_row_groups = _text.row_groups
_match_name = _text.match_name
_unmatched_row_name = _text.unmatched_row_name
_expedition_panel_present = _text.expedition_panel_present

# Expedition text column. The vendor UI is anchored left and scales with screen
# height, so x coordinates are height-relative instead of width-relative.
TEXT_REGION = (0.158, 0.54, 0.119, 0.72)  # x0, x1, y0, y1 height fractions


def expedition_crop(shot: np.ndarray) -> tuple[np.ndarray, int, int]:
    h, w = shot.shape[:2]
    x0 = min(w, int(h * TEXT_REGION[0]))
    x1 = min(w, int(h * TEXT_REGION[1]))
    y0 = int(h * TEXT_REGION[2])
    y1 = int(h * TEXT_REGION[3])
    return shot[y0:y1, x0:x1], x0, y0


def _base_match(
    *,
    name: str,
    row: dict,
    score: float,
    line: OcrLine,
    stack_size: int,
    scan_kind: str | None = None,
    anchor_at_right: bool = False,
) -> dict:
    unit_price = row.get("price", 0) or 0
    x0, y0, x1, y1 = line.box
    x = int(x1 + 8) if anchor_at_right else int(x0)
    out = {
        **match_row_fields(row),
        "name": name,
        "price": unit_price,
        "unitPrice": unit_price,
        "totalPrice": unit_price * stack_size,
        "stackSize": stack_size,
        "ambiguous": False,
        "score": round(score, 3),
        "ocr": line.text,
        "x": x,
        "y": int(y0),
        "w": int(max(1, x1 - x0)),
        "h": int(max(1, y1 - y0)),
    }
    if scan_kind is not None:
        out["scanKind"] = scan_kind
    return out


def scan_image(
    crop: np.ndarray, rows: dict, raw_lines: list[OcrLine] | None = None
) -> tuple[list[dict], list[OcrLine]]:
    raw_lines = raw_lines if raw_lines is not None else _ocr_lines(crop)
    row_lines = _row_groups(raw_lines, crop.shape[0])
    matches = []
    for line in row_lines:
        hit = _match_name(line.text, rows)
        if hit is None:
            name = _unmatched_row_name(line, crop)
            if name is None:
                continue
            row = {
                "price": 0,
                "quantity": 0,
                "kind": "catalog",
                "priceAvailable": False,
            }
            score = 0.0
        else:
            name, row, score = hit
        matches.append(
            _base_match(
                name=name,
                row=row,
                score=score,
                line=line,
                stack_size=_stack_size(line.text),
                anchor_at_right=True,
            )
        )
    matches.sort(key=lambda m: -(m.get("totalPrice") or m.get("price") or 0))
    return matches, row_lines


def scan_have_image(
    crop: np.ndarray,
    rows: dict,
    debug_dir: Path | None = None,
) -> tuple[list[dict], bool]:
    """Scan a Have-panel crop via :mod:`poed.have_scan`.

    Returns (matches, panel_present); the panel counts as present when the
    card grid was found, whether or not any names matched the catalog.
    """
    matches, cards = _have.scan_have_image(crop, rows, debug_dir=debug_dir)
    return matches, len(cards) >= 8
