import tomllib
import pathlib

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


def load() -> dict:
    p = pathlib.Path.home() / ".config/poe2-overlay/config.toml"
    cfg = dict(DEFAULTS)
    if p.exists():
        try:
            cfg.update(tomllib.loads(p.read_text()))
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"config error in {p}: {e}")
    return cfg
