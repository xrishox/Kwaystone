import json

from poed.loginstate import LoginState, default_path


def test_default_path_is_waystone(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))

    assert default_path() == tmp_path / "waystone/login.json"


def test_default_path_migrates_old_dir(tmp_path, monkeypatch):
    """Pre-rename installs keep the flag: poe2-overlay/ moves to waystone/."""
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    old = tmp_path / "poe2-overlay"
    old.mkdir()
    (old / "login.json").write_text(json.dumps({"logged_in": True}))

    s = LoginState(default_path())

    assert s.logged_in() is True
    assert not old.exists()


def test_missing_file_defaults_to_anonymous(tmp_path):
    s = LoginState(tmp_path / "login.json")
    assert s.logged_in() is False


def test_set_then_get_roundtrip(tmp_path):
    p = tmp_path / "login.json"
    s = LoginState(p)
    s.set(True)
    assert s.logged_in() is True
    # persisted to disk, readable by a fresh state
    assert LoginState(p).logged_in() is True


def test_clear_flag_persists(tmp_path):
    p = tmp_path / "login.json"
    LoginState(p).set(True)
    LoginState(p).set(False)
    assert LoginState(p).logged_in() is False


def test_corrupt_file_degrades_to_anonymous(tmp_path):
    p = tmp_path / "login.json"
    p.write_text("{ not json")
    s = LoginState(p)
    assert s.logged_in() is False
    s.set(True)            # still writable after a bad read
    assert s.logged_in() is True


def test_non_dict_json_degrades_to_anonymous(tmp_path):
    p = tmp_path / "login.json"
    p.write_text("[1, 2, 3]")
    assert LoginState(p).logged_in() is False


def test_non_bool_flag_is_coerced_falsey(tmp_path):
    p = tmp_path / "login.json"
    p.write_text(json.dumps({"logged_in": "yes"}))
    # Only a real JSON true counts as logged in; anything else is anonymous.
    assert LoginState(p).logged_in() is False


def test_creates_parent_dir(tmp_path):
    p = tmp_path / "nested" / "dir" / "login.json"
    LoginState(p).set(True)
    assert p.exists()


def test_set_never_writes_a_session_value(tmp_path):
    """Guard the design invariant: only the boolean flag is on disk."""
    p = tmp_path / "login.json"
    LoginState(p).set(True)
    assert json.loads(p.read_text()) == {"logged_in": True}
