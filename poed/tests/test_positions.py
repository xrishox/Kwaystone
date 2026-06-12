from poed.positions import PositionStore


def test_missing_file_returns_none(tmp_path):
    s = PositionStore(tmp_path / "positions.json")
    assert s.get("panel") is None


def test_set_then_get_roundtrip(tmp_path):
    p = tmp_path / "positions.json"
    s = PositionStore(p)
    s.set("panel", 100, 250)
    assert s.get("panel") == (100, 250)
    # persisted to disk, readable by a fresh store
    assert PositionStore(p).get("panel") == (100, 250)


def test_independent_keys(tmp_path):
    s = PositionStore(tmp_path / "positions.json")
    s.set("panel", 10, 20)
    s.set("login", 30, 40)
    assert s.get("panel") == (10, 20)
    assert s.get("login") == (30, 40)


def test_corrupt_file_degrades_to_empty(tmp_path):
    p = tmp_path / "positions.json"
    p.write_text("{ not json")
    s = PositionStore(p)
    assert s.get("panel") is None
    s.set("panel", 5, 6)            # still writable after a bad read
    assert s.get("panel") == (5, 6)


def test_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "positions.json"
    PositionStore(p).set("panel", 1, 2)
    assert p.exists()
