from __future__ import annotations

import json
import os
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import cv2

from poed import brain as brain_module
from poed import config, expeditionscan, scan_corpus, views
from poed.image_geometry import frame_source
from poed.scanners import core
from poed.scanners.core import ScannerSelection
from poed.scanners.scene import SceneAnalysis
from poed.scanners.types import ScanContext


@dataclass(frozen=True)
class CurrentScanData:
    scan_dir: Path
    frame_path: Path
    manifest: dict[str, Any]
    actual: str
    selected_scanners: list[str]
    category: str
    title: str
    matches: list[dict[str, Any]]
    elapsed_ms: float
    scan_error: str | None = None


def fixture_rows() -> dict[str, dict[str, Any]]:
    names = (
        "Orb of Alchemy",
        "Chaos Orb",
        "Exalted Orb",
        "Runic Alloy",
        "Regal Orb",
        "Orb of Augmentation",
        "Greater Orb of Augmentation",
        "Orb of Transmutation",
        "Greater Orb of Transmutation",
        "Greater Regal Orb",
        "Perfect Regal Orb",
        "Greater Chaos Orb",
        "Vaal Orb",
        "Divine Orb",
        "Orb of Chance",
        "Orb of Annulment",
        "Artificer's Orb",
        "Lesser Jeweller's Orb",
        "Greater Jeweller's Orb",
        "Perfect Jeweller's Orb",
        "Transmutation Shard",
        "Regal Shard",
        "Artificer's Shard",
        "Chance Shard",
        "Armourer's Scrap",
        "Blacksmith's Whetstone",
        "Glassblower's Bauble",
        "Gemcutter's Prism",
        "Scroll of Wisdom",
        "Cyclonic Alloy",
        "Expansive Alloy",
        "Vorana's Saga",
    )
    return {
        name: {
            "price": float(index + 1),
            "quantity": 1,
            "kind": "tagged",
            "iconPath": None,
            "w": 1,
            "h": 1,
        }
        for index, name in enumerate(names)
    }


def review_context() -> tuple[dict[str, Any], dict[str, Any], str]:
    """Return config and rows for current-scan review.

    Ritual/merchant review needs the same icon corpus the live app uses. The
    tiny fixture catalog is only a deterministic fallback for tests and local
    script debugging, enabled explicitly with WAYSTONE_SCAN_REVIEW_FIXTURE_ROWS=1.
    """

    if os.environ.get("WAYSTONE_SCAN_REVIEW_FIXTURE_ROWS") == "1":
        return {
            "league": "fixture",
            "unique_scan_min_price": 0.0,
        }, fixture_rows(), "fixture"

    cfg = config.load()
    config.apply_ocr_environment(cfg)
    with tempfile.TemporaryDirectory(prefix="waystone-review-brain-") as tmpdir:
        sock = str(Path(tmpdir) / "brain.sock")
        sessid = cfg["poesessid"] or ""
        brain = brain_module.Brain(
            brain_dir=brain_module.resolve_brain_dir(),
            socket_path=sock,
            env_extra={
                "POE2_LEAGUE": cfg["league"],
                **({"POE2_SESSID": sessid} if sessid else {}),
            },
        )
        brain.start()
        try:
            rows = brain.request(
                {"cmd": "uniqueprices", "league": cfg["league"]},
                timeout=120.0,
            )
        finally:
            brain.stop()
    return cfg.as_dict(), rows, "brain"


