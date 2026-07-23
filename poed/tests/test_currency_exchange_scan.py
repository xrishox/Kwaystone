import cv2
import numpy as np
import pytest

from poed import currency_exchange_scan as scan


def _exchange_frame(width=1600, height=900, panel_x=180, panel_y=80, panel_w=760):
    frame = np.zeros((height, width, 3), dtype=np.uint8)
    gold = (40, 150, 210)
    middle_w = int(panel_w * 0.185)
    band_h = max(10, int(panel_w * 0.022))
    middle_cx = panel_x + panel_w * 0.5
    band_y = int(panel_y + panel_w * 0.073)
    left_cx = panel_x + panel_w * 0.16
    right_cx = panel_x + panel_w * 0.83
    cv2.rectangle(
        frame,
        (int(middle_cx - middle_w / 2), band_y),
        (int(middle_cx + middle_w / 2), band_y + band_h),
        gold,
        -1,
    )
    side_y = band_y + int(band_h * 1.1)
    cv2.rectangle(
        frame,
        (int(left_cx - panel_w * 0.055), side_y),
        (int(left_cx + panel_w * 0.055), side_y + band_h),
        gold,
        -1,
    )
    cv2.rectangle(
        frame,
        (int(right_cx - panel_w * 0.045), side_y),
        (int(right_cx + panel_w * 0.045), side_y + band_h),
        gold,
        -1,
    )
    return frame


@pytest.mark.parametrize(
    ("width", "height", "panel_x", "panel_y", "panel_w"),
    [
        (1280, 720, 80, 70, 620),
        (1920, 1080, 260, 100, 900),
        (2560, 1080, 1100, 50, 900),
        (3840, 2160, 400, 220, 1700),
    ],
)
def test_localizes_scaled_and_moved_exchange(width, height, panel_x, panel_y, panel_w):
    panel = scan._exchange_panel(_exchange_frame(width, height, panel_x, panel_y, panel_w))
    assert panel is not None
    assert abs(panel.x - panel_x) < panel_w * 0.06
    assert abs(panel.y - panel_y) < panel_w * 0.06
    assert abs(panel.w - panel_w) < panel_w * 0.08


def test_read_frame_batches_verified_header(monkeypatch):
    frame = _exchange_frame()
    calls = []

    def reads(crops):
        calls.append(crops)
        return ["CURRENCY EXCHANGE", "Chaos Orb", "Omen of Whittling", "81:1"]

    monkeypatch.setattr(scan, "_cached_reads", reads)
    result = scan.read_frame(frame)

    assert len(calls) == 1
    assert len(calls[0]) == 4
    assert result.want_text == "Chaos Orb"
    assert result.have_text == "Omen of Whittling"
    assert result.want_amount == 81
    assert result.have_amount == 1
    assert result.panel_side == "right"


def test_read_frame_recovers_from_higher_ranked_false_panel(monkeypatch):
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    false_panel = scan.Rect(300, 40, 620, 167)
    exchange_panel = scan.Rect(180, 80, 760, 205)
    monkeypatch.setattr(
        scan,
        "_exchange_panels",
        lambda _frame: [false_panel, exchange_panel],
    )
    calls = []

    def reads(crops):
        calls.append(crops)
        if len(calls) == 1:
            return ["JLOCKER", "C", "NCT EXCHHAN", "NA"]
        if len(calls) == 2:
            return ["CURRENCY EXCHANGE"]
        return ["Chaos Orb", "Omen of Whittling", "81:1"]

    monkeypatch.setattr(scan, "_cached_reads", reads)
    result = scan.read_frame(frame)

    assert [len(crops) for crops in calls] == [4, 1, 3]
    assert result.want_text == "Chaos Orb"
    assert result.have_text == "Omen of Whittling"
    assert (result.want_amount, result.have_amount) == (81, 1)


def test_foreign_frame_rejects_before_ocr(monkeypatch):
    monkeypatch.setattr(
        scan.ocr_worker,
        "recognize_arrays",
        lambda *_args, **_kwargs: pytest.fail("negative frame should not invoke OCR"),
    )
    with pytest.raises(RuntimeError, match="not visible"):
        scan.read_frame(np.zeros((900, 1600, 3), dtype=np.uint8))


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("81:1", (81, 1)),
        ("1.25 : 3", (1.25, 3)),
        ("8O:1", (80, 1)),
        ("8 1；I", (81, 1)),
        ("81|1", (81, 1)),
    ],
)
def test_ratio_parser(raw, expected):
    assert scan._parse_ratio(raw) == expected


def test_ratio_parser_rejects_missing_or_zero_values():
    with pytest.raises(RuntimeError):
        scan._parse_ratio("retrieving info")
    with pytest.raises(RuntimeError):
        scan._parse_ratio("0:1")


def test_read_frame_falls_back_to_specialized_ratio_batch(monkeypatch):
    frame = _exchange_frame()
    calls = []

    def reads(crops):
        calls.append(crops)
        if len(calls) == 1:
            return ["CURRENCY EXCHANGE", "Chaos Orb", "Omen of Whittling", ""]
        return ["", "81;1", ""]

    monkeypatch.setattr(scan, "_cached_reads", reads)
    result = scan.read_frame(frame)

    assert [len(crops) for crops in calls] == [4, 3]
    assert (result.want_amount, result.have_amount) == (81, 1)


def test_read_frame_does_not_pay_fallback_cost_when_primary_ratio_works(monkeypatch):
    frame = _exchange_frame()
    calls = []

    def reads(crops):
        calls.append(crops)
        return ["CURRENCY EXCHANGE", "Chaos Orb", "Omen of Whittling", "81:1"]

    monkeypatch.setattr(scan, "_cached_reads", reads)
    scan.read_frame(frame)

    assert len(calls) == 1


def test_live_read_requires_two_agreeing_ratio_renderings(monkeypatch):
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    panel = scan.Rect(180, 80, 760, 205)
    monkeypatch.setattr(scan, "_exchange_panels", lambda _frame: [panel])
    calls = 0

    def recognitions(_crops):
        nonlocal calls
        calls += 1
        if calls == 1:
            return [("CURRENCY EXCHANGE", 0.99)]
        return [
            ("Chaos Orb", 0.96),
            ("Omen of Whittling", 0.95),
            ("81:1", 0.94),
            ("81;1", 0.93),
        ]

    monkeypatch.setattr(scan, "_cached_recognitions", recognitions)
    result = scan.read_live_frame(frame)

    assert (result.want_amount, result.have_amount) == (81, 1)
    assert result.ratio_score == pytest.approx(0.93)


def test_live_read_rejects_disagreeing_ratio_renderings(monkeypatch):
    frame = np.zeros((900, 1600, 3), dtype=np.uint8)
    panel = scan.Rect(180, 80, 760, 205)
    monkeypatch.setattr(scan, "_exchange_panels", lambda _frame: [panel])
    responses = iter(
        [
            [("CURRENCY EXCHANGE", 0.99)],
            [
                ("Chaos Orb", 0.96),
                ("Omen of Whittling", 0.95),
                ("81:1", 0.94),
                ("18:1", 0.93),
            ],
        ]
    )
    monkeypatch.setattr(scan, "_cached_recognitions", lambda _crops: next(responses))

    with pytest.raises(RuntimeError, match="disagree"):
        scan.read_live_frame(frame)


def test_visual_distance_is_stable_and_detects_changed_fields():
    black = bytes(96 * 24)
    white = bytes([255]) * (96 * 24)
    assert scan.visual_distance(black, black) == 0
    assert scan.visual_distance(black, white) == 1
