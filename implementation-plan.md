# POE2-Overlay Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Wayland-native PoE2 price-check overlay for Arch + Hyprland: hotkey → clipboard item → trade listings in a layer-shell panel.

**Architecture:** Python host (`poed/`) owns Wayland glue — portal GlobalShortcuts (DBus), clipboard, Ctrl+C injection, GTK4 + gtk4-layer-shell UI. Node sidecar (`brain/`) owns PoE logic — vendored EE2 parser, filter generation, trade2/bulk API, rate limiter. JSON-lines over a Unix socket between them.

**Tech Stack:** Python 3.14 + PyGObject (GTK4, gtk4-layer-shell, Gio DBus), Node 25 + TypeScript (tsx runtime, vitest), vendored Exiled Exchange 2 @ 7523eb4 (`brain/vendor/ee2/`, MIT).

**Token discipline:** Each task is one session, self-contained, ends with a commit. Read ONLY the files listed per task — never browse `vendor/ee2/` broadly (22k lines; it works, treat as a library). Spec: `docs/superpowers/specs/2026-06-05-poe2-overlay-design.md`. CLAUDE.md has the boundary rules.

## Wire protocol (reference for all tasks)

Newline-delimited JSON over Unix socket `$XDG_RUNTIME_DIR/poe2-overlay-brain.sock`:

```
→ {"id": 1, "cmd": "ping"}
← {"id": 1, "ok": true, "result": "pong"}
→ {"id": 2, "cmd": "parse", "clipboard": "<raw item text>"}
← {"id": 2, "ok": true, "result": {<ParsedItem>}}
→ {"id": 3, "cmd": "price", "clipboard": "<raw item text>"}
← {"id": 3, "ok": true, "result": {"summary": {...}, "listings": [...]}}
→ {"id": 4, "cmd": "bulk", "have": "exalted", "want": "divine"}
← {"id": 4, "ok": true, "result": {"offers": [...]}}
← {"id": N, "ok": false, "error": "human-readable message"}   // any failure
```

## File structure

```
brain/
  src/server.ts            Task 4   socket + dispatch
  src/bootstrap.ts         Task 2   fetch shim + data init
  src/stubs/Config.ts      Task 2   AppConfig/poeWebApi stub
  src/stubs/IPC.ts         Task 2   Host.proxy → direct fetch
  src/stubs/widgets.ts     Task 2   Widget/PriceCheckWidget types + defaults
  src/price.ts             Task 5   ParsedItem → query → listings
  src/bulk.ts              Task 6   bulk exchange
  test/parser.test.ts      Task 3   golden samples
  test/fixtures/*.txt      Task 3   real PoE2 item texts
  tsconfig.json            Task 2
poed/
  poed/__main__.py         Task 7   startup, wiring
  poed/config.py           Task 7   config.toml load
  poed/brain.py            Task 7   spawn child + socket client
  poed/portal.py           Task 8   GlobalShortcuts DBus session
  poed/capture.py          Task 9   inject Ctrl+C + wl-paste + window guard
  poed/overlay.py          Task 10  GTK4 layer-shell panel
  tests/test_brain.py      Task 7
spikes/
  spike_portal.py          Task 0
  spike_layershell.py      Task 1
```

---

### Task 0: Spike — portal GlobalShortcuts under xdph

De-risks the whole project. No tests — throwaway script, manual verify.

**Files:** Create: `spikes/spike_portal.py`

- [ ] **Step 1: Write the spike**

```python
#!/usr/bin/env python3
"""Spike: register Shift+Space via xdg-desktop-portal GlobalShortcuts, print Activated events."""
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gio, GLib

PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop")
bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
proxy = Gio.DBusProxy.new_sync(bus, 0, None, PORTAL[0], PORTAL[1],
                               "org.freedesktop.portal.GlobalShortcuts", None)

def on_signal(_proxy, _sender, signal, params):
    print(f"SIGNAL {signal}: {params}")

proxy.connect("g-signal", on_signal)

token = "poe2spike"
session = proxy.call_sync("CreateSession",
    GLib.Variant("(a{sv})", ({"handle_token": GLib.Variant("s", token),
                              "session_handle_token": GLib.Variant("s", token)},)),
    0, -1, None)
print("CreateSession request:", session)

# After Response signal arrives with the session handle, call BindShortcuts with:
# [("price-check", {"description": Variant("s","PoE2 price check"),
#                   "preferred_trigger": Variant("s","SHIFT+space")})]
# (Extend script iteratively at run time — Response handling is the spike's point.)
GLib.MainLoop().run()
```

- [ ] **Step 2: Run + iterate until Activated fires**

Run: `python spikes/spike_portal.py`, then press Shift+Space with another window (ideally PoE2) focused.
Expected: `SIGNAL Activated: ('price-check', ...)` printed.
Iterate inside the spike until that works — Response→session-handle→BindShortcuts→ListShortcuts flow is fiddly; the spike exists to nail the exact call sequence. Document the working sequence in comments.

- [ ] **Step 3: Record findings + commit**

Append a `## Findings` comment block at the bottom of the spike: exact DBus call order, whether xdph showed an approval dialog, whether the trigger fires while a fullscreen XWayland window is focused.

