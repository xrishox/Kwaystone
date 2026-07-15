import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from poed.image_geometry import Rect
from poed.image_geometry import frame_source
from poed import uniquescan


def _icon(tmp_path, name, img):
    p = tmp_path / f"{name}.png"
    cv2.imwrite(str(p), img)
    return str(p)


# Synthetic icons are 47px = PXSLOT for w=1, so _load_corpus applies no
# density normalization and factor=1.0 shots match directly. The wide-belt
# test below covers the normalization + factor path explicitly.
def _textured(rng, size=47):
    return rng.integers(0, 255, (size, size), dtype=np.uint8)


def _tint(gray_img, bgr_weights):
    """Tint a grayscale pattern into BGR. CCOEFF_NORMED is scale-invariant,
    so differently-tinted copies are IDENTICAL to the gray pass and only the
    color-verify stage can tell them apart."""
    g = gray_img.astype(np.float32)
    return np.stack([g * w for w in bgr_weights], axis=-1).astype(np.uint8)


def _fake_rows(tmp_path, rng, n=3, size=47):
    rows = {}
    for i in range(n):
        img = rng.integers(0, 255, (size, size, 3), dtype=np.uint8)
        rows[f"Unique {i}"] = {
            "price": float(100 - i), "quantity": 5, "kind": "unique",
            "w": 1, "h": 1,
            "iconPath": _icon(tmp_path, f"u{i}", img),
        }
    return rows


def test_scan_finds_planted_icons(tmp_path):
    rng = np.random.default_rng(42)
    rows = _fake_rows(tmp_path, rng)
    rows["Unique 0"].update({
        "quoteAmount": 0.5,
        "quoteCurrency": "chaos",
        "quoteCurrencyText": "Chaos Orb",
        "quoteLiquidity": 120,
        "quoteBuyerStock": 40,
        "quoteAvailable": True,
        "exaltedPerChaos": 0.25,
        "exaltedPerDivine": 333,
        "sourceTag": "unique-0",
        "sourceCategory": "currency",
    })
    shot = np.full((400, 600, 3), 10, np.uint8)
    icon0 = cv2.imread(rows["Unique 0"]["iconPath"])
    icon2 = cv2.imread(rows["Unique 2"]["iconPath"])
    shot[50:97, 40:87] = icon0
    shot[200:247, 300:347] = icon2
    matches = uniquescan._scan_shared(shot, rows, factors=(1.0,))
    names = sorted(m["name"] for m in matches)
    assert names == ["Unique 0", "Unique 2"]
    m0 = next(m for m in matches if m["name"] == "Unique 0")
    assert (m0["x"], m0["y"]) == (40, 50)
    assert m0["price"] == 100.0
    assert m0["quoteAmount"] == 0.5
    assert m0["quoteCurrency"] == "chaos"
    assert m0["quoteAvailable"] is True
    assert m0["exaltedPerChaos"] == 0.25
    assert m0["exaltedPerDivine"] == 333
    assert m0["sourceTag"] == "unique-0"
    assert m0["sourceCategory"] == "currency"


def test_filter_ritual_rows_excludes_fragment_tagged_candidates():
    rows = {
        "Origin Spark": {
            "kind": "tagged",
            "sourceCategory": "fragments",
            "price": 10.0,
        },
        "Omen of Resurgence": {
            "kind": "tagged",
            "sourceCategory": "ritual",
            "price": 10.0,
        },
        "Orb of Alchemy": {
            "kind": "tagged",
            "sourceCategory": "currency",
            "price": 1.0,
        },
        "Stale Tagged Row": {
            "kind": "tagged",
            "price": 1.0,
        },
        "Unique Reward": {
            "kind": "unique",
            "sourceCategory": "fragments",
            "price": 1.0,
        },
        "Catalog Reward": {
            "kind": "catalog",
            "sourceCategory": "fragments",
            "price": 0.0,
        },
    }

    filtered = uniquescan.filter_ritual_rows(rows)

    assert set(filtered) == {
        "Omen of Resurgence",
        "Orb of Alchemy",
        "Stale Tagged Row",
        "Unique Reward",
        "Catalog Reward",
    }


def test_scan_skips_rows_without_icon(tmp_path):
    rows = {"No Icon": {"price": 5.0, "quantity": 1, "kind": "tagged",
                        "w": 1, "h": 1, "iconPath": None}}
    shot = np.full((200, 200, 3), 10, np.uint8)
    assert uniquescan._scan_shared(shot, rows, factors=(1.0,)) == []


