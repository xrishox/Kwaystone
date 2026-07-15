import subprocess
from types import SimpleNamespace

from poed import clipboard
from poed.clipboard import grab_item_text, is_game_focused, _inject_copy

ITEM = "Item Class: Wands\nRarity: Magic\nVolatile Wand of the Apt\n"


def _main_xdotool_command(calls):
    return next(cmd for cmd in calls if cmd[:2] == ["xdotool", "getactivewindow"])


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
    monkeypatch.setattr(clipboard, "_release_modifiers", lambda: None)

    def _raise(*a, **kw):
        raise OSError("not found")

    monkeypatch.setattr(subprocess, "run", _raise)
    assert _inject_copy() is False


def test_read_clipboard_survives_binary_content(monkeypatch):
    """Screenshot-to-clipboard puts PNG bytes there; must not raise (found live:
    UnicodeDecodeError crashed the price worker when clipboard held an image)."""
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **kw: SimpleNamespace(
            returncode=0, stdout=b"\x89PNG\r\n\x1a\n\x00binary", stderr=b""
        ),
    )
    text = clipboard._read_clipboard()
    assert isinstance(text, str)
    assert not clipboard._looks_like_item(text)


# ---------------------------------------------------------------------------
# grab_item_text retry logic
# ---------------------------------------------------------------------------


def test_grab_retries_inject_once_on_first_failure(monkeypatch):
    """If first inject returns False, grab retries once; if retry True, proceeds."""
    monkeypatch.setattr(clipboard, "is_game_focused", lambda gc: True)

    inject_calls = []

    def fake_inject(*_args):
        inject_calls.append(1)
        # First call fails, second succeeds.
        return len(inject_calls) > 1

    monkeypatch.setattr(clipboard, "_inject_copy", fake_inject)
    reads = iter(["stale-old", ITEM])
    monkeypatch.setattr(clipboard, "_read_clipboard", lambda: next(reads))

    result = grab_item_text("steam_app_2694490")

    assert result == ITEM
    assert len(inject_calls) == 2, f"Expected 2 inject calls, got {len(inject_calls)}"


def test_grab_changed_content_accepted(monkeypatch):
    monkeypatch.setattr(clipboard, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(clipboard, "_inject_copy", lambda *_args: True)
    reads = iter(["stale-old", ITEM])
    monkeypatch.setattr(clipboard, "_read_clipboard", lambda: next(reads))
    assert grab_item_text("steam_app_2694490") == ITEM


def test_grab_non_item_rejected(monkeypatch):
    monkeypatch.setattr(clipboard, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(clipboard, "_inject_copy", lambda *_args: True)
    monkeypatch.setattr(clipboard, "_read_clipboard", lambda: "just some chat text")
    assert grab_item_text("steam_app_2694490") is None


def test_grab_unchanged_stale_item_accepted_on_final_retry(monkeypatch):
    monkeypatch.setattr(clipboard, "is_game_focused", lambda gc: True)
    monkeypatch.setattr(clipboard, "_inject_copy", lambda *_args: True)
    # clipboard never changes; same item text before and after every read.
    monkeypatch.setattr(clipboard, "_read_clipboard", lambda: ITEM)
    assert grab_item_text("steam_app_2694490") == ITEM


def test_grab_not_focused_returns_none(monkeypatch):
    monkeypatch.setattr(clipboard, "is_game_focused", lambda gc: False)
    assert grab_item_text("steam_app_2694490") is None


def test_grab_accepts_backend_focus_callback(monkeypatch):
    monkeypatch.setattr(clipboard, "_inject_copy", lambda *_args: True)
    reads = iter(["stale-old", ITEM])
    monkeypatch.setattr(clipboard, "_read_clipboard", lambda: next(reads))

    assert grab_item_text(lambda: True) == ITEM


def test_inject_copy_uses_advanced_copy_chord(monkeypatch):
    """Ctrl+Alt+C (advanced item text) — plain Ctrl+C lacks the mod-info blocks
    the parser needs for prefix/suffix/tier categorisation."""
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _inject_copy()
    joined = " ".join(_main_xdotool_command(calls))
    assert "alt" in joined
    assert "ctrl" in joined


def test_inject_copy_does_not_rely_on_physically_held_hotkey_modifier(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    _inject_copy(("alt",))

    joined = " ".join(_main_xdotool_command(calls))
    assert "ctrl" in joined
    assert "keydown --window %1 alt" in joined
    assert "keyup --window %1 alt" in joined
    assert "key --window %1 --delay 60 c" in joined


def test_inject_copy_releases_modifiers_after_copy(monkeypatch):
    calls = []

    def fake_run(cmd, **kw):
        calls.append(cmd)
        return SimpleNamespace(returncode=0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert _inject_copy() is True
    assert calls[-1][:2] == ["xdotool", "keyup"]
    assert "Alt_L" in calls[-1]
    assert "Control_L" in calls[-1]
