import sqlite3

import pytest

from poed import sessid


@pytest.fixture
def cookie_db(tmp_path):
    """A real throwaway Firefox-style cookie sqlite DB."""
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE moz_cookies ("
        "id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT, "
        "lastAccessed INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies (host, name, value, lastAccessed) "
        "VALUES (?, ?, ?, ?)",
        [
            (".pathofexile.com", "POESESSID", "sess-live", 2),
            (".pathofexile.com", "POESESSID", "sess-old", 1),
            (".example.com", "POESESSID", "wrong", 9),
            (".pathofexile.com", "other", "x", 9),
        ],
    )
    con.commit()
    con.close()
    return db


def test_read_poesessid_returns_newest_matching(cookie_db):
    assert sessid.read_poesessid(cookie_db) == "sess-live"


def test_read_poesessid_ignores_poe2_domain(tmp_path):
    """A .pathofexile2.com cookie alone yields None: the SPA domain has no API,
    so returning its session would mask the paste-into-config hint with a
    misleading invalid-session error."""
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE moz_cookies ("
        "id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT, "
        "lastAccessed INTEGER)"
    )
    con.execute(
        "INSERT INTO moz_cookies (host, name, value, lastAccessed) "
        "VALUES (?, ?, ?, ?)",
        (".pathofexile2.com", "POESESSID", "poe2-sess", 5),
    )
    con.commit()
    con.close()
    assert sessid.read_poesessid(db) is None


def test_read_poesessid_prefers_main_site_over_poe2_domain(tmp_path):
    """The .pathofexile.com cookie must win even when older.

    The trade2 + profile APIs exist only on www.pathofexile.com.
    www.pathofexile2.com is a pure SPA shell with no API — its POESESSID
    cannot authenticate anything.  Priority is by host: the pathofexile.com
    session is the useful one.  The poe2-domain cookie is only a last resort.
    """
    db = tmp_path / "cookies.sqlite"
    con = sqlite3.connect(db)
    con.execute(
        "CREATE TABLE moz_cookies ("
        "id INTEGER PRIMARY KEY, host TEXT, name TEXT, value TEXT, "
        "lastAccessed INTEGER)"
    )
    con.executemany(
        "INSERT INTO moz_cookies (host, name, value, lastAccessed) "
        "VALUES (?, ?, ?, ?)",
        [
            (".pathofexile.com", "POESESSID", "main-site-sess", 1),
            (".pathofexile2.com", "POESESSID", "poe2-sess-newer", 999),
        ],
    )
    con.commit()
    con.close()
    assert sessid.read_poesessid(db) == "main-site-sess"


def test_read_poesessid_missing_file(tmp_path):
    assert sessid.read_poesessid(tmp_path / "nope.sqlite") is None


def test_read_poesessid_garbage_file(tmp_path):
    junk = tmp_path / "junk.sqlite"
    junk.write_bytes(b"this is not a sqlite database at all")
    assert sessid.read_poesessid(junk) is None


def test_firefox_profiles_parses_ini(tmp_path, monkeypatch):
    # Isolate XDG so the host's real ~/.config/mozilla doesn't leak in.
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    ff = tmp_path / ".mozilla/firefox"
    ff.mkdir(parents=True)
    (ff / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=other\n"
        "IsRelative=1\n"
        "Path=abc.other\n"
        "Default=0\n"
        "\n"
        "[Profile1]\n"
        "Name=default\n"
        "IsRelative=1\n"
        "Path=xyz.default\n"
        "Default=1\n"
    )
    profiles = sessid.firefox_profiles(home=tmp_path)
    assert profiles == [ff / "xyz.default", ff / "abc.other"]


def test_firefox_profiles_absolute_path(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    ff = tmp_path / ".mozilla/firefox"
    ff.mkdir(parents=True)
    abs_dir = tmp_path / "elsewhere/prof"
    (ff / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=default\n"
        "IsRelative=0\n"
        f"Path={abs_dir}\n"
        "Default=1\n"
    )
    assert sessid.firefox_profiles(home=tmp_path) == [abs_dir]


def test_firefox_profiles_missing_ini(tmp_path, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / ".config"))
    assert sessid.firefox_profiles(home=tmp_path) == []


def test_firefox_profiles_xdg_only(tmp_path, monkeypatch):
    """XDG migration: only ~/.config/mozilla/firefox exists (the live layout on
    this machine), no legacy ~/.mozilla. firefox_profiles must still find it."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    ff = tmp_path / ".config/mozilla/firefox"
    ff.mkdir(parents=True)
    (ff / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=default\n"
        "IsRelative=1\n"
        "Path=xyz.default\n"
        "Default=1\n"
    )
    assert sessid.firefox_profiles(home=tmp_path) == [ff / "xyz.default"]


def test_firefox_profiles_both_dirs_xdg_first(tmp_path, monkeypatch):
    """Both base dirs present: XDG (live) profiles come first, legacy after.
    Each profile path resolves relative to its own base dir."""
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    xdg = tmp_path / ".config/mozilla/firefox"
    xdg.mkdir(parents=True)
    (xdg / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=xdgprof\n"
        "IsRelative=1\n"
        "Path=xdg.default\n"
        "Default=1\n"
    )
    legacy = tmp_path / ".mozilla/firefox"
    legacy.mkdir(parents=True)
    (legacy / "profiles.ini").write_text(
        "[Profile0]\n"
        "Name=legacyprof\n"
        "IsRelative=1\n"
        "Path=legacy.default\n"
        "Default=1\n"
    )
    assert sessid.firefox_profiles(home=tmp_path) == [
        xdg / "xdg.default",
        legacy / "legacy.default",
    ]


def test_autodetect(monkeypatch, cookie_db):
    profile_dir = cookie_db.parent
    monkeypatch.setattr(sessid, "firefox_profiles", lambda home=None: [profile_dir])
    assert sessid.autodetect() == "sess-live"
