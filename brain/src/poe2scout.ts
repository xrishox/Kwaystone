import { Host } from "./stubs/IPC";
import { MarketQuote, selectMarketQuotes } from "./scout/quotes";
import { Http429, retryAfterMs, Scheduler } from "./scheduler";

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
//     -> completed-hour pair volumes, relative prices, and quote-side stock
//   GET /poe2/Leagues/{enc}/Currencies/ByCategory?Category={cat}&PerPage=250
//     -> { Items: [{ ApiId, CurrentPrice, CurrentQuantity }], Pages, Total }
//
// We loop the currency categories (not the flat /Items pull) because /Items
// omits CurrentQuantity, and the currency rows need quantity for the listing
// "total". One refresh = 1 (Leagues) + 1 (Categories) + 1 (SnapshotPairs)
// + N (per category)
// requests; N is ~17, well within the hourly window. PerPage caps at 250
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

const BASE = "api.poe2scout.com";
const REALM = "poe2";
export const SCOUT_TTL_MS = 60 * 60 * 1000;
// Degraded snapshots (failed or partial pulls) retry on this cadence instead
// of serving catalog-only "no market price" rows for a full TTL window.
export const SCOUT_RETRY_TTL_MS = 60 * 1000;
const PER_PAGE = 250;
const REQUEST_TIMEOUT_MS = 10_000;
const REQUEST_ATTEMPTS = 3;
let retryBaseMs = 300;

export const SCOUT_PRIORITY = {
  interactive: 0,
  foreground: 10,
  background: 50,
} as const;

export type ScoutPriority = (typeof SCOUT_PRIORITY)[keyof typeof SCOUT_PRIORITY];

let scoutScheduler = new Scheduler();

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
  quoteMaxStock?: number; // Highest quote-side stock observed in the completed hour.
  quoteItemVolume?: number; // Item-side units traded in the snapshot window.
}

interface Snapshot {
  league: string;
  map: Map<string, ScoutPrice>;
  divine: number | null; // DivinePrice for this league (exalted per 1 divine)
  expires: number; // epoch ms
  complete: boolean; // false when any part of the pull failed or is stale
}

const caches = new Map<string, Snapshot>();
const inflights = new Map<string, { forced: boolean; promise: Promise<Snapshot> }>();
const queuedForced = new Map<string, Promise<Snapshot>>();

function enc(league: string): string {
  return encodeURIComponent(league);
}

