import logging
import os
import pathlib
import re
import tomllib
from collections.abc import Iterator, MutableMapping
from dataclasses import asdict, dataclass, field
from typing import Any

_LOG = logging.getLogger("waystone.config")

OCR_DEVICE_VALUES = {"auto", "cpu", "cuda"}
OCR_MODEL_SIZE_VALUES = {"auto", "small", "medium"}

DEFAULTS = {
    "league": "Runes of Aldur",  # verified current league
    "account_name": "",
    "hotkey_price": "ALT+z",       # Ctrl+D's pass-through habits collided with game keys; Alt+Z is free
    "game_window_class": "steam_app_2694490",  # PoE2 under Proton/XWayland.
    "poesessid": "",  # paste from browser devtools (Cookie POESESSID). Grants
                      # account access — keep config.toml private (chmod 600).
    "unique_min_exalted": 1.0,  # screen-scan: highlight items worth >= this (exalted)
    "unique_scan_min_price": 0.0,  # Ritual icon scan: ignore items worth < this (exalted).
        # Shrinks the match corpus for faster scans, at a MEASURED misID
        # cost (2026-06-11): a filtered-out item's cell gets claimed by a
        # surviving lookalike at a believable price — at 2.0 the ritual CT
        # shot relabeled ~2ex Omen of Resurgence as "Omen of Sinistral
        # Erasure 843ex" (the true item rounded just under the filter).
        # With the coarse-to-fine pyramid, full-corpus scans run ~1.2s, so
        # the filter buys ~0.6s for that risk. 0 = off (recommended).
    "ocr_device": "auto",
    "ocr_model_size": "auto",
    "ocr_quantity_model_size": "auto",
}


@dataclass
class AppConfig(MutableMapping[str, Any]):
    """Validated application configuration with mapping compatibility.

    Existing integrations can continue using ``cfg["league"]`` while new code
    gets an explicit, documented schema instead of an unbounded dictionary.
    """

    league: str = DEFAULTS["league"]
    account_name: str = DEFAULTS["account_name"]
    hotkey_price: str = DEFAULTS["hotkey_price"]
    game_window_class: str = DEFAULTS["game_window_class"]
    poesessid: str = DEFAULTS["poesessid"]
    unique_min_exalted: float = DEFAULTS["unique_min_exalted"]
    unique_scan_min_price: float = DEFAULTS["unique_scan_min_price"]
    ocr_device: str = DEFAULTS["ocr_device"]
    ocr_model_size: str = DEFAULTS["ocr_model_size"]
    ocr_quantity_model_size: str = DEFAULTS["ocr_quantity_model_size"]
    extras: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, values: dict[str, Any]) -> "AppConfig":
        merged = dict(DEFAULTS)
        merged.update(values)
        string_fields = (
            "league",
            "account_name",
            "hotkey_price",
            "game_window_class",
            "poesessid",
            "ocr_device",
            "ocr_model_size",
            "ocr_quantity_model_size",
        )
        for name in string_fields:
            if not isinstance(merged[name], str):
                raise ValueError(f"{name} must be a string")
        for name in ("ocr_device", "ocr_model_size", "ocr_quantity_model_size"):
            merged[name] = merged[name].strip().lower()
        if not merged["league"].strip():
            raise ValueError("league must not be empty")
        if merged["ocr_device"] not in OCR_DEVICE_VALUES:
            raise ValueError("ocr_device must be auto, cpu, or cuda")
        for name in ("ocr_model_size", "ocr_quantity_model_size"):
            if merged[name] not in OCR_MODEL_SIZE_VALUES:
                raise ValueError(f"{name} must be auto, small, or medium")
        for name in ("unique_min_exalted", "unique_scan_min_price"):
            value = merged[name]
            if isinstance(value, bool) or not isinstance(value, (int, float)):
                raise ValueError(f"{name} must be a number")
            merged[name] = float(value)
            if merged[name] < 0:
                raise ValueError(f"{name} must be non-negative")
        known = set(DEFAULTS)
        extras = {key: value for key, value in merged.items() if key not in known}
        return cls(
            **{key: merged[key] for key in known},
            extras=extras,
        )

    def as_dict(self) -> dict[str, Any]:
        values = asdict(self)
        extras = values.pop("extras")
        values.update(extras)
        return values

    def __getitem__(self, key: str) -> Any:
        if key in DEFAULTS:
            return getattr(self, key)
        return self.extras[key]

    def __setitem__(self, key: str, value: Any) -> None:
        updated = self.as_dict()
        updated[key] = value
        validated = self.from_mapping(updated)
        for name in DEFAULTS:
            setattr(self, name, getattr(validated, name))
        self.extras = validated.extras

    def __delitem__(self, key: str) -> None:
        if key in DEFAULTS:
            raise KeyError(f"required config key cannot be removed: {key}")
        del self.extras[key]

    def __iter__(self) -> Iterator[str]:
        yield from DEFAULTS
        yield from self.extras

    def __len__(self) -> int:
        return len(DEFAULTS) + len(self.extras)

    def __eq__(self, other: object) -> bool:
        if isinstance(other, AppConfig):
            return self.as_dict() == other.as_dict()
        if isinstance(other, dict):
            return self.as_dict() == other
        return NotImplemented


