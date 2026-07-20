"""Shared footprint identification with masked ZNCC verification.

The Favours grid background is translucent, so unmasked template correlation
is poisoned by whatever scene shows through around the item art. Matching
therefore scores only art pixels: template masks are recovered from the
corpus loader's flat 12-gray flattening, and both gray and color scores are
mean-centered within the mask (plain masked CCORR was rejected in the
original concept test because absent items scored into the present band —
mean-centering is what prevents that).

The measured lattice pitch gives the exact shot scale (one slot -> 47 px), so
there is no scale-factor guessing anywhere.
"""

from __future__ import annotations

import cv2
import numpy as np

from poed import scan_cache, uniquescan
from poed.match_fields import match_row_fields

MARKER_NAME = "Unrecognized reward"
PAD_SLOTS = 0.25
PREFILTER_1X1 = 64
PREFILTER_MULTI = 25
COARSE_KEEP = 10
# 1x1 icons form dense lookalike families (omens, talismans); the coarse cut
# must keep enough of them for full verification to arbitrate.
COARSE_KEEP_1X1 = 18
RESCUE_THRESHOLD = 0.62
ACCEPT_COLOR = 0.50
ACCEPT_COLOR_1X1 = 0.52
# Small margins mark the match ambiguous instead of rejecting it: omen
# variants share coin art and a marker is strictly worse than the best-scoring
# name (the production pipeline does the same).
AMBIGUOUS_MARGIN = 0.04
MASK_BG_DELTA = 14


def _template_mask(template: dict) -> np.ndarray:
    mask = template.get("_lab_mask")
    if mask is None:
        color = template["color"].astype(np.int16)
        mask = np.abs(color - 12).max(axis=2) > MASK_BG_DELTA
        mask = cv2.erode(
            mask.astype(np.uint8), np.ones((3, 3), np.uint8), iterations=1
        ).astype(bool)
        if mask.sum() < 60:
            mask = np.ones(template["gray"].shape, dtype=bool)
        template["_lab_mask"] = mask
    return mask


