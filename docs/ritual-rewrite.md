# Ritual scanner rewrite — living lab log

Branch `ritual-rewrite`. This is the working log for the ground-up ritual scanner rebuild.
It is pruned as work progresses; superseded claims are deleted, not struck through. At the
end of the effort it is condensed into the final report and `docs/architecture.md`.

Plan reference: `~/.claude/plans/i-am-having-a-witty-lynx.md` (approved). Tasks M0-M9.

## Why (problem statement)

The current ritual scanner misses reward boxes, false-positives on the player inventory
grid, and emits half-cell-misaligned rects. Root causes, verified 2026-07-19:

- Probe accepts any run of >=7 regular vertical Hough lines at 20-90px pitch with
  confidence hardcoded 1.0 (`poed/poed/scanners/scene.py:137-198`). The inventory grid
  qualifies (same ~102px pitch at 4K).
- Grid origin recovery + per-cell `int(round())` rect math drifts by cell fractions
  (`poed/poed/unique_grid_geometry.py:136-173`), producing half-square shifts.
- Occupancy uses absolute HSV thresholds (`unique_grid_geometry.py:176-204`); sparse
  diagonal art (2x4 staves) fails spread checks and splits into per-cell markers.
- Extraction costs 2500-3200 ms at 4K (observed in retained scans 2026-07-19).

## Verified facts (do not re-derive)

- Favours panel: ornate gold plaque header with text "FAVOURS", "N Rituals Remaining",
  "N TRIBUTE". No per-item cost labels in the grid. Occupied cells have navy tint under
  art; empty cells show faint gridlines. Deferred rewards get an overlay marker.
  Grid nominally 12 cols x 10 rows, cell pitch ~102px at 3840x2160.
- Merchant scanner already gold-plaque-anchors + recognition-only OCR strip
  (`poed/poed/scanners/merchant.py:232 _buy_sell_title_plaque_box`, `:209
  _read_plaque_title`) — the proven chrome-anchor pattern in this codebase.
- Merchant shares `scan_unique_grid`/`uniquescan._scan_shared` with
  `matching_mode="shared"`; `matching_mode="cells"` + `unique_grid_geometry` are
  ritual-only. Shared files stay untouched until final cleanup.
- Corpus: 4 ritual cases, all Level 3, all 3840x2160, truth = name multisets + counts
  (8/10/26/10 items), no positions. `scripts/evaluate-scan-corpus` is the gate
  (`--fixture-rows` offline). ~20 ritual-routed retained debug scans from 2026-07-19
  under `~/.local/state/waystone/debug/scans/` (diagnosis only, never corpus truth).
- Rows: brain `uniqueprices` -> dict name->row with `price`, `iconPath` (local PNG cache,
  ~2771 icons in `~/.cache/waystone/icons`), `w`/`h` slot footprint, `kind`,
  `sourceCategory`; ritual filter = `uniquescan.filter_ritual_rows`.
- Match contract: x,y,w,h absolute frame coords; name; score; scanKind="ritual";
  unitPrice/price/totalPrice; stackSize>=1; ambiguous; markerOnly for unknown-occupied;
  market fields only via `poed.match_fields.match_row_fields`.
- scan_cache: `digest/begin_scan/lookup/store`, two-generation rotation — keep equivalent
  reuse. Debug writes async via `poed.scanners.debug_io` only.

## Research notes (M0, 2026-07-19)

- Lattice pitch/phase: 2D autocorrelation (ACF) of the panel interior; peaks of the ACF
  (or Fourier spectrum of the ACF) give spacing; refine selected lattice vectors to
  nearest ACF maxima with subpixel precision (crystallography/Gwyddion practice).
  Zero-padded FFT cross-correlation gives subpixel phase offsets.
- Robust lattice fit: RANSAC over point sets (pivot basis-vector proposal + inlier
  maximization); "region of dominance" preferred over hard thresholds; lattice grows
  outward from seed texels along basis vectors (deformed-lattice literature). Our grids
  are axis-aligned, so per-axis 1D regression of clustered line/edge positions against
  integer indices (least squares + RANSAC over index assignment) is sufficient and
  simpler.
