"""Run candidate systems over sample sets and score reliability + speed.

Corpus samples are scored with the production tier logic
(`scan_corpus.evaluate_case_output`), synth samples against exact position
truth (IoU + name multiset), fp samples as must-not-fire, and debug samples
are recorded for overlay review only (they carry no stored truth).
"""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import numpy as np

from poed import scan_corpus
from poed.scanners.common import normalize_matches

from .datasets import Sample, lab_state_dir
from .stages_output import RitualScanOutput, RitualSystem

IOU_THRESHOLD = 0.5


@dataclass
class SampleRecord:
    sample_id: str
    kind: str
    system: str
    fired: bool
    expected_route: str
    match_count: int
    elapsed_ms: float
    l1: bool | None = None
    l2: bool | None = None
    l3: bool | None = None
    reasons: list[str] = field(default_factory=list)
    iou_mean: float | None = None
    position_precision: float | None = None
    position_recall: float | None = None
    name_accuracy: float | None = None
    timings_ms: dict[str, float] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    overlay: str | None = None


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    ax0, ay0, aw, ah = a
    bx0, by0, bw, bh = b
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax0 + aw, bx0 + bw), min(ay0 + ah, by0 + bh)
    if ix1 <= ix0 or iy1 <= iy0:
        return 0.0
    inter = (ix1 - ix0) * (iy1 - iy0)
    union = aw * ah + bw * bh - inter
    return inter / union if union > 0 else 0.0


def _score_synth(record: SampleRecord, sample: Sample, matches: list[dict]) -> None:
    truth = list(sample.metadata.get("items") or [])
    scale = sample.scale
    truth_rects = [
        tuple(int(round(v * scale)) for v in item["rect"])
        for item in truth
    ]
    pred_rects = [
        (int(m.get("x") or 0), int(m.get("y") or 0), int(m.get("w") or 0), int(m.get("h") or 0))
        for m in matches
    ]
    matched_ious: list[float] = []
    matched_pred: set[int] = set()
    matched_truth: dict[int, int] = {}
    pairs = sorted(
        (
            (_iou(truth_rects[ti], pred_rects[pi]), ti, pi)
            for ti in range(len(truth_rects))
            for pi in range(len(pred_rects))
        ),
        reverse=True,
    )
    for iou, ti, pi in pairs:
        if iou < IOU_THRESHOLD:
            break
        if ti in matched_truth or pi in matched_pred:
            continue
        matched_truth[ti] = pi
        matched_pred.add(pi)
        matched_ious.append(iou)
    record.iou_mean = float(np.mean(matched_ious)) if matched_ious else 0.0
    record.position_recall = len(matched_truth) / len(truth) if truth else 1.0
    record.position_precision = (
        len(matched_pred) / len(pred_rects) if pred_rects else (1.0 if not truth else 0.0)
    )
    named = [ti for ti, item in enumerate(truth) if item.get("checkName")]
    if named:
        correct = 0
        for ti in named:
            pi = matched_truth.get(ti)
            if pi is None:
                continue
            if str(matches[pi].get("name") or "") == str(truth[ti]["name"]):
                correct += 1
        record.name_accuracy = correct / len(named)
    missing = [truth[ti]["name"] for ti in range(len(truth)) if ti not in matched_truth]
    if missing:
        record.reasons.append("synth missing: " + ", ".join(sorted(missing)[:8]))
    extra = len(pred_rects) - len(matched_pred)
    if extra:
        record.reasons.append(f"synth unmatched predictions: {extra}")


