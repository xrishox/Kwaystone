"""Digit glyph bank and stencil matching for Have quantity overlays.

The game draws stack counts in a fixed font: pure-grayscale fill with a
thin dark outline, at a size proportional to the card.  A stencil score at
a position is ``median(fill pixels) - median(outline pixels)``; icon art
cannot be simultaneously bright in the exact fill shape, dark in the
outline ring, and fully unsaturated, which is what the score plus the
saturation gate require.

Glyph templates live in ``poed/data/have_digit_glyphs.png`` (grayscale
sprite sheet) with layout metadata in the sibling JSON file.  Fill and
outline masks are derived from the sheet at load time.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

import cv2
import numpy as np

_DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# Verification acceptance for a digit slot.
VTHRESH = 0.40
# A competing digit must beat the claimed one by this much to reject it.
ALT_MARGIN = 0.08
# Digit fill is rendered unsaturated; art masquerading as fill is colored.
MAX_FILL_SATURATION = 20
MAX_DIGITS = 4
DIGITS = frozenset("0123456789")


@dataclass(frozen=True)
class Glyph:
    gray: np.ndarray
    fill: np.ndarray
    outline: np.ndarray
    fill_x0: int
    fill_y0: int
    fill_w: int
    fill_h: int


@lru_cache(maxsize=1)
def load_bank() -> dict[str, Glyph]:
    meta = json.loads((_DATA_DIR / "have_digit_glyphs.json").read_text())
    sheet = cv2.imread(
        str(_DATA_DIR / "have_digit_glyphs.png"), cv2.IMREAD_GRAYSCALE
    )
    if sheet is None:
        raise RuntimeError("have digit glyph sheet missing or unreadable")
    fill_threshold = int(meta["fillThreshold"])
    outline_threshold = int(meta["outlineThreshold"])
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (5, 5))
    bank: dict[str, Glyph] = {}
    for char, g in meta["glyphs"].items():
        patch = sheet[: g["h"], g["x"] : g["x"] + g["w"]]
        fill = (patch > fill_threshold).astype(np.uint8)
        halo = cv2.dilate(fill, kernel) & (~fill.astype(bool)).astype(np.uint8)
        outline = (patch < outline_threshold).astype(np.uint8) & halo
        bank[char] = Glyph(
            gray=patch.astype(np.float32),
            fill=fill,
            outline=outline,
            fill_x0=int(g["fillX0"]),
            fill_y0=int(g["fillY0"]),
            fill_w=int(g["fillW"]),
            fill_h=int(g["fillH"]),
        )
    return bank


def reference_icon_side() -> int:
    meta = json.loads((_DATA_DIR / "have_digit_glyphs.json").read_text())
    return int(meta["referenceIconSide"])


def stencil_score(
    roi: np.ndarray, glyph: Glyph, x: int, y: int, sat: np.ndarray | None = None
) -> float:
    h, w = glyph.gray.shape
    if y < 0 or x < 0 or y + h > roi.shape[0] or x + w > roi.shape[1]:
        return -1.0
    window = roi[y : y + h, x : x + w]
    if sat is not None:
        fill_sat = float(np.median(sat[y : y + h, x : x + w][glyph.fill > 0]))
        if fill_sat > MAX_FILL_SATURATION:
            return -1.0
    fill = window[glyph.fill > 0]
    outline = window[glyph.outline > 0]
    return (float(np.median(fill)) - float(np.median(outline))) / 255.0


def best_slot(
    roi: np.ndarray, glyph: Glyph, xs, ys, sat: np.ndarray | None = None
) -> tuple[float, int, int]:
    best = (-1.0, 0, 0)
    for y in ys:
        for x in xs:
            score = stencil_score(roi, glyph, x, y, sat)
            if score > best[0]:
                best = (score, x, y)
    return best


def _best_alternative(
    bank: dict[str, Glyph], roi: np.ndarray, char: str, x: int, y: int
) -> float:
    """Best competing digit score at the matched fill position."""
    glyph = bank[char]
    cx, cy = x + glyph.fill_x0, y + glyph.fill_y0
    best = -1.0
    for other, og in bank.items():
        if other == char or other not in DIGITS:
            continue
        ox, oy = cx - og.fill_x0, cy - og.fill_y0
        score = max(
            stencil_score(roi, og, ox + dx, oy + dy)
            for dx in (-1, 0, 1)
            for dy in (-1, 0, 1)
        )
        best = max(best, score)
    return best


def _continuation_window(
    bank: dict[str, Glyph], prev_char: str, char: str, px: int, py: int
) -> tuple[range, range]:
    prev = bank[prev_char]
    nx = px + prev.fill_x0 + prev.fill_w + 1 - bank[char].fill_x0
    return range(nx - 4, nx + 4), range(py - 2, py + 3)


def verify_digits(
    roi: np.ndarray,
    digits: str,
    x0_range: range,
    y0_range: range,
    sat: np.ndarray | None = None,
) -> tuple[int | None, float, tuple[str, int, int]]:
    """Verify a proposed digit string.

    Returns (value, confidence, (last_char, last_x, last_y)); the last-digit
    position feeds suffix verification.  Accepts the longest verified
    prefix; value is None when not even the first digit verifies.
    """
    bank = load_bank()
    verified: list[str] = []
    scores: list[float] = []
    px = py = 0
    for index, char in enumerate(digits[:MAX_DIGITS]):
        if char not in DIGITS or char not in bank:
            break
        if index == 0:
            xs, ys = x0_range, y0_range
        else:
            xs, ys = _continuation_window(bank, verified[-1], char, px, py)
        score, x, y = best_slot(roi, bank[char], xs, ys, sat)
        if score < VTHRESH:
            break
        if _best_alternative(bank, roi, char, x, y) > score + ALT_MARGIN:
            break
        verified.append(char)
        scores.append(score)
        px, py = x, y
    if not verified:
        return None, 0.0, ("", 0, 0)
    return (
        int("".join(verified)),
        min(scores),
        (verified[-1], px, py),
    )


def verify_suffix(
    roi: np.ndarray,
    suffix: str,
    last_char: str,
    last_x: int,
    last_y: int,
    sat: np.ndarray | None = None,
) -> bool:
    """Verify a compact-count suffix glyph (e.g. ``K``) after the digits."""
    bank = load_bank()
    glyph = bank.get(suffix.upper())
    if glyph is None:
        return False
    xs, ys = _continuation_window(bank, last_char, suffix.upper(), last_x, last_y)
    score, _x, _y = best_slot(roi, glyph, xs, ys, sat)
    return score >= VTHRESH


def _gap_continuation_window(
    bank: dict[str, Glyph], prev_char: str, char: str, px: int, py: int
) -> tuple[range, range]:
    """Continuation window across a separator (comma/decimal point).

    Separators have no stencils; the next digit is expected one separator
    width (~55% of a digit) beyond the normal continuation point, with a
    wider search so rendering variance cannot break the walk.
    """
    prev = bank[prev_char]
    # Separator width tracks a representative digit ("0"), not the previous
    # char — a narrow "1" before the comma must not shrink the gap.
    sep = max(4, round(bank["0"].fill_w * 0.55))
    nx = px + prev.fill_x0 + prev.fill_w + 1 + sep - bank[char].fill_x0
    return range(nx - 3, nx + 6), range(py - 2, py + 3)


def _verify_walk(
    roi: np.ndarray,
    chars: str,
    x0_range: range,
    y0_range: range,
    sat: np.ndarray | None,
    *,
    start: tuple[str, int, int] | None = None,
    gap_first: bool = False,
) -> tuple[list[str], list[float], tuple[str, int, int]] | None:
    """Verify ``chars`` in sequence; returns None unless EVERY char verifies.

    ``start`` continues from a previous walk's last glyph; ``gap_first``
    crosses a separator before the first char of this walk.
    """
    bank = load_bank()
    verified: list[str] = []
    scores: list[float] = []
    prev = start
    for index, char in enumerate(chars):
        if char not in DIGITS or char not in bank:
            return None
        if prev is None:
            xs, ys = x0_range, y0_range
        elif index == 0 and gap_first:
            xs, ys = _gap_continuation_window(bank, prev[0], char, prev[1], prev[2])
        else:
            xs, ys = _continuation_window(bank, prev[0], char, prev[1], prev[2])
        score, x, y = best_slot(roi, bank[char], xs, ys, sat)
        if score < VTHRESH:
            return None
        if _best_alternative(bank, roi, char, x, y) > score + ALT_MARGIN:
            return None
        verified.append(char)
        scores.append(score)
        prev = (char, x, y)
    return verified, scores, prev


def verify_compact(
    roi: np.ndarray,
    groups: tuple[str, ...],
    frac: str | None,
    suffix: str | None,
    x0_range: range,
    y0_range: range,
    sat: np.ndarray | None = None,
) -> tuple[int | None, float]:
    """Verify a full compact-count token; every glyph must verify.

    Unlike ``verify_digits`` this never accepts a truncated prefix: a
    ``1,450`` proposal either verifies as 1450 or fails entirely, and a
    suffix without a stencil (``M``) always fails.
    """
    total_digits = sum(len(g) for g in groups) + (1 if frac else 0)
    if total_digits == 0 or total_digits > MAX_DIGITS + 1:
        return None, 0.0
    if suffix is not None and suffix not in load_bank():
        return None, 0.0
    if frac is not None and suffix is None:
        return None, 0.0

    prev: tuple[str, int, int] | None = None
    scores: list[float] = []
    for index, group in enumerate(groups):
        walked = _verify_walk(
            roi, group, x0_range, y0_range, sat,
            start=prev, gap_first=index > 0,
        )
        if walked is None:
            return None, 0.0
        _chars, group_scores, prev = walked
        scores.extend(group_scores)
    if frac is not None:
        walked = _verify_walk(
            roi, frac, x0_range, y0_range, sat, start=prev, gap_first=True
        )
        if walked is None:
            return None, 0.0
        _chars, frac_scores, prev = walked
        scores.extend(frac_scores)
    if suffix is not None:
        assert prev is not None
        if not verify_suffix(roi, suffix, *prev, sat=sat):
            return None, 0.0

    whole = int("".join(groups))
    if suffix == "K":
        value = whole * 1000 + (int(frac) * 100 if frac else 0)
    elif suffix is None:
        value = whole
    else:
        return None, 0.0
    return value, min(scores)


def _walk_digits(
    roi: np.ndarray,
    start: tuple[str, float, int, int],
    sat: np.ndarray | None,
    limit: int,
) -> list[tuple[str, float, int, int]]:
    """Greedy contiguous digit walk from a verified starting glyph."""
    bank = load_bank()
    digits = [start]
    while len(digits) < limit:
        prev_char, _score, px, py = digits[-1]
        next_char, best = "", (-1.0, 0, 0)
        for char, glyph in bank.items():
            if char not in DIGITS:
                continue
            xs, ys = _continuation_window(bank, prev_char, char, px, py)
            candidate = best_slot(roi, glyph, xs, ys, sat)
            if candidate[0] > best[0]:
                next_char, best = char, candidate
        if best[0] < VTHRESH:
            break
        if _best_alternative(bank, roi, next_char, best[1], best[2]) > (
            best[0] + ALT_MARGIN
        ):
            break
        digits.append((next_char, *best))
    return digits


def _gap_group(
    roi: np.ndarray,
    prev: tuple[str, float, int, int],
    sat: np.ndarray | None,
    limit: int,
) -> list[tuple[str, float, int, int]]:
    """Digit group across a separator gap, or empty when none verifies."""
    bank = load_bank()
    prev_char, _score, px, py = prev
    first_char, first = "", (-1.0, 0, 0)
    for char, glyph in bank.items():
        if char not in DIGITS:
            continue
        xs, ys = _gap_continuation_window(bank, prev_char, char, px, py)
        candidate = best_slot(roi, glyph, xs, ys, sat)
        if candidate[0] > first[0]:
            first_char, first = char, candidate
    if first[0] < VTHRESH:
        return []
    if _best_alternative(bank, roi, first_char, first[1], first[2]) > (
        first[0] + ALT_MARGIN
    ):
        return []
    return _walk_digits(roi, (first_char, *first), sat, limit)


def open_read(
    roi: np.ndarray,
    x0_range: range,
    y0_range: range,
    sat: np.ndarray | None = None,
) -> tuple[int | None, float]:
    """Stencil-only read: find the first digit, then walk right.

    Compact renders are handled without an OCR proposal: digits continuing
    across a separator gap are either thousands groups (exactly three
    digits: concatenate) or a decimal compact (one digit then a verified
    ``K``: scale). Anything else gap-continued is ambiguous and reads as
    None rather than a truncated prefix.
    """
    bank = load_bank()
    first_char, first = "", (-1.0, 0, 0)
    for char, glyph in bank.items():
        if char == "0" or char not in DIGITS:
            continue
        candidate = best_slot(roi, glyph, x0_range, y0_range, sat)
        if candidate[0] > first[0]:
            first_char, first = char, candidate
    if first[0] < VTHRESH:
        return None, 0.0
    digits = _walk_digits(roi, (first_char, *first), sat, MAX_DIGITS)

    if len(digits) == MAX_DIGITS:
        # A CONTIGUOUS fifth digit means the real number is longer than we
        # can read; refuse rather than return a truncated prefix. (Gap
        # digits do not count here — art phantoms routinely verify across
        # a gap and must not poison plain reads.) The walk is seeded with
        # the last read digit, so ANY growth means an overflow digit.
        overflow = _walk_digits(roi, digits[-1], sat, 2)
        if len(overflow) > 1:
            return None, 0.0

    groups = [digits]
    while sum(len(g) for g in groups) <= MAX_DIGITS:
        last = groups[-1][-1]
        if verify_suffix(roi, "K", last[0], last[2], last[3], sat=sat):
            break  # a verified K terminates the count; nothing follows it
        gap = _gap_group(roi, groups[-1][-1], sat, 4)
        if not gap:
            break
        groups.append(gap)

    contiguous_value = int("".join(g[0] for g in digits))
    contiguous_conf = min(g[1] for g in digits)

    if len(groups) > 1 and all(len(group) == 3 for group in groups[1:]):
        # Comma-grouped thousands: 1|450 -> 1450.
        if sum(len(g) for g in groups) <= MAX_DIGITS + 1:
            last = groups[-1][-1]
            k_verified = verify_suffix(
                roi, "K", last[0], last[2], last[3], sat=sat
            )
            value = int("".join(g[0] for group in groups for g in group))
            confidence = min(g[1] for group in groups for g in group)
            return (value * 1000 if k_verified else value), confidence
    if len(groups) == 2:
        # Decimal compact: one fractional digit then K, anchored on the
        # frac digit so phantom strokes on the K cannot break it.
        frac = groups[1][0]
        if verify_suffix(roi, "K", frac[0], frac[2], frac[3], sat=sat):
            return (
                contiguous_value * 1000 + int(frac[0]) * 100,
                min(contiguous_conf, frac[1]),
            )
    # Gap digits that form no valid compact pattern are icon-art phantoms:
    # ignore them and trust the contiguous read (with its own terminal K).
    last = digits[-1]
    k_verified = verify_suffix(roi, "K", last[0], last[2], last[3], sat=sat)
    return (
        contiguous_value * 1000 if k_verified else contiguous_value
    ), contiguous_conf
