import json
import os
from pathlib import Path

import numpy as np
import pytest

cv2 = pytest.importorskip("cv2")

from conftest import local_debug_tests_enabled
from poed import expeditionscan, unique_grid_geometry, uniquescan
from poed.scanners import core
from poed.scanners import ritual as ritual_scanner_module
from poed.image_geometry import Rect
from poed.image_geometry import frame_source
from poed.scanners.common import SCAN_HISTORY_PER_TYPE
from poed.scanners.scene import SceneAnalysis
from poed.scanners.types import Detection, ScanContext, ScanResult
from poed.scanners.unique_grid import scan_unique_grid


class FakeScanner:
    def __init__(self, sid, confidence=0.0, priority=0.0):
        self.id = sid
        self.title = sid
        self.confidence = confidence
        self.priority = priority
        self.probes = 0
        self.scans = 0

    def probe(self, ctx, scene):
        self.probes += 1
        if self.confidence <= 0:
            return None
        return Detection(self.id, self.confidence)

    def scan(self, ctx, detection):
        self.scans += 1
        return ScanResult(self.id, self.title, [{"name": self.id, "price": 1.0}])

    def warm(self, brain, cfg):
        return

    def stop(self):
        return


class GatedScanner(FakeScanner):
    def __init__(self, sid, confidence=0.0, should_probe=True):
        super().__init__(sid, confidence)
        self.should_probe_result = should_probe
        self.should_probe_calls = []

    def should_probe(self, ctx, scene, *, additive_detected, primary_detected):
        self.should_probe_calls.append((additive_detected, primary_detected))
        return self.should_probe_result


def _ctx(image=None, rows=None):
    shot = image if image is not None else np.zeros((200, 300, 3), np.uint8)
    frame, x0, y0, source = frame_source(shot, None)
    return ScanContext(
        cfg={"league": "L", "unique_scan_min_price": 0.0},
        output="fixture",
        shot=shot,
        frame=frame,
        frame_x=x0,
        frame_y=y0,
        source=source,
        rows=rows or {},
    )



def _select_primary(ctx, scanners=None, scene=None):
    """Winner-selection seam: first selection is the primary scanner."""
    selections = core.select_scanners(ctx, scanners, scene)
    if not selections:
        return None, None
    best = selections[0]
    return best.scanner, best.detection

def test_select_scanner_highest_confidence_wins():
    first = FakeScanner("first", 0.8)
    second = FakeScanner("second", 0.9)

    scanner, detection = _select_primary(_ctx(), [first, second])

    assert scanner is second
    assert detection.scanner_id == "second"
    assert first.probes == 1
    assert second.probes == 1


def test_select_scanner_later_semantic_probe_wins_confidence_tie():
    geometric = FakeScanner("geometric", 0.95)
    semantic = FakeScanner("semantic", 0.95)

    scanner, detection = _select_primary(
        _ctx(), [geometric, semantic]
    )

    assert scanner is semantic
    assert detection.scanner_id == "semantic"


def test_select_scanner_specific_probe_priority_wins_confidence_tie():
    broad_grid = FakeScanner("broad-grid", 0.99, priority=10.0)
    titled_panel = FakeScanner("titled-panel", 0.99, priority=30.0)

    scanner, detection = _select_primary(
        _ctx(), [broad_grid, titled_panel]
    )

    assert scanner is titled_panel
    assert detection.scanner_id == "titled-panel"


def test_select_scanner_returns_none_when_no_probe_matches():
    scanners = [FakeScanner("a"), FakeScanner("b")]

    scanner, detection = _select_primary(_ctx(), scanners)

    assert scanner is None
    assert detection is None
    assert [s.probes for s in scanners] == [1, 1]


def test_scan_unique_grid_applies_context_row_filter(monkeypatch):
    captured = {}

    def fake_scan_region(frame, rows, region, cell, **kwargs):
        captured["rows"] = rows
        return []

    rows = {
        "Keep": {"price": 1.0, "kind": "tagged"},
        "Drop": {"price": 1.0, "kind": "tagged"},
    }
    monkeypatch.setattr(uniquescan, "scan_region", fake_scan_region)

    result = scan_unique_grid(
        _ctx(rows=rows),
        Detection("ritual", 1.0, {"cell": 47}, region=Rect(0, 0, 100, 100)),
        scanner_id="ritual",
        title="ritual rewards",
        include_unknown=True,
        stage_name="result.jpg",
        stage_label="result",
        row_filter=lambda filtered_rows: {"Keep": filtered_rows["Keep"]},
    )

    assert result.matches == []
    assert set(captured["rows"]) == {"Keep"}


