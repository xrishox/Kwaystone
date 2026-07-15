export interface MarketQuote {
  price: number;
  amount: number;
  currency: string;
  currencyText: string;
  liquidity: number;
  buyerStock: number;
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
 * Select one liquid native-currency quote per item from a scout snapshot.
 * Executable pairs outrank dead pairs, then Exalted-equivalent volume wins.
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
    const relativePrice = number(itemData.RelativePrice);
    const price = item.ApiId === pair.BaseCurrencyApiId ? 1 : relativePrice;
    if (
      !apiId ||
      !currency ||
      itemVolume === null ||
      itemVolume <= 0 ||
      quoteVolume === null ||
      quoteVolume <= 0 ||
      price === null ||
      price <= 0
    ) {
      return;
    }

    const liquidity = number(pair.Volume) ?? 0;
    const buyerStock = number(quoteData.HighestStock) ?? 0;
    const candidate: MarketQuote = {
      price,
      amount: quoteVolume / itemVolume,
      currency,
      currencyText: text(quote.Text) ?? currency,
      liquidity,
      buyerStock,
      itemVolume,
    };
    const current = out.get(apiId);
    const candidateAvailable = candidate.buyerStock > 0;
    const currentAvailable = (current?.buyerStock ?? 0) > 0;
    if (
      !current ||
      (candidateAvailable && !currentAvailable) ||
      (
        candidateAvailable === currentAvailable &&
        (
          candidate.liquidity > current.liquidity ||
          (
            candidate.liquidity === current.liquidity &&
            candidate.itemVolume > current.itemVolume
          )
        )
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
