import {
  divinePrice,
  priceMapDetailed,
  uniquePriceMapDetailed,
} from "./poe2scout";
import { resolveIcon } from "./icons";

export interface ScanCorpusRow {
  price: number;
  quantity: number;
  iconPath: string | null;
  kind: "unique" | "tagged" | "catalog";
  priceAvailable: boolean;
  w: number; // inventory slots wide — poed normalizes template density with it
  h: number;
  trend: number | null; // fractional price change over the snapshot window
  quoteAmount?: number;
  quoteCurrency?: string;
  quoteCurrencyText?: string;
  quoteLiquidity?: number;
  quoteBuyerStock?: number;
  quoteItemVolume?: number;
  // "low" when a non-trivial price rests on thin traded volume; scanners
  // surface these as estimates instead of confident prices.
  priceConfidence?: "low" | "ok";
  quoteAvailable?: boolean;
  exaltedPerChaos?: number;
  exaltedPerDivine?: number;
  sourceTag?: string;
  sourceCategory?: string;
}

// poecdn gen/image URLs carry their item's slot size in a base64 JSON
// segment: /gen/image/<b64([25,14,{"f":...,"w":2,"h":1,...}])>/<sig>/x.png
function slotsFromUrl(url: string): { w: number; h: number } {
  try {
    const seg = url.split("/gen/image/")[1]?.split("/")[0] ?? "";
    const arr = JSON.parse(Buffer.from(seg, "base64").toString("utf8"));
    const o = arr?.[2];
    return { w: o?.w || 1, h: o?.h || 1 };
  } catch {
    return { w: 1, h: 1 };
  }
}

// Cold icon cache means ~1100 downloads on the first call; bound the fan-out
// so we don't open a thousand sockets at once. Warm calls are disk hits.

// The exchange order book prices anything, including items nobody actually
// trades: resting fantasy offers give idols/lineage supports divine-plus
// "prices" while the trade reality is empty. A non-trivial price is only
// confident when real volume stands behind it — item-side traded units for
// exchange rows, listing count for trade-priced uniques.
const CONFIDENT_PRICE_EX = 40;
function taggedConfidence(priceEx: number, itemVolume: number | undefined, quantity: number): "low" | "ok" {
  if (priceEx < CONFIDENT_PRICE_EX) return "ok";
  const volume = itemVolume ?? quantity;
  return volume >= 1000 ? "ok" : "low";
}
function uniqueConfidence(priceEx: number, listings: number): "low" | "ok" {
  if (priceEx < CONFIDENT_PRICE_EX) return "ok";
  return listings >= 10 ? "ok" : "low";
}

const ICON_CONCURRENCY = 16;
const CORPUS_TTL_MS = 15 * 60 * 1000;
// A corpus assembled while poe2scout was failing (missing/partial price data)
// is served — stale prices beat "no market price" — but only on this short
// TTL so the next scan rebuilds it as soon as the market data recovers.
const CORPUS_RETRY_TTL_MS = 60 * 1000;

interface CorpusSnapshot {
  league: string;
  rows: Record<string, ScanCorpusRow>;
  expires: number;
  version: number;
}

let corpusCache: CorpusSnapshot | null = null;
// Seeded per process so versions never collide across brain restarts: a poed
// cache holding version N from a dead process must not match the new
// process's version N, or `ifVersion` short-circuits ("unchanged") and poed
// keeps pricing from a stale snapshot for up to CORPUS_TTL_MS. Date.now()
// alone could still collide if two processes start within the same
// millisecond, so a random sub-millisecond component makes the seed
// collision-proof. Rebuilds still increment by 1.
let corpusVersion = Date.now() * 1000 + Math.floor(Math.random() * 1000);
let corpusInflight:
  | { league: string; promise: Promise<CorpusSnapshot> }
  | null = null;

async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (next < items.length) {
        const i = next++;
        out[i] = await fn(items[i]);
      }
    }),
  );
  return out;
}

