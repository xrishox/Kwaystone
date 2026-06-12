# Waystone

Path of Exile 2 price-checking overlay, built Wayland-first for Arch Linux + Hyprland. The name: a [Waystone](https://www.poe2wiki.net/wiki/Waystone) opens PoE2's endgame maps — and this one runs on Wayland.

Exists because existing tools (Exiled Exchange 2, Sidekick) are broken on Wayland — Electron refuses the portal GlobalShortcuts API, so this project goes around Electron entirely.

## Features

- **Price check** (`Alt+Z`): hover an item in-game, press the hotkey, get a layer-shell panel with trade listings or currency exchange rates. Press again to re-check the hovered item, `Esc` to close.
- **Unique scan** (`Alt+X`): scans the screen for unique items and shows click-through price badges, highlighting anything worth more than a configurable exalted threshold.
- Currency and material prices come from [poe2scout](https://poe2scout.com), with the official trade2 bulk exchange as fallback. Gear listings use trade2 search/fetch, rate-limited.

## Architecture

Two processes connected by a Unix socket (JSON-lines protocol):

- **poed** (Python) — everything Wayland/desktop: GTK4 + gtk4-layer-shell overlay, global hotkeys via xdg-desktop-portal GlobalShortcuts, clipboard (`wl-paste`), Ctrl+C injection into the game window (`xdotool`, PoE2 runs under Proton/XWayland), brain process lifecycle.
- **brain** (Node/TypeScript) — everything PoE: item-text parser, trade-query builder, trade2 + bulk API clients, rate limiter, cache. Parser and game data vendored from [Exiled Exchange 2](https://github.com/Kvan7/Exiled-Exchange-2) (MIT) under `brain/vendor/ee2/`.

Hotkey → portal activation → focused-window guard → inject Ctrl+C → read clipboard → send to brain → parsed item priced via trade APIs → result rendered in the overlay panel.

## Requirements

Arch packages:

```
python-gobject gtk4 gtk4-layer-shell xdg-desktop-portal-hyprland wl-clipboard xdotool nodejs
```

Python ≥ 3.12, Hyprland (hotkeys are bound dynamically via `hyprctl` while the PoE2 window exists).

## Setup

```sh
cd brain && npm install
```

Create `~/.config/poe2-overlay/config.toml`:

```toml
league = "Standard"
hotkey_price = "ALT+z"
# account = "your-account-name"
# poesessid = "..."        # optional session cookie — chmod 600 this file
# unique_min_exalted = 1.0 # unique-scan highlight threshold
```

Run the daemon:

```sh
cd poed && python -m poed
```

## Testing

- Brain: `cd brain && npm test` (vitest, golden clipboard samples → expected parse + query JSON).
- poed/protocol: `cd poed && pytest` (round-trips against a live brain child process).

## License

Brain parser and game data vendored from Exiled Exchange 2 under MIT — see `brain/vendor/ee2/PROVENANCE.md`.
