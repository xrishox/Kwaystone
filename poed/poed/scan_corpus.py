from __future__ import annotations

import json
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from poed import config
from typing import Any

EXPECTED_TYPES = frozenset({
    "have",
    "merchant",
    "ritual",
    "expedition",
    "runeshape",
    "combination",
    "none",
})
EXPECTED_CATEGORIES = frozenset({
    *EXPECTED_TYPES,
    "multi-rune",
})
ACTIVE_STATUSES = frozenset({"probation", "graduated"})
INDEX_VERSION = 2
HISTORY_VERSION = 1
RETENTION_FLOORS_BY_LEVEL = {1: 3, 2: 3, 3: 10}
DEFAULT_MAX_ACTIVE_PER_CATEGORY = sum(RETENTION_FLOORS_BY_LEVEL.values())
MAX_ACTIVE_PER_CATEGORY_OVERRIDES = {"ritual": 50}
PROBATION_RUNS = 5
RECENT_OUTCOMES = 10


@dataclass(frozen=True)
class CorpusCase:
    id: str
    expected: str
    image: Path
    status: str
    metadata: dict[str, Any]


@dataclass(frozen=True)
class CorpusEvaluation:
    passed: bool
    tiers: dict[str, bool]
    reasons: list[str]


def now_utc() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def state_root() -> Path:
    return config.state_home()


def debug_scan_root() -> Path:
    return state_root() / "waystone" / "debug" / "scans"


def history_path() -> Path:
    return state_root() / "waystone" / "debug" / "corpus-history.json"


def empty_index() -> dict[str, Any]:
    return {
        "version": INDEX_VERSION,
        "retentionFloorsByLevel": {
            str(level): count
            for level, count in RETENTION_FLOORS_BY_LEVEL.items()
        },
        "maxActivePerCategory": DEFAULT_MAX_ACTIVE_PER_CATEGORY,
        "maxActivePerCategoryOverrides": dict(MAX_ACTIVE_PER_CATEGORY_OVERRIDES),
        "probationRuns": PROBATION_RUNS,
        "cases": [],
    }


def load_index(corpus_root: Path) -> dict[str, Any]:
    path = corpus_root / "index.json"
    if not path.exists():
        return empty_index()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", INDEX_VERSION)
    data.setdefault(
        "retentionFloorsByLevel",
        {
            str(level): count
            for level, count in RETENTION_FLOORS_BY_LEVEL.items()
        },
    )
    data.setdefault("probationRuns", PROBATION_RUNS)
    data.setdefault("maxActivePerCategory", DEFAULT_MAX_ACTIVE_PER_CATEGORY)
    data.setdefault(
        "maxActivePerCategoryOverrides",
        dict(MAX_ACTIVE_PER_CATEGORY_OVERRIDES),
    )
    data.setdefault("cases", [])
    return data


