# Iteration Plan

Living backlog for the overlay. Add items under **Backlog** in any form (one
line is fine); when an iteration starts, items get pulled into a dated section,
brainstormed/planned if needed, and shipped task-by-task. Ask Claude to "run
the next iteration" to process the top of the backlog.

## Backlog

- Unique-scan OCR extensions (discussed 2026-06-11, not yet picked up):
  (a) tribute-cost verdict — OCR the Ritual tribute price per item, compare
  to scanned value, badge "worth it / skip"; (b) stack-size multiplication —
  OCR the stack digit on stacked reward cells, show stack value not unit
  value. Both need an OCR dep (tesseract or similar) — own brainstorm first.
- Unique-scan: in-game smoke checklist pending (badges placement/click-through,
  Esc dismiss, duplicates, DP-3 monitor targeting) — confirm then close.

## Shipped 2026-06-11 (unique-scan iteration)

- Unique-scan v1 (Alt+X): icon recognition of uniques/omens/currency on
  screen (CT-validated template matching: shared coarse pyramid + color
  verify, ppm capture), poe2scout unique+currency pricing, Tier-1 sorted
  panel with ≥N-exalt highlight + trend arrows, Tier-2 click-through badges,
  duplicate detection, startup warm. ~0.85s per scan. Plans + CT report under
  docs/superpowers/plans/2026-06-11-unique-scan-*.md.

## Known warts / deferred

- Multi-monitor: panel open (any keyboard mode) — verify typing/Esc on OTHER
  monitors isn't blocked now that grab is ON_DEMAND (was EXCLUSIVE-seat-wide)
- "ITEM" unsearchable lines mislead for merged stats: 3 evasion affixes show
  there because the engine collapses them into one searchable defence total.
  Options: hide lines whose merged total exists, or relabel "not individually
  searchable"
