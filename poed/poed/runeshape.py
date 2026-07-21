"""Visual detection for Expedition remnant runeshape combinations.

The in-world remnant UI is not OCR-friendly: the useful data is a strip of
glowing rune symbols that can appear anywhere on screen. This module detects
candidate rune glyph rows, classifies each visible slot against vendored rune
icons, and maps the ordered rune sequence to the local PoE2DB combination table.
"""

from __future__ import annotations

import json
import math
import os
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache
from importlib import resources
from pathlib import Path

import cv2
import numpy as np

from poed.image_geometry import Rect

MAX_WIDTH = 1920
CLASSIFY_SIZE = 86
MIN_RUNE_SCORE = 0.78
MIN_AVG_SCORE = 0.83
MIN_AVG_MARGIN = 0.045
CONTEXTUAL_MIN_LENGTH = 5
CONTEXTUAL_MIN_RUNE_SCORE = 0.68
CONTEXTUAL_WEAK_RUNE_SCORE = 0.62
CONTEXTUAL_MAX_WEAK_SLOTS = 1
CONTEXTUAL_MIN_AVG_SCORE = 0.72
CONTEXTUAL_WEAK_MIN_AVG_SCORE = 0.76
CONTEXTUAL_MAX_OPTIONS_PER_SLOT = 8
MAX_SEQUENCES_PER_LENGTH = 6
MAX_GEOMETRY_SEQUENCES_PER_SPAN = 8
# Bounded pool for strict slot classification; half the cores because the
# game is running alongside (same policy as the unique-icon matcher).
CLASSIFY_WORKERS = max(1, int(os.environ.get(
    "WAYSTONE_RUNESHAPE_WORKERS",
    max(1, (os.cpu_count() or 2) // 2),
)))


_pool_lock = threading.Lock()
_classify_pool: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """Reused classification pool; spin-up is not paid per press."""
    global _classify_pool
    with _pool_lock:
        if _classify_pool is None:
            _classify_pool = ThreadPoolExecutor(
                max_workers=CLASSIFY_WORKERS,
                thread_name_prefix="waystone-runeshape",
            )
        return _classify_pool


@dataclass(frozen=True)
class RuneCandidate:
    x: float
    y: float
    w: float
    h: float
    area: float

    @property
    def size(self) -> float:
        return max(self.w, self.h)


@dataclass(frozen=True)
class RuneClassification:
    rune: str
    score: float
    margin: float
    x: float
    y: float
    size: float


@dataclass(frozen=True)
class RuneshapeDetection:
    entry: dict
    alternatives: tuple[dict, ...]
    runes: tuple[str, ...]
    classifications: tuple[RuneClassification, ...]
    rect: Rect
    confidence: float
    avg_score: float
    avg_margin: float


@dataclass(frozen=True)
class _TemplateVariant:
    gray: np.ndarray


@dataclass(frozen=True, eq=False)
class _RuneTemplate:
    name: str
    gray: np.ndarray

    @lru_cache(maxsize=64)
    def resized(self, size: int) -> _TemplateVariant:
        size = max(8, int(size))
        gray = cv2.resize(self.gray, (size, size), interpolation=cv2.INTER_CUBIC)
        return _TemplateVariant(gray)


def _decode_image(data: bytes) -> np.ndarray:
    raw = np.frombuffer(data, dtype=np.uint8)
    image = cv2.imdecode(raw, cv2.IMREAD_UNCHANGED)
    if image is None:
        raise RuntimeError("could not decode runeshape image resource")
    return image


@lru_cache(maxsize=1)
def _data_root():
    local = Path(__file__).resolve().parent / "data"
    if local.exists():
        return local
    return resources.files("poed").joinpath("data")


@lru_cache(maxsize=1)
def _data() -> dict:
    payload = _data_root().joinpath("runeshape_combinations.json").read_text(
        encoding="utf-8"
    )
    return json.loads(payload)


@lru_cache(maxsize=1)
def entries_by_sequence() -> dict[tuple[str, ...], tuple[dict, ...]]:
    grouped: dict[tuple[str, ...], list[dict]] = {}
    seen: set[tuple] = set()
    for entry in _data()["entries"]:
        sequence = tuple(str(r) for r in entry.get("runes") or ())
        key = (
            str(entry.get("name") or ""),
            int(entry.get("stackSize") or 1),
            str(entry.get("level") or ""),
            sequence,
        )
        if key in seen:
            continue
        seen.add(key)
        grouped.setdefault(sequence, []).append(entry)
    return {k: tuple(v) for k, v in grouped.items()}


@lru_cache(maxsize=1)
def _sequence_prefixes_by_length() -> dict[int, set[tuple[str, ...]]]:
    prefixes: dict[int, set[tuple[str, ...]]] = {}
    for sequence in entries_by_sequence():
        length = len(sequence)
        bucket = prefixes.setdefault(length, set())
        for prefix_len in range(1, length + 1):
            bucket.add(sequence[:prefix_len])
    return prefixes


@lru_cache(maxsize=1)
def _templates() -> tuple[_RuneTemplate, ...]:
    root = _data_root()
    templates = []
    for rune, rel_path in sorted(_data()["runes"].items()):
        image = _decode_image(root.joinpath(rel_path).read_bytes())
        if image.ndim == 3 and image.shape[2] == 4:
            bgr = image[:, :, :3]
        elif image.ndim == 2:
            bgr = cv2.cvtColor(image, cv2.COLOR_GRAY2BGR)
        else:
            bgr = image
        gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        templates.append(_RuneTemplate(str(rune), gray))
    return tuple(templates)


def _scale_frame(frame: np.ndarray) -> tuple[np.ndarray, float]:
    height, width = frame.shape[:2]
    scale = min(1.0, MAX_WIDTH / max(1, width))
    if scale >= 1.0:
        return frame, 1.0
    return (
        cv2.resize(frame, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA),
        scale,
    )


def _hsv_planes(image: np.ndarray) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    return cv2.split(cv2.cvtColor(image, cv2.COLOR_BGR2HSV))


def _blue_purple_mask(
    image: np.ndarray,
    *,
    value_floor: int,
    hsv: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> np.ndarray:
    hue, sat, val = hsv if hsv is not None else _hsv_planes(image)
    return np.where(
        (hue >= 75)
        & (hue <= 172)
        & (sat >= 45)
        & (val >= value_floor),
        255,
        0,
    ).astype(np.uint8)


def _masked_value(
    image: np.ndarray,
    *,
    include_neutral: bool = True,
    hsv: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> np.ndarray:
    hue, sat, val = hsv if hsv is not None else _hsv_planes(image)
    mask = np.where(
        (
            (hue >= 75)
            & (hue <= 172)
            & (sat >= 35)
            & (val >= 80)
        )
        | (
            # Clicking a remnant rune can render its glyph as pale grey/white
            # instead of cyan/purple. Keep these neutral bright strokes
            # available for classification; candidate generation still uses
            # colored glyphs, and exact sequence matching keeps false positives
            # bounded.
            include_neutral
            &
            (sat <= 95)
            & (val >= 115)
        )
        | (
            # The selected/clicked rune can also render as warm gold/orange.
            # Treat that as another glyph visual state during classification,
            # not as a separate rune or item-specific exception.
            (hue >= 5)
            & (hue <= 35)
            & (sat >= 40)
            & (val >= 85)
        ),
        255,
        0,
    ).astype(np.uint8)
    return cv2.bitwise_and(val, val, mask=mask)


def _odd(value: float) -> int:
    out = max(3, int(round(value)))
    return out if out % 2 else out + 1


def _rune_candidates(
    image: np.ndarray,
    hsv: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None,
) -> list[RuneCandidate]:
    height, width = image.shape[:2]
    mask = _blue_purple_mask(image, value_floor=115, hsv=hsv)
    mask = cv2.morphologyEx(
        mask,
        cv2.MORPH_OPEN,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3)),
    )
    kernel = _odd(max(7, min(17, min(height, width) / 100.0)))
    mask = cv2.dilate(
        mask,
        cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (kernel, kernel)),
        iterations=1,
    )

    contours, _hierarchy = cv2.findContours(
        mask,
        cv2.RETR_EXTERNAL,
        cv2.CHAIN_APPROX_SIMPLE,
    )
    min_side = max(14.0, min(height, width) * 0.010)
    max_side = max(72.0, min(130.0, min(height, width) * 0.095))
    candidates: list[RuneCandidate] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        area = float(cv2.contourArea(contour))
        if area < 80:
            continue
        if not (min_side <= w <= max_side and min_side <= h <= max_side):
            continue
        aspect = w / max(1, h)
        if not (0.45 <= aspect <= 2.2):
            continue
        candidates.append(
            RuneCandidate(
                x=x + w / 2.0,
                y=y + h / 2.0,
                w=float(w),
                h=float(h),
                area=area,
            )
        )
    return candidates


