"""Identification-driven footprint cover.

Reward art overflows its cells (a sceptre's horns intrude into the neighbour
above), so boundary-local pixel evidence cannot partition occupied cells into
items. Identification itself is the partition authority: every legal footprint
hypothesis over the occupied set gets a cheap match plausibility, and a greedy
max-score exact cover picks the item layout. Backdrop tints never overflow, so
the occupied set remains the geometric ground the hypotheses stand on.
"""

from __future__ import annotations

import numpy as np

from .identify import Identifier
from .stages import Footprint, Lattice

QUICK_ACCEPT_FLOOR = 0.12
SINGLE_CELL_FALLBACK = 0.0
INTERNAL_CROSS_BONUS = 0.05
INTERNAL_GAP_PENALTY = 0.12
PROMOTE_ACCEPT = 0.66


def internal_boundary_term(
    frame: np.ndarray,
    lattice: Lattice,
    footprint: Footprint,
) -> float:
    """Per-cell score adjustment from the footprint's INTERNAL boundaries.

    One item's art crosses its own internal gridlines; two stacked lookalikes
    leave a clean gap there. Internal boundaries are immune to the
    neighbour-overflow problem that makes OUTER boundary evidence unusable.
    Returns a bonus when every internal boundary is crossed, a penalty when
    all of them are clean gaps, else 0."""
    if footprint.w == 1 and footprint.h == 1:
        return 0.0
    import cv2

    rect = footprint.rect(lattice)
    x0 = max(0, rect.x)
    y0 = max(0, rect.y)
    x1 = min(frame.shape[1], rect.x + rect.w)
    y1 = min(frame.shape[0], rect.y + rect.h)
    window = frame[y0:y1, x0:x1]
    if window.size == 0:
        return 0.0
    gray = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY).astype(np.float32)
    grad = cv2.magnitude(
        cv2.Scharr(gray, cv2.CV_32F, 1, 0), cv2.Scharr(gray, cv2.CV_32F, 0, 1)
    )
    threshold = float(np.quantile(grad, 0.75))
    if threshold <= 1e-3:
        return 0.0
    art = grad > threshold
    scale = (lattice.pitch_x + lattice.pitch_y) / (2.0 * 105.0)
    tight = max(3, int(round(scale * 4)))
    fractions = []
    for k in range(1, footprint.w):
        x = int(round(lattice.x0 + (footprint.col + k) * lattice.pitch_x)) - x0
        span0 = int(round(lattice.pitch_y * 0.15))
        left = art[span0:art.shape[0] - span0, max(0, x - tight - 2):max(0, x - 2)]
        right = art[span0:art.shape[0] - span0, x + 3:x + tight + 3]
        if left.size and right.size:
            fractions.append(float((left.any(axis=1) & right.any(axis=1)).mean()))
    for k in range(1, footprint.h):
        y = int(round(lattice.y0 + (footprint.row + k) * lattice.pitch_y)) - y0
        span0 = int(round(lattice.pitch_x * 0.15))
        top = art[max(0, y - tight - 2):max(0, y - 2), span0:art.shape[1] - span0]
        bottom = art[y + 3:y + tight + 3, span0:art.shape[1] - span0]
        if top.size and bottom.size:
            fractions.append(float((top.any(axis=0) & bottom.any(axis=0)).mean()))
    if not fractions:
        return 0.0
    if min(fractions) >= 0.10:
        return INTERNAL_CROSS_BONUS
    if max(fractions) <= 0.02:
        return -INTERNAL_GAP_PENALTY
    return 0.0


def _window(frame: np.ndarray, lattice: Lattice, footprint: Footprint, pad: int) -> np.ndarray:
    rect = footprint.rect(lattice)
    x0 = max(0, rect.x - pad)
    y0 = max(0, rect.y - pad)
    x1 = min(frame.shape[1], rect.x + rect.w + pad)
    y1 = min(frame.shape[0], rect.y + rect.h + pad)
    return frame[y0:y1, x0:x1]


CORE_BLOCK_FRACTION = 0.4


