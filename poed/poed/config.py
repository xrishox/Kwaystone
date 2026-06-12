import pathlib
import re
import tomllib

DEFAULTS = {
    "league": "Runes of Aldur",  # verified current league
    "account_name": "",
    "hotkey_price": "ALT+z",       # Ctrl+D's pass-through habits collided with game keys; Alt+Z is free
    "game_window_class": "steam_app_2694490",  # PoE2 under Proton; verify with hyprctl
    "poesessid": "",  # paste from browser devtools (Cookie POESESSID). Grants
                      # account access — keep config.toml private (chmod 600).
    "unique_min_exalted": 1.0,  # unique-scan: highlight items worth >= this (exalted)
    "unique_scan_min_price": 0.0,  # unique-scan: ignore items worth < this (exalted).
        # Shrinks the match corpus for faster scans, at a MEASURED misID
        # cost (2026-06-11): a filtered-out item's cell gets claimed by a
        # surviving lookalike at a believable price — at 2.0 the ritual CT
        # shot relabeled ~2ex Omen of Resurgence as "Omen of Sinistral
        # Erasure 843ex" (the true item rounded just under the filter).
        # With the coarse-to-fine pyramid, full-corpus scans run ~1.2s, so
        # the filter buys ~0.6s for that risk. 0 = off (recommended).
}


# Written on first run. Values must mirror DEFAULTS —
# test_created_template_roundtrips_to_defaults pins the sync.
TEMPLATE = """\
# Waystone config — created automatically on first run.
# Values below are the defaults; edit and restart to apply.

# Current PoE2 league for price lookups.
league = "{league}"

# Your account name (marks your own listings).
account_name = "{account_name}"

# Price-check hotkey. Unique-scan is ALT+x.
hotkey_price = "{hotkey_price}"

# Hyprland window class of the game (verify with: hyprctl activewindow).
game_window_class = "{game_window_class}"

# Optional session cookie (browser devtools -> Cookie POESESSID).
# Grants account access — this file stays chmod 600 for that reason.
poesessid = "{poesessid}"

# Unique-scan: highlight items worth >= this (exalted).
unique_min_exalted = {unique_min_exalted}

# Unique-scan: skip items worth < this (exalted) for faster scans, at a
# measured misidentification risk. 0 = off (recommended).
unique_scan_min_price = {unique_scan_min_price}
"""


def migrate_dir(old: pathlib.Path, new: pathlib.Path) -> None:
    """One-shot rename from the pre-Waystone dir name; best-effort."""
    if old.is_dir() and not new.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
        except OSError:
            pass


def default_path() -> pathlib.Path:
    base = pathlib.Path.home() / ".config"
    migrate_dir(base / "poe2-overlay", base / "waystone")
    return base / "waystone/config.toml"


def save_league(p: pathlib.Path | None, league: str) -> None:
    """Persist the league dropdown choice: surgical line replace so user
    comments and the rest of the file survive."""
    if p is None:
        p = default_path()
    if not p.exists():
        load(p)  # writes the template
    lines = p.read_text().splitlines(keepends=True)
    league_toml = 'league = "%s"' % league.replace("\\", "\\\\").replace('"', '\\"')
    for i, line in enumerate(lines):
        m = re.match(r'(\s*league\s*=\s*"[^"]*")(.*\n?)', line)
        if m:
            lines[i] = league_toml + m.group(2)
            break
    else:
        lines.append(league_toml + "\n")
    p.write_text("".join(lines))


def load(p: pathlib.Path | None = None) -> dict:
    if p is None:
        p = default_path()
    cfg = dict(DEFAULTS)
    if p.exists():
        try:
            cfg.update(tomllib.loads(p.read_text()))
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"config error in {p}: {e}")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(mode=0o600)
        p.write_text(TEMPLATE.format(**DEFAULTS))
    return cfg