# Written on first run. Values must mirror DEFAULTS —
# test_created_template_roundtrips_to_defaults pins the sync.
TEMPLATE = """\
# Kwaystone config — created automatically on first run.
# Values below are the defaults; edit and restart to apply.

# Current PoE2 league for price lookups.
league = "{league}"

# Your account name (marks your own listings).
account_name = "{account_name}"

# Price-check hotkey. Screen-scan is ALT+x.
hotkey_price = "{hotkey_price}"

# Window class of the game.
game_window_class = "{game_window_class}"

# Optional session cookie (browser devtools -> Cookie POESESSID).
# Grants account access — this file stays chmod 600 for that reason.
poesessid = "{poesessid}"

# Screen-scan: highlight items worth >= this (exalted).
unique_min_exalted = {unique_min_exalted}

# Ritual icon scan: skip items worth < this (exalted) for faster scans, at a
# measured misidentification risk. OCR screen scans always use the full corpus.
# 0 = off (recommended).
unique_scan_min_price = {unique_scan_min_price}

# OCR runtime. Auto uses CUDA only when CUDA is available and GPU VRAM is at
# least 16 GiB; otherwise it uses CPU.
# Values: "auto", "cpu", "cuda".
ocr_device = "{ocr_device}"

# General OCR recognition model size. Detection stays small.
# Values: "auto", "small", "medium"; auto means small.
ocr_model_size = "{ocr_model_size}"

# Have quantity OCR model preference. Small/medium overrides the general OCR
# model. Auto uses the general OCR model when it is small/medium; if both are
# auto, it uses medium only on CUDA with >=16 GiB VRAM, otherwise small.
# Kwaystone reuses the general recognizer when sizes match and loads a separate
# quantity recognizer only when this setting differs from the general model.
# Values: "auto", "small", "medium".
ocr_quantity_model_size = "{ocr_quantity_model_size}"
"""


def state_home() -> pathlib.Path:
    """XDG state directory root (~/.local/state unless overridden)."""
    return pathlib.Path(
        os.environ.get("XDG_STATE_HOME") or (pathlib.Path.home() / ".local/state")
    )


def migrate_dir(old: pathlib.Path, new: pathlib.Path) -> None:
    """One-shot rename from the pre-Kwaystone dir name; best-effort."""
    if old.is_dir() and not new.exists():
        try:
            new.parent.mkdir(parents=True, exist_ok=True)
            old.rename(new)
            _LOG.info("migrated %s -> %s", old, new)
        except OSError as e:
            _LOG.warning("could not migrate %s -> %s: %s", old, new, e)


def default_path() -> pathlib.Path:
    base = pathlib.Path(
        os.environ.get("XDG_CONFIG_HOME") or pathlib.Path.home() / ".config"
    )
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


def _toml_string(value: str) -> str:
    return '"%s"' % value.replace("\\", "\\\\").replace('"', '\\"')


def save_values(p: pathlib.Path | None, values: dict[str, str]) -> None:
    """Persist selected scalar string values without rewriting user comments."""

    if p is None:
        p = default_path()
    if not p.exists():
        load(p)  # writes the template

    updated = load(p).as_dict()
    updated.update(values)
    AppConfig.from_mapping(updated)

    lines = p.read_text().splitlines(keepends=True)
    remaining = dict(values)
    for i, line in enumerate(lines):
        match = re.match(r"(\s*([A-Za-z0-9_]+)\s*=\s*)(.*?)(\s*(?:#.*)?\n?)$", line)
        if not match:
            continue
        key = match.group(2)
        if key not in remaining:
            continue
        lines[i] = f"{match.group(1)}{_toml_string(remaining.pop(key))}{match.group(4)}"
    for key, value in remaining.items():
        lines.append(f"{key} = {_toml_string(value)}\n")
    p.write_text("".join(lines))


def save_ocr_settings(
    p: pathlib.Path | None,
    *,
    device: str,
    model_size: str,
    quantity_model_size: str,
) -> None:
    save_values(
        p,
        {
            "ocr_device": device,
            "ocr_model_size": model_size,
            "ocr_quantity_model_size": quantity_model_size,
        },
    )


def apply_ocr_environment(cfg: AppConfig) -> None:
    """Apply config-backed OCR env before the helper process starts."""

    device = cfg.ocr_device
    os.environ["WAYSTONE_PADDLE_DEVICE"] = "gpu:0" if device == "cuda" else device
    os.environ["WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE"] = cfg.ocr_model_size
    os.environ["WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE"] = cfg.ocr_quantity_model_size


def load(p: pathlib.Path | None = None) -> AppConfig:
    if p is None:
        p = default_path()
    values: dict[str, Any] = {}
    if p.exists():
        try:
            values = tomllib.loads(p.read_text())
        except tomllib.TOMLDecodeError as e:
            raise SystemExit(f"config error in {p}: {e}")
    else:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.touch(mode=0o600)
        p.write_text(TEMPLATE.format(**DEFAULTS))
        _LOG.info("wrote default config to %s", p)
    try:
        cfg = AppConfig.from_mapping(values)
    except ValueError as e:
        raise SystemExit(f"config error in {p}: {e}") from e
    # The file may hold a POESESSID; enforce owner-only permissions even for
    # files restored or copied in with looser modes (touch() only covers
    # first creation).
    if cfg["poesessid"]:
        try:
            os.chmod(p, 0o600)
        except OSError:
            pass
    return cfg
