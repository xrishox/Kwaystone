import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

// Mock the poe2scout data layer: no live API in tests.
vi.mock("../src/poe2scout", () => ({
  divinePrice: vi.fn(async () => 237),
  snapshotPairsRaw: vi.fn(async () => PAIRS),
  SCOUT_TTL_MS: 900_000,
}));

import { arbQuote, arbState, _clearArbStates, _setScheduler } from "../src/arbitrage";
import { Scheduler } from "../src/scheduler";

const PAIRS = [
  {
    BaseCurrencyApiId: "exalted",
    Volume: 100,
    CurrencyOne: { ApiId: "chaos", Text: "Chaos Orb", CategoryApiId: "currency" },
    CurrencyOneData: { RelativePrice: 0.04, StockValue: 100, VolumeTraded: 1000, HighestStock: 500 },
    CurrencyTwo: { ApiId: "exalted", Text: "Exalted Orb", CategoryApiId: "currency" },
    CurrencyTwoData: { RelativePrice: 1, StockValue: 100, VolumeTraded: 1000, HighestStock: 500 },
  },
  {
    BaseCurrencyApiId: "exalted",
    Volume: 90,
    CurrencyOne: { ApiId: "divine", Text: "Divine Orb", CategoryApiId: "currency" },
    CurrencyOneData: { RelativePrice: 237, StockValue: 90, VolumeTraded: 900, HighestStock: 100 },
    CurrencyTwo: { ApiId: "chaos", Text: "Chaos Orb", CategoryApiId: "currency" },
    CurrencyTwoData: { RelativePrice: 0.04, StockValue: 80, VolumeTraded: 800, HighestStock: 400 },
  },
  {
    BaseCurrencyApiId: "exalted",
    Volume: 50,
    CurrencyOne: { ApiId: "vaal", Text: "Vaal Orb", CategoryApiId: "currency" },
    CurrencyOneData: { RelativePrice: 21.6, StockValue: 50, VolumeTraded: 500, HighestStock: 12000 },
    CurrencyTwo: { ApiId: "divine", Text: "Divine Orb", CategoryApiId: "currency" },
    CurrencyTwoData: { RelativePrice: 237, StockValue: 40, VolumeTraded: 400, HighestStock: 90 },
  },
];

const DIVINE_TEXT = `Item Class: Currency
Rarity: Currency
Divine Orb
--------
Stack Size: 5/20
`;

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

function exchangeOffer(amount: number, currency: string, stock = 10) {
  return {
    result: {
      offer1: {
        listing: {
          offers: [
            {
              exchange: { amount, currency },
              item: { amount: 1, stock },
            },
          ],
          indexed: "2026-07-21T00:00:00Z",
        },
      },
    },
  };
}

