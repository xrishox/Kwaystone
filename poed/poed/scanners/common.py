from __future__ import annotations

import copy
import json
import logging
import os
import shutil
import time
from pathlib import Path

from poed import config
import cv2
import numpy as np

from . import debug_io

_LOG = logging.getLogger("waystone.scanners")
SCAN_HISTORY_PER_TYPE = 20


def normalize_match(match: dict, scan_kind: str) -> dict:
    out = dict(match)
    out.setdefault("scanKind", scan_kind)
    price = out.get("unitPrice", out.get("price") or 0) or 0
    out.setdefault("unitPrice", price)
    out.setdefault("price", price)
    try:
        stack = int(out.get("stackSize") or 1)
    except (TypeError, ValueError):
        stack = 1
    stack = max(1, stack)
    out["stackSize"] = stack
    out.setdefault("totalPrice", price * stack)
    out.setdefault("priceCurrency", "exalted")
    out.setdefault("ambiguous", False)
    return out


def normalize_matches(matches: list[dict], scan_kind: str) -> list[dict]:
    return [normalize_match(m, scan_kind) for m in matches]


def _write_debug_image_now(path: Path, image: np.ndarray) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        params = (
            [cv2.IMWRITE_JPEG_QUALITY, 90]
            if path.suffix.lower() in {".jpg", ".jpeg"}
            else []
        )
        if not cv2.imwrite(str(path), image, params):
            _LOG.warning("could not write debug image %s", path)
    except OSError as e:
        _LOG.warning("could not write debug image %s: %s", path, e)



def finalize_matches(
    ctx,
    matches: list[dict],
    scan_kind: str,
    *,
    stage: str,
    title: str,
    offset_x: int = 0,
    offset_y: int = 0,
) -> list[dict]:
    """Shared scan() epilogue: shift crop-relative matches into absolute
    monitor coordinates, normalize, and record the result stage."""

    for match in matches:
        match["x"] += ctx.frame_x + offset_x
        match["y"] += ctx.frame_y + offset_y
    matches = normalize_matches(matches, scan_kind)
    write_result_stage(ctx.debug_dir, stage, ctx.shot, matches, title)
    return matches

def write_debug_image(path: Path, image: np.ndarray) -> None:
    """Queue a debug image write; the array must not be mutated afterwards."""

    debug_io.submit_image(
        lambda: _write_debug_image_now(path, image), int(image.nbytes)
    )


def _debug_root() -> Path:
    state = config.state_home()
    return state / "waystone" / "debug" / "scans"


def _scan_bucket(path: Path) -> str | None:
    manifest = path / "manifest.json"
    try:
        data = json.loads(manifest.read_text())
    except (OSError, json.JSONDecodeError):
        return None
    if data.get("status") not in {"complete", "error"}:
        return None
    return str(data.get("selected_scanner") or "unclassified")


def _prune_scan_attempts(root: Path) -> None:
    buckets: dict[str, list[Path]] = {}
    for path in sorted(root.glob("scan-*")):
        if not path.is_dir():
            continue
        bucket = _scan_bucket(path)
        if bucket is not None:
            buckets.setdefault(bucket, []).append(path)

    for attempts in buckets.values():
        for path in attempts[:-SCAN_HISTORY_PER_TYPE]:
            try:
                shutil.rmtree(path)
            except OSError as e:
                _LOG.warning("could not remove old scan history %s: %s", path, e)


def finalize_debug_attempt(debug_dir: Path | None) -> None:
    if debug_dir is None:
        return
    root = debug_dir.parent
    debug_io.submit_keyed(f"prune:{root}", lambda: _prune_scan_attempts(root))


def _prune_abandoned_attempts(root: Path) -> None:
    """Bound incomplete attempts left behind by crashes without touching live ones."""
    cutoff = time.time() - 24 * 60 * 60
    abandoned = []
    for path in sorted(root.glob("scan-*")):
        if not path.is_dir() or _scan_bucket(path) is not None:
            continue
        try:
            if path.stat().st_mtime < cutoff:
                abandoned.append(path)
        except OSError:
            continue
    for path in abandoned[:-SCAN_HISTORY_PER_TYPE]:
        try:
            shutil.rmtree(path)
        except OSError as e:
            _LOG.warning("could not remove abandoned scan history %s: %s", path, e)


