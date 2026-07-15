from __future__ import annotations

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass

from .common import (
    begin_debug_attempt,
    finalize_debug_attempt,
    update_debug_manifest,
    write_result_stage,
)
from poed.image_geometry import frame_source
from poed import scan_cache
from .types import Detection, ScanContext, ScanResult, Scanner
from .scene import SceneAnalysis

_LOG = logging.getLogger("waystone.scanners")
_ADDITIVE_SCANNERS = frozenset({"runeshape"})


@dataclass(frozen=True)
class EngineResult:
    result: ScanResult
    output: str
    rows_elapsed: float
    scan_elapsed: float
    timings: dict[str, object]
    debug_dir: object = None


@dataclass(frozen=True)
class ScannerSelection:
    scanner: Scanner
    detection: Detection


def default_scanners() -> list[Scanner]:
    from .have import HaveScanner
    from .merchant import MerchantScanner
    from .ritual import RitualScanner
    from .runeshape import RuneshapeScanner
    from .expedition import ExpeditionScanner

    # Cheap, high-specificity visual probes go first. Expedition remains the
    # OCR fallback because its parchment rows require Paddle after Have,
    # merchant/vendor grids, Ritual, and in-world runeshape strips have been
    # ruled out.
    return [
        HaveScanner(),
        MerchantScanner(),
        RitualScanner(),
        RuneshapeScanner(),
        ExpeditionScanner(),
    ]


def _probe_scanners(
    ctx: ScanContext,
    scanners: list[Scanner] | None = None,
    scene: SceneAnalysis | None = None,
    combine_additive: bool = False,
    probe_timings: dict[str, float] | None = None,
) -> list[ScannerSelection]:
    scene = scene or SceneAnalysis(ctx.frame)
    scanner_list = scanners or default_scanners()
    additive_ids = _ADDITIVE_SCANNERS if combine_additive else frozenset()
    selections: list[ScannerSelection] = []
    timings = probe_timings if probe_timings is not None else {}
    best_primary: ScannerSelection | None = None
    additive_detected = False
    for index, scanner in enumerate(scanner_list):
        scanner_id = str(getattr(scanner, "id", ""))
        is_additive = scanner_id in additive_ids
        primary_is_definitive = (
            best_primary is not None
            and best_primary.detection.confidence >= 1.0
        )
        if primary_is_definitive and not is_additive:
            has_unprobed_additive = any(
                str(getattr(item, "id", "")) in additive_ids
                for item in scanner_list[index:]
            )
            if additive_detected or not has_unprobed_additive:
                break
            continue

        should_probe = getattr(scanner, "should_probe", None)
        if callable(should_probe) and not should_probe(
            ctx,
            scene,
            additive_detected=additive_detected,
            primary_detected=best_primary is not None,
        ):
            timings[scanner_id] = 0.0
            continue

        started = time.perf_counter()
        detection = scanner.probe(ctx, scene)
        timings[scanner_id] = round(
            (time.perf_counter() - started) * 1000,
            2,
        )
        if detection is None:
            continue
        selection = ScannerSelection(scanner, detection)
        selections.append(selection)
        if is_additive:
            additive_detected = True
        else:
            best_primary = _best_selection(
                [item for item in (best_primary, selection) if item is not None]
            )
    update_debug_manifest(ctx.debug_dir, probe_timings_ms=timings)
    return selections


def _scanner_priority(scanner: Scanner) -> float:
    return float(getattr(scanner, "priority", 0.0))


def _best_selection(selections: list[ScannerSelection]) -> ScannerSelection | None:
    best = None
    best_score = None
    for index, selection in enumerate(selections):
        # Probes can overlap: broad geometric detectors can interpret another
        # panel as a valid grid. Confidence remains the primary signal, but
        # scanners may expose a small static priority for equally confident
        # detections whose probe uses more specific evidence, such as a
        # recognized window title. Later scanners still win exact ties.
        score = (
            selection.detection.confidence,
            _scanner_priority(selection.scanner),
            index,
        )
        if best_score is None or score >= best_score:
            best = selection
            best_score = score
    return best




