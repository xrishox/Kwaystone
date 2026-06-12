import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

// Exchange (bulk) network edge. The factory gives an empty vi.fn();
// beforeEach installs the default LIQUID book so every test starts from a
// known implementation (mockResolvedValue in one test must not leak into the
// next — mockClear() would not restore an implementation).
vi.mock("../src/bulk", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/bulk")>();
  return { ...mod, bulkSearch: vi.fn() };
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
      total: 3,
      result: ["r1"],
    })),
    requestResults: vi.fn(async () => [
      { id: "r1", priceAmount: 5, priceCurrency: "exalted", accountName: "s1", listedAt: "1m" },
    ]),
  };
});

beforeAll(initBrainData);

const LIQUID = (have: string, want: string) => ({
  have, want, league: "L", queryId: "q", total: 5,
  offers: [
    { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: 85,
      stock: 10, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
  ],
  haveIconPath: null, wantIconPath: null,
});

const DRY = {
  have: "h", want: "w", league: "L", queryId: "q", total: 0,
  offers: [], haveIconPath: null, wantIconPath: null,
};

beforeEach(async () => {
  const scout = await import("../src/poe2scout");
  (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  const bulk = await import("../src/bulk");
  (bulk.bulkSearch as ReturnType<typeof vi.fn>).mockImplementation(
    async (have: string, want: string) => LIQUID(have, want),
  );
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no net in tests"); }));
});

const gemText = () =>
  readFileSync(new URL("./fixtures/gem.txt", import.meta.url), "utf8");

it("routes a tagged scout-missing item to the exchange-backed currency view", async () => {
  // scoutPrice is null (default) but the exchange book has offers: the
  // safeguard must consult the exchange BEFORE falling to gear listings.
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-route-1";
  const { priceCheck } = await import("../src/price");
  const r = (await priceCheck(gemText(), "L")) as any;
  expect(r.kind).toBe("currency");
  expect(r.rates.length).toBeGreaterThan(0);
});

it("falls through to listings when the exchange book is dry", async () => {
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-route-2";
  const bulk = await import("../src/bulk");
  (bulk.bulkSearch as ReturnType<typeof vi.fn>).mockResolvedValue(DRY);
  const { priceCheck } = await import("../src/price");
  const r = (await priceCheck(gemText(), "L")) as any;
  expect(r.kind).toBe("price");
  expect(r.listings).toHaveLength(1);
});

it("emits exchange then listings progress on the dry-book path", async () => {
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-route-3";
  const bulk = await import("../src/bulk");
  (bulk.bulkSearch as ReturnType<typeof vi.fn>).mockResolvedValue(DRY);
  const { priceCheck } = await import("../src/price");
  const stages: string[] = [];
  await priceCheck(gemText(), "L", [], (s: string) => stages.push(s));
  expect(stages).toEqual(["exchange", "listings"]);
});

it("requery overrides skip the currency route entirely", async () => {
  // A scout HIT would normally produce the currency view; overrides mean the
  // user is editing listing filters, so the lookup must stay on listings and
  // not re-pay an exchange probe per filter tweak.
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-route-4";
  const scout = await import("../src/poe2scout");
  (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue({
    price: 26000, quantity: 12, history: [],
  });
  const { priceCheck } = await import("../src/price");
  const r = (await priceCheck(gemText(), "L", [{ i: 99, enabled: false }] as any)) as any;
  expect(r.kind).toBe("price");
});