```bash
git add spikes/spike_portal.py && git commit -m "spike: portal GlobalShortcuts works under xdph"
```

**Abort criterion:** if Activated never fires while a fullscreen game window has focus, STOP — fall back to Hyprland `bind = ... exec` → socket (design alternative). Flag to user before continuing.

---

### Task 1: Spike — GTK4 layer-shell panel with keyboard input

**Files:** Create: `spikes/spike_layershell.py`

- [ ] **Step 1: Write the spike**

```python
#!/usr/bin/env python3
"""Spike: layer-shell overlay panel, keyboard on-demand, Esc closes."""
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, Gtk4LayerShell as LayerShell

def on_activate(app):
    win = Gtk.Window(application=app)
    LayerShell.init_for_window(win)
    LayerShell.set_layer(win, LayerShell.Layer.OVERLAY)
    LayerShell.set_keyboard_mode(win, LayerShell.KeyboardMode.ON_DEMAND)
    LayerShell.set_anchor(win, LayerShell.Edge.RIGHT, True)
    win.set_default_size(420, 600)
    win.set_child(Gtk.Label(label="overlay spike — press Esc"))
    ctl = Gtk.EventControllerKey()
    def on_key(_c, keyval, *_):
        if keyval == 65307:  # Esc
            win.close()
    ctl.connect("key-pressed", on_key)
    win.add_controller(ctl)
    win.present()

app = Gtk.Application(application_id="io.github.kriskruse.waystone.spike")
app.connect("activate", on_activate)
app.run(None)
```

- [ ] **Step 2: Verify over a fullscreen window**

Run: `python spikes/spike_layershell.py` with a fullscreen app (game, or `mpv --fullscreen` as stand-in) on the same monitor.
Expected: panel renders ABOVE the fullscreen surface; Esc closes it; focus returns to the game. Record findings in comments (incl. whether ON_DEMAND steals game keyboard while panel open).

- [ ] **Step 3: Commit**

```bash
git add spikes/spike_layershell.py && git commit -m "spike: layer-shell overlay renders above fullscreen"
```

---

### Task 2: Brain compiles — tsconfig, stubs, data bootstrap

Goal: `npx tsc --noEmit` clean, and a smoke script parses a hardcoded item.

**Files:**
- Create: `brain/tsconfig.json`, `brain/src/stubs/Config.ts`, `brain/src/stubs/IPC.ts`, `brain/src/stubs/widgets.ts`, `brain/src/bootstrap.ts`, `brain/src/smoke.ts`
- Modify: `brain/vendor/ee2/src/assets/data/index.ts` (one mechanical replace — record in PROVENANCE.md)

- [ ] **Step 1: tsconfig with stub-first path aliases**

```json
{
  "compilerOptions": {
    "target": "ES2022", "module": "ESNext", "moduleResolution": "bundler",
    "strict": true, "skipLibCheck": true, "noEmit": true,
    "types": ["node"],
    "baseUrl": ".",
    "paths": {
      "@/web/Config": ["src/stubs/Config.ts"],
      "@/web/background/IPC": ["src/stubs/IPC.ts"],
      "@/web/overlay/widgets": ["src/stubs/widgets.ts"],
      "@/web/overlay/interfaces": ["src/stubs/widgets.ts"],
      "@/*": ["vendor/ee2/src/*"]
    }
  },
  "include": ["src/**/*.ts", "vendor/ee2/src/**/*.ts"]
}
```

Order matters: specific aliases shadow the `@/*` catch-all, so vendored files stay unedited. Relative imports inside vendor (`./IPC`, `../Config` from `background/Prices.ts`, `trade/common.ts`) will still hit the REAL EE2 files — if tsc errors chase those, add the same-shape relative path entries OR (simpler) replace the two files' relative imports via additional paths entries is impossible for relative imports; in that case the fallback is: delete `vendor/ee2/src/web/background/Leagues.ts`'s body usage by adding it to the stub aliases too (`"@/web/background/Leagues": [...]`) and patching `Prices.ts`'s `./Leagues`/`../Config` imports — if needed, make these two one-line import-path edits in vendor and record both in PROVENANCE.md. Prefer ZERO vendor edits; accept max 3 import-line edits, never logic edits.

- [ ] **Step 2: Config stub**

```typescript
// src/stubs/Config.ts — replaces EE2's reactive app config
import type { PriceCheckWidget } from "./widgets";
import { PRICE_CHECK_DEFAULTS } from "./widgets";

export interface BrainConfig {
  league: string;
  accountName: string;
  language: "en";
  realm: "pc-ggg";
  preferredTradeSite: "www";
}

let current: BrainConfig = {
  league: process.env.POE2_LEAGUE ?? "Standard",
  accountName: process.env.POE2_ACCOUNT ?? "",
  language: "en", realm: "pc-ggg", preferredTradeSite: "www",
};

export function setBrainConfig(c: Partial<BrainConfig>) { current = { ...current, ...c }; }

export function AppConfig(): BrainConfig;
export function AppConfig<T>(type: string): T | undefined;
export function AppConfig(type?: string) {
  if (type === "price-check") return PRICE_CHECK_DEFAULTS as unknown as PriceCheckWidget;
  if (type !== undefined) return undefined;
  return current;
}

export function poeWebApi() { return "www.pathofexile.com"; }
```