def begin_debug_attempt(
    output: str,
    shot: np.ndarray,
    frame: np.ndarray,
    frame_x: int,
    frame_y: int,
    source: str,
) -> Path | None:
    root = _debug_root()
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    attempt = root / f"scan-{stamp}-{time.time_ns() % 1_000_000_000:09d}"
    try:
        attempt.mkdir(parents=True, exist_ok=False)
    except OSError as e:
        _LOG.warning("could not create debug scan directory %s: %s", attempt, e)
        return None

    def _write_capture() -> None:
        _write_debug_image_now(attempt / "00-capture.png", shot)
        frame_path = attempt / "01-game-frame.png"
        if source == "output" and shot.shape == frame.shape:
            try:
                os.link(attempt / "00-capture.png", frame_path)
            except OSError:
                _write_debug_image_now(frame_path, frame)
        else:
            _write_debug_image_now(frame_path, frame)

    debug_io.submit_image(_write_capture, int(shot.nbytes) + int(frame.nbytes))
    update_debug_manifest(
        attempt,
        status="probing",
        output=output,
        source=source,
        capture={"width": shot.shape[1], "height": shot.shape[0]},
        frame={
            "x": frame_x,
            "y": frame_y,
            "width": frame.shape[1],
            "height": frame.shape[0],
        },
    )
    debug_io.submit_keyed(
        f"prune-abandoned:{root}", lambda: _prune_abandoned_attempts(root)
    )
    return attempt


def _update_debug_manifest_now(path: Path, values: dict) -> None:
    data = {}
    try:
        if path.exists():
            data = json.loads(path.read_text())
        data.update(values)
        path.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    except (OSError, json.JSONDecodeError) as e:
        _LOG.warning("could not update debug manifest %s: %s", path, e)


def update_debug_manifest(debug_dir: Path | None, **values) -> None:
    if debug_dir is None:
        return
    path = debug_dir / "manifest.json"
    # Callers keep mutating their timing dicts after this call; snapshot now
    # so the queued write records the state at call time.
    snapshot = copy.deepcopy(values)
    debug_io.submit(lambda: _update_debug_manifest_now(path, snapshot))


def _banner(image: np.ndarray, text: str) -> None:
    scale = max(0.55, min(1.1, image.shape[1] / 1800.0))
    thickness = max(1, round(scale * 2))
    (width, height), baseline = cv2.getTextSize(
        text, cv2.FONT_HERSHEY_SIMPLEX, scale, thickness
    )
    cv2.rectangle(
        image,
        (0, 0),
        (min(image.shape[1] - 1, width + 20), height + baseline + 18),
        (20, 20, 20),
        -1,
    )
    cv2.putText(
        image,
        text,
        (10, height + 8),
        cv2.FONT_HERSHEY_SIMPLEX,
        scale,
        (255, 255, 255),
        thickness,
        cv2.LINE_AA,
    )


def write_ocr_stage(
    debug_dir: Path | None,
    filename: str,
    crop: np.ndarray,
    lines,
    title: str,
) -> None:
    if debug_dir is None:
        return
    boxes = [tuple(int(v) for v in line.box) for line in lines]

    def compose() -> None:
        marked = crop.copy()
        for x0, y0, x1, y1 in boxes:
            cv2.rectangle(marked, (x0, y0), (x1, y1), (80, 160, 255), 2)
        _banner(marked, title)
        _write_debug_image_now(debug_dir / filename, marked)

    debug_io.submit_image(compose, int(crop.nbytes))


def write_result_stage(
    debug_dir: Path | None,
    filename: str,
    shot: np.ndarray,
    matches: list[dict],
    title: str,
) -> None:
    if debug_dir is None:
        return
    # Composition copies and annotates a full frame (~25MB memcpy plus
    # drawing); snapshot the small match dicts now and do the heavy part
    # on the writer thread.
    matches = copy.deepcopy(matches)

    def compose() -> None:
        _compose_result_stage(debug_dir / filename, shot, matches, title)

    debug_io.submit_image(compose, int(shot.nbytes))


def _compose_result_stage(
    path: Path,
    shot: np.ndarray,
    matches: list[dict],
    title: str,
) -> None:
    marked = shot.copy()
    for match in matches:
        try:
            x, y, w, h = (int(match[key]) for key in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        cv2.rectangle(marked, (x, y), (x + w, y + h), (0, 255, 0), 3)
        label = str(match.get("name") or "")[:40]
        if label:
            cv2.putText(
                marked,
                label,
                (x, max(24, y - 8)),
                cv2.FONT_HERSHEY_SIMPLEX,
                0.8,
                (0, 255, 0),
                2,
                cv2.LINE_AA,
            )
    _banner(marked, title)
    _write_debug_image_now(path, marked)
