"""Vectorized batch scoring for the unique-icon grid matcher.

The broad Ritual fallbacks score hundreds of catalog templates against one
cell window.  Per-template OpenCV calls and Python tile loops dominate that
cost, not the arithmetic.  This module computes the same quantities —
whole-template grayscale ZNCC (``cv2.matchTemplate`` TM_CCOEFF_NORMED),
the tiled robust visual score, and the padded color verification — as
batched numpy/BLAS operations over template stacks.

Scores are mathematically identical to the scalar implementations in
:mod:`poed.uniquescan`; tiny float-accumulation differences are possible
(BLAS reduction order), which the acceptance margins absorb.
"""

from __future__ import annotations

import numpy as np


def _flat_centered(vec: np.ndarray) -> tuple[np.ndarray, float]:
    flat = vec.astype(np.float32).reshape(-1)
    flat = flat - float(flat.mean())
    norm = float(np.linalg.norm(flat))
    return flat, norm


def _template_flat_gray(t: dict) -> tuple[np.ndarray, float]:
    cached = t.get("_flat_gray")
    if cached is None:
        cached = _flat_centered(t["gray"])
        t["_flat_gray"] = cached
    return cached


def _template_flat_color(t: dict) -> tuple[np.ndarray, float]:
    # OpenCV's TM_CCOEFF_NORMED centers every channel separately.
    cached = t.get("_flat_color")
    if cached is None:
        color = t["color"].astype(np.float32)
        color = color - color.mean(axis=(0, 1), keepdims=True)
        flat = color.reshape(-1)
        cached = (flat, float(np.linalg.norm(flat)))
        t["_flat_color"] = cached
    return cached


def _patch_matrix(image: np.ndarray, shape: tuple[int, int]) -> np.ndarray | None:
    """(nPositions x pixels) matrix of all template-sized patches."""
    h, w = shape
    if image.shape[0] < h or image.shape[1] < w:
        return None
    view = np.lib.stride_tricks.sliding_window_view(image, (h, w), axis=(0, 1))
    positions = view.reshape(view.shape[0] * view.shape[1], -1).astype(np.float32)
    return positions


