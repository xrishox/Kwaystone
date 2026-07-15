"""Find unique/currency/omen items on a screenshot by matching brain-cached
icons.

Pipeline (CT-derived):
 1. Region crop — the lookup zone (ritual/vendor window) sits near screen
    center; cropping cuts pixels ~2.5x.
 2. Shot downscale by `factor` — GGG web icons are 47/64 px-per-slot thumbs
    while the game renders ~68 px/slot; templates are normalized to
    47 px/slot and the SHOT is brought down to them (never upscale the
    thumbs: CT showed upscaled templates match noise).
 3. Grayscale candidate pass (fast, threshold GRAY_THRESH) across the whole
    corpus, threaded at HALF the cores — the game is running during a scan.
 4. Per-candidate COLOR verify on a local crop (threshold COLOR_THRESH):
    restores the precision grayscale loses and disambiguates same-shape
    different-color arts (omen variants). Masked CCORR was measured and
    rejected in the CT — absent items scored into the present band.
 5. Greedy NMS on color scores; shared-art groups (one icon file, many
    names) report once with a grouped label and the group's max price.
"""
import os
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

from poed.image_geometry import Rect
from poed import scan_cache
from poed.match_fields import match_row_fields
from poed import unique_batch as _batch
from poed import unique_grid_geometry as _grid


def _positive_int_env(name: str, default: int) -> int:
    value = os.environ.get(name)
    if value is None:
        return max(1, default)
    try:
        return max(1, int(value))
    except ValueError:
        return max(1, default)


# CT-calibrated (docs/superpowers/plans/2026-06-11-unique-scan-concept-test.md)
PXSLOT = 47          # template density everything is normalized to
# Ritual calibrates to 0.68, the vendor window to 0.70 — and a 0.68-only scan
# loses the vendor's small 1x1 items (validated on the CT shots). scan_screen
# runs both and NMS-merges; ~2x match cost, still inside the 10-20s budget.
FACTORS = (0.68, 0.70)
HIRES_FACTORS = (0.38, 0.40, 0.42, 0.44)
GRAY_THRESH = 0.72   # candidate floor (vendor's recovered band starts ~0.76)
COLOR_THRESH = 0.75  # confirm floor on the color verify
CELL_GRAY_THRESH = 0.50
CELL_COLOR_THRESH = 0.55
RITUAL_1X1_COLOR_THRESH = 0.58
RITUAL_MULTI_COLOR_THRESH = 0.55
RITUAL_1X1_SCORE_MARGIN = 0.045
RITUAL_MULTI_SCORE_MARGIN = 0.045
RITUAL_DECORATED_1X1_SCORE_THRESH = 0.40
RITUAL_DECORATED_1X1_SCORE_MARGIN = 0.045
RITUAL_DECORATED_CONTEXT_SCORE_THRESH = 0.42
RITUAL_DECORATED_CONTEXT_SCORE_MARGIN = 0.015
RITUAL_DECORATED_CONTEXT_LOCAL_RADIUS = 1
RITUAL_DECORATED_CONTEXT_CANDIDATE_LIMIT = 24
RITUAL_DECORATED_CONTEXT_COLOR_FULL_SUPPORT = 0.45
RITUAL_DECORATED_CONTEXT_COLOR_PENALTY = 0.30
RITUAL_PREFILTER_LIMIT = 45
RITUAL_MULTI_PREFILTER_LIMIT = 12
# Broad 1x1 fallback is used after the descriptor shortlist is not decisive.
# At that point grayscale correlation is only a recovery gate before robust
# visual scoring, and retained Ritual captures kept the correct recovery inside
# the top few gray candidates. Keep this intentionally small so decorated/weak
# 1x1 cells do not rescore the whole 1x1 catalog.
RITUAL_FULL_GRAY_LIMIT = 4
# On the fast shortlist path the expensive robust/color verification runs only
# on the strongest gray candidates. Measured across retained Ritual captures
# and the corpus, the finally-accepted template sat within gray rank 11 in
# 815/816 shortlist classifications (rank 0 in 81%), and the weak-cell broad
# retry still covers the tail, so 12 keeps the exact acceptance semantics
# while cutting verification volume ~4x.
RITUAL_SHORTLIST_VERIFY_LIMIT = 12
RITUAL_1X1_FAST_TRUST_SCORE = 0.74
RITUAL_CELL_CLAIM_SCORE = 0.70
COARSE = 0.5         # pyramid level: half of the factor-scaled frame
COARSE_THRESH = 0.65  # keep-floor — CT-measured: vendor 1x1 items bottom out
                      # at 0.703 half-res; 0.65 keeps them with margin. False
                      # coarse survivors cost one cheap local fine match each.
# Windowed 1440p moves the Ritual panel left and down relative to the old
# full-output crop. Try these panel-shaped crops before falling back.
RITUAL_GRID_REGION = _grid.RITUAL_GRID_REGION
RITUAL_COLS = _grid.RITUAL_COLS
RITUAL_ROWS = _grid.RITUAL_ROWS
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


_LOG = logging.getLogger("waystone.uniquescan")
_corpus_cache: dict | None = None
_corpus_cache_entries: dict[tuple, dict] = {}
_CORPUS_CACHE_LIMIT = 4


class _TemplateGroup(list):
    """List of templates sharing a slot footprint, with cached vector data."""

    descriptor_matrix: np.ndarray | None

    def __init__(self) -> None:
        super().__init__()
        self.descriptor_matrix = None


Grid = _grid.Grid
_ritual_grid = _grid.ritual_grid
_cell_rect = _grid.cell_rect
_cluster_positions = _grid.cluster_positions
_best_grid_origin = _grid.best_grid_origin
_dynamic_ritual_grid = _grid.dynamic_ritual_grid
_occupied_cells = _grid.occupied_cells
_rect_intersection_area = _grid.rect_intersection_area
_cell_has_match = _grid.cell_has_match
_unknown_occupied_cells = _grid.unknown_occupied_cells
_candidate_slots = _grid.candidate_slots
_sparse_item_art_allowed = _grid.sparse_item_art_allowed
_candidate_required_cells = _grid.candidate_required_cells



@dataclass(frozen=True)
class _GridCandidate:
    match: dict
    col: int
    row: int
    slots_w: int
    slots_h: int
    cell_mask: int
    occupied_cells: int
    selection_score: float


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
        robust_tiles = _robust_template_tiles(img)
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
            "robust_tiles": robust_tiles,
            # Half-res copy for the coarse pyramid pass (1/16 the correlation
            # cost of the full-res sweep).
            "gray_half": cv2.resize(gray, None, fx=COARSE, fy=COARSE,
                                    interpolation=cv2.INTER_AREA),
        })
    corpus = list(out)
    entry = {
        "key": key,
        "tmpl": corpus,
        "by_slots": _build_templates_by_slots(corpus),
        "rows_ref": rows,
    }
    _corpus_cache_entries[key] = entry
    while len(_corpus_cache_entries) > _CORPUS_CACHE_LIMIT:
        _corpus_cache_entries.pop(next(iter(_corpus_cache_entries)))
    _corpus_cache = entry
    return corpus