def test_ritual_scanner_uses_ritual_candidate_filter(monkeypatch):
    captured = {}

    def fake_scan_unique_grid(ctx, detection, **kwargs):
        captured.update(kwargs)
        return ScanResult("ritual", "ritual rewards", [])

    monkeypatch.setattr(
        ritual_scanner_module,
        "scan_unique_grid",
        fake_scan_unique_grid,
    )

    scanner = ritual_scanner_module.RitualScanner()
    result = scanner.scan(
        _ctx(),
        Detection("ritual", 1.0, {"cell": 47}, region=Rect(0, 0, 100, 100)),
    )

    assert result.matches == []
    assert captured["row_filter"] is uniquescan.filter_ritual_rows


def test_ritual_grid_axis_count_keeps_full_grid_with_hidden_first_boundary():
    pitch = 106.0
    positions = [
        370.0,
        477.0,
        582.0,
        687.0,
        792.0,
        898.0,
        1003.0,
        1108.0,
        1214.0,
        1319.0,
        1424.0,
        1530.0,
    ]

    origin = unique_grid_geometry.best_grid_origin(
        positions,
        pitch,
        unique_grid_geometry.RITUAL_COLS,
        1847,
    )

    assert origin == pytest.approx(261.5, abs=1.0)
    assert unique_grid_geometry.axis_cell_count(
        positions,
        origin,
        pitch,
        unique_grid_geometry.RITUAL_COLS,
        1847,
    ) == 12


def test_ritual_grid_axis_count_trims_windowed_clipped_columns():
    pitch = 69.33333333333333
    positions = [
        3.0,
        15.0,
        33.0,
        107.0,
        175.0,
        245.0,
        312.0,
        381.0,
        450.0,
        519.0,
        588.0,
        657.0,
        725.0,
        794.0,
    ]

    origin = unique_grid_geometry.best_grid_origin(
        positions,
        pitch,
        unique_grid_geometry.RITUAL_COLS,
        1002,
    )

    assert origin == pytest.approx(33.8, abs=1.0)
    assert unique_grid_geometry.axis_cell_count(
        positions,
        origin,
        pitch,
        unique_grid_geometry.RITUAL_COLS,
        1002,
    ) == 11


def test_select_scanners_combines_additive_runeshape_with_primary_panel():
    runeshape = FakeScanner("runeshape", 1.0)
    expedition = FakeScanner("expedition", 1.0)

    selections = core.select_scanners(_ctx(), [runeshape, expedition])

    assert [selection.scanner.id for selection in selections] == [
        "expedition",
        "runeshape",
    ]
    assert runeshape.probes == 1
    assert expedition.probes == 1


def test_select_scanners_skips_expensive_fallback_when_primary_is_definitive():
    primary = FakeScanner("ritual", 1.0)
    runeshape = FakeScanner("runeshape", 0.0)
    fallback = FakeScanner("expedition", 1.0)

    selections = core.select_scanners(_ctx(), [primary, runeshape, fallback])

    assert [selection.scanner.id for selection in selections] == ["ritual"]
    assert primary.probes == 1
    assert runeshape.probes == 1
    assert fallback.probes == 0


def test_select_scanners_honors_contextual_probe_gate():
    runeshape = FakeScanner("runeshape", 1.0)
    fallback = GatedScanner("expedition", 1.0, should_probe=False)

    selections = core.select_scanners(_ctx(), [runeshape, fallback])

    assert [selection.scanner.id for selection in selections] == ["runeshape"]
    assert runeshape.probes == 1
    assert fallback.probes == 0
    assert fallback.should_probe_calls == [(True, False)]