def _row_groups(candidates: list[RuneCandidate]) -> list[list[RuneCandidate]]:
    if not candidates:
        return []
    median_size = float(np.median([c.size for c in candidates]))
    tolerance = max(12.0, median_size * 0.45)
    groups: list[list[RuneCandidate]] = []
    for candidate in sorted(candidates, key=lambda c: (c.y, c.x)):
        for group in groups:
            center_y = sum(c.y for c in group) / len(group)
            if abs(candidate.y - center_y) <= tolerance:
                group.append(candidate)
                break
        else:
            groups.append([candidate])
    return [sorted(group, key=lambda c: c.x) for group in groups if len(group) >= 2]


def _regular_sequences(row: list[RuneCandidate]):
    for start in range(len(row)):
        for end in range(start + 2, min(len(row), start + 10) + 1):
            sequence = row[start:end]
            gaps = [b.x - a.x for a, b in zip(sequence, sequence[1:])]
            if not gaps:
                continue
            median_gap = float(np.median(gaps))
            median_size = float(np.median([c.size for c in sequence]))
            if median_gap < max(24.0, median_size * 0.85):
                continue
            if median_gap > max(48.0, median_size * 3.0):
                continue
            if max(abs(gap - median_gap) for gap in gaps) > max(9.0, median_gap * 0.24):
                continue
            yield sequence, median_gap, median_size


