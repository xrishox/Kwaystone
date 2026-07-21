# Kwaystone architecture

Kwaystone is split into a GTK/Wayland Python process and a TypeScript brain.
Python owns compositor integration, screenshots, OCR, scanner routing, native
screen-scan UI, and the WebKitGTK window that hosts Alt+Z. The brain owns item
parsing, the embedded Exiled Exchange 2 host/proxy/config service, scanner
market snapshots, quote selection, rate limiting, and cached game data. They
communicate over a private JSON-lines Unix socket.

Scanner price rows come from poe2scout.com snapshots assembled by the brain.
The `uniqueprices` reply is versioned: the brain bumps a snapshot version only
when its corpus rebuilds, and a request carrying `ifVersion` gets
`{version, unchanged: true}` instead of the multi-megabyte row dict when
nothing changed. `poed.scanners.core` caches the last `(league, version,
rows)` per process and re-serves the identical rows object, which downstream
identity caches (template corpus, row filters) key on.

## Alt+Z price checking

Alt+Z is intentionally not a parallel native price-check implementation. The
Python process captures in-game item text, opens a narrow side-panel
layer-shell WebKitGTK surface, and sends the item text to the brain. The brain
serves the vendored EE2 renderer from `brain/vendor/ee2/renderer/dist`, exposes
the upstream browser host endpoints (`/config`, `/proxy`, `/events`), and
forwards `MAIN->CLIENT::item-text` into the renderer. The renderer emits a
small Kwaystone readiness event after its price-check listener is registered so
the host can replay the latest item text without relying on load-time guesses.
EE2 then owns parsing, query construction, item editing, stat toggling, listing
display, and settings UX for the price-check feature.

The EE2 host binds loopback only and is gated by a per-run random token: the
panel URL carries `?k=`, which plants an HttpOnly cookie and redirects away.
All routes (including the `/events` WebSocket upgrade) require that cookie,
and the WebSocket additionally rejects foreign `Origin` headers. `/proxy/` is
not open: targets must be HTTPS on an allowlist (trade API, EE2 data CDN,
price prediction) with request/response size caps and an upstream timeout,
and `set-cookie` responses are never forwarded into the panel. The POESESSID
cookie is attached only to exact `pathofexile.com` hosts (exact/suffix match,
never substring), so it cannot leak to lookalike domains or cleartext.

If native clipboard capture does not return valid item text, Alt+Z must not
open any in-game UI. The failure is logged and the game remains unobstructed.
The embedded EE2 document root is transparent; only visible EE2 widgets should
paint opaque UI.

Kwaystone keeps only a thin adapter around EE2:

- `brain/src/ee2-host.ts` is the browser host/proxy/config service.
- `poed/poed/ee2_overlay.py` is the native WebKitGTK layer-shell host window.
- `poed/poed/price_check.py` connects the Alt+Z hotkey path, clipboard
  capture, EE2 host startup, and native overlay lifecycle.
- `poed/poed/__main__.py` only routes shortcuts and process lifecycle.
  The brain is started from the primary GApplication instance's `on_activate`,
  never before `app.run()` registration, so duplicate launches cannot spawn a
  competing brain that steals or unlinks the shared socket path.

Do not reintroduce separate GTK price cards, Kwaystone-specific Alt+Z query
builders, or duplicate league/settings UI unless there is a documented reason
that cannot be handled by the vendored EE2 UI/library.

## Currency arbitrage (Alt+S)

Alt+S is a two-stage freshness pipeline, split the usual way: poed owns
clipboard capture, hotkey dispatch, and the panel; the brain owns all rate
math. Stage 1 is instant: a forced refresh of the poe2scout exchange-pairs
feed becomes the exchange matrix (exalted/chaos/divine + cross-rates) and,
for bulk-commodity items, the item priced in every major currency against
its most-liquid market pair. Stage 2 refines those rows live: official
exchange and trade search/fetch queries verify the shown pairs, flowing
through `brain/src/scheduler.ts` — a priority queue with per-source minimum
intervals, key-based coalescing, and 429/Retry-After pausing — so repeated
presses can never burst into a ban. poed polls `arbstate` from a worker
thread and flips rows from aggregate to live-verified as answers land.

