"""Ritual lab CLI.

    python -m poed.ritual_lab snapshot-rows
    python -m poed.ritual_lab donor --scan scan-... --panel X,Y,W,H --id donor-a
    python -m poed.ritual_lab synth --count 24 --seed 7
    python -m poed.ritual_lab fp-crops
    python -m poed.ritual_lab run --systems s0 --datasets corpus,fp
    python -m poed.ritual_lab report
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import cv2

from poed.image_geometry import Rect


def _cmd_snapshot_rows(args: argparse.Namespace) -> int:
    from poed import scan_review

    from .datasets import lab_state_dir, rows_snapshot_path

    cfg, rows, source = scan_review.review_context()
    lab_state_dir().mkdir(parents=True, exist_ok=True)
    payload = {
        "league": cfg.get("league"),
        "source": source,
        "savedAt": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "rows": rows,
    }
    rows_snapshot_path().write_text(json.dumps(payload) + "\n", encoding="utf-8")
    with_icons = sum(1 for row in rows.values() if row.get("iconPath"))
    print(
        f"saved {len(rows)} rows ({with_icons} with icons) from {source} "
        f"to {rows_snapshot_path()}"
    )
    return 0


def _cmd_donor(args: argparse.Namespace) -> int:
    from poed import config as poed_config
    from poed import scan_review

    from . import synth

    scan_dir = scan_review.resolve_scan(args.scan)
    frame_path = scan_review.frame_path(scan_dir)
    frame = cv2.imread(str(frame_path))
    if frame is None:
        print(f"could not read {frame_path}", file=sys.stderr)
        return 1
    donor_id = args.id or scan_dir.name
    lattice = None
    if args.panel:
        x, y, w, h = (int(v) for v in args.panel.split(","))
        panel_rect = Rect(x, y, w, h)
    else:
        try:
            poed_config.apply_ocr_environment(poed_config.load())
        except Exception:  # noqa: BLE001
            pass
        os.environ["WAYSTONE_PADDLE_DEVICE"] = "cpu"
        from .s2_chrome import locate_panel

        panel, lattice, notes = locate_panel(frame)
        if panel is None:
            print(f"could not locate Favours panel: {notes}", file=sys.stderr)
            return 1
        panel_rect = panel.rect
    donor, _overlay = synth.build_donor(donor_id, frame, panel_rect, lattice)
    root = synth.donors_dir() / donor_id
    print(f"donor {donor_id}: lattice pitch=({donor.lattice.pitch_x:.2f}, "
          f"{donor.lattice.pitch_y:.2f}) cols={donor.lattice.cols} rows={donor.lattice.rows}")
    print(f"review overlay: {root / 'donor-overlay.jpg'}")
    return 0


def _cmd_synth(args: argparse.Namespace) -> int:
    from . import synth
    from .datasets import load_rows

    donor_ids = args.donors.split(",") if args.donors else [
        path.name for path in sorted(synth.donors_dir().iterdir()) if path.is_dir()
    ]
    if not donor_ids:
        print("no donors built; run the donor command first", file=sys.stderr)
        return 1
    donors = [synth.Donor.load(donor_id) for donor_id in donor_ids]
    rows = load_rows()
    composites = synth.generate(
        donors, rows, count=args.count, seed=args.seed, sparse_chance=args.sparse_chance
    )
    print(f"generated {len(composites)} composites in {synth.donors_dir().parent}")
    return 0


def _cmd_fp_crops(args: argparse.Namespace) -> int:
    from .datasets import build_inventory_crops

    written = build_inventory_crops(limit=args.limit)
    print(f"wrote {len(written)} inventory crops")
    return 0


def _cmd_run(args: argparse.Namespace) -> int:
    from poed import config as poed_config

    from . import report, scoring
    from .datasets import load_rows, resolve_datasets
    from .systems import build_systems

    try:
        poed_config.apply_ocr_environment(poed_config.load())
    except Exception as e:  # noqa: BLE001 - OCR is optional for geometry-only runs
        print(f"ocr environment not applied: {e}", file=sys.stderr)
    # The game may own the GPU while the lab runs; CPU recognition is plenty
    # for plaque strips and keeps lab runs reproducible on any host.
    os.environ["WAYSTONE_PADDLE_DEVICE"] = "cpu"

    datasets = args.datasets.split(",")
    samples = resolve_datasets(datasets)
    if args.limit:
        samples = samples[: args.limit]
    if not samples:
        print("no samples resolved; build synth/fp inputs first", file=sys.stderr)
        return 1
    systems = build_systems(args.systems.split(","))
    rows = load_rows()

    run_dir = scoring.new_run_dir()
    records = []
    for system in systems:
        for sample in samples:
            try:
                record, output, frame, matches = scoring.score_sample(system, sample, rows)
            except RuntimeError as e:
                print(f"[{system.id}] {sample.id} skipped: {e}", flush=True)
                continue
            want_overlay = args.overlays == "all" or (
                args.overlays == "failures"
                and (record.reasons or any(v is False for v in (record.l1, record.l2, record.l3)))
            )
            if want_overlay:
                marked = report.render_overlay(frame, output, matches, sample)
                path = report.overlay_path(run_dir, system.id, sample.id)
                cv2.imwrite(str(path), marked, [cv2.IMWRITE_JPEG_QUALITY, 88])
                record.overlay = str(path)
            records.append(record)
            print(
                f"[{system.id}] {sample.id} fired={record.fired} "
                f"matches={record.match_count} {record.elapsed_ms:.0f}ms",
                flush=True,
            )

    summary = scoring.summarize(records)
    scoring.save_results(run_dir, records, summary, datasets=datasets)
    scoreboard = report.format_scoreboard(summary, datasets)
    (run_dir / "scoreboard.md").write_text(scoreboard, encoding="utf-8")
    failures = report.format_failures(records)
    if failures:
        (run_dir / "failures.txt").write_text(failures, encoding="utf-8")
    print()
    print(scoreboard)
    if failures:
        print(failures)
    print(f"run dir: {run_dir}")
    return 0


def _cmd_report(args: argparse.Namespace) -> int:
    from . import report

    run_dir = Path(args.run) if args.run else report.latest_run_dir()
    if run_dir is None or not run_dir.exists():
        print("no lab runs found", file=sys.stderr)
        return 1
    scoreboard = run_dir / "scoreboard.md"
    if scoreboard.exists():
        print(scoreboard.read_text(encoding="utf-8"))
    failures = run_dir / "failures.txt"
    if failures.exists():
        print(failures.read_text(encoding="utf-8"))
    print(f"run dir: {run_dir}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m poed.ritual_lab")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("snapshot-rows")

    donor = sub.add_parser("donor")
    donor.add_argument("--scan", default="latest")
    donor.add_argument("--panel", default=None, help="X,Y,W,H grid interior override")
    donor.add_argument("--id", default=None)

    synth = sub.add_parser("synth")
    synth.add_argument("--count", type=int, default=24)
    synth.add_argument("--seed", type=int, default=7)
    synth.add_argument("--donors", default=None)
    synth.add_argument("--sparse-chance", type=float, default=0.12)

    fp_crops = sub.add_parser("fp-crops")
    fp_crops.add_argument("--limit", type=int, default=8)

    run = sub.add_parser("run")
    run.add_argument("--systems", default="s0")
    run.add_argument("--datasets", default="corpus,fp")
    run.add_argument("--limit", type=int, default=0)
    run.add_argument("--overlays", choices=("none", "failures", "all"), default="failures")

    rep = sub.add_parser("report")
    rep.add_argument("--run", default=None)

    args = parser.parse_args(argv)
    handlers = {
        "snapshot-rows": _cmd_snapshot_rows,
        "donor": _cmd_donor,
        "synth": _cmd_synth,
        "fp-crops": _cmd_fp_crops,
        "run": _cmd_run,
        "report": _cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    raise SystemExit(main())
