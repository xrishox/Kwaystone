import tomllib

import pytest

from poed import config


def test_load_missing_file_creates_template(tmp_path):
    p = tmp_path / "poe2-overlay/config.toml"

    cfg = config.load(p)

    assert p.exists()
    assert cfg == config.DEFAULTS


def test_created_template_roundtrips_to_defaults(tmp_path):
    """Template comments/values must stay in sync with DEFAULTS."""
    p = tmp_path / "config.toml"
    config.load(p)

    assert tomllib.loads(p.read_text()) == config.DEFAULTS


def test_created_template_is_private(tmp_path):
    """poesessid may land in this file later — 0600 from birth."""
    p = tmp_path / "config.toml"
    config.load(p)

    assert p.stat().st_mode & 0o777 == 0o600


def test_load_existing_file_not_overwritten(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('league = "Standard"\n')

    cfg = config.load(p)

    assert cfg["league"] == "Standard"
    assert p.read_text() == 'league = "Standard"\n'


def test_default_path_is_waystone(tmp_path, monkeypatch):
    monkeypatch.setattr(config.pathlib.Path, "home", lambda: tmp_path)

    assert config.default_path() == tmp_path / ".config/waystone/config.toml"


def test_default_path_migrates_old_dir(tmp_path, monkeypatch):
    """Pre-rename installs keep their config: poe2-overlay/ moves to waystone/."""
    monkeypatch.setattr(config.pathlib.Path, "home", lambda: tmp_path)
    old = tmp_path / ".config/poe2-overlay"
    old.mkdir(parents=True)
    (old / "config.toml").write_text('league = "Standard"\n')

    cfg = config.load(config.default_path())

    assert cfg["league"] == "Standard"
    assert not old.exists()
    assert (tmp_path / ".config/waystone/config.toml").read_text() == 'league = "Standard"\n'


def test_save_league_replaces_line_keeps_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "# my settings\n"
        'league = "Standard"  # current league\n'
        'account_name = "kris"\n'
    )

    config.save_league(p, "Runes of Aldur")

    assert p.read_text() == (
        "# my settings\n"
        'league = "Runes of Aldur"  # current league\n'
        'account_name = "kris"\n'
    )


def test_save_league_appends_when_missing(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('account_name = "kris"\n')

    config.save_league(p, "Standard")

    assert config.load(p)["league"] == "Standard"
    assert 'account_name = "kris"' in p.read_text()


def test_save_league_creates_template_when_no_file(tmp_path):
    p = tmp_path / "config.toml"

    config.save_league(p, "Hardcore")

    assert config.load(p)["league"] == "Hardcore"
    assert p.stat().st_mode & 0o777 == 0o600


def test_load_invalid_toml_exits(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("league = [unclosed\n")

    with pytest.raises(SystemExit):
        config.load(p)
