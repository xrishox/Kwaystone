# Agent Instructions

## Mirror-file rule

- `AGENTS.md` and `CLAUDE.md` are intentionally identical. Any change to one must be made to both in the same patch.
- These files are the source of truth for agent behavior after context resets. If another document disagrees with these instructions, either update the stale document or ask before relying on it.
- `scripts/check-source-hygiene --strict` enforces that `AGENTS.md` and `CLAUDE.md` stay byte-identical.

## Engineering quality

- Prefer generalized, maintainable solutions over narrow patches. When fixing a bug, identify the underlying class of failure and address that class rather than adding item-specific, screen-specific, model-specific, or date-specific carve-outs.
- Do not stack exception after exception. If a fix requires another special case, stop and reassess the design before adding it.
- Actively minimize technical debt. Keep code paths simple, testable, and explainable; remove obsolete or duplicated logic when it is safe to do so.
- Preserve correctness across arbitrary resolutions, UI positions, windowed/fullscreen layouts, and supported scanner types unless a limitation is explicitly documented and accepted.
- Treat scanner speed and scanner quality as coupled requirements. Every scanner change must consider performance implications, especially added OCR passes, template searches, nested loops, full-frame scans, debug writes, or broad fallback paths. Prefer designs that improve detection quality by reducing the search space, caching reusable work, sharing precomputed features, and applying expensive verification only after cheap high-recall candidate generation.
- Do not accept a correctness fix that creates avoidable latency. If a robust fix appears expensive, redesign the pipeline or add staged filtering instead of piling expensive fallback scans onto the hot path. Report expected runtime impact and validate it when practical.
- Add regression tests for the generalized behavior being fixed, not only for the one screenshot or item that exposed the bug.
- Do not promote scans into the managed corpus automatically. Only add a case when the user identifies a real failure or explicitly asks for a scan to become a regression case. Accidental `none` scans from stray Alt+X presses must remain debug-only unless explicitly marked as intentional negative cases.
- Release/build work must not depend on untracked local files. Run `scripts/check-source-hygiene --strict` before AppImage/release builds; only bypass it with `WAYSTONE_SKIP_SOURCE_HYGIENE=1` for deliberate local experiments.

## Current architecture boundaries

- Python (`poed`) owns GTK4/gtk4-layer-shell UI, compositor backends, capture, OCR, scanner routing, native Alt+X result UI, the WebKitGTK Alt+Z host window, config/state, and process lifecycle.
- TypeScript (`brain`) owns PoE item parsing, the embedded Exiled Exchange 2 host/proxy/config service, trade/bulk API requests, rate limiting, poe2scout data, quote selection, and vendored game data.
- Alt+Z price check is the vendored Exiled Exchange 2 workflow hosted inside a native WebKitGTK layer-shell side panel. Python owns clipboard capture and native window lifecycle through `poed.price_check` and `poed.ee2_overlay`; the brain owns the EE2 host/proxy/config endpoints in `brain/src/ee2-host.ts`. The EE2 host is loopback-only and gated by a per-run auth token (planted as an HttpOnly cookie via the `?k=` URL handed to the WebView); `/proxy/` accepts only allowlisted HTTPS hosts (trade API, EE2 data CDN, price prediction) with timeouts and size caps, and the POESESSID cookie is attached only to exact `pathofexile.com` hosts — never to lookalike or plaintext targets.
- Do not reintroduce the old native GTK Alt+Z price cards, login window, league selector window, or Kwaystone-specific trade query builders. League/settings/account UI for Alt+Z should flow through EE2 unless there is a documented reason it cannot.
- Alt+X screen scan remains native GTK/layer-shell UI. `poed.scan_ui` owns the transient result panel and pointer-transparent badges; `poed.badges`, `poed.overlay`, and `poed.views` render the scan output.
- Alt+X and Alt+Z both hide when focus returns to the game after the overlay/panel has been shown. Esc/panel-close should still dismiss visible Kwaystone UI.
- Desktop backends provide capture, active-game geometry, focus tracking, and hotkey binding. Desktop backend modules must not import scanner modules.
- Global hotkeys go through the XDG GlobalShortcuts portal on both compositors (KDE: stable `kwaystone` session token so the first-run grant persists and later launches rebind silently; Hyprland: dynamic hyprctl binds scoped to the game window). Never call `org.kde.KGlobalAccel` directly — it is internal KDE RPC and a malformed message can kill kglobalacceld, taking every global shortcut in the session with it. Esc (`panel-close`) is bound only while a panel is visible (KDE: dedicated load/unload KWin script; Hyprland: dynamic bind); a statically bound Esc would be swallowed globally and never reach the game. Focus/panel gating happens at dispatch time, never by rebinding keys.
- The brain must be started only from the primary GApplication instance (`on_activate`), never before `app.run()` registration: a duplicate launch that spawned a brain would steal/unlink the shared socket path from under the running instance. Symmetrically, `Brain.stop()` must not unlink the socket unless this process actually spawned a brain.
- No blocking I/O on the GTK main loop: brain socket requests, subprocess spawns (hyprctl/xdotool/wl-paste), and synchronous D-Bus calls belong on worker threads with results delivered via `GLib.idle_add`. Poll loops (`ee2state`, focus tracking, cursor tracking) live on dedicated threads. The PaddleOCR helper has a three-lock discipline (state/spawn/request): `stop()` and shutdown paths never wait behind an in-flight OCR call, and a wedged helper is terminated and respawned rather than reused.
- Scanner plugins live under `poed.scanners`; reusable domain logic belongs in focused modules such as `ocr_worker`, `expedition_text`, `have_scan`, `ritual_scan`, `uniquescan`, and `runeshape`.
- The ritual route is owned by `poed.ritual_scan`; its probe must never accept on grid geometry alone — chrome plus OCR verification is required (measured: 52/112 false fires without it, 0/112 with). `poed.ritual_lab` is scoring tooling only: production code must not import it, and lab datasets are never corpus truth. Design record: `docs/ritual-scanner.md`.
- Cross-cutting scan infrastructure lives at the domain level: `poed.match_fields` (the single source for market fields copied into match dicts), `poed.scan_cache` (cross-press identification reuse keyed by exact pixel digests), and `poed.image_geometry` (shared `Rect` and frame geometry). Debug persistence is asynchronous through `poed.scanners.debug_io`; scan results must never wait on debug writes. Domain modules must not import `poed.scanners` or `poed.desktop` internals.