- Unique support gems with no tradeTag (e.g. Helbrym's Hide) get no price:
  vendored EE2 data has `tradeTag: undefined` for them → skips poe2scout (which
  also lacks them), and the trade2 listings query builds no name filter so it
  matches nothing despite sellers existing. Likely self-fixes on an upstream
  EE2 data re-pull; a name-filtered listings query would touch the vendored
  query path (breaking-change risk) — deferred as niche.
- E2E test: canned clipboard through full path (spec'd since v1, still pending)
- Hover cards for listings have no item icon (listing icons are remote URLs,
  not cached brain-side yet)
- Mod text drops the leading `+` ("30 to Dexterity" vs "+30 to Dexterity") —
  translation templates are unsigned
- Corrupted/eldritch implicits group under "Mods" instead of "Implicits"
  (generation values beyond prefix/suffix unhandled)
- Centered layer-shell surface: default height 640 may be ignored by the
  compositor (watch on different monitors)
- Esc during in-flight lookup: late result re-presents the panel
- `seller`/`accountName` shown only when `ign` empty — no toggle

## Shipped

### Gem pricing fix — 2026-06-09
- Gems (uncut, lineage/unique support) carry a poe2scout-priced tradeTag but
  don't stack, so `isCurrency` (tradeTag && stackSize) missed them → they fell
  to listings with no price. priceCheck now also routes a tradeTag item to the
  currency/scout view when poe2scout actually prices the tag; non-priced
  tradeTag items (unique gear) still fall to listings.

### Spring cleaning — 2026-06-09
Readability + performance refactor, our code only (vendor/ee2 untouched);
tests green throughout (brain 73, poed 94), all smoke-verified.
- brain: dropped dead smoke.ts; shared `resolveCurrencyIcon`/`round1` helpers;
  split `currencyCheck` into scout/exchange path helpers (+ `pushRate`, pure
  `median`, parallelized scout icon awaits); extracted `applyOverrides`/
  `collectProps` from `buildQueryAndStatsFromItem`.
- poed: dropped dead panel_position/panel_width config keys; `_num` view-model
  helper; extracted `build_currency_card` + module-level `_icon_or_text` +
  `_dispatch_requery` from overlay.py; converted `main()` closure-soup into an
  `App` class (state dict → attributes, closures → methods, merged on_app_run +
  the two signal handlers).
- Left intentionally: the exchange fallback path, dynamic `await import` lazy
  init, stat-filters vendor-mirror, the GTK sizing dance / popover debounce /
  draggable cursor math / portal DBus plumbing (fragile, smoke-verified).

### Iteration 10 — 2026-06-09
- Currency price trend: poe2scout `PriceLogs` (already in the bulk pull, free)
  carried through as `history` (oldest→newest), shown as a colored signed
  `+/-N.N%` on the currency card's name row (green up / red down). Started as a
  Cairo sparkline — felt out of place, swapped for the percent.
- Fixed the panel presenting at a giant first frame then snapping to size on
  drag: the post-present margin recommit ran before the rebuilt content was
  laid out; deferred a size-settle (default_size 1×1 + queue_resize + margin
  recommit) to a GLib idle, after the layout pass.

### Iteration 9 — 2026-06-09
- Currency/material pricing moved to **poe2scout** (poe2scout.com/api) —
  aggregated, volume-weighted prices keyed by tradeTag (=== their ApiId).
  Ends the trade2-exchange noise saga: exchange books were thin/baity on most
  pairs (chaos↔ex median 1 vs real ~4; cheap items only buy-side liquid).
- `poe2scout.ts`: ONE bulk pull per league (loop currency categories — carries
  CurrentQuantity, which the flat /Items omits) → in-memory `Map<tradeTag→
  {price,quantity}>` + league DivinePrice, 15-min TTL, single in-flight refresh,
  warmed fire-and-forget at brain startup. No DB/CSV, no disk cache (game-launch
  lead time + 0.1s re-warm cover cold start).
- currencyCheck: exalted row = CurrentPrice, divine row = price/DivinePrice;
  the iteration-8 exchange path (pairRate/marketRate/cross-rate) stays as the
  fallback when poe2scout lacks an item (404).

### Iteration 8 — 2026-06-08 (superseded by 9 for currency)
- Exchange-based currency exploration: dropped chaos, mode-of-cluster vs median,
  both-direction pairRate (sell/buy liquidity), cross-rate fallback. All kept
  only as the poe2scout fallback path now.

### Iteration 7 — 2026-06-08
- Drag-to-move both panels via a grab-handle strip; each remembers its own
  position across runs (PositionStore → ~/.local/state/poe2-overlay/positions.json)
- Drag tracking was a saga: GestureDrag offset and get_point() both read the
  panel's OWN coordinate frame, which lags because set_margin is async →
  feedback halving / jump-at-speed. Fixed by polling `hyprctl cursorpos`
  (compositor-absolute, frame-independent): margin = cursor − output_origin −
  grab_local, no surface term, no feedback. Falls back to laggy offset off-Hypr
- Union-extent monitor clamp (was first-monitor-only, walled off the wide
  game screen on multi-monitor)

### Iteration 6 — 2026-06-07
- Search defaults: affix mods only — property-tagged defences/ward now start
  disabled like runes (found+fixed an EE2 quirk force-enabling armour props);
  everything stays click-toggleable
- Currency prices the SELL side: live books showed ask 50(bait)/70/80/81 ex
  vs buyers paying 78 — lookup answers "what do I get selling this stack";
  median-of-top-5 bait guard kept
- Corrupted toggle: ItemFilters.corrupted is {value, exact?} — surfaced as a
  toggle-only prop row, override flips `exact`
- Unsearchable item lines shown dim under "ITEM" (normalized-text matcher vs
  the stat list; note: individual evasion-affix lines land here because the
  engine collapses them into the defence total)
- One-press Esc: consuming `,Escape` bind exists only while the panel is
  visible (portal shortcut "panel-close", BindManager-style lazy-resolved
  lifecycle, unbound on hide/exit) — game loses Esc only while overlay open
- Hotkey: Ctrl+D → Alt+Z
- Unique names in the game's deep orange (#af6025); 350px min card width
- Smoke polish: Implicits group above Mods; Corrupted moved to last base-stat row

### Iteration 5 — 2026-06-07
- Explicit stat filters: vendor's pseudo pass irreversibly absorbed resist/
  attr/life lines and no explicit preset exists for finished items —
  `initExplicitModFilters` (brain/src/stat-filters.ts) mirrors vendor
  initUiModFilters minus filterPseudo; keep lock-step on upstream pulls
- Currency rule A: rows normalized to "1 ⟨pricier⟩ ⇒ N ⟨cheaper⟩" (N ≥ 1,
  raw-precision median; sub-1 rates fixed)
- Two-column price panel (720px): interactive card left, listings right;
  props (ilvl/quality/sockets via ItemFilters + defences via property-tagged
  stats) queryable+toggleable in the card; small 16px min-price icon
  "N ⟨icon⟩" bottom-left, count right; min-entry width_chars=4
- Login: brain `login`/`logout` cmds (api/profile verification); standalone
  LoginBox layer-shell widget (top, 35% offset), shown with the panel;
  cookie detection click-only (startup auto-detect removed)
- Capture hardening: explicit keydown ctrl → key c (60ms) → keyup sequence,
  exit-code checked, one retry (live experiment in smoke; fallback variant
  documented in plan)
- Smoke fix-forward: icons truly capped now (Gtk.Image pixel_size — Picture
  + size_request only set a minimum); button hover/active feedback; currency
  rows redone ninja-style — fixed direction "1 lookup = X", adaptive
  decimals, grey inverse hint on sub-1 (mockup B), single-column boxed view
  with separators; 128px item header + readable filter labels (values
  substituted into templates); natural card width + shrink-to-fit window;
  bounded soft-wrap (max_width_chars) for long mods
- EXCLUSIVE keyboard grab was seat-wide: it blocked typing on every monitor
  and starved the LoginBox layer surface of ALL input (no hover/clicks).
  Panel now uses ON_DEMAND — Esc costs one panel click when the game holds
  focus
- Login saga (3 root causes): Firefox lives at ~/.config/mozilla (XDG
  migration); the API exists ONLY on pathofexile.com (pathofexile2.com is a
  pure SPA shell — its POESESSID authenticates nothing); pathofexile.com's
  POESESSID is a browser-session cookie Firefox keeps in memory only, so
  disk autodetect can rarely find it. Primary workflow = paste into
  config.toml (template created, chmod 600); verified live end-to-end
  ("login: verified", ● account name)

### Iteration 4 — 2026-06-07
- Interactive search card (mockup A): the merged search FILTERS are the card's
  lines, grouped Pseudo/Mods/Implicits/Enchants/Runes — value box left
  (editable min), click line toggles in/out, ~800ms debounced auto-requery.
  Engine note: trade filters are merged/pseudo stats, so per-line
  prefix/suffix boxes were impossible; tiers stay on hover cards
- `requery` cmd: stateless (clipboard re-sent + index-keyed overrides);
  body-keyed cache makes repeats free. Race-hardened: generation token drops
  stale results, dropped debounce fires re-arm, Esc'd panel never re-presents
- Currency view: aligned table (1 ⇒ / ×stack ⇒ / offers columns)
- POESESSID: optional `poesessid` in config.toml → brain child env → Cookie
  header on trade calls. Empty = anonymous. Keep config chmod 600
- Smoke fix-forward: Search button + in-card busy spinner; currency table
  roomier (bold headers, "Stack" column, plain amounts); POESESSID
  auto-detected from Firefox cookies.sqlite when config empty (read-only
  immutable URI, value never logged; Chromium encrypted → out of scope)

### Iteration 3 — 2026-06-07
- Currency rates: median of top-5 book ratios — bait listings ("1 ex per div")
  no longer poison the rate (live book confirmed: 5/1 bait atop 80-84 market)
- Hover cards grouped by trade-API categories (Enchants/Runes/Implicits/
  Fractured/Mods/Desecrated); prefix/suffix split impossible for listings —
  API doesn't tag generation
- Search runs with ALL non-rune, non-hidden stats enabled at -10%
  (EE2 searchStatRange); searched stats rendered under the item card with
  min-value boxes, enabled highlighted (read-only; toggles = next iteration).
  NOTE: well-rolled items can hit 0 results — toggles are the relief valve
- Repeat-lookup cache: already existed (vendored EE2 cache, search+fetch,
  ≥5-min TTL, keyed on query body — count-unbounded, time-evicted). Verified
  live: 501 ms first lookup → 4 ms repeat. Iteration-4 toggles get caching
  per filter combination for free
- Hotkey fix: portal shortcut appid is derived from the caller's systemd
  scope — poed now resolves the registered name via `hyprctl globalshortcuts -j`
  instead of hardcoding `:price-check`

### Iteration 2 — 2026-06-06
- Single hotkey: currency items auto-detected → rate view (exalt→chaos→divine,
  stack estimate); bulk hotkey/flow removed
- Hotkey while open re-looks-up; Esc only close (exclusive keyboard while visible)
- Instant popover switching between rows
- Ctrl+D consumed but bound only while PoE2 window exists (socket2-driven
  `hyprctl keyword bind/unbind`)

### Iteration 1 — 2026-06-06
- Card UI: item card top (Base/Runes/Prefixes/Suffixes), `N × <icon>` rows,
  hover popover with full listing item, centered panel
- Brain-side icon disk cache (poecdn → local paths in responses)
- Min-only summary (median dropped)

### v1 — 2026-06-06
- poed daemon: portal GlobalShortcuts, focus guard, Ctrl+C inject, wl-paste,
  brain child lifecycle, layer-shell panel
- brain: EE2-vendored parser, trade2 price check, bulk exchange, rate limiter
