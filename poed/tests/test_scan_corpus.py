from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

from poed import scan_corpus

cv2 = pytest.importorskip("cv2")


def _case(
    case_id: str,
    *,
    expected: str = "runeshape",
    category: str | None = None,
    level: int = 1,
    observed: str = "runeshape",
    status: str = "graduated",
    tags: list[str] | None = None,
    width: int = 3840,
    added: str = "2026-01-01T00:00:00Z",
):
    return {
        "id": case_id,
        "category": category or expected,
        "expected": expected,
        "verificationLevel": level,
        "observedAtAdmission": observed,
        "sourceScanId": f"scan-{case_id}",
        "image": f"images/{case_id}.webp",
        "resolution": {"width": width, "height": 2160},
        "status": status,
        "addedUtc": added,
        "reason": "fixture",
        "tags": tags or ["common"],
    }


def test_active_cases_include_probation_and_graduated(tmp_path: Path):
    index = scan_corpus.empty_index()
    index["cases"] = [
        _case("probation", status="probation"),
        _case("graduated", status="graduated"),
        _case("archived", status="archived"),
    ]

    cases = scan_corpus.active_cases(index, tmp_path)

    assert [case.id for case in cases] == ["graduated", "probation"]


def test_default_retention_policy_gives_ritual_larger_active_capacity():
    index = scan_corpus.empty_index()

    assert scan_corpus.max_active_for_category(index, "have") == 16
    assert scan_corpus.max_active_for_category(index, "ritual") == 50


def test_history_records_only_recent_outcomes():
    history = scan_corpus.empty_history()

    for index in range(scan_corpus.RECENT_OUTCOMES + 3):
        scan_corpus.record_outcome(
            history,
            case_id="case",
            expected="runeshape",
            actual="runeshape" if index % 2 else "none",
            elapsed_ms=10.0,
            recorded_at=f"2026-01-01T00:00:{index:02d}Z",
        )

    outcomes = scan_corpus.outcomes_for(history, "case")
    assert len(outcomes) == scan_corpus.RECENT_OUTCOMES
    assert outcomes[0]["at"] == "2026-01-01T00:00:03Z"


def test_history_records_detailed_corpus_failure_reasons():
    history = scan_corpus.empty_history()

    scan_corpus.record_outcome(
        history,
        case_id="case",
        expected="combination",
        actual="combination",
        elapsed_ms=10.0,
        passed=False,
        reasons=["count: expedition expected 7, actual 5"],
        tiers={"routing": True, "count": False},
        recorded_at="2026-01-01T00:00:00Z",
    )

    outcome = scan_corpus.outcomes_for(history, "case")[0]
    assert outcome["passed"] is False
    assert outcome["reasons"] == ["count: expedition expected 7, actual 5"]
    assert outcome["tiers"] == {"routing": True, "count": False}


def test_corpus_evaluation_routing_only_does_not_require_matches():
    result = scan_corpus.evaluate_case_output(
        expected="merchant",
        metadata={},
        actual="merchant",
        matches=[],
    )

    assert result.passed is True
    assert result.tiers == {"routing": True}
    assert result.reasons == []


