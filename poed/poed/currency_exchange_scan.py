"""Fast, hotkey-specific reader for the visible Currency Exchange header."""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass

import cv2
import numpy as np

from poed import ocr_worker, scan_cache
from poed.image_geometry import Rect, frame_source

_LOG = logging.getLogger("waystone.currency_exchange_scan")
_ANALYSIS_WIDTH = 1600
_GOLD_LOW = (10, 20, 100)
_GOLD_HIGH = (45, 255, 255)
_TITLE_RE = re.compile(r"CURRENCYEXCHANGE")
_RATIO_RE = re.compile(r"(\d+(?:[.,]\d+)?)\s*[:/;|=]\s*(\d+(?:[.,]\d+)?)")
_CACHE_LIMIT = 32
_cache_lock = threading.Lock()
_read_cache: dict[bytes, tuple[str, float]] = {}


@dataclass(frozen=True)
class ExchangeRead:
    want_text: str
    have_text: str
    want_amount: float
    have_amount: float
    observed_at: int
    panel_side: str
    timings_ms: dict[str, float]


@dataclass(frozen=True)
class LiveExchangeRead:
    """Strict same-frame evidence used by the persistent arbitrage monitor."""

    want_text: str
    have_text: str
    want_score: float
    have_score: float
    want_amount: float
    have_amount: float
    ratio_score: float
    observed_at: int
    want_visual: bytes
    have_visual: bytes


def _elapsed(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 2)


