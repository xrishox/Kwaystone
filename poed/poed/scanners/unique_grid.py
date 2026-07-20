from __future__ import annotations

from collections.abc import Callable

from poed import uniquescan

from .common import finalize_matches
from .types import Detection, ScanContext, ScanResult


def scan_unique_grid(
    ctx: ScanContext,
    detection: Detection,
    *,
    scanner_id: str,
    title: str,
    stage_name: str,
    stage_label: str,
    gray_thresh: float | None = None,
    color_thresh: float | None = None,
    row_filter: Callable[[dict], dict] | None = None,
) -> ScanResult:
    """Scan a visually localized stock grid and return normalized matches."""
    min_price = ctx.cfg.get("unique_scan_min_price", 0.0)
    rows = uniquescan.filter_rows(ctx.rows, min_price)
    if row_filter is not None:
        rows = row_filter(rows)
    matches = uniquescan.scan_region(
        ctx.frame,
        rows,
        detection.region,
        float(detection.payload["cell"]),
        **_threshold_kwargs(gray_thresh, color_thresh),
    )
    matches = finalize_matches(
        ctx,
        matches,
        scanner_id,
        stage=stage_name,
        title=f"{stage_label}: matches={len(matches)}",
    )
    return ScanResult(scanner_id, title, matches)


def _threshold_kwargs(
    gray_thresh: float | None,
    color_thresh: float | None,
) -> dict[str, float]:
    out = {}
    if gray_thresh is not None:
        out["gray_thresh"] = gray_thresh
    if color_thresh is not None:
        out["color_thresh"] = color_thresh
    return out