def test_corpus_evaluation_count_tier_requires_exact_counts_per_kind():
    result = scan_corpus.evaluate_case_output(
        expected="combination",
        metadata={"expectedCounts": {"expedition": 2, "merchant": 1}},
        actual="combination",
        matches=[
            {"scanKind": "expedition", "name": "Orb of Alchemy"},
            {"scanKind": "merchant", "name": "The Blood Thorn"},
            {"scanKind": "ritual", "name": "Wrong Panel"},
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": False}
    assert result.reasons == [
        "count: expedition expected 2, actual 1",
        "count: unexpected ritual actual 1",
    ]


def test_corpus_evaluation_semantic_tier_uses_name_multiset():
    result = scan_corpus.evaluate_case_output(
        expected="combination",
        metadata={
            "expectedCounts": {"expedition": 3},
            "expectedMatches": [
                {"scanKind": "expedition", "name": "Glassblower's Bauble"},
                {"scanKind": "expedition", "name": "Glassblower's Bauble"},
                {"scanKind": "expedition", "name": "Regal Orb"},
            ]
        },
        actual="combination",
        matches=[
            {"scanKind": "expedition", "name": "Glassblower's Bauble"},
            {"scanKind": "expedition", "name": "Orb of Alchemy"},
            {"scanKind": "expedition", "name": "Regal Orb"},
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": True, "semantic": False}
    assert result.reasons == [
        "semantic: missing expedition Glassblower's Bauble",
        "semantic: unexpected expedition Orb of Alchemy",
    ]


def test_corpus_evaluation_optional_stack_assertions_are_supported():
    result = scan_corpus.evaluate_case_output(
        expected="expedition",
        metadata={
            "expectedCounts": {"expedition": 1},
            "expectedMatches": [
                {
                    "scanKind": "expedition",
                    "name": "Orb of Alchemy",
                    "stackSize": 3,
                }
            ]
        },
        actual="expedition",
        matches=[
            {
                "scanKind": "expedition",
                "name": "Orb of Alchemy",
                "stackSize": 1,
            }
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": True, "semantic": False}
    assert result.reasons == [
        "semantic: missing stack expedition Orb of Alchemy x3"
    ]


def test_level_2_is_hierarchical_and_fails_when_routing_fails():
    result = scan_corpus.evaluate_case_output(
        expected="runeshape",
        metadata={
            "verificationLevel": 2,
            "expectedCounts": {"runeshape": 1},
            "expectedRuneshapeRuneCounts": [2],
        },
        actual="none",
        matches=[
            {
                "scanKind": "runeshape",
                "name": "Vorana's Saga",
                "runeshapeRunes": ["a", "b"],
            }
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": False, "count": True}
    assert result.reasons == ["routing: expected runeshape, actual none"]


def test_level_3_requires_level_2_truth_even_when_names_match():
    result = scan_corpus.evaluate_case_output(
        expected="runeshape",
        metadata={
            "verificationLevel": 3,
            "expectedMatches": [
                {"scanKind": "runeshape", "name": "Vorana's Saga"}
            ],
        },
        actual="runeshape",
        matches=[
            {"scanKind": "runeshape", "name": "Vorana's Saga"}
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": False, "semantic": False}
    assert result.reasons == [
        "count: expectedCounts required for verificationLevel >= 2",
        "count: expectedRuneshapeRuneCounts required for runeshape verificationLevel >= 2",
        "semantic: expectedRuneshapeRuneSequences required for runeshape verificationLevel >= 3",
    ]


def test_level_2_multi_rune_checks_rune_glyph_counts():
    result = scan_corpus.evaluate_case_output(
        expected="runeshape",
        metadata={
            "category": "multi-rune",
            "verificationLevel": 2,
            "expectedCounts": {"runeshape": 2},
            "expectedRuneshapeRuneCounts": [3, 6],
        },
        actual="runeshape",
        matches=[
            {"scanKind": "runeshape", "name": "Expansive Alloy", "runeshapeRunes": ["a", "b", "c"]},
            {"scanKind": "runeshape", "name": "Vorana's Saga", "runeshapeRunes": ["a"] * 5},
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": False}
    assert result.reasons == [
        "count: missing runeshape row with 6 runes",
        "count: unexpected runeshape row with 5 runes",
    ]


def test_level_3_multi_rune_checks_rune_sequences():
    result = scan_corpus.evaluate_case_output(
        expected="runeshape",
        metadata={
            "category": "multi-rune",
            "verificationLevel": 3,
            "expectedCounts": {"runeshape": 1},
            "expectedRuneshapeRuneCounts": [3],
            "expectedRuneshapeRuneSequences": [["r1", "r2", "r3"]],
            "expectedMatches": [
                {"scanKind": "runeshape", "name": "Expansive Alloy"}
            ],
        },
        actual="runeshape",
        matches=[
            {
                "scanKind": "runeshape",
                "name": "Expansive Alloy",
                "runeshapeRunes": ["r1", "wrong", "r3"],
            }
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": True, "semantic": False}
    assert result.reasons == [
        "semantic: missing runeshape sequence r1,r2,r3",
        "semantic: unexpected runeshape sequence r1,wrong,r3",
    ]


def test_level_3_runeshape_checks_level_when_recorded():
    result = scan_corpus.evaluate_case_output(
        expected="runeshape",
        metadata={
            "category": "runeshape",
            "verificationLevel": 3,
            "expectedCounts": {"runeshape": 1},
            "expectedRuneshapeRuneCounts": [4],
            "expectedRuneshapeRuneSequences": [["r1", "r2", "r3", "r4"]],
            "expectedMatches": [
                {
                    "scanKind": "runeshape",
                    "name": "Unique",
                    "stackSize": 1,
                    "runeshapeLevel": "Lv65-74",
                }
            ],
        },
        actual="runeshape",
        matches=[
            {
                "scanKind": "runeshape",
                "name": "Unique",
                "stackSize": 1,
                "runeshapeLevel": "Lv70+",
                "runeshapeRunes": ["r1", "r2", "r3", "r4"],
            }
        ],
    )

    assert result.passed is False
    assert result.tiers == {"routing": True, "count": True, "semantic": False}
    assert result.reasons == [
        "semantic: missing runeshape level runeshape Unique Lv65-74"
    ]


def test_probation_graduates_after_configured_future_runs():
    index = scan_corpus.empty_index()
    index["cases"] = [_case("new-failure", status="probation")]
    history = scan_corpus.empty_history()
    for index_num in range(scan_corpus.PROBATION_RUNS - 1):
        scan_corpus.record_outcome(
            history,
            case_id="new-failure",
            expected="runeshape",
            actual="runeshape",
            elapsed_ms=10.0,
            recorded_at=f"2026-01-01T00:00:{index_num:02d}Z",
        )

    assert scan_corpus.graduate_probation(index, history) == []
    assert index["cases"][0]["status"] == "probation"

    scan_corpus.record_outcome(
        history,
        case_id="new-failure",
        expected="runeshape",
        actual="runeshape",
        elapsed_ms=10.0,
        recorded_at="2026-01-01T00:00:05Z",
    )
    assert scan_corpus.graduate_probation(index, history) == ["new-failure"]
    assert index["cases"][0]["status"] == "graduated"


def test_retention_reports_deficits_per_category_and_level():
    index = scan_corpus.empty_index()
    index["cases"] = [
        _case("level-1", category="multi-rune", level=1, status="probation"),
        _case("level-2", category="multi-rune", level=2, status="graduated"),
        _case("level-3", category="multi-rune", level=3, status="graduated"),
    ]

    deficits = scan_corpus.retention_deficits(index)

    assert deficits["multi-rune"] == {1: 2, 2: 2, 3: 9}


def test_retention_archives_only_surplus_above_configured_floor(tmp_path: Path):
    index = scan_corpus.empty_index()
    index["retentionFloorsByLevel"] = {"1": 2, "2": 0, "3": 0}
    index["maxActivePerCategoryLevel"] = 2
    index["cases"] = [
        _case("problematic", category="runeshape", level=1, observed="none", tags=["selected"], added="2026-01-01T00:00:00Z"),
        _case("duplicate-old", category="runeshape", level=1, tags=["common"], added="2026-01-01T00:00:01Z"),
        _case("duplicate-new", category="runeshape", level=1, tags=["common"], added="2026-01-01T00:00:02Z"),
    ]
    for case in index["cases"]:
        image = tmp_path / case["image"]
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fixture")
    history = scan_corpus.empty_history()
    scan_corpus.record_outcome(
        history,
        case_id="problematic",
        expected="runeshape",
        actual="none",
        elapsed_ms=10.0,
    )
    scan_corpus.record_outcome(
        history,
        case_id="duplicate-old",
        expected="runeshape",
        actual="runeshape",
        elapsed_ms=10.0,
    )
    scan_corpus.record_outcome(
        history,
        case_id="duplicate-new",
        expected="runeshape",
        actual="runeshape",
        elapsed_ms=10.0,
    )

    archived = scan_corpus.enforce_graduated_limits(tmp_path, index, history)

    assert archived == ["duplicate-old"]
    statuses = {case["id"]: case["status"] for case in index["cases"]}
    assert statuses == {
        "problematic": "graduated",
        "duplicate-old": "archived",
        "duplicate-new": "graduated",
    }
    assert not (tmp_path / "images/duplicate-old.webp").exists()


def test_retention_does_not_archive_at_or_below_floor(tmp_path: Path):
    index = scan_corpus.empty_index()
    index["retentionFloorsByLevel"] = {"1": 3, "2": 0, "3": 0}
    index["maxActivePerCategoryLevel"] = 2
    index["cases"] = [
        _case("one", category="runeshape", level=1),
        _case("two", category="runeshape", level=1),
        _case("three", category="runeshape", level=1),
    ]
    for case in index["cases"]:
        image = tmp_path / case["image"]
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fixture")

    archived = scan_corpus.enforce_graduated_limits(
        tmp_path,
        index,
        scan_corpus.empty_history(),
    )

    assert archived == []


def test_retention_category_limit_allows_more_ritual_cases(tmp_path: Path):
    index = scan_corpus.empty_index()
    index["retentionFloorsByLevel"] = {"1": 0, "2": 0, "3": 0}
    index["maxActivePerCategory"] = 2
    index["maxActivePerCategoryOverrides"] = {"ritual": 4}
    index["cases"] = [
        _case("ritual-one", category="ritual", expected="ritual", level=3, added="2026-01-01T00:00:00Z"),
        _case("ritual-two", category="ritual", expected="ritual", level=3, added="2026-01-01T00:00:01Z"),
        _case("ritual-three", category="ritual", expected="ritual", level=3, added="2026-01-01T00:00:02Z"),
        _case("ritual-four", category="ritual", expected="ritual", level=3, added="2026-01-01T00:00:03Z"),
        _case("merchant-old", category="merchant", expected="merchant", level=3, added="2026-01-01T00:00:00Z"),
        _case("merchant-mid", category="merchant", expected="merchant", level=3, added="2026-01-01T00:00:01Z"),
        _case("merchant-new", category="merchant", expected="merchant", level=3, added="2026-01-01T00:00:02Z"),
    ]
    for case in index["cases"]:
        image = tmp_path / case["image"]
        image.parent.mkdir(parents=True, exist_ok=True)
        image.write_bytes(b"fixture")

    archived = scan_corpus.enforce_graduated_limits(
        tmp_path,
        index,
        scan_corpus.empty_history(),
    )

    assert archived == ["merchant-old"]
    statuses = {case["id"]: case["status"] for case in index["cases"]}
    assert statuses["ritual-one"] == "graduated"
    assert statuses["ritual-two"] == "graduated"
    assert statuses["ritual-three"] == "graduated"
    assert statuses["ritual-four"] == "graduated"
    assert statuses["merchant-old"] == "archived"
    assert statuses["merchant-mid"] == "graduated"
    assert statuses["merchant-new"] == "graduated"


def test_remove_superseded_source_cases_deletes_lower_levels_and_images(tmp_path: Path):
    image_dir = tmp_path / "images"
    image_dir.mkdir()
    for name in ("level-1.webp", "level-2.webp", "other.webp"):
        (image_dir / name).write_text("x", encoding="utf-8")
    index = {
        "cases": [
            {
                "id": "level-1",
                "sourceScanId": "scan-1",
                "verificationLevel": 1,
                "status": "probation",
                "image": "images/level-1.webp",
            },
            {
                "id": "level-2",
                "sourceScanId": "scan-1",
                "verificationLevel": 2,
                "status": "graduated",
                "image": "images/level-2.webp",
            },
            {
                "id": "other",
                "sourceScanId": "scan-2",
                "verificationLevel": 1,
                "status": "probation",
                "image": "images/other.webp",
            },
        ],
    }

    removed = scan_corpus.remove_superseded_source_cases(
        tmp_path,
        index,
        source_scan_id="scan-1",
        new_level=3,
    )

    assert removed == ["level-1", "level-2"]
    assert [case["id"] for case in index["cases"]] == ["other"]
    assert not (image_dir / "level-1.webp").exists()
    assert not (image_dir / "level-2.webp").exists()
    assert (image_dir / "other.webp").exists()


def test_remove_superseded_source_cases_refuses_downgrade(tmp_path: Path):
    index = {
        "cases": [
            {
                "id": "level-3",
                "sourceScanId": "scan-1",
                "verificationLevel": 3,
                "status": "graduated",
            },
        ],
    }

    with pytest.raises(ValueError, match="refusing to replace higher-level"):
        scan_corpus.remove_superseded_source_cases(
            tmp_path,
            index,
            source_scan_id="scan-1",
            new_level=2,
        )


def test_expected_none_requires_explicit_intent():
    assert scan_corpus.validate_expected("none") == "none"
    with pytest.raises(ValueError):
        scan_corpus.validate_expected("accidental")


def test_promote_script_rejects_unmarked_none_scan(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    state = tmp_path / "state"
    scan = state / "waystone/debug/scans/scan-20260101T000000-000000001"
    scan.mkdir(parents=True)
    (scan / "manifest.json").write_text(
        json.dumps({"selected_scanner": "none", "status": "complete"}) + "\n",
        encoding="utf-8",
    )
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(scan / "01-game-frame.png"), image)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/promote-scan-case"),
            scan.name,
            "--expected",
            "none",
            "--corpus-root",
            str(tmp_path / "corpus"),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode != 0
    assert "--intentional-negative" in result.stderr


def test_promote_script_allows_explicit_real_failure_from_observed_none(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    scan = state / "waystone/debug/scans/scan-20260101T000000-000000001"
    scan.mkdir(parents=True)
    (scan / "manifest.json").write_text(
        json.dumps({"selected_scanner": "none", "status": "complete"}) + "\n",
        encoding="utf-8",
    )
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(scan / "01-game-frame.png"), image)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/promote-scan-case"),
            scan.name,
            "--expected",
            "runeshape",
            "--reason",
            "fixture",
            "--tag",
            "selected-state",
            "--corpus-root",
            str(corpus),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    index = scan_corpus.load_index(corpus)
    assert len(index["cases"]) == 1
    case = index["cases"][0]
    assert case["expected"] == "runeshape"
    assert case["observedAtAdmission"] == "none"
    assert case["status"] == "probation"
    assert (corpus / case["image"]).exists()


def test_promote_script_level_zero_skips_without_corpus_write(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    scan = state / "waystone/debug/scans/scan-20260101T000000-000000001"
    scan.mkdir(parents=True)
    (scan / "manifest.json").write_text(
        json.dumps({"selected_scanner": "runeshape", "status": "complete"}) + "\n",
        encoding="utf-8",
    )
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(scan / "01-game-frame.png"), image)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/promote-scan-case"),
            scan.name,
            "--verification-level",
            "0",
            "--corpus-root",
            str(corpus),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "no corpus changes" in result.stdout
    assert not (corpus / "index.json").exists()


def test_evaluate_corpus_ignores_retained_debug_scans(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    state = tmp_path / "state"
    corpus = tmp_path / "corpus"
    scan = state / "waystone/debug/scans/scan-20260101T000000-000000001"
    scan.mkdir(parents=True)
    (scan / "manifest.json").write_text(
        json.dumps({"selected_scanner": "runeshape", "status": "complete"}) + "\n",
        encoding="utf-8",
    )
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(scan / "01-game-frame.png"), image)
    env = os.environ.copy()
    env["XDG_STATE_HOME"] = str(state)

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/evaluate-scan-corpus"),
            "--corpus-root",
            str(corpus),
        ],
        cwd=repo,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "checked=0 failures=0"


def test_evaluate_corpus_records_history_for_curated_case(tmp_path: Path):
    repo = Path(__file__).resolve().parents[2]
    corpus = tmp_path / "corpus"
    image_dir = corpus / "images"
    image_dir.mkdir(parents=True)
    image = np.zeros((80, 120, 3), dtype=np.uint8)
    assert cv2.imwrite(str(image_dir / "none.png"), image)
    scan_corpus.write_index(
        corpus,
        {
            **scan_corpus.empty_index(),
            "cases": [
                {
                    "id": "none-case",
                    "expected": "none",
                    "category": "none",
                    "verificationLevel": 1,
                    "observedAtAdmission": "none",
                    "sourceScanId": "scan-none",
                    "image": "images/none.png",
                    "status": "graduated",
                    "addedUtc": "2026-01-01T00:00:00Z",
                    "reason": "fixture",
                    "tags": [],
                }
            ],
        },
    )
    history_path = tmp_path / "history.json"

    result = subprocess.run(
        [
            sys.executable,
            str(repo / "scripts/evaluate-scan-corpus"),
            "--corpus-root",
            str(corpus),
            "--record-history",
            "--history",
            str(history_path),
        ],
        cwd=repo,
        text=True,
        capture_output=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert "checked=1 failures=0" in result.stdout
    history = scan_corpus.load_history(history_path)
    [outcome] = scan_corpus.outcomes_for(history, "none-case")
    assert outcome["expected"] == "none"
    assert outcome["actual"] == "none"
    assert outcome["passed"] is True
