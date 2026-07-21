import { Host } from "./stubs/IPC";
import { MarketQuote, selectMarketQuotes } from "./scout/quotes";

// poe2scout.com publishes volume-weighted aggregate prices for PoE2 currency
// and crafting materials, keyed by an ApiId that matches our item tradeTag.
// This module warms an in-memory snapshot (one bulk pull per refresh window)
// so per-item lookups never touch the network until the TTL expires.
//
// Endpoints (realm segment "poe2"):
//   GET /poe2/Leagues
//     -> [{ Value, IsCurrent, DivinePrice (exalted per 1 divine), ... }]
//   GET /poe2/Leagues/{enc}/Items/Categories
//     -> { CurrencyCategories: [{ ApiId }], UniqueCategories: [...] }
//   GET /poe2/Leagues/{enc}/SnapshotPairs
//     -> live pair volumes, relative prices, and quote-side stock
//   GET /poe2/Leagues/{enc}/Currencies/ByCategory?Category={cat}&PerPage=250
//     -> { Items: [{ ApiId, CurrentPrice, CurrentQuantity }], Pages, Total }
//
// We loop the currency categories (not the flat /Items pull) because /Items
// omits CurrentQuantity, and the currency rows need quantity for the listing
// "total". One refresh = 1 (Leagues) + 1 (Categories) + 1 (SnapshotPairs)
// + N (per category)
// requests; N is ~17, well within the 15-minute window. PerPage caps at 250
// (the API rejects 500 with 422), so we page each category to be safe.
//
// All calls go through Host.proxy: shared UA, and (importantly) NO POESESSID —
// Host.proxy attaches the cookie only for pathofexile.com hosts, never here.
//
// Robustness contract: poe2scout serves intermittent 503s per endpoint, so a
// refresh must never turn a partial outage into a total pricing loss.
//  - every request retries transient failures (network, 429/5xx, bad JSON);
//  - a category that still fails is skipped — the other categories' prices
//    survive, and the skipped entries are backfilled from the last good
//    snapshot for the same league;
//  - a degraded (incomplete/failed) snapshot is cached only for a short retry
//    TTL so the next lookup re-pulls soon, while still preventing per-lookup
//    hammering of a down API.

const BASE = "poe2scout.com/api";
const REALM = "poe2";
export const SCOUT_TTL_MS = 15 * 60 * 1000;
// Degraded snapshots (failed or partial pulls) retry on this cadence instead
// of serving catalog-only "no market price" rows for a full TTL window.
export const SCOUT_RETRY_TTL_MS = 60 * 1000;
const PER_PAGE = 250;
const REQUEST_TIMEOUT_MS = 10_000;
const REQUEST_ATTEMPTS = 3;
let retryBaseMs = 300;

export interface ScoutPrice {
  price: number; // Exalted-equivalent value used for sorting/thresholds.
  quantity: number; // CurrentQuantity
  history: number[]; // PriceLogs Price values, chronological (oldest→newest), nulls dropped
  category?: string; // poe2scout currency category ApiId, e.g. "currency", "ritual".
  name?: string; // Live display name; covers items newer than vendored EE2 data.
  iconUrl?: string; // Live icon; ItemMetadata.icon includes slot dimensions.
  quoteAmount?: number; // Native quote currency paid for one item.
  quoteCurrency?: string; // Quote currency ApiId.
  quoteCurrencyText?: string; // Human-readable quote currency.
  quoteLiquidity?: number; // Pair volume in Exalted-equivalent value.
  quoteBuyerStock?: number; // Current quote-side stock available to item sellers.
  quoteAvailable?: boolean; // True when the selected pair has quote-side stock.
  quoteItemVolume?: number; // Item-side units traded in the snapshot window.
}

interface Snapshot {
  league: string;
  map: Map<string, ScoutPrice>;
  divine: number | null; // DivinePrice for this league (exalted per 1 divine)
  expires: number; // epoch ms
  complete: boolean; // false when any part of the pull failed or is stale
}

// Module-level cache + single in-flight refresh promise (dedupes concurrent
// warms so two simultaneous lookups issue ONE bulk pull).
let cache: Snapshot | null = null;
// Keyed by league: an unkeyed inflight would serve league A's snapshot to a
// league B caller that arrives mid-refresh (cross-league price poisoning).
let inflight: { league: string; promise: Promise<Snapshot> } | null = null;

function enc(league: string): string {
  return encodeURIComponent(league);
}

