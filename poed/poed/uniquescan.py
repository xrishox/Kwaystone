"""Find unique/currency/omen items on a screenshot by matching brain-cached
icons. Production descendant of spikes/spike_unique_match.py (concept test
2026-06-11, PASS).

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
import subprocess
from concurrent.futures import ThreadPoolExecutor

import cv2
import numpy as np

# CT-calibrated (docs/superpowers/plans/2026-06-11-unique-scan-concept-test.md)
PXSLOT = 47          # template density everything is normalized to
FACTOR = 0.68        # shot downscale default (ritual calibration)
# Ritual calibrates to 0.68, the vendor window to 0.70 — and a 0.68-only scan
# loses the vendor's small 1x1 items (validated on the CT shots). scan_screen
# runs both and NMS-merges; ~2x match cost, still inside the 10-20s budget.
FACTORS = (0.68, 0.70)
GRAY_THRESH = 0.72   # candidate floor (vendor's recovered band starts ~0.76)
COLOR_THRESH = 0.75  # confirm floor on the color verify
COARSE = 0.5         # pyramid level: half of the factor-scaled frame
COARSE_THRESH = 0.65  # keep-floor — CT-measured: vendor 1x1 items bottom out
                      # at 0.703 half-res; 0.65 keeps them with margin. False
                      # coarse survivors cost one cheap local fine match each.
REGION = (0.18, 0.73)  # scan-zone width fraction (center / slightly left)
WORKERS = max(1, (os.cpu_count() or 2) // 2)  # half cores: game is running

_corpus_cache: dict | None = None


def grab_output(output: str) -> np.ndarray | None:
    """Screenshot one monitor via grim; None on failure. PPM, not PNG:
    measured 0.06s vs 1.69s for a 3440x1440 frame — PNG encode was the
    single biggest cost of a whole scan."""
    try:
        r = subprocess.run(
            ["grim", "-t", "ppm", "-o", output, "-"],
            capture_output=True, timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if r.returncode != 0 or not r.stdout:
        return None
    arr = np.frombuffer(r.stdout, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def region_crop(shot: np.ndarray, region: tuple[float, float] = REGION):
    """Crop the scan zone (width fractions); returns (crop, x_offset)."""
    x0 = int(shot.shape[1] * region[0])
    x1 = int(shot.shape[1] * region[1])
    return shot[:, x0:x1], x0


def filter_rows(rows: dict, min_price: float) -> dict:
    """Drop corpus rows priced under `min_price` exalted before matching.
    The full-frame correlation is O(corpus), so this trades coverage for
    speed (config: unique_scan_min_price; 0 = off). CAVEAT: a filtered-out
    item's cell can be claimed by a similar-art lookalike that survived the
    filter (measured on the CT shots — see config.py) — results on a
    filtered corpus may mislabel cheap items as their pricier lookalikes."""
    return {n: r for n, r in rows.items() if (r.get("price") or 0) >= min_price}


def _load_corpus(rows: dict) -> list[dict]:
    """One template per distinct icon FILE: gray + color images normalized to
    PXSLOT px/slot, grouped names, group max price. Cached by CONTENT (the
    sorted name set) — every scan request builds a fresh dict from JSON, so
    identity caching would reload ~1000 PNGs per scan."""
    global _corpus_cache
    key = tuple(sorted(rows))
    if _corpus_cache is not None and _corpus_cache["key"] == key:
        return _corpus_cache["tmpl"]

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
        density = img.shape[1] / slots_w
        if abs(density - PXSLOT) > 1:
            s = PXSLOT / density
            img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
        best = max(g["rows"], key=lambda r: r.get("price", 0))
        names = g["names"]
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        out.append({
            "label": names[0] + (f" +{len(names) - 1}" if len(names) > 1 else ""),
            "ambiguous": len(names) > 1,
            "price": best.get("price", 0),
            "quantity": best.get("quantity", 0),
            "kind": best.get("kind", "tagged"),
            "trend": best.get("trend"),
            "color": img,
            "gray": gray,
            # Half-res copy for the coarse pyramid pass (1/16 the correlation
            # cost of the full-res sweep).
            "gray_half": cv2.resize(gray, None, fx=COARSE, fy=COARSE,
                                    interpolation=cv2.INTER_AREA),
        })
    _corpus_cache = {"key": key, "tmpl": out}
    return out


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

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        cands = [c for peaks in ex.map(coarse, templates) for c in peaks]

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


def _overlap(a: dict, b: dict) -> bool:
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy > 0.4 * min(a["w"] * a["h"], b["w"] * b["h"])


def scan_image(shot: np.ndarray, rows: dict, factor: float = FACTOR,
               gray_thresh: float = GRAY_THRESH,
               color_thresh: float = COLOR_THRESH) -> list[dict]:
    """All corpus matches on `shot` (already region-cropped if desired):
    {name, kind, price, quantity, score, ambiguous, x, y, w, h} in native
    shot pixels, sorted by price descending."""
    templates = _load_corpus(rows)
    small = shot if abs(factor - 1.0) < 1e-6 else cv2.resize(
        shot, None, fx=factor, fy=factor, interpolation=cv2.INTER_AREA)
    gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)

    confirmed = []
    for t, _gray_score, loc in _gray_candidates(gray, templates, gray_thresh):
        score = _color_verify(small, t, loc)
        if score < color_thresh:
            continue
        confirmed.append({
            "name": t["label"], "kind": t["kind"], "price": t["price"],
            "quantity": t["quantity"], "ambiguous": t["ambiguous"],
            "trend": t.get("trend"),
            "score": round(score, 3),
            "x": int(loc[0] / factor), "y": int(loc[1] / factor),
            "w": int(t["gray"].shape[1] / factor),
            "h": int(t["gray"].shape[0] / factor),
        })

    # Greedy NMS on color scores: best claims its area (omen variants and
    # other near-ties at one cell resolve to the color winner).
    confirmed.sort(key=lambda h: -h["score"])
    kept: list[dict] = []
    for h in confirmed:
        if all(not _overlap(h, k) for k in kept):
            kept.append(h)
    kept.sort(key=lambda h: -h["price"])
    return kept


def warm(brain, cfg: dict) -> bool:
    """Pre-build the template corpus so the first Alt+X pays nothing:
    request the corpus (brain warms its own snapshot+icons at startup, so
    this is usually a fast cache hit) and run _load_corpus. Returns True
    when the corpus is warm; safe to call from a background thread — a
    concurrent scan at worst builds the same cache once more."""
    try:
        rows = brain.request(
            {"cmd": "uniqueprices", "league": cfg["league"]}, timeout=120.0
        )
    except (RuntimeError, OSError, TimeoutError):
        return False
    _load_corpus(filter_rows(rows, cfg["unique_scan_min_price"]))
    return True


MID = 0.69  # shared coarse-pass factor: midpoint of FACTORS (they differ ~3%,
            # invisible at half res, so ONE coarse sweep serves both fine passes)


def _scan_shared(crop: np.ndarray, rows: dict,
                 gray_thresh: float = GRAY_THRESH,
                 color_thresh: float = COLOR_THRESH) -> list[dict]:
    """Multi-factor scan with one shared coarse pass: coarse candidates are
    found at MID and mapped into each factor's frame for local fine + color
    verification. Halves the dominant (coarse) cost vs per-factor sweeps."""
    templates = _load_corpus(rows)
    gray_mid = cv2.cvtColor(
        cv2.resize(crop, None, fx=MID, fy=MID, interpolation=cv2.INTER_AREA),
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

    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        cands = [c for peaks in ex.map(coarse, templates) for c in peaks]

    confirmed: list[dict] = []
    pad = 10  # covers coarse quantization + the +-3% MID->factor mismatch
    for f in FACTORS:
        small = cv2.resize(crop, None, fx=f, fy=f, interpolation=cv2.INTER_AREA)
        gray = cv2.cvtColor(small, cv2.COLOR_BGR2GRAY)
        scale = f / (MID * COARSE)  # half-frame coords -> this factor's frame
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
                "name": t["label"], "kind": t["kind"], "price": t["price"],
                "quantity": t["quantity"], "ambiguous": t["ambiguous"],
                "trend": t.get("trend"),
                "score": round(score, 3),
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


def scan_screen(output: str, rows: dict) -> list[dict] | None:
    """grim-capture `output`, scan its center region (shared-coarse multi-
    factor); None when capture fails. Coordinates in the result are absolute
    monitor pixels."""
    shot = grab_output(output)
    if shot is None:
        return None
    crop, x0 = region_crop(shot)
    kept = _scan_shared(crop, rows)
    for m in kept:
        m["x"] += x0
    return kept