def _base_row_gap(ordered: list[RuneCandidate]) -> float | None:
    if len(ordered) < 2:
        return None
    gaps = [b.x - a.x for a, b in zip(ordered, ordered[1:]) if b.x > a.x]
    if not gaps:
        return None
    median_size = float(np.median([c.size for c in ordered]))
    plausible = [
        gap
        for gap in gaps
        if gap >= max(18.0, median_size * 0.75)
    ]
    source = plausible or gaps
    return float(np.percentile(source, 35))


def _merge_candidate_cluster(cluster: list[RuneCandidate]) -> RuneCandidate:
    if len(cluster) == 1:
        return cluster[0]
    weights = np.asarray([max(1.0, candidate.area) for candidate in cluster], dtype=np.float32)
    xs = np.asarray([candidate.x for candidate in cluster], dtype=np.float32)
    ys = np.asarray([candidate.y for candidate in cluster], dtype=np.float32)
    x0 = min(candidate.x - candidate.w / 2.0 for candidate in cluster)
    y0 = min(candidate.y - candidate.h / 2.0 for candidate in cluster)
    x1 = max(candidate.x + candidate.w / 2.0 for candidate in cluster)
    y1 = max(candidate.y + candidate.h / 2.0 for candidate in cluster)
    return RuneCandidate(
        x=float(np.average(xs, weights=weights)),
        y=float(np.average(ys, weights=weights)),
        w=float(x1 - x0),
        h=float(y1 - y0),
        area=float(sum(candidate.area for candidate in cluster)),
    )