def test_shared_art_groups_one_match_max_price(tmp_path):
    rng = np.random.default_rng(44)
    img = rng.integers(0, 255, (47, 47, 3), dtype=np.uint8)
    path = _icon(tmp_path, "shared", img)
    rows = {
        "Gem (Level 1)": {"price": 3.0, "quantity": 5, "kind": "tagged",
                          "w": 1, "h": 1, "iconPath": path},
        "Gem (Level 20)": {"price": 90.0, "quantity": 2, "kind": "tagged",
                           "w": 1, "h": 1, "iconPath": path},
    }
    shot = np.full((200, 200, 3), 10, np.uint8)
    shot[30:77, 30:77] = img
    matches = uniquescan._scan_shared(shot, rows, factors=(1.0,))
    assert len(matches) == 1
    m = matches[0]
    assert m["price"] == 90.0
    assert m["ambiguous"] is True
    assert "Gem (Level 1)" in m["name"] and "+1" in m["name"]


def test_color_verify_disambiguates_same_luminance_tints(tmp_path):
    rng = np.random.default_rng(45)
    pattern = _textured(rng)
    red = _tint(pattern, (0.2, 0.2, 1.0))
    blue = _tint(pattern, (1.0, 0.2, 0.2))
    rows = {
        "Red Omen": {"price": 10.0, "quantity": 5, "kind": "tagged",
                     "w": 1, "h": 1, "iconPath": _icon(tmp_path, "red", red)},
        "Blue Omen": {"price": 20.0, "quantity": 5, "kind": "tagged",
                      "w": 1, "h": 1, "iconPath": _icon(tmp_path, "blue", blue)},
    }
    shot = np.full((200, 200, 3), 10, np.uint8)
    shot[40:87, 60:107] = red
    matches = uniquescan._scan_shared(shot, rows, factors=(1.0,))
    assert [m["name"] for m in matches] == ["Red Omen"]


def test_density_normalization(tmp_path):
    """A 2-slot-wide icon stored at 94px (47 px/slot) must match a screen
    cell rendered at the same density after factor scaling."""
    rng = np.random.default_rng(46)
    img = rng.integers(0, 255, (47, 94, 3), dtype=np.uint8)  # 2x1 at 47 px/slot
    rows = {"Wide Belt": {"price": 50.0, "quantity": 1, "kind": "unique",
                          "w": 2, "h": 1, "iconPath": _icon(tmp_path, "belt", img)}}
    # Screen renders at 94 px/slot (2x): upscale the art into the shot, then
    # scan with factor 0.5 to bring the shot back to template density.
    big = cv2.resize(img, None, fx=2.0, fy=2.0, interpolation=cv2.INTER_LINEAR)
    shot = np.full((400, 600, 3), 10, np.uint8)
    shot[100:100 + big.shape[0], 80:80 + big.shape[1]] = big
    matches = uniquescan._scan_shared(shot, rows, factors=(0.5,))
    assert [m["name"] for m in matches] == ["Wide Belt"]
    m = matches[0]
    # coords map back to native shot pixels (tolerance: resize rounding)
    assert abs(m["x"] - 80) <= 2 and abs(m["y"] - 100) <= 2


def test_corpus_normalizes_sparse_icon_by_strongest_slot_density(tmp_path):
    rng = np.random.default_rng(4601)
    # Same shape class as a sparse 2x4 weapon icon: one-slot-wide art stored at
    # 94 px/slot with two-slot metadata. Normalization must preserve aspect
    # ratio instead of stretching the art to a full 2-slot canvas.
    img = rng.integers(0, 255, (376, 94, 3), dtype=np.uint8)
    rows = {
        "Sparse Tall Icon": {
            "price": 50.0,
            "quantity": 1,
            "kind": "unique",
            "w": 2,
            "h": 4,
            "iconPath": _icon(tmp_path, "tall-cache", img),
        }
    }
    uniquescan._corpus_cache = None

    [template] = uniquescan._load_corpus(rows)

    assert template["color"].shape[:2] == (uniquescan.PXSLOT * 4, uniquescan.PXSLOT)
    assert template["gray"].shape == (uniquescan.PXSLOT * 4, uniquescan.PXSLOT)


def test_precomputed_robust_template_tiles_preserve_visual_score():
    rng = np.random.default_rng(4602)
    template = rng.integers(0, 255, (47, 47, 3), dtype=np.uint8)
    sample = template.copy()
    sample[:8, :8] = 0

    direct = uniquescan._robust_visual_score(sample, template)
    cached = uniquescan._robust_visual_score(
        sample,
        template,
        uniquescan._robust_template_tiles(template),
    )

    assert cached == pytest.approx(direct, abs=1e-6)


def test_scan_frame_source_crops_to_game_rect():
    shot = np.zeros((100, 200, 3), np.uint8)

    frame, x0, y0, source = frame_source(shot, Rect(20, 10, 80, 50))

    assert source == "output"  # too small to trust, so fallback stays monitor-wide
    assert frame.shape == shot.shape

    large = np.zeros((1200, 2000, 3), np.uint8)
    frame, x0, y0, source = frame_source(large, Rect(200, 100, 1000, 800))

    assert source == "game"
    assert (x0, y0) == (200, 100)
    assert frame.shape == (800, 1000, 3)


