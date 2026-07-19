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
