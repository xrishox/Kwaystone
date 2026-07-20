import numpy as np
import pytest

from poed.ritual_scan import cover, estimate, occupancy
from poed.ritual_scan.stages import Lattice

PITCH = 40.0
PHASE = 13.0


def _grid_frame(
    cols: int = 12,
    rows: int = 10,
    occlusion: tuple[int, int] | None = None,
) -> np.ndarray:
    rng = np.random.default_rng(3)
    height = int(PHASE + rows * PITCH + 60)
    width = int(PHASE + cols * PITCH + 80)
    gray = rng.normal(24, 3, (height, width)).astype(np.float32)
    for k in range(cols + 1):
        x = int(round(PHASE + k * PITCH))
        gray[int(PHASE):int(PHASE + rows * PITCH), x - 1:x + 1] += 60
    for k in range(rows + 1):
        y = int(round(PHASE + k * PITCH))
        gray[y - 1:y + 1, int(PHASE):int(PHASE + cols * PITCH)] += 60
    if occlusion is not None:
        x0, x1 = occlusion
        gray[:, x0:x1] = rng.normal(90, 25, (height, x1 - x0))
    return np.clip(gray, 0, 255).astype(np.uint8)


def test_grid_from_roi_recovers_full_grid():
    gray = _grid_frame()
    lattice, stats = estimate.grid_from_roi(
        gray, 0, 0, gray.shape[1] / 2, min_pitch=30, max_pitch=55
    )
    assert lattice is not None
    assert lattice.cols == 12 and lattice.rows == 10
    assert abs(lattice.pitch_x - PITCH) < 0.6
    assert abs(lattice.x0 - PHASE) < 2.0
    assert abs(lattice.y0 - PHASE) < 2.0


def test_grid_from_roi_survives_occlusion():
    # A tooltip-like occluder over three interior gridlines must not truncate
    # the recovered extent (the failure class behind half-covered panels).
    x0 = int(PHASE + 5 * PITCH + 5)
    gray = _grid_frame(occlusion=(x0, x0 + int(PITCH * 2.5)))
    lattice, _ = estimate.grid_from_roi(
        gray, 0, 0, gray.shape[1] / 2, min_pitch=30, max_pitch=55
    )
    assert lattice is not None
    assert lattice.cols == 12
    assert abs(lattice.pitch_x - PITCH) < 0.8


def test_octave_corrected_pitch_prefers_fundamental():
    n = 1200
    signal = np.zeros(n)
    # Strong lines every 50 px with weaker mid-cell structure every 25 px:
    # the naive harmonic average picks 25, the octave correction must not.
    for x in range(0, n, 25):
        signal[x] = 0.45
    for x in range(0, n, 50):
        signal[x] = 1.0
    pitch, _ = estimate.octave_corrected_pitch(signal, 18, 70)
    assert abs(pitch - 50) < 1.0


class _StubIdentifier:
    def __init__(self, quick: dict, full: dict):
        self._quick = quick
        self._full = full

    def quick_footprint_score(self, window, wh, pitch, top=6):
        return self._quick.get(wh, -1.0)

    def identify_window(self, window, wh, pitch, interior=None):
        entry = self._full.get(wh)
        if entry is None:
            return None, 0.0
        label, score = entry
        return {"label": label, "price": 1.0, "kind": "unique"}, score


def _cover_frame() -> np.ndarray:
    return np.full((260, 260, 3), 30, dtype=np.uint8)


def _lattice(cols: int, rows: int) -> Lattice:
    return Lattice(x0=10, y0=10, pitch_x=50, pitch_y=50, cols=cols, rows=rows)


def test_cover_prefers_true_spanning_item():
    lattice = _lattice(1, 2)
    occupied = np.ones((2, 1), dtype=bool)
    legal = {(1, 1), (1, 2)}
    identifier = _StubIdentifier(
        quick={(1, 1): 0.5, (1, 2): 0.75},
        full={(1, 1): ("small", 0.55), (1, 2): ("tall", 0.80)},
    )
    footprints = cover.identification_cover(
        _cover_frame(), lattice, occupied, legal, identifier
    )
    assert [(f.w, f.h) for f in footprints] == [(1, 2)]


