"""Unit tests for the Have-panel scanner pipeline (poed.have_scan).

These pin the failure classes the old pipeline accumulated fixes for:
icon-edge digit hallucinations, colored-art fill rejection, proposal
truncation, grid completion when card backgrounds are split by bright art,
and scale invariance of the glyph reader.  OCR-dependent behavior is
covered by the fixture routing tests and the partial regressions.
"""

from __future__ import annotations

import cv2
import numpy as np

from poed.have_scan import geometry, glyphs, names, quantities


# ---------------------------------------------------------------------------
# synthetic panel helpers


def _paste_glyph(icon: np.ndarray, char: str, x: int, y: int) -> None:
    """Draw a digit glyph (fill + outline) onto a BGR icon crop."""
    glyph = glyphs.load_bank()[char]
    h, w = glyph.gray.shape
    region = icon[y : y + h, x : x + w]
    gray3 = cv2.cvtColor(glyph.gray.astype(np.uint8), cv2.COLOR_GRAY2BGR)
    mask = ((glyph.fill > 0) | (glyph.outline > 0))[..., None]
    np.copyto(region, gray3, where=mask.repeat(3, axis=2))


def _synthetic_icon(
    chars: str = "", art: str = "none", side: int = 76
) -> np.ndarray:
    """A card icon square: dark tile, optional art, optional digit overlay."""
    rng = np.random.default_rng(7)
    icon = np.full((side, side, 3), 24, dtype=np.uint8)
    if art == "colored-bar":
        # saturated bright vertical bar right of the digits — the phantom-1
        # generator seen on real crystal art
        icon[6:30, 26:32] = (200, 60, 220)
    elif art == "bright-clutter":
        noise = rng.integers(120, 255, size=(side, side, 3), dtype=np.uint8)
        icon[side // 2 :] = noise[side // 2 :]
    bank = glyphs.load_bank()
    x = 4
    for char in chars:
        glyph = bank[char]
        _paste_glyph(icon, char, x, 6)
        x += glyph.fill_w + 2
    return icon


def _synthetic_panel(
    rows: int = 4, cols: int = 3, card_w: int = 416, card_h: int = 76
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """Stone-textured panel with a full card grid; returns (image, anchors)."""
    rng = np.random.default_rng(3)
    height = rows * (card_h + 16) + 120
    width = cols * (card_w + 18) + 120
    panel = rng.integers(80, 110, size=(height, width, 3), dtype=np.uint8)
    anchors = []
    for row in range(rows):
        for col in range(cols):
            x = 60 + col * (card_w + 18)
            y = 60 + row * (card_h + 16)
            panel[y : y + card_h, x : x + card_w] = 24
            # icon art: saturated crystal blob
            icon = panel[y : y + card_h, x : x + card_h]
            icon[10:66, 10:66] = (180, 90, 40)
            # name text: bright line in the name area
            panel[y + 28 : y + 44, x + card_h + 20 : x + card_w - 30] = 190
            anchors.append((x, y))
    return panel, anchors


# ---------------------------------------------------------------------------
# geometry


def test_complete_grid_finds_full_card_grid():
    panel, anchors = _synthetic_panel()
    cards, med_w, med_h = geometry.complete_grid(panel)
    assert len(cards) == len(anchors)
    assert abs(med_w - 416) <= 8
    assert abs(med_h - 76) <= 6


def test_complete_grid_recovers_card_split_by_bright_art():
    panel, anchors = _synthetic_panel()
    # bright art slab across one card's background splits its dark mask
    x, y = anchors[4]
    panel[y : y + 76, x + 90 : x + 180] = 235
    cards, _mw, _mh = geometry.complete_grid(panel)
    assert len(cards) == len(anchors)
    recovered = [c for c in cards if abs(c.x - x) < 20 and abs(c.y - y) < 20]
    assert recovered


def test_complete_grid_is_translation_invariant():
    panel, _anchors = _synthetic_panel()
    shifted = np.roll(panel, (9, 13), axis=(0, 1))
    cards_a, _, _ = geometry.complete_grid(panel)
    cards_b, _, _ = geometry.complete_grid(shifted)
    assert len(cards_a) == len(cards_b)


def test_card_boxes_expose_icon_and_name_areas():
    card = geometry.Card(100, 200, 416, 76)
    ix, iy, iw, ih = card.icon_box
    nx, ny, nw, nh = card.name_box
    assert (ix, iy, iw, ih) == (100, 200, 76, 76)
    assert (nx, ny) == (176, 200)
    assert nw == 416 - 76 and nh == 76


# ---------------------------------------------------------------------------
# glyphs / quantity reading


def _read(icon: np.ndarray, proposal: str | None) -> tuple[int | None, float]:
    gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)[:, :, 1]
    x0r, y0r = range(0, 12), range(2, 12)
    if proposal is not None:
        value, confidence, _last = glyphs.verify_digits(gray, proposal, x0r, y0r, sat)
        return value, confidence
    return glyphs.open_read(gray, x0r, y0r, sat)


def test_bank_has_all_ten_digits_and_k_suffix():
    bank = glyphs.load_bank()
    assert sorted(bank) == [str(d) for d in range(10)] + ["K"]


def test_verify_accepts_true_digits():
    icon = _synthetic_icon("17")
    value, confidence = _read(icon, "17")
    assert value == 17
    assert confidence >= glyphs.VTHRESH


def test_verify_truncates_icon_edge_hallucination():
    # OCR proposes an extra trailing digit that is not on screen — the
    # classic icon-edge artifact ("17" read as "174").
    icon = _synthetic_icon("17")
    value, _confidence = _read(icon, "174")
    assert value == 17


def test_verify_rejects_colored_art_as_digit_fill():
    # A saturated bright bar after the digit must not verify as a trailing
    # digit even when OCR claims one (grayscale-fill gate).
    icon = _synthetic_icon("1", art="colored-bar")
    value, _confidence = _read(icon, "11")
    assert value == 1


def test_verify_rejects_wrong_leading_digit():
    icon = _synthetic_icon("8")
    value, _confidence = _read(icon, "3")
    assert value is None


def test_open_read_reads_digits_without_proposal():
    icon = _synthetic_icon("24")
    value, confidence = _read(icon, None)
    assert value == 24
    assert confidence >= glyphs.VTHRESH


def test_open_read_returns_none_on_artwork_only():
    icon = _synthetic_icon("", art="bright-clutter")
    value, _confidence = _read(icon, None)
    assert value is None


def test_glyph_reader_is_scale_invariant():
    # Same digits at 1.5x card scale must read identically after the
    # reference-side normalization used by read_card_quantities.
    icon = _synthetic_icon("12")
    scaled = cv2.resize(icon, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_CUBIC)
    reference = glyphs.reference_icon_side()
    normalized = cv2.resize(
        scaled,
        (reference, reference),
        interpolation=cv2.INTER_CUBIC,
    )
    value, _confidence = _read(normalized, "12")
    assert value == 12


def test_compact_count_parses_x_junk_and_compact_suffixes():
    from poed.expedition_text import parse_compact_count

    assert parse_compact_count("x12").groups == ("12",)
    assert parse_compact_count("12y/l").groups == ("12",)
    assert parse_compact_count("6All").groups == ("6",)
    assert parse_compact_count("R") is None
    token = parse_compact_count("11K")
    assert token.groups == ("11",) and token.suffix == "K"
    assert parse_compact_count("12y/l").suffix is None


def test_compact_count_never_yields_truncated_prefixes():
    """1,450 / 32.2K / 1.2M and over-long digit runs must parse as their
    full token or be flagged unsafe — never as a confident leading prefix."""
    from poed.expedition_text import parse_compact_count

    token = parse_compact_count("1,450")
    assert token.groups == ("1", "450") and not token.ambiguous
    token = parse_compact_count("32.2K")
    assert token.groups == ("32",) and token.frac == "2" and token.suffix == "K"
    token = parse_compact_count("1.2M")
    assert token.suffix == "M"
    assert parse_compact_count("12345").ambiguous
    assert parse_compact_count("1,45").ambiguous
    assert parse_compact_count("32.2").ambiguous


def test_verify_suffix_confirms_k_after_digits():
    icon = _synthetic_icon("11K")
    gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)[:, :, 1]
    value, _conf, last = glyphs.verify_digits(gray, "11", range(0, 12), range(2, 12), sat)
    assert value == 11
    assert glyphs.verify_suffix(gray, "K", *last, sat=sat)


