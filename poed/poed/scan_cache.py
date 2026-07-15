"""Cross-press reuse of per-cell identification work.

Consecutive Alt+X presses usually look at a barely-changed screen, and the
game's UI rasterizes deterministically: panel pixels that did not change
semantically are byte-identical across captures (verified on retained
frames 30s apart).  Scanners therefore key expensive per-card/cell work
(OCR, template matching) by an exact digest of the pixels that work reads;
a hit replays the previous identification, a changed cell recomputes.

Only identification is cached — matches and prices are rebuilt from the
current price rows on every scan.  Entries live for exactly one following
scan (two-generation rotation), so memory stays bounded at roughly one
screen's worth of digests and the cache can never serve results older
than the previous press.
"""

from __future__ import annotations

import hashlib
import threading

import numpy as np

_MISS = object()
_MAX_ENTRIES_PER_SCAN = 1024

_lock = threading.Lock()
_current: dict[str, dict[bytes, object]] = {}
_previous: dict[str, dict[bytes, object]] = {}


def digest(image: np.ndarray, extra: str = "") -> bytes:
    """Exact-content digest of an image region (shape-qualified).

    ``extra`` folds in non-pixel inputs the cached computation also depends
    on (e.g. the card height that sets a resample scale).
    """

    h = hashlib.blake2b(digest_size=16)
    h.update(str(image.shape).encode())
    if extra:
        h.update(extra.encode())
    h.update(np.ascontiguousarray(image).tobytes())
    return h.digest()


def begin_scan() -> None:
    """Rotate generations: last scan's entries stay visible, older ones drop."""

    global _current, _previous
    with _lock:
        _previous = _current
        _current = {}


def lookup(bucket: str, key: bytes):
    """Return the cached value for ``key`` or ``None``.

    A previous-generation hit is promoted into the current generation so a
    steadily-unchanged cell survives any number of presses.
    """

    with _lock:
        cur = _current.get(bucket)
        if cur is not None:
            value = cur.get(key, _MISS)
            if value is not _MISS:
                return value
        prev = _previous.get(bucket)
        if prev is not None:
            value = prev.get(key, _MISS)
            if value is not _MISS:
                _current.setdefault(bucket, {})[key] = value
                return value
    return None


def store(bucket: str, key: bytes, value: object) -> None:
    with _lock:
        entries = _current.setdefault(bucket, {})
        if len(entries) < _MAX_ENTRIES_PER_SCAN:
            entries[key] = value


def clear() -> None:
    """Drop all cached identifications (tests, config changes)."""

    global _current, _previous
    with _lock:
        _current = {}
        _previous = {}