def test_filter_rows_drops_cheap_corpus_entries():
    rows = {
        "Junk Scrap": {"price": 0.02, "iconPath": "/x", "w": 1, "h": 1},
        "Borderline": {"price": 0.5, "iconPath": "/y", "w": 1, "h": 1},
        "Mageblood": {"price": 67305.6, "iconPath": "/z", "w": 1, "h": 1},
        "No Price": {"price": None, "iconPath": "/q", "w": 1, "h": 1},
    }
    kept = uniquescan.filter_rows(rows, 0.5)
    assert sorted(kept) == ["Borderline", "Mageblood"]
    assert uniquescan.filter_rows(rows, 0.0) == rows


def test_positive_int_env_falls_back_for_invalid_value(monkeypatch):
    monkeypatch.setenv("WAYSTONE_TEST_INT", "not-an-int")
    assert uniquescan._positive_int_env("WAYSTONE_TEST_INT", 4) == 4
    monkeypatch.setenv("WAYSTONE_TEST_INT", "0")
    assert uniquescan._positive_int_env("WAYSTONE_TEST_INT", 4) == 1
    monkeypatch.setenv("WAYSTONE_TEST_INT", "3")
    assert uniquescan._positive_int_env("WAYSTONE_TEST_INT", 4) == 3


def test_corpus_cache_keyed_by_content_not_identity(tmp_path):
    rng = np.random.default_rng(47)
    rows = _fake_rows(tmp_path, rng, n=2)
    t1 = uniquescan._load_corpus(rows)
    t2 = uniquescan._load_corpus(dict(rows))  # fresh dict, same content
    assert t1 is t2
    smaller = dict(list(rows.items())[:1])
    t3 = uniquescan._load_corpus(smaller)  # different content -> rebuild
    assert t3 is not t1
    t4 = uniquescan._load_corpus(dict(rows))
    assert t4 is t1


def test_warm_prebuilds_template_cache(tmp_path):
    rng = np.random.default_rng(48)
    rows = _fake_rows(tmp_path, rng, n=2)

    class FakeBrain:
        def request(self, msg, timeout=None, on_progress=None):
            assert msg["cmd"] == "uniqueprices"
            return rows

    cfg = {"league": "L", "unique_scan_min_price": 0.0}
    uniquescan._corpus_cache = None
    assert uniquescan.warm(FakeBrain(), cfg) is True
    assert uniquescan._corpus_cache is not None
    # A scan with content-equal rows reuses the warmed templates.
    t = uniquescan._load_corpus(dict(rows))
    assert t is uniquescan._corpus_cache["tmpl"]


def test_warm_prebuilds_filtered_template_variant(tmp_path):
    rng = np.random.default_rng(4801)
    rows = _fake_rows(tmp_path, rng, n=3)

    class FakeBrain:
        def request(self, msg, timeout=None, on_progress=None):
            assert msg["cmd"] == "uniqueprices"
            return rows

    cfg = {"league": "L", "unique_scan_min_price": 0.0}
    uniquescan._corpus_cache = None
    assert uniquescan.warm(
        FakeBrain(),
        cfg,
        row_filter=lambda source: {"Unique 1": source["Unique 1"]},
    ) is True

    [template] = uniquescan._corpus_cache["tmpl"]
    assert template["label"] == "Unique 1"


def test_warm_swallows_brain_failure():
    class DeadBrain:
        def request(self, msg, timeout=None, on_progress=None):
            raise RuntimeError("brain down")

    assert uniquescan.warm(DeadBrain(), {"league": "L", "unique_scan_min_price": 0}) is False


def test_scan_finds_duplicate_items(tmp_path):
    """Two copies of the same item on screen (ritual duplicate omens, vendor
    duplicate stock) must both be reported, not just the best one."""
    rng = np.random.default_rng(49)
    rows = _fake_rows(tmp_path, rng, n=1)
    icon = cv2.imread(rows["Unique 0"]["iconPath"])
    shot = np.full((400, 600, 3), 10, np.uint8)
    shot[50:97, 40:87] = icon
    shot[250:297, 400:447] = icon
    matches = uniquescan._scan_shared(shot, rows, factors=(1.0,))
    assert [m["name"] for m in matches] == ["Unique 0", "Unique 0"]
    coords = sorted((m["x"], m["y"]) for m in matches)
    assert coords == [(40, 50), (400, 250)]


def _grid_fixture(cell=80):
    w, h = 1400, 1200
    crop = np.full((h, w, 3), 6, np.uint8)
    grid = uniquescan._ritual_grid(crop)
    assert grid is not None
    for c in range(grid.cols + 1):
        x = round(grid.x0 + c * grid.cell)
        cv2.line(crop, (x, round(grid.y0)), (x, round(grid.y0 + grid.rows * grid.cell)),
                 (35, 35, 55), 1)
    for r in range(grid.rows + 1):
        y = round(grid.y0 + r * grid.cell)
        cv2.line(crop, (round(grid.x0), y), (round(grid.x0 + grid.cols * grid.cell), y),
                 (35, 35, 55), 1)
    return crop, grid


