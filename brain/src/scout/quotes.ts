import {
  MAJOR_CURRENCY_SET,
  scoutConfidence,
} from "./policy";

export interface MarketQuote {
  amount: number;
  currency: string;
  currencyText: string;
  liquidity: number;
  maxStock: number;
  itemVolume: number;
}

type JsonRecord = Record<string, unknown>;

function record(value: unknown): JsonRecord | null {
  return value !== null && typeof value === "object"
    ? value as JsonRecord
    : null;
}

function text(value: unknown): string | null {
  return typeof value === "string" && value ? value : null;
}

function number(value: unknown): number | null {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

/**
 * Select one well-supported major-currency quote per item from the completed
 * exchange snapshot. These are historical context, never a live order book.
 */
export function selectMarketQuotes(pairs: unknown): Map<string, MarketQuote> {
  const out = new Map<string, MarketQuote>();
  if (!Array.isArray(pairs)) return out;

  const consider = (
    pairValue: unknown,
    itemValue: unknown,
    itemDataValue: unknown,
    quoteValue: unknown,
    quoteDataValue: unknown,
  ) => {
    const pair = record(pairValue);
    const item = record(itemValue);
    const itemData = record(itemDataValue);
    const quote = record(quoteValue);
    const quoteData = record(quoteDataValue);
    if (!pair || !item || !itemData || !quote || !quoteData) return;

    const apiId = text(item.ApiId);
    const currency = text(quote.ApiId);
    const itemVolume = number(itemData.VolumeTraded);
    const quoteVolume = number(quoteData.VolumeTraded);
    if (
      !apiId ||
      !currency ||
      !MAJOR_CURRENCY_SET.has(currency) ||
      itemVolume === null ||
      itemVolume <= 0 ||
      quoteVolume === null ||
      quoteVolume <= 0
    ) {
      return;
    }

    const liquidity = number(pair.Volume) ?? 0;
    if (scoutConfidence(itemVolume, quoteVolume, liquidity) !== "reliable") return;
    const candidate: MarketQuote = {
      amount: quoteVolume / itemVolume,
      currency,
      currencyText: text(quote.Text) ?? currency,
      liquidity,
      maxStock: number(quoteData.HighestStock) ?? 0,
      itemVolume,
    };
    const current = out.get(apiId);
    if (
      !current ||
      candidate.liquidity > current.liquidity ||
      (
        candidate.liquidity === current.liquidity &&
        candidate.itemVolume > current.itemVolume
      )
    ) {
      out.set(apiId, candidate);
    }
  };

  for (const pairValue of pairs) {
    const pair = record(pairValue);
    if (!pair) continue;
    consider(
      pair,
      pair.CurrencyOne,
      pair.CurrencyOneData,
      pair.CurrencyTwo,
      pair.CurrencyTwoData,
    );
    consider(
      pair,
      pair.CurrencyTwo,
      pair.CurrencyTwoData,
      pair.CurrencyOne,
      pair.CurrencyOneData,
    );
  }
  return out;
}
