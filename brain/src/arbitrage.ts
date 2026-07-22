/**
 * Currency arbitrage analysis for Alt+S.
 *
 * Stage 1 (instant): the aggregate poe2scout exchange snapshot becomes the
 * exchange matrix and the item's anchor view. Stage 2 (refinement): official
 * exchange/trade queries verify the shown pairs live, flowing through the
 * rate-limit scheduler. Every row carries its data source and age so
 * freshness is always explicit.
 *
 * The absolute scale is per-item: its most-liquid quote pair (the market that
 * actually clears it), never a blind exalted default — some items simply do
 * not trade in exalts.
 */
import { parseClipboard } from "@/parser";
import { snapshotPairsRaw, SCOUT_TTL_MS } from "./poe2scout";
import { Scheduler, Http429, retryAfterMs } from "./scheduler";
import { selectMarketQuotes } from "./scout/quotes";
import { WAYSTONE_USER_AGENT, cookieAllowedForHost } from "./session-headers";

const TRADE_HOST = "https://www.pathofexile.com";
const BIG_THREE = ["exalted", "chaos", "divine"] as const;
const FLAG_THRESHOLD = 0.05;
const LISTING_FETCH_COUNT = 25;

export type ArbRow = {
  key: string;
  label: string;
  priceText: string;
  detail?: string;
  source: "aggregate" | "live";
  ageMs: number;
  flagged?: boolean;
  liquidity?: number;
  stock?: number;
};

export type PerCurrency = {
  currency: string;
  amount?: number;      // price of one item in that currency
  exaltedPrice: number; // exalted per unit in that market
  direct: boolean;      // a real pair exists (else derived conversion)
  volume?: number;
  stock?: number;
};

export type Verdict = {
  kind: "opportunity" | "none" | "insufficient";
  text: string;
  buyWith?: string;
  savingsPct?: number;
};

export type LiquidPair = {
  currency: string;
  price: number;         // units of anchor currency per item
  priceExalted: number;  // exalted per unit
  liquidity: number;
  stock: number;
};

export type ArbAnswer = {
  mode: "commodity" | "listings-pending" | "matrix-only" | "error";
  league: string;
  refreshId: number;
  itemName?: string;
  stackSize?: number;
  note?: string;
  matrix: ArbRow[];
  itemRows: ArbRow[];
  ratesAgeMs: number;
  verdict?: Verdict;
  liquidPair?: LiquidPair;
  perCurrency?: PerCurrency[];
  exaltedPrices?: Record<string, number>;
};

export type ArbState = {
  refreshId: number;
  done: boolean;
  matrix: ArbRow[];
  itemRows: ArbRow[];
  verdict?: Verdict;
  liquidPair?: LiquidPair;
  perCurrency?: PerCurrency[];
  exaltedPrices?: Record<string, number>;
  listings?: {
    currency: string;
    count: number;
    median: number;
    exaltedMedian: number;
    deltaVsBest: number;
    flagged: boolean;
  }[];
  listingsNote?: string;
};

// --- snapshot-derived helpers ---------------------------------------------

type PairSide = {
  ApiId?: string;
  Text?: string;
  CategoryApiId?: string;
};

type Pair = {
  BaseCurrencyApiId?: string;
  Volume?: number;
  CurrencyOne?: PairSide;
  CurrencyTwo?: PairSide;
  CurrencyOneData?: SideData;
  CurrencyTwoData?: SideData;
};

type SideData = {
  RelativePrice?: number;
  StockValue?: number;
  VolumeTraded?: number;
  HighestStock?: number;
};

function pairsOf(raw: unknown): Pair[] {
  return Array.isArray(raw) ? (raw as Pair[]) : [];
}