def test_engine_can_run_new_registered_scanner_without_app_changes(
    tmp_path, monkeypatch
):
    shot = np.zeros((200, 300, 3), np.uint8)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class Brain:
        def request(self, msg, timeout=None):
            assert msg["cmd"] == "uniqueprices"
            return {}

    class Desktop:
        def active_game_output(self):
            return "DP-2"

        def capture_output(self, output):
            assert output == "DP-2"
            return shot

        def active_game_rect(self, output, frame_size):
            return None

    custom = FakeScanner("stash", 0.95)

    result = core.run(Brain(), Desktop(), {"league": "L"}, scanners=[custom])

    assert result.result.scanner_id == "stash"
    assert result.result.matches == [{"name": "stash", "price": 1.0}]
    assert custom.scans == 1


def test_engine_merges_additive_runeshape_with_open_window_scanner(
    tmp_path, monkeypatch
):
    shot = np.zeros((200, 300, 3), np.uint8)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class Brain:
        def request(self, msg, timeout=None):
            assert msg["cmd"] == "uniqueprices"
            return {}

    class Desktop:
        def active_game_output(self):
            return "DP-2"

        def capture_output(self, output):
            assert output == "DP-2"
            return shot

        def active_game_rect(self, output, frame_size):
            return None

    expedition = FakeScanner("expedition", 1.0)
    runeshape = FakeScanner("runeshape", 1.0)

    result = core.run(
        Brain(),
        Desktop(),
        {"league": "L"},
        scanners=[runeshape, expedition],
    )

    assert result.result.scanner_id == "combination"
    assert result.result.title == "expedition + runeshape"
    assert result.result.matches == [
        {"name": "expedition", "price": 1.0},
        {"name": "runeshape", "price": 1.0},
    ]
    assert expedition.scans == 1
    assert runeshape.scans == 1


def test_scan_history_is_always_enabled_and_keeps_configured_count_per_type(
    tmp_path, monkeypatch
):
    shot = np.zeros((200, 300, 3), np.uint8)
    monkeypatch.delenv("WAYSTONE_DEBUG", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    class Brain:
        def request(self, msg, timeout=None):
            return {}

    class Desktop:
        def active_game_output(self):
            return "DP-2"

        def capture_output(self, output):
            return shot

        def active_game_rect(self, output, frame_size):
            return None

    for scanner_id in ("expedition", "have", "ritual"):
        for _ in range(SCAN_HISTORY_PER_TYPE + 1):
            result = core.run(
                Brain(),
                Desktop(),
                {"league": "L"},
                scanners=[FakeScanner(scanner_id, 0.95)],
            )
            assert result.result.scanner_id == scanner_id

    from poed.scanners import debug_io

    assert debug_io.flush(timeout=60.0)
    attempts = sorted((tmp_path / "waystone" / "debug" / "scans").glob("scan-*"))
    assert len(attempts) == SCAN_HISTORY_PER_TYPE * 3
    counts = {"expedition": 0, "have": 0, "ritual": 0}
    for attempt in attempts:
        assert (attempt / "00-capture.png").exists()
        assert (attempt / "01-game-frame.png").exists()
        assert (attempt / "99-summary.jpg").exists()
        manifest = json.loads((attempt / "manifest.json").read_text())
        assert manifest["status"] == "complete"
        counts[manifest["selected_scanner"]] += 1
    assert counts == {
        "expedition": SCAN_HISTORY_PER_TYPE,
        "have": SCAN_HISTORY_PER_TYPE,
        "ritual": SCAN_HISTORY_PER_TYPE,
    }


FIXTURES_ENV = os.environ.get("WAYSTONE_TEST_FIXTURES")
FIXTURES = Path(FIXTURES_ENV).expanduser() if FIXTURES_ENV else None


def _fixture(name: str):
    if FIXTURES is None:
        pytest.skip("local screen fixture tests require WAYSTONE_TEST_FIXTURES")
    path = FIXTURES / name
    if not path.exists():
        pytest.skip(f"missing local fixture {path}")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"could not read local fixture {path}")
    return image


