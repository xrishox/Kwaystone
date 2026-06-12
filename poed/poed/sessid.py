"""Read POESESSID from the user's Firefox profile when config has none.

Read-only, single cookie, value never logged. Firefox keeps cookies in
plaintext sqlite; the live DB is WAL-locked by a running browser, so open
with immutable=1 on a tmp copy-free URI read.
"""

import configparser
import os
import sqlite3
from pathlib import Path


def _profiles_from_ini(ff: Path) -> list[Path]:
    """Parse one firefox base dir's profiles.ini, Default=1 first. Missing → []."""
    ini = ff / "profiles.ini"
    if not ini.exists():
        return []
    parser = configparser.ConfigParser()
    try:
        parser.read(ini)
    except configparser.Error:
        return []

    profiles: list[tuple[bool, Path]] = []
    for section in parser.sections():
        if not parser.has_option(section, "Path"):
            continue
        raw = parser.get(section, "Path")
        is_relative = parser.getboolean(section, "IsRelative", fallback=True)
        path = (ff / raw) if is_relative else Path(raw)
        is_default = parser.getboolean(section, "Default", fallback=False)
        profiles.append((is_default, path))

    # Default profile first, otherwise preserve ini order.
    profiles.sort(key=lambda item: not item[0])
    return [path for _, path in profiles]


def firefox_profiles(home: Path | None = None) -> list[Path]:
    """Profile directories from Firefox's profiles.ini, across both base dirs.

    Scans the XDG location (~/.config/mozilla/firefox, the live layout after
    Firefox's XDG migration) first, then the legacy ~/.mozilla/firefox. Each
    profile's relative ``Path=`` resolves against its own base dir. Within a
    base, ``Default=1`` is ordered first. No ini anywhere → [].
    """
    home = home or Path.home()
    xdg_config = Path(os.environ.get("XDG_CONFIG_HOME") or home / ".config")
    bases = [xdg_config / "mozilla/firefox", home / ".mozilla/firefox"]
    profiles: list[Path] = []
    for ff in bases:
        profiles.extend(_profiles_from_ini(ff))
    return profiles


def read_poesessid(db: Path) -> str | None:
    """Newest POESESSID from a Firefox cookie DB, preferring pathofexile.com, or None.

    Read-only via immutable URI (no copy, tolerates a WAL-locked live DB).
    Any sqlite error → None.

    pathofexile.com ONLY: the trade2 + profile APIs exist solely on
    www.pathofexile.com; www.pathofexile2.com is a pure SPA shell with no API,
    so its POESESSID can never authenticate anything — returning it would just
    mask the "paste POESESSID into config" hint with a misleading
    invalid-session error.  (LIKE '%pathofexile.com' also matches the poe2
    suffix, hence the explicit NOT LIKE.)

    Caveat discovered live: pathofexile.com marks POESESSID as a
    browser-session cookie, which Firefox keeps in memory only — it usually
    never reaches this DB. Config paste is the primary workflow; this
    detection is best-effort for setups where it does persist.
    """
    if not Path(db).exists():
        return None
    try:
        con = sqlite3.connect(f"file:{db}?immutable=1", uri=True)
        try:
            row = con.execute(
                "SELECT value FROM moz_cookies "
                "WHERE name='POESESSID' "
                "AND host LIKE '%pathofexile.com' "
                "AND host NOT LIKE '%pathofexile2.com' "
                "ORDER BY lastAccessed DESC LIMIT 1"
            ).fetchone()
        finally:
            con.close()
    except sqlite3.Error:
        return None
    return row[0] if row else None


def autodetect() -> str | None:
    """First POESESSID found across Firefox profiles' cookies.sqlite, or None."""
    for profile in firefox_profiles():
        value = read_poesessid(profile / "cookies.sqlite")
        if value:
            return value
    return None