def select_scanners(
    ctx: ScanContext,
    scanners: list[Scanner] | None = None,
    scene: SceneAnalysis | None = None,
) -> list[ScannerSelection]:
    selections = _probe_scanners(ctx, scanners, scene, combine_additive=True)
    return _combine_selections(selections)


def _combine_selections(selections: list[ScannerSelection]) -> list[ScannerSelection]:
    additive = [
        selection
        for selection in selections
        if selection.scanner.id in _ADDITIVE_SCANNERS
    ]
    primary = _best_selection([
        selection
        for selection in selections
        if selection.scanner.id not in _ADDITIVE_SCANNERS
    ])
    if primary is None:
        return additive[:1]
    return [primary, *additive]


def _combined_result(results: list[ScanResult]) -> ScanResult:
    if not results:
        return ScanResult("none", "screen scan", [])
    if len(results) == 1:
        return results[0]
    titles = []
    for result in results:
        if result.title not in titles:
            titles.append(result.title)
    matches = [
        match
        for result in results
        for match in result.matches
    ]
    return ScanResult("combination", " + ".join(titles), matches)


def _elapsed_ms(started: float) -> float:
    return round((time.perf_counter() - started) * 1000.0, 2)


# Last uniqueprices snapshot per process: (league, brain version, rows).
# The brain bumps the version only when its corpus snapshot rebuilds, so an
# unchanged reply skips re-shipping and re-parsing the multi-megabyte row
# dict and keeps the SAME rows object, which downstream identity caches
# (template corpus, row filters) rely on.
_rows_cache: tuple[str, int, dict] | None = None
_rows_cache_lock = threading.Lock()


def _fetch_rows(brain, cfg: dict) -> tuple[dict, float]:
    global _rows_cache
    started = time.perf_counter()
    league = cfg["league"]
    with _rows_cache_lock:
        cached = _rows_cache
    request: dict = {"cmd": "uniqueprices", "league": league}
    if cached is not None and cached[0] == league:
        request["ifVersion"] = cached[1]
    reply = brain.request(request, timeout=120.0)
    if isinstance(reply, dict) and "version" in reply:
        if reply.get("unchanged") and cached is not None:
            return cached[2], _elapsed_ms(started)
        rows = reply.get("rows") or {}
        with _rows_cache_lock:
            _rows_cache = (league, int(reply["version"]), rows)
        return rows, _elapsed_ms(started)
    # Older brain without the versioned handshake: plain rows dict.
    return reply, _elapsed_ms(started)


_rows_executor_lock = threading.Lock()
_rows_executor_instance: ThreadPoolExecutor | None = None


def _rows_executor() -> ThreadPoolExecutor:
    """Reused rows-fetch thread; per-press pool construction adds nothing."""
    global _rows_executor_instance
    with _rows_executor_lock:
        if _rows_executor_instance is None:
            _rows_executor_instance = ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="screen-scan-rows"
            )
        return _rows_executor_instance