function transientStatus(status: number): boolean {
  return status >= 500;
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

class ScoutHttpError extends Error {
  constructor(
    readonly status: number,
    path: string,
  ) {
    super(`poe2scout ${path} -> ${status}`);
  }
}

async function getJson(
  path: string,
  priority: ScoutPriority = SCOUT_PRIORITY.foreground,
): Promise<any> {
  let lastError: unknown;
  for (let attempt = 0; attempt < REQUEST_ATTEMPTS; attempt++) {
    if (attempt > 0) {
      // Exponential backoff with jitter so parallel pulls don't re-slam a
      // recovering API in lockstep.
      const base = retryBaseMs * 2 ** (attempt - 1);
      await sleep(base + Math.random() * base * 0.5);
    }
    try {
      return await scoutScheduler.schedule(
        `poe2scout:${path}:attempt:${attempt}`,
        priority,
        "scout",
        async () => {
          const response = await Host.proxy(`${BASE}/${REALM}/${path}`, {
            signal: AbortSignal.timeout(REQUEST_TIMEOUT_MS),
          });
          if (response.status === 429) {
            throw new Http429(retryAfterMs(response.headers) ?? 30_000);
          }
          if (!response.ok) throw new ScoutHttpError(response.status, path);
          return response.json();
        },
      );
    } catch (e) {
      lastError = e;
      if (e instanceof ScoutHttpError && !transientStatus(e.status)) break;
    }
  }
  throw lastError;
}

/**
 * Select the best well-supported historical native-currency quote for every
 * item in the current completed-hour exchange snapshot.
 */
async function fetchMarketQuotes(
  league: string,
  priority: ScoutPriority = SCOUT_PRIORITY.foreground,
): Promise<Map<string, MarketQuote>> {
  const pairs = await snapshotPairsRaw(league, { priority });
  return selectMarketQuotes(pairs);
}

// The raw exchange pairs behind the last quotes pull: the arbitrage matrix
// needs pair-level liquidity/stock and item display names, which the selected
// quote map alone doesn't carry.
type PairCache = {
  pairs: unknown[];
  fetchedAt: number;
  snapshotId?: number;
};

const pairCaches = new Map<string, PairCache>();
const pairInflights = new Map<
  string,
  { forced: boolean; promise: Promise<unknown[]> }
>();
const pairQueuedForced = new Map<string, Promise<unknown[]>>();

function validatedPairs(value: unknown): { pairs: unknown[]; snapshotId?: number } {
  if (!Array.isArray(value)) throw new Error("poe2scout SnapshotPairs is not an array");
  const ids = new Set<number>();
  for (const raw of value) {
    if (!raw || typeof raw !== "object") {
      throw new Error("poe2scout SnapshotPairs contains a malformed row");
    }
    const row = raw as {
      CurrencyExchangeSnapshotId?: unknown;
      Volume?: unknown;
      CurrencyOne?: { ApiId?: unknown };
      CurrencyTwo?: { ApiId?: unknown };
      CurrencyOneData?: { VolumeTraded?: unknown };
      CurrencyTwoData?: { VolumeTraded?: unknown };
    };
    const id = Number(row.CurrencyExchangeSnapshotId);
    const liquidity = Number(row.Volume);
    const oneVolume = Number(row.CurrencyOneData?.VolumeTraded);
    const twoVolume = Number(row.CurrencyTwoData?.VolumeTraded);
    if (
      !Number.isInteger(id) || id <= 0 ||
      typeof row.CurrencyOne?.ApiId !== "string" || !row.CurrencyOne.ApiId ||
      typeof row.CurrencyTwo?.ApiId !== "string" || !row.CurrencyTwo.ApiId ||
      !Number.isFinite(liquidity) || liquidity < 0 ||
      !Number.isFinite(oneVolume) || oneVolume < 0 ||
      !Number.isFinite(twoVolume) || twoVolume < 0
    ) {
      throw new Error("poe2scout SnapshotPairs contains a malformed row");
    }
    ids.add(id);
  }
  if (ids.size > 1) throw new Error("poe2scout SnapshotPairs mixes snapshot ids");
  return { pairs: value, ...(ids.size === 1 ? { snapshotId: [...ids][0] } : {}) };
}

function startPairPull(
  league: string,
  forced: boolean,
  priority: ScoutPriority,
): Promise<unknown[]> {
  const promise = getJson(`Leagues/${enc(league)}/SnapshotPairs`, priority)
    .then((raw) => {
      const validated = validatedPairs(raw);
      pairCaches.set(league, { ...validated, fetchedAt: Date.now() });
      return validated.pairs;
    })
    .finally(() => {
      if (pairInflights.get(league)?.promise === promise) pairInflights.delete(league);
    });
  pairInflights.set(league, { forced, promise });
  return promise;
}

/**
 * Raw SnapshotPairs for `league`, served from the same pull the price warm
 * just did when fresh (no duplicate request), fetched on demand otherwise.
 * `force` bypasses the freshness window (Alt+S wants the latest on press).
 */
export async function snapshotPairsRaw(
  league: string,
  options: { force?: boolean; priority?: ScoutPriority } = {},
): Promise<unknown> {
  const priority = options.priority ?? SCOUT_PRIORITY.foreground;
  const current = pairInflights.get(league);
  if (current) {
    if (!options.force || current.forced) return current.promise;
    const queued = pairQueuedForced.get(league);
    if (queued) return queued;
    const forced = current.promise
      .catch(() => null)
      .then(() => startPairPull(league, true, priority))
      .finally(() => {
        if (pairQueuedForced.get(league) === forced) pairQueuedForced.delete(league);
      });
    pairQueuedForced.set(league, forced);
    return forced;
  }
  const cached = pairCaches.get(league);
  if (
    !options.force &&
    cached &&
    Date.now() - cached.fetchedAt < SCOUT_TTL_MS
  ) {
    return cached.pairs;
  }
  return startPairPull(league, Boolean(options.force), priority);
}

/** Latest completed Currency Exchange snapshot identifier for a league. */
export async function exchangeSnapshotEpoch(
  league: string,
  options: { priority?: ScoutPriority } = {},
): Promise<string | null> {
  const data = await getJson(
    `Leagues/${enc(league)}/ExchangeSnapshot`,
    options.priority,
  );
  if (typeof data === "string" && data.trim()) return data;
  if (typeof data === "number" && Number.isFinite(data)) return String(data);
  const value = data?.Epoch ?? data?.epoch ?? data?.Data?.Epoch ?? data?.data?.epoch;
  if (typeof value === "string" && value.trim()) return value;
  if (typeof value === "number" && Number.isFinite(value)) return String(value);
  return null;
}

export interface ExchangePairSnapshot {
  pairs: unknown[];
  epoch: string;
  snapshotId?: number;
  fetchedAt: number;
}

/**
 * Pull one internally consistent completed-hour snapshot. A rollover between
 * the epoch probe and pair response is retried once rather than mislabelling
 * old pairs with a new timestamp.
 */
export async function exchangePairSnapshot(
  league: string,
  _options: { force?: boolean } = {},
): Promise<ExchangePairSnapshot> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    const before = await exchangeSnapshotEpoch(league, {
      priority: SCOUT_PRIORITY.interactive,
    });
    if (!before) throw new Error("poe2scout exchange epoch is unavailable");
    const pairs = await snapshotPairsRaw(league, {
      force: true,
      priority: SCOUT_PRIORITY.interactive,
    });
    const after = await exchangeSnapshotEpoch(league, {
      priority: SCOUT_PRIORITY.interactive,
    });
    if (before === after) {
      const cached = pairCaches.get(league);
      return {
        pairs: pairs as unknown[],
        epoch: before,
        ...(cached?.snapshotId ? { snapshotId: cached.snapshotId } : {}),
        fetchedAt: cached?.fetchedAt ?? Date.now(),
      };
    }
  }
  throw new Error("poe2scout snapshot changed during refresh");
}

