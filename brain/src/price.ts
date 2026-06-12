import { setBrainConfig } from "./stubs/Config";
import { PRICE_CHECK_DEFAULTS } from "./stubs/widgets";
import { buildItemCard, type ItemCard } from "./item-card";
import { resolveIcon, resolveCurrencyIcon } from "./icons";
import { isCurrency, currencyCheck } from "./currency";

// The trade-query options EE2's UI feeds into createPresets/createFilters/
// the stat-filter builders. We pull the user-facing knobs from the same
// PriceCheckWidget defaults the renderer uses, so query shape matches the
// real site.
function tradeOpts(league: string) {
  return {
    league,
    currency: undefined as string | undefined,
    // trade2 status filter. EE2's upstream default is "securable" (instant
    // buyout only), which returns ZERO results for items nobody lists with
    // buyout — verified 2026-06-11: Panther Idol had 0 securable vs 354
    // online listings. "online" matches the trade site's default UI status.
    listingType: "online" as const,
    collapseListings: PRICE_CHECK_DEFAULTS.collapseListings ?? "api",
    activateStockFilter: PRICE_CHECK_DEFAULTS.activateStockFilter ?? false,
    searchStatRange: PRICE_CHECK_DEFAULTS.searchStatRange ?? 10,
    useEn: true,
    defaultAllSelected: PRICE_CHECK_DEFAULTS.defaultAllSelected ?? false,
    autoFillEmptyAugmentSockets:
      PRICE_CHECK_DEFAULTS.autoFillEmptyRuneSockets ?? false,
  };
}

/**
 * ParsedItem -> trade2 search body (the JSON POSTed to /api/trade2/search).
 * Mirrors EE2's pipeline: parseClipboard -> createPresets (createFilters +
 * stat filters) -> createTradeRequest. No network calls.
 */
async function parseItem(clipboard: string) {
  const { parseClipboard } = await import("@/parser");
  const r = parseClipboard(clipboard);
  if (r.isErr()) throw new Error(`not an item: ${r.error}`);
  return r.value;
}

/** clipboard -> just the trade2 query (thin wrapper; existing callers/tests). */
export async function buildQuery(clipboard: string, league = "Standard") {
  return (await buildQueryAndStats(clipboard, league)).query;
}

/**
 * clipboard -> { query, preset, stats }. Parses once, builds presets once,
 * force-enables every parsed stat at the -10% default (iteration 3 decision)
 * EXCEPT rune/socketable lines (craftable onto any base, not part of the
 * item's value), then builds the trade request. `stats` is the searched-stat
 * list iteration 4 will toggle/requery against.
 */
export type StatOverride = {
  i: number;
  enabled?: boolean;
  min?: number | null;
};

/**
 * Prop override: toggles/edits an item-property FilterNumeric on
 * preset.filters (quality, itemLevel, armour-socket family, ...) keyed by its
 * ItemFilters member name. Discriminated from StatOverride by the `p` field.
 */
export type PropOverride = {
  p: string;
  enabled?: boolean;
  min?: number | null;
};

export type Override = StatOverride | PropOverride;

// The ItemFilters numeric (FilterNumeric {value, max?, disabled}) members we
// surface as toggleable/editable prop rows, with display labels. Armour /
// evasion / energy-shield / ward / block are NOT ItemFilters members — EE2
// routes those through the stat-filter pipeline (item.armour, item.evasion_rating,
// ...), so they appear in `stats`, not here. Only members actually present on a
// given item's preset.filters are emitted.
const PROP_CANDIDATES: ReadonlyArray<readonly [string, string]> = [
  ["quality", "Quality"],
  ["itemLevel", "Item Level"],
  ["socketNumber", "Sockets"],
  ["linkedSockets", "Linked Sockets"],
  ["augmentSockets", "Rune Sockets"],
  ["gemLevel", "Gem Level"],
  ["mapTier", "Map Tier"],
  ["areaLevel", "Area Level"],
];