def score_sample(
    system: RitualSystem,
    sample: Sample,
    rows: dict,
) -> tuple[SampleRecord, RitualScanOutput, np.ndarray, list[dict]]:
    from poed import scan_cache

    scan_cache.begin_scan()
    frame = sample.load()
    started = time.perf_counter()
    output = system.analyze(frame, rows)
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    matches = normalize_matches(list(output.matches), "ritual") if output.fired else []

    record = SampleRecord(
        sample_id=sample.id,
        kind=sample.kind,
        system=system.id,
        fired=output.fired,
        expected_route=sample.expected_route,
        match_count=len(matches),
        elapsed_ms=round(elapsed_ms, 2),
        timings_ms=dict(output.timings_ms),
        evidence=list(output.evidence),
    )

    if sample.kind == "fp":
        record.l1 = not output.fired
        if output.fired:
            record.reasons.append(
                f"fp fired ({sample.metadata.get('fpSource')}) with {len(matches)} matches"
            )
    elif sample.kind == "corpus":
        evaluation = scan_corpus.evaluate_case_output(
            expected="ritual",
            metadata=sample.metadata,
            actual="ritual" if output.fired else "none",
            matches=matches,
        )
        record.l1 = evaluation.tiers.get("routing", False)
        record.l2 = evaluation.tiers.get("count")
        record.l3 = evaluation.tiers.get("semantic")
        record.reasons.extend(evaluation.reasons)
    elif sample.kind == "synth":
        record.l1 = output.fired
        if not output.fired:
            record.reasons.append("did not fire on synth ritual frame")
        _score_synth(record, sample, matches)
    elif sample.kind == "debug":
        record.l1 = output.fired
        if not output.fired:
            record.reasons.append("did not fire on retained ritual frame")
    return record, output, frame, matches


METAMORPHIC_SHIFT = (23, 11)
METAMORPHIC_TOLERANCE_PX = 4


def _name_multiset(matches: list[dict]) -> dict[str, int]:
    out: dict[str, int] = {}
    for match in matches:
        name = str(match.get("name") or "")
        out[name] = out.get(name, 0) + 1
    return out


def metamorphic_record(
    system: RitualSystem,
    sample: Sample,
    rows: dict,
) -> SampleRecord:
    """Invariance checks that need no stored truth: translating the frame must
    translate every match by the same amount, downscaling must preserve the
    name multiset, and repeated runs must be identical."""
    frame = sample.load()
    base = system.analyze(frame, rows)
    record = SampleRecord(
        sample_id=f"{sample.id}#meta",
        kind="metamorphic",
        system=system.id,
        fired=base.fired,
        expected_route=sample.expected_route,
        match_count=len(base.matches),
        elapsed_ms=0.0,
    )
    if not base.fired:
        record.l1 = False
        record.reasons.append("base run did not fire")
        return record

    repeat = system.analyze(frame, rows)
    base_set = sorted(
        (m.get("name"), m.get("x"), m.get("y")) for m in base.matches
    )
    repeat_set = sorted(
        (m.get("name"), m.get("x"), m.get("y")) for m in repeat.matches
    )
    deterministic = base_set == repeat_set
    if not deterministic:
        record.reasons.append("non-deterministic output")

    dx, dy = METAMORPHIC_SHIFT
    shifted = frame[dy:, dx:]
    moved = system.analyze(shifted, rows)
    shift_ok = moved.fired and len(moved.matches) == len(base.matches)
    if shift_ok:
        base_sorted = sorted(base.matches, key=lambda m: (m.get("y"), m.get("x")))
        moved_sorted = sorted(moved.matches, key=lambda m: (m.get("y"), m.get("x")))
        for a, b in zip(base_sorted, moved_sorted):
            if a.get("name") != b.get("name"):
                shift_ok = False
                record.reasons.append(
                    f"shift name change: {a.get('name')} -> {b.get('name')}"
                )
                break
            if (
                abs((a.get("x") - dx) - b.get("x")) > METAMORPHIC_TOLERANCE_PX
                or abs((a.get("y") - dy) - b.get("y")) > METAMORPHIC_TOLERANCE_PX
            ):
                shift_ok = False
                record.reasons.append(
                    f"shift moved {a.get('name')} by unexpected offset"
                )
                break
    else:
        record.reasons.append(
            f"shifted run fired={moved.fired} matches={len(moved.matches)} "
            f"(base {len(base.matches)})"
        )

    import cv2

    scaled = cv2.resize(frame, None, fx=2 / 3, fy=2 / 3, interpolation=cv2.INTER_AREA)
    down = system.analyze(scaled, rows)
    scale_ok = down.fired and _name_multiset(down.matches) == _name_multiset(base.matches)
    if not scale_ok:
        record.reasons.append(
            f"downscale fired={down.fired} matches={len(down.matches)} "
            f"names_equal={_name_multiset(down.matches) == _name_multiset(base.matches)}"
        )

    record.l1 = deterministic and shift_ok and scale_ok
    return record


