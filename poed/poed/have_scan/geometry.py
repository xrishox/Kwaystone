"""Card geometry for the Have trade panel.

Cards are located directly from the panel image: dark card-background
contours seed column positions, then per-column border-line profiles give
precise card tops, including cards whose dark background is split by bright
item art.  All sizes derive from the observed modal card, so the grid works
at any resolution or panel position.
"""

from __future__ import annotations

from dataclasses import dataclass

import cv2
import numpy as np

CARD_ASPECT_MIN = 3.0
CARD_ASPECT_MAX = 9.0
# name-area width relative to card height for the classifier heuristics
FULL_CARD_ASPECT = 5.45


@dataclass(frozen=True)
class Card:
    """A full item card: icon square at the left, dark name area right."""

    x: int
    y: int
    w: int
    h: int
    synthesized: bool = False

    @property
    def icon_box(self) -> tuple[int, int, int, int]:
        return (self.x, self.y, self.h, self.h)

    @property
    def name_box(self) -> tuple[int, int, int, int]:
        return (self.x + self.h, self.y, self.w - self.h, self.h)


def _raw_rects(gray: np.ndarray) -> list[tuple[int, int, int, int]]:
    """Dark rects (cards, name areas, tabs, junk) from the panel crop."""
    mask = cv2.inRange(gray, 0, 45)
    scale = max(1.0, gray.shape[1] / 1024.0)
    kernel = cv2.getStructuringElement(
        cv2.MORPH_RECT, (max(3, int(5 * scale) | 1), max(3, int(3 * scale) | 1))
    )
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, kernel)
    contours, _ = cv2.findContours(mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE)
    rects = []
    for contour in contours:
        x, y, w, h = cv2.boundingRect(contour)
        aspect = w / max(1, h)
        if CARD_ASPECT_MIN <= aspect <= CARD_ASPECT_MAX and w * h >= 1800:
            rects.append((x, y, w, h))
    return rects


def _is_icon_tile(
    gray: np.ndarray, hsv: np.ndarray, x: int, y: int, side: int
) -> bool:
    """True when the square looks like an item-icon tile.

    Icon art varies from bright colorful crystals to nearly black idols, so
    the test accepts any of: colorful art, bright art, or a predominantly
    dark tile.  Panel stone matches none of these.
    """
    x0, y0 = max(0, x), max(0, y)
    x1, y1 = min(hsv.shape[1], x + side), min(hsv.shape[0], y + side)
    if x1 - x0 < side * 0.6 or y1 - y0 < side * 0.6:
        return False
    sat = hsv[y0:y1, x0:x1, 1]
    val = hsv[y0:y1, x0:x1, 2]
    sat_frac = float((sat > 80).mean())
    bright_frac = float((val > 120).mean())
    dark_frac = float((gray[y0:y1, x0:x1] <= 60).mean())
    colorful = sat_frac >= 0.10 and bright_frac >= 0.10
    pale = bright_frac >= 0.18
    dark_tile = dark_frac >= 0.35
    return colorful or pale or dark_tile


def _left_strip(
    gray: np.ndarray, hsv: np.ndarray, x: int, y: int, h: int
) -> tuple[float, float] | None:
    """(median gray, median saturation) of the strip just left of x."""
    x0, x1 = max(0, x - 10), max(0, x - 2)
    y0, y1 = max(0, y), min(gray.shape[0], y + h)
    if x1 <= x0 or y1 - y0 < h * 0.5:
        return None
    return (
        float(np.median(gray[y0:y1, x0:x1])),
        float(np.median(hsv[y0:y1, x0:x1, 1])),
    )


