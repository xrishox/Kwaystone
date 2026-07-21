# Kwaystone

Path of Exile 2 price-checking overlay, built Wayland-first for KDE Plasma and Hyprland.

## Install

### AppImage

Download one release artifact:

- `Kwaystone-x86_64.AppImage` — portable CPU build for Intel, AMD, and NVIDIA systems.
- `assemble-kwaystone-nvidia.sh` — downloads and verifies the CUDA 12.6 AppImage for NVIDIA systems. A compatible host NVIDIA driver is required.

Then:

```sh
chmod +x Kwaystone-*.AppImage
./Kwaystone-x86_64.AppImage
```

Both builds include Python 3.13, Node.js, the application libraries, and the OCR model directories Kwaystone uses: PP-OCRv6 small detection, PP-OCRv6 small recognition, and PP-OCRv6 medium recognition. They do not install system packages, modify the desktop, or download OCR models at runtime.

Paddle's self-contained CUDA runtime produces a roughly 3.9 GiB AppImage, while GitHub limits each release asset to 2 GiB. The release therefore stores that AppImage as three verified parts. The assembler downloads the parts, verifies the full AppImage checksum, and makes it executable:

```sh
chmod +x assemble-kwaystone-nvidia.sh
./assemble-kwaystone-nvidia.sh
./Kwaystone-nvidia-x86_64.AppImage
```

The supported baseline is x86_64 Linux with glibc 2.41 or newer and a Wayland session running KDE Plasma or Hyprland. Kwaystone targets current gaming distributions rather than old Linux LTS releases. The compositor, kernel Wayland/FUSE support, and NVIDIA kernel driver are host responsibilities.

KDE installations made through the desktop package use KWin's fast restricted screenshot interface. A directly launched AppImage falls back to the KDE screenshot portal when that permission is unavailable.

### Arch/AUR

The AUR packages install the corresponding release AppImage in extracted form, so they do not require FUSE at runtime. The CUDA package reconstructs the split NVIDIA artifact automatically:

```sh
yay -S waystone        # CPU
yay -S waystone-cuda   # NVIDIA CUDA 12.6
```

The packages conflict because both provide the same `waystone` command and desktop application.

## Features

- **Price check** (`Alt+Z`): hover an item in-game, press the hotkey, and get the embedded Exiled Exchange 2 price-check UI in a narrow native Wayland side panel. Press again to re-check; `Esc`, the EE2 close action, or returning focus to the game closes it.
- **Screen scan** (`Alt+X`): detects supported game panels, including Have trade panels, merchant Buy/Sell stock, Ritual rewards, Expedition rewards, and in-world runeshape remnant rows. Runeshape can combine with another visible scanner result.
- **Currency arbitrage** (`Alt+S`): hover an item (or open it bare) for the live exchange matrix — exalted/chaos/divine cross-rates plus the hovered item priced in every major currency. Bulk-commodity items show their most-liquid market pair; equipment shows live listings grouped by ask currency with each median converted at current rates and spreads of 5%+ flagged. Rates arrive as an instant aggregate snapshot and are then verified live against the official exchange through a rate-limit-aware queue.
- **League tracking**: the control window's league dropdown lists permanent and currently active leagues (softcore and Hardcore), refreshed from the live league list — dead leagues are never shown, and if your tracked league ends, tracking follows the newest league in the same family.