The absolute scale is per-item, not exalted-by-default: each item is
anchored to its most-liquid quote pair (liquidity and buyer stock are
shown), and conversions chain item → anchor → each currency. Equipment
(non-bulk items) uses one budgeted trade search+fetch per press; listings
group by ask currency, each median is normalized at the current chain rate,
and spreads of 5%+ versus the cheapest currency are flagged. Every row
carries its data source (aggregate vs live) and age so freshness is always
explicit. The brain commands are `arbquote` (stage 1) and `arbstate`
(stage 2); the panel is `poed.arb_check` + `poed.arb_panel`.

## Screen scanning

`poed.scanners.core` captures one game frame, creates one shared
`SceneAnalysis`, probes scanner plugins, and then runs the selected scanner(s).
The current routes are:

1. `have` — movable "I Have" trade panel, detected from repeated item cards.
2. `merchant` — Buy/Sell stock grid, detected from grid layout plus the gold
   title plaque; the located plaque strip is read recognition-only, with a
   full detection+recognition pass as fallback authority.
3. `ritual` — the Favours reward grid, chrome-anchored: a gold text band is
   geometry-gated by a qualifying 12x10 lattice below it, then verified with
   a recognition-only OCR read. Geometry-only acceptance is forbidden, so an
   unavailable OCR helper rejects the probe (rationale and measurements in
   [`docs/ritual-scanner.md`](ritual-scanner.md)).
4. `runeshape` — in-world remnant rune rows; additive and can combine with a
   primary scanner. Strict slot classification is precomputed with a bounded
   thread pool (half the cores; `WAYSTONE_RUNESHAPE_WORKERS` overrides).
5. `expedition` — OCR fallback for Expedition reward text rows. When the
   visual gate accepts on in-world loot labels, OCR reads only the detected
   label bands; the parchment panel keeps the full crop.
6. `combination` — result route when a primary scanner and additive runeshape
   scanner both succeed.
7. `none` — no supported UI detected.

Visual probes must be position- and resolution-independent. A scanner must
return a localized frame rectangle plus evidence, then translate extracted
matches back to output coordinates. Screen-percentage crops are acceptable only
inside a detector that has already established the relevant UI context, and
only when tests prove the crop cannot steal other scanner types.

Scanner internals are intentionally split by responsibility:

- `poed.ocr_worker` owns the persistent PaddleOCR helper subprocess.
- `poed.expedition_text` owns OCR row grouping and catalog name matching.
- `poed.have_scan` owns the Have-panel pipeline: card grid geometry, batched recognition-only name reading, and glyph-verified quantity reading (digit stencils vendored under `poed/data/have_digit_glyphs.*`). Compact
  counts (`11K`, `1,450`, `32.2K`) verify as whole tokens — every digit
  glyph plus any suffix stencil must verify or the read is rejected;
  truncated prefixes are never trusted. Known limitation: quantity glyph
  verification is currently validated only against 4K captures; synthetic
  downscales degrade below ~1440p, and native lower-resolution captures
  are needed before that path can be called resolution-independent.
- `poed.ritual_scan` owns the ritual pipeline: chrome-anchored panel
  localization, occlusion-tolerant lattice fitting, translucency-safe
  feature occupancy, an identification-driven footprint cover, and
  masked-ZNCC identification. Design facts, measured negative results, and
  the known sub-4K name-fidelity limitation are recorded in
  [`docs/ritual-scanner.md`](ritual-scanner.md).
- `poed.ritual_lab` is development tooling only (scoring datasets and CLI via
  `python -m poed.ritual_lab`); production code must not import it. The
  ritual design record, measured negative results, and lab guide live in
  [`docs/ritual-scanner.md`](ritual-scanner.md).
- `poed.uniquescan` owns the market icon template corpus and the shared
  coarse-to-fine matcher used by the merchant scanner; its corpus loader,
  descriptors, and scan thread pool also feed `poed.ritual_scan`.