def _masked_vectors(template: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cached = template.get("_lab_masked")
    if cached is None:
        mask = _template_mask(template)
        ys, xs = np.nonzero(mask)
        vec = template["gray"][ys, xs].astype(np.float32)
        vec -= vec.mean()
        norm = float(np.linalg.norm(vec))
        vec = vec / norm if norm > 1e-6 else vec
        cached = (ys, xs, vec)
        template["_lab_masked"] = cached
    return cached


def _masked_half_vectors(template: dict) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    cached = template.get("_lab_masked_half")
    if cached is None:
        mask = _template_mask(template).astype(np.uint8)
        half = cv2.resize(
            mask, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_NEAREST
        ).astype(bool)
        gray_half = template["gray_half"]
        hh = min(half.shape[0], gray_half.shape[0])
        hw = min(half.shape[1], gray_half.shape[1])
        ys, xs = np.nonzero(half[:hh, :hw])
        vec = gray_half[ys, xs].astype(np.float32)
        vec -= vec.mean()
        norm = float(np.linalg.norm(vec))
        vec = vec / norm if norm > 1e-6 else vec
        cached = (ys, xs, vec)
        template["_lab_masked_half"] = cached
    return cached


def _masked_zncc_best(
    window: np.ndarray,
    ys: np.ndarray,
    xs: np.ndarray,
    vec: np.ndarray,
    tpl_h: int,
    tpl_w: int,
    stride: int,
    center: tuple[int, int] | None = None,
    radius: int | None = None,
) -> tuple[float, tuple[int, int]]:
    height, width = window.shape[:2]
    max_dy = height - tpl_h
    max_dx = width - tpl_w
    if max_dy < 0 or max_dx < 0 or len(vec) < 8:
        return -1.0, (0, 0)
    if center is not None and radius is not None:
        dys = np.arange(
            max(0, center[0] - radius), min(max_dy, center[0] + radius) + 1, stride
        )
        dxs = np.arange(
            max(0, center[1] - radius), min(max_dx, center[1] + radius) + 1, stride
        )
    else:
        dys = np.arange(0, max_dy + 1, stride)
        dxs = np.arange(0, max_dx + 1, stride)
    if len(dys) == 0 or len(dxs) == 0:
        return -1.0, (0, 0)
    flat = np.ascontiguousarray(window, dtype=np.float32).reshape(-1)
    base = (ys.astype(np.int64) * window.shape[1] + xs.astype(np.int64))[None, :]
    offsets = (
        dys.astype(np.int64)[:, None] * window.shape[1] + dxs.astype(np.int64)[None, :]
    ).reshape(-1)[:, None]
    samples = flat[base + offsets]
    samples -= samples.mean(axis=1, keepdims=True)
    norms = np.linalg.norm(samples, axis=1)
    scores = samples @ vec
    with np.errstate(divide="ignore", invalid="ignore"):
        scores = np.where(norms > 1e-6, scores / norms, -1.0)
    index = int(np.argmax(scores))
    dy = int(dys[index // len(dxs)])
    dx = int(dxs[index % len(dxs)])
    return float(scores[index]), (dy, dx)


DISCRIMINATE_MARGIN = 0.06
DISCRIMINATE_DIFF = 24
DISCRIMINATE_MIN_PIXELS = 40


def _discriminate(
    scaled: np.ndarray,
    tpl_a: dict,
    off_a: tuple[int, int],
    tpl_b: dict,
    off_b: tuple[int, int],
) -> dict:
    """Lookalike tiebreak: rescore two close candidates on ONLY the pixels
    where their templates differ, so a shared golden ring (omen coins) cannot
    drown the discriminating center art. Returns the winning template."""
    if tpl_a["color"].shape != tpl_b["color"].shape:
        return tpl_a
    both = _template_mask(tpl_a) & _template_mask(tpl_b)
    diff = np.abs(
        tpl_a["color"].astype(np.int16) - tpl_b["color"].astype(np.int16)
    ).max(axis=2)
    dmask = both & (diff > DISCRIMINATE_DIFF)
    if int(dmask.sum()) < DISCRIMINATE_MIN_PIXELS:
        return tpl_a
    ys, xs = np.nonzero(dmask)

    def diff_score(template: dict, offset: tuple[int, int]) -> float:
        dy, dx = offset
        if (
            dy + template["color"].shape[0] > scaled.shape[0]
            or dx + template["color"].shape[1] > scaled.shape[1]
            or dy < 0
            or dx < 0
        ):
            return -1.0
        tvec = template["color"][ys, xs, :].astype(np.float32).reshape(-1)
        tvec -= tvec.mean()
        tnorm = float(np.linalg.norm(tvec))
        wvec = scaled[ys + dy, xs + dx, :].astype(np.float32).reshape(-1)
        wvec -= wvec.mean()
        wnorm = float(np.linalg.norm(wvec))
        if tnorm <= 1e-6 or wnorm <= 1e-6:
            return -1.0
        return float(np.dot(tvec, wvec) / (tnorm * wnorm))

    return tpl_a if diff_score(tpl_a, off_a) >= diff_score(tpl_b, off_b) else tpl_b


def _masked_color_score(
    window: np.ndarray,
    template: dict,
    offset: tuple[int, int],
) -> float:
    mask = _template_mask(template)
    ys, xs = np.nonzero(mask)
    dy, dx = offset
    height, width = window.shape[:2]
    if (
        dy + template["color"].shape[0] > height
        or dx + template["color"].shape[1] > width
    ):
        return -1.0
    # One joint vector across channels: per-channel normalization would cancel
    # the hue differences that separate same-shaped variants (omen coins).
    tvec = template["color"][ys, xs, :].astype(np.float32).reshape(-1)
    tvec -= tvec.mean()
    tnorm = float(np.linalg.norm(tvec))
    wvec = window[ys + dy, xs + dx, :].astype(np.float32).reshape(-1)
    wvec -= wvec.mean()
    wnorm = float(np.linalg.norm(wvec))
    if tnorm <= 1e-6 or wnorm <= 1e-6:
        return -1.0
    return float(np.dot(tvec, wvec) / (tnorm * wnorm))


class Identifier:
    def __init__(self, rows: dict):
        eligible = uniquescan.filter_ritual_rows(uniquescan.filter_rows(rows, 0.0))
        self.templates = uniquescan._load_corpus(eligible)
        self.groups: dict[tuple[int, int], list[dict]] = {}
        self.by_label: dict[str, dict] = {}
        for template in self.templates:
            key = (int(template["slots_w"]), int(template["slots_h"]))
            self.groups.setdefault(key, []).append(template)
            self.by_label[template["label"]] = template
        self._descriptor_matrices: dict[tuple[int, int], np.ndarray] = {}
        self._half_prefilter: tuple | None = None
        self._families: dict[int, list[dict]] | None = None

    def _descriptor_matrix(self, key: tuple[int, int]) -> np.ndarray:
        matrix = self._descriptor_matrices.get(key)
        if matrix is None:
            group = self.groups[key]
            matrix = np.stack([template["descriptor"] for template in group])
            self._descriptor_matrices[key] = matrix
        return matrix

    def _half_prefilter_matrix(self) -> tuple[np.ndarray, list[dict], tuple[int, int]]:
        """Dense masked-centered half-res template matrix for 1x1 ranking.

        The 18x18 visual descriptor was measured to EXCLUDE correct omens from
        its top-40 (backdrop + stack numerals swamp it); ranking by actual
        masked correlation at half resolution is faithful and still one matmul
        per window offset."""
        if self._half_prefilter is None:
            group = self.groups.get((1, 1), [])
            shapes: dict[tuple[int, int], list[dict]] = {}
            for template in group:
                shapes.setdefault(template["gray_half"].shape, []).append(template)
            shape, members = max(shapes.items(), key=lambda kv: len(kv[1]))
            extras = [t for s, ts in shapes.items() if s != shape for t in ts]
            length = shape[0] * shape[1]
            matrix = np.zeros((len(members), length), dtype=np.float32)
            correction = np.ones(len(members), dtype=np.float32)
            for index, template in enumerate(members):
                ys, xs, vec = _masked_half_vectors(template)
                matrix[index, ys * shape[1] + xs] = vec
                # Window patches are normalized by their FULL-patch energy but
                # each template only scores within its mask; compact-mask
                # templates (omen coins) would be systematically suppressed
                # without this energy-fraction correction.
                correction[index] = float(np.sqrt(length / max(1, len(vec))))
            self._half_prefilter = (matrix, members, shape, extras, correction)
        return self._half_prefilter

    FAMILY_SIMILARITY = 0.55
    FAMILY_CAP = 60

    def family_of(self, template: dict) -> list[dict]:
        """Lookalike family: 1x1 templates whose masked half-res art
        correlates strongly with this one (omen coins, essence bottles...).
        Whenever a family member wins, the whole family must be verified —
        prefilters cannot rank within a family."""
        if self._families is None:
            matrix, members, _shape, _extras, _correction = self._half_prefilter_matrix()
            similarity = matrix @ matrix.T
            families: dict[int, list[dict]] = {}
            for index in range(len(members)):
                close = np.nonzero(similarity[index] >= self.FAMILY_SIMILARITY)[0]
                if len(close) > 1:
                    families[id(members[index])] = [
                        members[int(j)] for j in close[: self.FAMILY_CAP]
                    ]
            self._families = families
        return self._families.get(id(template), [])

    def rank_1x1(
        self,
        gray_half: np.ndarray,
        limit: int,
    ) -> list[tuple[float, dict, tuple[int, int]]]:
        """Rank all 1x1 templates against a half-res window in one matmul.

        Returns (approximate score, template, best half-res offset)."""
        matrix, members, shape, extras, correction = self._half_prefilter_matrix()
        th, tw = shape
        max_dy = gray_half.shape[0] - th
        max_dx = gray_half.shape[1] - tw
        if max_dy < 0 or max_dx < 0 or not members:
            return []
        dys = np.arange(0, max_dy + 1, 2)
        dxs = np.arange(0, max_dx + 1, 2)
        patches = np.empty((len(dys) * len(dxs), th * tw), dtype=np.float32)
        offsets = []
        index = 0
        for dy in dys:
            for dx in dxs:
                patches[index] = gray_half[dy:dy + th, dx:dx + tw].reshape(-1)
                offsets.append((int(dy), int(dx)))
                index += 1
        patches -= patches.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(patches, axis=1)
        norms[norms <= 1e-6] = np.inf
        patches /= norms[:, None]
        scores = matrix @ patches.T
        best_offset_index = np.argmax(scores, axis=1)
        best = scores[np.arange(len(members)), best_offset_index] * correction
        scored = [
            (float(best[i]), members[i], offsets[int(best_offset_index[i])])
            for i in range(len(members))
        ]
        for template in extras:
            ys, xs, vec = _masked_half_vectors(template)
            score, offset = _masked_zncc_best(
                gray_half, ys, xs, vec,
                template["gray_half"].shape[0], template["gray_half"].shape[1],
                stride=2,
            )
            scored.append((score, template, offset))
        scored.sort(key=lambda item: -item[0])
        return scored[:limit]

    def quick_footprint_score(
        self,
        window: np.ndarray,
        footprint_wh: tuple[int, int],
        pitch: float,
        top: int = 6,
    ) -> float:
        """Cheap plausibility that `window` contains an item of this footprint:
        best half-res masked ZNCC over the descriptor short-list."""
        group = self.groups.get(footprint_wh)
        if not group:
            return -1.0
        scale = uniquescan.PXSLOT / max(pitch, 1e-6)
        scaled = cv2.resize(window, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        gray_half = cv2.resize(
            cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY),
            None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA,
        )
        if footprint_wh == (1, 1) and len(group) > top:
            ranked = self.rank_1x1(gray_half, 3)
            best = -1.0
            for _, template, offset in ranked:
                ys, xs, vec = _masked_half_vectors(template)
                score, _ = _masked_zncc_best(
                    gray_half,
                    ys,
                    xs,
                    vec,
                    template["gray_half"].shape[0],
                    template["gray_half"].shape[1],
                    stride=1,
                    center=offset,
                    radius=2,
                )
                best = max(best, score)
            return best
        if len(group) > top:
            descriptor = uniquescan._visual_descriptor(scaled)
            matrix = self._descriptor_matrix(footprint_wh)
            ranked_idx = np.argsort(matrix @ descriptor)[::-1][:top]
            candidates = [group[int(index)] for index in ranked_idx]
        else:
            candidates = group
        best = -1.0
        for template in candidates:
            ys, xs, vec = _masked_half_vectors(template)
            th, tw = template["gray_half"].shape[:2]
            center = ((gray_half.shape[0] - th) // 2, (gray_half.shape[1] - tw) // 2)
            score, _ = _masked_zncc_best(
                gray_half,
                ys,
                xs,
                vec,
                th,
                tw,
                stride=2,
                center=(max(0, center[0]), max(0, center[1])),
                radius=6,
            )
            best = max(best, score)
        return best

    def identify_window(
        self,
        window: np.ndarray,
        footprint_wh: tuple[int, int],
        pitch: float,
        interior: np.ndarray | None = None,
    ) -> tuple[dict | None, float]:
        group = self.groups.get(footprint_wh)
        if not group:
            return None, 0.0
        # Cross-press reuse keyed by the exact pixels the identification
        # consumed; only identity is cached — prices are rebuilt from current
        # rows by the caller (two-generation rotation in poed.scan_cache).
        cache_key = scan_cache.digest(
            np.ascontiguousarray(window),
            extra=f"ritual2:{footprint_wh[0]}x{footprint_wh[1]}:{pitch:.2f}",
        )
        cached = scan_cache.lookup("ritual-cell-v2", cache_key)
        if cached is not None:
            label, score, ambiguous = cached
            if label is None:
                return None, score
            template = self.by_label.get(label)
            if template is not None:
                return (dict(template, ambiguous=True) if ambiguous else template), score
        template, score = self._identify_uncached(window, footprint_wh, pitch, interior)
        scan_cache.store(
            "ritual-cell-v2",
            cache_key,
            (
                template["label"] if template is not None else None,
                score,
                bool(template.get("ambiguous")) if template is not None else False,
            ),
        )
        return template, score

    def _identify_uncached(
        self,
        window: np.ndarray,
        footprint_wh: tuple[int, int],
        pitch: float,
        interior: np.ndarray | None = None,
    ) -> tuple[dict | None, float]:
        group = self.groups.get(footprint_wh)
        scale = uniquescan.PXSLOT / max(pitch, 1e-6)
        scaled = cv2.resize(window, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(scaled, cv2.COLOR_BGR2GRAY)
        gray_half = cv2.resize(gray, None, fx=0.5, fy=0.5, interpolation=cv2.INTER_AREA)

        limit = PREFILTER_1X1 if footprint_wh == (1, 1) else PREFILTER_MULTI
        if footprint_wh == (1, 1) and len(group) > limit:
            # Union of two prefilters with complementary blind spots: masked
            # half-res correlation ranking (shape-faithful, offsets included)
            # and the interior visual descriptor — verification arbitrates.
            coarse = list(self.rank_1x1(gray_half, COARSE_KEEP_1X1))
            probe = interior if interior is not None else window
            descriptor = uniquescan._visual_descriptor(
                cv2.resize(probe, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
                if probe is not window
                else scaled
            )
            matrix = self._descriptor_matrix(footprint_wh)
            ranked = np.argsort(matrix @ descriptor)[::-1][:20]
            known = {id(template) for _, template, _ in coarse}
            for index in ranked:
                template = group[int(index)]
                if id(template) in known:
                    continue
                ys, xs, vec = _masked_half_vectors(template)
                score, offset = _masked_zncc_best(
                    gray_half,
                    ys,
                    xs,
                    vec,
                    template["gray_half"].shape[0],
                    template["gray_half"].shape[1],
                    stride=2,
                )
                if score > 0:
                    coarse.append((score, template, offset))
        else:
            if len(group) > limit:
                probe = interior if interior is not None else window
                descriptor = uniquescan._visual_descriptor(
                    cv2.resize(
                        probe, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA
                    )
                    if probe is not window
                    else scaled
                )
                matrix = self._descriptor_matrix(footprint_wh)
                ranked = np.argsort(matrix @ descriptor)[::-1][:limit]
                candidates = [group[int(index)] for index in ranked]
            else:
                candidates = group
            coarse = []
            for template in candidates:
                ys, xs, vec = _masked_half_vectors(template)
                score, offset = _masked_zncc_best(
                    gray_half,
                    ys,
                    xs,
                    vec,
                    template["gray_half"].shape[0],
                    template["gray_half"].shape[1],
                    stride=2,
                )
                if score > 0:
                    coarse.append((score, template, offset))
        coarse.sort(key=lambda item: -item[0])

        keep = COARSE_KEEP_1X1 if footprint_wh == (1, 1) else COARSE_KEEP
        finalists: list[tuple[float, dict, tuple[int, int]]] = []
        for _, template, half_offset in coarse[:keep]:
            ys, xs, vec = _masked_vectors(template)
            gray_score, offset = _masked_zncc_best(
                gray,
                ys,
                xs,
                vec,
                template["gray"].shape[0],
                template["gray"].shape[1],
                stride=1,
                center=(half_offset[0] * 2, half_offset[1] * 2),
                radius=4,
            )
            if gray_score <= 0:
                continue
            color_score = _masked_color_score(scaled, template, offset)
            finalists.append((color_score, template, offset))

        def pick_best(entries):
            top, top_score, top_offset, second = None, -1.0, (0, 0), -1.0
            for color_score, template, offset in entries:
                if color_score > top_score:
                    if top is not None and template["label"] != top["label"]:
                        second = top_score
                    top, top_score, top_offset = template, color_score, offset
                elif top is not None and template["label"] != top["label"]:
                    second = max(second, color_score)
            return top, top_score, top_offset, second

        best_template, best_score, best_offset, runner_up = pick_best(finalists)

        if footprint_wh == (1, 1) and best_score < RESCUE_THRESHOLD:
            # Dark or low-contrast art ranks weakly at half-res; when the fast
            # path is unconvinced, pay for the deep candidate list once.
            seen_ids = {id(template) for _, template, _ in finalists}
            for _, template, half_offset in self.rank_1x1(gray_half, PREFILTER_1X1):
                if id(template) in seen_ids:
                    continue
                ys, xs, vec = _masked_vectors(template)
                gray_score, offset = _masked_zncc_best(
                    gray, ys, xs, vec,
                    template["gray"].shape[0], template["gray"].shape[1],
                    stride=1,
                    center=(half_offset[0] * 2, half_offset[1] * 2),
                    radius=4,
                )
                if gray_score <= 0:
                    continue
                finalists.append(
                    (_masked_color_score(scaled, template, offset), template, offset)
                )
            best_template, best_score, best_offset, runner_up = pick_best(finalists)

        if best_template is not None and footprint_wh == (1, 1):
            seen_ids = {id(template) for _, template, _ in finalists}
            for template in self.family_of(best_template):
                if id(template) in seen_ids:
                    continue
                ys, xs, vec = _masked_vectors(template)
                gray_score, offset = _masked_zncc_best(
                    gray,
                    ys,
                    xs,
                    vec,
                    template["gray"].shape[0],
                    template["gray"].shape[1],
                    stride=1,
                    center=best_offset,
                    radius=2,
                )
                if gray_score <= 0:
                    continue
                finalists.append(
                    (_masked_color_score(scaled, template, offset), template, offset)
                )
            best_template, best_score, best_offset, runner_up = pick_best(finalists)

        accept = ACCEPT_COLOR_1X1 if footprint_wh == (1, 1) else ACCEPT_COLOR
        if best_template is None or best_score < accept:
            return None, max(best_score, 0.0)
        contested = [
            (score, template, offset)
            for score, template, offset in finalists
            if template["label"] != best_template["label"]
            and best_score - score < DISCRIMINATE_MARGIN
        ]
        for score, template, offset in contested:
            winner = _discriminate(
                scaled, best_template, best_offset, template, offset
            )
            if winner is template:
                best_template, best_score, best_offset = template, score, offset
        if runner_up > 0 and best_score - runner_up < AMBIGUOUS_MARGIN:
            best_template = dict(best_template, ambiguous=True)
        return best_template, best_score


def template_match(template: dict, score: float, rect) -> dict:
    return {
        **match_row_fields(template),
        "x": rect.x,
        "y": rect.y,
        "w": rect.w,
        "h": rect.h,
        "name": template["label"],
        "price": template["price"],
        "ambiguous": bool(template.get("ambiguous")),
        "score": round(score, 3),
    }


def marker_match(rect) -> dict:
    return {
        "x": rect.x,
        "y": rect.y,
        "w": rect.w,
        "h": rect.h,
        "name": MARKER_NAME,
        "markerOnly": True,
        "priceAvailable": False,
        "price": 0.0,
        "kind": "unknown",
        "score": 0.0,
    }


NAVY_HUE = (100, 140)
MARKER_MIN_NAVY = 0.05
MARKER_MIN_SCORE = 0.25


def _navy_fraction(interior: np.ndarray) -> float:
    hsv = cv2.cvtColor(interior, cv2.COLOR_BGR2HSV)
    mask = (
        (hsv[:, :, 0] >= NAVY_HUE[0])
        & (hsv[:, :, 0] <= NAVY_HUE[1])
        & (hsv[:, :, 1] >= 60)
        & (hsv[:, :, 2] >= 25)
    )
    return float(mask.mean())


def identify_footprints(
    identifier: Identifier,
    frame: np.ndarray,
    lattice,
    footprints,
) -> list[dict]:
    pitch = (lattice.pitch_x + lattice.pitch_y) / 2.0
    pad = int(round(pitch * PAD_SLOTS))
    matches = []
    for footprint in footprints:
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
            continue
        template, score = identifier.identify_window(
            window, (footprint.w, footprint.h), pitch, interior=interior
        )
        if template is not None:
            matches.append(template_match(template, score, rect))
            continue
        # An unidentified 1x1 with neither the navy footprint backdrop nor any
        # template affinity is scene bleed-through, not an item.
        if (
            footprint.w == 1
            and footprint.h == 1
            and score < MARKER_MIN_SCORE
            and _navy_fraction(interior) < MARKER_MIN_NAVY
        ):
            continue
        matches.append(marker_match(rect))
    return matches
