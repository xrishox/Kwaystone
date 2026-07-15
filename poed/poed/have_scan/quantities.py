"""Quantity reading for Have cards.

The quantity overlay sits at the icon's top-left corner.  One batched
recognition-only OCR pass over the tight per-card corner crops proposes
digit strings; each proposal is verified digit-by-digit against the glyph
stencils on a padded grayscale region.  Rows whose proposal fails
verification get a stencil-only open read instead — no extra OCR pass.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from poed import expedition_text, ocr_worker
from poed import scan_cache

from . import glyphs
from .geometry import Card

# ROI extents at the glyph bank's reference icon side (76 px).
_ROI_W = 56
_ROI_H = 40
_TIGHT_H = 36
_PROPOSAL_UPSCALE = 3


@dataclass(frozen=True)
class Quantity:
    value: int | None
    confidence: float
    source: str  # "verified", "open-read", or "none"
    proposal: str = ""


def _roi_images(
    crop: np.ndarray, card: Card, scale: float
) -> tuple[np.ndarray, np.ndarray, np.ndarray, int]:
    """(proposal_bgr, verify_gray, verify_sat, pad) for one card."""
    ix, iy, _iw, ih = card.icon_box
    pad = max(6, ih // 9)
    region = crop[
        max(0, iy - pad) : iy + ih + pad, max(0, ix - pad) : ix + ih + pad
    ]
    if abs(scale - 1.0) > 0.02:
        region = cv2.resize(
            region, None, fx=scale, fy=scale, interpolation=cv2.INTER_CUBIC
        )
        pad = int(round(pad * scale))
    tight = region[pad : pad + _TIGHT_H, pad : pad + _ROI_W]
    proposal = cv2.resize(
        tight,
        None,
        fx=_PROPOSAL_UPSCALE,
        fy=_PROPOSAL_UPSCALE,
        interpolation=cv2.INTER_CUBIC,
    )
    # Verification sees the full overlay width: a comma-grouped or
    # decimal-compact count ("1,450", "32.2K") spans more glyphs than the
    # tight OCR proposal crop, and clipping it made the trailing digits
    # unverifiable (the old truncation bug's second half).
    verify_region = region[: _ROI_H + 2 * pad, :]
    gray = cv2.cvtColor(verify_region, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(verify_region, cv2.COLOR_BGR2HSV)[:, :, 1]
    return proposal, gray, sat, pad


_OVERLAY_MIN_BRIGHT_PIXELS = 8


def _overlay_absent(gray, sat, pad) -> bool:
    """True when the tight count ROI holds no bright neutral pixels at all."""
    tight_gray = gray[pad : pad + _TIGHT_H, pad : pad + _ROI_W]
    tight_sat = sat[pad : pad + _TIGHT_H, pad : pad + _ROI_W]
    bright = (tight_gray > 150) & (tight_sat < 60)
    return int(bright.sum()) < _OVERLAY_MIN_BRIGHT_PIXELS


def read_card_quantities(
    crop: np.ndarray,
    cards: list[Card],
    debug_dir: Path | None = None,
    timeout: float = 60.0,
) -> list[Quantity]:
    if not cards:
        return []
    reference = glyphs.reference_icon_side()
    rois = []
    keys: list[bytes] = []
    cached: dict[int, Quantity] = {}
    proposals: list = []
    jobs: list[int] = []
    computed_early: dict[int, Quantity] = {}
    for index, card in enumerate(cards):
        ix, iy, _iw, ih = card.icon_box
        pad0 = max(6, ih // 9)
        source = crop[
            max(0, iy - pad0) : iy + ih + pad0,
            max(0, ix - pad0) : ix + ih + pad0,
        ]
        # The verified value depends only on these source pixels and the
        # card height (it sets the resample scale).
        key = scan_cache.digest(source, extra=f"h={card.h}")
        keys.append(key)
        hit = scan_cache.lookup("have-qty", key)
        if isinstance(hit, Quantity):
            cached[index] = hit
            continue
        scale = reference / max(1, card.h)
        proposal_img, gray, sat, pad = _roi_images(crop, card, scale)
        if _overlay_absent(gray, sat, pad):
            # No quantity overlay on this card (equipment, idols, single
            # logbooks...): a digit render carries hundreds of bright
            # neutral pixels, so an all-dark tight ROI cannot hold one.
            # Skipping saves the OCR read AND removes the only source of
            # open-read phantom digits on overlay-free art.
            quantity = Quantity(None, 0.0, "none")
            computed_early[index] = quantity
            scan_cache.store("have-qty", key, quantity)
            continue
        proposals.append(proposal_img)
        jobs.append(index)
        rois.append((gray, sat, pad))
    reads = ocr_worker.recognize_arrays(proposals, timeout)

    computed: dict[int, Quantity] = {}
    debug_rows = []
    for index, (gray, sat, pad), read in zip(jobs, rois, reads):
        text = str(read.get("text") or "").strip()
        token = expedition_text.parse_compact_count(text)
        x0_range = range(0, pad + 9)
        y0_range = range(max(0, pad - 6), pad + 11)
        value = None
        confidence = 0.0
        source = "none"
        if token is not None and token.suffix == "M":
            # No M stencil exists, so an M-compact can never verify and the
            # open read cannot distinguish it either; a None here is the
            # only value that cannot be catastrophically wrong (1.2M as 1).
            value, confidence, source = None, 0.0, "unverified-compact"
        elif token is not None and not token.ambiguous:
            if len(token.groups) == 1 and token.frac is None:
                # Plain digit run (optionally K): longest-verified-prefix
                # semantics — OCR routinely hallucinates a trailing digit
                # from icon art and the stencils are the authority on where
                # the real number ends.
                value, confidence, last = glyphs.verify_digits(
                    gray, token.groups[0], x0_range, y0_range, sat
                )
                source = "verified"
                if (
                    value is not None
                    and token.suffix == "K"
                    and len(str(value)) == len(token.groups[0])
                    and glyphs.verify_suffix(gray, "K", *last, sat=sat)
                ):
                    value *= 1000
            else:
                # Separator tokens ("1,450", "32.2K") verify whole-token:
                # every digit plus the suffix stencil, or rejected outright
                # — never a truncated prefix.
                value, confidence = glyphs.verify_compact(
                    gray, token.groups, token.frac, token.suffix,
                    x0_range, y0_range, sat,
                )
                source = "verified" if value is not None else "none"
        if value is None and source != "unverified-compact":
            value, confidence = glyphs.open_read(gray, x0_range, y0_range, sat)
            source = "open-read" if value is not None else "none"
        quantity = Quantity(value, confidence, source, proposal=text)
        computed[index] = quantity
        scan_cache.store("have-qty", keys[index], quantity)
    out: list[Quantity] = []
    for index in range(len(cards)):
        quantity = cached.get(index) or computed_early.get(index) or computed.get(index)
        if quantity is None:
            quantity = Quantity(None, 0.0, "none")
        out.append(quantity)
        debug_rows.append(
            {
                "proposal": quantity.proposal,
                "value": quantity.value,
                "confidence": round(quantity.confidence, 3),
                "source": quantity.source,
                "cached": index in cached,
            }
        )
    if debug_dir is not None:
        try:
            (Path(debug_dir) / "have-quantities.json").write_text(
                json.dumps(debug_rows, indent=1)
            )
        except OSError:
            pass
    return out