- `poed.runeshape` owns visual rune-row detection and combination lookup.
- `poed.match_fields` is the one place that copies market row fields into
  match dicts; scanners add only their scanner-specific keys.
- `poed.scan_cache` reuses per-card/cell identification across presses: OCR
  reads and template decisions are keyed by an exact digest of the pixels
  they consume, with a two-generation rotation so entries never outlive the
  previous press. Only identification is reused; prices are rebuilt from the
  current rows every scan.
- `poed.scanners.debug_io` persists debug artifacts (capture PNGs, stage
  images, manifests, retention pruning) on one background writer thread so
  none of that I/O sits between the hotkey press and the painted result.
- `poed.scanners.*` adapts those domain modules to the scanner protocol.
- `Rect` (shared geometry) lives in `poed.image_geometry`; the desktop layer
  re-exports it for its own interfaces.

The OCR helper is persistent and resilient: a wedged (hung, not dead) helper
is terminated and respawned on the next request, startup timeouts reap the
half-started process, and its three-lock discipline (state/spawn/request)
keeps shutdown from ever waiting behind an in-flight OCR call. More broadly,
no blocking I/O runs on the GTK main loop — brain requests, subprocess
spawns, and synchronous D-Bus all run on worker threads with results
delivered via `GLib.idle_add`.

Detection uses PP-OCRv6 small. Recognition model
size is config-driven with hardware-aware auto defaults: CUDA is selected only
when available with at least 16 GiB of VRAM; otherwise CPU is used. General
recognition auto means small. Quantity auto follows an explicit recognition
size; when both model settings are auto, quantity resolves to medium only on
that large-CUDA profile and small otherwise. The helper reuses the general
recognizer when sizes match and creates a separate quantity recognizer only when
the configured sizes differ. Do not add independent model instances for
parallelism; use batching or shared worker improvements instead.

To add a scanner:

- implement the scanner protocol in `poed.scanners`;
- add shared visual preparation to `SceneAnalysis` only when multiple scanners
  can use it;
- keep plugin-specific analysis inside the scanner/domain module otherwise;
- register the scanner before the OCR fallback only when its probe is specific;
- add transformed-position/resolution tests and corpus expectations.

## Corpus and verification levels

Run `scripts/evaluate-scan-corpus` after every scanner change. It evaluates only
active curated cases from the managed scanner corpus. Retained local debug scans
are review inputs, not full-corpus validation inputs.

The scanner writes retained debug attempts to
`${XDG_STATE_HOME:-~/.local/state}/waystone/debug/scans` and keeps the latest 20
completed attempts per saved `selected_scanner` bucket. These directories
contain the captured frame, intermediate stage images, and a manifest; they are
for diagnosis and latest-scan review, not regression truth.

Managed corpus cases are explicitly promoted only after a user-reported failure
or explicit user request. Accidental `Alt+X` scans that correctly route to
`none` stay debug-only unless they are intentionally promoted as negative cases.

Verification is hierarchical:

- Level 1: route/category only.
- Level 2: Level 1 plus exact counts, including runeshape row/glyph counts.
- Level 3: Level 2 plus exact names, stack sizes, and rune sequences.

`multi-rune` is a corpus category, not a scanner route. The route remains the
actual selected scanner, usually `runeshape` or `combination`.

Retention policy keeps a minimum of 10 Level 3, 3 Level 2, and 3 Level 1 active
cases per category. The default active category cap is 16 cases; `ritual` is
allowed 50 active cases because it has the highest historical regression rate.

Use `scripts/latest-scan-data` to review the latest retained scan, then promote
only the level the user confirms:

```sh
scripts/promote-scan-case latest \
  --verification-level 3 \
  --from-current-output \
  --reason "recognized reviewed multi-rune scan"
```

Promoting a scan at a higher level supersedes lower-level entries for the same
source scan. Higher-level truth must not be inferred without user confirmation.
If the user replies only `0`, `1`, `2`, or `3` immediately after the latest-scan
review output, that numeric reply is the reviewed verification level for the
latest scan.
After promotion, run `scripts/evaluate-scan-corpus --record-history` as a
separate full-corpus validation pass.