def test_verify_suffix_rejects_k_on_plain_number():
    icon = _synthetic_icon("11")
    gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)[:, :, 1]
    value, _conf, last = glyphs.verify_digits(gray, "11", range(0, 12), range(2, 12), sat)
    assert value == 11
    assert not glyphs.verify_suffix(gray, "K", *last, sat=sat)


# ---------------------------------------------------------------------------
# names


def test_split_text_lines_finds_wrapped_name_lines():
    strip = np.full((70, 300, 3), 20, dtype=np.uint8)
    strip[12:28, 10:250] = 200
    strip[40:56, 10:180] = 200
    spans = names.split_text_lines(strip)
    assert len(spans) == 2
    assert spans[0][0] <= 12 <= spans[0][1]
    assert spans[1][0] <= 40 <= spans[1][1]


def test_split_text_lines_ignores_empty_area():
    strip = np.full((70, 300, 3), 20, dtype=np.uint8)
    assert names.split_text_lines(strip) == []


# ---------------------------------------------------------------------------
# cross-press identification cache


def test_repeat_scan_reuses_ocr_reads_for_unchanged_cards(monkeypatch):
    from poed import ocr_worker
    from poed import scan_cache

    panel, _anchors = _synthetic_panel()
    cards, _mw, _mh = geometry.complete_grid(panel)
    assert cards

    calls = {"n": 0}

    def fake_recognize(paths, timeout=60.0):
        calls["n"] += len(paths)
        return [{"text": "Chaos Orb", "score": 0.99} for _ in paths]

    monkeypatch.setattr(ocr_worker, "recognize_images", fake_recognize)
    scan_cache.clear()

    scan_cache.begin_scan()
    first_names = names.read_card_names(panel, cards)
    first_qty = quantities.read_card_quantities(panel, cards)
    first_calls = calls["n"]
    assert first_calls > 0

    # Same pixels on the next press: no OCR, identical output.
    scan_cache.begin_scan()
    assert names.read_card_names(panel, cards) == first_names
    assert quantities.read_card_quantities(panel, cards) == first_qty
    assert calls["n"] == first_calls

    # A changed card re-reads only itself.
    changed = cards[0]
    panel[changed.y : changed.y + 8, changed.x : changed.x + changed.w] = 250
    scan_cache.begin_scan()
    names.read_card_names(panel, cards)
    assert calls["n"] > first_calls
    assert calls["n"] <= first_calls + 4
    scan_cache.clear()


