import { ParsedItem, ItemRarity, itemIsModifiable } from "@/parser";
import { ModifierType, StatCalculated } from "@/parser/modifiers";
import {
  FiltersCreationContext,
  calculatedStatToFilter,
  finalFilterTweaks,
} from "@/web/price-check/filters/create-stat-filters";
import { StatFilter } from "@/web/price-check/filters/interfaces";
import {
  filterItemProp,
  filterBasePercentile,
} from "@/web/price-check/filters/pseudo/item-property";

// Mirrors vendor initUiModFilters (create-stat-filters.ts:211) MINUS the
// filterPseudo(ctx) call. The pseudo pass irreversibly absorbs the source
// resist/attr/life explicit lines into pseudo.* totals (pseudo/index.ts) and
// drops the originals, but this project wants those lines displayed and queried
// as explicit mods. No explicit-only preset exists for finished items, and
// createExactStatFilters is unsuitable (≤2% clamp drops explicits on 5-mod
// rares), so we replicate the UI builder here instead of touching vendor.
//
// Keep this in lock-step with vendor create-stat-filters.ts:211 on upstream
// pulls: the ONLY intentional deviation is the removed filterPseudo line.
// enableAllFilters is inlined (vendor's copy is module-private at :865).
export function initExplicitModFilters(
  item: ParsedItem,
  opts: {
    searchStatRange: number;
    defaultAllSelected: boolean;
  },
): StatFilter[] {
  const ctx: FiltersCreationContext = {
    item,
    filters: [],
    searchInRange:
      item.rarity === ItemRarity.Normal ? 100 : opts.searchStatRange,
    statsByType: item.statsByType.map((calc) => {
      if (
        (calc.type === ModifierType.Fractured ||
          calc.type === ModifierType.Desecrated) &&
        calc.stat.trade.ids[ModifierType.Explicit]
      ) {
        return { ...calc, type: ModifierType.Explicit };
      } else {
        return calc;
      }
    }),
  };

  if (item.info.refName !== "Split Personality") {
    filterItemProp(ctx);
    // DEVIATION FROM VENDOR: filterPseudo(ctx) is intentionally omitted here.
    // (Vendor: `if (item.rarity !== ItemRarity.Unique || !getMaxSockets(item))
    // filterPseudo(ctx)`.) See header comment.
    if (item.info.refName === "Emperor's Vigilance") {
      filterBasePercentile(ctx);
    }
  }

  if (itemIsModifiable(item)) {
    ctx.statsByType = ctx.statsByType.filter(
      (mod) =>
        mod.type !== ModifierType.Fractured &&
        mod.type !== ModifierType.Desecrated,
    );
    ctx.statsByType.push(
      ...item.statsByType.filter(
        (mod) =>
          mod.type === ModifierType.Fractured ||
          mod.type === ModifierType.Desecrated,
      ),
    );
  }

  if (item.isVeiled) {
    ctx.statsByType = ctx.statsByType.filter(
      (mod) => mod.type !== ModifierType.Veiled,
    );
  }

  ctx.filters.push(
    ...ctx.statsByType.map((mod) =>
      calculatedStatToFilter(mod, ctx.searchInRange, item),
    ),
  );

  if (item.isVeiled) {
    ctx.filters.forEach((filter) => {
      filter.disabled = true;
    });
  }

  finalFilterTweaks(ctx);

  // DEVIATION FROM VENDOR: finalFilterTweaks marks EVERY non-property/-desecrated
  // affix on a map-category item hidden+disabled ("filters.hide_for_map",
  // create-stat-filters.ts:679) — a PoE1 assumption that maps are fungible by
  // tier. PoE2 waystone affixes (Item Rarity %, Pack Size %, Waystone Drop
  // Chance %, dangerous suffixes) are exactly what buyers filter on, and this
  // project's whole point is to keep explicit mods displayed and queryable
  // (same rationale as the omitted filterPseudo above). Un-hide them so they
  // surface as toggleable stats; the price.ts force-enable pass then enables
  // them by default like any other explicit. Genuine noise lines keep their
  // other hidden markers (hide_const_roll, hide_low_ilvl, ...).
  for (const filter of ctx.filters) {
    if (filter.hidden === "filters.hide_for_map") {
      filter.hidden = undefined;
      filter.disabled = false;
    }
    // Same reasoning for the waystone Revives property (vendor hides it as
    // rarely-traded-on): show it as a toggleable, default-off property stat
    // alongside Pack Size / Item Rarity / Drop Chance.
    if (filter.hidden === "filters.hide_revives") {
      filter.hidden = undefined;
    }
  }

  if (opts.defaultAllSelected) {
    enableAllFilters(ctx.filters);
  }

  return ctx.filters;
}

// Inlined from vendor create-stat-filters.ts:865 (module-private there).
function enableAllFilters(filters: StatFilter[]) {
  for (const filter of filters) {
    if (!filter.hidden) {
      filter.disabled = false;
    }
  }
}