def identification_cover(
    frame: np.ndarray,
    lattice: Lattice,
    occupied: np.ndarray,
    legal: set[tuple[int, int]],
    identifier: Identifier,
    candidates: np.ndarray | None = None,
) -> list[Footprint]:
    """Cover the occupied core with legal footprints chosen by identification.

    `candidates` marks cells that may belong to an item without carrying art
    (tinted corners of sparse items); a hypothesis may span them but must be
    at least CORE_BLOCK_FRACTION core, and unclaimed candidates are dropped —
    only unclaimed CORE cells fall back to 1x1."""
    pitch = (lattice.pitch_x + lattice.pitch_y) / 2.0
    pad = int(round(pitch * 0.25))
    core = {
        (col, row)
        for row in range(occupied.shape[0])
        for col in range(occupied.shape[1])
        if occupied[row, col]
    }
    if not core:
        return []
    extended = set(core)
    if candidates is not None:
        extended |= {
            (col, row)
            for row in range(candidates.shape[0])
            for col in range(candidates.shape[1])
            if candidates[row, col]
        }

    proposals: list[Footprint] = []
    for col, row in extended:
        for w, h in legal:
            block = {(col + dc, row + dr) for dc in range(w) for dr in range(h)}
            if not block <= extended:
                continue
            core_cells = len(block & core)
            if core_cells < max(1, int(np.ceil(w * h * CORE_BLOCK_FRACTION))):
                continue
            proposals.append(Footprint(col, row, w, h))

    from poed import uniquescan

    def quick(footprint: Footprint) -> float:
        return identifier.quick_footprint_score(
            _window(frame, lattice, footprint, pad),
            (footprint.w, footprint.h),
            pitch,
        ) + internal_boundary_term(frame, lattice, footprint)

    scores = list(uniquescan._pool().map(quick, proposals))
    hypotheses = [
        (score, footprint)
        for score, footprint in zip(scores, proposals)
        if score >= QUICK_ACCEPT_FLOOR or (footprint.w, footprint.h) == (1, 1)
    ]
    chosen = _solve_cover(core, extended, hypotheses)
    return sorted(chosen, key=lambda f: (f.row, f.col))


WEAK_SCORE = 0.60