- Icon retrieval: perceptual hashing as a prefilter to select a small subset of templates
  before expensive template matching is established practice; TM_CCOEFF_NORMED (ZNCC) as
  the verifier. Margin-over-runner-up acceptance mirrors region-of-dominance.
- Prior art (poe-archnemesis-scanner, github.com/4rtzel): fixed-scale template match with
  0.94 confidence over a known grid — works only when template scale == screen scale.
  Our design normalizes template scale from footprint metadata + measured pitch instead.
- Sources:
  - https://www.learncodebygaming.com/blog/opencv-object-detection-in-games-python-tutorial-1
  - https://github.com/4rtzel/poe-archnemesis-scanner
  - http://gwyddion.net/documentation/user-guide-en/edit-extended.html (ACF lattice refine)
  - https://www.researchgate.net/publication/26756908 (deformed lattice detection)
  - https://www.ncbi.nlm.nih.gov/pmc/articles/PMC11750840/ (pHash + template matching)

## Architecture decisions (stable)

- Stage pipeline shared by all candidates: PanelHypothesis -> Lattice (float origin/pitch;
  cell rect edges computed as `round(x0 + c*pitch)` — no cumulative rounding) ->
  OccupancyMap -> Footprints -> matches. A system = a composition of stage impls; the
  production system may mix stages from different candidates (ablation matrix decides).
- `poed/poed/ritual_lab/` = tracked experiment lab (s0 baseline + s1..s5, datasets,
  synth, scoring, CLI `python -m poed.ritual_lab`).
- `poed/poed/ritual_scan/` = production domain package (mirrors `have_scan/`); winner
  stages graduate here; `scanners/ritual.py` keeps its class surface and delegates.
- Candidate systems: S1 rebuilt lattice-first; S2 chrome-anchored top-down (gold plaque +
  OCR strip + border trace); S3 item-first blobs; S4 retrieval + geometric voting;
  S5 generative background reconstruction (ACF pitch/phase + modal cell texture +
  residual occupancy). Shared identification: pHash/thumb prefilter -> masked ZNCC verify
  -> margin acceptance; scan_cache bucket "ritual-cell-v2".
- Reliability instruments: 4 corpus cases; seeded synthetic composites with position
  truth (icons pasted onto harvested empty panel texture, incl. diagonal 2x4 staves and
  composited inventory panel); 1440p/1080p downscales; FP suite (other-category corpus
  frames + inventory-only crops) must never fire; metamorphic invariants (translate k px
  => boxes shift k; downscale => same name multiset; determinism).

## Empirical doctrine (hard-won facts; trust these before re-deriving)

- The Favours grid is a fixed 12x10 block; pitch is a constant fraction of
  frame height (105/2160 = 0.0486; bounds 0.038-0.060 exclude octave errors).
- The grid background is partially TRANSLUCENT (scene shows through empty
  cells); empty cells carry a quatrefoil ornament pattern; occupied footprints
  carry a smooth navy backdrop tint. Darkness/residual thresholds are
  unreliable; per-cell features (sat/edge/navy) robust-z + 2-means with
  separation gate work; deep inset 0.22 because ART OVERFLOWS CELLS.
- Item ART OVERFLOWS its footprint into neighbours (sceptre horns intrude into
  the cell above). Boundary-local pixel evidence CANNOT partition items; the
  partition authority is identification itself (footprint-hypothesis exact
  cover, branch&bound, objective = score*area - 0.05 per footprint,
  uncovered-core penalty 0.35; full-score repair of weak components).
- Gridlines are drawn over backdrops; separator visibility is a DEAD END
  (measured equal for empty-empty and internal-item boundaries).