type PropRow = {
  key: string;
  text: string;
  value: number | null;
  min: number | null;
  enabled: boolean;
};

/**
 * Apply caller overrides to a preset, in place. Discriminated by field: `{i}` =
 * stat override keyed by the visible-list index (a stale index from an older
 * panel is silently ignored); `{p}` = prop override keyed by the ItemFilters
 * member name, mutating the FilterNumeric in place. For FilterNumeric the
 * searched min IS `.value` (createTradeRequest emits `<filter>.min = .value`
 * gated on `!disabled`), so an override min writes `.value`.
 */
function applyOverrides(preset: any, visible: any[], overrides: Override[]) {
  for (const o of overrides) {
    if ("p" in o) {
      const f = (preset.filters as any)[o.p];
      if (!f) continue;
      // `corrupted` is not a FilterNumeric ({value,disabled}); its shape is
      // {value:boolean, exact?:boolean} and createTradeRequest gates the
      // misc_filters corrupted.option on `value === false || exact`. There is
      // no `disabled` and the value isn't a searched min, so the toggle flips
      // `exact`: enabled -> require the item's corruption state exactly
      // (exact:true); disabled -> leave it unconstrained (exact:false).
      if (o.p === "corrupted") {
        if (o.enabled !== undefined) f.exact = o.enabled;
        continue;
      }
      if (o.enabled !== undefined) f.disabled = !o.enabled;
      if (o.min !== undefined && o.min !== null) f.value = o.min;
    } else {
      const stat = visible[o.i];
      if (!stat) continue;
      if (o.enabled !== undefined) stat.disabled = !o.enabled;
      if (o.min !== undefined && stat.roll) stat.roll.min = o.min ?? "";
    }
  }
}

/**
 * Item-property filter rows: the FilterNumeric members present on this item's
 * preset.filters, in candidate-table order. `min` == `.value` (the searched
 * floor), `enabled` == `!disabled`.
 */
function collectProps(preset: any): PropRow[] {
  const props: PropRow[] = PROP_CANDIDATES.flatMap(([key, text]) => {
    const f = (preset.filters as any)[key] as
      | { value: number; disabled: boolean }
      | undefined;
    if (!f) return [];
    return [{ key, text, value: f.value, min: f.value, enabled: !f.disabled }];
  });

  // Corrupted is a toggle-only prop (no searched min -> value/min null, so poed
  // renders no entry box). It only appears when createPresets attached the
  // corrupted filter (gear/jewels, not maps/currency). `enabled` reflects
  // whether the filter currently constrains the query: createTradeRequest emits
  // the misc_filters corrupted.option iff `value === false || exact` (a
  // non-corrupted item is always constrained to non-corrupted; a corrupted item
  // is only constrained when the user toggles exact on).
  const corrupted = (preset.filters as any).corrupted as
    | { value: boolean; exact?: boolean }
    | undefined;
  if (corrupted) {
    props.push({
      key: "corrupted",
      text: "Corrupted",
      value: null,
      min: null,
      enabled: corrupted.value === false || corrupted.exact === true,
    });
  }

  return props;
}

export async function buildQueryAndStats(
  clipboard: string,
  league: string,
  overrides: Override[] = [],
) {
  return buildQueryAndStatsFromItem(await parseItem(clipboard), league, overrides);
}

