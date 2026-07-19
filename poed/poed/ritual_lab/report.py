"""Overlay rendering and scoreboard formatting for lab runs."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .datasets import Sample, lab_state_dir
from .scoring import SampleRecord
from .stages import RitualScanOutput


def render_overlay(
    frame: np.ndarray,
    output: RitualScanOutput,
    matches: list[dict],
    sample: Sample,
) -> np.ndarray:
    marked = frame.copy()
    if output.panel is not None:
        rect = output.panel.rect
        cv2.rectangle(
            marked, (rect.x, rect.y), (rect.x + rect.w, rect.y + rect.h), (255, 160, 40), 3
        )
        if output.panel.plaque_rect is not None:
            p = output.panel.plaque_rect
            cv2.rectangle(marked, (p.x, p.y), (p.x + p.w, p.y + p.h), (255, 240, 80), 2)
    if output.lattice is not None:
        lattice = output.lattice
        bounds = lattice.frame_rect()
        for col in range(lattice.cols + 1):
            x = int(round(lattice.x0 + col * lattice.pitch_x))
            cv2.line(marked, (x, bounds.y), (x, bounds.y + bounds.h), (200, 120, 0), 1)
        for row in range(lattice.rows + 1):
            y = int(round(lattice.y0 + row * lattice.pitch_y))
            cv2.line(marked, (bounds.x, y), (bounds.x + bounds.w, y), (200, 120, 0), 1)
        if output.occupancy is not None:
            occ = output.occupancy.occupied
            for row in range(min(occ.shape[0], lattice.rows)):
                for col in range(min(occ.shape[1], lattice.cols)):
                    if occ[row, col]:
                        rect = lattice.cell_rect(col, row)
                        cv2.rectangle(
                            marked,
                            (rect.x + 3, rect.y + 3),
                            (rect.x + rect.w - 3, rect.y + rect.h - 3),
                            (0, 210, 255),
                            1,
                        )
    for item in sample.metadata.get("items") or []:
        scale = sample.scale
        x, y, w, h = (int(round(v * scale)) for v in item["rect"])
        cv2.rectangle(marked, (x, y), (x + w, y + h), (0, 0, 235), 2)
    for match in matches:
        try:
            x, y, w, h = (int(match[k]) for k in ("x", "y", "w", "h"))
        except (KeyError, TypeError, ValueError):
            continue
        color = (160, 160, 160) if match.get("markerOnly") else (0, 235, 0)
        cv2.rectangle(marked, (x, y), (x + w, y + h), color, 2)
        label = str(match.get("name") or "")[:34]
        if label:
            cv2.putText(
                marked, label, (x, max(16, y - 6)),
                cv2.FONT_HERSHEY_SIMPLEX, 0.62, color, 2, cv2.LINE_AA,
            )
    return marked


def overlay_path(run_dir: Path, system_id: str, sample_id: str) -> Path:
    out_dir = run_dir / "overlays" / system_id
    out_dir.mkdir(parents=True, exist_ok=True)
    safe = sample_id.replace("/", "_")
    return out_dir / f"{safe}.jpg"


def format_scoreboard(summary: dict[str, Any], datasets: list[str]) -> str:
    columns = (
        ("corpus_l1", "corpus L1"),
        ("corpus_l2", "L2"),
        ("corpus_l3", "L3"),
        ("fp_fires", "FP fires"),
        ("synth_recall", "syn recall"),
        ("synth_precision", "syn prec"),
        ("synth_iou", "syn IoU"),
        ("synth_name_acc", "syn names"),
        ("debug_fired", "debug fired"),
        ("latency_p50_ms", "p50 ms"),
        ("latency_p95_ms", "p95 ms"),
    )
    lines = [
        f"datasets: {', '.join(datasets)}",
        "",
        "| system | " + " | ".join(title for _, title in columns) + " |",
        "|---" * (len(columns) + 1) + "|",
    ]
    for system_id, values in summary.items():
        cells = [str(values.get(key)) if values.get(key) is not None else "-" for key, _ in columns]
        lines.append(f"| {system_id} | " + " | ".join(cells) + " |")
    return "\n".join(lines) + "\n"


def failing_records(records: list[SampleRecord]) -> list[SampleRecord]:
    failing = []
    for record in records:
        tier_fail = any(value is False for value in (record.l1, record.l2, record.l3))
        if tier_fail or record.reasons:
            failing.append(record)
    return failing


def format_failures(records: list[SampleRecord], limit: int = 40) -> str:
    lines = []
    for record in failing_records(records)[:limit]:
        tiers = "".join(
            "-" if value is None else ("P" if value else "F")
            for value in (record.l1, record.l2, record.l3)
        )
        lines.append(f"[{record.system}] {record.sample_id} ({record.kind}) tiers={tiers}")
        for reason in record.reasons[:6]:
            lines.append(f"    {reason}")
    return "\n".join(lines) + ("\n" if lines else "")


def latest_run_dir() -> Path | None:
    root = lab_state_dir() / "results"
    if not root.exists():
        return None
    runs = sorted(path for path in root.iterdir() if path.is_dir())
    return runs[-1] if runs else None
