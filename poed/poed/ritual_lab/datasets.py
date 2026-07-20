"""Sample sets the ritual lab evaluates candidate systems against.

- corpus: managed ritual corpus cases (authoritative truth at Level 1/2/3).
- debug: retained ritual-routed debug scans (diagnosis only, no stored truth).
- synth: seeded composites with exact position truth (see synth.py).
- fp: frames that must never fire the ritual route (other-category corpus
  cases, non-ritual debug scans, and right-side crops of ritual frames where
  the inventory panel lives).
"""

from __future__ import annotations

import json
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from poed import config, scan_corpus

REPO_POED_DIR = Path(__file__).resolve().parents[2]
CORPUS_ROOT = REPO_POED_DIR / "tests" / "fixtures" / "scanner-corpus"


def lab_state_dir() -> Path:
    return config.state_home() / "waystone" / "ritual-lab"


def rows_snapshot_path() -> Path:
    return lab_state_dir() / "rows.json"


def load_rows() -> dict[str, Any]:
    path = rows_snapshot_path()
    if not path.exists():
        raise FileNotFoundError(
            f"rows snapshot missing: {path}\n"
            "run: .venv/bin/python -m poed.ritual_lab snapshot-rows"
        )
    data = json.loads(path.read_text(encoding="utf-8"))
    return data["rows"]


@dataclass(frozen=True)
class Sample:
    id: str
    kind: str
    image_path: Path
    expected_route: str
    metadata: dict[str, Any]
    scale: float = 1.0
    transform: str | None = None

    def load(self) -> np.ndarray:
        image = cv2.imread(str(self.image_path))
        if image is None:
            raise RuntimeError(f"could not read sample image: {self.image_path}")
        if self.scale != 1.0:
            image = cv2.resize(
                image, None, fx=self.scale, fy=self.scale, interpolation=cv2.INTER_AREA
            )
        if self.transform == "ultrawide":
            # Simulate 21:9: same height, extra world content left and right
            # (mirrored edges), shifting the panel relative to a 16:9 center.
            pad = image.shape[1] * 320 // 3840
            left = cv2.flip(image[:, :pad], 1)
            right = cv2.flip(image[:, -pad:], 1)
            image = np.concatenate([left, image, right], axis=1)
        elif self.transform == "offset":
            crop = image.shape[1] * 200 // 3840
            image = image[:, crop:]
        return image


def corpus_samples() -> list[Sample]:
    index = scan_corpus.load_index(CORPUS_ROOT)
    samples = []
    for case in scan_corpus.active_cases(index, CORPUS_ROOT):
        if scan_corpus.case_category(case.metadata) != "ritual":
            continue
        samples.append(
            Sample(
                id=case.id,
                kind="corpus",
                image_path=case.image,
                expected_route="ritual",
                metadata=dict(case.metadata),
            )
        )
    return samples


def fp_corpus_samples() -> list[Sample]:
    index = scan_corpus.load_index(CORPUS_ROOT)
    samples = []
    for case in scan_corpus.active_cases(index, CORPUS_ROOT):
        category = scan_corpus.case_category(case.metadata)
        if category == "ritual":
            continue
        samples.append(
            Sample(
                id=f"fp-corpus-{case.id}",
                kind="fp",
                image_path=case.image,
                expected_route="none",
                metadata={"fpSource": f"corpus:{category}"},
            )
        )
    return samples


def _debug_scan_dirs() -> list[tuple[Path, dict[str, Any]]]:
    root = scan_corpus.debug_scan_root()
    out = []
    for path in sorted(root.glob("scan-*")):
        manifest_path = path / "manifest.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if manifest.get("status") not in {"complete", "error"}:
            continue
        out.append((path, manifest))
    return out


def _frame_path(scan_dir: Path) -> Path | None:
    for name in ("01-game-frame.png", "00-capture.png"):
        candidate = scan_dir / name
        if candidate.exists():
            return candidate
    return None


