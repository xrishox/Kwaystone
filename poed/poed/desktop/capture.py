"""Shared screenshot decoding and compositor-independent capture helpers."""

from __future__ import annotations

import subprocess
from collections.abc import Mapping

import cv2
import numpy as np

from .base import Rect


def grim_output(output: str) -> np.ndarray | None:
    """Capture one wlroots output as uncompressed PPM."""
    try:
        result = subprocess.run(
            ["grim", "-t", "ppm", "-o", output, "-"],
            capture_output=True,
            timeout=5.0,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0 or not result.stdout:
        return None
    return cv2.imdecode(np.frombuffer(result.stdout, np.uint8), cv2.IMREAD_COLOR)


def decode_kwin_frame(
    data: bytes,
    metadata: Mapping[str, object],
) -> np.ndarray | None:
    """Decode ScreenShot2 raw BGRA frames or encoded image payloads."""
    if not data:
        return None
    if metadata.get("type") == "raw":
        try:
            width = int(metadata["width"])
            height = int(metadata["height"])
            stride = int(metadata["stride"])
            pixel_format = int(metadata["format"])
        except (KeyError, TypeError, ValueError):
            return None
        if pixel_format not in (4, 5, 6):
            return None
        if stride < width * 4 or len(data) < stride * height:
            return None
        rows = np.frombuffer(data[: stride * height], np.uint8).reshape(
            (height, stride)
        )
        bgra = rows[:, : width * 4].reshape((height, width, 4))
        return bgra[:, :, :3].copy()
    return cv2.imdecode(np.frombuffer(data, np.uint8), cv2.IMREAD_COLOR)


def workspace_rect_global() -> Rect | None:
    """Return the compositor workspace bounding box through GTK monitors."""
    try:
        import gi

        gi.require_version("Gdk", "4.0")
        from gi.repository import Gdk

        display = Gdk.Display.get_default()
        if display is None:
            return None
        monitors = display.get_monitors()
        geometries = [
            monitor.get_geometry()
            for index in range(monitors.get_n_items())
            if (monitor := monitors.get_item(index)) is not None
        ]
        if not geometries:
            return None
        x0 = min(geometry.x for geometry in geometries)
        y0 = min(geometry.y for geometry in geometries)
        x1 = max(geometry.x + geometry.width for geometry in geometries)
        y1 = max(geometry.y + geometry.height for geometry in geometries)
        return Rect(x0, y0, x1 - x0, y1 - y0)
    except Exception:
        return None


def crop_workspace_output(
    image: np.ndarray,
    workspace: Rect | None,
    target: Rect | None,
) -> np.ndarray:
    """Crop a full-workspace portal screenshot to one compositor output."""
    if (
        workspace is None
        or target is None
        or workspace.w <= 0
        or workspace.h <= 0
    ):
        return image
    frame_h, frame_w = image.shape[:2]
    crop = Rect(
        int(round((target.x - workspace.x) * frame_w / workspace.w)),
        int(round((target.y - workspace.y) * frame_h / workspace.h)),
        int(round(target.w * frame_w / workspace.w)),
        int(round(target.h * frame_h / workspace.h)),
    ).clipped(frame_w, frame_h)
    if crop is None:
        return image
    return image[crop.y:crop.y + crop.h, crop.x:crop.x + crop.w].copy()
