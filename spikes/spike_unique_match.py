"""Unique-scan concept test: find unique/currency/omen items on a screenshot
by template matching against the downloaded icon corpus (spike_unique_icons).

GGG serves web icons at mixed densities (mostly 47 or 64 px per inventory
slot) while the game renders cells at ~90 px/slot (3440x1440). Strategy:
normalize every template to a common PXSLOT density, then DOWNSCALE THE
SCREENSHOT by factor f = PXSLOT / cell_px so templates stay crisp (upscaling
47 px art 2x is mush). Match coords are mapped back to native shot pixels.

Two modes:
  calibrate — sweep shot factors for ONE named item known on the screenshot;
              prints the best (factor, score). Run once per screen context.
      python spikes/spike_unique_match.py calibrate SHOT.png "Surefooted Sigil"
  scan      — match the WHOLE corpus at a fixed factor; writes
              SHOT.matched.png with labeled boxes + JSON report.
      python spikes/spike_unique_match.py scan SHOT.png --factor 0.52 [--thresh 0.8]

Brute force on purpose: spike answers "is the art recognizable", not "fast".
Arts shared by several items (uncut-gem levels etc.) are matched once and
labeled "<first name> +N".
"""
import json
import os
import sys

import cv2
import numpy as np

CORPUS = os.path.expanduser("~/.cache/poe2-overlay/unique-icons")
PXSLOT = 47  # common per-slot density templates are normalized to


def load_index() -> dict:
    with open(os.path.join(CORPUS, "index.json")) as f:
        return json.load(f)


def label_of(meta: dict) -> str:
    names = meta["names"]
    return names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")


def load_template(meta: dict) -> np.ndarray | None:
    """Load an icon, composite alpha onto dark cell tone, normalize density
    to PXSLOT px per inventory slot."""
    img = cv2.imread(os.path.join(CORPUS, meta["file"]), cv2.IMREAD_UNCHANGED)
    if img is None:
        return None
    if img.ndim == 3 and img.shape[2] == 4:
        alpha = img[:, :, 3:4].astype(np.float32) / 255.0
        rgb = img[:, :, :3].astype(np.float32)
        bg = np.full_like(rgb, 12.0)  # PoE inventory cells are near-black
        img = (rgb * alpha + bg * (1.0 - alpha)).astype(np.uint8)
    slots_w = meta.get("w") or 1
    density = img.shape[1] / slots_w
    if abs(density - PXSLOT) > 1:
        s = PXSLOT / density
        img = cv2.resize(img, None, fx=s, fy=s, interpolation=cv2.INTER_AREA)
    return img


def shot_at_factor(shot: np.ndarray, factor: float) -> np.ndarray:
    if abs(factor - 1.0) < 1e-6:
        return shot
    return cv2.resize(shot, None, fx=factor, fy=factor,
                      interpolation=cv2.INTER_AREA)


def best_match(small: np.ndarray, tmpl: np.ndarray):
    if tmpl.shape[0] >= small.shape[0] or tmpl.shape[1] >= small.shape[1]:
        return None
    r = cv2.matchTemplate(small, tmpl, cv2.TM_CCOEFF_NORMED)
    _, mx, _, loc = cv2.minMaxLoc(r)
    return mx, loc


def find_meta(index: dict, name: str) -> dict | None:
    for meta in index.values():
        if name in meta["names"]:
            return meta
    return None


def calibrate(shot_path: str, name: str) -> None:
    index = load_index()
    meta = find_meta(index, name)
    if meta is None:
        sys.exit(f"'{name}' not in corpus index (use refName from items.ndjson)")
    shot = cv2.imread(shot_path)
    if shot is None:
        sys.exit(f"cannot read {shot_path}")
    tmpl = load_template(meta)
    rows = []
    factor = 0.30
    while factor <= 1.05:
        hit = best_match(shot_at_factor(shot, factor), tmpl)
        if hit:
            rows.append((hit[0], factor))
        factor = round(factor + 0.02, 3)
    rows.sort(reverse=True)
    for score, f in rows[:5]:
        print(f"factor={f:.3f} score={score:.3f}")
    print(f"\nBEST: --factor {rows[0][1]}  (cell ≈ {PXSLOT / rows[0][1]:.0f} px/slot)")


def overlap(a: dict, b: dict) -> bool:
    ix = max(0, min(a["x"] + a["w"], b["x"] + b["w"]) - max(a["x"], b["x"]))
    iy = max(0, min(a["y"] + a["h"], b["y"] + b["h"]) - max(a["y"], b["y"]))
    return ix * iy > 0.4 * min(a["w"] * a["h"], b["w"] * b["h"])


def scan(shot_path: str, factor: float, thresh: float) -> None:
    index = load_index()
    shot = cv2.imread(shot_path)
    if shot is None:
        sys.exit(f"cannot read {shot_path}")
    small = shot_at_factor(shot, factor)
    hits = []
    for meta in index.values():
        tmpl = load_template(meta)
        if tmpl is None:
            continue
        r = best_match(small, tmpl)
        if r and r[0] >= thresh:
            # Map small-image coords back to native shot pixels.
            hits.append({"name": label_of(meta), "kind": meta["kind"],
                         "score": round(float(r[0]), 3),
                         "x": int(r[1][0] / factor), "y": int(r[1][1] / factor),
                         "w": int(tmpl.shape[1] / factor),
                         "h": int(tmpl.shape[0] / factor)})
    # Greedy non-max suppression: highest score claims its area.
    hits.sort(key=lambda h: -h["score"])
    kept: list[dict] = []
    for h in hits:
        if all(not overlap(h, k) for k in kept):
            kept.append(h)

    for h in kept:
        color = (0, 255, 0) if h["kind"] == "unique" else (0, 200, 255)
        cv2.rectangle(shot, (h["x"], h["y"]),
                      (h["x"] + h["w"], h["y"] + h["h"]), color, 2)
        cv2.putText(shot, f'{h["name"]} {h["score"]}', (h["x"], h["y"] - 4),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.45, color, 1)
    out = shot_path.rsplit(".", 1)[0] + ".matched.png"
    cv2.imwrite(out, shot)
    print(out)
    print(json.dumps(kept, indent=1))


def main() -> None:
    args = sys.argv[1:]
    if len(args) >= 3 and args[0] == "calibrate":
        calibrate(args[1], args[2])
    elif len(args) >= 2 and args[0] == "scan":
        factor = float(args[args.index("--factor") + 1]) if "--factor" in args else 0.52
        thresh = float(args[args.index("--thresh") + 1]) if "--thresh" in args else 0.80
        scan(args[1], factor, thresh)
    else:
        sys.exit(__doc__)


if __name__ == "__main__":
    main()
