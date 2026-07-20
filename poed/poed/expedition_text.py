"""OCR text normalization, row grouping, and catalog name matching."""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from poed.ocr_worker import OcrLine

MATCH_FLOOR = 0.72
HAVE_MATCH_FLOOR = 0.82


_STACK_PREFIX_RE = re.compile(
    r"^\s*(?:(?P<xcount>\d+)\s*[xX]\b|(?P<amount>\d+(?:[.,]\d+)?)\s*(?P<suffix>[kKmM])?\b)\s*"
)


def _stack_prefix_value(text: str) -> int:
    match = _STACK_PREFIX_RE.match(str(text or ""))
    if not match:
        return 1
    raw = match.group("xcount") or match.group("amount")
    suffix = (match.group("suffix") or "").lower()
    try:
        value = float(raw.replace(",", "."))
    except ValueError:
        return 1
    if suffix == "k":
        value *= 1_000
    elif suffix == "m":
        value *= 1_000_000
    elif match.group("amount"):
        return 1
    return max(1, int(round(value)))


@dataclass(frozen=True)
class CompactCount:
    """A stack-count token parsed from OCR text (``1450``, ``1,450``,
    ``32.2K``, ``1.2M``, ``x54``)."""

    groups: tuple[str, ...]  # digit groups as rendered (comma-separated)
    frac: str | None         # single fractional digit for decimal compacts
    suffix: str | None       # "K" or "M"
    ambiguous: bool          # trailing digit-ish text makes the token unsafe


_COMPACT_COUNT_RE = re.compile(
    r"^[xX]?(?P<digits>\d{1,4}(?:,\d{3})*)"
    r"(?:[.](?P<frac>\d))?"
    r"\s*(?P<suffix>[kKmM])?"
    r"(?P<rest>.*)$",
    re.S,
)


def parse_compact_count(text: str) -> CompactCount | None:
    """Parse the full stack-count token at the start of ``text``.

    Unlike a bare leading-digits match, this refuses to hand back a
    truncated prefix of a larger number: ``1,450`` parses as groups
    ("1","450"), never as 1, and unparseable continuations are flagged
    ambiguous so callers fall back to stencil reads instead of trusting
    a confident wrong value.
    """

    match = _COMPACT_COUNT_RE.match(str(text or "").strip())
    if match is None:
        return None
    rest = (match.group("rest") or "").lstrip()
    ambiguous = bool(rest) and (rest[0].isdigit() or (
        rest[0] in ",." and len(rest) > 1 and rest[1].isdigit()
    ))
    frac = match.group("frac")
    suffix = (match.group("suffix") or "").upper() or None
    if frac is not None and suffix is None:
        # A fractional count without a compact suffix is not a thing the
        # game renders; treat as unsafe rather than guessing.
        ambiguous = True
    return CompactCount(
        groups=tuple(match.group("digits").split(",")),
        frac=frac,
        suffix=suffix,
        ambiguous=ambiguous,
    )


def strip_stack_prefix(text: str) -> str:
    return _STACK_PREFIX_RE.sub("", str(text or ""), count=1).strip()


def normalize(text: str) -> str:
    text = re.sub(r"^\s*(?:skill|support)\s*:\s*", "", text, flags=re.IGNORECASE)
    text = text.lower().replace("’", "'").replace("`", "'")
    text = re.sub(r"\b(uncut (?:skill|spirit|support)) gen\b", r"\1 gem", text)
    text = re.sub(r"^\s*\d+\s*[xX]\s*", "", text)
    text = re.sub(r"\b\d+(?=[a-z])", "", text)
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def stack_size(text: str) -> int:
    return _stack_prefix_value(text)


def row_groups(lines: list[OcrLine], crop_h: int) -> list[OcrLine]:
    """Merge OCR fragments that sit on the same Expedition reward row."""
    if not lines:
        return []
    threshold = max(16.0, crop_h * 0.035)
    buckets: list[list[OcrLine]] = []
    for line in sorted(lines, key=lambda l: ((l.box[1] + l.box[3]) / 2, l.box[0])):
        cy = (line.box[1] + line.box[3]) / 2
        for bucket in buckets:
            bcy = sum((b.box[1] + b.box[3]) / 2 for b in bucket) / len(bucket)
            if abs(cy - bcy) <= threshold:
                bucket.append(line)
                break
        else:
            buckets.append([line])

    out = []
    for bucket in buckets:
        bucket.sort(key=lambda l: l.box[0])
        text = " ".join(b.text for b in bucket)
        score = min((b.score for b in bucket), default=0.0)
        x0 = min(b.box[0] for b in bucket)
        y0 = min(b.box[1] for b in bucket)
        x1 = max(b.box[2] for b in bucket)
        y1 = max(b.box[3] for b in bucket)
        out.append(OcrLine(text, score, (x0, y0, x1, y1)))
    return out


@lru_cache(maxsize=8)
def _normalized_name_index(names: tuple[str, ...]) -> tuple[tuple[str, str], ...]:
    return tuple((normalize(name), name) for name in names)


def _row_index(rows: dict) -> list[tuple[str, str, dict]]:
    names = tuple(sorted(rows))
    return [
        (normalized, name, rows[name])
        for normalized, name in _normalized_name_index(names)
    ]


@lru_cache(maxsize=8)
def _exact_name_index(names: tuple[str, ...]) -> dict[str, str]:
    """normalized name -> first catalog name in sorted order sharing it."""
    out: dict[str, str] = {}
    for normalized, name in _normalized_name_index(names):
        if normalized and normalized not in out:
            out[normalized] = name
    return out


