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
        "quoteMaxStock": 40,
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
    assert m0["quoteMaxStock"] == 40
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