def write_index(corpus_root: Path, index: dict[str, Any]) -> None:
    corpus_root.mkdir(parents=True, exist_ok=True)
    (corpus_root / "index.json").write_text(
        json.dumps(index, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def empty_history() -> dict[str, Any]:
    return {"version": HISTORY_VERSION, "cases": {}}


def load_history(path: Path | None = None) -> dict[str, Any]:
    path = path or history_path()
    if not path.exists():
        return empty_history()
    data = json.loads(path.read_text(encoding="utf-8"))
    data.setdefault("version", HISTORY_VERSION)
    data.setdefault("cases", {})
    return data


def write_history(history: dict[str, Any], path: Path | None = None) -> None:
    path = path or history_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(history, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def validate_expected(expected: str) -> str:
    if expected not in EXPECTED_TYPES:
        allowed = ", ".join(sorted(EXPECTED_TYPES))
        raise ValueError(f"expected scanner must be one of: {allowed}")
    return expected


def validate_category(category: str) -> str:
    if category not in EXPECTED_CATEGORIES:
        allowed = ", ".join(sorted(EXPECTED_CATEGORIES))
        raise ValueError(f"category must be one of: {allowed}")
    return category


def case_category(case: dict[str, Any]) -> str:
    category = str(case.get("category") or case.get("expected") or "")
    return category if category in EXPECTED_CATEGORIES else str(case.get("expected") or "")


def verification_level(metadata: dict[str, Any]) -> int:
    raw = metadata.get("verificationLevel")
    if raw is not None:
        try:
            level = int(raw)
        except (TypeError, ValueError):
            return -1
        return level if level in {1, 2, 3} else -1
    if metadata.get("expectedMatches"):
        return 3
    if metadata.get("expectedCounts") or metadata.get("expectedRuneshapeRuneCounts"):
        return 2
    return 1


def active_cases(index: dict[str, Any], corpus_root: Path) -> list[CorpusCase]:
    cases = []
    for case in index.get("cases", []):
        status = str(case.get("status") or "")
        expected = str(case.get("expected") or "")
        image = str(case.get("image") or "")
        if status not in ACTIVE_STATUSES or expected not in EXPECTED_TYPES or not image:
            continue
        cases.append(
            CorpusCase(
                id=str(case.get("id") or ""),
                expected=expected,
                image=corpus_root / image,
                status=status,
                metadata=case,
            )
        )
    return sorted(
        cases,
        key=lambda case: (
            case_category(case.metadata),
            verification_level(case.metadata),
            case.status,
            case.id,
        ),
    )


def outcomes_for(history: dict[str, Any], case_id: str) -> list[dict[str, Any]]:
    item = history.get("cases", {}).get(case_id, {})
    outcomes = item.get("outcomes") or []
    return list(outcomes[-RECENT_OUTCOMES:])


def record_outcome(
    history: dict[str, Any],
    *,
    case_id: str,
    expected: str,
    actual: str,
    elapsed_ms: float,
    passed: bool | None = None,
    reasons: list[str] | None = None,
    tiers: dict[str, bool] | None = None,
    recorded_at: str | None = None,
) -> None:
    case_history = history.setdefault("cases", {}).setdefault(case_id, {})
    outcomes = list(case_history.get("outcomes") or [])
    did_pass = actual == expected if passed is None else bool(passed)
    outcome = {
        "at": recorded_at or now_utc(),
        "expected": expected,
        "actual": actual,
        "passed": did_pass,
        "elapsedMs": round(float(elapsed_ms), 2),
    }
    if reasons:
        outcome["reasons"] = list(reasons)
    if tiers:
        outcome["tiers"] = dict(tiers)
    outcomes.append(
        outcome
    )
    case_history["outcomes"] = outcomes[-RECENT_OUTCOMES:]


def has_output_expectations(metadata: dict[str, Any]) -> bool:
    return verification_level(metadata) >= 2 or bool(
        metadata.get("expectedCounts")
        or metadata.get("expectedMatches")
        or metadata.get("expectedRuneshapeRuneCounts")
        or metadata.get("expectedRuneshapeRuneSequences")
    )


def evaluate_case_output(
    *,
    expected: str,
    metadata: dict[str, Any],
    actual: str,
    matches: list[dict[str, Any]],
) -> CorpusEvaluation:
    """Evaluate a scan case at its declared hierarchical verification level.

    Tier 1 is routing and is always checked. Tier 2 includes Tier 1 plus count
    coverage. Tier 3 includes Tier 1 and Tier 2 plus semantic identity. A higher
    tier cannot pass if any lower tier fails.
    Expected output is never inferred from current scanner output here; callers
    must provide reviewed expectations in the corpus metadata.
    """

    level = verification_level(metadata)
    tiers = {"routing": actual == expected}
    reasons = []
    if level == -1:
        tiers["schema"] = False
        reasons.append("schema: verificationLevel must be 1, 2, or 3")
    if actual != expected:
        reasons.append(f"routing: expected {expected}, actual {actual}")

    if level >= 2:
        count_reasons = _count_failures(metadata, matches, actual)
        tiers["count"] = not count_reasons
        reasons.extend(count_reasons)

    if level >= 3:
        semantic_reasons = _semantic_failures(metadata, matches, actual)
        tiers["semantic"] = not semantic_reasons
        reasons.extend(semantic_reasons)

    return CorpusEvaluation(
        passed=all(tiers.values()),
        tiers=tiers,
        reasons=reasons,
    )


def _expected_counts(metadata: dict[str, Any]) -> dict[str, int] | None:
    raw = metadata.get("expectedCounts")
    if not raw:
        return None
    if not isinstance(raw, dict):
        return {"__schema_error__": -1}
    counts = {}
    for key, value in raw.items():
        try:
            counts[str(key)] = int(value)
        except (TypeError, ValueError):
            counts[str(key)] = -1
    return counts


def _expected_rune_counts(metadata: dict[str, Any]) -> list[int] | None:
    raw = metadata.get("expectedRuneshapeRuneCounts")
    if not raw:
        return None
    if not isinstance(raw, list):
        return [-1]
    counts = []
    for item in raw:
        try:
            counts.append(int(item))
        except (TypeError, ValueError):
            counts.append(-1)
    return counts


def _expected_rune_sequences(metadata: dict[str, Any]) -> list[tuple[str, ...]] | None:
    raw = metadata.get("expectedRuneshapeRuneSequences")
    if not raw:
        return None
    if not isinstance(raw, list):
        return [("__schema_error__",)]
    sequences = []
    for sequence in raw:
        if not isinstance(sequence, list):
            return [("__schema_error__",)]
        sequences.append(tuple(str(rune) for rune in sequence))
    return sequences


def _expected_matches(metadata: dict[str, Any]) -> list[dict[str, Any]] | None:
    raw = metadata.get("expectedMatches")
    if not raw:
        return None
    if not isinstance(raw, list):
        return [{"__schema_error__": "expectedMatches must be a list"}]
    return [item if isinstance(item, dict) else {"__schema_error__": item} for item in raw]


def _match_kind(match: dict[str, Any], fallback: str) -> str:
    return str(match.get("scanKind") or fallback)


def _match_name(match: dict[str, Any]) -> str:
    return str(match.get("name") or "")


def _match_label(kind: str, name: str, stack: Any = None) -> str:
    label = f"{kind} {name}".strip()
    if stack is not None:
        label = f"{label} x{stack}"
    return label


def _counter_delta(
    expected: Counter[tuple[Any, ...]],
    actual: Counter[tuple[Any, ...]],
) -> tuple[Counter[tuple[Any, ...]], Counter[tuple[Any, ...]]]:
    return expected - actual, actual - expected


def _requires_runeshape_truth(metadata: dict[str, Any]) -> bool:
    category = case_category(metadata)
    if category in {"runeshape", "multi-rune"}:
        return True
    counts = _expected_counts(metadata) or {}
    if counts.get("runeshape", 0) > 0:
        return True
    expected_matches = _expected_matches(metadata) or []
    return any(str(item.get("scanKind") or "") == "runeshape" for item in expected_matches)


def _count_failures(
    metadata: dict[str, Any],
    matches: list[dict[str, Any]],
    actual_scanner: str,
) -> list[str]:
    reasons = []
    expected_counts = _expected_counts(metadata)
    compare_counts = expected_counts is not None
    if expected_counts is None:
        reasons.append("count: expectedCounts required for verificationLevel >= 2")
        expected_counts = {}
    if "__schema_error__" in expected_counts:
        reasons.append("count: expectedCounts must be an object of non-negative integers")
        expected_counts = {}
    if compare_counts:
        actual_counts = Counter(_match_kind(match, actual_scanner) for match in matches)
        for kind, expected_count in sorted(expected_counts.items()):
            if expected_count < 0:
                reasons.append(f"count: expected count for {kind} is not a non-negative integer")
                continue
            actual_count = actual_counts.get(kind, 0)
            if actual_count != expected_count:
                reasons.append(
                    f"count: {kind} expected {expected_count}, actual {actual_count}"
                )
        for kind, actual_count in sorted(actual_counts.items()):
            if kind not in expected_counts and actual_count:
                reasons.append(f"count: unexpected {kind} actual {actual_count}")
    if _requires_runeshape_truth(metadata):
        expected_rune_counts = _expected_rune_counts(metadata)
        if expected_rune_counts is None:
            reasons.append(
                "count: expectedRuneshapeRuneCounts required for runeshape verificationLevel >= 2"
            )
        elif any(item < 0 for item in expected_rune_counts):
            reasons.append("count: expectedRuneshapeRuneCounts must be a list of non-negative integers")
        else:
            actual_rune_counts = sorted(
                len(match.get("runeshapeRunes") or ())
                for match in matches
                if _match_kind(match, actual_scanner) == "runeshape"
            )
            expected_counter = Counter(expected_rune_counts)
            actual_counter = Counter(actual_rune_counts)
            missing, unexpected = _counter_delta(expected_counter, actual_counter)
            for count, amount in sorted(missing.items()):
                suffix = f" ({amount} rows)" if amount > 1 else ""
                reasons.append(f"count: missing runeshape row with {count} runes{suffix}")
            for count, amount in sorted(unexpected.items()):
                suffix = f" ({amount} rows)" if amount > 1 else ""
                reasons.append(f"count: unexpected runeshape row with {count} runes{suffix}")
    return reasons


def _semantic_failures(
    metadata: dict[str, Any],
    matches: list[dict[str, Any]],
    actual_scanner: str,
) -> list[str]:
    expected_matches = _expected_matches(metadata)
    if expected_matches is None:
        return ["semantic: expectedMatches required for verificationLevel >= 3"]
    if expected_matches and "__schema_error__" in expected_matches[0]:
        return ["semantic: expectedMatches must be a list of objects"]

    expected_names = Counter(
        (
            str(item.get("scanKind") or actual_scanner),
            str(item.get("name") or ""),
        )
        for item in expected_matches
    )
    actual_names = Counter(
        (_match_kind(match, actual_scanner), _match_name(match))
        for match in matches
    )
    missing, unexpected = _counter_delta(expected_names, actual_names)
    reasons = []
    for (kind, name), count in sorted(missing.items()):
        suffix = f" ({count} copies)" if count > 1 else ""
        reasons.append(f"semantic: missing {_match_label(kind, name)}{suffix}")
    for (kind, name), count in sorted(unexpected.items()):
        suffix = f" ({count} copies)" if count > 1 else ""
        reasons.append(f"semantic: unexpected {_match_label(kind, name)}{suffix}")

    stack_expected = [
        item for item in expected_matches
        if "stackSize" in item
    ]
    if stack_expected:
        expected_stacks = Counter(
            (
                str(item.get("scanKind") or actual_scanner),
                str(item.get("name") or ""),
                int(item.get("stackSize") or 1),
            )
            for item in stack_expected
        )
        actual_stacks = Counter(
            (
                _match_kind(match, actual_scanner),
                _match_name(match),
                int(match.get("stackSize") or 1),
            )
            for match in matches
        )
        missing_stacks, _unexpected_stacks = _counter_delta(
            expected_stacks,
            actual_stacks,
        )
        for (kind, name, stack), count in sorted(missing_stacks.items()):
            suffix = f" ({count} copies)" if count > 1 else ""
            reasons.append(
                f"semantic: missing stack {_match_label(kind, name, stack)}{suffix}"
            )
    if _requires_runeshape_truth(metadata):
        level_expected = [
            item for item in expected_matches
            if str(item.get("scanKind") or actual_scanner) == "runeshape"
            and item.get("runeshapeLevel")
        ]
        if level_expected:
            expected_levels = Counter(
                (
                    str(item.get("scanKind") or actual_scanner),
                    str(item.get("name") or ""),
                    str(item.get("runeshapeLevel") or ""),
                )
                for item in level_expected
            )
            actual_levels = Counter(
                (
                    _match_kind(match, actual_scanner),
                    _match_name(match),
                    str(match.get("runeshapeLevel") or ""),
                )
                for match in matches
                if _match_kind(match, actual_scanner) == "runeshape"
            )
            missing_levels, _unexpected_levels = _counter_delta(
                expected_levels,
                actual_levels,
            )
            for (kind, name, level), count in sorted(missing_levels.items()):
                suffix = f" ({count} copies)" if count > 1 else ""
                reasons.append(
                    f"semantic: missing runeshape level "
                    f"{_match_label(kind, name)} {level}{suffix}"
                )
        expected_sequences = _expected_rune_sequences(metadata)
        if expected_sequences is None:
            reasons.append(
                "semantic: expectedRuneshapeRuneSequences required for runeshape verificationLevel >= 3"
            )
        elif expected_sequences == [("__schema_error__",)]:
            reasons.append("semantic: expectedRuneshapeRuneSequences must be a list of rune-name lists")
        else:
            actual_sequences = [
                tuple(str(rune) for rune in match.get("runeshapeRunes") or ())
                for match in matches
                if _match_kind(match, actual_scanner) == "runeshape"
            ]
            missing_sequences, unexpected_sequences = _counter_delta(
                Counter(expected_sequences),
                Counter(actual_sequences),
            )
            for sequence, count in sorted(missing_sequences.items()):
                suffix = f" ({count} rows)" if count > 1 else ""
                reasons.append(
                    "semantic: missing runeshape sequence "
                    f"{','.join(sequence)}{suffix}"
                )
            for sequence, count in sorted(unexpected_sequences.items()):
                suffix = f" ({count} rows)" if count > 1 else ""
                reasons.append(
                    "semantic: unexpected runeshape sequence "
                    f"{','.join(sequence)}{suffix}"
                )
    return reasons


def graduate_probation(index: dict[str, Any], history: dict[str, Any]) -> list[str]:
    graduated = []
    required = int(index.get("probationRuns") or PROBATION_RUNS)
    for case in index.get("cases", []):
        if case.get("status") != "probation":
            continue
        outcomes = outcomes_for(history, str(case.get("id") or ""))
        if len(outcomes) < required:
            continue
        case["status"] = "graduated"
        case["graduatedUtc"] = now_utc()
        graduated.append(str(case.get("id") or ""))
    return graduated


def _resolution_key(case: dict[str, Any]) -> str:
    resolution = case.get("resolution") or {}
    width = int(resolution.get("width") or 0)
    height = int(resolution.get("height") or 0)
    if width <= 0 or height <= 0:
        return "unknown"
    return f"{width}x{height}"


def case_signature(case: dict[str, Any]) -> tuple[Any, ...]:
    return (
        case_category(case),
        verification_level(case),
        str(case.get("expected") or ""),
        tuple(sorted(str(tag) for tag in case.get("tags") or [])),
        _resolution_key(case),
    )


def _recent_failure_rate(case: dict[str, Any], history: dict[str, Any]) -> float:
    outcomes = outcomes_for(history, str(case.get("id") or ""))
    if not outcomes:
        return 0.0
    failures = sum(1 for outcome in outcomes if not outcome.get("passed"))
    return failures / len(outcomes)


def _retention_score(
    case: dict[str, Any],
    history: dict[str, Any],
    signature_counts: Counter[tuple[Any, ...]],
) -> float:
    score = _recent_failure_rate(case, history) * 100.0
    if case.get("observedAtAdmission") != case.get("expected"):
        score += 20.0
    if case.get("reason"):
        score += 5.0
    if signature_counts[case_signature(case)] == 1:
        score += 10.0
    return score


def retention_floors(index: dict[str, Any]) -> dict[int, int]:
    raw = index.get("retentionFloorsByLevel") or {}
    floors = dict(RETENTION_FLOORS_BY_LEVEL)
    for key, value in raw.items():
        try:
            level = int(key)
            count = int(value)
        except (TypeError, ValueError):
            continue
        if level in {1, 2, 3} and count >= 0:
            floors[level] = count
    return floors


def _positive_int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        count = int(value)
    except (TypeError, ValueError):
        return None
    return count if count > 0 else None


def max_active_for_category(index: dict[str, Any], category: str) -> int | None:
    default_limit = _positive_int_or_none(index.get("maxActivePerCategory"))
    overrides = index.get("maxActivePerCategoryOverrides") or {}
    limit = default_limit
    if isinstance(overrides, dict) and category in overrides:
        limit = _positive_int_or_none(overrides.get(category))
    if limit is None:
        return None
    return max(limit, sum(retention_floors(index).values()))


def retention_deficits(index: dict[str, Any]) -> dict[str, dict[int, int]]:
    floors = retention_floors(index)
    active = [
        case for case in index.get("cases", [])
        if case.get("status") in ACTIVE_STATUSES
        and str(case.get("expected") or "") in EXPECTED_TYPES
    ]
    categories = sorted({
        *EXPECTED_CATEGORIES,
        *(case_category(case) for case in active),
    })
    deficits: dict[str, dict[int, int]] = {}
    for category in categories:
        by_level = {
            level: sum(
                1 for case in active
                if case_category(case) == category
                and verification_level(case) == level
            )
            for level in floors
        }
        missing = {
            level: floors[level] - count
            for level, count in by_level.items()
            if count < floors[level]
        }
        if missing:
            deficits[category] = missing
    return deficits


def _protected_by_floor(
    case: dict[str, Any],
    active_cases_by_bucket: dict[tuple[str, int], list[dict[str, Any]]],
    floors: dict[int, int],
) -> bool:
    bucket = (case_category(case), verification_level(case))
    return len(active_cases_by_bucket.get(bucket, [])) <= floors.get(bucket[1], 0)


def enforce_graduated_limits(
    corpus_root: Path,
    index: dict[str, Any],
    history: dict[str, Any],
    *,
    delete_images: bool = True,
) -> list[str]:
    floors = retention_floors(index)
    archived = []
    now = now_utc()
    while True:
        active = [
            case
            for case in index.get("cases", [])
            if case.get("status") in ACTIVE_STATUSES
            and case.get("expected") in EXPECTED_TYPES
        ]
        active_by_bucket: dict[tuple[str, int], list[dict[str, Any]]] = {}
        active_by_category: dict[str, list[dict[str, Any]]] = {}
        for case in active:
            category = case_category(case)
            active_by_category.setdefault(category, []).append(case)
            active_by_bucket.setdefault((category, verification_level(case)), []).append(case)
        max_per_bucket = _positive_int_or_none(index.get("maxActivePerCategoryLevel"))
        overfull = {
            bucket: cases
            for bucket, cases in active_by_bucket.items()
            if max_per_bucket is not None
            and len(cases) > max_per_bucket
            and len(cases) > floors.get(bucket[1], 0)
        }
        category_overfull = {
            category: cases
            for category, cases in active_by_category.items()
            if (limit := max_active_for_category(index, category)) is not None
            and len(cases) > limit
            and len(cases) > sum(floors.values())
        }
        if overfull:
            bucket = sorted(overfull, key=lambda key: len(overfull[key]), reverse=True)[0]
            cases = overfull[bucket]
            archived_reason = "graduated level bucket over capacity"
        elif category_overfull:
            category = sorted(
                category_overfull,
                key=lambda key: len(category_overfull[key]),
                reverse=True,
            )[0]
            cases = category_overfull[category]
            archived_reason = "active category over capacity"
        else:
            return archived

        signature_counts = Counter(case_signature(case) for case in cases)
        candidates = [
            case for case in cases
            if not case.get("protected")
            and case.get("status") == "graduated"
            and not _protected_by_floor(case, active_by_bucket, floors)
        ]
        if not candidates:
            return archived

        evicted = sorted(
            candidates,
            key=lambda case: (
                _retention_score(case, history, signature_counts),
                str(case.get("addedUtc") or ""),
                str(case.get("id") or ""),
            ),
        )[0]
        evicted["status"] = "archived"
        evicted["archivedUtc"] = now
        evicted["archivedReason"] = archived_reason
        archived.append(str(evicted.get("id") or ""))
        image = str(evicted.get("image") or "")
        if delete_images and image:
            try:
                (corpus_root / image).unlink()
            except FileNotFoundError:
                pass


def remove_superseded_source_cases(
    corpus_root: Path,
    index: dict[str, Any],
    *,
    source_scan_id: str,
    new_level: int,
    delete_images: bool = True,
) -> list[str]:
    if new_level not in {1, 2, 3}:
        raise ValueError("new_level must be 1, 2, or 3")
    cases = list(index.get("cases") or [])
    same_source = [
        case for case in cases
        if str(case.get("sourceScanId") or "") == source_scan_id
    ]
    higher = [
        str(case.get("id") or "")
        for case in same_source
        if verification_level(case) > new_level
        and case.get("status") in ACTIVE_STATUSES
    ]
    if higher:
        joined = ", ".join(sorted(higher))
        raise ValueError(
            f"refusing to replace higher-level case(s) for {source_scan_id}: {joined}"
        )

    removed = [
        case for case in same_source
        if verification_level(case) <= new_level
    ]
    if not removed:
        return []

    removed_ids = {id(case) for case in removed}
    index["cases"] = [case for case in cases if id(case) not in removed_ids]
    removed_case_ids = []
    if delete_images:
        for case in removed:
            image = str(case.get("image") or "")
            if image:
                try:
                    (corpus_root / image).unlink()
                except FileNotFoundError:
                    pass
    for case in removed:
        removed_case_ids.append(str(case.get("id") or ""))
    return removed_case_ids


def unique_case_id(
    index: dict[str, Any],
    expected: str,
    source_scan_id: str,
    *,
    category: str | None = None,
) -> str:
    prefix = category or expected
    base = f"{prefix}-{source_scan_id.removeprefix('scan-')}"
    used = {str(case.get("id") or "") for case in index.get("cases", [])}
    if base not in used:
        return base
    suffix = 2
    while f"{base}-{suffix}" in used:
        suffix += 1
    return f"{base}-{suffix}"