def scan_route(
    image: Any,
    cfg: dict[str, Any],
    rows: dict[str, Any],
    *,
    output: str = "fixture",
    timings_ms: dict[str, Any] | None = None,
) -> tuple[str, list[ScannerSelection], ScanContext]:
    """Build a ScanContext for a captured frame and pick the scanner route.

    Returns the route id ("combination", a single scanner id, or "none"),
    the raw selections for follow-up extraction, and the built context.
    When ``timings_ms`` is provided, per-stage benchmark timings are recorded
    into it (frame_source_ms, scene_ms, selection_ms, probe_ms).
    """
    started = time.perf_counter()
    frame, x0, y0, source = frame_source(image)
    if timings_ms is not None:
        timings_ms["frame_source_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
    ctx = ScanContext(
        cfg=cfg,
        output=output,
        shot=image,
        frame=frame,
        frame_x=x0,
        frame_y=y0,
        source=source,
        rows=rows,
    )
    if timings_ms is None:
        selections = core.select_scanners(ctx)
    else:
        started = time.perf_counter()
        scene = SceneAnalysis(frame)
        timings_ms["scene_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        probe_timings: dict[str, float] = {}
        started = time.perf_counter()
        probed = core._probe_scanners(
            ctx,
            scene=scene,
            combine_additive=True,
            probe_timings=probe_timings,
        )
        selections = core._combine_selections(probed)
        timings_ms["selection_ms"] = round((time.perf_counter() - started) * 1000.0, 2)
        timings_ms["probe_ms"] = probe_timings
    if len(selections) > 1:
        route = "combination"
    elif selections:
        route = selections[0].scanner.id
    else:
        route = "none"
    return route, selections, ctx


def resolve_scan(value: str = "latest") -> Path:
    root = scan_corpus.debug_scan_root()
    if value == "latest":
        scans = [path for path in root.glob("scan-*") if path.is_dir()]
        if not scans:
            raise FileNotFoundError(f"no retained scans found in {root}")
        return sorted(scans, key=lambda path: path.stat().st_mtime)[-1]

    path = Path(value).expanduser()
    if path.exists():
        return path
    candidate = root / value
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"scan not found: {value}")


def load_manifest(scan_dir: Path) -> dict[str, Any]:
    path = scan_dir / "manifest.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def frame_path(scan_dir: Path) -> Path:
    for name in ("01-game-frame.png", "00-capture.png"):
        path = scan_dir / name
        if path.exists():
            return path
    raise FileNotFoundError(f"scan has no frame image: {scan_dir}")


def suggested_category(actual: str, matches: list[dict[str, Any]]) -> str:
    label = views.screen_scan_route_label(actual, matches)
    if label in scan_corpus.EXPECTED_CATEGORIES:
        return label
    return actual if actual in scan_corpus.EXPECTED_CATEGORIES else "none"


def run_current_scan(
    scan_dir: Path,
    *,
    cfg: dict[str, Any] | None = None,
    rows: dict[str, Any] | None = None,
) -> CurrentScanData:
    path = frame_path(scan_dir)
    image = cv2.imread(str(path))
    if image is None:
        raise RuntimeError(f"could not read scan frame: {path}")
    if cfg is None or rows is None:
        cfg, rows, _source = review_context()

    scan_error = None
    started = time.perf_counter()
    expeditionscan.warm()
    try:
        actual, selections, ctx = scan_route(image, cfg, rows)
        selected = [selection.scanner.id for selection in selections]
        result = None
        if selections:
            try:
                result = core._combined_result([
                    selection.scanner.scan(ctx, selection.detection)
                    for selection in selections
                ])
            except Exception as e:  # noqa: BLE001 - review output must show scanner failures
                scan_error = f"{type(e).__name__}: {e}"
        title = result.title if result is not None else "screen scan"
        matches = result.matches if result is not None else []
    finally:
        expeditionscan.stop()

    elapsed_ms = (time.perf_counter() - started) * 1000.0
    category = suggested_category(actual, matches)
    return CurrentScanData(
        scan_dir=scan_dir,
        frame_path=path,
        manifest=load_manifest(scan_dir),
        actual=actual,
        selected_scanners=selected,
        category=category,
        title=title,
        matches=matches,
        elapsed_ms=elapsed_ms,
        scan_error=scan_error,
    )


def match_sort_key(match: dict[str, Any]) -> tuple[Any, ...]:
    return (
        str(match.get("scanKind") or ""),
        int(match.get("y") or 0),
        int(match.get("x") or 0),
        str(match.get("name") or ""),
        int(match.get("stackSize") or 1),
    )


def expected_counts(matches: list[dict[str, Any]], fallback: str) -> dict[str, int]:
    counts = Counter(str(match.get("scanKind") or fallback) for match in matches)
    return {key: counts[key] for key in sorted(counts)}


def runeshape_rune_counts(matches: list[dict[str, Any]]) -> list[int]:
    return sorted(
        len(match.get("runeshapeRunes") or ())
        for match in matches
        if str(match.get("scanKind") or "") == "runeshape"
    )


def runeshape_rune_sequences(matches: list[dict[str, Any]]) -> list[list[str]]:
    sequences = [
        [str(rune) for rune in match.get("runeshapeRunes") or ()]
        for match in sorted(matches, key=match_sort_key)
        if str(match.get("scanKind") or "") == "runeshape"
    ]
    return sequences


def expected_matches(
    matches: list[dict[str, Any]],
    fallback: str,
) -> list[dict[str, Any]]:
    reviewed = []
    for match in sorted(matches, key=match_sort_key):
        kind = str(match.get("scanKind") or fallback)
        item = {
            "scanKind": kind,
            "name": str(match.get("name") or ""),
        }
        try:
            stack = int(match.get("stackSize") or 1)
        except (TypeError, ValueError):
            stack = 1
        if kind == "runeshape" or stack != 1:
            item["stackSize"] = max(1, stack)
        if kind == "runeshape" and match.get("runeshapeLevel"):
            item["runeshapeLevel"] = str(match["runeshapeLevel"])
        reviewed.append(item)
    return reviewed


def truth_metadata(
    *,
    level: int,
    expected: str,
    category: str,
    matches: list[dict[str, Any]],
) -> dict[str, Any]:
    metadata: dict[str, Any] = {}
    if level >= 2:
        metadata["expectedCounts"] = expected_counts(matches, expected)
        rune_counts = runeshape_rune_counts(matches)
        if rune_counts or category in {"runeshape", "multi-rune"}:
            metadata["expectedRuneshapeRuneCounts"] = rune_counts
    if level >= 3:
        metadata["expectedMatches"] = expected_matches(matches, expected)
        rune_sequences = runeshape_rune_sequences(matches)
        if rune_sequences or category in {"runeshape", "multi-rune"}:
            metadata["expectedRuneshapeRuneSequences"] = rune_sequences
    return metadata


def to_jsonable(data: CurrentScanData) -> dict[str, Any]:
    return {
        "scan": data.scan_dir.name,
        "scanPath": str(data.scan_dir),
        "framePath": str(data.frame_path),
        "savedManifestRoute": data.manifest.get("selected_scanner"),
        "currentRoute": data.actual,
        "selectedScanners": data.selected_scanners,
        "suggestedCategory": data.category,
        "title": data.title,
        "elapsedMs": round(data.elapsed_ms, 2),
        "scanError": data.scan_error,
        "level1": {
            "expected": data.actual,
            "category": data.category,
        },
        "level2": truth_metadata(
            level=2,
            expected=data.actual,
            category=data.category,
            matches=data.matches,
        ),
        "level3": truth_metadata(
            level=3,
            expected=data.actual,
            category=data.category,
            matches=data.matches,
        ),
        "matches": sorted(data.matches, key=match_sort_key),
    }


def format_review(data: CurrentScanData) -> str:
    saved_route = data.manifest.get("selected_scanner")
    selected = ", ".join(data.selected_scanners) if data.selected_scanners else "none"
    counts = expected_counts(data.matches, data.actual)
    rune_counts = runeshape_rune_counts(data.matches)
    rune_sequences = runeshape_rune_sequences(data.matches)

    lines = [
        "Latest scan data",
        f"scan: {data.scan_dir.name}",
        f"frame: {data.frame_path}",
        f"saved manifest route: {saved_route or 'unknown'}",
        f"current route: {data.actual}",
        f"selected scanners: {selected}",
        f"suggested corpus category: {data.category}",
        f"current scan time: {data.elapsed_ms:.1f} ms",
        "",
        "Level 1 — routing/category",
        f"  expected route: {data.actual}",
        f"  category: {data.category}",
        "",
        "Level 2 — exact counts",
    ]
    if counts:
        for kind, count in counts.items():
            lines.append(f"  {kind}: {count}")
    else:
        lines.append("  matches: 0")
    if rune_counts:
        lines.append(f"  runeshape rune counts: {rune_counts}")
    elif data.category in {"runeshape", "multi-rune"}:
        lines.append("  runeshape rune counts: []")

    lines.extend(["", "Level 3 — exact names and rune data"])
    if not data.matches:
        lines.append("  no matches")
    for index, match in enumerate(sorted(data.matches, key=match_sort_key), start=1):
        kind = str(match.get("scanKind") or data.actual)
        name = views.match_display_name(match) or "<unnamed>"
        stack = int(match.get("stackSize") or 1)
        marker = " marker-only" if match.get("markerOnly") else ""
        pos = f"x={int(match.get('x') or 0)} y={int(match.get('y') or 0)}"
        lines.append(f"  {index}. {kind}: {name} x{stack}{marker} ({pos})")
        runes = [str(rune) for rune in match.get("runeshapeRunes") or ()]
        if runes:
            lines.append(f"     runes ({len(runes)}): {', '.join(runes)}")
    if rune_sequences and not any(match.get("runeshapeRunes") for match in data.matches):
        lines.append(f"  runeshape rune sequences: {rune_sequences}")
    if data.scan_error:
        lines.extend(["", f"scan error: {data.scan_error}"])

    lines.extend([
        "",
        "Tell Codex one of:",
        "  Level 0: skip this scan for now.",
        "  Level 1: route/category above is correct.",
        "  Level 2: route/category and exact counts above are correct.",
        "  Level 3: route/category, counts, names, and rune data above are correct.",
        "",
        "Promotion command template after confirmation:",
        (
            f"  scripts/promote-scan-case {data.scan_dir.name} "
            "--verification-level LEVEL --from-current-output "
            '--reason "<why this belongs in the corpus>"'
        ),
    ])
    return "\n".join(lines)
