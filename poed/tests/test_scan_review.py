from pathlib import Path

import numpy as np
import pytest

from poed import scan_review

cv2 = pytest.importorskip("cv2")


def test_suggested_category_identifies_multi_rune():
    category = scan_review.suggested_category("runeshape", [
        {"scanKind": "runeshape", "name": "A"},
        {"scanKind": "runeshape", "name": "B"},
    ])

    assert category == "multi-rune"


def test_review_context_can_use_explicit_fixture_rows(monkeypatch):
    monkeypatch.setenv("WAYSTONE_SCAN_REVIEW_FIXTURE_ROWS", "1")

    cfg, rows, source = scan_review.review_context()

    assert source == "fixture"
    assert cfg["league"] == "fixture"
    assert "Orb of Alchemy" in rows


def test_run_current_scan_uses_injected_rows_and_config(tmp_path, monkeypatch):
    scan_dir = tmp_path / "scan-1"
    scan_dir.mkdir()
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(scan_dir / "01-game-frame.png"), image)
    monkeypatch.setattr(scan_review.expeditionscan, "warm", lambda: None)
    monkeypatch.setattr(scan_review.expeditionscan, "stop", lambda: None)
    monkeypatch.setattr(scan_review.core, "select_scanners", lambda ctx: [])

    data = scan_review.run_current_scan(
        scan_dir,
        cfg={"league": "fixture", "unique_scan_min_price": 0.0},
        rows={},
    )

    assert data.actual == "none"
    assert data.matches == []


def test_truth_metadata_level_2_stores_counts_and_rune_counts():
    matches = [
        {"scanKind": "runeshape", "name": "A", "runeshapeRunes": ["r1", "r2"]},
        {"scanKind": "runeshape", "name": "B", "runeshapeRunes": ["r1"]},
    ]

    metadata = scan_review.truth_metadata(
        level=2,
        expected="runeshape",
        category="multi-rune",
        matches=matches,
    )

    assert metadata == {
        "expectedCounts": {"runeshape": 2},
        "expectedRuneshapeRuneCounts": [1, 2],
    }


def test_truth_metadata_level_3_stores_names_stacks_and_runes():
    matches = [
        {
            "scanKind": "runeshape",
            "name": "B",
            "stackSize": 2,
            "x": 20,
            "y": 10,
            "runeshapeLevel": "Lv70+",
            "runeshapeRunes": ["r2"],
        },
        {
            "scanKind": "runeshape",
            "name": "A",
            "stackSize": 1,
            "x": 10,
            "y": 10,
            "runeshapeRunes": ["r1", "r3"],
        },
    ]

    metadata = scan_review.truth_metadata(
        level=3,
        expected="runeshape",
        category="multi-rune",
        matches=matches,
    )

    assert metadata["expectedCounts"] == {"runeshape": 2}
    assert metadata["expectedRuneshapeRuneCounts"] == [1, 2]
    assert metadata["expectedMatches"] == [
        {"scanKind": "runeshape", "name": "A", "stackSize": 1},
        {
            "scanKind": "runeshape",
            "name": "B",
            "stackSize": 2,
            "runeshapeLevel": "Lv70+",
        },
    ]
    assert metadata["expectedRuneshapeRuneSequences"] == [
        ["r1", "r3"],
        ["r2"],
    ]


def test_format_review_groups_output_by_level():
    data = scan_review.CurrentScanData(
        scan_dir=Path("/tmp/scan-1"),
        frame_path=Path("/tmp/scan-1/01-game-frame.png"),
        manifest={"selected_scanner": "none"},
        actual="runeshape",
        selected_scanners=["runeshape"],
        category="multi-rune",
        title="expedition runeshape",
        matches=[
            {
                "scanKind": "runeshape",
                "name": "A",
                "stackSize": 1,
                "runeshapeLevel": "Lv65-74",
                "x": 1,
                "y": 2,
                "runeshapeRunes": ["r1"],
            }
        ],
        elapsed_ms=12.3,
    )

    text = scan_review.format_review(data)

    assert "Level 1 — routing/category" in text
    assert "Level 2 — exact counts" in text
    assert "Level 3 — exact names and rune data" in text
    assert "A Lv65-74 x1" in text
    assert "--verification-level LEVEL --from-current-output" in text
