"""Control-window league selector behavior (requires a display)."""

import os

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("DISPLAY"), reason="needs a display (xvfb)"
)

from poed import config  # noqa: E402
from poed.positions import PositionStore  # noqa: E402


class _Brain:
    def request(self, *_args, **_kwargs):
        raise RuntimeError("no brain in tests")


class _Desktop:
    name = "kwin"
    portal_required = False

    def is_game_focused(self):
        return True


def _app(tmp_path, monkeypatch, league="Standard"):
    from poed.__main__ import App

    monkeypatch.setattr(config, "default_path", lambda: tmp_path / "config.toml")
    cfg = config.AppConfig.from_mapping({**config.DEFAULTS, "league": league})
    app = App(None, cfg, _Brain(), PositionStore(tmp_path / "positions.json"), _Desktop())
    app._show_control_window()
    return app


def _dd_items(dd):
    model = dd.get_model()
    return [model.get_string(i) for i in range(model.get_n_items())]


def test_dropdown_fills_with_active_leagues(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    app._fill_league_list(
        ["Standard", "Hardcore", "Runes of Aldur", "HC Runes of Aldur"],
        {
            "Standard": False,
            "Hardcore": False,
            "Runes of Aldur": True,
            "HC Runes of Aldur": True,
        },
        0.0,
        None,
    )

    assert _dd_items(app._league_dd) == [
        "Standard",
        "Hardcore",
        "Runes of Aldur",
        "HC Runes of Aldur",
    ]
    assert app._league_dd.get_selected() == 0


def test_dead_tracked_league_follows_new_league_and_persists(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, league="Fate of the Vaal")

    app._fill_league_list(
        ["Standard", "Hardcore", "Runes of Aldur", "HC Runes of Aldur"],
        {
            "Standard": False,
            "Hardcore": False,
            "Runes of Aldur": True,
            "HC Runes of Aldur": True,
        },
        0.0,
        None,
    )

    assert app.cfg["league"] == "Runes of Aldur"
    assert _dd_items(app._league_dd)[app._league_dd.get_selected()] == "Runes of Aldur"
    assert config.load(tmp_path / "config.toml")["league"] == "Runes of Aldur"
    assert "ended" in app._league_status.get_text()


def test_dead_hc_league_follows_hc_family(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, league="HC Fate of the Vaal")

    app._fill_league_list(
        ["Standard", "Hardcore", "Runes of Aldur", "HC Runes of Aldur"],
        {
            "Standard": False,
            "Hardcore": False,
            "Runes of Aldur": True,
            "HC Runes of Aldur": True,
        },
        0.0,
        None,
    )

    assert app.cfg["league"] == "HC Runes of Aldur"


def test_brain_failure_keeps_current_league(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch, league="Runes of Aldur")

    app._fill_league_list(None, None, 0.0, "boom")

    assert _dd_items(app._league_dd) == ["Runes of Aldur"]
    assert "unavailable" in app._league_status.get_text()
    assert app.cfg["league"] == "Runes of Aldur"


def test_manual_selection_saves_immediately(tmp_path, monkeypatch):
    app = _app(tmp_path, monkeypatch)

    app._fill_league_list(
        ["Standard", "Hardcore", "Runes of Aldur"],
        {"Standard": False, "Hardcore": False, "Runes of Aldur": True},
        0.0,
        None,
    )
    app._league_dd.set_selected(2)

    assert app.cfg["league"] == "Runes of Aldur"
    assert config.load(tmp_path / "config.toml")["league"] == "Runes of Aldur"
