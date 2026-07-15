import { afterEach, beforeEach, expect, it, vi } from "vitest";

// Mock the shared fetch proxy so tests never touch the live poe2scout API.
// Host.proxy(url) -> Response; we drive responses per-URL via `routes`.
const proxy = vi.fn();
vi.mock("../src/stubs/IPC", () => ({
  Host: { proxy: (...a: any[]) => proxy(...a), isElectron: false },
}));

import {
  divinePrice,
  priceMap,
  priceMapDetailed,
  refreshPriceMap,
  refreshUniquePriceMap,
  scoutPrice,
  uniquePriceMap,
  uniquePriceMapDetailed,
  SCOUT_RETRY_TTL_MS,
  SCOUT_TTL_MS,
  _clearCache,
  _clearUniqueCache,
  _setRetryBaseMs,
} from "../src/poe2scout";

const LEAGUE = "Runes of Aldur";

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

// A minimal poe2scout response set: a Leagues array, a Categories doc with a
// single currency category, and that category's currency page.
function wireHappyPath() {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({
        CurrencyCategories: [{ ApiId: "currency" }],
        UniqueCategories: [{ ApiId: "weapon" }],
      });
    }
    if (u.includes("/Currencies/ByCategory")) {
      return json({
        CurrentPage: 1,
        Pages: 1,
        Total: 2,
        Items: [
          {
            ApiId: "greater-exalted-orb",
            CurrentPrice: 5.8,
            CurrentQuantity: 33173,
            Text: "Greater Exalted Orb",
            ItemMetadata: {
              icon: "https://web.poecdn.com/gen/image/greater-exalted.png",
            },
          },
          { ApiId: "mirror", CurrentPrice: 12000, CurrentQuantity: 4 },
        ],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([
        { Value: "Dawn of the Hunt", IsCurrent: false, DivinePrice: 1828.4 },
        { Value: "Runes of Aldur", IsCurrent: true, DivinePrice: 100.2 },
      ]);
    }
    throw new Error(`unexpected url: ${u}`);
  });
}

beforeEach(() => {
  _clearCache();
  _clearUniqueCache();
  proxy.mockReset();
  // Keep transient-failure retries fast in tests.
  _setRetryBaseMs(1);
});

afterEach(() => {
  _clearCache();
  _clearUniqueCache();
  _setRetryBaseMs(300);
  vi.useRealTimers();
});

it("divinePrice: exact Value match", async () => {
  wireHappyPath();
  expect(await divinePrice("Runes of Aldur")).toBe(100.2);
});

it("divinePrice: IsCurrent fallback when no exact match", async () => {
  wireHappyPath();
  expect(await divinePrice("Nonexistent League")).toBe(100.2);
});

it("divinePrice: null when the leagues fetch fails", async () => {
  proxy.mockImplementation(async () => {
    throw new Error("network down");
  });
  expect(await divinePrice(LEAGUE)).toBeNull();
});

it("priceMap: warms once within TTL (dedupes/caches)", async () => {
  wireHappyPath();
  const m1 = await priceMap(LEAGUE);
  const callsAfterFirst = proxy.mock.calls.length;
  expect(callsAfterFirst).toBeGreaterThan(0);

  const m2 = await priceMap(LEAGUE);
  // Second call within TTL must not issue any new requests.
  expect(proxy.mock.calls.length).toBe(callsAfterFirst);

  expect(m1).toBe(m2);
  expect(m1.get("greater-exalted-orb")).toEqual({
    price: 5.8,
    quantity: 33173,
    history: [],
    name: "Greater Exalted Orb",
    iconUrl: "https://web.poecdn.com/gen/image/greater-exalted.png",
    category: "currency",
  });
  expect(m1.get("mirror")).toEqual({
    price: 12000,
    quantity: 4,
    history: [],
    category: "currency",
  });
});

it("priceMap: refetches after the cache is cleared (TTL expiry seam)", async () => {
  wireHappyPath();
  await priceMap(LEAGUE);
  const before = proxy.mock.calls.length;
  _clearCache();
  await priceMap(LEAGUE);
  expect(proxy.mock.calls.length).toBeGreaterThan(before);
});

it("refreshPriceMap: force-refreshes before TTL expiry", async () => {
  wireHappyPath();
  await priceMap(LEAGUE);
  const before = proxy.mock.calls.length;
  await refreshPriceMap(LEAGUE);
  expect(proxy.mock.calls.length).toBeGreaterThan(before);
});