def _merge_fragmented_row_candidates(row: list[RuneCandidate]) -> list[RuneCandidate]:
    """Merge adjacent sub-slot components from one visual rune button.

    Selected or modified runes can split into multiple colored contours: one
    small component from the border/glow and one from the glyph. Those fragments
    are closer than a real rune-slot gap. Merge only sub-gap neighbors, then let
    exact table matching decide whether the normalized row is meaningful.
    """

    ordered = sorted(row, key=lambda c: c.x)
    base_gap = _base_row_gap(ordered)
    if base_gap is None or base_gap < 18:
        return ordered
    merge_gap = base_gap * 0.65
    clusters: list[list[RuneCandidate]] = []
    for candidate in ordered:
        if clusters and candidate.x - clusters[-1][-1].x <= merge_gap:
            clusters[-1].append(candidate)
        else:
            clusters.append([candidate])
    merged = [_merge_candidate_cluster(cluster) for cluster in clusters]
    return sorted(merged, key=lambda c: c.x)


def _candidate_rows(row: list[RuneCandidate], image_width: int) -> list[list[RuneCandidate]]:
    """Return row variants, including inferred selected rune slots.

    Most rune glyphs are blue/purple and therefore become candidates directly.
    A selected/clicked rune can become neutral grey or warm gold, so it may not
    seed the row. When a row has otherwise regular spacing with a one-slot gap,
    infer that missing slot before classification.
    """

    if len(row) < 2:
        return [row]
    ordered = _merge_fragmented_row_candidates(row)
    if len(ordered) < 2:
        return [ordered]
    gaps = [b.x - a.x for a, b in zip(ordered, ordered[1:])]
    # Use a lower percentile so one missing slot (a doubled gap) does not
    # inflate the base spacing estimate.
    base_gap = _base_row_gap(ordered)
    if base_gap is None:
        return [ordered]
    if base_gap < 18:
        return [ordered]
    median_size = float(np.median([c.size for c in ordered]))
    median_y = float(np.median([c.y for c in ordered]))

    augmented: list[RuneCandidate] = []
    inferred = False
    for left, right, gap in zip(ordered, ordered[1:], gaps):
        if not augmented:
            augmented.append(left)
        ratio = gap / base_gap
        if 1.70 <= ratio <= 2.30:
            augmented.append(
                RuneCandidate(
                    x=(left.x + right.x) / 2.0,
                    y=median_y,
                    w=median_size,
                    h=median_size,
                    area=0.0,
                )
            )
            inferred = True
        augmented.append(right)

    variants = [ordered]
    if inferred:
        variants.insert(0, augmented)

    # If the selected rune is at either edge, there is no doubled internal gap.
    # Try one inferred edge slot on each side; classification/exact table lookup
    # will reject it unless the visual glyph and sequence are coherent.
    left_x = ordered[0].x - base_gap
    right_x = ordered[-1].x + base_gap
    if left_x > median_size:
        variants.append([
            RuneCandidate(left_x, median_y, median_size, median_size, 0.0),
            *ordered,
        ])
    if right_x < image_width - median_size:
        variants.append([
            *ordered,
            RuneCandidate(right_x, median_y, median_size, median_size, 0.0),
        ])

    return variants


def _score_slot_templates(
    masked_values: tuple[np.ndarray, ...],
    center_x: float,
    center_y: float,
    slot_size: int,
) -> dict[str, float]:
    height, width = masked_values[0].shape[:2]
    half = slot_size // 2
    x0 = max(0, int(round(center_x)) - half)
    y0 = max(0, int(round(center_y)) - half)
    x1 = min(width, int(round(center_x)) + half)
    y1 = min(height, int(round(center_y)) + half)

    expected = max(18, int(round(slot_size * 0.48)))
    sizes = sorted({
        max(14, min(slot_size - 2, int(round(expected * scale))))
        for scale in (0.78, 0.90, 1.0, 1.10)
    })
    best_by_name: dict[str, float] = {}
    for masked_value in masked_values:
        crop = masked_value[y0:y1, x0:x1]
        if crop.shape[0] < 24 or crop.shape[1] < 24:
            continue
        for template in _templates():
            best = best_by_name.get(template.name, 0.0)
            for size in sizes:
                variant = template.resized(size)
                if crop.shape[0] < size or crop.shape[1] < size:
                    continue
                # The crop has already been masked down to glyph intensity.
                # Unmasked correlation is both faster and avoids OpenCV
                # masked-template NaN/Inf edge cases on transparent WebP assets.
                result = cv2.matchTemplate(
                    crop,
                    variant.gray,
                    cv2.TM_CCORR_NORMED,
                )
                _min_val, max_val, _min_loc, _max_loc = cv2.minMaxLoc(result)
                if math.isfinite(max_val):
                    best = max(best, float(max_val))
            best_by_name[template.name] = best
    return best_by_name