## Current tracked script inventory

- `scripts/bootstrap`: creates the managed Python 3.13 development environment, installs Node dependencies, builds the brain, and fetches OCR models.
- `scripts/run`: runs the development checkout using the managed environment.
- `scripts/fetch-ocr-models`: downloads checksum-verified OCR models listed by `packaging/appimage/models.sha256`.
- `scripts/update-runeshape-data`: updates vendored runeshape combination data.
- `scripts/latest-scan-data`: reviews one retained debug scan, normally the latest, and prints Level 1/2/3 truth candidates.
- `scripts/promote-scan-case`: promotes a manually reviewed retained scan into the managed corpus at the user-confirmed verification level.
- `scripts/evaluate-scan-corpus`: evaluates every active managed corpus case at its stored verification level; it must not scan retained debug dumps.
- `scripts/evaluate-scan-partials`: evaluates temporary partial regressions under `poed/tests/fixtures/scanner-partials/`; these are not managed corpus truth.
- `scripts/maintain-scan-corpus`: graduates probation cases and enforces managed corpus retention policy after recorded corpus runs.
- `scripts/benchmark-screen-scan-latency`: measures end-to-end scanner/price-row latency on curated cases and optional retained debug scans. It is performance tooling, not correctness validation.
- `scripts/check-source-hygiene`: rejects untracked release-critical files, tracked generated files, AGENTS/CLAUDE drift, missing README screenshots, brain lock drift, and Python-pin disagreement before release builds.
- `scripts/_common.py`: shared bootstrap (managed-venv re-exec, poed import path) for the Python scripts; not directly invocable.

Do not delete a tracked script unless its README/CI/packaging/test references are removed or replaced in the same change.

## Scanner and debug workflow

- Every scanner change must be validated with `scripts/evaluate-scan-corpus` before handoff. This scans only active managed corpus cases, both probation/pre-graduate and graduated. It must not scan retained debug dumps.
- The app retains the latest 20 completed debug scan attempts per saved `selected_scanner` bucket under `${XDG_STATE_HOME:-~/.local/state}/waystone/debug/scans`. These retained scans are investigation inputs only; they are not the managed corpus.
- Debug scan directories contain the captured frame, game-frame crop, intermediate stage images, and `manifest.json`. Use them to diagnose, but do not infer permanent Level 2/3 corpus truth from them without user confirmation.
- Temporary partial regressions live under `poed/tests/fixtures/scanner-partials/` and run with `scripts/evaluate-scan-partials`. Use them only when the user explicitly verifies some facts but says the rest is provisional. They must report contradictions in provisional rows and must not silently update truth or count as managed corpus cases.
- Normal scanner validation after code changes should include focused tests for the changed module, lint for changed Python/scripts, and the managed corpus evaluator. For broad scanner refactors, run:

```sh
cd poed
../.venv/bin/ruff check poed tests ../scripts/evaluate-scan-corpus ../scripts/evaluate-scan-partials ../scripts/promote-scan-case ../scripts/maintain-scan-corpus ../scripts/latest-scan-data ../scripts/check-source-hygiene ../scripts/update-runeshape-data
../.venv/bin/python -m pytest -q
cd ..
scripts/evaluate-scan-corpus
```

## Scanner corpus verification levels