it("priceMap: never throws on fetch failure; returns empty map", async () => {
  proxy.mockImplementation(async () => {
    throw new Error("boom");
  });
  const m = await priceMap(LEAGUE);
  expect(m.size).toBe(0);
});

it("priceMap: a non-OK response yields an empty map, no throw", async () => {
  proxy.mockImplementation(async () => json({ detail: "nope" }, 500));
  const m = await priceMap(LEAGUE);
  expect(m.size).toBe(0);
});

it("scoutPrice: hit returns the cached entry", async () => {
  wireHappyPath();
  expect(await scoutPrice("greater-exalted-orb", LEAGUE)).toEqual({
    price: 5.8,
    quantity: 33173,
    history: [],
    name: "Greater Exalted Orb",
    iconUrl: "https://web.poecdn.com/gen/image/greater-exalted.png",
    category: "currency",
  });
});

it("scoutPrice: miss returns null", async () => {
  wireHappyPath();
  expect(await scoutPrice("not-a-currency", LEAGUE)).toBeNull();
});

it("scoutPrice: null when the warm failed entirely", async () => {
  proxy.mockImplementation(async () => {
    throw new Error("down");
  });
  expect(await scoutPrice("greater-exalted-orb", LEAGUE)).toBeNull();
});

it("PriceLogs: history reversed to chronological order, nulls dropped", async () => {
  // Wire a ByCategory response with PriceLogs on the item — newest-first as
  // poe2scout returns them. One Price is null (must be dropped).
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({ CurrencyCategories: [{ ApiId: "currency" }], UniqueCategories: [] });
    }
    if (u.includes("/Currencies/ByCategory")) {
      return json({
        CurrentPage: 1, Pages: 1, Total: 1,
        Items: [
          {
            ApiId: "exalted",
            CurrentPrice: 1.0,
            CurrentQuantity: 500,
            PriceLogs: [
              { Price: 1.2, Time: "2026-06-09T12:00:00Z", Quantity: 400 },
              { Price: null, Time: "2026-06-09T06:00:00Z", Quantity: 300 },
              { Price: 0.9, Time: "2026-06-09T00:00:00Z", Quantity: 350 },
              { Price: 0.8, Time: "2026-06-08T18:00:00Z", Quantity: 200 },
            ],
          },
        ],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });

  const entry = await scoutPrice("exalted", LEAGUE);
  // history must be chronological (oldest→newest), null dropped:
  // poe2scout order: [1.2, null, 0.9, 0.8] → reversed: [0.8, 0.9, 1.2]
  expect(entry).not.toBeNull();
  expect(entry!.history).toEqual([0.8, 0.9, 1.2]);
  // price + quantity still present
  expect(entry!.price).toBe(1.0);
  expect(entry!.quantity).toBe(500);
  expect(entry!.category).toBe("currency");
});

function wireUniques() {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({
        CurrencyCategories: [],
        UniqueCategories: [{ ApiId: "accessory" }, { ApiId: "weapon" }],
      });
    }
    if (u.includes("/Uniques/ByCategory") && u.includes("Category=accessory")) {
      return json({
        CurrentPage: 1, Pages: 1, Total: 1,
        Items: [{
          Name: "Mageblood",
          IconUrl: "https://web.poecdn.com/gen/image/MB.png",
          // newest-first, as the live API returns them
          PriceLogs: [
            { Price: 67305.6, Time: "2026-06-11T00:00:00", Quantity: 1386 },
            { Price: 62712.5, Time: "2026-06-10T00:00:00", Quantity: 2464 },
          ],
        }],
      });
    }
    if (u.includes("/Uniques/ByCategory") && u.includes("Category=weapon")) {
      return json({
        CurrentPage: 1, Pages: 1, Total: 1,
        Items: [{
          Name: "Splinter of Loratta",
          IconUrl: "https://web.poecdn.com/gen/image/SL.png",
          PriceLogs: [{ Price: 12.5, Time: "t", Quantity: 50 }],
        }, {
          // Young-league shape: listed with all-null PriceLogs but a live
          // CurrentPrice — must fall back, not vanish into "no market price".
          Name: "Lightning Coil",
          IconUrl: "https://web.poecdn.com/gen/image/LC.png",
          PriceLogs: [null, null, null],
          CurrentPrice: 1618.5,
          CurrentQuantity: 62,
        }, {
          // No price signal at all: stays out of the map.
          Name: "Ghost Item",
          IconUrl: "https://web.poecdn.com/gen/image/GI.png",
          PriceLogs: [null],
          CurrentPrice: 0,
        }],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });
}