- [ ] **Step 3: IPC stub — Host.proxy as direct fetch**

```typescript
// src/stubs/IPC.ts — EE2 routes API calls through Electron main to dodge CORS.
// Node has no CORS; call the API directly. EE2 passes scheme-less URLs.
export const Host = {
  proxy: async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const u = String(url);
    return fetch(u.startsWith("http") ? u : `https://${u}`, {
      ...init,
      headers: { "user-agent": "poe2-overlay/0.1 (contact: github.com/kriskruse)", ...(init?.headers ?? {}) },
    });
  },
  isElectron: false,
  // add further members only when tsc demands them; keep inert (no-op) versions
};
```

- [ ] **Step 4: widgets stub**

Copy the `Widget`, `Anchor`, and `PriceCheckWidget` interface bodies VERBATIM from `~/Documents/Github/Exiled-Exchange-2/renderer/src/web/overlay/widgets.ts` (PriceCheckWidget is lines 38–62) into `src/stubs/widgets.ts`, then add sane defaults:

```typescript
export const PRICE_CHECK_DEFAULTS: Partial<PriceCheckWidget> = {
  showSeller: "account", searchStatRange: 10, apiLatencySeconds: 2,
  collapseListings: "api", smartInitialSearch: true, lockedInitialSearch: true,
  activateStockFilter: false, requestPricePrediction: false,
  rememberCurrency: false, defaultAllSelected: false,
  autoFillEmptyRuneSockets: false, alwaysShowTier: false,
  coreCurrency: "exalted", currencyVolume: "none", rememberListingType: false,
};
```

- [ ] **Step 5: data bootstrap — fetch shim + init**

One vendor edit in `vendor/ee2/src/assets/data/index.ts`: replace every `import.meta.env.BASE_URL` with `globalThis.EE2_DATA_BASE` (mechanical, ~8 occurrences; add to PROVENANCE.md mod list). Then:

```typescript
// src/bootstrap.ts — point the vendored data loader at local files
import { readFile } from "node:fs/promises";
import { fileURLToPath } from "node:url";
import path from "node:path";

const VENDOR_PUBLIC = path.join(path.dirname(fileURLToPath(import.meta.url)), "../vendor/ee2/public/");
(globalThis as any).EE2_DATA_BASE = "ee2data://";

const realFetch = globalThis.fetch;
globalThis.fetch = (async (input: any, init?: any) => {
  const u = String(input);
  if (u.startsWith("ee2data://")) {
    const buf = await readFile(path.join(VENDOR_PUBLIC, u.slice("ee2data://".length)));
    return new Response(buf);
  }
  return realFetch(input, init);
}) as typeof fetch;