def test_cover_prefers_two_singles_when_they_score_higher():
    lattice = _lattice(1, 2)
    occupied = np.ones((2, 1), dtype=bool)
    legal = {(1, 1), (1, 2)}
    identifier = _StubIdentifier(
        quick={(1, 1): 0.9, (1, 2): 0.4},
        full={(1, 1): ("small", 0.9), (1, 2): ("tall", 0.4)},
    )
    footprints = cover.identification_cover(
        _cover_frame(), lattice, occupied, legal, identifier
    )
    assert [(f.w, f.h) for f in footprints] == [(1, 1), (1, 1)]


def test_cover_spans_sparse_diagonal_over_candidates():
    # A 2x4 staff with diagonal art: only the diagonal cells are core, the
    # corners are expansion candidates; the cover must still produce ONE 2x4.
    lattice = _lattice(2, 4)
    occupied = np.zeros((4, 2), dtype=bool)
    for row in range(4):
        occupied[row, 0 if row < 2 else 1] = True
    candidates = ~occupied
    legal = {(1, 1), (2, 4)}
    identifier = _StubIdentifier(
        quick={(1, 1): 0.2, (2, 4): 0.6},
        full={(2, 4): ("staff", 0.7)},
    )
    footprints, matches = cover.refined_cover(
        _cover_frame(), lattice, occupied, legal, identifier, candidates=candidates
    )
    assert [(f.w, f.h) for f in footprints] == [(2, 4)]
    assert matches[0]["name"] == "staff"


def test_expansion_candidates_require_core_adjacency():
    rng = np.random.default_rng(5)
    side = 16
    pattern = rng.normal(60, 18, (side, side, 3)).astype(np.float32)
    cells = np.tile(pattern, (3, 3, 1, 1, 1)).astype(np.float32)
    core = np.zeros((3, 3), dtype=bool)
    core[1, 1] = True
    cells[1, 1] = 200.0
    smooth = np.full((side, side, 3), 90.0, dtype=np.float32)
    cells[1, 2] = smooth
    cells[0, 0] = smooth * 0.9
    result = occupancy.expansion_candidates(cells, core)
    assert result[1, 2]
    assert not result[0, 0]


def test_legal_footprints_from_rows():
    rows = {
        "a": {"w": 1, "h": 1},
        "b": {"w": 2, "h": 3},
        "c": {"w": 9, "h": 9},
    }
    legal = occupancy.legal_footprints(rows)
    assert (2, 3) in legal and (1, 1) in legal and (9, 9) not in legal


def test_lattice_cell_rect_has_no_cumulative_drift():
    lattice = Lattice(x0=100.4, y0=50.6, pitch_x=105.3, pitch_y=105.3, cols=12, rows=10)
    r_last = lattice.cell_rect(11, 9)
    assert abs((r_last.x + r_last.w) - (lattice.x0 + 12 * lattice.pitch_x)) <= 1
    assert abs((r_last.y + r_last.h) - (lattice.y0 + 10 * lattice.pitch_y)) <= 1


# Shifts stay below PHASE so the first gridline is never cropped out of the
# frame (losing an edge line legitimately re-anchors the grid).
@pytest.mark.parametrize("shift", [5, 11])
def test_grid_from_roi_translation_invariance(shift):
    gray = _grid_frame()
    lattice_a, _ = estimate.grid_from_roi(
        gray, 0, 0, gray.shape[1] / 2, min_pitch=30, max_pitch=55
    )
    shifted = gray[:, shift:]
    lattice_b, _ = estimate.grid_from_roi(
        shifted, 0, 0, shifted.shape[1] / 2, min_pitch=30, max_pitch=55
    )
    assert lattice_a is not None and lattice_b is not None
    assert abs((lattice_a.x0 - shift) - lattice_b.x0) < 1.5
    assert lattice_a.cols == lattice_b.cols