it("uniquePriceMap: pulls all unique categories, keys by Name, newest price", async () => {
  wireUniques();
  const m = await uniquePriceMap(LEAGUE);
  expect(m.size).toBe(3);
  expect(m.get("Lightning Coil")).toEqual({
    price: 1618.5,
    quantity: 62,
    iconUrl: "https://web.poecdn.com/gen/image/LC.png",
    trend: null,
  });
  expect(m.has("Ghost Item")).toBe(false);
  expect(m.get("Mageblood")).toEqual({
    price: 67305.6,
    quantity: 1386,
    iconUrl: "https://web.poecdn.com/gen/image/MB.png",
    // newest 67305.6 vs oldest 62712.5 -> +7.3%
    trend: expect.closeTo(0.0732, 3),
  });
  expect(m.get("Splinter of Loratta")!.price).toBe(12.5);
  // single log entry -> no trend
  expect(m.get("Splinter of Loratta")!.trend).toBeNull();
});

it("uniquePriceMap: caches within TTL, no second fetch", async () => {
  wireUniques();
  const m1 = await uniquePriceMap(LEAGUE);
  const calls = proxy.mock.calls.length;
  const m2 = await uniquePriceMap(LEAGUE);
  expect(proxy.mock.calls.length).toBe(calls);
  expect(m1).toBe(m2);
});

it("refreshUniquePriceMap: force-refreshes before TTL expiry", async () => {
  wireUniques();
  await uniquePriceMap(LEAGUE);
  const before = proxy.mock.calls.length;
  await refreshUniquePriceMap(LEAGUE);
  expect(proxy.mock.calls.length).toBeGreaterThan(before);
});

it("uniquePriceMap: never throws on fetch failure; returns empty map", async () => {
  proxy.mockImplementation(async () => {
    throw new Error("down");
  });
  const m = await uniquePriceMap(LEAGUE);
  expect(m.size).toBe(0);
});

it("PriceLogs: missing PriceLogs yields empty history", async () => {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({ CurrencyCategories: [{ ApiId: "currency" }], UniqueCategories: [] });
    }
    if (u.includes("/Currencies/ByCategory")) {
      return json({
        CurrentPage: 1, Pages: 1, Total: 1,
        Items: [{ ApiId: "exalted", CurrentPrice: 1.0, CurrentQuantity: 500 }],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });

  const entry = await scoutPrice("exalted", LEAGUE);
  expect(entry).not.toBeNull();
  expect(entry!.history).toEqual([]);
});

it("priceMap: retains live metadata for items newer than vendored data", async () => {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({ CurrencyCategories: [{ ApiId: "verisium" }], UniqueCategories: [] });
    }
    if (u.includes("/Currencies/ByCategory")) {
      return json({
        CurrentPage: 1,
        Pages: 1,
        Total: 1,
        Items: [{
          ApiId: "swift-alloy",
          Text: "Swift Alloy",
          CurrentPrice: 4.5,
          CurrentQuantity: 455,
          ItemMetadata: {
            icon: "https://web.poecdn.com/gen/image/swift-alloy.png",
          },
        }],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });

  expect(await scoutPrice("swift-alloy", LEAGUE)).toEqual({
    price: 4.5,
    quantity: 455,
    history: [],
    name: "Swift Alloy",
    iconUrl: "https://web.poecdn.com/gen/image/swift-alloy.png",
    category: "verisium",
  });
});