## Environment variables

Runtime and tuning knobs read by the Python process:

| Variable | Purpose |
| --- | --- |
| `WAYSTONE_DEBUG` | Verbose scan logging. |
| `WAYSTONE_DESKTOP_BACKEND` | Force `kwin` or `hyprland` instead of autodetect. |
| `WAYSTONE_LAYER_SHELL` | Set to `0` to disable gtk4-layer-shell preload. |
| `WAYSTONE_BRAIN_DIR` | Override the brain checkout the launcher runs. |
| `WAYSTONE_PADDLE_PYTHON` | Interpreter for the PaddleOCR helper subprocess. |
| `WAYSTONE_PADDLE_DEVICE` | OCR device override (`cpu`, `gpu:0`). |
| `WAYSTONE_OCR_MODEL_ROOT` | OCR model cache root override. |
| `WAYSTONE_PADDLE_DETECTION_MODEL_SIZE` / `WAYSTONE_PADDLE_RECOGNITION_MODEL_SIZE` / `WAYSTONE_PADDLE_QUANTITY_MODEL_SIZE` | Per-model size overrides (`WAYSTONE_PADDLE_MODEL_SIZE` is the deprecated fallback). |
| `WAYSTONE_PADDLE_RECOGNITION_BATCH_SIZE` | Recognition batch size override. |
| `WAYSTONE_RUNESHAPE_WORKERS` | Runeshape slot-classification pool size. |
| `WAYSTONE_RITUAL_CELL_WORKERS` | Shared scan worker-pool size (merchant matching and ritual hypothesis scoring). |
| `WAYSTONE_SCAN_REVIEW_FIXTURE_ROWS` | Set to `1` to use the tiny deterministic fixture catalog for offline scan review. |
| `WAYSTONE_TEST_FIXTURES` / `WAYSTONE_RUN_LOCAL_DEBUG_TESTS` | Opt-in local test suites. |
| `WAYSTONE_SKIP_SOURCE_HYGIENE` | Bypass release hygiene for local experiments. |

The brain reads `BRAIN_SOCKET`, `POE2_LEAGUE`, `POE2_ACCOUNT`, `POE2_SESSID`,
`POE2_ICON_CACHE`, and `WAYSTONE_BRAIN_REFRESH_MS`.

## Desktop and packaging

Desktop backends expose the same capture/window/shortcut interface. Shared
capture decoding lives in `poed.desktop.capture`; compositor adapters must not
import scanner modules.

Alt+Z/Alt+X are bound through the XDG GlobalShortcuts portal on both
compositors. KDE derives a persistent kglobalaccel component from the stable
session token (`kwaystone`), so the first-run permission grant is reused
silently on later launches and the bindings stay user-editable in System
Settings; Hyprland routes portal-registered shortcut names through dynamic
hyprctl binds that exist only while the game window does. Never call
`org.kde.KGlobalAccel` directly: it is internal KDE RPC, and a malformed
message can kill kglobalacceld — every global shortcut in the session dies
with it. Esc is deliberately different: KDE binds it only while a panel is
visible via a dedicated load/unload KWin script, Hyprland via a dynamic
bind, because a statically bound Esc would be consumed globally and never
reach the game. Focus/panel gating happens at dispatch time in the backend,
never by rebinding keys.

AppImages are built inside the pinned Debian 13 container. CPU and NVIDIA
artifacts include Python, GTK/PyGObject, WebKitGTK, the brain, the built EE2
renderer, OCR model directories, native libraries, licenses, and an SBOM. The
host supplies a recent x86-64 Linux kernel/glibc baseline, FUSE or AppImage
extraction support, a KDE Wayland or Hyprland session, compositor portals/DBus
services, and the GPU driver for the NVIDIA build.

`scripts/check-source-hygiene --strict` protects release builds from local-only
source. AppImage builds fail if release-critical files are untracked or generated
artifacts are tracked. Set `WAYSTONE_SKIP_SOURCE_HYGIENE=1` only for deliberate
local experiments that must not be treated as release artifacts.