# --- Feature extraction and visual scoring ----------------------------------
def _gray_candidates(gray_small: np.ndarray, templates: list[dict],
                     thresh: float) -> list[tuple[dict, float, tuple[int, int]]]:
    """Coarse-to-fine: candidate locations from a half-res sweep (1/16 the
    correlation cost), each refined by a local full-res gray match that must
    clear `thresh`. Detection quality is set by the fine+color stages; the
    coarse stage only needs to not DROP true items (COARSE_THRESH margin)."""
    half = cv2.resize(gray_small, None, fx=COARSE, fy=COARSE,
                      interpolation=cv2.INTER_AREA)

    def coarse(t):
        th = t["gray_half"]
        if th.shape[0] >= half.shape[0] or th.shape[1] >= half.shape[1]:
            return []
        r = cv2.matchTemplate(half, th, cv2.TM_CCOEFF_NORMED)
        # ALL peaks, not just the best: duplicate omens/uniques on screen are
        # routine (ritual rewards, vendor stock). Iteratively take the max and
        # suppress its template-sized neighbourhood; cap guards degenerate maps.
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

    out = []
    pad = 8
    for t, cloc in cands:
        g = t["gray"]
        cx, cy = int(cloc[0] / COARSE), int(cloc[1] / COARSE)
        y0, y1 = max(0, cy - pad), min(gray_small.shape[0], cy + g.shape[0] + pad)
        x0, x1 = max(0, cx - pad), min(gray_small.shape[1], cx + g.shape[1] + pad)
        win = gray_small[y0:y1, x0:x1]
        if win.shape[0] < g.shape[0] or win.shape[1] < g.shape[1]:
            continue
        r = cv2.matchTemplate(win, g, cv2.TM_CCOEFF_NORMED)
        _, mx, _, loc = cv2.minMaxLoc(r)
        if mx >= thresh:
            out.append((t, float(mx), (x0 + loc[0], y0 + loc[1])))
    return out


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


def _zncc(a: np.ndarray, b: np.ndarray) -> float:
    av = a.astype(np.float32).reshape(-1)
    bv = b.astype(np.float32).reshape(-1)
    av -= float(av.mean())
    bv -= float(bv.mean())
    denom = float(np.linalg.norm(av) * np.linalg.norm(bv))
    if denom <= 1e-6:
        return 0.0
    return float(np.dot(av, bv) / denom)


