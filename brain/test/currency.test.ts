import { beforeAll, beforeEach, describe, expect, it, vi } from "vitest";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

// bulkSearch is the network edge — mock it; rate math + ordering is ours.
// One call per payment `have`. Orientation: have=lookup item, want=payment,
// so an offer "exchangeAmount lookup : itemAmount payment" gives the rate
// payment-per-item = itemAmount / exchangeAmount.
// offers[0] is a bait listing (implausibly high payout ratio 200); real rate ~84;
// one multi-unit offer (10:840) exercises ratio math. Each entry's
// `wantIconPath` is the payment-currency icon (the want side of the call).
const PAYMENT_ICON: Record<string, string> = {
  exalted: "/cached/exalted.png",
  divine: "/cached/divine.png",
};
const book = (have: string, want: string) => ({
  have, want, league: "L", queryId: "q", total: 5,
  offers: [
    { id: "bait", relativeDate: "1m", exchangeAmount: 1, itemAmount: 200,
      stock: 1, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
    { id: "b", relativeDate: "1m", exchangeAmount: 1, itemAmount: 84,
      stock: 3, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
    { id: "c", relativeDate: "1m", exchangeAmount: 10, itemAmount: 840,
      stock: 40, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
    { id: "d", relativeDate: "1m", exchangeAmount: 1, itemAmount: 84,
      stock: 12, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
    { id: "e", relativeDate: "1m", exchangeAmount: 1, itemAmount: 83,
      stock: 7, isMine: false, accountName: "x", ign: "y",
      accountStatus: "online" },
  ],
  // have=lookup (divine), want=payment; the payment icon is the want side.
  haveIconPath: "/cached/divine.png", wantIconPath: PAYMENT_ICON[want] ?? null,
});
vi.mock("../src/bulk", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/bulk")>();
  return {
    ...mod,
    bulkSearch: vi.fn(async (have: string, want: string) => book(have, want)),
  };
});

// poe2scout is the preferred price source; the exchange (bulkSearch) is only
// the fallback. Default both scout fns to "no data" so the existing exchange
// tests below exercise the fallback path unchanged; the poe2scout tests
// override scoutPrice/divinePrice per-case.
vi.mock("../src/poe2scout", () => ({
  scoutPrice: vi.fn(async () => null),
  divinePrice: vi.fn(async () => null),
}));

const DIVINE_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Divine Orb
--------
Stack Size: 7/20
--------
Modifies a magic or rare item, rerolling the values of its modifiers
`;

describe("currencyCheck", () => {
  // Default the scout source to "miss" before every case so the exchange
  // fallback tests are unaffected; the poe2scout cases set it explicitly.
  beforeEach(async () => {
    const scout = await import("../src/poe2scout");
    (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
    (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  });

  it("poe2scout hit: prices both rows from the aggregate, exchange untouched", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const scout = await import("../src/poe2scout");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    mockBulk.mockClear();

    // A greater-exalt-style material: scout gives 5.8 exalted/item, q30776;
    // divinePrice 100.2 exalted/divine. The item's own tag is "chaos" (not a
    // payment currency) so BOTH exalted and divine rows are produced.
    (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue({
      price: 5.8,
      quantity: 30776,
    });
    (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(100.2);

    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted", "divine"]);
    const exalted = r.rates.find((x: any) => x.have === "exalted");
    const divine = r.rates.find((x: any) => x.have === "divine");
    // exalted row: rawUnit IS the scout price (exalted-per-item).
    expect(exalted.rawUnit).toBe(5.8);
    expect(exalted.total).toBe(30776);
    expect(exalted.stackValue).toBe(Math.round(5.8 * 7 * 10) / 10);
    // divine row: rawUnit = scout.price / divinePrice ≈ 0.0579.
    expect(divine.rawUnit).toBeCloseTo(5.8 / 100.2, 5);
    expect(divine.total).toBe(30776);
    // poe2scout fully priced the rows; the exchange must not be consulted.
    expect(mockBulk).not.toHaveBeenCalled();
  });

  it("poe2scout hit: divine lookup has no divine row (self-excluded)", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const scout = await import("../src/poe2scout");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    mockBulk.mockClear();

    (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue({
      price: 100.2,
      quantity: 1234,
    });
    (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(100.2);

    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    // divine self-excluded; only the exalted row, valued at scout.price.
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);
    expect(r.rates[0].rawUnit).toBe(100.2);
    expect(r.rates[0].total).toBe(1234);
    expect(mockBulk).not.toHaveBeenCalled();
  });

  it("poe2scout miss: falls back to the exchange pairRate path", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const scout = await import("../src/poe2scout");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    mockBulk.mockClear();
    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
    // scoutPrice null (beforeEach default) → exchange fallback must run.

    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    // Exchange path produced the row (sell-side modal rate 84).
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);
    expect(r.rates[0].rawUnit).toBe(84);
    // The exchange WAS consulted (proves the fallback ran).
    expect(mockBulk).toHaveBeenCalled();
    expect(scout.scoutPrice).toHaveBeenCalled();
  });

  it("derive path: empty divine book → cross-rate via exalted anchor", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();

    // A chaos-tagged material (chaos isn't a payment currency, so divine IS in
    // its payment set and the item is NOT exalted): the item->divine book is
    // EMPTY; item->exalted is the anchor (6 ex/item); divine->exalted = 0.1
    // (exalted-per-divine). Cross-rate divine-per-item = 6 / 0.1 = 60,
    // pure-derived.
    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const empty = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 0, offers: [],
      haveIconPath: null, wantIconPath: PAYMENT_ICON[want] ?? null,
    });
    const oneRatio = (have: string, want: string, ratio: number) => ({
      have, want, league: "L", queryId: "q", total: 5,
      offers: [
        { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: ratio,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: PAYMENT_ICON[want] ?? null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) => {
      // Chaos lookup: chaos->exalted is the anchor (6 ex/item).
      if (have === "chaos" && want === "exalted") return oneRatio(have, want, 6);
      // Payment cross-rate leg priced in exalted.
      if (have === "divine" && want === "exalted") return oneRatio(have, want, 0.1);
      // No direct book for the divine payment pair.
      return empty(have, want);
    });

    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    // exalted row = anchor (direct exalted pair); divine row derived.
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted", "divine"]);
    const exalted = r.rates.find((x: any) => x.have === "exalted");
    const divine = r.rates.find((x: any) => x.have === "divine");
    expect(exalted.rawUnit).toBe(6);      // anchor, direct
    expect(exalted.total).toBe(5);        // direct offers count
    // divine-per-item = 6 / 0.1 = 60, pure-derived → total 0.
    expect(divine.rawUnit).toBeCloseTo(60, 5);
    expect(divine.total).toBe(0);

    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
  });

  it("derive path: divine ≈ 0.072 per item (greater-exalt-style)", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();

    // Lookup a chaos-tagged material (so divine IS in its payment set and the
    // item is NOT exalted): chaos->divine EMPTY; chaos->exalted anchor = 6;
    // divine->exalted = 83. divine-per-item = 6/83 ≈ 0.0723.
    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const empty = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 0, offers: [],
      haveIconPath: null, wantIconPath: null,
    });
    const oneRatio = (have: string, want: string, ratio: number) => ({
      have, want, league: "L", queryId: "q", total: 5,
      offers: [
        { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: ratio,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) => {
      if (have === "chaos" && want === "exalted") return oneRatio(have, want, 6);
      if (have === "divine" && want === "exalted") return oneRatio(have, want, 83);
      return empty(have, want);
    });

    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    const divine = r.rates.find((x: any) => x.have === "divine");
    expect(divine.rawUnit).toBeCloseTo(6 / 83, 5);  // ≈ 0.0723
    expect(divine.total).toBe(0);

    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
  });

  it("exaltedPer is cached: divine->exalted queried only once across two checks", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();

    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const empty = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 0, offers: [],
      haveIconPath: null, wantIconPath: null,
    });
    const oneRatio = (have: string, want: string, ratio: number) => ({
      have, want, league: "L", queryId: "q", total: 5,
      offers: [
        { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: ratio,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: null,
    });
    const calls: Record<string, number> = {};
    mockBulk.mockImplementation(async (have: string, want: string) => {
      calls[`${have}->${want}`] = (calls[`${have}->${want}`] ?? 0) + 1;
      if (have === "chaos" && want === "exalted") return oneRatio(have, want, 6);
      if (have === "divine" && want === "exalted") return oneRatio(have, want, 83);
      return empty(have, want);
    });

    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    await currencyCheck(item, "L");
    await currencyCheck(item, "L");

    // divine->exalted is the shared core rate (exaltedPer cache) — fetched
    // once and reused on the second lookup.
    expect(calls["divine->exalted"]).toBe(1);

    _clearExaltedPerCache();
    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
  });

  it("returns rates vs payment currencies, self excluded, ordered", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { isCurrency, currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    mockBulk.mockClear();
    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();

    expect(isCurrency(item)).toBe(true);

    const r = await currencyCheck(item, "L");
    expect(r.kind).toBe("currency");
    expect(r.name).toBe("Divine Orb");
    expect(r.stack).toBe(7);
    // divine excluded (self); chaos isn't a payment currency → only exalted.
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);

    // One bulkSearch per payment currency: have=lookup (divine), want=payment.
    // Self (divine) is excluded from the payment set; only exalted remains.
    expect(mockBulk).toHaveBeenCalledTimes(1);
    expect(mockBulk).toHaveBeenNthCalledWith(1, "divine", "exalted", "L");

    // rawUnit = itemAmount / exchangeAmount (have=lookup, want=payment).
    // Ratios from top offers: [200/1, 84/1, 840/10, 84/1, 83/1] = [200,84,84,84,83].
    // Buckets by sig3: {200:[200], 84:[84,84,84], 83:[83]}; modal bucket = 84
    // (3 offers) → median 84. The lone bait (200) is a singleton and loses.
    expect(r.rates[0].rawUnit).toBe(84);
    expect(r.rates[0]).not.toHaveProperty("display");
    expect(r.rates[0].stackValue).toBe(588);   // 7 * 84, 1dp

    // Payment currency is the `want` side; haveIconPath comes from wantIconPath.
    expect(r.rates[0].haveIconPath).toBe("/cached/exalted.png");
  });

  it("sell-side (orb): rate from item->payment book, buy side never queried", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();
    mockBulk.mockClear();
    // Orb has a deep SELL side: bulkSearch(divine, exalted) returns offers, so
    // the buy side (bulkSearch(exalted, divine)) must never be queried.
    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));

    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);
    expect(r.rates[0].rawUnit).toBe(84); // sell-side market rate

    // Only the sell-side direction is ever queried.
    for (const call of mockBulk.mock.calls) {
      expect(call).toEqual(["divine", "exalted", "L"]);
    }
    // No buy-side (exalted -> divine) lookup happened.
    const buySide = mockBulk.mock.calls.some(
      (c) => c[0] === "exalted" && c[1] === "divine");
    expect(buySide).toBe(false);

    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
  });

  it("buy-only (rune): empty sell side, inverted buy side → exalted-per-rune ≈ 1", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();
    mockBulk.mockClear();

    // A rune trades buy-only: the SELL side (rune -> exalted) is EMPTY, but the
    // BUY side (exalted -> rune) has 186 offers, all 1:1 (give 1 exalted, get 1
    // rune). item-per-exalted = 1 → inverted → exalted-per-rune = 1.
    const RUNE_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Adept Rune
--------
Stack Size: 5/10
--------
Martial Weapon: 12% increased Attack Speed
`;
    const empty = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 0, offers: [],
      haveIconPath: null, wantIconPath: PAYMENT_ICON[want] ?? null,
    });
    const buyBook = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 186,
      offers: Array.from({ length: 186 }, (_, i) => ({
        id: `o${i}`, relativeDate: "1m", exchangeAmount: 1, itemAmount: 1,
        stock: 5, isMine: false, accountName: "x", ign: "y",
        accountStatus: "online",
      })),
      // have=exalted (payment), want=rune; on the buy side the payment is the
      // `have` side, so its icon is haveIconPath.
      haveIconPath: PAYMENT_ICON[have] ?? null, wantIconPath: null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) => {
      // Sell side rune -> exalted: no liquidity.
      if (have === "adept-rune" && want === "exalted") return empty(have, want);
      // Buy side exalted -> rune: deep, all 1:1.
      if (have === "exalted" && want === "adept-rune") return buyBook(have, want);
      // Divine pairs (both directions) empty → divine row derives via exalted.
      if (want === "exalted") return oneRatioBook(have, want, 0.005);
      return empty(have, want);
    });
    // divine -> exalted exists so the derived divine row can be priced.
    function oneRatioBook(have: string, want: string, ratio: number) {
      return {
        have, want, league: "L", queryId: "q", total: 5,
        offers: [
          { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: ratio,
            stock: 5, isMine: false, accountName: "x", ign: "y",
            accountStatus: "online" },
        ],
        haveIconPath: null, wantIconPath: PAYMENT_ICON[want] ?? null,
      };
    }

    const item = parseClipboard(RUNE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    const exalted = r.rates.find((x: any) => x.have === "exalted");
    expect(exalted).toBeDefined();
    // Inverted buy side: exalted-per-rune = 1 / (item-per-exalted = 1) = 1.
    expect(exalted.rawUnit).toBeCloseTo(1, 5);
    // The buy-side icon for the exalted row comes from haveIconPath.
    expect(exalted.haveIconPath).toBe("/cached/exalted.png");
    // total reflects the buy side's offer count.
    expect(exalted.total).toBe(186);

    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
  });

  it("cheap want (sub-1 rate): rawUnit ~ 0.4, full precision", async () => {
    // Book where the lookup currency is cheap: payment-per-item < 1.
    // rawUnit = itemAmount / exchangeAmount < 1.
    // e.g. you give 2.5 lookup to receive 1 payment → exchangeAmount=2.5,
    // itemAmount=1 → ratio = 1/2.5 = 0.4.
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    const cheapBook = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 5,
      offers: [
        { id: "a", relativeDate: "1m", exchangeAmount: 2.4, itemAmount: 1,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
        { id: "b", relativeDate: "1m", exchangeAmount: 2.5, itemAmount: 1,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
        { id: "c", relativeDate: "1m", exchangeAmount: 2.6, itemAmount: 1,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) =>
      cheapBook(have, want),
    );

    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    // ratios = [1/2.4, 1/2.5, 1/2.6] sorted ascending; median = 1/2.5 = 0.4
    expect(r.rates[0].rawUnit).toBeCloseTo(0.4, 5);
    expect(r.rates[0]).not.toHaveProperty("display");
    // stackValue keeps full-precision-derived meaning: stack * rawUnit, 1dp
    // 7 * 0.4 = 2.8
    expect(r.rates[0].stackValue).toBeCloseTo(2.8, 5);
    mockBulk.mockImplementation(async (have: string, want: string) =>
      book(have, want),
    );
  });

  it("two-offer book, two singleton buckets: tie breaks to the lower key", async () => {
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    const smallBook = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 2,
      offers: [
        { id: "x", relativeDate: "1m", exchangeAmount: 1, itemAmount: 80,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
        { id: "y", relativeDate: "1m", exchangeAmount: 1, itemAmount: 90,
          stock: 5, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) =>
      smallBook(have, want),
    );
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    // ratios = [80, 90]; buckets {80:[80], 90:[90]} both count 1. Overall
    // median = 85, equidistant from both keys → tie. The first-seen bucket
    // (80, the more conservative sell value) wins the strict-less tie-break.
    expect(r.rates[0].rawUnit).toBe(80);
    mockBulk.mockImplementation(async (have: string, want: string) =>
      book(have, want),
    );
  });

  it("rate-limit: a 'Retry after N' throw backs off and retries the pair once", async () => {
    vi.useFakeTimers();
    try {
      const { bulkSearch } = await import("../src/bulk");
      const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
      // exalted: throws Retry-after once, then succeeds on retry.
      const calls: Record<string, number> = {};
      mockBulk.mockImplementation(async (have: string, want: string) => {
        calls[want] = (calls[want] ?? 0) + 1;
        if (want === "exalted" && calls[want] === 1) {
          throw new Error("Retry after 1 seconds");
        }
        return book(have, want);
      });

      const { currencyCheck } = await import("../src/currency");
      const { parseClipboard } = await import("@/parser");
      const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();

      const promise = currencyCheck(item, "L");
      // Let the first (throwing) call settle, then drain the backoff timer.
      await vi.runAllTimersAsync();
      const r = await promise;

      // exalted recovered after the backoff retry (divine self-excluded, chaos
      // not a payment currency → exalted is the only row).
      expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);
      expect(r.rates[0].rawUnit).toBe(84);
      // exalted was called twice (initial throw + retry).
      expect(calls["exalted"]).toBe(2);
    } finally {
      vi.useRealTimers();
      const { bulkSearch } = await import("../src/bulk");
      const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
      mockBulk.mockImplementation(async (have: string, want: string) =>
        book(have, want),
      );
    }
  });

  it("market rate rejects bait/lowball: modal cluster wins over a tied singleton crowd", async () => {
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    // A baity book: two lowball 1s, one 200 bait, and the real cluster at ~83.
    // ratios [1,1,200,84,83,82,84] → buckets {1:[1,1], 200:[200], 84:[84,84],
    // 83:[83], 82:[82]}. The 1-bucket (count 2) ties the 84-bucket (count 2);
    // overall median is 83, so the tie-break (key nearest median) picks the
    // ~83 real cluster, NOT the lowball 1s. Median of [84,84] = 84.
    const ratios = [1, 1, 200, 84, 83, 82, 84];
    const baitBook = (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: ratios.length,
      offers: ratios.map((r, i) => ({
        id: `o${i}`, relativeDate: "1m", exchangeAmount: 1, itemAmount: r,
        stock: 5, isMine: false, accountName: "x", ign: "y",
        accountStatus: "online",
      })),
      haveIconPath: null, wantIconPath: null,
    });
    mockBulk.mockImplementation(async (have: string, want: string) =>
      baitBook(have, want),
    );
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    // Lands in the real cluster (~83), not the 1s and not a median skewed by
    // the 200 bait.
    expect(r.rates[0].rawUnit).toBe(84);
    expect(r.rates[0].rawUnit).toBeGreaterThan(50);
    mockBulk.mockImplementation(async (have: string, want: string) =>
      book(have, want),
    );
  });

  it("PAYMENT_TAGS is exalted + divine only (no chaos)", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck, _clearExaltedPerCache } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    _clearExaltedPerCache();
    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));

    // Lookup a chaos-tagged item: chaos is NOT a payment currency, so the only
    // payment rows are exalted + divine. No chaos row, no chaos pair queried.
    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");
    expect(r.rates.map((x: any) => x.have)).toEqual(["exalted", "divine"]);
    expect(r.rates.map((x: any) => x.have)).not.toContain("chaos");
    // No bulkSearch call ever names chaos as a payment (want) side.
    for (const call of mockBulk.mock.calls) {
      expect(call[1]).not.toBe("chaos");
    }
  });

  it("poe2scout hit: history is carried through to the currency result", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const scout = await import("../src/poe2scout");

    const HISTORY = [0.8, 0.9, 1.1, 1.2]; // oldest→newest
    (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue({
      price: 5.8,
      quantity: 30776,
      history: HISTORY,
    });
    (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(100.2);

    const CHAOS_CLIP = `Item Class: Stackable Currency
Rarity: Currency
Chaos Orb
--------
Stack Size: 7/20
--------
Reforges a rare item with new random modifier values
`;
    const item = parseClipboard(CHAOS_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    expect(r.history).toEqual(HISTORY);
  });

  it("poe2scout miss (exchange fallback): history is empty array", async () => {
    process.env.POE2_ICON_CACHE = "/tmp/poed-icons-cur-test";
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { currencyCheck } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { bulkSearch } = await import("../src/bulk");
    const mockBulk = bulkSearch as ReturnType<typeof vi.fn>;
    mockBulk.mockImplementation(async (have: string, want: string) => book(have, want));
    // scoutPrice null (beforeEach default) → exchange fallback.

    const item = parseClipboard(DIVINE_CLIP)._unsafeUnwrap();
    const r = await currencyCheck(item, "L");

    expect(r.history).toEqual([]);
  });

  it("isCurrency false for gear", async () => {
    const { isCurrency } = await import("../src/currency");
    const { parseClipboard } = await import("@/parser");
    const { readFileSync } = await import("node:fs");
    const gear = readFileSync(
      new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url), "utf8");
    expect(isCurrency(parseClipboard(gear)._unsafeUnwrap())).toBe(false);
  });
});