- Unmasked template correlation dies on translucency; masked ZNCC over art
  pixels (mask = template pixels differing from the 12-gray flatten) with
  mean-centering works; COLOR must be scored as ONE joint vector across
  channels or omen-variant hues cancel out.
- The TRIBUTE plaque reads perfectly via recognition-only OCR, but frames can
  contain 30+ gold bands; 'OFFER TRIBUTE TO THE KING' (below the panel) also
  matches /TRIBUTE/ — candidate ranking must be by GRID QUALITY under the
  band (autocorr score sum, min 0.22), not band order.
- OCR helper: WAYSTONE_PADDLE_DEVICE=cpu required in lab runs (config
  auto-selects gpu:0 which crashes while the game owns the GPU).
- Row metadata (w,h) matches icon-file shape (e.g. Blood of the Warrior 1x2,
  file 47x94); remaining corpus misses are cover arbitration between
  confidently-wrong claims (Head of the King vs Blood of the Warrior) and
  omen-variant discrimination.
- Production ritual latency baseline: extraction 2.5-15.7s; probe accepts any
  regular line run (23/112 FP fires; inventory match-storms).

## Status

- M0 done: branch created, research logged, memory anchor written.
- M1 done: `poed/poed/ritual_lab/` scaffolding (stages/estimate/datasets/synth/scoring/
  report/systems/CLI), rows snapshot (3164 rows, all with icons), 10 inventory fp-crops.
- M2 done: s0 baseline measured (below).
- M3 done: S2 chrome-anchored system working end to end: gold-band OCR anchor +
  quality-ranked gridline lattice (13/11-line capped windows + weighted LSQ
  refit) + feature occupancy + expansion candidates + identification-driven
  exact cover with full-score repair. Corpus 4/4 L1, 1/4 L2 (26-item case
  exact), synth recall 0.871 / precision 0.748 / IoU 0.985 / names 0.811.
  Overcounts of +1..+2 phantom markers and omen-variant swaps remain (M7).
  Latency unoptimized (p50 ~4.2s — full-score repair is the hot spot; masked
  ZNCC loops are pure Python, no caching/vectorization yet).
- Next: M4-M6 (S1/S5/S3/S4 variants over shared stages), then M7 ablation +
  arbitration/omen iteration + latency work.

## Lab usage

- `.venv/bin/python -m poed.ritual_lab snapshot-rows` (once; spawns temp brain)
- `.venv/bin/python -m poed.ritual_lab run --systems s0,s2 --datasets corpus,debug,fp,synth`
- `.venv/bin/python -m poed.ritual_lab donor --scan <id> --panel X,Y,W,H` then
  `synth --count N --seed S` (verify donor-overlay.jpg visually before use)
- Results + overlays under `~/.local/state/waystone/ritual-lab/results/<stamp>/`

## Scoreboard

M2 baseline (datasets corpus+debug+fp, run 20260719T221420):

| system | corpus L1 | L2 | L3 | FP fires | debug fired | p50 ms | p95 ms |
|---|---|---|---|---|---|---|---|
| s0 | 4/4 | 4/4 | 4/4 | 23/112 | 15/15 | 1944 | 19161 |

- s0 passes the whole 4-case corpus (corpus too easy) but fires on 20 merchant
  frames and 3 `none` frames (17-18 garbage matches each — the stray-scan grabs).
- Catastrophic frames observed live (scan-20260719T220654, 68 matches, 15.7s):
  probe latched onto the PLAYER INVENTORY grid; every match painted over the
  inventory while the fully stocked Favours panel got zero. Cause: inventory
  yields more clean vertical Hough lines than the item-occluded Favours grid,
  and `scene.ritual` takes the highest line-count candidate.
- s0 does NOT fire on inventory-only right-crops (line length gate relative to
  full frame height), so the fp-crop set alone is insufficient — full frames
  with both panels open are the discriminating negative/positive cases.
- Latency: corpus 0.5-1.1 s, retained frames 1-2.6 s typical, 8.8-15.7 s on
  match explosions.