def _fixture_rows():
    names = [
        "Orb of Alchemy",
        "Chaos Orb",
        "Exalted Orb",
        "Runic Alloy",
        "Regal Orb",
        "Orb of Augmentation",
        "Greater Orb of Augmentation",
        "Orb of Transmutation",
        "Greater Orb of Transmutation",
        "Greater Regal Orb",
        "Perfect Regal Orb",
        "Greater Chaos Orb",
        "Vaal Orb",
        "Divine Orb",
        "Orb of Chance",
        "Orb of Annulment",
        "Artificer's Orb",
        "Lesser Jeweller's Orb",
        "Greater Jeweller's Orb",
        "Perfect Jeweller's Orb",
        "Transmutation Shard",
        "Regal Shard",
        "Artificer's Shard",
        "Chance Shard",
        "Armourer's Scrap",
        "Blacksmith's Whetstone",
        "Glassblower's Bauble",
        "Gemcutter's Prism",
        "Scroll of Wisdom",
    ]
    return {
        name: {
            "price": float(i + 1),
            "quantity": 100,
            "kind": "tagged",
            "iconPath": None,
            "w": 1,
            "h": 1,
            "trend": None,
        }
        for i, name in enumerate(names)
    }


def _synthetic_grid_panel(*, merchant_title: bool):
    image = np.zeros((900, 1200, 3), np.uint8)
    cv2.rectangle(image, (150, 180), (860, 830), (10, 10, 10), -1)
    pitch = 52
    grid_left = 220
    grid_top = 300
    grid_bottom = 780
    line_color = (62, 58, 46)
    for index in range(10):
        x = grid_left + index * pitch
        cv2.line(image, (x, grid_top), (x, grid_bottom), line_color, 2)
    for index in range(10):
        y = grid_top + index * pitch
        cv2.line(
            image,
            (grid_left, y),
            (grid_left + pitch * 9, y),
            line_color,
            2,
        )
    if merchant_title:
        cv2.rectangle(image, (330, 228), (555, 264), (42, 150, 224), -1)
        cv2.rectangle(image, (330, 228), (555, 264), (25, 90, 150), 2)
        cv2.putText(
            image,
            "BUY OR SELL",
            (354, 253),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.75,
            (25, 20, 10),
            2,
            cv2.LINE_AA,
        )
    return image


def _ritual_rows_from_fixture(image, tmp_path):
    layout = SceneAnalysis(image).ritual
    assert layout is not None
    region = layout.region
    crop = image[region.y:region.y + region.h, region.x:region.x + region.w]
    grid = uniquescan._dynamic_ritual_grid(crop, float(layout.cell or 0))
    assert grid is not None
    occupied = uniquescan._occupied_cells(crop, grid)
    row, col = np.argwhere(occupied)[0]
    x0, y0, x1, y1 = uniquescan._cell_rect(grid, int(col), int(row), pad=0)
    icon = tmp_path / "ritual-cell.png"
    cv2.imwrite(str(icon), crop[y0:y1, x0:x1])
    return {
        "Fixture Ritual Item": {
            "price": 10.0,
            "quantity": 1,
            "kind": "tagged",
            "iconPath": str(icon),
            "w": 1,
            "h": 1,
            "trend": None,
        }
    }


def test_scene_reports_grid_candidates_independently_of_merchant_title():
    titled_grid = SceneAnalysis(_synthetic_grid_panel(merchant_title=True))
    generic_grid = SceneAnalysis(_synthetic_grid_panel(merchant_title=False))

    assert titled_grid.grid_candidates
    assert titled_grid.ritual is not None
    assert generic_grid.grid_candidates
    assert generic_grid.ritual is not None


def test_merchant_title_match_is_semantic():
    from poed.scanners.merchant import _is_buy_or_sell_title

    assert _is_buy_or_sell_title("BUY OR SELL")
    assert _is_buy_or_sell_title("Buy 0r Sell")
    assert not _is_buy_or_sell_title("INVENTORY")
    assert not _is_buy_or_sell_title("FAVOURS")


def test_merchant_title_plaque_gate_rejects_thin_ritual_chrome():
    from poed.scanners.merchant import _buy_sell_title_plaque_box

    cell = 100.0
    merchant = np.zeros((220, 900, 3), np.uint8)
    cv2.rectangle(merchant, (240, 20), (700, 95), (42, 150, 224), -1)
    cv2.rectangle(merchant, (240, 20), (700, 95), (25, 90, 150), 2)

    ritual = np.zeros((220, 900, 3), np.uint8)
    cv2.rectangle(ritual, (200, 0), (700, 22), (42, 150, 224), -1)
    cv2.rectangle(ritual, (330, 90), (570, 170), (5, 5, 5), -1)

    assert _buy_sell_title_plaque_box(merchant, cell) is not None
    assert _buy_sell_title_plaque_box(ritual, cell) is None