def summarize(records: list[SampleRecord]) -> dict[str, Any]:
    by_system: dict[str, dict[str, Any]] = {}
    for record in records:
        summary = by_system.setdefault(
            record.system,
            {
                "corpus_l1": [0, 0],
                "corpus_l2": [0, 0],
                "corpus_l3": [0, 0],
                "fp_fired": 0,
                "fp_total": 0,
                "synth_recall": [],
                "synth_precision": [],
                "synth_iou": [],
                "synth_name_acc": [],
                "debug_fired": [0, 0],
                "meta_pass": [0, 0],
                "latency_ms": [],
            },
        )
        if record.kind == "corpus":
            for tier, value in (("l1", record.l1), ("l2", record.l2), ("l3", record.l3)):
                bucket = summary[f"corpus_{tier}"]
                if value is not None:
                    bucket[1] += 1
                    bucket[0] += int(bool(value))
            summary["latency_ms"].append(record.elapsed_ms)
        elif record.kind == "fp":
            summary["fp_total"] += 1
            summary["fp_fired"] += int(record.fired)
        elif record.kind == "synth":
            if record.position_recall is not None:
                summary["synth_recall"].append(record.position_recall)
            if record.position_precision is not None:
                summary["synth_precision"].append(record.position_precision)
            if record.iou_mean is not None:
                summary["synth_iou"].append(record.iou_mean)
            if record.name_accuracy is not None:
                summary["synth_name_acc"].append(record.name_accuracy)
            summary["latency_ms"].append(record.elapsed_ms)
        elif record.kind == "debug":
            summary["debug_fired"][1] += 1
            summary["debug_fired"][0] += int(record.fired)
            summary["latency_ms"].append(record.elapsed_ms)
        elif record.kind == "metamorphic":
            summary["meta_pass"][1] += 1
            summary["meta_pass"][0] += int(bool(record.l1))

    def _fraction(pair: list[int]) -> str:
        return f"{pair[0]}/{pair[1]}" if pair[1] else "-"

    def _mean(values: list[float]) -> float | None:
        return round(float(np.mean(values)), 3) if values else None

    out = {}
    for system_id, summary in sorted(by_system.items()):
        latencies = sorted(summary["latency_ms"])
        out[system_id] = {
            "corpus_l1": _fraction(summary["corpus_l1"]),
            "corpus_l2": _fraction(summary["corpus_l2"]),
            "corpus_l3": _fraction(summary["corpus_l3"]),
            "fp_fires": f"{summary['fp_fired']}/{summary['fp_total']}"
            if summary["fp_total"]
            else "-",
            "synth_recall": _mean(summary["synth_recall"]),
            "synth_precision": _mean(summary["synth_precision"]),
            "synth_iou": _mean(summary["synth_iou"]),
            "synth_name_acc": _mean(summary["synth_name_acc"]),
            "debug_fired": _fraction(summary["debug_fired"]),
            "meta_pass": _fraction(summary["meta_pass"]),
            "latency_p50_ms": round(latencies[len(latencies) // 2], 1) if latencies else None,
            "latency_p95_ms": round(latencies[int(len(latencies) * 0.95)], 1)
            if latencies
            else None,
        }
    return out


def new_run_dir() -> Path:
    stamp = time.strftime("%Y%m%dT%H%M%S", time.gmtime())
    run_dir = lab_state_dir() / "results" / stamp
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def save_results(
    run_dir: Path,
    records: list[SampleRecord],
    summary: dict[str, Any],
    *,
    datasets: list[str],
) -> None:
    payload = {
        "datasets": datasets,
        "generatedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "summary": summary,
        "records": [asdict(record) for record in records],
    }
    (run_dir / "results.json").write_text(
        json.dumps(payload, indent=1) + "\n", encoding="utf-8"
    )
