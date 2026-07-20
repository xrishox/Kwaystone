# Ritual scanner rewrite — lab record and final report

Branch `ritual-rewrite`, 2026-07-19. Ground-up rebuild of the ritual (Favours)
scanner. Production result lives in `poed/poed/ritual_scan/` (architecture in
`docs/architecture.md`); `poed/poed/ritual_lab/` holds the five candidate
systems and the scoring harness used to choose it.

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

## Upgrade experiments (post-ship, 2026-07-19)

Research-driven candidates raced against the shipped s2 on the lab harness.
Measured NEGATIVE results (do not retry without new evidence):

- Global ambient-tint color calibration: UI chrome colors are scene-stable
  (plaque/gridline medians within ~1-2 BGR units across all corpus frames) —
  there is no global tint to correct. The weak-true-score cases (Blood of the
  Warrior 0.55) are art-RENDITION mismatch, not color transform.
- Channel-affine color scoring (ANCC-lite): separates worse than joint ZNCC
  (true 0.369 vs wrong 0.370 on the BotW window).
- opencv-contrib learned patch descriptors (VGG-120d, BEBLID): rank the true
  template at #901/#224 on hard cells; natural-image patch training does not
  transfer to tiny stylized icons (top-3 all ~0.92 generic circles). Fast
  (37 ms / 120 cells) but not discriminative.

Live candidates (flags in `poed.ritual_scan.identify`, defaults = shipped):
blend gray+color acceptance scoring, LINE-2D-style orientation gate,
small-pitch unsharp; aspect/offset corpus transforms added to the lab
datasets ("aspect": ultrawide pad + horizontal crop).

## Outcome

- Winner: S2 (chrome-anchored) composition, graduated into `poed.ritual_scan`
  and wired into `poed.scanners.ritual`. Selection basis (five-system
  comparison over corpus + synthetic-truth + 112-frame negative suite +
  retained scans):

| system | corpus L3 | FP fires | syn recall | syn names | verdict |
|---|---|---|---|---|---|
| s0 (old) | 4/4* | 23/112 | - | - | baseline; inventory match-storms, p95 19s |
| s1 lattice-only | ~ | 52/112 | 0.87 | 0.81 | pitch prior alone unsafe |
| s2 chrome | 4/4 | 0/112 | 0.94 | 0.89 | WINNER |
| s3 blobs | - | 112/112 | ~0 | 0 | killed: densest-window latches on any grid |
| s4 retrieval-vote | - | 112/112 | ~0 | 0 | killed: inventory items are known icons too |
| s5 generative | - | 80/112 | 0.79 | 0.73 | killed: membership extent not selective |

  (*s0 passed the 4-case corpus while failing badly on live frames — the
  corpus alone was too easy, which is why the lab added synthetic position
  truth and the negative suite.)
- Final gates: full managed corpus 6/6 PASS (all ritual cases Level 3;
  combination/multi-rune untouched), poed pytest 301 passed, metamorphic
  shift/determinism pass.
- Latency: ritual corpus scans 1.1-3.1 s end-to-end (probe 120-520 ms with
  CPU OCR) vs old extraction 2.5-15.7 s; the unbounded match-storm failure
  class is structurally gone (work bounded by the 12x10 panel).
- Known limitation (documented in architecture.md): name fidelity validated
  at 4K; synthetic downscales keep routing/counts but degrade names; native
  sub-4K captures are needed before identification is resolution-independent.
- Deferred cleanup: `matching_mode="cells"` in `poed.uniquescan` +
  `poed.unique_grid_geometry` are production-orphaned (merchant uses the
  "shared" mode) but still test-covered; removal is a follow-up decision.

## Lab usage

- `.venv/bin/python -m poed.ritual_lab snapshot-rows` (once; spawns temp brain)
- `.venv/bin/python -m poed.ritual_lab run --systems s0,s2 --datasets corpus,debug,fp,synth`
- `.venv/bin/python -m poed.ritual_lab donor --scan <id> --panel X,Y,W,H` then
  `synth --count N --seed S` (verify donor-overlay.jpg visually before use)
- Results + overlays under `~/.local/state/waystone/ritual-lab/results/<stamp>/`