# One-entry memo per rows snapshot: the engine re-serves the same rows
# object across presses, and Have panels repeat the same card names many
# times per scan.
_match_memo_rows: dict | None = None
_match_memo: dict[tuple[str, float], tuple[str, dict, float] | None] = {}


def match_name(
    text: str,
    rows: dict,
    floor: float = MATCH_FLOOR,
) -> tuple[str, dict, float] | None:
    global _match_memo_rows, _match_memo
    if _match_memo_rows is not rows:
        _match_memo_rows = rows
        _match_memo = {}
    memo_key = (text, floor)
    if memo_key in _match_memo:
        return _match_memo[memo_key]
    result = _match_name_uncached(text, rows, floor)
    if len(_match_memo) < 4096:
        _match_memo[memo_key] = result
    return result


def _match_name_uncached(
    text: str,
    rows: dict,
    floor: float = MATCH_FLOOR,
) -> tuple[str, dict, float] | None:
    norms = []
    for candidate in (text, strip_stack_prefix(text)):
        norm = normalize(candidate)
        if norm and norm not in norms:
            norms.append(norm)
    # Gem cards render "(Level N)" after the name while most catalog rows
    # do not carry the suffix; a level-stripped variant lets those match.
    # Full-name variants come first, so rows that DO include the level
    # (runeshape combination entries) still win via the exact index.
    # A DANGLING fragment ("... (Level" with the number lost to OCR) is
    # stripped too: an unknowable level must resolve to the base row, not
    # fuzzy-match a specific level's price.
    for norm in list(norms):
        stripped = re.sub(r"\s*\blevel \d{1,2}$", "", norm)
        stripped = re.sub(r"\s*\ble?ve?l?$", "", stripped)
        if stripped and stripped != norm and stripped not in norms:
            norms.append(stripped)
    if not norms or all(len(norm) < 4 for norm in norms):
        return None

    # An exact normalized hit scores 1.0, which nothing non-identical can
    # outrank ((score, len) ordering: ratio 1.0 requires identical strings,
    # substring caps at 0.99) — so the full scan is skipped entirely.
    exact = _exact_name_index(tuple(sorted(rows)))
    exact_best: tuple[str, dict, float] | None = None
    for norm in norms:
        if len(norm) < 4:
            continue
        name = exact.get(norm)
        if name is not None and (
            exact_best is None or len(norm) > len(normalize(exact_best[0]))
        ):
            exact_best = (name, rows[name], 1.0)
    if exact_best is not None:
        return exact_best

    best: tuple[str, dict, float] | None = None
    best_rank: tuple[float, int] | None = None
    for norm in norms:
        if len(norm) < 4:
            continue
        norm_len = len(norm)
        for name_norm, name, row in _row_index(rows):
            if not name_norm:
                continue
            if name_norm == norm:
                score = 1.0
            elif name_norm in norm:
                score = 0.99 if len(name_norm) >= norm_len * 0.75 else 0.0
            else:
                # difflib ratio is bounded above by 2*min/(la+lb); skip the
                # expensive call when that bound cannot beat the current
                # best rank or reach the floor — the selected best (and the
                # floor decision) are unchanged.
                la = len(name_norm)
                bound = 2.0 * min(norm_len, la) / (norm_len + la)
                if bound < floor and (best_rank is None or bound < best_rank[0]):
                    continue
                if best_rank is not None and (
                    bound < best_rank[0]
                    or (bound == best_rank[0] and la <= best_rank[1])
                ):
                    continue
                score = difflib.SequenceMatcher(None, norm, name_norm).ratio()
            rank = (score, len(name_norm))
            if best is None or best_rank is None or rank > best_rank:
                best = (name, row, score)
                best_rank = rank
    if best is None or best[2] < floor:
        return None
    return best


def unmatched_row_name(line: OcrLine, crop: np.ndarray) -> str | None:
    """Recover a visible item name when the local market catalog is stale."""
    x0, y0, x1, y1 = line.box
    if x1 < crop.shape[1] * 0.70:
        return None
    ix0, iy0 = max(0, int(x0) - 8), max(0, int(y0) - 8)
    ix1 = min(crop.shape[1], int(x1) + 8)
    iy1 = min(crop.shape[0], int(y1) + 8)
    roi = crop[iy0:iy1, ix0:ix1]
    if roi.size == 0:
        return None
    if float(np.median(cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY))) < 100:
        return None
    text = str(line.text or "").strip()
    if ":" in text:
        prefix, suffix = text.split(":", 1)
        if normalize(prefix) in {"skill", "support"}:
            text = suffix
    text = re.sub(r"^\s*\d+\s*[xX]\s*", "", text)
    words = re.findall(r"[A-Za-z0-9]+(?:['’][A-Za-z0-9]+)?", text)
    name = " ".join(words).strip()
    if len(normalize(name)) < 4 or normalize(name) == "runeshape combinations":
        return None
    return name


def expedition_panel_present(lines: list[OcrLine]) -> bool:
    expected = "runeshape combinations"
    return any(
        difflib.SequenceMatcher(None, normalize(line.text), expected).ratio() >= 0.72
        for line in lines
        if line.score >= 0.45
    )


# Compatibility aliases for older private tests/callers.
_normalize = normalize
_stack_size = stack_size
_row_groups = row_groups
_expedition_panel_present = expedition_panel_present
