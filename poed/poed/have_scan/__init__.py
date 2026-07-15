"""Have-panel scanner pipeline.

Geometry-first: item cards are located from the panel image alone, then one
batched recognition-only OCR pass reads card names and one more reads the
quantity overlays, which are verified against the game's digit glyphs.  No
full-image text detection pass is needed.
"""

from __future__ import annotations

import logging
from pathlib import Path

import numpy as np

from poed import expedition_text
from poed.match_fields import match_row_fields

from .geometry import Card, complete_grid
from .names import read_card_names
from .quantities import read_card_quantities

_LOG = logging.getLogger("waystone.have_scan")


def _heading_text(text: str) -> bool:
    """Sidebar tabs and section headings render in ALL CAPS; names do not."""
    letters = [c for c in text if c.isalpha()]
    return bool(letters) and all(c.isupper() for c in letters)


def _base_match(
    *,
    name: str,
    row: dict,
    score: float,
    card: Card,
    stack_size: int,
    ocr_text: str,
) -> dict:
    unit_price = row.get("price", 0) or 0
    nx, ny, nw, nh = card.name_box
    return {
        **match_row_fields(row),
        "name": name,
        "price": unit_price,
        "unitPrice": unit_price,
        "totalPrice": unit_price * stack_size,
        "stackSize": stack_size,
        "ambiguous": False,
        "score": round(score, 3),
        "ocr": ocr_text,
        "scanKind": "have",
        "x": int(nx),
        "y": int(ny),
        "w": int(max(1, nw)),
        "h": int(max(1, nh)),
    }


def scan_have_image(
    crop: np.ndarray,
    rows: dict,
    debug_dir: Path | None = None,
) -> tuple[list[dict], list[Card]]:
    """Scan a Have-panel crop; return (matches, detected cards)."""
    cards, _med_w, _med_h = complete_grid(crop)
    if not cards:
        return [], []
    texts = read_card_names(crop, cards)
    matched: list[tuple[Card, str, tuple[str, dict, float]]] = []
    for card, text in zip(cards, texts):
        if not text or _heading_text(text):
            continue
        hit = expedition_text.match_name(
            text, rows, floor=expedition_text.HAVE_MATCH_FLOOR
        )
        if hit is not None:
            matched.append((card, text, hit))

    quantities = read_card_quantities(
        crop, [card for card, _text, _hit in matched], debug_dir=debug_dir
    )
    matches = []
    for (card, text, (name, row, score)), quantity in zip(matched, quantities):
        stack_size = quantity.value if quantity.value is not None else 1
        matches.append(
            _base_match(
                name=name,
                row=row,
                score=score,
                card=card,
                stack_size=stack_size,
                ocr_text=text,
            )
        )
    matches.sort(key=lambda m: -(m.get("totalPrice") or m.get("price") or 0))
    _LOG.info(
        "have scan cards=%d matched=%d", len(cards), len(matches)
    )
    return matches, cards
