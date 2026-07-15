# Vendored from Exiled Exchange 2

- **Source:** https://github.com/Kvan7/Exiled-Exchange-2
- **Commit:** `acc7653f05629228f12e273ab1b8da3e46d6bcd1` (renderer copied 2026-06-28)
- **License:** MIT (see `LICENSE` in this directory)
- **Upstream paths → vendor paths:**
  - `renderer/` → `renderer/` (full upstream renderer for the Alt+Z price-check UI)
  - `ipc/` → `ipc/` (browser-host event types used by the renderer)
  - `renderer/src/parser/` → `src/parser/`
  - `renderer/src/assets/data/` → `src/assets/data/`
  - `renderer/src/web/price-check/filters/` → `src/web/price-check/filters/` (`.ts` only, Vue components excluded)
  - `renderer/src/web/price-check/trade/` → `src/web/price-check/trade/` (`.ts` only)
  - `renderer/src/web/price-check/trends/getDetailsId.ts` → `src/web/price-check/trends/`
  - `renderer/src/web/background/{Prices,Leagues,TradeData}.ts` → `src/web/background/`
  - `renderer/src/assets/client-string-loader.ts` → `src/assets/`
  - `renderer/public/data/en/` + `item-drop.json` + `patrons.json` → `public/data/`
  - `renderer/src/assets/make-index-files.mjs` → `tools/`

## Local modifications

Keep this list short — the goal is byte-identical files so upstream re-pulls stay a plain copy.

1. `tools/make-index-files.mjs`: `LANGUAGES` reduced to `["en"]` (only English data vendored).
2. `src/assets/data/index.ts`: 8× mechanical replace `import.meta.env.BASE_URL` → `globalThis.EE2_DATA_BASE` (no Vite in the Node sidecar; `brain/src/bootstrap.ts` sets this global to a `file://` URL and shims `fetch` to read the vendored `public/` dir). No logic changes.
3. `src/assets/client-string-loader.ts`: 1× same mechanical `import.meta.env.BASE_URL` → `globalThis.EE2_DATA_BASE` replace (the global is a `file://` prefix so the dynamic `import()` of `client_strings.js` resolves as a native Node ESM file import). No logic changes.
4. `src/web/background/Leagues.ts`: 1× import-path edit `from "./IPC"` → `from "@/web/background/IPC"` (relative import bypassed the tsconfig stub alias). No logic changes.
5. `src/web/background/Prices.ts`: 2× import-path edits `from "../Config"` → `from "@/web/Config"` and `from "../overlay/widgets"` → `from "@/web/overlay/widgets"` (relative imports bypassed the tsconfig stub aliases). No logic changes.
6. `renderer/package.json`: one build-safety adapter. `prebuild` runs `make-index-files` so the renderer's ignored `public/data/**/*.index.bin` files are regenerated before Vite copies data into `dist/`; otherwise WebKit fetches 404s for the index files and the EE2 data loader crashes while constructing typed arrays.
7. `renderer/src/main.ts`: one Kwaystone diagnostic adapter. Renderer startup errors are shown in a small in-page error panel and posted to `/client-error` so WebKit/Vue/data-load failures do not become silent blank overlays.
8. `renderer/src/web/App.vue`: one native-overlay styling adapter. The document/app root is explicitly transparent so a hidden or not-yet-mounted widget cannot paint an opaque full-screen WebKit canvas over the game.
9. `renderer/src/web/overlay/OverlayWindow.vue`: two Kwaystone host-lifecycle adapters. When the browser-mode price-check widget transitions from shown to hidden, it emits `CLIENT->MAIN::user-action` with `action: "kwaystone-hide-price-check"` so the native WebKit layer-shell window can be hidden too. It also avoids hiding an already-visible exclusive widget when that same widget is shown again; Kwaystone intentionally replays the latest item text after renderer readiness, and the upstream self-hide behavior would otherwise close the native price-check window immediately.
10. `renderer/src/web/price-check/PriceCheckWindow.vue`: one Kwaystone host-lifecycle adapter. After the price-check component registers its `MAIN->CLIENT::item-text` listener, it emits `CLIENT->MAIN::user-action` with `action: "kwaystone-price-check-ready"` so the native host can replay the latest captured item text into a ready renderer. Without this readiness handshake, a cold WebKit/Vue load can miss the initial item-text broadcast and leave a blank price-check panel.
11. `src/web/price-check/trade/pathofexile-trade.ts`: one defensive parser guard. Object-shaped modifier entries without a string `description` are ignored so a future/unknown trade API entry cannot crash display-item rendering. This is shape-based, not item-specific.

All other coupling to the EE2 app (`@/web/Config`, `@/web/background/IPC`, `@/web/overlay/*`) is resolved via tsconfig path aliases pointing at stub modules in `brain/src/stubs/` — vendored files are NOT edited for this.

The full `renderer/` subtree is kept upstream-like for the embedded Alt+Z UI.
Kwaystone implements the host APIs the renderer already expects (`/config`,
`/proxy`, `/events`, and `MAIN->CLIENT::item-text`) in `brain/src/ee2-host.ts`
instead of rewriting the EE2 price-check UI in GTK.

## Updating

On PoE2 game patches, upstream regenerates `public/data/*.ndjson` via its `dataParser/` pipeline. To update:

1. Pull latest EE2 in any local upstream checkout (`git -C /path/to/Exiled-Exchange-2 pull`).
2. Re-copy the paths above.
3. Re-apply the modifications above (1–11).
4. Run `npm run make-index-files` in `brain/`.
5. Run `npm ci --prefix brain/vendor/ee2/renderer`.
6. Run `npm run build --prefix brain/vendor/ee2/renderer`.
7. Update the commit hash at the top of this file.