def _classify(
    gray: np.ndarray, hsv: np.ndarray, rect: tuple[int, int, int, int]
) -> Card | None:
    """Normalize a dark rect to a full-card box, or None for junk.

    A dark rect is either a full card (dim icon merged into the dark mask)
    or a name-only area whose bright icon art split the mask.  The strip
    just left of the rect decides: a card's outer edge abuts panel stone
    (mid gray, unsaturated) while a name area abuts its icon tile (dark
    tile or colorful art).
    """
    x, y, w, h = rect
    left = _left_strip(gray, hsv, x, y, h)
    if left is None:
        return None
    left_gray, left_sat = left
    icon_left = left_gray < 45 or left_sat > 80
    side = int(round(h * 1.08))
    if icon_left:
        return Card(x - side, y - (side - h) // 2, w + side, side)
    if _is_icon_tile(gray, hsv, x, y, h) and w / h >= FULL_CARD_ASPECT - 0.9:
        return Card(x, y, w, h)
    return None


def _cluster_1d(values: list[int], tolerance: float) -> list[float]:
    centers: list[list[int]] = []
    for value in sorted(values):
        if centers and value - centers[-1][-1] <= tolerance:
            centers[-1].append(value)
        else:
            centers.append([value])
    return [float(np.mean(group)) for group in centers]


def _largest_component(cards: list[Card], med_w: float, med_h: float) -> list[Card]:
    """Keep the biggest group of cards linked by row/column adjacency."""
    remaining = set(range(len(cards)))
    best: list[Card] = []
    while remaining:
        pending = [remaining.pop()]
        component = []
        while pending:
            index = pending.pop()
            card = cards[index]
            component.append(card)
            linked = [
                other
                for other in remaining
                if (
                    abs(cards[other].x - card.x) <= med_w * 1.6
                    and abs(cards[other].y - card.y) <= med_h * 3.2
                )
                or (
                    abs(cards[other].y - card.y) <= med_h * 0.8
                    and abs(cards[other].x - card.x) <= med_w * 2.6
                )
            ]
            for other in linked:
                remaining.remove(other)
                pending.append(other)
        if len(component) > len(best):
            best = component
    return best


class _CellProbe:
    """O(1) card-cell tests over the crop via integral images."""

    def __init__(self, gray: np.ndarray, hsv: np.ndarray):
        self.sat = cv2.integral((hsv[:, :, 1] > 80).astype(np.uint8))
        self.val = cv2.integral((hsv[:, :, 2] > 120).astype(np.uint8))
        self.dark = cv2.integral((gray <= 60).astype(np.uint8))
        self.height, self.width = gray.shape[:2]

    def _frac(self, integral: np.ndarray, x: int, y: int, w: int, h: int) -> float:
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(self.width, x + w), min(self.height, y + h)
        if x1 - x0 < w * 0.6 or y1 - y0 < h * 0.6:
            return 0.0
        total = (
            integral[y1, x1] - integral[y0, x1] - integral[y1, x0] + integral[y0, x0]
        )
        return float(total) / max(1, (x1 - x0) * (y1 - y0))

    def is_card(self, card: Card) -> bool:
        ix, iy, iw, ih = card.icon_box
        nx, ny, nw, nh = card.name_box
        sat_frac = self._frac(self.sat, ix, iy, iw, ih)
        bright_frac = self._frac(self.val, ix, iy, iw, ih)
        dark_frac = self._frac(self.dark, ix, iy, iw, ih)
        icon = (
            (sat_frac >= 0.10 and bright_frac >= 0.10)
            or bright_frac >= 0.18
            or dark_frac >= 0.35
        )
        return icon and self._frac(self.dark, nx, ny, nw, nh) >= 0.55

    def dark_name_area(self, card: Card) -> bool:
        nx, ny, nw, nh = card.name_box
        return self._frac(self.dark, nx, ny, nw, nh) >= 0.55


def _column_card_tops(
    gray: np.ndarray,
    col_x: float,
    card_h: int,
    card_w: int,
    known_tops: list[int] | None = None,
) -> list[int]:
    """Card-top y positions in one column strip.

    The card's top border crosses the icon strip at full strength while
    name-text lines barely register.  Bottom borders can rival top borders,
    so every peak must also have the dark card name-area immediately below
    it — a bottom border has panel stone there instead.
    """
    x0 = max(0, int(round(col_x)))
    x1 = min(gray.shape[1], x0 + card_h)
    if x1 - x0 < card_h * 0.5:
        return []
    strip = gray[:, x0:x1].astype(np.float32)
    grad = np.abs(strip[2:] - strip[:-2]).mean(axis=1)
    if grad.size == 0:
        return []
    name_x0 = min(gray.shape[1], x0 + card_h)
    name_x1 = min(gray.shape[1], x0 + card_w)
    name_rows = gray[:, name_x0:name_x1].astype(np.float32).mean(axis=1)
    # Border strength varies with panel lighting; when the column already
    # has detected cards, their measured top strengths calibrate the
    # threshold instead of an arbitrary fraction of the global maximum
    # (which panel chrome can dominate).
    if known_tops:
        strengths = [
            float(grad[max(0, min(grad.shape[0] - 1, top - 1))])
            for top in known_tops
        ]
        threshold = max(12.0, float(np.median(strengths)) * 0.55)
    else:
        threshold = max(25.0, float(grad.max()) * 0.5)
    peak_indices = np.where(grad > threshold)[0]

    def is_card_top(y: int) -> bool:
        # Below a card top lies the card interior, above it panel stone; a
        # bottom border has the opposite ordering.  Compare brightness on
        # both sides instead of using an absolute darkness threshold so the
        # test holds across panel lighting.
        margin = max(6, card_h // 12)
        lo = y + 4
        hi = min(name_rows.shape[0], y + 4 + margin)
        above_lo = max(0, y - 4 - margin)
        above_hi = max(0, y - 4)
        if hi <= lo or above_hi <= above_lo or name_x1 - name_x0 < card_h:
            return True
        return float(name_rows[lo:hi].mean()) + 5.0 < float(
            name_rows[above_lo:above_hi].mean()
        )

    # Only card tops compete in NMS: bottom borders rival tops in strength
    # and would otherwise suppress the true top of the row below them.  Art
    # edges inside a card fail the relative test too (card interior on both
    # sides), so they do not slip in when borders are removed.
    peak_indices = [p for p in peak_indices if is_card_top(int(p) + 1)]
    peak_indices = sorted(peak_indices, key=lambda p: -grad[p])
    peaks: list[int] = []
    for index in peak_indices:
        # Cards are one card-height tall, so two tops closer than that are
        # physically impossible; keep the stronger border.
        if all(abs(int(index) - p) > card_h * 0.9 for p in peaks):
            peaks.append(int(index))
    return sorted(p + 1 for p in peaks)


def _refine_column_x(
    gray: np.ndarray,
    col_x: float,
    row_ys: list[int],
    card_w: int,
    card_h: int,
) -> float:
    """Snap a column estimate to the cards' left border line.

    Column centers from clustering or pitch extrapolation can drift tens of
    pixels; the cards' shared left edge is a strong vertical gradient line
    across the column's rows and gives the exact position.
    """
    if not row_ys:
        return col_x
    span = max(8, int(card_w * 0.12))
    x0 = max(1, int(round(col_x)) - span)
    x1 = min(gray.shape[1] - 1, int(round(col_x)) + span)
    if x1 - x0 < 4:
        return col_x
    bands = []
    for row_y in row_ys:
        y0 = max(0, int(row_y) + 4)
        y1 = min(gray.shape[0], int(row_y) + card_h - 4)
        if y1 > y0:
            band = gray[y0:y1, x0 - 1 : x1 + 1].astype(np.float32)
            bands.append(np.abs(band[:, 2:] - band[:, :-2]).mean(axis=0))
    if not bands:
        return col_x
    profile = np.mean(bands, axis=0)
    peak = int(np.argmax(profile))
    if profile[peak] < 10.0:
        return col_x
    return float(x0 + peak)


def complete_grid(bgr: np.ndarray) -> tuple[list[Card], float, float]:
    """Detect, classify, and grid-complete the panel's item cards."""
    gray = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    hsv = cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)
    cards = [
        c for c in (_classify(gray, hsv, r) for r in _raw_rects(gray)) if c
    ]
    if not cards:
        return [], 0.0, 0.0

    med_w = float(np.median([c.w for c in cards]))
    med_h = float(np.median([c.h for c in cards]))
    cards = [
        c
        for c in cards
        if 0.8 * med_w <= c.w <= 1.2 * med_w and 0.8 * med_h <= c.h <= 1.2 * med_h
    ]
    if not cards:
        return [], 0.0, 0.0
    cards = _largest_component(cards, med_w, med_h)

    col_centers = _cluster_1d([c.x for c in cards], tolerance=med_w * 0.3)
    iw, ih = int(round(med_w)), int(round(med_h))
    probe = _CellProbe(gray, hsv)

    # Probe for whole columns the dark mask missed, at the observed pitch.
    if len(col_centers) >= 2:
        pitch = float(np.median(np.diff(col_centers)))
        row_seeds = sorted({c.y for c in cards})
        for direction in (-1, 1):
            edge = col_centers[0] if direction < 0 else col_centers[-1]
            while True:
                probe_x = edge + direction * pitch
                hits = sum(
                    probe.is_card(Card(int(round(probe_x)), int(round(ry)), iw, ih))
                    for ry in row_seeds
                )
                if hits < 2:
                    break
                col_centers.insert(0 if direction < 0 else len(col_centers), probe_x)
                edge = probe_x

    # Per-column card recovery: border-line profiles give precise card tops
    # for every column, including rows whose dark mask was split by art.
    out: list[Card] = []
    col_centers = [
        _refine_column_x(
            gray,
            col_x,
            [card.y for card in cards if abs(card.x - col_x) <= med_w * 0.3]
            or sorted({card.y for card in cards}),
            iw,
            ih,
        )
        for col_x in col_centers
    ]
    for col_x in col_centers:
        detected_in_col = [
            card for card in cards if abs(card.x - col_x) <= med_w * 0.3
        ]
        tops = _column_card_tops(
            gray, col_x, ih, iw, known_tops=[card.y for card in detected_in_col]
        )
        for top in tops:
            existing = next(
                (
                    card
                    for card in detected_in_col
                    if abs(card.y - top) <= med_h * 0.5
                ),
                None,
            )
            # Profile tops are precise; modal dimensions are stable.  Always
            # use normalized geometry — detected rects only vouch for
            # presence, their exact boxes can be fat or shifted.
            candidate = Card(
                int(round(col_x)), int(top), iw, ih, synthesized=existing is None
            )
            if existing is not None:
                out.append(candidate)
                detected_in_col.remove(existing)
                continue
            # The strong border line already vouches for a card here; junk
            # (search box, chrome) is cheap to carry and gets dropped by
            # catalog name matching.  Only require the dark name area so
            # pale or dim icons are not rejected.
            if probe.dark_name_area(candidate):
                out.append(candidate)
        # Detected cards whose top the profile missed still count.
        for card in detected_in_col:
            if all(abs(card.y - top) > med_h * 0.5 for top in tops):
                out.append(card)

    # Cross-column row propagation: a row seen in any column marks a grid
    # row; other columns may have a card there whose border was too weak
    # for the profile (top rows near panel chrome).  The cell probe keeps
    # empty grid positions out.
    row_centers = _cluster_1d(sorted(card.y for card in out), tolerance=med_h * 0.4)
    for col_x in col_centers:
        col_ys = [card.y for card in out if abs(card.x - col_x) <= med_w * 0.3]
        for row_y in row_centers:
            # A propagated card must not overlap an existing one: junk
            # columns (sidebar tabs) contribute row centers on their own
            # rhythm, and cards are one card-height tall.
            if any(abs(existing - row_y) < med_h * 0.9 for existing in col_ys):
                continue
            candidate = Card(int(round(col_x)), int(round(row_y)), iw, ih, True)
            if probe.is_card(candidate):
                out.append(candidate)
                col_ys.append(candidate.y)
    return out, med_w, med_h