def debug_samples(limit: int | None = None) -> list[Sample]:
    samples = []
    for path, manifest in reversed(_debug_scan_dirs()):
        if manifest.get("selected_scanner") != "ritual":
            continue
        frame = _frame_path(path)
        if frame is None:
            continue
        samples.append(
            Sample(
                id=path.name,
                kind="debug",
                image_path=frame,
                expected_route="ritual",
                metadata={"manifestMatches": manifest.get("matches")},
            )
        )
        if limit is not None and len(samples) >= limit:
            break
    return samples


def fp_debug_samples(limit: int | None = None) -> list[Sample]:
    samples = []
    for path, manifest in reversed(_debug_scan_dirs()):
        route = str(manifest.get("selected_scanner") or "")
        if route in {"ritual", "combination", ""}:
            continue
        frame = _frame_path(path)
        if frame is None:
            continue
        samples.append(
            Sample(
                id=f"fp-{path.name}",
                kind="fp",
                image_path=frame,
                expected_route="none",
                metadata={"fpSource": f"debug:{route}"},
            )
        )
        if limit is not None and len(samples) >= limit:
            break
    return samples


def inventory_crop_dir() -> Path:
    return lab_state_dir() / "fp-crops"


def build_inventory_crops(limit: int = 8) -> list[Path]:
    """Right-side crops of ritual frames: the PoE2 inventory docks right while
    the Favours window opens left-of-center, so these crops exercise the
    inventory grid without the ritual panel. Overlay review catches any donor
    where the panel was dragged right."""
    out_dir = inventory_crop_dir()
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for sample in debug_samples(limit=limit):
        image = cv2.imread(str(sample.image_path))
        if image is None:
            continue
        crop = image[:, int(image.shape[1] * 0.60):]
        path = out_dir / f"{sample.id}-right.png"
        cv2.imwrite(str(path), crop)
        written.append(path)
    return written


def fp_inventory_samples() -> list[Sample]:
    out_dir = inventory_crop_dir()
    if not out_dir.exists():
        return []
    return [
        Sample(
            id=f"fp-inv-{path.stem}",
            kind="fp",
            image_path=path,
            expected_route="none",
            metadata={"fpSource": "inventory-crop"},
        )
        for path in sorted(out_dir.glob("*.png"))
    ]


def synth_dir() -> Path:
    return lab_state_dir() / "synth"


def synth_samples() -> list[Sample]:
    index_path = synth_dir() / "index.json"
    if not index_path.exists():
        return []
    index = json.loads(index_path.read_text(encoding="utf-8"))
    samples = []
    for entry in index.get("composites", []):
        samples.append(
            Sample(
                id=str(entry["id"]),
                kind="synth",
                image_path=synth_dir() / str(entry["image"]),
                expected_route="ritual",
                metadata=entry,
            )
        )
    return samples


def scaled_variants(samples: list[Sample], factors: tuple[float, ...]) -> list[Sample]:
    out = []
    for sample in samples:
        for factor in factors:
            out.append(
                replace(
                    sample,
                    id=f"{sample.id}@{factor:.3g}",
                    scale=factor,
                )
            )
    return out


def aspect_samples() -> list[Sample]:
    """Corpus frames under aspect/position transforms. Corpus truth is name
    multisets and counts, so it stays valid under any translation."""
    out = []
    for sample in corpus_samples():
        for transform in ("ultrawide", "offset"):
            out.append(
                replace(sample, id=f"{sample.id}~{transform}", transform=transform)
            )
    return out


DATASET_BUILDERS = {
    "aspect": aspect_samples,
    "corpus": corpus_samples,
    "debug": debug_samples,
    "synth": synth_samples,
    "fp": lambda: fp_corpus_samples() + fp_debug_samples() + fp_inventory_samples(),
}


def resolve_datasets(names: list[str], *, scale_factors: tuple[float, ...] = ()) -> list[Sample]:
    samples: list[Sample] = []
    for name in names:
        if name == "scaled":
            continue
        builder = DATASET_BUILDERS.get(name)
        if builder is None:
            raise ValueError(f"unknown dataset: {name}")
        samples.extend(builder())
    if "scaled" in names:
        base = [s for s in samples if s.kind in {"corpus", "synth"}]
        factors = scale_factors or (2.0 / 3.0, 0.5)
        samples.extend(scaled_variants(base, factors))
    return samples