@pytest.mark.parametrize(
    "scan",
    [
        "scan-20260627T132655-748790981",
        "scan-20260627T135427-712524853",
    ],
)
def test_merchant_scanner_routes_reported_captures_if_available(scan):
    if not local_debug_tests_enabled():
        pytest.skip("local debug capture tests require WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1")
    path = (
        Path.home()
        / ".local/state/waystone/debug/scans"
        / scan
        / "01-game-frame.png"
    )
    if not path.exists():
        pytest.skip(f"missing local debug capture {path}")
    image = cv2.imread(str(path))
    if image is None:
        pytest.skip(f"could not read local debug capture {path}")

    scene = SceneAnalysis(image)
    scanner, detection = _select_primary(_ctx(image, _fixture_rows()), scene=scene)

    assert scene.grid_candidates
    assert scanner is not None
    assert detection is not None
    assert scanner.id == "merchant"


def test_merchant_scan_uses_fast_shared_matching_without_unknown_badges(monkeypatch):
    from poed.scanners.merchant import MerchantScanner

    calls = {}

    def fake_scan_region(
        frame,
        rows,
        region,
        cell,
        include_unknown=True,
        matching_mode="cells",
        gray_thresh=0.72,
        color_thresh=0.75,
    ):
        calls["include_unknown"] = include_unknown
        calls["matching_mode"] = matching_mode
        calls["gray_thresh"] = gray_thresh
        calls["color_thresh"] = color_thresh
        return []

    monkeypatch.setattr(uniquescan, "scan_region", fake_scan_region)
    ctx = _ctx(np.zeros((120, 120, 3), np.uint8), rows={})
    detection = Detection(
        "merchant",
        1.0,
        {"cell": 52.0},
        region=Rect(0, 0, 100, 100),
    )

    result = MerchantScanner().scan(ctx, detection)

    assert calls["include_unknown"] is False
    assert calls["matching_mode"] == "shared"
    assert calls["gray_thresh"] == 0.70
    assert calls["color_thresh"] == 0.73
    assert result.scanner_id == "merchant"
    assert result.matches == []


def test_merchant_stock_scan_region_keeps_edge_columns_visible():
    from poed.scanners.merchant import _stock_scan_region
    from poed.scanners.scene import LayoutEvidence

    frame = np.zeros((2160, 3840, 3), np.uint8)
    layout = LayoutEvidence(
        confidence=1.0,
        region=Rect(680, 526, 1410, 1375),
        score=8,
        cell=104.0,
        details=("regular-grid-lines=8", "cell-pitch=104.0"),
    )

    region = _stock_scan_region(frame, layout)
    expected_left = layout.region.x - round(float(layout.cell or 0) * 2.0)

    assert region.x == expected_left
    assert region.y < layout.region.y
    assert region.x + region.w > layout.region.x + layout.region.w
    assert region.y + region.h > layout.region.y + layout.region.h


def test_expedition_probe_rejects_titleless_loose_world_label_hits(monkeypatch):
    from poed.scanners.expedition import ExpeditionScanner
    from poed.scanners import expedition as expedition_module

    lines = [
        expeditionscan.OcrLine("Orb of Augmentation", 0.99, (10, 10, 210, 40)),
        expeditionscan.OcrLine("Scroll of Wisdom", 0.99, (10, 80, 210, 110)),
        expeditionscan.OcrLine("Regal Orb", 0.99, (10, 150, 210, 180)),
    ]
    monkeypatch.setattr(expedition_module.ocr, "read_lines", lambda crop: lines)

    detection = ExpeditionScanner().probe(
        _ctx(
            np.zeros((600, 900, 3), np.uint8),
            rows=_fixture_rows(),
        ),
        None,
    )

    assert detection is None