def _feature_planes(img: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    gray = cv2.equalizeHist(gray)
    gx = cv2.Sobel(gray, cv2.CV_32F, 1, 0, ksize=3)
    gy = cv2.Sobel(gray, cv2.CV_32F, 0, 1, ksize=3)
    grad = cv2.magnitude(gx, gy)
    return gray.astype(np.float32), grad.astype(np.float32)


@lru_cache(maxsize=64)
def _tile_slices(length: int, count: int = 6) -> tuple[tuple[int, int], ...]:
    edges = np.linspace(0, length, count + 1).round().astype(int)
    return tuple(
        (int(edges[i]), int(edges[i + 1]))
        for i in range(count)
        if int(edges[i + 1]) - int(edges[i]) >= 4
    )


def _robust_template_tiles(
    template: np.ndarray,
) -> tuple[tuple[int, int, int, int, np.ndarray, np.ndarray], ...]:
    """Precompute the stable template side of the robust cell comparator."""

    tmpl_gray, tmpl_grad = _feature_planes(template)
    tiles = []
    for y0, y1 in _tile_slices(template.shape[0]):
        for x0, x1 in _tile_slices(template.shape[1]):
            tg = tmpl_gray[y0:y1, x0:x1]
            te = tmpl_grad[y0:y1, x0:x1]
            # Skip low-information catalog tiles. UI decoration can be
            # high-contrast, so template information is the stable criterion.
            if float(tg.std()) < 6.0 and float(te.std()) < 8.0:
                continue
            tiles.append((
                y0,
                y1,
                x0,
                x1,
                _normalized_vector(tg),
                _normalized_vector(te),
            ))
    return tuple(tiles)


def _sample_tile_vector(values: np.ndarray) -> np.ndarray | None:
    """Sample-side normalization of ``_zncc_to_normalized``, cacheable."""

    vector = values.astype(np.float32).reshape(-1)
    vector -= float(vector.mean())
    norm = float(np.sqrt(np.dot(vector, vector)))
    if norm <= 1e-6:
        return None
    return vector / norm


def _zncc_to_normalized(values: np.ndarray, normalized_template: np.ndarray) -> float:
    vector = values.astype(np.float32).reshape(-1)
    vector -= float(vector.mean())
    norm = float(np.sqrt(np.dot(vector, vector)))
    if norm <= 1e-6:
        return 0.0
    return float(np.dot(vector, normalized_template) / norm)


def _robust_visual_score(
    sample: np.ndarray,
    template: np.ndarray,
    template_tiles: tuple[tuple[int, int, int, int, np.ndarray, np.ndarray], ...] | None = None,
    plane_cache: dict | None = None,
    plane_key: tuple | None = None,
) -> float:
    """Decoration-tolerant score for an item cell and clean catalog icon.

    Borders, stack counts, highlight/defer markers, greyout overlays and other
    UI effects are localized.  Instead of masking known colors/shapes, compare
    many local feature tiles and aggregate the best consistent subset.

    Several shortlist candidates usually share the exact same sample patch
    (same correlation peak); ``plane_cache``/``plane_key`` let one window
    classification reuse the sample feature planes across those candidates.
    """

    if sample.shape[:2] != template.shape[:2]:
        sample = cv2.resize(
            sample,
            (template.shape[1], template.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
        plane_key = None
    if plane_cache is not None and plane_key is not None and plane_key in plane_cache:
        sample_gray, sample_grad = plane_cache[plane_key]
    else:
        sample_gray, sample_grad = _feature_planes(sample)
        if plane_cache is not None and plane_key is not None:
            plane_cache[plane_key] = (sample_gray, sample_grad)
    tiles = template_tiles if template_tiles is not None else _robust_template_tiles(template)
    tile_cache = None
    if plane_cache is not None and plane_key is not None:
        tile_cache = plane_cache.setdefault(("tiles", plane_key), {})
    scores: list[float] = []
    for y0, y1, x0, x1, tmpl_gray_vec, tmpl_grad_vec in tiles:
        box = (y0, y1, x0, x1)
        cached = tile_cache.get(box) if tile_cache is not None else None
        if cached is None:
            cached = (
                _sample_tile_vector(sample_gray[y0:y1, x0:x1]),
                _sample_tile_vector(sample_grad[y0:y1, x0:x1]),
            )
            if tile_cache is not None:
                tile_cache[box] = cached
        gray_vec, grad_vec = cached
        gray_score = float(gray_vec @ tmpl_gray_vec) if gray_vec is not None else 0.0
        grad_score = float(grad_vec @ tmpl_grad_vec) if grad_vec is not None else 0.0
        scores.append(0.38 * gray_score + 0.62 * grad_score)
    if len(scores) < 5:
        return 0.0
    ranked = sorted(scores, reverse=True)
    keep = max(5, int(round(len(ranked) * 0.62)))
    core = float(np.mean(ranked[:keep]))
    coverage = sum(score >= 0.30 for score in scores) / len(scores)
    # Coverage prevents one small matching patch from naming an unrelated icon;
    # the trimmed core lets arbitrary localized UI decoration fail harmlessly.
    return max(0.0, min(1.0, 0.82 * core + 0.18 * coverage))


def _overlap(a: dict, b: dict) -> bool:
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy > 0.4 * min(a["w"] * a["h"], b["w"] * b["h"])


def _nms(matches: list[dict]) -> list[dict]:
    matches.sort(key=lambda h: -h["score"])
    kept: list[dict] = []
    for h in matches:
        if all(not _overlap(h, k) for k in kept):
            kept.append(h)
    kept.sort(key=lambda h: -h["price"])
    return kept

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


def _build_templates_by_slots(templates: list[dict]) -> dict[tuple[int, int], _TemplateGroup]:
    out: dict[tuple[int, int], _TemplateGroup] = {}
    for t in templates:
        key = (int(t.get("slots_w") or 1), int(t.get("slots_h") or 1))
        if 1 <= key[0] <= RITUAL_COLS and 1 <= key[1] <= RITUAL_ROWS:
            out.setdefault(key, _TemplateGroup()).append(t)
    for group in out.values():
        if all("descriptor" in t for t in group):
            group.descriptor_matrix = np.asarray(
                [t["descriptor"] for t in group],
                dtype=np.float32,
            )
    return out


def _templates_by_slots(templates: list[dict]) -> dict[tuple[int, int], list[dict]]:
    if _corpus_cache is not None and templates is _corpus_cache.get("tmpl"):
        by_slots = _corpus_cache.get("by_slots")
        if isinstance(by_slots, dict):
            return by_slots
    return _build_templates_by_slots(templates)


def _descriptor_matrix_for_templates(templates: list[dict]) -> np.ndarray:
    matrix = getattr(templates, "descriptor_matrix", None)
    if isinstance(matrix, np.ndarray) and matrix.shape[0] == len(templates):
        return matrix
    return np.asarray([t["descriptor"] for t in templates], dtype=np.float32)


# --- Cell-window candidate matching ------------------------------------------
def _best_template_in_window(
    small: np.ndarray,
    templates: list[dict],
    gray_thresh: float,
    color_thresh: float,
) -> tuple[dict, float, tuple[int, int]] | None:
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
    best: tuple[dict, float, tuple[int, int]] | None = None
    for t in templates:
        g = t["gray"]
        if g.shape[0] >= gray.shape[0] or g.shape[1] >= gray.shape[1]:
            continue
        r = cv2.matchTemplate(gray, g, cv2.TM_CCOEFF_NORMED)
        _, gray_score, _, loc = cv2.minMaxLoc(r)
        if gray_score < gray_thresh:
            continue
        score = _color_verify(small, t, loc, pad=4)
        if score < color_thresh:
            continue
        if best is None or score > best[1]:
            best = (t, float(score), loc)
    return best


def _robust_candidates_in_window(
    window: np.ndarray,
    templates: list[dict],
    limit: int | None = RITUAL_PREFILTER_LIMIT,
    gray_limit: int | None = RITUAL_FULL_GRAY_LIMIT,
    verify_limit: int | None = RITUAL_SHORTLIST_VERIFY_LIMIT,
) -> list[tuple[dict, float, float, tuple[int, int]]]:
    if not templates:
        return []
    if limit is None:
        candidate_indices = np.arange(len(templates))
    else:
        descriptor = _visual_descriptor(window)
        matrix = _descriptor_matrix_for_templates(templates)
        pre_scores = matrix @ descriptor
        if len(pre_scores) > limit:
            candidate_indices = np.argpartition(pre_scores, -limit)[-limit:]
        else:
            candidate_indices = np.arange(len(pre_scores))
    gray_window = cv2.cvtColor(window, cv2.COLOR_BGR2GRAY)
    gray_candidates: list[tuple[float, tuple[int, int], dict]] = []
    selected = [templates[int(index)] for index in candidate_indices]
    # The batched path materializes a (positions x template-px) patch
    # matrix per window; that beats per-template cv2 calls only while the
    # matrix stays small. Past the cap, cv2's FFT correlation wins by a
    # wide margin (measured 119ms/window batched vs ~8ms scalar on a
    # small-cell ritual grid), so gate on materialization cost, not just
    # template count.
    max_h = max((t["gray"].shape[0] for t in selected), default=0)
    max_w = max((t["gray"].shape[1] for t in selected), default=0)
    positions = max(0, gray_window.shape[0] - max_h + 1) * max(
        0, gray_window.shape[1] - max_w + 1
    )
    batch_cells = positions * max_h * max_w
    if len(selected) > 32 and batch_cells <= 1_500_000:
        gray_candidates = [
            item
            for item in _batch.batched_gray_candidates(gray_window, selected)
        ]
    else:
        for t in selected:
            g = t["gray"]
            if g.shape[0] > gray_window.shape[0] or g.shape[1] > gray_window.shape[1]:
                continue
            r = cv2.matchTemplate(gray_window, g, cv2.TM_CCOEFF_NORMED)
            _, gray_score, _, loc = cv2.minMaxLoc(r)
            gray_candidates.append((float(gray_score), loc, t))
    # Robust/color verification is the expensive stage; run it only on the
    # strongest gray candidates. The accepted template overwhelmingly sits at
    # the top of the gray ranking; cells whose winner does not are recovered
    # by the broad-retry path.
    effective_limit = gray_limit if limit is None else verify_limit
    if effective_limit is not None and len(gray_candidates) > effective_limit:
        gray_candidates.sort(key=lambda item: item[0], reverse=True)
        gray_candidates = gray_candidates[:effective_limit]

    out: list[tuple[dict, float, float, tuple[int, int]]] = []
    if len(gray_candidates) > 16:
        items = [(t, loc) for _score, loc, t in gray_candidates]
        robust_scores = _batch.batched_robust_scores(
            window, items, _feature_planes, _tile_slices
        )
        color_scores = _batch.batched_color_verify(window, items, pad=3)
        for (gray_score, loc, t), robust_score, color_score in zip(
            gray_candidates, robust_scores, color_scores
        ):
            if robust_score is None:
                g = t["gray"]
                patch = window[loc[1]:loc[1] + g.shape[0], loc[0]:loc[0] + g.shape[1]]
                robust_score = _robust_visual_score(
                    patch, t["color"], t.get("robust_tiles")
                )
            out.append((t, max(robust_score, color_score), gray_score, loc))
    else:
        plane_cache: dict[tuple, tuple[np.ndarray, np.ndarray]] = {}
        for gray_score, loc, t in gray_candidates:
            g = t["gray"]
            y0, y1 = loc[1], loc[1] + g.shape[0]
            x0, x1 = loc[0], loc[0] + g.shape[1]
            patch = window[y0:y1, x0:x1]
            robust_score = _robust_visual_score(
                patch,
                t["color"],
                t.get("robust_tiles"),
                plane_cache=plane_cache,
                plane_key=(x0, y0, x1, y1),
            )
            color_score = _color_verify(window, t, loc, pad=3)
            score = max(robust_score, color_score)
            out.append((t, score, gray_score, loc))
    out.sort(key=lambda item: item[1], reverse=True)
    return out


# --- Cell acceptance and decorated-cell recovery -----------------------------
def _cell_acceptance_thresholds(slots_w: int, slots_h: int) -> tuple[float, float]:
    if slots_w * slots_h <= 1:
        return RITUAL_1X1_COLOR_THRESH, RITUAL_1X1_SCORE_MARGIN
    return RITUAL_MULTI_COLOR_THRESH, RITUAL_MULTI_SCORE_MARGIN


def _cell_score_margin(score: float, base_margin: float) -> float:
    """Required lead over the next candidate for a Ritual cell.

    The fast cell matcher sees clean catalog icons through game UI decoration:
    selected/deferred styling, stack text, greyout, and Ritual borders. Similar
    variants can all score highly, especially one-slot Omens. Keep the original
    conservative margin near the minimum acceptance floor, but allow a smaller
    lead for strong absolute matches so valid variants are not downgraded to
    marker-only.
    """

    if score >= 0.80:
        return min(base_margin, 0.018)
    if score >= 0.76:
        return min(base_margin, 0.022)
    if score >= 0.72:
        return min(base_margin, 0.028)
    return base_margin


def _accepted_cell_candidate(
    candidates: list[tuple[dict, float, float, tuple[int, int]]],
    slots_w: int,
    slots_h: int,
) -> tuple[dict, float, tuple[int, int]] | None:
    if not candidates:
        return None
    t, score, _gray_score, loc = candidates[0]
    min_score, min_margin = _cell_acceptance_thresholds(slots_w, slots_h)
    if score < min_score:
        return None
    if len(candidates) > 1 and score - candidates[1][1] < _cell_score_margin(
        score,
        min_margin,
    ):
        return None
    return t, score, loc


def _cell_has_warm_ritual_decoration(window: np.ndarray) -> bool:
    """Detect strong warm-gold Ritual/deferred decoration in a one-slot cell.

    This is intentionally used only after ordinary 1x1 matching fails. The
    marker can cover enough of the clean icon that gray-correlation pruning
    drops the correct template, so decorated cells get one broader robust pass.
    """

    hsv = cv2.cvtColor(window, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    warm = (hue >= 5) & (hue <= 42) & (sat >= 45) & (val >= 80)
    if float(np.mean(warm)) < 0.15:
        return False
    bottom = int(round(warm.shape[0] * 0.45))
    right = int(round(warm.shape[1] * 0.45))
    return float(np.mean(warm[bottom:, right:])) >= 0.18


def _accepted_decorated_1x1_candidate(
    candidates: list[tuple[dict, float, float, tuple[int, int]]],
) -> tuple[dict, float, tuple[int, int]] | None:
    if not candidates:
        return None
    t, score, _gray_score, loc = candidates[0]
    if score < RITUAL_DECORATED_1X1_SCORE_THRESH:
        return None
    if (
        len(candidates) > 1
        and score - candidates[1][1] < RITUAL_DECORATED_1X1_SCORE_MARGIN
    ):
        return None
    return t, score, loc


def _ritual_decoration_stable_mask(sample: np.ndarray) -> np.ndarray:
    """Pixels likely to be item art rather than Ritual cell chrome."""

    hsv = cv2.cvtColor(sample, cv2.COLOR_BGR2HSV)
    hue = hsv[:, :, 0]
    sat = hsv[:, :, 1]
    val = hsv[:, :, 2]
    warm_ui = (hue >= 5) & (hue <= 45) & (sat >= 45) & (val >= 70)
    yy, xx = np.indices(val.shape)
    top_left = (yy < sample.shape[0] * 0.35) & (xx < sample.shape[1] * 0.35)
    bright_stack_text = (sat <= 70) & (val >= 150) & top_left
    unstable = warm_ui | bright_stack_text
    unstable = cv2.dilate(
        unstable.astype(np.uint8),
        np.ones((3, 3), np.uint8),
        iterations=1,
    ).astype(bool)
    return ~unstable


def _masked_feature_score(
    values: np.ndarray,
    template_values: np.ndarray,
) -> float:
    template_vector = _normalized_vector(template_values)
    if not np.any(template_vector):
        return 0.0
    value_vector = values.astype(np.float32).reshape(-1)
    value_vector -= float(value_vector.mean())
    norm = float(np.sqrt(np.dot(value_vector, value_vector)))
    if norm <= 1e-6:
        return 0.0
    return float(np.dot(value_vector / norm, template_vector))


def _template_feature_planes(template_entry: dict | None, template: np.ndarray):
    """Feature planes for catalog art, cached on the template entry."""

    if template_entry is None:
        return _feature_planes(template)
    cached = template_entry.get("_masked_planes")
    if cached is None:
        cached = _feature_planes(template)
        template_entry["_masked_planes"] = cached
    return cached


def _masked_ritual_visual_score(
    sample: np.ndarray,
    template: np.ndarray,
    template_entry: dict | None = None,
    sample_cache: dict | None = None,
    sample_key: tuple | None = None,
) -> float:
    """Decoration-aware score for one possible template alignment.

    ``sample_cache``/``sample_key`` share the per-alignment stable mask and
    feature planes across the many templates scored at the same alignment;
    ``template_entry`` caches the catalog-side planes across windows.
    """

    if sample.shape[:2] != template.shape[:2]:
        sample = cv2.resize(
            sample,
            (template.shape[1], template.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
        sample_key = None
    if sample_cache is not None and sample_key is not None and sample_key in sample_cache:
        stable, sample_gray, sample_grad = sample_cache[sample_key]
    else:
        stable = _ritual_decoration_stable_mask(sample)
        sample_gray, sample_grad = _feature_planes(sample)
        if sample_cache is not None and sample_key is not None:
            sample_cache[sample_key] = (stable, sample_gray, sample_grad)
    template_gray, template_grad = _template_feature_planes(template_entry, template)
    tile_cache = None
    if sample_cache is not None and sample_key is not None:
        tile_cache = sample_cache.setdefault(("masked-tiles", sample_key), {})
    scores: list[float] = []
    for y0, y1 in _tile_slices(template.shape[0]):
        for x0, x1 in _tile_slices(template.shape[1]):
            box = (y0, y1, x0, x1)
            cached = tile_cache.get(box) if tile_cache is not None else None
            if cached is None:
                mask = stable[y0:y1, x0:x1]
                if float(np.mean(mask)) < 0.45:
                    cached = (None, None, None)
                else:
                    cached = (
                        mask,
                        _sample_tile_vector(sample_gray[y0:y1, x0:x1][mask]),
                        _sample_tile_vector(sample_grad[y0:y1, x0:x1][mask]),
                    )
                if tile_cache is not None:
                    tile_cache[box] = cached
            mask, sample_gray_vec, sample_grad_vec = cached
            if mask is None:
                continue
            tmpl_gray_tile = template_gray[y0:y1, x0:x1][mask]
            tmpl_grad_tile = template_grad[y0:y1, x0:x1][mask]
            if float(tmpl_gray_tile.std()) < 5.0 and float(tmpl_grad_tile.std()) < 7.0:
                continue
            tmpl_gray_vec = _normalized_vector(tmpl_gray_tile)
            tmpl_grad_vec = _normalized_vector(tmpl_grad_tile)
            gray_score = (
                float(sample_gray_vec @ tmpl_gray_vec)
                if sample_gray_vec is not None and np.any(tmpl_gray_vec)
                else 0.0
            )
            grad_score = (
                float(sample_grad_vec @ tmpl_grad_vec)
                if sample_grad_vec is not None and np.any(tmpl_grad_vec)
                else 0.0
            )
            scores.append(0.38 * gray_score + 0.62 * grad_score)
    if len(scores) < 5:
        return 0.0
    ranked = sorted(scores, reverse=True)
    keep = max(5, int(round(len(ranked) * 0.62)))
    core = float(np.mean(ranked[:keep]))
    coverage = sum(score >= 0.30 for score in scores) / len(scores)
    return max(0.0, min(1.0, 0.82 * core + 0.18 * coverage))


def _best_masked_ritual_visual_score(
    window: np.ndarray,
    template: dict,
    loc: tuple[int, int],
    radius: int = RITUAL_DECORATED_CONTEXT_LOCAL_RADIUS,
    sample_cache: dict | None = None,
) -> tuple[float, tuple[int, int]]:
    """Best decoration-aware score near the ordinary gray-correlation location."""

    art = template["color"]
    max_y = window.shape[0] - art.shape[0]
    max_x = window.shape[1] - art.shape[1]
    if max_x < 0 or max_y < 0:
        return 0.0, loc
    best_score = 0.0
    best_loc = loc
    x_start = max(0, loc[0] - radius)
    x_stop = min(max_x, loc[0] + radius)
    y_start = max(0, loc[1] - radius)
    y_stop = min(max_y, loc[1] + radius)
    for y in range(y_start, y_stop + 1):
        for x in range(x_start, x_stop + 1):
            patch = window[y:y + art.shape[0], x:x + art.shape[1]]
            score = _masked_ritual_visual_score(
                patch,
                art,
                template_entry=template,
                sample_cache=sample_cache,
                sample_key=(x, y, art.shape[1], art.shape[0]),
            )
            if score > best_score:
                best_score = score
                best_loc = (x, y)
    return best_score, best_loc


def _masked_ritual_color_support_score(
    sample: np.ndarray,
    template: np.ndarray,
    template_entry: dict | None = None,
) -> float:
    """Local color-texture support for a decorated Ritual alignment.

    This is not strong enough to identify an item by itself. It only prevents
    grayscale structure from accepting a candidate whose foreground color
    texture has no support under the stable, non-decoration pixels.
    """

    if sample.shape[:2] != template.shape[:2]:
        sample = cv2.resize(
            sample,
            (template.shape[1], template.shape[0]),
            interpolation=cv2.INTER_AREA,
        )
    stable = _ritual_decoration_stable_mask(sample)
    sample_lab = cv2.cvtColor(sample, cv2.COLOR_BGR2LAB)
    if template_entry is not None:
        cached = template_entry.get("_masked_lab")
        if cached is None:
            cached = (
                cv2.cvtColor(template, cv2.COLOR_BGR2LAB),
                cv2.cvtColor(template, cv2.COLOR_BGR2GRAY),
            )
            template_entry["_masked_lab"] = cached
        template_lab, template_gray = cached
    else:
        template_lab = cv2.cvtColor(template, cv2.COLOR_BGR2LAB)
        template_gray = cv2.cvtColor(template, cv2.COLOR_BGR2GRAY)
    scores: list[float] = []
    for y0, y1 in _tile_slices(template.shape[0]):
        for x0, x1 in _tile_slices(template.shape[1]):
            mask = stable[y0:y1, x0:x1] & (template_gray[y0:y1, x0:x1] > 16)
            if float(np.mean(mask)) < 0.25:
                continue
            score = (
                0.45
                * _masked_feature_score(
                    sample_lab[y0:y1, x0:x1, 1][mask],
                    template_lab[y0:y1, x0:x1, 1][mask],
                )
                + 0.45
                * _masked_feature_score(
                    sample_lab[y0:y1, x0:x1, 2][mask],
                    template_lab[y0:y1, x0:x1, 2][mask],
                )
                + 0.10
                * _masked_feature_score(
                    sample_lab[y0:y1, x0:x1, 0][mask],
                    template_lab[y0:y1, x0:x1, 0][mask],
                )
            )
            scores.append(score)
    if len(scores) < 3:
        return 0.0
    ranked = sorted(scores, reverse=True)
    keep = max(3, int(round(len(ranked) * 0.55)))
    return max(0.0, min(1.0, (float(np.mean(ranked[:keep])) + 1.0) / 2.0))


def _decoration_context_score(
    window: np.ndarray,
    template: dict,
    loc: tuple[int, int],
    sample_cache: dict | None = None,
) -> tuple[float, tuple[int, int]]:
    structure_score, best_loc = _best_masked_ritual_visual_score(
        window, template, loc, sample_cache=sample_cache
    )
    art = template["color"]
    patch = window[
        best_loc[1]:best_loc[1] + art.shape[0],
        best_loc[0]:best_loc[0] + art.shape[1],
    ]
    color_support = _masked_ritual_color_support_score(
        patch, art, template_entry=template
    )
    support = min(1.0, color_support / RITUAL_DECORATED_CONTEXT_COLOR_FULL_SUPPORT)
    multiplier = 1.0 - RITUAL_DECORATED_CONTEXT_COLOR_PENALTY * (1.0 - support)
    return structure_score * multiplier, best_loc


def _decorated_context_templates(templates: list[dict]) -> list[dict]:
    """Templates whose source metadata is specific to Ritual rewards."""

    return [
        template
        for template in templates
        if str(template.get("sourceCategory") or "").strip().lower() == "ritual"
    ]


def _decorated_context_candidates_in_window(
    window: np.ndarray,
    templates: list[dict],
    *,
    limit: int = RITUAL_DECORATED_CONTEXT_CANDIDATE_LIMIT,
) -> list[tuple[dict, float, float, tuple[int, int]]]:
    ritual_templates = _decorated_context_templates(templates)
    if not ritual_templates:
        return []
    candidates = _robust_candidates_in_window(
        window,
        ritual_templates,
        limit=None,
        gray_limit=None,
    )
    rescored: list[tuple[dict, float, float, tuple[int, int]]] = []
    sample_cache: dict = {}
    for template, _score, gray_score, loc in candidates[:limit]:
        score, best_loc = _decoration_context_score(
            window, template, loc, sample_cache=sample_cache
        )
        rescored.append((template, score, gray_score, best_loc))
    rescored.sort(key=lambda item: item[1], reverse=True)
    return rescored


def _accepted_decorated_context_candidate(
    candidates: list[tuple[dict, float, float, tuple[int, int]]],
) -> tuple[dict, float, tuple[int, int]] | None:
    if not candidates:
        return None
    template, score, _gray_score, loc = candidates[0]
    if score < RITUAL_DECORATED_CONTEXT_SCORE_THRESH:
        return None
    if (
        len(candidates) > 1
        and score - candidates[1][1] < RITUAL_DECORATED_CONTEXT_SCORE_MARGIN
    ):
        return None
    return template, score, loc


def _accepted_cell_candidate_in_window(
    window: np.ndarray,
    templates: list[dict],
    slots_w: int,
    slots_h: int,
    *,
    full_search: bool = False,
) -> tuple[dict, float, tuple[int, int]] | None:
    # Multi-slot Ritual rewards are much more descriptor-stable than one-slot
    # Omens/currency. Keep the broad 1x1 shortlist, but classify multi-slot
    # windows from a smaller descriptor-ranked candidate set before exact-cover
    # selection. This cuts the dominant Ritual cost without changing the
    # acceptance thresholds or adding item-specific rules.
    prefilter_limit = (
        RITUAL_PREFILTER_LIMIT
        if slots_w * slots_h == 1
        else RITUAL_MULTI_PREFILTER_LIMIT
    )
    candidates = _robust_candidates_in_window(
        window,
        templates,
        limit=None if full_search else prefilter_limit,
    )
    accepted = _accepted_cell_candidate(candidates, slots_w, slots_h)
    if slots_w * slots_h != 1:
        return accepted
    if accepted is not None and not full_search and accepted[1] >= RITUAL_1X1_FAST_TRUST_SCORE:
        return accepted

    # A low-confidence 1x1 fast match is a common false-positive shape match:
    # the descriptor prefilter can miss the true icon while a visually similar
    # expensive item barely clears the floor. Verify these cases against the
    # broader gray-first search; if broad search cannot produce a clear winner,
    # prefer marker-only over a wrong name.
    if full_search:
        full_accepted = accepted
    else:
        full_candidates = _robust_candidates_in_window(window, templates, limit=None)
        full_accepted = _accepted_cell_candidate(full_candidates, slots_w, slots_h)
        if full_accepted is not None:
            return full_accepted
    if not _cell_has_warm_ritual_decoration(window):
        return full_accepted
    context_accepted = _accepted_decorated_context_candidate(
        _decorated_context_candidates_in_window(window, templates)
    )
    if context_accepted is not None:
        return context_accepted
    decorated_accepted = _accepted_decorated_1x1_candidate(
        _robust_candidates_in_window(
            window,
            templates,
            limit=None,
            gray_limit=None,
        )
    )
    return decorated_accepted or full_accepted


def _match_overlaps_target_cell(
    match_rect: tuple[int, int, int, int],
    target_rect: tuple[int, int, int, int],
) -> bool:
    target_area = max(
        1, (target_rect[2] - target_rect[0]) * (target_rect[3] - target_rect[1])
    )
    if _rect_intersection_area(match_rect, target_rect) < target_area * 0.45:
        return False
    cx = (match_rect[0] + match_rect[2]) / 2.0
    cy = (match_rect[1] + match_rect[3]) / 2.0
    return target_rect[0] <= cx <= target_rect[2] and target_rect[1] <= cy <= target_rect[3]


def _cell_match_from_template(
    t: dict,
    score: float,
    loc: tuple[int, int],
    grid: Grid,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
    x0: int,
    y0: int,
    factor: float,
) -> dict | None:
    target_rect = _cell_rect(grid, col, row, slots_w, slots_h, pad=0.0)
    match_x = x0 + int(loc[0] / factor)
    match_y = y0 + int(loc[1] / factor)
    match_w = int(t["gray"].shape[1] / factor)
    match_h = int(t["gray"].shape[0] / factor)
    if not _match_overlaps_target_cell(
        (match_x, match_y, match_x + match_w, match_y + match_h),
        target_rect,
    ):
        return None
    return {
        **_template_match_fields(t, score),
        "x": match_x,
        "y": match_y,
        "w": match_w,
        "h": match_h,
        "cell": (col, row),
    }


def _cell_window(
    crop: np.ndarray,
    grid: Grid,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
) -> tuple[np.ndarray, int, int] | None:
    x0, y0, x1, y1 = _cell_rect(grid, col, row, slots_w, slots_h, pad=0.12)
    x0, y0 = max(0, x0), max(0, y0)
    x1, y1 = min(crop.shape[1], x1), min(crop.shape[0], y1)
    window = crop[y0:y1, x0:x1]
    if (
        window.shape[0] < grid.cell * slots_h * 0.75
        or window.shape[1] < grid.cell * slots_w * 0.75
    ):
        return None
    return window, x0, y0


def _classify_cell_window(
    crop: np.ndarray,
    group: list[dict],
    grid: Grid,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
    factor: float,
    *,
    full_search: bool = False,
) -> dict | None:
    cell_window = _cell_window(crop, grid, col, row, slots_w, slots_h)
    if cell_window is None:
        return None
    window, x0, y0 = cell_window
    small = cv2.resize(
        window,
        None,
        fx=factor,
        fy=factor,
        interpolation=cv2.INTER_AREA,
    )
    # Cross-press reuse: the accept/reject decision is a pure function of the
    # resized window bytes, the template group, and the search mode. The
    # cached value carries the group reference so a rebuilt corpus (or a
    # recycled id) can never serve stale template objects; position is not
    # part of the key, so an unchanged cell hits even after the panel scrolls.
    cache_key = scan_cache.digest(
        small, extra=f"{slots_w}x{slots_h}:{int(full_search)}"
    )
    hit = scan_cache.lookup("cell", cache_key)
    if hit is not None and hit[0] is group:
        accepted = hit[1]
    else:
        accepted = _accepted_cell_candidate_in_window(
            small,
            group,
            slots_w,
            slots_h,
            full_search=full_search,
        )
        scan_cache.store("cell", cache_key, (group, accepted))
    if accepted is None:
        return None
    t, score, loc = accepted
    return _cell_match_from_template(
        t,
        score,
        loc,
        grid,
        col,
        row,
        slots_w,
        slots_h,
        x0,
        y0,
        factor,
    )


def _grid_candidate_mask(
    grid: Grid,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
) -> int:
    mask = 0
    for yy in range(row, min(row + slots_h, grid.rows)):
        for xx in range(col, min(col + slots_w, grid.cols)):
            mask |= 1 << (yy * grid.cols + xx)
    return mask


def _grid_candidate_selection_score(
    match: dict,
    occupied: np.ndarray,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
) -> tuple[float, int]:
    cells = occupied[row:row + slots_h, col:col + slots_w]
    occupied_cells = int(np.count_nonzero(cells))
    score = float(match.get("score") or 0.0)
    # Reward evidence explained, not merely rectangle size. This lets a true
    # sparse multi-slot item beat same-cell fragments, while a weak large false
    # positive does not automatically defeat several strong 1x1 candidates.
    selection_score = score * max(1, occupied_cells)
    selection_score += 0.03 * (slots_w * slots_h)
    if score >= 0.80:
        selection_score += 0.04 * occupied_cells
    return selection_score, occupied_cells


# --- Grid candidate selection -------------------------------------------------
def _grid_candidate_is_strong_multislot(candidate: _GridCandidate) -> bool:
    if candidate.slots_w * candidate.slots_h <= 1:
        return False
    score = float(candidate.match.get("score") or 0.0)
    support_ratio = candidate.occupied_cells / max(1, candidate.slots_w * candidate.slots_h)
    if score >= 0.82:
        return True
    if score >= 0.76 and support_ratio >= 0.50:
        return True
    return score >= RITUAL_CELL_CLAIM_SCORE and support_ratio >= 0.75


def _grid_candidate_cells_mask(candidates: list[_GridCandidate]) -> int:
    mask = 0
    for candidate in candidates:
        mask |= candidate.cell_mask
    return mask


def _grid_cell_bit(grid: Grid, col: int, row: int) -> int:
    return 1 << (row * grid.cols + col)


def _grid_candidate_from_match(
    match: dict | None,
    grid: Grid,
    occupied: np.ndarray,
    col: int,
    row: int,
    slots_w: int,
    slots_h: int,
) -> _GridCandidate | None:
    if match is None:
        return None
    selection_score, occupied_cells = _grid_candidate_selection_score(
        match,
        occupied,
        col,
        row,
        slots_w,
        slots_h,
    )
    return _GridCandidate(
        match=match,
        col=col,
        row=row,
        slots_w=slots_w,
        slots_h=slots_h,
        cell_mask=_grid_candidate_mask(grid, col, row, slots_w, slots_h),
        occupied_cells=occupied_cells,
        selection_score=selection_score,
    )


def _grid_candidate_sort_key(candidate: _GridCandidate) -> tuple[float, int, float, int, int]:
    return (
        candidate.selection_score,
        candidate.occupied_cells,
        float(candidate.match.get("score") or 0.0),
        candidate.slots_w * candidate.slots_h,
        -candidate.row * RITUAL_COLS - candidate.col,
    )


def _greedy_grid_candidate_selection(
    candidates: list[_GridCandidate],
) -> list[_GridCandidate]:
    selected: list[_GridCandidate] = []
    used_mask = 0
    for candidate in sorted(candidates, key=_grid_candidate_sort_key, reverse=True):
        if used_mask & candidate.cell_mask:
            continue
        selected.append(candidate)
        used_mask |= candidate.cell_mask
    return selected


def _exact_grid_candidate_selection(
    candidates: list[_GridCandidate],
) -> list[_GridCandidate]:
    ordered = sorted(candidates, key=_grid_candidate_sort_key, reverse=True)
    suffix_bound = [0.0] * (len(ordered) + 1)
    for index in range(len(ordered) - 1, -1, -1):
        suffix_bound[index] = suffix_bound[index + 1] + max(
            0.0,
            ordered[index].selection_score,
        )

    best_key: tuple[float, int, int] = (0.0, 0, 0)
    best: list[_GridCandidate] = []

    def visit(
        index: int,
        used_mask: int,
        score: float,
        occupied_cells: int,
        chosen: list[_GridCandidate],
    ) -> None:
        nonlocal best_key, best
        if score + suffix_bound[index] < best_key[0] - 1e-9:
            return
        if index >= len(ordered):
            key = (score, occupied_cells, -len(chosen))
            if key > best_key:
                best_key = key
                best = chosen.copy()
            return

        candidate = ordered[index]
        if not (used_mask & candidate.cell_mask):
            chosen.append(candidate)
            visit(
                index + 1,
                used_mask | candidate.cell_mask,
                score + candidate.selection_score,
                occupied_cells + candidate.occupied_cells,
                chosen,
            )
            chosen.pop()
        visit(index + 1, used_mask, score, occupied_cells, chosen)

    visit(0, 0, 0.0, 0, [])
    return best


def _grid_candidate_components(
    candidates: list[_GridCandidate],
) -> list[list[_GridCandidate]]:
    remaining = set(range(len(candidates)))
    components: list[list[_GridCandidate]] = []
    while remaining:
        first = remaining.pop()
        stack = [first]
        component_indexes = [first]
        component_mask = candidates[first].cell_mask
        while stack:
            _current = stack.pop()
            overlaps = [
                index
                for index in list(remaining)
                if candidates[index].cell_mask & component_mask
            ]
            for index in overlaps:
                remaining.remove(index)
                stack.append(index)
                component_indexes.append(index)
                component_mask |= candidates[index].cell_mask
        components.append([candidates[index] for index in component_indexes])
    return components


def _select_grid_candidate_objects(
    candidates: list[_GridCandidate],
    *,
    exact_limit: int = 24,
) -> list[_GridCandidate]:
    selected: list[_GridCandidate] = []
    for component in _grid_candidate_components(candidates):
        if len(component) <= exact_limit:
            selected.extend(_exact_grid_candidate_selection(component))
        else:
            selected.extend(_greedy_grid_candidate_selection(component))
    return selected


# --- Grid scanning (the ritual/merchant hot path) -----------------------------
def _scan_grid_cells(
    crop: np.ndarray,
    rows: dict,
    grid: Grid,
) -> list[dict]:
    """Classify a localized Ritual/Merchant item grid.

    The hot path is deliberately staged:
      1. generate geometry-bounded slot candidates from occupied-cell evidence;
      2. classify those candidates in parallel with the existing matcher;
      3. globally select the best non-overlapping explanation of the grid;
      4. run the expensive one-slot fallback only for cells still unexplained.

    This avoids the older failure mode where a greedy early claim or a missing
    sparse footprint made one real item become several marker-only cells.
    """

    templates = _load_corpus(rows)
    by_slots = _templates_by_slots(templates)
    if not by_slots:
        return []
    occupied = _occupied_cells(crop, grid)
    factor = PXSLOT / grid.cell
    slot_groups = sorted(
        by_slots.items(),
        key=lambda item: (-(item[0][0] * item[0][1]), -item[0][1], -item[0][0]),
    )
    executor = _pool() if RITUAL_CELL_WORKERS > 1 else None

    def should_retry_sparse_slot(col: int, row: int, slots_w: int, slots_h: int) -> bool:
        if not _sparse_item_art_allowed(slots_w, slots_h):
            return False
        cells = occupied[row:row + slots_h, col:col + slots_w]
        filled = int(np.count_nonzero(cells))
        required = _candidate_required_cells(slots_w, slots_h)
        # Fully-filled windows retry too: the full search rescues art whose
        # alignment defeats the strict pass (dark staff shafts), not just
        # sparse footprints. Footprint-tint occupancy made real multi-slot
        # items read as fully filled, which used to skip this retry and
        # silently lose them.
        return required <= filled

    try:
        one_slot_group = by_slots.get((1, 1), [])
        def classify_slot(
            task: tuple[int, int, list[dict], int, int],
            *,
            full_search: bool = False,
        ):
            slots_w, slots_h, group, col, row = task
            match = _classify_cell_window(
                crop,
                group,
                grid,
                col,
                row,
                slots_w,
                slots_h,
                factor,
                full_search=full_search,
            )
            return _grid_candidate_from_match(
                match,
                grid,
                occupied,
                col,
                row,
                slots_w,
                slots_h,
            )

        def classify_slot_fast(task: tuple[int, int, list[dict], int, int]):
            slots_w, slots_h, _group, col, row = task
            candidate = classify_slot(task)
            retry_task = (
                task
                if candidate is None
                and should_retry_sparse_slot(col, row, slots_w, slots_h)
                else None
            )
            return candidate, retry_task

        def candidate_has_unexplained_occupied_cell(
            task: tuple[int, int, list[dict], int, int],
            selected_mask: int,
        ) -> bool:
            slots_w, slots_h, _group, col, row = task
            for yy in range(row, row + slots_h):
                for xx in range(col, col + slots_w):
                    if not bool(occupied[yy, xx]):
                        continue
                    if not (selected_mask & _grid_cell_bit(grid, xx, yy)):
                        return True
            return False

        multi_candidates: list[_GridCandidate] = []
        sparse_retry_tasks: list[tuple[int, int, list[dict], int, int]] = []
        strong_multislot_mask = 0
        for (slots_w, slots_h), group in slot_groups:
            if (slots_w, slots_h) == (1, 1):
                continue
            shape_tasks = [
                (slots_w, slots_h, group, col, row)
                for col, row in _candidate_slots(occupied, slots_w, slots_h)
                if not (
                    strong_multislot_mask
                    & _grid_candidate_mask(grid, col, row, slots_w, slots_h)
                )
            ]
            if not shape_tasks:
                continue
            if executor is not None and len(shape_tasks) > 1:
                candidate_results = executor.map(classify_slot_fast, shape_tasks)
            else:
                candidate_results = map(classify_slot_fast, shape_tasks)
            for candidate, retry_task in candidate_results:
                if candidate is not None:
                    multi_candidates.append(candidate)
                if retry_task is not None:
                    sparse_retry_tasks.append(retry_task)
            selected_multi = _select_grid_candidate_objects(multi_candidates)
            strong_multislot_mask = _grid_candidate_cells_mask([
                candidate
                for candidate in selected_multi
                if _grid_candidate_is_strong_multislot(candidate)
            ])

        candidates = list(multi_candidates)
        if one_slot_group:
            one_slot_tasks = [
                (1, 1, one_slot_group, col, row)
                for col, row in _candidate_slots(occupied, 1, 1)
                if not (strong_multislot_mask & _grid_cell_bit(grid, col, row))
            ]
            if executor is not None and len(one_slot_tasks) > 1:
                one_slot_results = executor.map(classify_slot, one_slot_tasks)
            else:
                one_slot_results = map(classify_slot, one_slot_tasks)
            candidates.extend(
                candidate
                for candidate in one_slot_results
                if candidate is not None
            )

        selected_candidates = _select_grid_candidate_objects(candidates)
        selected_mask = _grid_candidate_cells_mask(selected_candidates)

        retry_tasks = [
            task
            for task in sparse_retry_tasks
            if candidate_has_unexplained_occupied_cell(task, selected_mask)
        ]
        if retry_tasks:
            if executor is not None and len(retry_tasks) > 1:
                retry_results = executor.map(
                    lambda task: classify_slot(task, full_search=True),
                    retry_tasks,
                )
            else:
                retry_results = (
                    classify_slot(task, full_search=True)
                    for task in retry_tasks
                )
            retry_candidates = [
                candidate
                for candidate in retry_results
                if candidate is not None
            ]
            if retry_candidates:
                selected_candidates = _select_grid_candidate_objects(
                    selected_candidates + retry_candidates
                )
                selected_mask = _grid_candidate_cells_mask(selected_candidates)

        kept = [candidate.match for candidate in selected_candidates]

        if one_slot_group:
            fallback_slots = []
            for col, row in _candidate_slots(occupied, 1, 1):
                if selected_mask & _grid_cell_bit(grid, col, row):
                    continue
                fallback_slots.append((col, row))

            def classify_fallback(position):
                col, row = position
                match = _classify_cell_window(
                    crop,
                    one_slot_group,
                    grid,
                    col,
                    row,
                    1,
                    1,
                    factor,
                    full_search=True,
                )
                return _grid_candidate_from_match(
                    match,
                    grid,
                    occupied,
                    col,
                    row,
                    1,
                    1,
                )

            if executor is not None and len(fallback_slots) > 1:
                results = executor.map(classify_fallback, fallback_slots)
            else:
                results = map(classify_fallback, fallback_slots)
            fallback_candidates = [
                candidate
                for candidate in results
                if candidate is not None
            ]
            if fallback_candidates:
                selected_candidates = _select_grid_candidate_objects(
                    selected_candidates + fallback_candidates
                )
                kept = [candidate.match for candidate in selected_candidates]

        kept.sort(key=lambda match: -float(match.get("price") or 0.0))
        return kept
    finally:
        # The worker pool is shared and reused across presses; there is
        # nothing per-scan to shut down anymore.
        pass


MID = 0.69  # shared coarse-pass factor: midpoint of FACTORS (they differ ~3%,
            # invisible at half res, so ONE coarse sweep serves both fine passes)


def _ritual_grid_factors(crop: np.ndarray) -> tuple[float, ...] | None:
    grid = _ritual_grid(crop)
    if grid is None:
        return None
    factor = PXSLOT / grid.cell
    if not 0.25 <= factor <= 0.90:
        return None
    # KWin/HiDPI captures can render Ritual slots much larger than upstream's
    # calibrated 68 px/slot. Derive the normalization from the visible grid and
    # scan a narrow band around it to absorb UI scale and resize rounding.
    return tuple(round(factor * s, 3) for s in (0.96, 0.98, 1.0, 1.02, 1.04))


def _scan_factors(frame: np.ndarray, crop: np.ndarray | None = None) -> tuple[float, ...]:
    if crop is not None:
        factors = _ritual_grid_factors(crop)
        if factors is not None:
            return factors
    # Upstream's factors are calibrated for ~1440p captures where ritual/vendor
    # item slots are about 68 px. KWin ScreenShot2 can return the full 4K
    # physical framebuffer, where the same slots are ~110-120 px. Add lower
    # factors there so the screenshot is still normalized to 47 px templates.
    if frame.shape[0] >= 2000:
        return HIRES_FACTORS + FACTORS
    return FACTORS


# --- Shared multi-factor scanning and region entry ----------------------------
def _scan_shared(crop: np.ndarray, rows: dict,
                 gray_thresh: float = GRAY_THRESH,
                 color_thresh: float = COLOR_THRESH,
                 factors: tuple[float, ...] | None = None) -> list[dict]:
    """Multi-factor scan with one shared coarse pass: coarse candidates are
    found at MID and mapped into each factor's frame for local fine + color
    verification. Halves the dominant (coarse) cost vs per-factor sweeps."""
    scan_factors = factors or _scan_factors(crop)
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
    include_unknown: bool = True,
    matching_mode: str = "cells",
    gray_thresh: float = GRAY_THRESH,
    color_thresh: float = COLOR_THRESH,
) -> list[dict]:
    """Scan a dynamically localized unique-item grid in frame coordinates."""
    if region is None:
        return []
    clipped = region.clipped(frame.shape[1], frame.shape[0])
    if clipped is None:
        return []
    crop = frame[
        clipped.y:clipped.y + clipped.h,
        clipped.x:clipped.x + clipped.w,
    ]
    mode = matching_mode.strip().lower()
    grid = None if mode == "shared" else _dynamic_ritual_grid(crop, cell)
    if mode == "shared":
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
    elif grid is not None:
        kept = _scan_grid_cells(crop, rows, grid)
    else:
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
    if include_unknown:
        if grid is not None:
            kept = _nms(kept + _unknown_occupied_cells(crop, grid, kept))
    for match in kept:
        match["x"] += clipped.x
        match["y"] += clipped.y
    return kept