async function buildQueryAndStatsFromItem(
  item: Awaited<ReturnType<typeof parseItem>>,
  league: string,
  overrides: Override[] = [],
) {
  const { createPresets } = await import(
    "@/web/price-check/filters/create-presets"
  );
  const { createTradeRequest } = await import(
    "@/web/price-check/trade/pathofexile-trade"
  );

  const opts = tradeOpts(league);
  const { presets, active } = createPresets(item, opts);
  const preset = presets.find((p) => p.id === active) ?? presets[0];

  // Map / waystone search scoping (issue #1). EE2's map flow sets BOTH an exact
  // base (filters.searchExact.baseType, e.g. "Waystone (Tier 15)" — a real,
  // trade-indexed base whose name encodes the tier) AND a relaxed category
  // search (filters.searchRelaxed { category: Map, disabled: false }).
  // createTradeRequest prefers searchRelaxed whenever it's enabled, so the
  // emitted query carried only category=map and NO base type — matching every
  // map of any tier (the "wrong/unrelated listings" symptom). Disable the
  // relaxed search so the exact tiered base type drives the query. The separate
  // mapTier filter is then redundant (the base type already pins the tier) and
  // parses to an empty FilterNumeric here anyway (PoE2 puts the tier in the
  // base-type line, not a "Waystone Tier:" section EE2 expects), so drop it to
  // avoid a valueless "Map Tier" prop row.
  //
  // Scope strictly to map-category items: rare GEAR also carries a relaxed
  // category search ("armour.chest", ...), and there searching by category is
  // the intended behaviour (find every chest with these mods, not just the
  // exact base), so we must NOT touch it.
  const { ItemCategory } = await import("@/parser/meta");
  const f = preset.filters as any;
  if (
    item.category === ItemCategory.Map &&
    f.searchRelaxed &&
    f.searchExact?.baseType
  ) {
    f.searchRelaxed.disabled = true;
    if (f.mapTier && f.mapTier.value == null) delete f.mapTier;
  }

  // We keep createPresets' `preset.filters` (ItemFilters: quality, sockets,
  // category, rarity, corrupted, trade status, etc.) but REPLACE its stat list.
  // createPresets runs EE2's filterPseudo pass, which absorbs resist/attr/life
  // explicit lines into pseudo.* totals and drops the source explicits — this
  // project wants those displayed and queried as explicit mods, so we rebuild
  // the stats via initExplicitModFilters (a filterPseudo-free mirror of EE2's
  // initUiModFilters) using the same opts createPresets received.
  const { initExplicitModFilters } = await import("./stat-filters");
  const statFilters: typeof preset.stats = initExplicitModFilters(item, {
    searchStatRange: opts.searchStatRange,
    defaultAllSelected: opts.defaultAllSelected,
  });

  // User decision (iteration 3): search with every parsed stat enabled at the
  // -10% default instead of EE2's selective defaults. Skip these categories:
  //   - rune/added-rune: craftable onto any base, not part of the item's value.
  //   - property: base defences (evasion, armour, energy shield) and runic ward
  //     are item-base stats, not rolled affixes. User wants affix mods only by
  //     default; property stats are toggleable but start off (iteration 6).
  //   - hidden stats: EE2 sets `hidden` on const-roll/low-ilvl/placeholder
  //     lines (hide_const_roll, hide_empty_mod, hide_ele_res, hide_low_ilvl,
  //     hide_crafted_chaos, etc.) — enabling these over-constrains the query.
  // Mutate disabled BEFORE createTradeRequest so the query actually includes them.
  for (const stat of statFilters) {
    if (stat.tag === "rune" || stat.tag === "added-rune" || stat.hidden) continue;
    if (stat.tag === "property") {
      // Property stats (defences, ward) may arrive with disabled:false from EE2
      // (e.g. single-attr armours). Force them back off so the default is
      // affix-mods-only (they are toggleable in the UI — iteration 6 decision).
      stat.disabled = true;
      continue;
    }
    stat.disabled = false;
  }

  // Hidden stats (EE2 noise lines) are filtered out of the returned list
  // entirely — they add no value to the UI. The index into this visible list
  // is the stable `id` the panel toggles/requeries against (statRef is NOT
  // unique within the stat list, so an index is the only safe key).
  const visible = statFilters.filter((s) => !s.hidden);

  // Apply caller overrides AFTER the force-enable defaults and BEFORE
  // createTradeRequest.
  applyOverrides(preset, visible, overrides);

  // Item-property filter rows, built after overrides so they reflect them.
  const props = collectProps(preset);

  const query = createTradeRequest(preset.filters, statFilters, item);

  const stats = visible.map((s, i) => ({
    id: i, // array index — stable key for iteration-4 toggle requery
    text: s.text,
    value: s.roll?.value ?? null,
    min: typeof s.roll?.min === "number" ? s.roll.min : null,
    max: typeof s.roll?.max === "number" ? s.roll.max : null,
    enabled: !s.disabled,
    tag: s.tag,
  }));

  return { query, preset, stats, props };
}