def _classify_slot_options(
    masked_values: tuple[np.ndarray, ...],
    center_x: float,
    center_y: float,
    slot_size: int,
    *,
    limit: int = CONTEXTUAL_MAX_OPTIONS_PER_SLOT,
) -> tuple[RuneClassification, ...]:
    if not masked_values:
        return ()

    best_by_name = _score_slot_templates(masked_values, center_x, center_y, slot_size)
    if not best_by_name:
        return ()

    ranked = sorted(best_by_name.items(), key=lambda item: item[1], reverse=True)
    out: list[RuneClassification] = []
    for index, (name, score) in enumerate(ranked[:limit]):
        if index == 0:
            competitor = ranked[1][1] if len(ranked) > 1 else 0.0
        else:
            competitor = ranked[0][1]
        out.append(
            RuneClassification(
                rune=name,
                score=score,
                margin=score - competitor,
                x=center_x,
                y=center_y,
                size=float(slot_size),
            )
        )
    return tuple(out)


def _classify_slot(
    masked_value: np.ndarray,
    center_x: float,
    center_y: float,
    slot_size: int,
) -> RuneClassification | None:
    options = _classify_slot_options(
        (masked_value,),
        center_x,
        center_y,
        slot_size,
        limit=1,
    )
    return options[0] if options else None


def _choose_entry(entries: tuple[dict, ...]) -> dict:
    return sorted(
        entries,
        key=lambda entry: (
            int(entry.get("levelMin") or -1),
            -int(entry.get("levelMax") or 999),
            int(entry.get("stackSize") or 1),
            str(entry.get("name") or ""),
        ),
        reverse=True,
    )[0]


def _contextual_classifications(
    sequence: list[RuneCandidate],
    center_y: float,
    slot_size: int,
    masked_values: tuple[np.ndarray, ...],
    valid_prefixes: set[tuple[str, ...]],
    by_sequence: dict[tuple[str, ...], tuple[dict, ...]],
    classify_cache: dict[tuple[int, int, int], tuple[RuneClassification, ...]],
) -> list[RuneClassification] | None:
    """Recover exact long table sequences from ambiguous per-slot rankings.

    Some in-game remnant rows render the glyph inside a large glowing circular
    button. The circle/ring lowers raw template scores and can make a visually
    similar rare rune rank above the real rune. This beam keeps only prefixes
    that exist in the vendored combination table, so lower-score recovery is
    constrained by the same exact data gate as the normal detector.

    A selected or visually modified slot can occasionally score below the
    normal contextual floor even though the surrounding row is strong. Permit
    one such weak slot, but only through exact-prefix pruning and only when the
    completed table sequence keeps a high row average.
    """

    if len(sequence) < CONTEXTUAL_MIN_LENGTH:
        return None
    beams: list[tuple[RuneClassification, ...]] = [()]
    for candidate in sequence:
        key = (int(round(candidate.x)), int(round(center_y)), slot_size)
        if key not in classify_cache:
            classify_cache[key] = _classify_slot_options(
                masked_values,
                candidate.x,
                center_y,
                slot_size,
            )
        options = [
            option
            for option in classify_cache[key]
            if option.score >= CONTEXTUAL_WEAK_RUNE_SCORE
        ]
        if not options:
            return None

        next_beams: list[tuple[RuneClassification, ...]] = []
        for beam in beams:
            for option in options:
                proposed = (*beam, option)
                prefix = tuple(item.rune for item in proposed)
                weak_slots = sum(
                    item.score < CONTEXTUAL_MIN_RUNE_SCORE
                    for item in proposed
                )
                if weak_slots > CONTEXTUAL_MAX_WEAK_SLOTS:
                    continue
                if prefix in valid_prefixes:
                    next_beams.append(proposed)
        if not next_beams:
            return None

        next_beams.sort(
            key=lambda beam: (
                -sum(item.score < CONTEXTUAL_MIN_RUNE_SCORE for item in beam),
                float(np.mean([item.score for item in beam])),
                min(item.score for item in beam),
                sum(item.margin for item in beam),
            ),
            reverse=True,
        )
        beams = next_beams[:MAX_SEQUENCES_PER_LENGTH]

    complete = [
        beam
        for beam in beams
        if tuple(item.rune for item in beam) in by_sequence
    ]
    if not complete:
        return None
    complete.sort(
        key=lambda beam: (
            -sum(item.score < CONTEXTUAL_MIN_RUNE_SCORE for item in beam),
            float(np.mean([item.score for item in beam])),
            min(item.score for item in beam),
            sum(item.margin for item in beam),
        ),
        reverse=True,
    )
    best = complete[0]
    avg_score = float(np.mean([item.score for item in best]))
    if avg_score < CONTEXTUAL_MIN_AVG_SCORE:
        return None
    if (
        any(item.score < CONTEXTUAL_MIN_RUNE_SCORE for item in best)
        and avg_score < CONTEXTUAL_WEAK_MIN_AVG_SCORE
    ):
        return None
    return list(best)