function transientStatus(status: number): boolean {
  return status === 429 || status >= 500;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function getJson(path: string): Promise<any> {
  let lastError: unknown;
  for (let attempt = 0; attempt < REQUEST_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      // Exponential backoff with jitter so parallel pulls don't re-slam a
      // recovering API in lockstep.
      const base = retryBaseMs * 2 ** (attempt - 1);
      await sleep(base + Math.random() * base * 0.5);
    }
    try {
      const r = await Host.proxy(`${BASE}/${REALM}/${path}`, {
        signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
      });
      if (r.ok) return await r.json(); // parse failure retries too
      lastError = new Error(`poe2scout ${path} -> ${r.status}`);
      if (!transientStatus(r.status)) break;
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError;
}

/**
 * Select the best native-currency quote for every item in the current exchange
 * snapshot. Executable pairs (quote-side buyer stock > 0) outrank dead pairs;
 * within that class, the highest Exalted-equivalent traded volume wins.
 */
async function fetchMarketQuotes(
  league: string,
): Promise<Map<string, MarketQuote>> {
  const pairs = await getJson(`Leagues/${enc(league)}/SnapshotPairs`);
  return selectMarketQuotes(pairs);
}

/**
 * Fetch the leagues list and resolve the DivinePrice for `league`: exact
 * `Value` match preferred, else the `IsCurrent` entry. Returns null on miss or
 * any failure (never throws).
 */
export async function divinePrice(league: string): Promise<number | null> {
  // Prefer the warmed snapshot (one warm fetches leagues + categories), but
  // stay callable standalone: if nothing is warm yet, fetch the list directly.
  const hit = fresh(league);
  if (hit) return hit.divine;
  try {
    const leagues = await getJson("Leagues");
    return divineFromLeagues(leagues, league);
  } catch {
    return null;
  }
}

function divineFromLeagues(leagues: any, league: string): number | null {
  if (!Array.isArray(leagues)) return null;
  const exact = leagues.find((l) => l && l.Value === league);
  const pick = exact ?? leagues.find((l) => l && l.IsCurrent);
  const dp = pick?.DivinePrice;
  return typeof dp === "number" && Number.isFinite(dp) ? dp : null;
}

/**
 * Pull every currency item across all currency categories into a price map.
 *
 * Category pulls are independently best-effort: poe2scout 503s endpoints
 * individually, and one dead category must not discard every other price.
 * `complete` is false when anything was skipped so the caller can cache the
 * partial result on a short retry TTL and backfill from the last good pull.
 */
async function fetchPriceMap(
  league: string,
): Promise<{ map: Map<string, ScoutPrice>; complete: boolean }> {
  const map = new Map<string, ScoutPrice>();
  let complete = true;
  const cats = await getJson(`Leagues/${enc(league)}/Items/Categories`);
  const categories: string[] = Array.isArray(cats?.CurrencyCategories)
    ? cats.CurrencyCategories.map((c: any) => c?.ApiId).filter(
        (a: unknown): a is string => typeof a === "string",
      )
    : [];
  if (!categories.length) complete = false;
  const marketQuotes = await fetchMarketQuotes(league).catch(() => {
    complete = false;
    return new Map<string, MarketQuote>();
  });

  for (const cat of categories) {
    try {
      await fetchCurrencyCategory(league, cat, marketQuotes, map);
    } catch (e) {
      complete = false;
      console.error(
        `poe2scout: currency category "${cat}" pull failed:`,
        e instanceof Error ? e.message : e,
      );
    }
  }
  return { map, complete };
}

async function fetchCurrencyCategory(
  league: string,
  cat: string,
  marketQuotes: Map<string, MarketQuote>,
  map: Map<string, ScoutPrice>,
): Promise<void> {
  let page = 1;
  let pages = 1;
  do {
    const res = await getJson(
      `Leagues/${enc(league)}/Currencies/ByCategory?Category=${encodeURIComponent(cat)}&PerPage=${PER_PAGE}&Page=${page}`,
    );
    pages = typeof res?.Pages === "number" ? res.Pages : 1;
    const items = Array.isArray(res?.Items) ? res.Items : [];
    for (const it of items) {
      const apiId = it?.ApiId;
      const aggregatePrice = it?.CurrentPrice;
      if (typeof apiId !== "string" || typeof aggregatePrice !== "number") continue;
      const quote = marketQuotes.get(apiId);
      const price = quote?.price ?? aggregatePrice;
      const quantity =
        typeof it?.CurrentQuantity === "number" ? it.CurrentQuantity : 0;
      // PriceLogs arrive newest-first; reverse to chronological order and
      // drop any null/non-finite Price values.
      const logs: unknown[] = Array.isArray(it?.PriceLogs) ? it.PriceLogs : [];
      const history: number[] = logs
        .map((l: any) => l?.Price)
        .filter((p: unknown): p is number => typeof p === "number" && Number.isFinite(p))
        .reverse();
      const name =
        typeof it?.Text === "string" && it.Text
          ? it.Text
          : typeof it?.ItemMetadata?.name === "string"
            ? it.ItemMetadata.name
            : undefined;
      const iconUrl =
        typeof it?.ItemMetadata?.icon === "string" && it.ItemMetadata.icon
          ? it.ItemMetadata.icon
          : typeof it?.IconUrl === "string" && it.IconUrl
            ? it.IconUrl
            : undefined;
      // First write wins; an ApiId should be unique across categories anyway.
      if (!map.has(apiId)) {
        map.set(apiId, {
          price,
          quantity,
          history,
          category: cat,
          ...(name ? { name } : {}),
          ...(iconUrl ? { iconUrl } : {}),
          ...(quote
            ? {
                quoteAmount: quote.amount,
                quoteCurrency: quote.currency,
                quoteCurrencyText: quote.currencyText,
                quoteLiquidity: quote.liquidity,
                quoteBuyerStock: quote.buyerStock,
                quoteAvailable: quote.buyerStock > 0,
                quoteItemVolume: quote.itemVolume,
              }
            : {}),
        });
      }
    }
    page += 1;
  } while (page <= pages);
}

// Backfill entries a degraded pull is missing from the last good same-league
// snapshot: a stale price beats a catalog-only "no market price" row, and the
// short retry TTL replaces it with live data as soon as the API recovers.
function backfill<T>(map: Map<string, T>, last: Map<string, T>): void {
  for (const [key, value] of last) {
    if (!map.has(key)) map.set(key, value);
  }
}

// Perform one full warm (leagues-list + category loop). Defensive: each half is
// independently best-effort, but a hard failure (the categories list itself)
// rejects so the caller can fall back to the last-good snapshot.
async function warm(league: string): Promise<Snapshot> {
  // Run both pulls; tolerate a leagues failure (divine -> null, backfilled
  // from the last good snapshot below).
  const [divine, fetched] = await Promise.all([
    getJson("Leagues")
      .then((ls) => divineFromLeagues(ls, league))
      .catch(() => null),
    fetchPriceMap(league),
  ]);
  // An empty map from a "successful" pull is not trustworthy either: treat it
  // as degraded so it retries soon instead of pricing nothing for a full TTL.
  const complete = fetched.complete && fetched.map.size > 0;
  const last = cache && cache.league === league ? cache : null;
  if (!complete && last) backfill(fetched.map, last.map);
  return {
    league,
    map: fetched.map,
    divine: divine ?? last?.divine ?? null,
    complete,
    expires: Date.now() + (complete ? SCOUT_TTL_MS : SCOUT_RETRY_TTL_MS),
  };
}

function fresh(league: string): Snapshot | null {
  if (cache && cache.league === league && cache.expires > Date.now()) {
    return cache;
  }
  return null;
}

// Return a warmed snapshot, refreshing if stale/missing. Concurrent callers
// share a single in-flight warm. On failure, keep serving the last-good
// snapshot (or an empty one) marked degraded on the short retry TTL — cached
// so we don't hammer a failing API on every lookup, but recovering within a
// minute instead of a full TTL window. Never throws.
async function snapshot(
  league: string,
  options: { force?: boolean } = {},
): Promise<Snapshot> {
  const hit = options.force ? null : fresh(league);
  if (hit) return hit;

  if (!inflight || inflight.league !== league) {
    const promise = warm(league)
      .then((snap) => {
        cache = snap;
        return snap;
      })
      .catch((error) => {
        console.error(
          "poe2scout: currency snapshot refresh failed:",
          error instanceof Error ? error.message : error,
        );
        const last = cache && cache.league === league ? cache : null;
        const fallback: Snapshot = {
          league,
          map: last?.map ?? new Map(),
          divine: last?.divine ?? null,
          complete: false,
          expires: Date.now() + SCOUT_RETRY_TTL_MS,
        };
        cache = fallback;
        return fallback;
      })
      .finally(() => {
        if (inflight?.promise === promise) inflight = null;
      });
    inflight = { league, promise };
  }
  return inflight.promise;
}

/** A market map plus whether the backing pull was fully fresh. */
interface MarketData<T> {
  map: Map<string, T>;
  complete: boolean;
}

/**
 * Warmed bulk price map for `league` (ApiId -> {price, quantity}). Cached with
 * a 15-minute TTL; a second call within the window issues no network. Never
 * throws — returns the last-good (or empty) map on failure.
 */
export async function priceMap(league: string): Promise<Map<string, ScoutPrice>> {
  return (await snapshot(league)).map;
}

/**
 * priceMap() plus the snapshot's `complete` flag, so corpus builders can cache
 * results assembled from degraded market data on a shorter TTL.
 */
export async function priceMapDetailed(
  league: string,
): Promise<MarketData<ScoutPrice>> {
  const snap = await snapshot(league);
  return { map: snap.map, complete: snap.complete };
}

/**
 * Proactively refresh the aggregate currency snapshot even if the current
 * cache has not expired yet. Foreground lookups still use the last fresh cache
 * while this request is in flight.
 */
export async function refreshPriceMap(
  league: string,
): Promise<Map<string, ScoutPrice>> {
  return (await snapshot(league, { force: true })).map;
}

/**
 * Test seam. Aggregate price for a single tradeTag (== poe2scout ApiId) in
 * `league`, or null when the item is not tracked / the warm failed. Production
 * callers go through priceMap(); tests use this as the per-tag lookup idiom.
 */
export async function scoutPrice(
  tradeTag: string,
  league: string,
): Promise<ScoutPrice | null> {
  return (await priceMap(league)).get(tradeTag) ?? null;
}

// ---- Unique items (separate snapshot; same TTL/inflight discipline) -------

export interface UniquePrice {
  price: number; // newest PriceLogs entry, in exalted
  quantity: number;
  iconUrl: string;
  trend: number | null; // fractional change over the PriceLogs window, oldest -> newest
}

interface UniqueSnapshot {
  league: string;
  map: Map<string, UniquePrice>; // keyed by unique Name
  expires: number;
  complete: boolean; // false when any part of the pull failed or is stale
}

let uniqueCache: UniqueSnapshot | null = null;
let uniqueInflight: { league: string; promise: Promise<UniqueSnapshot> } | null = null;

/** Same per-category fault tolerance and `complete` contract as fetchPriceMap. */
async function fetchUniqueMap(
  league: string,
): Promise<{ map: Map<string, UniquePrice>; complete: boolean }> {
  const map = new Map<string, UniquePrice>();
  let complete = true;
  const cats = await getJson(`Leagues/${enc(league)}/Items/Categories`);
  const categories: string[] = Array.isArray(cats?.UniqueCategories)
    ? cats.UniqueCategories.map((c: any) => c?.ApiId).filter(
        (x: unknown): x is string => typeof x === "string",
      )
    : [];
  if (!categories.length) complete = false;
  for (const cat of categories) {
    try {
      await fetchUniqueCategory(league, cat, map);
    } catch (e) {
      complete = false;
      console.error(
        `poe2scout: unique category "${cat}" pull failed:`,
        e instanceof Error ? e.message : e,
      );
    }
  }
  return { map, complete };
}

async function fetchUniqueCategory(
  league: string,
  cat: string,
  map: Map<string, UniquePrice>,
): Promise<void> {
  let page = 1;
  let pages = 1;
  do {
    const res = await getJson(
      `Leagues/${enc(league)}/Uniques/ByCategory?Category=${encodeURIComponent(cat)}&PerPage=${PER_PAGE}&Page=${page}`,
    );
    pages = typeof res?.Pages === "number" ? res.Pages : 1;
    const items = Array.isArray(res?.Items) ? res.Items : [];
    for (const it of items) {
      const name = it?.Name;
      if (typeof name !== "string") continue;
      // PriceLogs arrive newest-first; the first numeric Price is current.
      const logs: any[] = Array.isArray(it?.PriceLogs) ? it.PriceLogs : [];
      const prices = logs
        .map((l) => l?.Price)
        .filter((p): p is number => typeof p === "number" && Number.isFinite(p));
      let entry: UniquePrice | null = null;
      if (prices.length) {
        const newest = logs.find((l) => typeof l?.Price === "number");
        const oldest = prices[prices.length - 1];
        entry = {
          price: newest.Price,
          quantity: typeof newest.Quantity === "number" ? newest.Quantity : 0,
          iconUrl: typeof it?.IconUrl === "string" ? it.IconUrl : "",
          trend:
            prices.length >= 2 && oldest > 0
              ? (newest.Price - oldest) / oldest
              : null,
        };
      } else if (
        typeof it?.CurrentPrice === "number" &&
        Number.isFinite(it.CurrentPrice) &&
        it.CurrentPrice > 0
      ) {
        // Young leagues: scout lists many uniques before any aggregate
        // lands in PriceLogs (all-null logs), but CurrentPrice is
        // populated. Dropping them silently rendered real items as
        // "no market price" (Lightning Coil at 1ex, Death Articulated
        // at 1600ex). Same fallback the currency path always had.
        entry = {
          price: it.CurrentPrice,
          quantity:
            typeof it?.CurrentQuantity === "number" ? it.CurrentQuantity : 0,
          iconUrl: typeof it?.IconUrl === "string" ? it.IconUrl : "",
          trend: null,
        };
      }
      if (entry && !map.has(name)) {
        map.set(name, entry);
      }
    }
    page += 1;
  } while (page <= pages);
}

function freshUnique(league: string): UniqueSnapshot | null {
  if (
    uniqueCache &&
    uniqueCache.league === league &&
    uniqueCache.expires > Date.now()
  ) {
    return uniqueCache;
  }
  return null;
}

async function uniqueSnapshot(
  league: string,
  options: { force?: boolean } = {},
): Promise<UniqueSnapshot> {
  const hit = options.force ? null : freshUnique(league);
  if (hit) return hit;

  if (!uniqueInflight || uniqueInflight.league !== league) {
    const promise = fetchUniqueMap(league)
      .then((fetched) => {
        // Same degraded-pull policy as the currency snapshot: backfill from
        // last-good and retry soon instead of caching holes for a full TTL.
        const complete = fetched.complete && fetched.map.size > 0;
        const last =
          uniqueCache && uniqueCache.league === league ? uniqueCache : null;
        if (!complete && last) backfill(fetched.map, last.map);
        uniqueCache = {
          league,
          map: fetched.map,
          complete,
          expires:
            Date.now() + (complete ? SCOUT_TTL_MS : SCOUT_RETRY_TTL_MS),
        };
        return uniqueCache;
      })
      .catch((error) => {
        console.error(
          "poe2scout: unique snapshot refresh failed:",
          error instanceof Error ? error.message : error,
        );
        const last =
          uniqueCache && uniqueCache.league === league ? uniqueCache : null;
        const fallback: UniqueSnapshot = {
          league,
          map: last?.map ?? new Map(),
          complete: false,
          expires: Date.now() + SCOUT_RETRY_TTL_MS,
        };
        uniqueCache = fallback;
        return fallback;
      })
      .finally(() => {
        if (uniqueInflight?.promise === promise) uniqueInflight = null;
      });
    uniqueInflight = { league, promise };
  }
  return uniqueInflight.promise;
}

/**
 * Warmed unique price map for `league` (Name -> {price, quantity, iconUrl}).
 * 15-minute TTL; concurrent callers share one in-flight pull. Never throws —
 * last-good (or empty) map on failure, same contract as priceMap().
 */
export async function uniquePriceMap(league: string): Promise<Map<string, UniquePrice>> {
  return (await uniqueSnapshot(league)).map;
}

/**
 * uniquePriceMap() plus the snapshot's `complete` flag, so corpus builders can
 * cache results assembled from degraded market data on a shorter TTL.
 */
export async function uniquePriceMapDetailed(
  league: string,
): Promise<MarketData<UniquePrice>> {
  const snap = await uniqueSnapshot(league);
  return { map: snap.map, complete: snap.complete };
}

/**
 * Proactively refresh the unique-item snapshot even if the current cache has
 * not expired yet.
 */
export async function refreshUniquePriceMap(
  league: string,
): Promise<Map<string, UniquePrice>> {
  return (await uniqueSnapshot(league, { force: true })).map;
}

/** Test seam for the unique snapshot. */
export function _clearUniqueCache(): void {
  uniqueCache = null;
  uniqueInflight = null;
}

/** Test seam: drop the in-memory cache and any in-flight refresh. */
export function _clearCache(): void {
  cache = null;
  inflight = null;
}

/** Test seam: shrink the retry backoff so retry tests run fast. */
export function _setRetryBaseMs(ms: number): void {
  retryBaseMs = ms;
}
