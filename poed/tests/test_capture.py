import subprocess
from types import SimpleNamespace

import pytest

from poed import capture
from poed.capture import grab_item_text, is_game_focused, _inject_copy

ITEM = "Item Class: Wands\nRarity: Magic\nVolatile Wand of the Apt\n"


def test_guard_parses_hyprctl_json():
    fake = '{"class": "steam_app_2694490", "title": "Path of Exile 2"}'
    assert is_game_focused("steam_app_2694490", _raw=fake)
    assert not is_game_focused("steam_app_2694490", _raw='{"class": "firefox"}')


def test_guard_handles_garbage_json():
    assert not is_game_focused("steam_app_2694490", _raw="not json")


# ---------------------------------------------------------------------------
# _inject_copy return value
# ---------------------------------------------------------------------------


def test_inject_copy_returns_true_on_success(monkeypatch):
    """subprocess.run rc==0 → _inject_copy returns True."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=0, stdout="", stderr=""),
    )
    assert _inject_copy() is True


def test_inject_copy_returns_false_on_nonzero_rc(monkeypatch):
    """subprocess.run rc!=0 → _inject_copy returns False."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(returncode=1, stdout="", stderr="error"),
    )
    assert _inject_copy() is False


def test_inject_copy_returns_false_on_os_error(monkeypatch):
    """OSError (xdotool not found) → _inject_copy returns False."""
    def _raise(*a, **kw):
        raise OSError("not found")
    monkeypatch.setattr(subprocess, "run", _raise)
    assert _inject_copy() is False


# ---------------------------------------------------------------------------
# grab_item_text retry logic
# ---------------------------------------------------------------------------


def test_grab_retries_inject_once_on_first_failure(monkeypatch):
    """If first inject returns False, grab retries once; if retry True, proceeds."""
    monkeypatch.setattr(capture, "is_game_focused", lambda gc: True)

    inject_calls = []

    def fake_inject():
        inject_calls.append(1)
        # First call fails, second succeeds.
        return len(inject_calls) > 1

    monkeypatch.setattr(capture, "_inject_copy", fake_inject)
    reads = iter(["stale-old", ITEM])
    monkeypatch.setattr(capture, "_read_clipboard", lambda: next(reads))

    result = grab_item_text("steam_app_2694490")

    assert result == ITEM
    assert len(inject_calls) == 2, f"Expected 2 inject calls, got {len(inject_calls)}"


def test_grab_changed_content_accepted(monkeypatch):
    monkeypatch.setattr(capture, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(capture, "_inject_copy", lambda: True)
    reads = iter(["stale-old", ITEM])
    monkeypatch.setattr(capture, "_read_clipboard", lambda: next(reads))
    assert grab_item_text("steam_app_2694490") == ITEM


def test_grab_non_item_rejected(monkeypatch):
    monkeypatch.setattr(capture, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(capture, "_inject_copy", lambda: True)
    monkeypatch.setattr(capture, "_read_clipboard", lambda: "just some chat text")
    assert grab_item_text("steam_app_2694490") is None


def test_grab_unchanged_stale_item_accepted_on_final_retry(monkeypatch):
    monkeypatch.setattr(capture, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(capture, "_inject_copy", lambda: True)
    # clipboard never changes; same item text before and after every read.
    monkeypatch.setattr(capture, "_read_clipboard", lambda: ITEM)
    assert grab_item_text("steam_app_2694490") == ITEM


def test_grab_not_focused_returns_none(monkeypatch):
    monkeypatch.setattr(capture, "is_game_focused", lambda gc: False)
    assert grab_item_text("steam_app_2694490") is None