def test_cell_scan_classifies_occupied_grid_cell(tmp_path):
    rng = np.random.default_rng(50)
    rows = _fake_rows(tmp_path, rng, n=1)
    icon = cv2.imread(rows["Unique 0"]["iconPath"])
    crop, grid = _grid_fixture()
    x0, y0, x1, y1 = uniquescan._cell_rect(grid, 2, 3, pad=0)
    big = cv2.resize(icon, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
    crop[y0:y1, x0:x1] = big

    matches = uniquescan._scan_grid_cells(crop, rows, grid)

    assert [m["name"] for m in matches] == ["Unique 0"]
    assert abs(matches[0]["x"] - x0) <= 4
    assert abs(matches[0]["y"] - y0) <= 4


def test_ritual_grid_factors_follow_visible_cell_size():
    crop = np.full((1404, 1383, 3), 6, np.uint8)

    factors = uniquescan._ritual_grid_factors(crop)

    assert factors is not None
    assert 0.45 < factors[2] < 0.47
    assert len(factors) == 5


def test_cell_scan_handles_two_slot_item(tmp_path):
    rng = np.random.default_rng(51)
    img = rng.integers(0, 255, (47, 94, 3), dtype=np.uint8)
    rows = {"Wide Belt": {"price": 50.0, "quantity": 1, "kind": "unique",
                          "w": 2, "h": 1, "iconPath": _icon(tmp_path, "belt", img)}}
    crop, grid = _grid_fixture()
    x0, y0, x1, y1 = uniquescan._cell_rect(grid, 1, 4, slots_w=2, slots_h=1, pad=0)
    big = cv2.resize(img, (x1 - x0, y1 - y0), interpolation=cv2.INTER_LINEAR)
    crop[y0:y1, x0:x1] = big

    matches = uniquescan._scan_grid_cells(crop, rows, grid)

    assert [m["name"] for m in matches] == ["Wide Belt"]


def _dynamic_grid_fixture(cell=80):
    width = 1100
    height = 900
    x0 = 20
    y0 = 24
    crop = np.full((height, width, 3), 6, np.uint8)
    for col in range(uniquescan.RITUAL_COLS + 1):
        x = round(x0 + col * cell)
        cv2.line(crop, (x, y0), (x, y0 + uniquescan.RITUAL_ROWS * cell),
                 (42, 42, 62), 2)
    for row in range(uniquescan.RITUAL_ROWS + 1):
        y = round(y0 + row * cell)
        cv2.line(crop, (x0, y), (x0 + uniquescan.RITUAL_COLS * cell, y),
                 (42, 42, 62), 2)
    return crop, x0, y0, cell


def test_dynamic_ritual_grid_recovers_origin_from_panel_crop():
    crop, x0, y0, cell = _dynamic_grid_fixture()

    grid = uniquescan._dynamic_ritual_grid(crop, cell)

    assert grid is not None
    assert abs(grid.x0 - x0) <= 2
    assert abs(grid.y0 - y0) <= 2
    assert grid.cell == cell


def test_candidate_slots_allow_sparse_cells_for_large_multislot():
    occupied = np.array([
        [False, True],
        [True, True],
        [True, False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 3)) == [(0, 0)]


def test_candidate_slots_allow_half_visible_large_multislot_with_spread():
    occupied = np.array([
        [False, True],
        [True, True],
        [True, False],
        [True, False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 4)) == [(0, 0)]


def test_candidate_slots_allow_adjacent_row_sparse_large_multislot():
    occupied = np.array([
        [True, True],
        [True, False],
        [False, False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 3)) == [(0, 0)]


def test_candidate_slots_allow_sparse_long_skinny_item():
    occupied = np.array([
        [True],
        [True],
        [False],
        [False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 1, 4)) == [(0, 0)]


def test_candidate_slots_reject_sparse_large_multislot_without_width_spread():
    occupied = np.array([
        [True, False],
        [True, False],
        [True, False],
        [True, False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 4)) == []


def test_candidate_slots_reject_sparse_2x2_with_only_two_cells():
    occupied = np.array([
        [True, True],
        [False, False],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 2)) == []


def test_candidate_slots_allow_sparse_2x2_with_three_cells_and_spread():
    occupied = np.array([
        [False, True],
        [True, True],
    ])

    assert list(uniquescan._candidate_slots(occupied, 2, 2)) == [(0, 0)]


def test_decorated_1x1_fallback_uses_unrestricted_robust_search(monkeypatch):
    true = {"label": "True Reward"}
    false = {"label": "False Reward"}
    calls = []

    def fake_candidates(window, templates, limit=uniquescan.RITUAL_PREFILTER_LIMIT,
                        gray_limit=uniquescan.RITUAL_FULL_GRAY_LIMIT,
                        verify_limit=uniquescan.RITUAL_SHORTLIST_VERIFY_LIMIT):
        calls.append((limit, gray_limit))
        if gray_limit is None:
            return [
                (true, 0.43, 0.20, (0, 0)),
                (false, 0.36, 0.50, (0, 0)),
            ]
        return []

    monkeypatch.setattr(uniquescan, "_robust_candidates_in_window", fake_candidates)
    window = np.zeros((47, 47, 3), dtype=np.uint8)
    window[20:, 20:] = (0, 170, 220)

    accepted = uniquescan._accepted_cell_candidate_in_window(
        window,
        [true, false],
        1,
        1,
    )

    assert accepted == (true, 0.43, (0, 0))
    assert calls[-1] == (None, None)


def test_scan_region_marks_unmatched_occupied_ritual_cell():
    crop, x0, y0, cell = _dynamic_grid_fixture()
    rng = np.random.default_rng(52)
    cx0, cy0, cx1, cy1 = x0, y0, x0 + cell, y0 + cell
    art = rng.integers(45, 160, (cell, cell, 3), dtype=np.uint8)
    # Simulate the visual shape that triggered the bug: a reward cell with item
    # art plus a bright bottom-right Ritual/deferred marker that does not exist
    # in the clean web icon template.
    cv2.rectangle(art, (cell - 28, cell - 28), (cell - 4, cell - 4),
                  (20, 220, 240), -1)
    crop[cy0:cy1, cx0:cx1] = art

    matches = uniquescan.scan_region(
        crop,
        {},
        Rect(0, 0, crop.shape[1], crop.shape[0]),
        cell,
        include_unknown=True,
    )

    assert len(matches) == 1
    assert matches[0]["name"] == "Unrecognized reward"
    assert matches[0]["priceAvailable"] is False
    assert matches[0]["markerOnly"] is True
    assert abs(matches[0]["x"] - x0) <= 2
    assert abs(matches[0]["y"] - y0) <= 2


def test_scan_region_can_disable_unknown_ritual_cells():
    crop, x0, y0, cell = _dynamic_grid_fixture()
    crop[y0:y0 + cell, x0:x0 + cell] = 120

    matches = uniquescan.scan_region(
        crop,
        {},
        Rect(0, 0, crop.shape[1], crop.shape[0]),
        cell,
        include_unknown=False,
    )

    assert matches == []


def test_scan_region_marks_unknown_ritual_cells_by_default():
    crop, x0, y0, cell = _dynamic_grid_fixture()
    rng = np.random.default_rng(54)
    crop[y0:y0 + cell, x0:x0 + cell] = rng.integers(
        45, 160, (cell, cell, 3), dtype=np.uint8
    )

    matches = uniquescan.scan_region(
        crop,
        {},
        Rect(0, 0, crop.shape[1], crop.shape[0]),
        cell,
    )

    assert len(matches) == 1
    assert matches[0]["markerOnly"] is True


def test_scan_region_classifies_dynamic_ritual_cell(tmp_path):
    rng = np.random.default_rng(53)
    rows = _fake_rows(tmp_path, rng, n=1)
    icon = cv2.imread(rows["Unique 0"]["iconPath"])
    crop, x0, y0, cell = _dynamic_grid_fixture()
    target_x0 = x0 + 2 * cell
    target_y0 = y0 + 3 * cell
    big = cv2.resize(icon, (cell, cell), interpolation=cv2.INTER_LINEAR)
    crop[target_y0:target_y0 + cell, target_x0:target_x0 + cell] = big

    matches = uniquescan.scan_region(
        crop,
        rows,
        Rect(0, 0, crop.shape[1], crop.shape[0]),
        cell,
    )

    assert [m["name"] for m in matches] == ["Unique 0"]
    assert matches[0]["cell"] == (2, 3)


def test_scan_region_classifies_decorated_greyed_ritual_cell(tmp_path):
    rng = np.random.default_rng(55)
    rows = _fake_rows(tmp_path, rng, n=1)
    icon = cv2.imread(rows["Unique 0"]["iconPath"])
    crop, x0, y0, cell = _dynamic_grid_fixture()
    target_x0 = x0 + 3 * cell
    target_y0 = y0 + 2 * cell
    big = cv2.resize(icon, (cell, cell), interpolation=cv2.INTER_LINEAR)
    grey = cv2.cvtColor(cv2.cvtColor(big, cv2.COLOR_BGR2GRAY), cv2.COLOR_GRAY2BGR)
    decorated = cv2.addWeighted(big, 0.45, grey, 0.55, 0)
    cv2.rectangle(decorated, (0, 0), (cell - 1, cell - 1), (180, 40, 220), 7)
    cv2.circle(decorated, (cell - 18, cell - 18), 15, (20, 230, 140), -1)
    crop[target_y0:target_y0 + cell, target_x0:target_x0 + cell] = decorated

    matches = uniquescan.scan_region(
        crop,
        rows,
        Rect(0, 0, crop.shape[1], crop.shape[0]),
        cell,
    )

    named = [m for m in matches if not m.get("markerOnly")]
    assert [m["name"] for m in named] == ["Unique 0"]
    assert named[0]["cell"] == (3, 2)


def test_ritual_cell_candidate_accepts_high_confidence_modest_margin():
    strong = ({"label": "Strong"}, 0.90, 0.91, (0, 0))
    mid = ({"label": "Mid"}, 0.778, 0.80, (0, 0))
    weak = ({"label": "Weak"}, 0.40, 0.90, (0, 0))
    mid_close = ({"label": "Mid Close"}, 0.753, 0.79, (0, 0))
    modest = ({"label": "Modest"}, 0.875, 0.90, (0, 0))
    close = ({"label": "Close"}, 0.89, 0.90, (0, 0))

    accepted = uniquescan._accepted_cell_candidate([strong, weak], 1, 1)

    assert accepted is not None
    assert accepted[0]["label"] == "Strong"
    accepted = uniquescan._accepted_cell_candidate([strong, modest], 1, 1)
    assert accepted is not None
    assert accepted[0]["label"] == "Strong"
    accepted = uniquescan._accepted_cell_candidate([mid, mid_close], 1, 1)
    assert accepted is not None
    assert accepted[0]["label"] == "Mid"
    assert uniquescan._accepted_cell_candidate([weak], 1, 1) is None
    assert uniquescan._accepted_cell_candidate([strong, close], 1, 1) is None


def test_ritual_grid_falls_back_for_unmatched_one_slot_cell(monkeypatch):
    crop = np.zeros((80, 80, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=10, y0=10, cell=47, cols=1, rows=1)
    occupied = np.array([[True]])
    template = {
        "label": "Correct",
        "kind": "tagged",
        "price": 1.0,
        "quantity": 1,
        "ambiguous": False,
        "gray": np.zeros((47, 47), dtype=np.uint8),
    }
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: [template])
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(*args, full_search=False, **kwargs):
        calls.append(full_search)
        if not full_search:
            return None
        return {
            "name": "Correct",
            "kind": "tagged",
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.86,
            "x": 10,
            "y": 10,
            "w": 47,
            "h": 47,
            "cell": (0, 0),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert calls == [False, True]
    assert [match["name"] for match in matches] == ["Correct"]


def test_ritual_grid_skips_cells_claimed_by_confident_large_match(monkeypatch):
    crop = np.zeros((120, 120, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=2)
    occupied = np.ones((2, 2), dtype=bool)
    templates = [
        {
            "label": "Large",
            "slots_w": 2,
            "slots_h": 2,
            "price": 10.0,
            "quantity": 1,
            "ambiguous": False,
        },
        {
            "label": "Small",
            "slots_w": 1,
            "slots_h": 1,
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
        },
    ]
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: templates)
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        calls.append((slots_w, slots_h, col, row))
        if (slots_w, slots_h) != (2, 2):
            return None
        return {
            "name": "Large",
            "kind": "unique",
            "price": 10.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.90,
            "x": 0,
            "y": 0,
            "w": 94,
            "h": 94,
            "cell": (0, 0),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == ["Large"]
    assert (2, 2, 0, 0) in calls


def test_ritual_grid_claims_sparse_2x2_before_cell_unknowns(monkeypatch):
    crop = np.zeros((120, 120, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=2)
    occupied = np.array([
        [False, True],
        [True, True],
    ])
    templates = [
        {
            "label": "Sparse Medium",
            "slots_w": 2,
            "slots_h": 2,
            "price": 10.0,
            "quantity": 1,
            "ambiguous": False,
        },
        {
            "label": "Small",
            "slots_w": 1,
            "slots_h": 1,
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
        },
    ]
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: templates)
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        calls.append((slots_w, slots_h, col, row, bool(kwargs.get("full_search"))))
        if (slots_w, slots_h, col, row) != (2, 2, 0, 0):
            return None
        return {
            "name": "Sparse Medium",
            "kind": "unique",
            "price": 10.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.90,
            "x": 0,
            "y": 0,
            "w": 94,
            "h": 94,
            "cell": (0, 0),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == ["Sparse Medium"]
    assert (2, 2, 0, 0, False) in calls


def test_ritual_grid_solver_keeps_strong_cells_over_weak_large_false_positive(monkeypatch):
    crop = np.zeros((120, 120, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=2)
    occupied = np.ones((2, 2), dtype=bool)
    templates = [
        {
            "label": "Weak Large",
            "slots_w": 2,
            "slots_h": 2,
            "price": 10.0,
            "quantity": 1,
            "ambiguous": False,
        },
        {
            "label": "Strong Small",
            "slots_w": 1,
            "slots_h": 1,
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
        },
    ]

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: templates)
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        if (slots_w, slots_h) == (2, 2):
            return {
                "name": "Weak Large",
                "kind": "unique",
                "price": 10.0,
                "quantity": 1,
                "ambiguous": False,
                "score": 0.55,
                "x": 0,
                "y": 0,
                "w": 94,
                "h": 94,
                "cell": (0, 0),
            }
        return {
            "name": "Strong Small",
            "kind": "tagged",
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.90,
            "x": col * 47,
            "y": row * 47,
            "w": 47,
            "h": 47,
            "cell": (col, row),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == [
        "Strong Small",
        "Strong Small",
        "Strong Small",
        "Strong Small",
    ]


def test_ritual_grid_retries_sparse_large_slot_with_full_search(monkeypatch):
    crop = np.zeros((180, 140, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=3)
    occupied = np.array([
        [True, True],
        [True, False],
        [False, False],
    ])
    template = {
        "label": "Sparse Large",
        "slots_w": 2,
        "slots_h": 3,
        "price": 1.0,
        "quantity": 1,
        "ambiguous": False,
    }
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: [template])
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        calls.append(bool(kwargs.get("full_search")))
        if not kwargs.get("full_search"):
            return None
        return {
            "name": "Sparse Large",
            "kind": "unique",
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.80,
            "x": 0,
            "y": 0,
            "w": 94,
            "h": 141,
            "cell": (0, 0),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == ["Sparse Large"]
    assert calls == [False, True]


def test_ritual_grid_skips_sparse_retry_when_cells_already_explained(monkeypatch):
    crop = np.zeros((180, 140, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=3)
    occupied = np.array([
        [True, True],
        [True, True],
        [False, False],
    ])
    templates = [
        {
            "label": "Sparse Probe",
            "slots_w": 2,
            "slots_h": 3,
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
        },
        {
            "label": "Strong Medium",
            "slots_w": 2,
            "slots_h": 2,
            "price": 2.0,
            "quantity": 1,
            "ambiguous": False,
        },
    ]
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: templates)
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        calls.append((slots_w, slots_h, bool(kwargs.get("full_search"))))
        if (slots_w, slots_h) == (2, 2):
            return {
                "name": "Strong Medium",
                "kind": "unique",
                "price": 2.0,
                "quantity": 1,
                "ambiguous": False,
                "score": 0.90,
                "x": 0,
                "y": 0,
                "w": 94,
                "h": 94,
                "cell": (0, 0),
            }
        return None

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == ["Strong Medium"]
    assert (2, 3, True) not in calls


def test_ritual_grid_retries_sparse_2x2_slot_with_full_search(monkeypatch):
    crop = np.zeros((120, 120, 3), dtype=np.uint8)
    grid = uniquescan.Grid(x0=0, y0=0, cell=47, cols=2, rows=2)
    occupied = np.array([
        [False, True],
        [True, True],
    ])
    template = {
        "label": "Sparse Medium",
        "slots_w": 2,
        "slots_h": 2,
        "price": 1.0,
        "quantity": 1,
        "ambiguous": False,
    }
    calls = []

    monkeypatch.setattr(uniquescan, "_load_corpus", lambda rows: [template])
    monkeypatch.setattr(uniquescan, "_occupied_cells", lambda crop, grid: occupied)

    def fake_classify(crop, group, grid, col, row, slots_w, slots_h, factor, **kwargs):
        calls.append(bool(kwargs.get("full_search")))
        if not kwargs.get("full_search"):
            return None
        return {
            "name": "Sparse Medium",
            "kind": "unique",
            "price": 1.0,
            "quantity": 1,
            "ambiguous": False,
            "score": 0.80,
            "x": 0,
            "y": 0,
            "w": 94,
            "h": 94,
            "cell": (0, 0),
        }

    monkeypatch.setattr(uniquescan, "_classify_cell_window", fake_classify)

    matches = uniquescan._scan_grid_cells(crop, {}, grid)

    assert [match["name"] for match in matches] == ["Sparse Medium"]
    assert calls == [False, True]


def test_ritual_cell_verifies_low_confidence_fast_match(monkeypatch):
    calls = []
    fast = [
        ({"label": "Wrong"}, 0.61, 0.63, (0, 0)),
        ({"label": "Weak"}, 0.40, 0.40, (0, 0)),
    ]
    full = [
        ({"label": "Correct"}, 0.87, 0.87, (0, 0)),
        ({"label": "Wrong"}, 0.61, 0.63, (0, 0)),
    ]

    def fake_candidates(window, templates, limit=uniquescan.RITUAL_PREFILTER_LIMIT,
                        gray_limit=uniquescan.RITUAL_FULL_GRAY_LIMIT,
                        verify_limit=uniquescan.RITUAL_SHORTLIST_VERIFY_LIMIT):
        calls.append(limit)
        return full if limit is None else fast

    monkeypatch.setattr(uniquescan, "_robust_candidates_in_window", fake_candidates)

    accepted = uniquescan._accepted_cell_candidate_in_window(
        np.zeros((47, 47, 3), dtype=np.uint8),
        [{"label": "Correct"}, {"label": "Wrong"}],
        1,
        1,
    )

    assert calls == [uniquescan.RITUAL_PREFILTER_LIMIT, None]
    assert accepted is not None
    assert accepted[0]["label"] == "Correct"


def test_ritual_cell_keeps_high_confidence_fast_match(monkeypatch):
    calls = []
    fast = [
        ({"label": "Correct"}, 0.83, 0.83, (0, 0)),
        ({"label": "Wrong"}, 0.50, 0.50, (0, 0)),
    ]

    def fake_candidates(window, templates, limit=uniquescan.RITUAL_PREFILTER_LIMIT,
                        gray_limit=uniquescan.RITUAL_FULL_GRAY_LIMIT,
                        verify_limit=uniquescan.RITUAL_SHORTLIST_VERIFY_LIMIT):
        calls.append(limit)
        return fast

    monkeypatch.setattr(uniquescan, "_robust_candidates_in_window", fake_candidates)

    accepted = uniquescan._accepted_cell_candidate_in_window(
        np.zeros((47, 47, 3), dtype=np.uint8),
        [{"label": "Correct"}, {"label": "Wrong"}],
        1,
        1,
    )

    assert calls == [uniquescan.RITUAL_PREFILTER_LIMIT]
    assert accepted is not None
    assert accepted[0]["label"] == "Correct"


def test_ritual_decorated_cell_uses_context_fallback(monkeypatch):
    wrong = {"label": "Wrong Rune", "sourceCategory": "runes"}
    correct = {"label": "Correct Omen", "sourceCategory": "ritual"}
    close = {"label": "Close Omen", "sourceCategory": "ritual"}
    calls = []

    def fake_candidates(window, templates, limit=uniquescan.RITUAL_PREFILTER_LIMIT,
                        gray_limit=uniquescan.RITUAL_FULL_GRAY_LIMIT,
                        verify_limit=uniquescan.RITUAL_SHORTLIST_VERIFY_LIMIT):
        calls.append(limit)
        return [
            (wrong, 0.36, 0.40, (0, 0)),
            (correct, 0.31, 0.38, (0, 0)),
        ]

    monkeypatch.setattr(uniquescan, "_robust_candidates_in_window", fake_candidates)
    monkeypatch.setattr(
        uniquescan,
        "_cell_has_warm_ritual_decoration",
        lambda window: True,
    )
    monkeypatch.setattr(
        uniquescan,
        "_decorated_context_candidates_in_window",
        lambda window, templates: [
            (correct, 0.52, 0.38, (1, 1)),
            (close, 0.47, 0.37, (1, 1)),
        ],
    )

    accepted = uniquescan._accepted_cell_candidate_in_window(
        np.zeros((47, 47, 3), dtype=np.uint8),
        [wrong, correct, close],
        1,
        1,
        full_search=True,
    )

    assert calls == [None]
    assert accepted is not None
    assert accepted[0]["label"] == "Correct Omen"
    assert accepted[1] == 0.52


def test_ritual_decorated_context_candidate_requires_margin():
    best = ({"label": "Best"}, 0.52, 0.38, (0, 0))
    close = ({"label": "Close"}, 0.51, 0.37, (0, 0))

    assert uniquescan._accepted_decorated_context_candidate([best, close]) is None


def test_classify_cell_window_caches_by_window_bytes(monkeypatch):
    from poed import scan_cache

    calls = {"n": 0}

    def fake_accept(small, group, slots_w, slots_h, full_search=False):
        calls["n"] += 1
        return None

    monkeypatch.setattr(
        uniquescan, "_accepted_cell_candidate_in_window", fake_accept
    )
    crop = np.full((200, 200, 3), 40, dtype=np.uint8)
    grid = uniquescan.Grid(x0=10, y0=10, cell=60, cols=3, rows=3)
    group = [{"label": "X"}]
    factor = uniquescan.PXSLOT / grid.cell

    scan_cache.clear()
    scan_cache.begin_scan()
    assert uniquescan._classify_cell_window(
        crop, group, grid, 0, 0, 1, 1, factor
    ) is None
    assert calls["n"] == 1
    # Same bytes, next press: decision replayed without matching.
    scan_cache.begin_scan()
    assert uniquescan._classify_cell_window(
        crop, group, grid, 0, 0, 1, 1, factor
    ) is None
    assert calls["n"] == 1
    # Identical bytes but a different (rebuilt) group must not hit.
    assert uniquescan._classify_cell_window(
        crop, [{"label": "X"}], grid, 0, 0, 1, 1, factor
    ) is None
    assert calls["n"] == 2
    # Changed pixels recompute.
    crop[20:30, 20:30] = 200
    assert uniquescan._classify_cell_window(
        crop, group, grid, 0, 0, 1, 1, factor
    ) is None
    assert calls["n"] == 3
    scan_cache.clear()
