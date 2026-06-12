import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

// Same offline network edges as price.test.ts (trade2 search/fetch mocked),
// but with the icons module forced to REJECT — modelling an icon-CDN outage.
// Listings are the product, icons are decoration: priceCheck must still
// return listings with null icon paths and a null item card.
vi.mock("../src/icons", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/icons")>();
  return {
    ...mod,
    resolveIcon: vi.fn(async () => {
      throw new Error("icon outage");
    }),
    resolveCurrencyIcon: vi.fn(async () => {
      throw new Error("icon outage");
    }),
  };
});

vi.mock("../src/poe2scout", () => ({
  scoutPrice: vi.fn(async () => null),
  divinePrice: vi.fn(async () => null),
}));

vi.mock("@/web/price-check/trade/pathofexile-trade", async (importOriginal) => {
  const mod =
    await importOriginal<
      typeof import("@/web/price-check/trade/pathofexile-trade")
    >();
  return {
    ...mod,
    requestTradeResultList: vi.fn(async () => ({
      id: "query-id",
      total: 2,
      result: ["r1", "r2"],
    })),
    requestResults: vi.fn(async () => [
      {
        id: "r1", priceAmount: 5, priceCurrency: "exalted", accountName: "s1", listedAt: "1m",
        displayItem: {
          title: ["Storm Caress", "Vile Robe"],
          icon: { url: "https://web.poecdn.com/image/storm-caress.png", w: 2, h: 3 },
        },
      },
      { id: "r2", priceAmount: 6, priceCurrency: "exalted", accountName: "s2", listedAt: "2m" },
    ]),
  };
});

beforeAll(initBrainData);

beforeEach(async () => {
  const scout = await import("../src/poe2scout");
  (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("priceCheck survives icon-resolution failure: listings intact, icons/card null", async () => {
  const { priceCheck } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-armour.txt", import.meta.url),
    "utf8",
  );
  const r = (await priceCheck(text, "L")) as any;
  expect(r.kind).toBe("price");
  expect(r.listings).toHaveLength(2);
  expect(r.listings[0].currencyIconPath).toBeNull();
  expect(r.listings[0].displayItem.iconPath).toBeNull();
  // cardFromItem's resolveIcon rejected -> the card degrades to null rather
  // than failing the whole response. poed renders no hover card for null.
  expect(r.item).toBeNull();
  // The listing payload itself is untouched by the degrade path.
  expect(r.listings.map((l: any) => l.priceAmount)).toEqual([5, 6]);
});