def _frame_rect(
    sequence: list[RuneCandidate],
    slot_size: int,
    scale: float,
    frame_shape,
) -> Rect:
    inv = 1.0 / scale
    height, width = frame_shape[:2]
    y = float(np.median([c.y for c in sequence]))
    x0 = min(c.x for c in sequence) - slot_size * 0.55
    x1 = max(c.x for c in sequence) + slot_size * 0.55
    y0 = y - slot_size * 0.55
    y1 = y + slot_size * 0.55
    left = max(0, min(width - 1, int(round(x0 * inv))))
    top = max(0, min(height - 1, int(round(y0 * inv))))
    right = max(left + 1, min(width, int(round(x1 * inv))))
    bottom = max(top + 1, min(height, int(round(y1 * inv))))
    return Rect(left, top, right - left, bottom - top)


def _score_detection(classifications: list[RuneClassification], length: int) -> float:
    avg_score = float(np.mean([c.score for c in classifications]))
    avg_margin = float(np.mean([c.margin for c in classifications]))
    return (
        length * 0.12
        + avg_score
        + min(0.12, avg_margin)
        + min(0.10, max(0, length - 2) * 0.02)
    )


def _detection_score(detection: RuneshapeDetection) -> float:
    return _score_detection(list(detection.classifications), len(detection.runes))


def _sequence_geometry_score(
    sequence: list[RuneCandidate],
    median_gap: float,
    median_size: float,
) -> float:
    ratio = median_gap / max(1.0, median_size)
    y_spread = max(c.y for c in sequence) - min(c.y for c in sequence)
    return (
        len(sequence) * 1.2
        - abs(ratio - 1.42) * 1.6
        - abs(median_size - 39.0) * 0.025
        - y_spread * 0.02
    )


def _sequence_span(sequence: list[RuneCandidate]) -> tuple[float, float]:
    return min(c.x for c in sequence), max(c.x for c in sequence)


def _span_overlap_ratio(
    left: tuple[float, float],
    right: tuple[float, float],
) -> float:
    overlap = max(0.0, min(left[1], right[1]) - max(left[0], right[0]))
    smaller = max(1.0, min(left[1] - left[0], right[1] - right[0]))
    return overlap / smaller


def _limit_ranked_geometry_sequences(
    items: list[tuple[float, list[RuneCandidate], float, float]],
) -> list[tuple[float, list[RuneCandidate], float, float]]:
    """Limit classification work while preserving separate same-band strips."""

    ranked = sorted(items, key=lambda item: item[0], reverse=True)
    kept: list[tuple[float, list[RuneCandidate], float, float]] = []
    spans: list[tuple[float, float]] = []
    counts: list[int] = []
    for item in ranked:
        span = _sequence_span(item[1])
        cluster_index = None
        for index, existing in enumerate(spans):
            if _span_overlap_ratio(span, existing) >= 0.35:
                cluster_index = index
                break
        if cluster_index is None:
            spans.append(span)
            counts.append(1)
            kept.append(item)
            continue
        if counts[cluster_index] >= MAX_GEOMETRY_SEQUENCES_PER_SPAN:
            continue
        counts[cluster_index] += 1
        kept.append(item)
    return kept