function normName(name: string): string {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

/** name -> { apiId, text, category } index over every item in the pairs feed. */
export function buildCommodityIndex(rawPairs: unknown) {
  const index = new Map<
    string,
    { apiId: string; text: string; category: string }
  >();
  for (const pair of pairsOf(rawPairs)) {
    for (const side of [pair.CurrencyOne, pair.CurrencyTwo]) {
      const apiId = side?.ApiId;
      const text = side?.Text;
      if (!apiId || !text) continue;
      index.set(normName(text), {
        apiId,
        text,
        category: side?.CategoryApiId ?? "",
      });
    }
  }
  return index;
}

/**
 * Exalted price per unit for every item, selected by the same rule the price
 * warm uses: executable pairs first (buyer stock > 0), then highest
 * Exalted-equivalent traded volume. Selecting any other way lets stale or
 * junk low-volume pairs set headline rates (a real divine↔vaal window once
 * read 237 ex while every liquid pair read 1192 ex).
 */
export function exaltedPrices(rawPairs: unknown): Map<string, number> {
  const out = new Map<string, number>();
  for (const [apiId, quote] of selectMarketQuotes(rawPairs)) {
    out.set(apiId, quote.price);
  }
  return out;
}

function fmt(value: number): string {
  if (value >= 1000) return value.toLocaleString("en-US", { maximumFractionDigits: 0 });
  if (value >= 10) return value.toLocaleString("en-US", { maximumFractionDigits: 1 });
  return value.toLocaleString("en-US", { maximumFractionDigits: 2 });
}

const CURRENCY_CATEGORY = "currency";
const CURRENCY_NAMES: Record<string, string> = {
  exalted: "Exalted Orb",
  chaos: "Chaos Orb",
  divine: "Divine Orb",
};

/**
 * The bottom exchange matrix: REAL currency items only (the 'currency'
 * category — never omens, idols, or lineage gems that pollute the raw
 * liquidity sort). Big three first in fixed order, then the rest of the
 * category by liquidity, capped so it always fits without scrolling.
 */
function currencyMatrix(rawPairs: unknown, ageMs: number, limit = 8): ArbRow[] {
  const prices = exaltedPrices(rawPairs);
  const byId = new Map<
    string,
    { text: string; price: number; liquidity: number; stock: number }
  >();
  for (const pair of pairsOf(rawPairs)) {
    for (const [side, data] of [
      [pair.CurrencyOne, pair.CurrencyOneData],
      [pair.CurrencyTwo, pair.CurrencyTwoData],
    ] as const) {
      const apiId = side?.ApiId;
      if (!apiId || side?.CategoryApiId !== CURRENCY_CATEGORY) continue;
      const price = prices.get(apiId);
      if (price === undefined) continue;
      const liquidity = pair.Volume ?? 0;
      const current = byId.get(apiId);
      if (!current || liquidity > current.liquidity) {
        byId.set(apiId, {
          text: side?.Text ?? CURRENCY_NAMES[apiId] ?? apiId,
          price,
          liquidity,
          stock: data?.HighestStock ?? 0,
        });
      }
    }
  }
  const rest = [...byId.entries()]
    .filter(([apiId]) => !(BIG_THREE as readonly string[]).includes(apiId))
    .sort((a, b) => b[1].liquidity - a[1].liquidity);
  const rows: ArbRow[] = [];
  const exPrice = 1;
  const chaos = prices.get("chaos");
  const divine = prices.get("divine");
  // Big three keep pair:* keys so stage-2 refinement patches them in place.
  rows.push({
    key: "pair:exalted",
    label: "Exalted Orb",
    priceText: "1 ex",
    detail: "the market base unit",
    source: "aggregate",
    ageMs,
  });
  if (chaos !== undefined) {
    rows.push({
      key: "pair:chaos",
      label: "Chaos Orb",
      priceText: `${fmt(chaos)} ex`,
      detail: "per orb",
      source: "aggregate",
      ageMs,
    });
  }
  if (divine !== undefined) {
    rows.push({
      key: "pair:divine",
      label: "Divine Orb",
      priceText: `${fmt(divine)} ex`,
      detail: "per orb",
      source: "aggregate",
      ageMs,
    });
  }
  if (chaos !== undefined && divine !== undefined && rows.length < limit) {
    rows.push({
      key: "pair:divine:chaos",
      label: "Divine ↔ Chaos",
      priceText: `1 div = ${fmt(divine / chaos)} chaos`,
      detail: "cross rate",
      source: "aggregate",
      ageMs,
    });
  }
  for (const [apiId, info] of rest) {
    rows.push({
      key: `cur:${apiId}`,
      label: info.text,
      priceText: `${fmt(info.price)} ex`,
      detail: `vol ${fmt(info.liquidity)} · stock ${fmt(info.stock)}`,
      source: "aggregate",
      ageMs,
      liquidity: info.liquidity,
      stock: info.stock,
    });
    if (rows.length >= limit) break;
  }
  return rows;
}

// --- official GGG requests (through the scheduler) -------------------------

function gggFetch(
  path: string,
  init: RequestInit,
  sessionId: string,
): Promise<Response> {
  return (async () => {
    const url = `${TRADE_HOST}${path}`;
    const headers: Record<string, string> = {
      "user-agent": WAYSTONE_USER_AGENT,
      "accept": "application/json",
      "content-type": "application/json",
      ...(init.headers as Record<string, string> | undefined),
    };
    if (sessionId && cookieAllowedForHost(new URL(url).hostname)) {
      headers["cookie"] = `POESESSID=${sessionId}`;
    }
    const r = await fetch(url, { ...init, headers });
    if (r.status === 429) {
      throw new Http429(retryAfterMs(r.headers) ?? 30_000);
    }
    return r;
  })();
}

type ExchangeOffer = {
  listing: {
    offers: { exchange: { amount: number; currency: string }; item: { amount: number; stock: number } }[];
    indexed: string;
  };
};

/** Best ask for one unit of `wantApiId` payable in `haveApiId`, live. */
async function exchangeBest(
  league: string,
  wantApiId: string,
  haveApiId: string,
  sessionId: string,
): Promise<{ price: number; stock: number } | null> {
  const body = {
    engine: "new",
    query: {
      status: { option: "online" },
      have: [haveApiId],
      want: [wantApiId],
    },
    sort: { have: "asc" },
  };
  const r = await gggFetch(
    `/api/trade2/exchange/poe2/${encodeURIComponent(league)}`,
    { method: "POST", body: JSON.stringify(body) },
    sessionId,
  );
  if (!r.ok) return null;
  const data = (await r.json()) as { result?: Record<string, ExchangeOffer> };
  let best: { price: number; stock: number } | null = null;
  for (const offer of Object.values(data.result ?? {})) {
    const first = offer.listing.offers[0];
    if (!first || first.exchange.currency !== haveApiId) continue;
    const perUnit = first.exchange.amount / first.item.amount;
    if (!Number.isFinite(perUnit) || perUnit <= 0) continue;
    if (!best || perUnit < best.price) best = { price: perUnit, stock: first.item.stock };
  }
  return best;
}

// --- the engine ------------------------------------------------------------

let scheduler = new Scheduler();
let refreshSeq = 0;
const states = new Map<number, ArbState & { league: string; createdAt: number }>();
const MAX_STATES = 4;

/** Test seam: inject a scheduler with a fake clock/intervals. */
export function _setScheduler(next: Scheduler): void {
  scheduler = next;
}

/** Test seam: drop all refinement state. */
export function _clearArbStates(): void {
  states.clear();
  refreshSeq = 0;
}

function storeState(state: ArbState & { league: string; createdAt: number }): void {
  states.set(state.refreshId, state);
  if (states.size > MAX_STATES) {
    const oldest = Math.min(...states.keys());
    states.delete(oldest);
  }
}

export function arbState(refreshId: number): ArbState | null {
  const state = states.get(refreshId);
  if (!state) return null;
  const { league: _league, createdAt: _createdAt, ...rest } = state;
  return rest;
}

function patchMatrix(
  state: ArbState & { league: string; createdAt: number },
  key: string,
  patch: Partial<ArbRow>,
): void {
  const row = state.matrix.find((r) => r.key === key);
  if (row) Object.assign(row, patch, { source: "live" as const, ageMs: 0 });
}

async function refineBigThree(
  state: ArbState & { league: string; createdAt: number },
  sessionId: string,
): Promise<void> {
  const jobs: { key: string; want: string; have: string; priority: number }[] = [
    { key: "pair:divine", want: "divine", have: "exalted", priority: 10 },
    { key: "pair:chaos", want: "chaos", have: "exalted", priority: 11 },
    { key: "pair:divine:chaos", want: "divine", have: "chaos", priority: 12 },
  ];
  await Promise.all(
    jobs.map((job) =>
      scheduler
        .schedule(`xchg:${state.league}:${job.want}:${job.have}`, job.priority, "ggg", () =>
          exchangeBest(state.league, job.want, job.have, sessionId),
        )
        .then((best) => {
          if (!best) return;
          if (job.key === "pair:divine:chaos") {
            const chaosRow = state.matrix.find((r) => r.key === "pair:chaos");
            const divRow = state.matrix.find((r) => r.key === "pair:divine");
            if (divRow && chaosRow) {
              const divEx = Number(divRow.priceText.replace(/[^\d.]/g, ""));
              const chaosPerDiv = best.price;
              if (divEx > 0 && chaosPerDiv > 0) {
                patchMatrix(state, job.key, {
                  priceText: `1 div = ${fmt(chaosPerDiv)} chaos`,
                  detail: "cross rate (live)",
                });
              }
            }
            return;
          }
          patchMatrix(state, job.key, {
            priceText: `${fmt(best.price)} ex`,
            detail: `per orb (live, stock ${fmt(best.stock)})`,
            stock: best.stock,
          });
        })
        .catch(() => {}),
    ),
  );
}

async function refineCommodity(
  state: ArbState & { league: string; createdAt: number },
  apiId: string,
  sessionId: string,
): Promise<void> {
  const results = await Promise.all(
    BIG_THREE.map((have, i) =>
      scheduler
        .schedule(`xchg:${state.league}:${apiId}:${have}`, 5 + i, "ggg", () =>
          exchangeBest(state.league, apiId, have, sessionId),
        )
        .catch(() => null),
    ),
  );
  for (let i = 0; i < BIG_THREE.length; i++) {
    const best = results[i];
    if (!best) continue;
    const have = BIG_THREE[i];
    const row = state.itemRows.find((r) => r.key === `item:${apiId}:${have}`);
    if (row) {
      Object.assign(row, {
        priceText: `${fmt(best.price)} ${have}`,
        detail: `live best ask, stock ${fmt(best.stock)}`,
        source: "live" as const,
        ageMs: 0,
        stock: best.stock,
      });
    }
  }
}

type Listing = { amount: number; currency: string };

async function fetchListings(
  league: string,
  name: string,
  sessionId: string,
): Promise<Listing[]> {
  const searchBody = {
    query: { name, status: { option: "online" } },
    sort: { price: "asc" },
  };
  const searchRes = await scheduler.schedule(
    `search:${league}:${name}`,
    1,
    "ggg",
    () =>
      gggFetch(
        `/api/trade2/search/poe2/${encodeURIComponent(league)}`,
        { method: "POST", body: JSON.stringify(searchBody) },
        sessionId,
      ),
  );
  if (!searchRes.ok) throw new Error(`trade search failed (${searchRes.status})`);
  const searchData = (await searchRes.json()) as { id?: string; result?: string[] };
  const ids = (searchData.result ?? []).slice(0, LISTING_FETCH_COUNT);
  if (!searchData.id || ids.length === 0) return [];
  const fetchRes = await scheduler.schedule(
    `fetch:${searchData.id}:${ids.join(",")}`,
    2,
    "ggg",
    () =>
      gggFetch(
        `/api/trade2/fetch/${ids.join(",")}?query=${searchData.id}`,
        { method: "GET" },
        sessionId,
      ),
  );
  if (!fetchRes.ok) throw new Error(`trade fetch failed (${fetchRes.status})`);
  const fetchData = (await fetchRes.json()) as {
    result?: { listing?: { price?: { amount?: number; currency?: string } } }[];
  };
  const out: Listing[] = [];
  for (const entry of fetchData.result ?? []) {
    const price = entry.listing?.price;
    if (
      price &&
      typeof price.amount === "number" &&
      price.amount > 0 &&
      typeof price.currency === "string"
    ) {
      out.push({ amount: price.amount, currency: price.currency });
    }
  }
  return out;
}

function median(values: number[]): number {
  if (!values.length) return 0;
  const sorted = [...values].sort((a, b) => a - b);
  const mid = Math.floor(sorted.length / 2);
  return sorted.length % 2 ? sorted[mid] : (sorted[mid - 1] + sorted[mid]) / 2;
}

async function refineListings(
  state: ArbState & { league: string; createdAt: number },
  name: string,
  sessionId: string,
  rawPairs: unknown,
): Promise<void> {
  try {
    const listings = await fetchListings(state.league, name, sessionId);
    const prices = exaltedPrices(rawPairs);
    prices.set("exalted", 1);
    const groups = new Map<string, number[]>();
    for (const listing of listings) {
      if (!prices.has(listing.currency)) continue;
      groups.set(listing.currency, [
        ...(groups.get(listing.currency) ?? []),
        listing.amount,
      ]);
    }
    if (groups.size === 0) {
      state.listingsNote = listings.length
        ? `${listings.length} listings, none in convertible currencies`
        : "no online listings right now";
      return;
    }
    const stats = [...groups.entries()].map(([currency, amounts]) => {
      const med = median(amounts);
      return {
        currency,
        count: amounts.length,
        median: med,
        exaltedMedian: med * (prices.get(currency) ?? 0),
        deltaVsBest: 0,
        flagged: false,
      };
    });
    const best = Math.min(...stats.map((s) => s.exaltedMedian));
    for (const s of stats) {
      s.deltaVsBest = best > 0 ? (s.exaltedMedian - best) / best : 0;
      s.flagged = best > 0 && s.deltaVsBest >= FLAG_THRESHOLD;
    }
    stats.sort((a, b) => a.exaltedMedian - b.exaltedMedian);
    state.listings = stats;
    // Same rule as commodities: a >=5% spread between ask currencies is a
    // real buy-with signal.
    if (stats.length >= 2 && best > 0) {
      const cheapest = stats[0];
      const next = stats[1];
      const savings = (next.exaltedMedian - cheapest.exaltedMedian) / next.exaltedMedian;
      state.verdict =
        savings >= FLAG_THRESHOLD
          ? {
              kind: "opportunity",
              text: `buy with ${cheapest.currency} — ${(savings * 100).toFixed(1)}% cheaper than ${next.currency}`,
              buyWith: cheapest.currency,
              savingsPct: savings * 100,
            }
          : {
              kind: "none",
              text: `no arb — listings within ${(savings * 100).toFixed(1)}%`,
            };
    }
  } catch (e) {
    state.listingsNote = `listings unavailable: ${e instanceof Error ? e.message : e}`;
  }
}

export async function arbQuote(options: {
  clipboard: string;
  league: string;
  accountName?: string;
  sessionId?: string;
}): Promise<ArbAnswer> {
  const league = options.league;
  const sessionId = options.sessionId ?? "";
  // Stage 1 is poe2scout-aggregate and LIGHT: just the exchange pairs feed
  // (no full icon warm), forced on every press and budgeted through the
  // scout lane for politeness.
  const rawPairs = await scheduler
    .schedule(`pairs:${league}`, 1, "scout", () =>
      snapshotPairsRaw(league, { force: true }),
    )
    .catch(() => []);
  const ratesAgeMs = 0;

  refreshSeq += 1;
  const refreshId = refreshSeq;
  const state: ArbState & { league: string; createdAt: number } = {
    refreshId,
    league,
    createdAt: Date.now(),
    done: false,
    matrix: currencyMatrix(rawPairs, ratesAgeMs),
    itemRows: [],
  };
  storeState(state);

  // The vendored parser is not total: some inputs throw instead of returning
  // an error Result. A bad clipboard must degrade to the matrix, never kill
  // the request.
  let itemName = "";
  try {
    const parsed = options.clipboard
      ? parseClipboard(options.clipboard)
      : null;
    if (parsed?.isOk()) itemName = parsed._unsafeUnwrap().info.refName;
  } catch {
    itemName = "";
  }

  const finish = (answer: ArbAnswer) => {
    void (async () => {
      try {
        await refineBigThree(state, sessionId);
        if (answer.mode === "commodity" && answer.itemName) {
          const index = buildCommodityIndex(rawPairs);
          const hit = index.get(normName(answer.itemName));
          if (hit) await refineCommodity(state, hit.apiId, sessionId);
        }
        if (answer.mode === "listings-pending" && answer.itemName) {
          await refineListings(state, answer.itemName, sessionId, rawPairs);
        }
      } finally {
        state.done = true;
      }
    })();
    return answer;
  };

  if (!itemName) {
    return finish({
      mode: options.clipboard ? "error" : "matrix-only",
      league,
      refreshId,
      note: options.clipboard
        ? "could not parse the hovered item — showing the exchange matrix"
        : "no item hovered — showing the exchange matrix",
      matrix: state.matrix,
      itemRows: [],
      ratesAgeMs,
    });
  }

  const index = buildCommodityIndex(rawPairs);
  const hit = index.get(normName(itemName));
  if (hit) {
    const prices = exaltedPrices(rawPairs);
    const quote = selectMarketQuotes(rawPairs).get(hit.apiId);
    const exPrice = prices.get(hit.apiId) ?? 0;

    // Per-major-currency view: a DIRECT pair price when the item actually
    // trades in that currency, otherwise a derived conversion (marked, so
    // the verdict never treats math as a market).
    const perCurrency: PerCurrency[] = BIG_THREE.map((have) => {
      const haveEx = have === "exalted" ? 1 : prices.get(have);
      let directPair: Pair | undefined;
      let itemSide: SideData | undefined;
      for (const pair of pairsOf(rawPairs)) {
        const sides = [
          [pair.CurrencyOne, pair.CurrencyOneData, pair.CurrencyTwo],
          [pair.CurrencyTwo, pair.CurrencyTwoData, pair.CurrencyOne],
        ] as const;
        for (const [side, data, other] of sides) {
          if (side?.ApiId === hit.apiId && other?.ApiId === have) {
            directPair = pair;
            itemSide = data;
          }
        }
      }
      const pairPrice = itemSide?.RelativePrice ?? exPrice;
      return {
        currency: have,
        amount:
          haveEx && pairPrice > 0 ? pairPrice / haveEx : undefined,
        exaltedPrice: pairPrice,
        direct: directPair !== undefined,
        volume: directPair?.Volume,
        stock: itemSide?.HighestStock,
      };
    });

    // Verdict over DIRECT pairs only: a derived conversion is not a market.
    const directRows = perCurrency.filter(
      (row) => row.direct && row.exaltedPrice > 0,
    );
    let verdict: Verdict;
    if (directRows.length >= 2) {
      const sorted = [...directRows].sort(
        (a, b) => a.exaltedPrice - b.exaltedPrice,
      );
      const cheapest = sorted[0];
      const next = sorted[1];
      const savings =
        (next.exaltedPrice - cheapest.exaltedPrice) / next.exaltedPrice;
      verdict =
        savings >= FLAG_THRESHOLD
          ? {
              kind: "opportunity",
              text: `buy with ${cheapest.currency} — ${(savings * 100).toFixed(1)}% cheaper than ${next.currency}`,
              buyWith: cheapest.currency,
              savingsPct: savings * 100,
            }
          : {
              kind: "none",
              text: `no arb — direct pairs within ${(savings * 100).toFixed(1)}%`,
            };
    } else if (directRows.length === 1) {
      verdict = {
        kind: "insufficient",
        text: `trades only in ${directRows[0].currency} — no cross-pair spread`,
      };
    } else {
      verdict = { kind: "insufficient", text: "no direct pairs found" };
    }

    const liquidPair: LiquidPair | undefined = quote
      ? {
          currency: quote.currency,
          price: quote.amount,
          priceExalted: quote.price,
          liquidity: quote.liquidity,
          stock: quote.buyerStock,
        }
      : undefined;

    const rows: ArbRow[] = perCurrency.map((row) => ({
      key: `item:${hit.apiId}:${row.currency}`,
      label: `1 ${hit.text} in ${row.currency}`,
      priceText:
        row.amount !== undefined ? `${fmt(row.amount)} ${row.currency}` : "—",
      detail: row.direct ? "direct pair" : "derived",
      source: "aggregate" as const,
      ageMs: ratesAgeMs,
      liquidity: row.volume,
      stock: row.stock,
    }));
    state.itemRows = rows;
    state.verdict = verdict;
    state.liquidPair = liquidPair;
    state.perCurrency = perCurrency;
    state.exaltedPrices = Object.fromEntries(
      BIG_THREE.map((have) => [
        have,
        have === "exalted" ? 1 : (prices.get(have) ?? 0),
      ]),
    );
    return finish({
      mode: "commodity",
      league,
      refreshId,
      itemName: hit.text,
      matrix: state.matrix,
      itemRows: rows,
      ratesAgeMs,
      verdict,
      liquidPair,
      perCurrency,
      exaltedPrices: state.exaltedPrices,
    });
  }

  // Not a bulk commodity: treat as a listed item (unique/equipment) and
  // normalize live listings per ask currency in stage 2.
  return finish({
    mode: "listings-pending",
    league,
    refreshId,
    itemName,
    note: "fetching live listings…",
    matrix: state.matrix,
    itemRows: [],
    ratesAgeMs,
  });
}

export { SCOUT_TTL_MS };