def _gold_bands(image: np.ndarray) -> list[Rect]:
    hsv = cv2.cvtColor(image, cv2.COLOR_BGR2HSV)
    gold = cv2.inRange(hsv, _GOLD_LOW, _GOLD_HIGH)
    height, width = image.shape[:2]
    kernel_w = max(11, int(width * 0.014) | 1)
    gold = cv2.morphologyEx(
        gold,
        cv2.MORPH_CLOSE,
        cv2.getStructuringElement(cv2.MORPH_RECT, (kernel_w, 3)),
    )
    contours, _ = cv2.findContours(gold, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    bands: list[Rect] = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        if not (width * 0.012 <= w <= width * 0.14):
            continue
        if not (height * 0.008 <= h <= height * 0.035):
            continue
        if y >= height * 0.45 or w / max(1, h) < 2.0:
            continue
        bands.append(Rect(x, y, w, h))
    return bands


def _same_panel(first: Rect, second: Rect) -> bool:
    """Treat near-identical geometric proposals as one OCR candidate."""
    scale = max(first.w, second.w)
    return (
        abs(first.x - second.x) <= scale * 0.03
        and abs(first.y - second.y) <= scale * 0.03
        and abs(first.w - second.w) <= scale * 0.06
    )


def _exchange_panels(image: np.ndarray) -> list[Rect]:
    """Rank distinct three-label exchange-header candidates without OCR."""
    height, width = image.shape[:2]
    scale = min(1.0, _ANALYSIS_WIDTH / max(1, width))
    small = (
        cv2.resize(image, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        if scale < 1.0
        else image
    )
    sh, sw = small.shape[:2]
    bands = _gold_bands(small)
    candidates: list[tuple[float, Rect]] = []
    for middle in bands:
        mcx = middle.x + middle.w / 2.0
        mcy = middle.y + middle.h / 2.0
        if middle.w < sw * 0.03:
            continue
        left = []
        right = []
        for other in bands:
            if other is middle:
                continue
            ocx = other.x + other.w / 2.0
            ocy = other.y + other.h / 2.0
            dy = ocy - mcy
            if not (middle.h * 0.25 <= dy <= middle.h * 2.2):
                continue
            distance = abs(ocx - mcx)
            if not (middle.w * 1.25 <= distance <= middle.w * 2.7):
                continue
            (left if ocx < mcx else right).append(other)
        for lhs in left:
            for rhs in right:
                lcx = lhs.x + lhs.w / 2.0
                rcx = rhs.x + rhs.w / 2.0
                symmetry = abs((mcx - lcx) - (rcx - mcx)) / max(1.0, rcx - lcx)
                panel_w = (rcx - lcx) * 1.5
                if not (sw * 0.20 <= panel_w <= sw * 0.65):
                    continue
                panel_x = mcx - panel_w / 2.0
                panel_y = middle.y - panel_w * 0.073
                panel = Rect(
                    int(round(panel_x / scale)),
                    int(round(panel_y / scale)),
                    int(round(panel_w / scale)),
                    int(round(panel_w * 0.27 / scale)),
                ).clipped(width, height)
                if panel is None:
                    continue
                score = 1.0 - symmetry - abs((mcy - sh * 0.17) / max(1.0, sh)) * 0.15
                candidates.append((score, panel))
    panels: list[Rect] = []
    for _score, panel in sorted(candidates, key=lambda entry: entry[0], reverse=True):
        if not any(_same_panel(panel, existing) for existing in panels):
            panels.append(panel)
    return panels


def _exchange_panel(image: np.ndarray) -> Rect | None:
    panels = _exchange_panels(image)
    return panels[0] if panels else None


def _relative_crop(frame: np.ndarray, panel: Rect, box: tuple[float, float, float, float]) -> np.ndarray:
    x0, y0, x1, y1 = box
    left = int(round(panel.x + panel.w * x0))
    top = int(round(panel.y + panel.w * y0))
    right = int(round(panel.x + panel.w * x1))
    bottom = int(round(panel.y + panel.w * y1))
    rect = Rect(left, top, right - left, bottom - top).clipped(frame.shape[1], frame.shape[0])
    if rect is None:
        return frame[0:0, 0:0]
    return frame[rect.y : rect.y + rect.h, rect.x : rect.x + rect.w]


def _cached_recognitions(crops: list[np.ndarray]) -> list[tuple[str, float]]:
    recognized = [("", 0.0)] * len(crops)
    misses: list[int] = []
    keys: list[bytes | None] = []
    with _cache_lock:
        for index, crop in enumerate(crops):
            key = scan_cache.digest(crop) if crop.size else None
            keys.append(key)
            if key is not None and key in _read_cache:
                recognized[index] = _read_cache[key]
            else:
                misses.append(index)
    reads = ocr_worker.recognize_arrays([crops[index] for index in misses], timeout=20.0)
    with _cache_lock:
        for index, read in zip(misses, reads):
            text = str(read.get("text") or "").strip()
            score = float(read.get("score") or 0.0)
            recognized[index] = (text, score)
            key = keys[index]
            if key is not None:
                if len(_read_cache) >= _CACHE_LIMIT:
                    _read_cache.pop(next(iter(_read_cache)))
                _read_cache[key] = (text, score)
    return recognized


def _cached_reads(crops: list[np.ndarray]) -> list[str]:
    return [text for text, _score in _cached_recognitions(crops)]


def _parse_ratio(text: str) -> tuple[float, float]:
    normalized = text.translate(
        str.maketrans(
            {
                "O": "0",
                "o": "0",
                "I": "1",
                "l": "1",
                "!": "1",
                "：": ":",
                "；": ";",
                "｜": "|",
            }
        )
    )
    # Recognition can insert a space inside a multi-digit amount. Preserve
    # spaces between the two sides: without a visible separator, the ratio is
    # ambiguous and must still be rejected.
    normalized = re.sub(r"(?<=\d)\s+(?=\d(?=.*[:/;|=]))", "", normalized)
    match = _RATIO_RE.search(normalized)
    if not match:
        raise RuntimeError("could not read the Currency Exchange market ratio")
    want = float(match.group(1).replace(",", "."))
    have = float(match.group(2).replace(",", "."))
    if not (want > 0 and have > 0):
        raise RuntimeError("Currency Exchange market ratio is invalid")
    return want, have


def _ratio_fallback_crops(frame: np.ndarray, panel: Rect) -> list[np.ndarray]:
    """Generate robust recognition inputs only after the fast ratio read fails."""
    # Expand horizontally for long ratios but stay below MARKET RATIO. A taller
    # crop includes that label and recognition returns the label instead of the
    # numeric line.
    expanded = _relative_crop(frame, panel, (0.350, 0.085, 0.650, 0.135))
    if expanded.size == 0:
        return []
    enlarged = cv2.resize(expanded, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    _threshold, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    return [
        enlarged,
        cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR),
        cv2.cvtColor(255 - binary, cv2.COLOR_GRAY2BGR),
    ]


def _read_ratio(frame: np.ndarray, panel: Rect, primary_text: str) -> tuple[float, float]:
    try:
        return _parse_ratio(primary_text)
    except RuntimeError:
        fallback_texts = _cached_reads(_ratio_fallback_crops(frame, panel))
        _LOG.info(
            "currency exchange ratio fallback OCR: primary=%r fallback=%r",
            primary_text,
            fallback_texts,
        )
        for text in fallback_texts:
            try:
                return _parse_ratio(text)
            except RuntimeError:
                continue
    raise RuntimeError("could not read the Currency Exchange market ratio")


def _panel_crops(frame: np.ndarray, panel: Rect) -> list[np.ndarray]:
    return [
        _relative_crop(frame, panel, (0.33, 0.006, 0.67, 0.060)),
        _relative_crop(frame, panel, (0.075, 0.142, 0.335, 0.195)),
        _relative_crop(frame, panel, (0.690, 0.142, 0.950, 0.195)),
        _relative_crop(frame, panel, (0.380, 0.090, 0.620, 0.125)),
    ]


def _visual_signature(frame: np.ndarray, panel: Rect, side: str) -> bytes:
    box = (0.035, 0.125, 0.370, 0.215) if side == "want" else (
        0.630,
        0.125,
        0.965,
        0.215,
    )
    crop = _relative_crop(frame, panel, box)
    if crop.size == 0:
        return b""
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    normalized = cv2.resize(gray, (96, 24), interpolation=cv2.INTER_AREA)
    return normalized.tobytes()


def visual_distance(first: bytes, second: bytes) -> float:
    """Normalized visual distance between fixed-size live item signatures."""
    if not first or len(first) != len(second):
        return 1.0
    left = np.frombuffer(first, dtype=np.uint8).astype(np.int16)
    right = np.frombuffer(second, dtype=np.uint8).astype(np.int16)
    return float(np.mean(np.abs(left - right)) / 255.0)


def read_live_frame(frame: np.ndarray) -> LiveExchangeRead:
    """Read one frame without cross-candidate or single-pass ratio fallbacks.

    The live monitor needs evidence, not a best guess. Both ratio renderings
    come from the same localized panel and must independently parse to the
    same numerical value before the frame can update any market edge.
    """
    panels = _exchange_panels(frame)
    if not panels:
        raise RuntimeError("Currency Exchange is not visible")
    title_crops = [_panel_crops(frame, panel)[0] for panel in panels]
    titles = _cached_recognitions(title_crops)
    verified = [
        (panel, score)
        for panel, (text, score) in zip(panels, titles)
        if _title_matches(text) and score >= 0.70
    ]
    if len(verified) != 1:
        raise RuntimeError("Currency Exchange panel is ambiguous")
    panel, _title_score = verified[0]
    crops = _panel_crops(frame, panel)
    expanded = _relative_crop(frame, panel, (0.350, 0.085, 0.650, 0.135))
    if any(crop.size == 0 for crop in crops[1:]) or expanded.size == 0:
        raise RuntimeError("Currency Exchange header is clipped")
    enlarged = cv2.resize(expanded, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_CUBIC)
    gray = cv2.cvtColor(enlarged, cv2.COLOR_BGR2GRAY)
    _threshold, binary = cv2.threshold(
        gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU
    )
    fields = _cached_recognitions(
        [crops[1], crops[2], enlarged, cv2.cvtColor(binary, cv2.COLOR_GRAY2BGR)]
    )
    (want_text, want_score), (have_text, have_score), first, second = fields
    if want_score < 0.70 or have_score < 0.70 or not want_text or not have_text:
        raise RuntimeError("Currency Exchange item names are not reliable")
    if first[1] < 0.70 or second[1] < 0.70:
        raise RuntimeError("Currency Exchange market ratio confidence is too low")
    first_ratio = _parse_ratio(first[0])
    second_ratio = _parse_ratio(second[0])
    first_rate = first_ratio[0] / first_ratio[1]
    second_rate = second_ratio[0] / second_ratio[1]
    if abs(first_rate - second_rate) > max(first_rate, second_rate) * 0.002:
        raise RuntimeError("Currency Exchange market ratio reads disagree")
    return LiveExchangeRead(
        want_text=want_text,
        have_text=have_text,
        want_score=want_score,
        have_score=have_score,
        want_amount=first_ratio[0],
        have_amount=first_ratio[1],
        ratio_score=min(first[1], second[1]),
        observed_at=int(time.time() * 1000),
        want_visual=_visual_signature(frame, panel, "want"),
        have_visual=_visual_signature(frame, panel, "have"),
    )


def _title_matches(text: str) -> bool:
    return bool(_TITLE_RE.search(re.sub(r"[^A-Z]+", "", text.upper())))


def _finish_read(
    frame: np.ndarray,
    panel: Rect,
    texts: list[str],
    timings: dict[str, float],
) -> ExchangeRead:
    title, want_text, have_text, ratio_text = texts
    _LOG.info(
        "currency exchange OCR: panel=%s title=%r want=%r have=%r ratio=%r",
        panel,
        title,
        want_text,
        have_text,
        ratio_text,
    )
    if not _title_matches(title):
        raise RuntimeError("Currency Exchange title could not be verified")
    if not want_text or not have_text:
        raise RuntimeError("could not read both Currency Exchange items")
    want_amount, have_amount = _read_ratio(frame, panel, ratio_text)
    panel_side = "right" if panel.x + panel.w / 2.0 < frame.shape[1] / 2.0 else "left"
    _LOG.info(
        "currency exchange read: want=%r have=%r ratio=%s:%s side=%s timings=%s",
        want_text,
        have_text,
        want_amount,
        have_amount,
        panel_side,
        timings,
    )
    return ExchangeRead(
        want_text=want_text,
        have_text=have_text,
        want_amount=want_amount,
        have_amount=have_amount,
        observed_at=int(time.time() * 1000),
        panel_side=panel_side,
        timings_ms=timings,
    )


def read_frame(frame: np.ndarray) -> ExchangeRead:
    timings: dict[str, float] = {}
    started = time.perf_counter()
    panels = _exchange_panels(frame)
    timings["localize"] = _elapsed(started)
    if not panels:
        raise RuntimeError("Currency Exchange is not visible")

    # Preserve the one-batch hot path. Only pay for candidate verification when
    # unrelated gold UI happens to outrank the real exchange header.
    panel = panels[0]
    crops = _panel_crops(frame, panel)
    if any(crop.size == 0 for crop in crops):
        raise RuntimeError("Currency Exchange header is clipped")
    started = time.perf_counter()
    texts = _cached_reads(crops)
    timings["ocr"] = _elapsed(started)
    first_error: RuntimeError | None = None
    try:
        return _finish_read(frame, panel, texts, timings)
    except RuntimeError as error:
        first_error = error
        if len(panels) == 1:
            raise

    # Batch only the cheap title strips for every remaining proposal. Detailed
    # fields are recognized solely for candidates whose title verifies.
    remaining = panels[1:]
    title_crops = [_panel_crops(frame, candidate)[0] for candidate in remaining]
    started = time.perf_counter()
    titles = _cached_reads(title_crops)
    timings["candidate_titles"] = _elapsed(started)
    for index, (candidate, title) in enumerate(zip(remaining, titles), start=2):
        if not _title_matches(title):
            continue
        candidate_crops = _panel_crops(frame, candidate)
        if any(crop.size == 0 for crop in candidate_crops):
            continue
        started = time.perf_counter()
        fields = _cached_reads(candidate_crops[1:])
        timings["candidate_fields"] = _elapsed(started)
        try:
            result = _finish_read(frame, candidate, [title, *fields], timings)
        except RuntimeError:
            continue
        _LOG.info(
            "currency exchange selected verified candidate %d of %d",
            index,
            len(panels),
        )
        return result
    if first_error is not None:
        raise first_error
    raise RuntimeError("Currency Exchange could not be verified")


def capture(desktop) -> ExchangeRead:
    timings: dict[str, float] = {}
    started = time.perf_counter()
    output = desktop.active_game_output()
    if output is None:
        raise RuntimeError("no active game output found")
    shot = desktop.capture_output(output)
    timings["capture"] = _elapsed(started)
    if shot is None:
        raise RuntimeError("screen capture failed")
    game_rect = desktop.active_game_rect(output, (shot.shape[1], shot.shape[0]))
    frame, _x, _y, _source = frame_source(shot, game_rect)
    result = read_frame(frame)
    return ExchangeRead(
        want_text=result.want_text,
        have_text=result.have_text,
        want_amount=result.want_amount,
        have_amount=result.have_amount,
        observed_at=result.observed_at,
        panel_side=result.panel_side,
        timings_ms={**timings, **result.timings_ms},
    )