/**
 * The unique-scan corpus: display name -> {price, quantity, iconPath, kind}.
 *
 * Recognition uses the full vendored item catalog, then overlays live market
 * prices where Poe2Scout has them:
 *  - all local ITEM/GEM/UNIQUE icons are included so visual scanning can name
 *    unpriced or temporarily unavailable rewards;
 *  - uniques from poe2scout Uniques/ByCategory override catalog rows by Name;
 *  - tradeTagged items (currency/omens/gems — Ritual rewards) from the
 *    existing currency priceMap override catalog rows by live Name/ref.
 *
 * Icons are resolved to brain-cached local files (poed never fetches remote
 * URLs). Any icon failure degrades that row's iconPath to null — prices still
 * flow. Shared arts (e.g. uncut-gem levels) intentionally keep one row per
 * NAME here; poed's matcher dedupes by icon file and groups the labels.
 *
 * `marketComplete` is false when either poe2scout pull was degraded (failed or
 * partial); the caller caches such a corpus on a short retry TTL so pricing
 * recovers quickly after an outage instead of waiting out the full TTL.
 *
 * PRECONDITION: initBrainData() must have run (server.ts awaits it) — the
 * vendored tag->name lookups are empty until then.
 */
async function buildScanCorpus(
  league: string,
): Promise<{ rows: Record<string, ScanCorpusRow>; marketComplete: boolean }> {
  const { ITEMS_ITERATOR, TRADE_TAG_TO_REF, ITEM_BY_REF } = await import(
    "@/assets/data"
  );
  const [uniqueData, currencyData] = await Promise.all([
    uniquePriceMapDetailed(league),
    priceMapDetailed(league),
  ]);
  const uniques = uniqueData.map;
  const currencies = currencyData.map;
  const marketComplete = uniqueData.complete && currencyData.complete;
  const divine = await divinePrice(league);
  const chaos = currencies.get("chaos")?.price;
  const conversionRates = {
    ...(typeof chaos === "number" && Number.isFinite(chaos) && chaos > 0
      ? { exaltedPerChaos: chaos }
      : {}),
    ...(typeof divine === "number" && Number.isFinite(divine) && divine > 0
      ? { exaltedPerDivine: divine }
      : {}),
  };

  const rows: Array<{ name: string; iconUrl: string; row: Omit<ScanCorpusRow, "iconPath"> }> = [];
  const supersededCatalogNames = new Set<string>();

  for (const namespace of ["ITEM", "GEM", "UNIQUE"] as const) {
    for (const item of ITEMS_ITERATOR(`"namespace": "${namespace}"`)) {
      if (!item.name || !item.icon?.startsWith("https://")) continue;
      rows.push({
        name: item.name,
        iconUrl: item.icon,
        row: {
          price: 0,
          quantity: 0,
          kind: "catalog",
          priceAvailable: false,
          trend: null,
          w: item.w || slotsFromUrl(item.icon).w,
          h: item.h || slotsFromUrl(item.icon).h,
        },
      });
    }
  }

  for (const [name, u] of uniques) {
    rows.push({
      name,
      iconUrl: u.iconUrl,
      row: {
        price: u.price, quantity: u.quantity, kind: "unique",
        priceAvailable: true,
        trend: u.trend, ...slotsFromUrl(u.iconUrl),
        priceConfidence: uniqueConfidence(u.price, u.quantity),
        ...conversionRates,
      },
    });
  }

  for (const [tag, scout] of currencies) {
    const ref = TRADE_TAG_TO_REF.get(tag);
    const name = scout.name ?? ref;
    if (!name) continue;
    if (ref && ref !== name) supersededCatalogNames.add(ref);
    const icon =
      scout.iconUrl ??
      (ref
        ? ITEM_BY_REF("ITEM", ref)?.[0]?.icon ??
          ITEM_BY_REF("GEM", ref)?.[0]?.icon
        : undefined);
    if (!icon || !icon.startsWith("https://")) continue;
    // Trend from the currency snapshot's chronological history.
    const h = scout.history;
    const trend =
      h.length >= 2 && h[0] > 0 ? (h[h.length - 1] - h[0]) / h[0] : null;
    rows.push({
      name,
      iconUrl: icon,
      row: {
        price: scout.price, quantity: scout.quantity, kind: "tagged",
        priceAvailable: true,
        trend,
        sourceTag: tag,
        ...(scout.category ? { sourceCategory: scout.category } : {}),
        ...conversionRates,
        ...(scout.quoteAmount !== undefined
          ? { quoteAmount: scout.quoteAmount }
          : {}),
        ...(scout.quoteCurrency
          ? { quoteCurrency: scout.quoteCurrency }
          : {}),
        ...(scout.quoteCurrencyText
          ? { quoteCurrencyText: scout.quoteCurrencyText }
          : {}),
        ...(scout.quoteLiquidity !== undefined
          ? { quoteLiquidity: scout.quoteLiquidity }
          : {}),
        ...(scout.quoteBuyerStock !== undefined
          ? { quoteBuyerStock: scout.quoteBuyerStock }
          : {}),
        ...(scout.quoteAvailable !== undefined
          ? { quoteAvailable: scout.quoteAvailable }
          : {}),
        ...(scout.quoteItemVolume !== undefined
          ? { quoteItemVolume: scout.quoteItemVolume }
          : {}),
        priceConfidence: taggedConfidence(
          scout.price,
          scout.quoteItemVolume,
          scout.quantity,
        ),
        ...slotsFromUrl(icon),
      },
    });
  }

  const resolved = await mapLimit(rows, ICON_CONCURRENCY, async (r) => {
    const iconPath = r.iconUrl
      ? await resolveIcon(r.iconUrl).catch(() => null)
      : null;
    return [r.name, { ...r.row, iconPath }] as const;
  });

  const out: Record<string, ScanCorpusRow> = {};
  for (const [name, row] of resolved) {
    out[name] = row;
  }
  for (const name of supersededCatalogNames) {
    delete out[name];
  }
  return { rows: out, marketComplete };
}

