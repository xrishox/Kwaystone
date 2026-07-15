import { describe, expect, it } from "vitest";

import { selectMarketQuotes } from "../src/scout/quotes";

describe("selectMarketQuotes", () => {
  it("prefers executable liquidity over a larger dead pair", () => {
    const quotes = selectMarketQuotes([
      {
        BaseCurrencyApiId: "item",
        Volume: 100,
        CurrencyOne: { ApiId: "item", Text: "Item" },
        CurrencyOneData: { VolumeTraded: 10, RelativePrice: 1 },
        CurrencyTwo: { ApiId: "dead", Text: "Dead" },
        CurrencyTwoData: { VolumeTraded: 20, HighestStock: 0 },
      },
      {
        BaseCurrencyApiId: "item",
        Volume: 20,
        CurrencyOne: { ApiId: "item", Text: "Item" },
        CurrencyOneData: { VolumeTraded: 5, RelativePrice: 1 },
        CurrencyTwo: { ApiId: "live", Text: "Live" },
        CurrencyTwoData: { VolumeTraded: 10, HighestStock: 50 },
      },
    ]);

    expect(quotes.get("item")).toMatchObject({
      currency: "live",
      amount: 2,
      buyerStock: 50,
    });
  });

  it("rejects malformed and zero-volume pairs", () => {
    expect(selectMarketQuotes(null).size).toBe(0);
    expect(selectMarketQuotes([{ CurrencyOne: {} }]).size).toBe(0);
  });
});
