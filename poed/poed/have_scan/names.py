"""Card-name reading for the Have panel.

Names are gold serif text on the dark card background, wrapped over one or
two lines.  Each line strip is cropped by horizontal projection and all
strips across all cards go to the OCR helper in a single recognition-only
batch — no text detection pass.
"""

from __future__ import annotations

import numpy as np

from poed import ocr_worker
from poed import scan_cache

from .geometry import Card

_BRIGHT_TEXT = 110
_MIN_ROW_PIXELS = 4
_MIN_LINE_FRACTION = 0.18


def split_text_lines(name_crop: np.ndarray) -> list[tuple[int, int]]:
    """(y0, y1) spans of text lines inside a card's name area.

    Rows count as text when they carry at least a character's worth of
    bright pixels in absolute terms — a width-relative threshold silently
    dropped short wrapped continuation lines like the "16)" of
    "Uncut Skill Gem (Level\n16)", which truncated every gem level.
    """
    if name_crop.size == 0:
        return []
    gray = name_crop.mean(axis=2) if name_crop.ndim == 3 else name_crop
    profile = (gray > _BRIGHT_TEXT).sum(axis=1)
    height = profile.shape[0]
    spans: list[tuple[int, int]] = []
    start = None
    for y in range(height):
        if profile[y] >= _MIN_ROW_PIXELS and start is None:
            start = y
        elif profile[y] < _MIN_ROW_PIXELS and start is not None:
            if y - start >= height * _MIN_LINE_FRACTION:
                spans.append((start, y))
            start = None
    if start is not None and height - start >= height * _MIN_LINE_FRACTION:
        spans.append((start, height))
    return spans


def _line_crops(name_crop: np.ndarray, pad: int = 3) -> list[np.ndarray]:
    height = name_crop.shape[0]
    return [
        name_crop[max(0, y0 - pad) : min(height, y1 + pad)]
        for y0, y1 in split_text_lines(name_crop)
    ]


def read_card_names(
    crop: np.ndarray, cards: list[Card], timeout: float = 60.0
) -> list[str]:
    """One batched recognition-only pass over all cards' name-line strips.

    Cards whose name-area pixels are byte-identical to a card read on the
    previous press reuse that read (OCR is deterministic on identical
    input); only changed cards go to the OCR helper.
    """
    jobs: list[int] = []
    strips: list = []
    keys: list[bytes | None] = []
    cached: dict[int, str] = {}
    for index, card in enumerate(cards):
        nx, ny, nw, nh = card.name_box
        pad = max(2, card.h // 12)
        # Start slightly left of the icon/name divider: grid snapping can
        # sit a few pixels right of the true divider and clip the first
        # letter otherwise.
        name_crop = crop[
            max(0, ny + 2) : ny + nh - 2, max(0, nx - 4) : nx + nw - pad
        ]
        key = scan_cache.digest(name_crop) if name_crop.size else None
        keys.append(key)
        hit = scan_cache.lookup("have-name", key) if key is not None else None
        if isinstance(hit, str):
            cached[index] = hit
            continue
        for line in _line_crops(name_crop):
            jobs.append(index)
            strips.append(line)
    reads = ocr_worker.recognize_arrays(strips, timeout)
    texts: dict[int, list[str]] = {}
    for index, read in zip(jobs, reads):
        text = str(read.get("text") or "").strip()
        if text:
            texts.setdefault(index, []).append(text)
    out = []
    for index in range(len(cards)):
        if index in cached:
            out.append(cached[index])
            continue
        text = " ".join(texts.get(index, []))
        if keys[index] is not None:
            scan_cache.store("have-name", keys[index], text)
        out.append(text)
    return out