def test_cache_entries_expire_after_two_generations(monkeypatch):
    from poed import ocr_worker
    from poed import scan_cache

    panel, _anchors = _synthetic_panel()
    cards, _mw, _mh = geometry.complete_grid(panel)
    calls = {"n": 0}

    def fake_recognize(paths, timeout=60.0):
        calls["n"] += len(paths)
        return [{"text": "Exalted Orb", "score": 0.9} for _ in paths]

    monkeypatch.setattr(ocr_worker, "recognize_images", fake_recognize)
    scan_cache.clear()
    scan_cache.begin_scan()
    names.read_card_names(panel, cards)
    first_calls = calls["n"]

    # Two presses that never touch these digests age them out.
    scan_cache.begin_scan()
    scan_cache.begin_scan()
    names.read_card_names(panel, cards)
    assert calls["n"] == first_calls * 2
    scan_cache.clear()


def _paste_sequence(tokens: list[str], side: int = 76) -> np.ndarray:
    """Icon with a rendered count: digit/K glyphs and ',' / '.' separators."""
    bank = glyphs.load_bank()
    icon = np.full((side, side, 3), 24, dtype=np.uint8)
    x, y = 4, 6
    for token in tokens:
        if token in (",", "."):
            height = bank["1"].gray.shape[0]
            sep = max(4, round(bank["0"].fill_w * 0.55))
            # small bright separator mark near the baseline
            icon[y + height - 4 : y + height - 1, x + 1 : x + 3] = 235
            x += sep
            continue
        _paste_glyph(icon, token, x, y)
        x += bank[token].fill_w + 2
    return icon