def refined_cover(
    frame: np.ndarray,
    lattice: Lattice,
    occupied: np.ndarray,
    legal: set[tuple[int, int]],
    identifier: Identifier,
    candidates: np.ndarray | None = None,
) -> tuple[list[Footprint], list[dict]]:
    """Two-tier cover: quick scores propose a partition, then components with
    weak outcomes (markers or low identification scores) are re-solved using
    FULL identification scores, which are decisive where quick scores are
    noisy. Returns final footprints and their identified matches."""
    from . import identify as identify_mod

    pitch = (lattice.pitch_x + lattice.pitch_y) / 2.0
    pad = int(round(pitch * identify_mod.PAD_SLOTS))
    full_cache: dict[Footprint, tuple[dict | None, float]] = {}

    def full_result(footprint: Footprint) -> tuple[dict | None, float]:
        cached = full_cache.get(footprint)
        if cached is None:
            rect = footprint.rect(lattice)
            x0 = max(0, rect.x - pad)
            y0 = max(0, rect.y - pad)
            x1 = min(frame.shape[1], rect.x + rect.w + pad)
            y1 = min(frame.shape[0], rect.y + rect.h + pad)
            window = frame[y0:y1, x0:x1]
            inset_x = int(round(rect.w * 0.08))
            inset_y = int(round(rect.h * 0.08))
            interior = frame[
                rect.y + inset_y:rect.y + rect.h - inset_y,
                rect.x + inset_x:rect.x + rect.w - inset_x,
            ]
            if window.size == 0 or interior.size == 0:
                cached = (None, -1.0)
            else:
                cached = identifier.identify_window(
                    window, (footprint.w, footprint.h), pitch, interior=interior
                )
            full_cache[footprint] = cached
        return cached

    # Identification-confirmed occupancy: a candidate cell where a full 1x1
    # identification ACCEPTS holds a real item and must be covered like core —
    # otherwise a spanning hypothesis is the only thing allowed to explain it
    # and wins by default.
    if candidates is not None and candidates.any():
        occupied = occupied.copy()
        promote = [
            Footprint(col, row, 1, 1)
            for row in range(candidates.shape[0])
            for col in range(candidates.shape[1])
            if candidates[row, col]
        ]
        from poed import uniquescan

        list(uniquescan._pool().map(full_result, promote))
        for footprint in promote:
            template, score = full_result(footprint)
            # Candidate cells carry a strong empty prior (gray-stone icons can
            # score ~0.6 against the bare ornament pattern), so promotion
            # demands more than the ordinary accept bar.
            if template is not None and score >= PROMOTE_ACCEPT:
                occupied[footprint.row, footprint.col] = True
                candidates[footprint.row, footprint.col] = False

    footprints = identification_cover(
        frame, lattice, occupied, legal, identifier, candidates=candidates
    )

    core = {
        (col, row)
        for row in range(occupied.shape[0])
        for col in range(occupied.shape[1])
        if occupied[row, col]
    }
    extended = set(core)
    if candidates is not None:
        extended |= {
            (col, row)
            for row in range(candidates.shape[0])
            for col in range(candidates.shape[1])
            if candidates[row, col]
        }

    weak_cells: set[tuple[int, int]] = set()
    for footprint in footprints:
        template, score = full_result(footprint)
        if template is None or score < WEAK_SCORE:
            weak_cells |= set(_block(footprint))

    if weak_cells:
        seen: set[tuple[int, int]] = set()
        for start in sorted(weak_cells, key=lambda cr: (cr[1], cr[0])):
            if start in seen:
                continue
            component = set()
            frontier = [start]
            while frontier:
                cell = frontier.pop()
                if cell in component or cell not in extended:
                    continue
                component.add(cell)
                col, row = cell
                frontier.extend(
                    [(col + 1, row), (col - 1, row), (col, row + 1), (col, row - 1)]
                )
            seen |= component
            comp_core = component & core
            if not comp_core:
                continue
            proposals: list[Footprint] = []
            for col, row in sorted(component, key=lambda cr: (cr[1], cr[0])):
                for w, h in legal:
                    block = {(col + dc, row + dr) for dc in range(w) for dr in range(h)}
                    if not block <= component:
                        continue
                    core_count = len(block & comp_core)
                    if core_count < max(1, int(np.ceil(w * h * CORE_BLOCK_FRACTION))):
                        continue
                    proposals.append(Footprint(col, row, w, h))
            from poed import uniquescan

            list(uniquescan._pool().map(full_result, proposals))
            scored = [
                (
                    max(full_result(footprint)[1], 0.0)
                    + internal_boundary_term(frame, lattice, footprint),
                    footprint,
                )
                for footprint in proposals
            ]
            resolved = _solve_cover(comp_core, component, scored)
            footprints = [
                fp for fp in footprints if not (set(_block(fp)) & component)
            ] + resolved

    footprints = sorted(footprints, key=lambda f: (f.row, f.col))
    matches: list[dict] = []
    for footprint in footprints:
        rect = footprint.rect(lattice)
        template, score = full_result(footprint)
        if template is not None:
            matches.append(identify_mod.template_match(template, score, rect))
            continue
        if (
            footprint.w == 1
            and footprint.h == 1
            and score < identify_mod.MARKER_MIN_SCORE
        ):
            inset = max(2, int(round(rect.w * 0.08)))
            interior = frame[
                rect.y + inset:rect.y + rect.h - inset,
                rect.x + inset:rect.x + rect.w - inset,
            ]
            if (
                interior.size == 0
                or identify_mod._navy_fraction(interior) < identify_mod.MARKER_MIN_NAVY
            ):
                continue
        matches.append(identify_mod.marker_match(rect))
    return footprints, matches


UNCOVERED_PENALTY = 0.35
BRANCH_LIMIT = 6
NODE_LIMIT = 25000