def _rect_overlap_ratio(a: Rect, b: Rect) -> float:
    x0 = max(a.x, b.x)
    y0 = max(a.y, b.y)
    x1 = min(a.x + a.w, b.x + b.w)
    y1 = min(a.y + a.h, b.y + b.h)
    if x1 <= x0 or y1 <= y0:
        return 0.0
    overlap = float((x1 - x0) * (y1 - y0))
    smaller = float(max(1, min(a.w * a.h, b.w * b.h)))
    return overlap / smaller


def _dedupe_detections(
    items: list[tuple[float, RuneshapeDetection]],
) -> tuple[RuneshapeDetection, ...]:
    kept: list[RuneshapeDetection] = []
    for _score, detection in sorted(
        items,
        key=lambda item: (
            -len(item[1].runes),
            -item[0],
            item[1].rect.y,
            item[1].rect.x,
        ),
    ):
        if any(_rect_overlap_ratio(detection.rect, other.rect) >= 0.45 for other in kept):
            continue
        kept.append(detection)
    return tuple(sorted(kept, key=lambda item: (round(item.rect.y / 32.0), item.rect.x)))


def detect_all(frame: np.ndarray) -> tuple[RuneshapeDetection, ...]:
    """Return every visible non-overlapping runeshape combination in ``frame``."""

    image, scale = _scale_frame(frame)
    # One HSV conversion feeds candidate generation and both mask variants;
    # the masks themselves are built only if rune-row candidates exist at
    # all, so frames without rune strips pay a single cheap pass.
    hsv = _hsv_planes(image)
    candidates = _rune_candidates(image, hsv=hsv)
    by_sequence = entries_by_sequence()
    prefixes_by_length = _sequence_prefixes_by_length()

    sequences: list[tuple[float, list[RuneCandidate], float, float]] = []
    for row in _row_groups(candidates):
        row_sequences = []
        for candidate_row in _candidate_rows(row, image.shape[1]):
            row_sequences.extend(
                (
                    _sequence_geometry_score(sequence, median_gap, median_size),
                    sequence,
                    median_gap,
                    median_size,
                )
                for sequence, median_gap, median_size in _regular_sequences(candidate_row)
            )
        row_buckets: dict[int, list[tuple[float, list[RuneCandidate], float, float]]] = {}
        for item in row_sequences:
            length = len(item[1])
            if length in prefixes_by_length:
                row_buckets.setdefault(length, []).append(item)
        for length in sorted(row_buckets, reverse=True):
            sequences.extend(_limit_ranked_geometry_sequences(row_buckets[length]))

    # Longer exact combinations are more specific and avoid false positives from
    # adjacent pairs inside a real 5-10 rune strip.
    buckets: dict[int, list[tuple[float, list[RuneCandidate], float, float]]] = {}
    for item in sequences:
        length = len(item[1])
        if length in prefixes_by_length:
            buckets.setdefault(length, []).append(item)
    sequences = []
    for length in sorted(buckets, reverse=True):
        ranked = sorted(buckets[length], key=lambda item: item[0], reverse=True)
        sequences.extend(ranked)

    if not sequences:
        return ()

    masked = _masked_value(image, hsv=hsv)
    masked_contextual: tuple[np.ndarray, np.ndarray] | None = None

    classify_cache: dict[tuple[int, int, int], RuneClassification | None] = {}
    contextual_cache: dict[tuple[int, int, int], tuple[RuneClassification, ...]] = {}
    detections: list[tuple[float, RuneshapeDetection]] = []

    # Strict slot classification is the dominant probe cost and every slot
    # is independent, so precompute the whole cache with a bounded pool
    # (cv2 releases the GIL).  Results are identical to on-demand
    # classification; sequences whose prefix would have failed early just
    # classify a few extra slots, which the parallelism more than repays.
    slot_jobs: dict[tuple[int, int, int], tuple[float, float, int]] = {}
    for _geometry, sequence, median_gap, median_size in sequences:
        slot_size = int(round(max(CLASSIFY_SIZE * 0.55, median_gap * 1.55, median_size * 2.0)))
        slot_size = min(max(34, slot_size), 220)
        center_y = float(np.median([candidate.y for candidate in sequence]))
        for candidate in sequence:
            key = (int(round(candidate.x)), int(round(center_y)), slot_size)
            if key not in slot_jobs:
                slot_jobs[key] = (candidate.x, center_y, slot_size)
    if len(slot_jobs) > 1 and CLASSIFY_WORKERS > 1:
        keys = list(slot_jobs)
        results = _pool().map(
            lambda key: _classify_slot(masked, *slot_jobs[key]),
            keys,
        )
        classify_cache.update(zip(keys, results))
    else:
        for key, (cx, cy, ss) in slot_jobs.items():
            classify_cache[key] = _classify_slot(masked, cx, cy, ss)

    for _geometry, sequence, median_gap, median_size in sequences:
        length = len(sequence)
        slot_size = int(round(max(CLASSIFY_SIZE * 0.55, median_gap * 1.55, median_size * 2.0)))
        slot_size = min(max(34, slot_size), 220)
        center_y = float(np.median([candidate.y for candidate in sequence]))
        classifications = []
        valid_prefixes = prefixes_by_length.get(length, set())
        strict_failed = False
        for candidate in sequence:
            key = (int(round(candidate.x)), int(round(center_y)), slot_size)
            if key not in classify_cache:
                classify_cache[key] = _classify_slot(
                    masked,
                    candidate.x,
                    center_y,
                    slot_size,
                )
            classified = classify_cache[key]
            if classified is None:
                strict_failed = True
                break
            if classified.score < MIN_RUNE_SCORE:
                strict_failed = True
                break
            classifications.append(classified)
            if tuple(c.rune for c in classifications) not in valid_prefixes:
                strict_failed = True
                break
        strict_accepted = False
        if not strict_failed and len(classifications) == length:
            avg_score = float(np.mean([c.score for c in classifications]))
            avg_margin = float(np.mean([c.margin for c in classifications]))
            strict_accepted = avg_score >= MIN_AVG_SCORE and avg_margin >= MIN_AVG_MARGIN
        if not strict_accepted:
            if masked_contextual is None:
                masked_contextual = (
                    masked,
                    _masked_value(image, include_neutral=False, hsv=hsv),
                )
            contextual = _contextual_classifications(
                sequence,
                center_y,
                slot_size,
                masked_contextual,
                valid_prefixes,
                by_sequence,
                contextual_cache,
            )
            if contextual is None:
                continue
            classifications = contextual
            avg_score = float(np.mean([c.score for c in classifications]))
            avg_margin = float(np.mean([c.margin for c in classifications]))
        runes = tuple(c.rune for c in classifications)
        entries = by_sequence.get(runes)
        if not entries:
            continue
        entry = _choose_entry(entries)
        confidence = 1.0 if strict_accepted and len(runes) >= 3 else 0.99
        detection = RuneshapeDetection(
            entry=entry,
            alternatives=entries,
            runes=runes,
            classifications=tuple(classifications),
            rect=_frame_rect(sequence, slot_size, scale, frame.shape),
            confidence=confidence,
            avg_score=avg_score,
            avg_margin=avg_margin,
        )
        score = _score_detection(classifications, len(runes))
        detections.append((score, detection))
    return _dedupe_detections(detections)


def detect(frame: np.ndarray) -> RuneshapeDetection | None:
    """Return the strongest visible runeshape combination in ``frame``."""

    detections = detect_all(frame)
    if not detections:
        return None
    return sorted(
        detections,
        key=lambda detection: (
            _detection_score(detection),
            len(detection.runes),
            -detection.rect.y,
            -detection.rect.x,
        ),
        reverse=True,
    )[0]
