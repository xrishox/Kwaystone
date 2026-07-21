import json
import logging
import subprocess
import time

from poed.subproc import scrubbed_env

_LOG = logging.getLogger("waystone.clipboard")

_POLL_DEADLINE = 1.5
_READ_TIMEOUT = 0.12
_SLEEP = 0.05
_MODIFIER_KEYS = (
    "Alt_L",
    "Alt_R",
    "Control_L",
    "Control_R",
    "Shift_L",
    "Shift_R",
    "Super_L",
    "Super_R",
    "Meta_L",
    "Meta_R",
)


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
                env=scrubbed_env(),
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
            timeout=_READ_TIMEOUT,
            env=scrubbed_env(),
        )
        return r.stdout.decode("utf-8", errors="replace")
    except (OSError, subprocess.SubprocessError):
        return ""


def _release_modifiers() -> bool:
    """Best-effort modifier keyup; warns (never raises) when both tries fail."""
    for attempt in (1, 2):
        try:
            r = subprocess.run(
                ["xdotool", "keyup", *_MODIFIER_KEYS],
                capture_output=True,
                timeout=1.0,
                env=scrubbed_env(),
            )
            if r.returncode == 0:
                return True
        except (OSError, subprocess.SubprocessError):
            pass
        if attempt == 1:
            time.sleep(0.1)
    _LOG.warning(
        "could not release keyboard modifiers; Ctrl/Alt/Shift may be logically stuck"
    )
    return False


def _inject_copy(pressed_modifiers=()) -> bool:
    # keydown/keyup spelled out with a per-key delay: a bare `key ctrl+c` can
    # race the game's input polling under XWayland and deliver 'c' without
    # ctrl — which opens the character panel.
    # %1 is xdotool window-stack syntax: key goes to the window getactivewindow
    # pushed. Small TOCTOU vs the hyprctl guard (user may alt-tab between guard
    # and injection); worst case a stray Ctrl+C lands elsewhere — benign.
    # Ctrl+ALT+C: the game's "advanced item text" copy. Plain Ctrl+C lacks the
    # { Prefix Modifier ... (Tier: N) } blocks the parser needs to categorise
    # mods (prefix/suffix groups, tiers, roll bounds on the card).
    #
    # Do not rely on the physical Alt from Alt+Z still being held. KWin may
    # activate the global shortcut on key release, and then skipping synthetic
    # Alt degrades this to plain Ctrl+C. Release any stale shortcut modifiers
    # first, then synthesize the full copy chord ourselves.
    _release_modifiers()
    time.sleep(0.03)
    chord_mods = ("ctrl", "alt")
    cmd = ["xdotool", "getactivewindow"]
    for mod in chord_mods:
        cmd.extend(["keydown", "--window", "%1", mod])
    cmd.extend(["key", "--window", "%1", "--delay", "60", "c"])
    for mod in reversed(chord_mods):
        cmd.extend(["keyup", "--window", "%1", mod])

    ok = False
    try:
        r = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=2.0,
            env=scrubbed_env(),
        )
        ok = r.returncode == 0
        if not ok:
            _LOG.debug("xdotool copy failed rc=%s stderr=%r", r.returncode, r.stderr)
    except (OSError, subprocess.SubprocessError):
        ok = False
    finally:
        _release_modifiers()
    return ok


def _looks_like_item(text: str) -> bool:
    # Cheap pre-filter; brain's parser is the real validator.
    return text.startswith("Item Class:")


def grab_item_text(game_class_or_focused, pressed_modifiers=()) -> str | None:
    if callable(game_class_or_focused):
        focused = bool(game_class_or_focused())
    else:
        focused = is_game_focused(game_class_or_focused)
    if not focused:
        return None
    baseline = _read_clipboard()
    if not _inject_copy(pressed_modifiers):
        # First inject failed (xdotool non-zero exit); retry once before giving up.
        _inject_copy(pressed_modifiers)
    text = baseline
    deadline = time.monotonic() + _POLL_DEADLINE
    reinjected = False
    while time.monotonic() < deadline:
        text = _read_clipboard()
        if text != baseline and _looks_like_item(text):
            return text
        # The game is a native Wayland client (Wine Wayland driver); our
        # xdotool XTEST chord only reaches it through the XWayland->libei
        # bridge, which can drop a chord (journal: "[xwayland ei] Unhandled
        # event EI_EVENT_SYNC" at press time). One mid-poll re-copy recovers
        # those presses. The first-class replacement would be the XDG
        # RemoteDesktop portal's synthetic-keyboard path.
        if not reinjected and time.monotonic() > deadline - _POLL_DEADLINE / 2:
            reinjected = True
            _inject_copy(pressed_modifiers)
        time.sleep(_SLEEP)
    # Clipboard may not have changed (same item price-checked twice); accept
    # whatever is there now if it still looks like an item.
    if _looks_like_item(text):
        return text
    # Never log clipboard CONTENT: on failure it holds arbitrary user data.
    _LOG.debug("clipboard after copy is not an item: len=%d", len(text))
    return None