/**
 * Fetch the leagues list and resolve the DivinePrice for the exact `league`.
 * Returns null on a miss or any failure; another current league must never
 * contaminate the requested economy.
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
  const dp = exact?.DivinePrice;
  return typeof dp === "number" && Number.isFinite(dp) && dp > 0 ? dp : null;
}

type Categories = { currency: string[]; unique: string[] };
const categoryCaches = new Map<string, { value: Categories; fetchedAt: number }>();
const categoryInflights = new Map<string, Promise<Categories>>();

function categoryIds(value: unknown): string[] {
  if (!Array.isArray(value)) return [];
  return value
    .map((entry) =>
      entry && typeof entry === "object"
        ? (entry as { ApiId?: unknown }).ApiId
        : null,
    )
    .filter(
      (apiId): apiId is string => typeof apiId === "string" && apiId.length > 0,
    );
}

async function categories(
  league: string,
  priority: ScoutPriority,
): Promise<Categories> {
  const cached = categoryCaches.get(league);
  if (cached && Date.now() - cached.fetchedAt < SCOUT_TTL_MS) return cached.value;
  const active = categoryInflights.get(league);
  if (active) return active;
  const promise = getJson(
    `Leagues/${enc(league)}/Items/Categories`,
    priority,
  )
    .then((raw) => {
      if (!raw || typeof raw !== "object") {
        throw new Error("poe2scout category response is malformed");
      }
      const record = raw as {
        CurrencyCategories?: unknown;
        UniqueCategories?: unknown;
      };
      const value = {
        currency: categoryIds(record.CurrencyCategories),
        unique: categoryIds(record.UniqueCategories),
      };
      if (!value.currency.length && !value.unique.length) {
        throw new Error("poe2scout category response is empty");
      }
      categoryCaches.set(league, { value, fetchedAt: Date.now() });
      return value;
    })
    .finally(() => {
      if (categoryInflights.get(league) === promise) categoryInflights.delete(league);
    });
  categoryInflights.set(league, promise);
  return promise;
}

function pageData(value: unknown): { pages: number; items: unknown[] } {
  if (!value || typeof value !== "object") {
    throw new Error("poe2scout category page is malformed");
  }
  const record = value as { Pages?: unknown; Items?: unknown };
  const pages = Number(record.Pages);
  if (!Number.isInteger(pages) || pages < 1 || pages > 100) {
    throw new Error("poe2scout category page count is invalid");
  }
  if (!Array.isArray(record.Items)) {
    throw new Error("poe2scout category items are malformed");
  }
  return { pages, items: record.Items };
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
  priority: ScoutPriority,
): Promise<{ map: Map<string, ScoutPrice>; complete: boolean }> {
  const map = new Map<string, ScoutPrice>();
  let complete = true;
  const categorySet = await categories(league, priority);
  if (!categorySet.currency.length) complete = false;
  const marketQuotes = await fetchMarketQuotes(league, priority).catch(() => {
    complete = false;
    return new Map<string, MarketQuote>();
  });

  for (const cat of categorySet.currency) {
    try {
      await fetchCurrencyCategory(league, cat, marketQuotes, map, priority);
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
  priority: ScoutPriority,
): Promise<void> {
  let page = 1;
  let pages = 1;
  do {
    const res = await getJson(
      `Leagues/${enc(league)}/Currencies/ByCategory?Category=${encodeURIComponent(cat)}&PerPage=${PER_PAGE}&Page=${page}`,
      priority,
    );
    const pageResult = pageData(res);
    pages = pageResult.pages;
    for (const raw of pageResult.items) {
      const it = raw as any;
      const apiId = it?.ApiId;
      const aggregatePrice = it?.CurrentPrice;
      if (
        typeof apiId !== "string" ||
        typeof aggregatePrice !== "number" ||
        !Number.isFinite(aggregatePrice) ||
        aggregatePrice <= 0
      ) continue;
      const quote = marketQuotes.get(apiId);
      const quantity =
        typeof it?.CurrentQuantity === "number" &&
        Number.isFinite(it.CurrentQuantity) &&
        it.CurrentQuantity >= 0
          ? it.CurrentQuantity
          : 0;
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
          price: aggregatePrice,
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
                quoteMaxStock: quote.maxStock,
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
async function warm(
  league: string,
  priority: ScoutPriority,
): Promise<Snapshot> {
  // Run both pulls; tolerate a leagues failure (divine -> null, backfilled
  // from the last good snapshot below).
  const [divine, fetched] = await Promise.all([
    getJson("Leagues", priority)
      .then((ls) => divineFromLeagues(ls, league))
      .catch(() => null),
    fetchPriceMap(league, priority),
  ]);
  // An empty map from a "successful" pull is not trustworthy either: treat it
  // as degraded so it retries soon instead of pricing nothing for a full TTL.
  const complete = fetched.complete && fetched.map.size > 0;
  const last = caches.get(league) ?? null;
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
  const cached = caches.get(league);
  if (cached && cached.expires > Date.now()) return cached;
  return null;
}

function startSnapshotPull(
  league: string,
  forced: boolean,
  priority: ScoutPriority,
): Promise<Snapshot> {
  const promise = warm(league, priority)
    .then((value) => {
      caches.set(league, value);
      return value;
    })
    .catch((error) => {
      console.error(
        "poe2scout: currency snapshot refresh failed:",
        error instanceof Error ? error.message : error,
      );
      const last = caches.get(league) ?? null;
      const fallback: Snapshot = {
        league,
        map: last?.map ?? new Map(),
        divine: last?.divine ?? null,
        complete: false,
        expires: Date.now() + SCOUT_RETRY_TTL_MS,
      };
      caches.set(league, fallback);
      return fallback;
    })
    .finally(() => {
      if (inflights.get(league)?.promise === promise) inflights.delete(league);
    });
  inflights.set(league, { forced, promise });
  return promise;
}

// Return a warmed snapshot, refreshing if stale/missing. Concurrent callers
// share a single in-flight warm. On failure, keep serving the last-good
// snapshot (or an empty one) marked degraded on the short retry TTL — cached
// so we don't hammer a failing API on every lookup, but recovering within a
// minute instead of a full TTL window. Never throws.
async function snapshot(
  league: string,
  options: { force?: boolean; priority?: ScoutPriority } = {},
): Promise<Snapshot> {
  const priority = options.priority ?? SCOUT_PRIORITY.foreground;
  const hit = options.force ? null : fresh(league);
  if (hit) return hit;

  const active = inflights.get(league);
  if (active) {
    if (!options.force || active.forced) return active.promise;
    const queued = queuedForced.get(league);
    if (queued) return queued;
    const forced = active.promise
      .catch(() => null)
      .then(() => startSnapshotPull(league, true, priority))
      .finally(() => {
        if (queuedForced.get(league) === forced) queuedForced.delete(league);
      });
    queuedForced.set(league, forced);
    return forced;
  }
  return startSnapshotPull(league, Boolean(options.force), priority);
}

/** A market map plus whether the backing pull was fully fresh. */
interface MarketData<T> {
  map: Map<string, T>;
  complete: boolean;
}

