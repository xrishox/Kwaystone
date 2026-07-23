import numpy as np

from poed.image_geometry import Rect
from poed.image_geometry import frame_source
from poed import expeditionscan


def test_expedition_crop_uses_left_vendor_panel():
    shot = np.zeros((2000, 3800, 3), dtype=np.uint8)

    crop, x0, y0 = expeditionscan.expedition_crop(shot)

    assert x0 == 316
    assert y0 == 238
    assert crop.shape == (1202, 764, 3)


def test_expedition_crop_offsets_from_game_rect():
    shot = np.zeros((1200, 2000, 3), dtype=np.uint8)

    frame, fx0, fy0, source = frame_source(shot, Rect(200, 100, 1000, 800))
    crop, x0, y0 = expeditionscan.expedition_crop(frame)

    assert source == "game"
    assert (fx0 + x0, fy0 + y0) == (326, 195)
    assert crop.shape == (481, 306, 3)


def test_expedition_crop_covers_lower_reward_rows_at_arbitrary_sizes():
    for height, width in ((720, 1280), (1440, 2560), (2160, 3840)):
        shot = np.zeros((height, width, 3), dtype=np.uint8)
        row_y = int(height * 0.68)
        row_x = int(height * 0.30)
        shot[row_y, row_x] = (17, 31, 47)

        crop, x0, y0 = expeditionscan.expedition_crop(shot)

        assert tuple(crop[row_y - y0, row_x - x0]) == (17, 31, 47)
        assert crop.shape[0] == int(height * 0.72) - int(height * 0.119)


def test_match_name_strips_stack_prefix_and_fuzzes_ocr_noise():
    rows = {
        "Ancient Rune of Witchcraft": {"price": 12.0, "kind": "tagged"},
        "Rune of Reach": {"price": 1.0, "kind": "tagged"},
    }

    hit = expeditionscan._match_name("1x Ancient Rune of Witcheraft", rows)

    assert hit is not None
    assert hit[0] == "Ancient Rune of Witchcraft"


def test_expedition_panel_title_tolerates_ocr_noise():
    lines = [
        expeditionscan.OcrLine(
            "Runesnape Combinalions",
            0.93,
            (0, 0, 200, 30),
        )
    ]

    assert expeditionscan._expedition_panel_present(lines)


def test_match_name_prefers_longer_exact_name():
    rows = {
        "Chaos Orb": {"price": 15.0, "kind": "tagged"},
        "Greater Chaos Orb": {"price": 45.0, "kind": "tagged"},
    }

    hit = expeditionscan._match_name("Greater Chaos Orb", rows)

    assert hit is not None
    assert hit[0] == "Greater Chaos Orb"


def test_match_name_does_not_let_short_currency_hijack_long_item_name():
    rows = {
        "Verisium": {"price": 1.0},
        "Verisium Manifestations": {"price": 0, "priceAvailable": False},
    }

    hit = expeditionscan._match_name("Skill: Verisium Manifestations", rows)

    assert hit is not None
    assert hit[0] == "Verisium Manifestations"


def test_match_name_rejects_short_substring_when_full_item_is_unknown():
    rows = {"Verisium": {"price": 1.0}}

    assert expeditionscan._match_name("Verisium Pile", rows) is None


def test_match_name_strips_have_stack_prefix_before_matching_short_name():
    rows = {
        "Verisium": {"price": 1.0},
        "Exceptional Verisium": {"price": 4.0},
    }

    hit = expeditionscan._match_name("32.2K Verisium", rows, floor=0.82)

    assert hit is not None
    assert hit[0] == "Verisium"


def test_stack_size_parses_ocr_quantity_prefix():
    assert expeditionscan._stack_size("2x Rune of Reach") == 2
    assert expeditionscan._stack_size("12X Lesser Ward Rune") == 12
    assert expeditionscan._stack_size("32.2K Verisium") == 32200
    assert expeditionscan._stack_size("20 Exceptional Verisium") == 1
    assert expeditionscan._stack_size("Rune of Reach") == 1


def test_scan_image_carries_universal_market_quote_metadata():
    crop = np.zeros((800, 1200, 3), dtype=np.uint8)
    lines = [
        expeditionscan.OcrLine(
            "Greater Iron Rune",
            1.0,
            (100, 100, 320, 140),
        )
    ]
    rows = {
        "Greater Iron Rune": {
            "price": 7.1,
            "quoteAmount": 1 / 3,
            "quoteCurrency": "chaos",
            "quoteCurrencyText": "Chaos Orb",
            "quoteLiquidity": 21.2,
            "quoteMaxStock": 20,
            "exaltedPerChaos": 0.25,
            "exaltedPerDivine": 333,
        }
    }

    matches, _grouped = expeditionscan.scan_image(crop, rows, raw_lines=lines)

    assert matches[0]["quoteAmount"] == 1 / 3
    assert matches[0]["quoteCurrency"] == "chaos"
    assert matches[0]["quoteMaxStock"] == 20
    assert matches[0]["exaltedPerChaos"] == 0.25
    assert matches[0]["exaltedPerDivine"] == 333


def test_scan_image_keeps_unknown_visible_item_as_unpriced():
    crop = np.full((800, 1200, 3), 180, dtype=np.uint8)
    lines = [
        expeditionscan.OcrLine(
            "Verisium Pile",
            1.0,
            (700, 100, 1120, 150),
        )
    ]

    matches, _grouped = expeditionscan.scan_image(crop, {}, raw_lines=lines)

    assert matches[0]["name"] == "Verisium Pile"
    assert matches[0]["kind"] == "catalog"
    assert matches[0]["priceAvailable"] is False


def test_scan_image_does_not_invent_unpriced_item_from_dark_game_text():
    crop = np.zeros((800, 1200, 3), dtype=np.uint8)
    lines = [
        expeditionscan.OcrLine(
            "Set up a second weapon set",
            1.0,
            (700, 100, 1120, 150),
        )
    ]

    matches, _grouped = expeditionscan.scan_image(crop, {}, raw_lines=lines)

    assert matches == []
