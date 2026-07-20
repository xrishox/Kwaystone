# Ritual scanner: design record and lab guide

The ritual (Favours) scanner lives in `poed/poed/ritual_scan/` and is wired
through `poed/poed/scanners/ritual.py`. This document records the empirical
facts the design rests on, why this architecture won over the alternatives,
and how to use the development lab in `poed/poed/ritual_lab/`. The module
boundary summary lives in `docs/architecture.md`.

## Verified facts about the Favours UI

- Gold chrome bands with the texts "FAVOURS", "N Rituals Remaining", and
  "N TRIBUTE" sit above the reward grid; there are no per-item cost labels.
- The grid is a fixed 12x10 cell block. Cell pitch is a constant fraction of
  frame height (105/2160 = 0.0486 at 4K; prior bounds 0.038-0.060), which
  holds across resolutions, ultrawide aspect ratios, and windowed mode
  because the game UI scales with height.
- The grid background is partially translucent: the scene shows through
  empty cells. Empty cells carry a quatrefoil ornament pattern; occupied
  footprints carry a smooth navy backdrop tint that never overflows.
- Item ART overflows its footprint into neighbouring cells (a sceptre's
  horns intrude into the cell above), and gridlines are drawn over item
  backdrops.
- Frames can contain 30+ gold bands, including 'OFFER TRIBUTE TO THE KING'
  below the panel, which also matches /TRIBUTE/.

## Design consequences (why the pipeline looks like this)

- Panel localization must be chrome-anchored and OCR-verified. Geometry-only
  acceptance was measured at 52/112 false fires on the negative suite
  (player inventory, merchant stock, world scenes) versus 0/112 with
  chrome+OCR verification, so an unavailable OCR helper REJECTS the probe.
  Band candidates are geometry-gated first (zero OCR cost on negative
  frames) and ranked by lattice autocorrelation quality, never by band size.
- The lattice is fit as a fixed 13/11-line window with capped line strengths
  and a weighted least-squares refit of detected peaks: occlusion by item
  art or tooltips cannot truncate the extent, ornament edges cannot buy the
  window, and per-edge rounding of the float lattice eliminates cumulative
  half-cell drift.
- Occupancy uses per-cell features (saturation, edge energy, navy fraction)
  robust-z-normalized across the grid's own cells with a 2-means separation
  gate — absolute thresholds break on translucency. A deep 0.22 cell inset
  keeps neighbour art overflow out of the features.
- Because art overflows cells, boundary-local pixel evidence cannot
  partition occupied cells into items (separator visibility measured equal
  for empty-empty and internal-item boundaries). Identification is the
  partition authority: legal footprint hypotheses are scored and a
  branch-and-bound exact cover selects the layout, with full-score repair of
  weak components, identification-confirmed promotion of expansion-candidate
  cells (strict 0.66 bar — gray-stone icons score ~0.6 against the bare
  ornament pattern), and candidate cells weighted 0.3 in the objective.
- Identification is masked ZNCC over art pixels (masks recovered from the
  corpus loader's flat 12-gray flattening) with mean-centering; color is
  scored as ONE joint vector across channels (per-channel normalization
  cancels omen-variant hues). Lookalike families (from pairwise half-res
  similarity) are fully verified whenever a member wins, close variants are
  settled by difference-mask discrimination, and low-confidence cells pay
  for a deep rescue pass. The orientation gate (LINE-2D-style spread
  gradient orientations) and small-pitch sharpening are production defaults
  selected by the 2026-07-19 upgrade race.

## Measured negative results (do not retry without new evidence)

- Global ambient-tint color calibration: UI chrome colors are scene-stable;
  there is no global tint to correct. Weak true-match color scores are art
  RENDITION mismatch, not a color transform.
- Channel-affine color scoring (ANCC-lite): separates worse than joint ZNCC
  (true 0.369 vs wrong 0.370 on the hardest case).
- opencv-contrib learned patch descriptors (VGG/BEBLID): rank true templates
  at #901/#224 on hard cells; natural-image patch training does not transfer
  to tiny stylized icons.
- Raw descriptor-tile retrieval sweeps: matched cells and world tiles both
  score ~0.3 — no separation.

## Why this architecture won (five-system race, 2026-07-19)

| system | localization strategy | FP fires /112 | synth names | verdict |
|---|---|---|---|---|
| s0 (old) | strongest line-run grid | 23 | - | inventory match-storms, p95 19s |
| s1 | periodic-region lattice only | 52 | 0.81 | pitch prior alone unsafe |
| s2 | chrome anchor + OCR | **0** | **0.89** | shipped |
| s3 | densest art-blob window | 112 | ~0 | latches onto any item grid |
| s4 | retrieval-gated blob voting | 112 | ~0 | inventory items are known icons too |
| s5 | generative pattern membership | 80 | 0.73 | extent not selective |

Final state: full managed corpus at Level 3, 0/112 negative-suite fires,
12/12 Level 3 on ultrawide/offset aspect transforms, scans 1.1-3.1 s
end-to-end (vs 2.5-15.7 s before). Known limitation: name fidelity is
validated on 4K captures; synthetic downscales keep routing and counts but
degrade names; native sub-4K captures are needed to certify lower
resolutions.

## Ritual lab (development tooling)

`poed/poed/ritual_lab/` scores the production pipeline against datasets
with known truth: `s2` drives locate/extract directly, `s0` drives the
full production RitualScanner (probe + scan) — identical outputs are a
regression check on the scanner-layer plumbing. Production
code must not import it, and lab datasets are never corpus truth.

- `.venv/bin/python -m poed.ritual_lab snapshot-rows` — one-time market rows
  snapshot via a temporary brain (needed before offline runs).
- `.venv/bin/python -m poed.ritual_lab donor --scan <id>` then
  `synth --count N --seed S` — build synthetic composites with exact
  position truth from a retained ritual frame (visually review
  `donor-overlay.jpg` before use).
- `.venv/bin/python -m poed.ritual_lab fp-crops` — inventory-side negative
  crops.
- `.venv/bin/python -m poed.ritual_lab run --systems s0,s2
  --datasets corpus,synth,fp,debug[,aspect,scaled] [--metamorphic]` — score;
  results, scoreboards, and failure overlays land under
  `~/.local/state/waystone/ritual-lab/results/<stamp>/`.
- Lab OCR note: runs force `WAYSTONE_PADDLE_DEVICE=cpu` (the game may own
  the GPU).

The killed candidate systems live in git history (branch `ritual-rewrite`);
their designs and failure modes are the table above.
