import { Host } from "./stubs/IPC";

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
//   GET /poe2/Leagues/{enc}/Currencies/ByCategory?Category={cat}&PerPage=250
//     -> { Items: [{ ApiId, CurrentPrice, CurrentQuantity }], Pages, Total }
//
// We loop the currency categories (not the flat /Items pull) because /Items
// omits CurrentQuantity, and the currency rows need quantity for the listing
// "total". One refresh = 1 (Leagues) + 1 (Categories) + N (per category)
// requests; N is ~17, well within the 15-minute window. PerPage caps at 250
// (the API rejects 500 with 422), so we page each category to be safe.
//
// All calls go through Host.proxy: shared UA, and (importantly) NO POESESSID —
// Host.proxy attaches the cookie only for pathofexile.com hosts, never here.

const BASE = "poe2scout.com/api";
const REALM = "poe2";
const TTL_MS = 15 * 60 * 1000;
const PER_PAGE = 250;

export interface ScoutPrice {
  price: number; // CurrentPrice, in exalted
  quantity: number; // CurrentQuantity
  history: number[]; // PriceLogs Price values, chronological (oldest→newest), nulls dropped
}

interface Snapshot {
  league: string;
  map: Map<string, ScoutPrice>;
  divine: number | null; // DivinePrice for this league (exalted per 1 divine)
  expires: number; // epoch ms
}

// Module-level cache + single in-flight refresh promise (dedupes concurrent
// warms so two simultaneous lookups issue ONE bulk pull).
let cache: Snapshot | null = null;
let inflight: Promise<Snapshot> | null = null;

function enc(league: string): string {
  return encodeURIComponent(league);
}

async function getJson(path: string): Promise<any> {
  const r = await Host.proxy(`${BASE}/${REALM}/${path}`);
  if (!r.ok) throw new Error(`poe2scout ${path} -> ${r.status}`);
  return r.json();
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

/** Pull every currency item across all currency categories into a price map. */
async function fetchPriceMap(league: string): Promise<Map<string, ScoutPrice>> {
  const map = new Map<string, ScoutPrice>();
  const cats = await getJson(`Leagues/${enc(league)}/Items/Categories`);
  const categories: string[] = Array.isArray(cats?.CurrencyCategories)
    ? cats.CurrencyCategories.map((c: any) => c?.ApiId).filter(
        (a: unknown): a is string => typeof a === "string",
      )
    : [];

  for (const cat of categories) {
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
        const price = it?.CurrentPrice;
        if (typeof apiId !== "string" || typeof price !== "number") continue;
        const quantity =
          typeof it?.CurrentQuantity === "number" ? it.CurrentQuantity : 0;
        // PriceLogs arrive newest-first; reverse to chronological order and
        // drop any null/non-finite Price values.
        const logs: unknown[] = Array.isArray(it?.PriceLogs) ? it.PriceLogs : [];
        const history: number[] = logs
          .map((l: any) => l?.Price)
          .filter((p: unknown): p is number => typeof p === "number" && Number.isFinite(p))
          .reverse();
        // First write wins; an ApiId should be unique across categories anyway.
        if (!map.has(apiId)) map.set(apiId, { price, quantity, history });
      }
      page += 1;
    } while (page <= pages);
  }
  return map;
}

// Perform one full warm (leagues-list + category loop). Defensive: each half is
// independently best-effort, but a hard failure rejects so the caller can fall
// back to the last-good snapshot.
async function warm(league: string): Promise<Snapshot> {
  // Run both pulls; tolerate a leagues failure (divine -> null) but a failed
  // map pull is a real failure (we'd cache an empty map otherwise).
  const [divine, map] = await Promise.all([
    getJson("Leagues")
      .then((ls) => divineFromLeagues(ls, league))
      .catch(() => null),
    fetchPriceMap(league),
  ]);
  return { league, map, divine, expires: Date.now() + TTL_MS };
}

function fresh(league: string): Snapshot | null {
  if (cache && cache.league === league && cache.expires > Date.now()) {
    return cache;
  }
  return null;
}

// Return a warmed snapshot, refreshing if stale/missing. Concurrent callers
// share a single in-flight warm. On failure, keep the last-good snapshot (or
// fall back to an empty snapshot) and never throw.
async function snapshot(league: string): Promise<Snapshot> {
  const hit = fresh(league);
  if (hit) return hit;

  if (!inflight) {
    inflight = warm(league)
      .then((snap) => {
        cache = snap;
        return snap;
      })
      .catch(() => {
        // Keep last-good if it is for the same league; else an empty snapshot
        // (still cached so we don't hammer a failing API every lookup).
        const fallback: Snapshot =
          cache && cache.league === league
            ? cache
            : {
                league,
                map: new Map(),
                divine: null,
                expires: Date.now() + TTL_MS,
              };
        cache = fallback;
        return fallback;
      })
      .finally(() => {
        inflight = null;
      });
  }
  return inflight;
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
 * Aggregate price for a single tradeTag (== poe2scout ApiId) in `league`, or
 * null when the item is not tracked / the warm failed.
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
}

let uniqueCache: UniqueSnapshot | null = null;
let uniqueInflight: Promise<UniqueSnapshot> | null = null;

async function fetchUniqueMap(league: string): Promise<Map<string, UniquePrice>> {
  const map = new Map<string, UniquePrice>();
  const cats = await getJson(`Leagues/${enc(league)}/Items/Categories`);
  const categories: string[] = Array.isArray(cats?.UniqueCategories)
    ? cats.UniqueCategories.map((c: any) => c?.ApiId).filter(
        (x: unknown): x is string => typeof x === "string",
      )
    : [];
  for (const cat of categories) {
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
        // PriceLogs arrive newest-first; the first numeric Price is current.
        const logs: any[] = Array.isArray(it?.PriceLogs) ? it.PriceLogs : [];
        const prices = logs
          .map((l) => l?.Price)
          .filter((p): p is number => typeof p === "number" && Number.isFinite(p));
        if (typeof name !== "string" || !prices.length) continue;
        const newest = logs.find((l) => typeof l?.Price === "number");
        const oldest = prices[prices.length - 1];
        if (!map.has(name)) {
          map.set(name, {
            price: newest.Price,
            quantity: typeof newest.Quantity === "number" ? newest.Quantity : 0,
            iconUrl: typeof it?.IconUrl === "string" ? it.IconUrl : "",
            trend:
              prices.length >= 2 && oldest > 0
                ? (newest.Price - oldest) / oldest
                : null,
          });
        }
      }
      page += 1;
    } while (page <= pages);
  }
  return map;
}

/**
 * Warmed unique price map for `league` (Name -> {price, quantity, iconUrl}).
 * 15-minute TTL; concurrent callers share one in-flight pull. Never throws —
 * last-good (or empty) map on failure, same contract as priceMap().
 */
export async function uniquePriceMap(league: string): Promise<Map<string, UniquePrice>> {
  if (uniqueCache && uniqueCache.league === league && uniqueCache.expires > Date.now()) {
    return uniqueCache.map;
  }
  if (!uniqueInflight) {
    uniqueInflight = fetchUniqueMap(league)
      .then((map) => {
        uniqueCache = { league, map, expires: Date.now() + TTL_MS };
        return uniqueCache;
      })
      .catch(() => {
        const fallback: UniqueSnapshot =
          uniqueCache && uniqueCache.league === league
            ? uniqueCache
            : { league, map: new Map(), expires: Date.now() + TTL_MS };
        uniqueCache = fallback;
        return fallback;
      })
      .finally(() => {
        uniqueInflight = null;
      });
  }
  return (await uniqueInflight).map;
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
