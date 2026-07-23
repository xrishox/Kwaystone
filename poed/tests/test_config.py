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


def test_load_migrates_legacy_arb_hotkeys_to_alt_d_monitor(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "# keep this comment\n"
        'hotkey_arb_bridge = "ALT+d"\n'
        'hotkey_arb_monitor = "ALT+f"\n'
    )

    cfg = config.load(p)

    assert cfg["hotkey_arb_monitor"] == "ALT+d"
    assert "hotkey_arb_bridge" not in cfg
    assert p.read_text() == (
        "# keep this comment\n"
        'hotkey_arb_monitor = "ALT+d"\n'
    )
    assert (p.stat().st_mode & 0o777) == 0o600


def test_default_path_is_waystone(tmp_path, monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(config.pathlib.Path, "home", lambda: tmp_path)

    assert config.default_path() == tmp_path / ".config/waystone/config.toml"


def test_default_path_honors_xdg_config_home(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))

    assert config.default_path() == tmp_path / "config/waystone/config.toml"


def test_default_path_migrates_old_dir(tmp_path, monkeypatch):
    """Pre-rename installs keep their config: poe2-overlay/ moves to waystone/."""
    monkeypatch.setattr(config.pathlib.Path, "home", lambda: tmp_path)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
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


def test_save_ocr_settings_replaces_lines_keeps_comments(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text(
        "# my settings\n"
        'ocr_device = "auto"  # hardware default\n'
        'ocr_model_size = "auto"\n'
        'ocr_quantity_model_size = "auto"\n'
    )

    config.save_ocr_settings(
        p,
        device="cuda",
        model_size="small",
        quantity_model_size="medium",
    )

    assert p.read_text() == (
        "# my settings\n"
        'ocr_device = "cuda"  # hardware default\n'
        'ocr_model_size = "small"\n'
        'ocr_quantity_model_size = "medium"\n'
    )


def test_load_validates_ocr_settings(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('ocr_device = "vulkan"\n')

    with pytest.raises(SystemExit, match="ocr_device"):
        config.load(p)


def test_apply_ocr_environment_sets_helper_env(monkeypatch):
    monkeypatch.delenv("WAYSTONE_PADDLE_DEVICE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE", raising=False)
    monkeypatch.delenv("WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE", raising=False)
    cfg = config.AppConfig.from_mapping(
        {
            "ocr_device": "cuda",
            "ocr_model_size": "small",
            "ocr_quantity_model_size": "medium",
        }
    )

    config.apply_ocr_environment(cfg)

    assert config.os.environ["WAYSTONE_PADDLE_DEVICE"] == "gpu:0"
    assert config.os.environ["WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE"] == "small"
    assert config.os.environ["WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE"] == "medium"


def test_load_invalid_toml_exits(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("league = [unclosed\n")

    with pytest.raises(SystemExit):
        config.load(p)


def test_load_validates_known_value_types(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('unique_min_exalted = "many"\n')

    with pytest.raises(SystemExit, match="unique_min_exalted must be a number"):
        config.load(p)


def test_config_keeps_mapping_compatibility_and_validates_mutation():
    cfg = config.AppConfig.from_mapping({"league": "Standard", "custom": True})

    assert cfg["league"] == "Standard"
    assert cfg["custom"] is True
    cfg["league"] = "Hardcore"
    assert cfg.league == "Hardcore"
    with pytest.raises(ValueError):
        cfg["unique_scan_min_price"] = -1


def test_arbitrage_safety_buffer_is_bounded_and_quantized():
    cfg = config.AppConfig.from_mapping({"arb_safety_buffer_percent": 7.4})
    assert cfg.arb_safety_buffer_percent == 7.5

    with pytest.raises(ValueError, match="must not exceed 15"):
        config.AppConfig.from_mapping({"arb_safety_buffer_percent": 15.5})


def test_arbitrage_execution_concession_is_bounded_and_quantized():
    cfg = config.AppConfig.from_mapping(
        {"arb_execution_concession_percent": 7.4}
    )
    assert cfg.arb_execution_concession_percent == 7.5

    with pytest.raises(ValueError, match="must not exceed 15"):
        config.AppConfig.from_mapping(
            {"arb_execution_concession_percent": 15.5}
        )


def test_arbitrage_losing_candidate_filter_requires_a_boolean():
    cfg = config.AppConfig.from_mapping({"arb_show_losing_candidates": True})
    assert cfg.arb_show_losing_candidates is True

    with pytest.raises(ValueError, match="must be a boolean"):
        config.AppConfig.from_mapping({"arb_show_losing_candidates": "true"})


def test_save_league_normalizes_single_quoted_value(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("league = 'Standard'\n# keep me\n", encoding="utf-8")

    config.save_league(p, "Runes of Aldur")

    text = p.read_text()
    # The old regex appended a second league key for non-double-quoted
    # values, producing invalid TOML that killed the next launch.
    assert text.count("league =") == 1
    assert "# keep me" in text
    assert config.load(p)["league"] == "Runes of Aldur"


def test_save_values_writes_atomically_and_privately(tmp_path):
    p = tmp_path / "config.toml"

    config.save_values(p, {"poesessid": "secret", "league": "Standard"})

    assert not (tmp_path / "config.toml.tmp").exists()
    assert (p.stat().st_mode & 0o777) == 0o600
    assert config.load(p)["poesessid"] == "secret"


def test_save_values_preserves_numeric_types(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text("arb_execution_concession_percent = 5.0\n")

    config.save_values(p, {"arb_execution_concession_percent": 7.5})

    assert tomllib.loads(p.read_text())["arb_execution_concession_percent"] == 7.5


def test_load_repairs_loose_permissions_when_sessid_present(tmp_path):
    import os

    p = tmp_path / "config.toml"
    p.write_text('poesessid = "abc"\n', encoding="utf-8")
    os.chmod(p, 0o644)

    config.load(p)

    assert (p.stat().st_mode & 0o777) == 0o600