export async function initBrainData() {
  const { init } = await import("@/assets/data");
  await init("en");
}
```

- [ ] **Step 6: smoke script**

```typescript
// src/smoke.ts — run: npx tsx src/smoke.ts
import { initBrainData } from "./bootstrap";
const ITEM = `Item Class: Wands
Rarity: Magic
Volatile Wand of the Apt
--------
Wand
--------
Item Level: 60
--------
15% reduced Attribute Requirements
`;
await initBrainData();
const { parseClipboard } = await import("@/parser");
const r = parseClipboard(ITEM);
console.log(r.isOk() ? JSON.stringify(r.value, null, 2) : `PARSE ERR: ${r.error}`);
```

- [ ] **Step 7: Make it compile + run**

Run: `cd brain && npx tsc --noEmit` — chase errors. Expected error classes: missing Host members (add inert stubs), vue-component imports in vendored `.ts` (should be none — we copied `.ts` only; if one slips in, alias it), relative-import leaks (see Step 1 note). `tsx` needs the same aliases at runtime: add `"tsconfig": "./tsconfig.json"` support is automatic in tsx ≥4 via tsconfig paths.
Then: `npx tsx src/smoke.ts`
Expected: ParsedItem JSON printed (category Wand, rarity Magic, one stat).

- [ ] **Step 8: Update PROVENANCE.md mod list + commit**

```bash
git add -A && git commit -m "feat(brain): stubs + data bootstrap, vendored parser runs in Node"
```

---

### Task 3: Parser golden tests

**Files:** Create: `brain/test/parser.test.ts`, `brain/test/fixtures/{magic-wand,rare-armour,unique,currency,gem}.txt`, `brain/vitest.config.ts`

- [ ] **Step 1: Collect 5 real PoE2 item texts**

In-game Ctrl+C on: a magic item, a rare with 4+ mods, a unique, a stackable currency, a skill gem. One file each under `test/fixtures/`. (No game running? Pull sample texts from EE2's own test fixtures at `~/Documents/Github/Exiled-Exchange-2/renderer/src/parser/*.test.ts` if present, or from its GitHub issues. Real texts only — never invent.)

- [ ] **Step 2: vitest config with the same aliases**

```typescript
// vitest.config.ts
import { defineConfig } from "vitest/config";
import path from "node:path";
const r = (p: string) => path.resolve(__dirname, p);
export default defineConfig({
  resolve: { alias: [
    { find: "@/web/Config", replacement: r("src/stubs/Config.ts") },
    { find: "@/web/background/IPC", replacement: r("src/stubs/IPC.ts") },
    { find: "@/web/overlay/widgets", replacement: r("src/stubs/widgets.ts") },
    { find: "@/web/overlay/interfaces", replacement: r("src/stubs/widgets.ts") },
    { find: /^@\/(.*)/, replacement: r("vendor/ee2/src") + "/$1" },
  ]},
});
```

- [ ] **Step 3: Write failing test**

```typescript
// test/parser.test.ts
import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

describe("parseClipboard golden fixtures", () => {
  for (const f of readdirSync(new URL("./fixtures", import.meta.url))) {
    it(`parses ${f}`, async () => {
      const { parseClipboard } = await import("@/parser");
      const text = readFileSync(new URL(`./fixtures/${f}`, import.meta.url), "utf8");
      const r = parseClipboard(text);
      expect(r.isOk(), r.isErr() ? String(r.error) : "").toBe(true);
      expect(r._unsafeUnwrap()).toMatchSnapshot();
    });
  }
});
```

- [ ] **Step 4: Run, inspect snapshots, commit**

Run: `npx vitest run` — first run writes snapshots; READ them: rarity/category/stats must match the fixture text. Garbage snapshot = fixture or stub bug, fix before committing.

```bash
git add -A && git commit -m "test(brain): parser golden fixtures + snapshots"
```

---

### Task 4: Brain server — socket + dispatch (ping, parse)

**Files:** Create: `brain/src/server.ts`, `brain/test/server.test.ts`

- [ ] **Step 1: Write failing test**

```typescript
// test/server.test.ts
import { afterAll, beforeAll, expect, it } from "vitest";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { startServer } from "../src/server";

const SOCK = path.join(os.tmpdir(), `brain-test-${process.pid}.sock`);
let close: () => Promise<void>;
beforeAll(async () => { close = await startServer(SOCK); });
afterAll(() => close());

function rpc(msg: object): Promise<any> {
  return new Promise((resolve, reject) => {
    const c = net.connect(SOCK, () => c.write(JSON.stringify(msg) + "\n"));
    let buf = "";
    c.on("data", (d) => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl >= 0) { c.end(); resolve(JSON.parse(buf.slice(0, nl))); }
    });
    c.on("error", reject);
  });
}

it("ping", async () => {
  expect(await rpc({ id: 1, cmd: "ping" })).toEqual({ id: 1, ok: true, result: "pong" });
});
it("parse magic wand", async () => {
  const { readFileSync } = await import("node:fs");
  const text = readFileSync(new URL("./fixtures/magic-wand.txt", import.meta.url), "utf8");
  const res = await rpc({ id: 2, cmd: "parse", clipboard: text });
  expect(res.ok).toBe(true);
  expect(res.result.rarity).toBe("Magic");
});
it("unknown cmd errors cleanly", async () => {
  const res = await rpc({ id: 3, cmd: "nope" });
  expect(res).toMatchObject({ id: 3, ok: false });
});
```

- [ ] **Step 2: Run to verify fail** — `npx vitest run test/server.test.ts` → FAIL (`startServer` missing).

- [ ] **Step 3: Implement**

```typescript
// src/server.ts
import net from "node:net";
import { unlink } from "node:fs/promises";
import { initBrainData } from "./bootstrap";

type Handler = (req: any) => Promise<unknown>;
const handlers: Record<string, Handler> = {
  ping: async () => "pong",
  parse: async (req) => {
    const { parseClipboard } = await import("@/parser");
    const r = parseClipboard(req.clipboard ?? "");
    if (r.isErr()) throw new Error(`not an item: ${r.error}`);
    return r.value;
  },
};

export async function startServer(sockPath: string): Promise<() => Promise<void>> {
  await initBrainData();
  await unlink(sockPath).catch(() => {});
  const server = net.createServer((conn) => {
    let buf = "";
    conn.on("data", async (d) => {
      buf += d;
      let nl;
      while ((nl = buf.indexOf("\n")) >= 0) {
        const line = buf.slice(0, nl); buf = buf.slice(nl + 1);
        let id: unknown = null;
        try {
          const req = JSON.parse(line); id = req.id;
          const h = handlers[req.cmd];
          if (!h) throw new Error(`unknown cmd: ${req.cmd}`);
          conn.write(JSON.stringify({ id, ok: true, result: await h(req) }) + "\n");
        } catch (e) {
          conn.write(JSON.stringify({ id, ok: false, error: String(e instanceof Error ? e.message : e) }) + "\n");
        }
      }
    });
  });
  await new Promise<void>((res) => server.listen(sockPath, res));
  return () => new Promise((res) => server.close(() => res()));
}

if (process.argv[1]?.endsWith("server.ts")) {
  const sock = process.env.BRAIN_SOCKET
    ?? `${process.env.XDG_RUNTIME_DIR ?? "/tmp"}/poe2-overlay-brain.sock`;
  startServer(sock).then(() => console.error(`brain listening on ${sock}`));
}
```

- [ ] **Step 4: Run tests** — `npx vitest run` → all PASS.

- [ ] **Step 5: Commit** — `git add -A && git commit -m "feat(brain): socket server with ping/parse"`

---

### Task 5: Price check — query build + trade API

The riskiest brain task: EE2's `createTradeRequest`/`requestTradeResultList`/`requestResults` in `vendor/ee2/src/web/price-check/trade/pathofexile-trade.ts` and filter creation in `filters/create-stat-filters.ts` + `filters/create-item-filters.ts`. Read THOSE exports only (grep for `export`), not whole files.

**Files:** Create: `brain/src/price.ts`, `brain/test/price.test.ts`. Modify: `brain/src/server.ts` (register handler).

- [ ] **Step 1: Locate the call chain EE2's UI uses**

Run: `grep -n "createFilters\|createTradeRequest\|requestTradeResultList\|requestResults" vendor/ee2/src/web/price-check/ -r | grep -v "\.vue"`
Map: `ParsedItem` → `createFilters(item, opts)` (filters/create-stat-filters.ts) → `createTradeRequest(filters, stats, item)` → `requestTradeResultList(body, league)` → `requestResults(ids, queryId)`. Exact signatures from the grep output — adapt `price.ts` below to what's actually exported.

- [ ] **Step 2: Failing test for query JSON (offline — no API call)**

```typescript
// test/price.test.ts
import { beforeAll, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";
import { buildQuery } from "../src/price";

beforeAll(initBrainData);

it("builds a trade2 query for the rare fixture", async () => {
  const text = readFileSync(new URL("./fixtures/rare-armour.txt", import.meta.url), "utf8");
  const q = await buildQuery(text);
  expect(q.query).toBeDefined();
  expect(q.query.status).toBeDefined();
  expect(JSON.stringify(q)).toMatchSnapshot();
});
```

- [ ] **Step 3: Implement `price.ts`**

```typescript
// src/price.ts — shape depends on Step 1 findings; this is the intended skeleton
import { setBrainConfig } from "./stubs/Config";

export async function buildQuery(clipboard: string) {
  const { parseClipboard } = await import("@/parser");
  const item = parseClipboard(clipboard)._unsafeUnwrap();
  const { createFilters } = await import("@/web/price-check/filters/create-item-filters");
  const { createStatFilters } = await import("@/web/price-check/filters/create-stat-filters");
  const { createTradeRequest } = await import("@/web/price-check/trade/pathofexile-trade");
  const opts = { league: "", currency: undefined, collapseListings: "api" } as any; // match real signature from Step 1
  const filters = createFilters(item, opts);
  const stats = createStatFilters(item, filters, opts);
  return createTradeRequest(filters, stats, item);
}

export async function priceCheck(clipboard: string, league: string) {
  setBrainConfig({ league });
  const { requestTradeResultList, requestResults } =
    await import("@/web/price-check/trade/pathofexile-trade");
  const query = await buildQuery(clipboard);
  const list = await requestTradeResultList(query as any, league);
  const listings = await requestResults(list.result.slice(0, 10), list.id);
  return { total: list.total, listings };
}
```

Register in `server.ts` handlers:

```typescript
price: async (req) => {
  const { priceCheck } = await import("./price");
  return priceCheck(req.clipboard ?? "", req.league ?? process.env.POE2_LEAGUE ?? "Standard");
},
```

- [ ] **Step 4: Offline test green** — `npx vitest run test/price.test.ts` → PASS, snapshot shows plausible trade2 query (stat ids, status online).

- [ ] **Step 5: ONE manual live check**

Run server (`npx tsx src/server.ts`), then:
`printf '{"id":9,"cmd":"price","clipboard":"%s"}\n' "$(cat test/fixtures/rare-armour.txt | python3 -c 'import json,sys; print(json.dumps(sys.stdin.read())[1:-1])')" | nc -U $XDG_RUNTIME_DIR/poe2-overlay-brain.sock`
Expected: listings JSON with prices. 400 from API = query-shape bug → compare against a browser devtools capture of pathofexile.com/trade2 search. Respect rate limits: ONE call per iteration, RateLimiter handles backoff.
League name must be the CURRENT PoE2 league — ask user if unsure.

- [ ] **Step 6: Commit** — `git add -A && git commit -m "feat(brain): price check via trade2 API"`

---

### Task 6: Bulk exchange

**Files:** Create: `brain/src/bulk.ts`. Modify: `brain/src/server.ts`.

- [ ] **Step 1: Locate bulk API** — `grep -n "export" vendor/ee2/src/web/price-check/trade/{bulk-api,pathofexile-bulk}.ts`

- [ ] **Step 2: Implement `bulk.ts` wrapping the exported search (have/want currency refs → offers), register `bulk` handler in server.ts following the `price` pattern:**

```typescript
bulk: async (req) => {
  const { bulkSearch } = await import("./bulk");
  return bulkSearch(req.have, req.want, req.league ?? process.env.POE2_LEAGUE ?? "Standard");
},
```

- [ ] **Step 3: Manual live check (one call, like Task 5 Step 5), then commit**

```bash
git add -A && git commit -m "feat(brain): bulk currency exchange"
```

---

### Task 7: poed skeleton — config, brain client, child lifecycle

**Files:** Create: `poed/poed/__init__.py` (empty), `poed/poed/config.py`, `poed/poed/brain.py`, `poed/poed/__main__.py`, `poed/tests/test_brain.py`, `poed/pyproject.toml`

- [ ] **Step 1: pyproject**

```toml
[project]
name = "poed"
version = "0.1.0"
requires-python = ">=3.12"
dependencies = ["PyGObject"]
[project.optional-dependencies]
dev = ["pytest"]
```

- [ ] **Step 2: config.py**

```python
import tomllib, pathlib

DEFAULTS = {
    "league": "Standard",
    "account_name": "",
    "hotkey_price": "SHIFT+space",
    "hotkey_bulk": "SHIFT+d",
    "panel_side": "right",
    "panel_width": 420,
    "game_window_class": "steam_app_2694490",  # PoE2 under Proton; verify with hyprctl
}

def load() -> dict:
    p = pathlib.Path.home() / ".config/poe2-overlay/config.toml"
    cfg = dict(DEFAULTS)
    if p.exists():
        cfg.update(tomllib.loads(p.read_text()))
    return cfg
```

- [ ] **Step 3: Failing test for brain client**

```python
# tests/test_brain.py
import os
from poed.brain import Brain

def test_ping_roundtrip(tmp_path):
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        assert b.request({"cmd": "ping"}) == "pong"
    finally:
        b.stop()

def test_parse_error_is_clean(tmp_path):
    sock = str(tmp_path / "b.sock")
    brain_dir = os.path.join(os.path.dirname(__file__), "../../brain")
    b = Brain(brain_dir=brain_dir, socket_path=sock)
    b.start()
    try:
        b.request({"cmd": "parse", "clipboard": "garbage"})
        assert False, "should raise"
    except RuntimeError as e:
        assert "not an item" in str(e)
    finally:
        b.stop()
```

- [ ] **Step 4: Run to verify fail** — `cd poed && python -m pytest tests/ -v` → FAIL (no `poed.brain`).

- [ ] **Step 5: Implement brain.py**

```python
import json, socket, subprocess, time, threading

class Brain:
    """Spawns the Node brain as a child and speaks JSON-lines to it."""

    def __init__(self, brain_dir: str, socket_path: str):
        self.brain_dir, self.socket_path = brain_dir, socket_path
        self.proc = None
        self._id = 0
        self._lock = threading.Lock()

    def start(self, timeout=15.0):
        self.proc = subprocess.Popen(
            ["npx", "tsx", "src/server.ts"],
            cwd=self.brain_dir,
            env={**__import__("os").environ, "BRAIN_SOCKET": self.socket_path},
        )
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                self._connect().close()
                return
            except OSError:
                if self.proc.poll() is not None:
                    raise RuntimeError(f"brain exited rc={self.proc.returncode}")
                time.sleep(0.2)
        raise TimeoutError("brain did not come up")

    def _connect(self):
        s = socket.socket(socket.AF_UNIX)
        s.connect(self.socket_path)
        return s

    def request(self, msg: dict, timeout=30.0):
        with self._lock:
            self._id += 1
            msg = {"id": self._id, **msg}
        s = self._connect()
        s.settimeout(timeout)
        try:
            s.sendall((json.dumps(msg) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(65536)
                if not chunk:
                    raise RuntimeError("brain closed connection")
                buf += chunk
            resp = json.loads(buf.split(b"\n", 1)[0])
        finally:
            s.close()
        if not resp.get("ok"):
            raise RuntimeError(resp.get("error", "unknown brain error"))
        return resp["result"]

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            try:
                self.proc.wait(5)
            except subprocess.TimeoutExpired:
                self.proc.kill()
```

- [ ] **Step 6: Tests pass** — `python -m pytest tests/ -v` → 2 PASS. (First run slow: brain loads 2.4MB data.)

- [ ] **Step 7: Minimal `__main__.py`**

```python
import os, sys
from poed import config
from poed.brain import Brain

def main():
    cfg = config.load()
    sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "poe2-overlay-brain.sock")
    brain = Brain(brain_dir=os.path.join(os.path.dirname(__file__), "../../brain"), socket_path=sock)
    brain.start()
    print("brain up:", brain.request({"cmd": "ping"}))
    return brain, cfg

if __name__ == "__main__":
    main()
```

- [ ] **Step 8: Commit** — `git add -A && git commit -m "feat(poed): config + brain child lifecycle + socket client"`

---

### Task 8: portal.py — productionize the hotkey spike

**Files:** Create: `poed/poed/portal.py`. Modify: `poed/poed/__main__.py`. Reference: `spikes/spike_portal.py` findings (exact DBus sequence recorded in Task 0).

- [ ] **Step 1: Implement `GlobalShortcuts` class from spike findings**

Shape (exact DBus calls from the spike — do not re-derive):

```python
class GlobalShortcuts:
    """xdg-desktop-portal GlobalShortcuts session. on_activated(shortcut_id) callback."""
    def __init__(self, app_id: str, on_activated):
        ...
    def bind(self, shortcuts: list[tuple[str, str, str]]):
        """[(id, description, preferred_trigger)] e.g. ("price-check", "PoE2 price check", "SHIFT+space")"""
        ...
```

- [ ] **Step 2: Wire into `__main__.py`**: GLib main loop, bind `price-check` + `bulk` from config, callback prints shortcut id for now.

- [ ] **Step 3: Manual verify**: run `python -m poed`, press both hotkeys (other window focused), see ids printed.

- [ ] **Step 4: Commit** — `git commit -am "feat(poed): portal global shortcuts"`

---

### Task 9: capture.py — focused-window guard, Ctrl+C inject, clipboard read

**Files:** Create: `poed/poed/capture.py`, `poed/tests/test_capture.py`. Modify: `poed/poed/__main__.py`.

Requires: `xdotool` installed (`sudo pacman -S xdotool`) — PoE2 runs under Proton (XWayland), so xdotool can target its window.

- [ ] **Step 1: Failing test (parse-side only — injection itself is manual-verify)**

```python
# tests/test_capture.py
from poed.capture import is_game_focused

def test_guard_parses_hyprctl_json():
    fake = '{"class": "steam_app_2694490", "title": "Path of Exile 2"}'
    assert is_game_focused("steam_app_2694490", _raw=fake)
    assert not is_game_focused("steam_app_2694490", _raw='{"class": "firefox"}')
```

- [ ] **Step 2: Implement**

```python
import json, subprocess

def is_game_focused(game_class: str, _raw: str | None = None) -> bool:
    raw = _raw or subprocess.run(
        ["hyprctl", "activewindow", "-j"], capture_output=True, text=True
    ).stdout
    try:
        return json.loads(raw).get("class") == game_class
    except json.JSONDecodeError:
        return False

def grab_item_text(game_class: str) -> str | None:
    """Inject Ctrl+C into the focused game window, return clipboard text."""
    if not is_game_focused(game_class):
        return None
    subprocess.run(["xdotool", "getactivewindow", "key", "--clearmodifiers", "ctrl+c"], check=True)
    out = subprocess.run(["wl-paste", "--no-newline"], capture_output=True, text=True, timeout=2)
    text = out.stdout
    return text if text.startswith("Item Class:") else None
```

- [ ] **Step 3: Test green, wire hotkey→grab→brain parse in `__main__.py`, manual verify in game (or any XWayland window with fake guard class), commit**

```bash
git commit -am "feat(poed): item text capture via xdotool + wl-paste"
```

Note: timing matters — game needs a beat to populate clipboard after Ctrl+C. If `wl-paste` returns stale content, add `time.sleep(0.05)` between inject and read; if still flaky, retry-loop comparing against previous clipboard content (max 3 × 50ms).

---

### Task 10: overlay.py — GTK4 layer-shell panel

**Files:** Create: `poed/poed/overlay.py`. Modify: `poed/poed/__main__.py`. Reference: `spikes/spike_layershell.py`.

- [ ] **Step 1: Implement panel from spike**

```python
import gi
gi.require_version("Gtk", "4.0")
gi.require_version("Gtk4LayerShell", "1.0")
from gi.repository import Gtk, GLib, Gtk4LayerShell as LayerShell

class OverlayPanel:
    """Layer-shell results panel. show_price(result), show_error(msg), hide()."""

    def __init__(self, app: Gtk.Application, side: str = "right", width: int = 420):
        self.win = Gtk.Window(application=app)
        LayerShell.init_for_window(self.win)
        LayerShell.set_layer(self.win, LayerShell.Layer.OVERLAY)
        LayerShell.set_keyboard_mode(self.win, LayerShell.KeyboardMode.ON_DEMAND)
        edge = LayerShell.Edge.RIGHT if side == "right" else LayerShell.Edge.LEFT
        LayerShell.set_anchor(self.win, edge, True)
        self.win.set_default_size(width, 700)
        self.box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        scroll = Gtk.ScrolledWindow(child=self.box, vexpand=True)
        self.win.set_child(scroll)
        ctl = Gtk.EventControllerKey()
        ctl.connect("key-pressed", lambda _c, kv, *_: self.hide() if kv == 65307 else None)
        self.win.add_controller(ctl)

    def _clear(self):
        while (c := self.box.get_first_child()) is not None:
            self.box.remove(c)

    def show_price(self, result: dict):
        self._clear()
        listings = result.get("listings", [])
        self.box.append(Gtk.Label(label=f"{result.get('total', 0)} listings", xalign=0.0))
        for l in listings:
            # listing shape comes from EE2 requestResults — adjust keys after Task 5 live check
            price = l.get("priceAmount"), l.get("priceCurrency")
            acct = l.get("accountName", "?")
            self.box.append(Gtk.Label(label=f"{price[0]} {price[1]}  —  {acct}", xalign=0.0))
        self.win.present()

    def show_error(self, msg: str):
        self._clear()
        self.box.append(Gtk.Label(label=f"⚠ {msg}", xalign=0.0))
        self.win.present()
        GLib.timeout_add(2500, self.hide)

    def hide(self, *_):
        self.win.set_visible(False)
        return False

    def toggle_or_show(self):
        if self.win.get_visible():
            self.hide()
        else:
            self.win.present()
```

- [ ] **Step 2: Manual verify with canned data** (no game): temporary `__main__` flag `--demo` calling `show_price({"total": 2, "listings": [{"priceAmount": 5, "priceCurrency": "exalted", "accountName": "test"}]})`.

- [ ] **Step 3: Commit** — `git commit -am "feat(poed): layer-shell results panel"`

---

### Task 11: End-to-end wiring

**Files:** Modify: `poed/poed/__main__.py`.

- [ ] **Step 1: Full flow**

```python
import os, threading
import gi
gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib
from poed import config
from poed.brain import Brain
from poed.capture import grab_item_text
from poed.overlay import OverlayPanel
from poed.portal import GlobalShortcuts

def main():
    cfg = config.load()
    sock = os.path.join(os.environ.get("XDG_RUNTIME_DIR", "/tmp"), "poe2-overlay-brain.sock")
    brain = Brain(brain_dir=os.path.join(os.path.dirname(__file__), "../../brain"), socket_path=sock)
    brain.start()

    app = Gtk.Application(application_id="io.github.kriskruse.waystone")

    def on_activate(app):
        panel = OverlayPanel(app, cfg["panel_side"], cfg["panel_width"])

        def on_hotkey(shortcut_id):
            if shortcut_id != "price-check":
                return
            text = grab_item_text(cfg["game_window_class"])
            if text is None:
                panel.show_error("not an item (or game not focused)")
                return
            def work():
                try:
                    res = brain.request({"cmd": "price", "clipboard": text, "league": cfg["league"]})
                    GLib.idle_add(panel.show_price, res)
                except RuntimeError as e:
                    GLib.idle_add(panel.show_error, str(e))
            threading.Thread(target=work, daemon=True).start()

        shortcuts = GlobalShortcuts("io.github.kriskruse.waystone", on_hotkey)
        shortcuts.bind([
            ("price-check", "PoE2 price check", cfg["hotkey_price"]),
            ("bulk", "PoE2 bulk exchange", cfg["hotkey_bulk"]),
        ])
        app.hold()  # stay alive without visible window

    app.connect("activate", on_activate)
    try:
        app.run(None)
    finally:
        brain.stop()

if __name__ == "__main__":
    main()
```

- [ ] **Step 2: Brain crash resilience** — in `Brain.request`, catch `ConnectionRefusedError`/`FileNotFoundError`, call `self.start()` once, retry the request once. Add pytest: kill `b.proc`, assert next `request` still succeeds.

- [ ] **Step 3: Full manual run with game**: launch PoE2, `python -m poed`, hover item, Shift+Space → panel with prices. Fix the listing-key mismatches between brain output and `show_price` here (real shapes now visible).

- [ ] **Step 4: Commit** — `git commit -am "feat: end-to-end price check"`

---

### Task 12: Bulk view + README

**Files:** Modify: `poed/poed/overlay.py` (`show_bulk(result)` — same pattern as `show_price`: have/want rates list), `poed/poed/__main__.py` (route `"bulk"` shortcut id: skip clipboard, call `{"cmd": "bulk", "have": ..., "want": ...}` with config defaults `bulk_have = "exalted"`, `bulk_want = "divine"` added to `config.py` DEFAULTS). Create: `README.md`.

- [ ] **Step 1: Implement + manual verify bulk hotkey**

- [ ] **Step 2: README**: what it is, Arch deps (`sudo pacman -S python-gobject gtk4 gtk4-layer-shell xdg-desktop-portal-hyprland wl-clipboard xdotool`), node via mise OK, `cd brain && npm install`, config.toml example (league! game_window_class — find via `hyprctl activewindow` with game running), run `python -m poed`, data-update procedure (pointer to `brain/vendor/ee2/PROVENANCE.md`).

- [ ] **Step 3: Commit** — `git add -A && git commit -m "feat: bulk exchange view + README"`

---

## Task dependency graph

```
0 (portal spike) ──┬─→ 8 (portal.py)
1 (layershell spike) ─→ 10 (overlay.py)
2 (brain compiles) → 3 (parser tests) → 4 (server) → 5 (price) → 6 (bulk)
4 ─→ 7 (poed skeleton) → 8 → 9 (capture) → 11 (e2e) → 12 (bulk view)
```

Run order: 0, 1 first (cheap, kill-risks). Then 2→6 (brain). Then 7→12 (poed). Tasks 0/1 independent of 2/3 — parallelizable if desired.

## Known unknowns (resolve at the marked task, nowhere else)

- Exact EE2 filter/trade function signatures → Task 5 Step 1
- Trade listing JSON keys → Task 5 Step 5 / Task 11 Step 3
- PoE2 window class under Proton → Task 9 (via `hyprctl activewindow`)
- Current league name → ask user at Task 5
- Portal DBus exact sequence → Task 0 (spike's whole purpose)
