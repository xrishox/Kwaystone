"""Fast, shared visual analysis for screen-scanner routing.

The expensive recognizers should only run after the visible panel type is
known.  This module intentionally uses scale-normalized OpenCV primitives and
does not know anything about prices or OCR models.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import cached_property
from time import perf_counter

import cv2
import numpy as np

from poed.image_geometry import Rect

ANALYSIS_MAX_WIDTH = 1920


@dataclass(frozen=True)
class LayoutEvidence:
    region: Rect
    confidence: float
    score: int
    details: tuple[str, ...]
    cell: float | None = None


@dataclass(frozen=True)
class _Line:
    position: float
    start: float
    end: float

    @property
    def length(self) -> float:
        return self.end - self.start


class SceneAnalysis:
    """Reusable image pyramid and layout evidence for one captured frame."""

    def __init__(self, frame: np.ndarray):
        self.frame = frame
        self.started_at = perf_counter()
        height, width = frame.shape[:2]
        self.scale = min(1.0, ANALYSIS_MAX_WIDTH / max(1, width))
        if self.scale < 1.0:
            self.image = cv2.resize(
                frame,
                None,
                fx=self.scale,
                fy=self.scale,
                interpolation=cv2.INTER_AREA,
            )
        else:
            self.image = frame
        self.preparation_elapsed = perf_counter() - self.started_at

    @cached_property
    def gray(self) -> np.ndarray:
        return cv2.cvtColor(self.image, cv2.COLOR_BGR2GRAY)

    @cached_property
    def edges(self) -> np.ndarray:
        return cv2.Canny(self.gray, 40, 100)

    def to_frame_rect(self, x0: float, y0: float, x1: float, y1: float) -> Rect:
        inv = 1.0 / self.scale
        height, width = self.frame.shape[:2]
        left = max(0, min(width, int(round(x0 * inv))))
        top = max(0, min(height, int(round(y0 * inv))))
        right = max(left + 1, min(width, int(round(x1 * inv))))
        bottom = max(top + 1, min(height, int(round(y1 * inv))))
        return Rect(left, top, right - left, bottom - top)

    @cached_property
    def have(self) -> LayoutEvidence | None:
        """Find the repeated dark cards used by the movable ``I HAVE`` panel."""
        mask = cv2.inRange(self.gray, 0, 45)
        mask = cv2.morphologyEx(
            mask,
            cv2.MORPH_CLOSE,
            cv2.getStructuringElement(cv2.MORPH_RECT, (5, 3)),
        )
        contours, _hierarchy = cv2.findContours(
            mask, cv2.RETR_LIST, cv2.CHAIN_APPROX_SIMPLE
        )
        cards: list[tuple[int, int, int, int]] = []
        for contour in contours:
            x, y, width, height = cv2.boundingRect(contour)
            aspect = width / max(1, height)
            if (
                80 <= width <= 360
                and 20 <= height <= 70
                and 3.2 <= aspect <= 8.0
                and width * height >= 1800
            ):
                cards.append((x, y, width, height))

        components = _card_components(cards)
        if not components or len(components[0]) < 8:
            return None
        component = components[0]
        widths = np.asarray([card[2] for card in component], dtype=np.float32)
        heights = np.asarray([card[3] for card in component], dtype=np.float32)
        median_width = float(np.median(widths))
        median_height = float(np.median(heights))
        x0 = min(card[0] for card in component)
        y0 = min(card[1] for card in component)
        x1 = max(card[0] + card[2] for card in component)
        y1 = max(card[1] + card[3] for card in component)

        # Include the title/search chrome and tolerate incomplete edge cards.
        x0 -= median_width * 0.35
        # Some card backgrounds are split by bright item art and therefore do
        # not survive the dark-mask contour pass. One extra card width keeps
        # the rightmost column without reverting to a screen-relative crop.
        x1 += median_width * 1.5
        y0 -= median_height * 2.2
        y1 += median_height * 2.2
        region = self.to_frame_rect(x0, y0, x1, y1)
        score = len(component)
        return LayoutEvidence(
            region=region,
            confidence=1.0,
            score=score,
            details=(
                f"repeated-dark-cards={score}",
                f"median-card={median_width:.0f}x{median_height:.0f}",
            ),
        )

    @cached_property
    def grid_candidates(self) -> tuple[LayoutEvidence, ...]:
        """Find dense, regularly spaced item grids independently of screen position."""
        height, width = self.image.shape[:2]
        lines = cv2.HoughLinesP(
            self.edges,
            1,
            np.pi / 180,
            threshold=50,
            minLineLength=max(80, int(height * 0.15)),
            maxLineGap=12,
        )
        if lines is None:
            return ()
        vertical: list[_Line] = []
        for x0, y0, x1, y1 in lines[:, 0]:
            if abs(int(x1) - int(x0)) > 3:
                continue
            top, bottom = sorted((float(y0), float(y1)))
            if bottom - top < height * 0.18:
                continue
            vertical.append(_Line((float(x0) + float(x1)) / 2.0, top, bottom))

        clustered = _cluster_lines(vertical)
        runs = _regular_runs(clustered)
        if not runs:
            runs = _regular_runs(clustered, refine_pitch=True)
        candidates = []
        for run, pitch in runs:
            selected = [
                min(clustered, key=lambda line: abs(line.position - position))
                for position in run
            ]
            top = float(np.median([line.start for line in selected]))
            bottom = float(np.median([line.end for line in selected]))
            if bottom - top < pitch * 6:
                continue

            # Item art can hide the first/last grid boundaries.  Extend a
            # bounded number of cells around the observed regular run.
            left = max(0.0, min(run) - pitch * 3.5)
            right = min(float(width), max(run) + pitch * 3.0)
            top = max(0.0, top - pitch * 0.15)
            bottom = min(float(height), bottom + pitch * 0.15)
            score = len(run)
            candidates.append(
                LayoutEvidence(
                    region=self.to_frame_rect(left, top, right, bottom),
                    confidence=1.0,
                    score=score,
                    details=(
                        f"regular-grid-lines={score}",
                        f"cell-pitch={pitch / self.scale:.1f}",
                    ),
                    cell=pitch / self.scale,
                )
            )
        return tuple(
            sorted(
                _deduplicate_layouts(candidates),
                key=lambda layout: (-layout.score, layout.region.x),
            )
        )

    @cached_property
    def ritual(self) -> LayoutEvidence | None:
        """Find the strongest dense grid independently of screen position."""
        return self.grid_candidates[0] if self.grid_candidates else None


def _card_components(
    cards: list[tuple[int, int, int, int]],
) -> list[list[tuple[int, int, int, int]]]:
    remaining = set(range(len(cards)))
    components: list[list[tuple[int, int, int, int]]] = []
    while remaining:
        pending = [remaining.pop()]
        component: list[tuple[int, int, int, int]] = []
        while pending:
            index = pending.pop()
            card = cards[index]
            component.append(card)
            linked = [
                other
                for other in remaining
                if _cards_near(card, cards[other])
            ]
            for other in linked:
                remaining.remove(other)
                pending.append(other)
        components.append(component)
    return sorted(components, key=len, reverse=True)


def _deduplicate_layouts(layouts: list[LayoutEvidence]) -> list[LayoutEvidence]:
    kept: list[LayoutEvidence] = []
    for layout in sorted(layouts, key=lambda item: (-item.score, item.region.x)):
        duplicate = False
        for existing in kept:
            overlap_x = max(
                0,
                min(
                    layout.region.x + layout.region.w,
                    existing.region.x + existing.region.w,
                )
                - max(layout.region.x, existing.region.x),
            )
            overlap_y = max(
                0,
                min(
                    layout.region.y + layout.region.h,
                    existing.region.y + existing.region.h,
                )
                - max(layout.region.y, existing.region.y),
            )
            overlap = overlap_x * overlap_y
            smaller = min(
                layout.region.w * layout.region.h,
                existing.region.w * existing.region.h,
            )
            if smaller > 0 and overlap / smaller > 0.65:
                duplicate = True
                break
        if not duplicate:
            kept.append(layout)
    return kept


def _cards_near(
    left: tuple[int, int, int, int],
    right: tuple[int, int, int, int],
) -> bool:
    lx, ly, lw, lh = left
    rx, ry, rw, rh = right
    if not (0.5 <= rw / lw <= 2.0 and 0.55 <= rh / lh <= 1.8):
        return False
    lcx, lcy = lx + lw / 2.0, ly + lh / 2.0
    rcx, rcy = rx + rw / 2.0, ry + rh / 2.0
    same_row = (
        abs(rcy - lcy) <= max(lh, rh) * 0.8
        and abs(rcx - lcx) <= max(lw, rw) * 4.0
    )
    same_column = (
        abs(rcx - lcx) <= max(lw, rw) * 1.2
        and abs(rcy - lcy) <= max(lh, rh) * 3.0
    )
    return same_row or same_column


def _cluster_lines(lines: list[_Line], tolerance: float = 4.0) -> list[_Line]:
    groups: list[list[_Line]] = []
    for line in sorted(lines, key=lambda item: item.position):
        if not groups or line.position - groups[-1][-1].position > tolerance:
            groups.append([line])
        else:
            groups[-1].append(line)
    out = []
    for group in groups:
        out.append(
            _Line(
                position=float(np.mean([line.position for line in group])),
                start=min(line.start for line in group),
                end=max(line.end for line in group),
            )
        )
    return out


def _regular_runs(
    lines: list[_Line], *, refine_pitch: bool = False
) -> list[tuple[list[float], float]]:
    positions = [line.position for line in lines]
    candidates: list[tuple[list[float], float]] = []
    for index, start in enumerate(positions):
        for second in positions[index + 1:]:
            pitch = second - start
            if not 20 <= pitch <= 90:
                continue
            hits: list[float] = []
            misses = 0
            # A scaled capture can make the true pitch non-integral (a
            # 101.5px grid at half scale is 50.75px); a fixed-pitch lattice
            # then drifts out of tolerance within a few columns. The
            # drift-corrected walk is a FALLBACK pass only: it slightly
            # perturbs run extents, so frames the fixed walk already
            # explains must keep their exact geometry.
            refined = float(pitch)
            for step in range(17):
                target = start + step * refined
                tolerance = max(4.0, refined * 0.08)
                nearby = [
                    position
                    for position in positions
                    if abs(position - target) <= tolerance
                ]
                if nearby:
                    hit = min(nearby, key=lambda value: abs(value - target))
                    hits.append(hit)
                    if refine_pitch and step >= 1:
                        refined = (hit - start) / step
                    misses = 0
                else:
                    misses += 1
                    if misses >= 2:
                        break
            if len(hits) >= 7:
                candidates.append((hits, refined if refine_pitch else pitch))

    out: list[tuple[list[float], float]] = []
    for run, pitch in sorted(candidates, key=lambda item: (-len(item[0]), item[0][0])):
        duplicate = False
        run_min = min(run)
        run_max = max(run)
        for existing, existing_pitch in out:
            existing_min = min(existing)
            existing_max = max(existing)
            same_pitch = abs(pitch - existing_pitch) <= max(2.0, pitch * 0.05)
            overlap = min(run_max, existing_max) - max(run_min, existing_min)
            if same_pitch and overlap >= min(
                run_max - run_min,
                existing_max - existing_min,
            ) * 0.65:
                duplicate = True
                break
        if not duplicate:
            out.append((run, pitch))
    return out