def batched_gray_candidates(
    gray_window: np.ndarray,
    templates: list[dict],
) -> list[tuple[float, tuple[int, int], dict]]:
    """Per-template best ZNCC score + location, batched.

    Equivalent to running ``cv2.matchTemplate(gray_window, t["gray"],
    TM_CCOEFF_NORMED)`` + ``minMaxLoc`` per template.  Templates are grouped
    by pixel shape (slot groups can mix aspect-preserved sizes).
    """

    by_shape: dict[tuple[int, int], list[dict]] = {}
    for t in templates:
        by_shape.setdefault(t["gray"].shape, []).append(t)
    out: list[tuple[float, tuple[int, int], dict]] = []
    for shape, group in by_shape.items():
        patches = _patch_matrix(gray_window, shape)
        if patches is None:
            continue
        cols = gray_window.shape[1] - shape[1] + 1
        centered = patches - patches.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(centered, axis=1)
        np.maximum(norms, 1e-6, out=norms)
        tmat = np.stack([_template_flat_gray(t)[0] for t in group])
        tnorm = np.asarray([_template_flat_gray(t)[1] for t in group], dtype=np.float32)
        np.maximum(tnorm, 1e-6, out=tnorm)
        scores = (tmat @ centered.T) / (tnorm[:, None] * norms[None, :])
        np.clip(scores, -1.0, 1.0, out=scores)
        best_index = np.argmax(scores, axis=1)
        best_score = scores[np.arange(len(group)), best_index]
        for i, t in enumerate(group):
            position = int(best_index[i])
            loc = (position % cols, position // cols)
            out.append((float(best_score[i]), loc, t))
    return out


def batched_color_verify(
    window: np.ndarray,
    items: list[tuple[dict, tuple[int, int]]],
    pad: int = 3,
) -> list[float]:
    """Batched equivalent of ``uniquescan._color_verify`` for many templates.

    Groups by (location, template shape); each group shares one padded patch
    matrix, so the correlation becomes a single matmul per group.
    """

    out = [0.0] * len(items)
    groups: dict[tuple[tuple[int, int], tuple[int, int]], list[int]] = {}
    for index, (t, loc) in enumerate(items):
        groups.setdefault((loc, t["color"].shape[:2]), []).append(index)
    for (loc, shape), indexes in groups.items():
        h, w = shape
        y0 = max(0, loc[1] - pad)
        y1 = min(window.shape[0], loc[1] + h + pad)
        x0 = max(0, loc[0] - pad)
        x1 = min(window.shape[1], loc[0] + w + pad)
        win = window[y0:y1, x0:x1]
        if win.shape[0] < h or win.shape[1] < w:
            continue
        view = np.lib.stride_tricks.sliding_window_view(win, (h, w), axis=(0, 1))
        # view is (Y, X, C, h, w); match the template's (h, w, C) flattening
        # and center every channel separately, as OpenCV does.
        view = np.moveaxis(view, 2, -1)
        count = view.shape[0] * view.shape[1]
        patches = view.reshape(count, h * w, view.shape[-1]).astype(np.float32)
        patches = patches - patches.mean(axis=1, keepdims=True)
        centered = patches.reshape(count, -1)
        norms = np.linalg.norm(centered, axis=1)
        np.maximum(norms, 1e-6, out=norms)
        tmat = np.stack([_template_flat_color(items[i][0])[0] for i in indexes])
        tnorm = np.asarray(
            [_template_flat_color(items[i][0])[1] for i in indexes],
            dtype=np.float32,
        )
        np.maximum(tnorm, 1e-6, out=tnorm)
        scores = (tmat @ centered.T) / (tnorm[:, None] * norms[None, :])
        np.clip(scores, -1.0, 1.0, out=scores)
        best = scores.max(axis=1)
        for gi, index in enumerate(indexes):
            out[index] = float(best[gi])
    return out


class _SlidingTileBank:
    """Normalized sample tile vectors at every window position.

    ``rows(size)`` returns (matrix, cols) where ``matrix[y * cols + x]`` is
    the mean-centered, L2-normalized tile of ``size`` at window position
    (x, y); zero rows mark degenerate (flat) tiles, matching the scalar
    scorer's 0.0 for them.
    """

    def __init__(self, plane: np.ndarray):
        self.plane = plane
        self._cache: dict[tuple[int, int], tuple[np.ndarray, int]] = {}

    def rows(self, size: tuple[int, int]) -> tuple[np.ndarray, int] | None:
        cached = self._cache.get(size)
        if cached is not None:
            return cached
        th, tw = size
        if self.plane.shape[0] < th or self.plane.shape[1] < tw:
            return None
        view = np.lib.stride_tricks.sliding_window_view(self.plane, (th, tw))
        cols = view.shape[1]
        flat = view.reshape(view.shape[0] * cols, th * tw).astype(np.float32)
        flat = flat - flat.mean(axis=1, keepdims=True)
        norms = np.linalg.norm(flat, axis=1, keepdims=True)
        good = norms[:, 0] > 1e-6
        flat = np.where(good[:, None], flat / np.maximum(norms, 1e-6), 0.0)
        self._cache[size] = (flat, cols)
        return flat, cols


def batched_robust_scores(
    window: np.ndarray,
    items: list[tuple[dict, tuple[int, int]]],
    feature_planes,
    tile_slices,
) -> list[float | None]:
    """Batched equivalent of ``uniquescan._robust_visual_score``.

    The window's feature planes are normalized once at every tile position;
    each (template, location) score is then a row gather + dot against the
    template's precomputed tile vectors.  Aggregation replicates the scalar
    trimmed mean per template.  Entries whose patch would need resizing
    return None so the caller can use the scalar path.
    """

    out: list[float | None] = [0.0] * len(items)
    window_gray, window_grad = feature_planes(window)
    gray_bank = _SlidingTileBank(window_gray)
    grad_bank = _SlidingTileBank(window_grad)
    height, width = window.shape[:2]

    for index, (t, loc) in enumerate(items):
        shape = t["color"].shape[:2]
        x0, y0 = loc
        if y0 + shape[0] > height or x0 + shape[1] > width:
            out[index] = None
            continue
        template_tiles = t.get("robust_tiles")
        if template_tiles is None:
            out[index] = None
            continue
        scores: list[float] = []
        ok = True
        for ty0, ty1, tx0, tx1, tg_vec, te_vec in template_tiles:
            size = (ty1 - ty0, tx1 - tx0)
            gray_rows = gray_bank.rows(size)
            grad_rows = grad_bank.rows(size)
            if gray_rows is None or grad_rows is None:
                ok = False
                break
            row = (y0 + ty0) * gray_rows[1] + (x0 + tx0)
            gray_score = float(gray_rows[0][row] @ tg_vec)
            grad_score = float(grad_rows[0][row] @ te_vec)
            scores.append(0.38 * gray_score + 0.62 * grad_score)
        if not ok:
            out[index] = None
            continue
        if len(scores) < 5:
            out[index] = 0.0
            continue
        ranked = sorted(scores, reverse=True)
        keep = max(5, int(round(len(ranked) * 0.62)))
        core = float(np.mean(ranked[:keep]))
        coverage = sum(score >= 0.30 for score in scores) / len(scores)
        out[index] = max(0.0, min(1.0, 0.82 * core + 0.18 * coverage))
    return out
