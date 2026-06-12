import json
import subprocess
import time

_RETRIES = 8
_SLEEP = 0.05


def is_game_focused(game_class: str, _raw: str | None = None) -> bool:
    """True iff the focused window's class matches game_class.

    _raw lets tests inject hyprctl output; in production we shell out.
    Any failure (no hyprctl, bad JSON) degrades to False — never raise.
    """
    if _raw is None:
        try:
            r = subprocess.run(
                ["hyprctl", "activewindow", "-j"],
                capture_output=True,
                text=True,
                timeout=1.0,
            )
            _raw = r.stdout
        except (OSError, subprocess.SubprocessError):
            return False
    try:
        return json.loads(_raw).get("class") == game_class
    except (ValueError, AttributeError):
        return False


def _read_clipboard() -> str:
    # Bytes + lossy decode, NOT text=True: the clipboard can hold arbitrary
    # binary (e.g. a screenshot PNG), and a strict decode raises
    # UnicodeDecodeError out of the price worker. Garbage decodes to garbage
    # and fails the item pre-filter instead.
    try:
        r = subprocess.run(
            ["wl-paste", "--no-newline"],
            capture_output=True,
            timeout=1.0,
        )
        return r.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _inject_copy() -> bool:
    # keydown/keyup spelled out with a per-key delay: a bare `key ctrl+c` can
    # race the game's input polling under XWayland and deliver 'c' without
    # ctrl — which opens the character panel. --clearmodifiers strips the
    # user's still-held physical Ctrl from the equation.
    # %1 is xdotool window-stack syntax: key goes to the window getactivewindow
    # pushed. Small TOCTOU vs the hyprctl guard (user may alt-tab between guard
    # and injection); worst case a stray Ctrl+C lands elsewhere — benign.
    # Ctrl+ALT+C: the game's "advanced item text" copy. Plain Ctrl+C lacks the
    # { Prefix Modifier ... (Tier: N) } blocks the parser needs to categorise
    # mods (prefix/suffix groups, tiers, roll bounds on the card).
    try:
        r = subprocess.run(
            [
                "xdotool", "getactivewindow",
                "keydown", "--window", "%1", "ctrl",
                "keydown", "--window", "%1", "alt",
                "key",     "--window", "%1", "--delay", "60", "c",
                "keyup",   "--window", "%1", "alt",
                "keyup",   "--window", "%1", "ctrl",
            ],
            capture_output=True,
            text=True,
            timeout=2.0,
        )
        return r.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def _looks_like_item(text: str) -> bool:
    # Cheap pre-filter; brain's parser is the real validator.
    return text.startswith("Item Class:")


def grab_item_text(game_class: str) -> str | None:
    if not is_game_focused(game_class):
        return None
    baseline = _read_clipboard()
    if not _inject_copy():
        # First inject failed (xdotool non-zero exit); retry once before giving up.
        _inject_copy()
    text = baseline
    for _ in range(_RETRIES):
        text = _read_clipboard()
        if text != baseline and _looks_like_item(text):
            return text
        time.sleep(_SLEEP)
    # Clipboard may not have changed (same item price-checked twice); accept
    # whatever is there now if it still looks like an item.
    return text if _looks_like_item(text) else None