async function cardFromItem(
  item: Awaited<ReturnType<typeof parseItem>>,
): Promise<Omit<ItemCard, "iconUrl"> & { iconPath: string | null }> {
  const { iconUrl, ...rest } = buildItemCard(item);
  return { ...rest, iconPath: await resolveIcon(iconUrl) };
}

/** clipboard -> card with iconUrl swapped for a local iconPath. */
export async function buildCard(clipboard: string) {
  return cardFromItem(await parseItem(clipboard));
}

/**
 * Full price check: build the query, hit the live trade2 search + fetch
 * endpoints (rate-limited inside the vendored client), return total + the
 * first page of listings.
 */
export async function priceCheck(
  clipboard: string,
  league: string,
  overrides: Override[] = [],
  onProgress?: (stage: string) => void,
) {
  setBrainConfig({ league, leagueId: league });

  const { AppConfig } = await import("./stubs/Config");
  const { requestTradeResultList, requestResults } = await import(
    "@/web/price-check/trade/pathofexile-trade"
  );

  const item = await parseItem(clipboard);
  if (isCurrency(item)) {
    return currencyCheck(item, league, onProgress);
  }

  // Safeguard (2026-06-11 spec): any tradeTagged item is exchange-listed
  // (tradeTag mirrors trade2 exchange static), so try the currency view —
  // poe2scout first, exchange book fallback inside currencyCheck — before
  // gear listings, which price fungible items poorly. A dry exchange book
  // (zero rates) falls through to listings. Overrides mean the user is
  // editing listing filters: skip the currency route so a dry-book item
  // doesn't re-pay the exchange probe on every requery.
  if (!overrides.length && item.info.tradeTag) {
    const cur = await currencyCheck(item, league, onProgress);
    if (cur.rates.length) return cur;
  }

  const { query, stats, props } = await buildQueryAndStatsFromItem(item, league, overrides);
  onProgress?.("listings");
  const list = await requestTradeResultList(query, league);

  const ids = list.result.slice(0, 10);
  const rawListings = ids.length
    ? await requestResults(list.id, ids, {
        accountName: AppConfig().accountName,
      })
    : [];

  // Icons and the hover card are decoration; listings are the product. Any
  // icon/card failure degrades that part to null instead of failing the
  // whole response (one bad CDN fetch must not blank the price check).
  const [card, listings] = await Promise.all([
    cardFromItem(item).catch(() => null),
    Promise.all(
      rawListings.map(async (l) => {
        // Resolve the payment-currency icon AND the seller's own item icon in
        // the same pass. resolveIcon dedups + disk-caches, so repeated bases
        // across listings are cheap. poed can't fetch remote URLs, so the hover
        // card relies on this brain-cached iconPath.
        const [currencyIconPath, iconPath] = await Promise.all([
          resolveCurrencyIcon(l.priceCurrency).catch(() => null),
          l.displayItem?.icon?.url
            ? resolveIcon(l.displayItem.icon.url).catch(() => null)
            : null,
        ]);
        return {
          ...l,
          currencyIconPath,
          // Shallow-copy the vendored displayItem so we don't mutate it in
          // place; pass it through unchanged when absent.
          displayItem: l.displayItem ? { ...l.displayItem, iconPath } : l.displayItem,
        };
      }),
    ),
  ]);

  return { kind: "price" as const, total: list.total, id: list.id, item: card, listings, stats, props };
}