/**
 * Warmed bulk price map for `league` (ApiId -> {price, quantity}). Cached with
 * an hourly TTL; a second call within the window issues no network. Never
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
  options: { priority?: ScoutPriority } = {},
): Promise<Map<string, ScoutPrice>> {
  return (await snapshot(league, {
    force: true,
    priority: options.priority ?? SCOUT_PRIORITY.background,
  })).map;
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
  price: number; // CurrentPrice in exalted, or a marked historical fallback.
  quantity: number;
  iconUrl: string;
  trend: number | null; // fractional change over the PriceLogs window, oldest -> newest
  priceSource: "current" | "history";
}

interface UniqueSnapshot {
  league: string;
  map: Map<string, UniquePrice>; // keyed by unique Name
  expires: number;
  complete: boolean; // false when any part of the pull failed or is stale
}

const uniqueCaches = new Map<string, UniqueSnapshot>();
const uniqueInflights = new Map<
  string,
  { forced: boolean; promise: Promise<UniqueSnapshot> }
>();
const uniqueQueuedForced = new Map<string, Promise<UniqueSnapshot>>();

/** Same per-category fault tolerance and `complete` contract as fetchPriceMap. */
async function fetchUniqueMap(
  league: string,
  priority: ScoutPriority,
): Promise<{ map: Map<string, UniquePrice>; complete: boolean }> {
  const map = new Map<string, UniquePrice>();
  let complete = true;
  const categorySet = await categories(league, priority);
  if (!categorySet.unique.length) complete = false;
  for (const cat of categorySet.unique) {
    try {
      await fetchUniqueCategory(league, cat, map, priority);
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
  priority: ScoutPriority,
): Promise<void> {
  let page = 1;
  let pages = 1;
  do {
    const res = await getJson(
      `Leagues/${enc(league)}/Uniques/ByCategory?Category=${encodeURIComponent(cat)}&PerPage=${PER_PAGE}&Page=${page}`,
      priority,
    );
    const pageResult = pageData(res);
    pages = pageResult.pages;
    for (const raw of pageResult.items) {
      const it = raw as any;
      const name = it?.Name;
      if (typeof name !== "string") continue;
      const logs: any[] = Array.isArray(it?.PriceLogs) ? it.PriceLogs : [];
      const prices = logs
        .map((l) => l?.Price)
        .filter(
          (p): p is number =>
            typeof p === "number" && Number.isFinite(p) && p > 0,
        );
      let entry: UniquePrice | null = null;
      const currentPrice = Number(it?.CurrentPrice);
      const currentQuantity = Number(it?.CurrentQuantity);
      const oldest = prices[prices.length - 1];
      if (Number.isFinite(currentPrice) && currentPrice > 0) {
        entry = {
          price: currentPrice,
          quantity:
            Number.isFinite(currentQuantity) && currentQuantity >= 0
              ? currentQuantity
              : 0,
          iconUrl: typeof it?.IconUrl === "string" ? it.IconUrl : "",
          trend: oldest > 0 ? (currentPrice - oldest) / oldest : null,
          priceSource: "current",
        };
      } else if (prices.length) {
        const newest = logs.find(
          (l) =>
            typeof l?.Price === "number" &&
            Number.isFinite(l.Price) &&
            l.Price > 0,
        );
        entry = {
          price: newest.Price,
          quantity:
            typeof newest.Quantity === "number" && newest.Quantity >= 0
              ? newest.Quantity
              : 0,
          iconUrl: typeof it?.IconUrl === "string" ? it.IconUrl : "",
          trend:
            prices.length >= 2 && oldest > 0
              ? (newest.Price - oldest) / oldest
              : null,
          priceSource: "history",
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
  const cached = uniqueCaches.get(league);
  if (cached && cached.expires > Date.now()) return cached;
  return null;
}

function startUniquePull(
  league: string,
  forced: boolean,
  priority: ScoutPriority,
): Promise<UniqueSnapshot> {
  const promise = fetchUniqueMap(league, priority)
    .then((fetched) => {
      const complete = fetched.complete && fetched.map.size > 0;
      const last = uniqueCaches.get(league) ?? null;
      if (!complete && last) backfill(fetched.map, last.map);
      const value = {
        league,
        map: fetched.map,
        complete,
        expires: Date.now() + (complete ? SCOUT_TTL_MS : SCOUT_RETRY_TTL_MS),
      };
      uniqueCaches.set(league, value);
      return value;
    })
    .catch((error) => {
      console.error(
        "poe2scout: unique snapshot refresh failed:",
        error instanceof Error ? error.message : error,
      );
      const last = uniqueCaches.get(league) ?? null;
      const fallback: UniqueSnapshot = {
        league,
        map: last?.map ?? new Map(),
        complete: false,
        expires: Date.now() + SCOUT_RETRY_TTL_MS,
      };
      uniqueCaches.set(league, fallback);
      return fallback;
    })
    .finally(() => {
      if (uniqueInflights.get(league)?.promise === promise) {
        uniqueInflights.delete(league);
      }
    });
  uniqueInflights.set(league, { forced, promise });
  return promise;
}

async function uniqueSnapshot(
  league: string,
  options: { force?: boolean; priority?: ScoutPriority } = {},
): Promise<UniqueSnapshot> {
  const priority = options.priority ?? SCOUT_PRIORITY.foreground;
  const hit = options.force ? null : freshUnique(league);
  if (hit) return hit;

  const active = uniqueInflights.get(league);
  if (active) {
    if (!options.force || active.forced) return active.promise;
    const queued = uniqueQueuedForced.get(league);
    if (queued) return queued;
    const forced = active.promise
      .catch(() => null)
      .then(() => startUniquePull(league, true, priority))
      .finally(() => {
        if (uniqueQueuedForced.get(league) === forced) {
          uniqueQueuedForced.delete(league);
        }
      });
    uniqueQueuedForced.set(league, forced);
    return forced;
  }
  return startUniquePull(league, Boolean(options.force), priority);
}

/**
 * Warmed unique price map for `league` (Name -> {price, quantity, iconUrl}).
 * Hourly TTL; concurrent callers share one in-flight pull. Never throws —
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
  options: { priority?: ScoutPriority } = {},
): Promise<Map<string, UniquePrice>> {
  return (await uniqueSnapshot(league, {
    force: true,
    priority: options.priority ?? SCOUT_PRIORITY.background,
  })).map;
}

/** Test seam for the unique snapshot. */
export function _clearUniqueCache(): void {
  uniqueCaches.clear();
  uniqueInflights.clear();
  uniqueQueuedForced.clear();
}

/** Test seam: drop the in-memory cache and any in-flight refresh. */
export function _clearCache(): void {
  caches.clear();
  inflights.clear();
  queuedForced.clear();
  pairCaches.clear();
  pairInflights.clear();
  pairQueuedForced.clear();
  categoryCaches.clear();
  categoryInflights.clear();
  leagueListCache = null;
}

export interface LeagueInfo {
  name: string;
  current: boolean;
  permanent: boolean;
}

// Permanent leagues are always trackable even though the API flags them
// IsCurrent=false (that flag means "current challenge league").
const PERMANENT_LEAGUES = new Set(["Standard", "Hardcore"]);
const LEAGUE_LIST_TTL_MS = 5 * 60 * 1000;

let leagueListCache: { leagues: LeagueInfo[]; fetchedAt: number } | null = null;

/**
 * Trackable leagues right now: permanent leagues plus every currently active
 * (IsCurrent) league, softcore and Hardcore variants alike. Dead leagues are
 * never returned. Cached briefly; a failed refresh serves the last-good list
 * so the league selector never empties mid-session.
 */
export async function leagueList(
  options: { force?: boolean } = {},
): Promise<{ leagues: LeagueInfo[]; fetchedAt: number }> {
  if (
    !options.force &&
    leagueListCache &&
    Date.now() - leagueListCache.fetchedAt < LEAGUE_LIST_TTL_MS
  ) {
    return leagueListCache;
  }
  try {
    const raw = await getJson("Leagues");
    if (!Array.isArray(raw)) throw new Error("poe2scout league list is malformed");
    const leagues = raw
      .map((entry) => {
        const name = String(entry?.Value ?? "");
        return {
          name,
          current: entry?.IsCurrent === true,
          permanent: PERMANENT_LEAGUES.has(name),
        };
      })
      .filter(
        (entry) =>
          entry.name.length > 0 && (entry.current || entry.permanent),
      );
    if (!leagues.length) throw new Error("poe2scout league list is empty");
    leagueListCache = { leagues, fetchedAt: Date.now() };
    return leagueListCache;
  } catch (e) {
    if (leagueListCache) return leagueListCache;
    throw e;
  }
}

/** Test seam: shrink the retry backoff so retry tests run fast. */
export function _setRetryBaseMs(ms: number): void {
  retryBaseMs = ms;
}

export function _setScoutScheduler(next: Scheduler): void {
  scoutScheduler = next;
}
