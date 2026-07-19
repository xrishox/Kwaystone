"""Scale-free lattice estimation primitives shared by candidate systems.

The Favours grid is axis-aligned, so lattice recovery decomposes into two 1D
problems per axis: pitch from the autocorrelation of a line-strength signal
(harmonic-averaged so double periods cannot win) and phase from folding that
signal modulo the pitch. Both refine peaks with parabolic interpolation for
subpixel results.
"""

from __future__ import annotations

import numpy as np

from .stages import Lattice

MIN_PITCH_FRACTION = 0.010
MAX_PITCH_FRACTION = 0.075


def to_gray(image: np.ndarray) -> np.ndarray:
    if image.ndim == 2:
        return image
    import cv2

    return cv2.cvtColor(image, cv2.COLOR_BGR2GRAY)


def line_strength_signals(gray: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Per-axis gridline strength: mean absolute derivative across the axis."""
    import cv2

    g = gray.astype(np.float32)
    dx = np.abs(cv2.Scharr(g, cv2.CV_32F, 1, 0))
    dy = np.abs(cv2.Scharr(g, cv2.CV_32F, 0, 1))
    return dx.mean(axis=0), dy.mean(axis=1)


def autocorrelation(signal: np.ndarray) -> np.ndarray:
    s = signal.astype(np.float64) - float(signal.mean())
    n = len(s)
    if n < 8:
        return np.zeros(max(n, 1))
    f = np.fft.rfft(s, n=2 * n)
    r = np.fft.irfft(f * np.conj(f))[:n]
    r0 = r[0] if r[0] > 1e-9 else 1e-9
    return r / r0


def _parabolic_refine(values: np.ndarray, index: int) -> float:
    if index <= 0 or index >= len(values) - 1:
        return float(index)
    a, b, c = float(values[index - 1]), float(values[index]), float(values[index + 1])
    denom = a - 2.0 * b + c
    if abs(denom) < 1e-12:
        return float(index)
    return float(index) + 0.5 * (a - c) / denom


def estimate_pitch(
    signal: np.ndarray,
    min_pitch: float,
    max_pitch: float,
    harmonics: int = 4,
) -> tuple[float, float]:
    """Return (pitch, score). Score is the harmonic-averaged autocorrelation."""
    r = autocorrelation(signal)
    n = len(r)
    lo = max(2, int(np.floor(min_pitch)))
    hi = min(n - 2, int(np.ceil(max_pitch)))
    if hi <= lo:
        return 0.0, 0.0
    lags = np.arange(lo, hi + 1)
    scores = np.zeros(len(lags))
    for i, lag in enumerate(lags):
        total = 0.0
        count = 0
        for k in range(1, harmonics + 1):
            pos = k * lag
            if pos >= n - 1:
                break
            total += float(r[pos])
            count += 1
        scores[i] = total / count if count else -1.0
    best = int(np.argmax(scores))
    lag = float(lags[best])
    refined_positions = []
    for k in range(1, harmonics + 1):
        pos = int(round(k * lag))
        if pos >= n - 1 or pos <= 1:
            break
        local = int(pos + np.argmax(r[max(0, pos - 2):pos + 3]) - min(2, pos))
        refined_positions.append(_parabolic_refine(r, local) / k)
    pitch = float(np.mean(refined_positions)) if refined_positions else lag
    return pitch, float(scores[best])


def fold_phase(signal: np.ndarray, pitch: float) -> float:
    """Return the offset in [0, pitch) of the periodic maxima of `signal`."""
    n = len(signal)
    if pitch <= 1.0 or n < pitch * 2:
        return 0.0
    bins = max(4, int(round(pitch)))
    positions = np.arange(n, dtype=np.float64)
    fractional = (positions % pitch) / pitch * bins
    bin_idx = np.floor(fractional).astype(int) % bins
    sums = np.zeros(bins)
    counts = np.zeros(bins)
    np.add.at(sums, bin_idx, signal.astype(np.float64))
    np.add.at(counts, bin_idx, 1.0)
    folded = sums / np.maximum(counts, 1.0)
    padded = np.concatenate((folded[-1:], folded, folded[:1]))
    best = int(np.argmax(folded))
    refined = _parabolic_refine(padded, best + 1) - 1.0
    return float((refined / bins) * pitch % pitch)


def _line_positions(length: int, pitch: float, phase: float) -> list[float]:
    positions = []
    x = phase
    while x < length:
        positions.append(x)
        x += pitch
    return positions


def _line_strengths(signal: np.ndarray, positions: list[float]) -> np.ndarray:
    strengths = np.zeros(len(positions))
    n = len(signal)
    for index, position in enumerate(positions):
        lo = max(0, int(np.floor(position)) - 1)
        hi = min(n, int(np.ceil(position)) + 2)
        if hi > lo:
            strengths[index] = float(signal[lo:hi].max())
    return strengths


def _harmonic_score(r: np.ndarray, lag: float, harmonics: int = 4) -> float:
    n = len(r)
    total = 0.0
    count = 0
    for k in range(1, harmonics + 1):
        pos = int(round(k * lag))
        if pos >= n - 1 or pos <= 1:
            break
        total += float(r[max(0, pos - 1):pos + 2].max())
        count += 1
    return total / count if count else -1.0


def octave_corrected_pitch(
    signal: np.ndarray,
    min_pitch: float,
    max_pitch: float,
) -> tuple[float, float]:
    """estimate_pitch with a preference for the larger period when a
    subharmonic (half/third pitch) scores nearly as well — periodic art inside
    cells otherwise wins the octave-down error."""
    pitch, score = estimate_pitch(signal, min_pitch, max_pitch)
    if pitch <= 2:
        return pitch, score
    r = autocorrelation(signal)
    best_pitch, best_score = pitch, score
    for factor in (2.0, 3.0):
        candidate = pitch * factor
        if candidate > max_pitch * 1.05:
            continue
        cand_score = _harmonic_score(r, candidate)
        if cand_score >= score * 0.82 and cand_score >= best_score * 0.98:
            best_pitch, best_score = candidate, cand_score
    return best_pitch, best_score


def _occlusion_tolerant_signal(gray_f: np.ndarray, axis: int) -> np.ndarray:
    """Per-position gridline strength: a high quantile over the crossing axis
    survives partial occlusion by item art or tooltips far better than the
    mean."""
    import cv2

    if axis == 0:
        d = np.abs(cv2.Scharr(gray_f, cv2.CV_32F, 1, 0))
        return np.quantile(d, 0.60, axis=0)
    d = np.abs(cv2.Scharr(gray_f, cv2.CV_32F, 0, 1))
    return np.quantile(d, 0.60, axis=1)


def _peak_near(signal: np.ndarray, position: float, radius: int = 3) -> tuple[float, float]:
    lo = max(1, int(round(position)) - radius)
    hi = min(len(signal) - 1, int(round(position)) + radius + 1)
    if hi <= lo:
        return position, 0.0
    local = int(lo + np.argmax(signal[lo:hi]))
    return _parabolic_refine(signal, local), float(signal[local])


def _best_window(
    positions: list[float],
    strengths: np.ndarray,
    count: int,
    anchor: float | None,
) -> tuple[int, int] | None:
    """Best `count`-line window by total strength; if an anchor coordinate is
    given the window must span it (the plaque centers on the panel)."""
    n = len(positions)
    if n < 4:
        return None
    count = min(count, n)
    positive = strengths[strengths > 0]
    if len(positive) == 0:
        return None
    # Cap outliers: a couple of very strong ornament/border edges must not buy
    # a window over many moderate true gridlines.
    cap = float(np.quantile(positive, 0.70))
    capped = np.minimum(strengths, cap) if cap > 0 else strengths
    best = None
    for start in range(0, n - count + 1):
        end = start + count - 1
        if anchor is not None and not (positions[start] - 2 <= anchor <= positions[end] + 2):
            continue
        total = float(capped[start:end + 1].sum())
        if best is None or total > best[0]:
            best = (total, start, end)
    if best is None and anchor is not None:
        return _best_window(positions, strengths, count, None)
    return (best[1], best[2]) if best else None


def _fit_axis(
    signal: np.ndarray,
    pitch: float,
    length: int,
    count: int,
    anchor: float | None,
) -> tuple[float, float, int, int] | None:
    """Return (origin, refined_pitch, first_index, line_count) for one axis."""
    phase = fold_phase(signal, pitch)
    positions = _line_positions(length, pitch, phase)
    if len(positions) < 4:
        return None
    strengths = _line_strengths(signal, positions)
    window = _best_window(positions, strengths, count, anchor)
    if window is None:
        return None
    start, end = window
    peaks = []
    for index in range(start, end + 1):
        peak, strength = _peak_near(signal, positions[index])
        if strength > 0:
            peaks.append((index - start, peak, strength))
    if len(peaks) < 3:
        return None
    indices = np.array([p[0] for p in peaks], dtype=np.float64)
    coords = np.array([p[1] for p in peaks], dtype=np.float64)
    weights = np.sqrt(np.array([p[2] for p in peaks], dtype=np.float64))
    matrix = np.stack([np.ones_like(indices), indices], axis=1) * weights[:, None]
    target = coords * weights
    solution, *_ = np.linalg.lstsq(matrix, target, rcond=None)
    origin, refined_pitch = float(solution[0]), float(solution[1])
    if not (pitch * 0.9 <= refined_pitch <= pitch * 1.1):
        origin, refined_pitch = positions[start], pitch
    return origin, refined_pitch, start, end - start


def grid_from_roi(
    gray: np.ndarray,
    roi_x: int,
    roi_y: int,
    anchor_x: float,
    *,
    min_pitch: float,
    max_pitch: float,
    cols: int = 12,
    rows: int = 10,
) -> tuple[Lattice | None, dict[str, float]]:
    """Locate the Favours grid lattice inside a plaque-anchored ROI.

    The grid layout is a fixed cols x rows block, so extent selection is a
    best-total-strength window over candidate lines (occlusion by item art or
    tooltips cannot truncate it) and the final origin/pitch come from a
    weighted least-squares fit of detected line peaks."""
    height, width = gray.shape[:2]
    gray_f = gray.astype(np.float32)
    sx = _occlusion_tolerant_signal(gray_f, 0)
    sy = _occlusion_tolerant_signal(gray_f, 1)
    pitch_x, score_x = octave_corrected_pitch(sx, min_pitch, max_pitch)
    pitch_y, score_y = octave_corrected_pitch(sy, min_pitch, max_pitch)
    stats = {
        "pitch_x": pitch_x, "pitch_y": pitch_y,
        "score_x": score_x, "score_y": score_y,
    }
    if pitch_x <= 2 and pitch_y <= 2:
        return None, stats
    if pitch_x <= 2:
        pitch_x = pitch_y
    if pitch_y <= 2:
        pitch_y = pitch_x
    if abs(pitch_x - pitch_y) > 0.06 * max(pitch_x, pitch_y):
        if score_x >= score_y:
            pitch_y = pitch_x
        else:
            pitch_x = pitch_y

    fit_x = _fit_axis(sx, pitch_x, width, cols + 1, anchor_x)
    if fit_x is None:
        return None, stats
    x0, pitch_x, _, span_x = fit_x

    x_lo = max(0, int(round(x0)))
    x_hi = min(width, int(round(x0 + span_x * pitch_x)))
    sy_local = _occlusion_tolerant_signal(gray_f[:, x_lo:x_hi], 1)
    fit_y = _fit_axis(sy_local, pitch_y, height, rows + 1, None)
    if fit_y is None:
        return None, stats
    y0, pitch_y, _, span_y = fit_y

    y_lo = max(0, int(round(y0)))
    y_hi = min(height, int(round(y0 + span_y * pitch_y)))
    sx_local = _occlusion_tolerant_signal(gray_f[y_lo:y_hi, :], 0)
    fit_x = _fit_axis(sx_local, pitch_x, width, cols + 1, anchor_x)
    if fit_x is not None:
        x0, pitch_x, _, span_x = fit_x

    if span_x < 4 or span_y < 4:
        return None, stats
    stats.update({"cols": span_x, "rows": span_y})
    return (
        Lattice(
            x0=roi_x + x0,
            y0=roi_y + y0,
            pitch_x=pitch_x,
            pitch_y=pitch_y,
            cols=span_x,
            rows=span_y,
        ),
        stats,
    )


def periodic_region(
    gray: np.ndarray,
    min_pitch: float,
    max_pitch: float,
    tiles: tuple[int, int] = (8, 5),
) -> tuple[tuple[int, int, int, int] | None, float]:
    """Find the strongest grid-periodic region: score coarse tiles by ACF
    harmonic quality in the pitch band and return the bbox of the best
    connected high-quality cluster (x0, y0, x1, y1)."""
    height, width = gray.shape[:2]
    cols, rows = tiles
    tile_w = width // cols
    tile_h = height // rows
    if tile_w < max_pitch * 2 or tile_h < max_pitch * 2:
        return None, 0.0
    quality = np.zeros((rows, cols))
    for ty in range(rows):
        for tx in range(cols):
            tile = gray[ty * tile_h:(ty + 1) * tile_h, tx * tile_w:(tx + 1) * tile_w]
            sx, sy = line_strength_signals(tile)
            _, qx = estimate_pitch(sx, min_pitch, max_pitch, harmonics=3)
            _, qy = estimate_pitch(sy, min_pitch, max_pitch, harmonics=3)
            quality[ty, tx] = max(0.0, qx) + max(0.0, qy)
    threshold = max(0.15, float(np.quantile(quality, 0.75)))
    strong = quality >= threshold
    if not strong.any():
        return None, float(quality.max())
    import cv2

    count, labels = cv2.connectedComponents(strong.astype(np.uint8), connectivity=8)
    best_label, best_sum = 0, -1.0
    for label in range(1, count):
        total = float(quality[labels == label].sum())
        if total > best_sum:
            best_label, best_sum = label, total
    ys, xs = np.nonzero(labels == best_label)
    x0 = int(xs.min()) * tile_w
    x1 = (int(xs.max()) + 1) * tile_w
    y0 = int(ys.min()) * tile_h
    y1 = (int(ys.max()) + 1) * tile_h
    pad_x, pad_y = tile_w // 2, tile_h // 2
    return (
        (max(0, x0 - pad_x), max(0, y0 - pad_y), min(width, x1 + pad_x), min(height, y1 + pad_y)),
        best_sum,
    )


def lattice_from_region(
    gray: np.ndarray,
    region_x: int,
    region_y: int,
    *,
    min_pitch: float | None = None,
    max_pitch: float | None = None,
) -> tuple[Lattice | None, dict[str, float]]:
    """Estimate a full lattice for a grid-panel crop located at region_x/y."""
    height, width = gray.shape[:2]
    span = float(max(width, height))
    lo = min_pitch if min_pitch is not None else span * MIN_PITCH_FRACTION
    hi = max_pitch if max_pitch is not None else span * MAX_PITCH_FRACTION
    sx, sy = line_strength_signals(gray)
    pitch_x, score_x = estimate_pitch(sx, lo, hi)
    pitch_y, score_y = estimate_pitch(sy, lo, hi)
    stats = {
        "pitch_x": pitch_x,
        "pitch_y": pitch_y,
        "score_x": score_x,
        "score_y": score_y,
    }
    if pitch_x <= 2 or pitch_y <= 2:
        return None, stats
    # The two axes observe the same square cells; disagreement means one axis
    # locked onto art rather than gridlines, so trust the stronger axis.
    if abs(pitch_x - pitch_y) > 0.06 * max(pitch_x, pitch_y):
        if score_x >= score_y:
            pitch_y = pitch_x
        else:
            pitch_x = pitch_y
    phase_x = fold_phase(sx, pitch_x)
    phase_y = fold_phase(sy, pitch_y)
    cols = int((width - phase_x) // pitch_x)
    rows = int((height - phase_y) // pitch_y)
    stats.update({"phase_x": phase_x, "phase_y": phase_y})
    if cols < 2 or rows < 2:
        return None, stats
    lattice = Lattice(
        x0=region_x + phase_x,
        y0=region_y + phase_y,
        pitch_x=pitch_x,
        pitch_y=pitch_y,
        cols=cols,
        rows=rows,
    )
    return lattice, stats