def test_expedition_additive_gate_requires_panel_visual(monkeypatch):
    from poed.scanners.expedition import ExpeditionScanner
    from poed.scanners import expedition as expedition_module

    scanner = ExpeditionScanner()
    ctx = _ctx(np.zeros((600, 900, 3), np.uint8))
    monkeypatch.setattr(
        expedition_module,
        "_visual_ocr_gate",
        lambda crop: {
            "accepted": True,
            "reason": "prominent-labels",
            "parchmentRatio": 0.0,
            "prominentLabelRows": 5,
        },
    )

    assert scanner.should_probe(
        ctx,
        None,
        additive_detected=False,
        primary_detected=False,
    )
    assert not scanner.should_probe(
        ctx,
        None,
        additive_detected=True,
        primary_detected=False,
    )

    monkeypatch.setattr(
        expedition_module,
        "_visual_ocr_gate",
        lambda crop: {
            "accepted": True,
            "reason": "parchment",
            "parchmentRatio": 0.55,
            "prominentLabelRows": 0,
        },
    )

    assert scanner.should_probe(
        ctx,
        None,
        additive_detected=True,
        primary_detected=False,
    )


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ritual.png", "ritual"),
        ("have.png", "have"),
        ("expedition.png", "expedition"),
    ],
)
def test_fixture_routes_to_expected_scanner(filename, expected, tmp_path):
    image = _fixture(filename)
    rows = _ritual_rows_from_fixture(image, tmp_path) if expected == "ritual" else _fixture_rows()
    try:
        scanner, detection = _select_primary(_ctx(image, rows))
    except expeditionscan.OcrUnavailable as e:
        pytest.skip(str(e))

    assert scanner is not None
    assert detection is not None
    assert scanner.id == expected


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("ritual.png", "ritual"),
        ("have.png", "have"),
        ("expedition.png", "expedition"),
    ],
)
def test_fixture_scanner_returns_non_empty_matches(filename, expected, tmp_path):
    image = _fixture(filename)
    rows = _ritual_rows_from_fixture(image, tmp_path) if expected == "ritual" else _fixture_rows()
    ctx = _ctx(image, rows)
    try:
        scanner, detection = _select_primary(ctx)
        assert scanner is not None and detection is not None
        result = scanner.scan(ctx, detection)
    except expeditionscan.OcrUnavailable as e:
        pytest.skip(str(e))

    assert result.scanner_id == expected
    assert result.matches


def test_visual_layout_detection_survives_translation_and_scale():
    image = _fixture("have.png")
    layout = SceneAnalysis(image).have
    assert layout is not None
    panel = image[
        layout.region.y:layout.region.y + layout.region.h,
        layout.region.x:layout.region.x + layout.region.w,
    ]

    for factor, offset in ((0.72, (80, 140)), (1.0, (260, 40)), (1.18, (30, 90))):
        moved = cv2.resize(
            panel,
            None,
            fx=factor,
            fy=factor,
            interpolation=cv2.INTER_AREA if factor < 1 else cv2.INTER_CUBIC,
        )
        canvas = np.zeros(
            (
                max(1200, offset[1] + moved.shape[0] + 80),
                max(2200, offset[0] + moved.shape[1] + 80),
                3,
            ),
            dtype=np.uint8,
        )
        x, y = offset
        canvas[y:y + moved.shape[0], x:x + moved.shape[1]] = moved

        detected = SceneAnalysis(canvas).have

        assert detected is not None
        assert detected.score >= 8


def test_retained_debug_scans_have_unambiguous_visual_routing():
    if not local_debug_tests_enabled():
        pytest.skip("local debug capture tests require WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1")
    root = Path.home() / ".local/state/waystone/debug/scans"
    checked = 0
    for manifest_path in sorted(root.glob("scan-*/manifest.json")):
        frame_path = manifest_path.parent / "01-game-frame.png"
        if not frame_path.exists():
            continue
        manifest = json.loads(manifest_path.read_text())
        expected = manifest.get("selected_scanner")
        image = cv2.imread(str(frame_path))
        if image is None:
            continue
        scene = SceneAnalysis(image)
        if expected == "have":
            assert scene.have is not None, manifest_path.parent.name
        elif expected == "ritual":
            assert scene.have is None, manifest_path.parent.name
            assert scene.ritual is not None, manifest_path.parent.name
        elif expected == "expedition":
            assert scene.have is None, manifest_path.parent.name
            assert scene.ritual is None, manifest_path.parent.name
        else:
            continue
        checked += 1
    if not checked:
        pytest.skip("no retained local debug scans with supported routes")