it("priceMap: universally prefers an active liquid native-currency quote", async () => {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({
        CurrencyCategories: [{ ApiId: "items" }],
        UniqueCategories: [],
      });
    }
    if (u.endsWith("/SnapshotPairs")) {
      return json([
        {
          BaseCurrencyApiId: "exalted",
          CurrencyOne: {
            ApiId: "sample-item",
            CategoryApiId: "items",
          },
          CurrencyTwo: { ApiId: "exalted", CategoryApiId: "currency" },
          CurrencyOneData: {
            RelativePrice: "2.54545455",
            VolumeTraded: 33,
            HighestStock: 780,
          },
          CurrencyTwoData: {
            RelativePrice: "1.147",
            VolumeTraded: 84,
            HighestStock: 0,
          },
        },
        {
          BaseCurrencyApiId: "exalted",
          Volume: "21.176",
          CurrencyOne: {
            ApiId: "chaos",
            Text: "Chaos Orb",
            CategoryApiId: "currency",
          },
          CurrencyTwo: {
            ApiId: "sample-item",
            CategoryApiId: "items",
          },
          CurrencyOneData: {
            RelativePrice: "21.176",
            VolumeTraded: 1,
            HighestStock: 20,
          },
          CurrencyTwoData: {
            RelativePrice: "7.05880206",
            VolumeTraded: 3,
            HighestStock: 57,
          },
        },
        {
          BaseCurrencyApiId: "exalted",
          Volume: "50",
          CurrencyOne: {
            ApiId: "swift-alloy",
            CategoryApiId: "items",
          },
          CurrencyTwo: {
            ApiId: "exalted",
            Text: "Exalted Orb",
            CategoryApiId: "currency",
          },
          CurrencyOneData: {
            RelativePrice: "5",
            VolumeTraded: 10,
            HighestStock: 100,
          },
          CurrencyTwoData: {
            RelativePrice: "1",
            VolumeTraded: 50,
            HighestStock: 0,
          },
        },
      ]);
    }
    if (u.includes("/Currencies/ByCategory")) {
      return json({
        CurrentPage: 1,
        Pages: 1,
        Total: 2,
        Items: [
          {
            ApiId: "sample-item",
            Text: "Sample Item",
            CurrentPrice: 21.595,
            CurrentQuantity: 69,
          },
          {
            ApiId: "swift-alloy",
            Text: "Swift Alloy",
            CurrentPrice: 99,
            CurrentQuantity: 20,
          },
        ],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });

  const prices = await priceMap(LEAGUE);

  expect(prices.get("sample-item")).toMatchObject({
    price: 7.05880206,
    quoteAmount: 1 / 3,
    quoteCurrency: "chaos",
    quoteCurrencyText: "Chaos Orb",
    quoteLiquidity: 21.176,
    quoteBuyerStock: 20,
    quoteAvailable: true,
    category: "items",
  });
  expect(prices.get("swift-alloy")).toMatchObject({
    price: 5,
    quantity: 20,
    quoteAmount: 5,
    quoteCurrency: "exalted",
    quoteCurrencyText: "Exalted Orb",
    quoteLiquidity: 50,
    quoteBuyerStock: 0,
    quoteAvailable: false,
  });
});

// ---- Outage robustness ------------------------------------------------
// poe2scout 503s endpoints individually and intermittently. One bad category
// must never wipe every price ("no market price" across a whole Alt+X scan),
// and degraded snapshots must retry quickly instead of waiting out a full TTL.

// Wire a two-category currency league; `failing` categories 503 persistently.
// Prices are read from `prices` so recovery tests can change them per warm.
function wireCategories(
  failing: Set<string>,
  prices: { exalted: number; "breach-splinter": number },
) {
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({
        CurrencyCategories: [{ ApiId: "currency" }, { ApiId: "breach" }],
        UniqueCategories: [],
      });
    }
    if (u.endsWith("/SnapshotPairs")) return json([]);
    if (u.includes("/Currencies/ByCategory")) {
      const cat = u.includes("Category=breach") ? "breach" : "currency";
      if (failing.has(cat)) return json({ detail: "unavailable" }, 503);
      const item =
        cat === "breach"
          ? { ApiId: "breach-splinter", CurrentPrice: prices["breach-splinter"], CurrentQuantity: 100 }
          : { ApiId: "exalted", CurrentPrice: prices.exalted, CurrentQuantity: 500 };
      return json({ CurrentPage: 1, Pages: 1, Total: 1, Items: [item] });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });
}

it("retries transient 503s within a request: flaky endpoint, complete map", async () => {
  let byCategoryAttempts = 0;
  proxy.mockImplementation(async (url: string) => {
    const u = String(url);
    if (u.includes("/Leagues/") && u.includes("/Items/Categories")) {
      return json({ CurrencyCategories: [{ ApiId: "currency" }], UniqueCategories: [] });
    }
    if (u.endsWith("/SnapshotPairs")) return json([]);
    if (u.includes("/Currencies/ByCategory")) {
      byCategoryAttempts += 1;
      if (byCategoryAttempts < 3) return json({ detail: "unavailable" }, 503);
      return json({
        CurrentPage: 1, Pages: 1, Total: 1,
        Items: [{ ApiId: "exalted", CurrentPrice: 1, CurrentQuantity: 500 }],
      });
    }
    if (u.endsWith("/Leagues")) {
      return json([{ Value: LEAGUE, IsCurrent: true, DivinePrice: 100 }]);
    }
    throw new Error(`unexpected url: ${u}`);
  });

  const { map, complete } = await priceMapDetailed(LEAGUE);
  expect(byCategoryAttempts).toBe(3);
  expect(complete).toBe(true);
  expect(map.get("exalted")!.price).toBe(1);
});

