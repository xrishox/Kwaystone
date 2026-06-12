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
  scoutPrice,
  uniquePriceMap,
  _clearCache,
  _clearUniqueCache,
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
          { ApiId: "greater-exalted-orb", CurrentPrice: 5.8, CurrentQuantity: 33173 },
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
});

afterEach(() => {
  _clearCache();
  _clearUniqueCache();
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
  expect(m1.get("greater-exalted-orb")).toEqual({ price: 5.8, quantity: 33173, history: [] });
  expect(m1.get("mirror")).toEqual({ price: 12000, quantity: 4, history: [] });
});

it("priceMap: refetches after the cache is cleared (TTL expiry seam)", async () => {
  wireHappyPath();
  await priceMap(LEAGUE);
  const before = proxy.mock.calls.length;
  _clearCache();
  await priceMap(LEAGUE);
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
  expect(m.size).toBe(2);
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
