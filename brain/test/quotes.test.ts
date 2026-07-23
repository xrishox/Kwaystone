import { describe, expect, it } from "vitest";

import { selectMarketQuotes } from "../src/scout/quotes";

describe("selectMarketQuotes", () => {
  it("selects the most-liquid reliable major-currency history", () => {
    const quotes = selectMarketQuotes([
      {
        BaseCurrencyApiId: "exalted",
        Volume: 12_000,
        CurrencyOne: { ApiId: "item", Text: "Item" },
        CurrencyOneData: { VolumeTraded: 200, HighestStock: 10 },
        CurrencyTwo: { ApiId: "chaos", Text: "Chaos Orb" },
        CurrencyTwoData: { VolumeTraded: 800, HighestStock: 50 },
      },
      {
        BaseCurrencyApiId: "exalted",
        Volume: 20_000,
        CurrencyOne: { ApiId: "item", Text: "Item" },
        CurrencyOneData: { VolumeTraded: 400, HighestStock: 20 },
        CurrencyTwo: { ApiId: "exalted", Text: "Exalted Orb" },
        CurrencyTwoData: { VolumeTraded: 2_000, HighestStock: 75 },
      },
    ]);

    expect(quotes.get("item")).toEqual({
      currency: "exalted",
      currencyText: "Exalted Orb",
      amount: 5,
      liquidity: 20_000,
      maxStock: 75,
      itemVolume: 400,
    });
  });

  it("omits thin history and non-major quote currencies", () => {
    const quotes = selectMarketQuotes([
      {
        Volume: 50_000,
        CurrencyOne: { ApiId: "thin-item" },
        CurrencyOneData: { VolumeTraded: 99 },
        CurrencyTwo: { ApiId: "chaos" },
        CurrencyTwoData: { VolumeTraded: 500 },
      },
      {
        Volume: 50_000,
        CurrencyOne: { ApiId: "odd-item" },
        CurrencyOneData: { VolumeTraded: 500 },
        CurrencyTwo: { ApiId: "vaal" },
        CurrencyTwoData: { VolumeTraded: 500 },
      },
    ]);

    expect(quotes.size).toBe(0);
  });

  it("rejects malformed and zero-volume pairs", () => {
    expect(selectMarketQuotes(null).size).toBe(0);
    expect(selectMarketQuotes([{ CurrencyOne: {} }]).size).toBe(0);
  });
});
