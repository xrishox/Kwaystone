from __future__ import annotations

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from conftest import local_debug_tests_enabled
from poed import runeshape, scan_corpus
from poed.scanners.runeshape import RuneshapeScanner
from poed.scanners.types import ScanContext


TARGET_RUNES = [
    "RemnantRuneRebirth",
    "RemnantRuneArcane",
    "RemnantRuneElectrocuting",
    "RemnantRuneAdaptive",
    "RemnantRareRuneBond",
]

OLROTH_RUNES = [
    "RemnantRuneTempest",
    "RemnantRuneVision",
    "RemnantRareRuneTime",
    "RemnantRuneCelestial",
    "RemnantRuneWisdom",
    "RemnantRareRunePower",
]

GREATER_REGAL_RUNES = [
    "RemnantRareRuneOath",
    "RemnantRuneCyclonic",
    "RemnantRuneMomentum",
    "RemnantRuneMoon",
    "RemnantRuneOpulent",
    "RemnantRuneBloodletting",
]


def _load_rune(name: str) -> np.ndarray:
    rel = runeshape._data()["runes"][name]
    data = runeshape._data_root().joinpath(rel).read_bytes()
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), cv2.IMREAD_UNCHANGED)
    assert img is not None
    return img


def _composite_rgba(dst: np.ndarray, rgba: np.ndarray, x: int, y: int) -> None:
    h, w = rgba.shape[:2]
    x0 = max(0, x)
    y0 = max(0, y)
    x1 = min(dst.shape[1], x + w)
    y1 = min(dst.shape[0], y + h)
    if x0 >= x1 or y0 >= y1:
        return
    src = rgba[y0 - y:y1 - y, x0 - x:x1 - x]
    alpha = (src[:, :, 3:4].astype(np.float32) / 255.0)
    dst[y0:y1, x0:x1] = (
        src[:, :, :3].astype(np.float32) * alpha
        + dst[y0:y1, x0:x1].astype(np.float32) * (1.0 - alpha)
    ).astype(np.uint8)


def _selected_rune_icon(icon: np.ndarray, *, warm_gold: bool = False) -> np.ndarray:
    out = icon.copy()
    if out.shape[2] < 4:
        return out
    alpha = out[:, :, 3] > 0
    gray = cv2.cvtColor(out[:, :, :3], cv2.COLOR_BGR2GRAY).astype(np.float32)
    if warm_gold:
        value = np.clip(gray * 0.55 + 115.0, 0, 255).astype(np.uint8)
        selected = cv2.merge([
            np.clip(value * 0.18, 0, 255).astype(np.uint8),
            np.clip(value * 0.62, 0, 255).astype(np.uint8),
            value,
        ])
    else:
        bright = np.clip(gray * 0.55 + 140.0, 0, 255).astype(np.uint8)
        selected = cv2.merge([bright, bright, bright])
    bgr = out[:, :, :3]
    bgr[alpha] = selected[alpha]
    out[:, :, :3] = bgr
    return out


def _draw_distractor(canvas: np.ndarray, cx: int, cy: int, spacing: int) -> None:
    cv2.circle(canvas, (cx, cy), int(spacing * 0.40), (5, 5, 7), -1)
    cv2.circle(canvas, (cx, cy), int(spacing * 0.43), (60, 90, 120), 3)
    cv2.line(
        canvas,
        (cx - int(spacing * 0.16), cy - int(spacing * 0.22)),
        (cx + int(spacing * 0.18), cy + int(spacing * 0.24)),
        (170, 190, 235),
        max(2, int(spacing * 0.05)),
    )
    cv2.line(
        canvas,
        (cx + int(spacing * 0.16), cy - int(spacing * 0.22)),
        (cx - int(spacing * 0.18), cy + int(spacing * 0.24)),
        (170, 190, 235),
        max(2, int(spacing * 0.05)),
    )