On KDE, the first launch asks once — through the standard GlobalShortcuts
portal dialog — to allow the `Alt+Z`/`Alt+X` shortcuts; later launches reuse
the grant silently. The shortcuts can be viewed and changed any time in
System Settings → Shortcuts.
- Screen-scan currency/material prices come from [poe2scout](https://poe2scout.com) snapshots assembled by the brain (market pair quotes, falling back to the aggregate price). Alt+Z uses the vendored EE2 price-check workflow and the official trade APIs through a local host proxy.

## Screenshots

**Item price check**

![Item price check](screenshots/Item_lookup.png)

**Currency lookup**

![Currency lookup](screenshots/Currency_lookup.png)

**Screen scan**

![Screen scan](screenshots/auto_Scan.png)

## Architecture

Kwaystone runs two child workloads connected by a private Unix socket:

- **poed** (Python) handles GTK4, WebKitGTK, gtk4-layer-shell, compositor integration, screenshots, clipboard access, OCR, and process lifecycle.
- **brain** (Node/TypeScript) handles item parsing, the embedded EE2 host/proxy/config service, scanner market snapshots, rate limiting, and cached game data.

OCR remains a lazy subprocess to isolate its large native runtime, but it uses the same Python executable and environment as the main process. The default OCR settings are hardware-aware: CUDA is used only when available with at least 16 GiB of VRAM, otherwise CPU is used. General recognition defaults to small. Quantity recognition follows the selected recognition size unless both model settings are auto, in which case quantity uses medium only on that large-CUDA profile. Kwaystone reuses the general recognizer when the sizes match and loads a separate quantity recognizer only when they differ, so changing OCR settings requires restarting the app.

User files follow the XDG directories:

- config: `${XDG_CONFIG_HOME:-~/.config}/waystone/config.toml`
- cache: `${XDG_CACHE_HOME:-~/.cache}/waystone`
- state/logs: `${XDG_STATE_HOME:-~/.local/state}/waystone`
- socket: `$XDG_RUNTIME_DIR/waystone-brain.sock`, with a private per-user `/tmp` fallback

## Development

Development and release builds use managed Python 3.13.14. Nothing is installed into the system Python.

Required host tools:

- [uv](https://docs.astral.sh/uv/)
- Node.js 22 and npm
- a C compiler plus GTK4, WebKitGTK 6, Cairo, GObject Introspection, and gtk4-layer-shell development files

On Arch:

```sh
sudo pacman -S --needed base-devel cairo gobject-introspection gtk4 gtk4-layer-shell webkitgtk-6.0 nodejs npm
```

Create a CPU or NVIDIA development environment:

```sh
scripts/bootstrap cpu
# or
scripts/bootstrap nvidia
```

Bootstrap creates `.venv`, installs the hash-locked dependency set, runs `npm ci`, builds the brain, and downloads checksum-verified OCR models into the ignored local cache. Run:

```sh
scripts/run
```

On first launch, Kwaystone creates the private config file with documented defaults.

## Testing

```sh
cd brain && npm test -- --run
cd ../poed && ../.venv/bin/python -m pytest -q
```

The subsystem boundaries and scanner extension rules are documented in
[`docs/architecture.md`](docs/architecture.md). To regression-test scanner
routing against the curated managed scanner corpus, run:

```sh
./scripts/evaluate-scan-corpus
```

Temporary partial regressions are kept separately from the managed corpus when
only part of a scan has been manually verified. They are evaluated with:

```sh
./scripts/evaluate-scan-partials
```

End-to-end scanner latency on curated cases (performance tooling, not
correctness validation) is measured with:

```sh
./scripts/benchmark-screen-scan-latency
```

Optional local debug-image pytest checks are off by default. Enable them only for
manual investigation with `WAYSTONE_TEST_FIXTURES=/path/to/pngs` or
`WAYSTONE_RUN_LOCAL_DEBUG_TESTS=1`; they are not part of corpus validation.

To turn a real scan failure into a permanent regression case, review the
retained frame with `./scripts/latest-scan-data`, then promote it at the
manually confirmed level with `./scripts/promote-scan-case`. The corpus
workflow, hierarchical verification levels, and retention policy are
documented in [`docs/architecture.md`](docs/architecture.md#corpus-and-verification-levels).

Build release artifacts in the pinned Debian 13 container:

```sh
packaging/appimage/build.sh cpu
packaging/appimage/build.sh nvidia
```

Docker with BuildKit or Podman is required. Outputs go to `dist/appimage/` with SHA-256 checksums and SPDX dependency inventories. The build rejects untracked release-critical source files, tracked generated artifacts, missing shared libraries, unexpected OCR files, build-machine paths, and artifacts that cannot fit the release layout. The release workflow divides the NVIDIA build into three balanced, sub-2-GiB assets.

## Release and dependency policy

- Python dependency graphs are hash-locked separately for CPU and CUDA 12.6.
- `npm ci` uses the committed lockfile.
- The container base, Debian package set (snapshot.debian.org timestamp), AppImage tool, Node archive, gtk4-layer-shell source, and OCR model files are pinned and verified.
- OCR models are downloaded only while building/bootstrapping and are bundled in release artifacts.
- Pricing data, trade searches, and item icons remain online features and are cached under the XDG cache directory.
- `scripts/check-source-hygiene --strict` must pass before release builds; it prevents local untracked source from making artifacts that a fresh clone cannot reproduce.
- Dependabot tracks Python, npm, container, and GitHub Actions updates. Major upgrades remain review decisions.

## AI disclosure

This project has been developed with AI assistance.

## License

[AGPL-3.0-or-later](LICENSE), except `brain/vendor/ee2/`, whose renderer, parser, and game data are vendored from Exiled Exchange 2 under MIT. Bundled PaddleOCR models are Apache-2.0.