async function waitDone(refreshId: number): Promise<void> {
  const deadline = Date.now() + 5000;
  for (;;) {
    const state = arbState(refreshId);
    if (state?.done) return;
    if (Date.now() > deadline) throw new Error("refinement never finished");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

beforeEach(() => {
  _clearArbStates();
  _setScheduler(new Scheduler(() => Date.now(), { ggg: 1, scout: 1 }));
  vi.stubGlobal("fetch", vi.fn(async () => json({ result: {} })));
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("matrix-only mode without an item shows the exchange matrix", async () => {
  const answer = await arbQuote({ clipboard: "", league: "Standard" });

  expect(answer.mode).toBe("matrix-only");
  const labels = answer.matrix.map((r) => r.label);
  expect(labels).toContain("Exalted Orb");
  expect(labels).toContain("Chaos Orb");
  expect(labels).toContain("Divine Orb");
  expect(labels).toContain("Divine ↔ Chaos");
  const divine = answer.matrix.find((r) => r.key === "pair:divine");
  expect(divine?.priceText).toBe("237 ex");
  const cross = answer.matrix.find((r) => r.key === "pair:divine:chaos");
  expect(cross?.priceText).toContain("5,925");
});

it("commodity mode prices the hovered item in every major currency", async () => {
  const answer = await arbQuote({ clipboard: DIVINE_TEXT, league: "Standard" });

  expect(answer.mode).toBe("commodity");
  expect(answer.itemName).toBe("Divine Orb");
  const rows = Object.fromEntries(answer.itemRows.map((r) => [r.key, r]));
  expect(rows["item:divine:exalted"].priceText).toBe("237 exalted");
  expect(rows["item:divine:chaos"].priceText).toContain("5,925");
  expect(rows["item:divine:divine"].priceText).toBe("1 divine");
});

it("stage 2 marks big-three rows live from official exchange answers", async () => {
  const fetchMock = vi.fn(async (input: unknown) => {
    const url = String(input);
    if (url.includes("want")) throw new Error("unexpected GET");
    return json(exchangeOffer(250, "exalted", 7));
  });
  vi.stubGlobal("fetch", fetchMock);

  const answer = await arbQuote({ clipboard: "", league: "Standard" });
  await waitDone(answer.refreshId);

  const state = arbState(answer.refreshId);
  const divine = state?.matrix.find((r) => r.key === "pair:divine");
  expect(divine?.source).toBe("live");
  expect(divine?.priceText).toBe("250 ex");
});

it("listings mode normalizes per-currency medians and flags the spread", async () => {
  const uniqueText = readFileSync(
    new URL("./fixtures/unique.txt", import.meta.url),
    "utf8",
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async (input: unknown, init?: RequestInit) => {
      const url = String(input);
      const body = String(init?.body ?? "");
      if (url.includes("/api/trade2/search/")) {
        return json({ id: "q1", result: ["i1", "i2", "i3"] });
      }
      if (url.includes("/api/trade2/fetch/")) {
        return json({
          result: [
            { listing: { price: { amount: 10, currency: "divine" } } },
            { listing: { price: { amount: 9.5, currency: "divine" } } },
            { listing: { price: { amount: 2500, currency: "exalted" } } },
          ],
        });
      }
      if (url.includes("/api/trade2/exchange/")) {
        return json(exchangeOffer(237, "exalted"));
      }
      throw new Error(`unexpected fetch ${url} ${body}`);
    }),
  );

  const answer = await arbQuote({ clipboard: uniqueText, league: "Standard" });
  expect(answer.mode).toBe("listings-pending");
  await waitDone(answer.refreshId);

  const state = arbState(answer.refreshId);
  expect(state?.listings).toBeDefined();
  const [cheapest, ...rest] = state!.listings!;
  // divine median 9.75 * 237 = 2310.75 ex — cheapest; exalted 2500 lags by >5%.
  expect(cheapest.currency).toBe("divine");
  expect(cheapest.exaltedMedian).toBeCloseTo(2310.75, 1);
  expect(rest[0].currency).toBe("exalted");
  expect(rest[0].flagged).toBe(true);
  expect(rest[0].deltaVsBest).toBeGreaterThan(0.05);
});

it("a failed listings search leaves a note instead of crashing", async () => {
  const uniqueText = readFileSync(
    new URL("./fixtures/unique.txt", import.meta.url),
    "utf8",
  );
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => json({ error: { message: "nope" } }, 500)),
  );

  const answer = await arbQuote({ clipboard: uniqueText, league: "Standard" });
  await waitDone(answer.refreshId);

  const state = arbState(answer.refreshId);
  expect(state?.listings).toBeUndefined();
  expect(state?.listingsNote).toContain("unavailable");
});

it("refresh ids are distinct and old states are evicted", async () => {
  const first = await arbQuote({ clipboard: "", league: "Standard" });
  const second = await arbQuote({ clipboard: "", league: "Standard" });
  const third = await arbQuote({ clipboard: "", league: "Standard" });
  const fourth = await arbQuote({ clipboard: "", league: "Standard" });
  const fifth = await arbQuote({ clipboard: "", league: "Standard" });

  expect(second.refreshId).toBe(first.refreshId + 1);
  expect(arbState(first.refreshId)).toBeNull();
  expect(arbState(fifth.refreshId)).not.toBeNull();
  void third;
  void fourth;
});