it("does not retry non-transient statuses", async () => {
  let attempts = 0;
  proxy.mockImplementation(async () => {
    attempts += 1;
    return json({ detail: "gone" }, 404);
  });
  expect(await divinePrice(LEAGUE)).toBeNull();
  expect(attempts).toBe(1);
});

it("skips a persistently failing category; other categories' prices survive", async () => {
  wireCategories(new Set(["breach"]), { exalted: 1, "breach-splinter": 0.5 });
  const { map, complete } = await priceMapDetailed(LEAGUE);
  expect(complete).toBe(false);
  expect(map.get("exalted")!.price).toBe(1);
  expect(map.has("breach-splinter")).toBe(false); // nothing to backfill from yet
});

it("degraded pulls backfill from last-good and recover on the retry TTL", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });

  // Warm 1: everything healthy.
  wireCategories(new Set(), { exalted: 1, "breach-splinter": 0.5 });
  expect((await priceMapDetailed(LEAGUE)).complete).toBe(true);

  // Warm 2 (past the full TTL): breach is down; its price must survive from
  // the last good pull while currency updates live.
  vi.setSystemTime(Date.now() + SCOUT_TTL_MS + 1000);
  wireCategories(new Set(["breach"]), { exalted: 2, "breach-splinter": 0.5 });
  const degraded = await priceMapDetailed(LEAGUE);
  expect(degraded.complete).toBe(false);
  expect(degraded.map.get("exalted")!.price).toBe(2);
  expect(degraded.map.get("breach-splinter")!.price).toBe(0.5); // backfilled

  // Within the retry window: cached, the API is not hammered.
  const calls = proxy.mock.calls.length;
  await priceMapDetailed(LEAGUE);
  expect(proxy.mock.calls.length).toBe(calls);

  // Warm 3 (past only the SHORT retry TTL): breach recovered — fresh prices
  // long before the normal 15-minute window.
  vi.setSystemTime(Date.now() + SCOUT_RETRY_TTL_MS + 1000);
  wireCategories(new Set(), { exalted: 3, "breach-splinter": 0.75 });
  const recovered = await priceMapDetailed(LEAGUE);
  expect(recovered.complete).toBe(true);
  expect(recovered.map.get("exalted")!.price).toBe(3);
  expect(recovered.map.get("breach-splinter")!.price).toBe(0.75);
});

it("a total failure is cached only for the retry TTL, not the full window", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  proxy.mockImplementation(async () => {
    throw new Error("down");
  });
  expect((await priceMap(LEAGUE)).size).toBe(0);

  // Within the retry window: served from cache, no hammering.
  const calls = proxy.mock.calls.length;
  await priceMap(LEAGUE);
  expect(proxy.mock.calls.length).toBe(calls);

  // Past the retry window the API recovered; prices flow again quickly.
  vi.setSystemTime(Date.now() + SCOUT_RETRY_TTL_MS + 1000);
  wireHappyPath();
  const m = await priceMap(LEAGUE);
  expect(m.get("mirror")!.price).toBe(12000);
});

it("unique categories are independently best-effort and backfill last-good", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });

  wireUniques();
  const first = await uniquePriceMapDetailed(LEAGUE);
  expect(first.complete).toBe(true);
  expect(first.map.get("Mageblood")!.price).toBe(67305.6);

  // Past the full TTL, the weapon category goes down: accessory data flows,
  // weapon uniques survive from the last good pull, snapshot marked degraded.
  vi.setSystemTime(Date.now() + SCOUT_TTL_MS + 1000);
  const healthy = proxy.getMockImplementation()!;
  proxy.mockImplementation(async (url: string) => {
    if (String(url).includes("Category=weapon")) {
      return json({ detail: "unavailable" }, 503);
    }
    return healthy(url);
  });
  const degraded = await uniquePriceMapDetailed(LEAGUE);
  expect(degraded.complete).toBe(false);
  expect(degraded.map.get("Mageblood")!.price).toBe(67305.6);
  expect(degraded.map.get("Lightning Coil")!.price).toBe(1618.5); // backfilled
});
