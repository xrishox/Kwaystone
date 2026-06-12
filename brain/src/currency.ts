import type { ParsedItem } from "@/parser/ParsedItem";
import { bulkSearch, type BulkSearchResult } from "./bulk";
import { resolveIcon, resolveCurrencyIcon, round1 } from "./icons";
import { scoutPrice, divinePrice } from "./poe2scout";

// The standard payment currencies, display order fixed by design. Exalted and
// divine are the only liquid main-exchange currencies; chaos<->exalted is too
// thin/noisy to price, so it's intentionally absent.
const PAYMENT_TAGS = ["exalted", "divine"] as const;

const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

// exalted-per-1-payment for divine is shared across ALL material
// lookups and moves slowly, but the exchange limiter is 1 req / 5 s. Cache
// these core rates module-level with a TTL so repeat material lookups don't
// re-query them. Warmed lazily inside exaltedPer. We cache the payment's icon
// alongside the rate so a pure-derived row still has an icon without an extra
// query.
const EXALTED_PER_TTL_MS = 5 * 60 * 1000;
const exaltedPerCache = new Map<
  string,
  { value: number | null; icon: string | null; expires: number }
>();

// Test seam: lets the suite reset the module-level cache between cases.
export function _clearExaltedPerCache() {
  exaltedPerCache.clear();
  paymentIconCache.clear();
}

// Payment-currency icon resolution, cached module-level. Shared by the
// poe2scout path (which has no exchange book to read an icon from) and the
// exchange fallback (exaltedPer's derived rows). resolveIcon already caches to
// disk, but holding the resolved path here avoids re-resolving per lookup.
const paymentIconCache = new Map<string, string | null>();
async function paymentIcon(tag: string): Promise<string | null> {
  const cached = paymentIconCache.get(tag);
  if (cached !== undefined) return cached;
  const icon = await resolveCurrencyIcon(tag);
  paymentIconCache.set(tag, icon);
  return icon;
}

export function isCurrency(item: ParsedItem): boolean {
  // Any stackable with a trade tag trades on the bulk exchange — that
  // includes omens etc., not just orbs. Routing those here is intentional.
  return Boolean(item.info.tradeTag && item.stackSize);
}

export interface CurrencyRate {
  have: string; // payment currency
  haveIconPath: string | null;
  rawUnit: number; // full precision: how much `have` buys 1 of this item
  stackValue: number; // stack * rawUnit (rounded to 1dp)
  total: number; // offers seen for this pair (0 for a pure-derived row)
}