def _roi(icon: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    gray = cv2.cvtColor(icon, cv2.COLOR_BGR2GRAY).astype(np.float32)
    sat = cv2.cvtColor(icon, cv2.COLOR_BGR2HSV)[:, :, 1]
    return gray, sat


def test_verify_compact_reads_comma_grouped_thousands():
    gray, sat = _roi(_paste_sequence(["1", ",", "4", "5", "0"]))
    value, conf = glyphs.verify_compact(
        gray, ("1", "450"), None, None, range(0, 12), range(2, 12), sat
    )
    assert value == 1450
    assert conf >= glyphs.VTHRESH


def test_verify_compact_reads_decimal_k():
    gray, sat = _roi(_paste_sequence(["3", "2", ".", "2", "K"]))
    value, _conf = glyphs.verify_compact(
        gray, ("32",), "2", "K", range(0, 12), range(2, 12), sat
    )
    assert value == 32200


def test_verify_compact_refuses_m_suffix_without_stencil():
    gray, sat = _roi(_paste_sequence(["1", ".", "2"]))
    value, _conf = glyphs.verify_compact(
        gray, ("1",), "2", "M", range(0, 12), range(2, 12), sat
    )
    assert value is None


def test_open_read_concatenates_comma_groups():
    gray, sat = _roi(_paste_sequence(["1", ",", "4", "5", "0"]))
    value, _conf = glyphs.open_read(gray, range(0, 12), range(2, 12), sat)
    assert value == 1450


def test_open_read_reads_decimal_k_without_proposal():
    gray, sat = _roi(_paste_sequence(["3", "2", ".", "2", "K"]))
    value, _conf = glyphs.open_read(gray, range(0, 12), range(2, 12), sat)
    assert value == 32200


def test_open_read_ignores_gap_digits_without_compact_pattern():
    """Gap digits forming no valid compact (two digits, no K) are art
    phantoms as far as the reader is concerned: the contiguous read wins.
    Bare decimal counts are not a game render, so this cannot truncate a
    real value — but real art regularly fakes gap digits."""
    gray, sat = _roi(_paste_sequence(["1", ".", "4", "5"]))
    value, _conf = glyphs.open_read(gray, range(0, 12), range(2, 12), sat)
    assert value == 1


def test_quantities_never_truncate_compact_proposals(monkeypatch):
    """End-to-end: a card rendering 1,450 with OCR proposing '1,450' must
    read 1450 — and never the truncated 1 the old prefix regex produced."""
    from poed import ocr_worker, scan_cache

    reference = glyphs.reference_icon_side()
    icon = _paste_sequence(["1", ",", "4", "5", "0"], side=reference)
    crop = np.full((reference + 40, 420, 3), 90, dtype=np.uint8)
    crop[8 : 8 + reference, 8 : 8 + reference] = icon
    card = geometry.Card(x=8, y=8, w=400, h=reference)

    monkeypatch.setattr(
        ocr_worker, "recognize_arrays",
        lambda images, timeout=60.0: [{"text": "1,450", "score": 0.9} for _ in images],
    )
    scan_cache.clear()
    [quantity] = quantities.read_card_quantities(crop, [card])
    scan_cache.clear()
    assert quantity.value == 1450
    assert quantity.source == "verified"


def test_split_text_lines_keeps_short_wrapped_continuation():
    """A wrapped name's second line can be two characters ("16)"); the
    row threshold must be absolute pixels, not a fraction of strip width."""
    strip = np.full((44, 320, 3), 20, dtype=np.uint8)
    strip[6:16, 4:200] = 220     # long first line
    strip[26:36, 4:14] = 220     # tiny second line, ~2% of width
    spans = names.split_text_lines(strip)
    assert len(spans) == 2


def test_open_read_refuses_contiguous_five_digit_overflow():
    """Five contiguous digits exceed MAX_DIGITS: refuse, never truncate."""
    gray, sat = _roi(_paste_sequence(["1", "2", "3", "4", "5"], side=110))
    value, _conf = glyphs.open_read(gray, range(0, 12), range(2, 12), sat)
    assert value is None