function freshCorpus(league: string): CorpusSnapshot | null {
  if (
    corpusCache &&
    corpusCache.league === league &&
    corpusCache.expires > Date.now()
  ) {
    return corpusCache;
  }
  return null;
}

function startCorpusRefresh(
  league: string,
  options: { forceMarketRefresh?: boolean } = {},
): Promise<CorpusSnapshot> {
  if (corpusInflight && corpusInflight.league === league) {
    return corpusInflight.promise;
  }

  const promise = (async () => {
    if (options.forceMarketRefresh) {
      const { refreshPriceMap, refreshUniquePriceMap } = await import("./poe2scout");
      await Promise.all([
        refreshPriceMap(league),
        refreshUniquePriceMap(league),
      ]);
    }
    const { rows, marketComplete } = await buildScanCorpus(league);
    corpusVersion += 1;
    return {
      league,
      rows,
      expires:
        Date.now() + (marketComplete ? CORPUS_TTL_MS : CORPUS_RETRY_TTL_MS),
      version: corpusVersion,
    };
  })()
    .then((snapshot) => {
      corpusCache = snapshot;
      return snapshot;
    })
    .catch((error) => {
      if (corpusCache && corpusCache.league === league) {
        return corpusCache;
      }
      throw error;
    })
    .finally(() => {
      if (corpusInflight?.promise === promise) {
        corpusInflight = null;
      }
    });

  corpusInflight = { league, promise };
  return promise;
}

export async function scanCorpus(
  league: string,
): Promise<Record<string, ScanCorpusRow>> {
  const hit = freshCorpus(league);
  if (hit) return hit.rows;
  return (await startCorpusRefresh(league)).rows;
}

/**
 * Versioned corpus access: the version bumps only when the snapshot is
 * rebuilt, so clients holding the same version can skip the multi-megabyte
 * row transfer entirely.
 */
export async function scanCorpusVersioned(
  league: string,
): Promise<{ version: number; rows: Record<string, ScanCorpusRow> }> {
  const hit = freshCorpus(league);
  if (hit) return { version: hit.version, rows: hit.rows };
  const snapshot = await startCorpusRefresh(league);
  return { version: snapshot.version, rows: snapshot.rows };
}

export async function refreshScanCorpus(
  league: string,
): Promise<Record<string, ScanCorpusRow>> {
  return (await startCorpusRefresh(league, { forceMarketRefresh: true })).rows;
}

/** Test seam: clear the assembled scan-corpus cache. */
export function _clearCorpusCache(): void {
  corpusCache = null;
  corpusInflight = null;
}