- Corpus verification is hierarchical:
  - Level 0: skip corpus truth update for now.
  - Level 1: routing only. The stored `expected` scanner route must pass.
  - Level 2: Level 1 plus exact counts. The stored `expectedCounts` must pass. For runeshape or multi-rune cases, stored rune row/glyph counts must also pass.
  - Level 3: Level 1 and Level 2 plus full semantic truth. Stored item names, stack sizes when meaningful, and runeshape rune sequences must pass.
- A higher level can never pass if a lower level fails. Level 3 implicitly requires Level 2 and Level 1 to pass; Level 2 implicitly requires Level 1 to pass.
- After fixing a user-reported scan failure and running tests, ask the user which level the fix satisfies:
  - Level 0: skip for now.
  - Level 1: route is correct.
  - Level 2: route and counts are correct.
  - Level 3: route, counts, names, stack sizes, and rune data are fully correct.
- When the user confirms a level, update the corpus case to store all truth required for that level:
  - Level 1 stores route/category only.
  - Level 2 stores route/category plus count truth; multi-rune stores runeshape row count and rune-glyph counts.
  - Level 3 stores all Level 2 truth plus exact names, stack sizes when meaningful, and full runeshape rune sequences.
- Never store Level 2 or Level 3 truth from current scanner output without user confirmation. Level 3 data becomes permanent regression truth and must be treated as manually reviewed.
- `multi-rune` is a corpus category, not a scanner route. The route is still the real selected scanner, usually `runeshape` or `combination`.
- Maintain at least 10 active Level 3 cases, 3 active Level 2 cases, and 3 active Level 1 cases for every corpus category, including `have`, `ritual`, `expedition`, `merchant`, `runeshape`, `multi-rune`, and `combination`. If the corpus lacks enough cases, report the deficit; do not pretend the floor is satisfied.
- The normal active corpus cap is 16 cases per category. Ritual is intentionally allowed 50 active cases because its grid/icon recognition is the most regression-prone scanner. Do not reduce Ritual back to the default cap unless the user explicitly asks.

## Latest scan review workflow

- If the user says something like `what's the latest scan`, `scan result`, `give me the latest scan data`, or asks to review the latest scan, run `scripts/latest-scan-data`. This is the only step that reads the latest retained debug frame directly. Do not run the full corpus test before showing this output.
- Feed the user the Level 1/2/3 review output in plain language:
  - Level 1 shows the current route and suggested corpus category.
  - Level 2 shows exact match counts and runeshape row/glyph counts when present.
  - Level 3 shows exact names, stack sizes when meaningful, and full runeshape rune sequences.
- Ask the user what level is correct, including Level 0 as the option to skip corpus updates for now.
- If the immediately previous assistant message showed latest-scan review output and the user replies only `0`, `1`, `2`, or `3`, treat that as the verification-level answer for that latest scan.
- When the user replies with the verified level:
  - Level 0: do not add or update the corpus. If a command is useful for bookkeeping, `scripts/promote-scan-case latest --verification-level 0` is a no-op skip.
  - Level 1/2/3: promote the latest scan with `scripts/promote-scan-case latest --verification-level <level> --from-current-output --reason "<short reason>"`. The script reruns the latest scan, infers the route/category from the current scanner output, copies the frame into `poed/tests/fixtures/scanner-corpus/images/`, and stores the case in `poed/tests/fixtures/scanner-corpus/index.json` at the confirmed level.
  - If the expected result is intentionally `none`, pass `--intentional-negative`; otherwise accidental `none` scans must stay out of the corpus.
- After promotion or a Level 0 skip, run a separate full corpus validation with `scripts/evaluate-scan-corpus --record-history`. This command scans only the curated managed corpus, never `~/.local/state/waystone/debug/scans` or `~/.local/state/waystone/debug/tests`.
- Report the full corpus result to the user. If it fails, include the failed case id/path, expected route, actual route, failed tier, and reason.
- After a successful recorded full corpus run, run `scripts/maintain-scan-corpus` when maintaining probation/graduation/retention state is relevant; this is separate from the corpus evaluator.
- Promoting a scan at a higher level supersedes lower-level entries for that same `sourceScanId`. The promotion script removes lower/equal-level corpus entries and their copied corpus images before writing the upgraded case. Do not keep duplicate Level 1/2 versions when the same scan has been upgraded to Level 2 or Level 3.
- If a higher-level active case already exists for the same `sourceScanId`, do not downgrade it. The promotion script refuses this automatically.
- The corpus evaluator has exactly one job: test every active curated corpus case at its stored verification level. It must not infer new truth from debug images, promote images, or scan local debug dumps.

## Release hygiene

- Use `scripts/check-source-hygiene --strict` before release/AppImage builds.
- Release-critical source/data/scripts must be tracked; generated artifacts must remain ignored.
- AppImage builds run source hygiene unless `WAYSTONE_SKIP_SOURCE_HYGIENE=1` is set for a deliberate local experiment. Do not treat bypassed builds as release candidates.
- AppImage/release work should not rely on absolute local paths, untracked files, manually installed models outside the scripted model cache, or generated files that are absent from a fresh clone.