def run(brain, desktop, cfg: dict, scanners: list[Scanner] | None = None) -> EngineResult:
    t0 = time.monotonic()
    timings: dict[str, object] = {}
    scan_cache.begin_scan()
    executor = _rows_executor()
    rows_future = executor.submit(_fetch_rows, brain, cfg)
    started = time.perf_counter()
    output = desktop.active_game_output()
    timings["active_output_ms"] = _elapsed_ms(started)
    if output is None:
        raise RuntimeError("no active monitor found")
    started = time.perf_counter()
    shot = desktop.capture_output(output)
    timings["capture_ms"] = _elapsed_ms(started)
    if shot is None:
        raise RuntimeError("screen capture failed")
    started = time.perf_counter()
    rows, rows_request_ms = rows_future.result()
    timings["rows_wait_ms"] = _elapsed_ms(started)
    timings["rows_request_ms"] = rows_request_ms
    t_rows = time.monotonic()
    started = time.perf_counter()
    game_rect = desktop.active_game_rect(output, (shot.shape[1], shot.shape[0]))
    timings["active_game_rect_ms"] = _elapsed_ms(started)
    started = time.perf_counter()
    frame, fx0, fy0, source = frame_source(shot, game_rect)
    timings["frame_source_ms"] = _elapsed_ms(started)
    started = time.perf_counter()
    debug_dir = begin_debug_attempt(output, shot, frame, fx0, fy0, source)
    timings["debug_begin_ms"] = _elapsed_ms(started)
    ctx = ScanContext(
        cfg=cfg,
        output=output,
        shot=shot,
        frame=frame,
        frame_x=fx0,
        frame_y=fy0,
        source=source,
        rows=rows,
        game_rect=game_rect,
        debug_dir=debug_dir,
    )
    try:
        started = time.perf_counter()
        scene = SceneAnalysis(frame)
        timings["scene_ms"] = _elapsed_ms(started)
        probe_timings: dict[str, float] = {}
        started = time.perf_counter()
        probed = _probe_scanners(
            ctx,
            scanners,
            scene=scene,
            combine_additive=True,
            probe_timings=probe_timings,
        )
        selections = _combine_selections(probed)
        timings["selection_ms"] = _elapsed_ms(started)
        timings["probe_ms"] = dict(probe_timings)
        if not selections:
            result = ScanResult("none", "screen scan", [])
        else:
            selected_ids = [selection.scanner.id for selection in selections]
            selected_scanner = (
                selected_ids[0] if len(selected_ids) == 1 else "combination"
            )
            _LOG.info(
                "screen scan selected %s",
                ", ".join(
                    f"{selection.scanner.id}={selection.detection.confidence:.2f}"
                    for selection in selections
                ),
            )
            update_debug_manifest(
                debug_dir,
                selected_scanner=selected_scanner,
                selected_scanners=selected_ids,
                confidence=max(
                    selection.detection.confidence
                    for selection in selections
                ),
                evidence={
                    selection.scanner.id: list(selection.detection.evidence)
                    for selection in selections
                },
                scene_preparation_ms=round(scene.preparation_elapsed * 1000, 2),
            )
            scan_started = time.perf_counter()
            results = []
            scanner_timings = {}
            for selection in selections:
                child_started = time.perf_counter()
                results.append(selection.scanner.scan(ctx, selection.detection))
                scanner_timings[selection.scanner.id] = round(
                    (time.perf_counter() - child_started) * 1000,
                    2,
                )
            result = _combined_result(results)
            update_debug_manifest(
                debug_dir,
                extraction_ms=round(
                    (time.perf_counter() - scan_started) * 1000,
                    2,
                ),
                scanner_extraction_ms=scanner_timings,
            )
            timings["scanner_extraction_ms"] = dict(scanner_timings)
        started = time.perf_counter()
        write_result_stage(
            debug_dir,
            "99-summary.jpg",
            shot,
            result.matches,
            f"scanner={result.scanner_id} matches={len(result.matches)}",
        )
        update_debug_manifest(
            debug_dir,
            status="complete",
            selected_scanner=result.scanner_id,
            matches=len(result.matches),
            timings_ms=timings,
        )
        timings["debug_result_ms"] = _elapsed_ms(started)
        started = time.perf_counter()
        finalize_debug_attempt(debug_dir)
        timings["debug_finalize_ms"] = _elapsed_ms(started)
    except Exception as e:
        started = time.perf_counter()
        write_result_stage(
            debug_dir,
            "99-error.jpg",
            shot,
            [],
            f"error: {type(e).__name__}: {e}",
        )
        update_debug_manifest(
            debug_dir,
            status="error",
            error=f"{type(e).__name__}: {e}",
            timings_ms=timings,
        )
        timings["debug_error_ms"] = _elapsed_ms(started)
        started = time.perf_counter()
        finalize_debug_attempt(debug_dir)
        timings["debug_finalize_ms"] = _elapsed_ms(started)
        raise
    t_scan = time.monotonic()
    timings["rows_elapsed_ms"] = round((t_rows - t0) * 1000.0, 2)
    timings["scan_elapsed_ms"] = round((t_scan - t_rows) * 1000.0, 2)
    return EngineResult(
        result=result,
        output=output,
        rows_elapsed=t_rows - t0,
        scan_elapsed=t_scan - t_rows,
        timings=timings,
        debug_dir=debug_dir,
    )


def warm(brain, cfg: dict) -> None:
    for scanner in default_scanners():
        scanner.warm(brain, cfg)


def stop() -> None:
    for scanner in default_scanners():
        scanner.stop()
