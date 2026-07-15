"""Shortcut normalization shared by compositor backends.

Config uses human-friendly strings such as ``ALT+z``.  Hyprland and KWin
want slightly different spellings, so keep that translation in one small
place instead of baking Alt+Z into startup code.
"""

_MODS = {
    "ALT": ("ALT", "Alt"),
    "CTRL": ("CTRL", "Ctrl"),
    "CONTROL": ("CTRL", "Ctrl"),
    "SHIFT": ("SHIFT", "Shift"),
    "SUPER": ("SUPER", "Meta"),
    "META": ("SUPER", "Meta"),
    "WIN": ("SUPER", "Meta"),
}

_KEYS = {
    "ESC": ("Esc", "Escape"),
    "ESCAPE": ("Esc", "Escape"),
    "SPACE": ("Space", "Space"),
    "RETURN": ("Return", "Return"),
    "ENTER": ("Return", "Return"),
    "TAB": ("Tab", "Tab"),
}


def _parts(raw: str) -> tuple[list[tuple[str, str]], str]:
    pieces = [p.strip() for p in str(raw).replace("-", "+").split("+") if p.strip()]
    if not pieces:
        raise ValueError("empty shortcut")
    key = pieces[-1]
    mods = []
    for mod in pieces[:-1]:
        mapped = _MODS.get(mod.upper())
        if mapped is None:
            raise ValueError(f"unknown shortcut modifier: {mod}")
        mods.append(mapped)
    return mods, key


def kwin_trigger(raw: str) -> str:
    """Return a KWin/Qt-style trigger, e.g. ``Alt+Z`` or ``Esc``."""
    mods, key = _parts(raw)
    key_name = _KEYS.get(key.upper(), (key.upper() if len(key) == 1 else key,))[0]
    return "+".join([m[1] for m in mods] + [key_name])


def shortcut_modifiers(raw: str) -> tuple[str, ...]:
    """Return xdotool-style modifier names from a configured shortcut."""
    mods, _key = _parts(raw)
    return tuple(m[0].lower() for m in mods)


def hypr_bind(raw: str) -> tuple[str, str]:
    """Return ``(mods, key)`` for ``hyprctl keyword bind``."""
    mods, key = _parts(raw)
    key_name = _KEYS.get(
        key.upper(), (None, key.upper() if len(key) == 1 else key)
    )[1]
    return "+".join(m[0] for m in mods), key_name.upper()