/** Median of a numeric array. Copies internally; never mutates the input. */
function median(xs: number[]): number {
  const sorted = [...xs].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

/** Round to 3 significant figures (robust across magnitudes). */
function sig3(x: number): number {
  return Number(x.toPrecision(3));
}

/**
 * Market-rate payment-per-1-item from a lookup-side book: the rate the item is
 * CURRENTLY trading at, i.e. where competitive offers cluster.
 *
 * rate = itemAmount / exchangeAmount: you give exchangeAmount of the lookup
 * item (the `have` side), receive itemAmount of the payment (`want` side).
 * Kept full-precision so sub-1 rates survive — rounding is display-only.
 *
 * Median is clouded by old/odd/lowball offers. Instead bucket the first 12
 * ratios by their 3-significant-figure value; the densest bucket is the going
 * rate. Lone old/lowball/bait offers fall into singleton buckets and lose to
 * the cluster. Ties between equally-populated buckets break toward the bucket
 * whose key is nearest the overall median ratio (the central / real cluster),
 * so a bait-cluster tied with a real-cluster yields the real one. Returns the
 * median of the chosen bucket's raw ratios, or null when no usable ratios.
 */
function marketRate(offers: BulkSearchResult["offers"]): number | null {
  const ratios = offers
    .slice(0, 12)
    .map((o) => o.itemAmount / o.exchangeAmount)
    .filter((x) => Number.isFinite(x) && x > 0);
  if (!ratios.length) return null;
  if (ratios.length === 1) return ratios[0];

  // Bucket raw ratios by their 3-sig-fig key.
  const buckets = new Map<number, number[]>();
  for (const r of ratios) {
    const k = sig3(r);
    const bucket = buckets.get(k);
    if (bucket) bucket.push(r);
    else buckets.set(k, [r]);
  }

  // Overall median ratio is the tie-break anchor.
  const overallMedian = median(ratios);

  // Pick the most-populated bucket; tie → key nearest the overall median.
  let bestKey: number | null = null;
  let bestCount = -1;
  let bestDist = Infinity;
  for (const [key, bucket] of buckets) {
    const count = bucket.length;
    const dist = Math.abs(key - overallMedian);
    if (count > bestCount || (count === bestCount && dist < bestDist)) {
      bestKey = key;
      bestCount = count;
      bestDist = dist;
    }
  }

  return median(buckets.get(bestKey!)!);
}

/**
 * bulkSearch wrapper with the exchange-limiter backoff. The limiter (1 req/5s)
 * throws "Retry after N seconds" when calls stack up; parse N, wait N+0.25s,
 * and retry the pair exactly once. Returns the result, or null on a genuine
 * failure / second-attempt failure (caller drops/derives the row).
 */
async function bulkWithBackoff(
  have: string,
  want: string,
  league: string,
): Promise<BulkSearchResult | null> {
  try {
    return await bulkSearch(have, want, league);
  } catch (e) {
    const m = String((e as Error)?.message ?? e).match(/Retry after (\d+)/);
    if (m) {
      await sleep((Number(m[1]) + 0.25) * 1000);
      try {
        return await bulkSearch(have, want, league);
      } catch (e2) {
        console.error(`currency pair ${have}->${want} dropped after retry: ${e2}`);
        return null;
      }
    }
    // A genuine failure (network/empty) drops just this row; others render.
    console.error(`currency pair ${have}->${want} dropped: ${e}`);
    return null;
  }
}

/**
 * Rate of pair (a, b) priced from whichever exchange side has liquidity,
 * returning **b-per-1-a** (how much b one a is worth).
 *
 * Liquidity sits on different sides per item: orbs have a deep SELL side
 * (offers where you give the item, get payment), while runes etc. have ZERO
 * sell offers but a deep BUY side (offers where you give payment, get the
 * item). Query both, prefer the sell side (it's "what you get selling"),
 * fall back to the inverted buy side, so buy-only items still get a price.
 *
 *  - SELL side: bulkSearch(a, b) — an offer gives exchangeAmount of a for
 *    itemAmount of b, so b-per-a = marketRate = itemAmount/exchangeAmount.
 *    b is the `want` side → bIcon = wantIconPath.
 *  - BUY side: bulkSearch(b, a) — gives exchangeAmount of b for itemAmount of
 *    a, so a-per-b = marketRate; invert → b-per-a = 1 / that. b is the `have`
 *    side here → bIcon = haveIconPath.
 *
 * Returns null when neither side has a usable rate.
 */
async function pairRate(
  aTag: string,
  bTag: string,
  league: string,
): Promise<{ rate: number; total: number; bIcon: string | null } | null> {
  // SELL side first: a -> b. b is the `want` side.
  const sell = await bulkWithBackoff(aTag, bTag, league);
  const sellRate = sell ? marketRate(sell.offers) : null;
  if (sellRate != null) {
    return { rate: sellRate, total: sell!.total, bIcon: sell!.wantIconPath };
  }

  // No sell-side liquidity → invert the BUY side: b -> a. b is the `have` side.
  const buy = await bulkWithBackoff(bTag, aTag, league);
  const aPerB = buy ? marketRate(buy.offers) : null;
  if (aPerB != null && aPerB > 0) {
    return { rate: 1 / aPerB, total: buy!.total, bIcon: buy!.haveIconPath };
  }

  return null;
}

/**
 * exalted-per-1-payment for a payment tag, cached with a TTL alongside the
 * payment's icon. "exalted" is 1 by definition (its icon is unknown here, so
 * null — exalted never derives, it's the anchor). Otherwise it's the
 * liquid-side pairRate(P, "exalted") — payment P priced in exalted. Returns
 * null value when P has no exalted liquidity either way.
 */
async function exaltedPer(
  tag: string,
  league: string,
): Promise<{ value: number | null; icon: string | null }> {
  if (tag === "exalted") return { value: 1, icon: null };

  const cached = exaltedPerCache.get(tag);
  if (cached && cached.expires > Date.now()) return cached;

  const r = await pairRate(tag, "exalted", league);
  const entry = {
    value: r ? r.rate : null,
    // pairRate(P, "exalted") returns the EXALTED icon as bIcon; the payment's
    // own icon is the haveIconPath of the sell side. Re-derive the payment
    // icon from a known url so a derived row can label its payment column.
    icon: r ? await paymentIcon(tag) : null,
    expires: Date.now() + EXALTED_PER_TTL_MS,
  };
  exaltedPerCache.set(tag, entry);
  return entry;
}

/**
 * poe2scout path: volume-weighted aggregate. scout.price is exalted-per-item
 * DIRECTLY (not a market-side ratio), so the exalted row's rawUnit IS
 * scout.price and the divine row is scout.price / divinePrice. No
 * median/marketRate needed here — that's the exchange fallback's job. Returns
 * null when the item isn't tracked, so currencyCheck falls through to exchange.
 */
async function scoutRates(
  tag: string,
  stack: number,
  haves: readonly string[],
  league: string,
): Promise<{ rates: CurrencyRate[]; history: unknown[] } | null> {
  const scout = await scoutPrice(tag, league);
  if (!scout) return null;

  const dp = await divinePrice(league);

  // Collect the rawUnit rows first, then resolve every payment icon in
  // parallel (each paymentIcon is independent of the others).
  const rows: Array<{ have: string; rawUnit: number }> = [];
  for (const have of haves) {
    if (have === "exalted") {
      rows.push({ have, rawUnit: scout.price });
    } else if (have === "divine") {
      // Need the league's exalted-per-divine to convert; skip if unknown.
      if (dp == null || !(dp > 0)) continue;
      rows.push({ have, rawUnit: scout.price / dp });
    }
  }

  const icons = await Promise.all(rows.map((r) => paymentIcon(r.have)));
  const rates: CurrencyRate[] = rows.map((r, i) => ({
    have: r.have,
    haveIconPath: icons[i],
    rawUnit: r.rawUnit,
    stackValue: round1(r.rawUnit * stack),
    total: scout.quantity,
  }));

  return { rates, history: scout.history };
}

/**
 * Exchange fallback: price each payment pair from whichever exchange side has
 * liquidity.
 *
 * Anchor = exalted-per-1-item. Cheap crafting materials have NO direct
 * divine/chaos book — they trade only in exalted — so empty payment pairs
 * derive payment-per-item = anchorExalted / exaltedPer(P), matching poe.ninja's
 * cross-rates. If the item IS exalted, every exalted-per-exalted unit is 1 and
 * there's no own exalted pair to fetch.
 */
async function exchangeRates(
  tag: string,
  stack: number,
  haves: readonly string[],
  league: string,
): Promise<CurrencyRate[]> {
  const anchor =
    tag === "exalted"
      ? { rate: 1, total: 0, bIcon: null }
      : await pairRate(tag, "exalted", league);
  const anchorExalted = anchor ? anchor.rate : null;

  const rates: CurrencyRate[] = [];
  const pushRate = (
    have: string,
    rawUnit: number,
    icon: string | null,
    total: number,
  ) => {
    rates.push({
      have,
      haveIconPath: icon,
      rawUnit,
      stackValue: round1(rawUnit * stack),
      total,
    });
  };

  for (const have of haves) {
    // The item's exalted pair IS the anchor we already fetched; reuse its
    // result (rate, icon, total) rather than re-querying.
    if (have === "exalted" && tag !== "exalted") {
      if (anchorExalted == null) continue;
      pushRate(have, anchorExalted, anchor?.bIcon ?? null, anchor?.total ?? 0);
      continue;
    }

    // Direct: payment-per-item from whichever side of the (item, payment) pair
    // has liquidity. bIcon is the payment's icon for this row.
    const direct = await pairRate(tag, have, league);
    if (direct != null) {
      pushRate(have, direct.rate, direct.bIcon, direct.total);
      continue;
    }

    // Empty book both ways → derive the cross-rate. payment-per-item =
    // (exalted-per-item) / (exalted-per-payment). Skip if either leg is null.
    if (anchorExalted == null) continue;
    const exPerHave = await exaltedPer(have, league);
    if (exPerHave.value == null || !(exPerHave.value > 0)) continue;

    // No direct book for the icon; reuse the payment icon cached alongside its
    // exalted rate. total 0: pure-derived, no direct offers produced this rate.
    pushRate(have, anchorExalted / exPerHave.value, exPerHave.icon, 0);
  }

  return rates;
}

export async function currencyCheck(
  item: ParsedItem,
  league: string,
  onProgress?: (stage: string) => void,
) {
  const tag = item.info.tradeTag!;
  const stack = item.stackSize?.value ?? 1;

  // The payment currencies we price against, self excluded (no self-exchange
  // row). Display order is preserved by PAYMENT_TAGS.
  const haves = PAYMENT_TAGS.filter((t) => t !== tag);

  // Preferred source is poe2scout; fall through to the exchange book when the
  // item isn't tracked there. The exchange probe is slow (1 req/5s limiter),
  // so announce it before starting.
  const scout = await scoutRates(tag, stack, haves, league);
  if (!scout) onProgress?.("exchange");
  const rates = scout?.rates ?? (await exchangeRates(tag, stack, haves, league));
  const history = scout?.history ?? [];

  return {
    kind: "currency" as const,
    name: item.info.name,
    iconPath: await resolveIcon(item.info.icon),
    stack,
    rates,
    history,
  };
}
