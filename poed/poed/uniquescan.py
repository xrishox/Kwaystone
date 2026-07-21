"""Market icon corpus and shared template matching.

Owns: brain-row filtering (`filter_rows`, `filter_ritual_rows`), the icon
template corpus (`_load_corpus`: one template per distinct icon file,
normalized to PXSLOT px/slot, content-keyed caching), the shared coarse-to-
fine matcher used by the merchant scanner (`scan_region`/`_scan_shared`), and
the process-wide scan thread pool. `poed.ritual_scan` consumes the corpus,
descriptors, and pool from here; ritual matching itself lives there.
"""
import os
import threading
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

from poed.image_geometry import Rect
from poed.match_fields import match_row_fields


def _positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return max(1, default)
    try:
        return max(1, int(value))
    except ValueError:
        return max(1, default)


# CT-calibrated (concept test, 2026-06-11)
PXSLOT = 47          # template density everything is normalized to
GRAY_THRESH = 0.72   # candidate floor (vendor's recovered band starts ~0.76)
COLOR_THRESH = 0.75  # confirm floor on the color verify
COARSE = 0.5         # pyramid level: half of the factor-scaled frame
COARSE_THRESH = 0.65  # keep-floor — CT-measured: vendor 1x1 items bottom out
WORKERS = max(1, (os.cpu_count() or 2) // 2)  # half cores: game is running
RITUAL_CELL_WORKERS = _positive_int_env("WAYSTONE_RITUAL_CELL_WORKERS", min(WORKERS, 4))

_shared_pool_lock = threading.Lock()
_shared_pool: ThreadPoolExecutor | None = None


def _pool() -> ThreadPoolExecutor:
    """Process-wide worker pool for scan parallelism.

    Created lazily and reused across presses so each scan does not pay
    thread spin-up; sized to half the cores because the game runs
    alongside.
    """
    global _shared_pool
    with _shared_pool_lock:
        if _shared_pool is None:
            _shared_pool = ThreadPoolExecutor(
                max_workers=max(WORKERS, RITUAL_CELL_WORKERS),
                thread_name_prefix="waystone-scan",
            )
        return _shared_pool


_corpus_cache: dict | None = None
_corpus_cache_entries: dict[tuple, dict] = {}
_CORPUS_CACHE_LIMIT = 4


# One-entry identity caches: the engine re-serves the same rows object for
# every press while the brain snapshot is unchanged, so the per-press dict
# rebuilds collapse to an `is` check.
_filter_rows_cache: tuple[int, float, dict, dict] | None = None
_ritual_rows_cache: tuple[int, dict, dict] | None = None


# --- Row filtering and the template corpus ---------------------------------
def _template_match_fields(t: dict, score: float) -> dict:
    """Match fields shared by every template-derived hit."""
    return {
        **match_row_fields(t),
        "name": t["label"],
        "price": t["price"],
        "ambiguous": t["ambiguous"],
        "score": round(score, 3),
    }


def filter_rows(rows: dict, min_price: float) -> dict:
    """Drop corpus rows priced under `min_price` exalted before matching.
    The full-frame correlation is O(corpus), so this trades coverage for
    speed (config: unique_scan_min_price; 0 = off). CAVEAT: a filtered-out
    item's cell can be claimed by a similar-art lookalike that survived the
    filter (measured on the CT shots — see config.py) — results on a
    filtered corpus may mislabel cheap items as their pricier lookalikes."""
    global _filter_rows_cache
    cached = _filter_rows_cache
    if cached is not None and cached[2] is rows and cached[1] == min_price:
        return cached[3]
    out = {n: r for n, r in rows.items() if (r.get("price") or 0) >= min_price}
    _filter_rows_cache = (id(rows), min_price, rows, out)
    return out


RITUAL_EXCLUDED_TAGGED_SOURCE_CATEGORIES = frozenset({
    # poe2scout's "fragments" category contains pinnacle/fragments such as
    # Origin Spark. Those icons are not Ritual reward-currency rows, and their
    # art can be close enough to omen coins to win weak 1x1 matches. Keep this
    # at category level so scanner context, not item-name exceptions, controls
    # candidate eligibility.
    "fragments",
})


def filter_ritual_rows(rows: dict) -> dict:
    """Return corpus rows that are eligible candidates in a Ritual reward grid.

    Rows built by older/running brain processes may not yet include
    `sourceCategory`; keep those rows so a stale brain does not erase all
    tagged currency candidates. Once the brain refreshes, scanner-specific
    category metadata can remove known ineligible classes without hard-coding
    individual item names.
    """
    global _ritual_rows_cache
    cached = _ritual_rows_cache
    if cached is not None and cached[1] is rows:
        return cached[2]
    out = _filter_ritual_rows_impl(rows)
    _ritual_rows_cache = (id(rows), rows, out)
    return out


def _filter_ritual_rows_impl(rows: dict) -> dict:
    out = {}
    for name, row in rows.items():
        if row.get("kind") != "tagged":
            out[name] = row
            continue
        category = row.get("sourceCategory")
        if not isinstance(category, str) or not category:
            out[name] = row
            continue
        if category.strip().lower() in RITUAL_EXCLUDED_TAGGED_SOURCE_CATEGORIES:
            continue
        out[name] = row
    return out


def _load_corpus(rows: dict) -> list[dict]:
    """One template per distinct icon FILE: gray + color images normalized to
    PXSLOT px/slot, grouped names, group max price. Cached by CONTENT (the
    rows' matching-relevant content) — every scan request builds a fresh dict
    from JSON, so identity caching would reload ~1000 PNGs per scan."""
    global _corpus_cache, _corpus_cache_entries
    if _corpus_cache is None and _corpus_cache_entries:
        # Tests and debug tools historically clear _corpus_cache directly.
        # Preserve that seam by treating it as a full cache reset.
        _corpus_cache_entries = {}
    if _corpus_cache is not None and _corpus_cache.get("rows_ref") is rows:
        # Same rows object as the last build (the engine re-serves it while
        # the brain snapshot is unchanged): skip the O(n log n) content key.
        return _corpus_cache["tmpl"]
    key = tuple(
        sorted(
            (
                name,
                row.get("iconPath"),
                row.get("price"),
                row.get("priceAvailable"),
                row.get("kind"),
                row.get("w"),
                row.get("h"),
                row.get("sourceTag"),
                row.get("sourceCategory"),
            )
            for name, row in rows.items()
        )
    )
    if _corpus_cache is not None and _corpus_cache["key"] == key:
        _corpus_cache["rows_ref"] = rows
        return _corpus_cache["tmpl"]
    cached = _corpus_cache_entries.get(key)
    if cached is not None:
        cached["rows_ref"] = rows
        _corpus_cache = cached
        return cached["tmpl"]

    by_file: dict[str, dict] = {}
    for name, row in rows.items():
        path = row.get("iconPath")
        if not path:
            continue
        g = by_file.setdefault(path, {"names": [], "rows": []})
        g["names"].append(name)
        g["rows"].append(row)

    out = []
    for path, g in by_file.items():
        img = cv2.imread(path, cv2.IMREAD_UNCHANGED)
        if img is None:
            continue
        if img.ndim == 3 and img.shape[2] == 4:
            alpha = img[:, :, 3:4].astype(np.float32) / 255.0
            rgb = img[:, :, :3].astype(np.float32)
            img = (rgb * alpha + 12.0 * (1.0 - alpha)).astype(np.uint8)
        slots_w = g["rows"][0].get("w") or 1
        slots_h = g["rows"][0].get("h") or 1
        # Catalog icons are not guaranteed to be a full slot canvas in both
        # dimensions. Some tall/thin two-slot weapons are stored as one-slot
        # wide art with two-slot metadata. Preserve aspect ratio and normalize
        # by the strongest observed pixels-per-slot axis so sparse art is not
        # stretched into a false full-cell shape.
        density = max(img.shape[1] / slots_w, img.shape[0] / slots_h)
        if abs(density - PXSLOT) > 1:
            scale = PXSLOT / density
            img = cv2.resize(img, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        best = max(g["rows"], key=lambda r: r.get("price", 0))
        names = g["names"]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        descriptor = _visual_descriptor(img)
        out.append({
            **match_row_fields(best),
            "label": names[0] + (f" +{len(names) - 1}" if len(names) > 1 else ""),
            "ambiguous": len(names) > 1,
            "price": best.get("price", 0),
            "slots_w": int(slots_w),
            "slots_h": int(slots_h),
            "color": img,
            "gray": gray,
            "descriptor": descriptor,
            # Half-res copy for the coarse pyramid pass (1/16 the correlation
            # cost of the full-res sweep).
            "gray_half": cv2.resize(gray, None, fx=COARSE, fy=COARSE,
                                    interpolation=cv2.INTER_AREA),
        })
    corpus = list(out)
    entry = {
        "key": key,
        "tmpl": corpus,
        "rows_ref": rows,
    }
    _corpus_cache_entries[key] = entry
    while len(_corpus_cache_entries) > _CORPUS_CACHE_LIMIT:
        _corpus_cache_entries.pop(next(iter(_corpus_cache_entries)))
    _corpus_cache = entry
    return corpus


def _color_verify(small: np.ndarray, t: dict, loc: tuple[int, int],
                  pad: int = 6) -> float:
    """Best color correlation for template `t` near `loc` in the small frame."""
    c = t["color"]
    y0, y1 = max(0, loc[1] - pad), min(small.shape[0], loc[1] + c.shape[0] + pad)
    x0, x1 = max(0, loc[0] - pad), min(small.shape[1], loc[0] + c.shape[1] + pad)
    win = small[y0:y1, x0:x1]
    if win.shape[0] < c.shape[0] or win.shape[1] < c.shape[1]:
        return 0.0
    r = cv2.matchTemplate(win, c, cv2.TM_CCOEFF_NORMED)
    return float(cv2.minMaxLoc(r)[1])


def _normalized_vector(values: np.ndarray) -> np.ndarray:
    vector = values.astype(np.float32).reshape(-1)
    vector -= float(vector.mean())
    norm = float(np.sqrt(np.dot(vector, vector)))
    if norm <= 1e-6:
        return np.zeros_like(vector, dtype=np.float32)
    return vector / norm


def _visual_descriptor(img: np.ndarray, size: int = 18) -> np.ndarray:
    """Small, illumination-tolerant descriptor used only for candidate pruning."""

    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(cv2.resize(gray, (size, size), interpolation=cv2.INTER_AREA))
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.resize(cv2.magnitude(gx, gy), (size, size), interpolation=cv2.INTER_AREA)
    return np.concatenate((
        _normalized_vector(gray) * 0.55,
        _normalized_vector(grad) * 0.45,
    )).astype(np.float32)


def _overlap(a: dict, b: dict) -> bool:
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy > 0.4 * min(a["w"] * a["h"], b["w"] * b["h"])


# --- Warmup and template grouping -------------------------------------------
def warm(brain, cfg: dict, row_filter=None) -> bool:
    """Pre-build the template corpus so the first Alt+X pays nothing:
    request the corpus (brain warms its own snapshot+icons at startup, so
    this is usually a fast cache hit) and run _load_corpus. Returns True
    when the corpus is warm; safe to call from a background thread — a
    concurrent scan at worst builds the same cache once more.

    `row_filter` lets scanners warm the exact row variant they use at scan
    time, e.g. Ritual's scanner-specific candidate eligibility filter.
    """
    try:
        rows = brain.request(
            {"cmd": "uniqueprices", "league": cfg["league"]}, timeout=120.0
        )
    except (RuntimeError, OSError, TimeoutError):
        return False
    rows = filter_rows(rows, cfg["unique_scan_min_price"])
    if row_filter is not None:
        rows = row_filter(rows)
    _load_corpus(rows)
    return True


MID = 0.69  # shared coarse-pass factor: midpoint of FACTORS (they differ ~3%,
            # invisible at half res, so ONE coarse sweep serves both fine passes)


# --- Shared multi-factor scanning and region entry ----------------------------
def _scan_shared(crop: np.ndarray, rows: dict,
                 factors: tuple[float, ...],
                 gray_thresh: float = GRAY_THRESH,
                 color_thresh: float = COLOR_THRESH) -> list[dict]:
    """Multi-factor scan with one shared coarse pass: coarse candidates are
    found at MID and mapped into each factor's frame for local fine + color
    verification. Halves the dominant (coarse) cost vs per-factor sweeps."""
    scan_factors = factors
    mid = sum(scan_factors) / len(scan_factors)
    templates = _load_corpus(rows)
    gray_mid = cv2.cvtColor(
        cv2.resize(crop, None, fx=mid, fy=mid, interpolation=cv2.INTER_AREA),
        cv2.COLOR_BGR2GRAY)
    half = cv2.resize(gray_mid, None, fx=COARSE, fy=COARSE,
                      interpolation=cv2.INTER_AREA)

    def coarse(t):
        th = t["gray_half"]
        if th.shape[0] >= half.shape[0] or th.shape[1] >= half.shape[1]:
            return []
        r = cv2.matchTemplate(half, th, cv2.TM_CCOEFF_NORMED)
        peaks = []
        for _ in range(8):
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx < COARSE_THRESH:
                break
            peaks.append((t, loc))
            y0 = max(0, loc[1] - th.shape[0] // 2)
            x0 = max(0, loc[0] - th.shape[1] // 2)
            r[y0:loc[1] + th.shape[0], x0:loc[0] + th.shape[1]] = -1.0
        return peaks

    cands = [c for peaks in _pool().map(coarse, templates) for c in peaks]

    confirmed: list[dict] = []
    pad = 10  # covers coarse quantization + the +-3% MID->factor mismatch
    for f in scan_factors:
        small = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        scale = f / (mid * COARSE)  # half-frame coords -> this factor's frame
        for t, cloc in cands:
            g = t["gray"]
            cx, cy = int(cloc[0] * scale), int(cloc[1] * scale)
            y0, y1 = max(0, cy - pad), min(gray.shape[0], cy + g.shape[0] + pad)
            x0, x1 = max(0, cx - pad), min(gray.shape[1], cx + g.shape[1] + pad)
            win = gray[y0:y1, x0:x1]
            if win.shape[0] < g.shape[0] or win.shape[1] < g.shape[1]:
                continue
            r = cv2.matchTemplate(win, g, cv2.TM_CCOEFF_NORMED)
            _, mx, _, loc = cv2.minMaxLoc(r)
            if mx < gray_thresh:
                continue
            floc = (x0 + loc[0], y0 + loc[1])
            score = _color_verify(small, t, floc)
            if score < color_thresh:
                continue
            confirmed.append({
                **_template_match_fields(t, score),
                "x": int(floc[0] / f), "y": int(floc[1] / f),
                "w": int(g.shape[1] / f), "h": int(g.shape[0] / f),
            })

    confirmed.sort(key=lambda h: -h["score"])
    kept: list[dict] = []
    for h in confirmed:
        if all(not _overlap(h, k) for k in kept):
            kept.append(h)
    kept.sort(key=lambda h: -h["price"])
    return kept

def scan_region(
    frame: np.ndarray,
    rows: dict,
    region: Rect | None,
    cell: float,
    gray_thresh: float = GRAY_THRESH,
    color_thresh: float = COLOR_THRESH,
) -> list[dict]:
    """Scan a localized stock grid in frame coordinates (merchant path).

    The measured cell size sets the template normalization factor; a narrow
    factor band absorbs UI scale and resize rounding."""
    if region is None:
        return []
    clipped = region.clipped(frame.shape[1], frame.shape[0])
    if clipped is None:
        return []
    crop = frame[
        clipped.y:clipped.y + clipped.h,
        clipped.x:clipped.x + clipped.w,
    ]
    factor = PXSLOT / max(1.0, cell)
    factors = tuple(
        round(factor * scale, 3) for scale in (0.96, 0.98, 1.0, 1.02, 1.04)
    )
    kept = _scan_shared(
        crop,
        rows,
        gray_thresh=gray_thresh,
        color_thresh=color_thresh,
        factors=factors,
    )
    for match in kept:
        match["x"] += clipped.x
        match["y"] += clipped.y
    return kept