def _synthetic_strip(
    *,
    size: tuple[int, int] = (1200, 1800),
    canvas: np.ndarray | None = None,
    origin: tuple[int, int] = (420, 260),
    spacing: int = 112,
    icon_size: int = 76,
    runes: list[str] | None = None,
    selected_index: int | None = None,
    selected_warm_gold: bool = False,
    left_distractors: int = 0,
    wide_gap_after: int | None = None,
) -> np.ndarray:
    runes = runes or TARGET_RUNES
    if canvas is None:
        h, w = size
        rng = np.random.default_rng(1234)
        canvas = rng.integers(16, 48, (h, w, 3), dtype=np.uint8)
    x0, y0 = origin
    bar_h = int(spacing * 0.82)
    bar_left = x0 - int(spacing * (left_distractors + 0.70))
    bar_w = spacing * (len(runes) + left_distractors - 1) + int(spacing * 1.45)
    cv2.rectangle(
        canvas,
        (bar_left, y0 - bar_h // 2),
        (bar_left + bar_w, y0 + bar_h // 2),
        (12, 12, 14),
        -1,
    )
    for index in range(left_distractors):
        cx = x0 - spacing * (left_distractors - index)
        _draw_distractor(canvas, cx, y0, spacing)
    for index, rune in enumerate(runes):
        extra_gap = spacing * 3 if wide_gap_after is not None and index > wide_gap_after else 0
        cx = x0 + index * spacing + extra_gap
        selected = index == selected_index
        color = (
            (90, 90, 94)
            if selected
            else (75, 72, 110)
            if "Rare" in rune
            else (60, 90, 120)
        )
        border = (45, 145, 210) if selected else color
        cv2.circle(canvas, (cx, y0), int(spacing * 0.40), (5, 5, 7), -1)
        cv2.circle(canvas, (cx, y0), int(spacing * 0.43), border, 3)
        icon = cv2.resize(
            _load_rune(rune),
            (icon_size, icon_size),
            interpolation=cv2.INTER_CUBIC,
        )
        if selected:
            icon = _selected_rune_icon(icon, warm_gold=selected_warm_gold)
        _composite_rgba(canvas, icon, cx - icon_size // 2, y0 - icon_size // 2)
    return canvas


def _names(detections):
    return [str(detection.entry.get("name") or "") for detection in detections]


@pytest.mark.parametrize(
    ("origin", "spacing", "icon_size"),
    [
        ((250, 180), 84, 58),
        ((720, 540), 112, 76),
        ((1080, 810), 138, 94),
    ],
)
def test_detects_synthetic_runeshape_strip_at_different_positions(origin, spacing, icon_size):
    image = _synthetic_strip(origin=origin, spacing=spacing, icon_size=icon_size)

    detected = runeshape.detect(image)

    assert detected is not None
    assert detected.entry["name"] == "Greater Orb of Augmentation"
    assert detected.entry["stackSize"] == 3
    assert detected.entry["level"] == "Lv70+"
    assert detected.runes == tuple(TARGET_RUNES)
    assert detected.confidence >= 0.99


def test_detects_selected_grey_rune_with_noisy_prefix():
    image = _synthetic_strip(selected_index=2, left_distractors=2)

    detected = runeshape.detect(image)

    assert detected is not None
    assert detected.entry["name"] == "Greater Orb of Augmentation"
    assert detected.entry["stackSize"] == 3
    assert detected.entry["level"] == "Lv70+"
    assert detected.runes == tuple(TARGET_RUNES)


def test_detects_selected_warm_gold_rune():
    image = _synthetic_strip(
        runes=GREATER_REGAL_RUNES,
        selected_index=4,
        selected_warm_gold=True,
    )

    detected = runeshape.detect(image)

    assert detected is not None
    assert detected.entry["name"] == "Greater Regal Orb"
    assert detected.entry["stackSize"] == 3
    assert detected.entry["level"] == "Lv70+"
    assert detected.runes == tuple(GREATER_REGAL_RUNES)


def test_large_gap_is_not_filled_with_multiple_inferred_slots():
    image = _synthetic_strip(wide_gap_after=1)

    detected = runeshape.detect(image)

    assert detected is None


def test_candidate_rows_merge_fragmented_selected_slot():
    row = [
        runeshape.RuneCandidate(x=float(x), y=20.0, w=42.0, h=42.0, area=1000.0)
        for x in (100, 156, 212, 268, 324)
    ]
    row.extend([
        runeshape.RuneCandidate(x=370.0, y=15.0, w=18.0, h=18.0, area=120.0),
        runeshape.RuneCandidate(x=392.0, y=21.0, w=28.0, h=42.0, area=620.0),
        runeshape.RuneCandidate(x=436.0, y=20.0, w=42.0, h=42.0, area=1000.0),
    ])

    variants = runeshape._candidate_rows(row, image_width=1200)
    sequences = [
        sequence
        for variant in variants
        for sequence, _gap, _size in runeshape._regular_sequences(variant)
        if len(sequence) == 7
    ]

    assert sequences
    assert [round(candidate.x) for candidate in sequences[0]] == [
        100,
        156,
        212,
        268,
        324,
        388,
        436,
    ]


def test_slot_scoring_handles_small_glyph_inside_large_button():
    canvas = np.zeros((102, 102, 3), dtype=np.uint8)
    icon = cv2.resize(
        _load_rune("RemnantRuneWisdom"),
        (38, 38),
        interpolation=cv2.INTER_CUBIC,
    )
    _composite_rgba(canvas, icon, 32, 32)
    masked = runeshape._masked_value(canvas)

    scores = runeshape._score_slot_templates((masked,), 51.0, 51.0, 102)

    assert scores["RemnantRuneWisdom"] > 0.70
    assert scores["RemnantRuneWisdom"] > scores["RemnantRuneEnrage"]


def test_contextual_table_beam_recovers_valid_sequence_when_top_slot_is_wrong(monkeypatch):
    target = tuple(TARGET_RUNES)
    sequence = [
        runeshape.RuneCandidate(x=float(index * 50), y=20.0, w=20.0, h=20.0, area=1.0)
        for index in range(len(target))
    ]
    valid_prefixes = {
        target[:prefix_len]
        for prefix_len in range(1, len(target) + 1)
    }
    by_sequence = {target: ({"name": "Expected"},)}

    def fake_options(masked_values, center_x, center_y, slot_size):
        index = int(round(center_x / 50.0))
        correct = target[index]
        wrong = "RemnantRareRuneWard"
        if wrong == correct:
            wrong = "RemnantRareRunePower"
        if index == 2:
            return (
                runeshape.RuneClassification(wrong, 0.75, 0.02, center_x, center_y, slot_size),
                runeshape.RuneClassification(correct, 0.73, -0.02, center_x, center_y, slot_size),
            )
        return (
            runeshape.RuneClassification(correct, 0.82, 0.05, center_x, center_y, slot_size),
            runeshape.RuneClassification(wrong, 0.70, -0.12, center_x, center_y, slot_size),
        )

    monkeypatch.setattr(runeshape, "_classify_slot_options", fake_options)

    classifications = runeshape._contextual_classifications(
        sequence,
        20.0,
        80,
        (np.zeros((120, 300), dtype=np.uint8),),
        valid_prefixes,
        by_sequence,
        {},
    )

    assert classifications is not None
    assert tuple(item.rune for item in classifications) == target


def test_contextual_table_beam_allows_one_weak_exact_slot(monkeypatch):
    target = tuple(TARGET_RUNES)
    sequence = [
        runeshape.RuneCandidate(x=float(index * 50), y=20.0, w=20.0, h=20.0, area=1.0)
        for index in range(len(target))
    ]
    valid_prefixes = {
        target[:prefix_len]
        for prefix_len in range(1, len(target) + 1)
    }
    by_sequence = {target: ({"name": "Expected"},)}

    def fake_options(masked_values, center_x, center_y, slot_size):
        index = int(round(center_x / 50.0))
        correct = target[index]
        wrong = "RemnantRareRuneWard"
        if wrong == correct:
            wrong = "RemnantRareRunePower"
        if index == 3:
            return (
                runeshape.RuneClassification(wrong, 0.66, 0.01, center_x, center_y, slot_size),
                runeshape.RuneClassification(correct, 0.64, -0.02, center_x, center_y, slot_size),
            )
        return (
            runeshape.RuneClassification(correct, 0.86, 0.08, center_x, center_y, slot_size),
            runeshape.RuneClassification(wrong, 0.72, -0.14, center_x, center_y, slot_size),
        )

    monkeypatch.setattr(runeshape, "_classify_slot_options", fake_options)

    classifications = runeshape._contextual_classifications(
        sequence,
        20.0,
        80,
        (np.zeros((120, 300), dtype=np.uint8),),
        valid_prefixes,
        by_sequence,
        {},
    )

    assert classifications is not None
    assert tuple(item.rune for item in classifications) == target


def test_contextual_table_beam_rejects_multiple_weak_slots(monkeypatch):
    target = tuple(TARGET_RUNES)
    sequence = [
        runeshape.RuneCandidate(x=float(index * 50), y=20.0, w=20.0, h=20.0, area=1.0)
        for index in range(len(target))
    ]
    valid_prefixes = {
        target[:prefix_len]
        for prefix_len in range(1, len(target) + 1)
    }
    by_sequence = {target: ({"name": "Expected"},)}

    def fake_options(masked_values, center_x, center_y, slot_size):
        index = int(round(center_x / 50.0))
        correct = target[index]
        if index in {1, 3}:
            return (
                runeshape.RuneClassification(correct, 0.64, 0.04, center_x, center_y, slot_size),
            )
        return (
            runeshape.RuneClassification(correct, 0.86, 0.08, center_x, center_y, slot_size),
        )

    monkeypatch.setattr(runeshape, "_classify_slot_options", fake_options)

    classifications = runeshape._contextual_classifications(
        sequence,
        20.0,
        80,
        (np.zeros((120, 300), dtype=np.uint8),),
        valid_prefixes,
        by_sequence,
        {},
    )

    assert classifications is None


def test_detect_all_returns_multiple_runeshape_rows():
    image = _synthetic_strip(size=(1300, 2200), origin=(320, 260))
    _synthetic_strip(
        canvas=image,
        origin=(420, 760),
        runes=GREATER_REGAL_RUNES,
    )

    detections = runeshape.detect_all(image)

    assert _names(detections) == [
        "Greater Orb of Augmentation",
        "Greater Regal Orb",
    ]
    assert [len(detection.runes) for detection in detections] == [5, 6]


def test_detect_all_keeps_same_band_separate_rows():
    image = _synthetic_strip(size=(900, 2300), origin=(260, 360), spacing=86, icon_size=58)
    _synthetic_strip(
        canvas=image,
        origin=(1320, 360),
        spacing=86,
        icon_size=58,
        runes=GREATER_REGAL_RUNES,
    )

    detections = runeshape.detect_all(image)

    assert _names(detections) == [
        "Greater Orb of Augmentation",
        "Greater Regal Orb",
    ]


def test_detect_all_deduplicates_overlapping_candidates():
    image = _synthetic_strip()

    detections = runeshape.detect_all(image)

    assert len(detections) == 1
    assert detections[0].entry["name"] == "Greater Orb of Augmentation"


def test_runeshape_scanner_returns_priced_match():
    image = _synthetic_strip()
    ctx = ScanContext(
        cfg={"league": "L"},
        output="fixture",
        shot=image,
        frame=image,
        frame_x=0,
        frame_y=0,
        source="fixture",
        rows={
            "Greater Orb of Augmentation": {
                "price": 2.0,
                "priceAvailable": True,
                "kind": "tagged",
                "quantity": 50,
                "exaltedPerChaos": 0.25,
                "exaltedPerDivine": 333,
            }
        },
    )
    scanner = RuneshapeScanner()
    detection = scanner.probe(ctx, None)

    assert detection is not None
    result = scanner.scan(ctx, detection)

    assert result.scanner_id == "runeshape"
    assert result.matches[0]["name"] == "Greater Orb of Augmentation"
    assert result.matches[0]["stackSize"] == 3
    assert result.matches[0]["totalPrice"] == 6.0
    assert result.matches[0]["priceAvailable"] is True
    assert result.matches[0]["exaltedPerChaos"] == 0.25
    assert result.matches[0]["exaltedPerDivine"] == 333


def test_runeshape_scanner_returns_multiple_priced_matches():
    image = _synthetic_strip(size=(1300, 2200), origin=(320, 260))
    _synthetic_strip(
        canvas=image,
        origin=(420, 760),
        runes=GREATER_REGAL_RUNES,
    )
    ctx = ScanContext(
        cfg={"league": "L"},
        output="fixture",
        shot=image,
        frame=image,
        frame_x=0,
        frame_y=0,
        source="fixture",
        rows={
            "Greater Orb of Augmentation": {
                "price": 2.0,
                "priceAvailable": True,
                "kind": "tagged",
                "quantity": 50,
            },
            "Greater Regal Orb": {
                "price": 3.0,
                "priceAvailable": True,
                "kind": "tagged",
                "quantity": 50,
            },
        },
    )
    scanner = RuneshapeScanner()
    detection = scanner.probe(ctx, None)

    assert detection is not None
    result = scanner.scan(ctx, detection)

    assert result.scanner_id == "runeshape"
    assert [match["name"] for match in result.matches] == [
        "Greater Orb of Augmentation",
        "Greater Regal Orb",
    ]
    assert [match["stackSize"] for match in result.matches] == [3, 3]
    assert [match["totalPrice"] for match in result.matches] == [6.0, 9.0]


def test_latest_local_debug_capture_if_available():
    if not local_debug_tests_enabled():
        pytest.skip("local debug capture tests require WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1")
    path = (
        scan_corpus.debug_scan_root()
        / "scan-20260627T034947-595545893"
        / "00-capture.png"
    )
    if not path.exists():
        pytest.skip(f"missing local debug capture {path}")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"could not read local debug capture {path}")

    detected = runeshape.detect(image)

    assert detected is not None
    assert detected.entry["name"] == "Greater Orb of Augmentation"
    assert detected.entry["stackSize"] == 3
    assert detected.runes == tuple(TARGET_RUNES)


def test_latest_multi_rune_local_debug_capture_if_available():
    if not local_debug_tests_enabled():
        pytest.skip("local debug capture tests require WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1")
    path = (
        scan_corpus.debug_scan_root()
        / "scan-20260627T161506-691690564"
        / "01-game-frame.png"
    )
    if not path.exists():
        pytest.skip(f"missing local debug capture {path}")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"could not read local debug capture {path}")

    detections = runeshape.detect_all(image)

    assert len(detections) >= 2
    assert [len(detection.runes) for detection in detections[:2]] == [3, 6]


@pytest.mark.parametrize(
    ("scan", "expected_name", "expected_stack", "expected_runes"),
    [
        (
            "scan-20260627T044531-710904802",
            "Olroth's Saga",
            1,
            OLROTH_RUNES,
        ),
        (
            "scan-20260627T044538-665578489",
            "Olroth's Saga",
            1,
            OLROTH_RUNES,
        ),
        (
            "scan-20260627T050431-518107408",
            "Greater Regal Orb",
            3,
            GREATER_REGAL_RUNES,
        ),
        (
            "scan-20260627T050436-886665655",
            "Greater Regal Orb",
            3,
            GREATER_REGAL_RUNES,
        ),
        (
            "scan-20260627T124352-408990244",
            "Greater Regal Orb",
            3,
            GREATER_REGAL_RUNES,
        ),
    ],
)
def test_selected_local_debug_captures_if_available(
    scan: str,
    expected_name: str,
    expected_stack: int,
    expected_runes: list[str],
):
    if not local_debug_tests_enabled():
        pytest.skip("local debug capture tests require WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1")
    path = scan_corpus.debug_scan_root() / scan / "00-capture.png"
    if not path.exists():
        pytest.skip(f"missing local debug capture {path}")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"could not read local debug capture {path}")

    detected = runeshape.detect(image)

    assert detected is not None
    assert detected.entry["name"] == expected_name
    assert detected.entry["stackSize"] == expected_stack
    assert detected.entry["level"] == "Lv70+"
    assert detected.runes == tuple(expected_runes)
