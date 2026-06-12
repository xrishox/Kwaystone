import { setBrainConfig } from "./stubs/Config";
import { PRICE_CHECK_DEFAULTS } from "./stubs/widgets";
import { resolveCurrencyIcon } from "./icons";
import type { ItemFilters } from "@/web/price-check/filters/interfaces";

// EE2's bulk endpoint trades currency-for-currency: you say which currencies
// you HAVE and which one you WANT, and it returns offers (exchange rates).
// `execBulkSearch` reads only two things off the ParsedItem/ItemFilters we pass:
//   - tradeTag(want)            -> the currency you want to receive
//   - filters.trade.listingType -> online/any status
//   - filters.trade.league      -> which league's exchange
//   - filters.stackSize         -> optional minimum stock (we leave it off)
// so we build a minimal-but-typed item + filters rather than running the full
// price-check filter pipeline (which is geared toward gear, not raw currency).

function bulkFilters(league: string): ItemFilters {
  return {
    searchExact: {},
    trade: {
      offline: false,
      onlineInLeague: false,
      // bulk rejects "securable"/"available"; "online" is the sane default.
      listingType: "online",
      listed: undefined,
      currency: undefined,
      league,
      collapseListings: PRICE_CHECK_DEFAULTS.collapseListings ?? "api",
    },
  };
}

/**
 * Resolve a currency trade tag (e.g. "exalted", "divine") to a real ParsedItem
 * via the vendored game data, so `tradeTag(item)` returns the right tag and the
 * exchange query targets a genuine base type.
 */
async function virtualCurrency(want: string) {
  const { TRADE_TAG_TO_REF, ITEM_BY_REF } = await import("@/assets/data");
  const { createVirtualItem } = await import("@/parser/ParsedItem");

  const refName = TRADE_TAG_TO_REF.get(want);
  if (!refName) {
    throw new Error(`unknown currency tag: "${want}"`);
  }
  const matches = ITEM_BY_REF("ITEM", refName);
  if (!matches || matches.length === 0) {
    throw new Error(`no base type for currency: "${want}" (${refName})`);
  }
  return createVirtualItem({ info: matches[0] });
}

/**
 * Bulk currency exchange. `have`/`want` are currency trade tags
 * ("exalted", "divine", "chaos", ...). Returns the offers for the want
 * currency: total listed and the cheapest exchange rates (rate-limited inside
 * the vendored client).
 */
export async function bulkSearch(have: string, want: string, league: string) {
  setBrainConfig({ league, leagueId: league });

  const { AppConfig } = await import("./stubs/Config");
  const { execBulkSearch } = await import(
    "@/web/price-check/trade/pathofexile-bulk"
  );

  const item = await virtualCurrency(want);
  const filters = bulkFilters(league);

  const results = await execBulkSearch(item, filters, [have], {
    accountName: AppConfig().accountName,
  });

  // execBulkSearch returns one entry per `have` tag (or null when the API
  // deferred that tag's results); we asked for exactly one `have`.
  const result = results[0];
  if (!result) {
    throw new Error(
      `no offers returned (none exist, or API deferred a large result set) for ${have} -> ${want} in "${league}"`,
    );
  }

  const [haveIconPath, wantIconPath] = await Promise.all([
    resolveCurrencyIcon(have),
    resolveCurrencyIcon(want),
  ]);

  return {
    have,
    want,
    league,
    queryId: result.queryId,
    total: result.total,
    offers: result.listed,
    haveIconPath,
    wantIconPath,
  };
}

export interface BulkSearchResult {
  have: string;
  want: string;
  league: string;
  queryId: string;
  total: number;
  offers: import("@/web/price-check/trade/pathofexile-bulk").BulkSearch["listed"];
  haveIconPath: string | null;
  wantIconPath: string | null;
}