def _block(footprint: Footprint) -> frozenset[tuple[int, int]]:
    return frozenset(
        (footprint.col + dc, footprint.row + dr)
        for dc in range(footprint.w)
        for dr in range(footprint.h)
    )


def _greedy(core, extended, hypotheses):
    ordered = sorted(
        hypotheses, key=lambda item: -(item[0] + 0.015 * (item[1].w * item[1].h - 1))
    )
    remaining = set(extended)
    chosen = []
    for score, footprint in ordered:
        block = _block(footprint)
        if block <= remaining:
            chosen.append(footprint)
            remaining -= block
    for col, row in sorted(remaining & core, key=lambda cr: (cr[1], cr[0])):
        chosen.append(Footprint(col, row, 1, 1))
    return chosen


def _solve_cover(
    core: set[tuple[int, int]],
    extended: set[tuple[int, int]],
    hypotheses: list[tuple[float, Footprint]],
) -> list[Footprint]:
    """Max-total-score exact cover of the core cells (branch and bound).

    Greedy claims can block the true layout (a 2x1 stealing one cell of a
    1x4 staff), so each connected component is solved globally: every core
    cell is covered by exactly one footprint or pays UNCOVERED_PENALTY, and
    candidate cells are optional. Components beyond the node budget fall back
    to greedy."""
    by_cell: dict[tuple[int, int], list[tuple[float, Footprint]]] = {}
    for score, footprint in hypotheses:
        for cell in _block(footprint):
            by_cell.setdefault(cell, []).append((score, footprint))
    for cell in by_cell:
        by_cell[cell].sort(key=lambda item: -item[0])

    seen: set[tuple[int, int]] = set()
    out: list[Footprint] = []
    for start in sorted(extended, key=lambda cr: (cr[1], cr[0])):
        if start in seen:
            continue
        component = set()
        frontier = [start]
        while frontier:
            cell = frontier.pop()
            if cell in component or cell not in extended:
                continue
            component.add(cell)
            col, row = cell
            frontier.extend([(col + 1, row), (col - 1, row), (col, row + 1), (col, row - 1)])
        seen |= component
        comp_core = component & core
        if not comp_core:
            continue

        best: tuple[float, list[Footprint]] = (
            -UNCOVERED_PENALTY * len(comp_core) - 1e-6,
            [],
        )
        nodes = 0

        def dfs(remaining_core: set, used: set, total: float, picked: list[Footprint]):
            nonlocal best, nodes
            nodes += 1
            if nodes > NODE_LIMIT:
                return
            if not remaining_core:
                if total > best[0]:
                    best = (total, list(picked))
                return
            if total + 1.0 * len(remaining_core) <= best[0]:
                return
            cell = min(remaining_core, key=lambda cr: (cr[1], cr[0]))
            options = [
                (score, footprint)
                for score, footprint in by_cell.get(cell, [])
                if _block(footprint) & used == set()
                and _block(footprint) <= component
            ][:BRANCH_LIMIT]
            for score, footprint in options:
                block = _block(footprint)
                # Candidate (non-core) cells carry reduced evidence weight so
                # a spanning hypothesis cannot buy area from possibly-empty
                # cells; sparse items still win because their tinted corners
                # add weight while fragments pay per-footprint costs.
                effective = len(block & core) + 0.3 * len(block - core)
                picked.append(footprint)
                dfs(
                    remaining_core - block,
                    used | block,
                    total + score * effective - 0.05,
                    picked,
                )
                picked.pop()
            dfs(
                remaining_core - {cell},
                used | {cell},
                total - UNCOVERED_PENALTY,
                picked,
            )

        dfs(set(comp_core), set(), 0.0, [])
        if nodes > NODE_LIMIT:
            out.extend(
                fp
                for fp in _greedy(comp_core, component, hypotheses)
                if _block(fp) <= component
            )
            continue
        covered = set()
        for footprint in best[1]:
            covered |= _block(footprint)
        out.extend(best[1])
        for col, row in sorted(comp_core - covered, key=lambda cr: (cr[1], cr[0])):
            out.append(Footprint(col, row, 1, 1))
    return out
