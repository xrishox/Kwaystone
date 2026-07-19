"""Synthetic ritual composites with exact position truth.

A donor is a real retained ritual frame whose panel has been emptied: the
lattice is estimated, occupied cells are replaced by the modal (median) cell
texture, and the result plus lattice metadata is stored. Composites paste real
corpus icons (and adversarial sparse-art shapes) onto donor grids at known
cells, giving unlimited position/name truth for iteration. Synthetic frames
are never promoted to the managed corpus.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np

from poed.image_geometry import Rect

from . import estimate
from .datasets import synth_dir
from .stages import Lattice

TINT_BGR = (70.0, 38.0, 16.0)
TINT_ALPHA = 0.45
EMPTY_QUANTILE = 0.35
FOOTPRINT_MARGIN = 0.06


def donors_dir() -> Path:
    return synth_dir() / "donors"


@dataclass(frozen=True)
class Donor:
    id: str
    frame_path: Path
    panel: Rect
    lattice: Lattice

    @staticmethod
    def load(donor_id: str) -> "Donor":
        root = donors_dir() / donor_id
        data = json.loads((root / "donor.json").read_text(encoding="utf-8"))
        lattice = Lattice(**data["lattice"])
        panel = Rect(**data["panel"])
        return Donor(donor_id, root / "frame-empty.png", panel, lattice)


def _lattice_json(lattice: Lattice) -> dict:
    return {
        "x0": lattice.x0,
        "y0": lattice.y0,
        "pitch_x": lattice.pitch_x,
        "pitch_y": lattice.pitch_y,
        "cols": lattice.cols,
        "rows": lattice.rows,
    }


def _cell_canvas(frame: np.ndarray, lattice: Lattice, col: int, row: int, side: int) -> np.ndarray:
    rect = lattice.cell_rect(col, row)
    crop = frame[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w]
    if crop.size == 0:
        return np.zeros((side, side, 3), dtype=np.uint8)
    return cv2.resize(crop, (side, side), interpolation=cv2.INTER_AREA)


def _cell_energies(frame: np.ndarray, lattice: Lattice, side: int) -> tuple[np.ndarray, np.ndarray]:
    cells = np.stack([
        np.stack([
            _cell_canvas(frame, lattice, col, row, side)
            for col in range(lattice.cols)
        ])
        for row in range(lattice.rows)
    ]).astype(np.float32)
    modal = np.median(cells.reshape(-1, side, side, 3), axis=0)
    residual = np.abs(cells - modal[None, None]).mean(axis=(2, 3, 4))
    return residual, modal


def build_donor(
    donor_id: str,
    frame: np.ndarray,
    panel: Rect,
) -> tuple[Donor, np.ndarray]:
    """Empty the panel of a real ritual frame and persist donor metadata.

    Returns the donor and a review overlay that must be inspected visually
    before the donor is used (lattice lines + cells judged occupied)."""
    gray = estimate.to_gray(frame[panel.y:panel.y + panel.h, panel.x:panel.x + panel.w])
    lattice, stats = estimate.lattice_from_region(gray, panel.x, panel.y)
    if lattice is None:
        raise RuntimeError(f"could not estimate donor lattice: {stats}")
    side = max(16, int(round(min(lattice.pitch_x, lattice.pitch_y))))
    residual, modal = _cell_energies(frame, lattice, side)
    threshold = float(np.quantile(residual, EMPTY_QUANTILE)) * 1.9
    occupied = residual > threshold

    emptied = frame.copy()
    modal_u8 = np.clip(modal, 0, 255).astype(np.uint8)
    for row in range(lattice.rows):
        for col in range(lattice.cols):
            if not occupied[row, col]:
                continue
            rect = lattice.cell_rect(col, row)
            patch = cv2.resize(modal_u8, (rect.w, rect.h), interpolation=cv2.INTER_AREA)
            emptied[rect.y:rect.y + rect.h, rect.x:rect.x + rect.w] = patch

    root = donors_dir() / donor_id
    root.mkdir(parents=True, exist_ok=True)
    cv2.imwrite(str(root / "frame-empty.png"), emptied)
    (root / "donor.json").write_text(
        json.dumps(
            {
                "panel": {"x": panel.x, "y": panel.y, "w": panel.w, "h": panel.h},
                "lattice": _lattice_json(lattice),
                "stats": stats,
                "sourceOccupiedCells": int(occupied.sum()),
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )

    overlay = frame.copy()
    _draw_lattice(overlay, lattice)
    for row in range(lattice.rows):
        for col in range(lattice.cols):
            if occupied[row, col]:
                rect = lattice.cell_rect(col, row)
                cv2.rectangle(
                    overlay,
                    (rect.x + 2, rect.y + 2),
                    (rect.x + rect.w - 2, rect.y + rect.h - 2),
                    (0, 220, 255),
                    2,
                )
    cv2.imwrite(str(root / "donor-overlay.jpg"), overlay, [cv2.IMWRITE_JPEG_QUALITY, 88])
    return Donor(donor_id, root / "frame-empty.png", panel, lattice), overlay


def _draw_lattice(image: np.ndarray, lattice: Lattice) -> None:
    frame_rect = lattice.frame_rect()
    for col in range(lattice.cols + 1):
        x = int(round(lattice.x0 + col * lattice.pitch_x))
        cv2.line(image, (x, frame_rect.y), (x, frame_rect.y + frame_rect.h), (255, 140, 0), 1)
    for row in range(lattice.rows + 1):
        y = int(round(lattice.y0 + row * lattice.pitch_y))
        cv2.line(image, (frame_rect.x, y), (frame_rect.x + frame_rect.w, y), (255, 140, 0), 1)


def _icon_entries(rows: dict) -> list[dict]:
    by_file: dict[str, dict] = {}
    for name, row in rows.items():
        path = row.get("iconPath")
        if not path:
            continue
        entry = by_file.setdefault(
            path,
            {"path": path, "names": [], "w": int(row.get("w") or 1), "h": int(row.get("h") or 1)},
        )
        entry["names"].append(name)
    entries = []
    for entry in by_file.values():
        names = entry["names"]
        entry["label"] = names[0] + (f" +{len(names) - 1}" if len(names) > 1 else "")
        entries.append(entry)
    return entries


def _paste_icon(
    frame: np.ndarray,
    rect: Rect,
    icon: np.ndarray,
    rng: np.random.Generator,
) -> None:
    inset = 3
    x0, y0 = rect.x + inset, rect.y + inset
    w, h = rect.w - 2 * inset, rect.h - 2 * inset
    if w <= 4 or h <= 4:
        return
    region = frame[y0:y0 + h, x0:x0 + w].astype(np.float32)
    tint = np.array(TINT_BGR, dtype=np.float32)
    region = region * (1.0 - TINT_ALPHA) + tint[None, None] * TINT_ALPHA

    if icon.ndim == 3 and icon.shape[2] == 4:
        rgb = icon[:, :, :3].astype(np.float32)
        alpha = icon[:, :, 3].astype(np.float32) / 255.0
    else:
        rgb = icon[:, :, :3].astype(np.float32)
        alpha = np.ones(icon.shape[:2], dtype=np.float32)
    margin = 1.0 - FOOTPRINT_MARGIN * 2
    scale = min(w * margin / rgb.shape[1], h * margin / rgb.shape[0])
    tw = max(4, int(round(rgb.shape[1] * scale)))
    th = max(4, int(round(rgb.shape[0] * scale)))
    rgb = cv2.resize(rgb, (tw, th), interpolation=cv2.INTER_AREA)
    alpha = cv2.resize(alpha, (tw, th), interpolation=cv2.INTER_AREA)
    ox = (w - tw) // 2 + int(rng.integers(-2, 3))
    oy = (h - th) // 2 + int(rng.integers(-2, 3))
    ox = max(0, min(w - tw, ox))
    oy = max(0, min(h - th, oy))
    target = region[oy:oy + th, ox:ox + tw]
    region[oy:oy + th, ox:ox + tw] = target * (1.0 - alpha[..., None]) + rgb * alpha[..., None]
    frame[y0:y0 + h, x0:x0 + w] = np.clip(region, 0, 255).astype(np.uint8)


def _draw_sparse_art(
    frame: np.ndarray,
    rect: Rect,
    rng: np.random.Generator,
) -> None:
    """Adversarial staff-like art: a thin diagonal occupying a tall footprint."""
    inset = 3
    x0, y0 = rect.x + inset, rect.y + inset
    w, h = rect.w - 2 * inset, rect.h - 2 * inset
    region = frame[y0:y0 + h, x0:x0 + w].astype(np.float32)
    tint = np.array(TINT_BGR, dtype=np.float32)
    region = region * (1.0 - TINT_ALPHA) + tint[None, None] * TINT_ALPHA
    frame[y0:y0 + h, x0:x0 + w] = np.clip(region, 0, 255).astype(np.uint8)
    thickness = max(3, int(round(min(w, h) * 0.10)))
    color = (
        int(rng.integers(120, 200)),
        int(rng.integers(120, 200)),
        int(rng.integers(140, 230)),
    )
    if rng.random() < 0.5:
        cv2.line(frame, (x0 + 4, y0 + h - 5), (x0 + w - 5, y0 + 4), color, thickness, cv2.LINE_AA)
    else:
        cv2.line(frame, (x0 + 4, y0 + 4), (x0 + w - 5, y0 + h - 5), color, thickness, cv2.LINE_AA)


def _place_items(
    lattice: Lattice,
    entries: list[dict],
    rng: np.random.Generator,
    count: int,
    sparse_chance: float,
) -> list[dict]:
    free = np.ones((lattice.rows, lattice.cols), dtype=bool)
    placed = []
    attempts = 0
    while len(placed) < count and attempts < count * 30:
        attempts += 1
        if rng.random() < sparse_chance:
            w, h = (2, 4) if rng.random() < 0.6 else (1, 4)
            item = {"label": "synthetic-sparse", "checkName": False, "w": w, "h": h, "path": None}
        else:
            entry = entries[int(rng.integers(0, len(entries)))]
            item = {
                "label": entry["label"],
                "checkName": True,
                "w": entry["w"],
                "h": entry["h"],
                "path": entry["path"],
            }
        w, h = item["w"], item["h"]
        if w > lattice.cols or h > lattice.rows:
            continue
        col = int(rng.integers(0, lattice.cols - w + 1))
        row = int(rng.integers(0, lattice.rows - h + 1))
        if not free[row:row + h, col:col + w].all():
            continue
        free[row:row + h, col:col + w] = False
        item.update({"col": col, "row": row})
        placed.append(item)
    return placed


def generate(
    donors: list[Donor],
    rows: dict,
    *,
    count: int,
    seed: int,
    sparse_chance: float = 0.12,
) -> list[dict]:
    rng = np.random.default_rng(seed)
    entries = _icon_entries(rows)
    if not entries:
        raise RuntimeError("rows snapshot has no iconPath entries; re-run snapshot-rows")
    out_dir = synth_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    index_path = out_dir / "index.json"
    composites = []
    donor_frames = {donor.id: cv2.imread(str(donor.frame_path)) for donor in donors}
    for number in range(count):
        donor = donors[number % len(donors)]
        base = donor_frames[donor.id]
        if base is None:
            raise RuntimeError(f"could not read donor frame: {donor.frame_path}")
        frame = base.copy()
        n_items = int(rng.integers(4, 27))
        placed = _place_items(donor.lattice, entries, rng, n_items, sparse_chance)
        truth_items = []
        for item in placed:
            rect = donor.lattice.cell_rect(item["col"], item["row"], item["w"], item["h"])
            if item["path"] is None:
                _draw_sparse_art(frame, rect, rng)
            else:
                icon = cv2.imread(item["path"], cv2.IMREAD_UNCHANGED)
                if icon is None:
                    continue
                _paste_icon(frame, rect, icon, rng)
            truth_items.append(
                {
                    "name": item["label"],
                    "checkName": item["checkName"],
                    "col": item["col"],
                    "row": item["row"],
                    "w": item["w"],
                    "h": item["h"],
                    "rect": [rect.x, rect.y, rect.w, rect.h],
                    "stackSize": 1,
                }
            )
        gamma = float(rng.uniform(0.94, 1.06))
        lut = np.clip(((np.arange(256) / 255.0) ** gamma) * 255.0, 0, 255).astype(np.uint8)
        frame = lut[frame]
        noise = rng.normal(0.0, 1.2, frame.shape).astype(np.float32)
        frame = np.clip(frame.astype(np.float32) + noise, 0, 255).astype(np.uint8)

        composite_id = f"synth-{seed}-{number:04d}"
        image_name = f"{composite_id}.png"
        cv2.imwrite(str(out_dir / image_name), frame)
        composites.append(
            {
                "id": composite_id,
                "image": image_name,
                "donor": donor.id,
                "seed": seed,
                "panel": {
                    "x": donor.panel.x,
                    "y": donor.panel.y,
                    "w": donor.panel.w,
                    "h": donor.panel.h,
                },
                "lattice": _lattice_json(donor.lattice),
                "items": truth_items,
                "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            }
        )
    index_path.write_text(json.dumps({"composites": composites}, indent=1) + "\n", encoding="utf-8")
    return composites
