import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";
import { buildQuery } from "../src/price";

// bulkSearch is the network edge for the currency route — mock it so the
// priceCheck currency branch runs offline. Orientation: have=lookup item,
// want=payment, so each call resolves one payment-currency row.
vi.mock("../src/bulk", async (importOriginal) => {
  const mod = await importOriginal<typeof import("../src/bulk")>();
  return {
    ...mod,
    bulkSearch: vi.fn(async (have: string, want: string) => ({
      have, want, league: "L", queryId: "q", total: 5,
      offers: [
        { id: "a", relativeDate: "1m", exchangeAmount: 1, itemAmount: 85,
          stock: 10, isMine: false, accountName: "x", ign: "y",
          accountStatus: "online" },
      ],
      haveIconPath: null, wantIconPath: null,
    })),
  };
});

// poe2scout is the currency-view price source. priceCheck consults scoutPrice
// to decide whether a non-stackable-but-fungible item (gems) routes to the
// currency view. Default it to "no data"; the gem cases override per-case.
// divinePrice is also stubbed (currencyCheck → scoutRates calls it).
vi.mock("../src/poe2scout", () => ({
  scoutPrice: vi.fn(async () => null),
  divinePrice: vi.fn(async () => null),
}));

// The trade2 search + fetch network edge for the listings route. Keep the
// real createTradeRequest/createPresets exports (priceCheck builds the query
// through them); override only the two functions that hit the network so the
// listings path runs offline.
vi.mock("@/web/price-check/trade/pathofexile-trade", async (importOriginal) => {
  const mod =
    await importOriginal<
      typeof import("@/web/price-check/trade/pathofexile-trade")
    >();
  return {
    ...mod,
    requestTradeResultList: vi.fn(async () => ({
      id: "query-id",
      total: 3,
      result: ["r1", "r2", "r3"],
    })),
    requestResults: vi.fn(async () => [
      {
        id: "r1", priceAmount: 5, priceCurrency: "exalted", accountName: "s1", listedAt: "1m",
        // r1 carries a displayItem with an icon url -> brain must resolve it to
        // a cached iconPath (null in tests, where fetch is stubbed to fail).
        displayItem: {
          title: ["Storm Caress", "Vile Robe"],
          icon: { url: "https://web.poecdn.com/image/storm-caress.png", w: 2, h: 3 },
        },
      },
      // r2 has no displayItem at all -> iconPath path must guard cleanly.
      { id: "r2", priceAmount: 6, priceCurrency: "exalted", accountName: "s2", listedAt: "2m" },
    ]),
  };
});

beforeAll(initBrainData);

// Reset the scout source to "miss" before each case so the listings-route
// tests are unaffected; the gem cases set scoutPrice explicitly.
beforeEach(async () => {
  const scout = await import("../src/poe2scout");
  (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
  (scout.divinePrice as ReturnType<typeof vi.fn>).mockResolvedValue(null);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

it("rejects garbage clipboard with a readable error", async () => {
  await expect(buildQuery("garbage clipboard")).rejects.toThrow("not an item");
});

it("builds a trade2 query for the rare fixture", async () => {
  const text = readFileSync(
    new URL("./fixtures/rare-armour.txt", import.meta.url),
    "utf8",
  );
  const q = await buildQuery(text);
  expect(q.query).toBeDefined();
  expect(q.query.status).toBeDefined();
  expect(JSON.stringify(q)).toMatchSnapshot();
});

it("currencyIconUrl maps trade tag via vendored data", async () => {
  const { currencyIconUrl } = await import("../src/icons");
  const url = await currencyIconUrl("divine");
  expect(url).toMatch(/^https:\/\/web\.poecdn\.com\//);
  expect(await currencyIconUrl("not-a-tag")).toBeUndefined();
});

it("buildCard exposes item card with icon path slot", async () => {
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-price-test";
  // No network in tests: force resolveIcon's fetch to fail -> iconPath null.
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("no net in tests");
    }),
  );
  const { buildCard } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const card = await buildCard(text);
  expect(card.name).toBe("Storm Caress");
  expect(card.mods.prefix.length).toBeGreaterThan(0);
  // iconPath is string (cached) or null (offline test env) — never undefined
  expect(card).toHaveProperty("iconPath");
  expect(card.iconPath).toBeNull();
  expect((card as any).iconUrl).toBeUndefined(); // URL stays brain-side
});

it("priceCheck routes stackable currency to the currency view", async () => {
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-price-cur-test";
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => {
      throw new Error("no net in tests");
    }),
  );
  const { priceCheck } = await import("../src/price");
  const divine = `Item Class: Stackable Currency
Rarity: Currency
Divine Orb
--------
Stack Size: 7/20
--------
Modifies a magic or rare item, rerolling the values of its modifiers
`;
  const r = (await priceCheck(divine, "L")) as any;
  expect(r.kind).toBe("currency");
  expect(r.name).toBe("Divine Orb");
  // divine self-excluded; chaos isn't a payment currency → only exalted.
  expect(r.rates.map((x: any) => x.have)).toEqual(["exalted"]);
});

it("priceCheck routes a poe2scout-priced gem to the currency view", async () => {
  // A gem (Uncut Skill Gem) parses with info.tradeTag set but NO stackSize, so
  // isCurrency misses it. When poe2scout prices the tag, priceCheck must route
  // to the currency view instead of falling through to listings.
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-price-gem-test";
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no net in tests"); }));
  const scout = await import("../src/poe2scout");
  (scout.scoutPrice as ReturnType<typeof vi.fn>).mockResolvedValue({
    price: 26000, quantity: 12, history: [],
  });
  const { priceCheck } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/gem.txt", import.meta.url),
    "utf8",
  );
  const r = (await priceCheck(text, "L")) as any;
  expect(r.kind).toBe("currency");
  // Non-stackable → stack defaults to 1, so the stack row equals the unit.
  expect(r.stack).toBe(1);
  const exalted = r.rates.find((x: any) => x.have === "exalted");
  expect(exalted.rawUnit).toBe(26000);
});

it("priceCheck attaches a resolved iconPath onto each listing's displayItem", async () => {
  // The hover card needs the seller's item icon, but poed can't fetch remote
  // URLs — brain resolves displayItem.icon.url to a cached local path and
  // attaches it as displayItem.iconPath. With fetch stubbed to fail, the cache
  // miss resolves to null, but the slot must always be present.
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-listing-icon-test";
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no net in tests"); }));
  const { priceCheck } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const r = (await priceCheck(text, "L")) as any;
  expect(r.kind).toBe("price");

  // r1 had a displayItem with an icon url -> iconPath slot present, null offline.
  const r1 = r.listings.find((l: any) => l.id === "r1");
  expect(r1.displayItem).toBeDefined();
  expect(r1.displayItem).toHaveProperty("iconPath");
  expect(r1.displayItem.iconPath).toBeNull();
  // the rest of the displayItem is passed through untouched.
  expect(r1.displayItem.title).toEqual(["Storm Caress", "Vile Robe"]);
  expect(r1.displayItem.icon.url).toBe("https://web.poecdn.com/image/storm-caress.png");

  // r2 had no displayItem at all -> passed through unchanged, no crash.
  const r2 = r.listings.find((l: any) => l.id === "r2");
  expect(r2.displayItem).toBeUndefined();
});

it("gear query enables non-rune stats and reports them", async () => {
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { query, stats } = await buildQueryAndStats(text, "L");
  expect(query).toBeTruthy();
  expect(stats.length).toBeGreaterThan(3);
  // Only affix mods (non-rune, non-property) are enabled by default.
  const affixMods = stats.filter(
    (s: any) => s.tag !== "rune" && s.tag !== "added-rune" && s.tag !== "property",
  );
  expect(affixMods.every((s: any) => s.enabled)).toBe(true);
  const dex = stats.find((s: any) => s.text.includes("Dexterity"));
  expect(dex.value).toBe(30);
  expect(dex.min).toBeLessThan(30);
});

it("property-tagged stats (defences/ward) default to disabled; explicit affix mods stay enabled; rune stays disabled", async () => {
  // User decision (iteration 6): search defaults to affix mods only.
  // Defences and runic ward carry tag "property" — they are base stats, not
  // rolled affixes, so they start disabled (toggleable by the user).
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");

  const propertyStats = stats.filter((s: any) => s.tag === "property");
  const explicitStats = stats.filter((s: any) => s.tag === "explicit");
  const runeStats = stats.filter((s: any) => s.tag === "rune" || s.tag === "added-rune");

  // Property-tagged stats (Evasion Rating, Runic Ward) start disabled.
  expect(propertyStats.length).toBeGreaterThan(0);
  expect(propertyStats.every((s: any) => s.enabled === false)).toBe(true);

  // Explicit affix mods stay enabled.
  expect(explicitStats.length).toBeGreaterThan(0);
  expect(explicitStats.every((s: any) => s.enabled === true)).toBe(true);

  // Rune stats stay disabled.
  expect(runeStats.every((s: any) => s.enabled === false)).toBe(true);
});

it("stats carry index ids and honor overrides", async () => {
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const base = await buildQueryAndStats(text, "L");
  expect(base.stats.map((s: any) => s.id)).toEqual(
    base.stats.map((_: any, i: number) => i),
  ); // ids are indexes

  const dex = base.stats.findIndex((s: any) => s.text.includes("Dexterity"));
  const evas = base.stats.findIndex((s: any) => s.text.includes("Evasion"));
  expect(dex).toBeGreaterThanOrEqual(0);
  expect(evas).toBeGreaterThanOrEqual(0);
  const o = await buildQueryAndStats(text, "L", [
    { i: dex, enabled: false },
    { i: evas, enabled: true, min: 50 },
  ]);
  expect(o.stats[dex].enabled).toBe(false);
  expect(o.stats[evas].min).toBe(50);
  // and the emitted query body reflects it: the evasion stat's filter carries
  // min 50, the disabled dex stat is no longer an enabled filter.
  const qs = JSON.stringify(o.query);
  expect(qs).toContain('"min":50');
});

it("exposes item property filters as a queryable props list", async () => {
  // The gloves fixture's createPresets emits FilterNumeric members on
  // preset.filters — probed: itemLevel {value:80, disabled:true} and
  // augmentSockets {value:1, disabled:true} (no quality: EE2 omits quality at
  // exactly 20 for non-flasks). props mirror those: searched min == .value,
  // enabled == !disabled.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { props } = await buildQueryAndStats(text, "L");
  expect(Array.isArray(props)).toBe(true);

  const ilvl = props.find((p: any) => p.key === "itemLevel");
  expect(ilvl).toBeDefined();
  expect(ilvl.text).toBe("Item Level");
  expect(ilvl.value).toBe(80);
  expect(ilvl.min).toBe(80); // the searched min IS .value
  expect(ilvl.enabled).toBe(false); // disabled:true -> enabled false

  const runes = props.find((p: any) => p.key === "augmentSockets");
  expect(runes).toBeDefined();
  expect(runes.value).toBe(1);
  expect(runes.enabled).toBe(false);

  // Absent members are not in the list (no quality on this fixture).
  expect(props.some((p: any) => p.key === "quality")).toBe(false);
});

it("prop overrides flip disabled + set value and reach the query body", async () => {
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  // Default: itemLevel disabled -> ilvl.min absent from the query body.
  const base = await buildQueryAndStats(text, "L");
  expect(JSON.stringify(base.query)).not.toContain("ilvl");

  // Enable itemLevel at a lower min -> prop reports it AND the body carries
  // type_filters.filters.ilvl.min = 70.
  const o = await buildQueryAndStats(text, "L", [
    { p: "itemLevel", enabled: true, min: 70 },
  ]);
  const ilvl = o.props.find((p: any) => p.key === "itemLevel");
  expect(ilvl.enabled).toBe(true);
  expect(ilvl.min).toBe(70);
  expect(ilvl.value).toBe(70);
  expect(JSON.stringify(o.query)).toContain('"ilvl":{"min":70}');
});

it("mixes prop overrides and stat overrides in one list", async () => {
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const base = await buildQueryAndStats(text, "L");
  const dex = base.stats.findIndex((s: any) => s.text.includes("Dexterity"));
  expect(dex).toBeGreaterThanOrEqual(0);

  const o = await buildQueryAndStats(text, "L", [
    { i: dex, enabled: false },
    { p: "augmentSockets", enabled: true, min: 1 },
  ]);
  // Stat override still applies.
  expect(o.stats[dex].enabled).toBe(false);
  // Prop override applies alongside.
  const runes = o.props.find((p: any) => p.key === "augmentSockets");
  expect(runes.enabled).toBe(true);
  expect(JSON.stringify(o.query)).toContain("rune_sockets");
});

it("surfaces corrupted as a toggle-only prop and the override reaches the query body", async () => {
  // No corrupted fixture exists, so craft a corrupted rare from the gloves
  // fixture by appending a `Corrupted` section. ItemFilters.corrupted has shape
  // { value: boolean; exact?: boolean } — NOT a FilterNumeric — so it surfaces
  // as a toggle-only prop (value null -> poed renders no entry box) and its
  // override flips `exact` instead of `disabled`.
  const { buildQueryAndStats } = await import("../src/price");
  const gloves = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const corruptedGloves = gloves.trimEnd() + "\n--------\nCorrupted\n";

  // Default: corrupted prop present, toggle-only (value/min null). A corrupted
  // item with exact:false emits NO corrupted constraint in the query body, so
  // the default `enabled` is false.
  const base = await buildQueryAndStats(corruptedGloves, "L");
  const corr = base.props.find((p: any) => p.key === "corrupted");
  expect(corr).toBeDefined();
  expect(corr.text).toBe("Corrupted");
  expect(corr.value).toBeNull(); // toggle-only: no entry box in poed
  expect(corr.min).toBeNull();
  expect(corr.enabled).toBe(false);
  expect(JSON.stringify(base.query)).not.toContain("corrupted");

  // Enabling the toggle requires the corrupted state exactly (exact:true), so
  // the query body gains misc_filters corrupted.option = "true".
  const on = await buildQueryAndStats(corruptedGloves, "L", [
    { p: "corrupted", enabled: true },
  ]);
  const onCorr = on.props.find((p: any) => p.key === "corrupted");
  expect(onCorr.enabled).toBe(true);
  const body = JSON.stringify(on.query);
  expect(body).toContain("corrupted");
  expect(body).toContain('"option":"true"');
});

it("builds stats as explicit mods, never absorbing them into pseudo totals", async () => {
  // The whole point of initExplicitModFilters: EE2's filterPseudo pass would
  // turn "Cold Resistance" into a pseudo.pseudo_total_cold_resistance line and
  // drop the source explicit. We bypass it, so the resist must surface as an
  // explicit-tagged stat and the query must carry no "pseudo." trade ids.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { query, stats } = await buildQueryAndStats(text, "L");

  // No stat may be tagged "pseudo".
  expect(stats.some((s: any) => s.tag === "pseudo")).toBe(false);

  // Cold Resistance is present as an explicit-tagged stat (not a pseudo total).
  const cold = stats.find((s: any) => s.text.includes("Cold Resistance"));
  expect(cold).toBeDefined();
  expect(cold.tag).toBe("explicit");

  // The emitted query body contains no pseudo.* trade ids.
  expect(JSON.stringify(query)).not.toContain('"pseudo.');
});

it("explicit-mode keeps a sane, non-degenerate stat count", async () => {
  // Bypassing pseudo means resist/attr/life lines stay as individual explicits
  // instead of collapsing into fewer pseudo totals, so the visible count should
  // be at least as large as the parsed explicit mods and nowhere near empty.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");
  // The fixture has 6 explicit mods + a rune; expect the visible explicit set
  // to be substantial (the old pseudo path returned ~4-5 after absorption).
  expect(stats.length).toBeGreaterThanOrEqual(5);
  expect(stats.filter((s: any) => s.tag === "explicit").length).toBeGreaterThan(
    3,
  );
});

it("returned stats list excludes EE2-hidden lines (hide_const_roll, hide_low_ilvl etc.)", async () => {
  // EE2 still marks genuine noise lines hidden even in the explicit path
  // (filterItemProp + finalFilterTweaks set hide_const_roll, hide_low_ilvl,
  // hide_empty_mod, etc.). Those must never leak into the returned stats list
  // (no value to the UI) regardless of how they're tagged.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");

  // No returned stat should carry a hidden string (they were filtered out).
  expect(stats.every((s: any) => !s.hidden)).toBe(true);
});

it("waystone query searches the exact tiered base, not every map by category (issue #1)", async () => {
  // PoE2 waystones (Item Class: Waystones) carry their tier in the base-type
  // line ("Waystone (Tier 15)"), which is a real, trade-indexed base type. EE2's
  // map flow sets searchRelaxed { category: Map, disabled: false } alongside the
  // exact base, and createTradeRequest prefers the relaxed search when it's
  // enabled — so the emitted query carried only category=map and NO base type,
  // returning every map of any tier (the "wrong/unrelated listings" bug). The
  // query must constrain to the exact base type.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/waystone.txt", import.meta.url),
    "utf8",
  );
  const { query } = await buildQueryAndStats(text, "L");

  // The exact tiered base type must be the search term.
  expect((query.query as any).type).toBe("Waystone (Tier 15)");
});

it("waystone explicit mods are visible, enabled and reach the query body (issue #1)", async () => {
  // EE2 marks every explicit affix on a map item hidden+disabled
  // ("filters.hide_for_map"), because PoE1 maps were fungible by tier. PoE2
  // waystone affixes (Item Rarity %, Pack Size %, Waystone Drop Chance %, and
  // dangerous suffixes) are exactly what buyers filter on, so they rendered gray
  // (not matched/toggleable) and never reached the trade query. They must
  // surface as enabled, toggleable explicit stats and emit enabled stat filters.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/waystone.txt", import.meta.url),
    "utf8",
  );
  const { query, stats } = await buildQueryAndStats(text, "L");

  // The 4 parsed explicit mods are now visible (not hidden) and default-enabled.
  const explicits = stats.filter((s: any) => s.tag === "explicit");
  expect(explicits.length).toBe(4);
  expect(explicits.every((s: any) => s.enabled)).toBe(true);
  expect(stats.every((s: any) => !s.hidden)).toBe(true);

  // Those stat filters reach the query body as enabled (not disabled) filters.
  const statFilters = (query.query as any).stats[0].filters;
  expect(statFilters.length).toBeGreaterThanOrEqual(4);
  expect(statFilters.some((f: any) => f.disabled === true)).toBe(false);
});

it("waystone properties (revives, pack size, rarity, drop chance) surface as toggleable stats (issue #1 follow-up)", async () => {
  // Vendor parseWaystone only fires when the section starts with a
  // "Waystone Tier: " line; PoE2 encodes the tier in the base-type name and
  // opens the property section with "Revives Available:", so the whole block
  // was skipped — item.map* stayed unset and vendor mapProps() emitted no
  // property filters. The card/panel showed no Item Rarity / Pack Size /
  // Waystone Drop Chance / Revives at all.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/waystone.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");

  const texts = stats.map((s: any) => s.text).join("\n");
  for (const expected of [
    "Revives Available",
    "Pack Size",
    "Item Rarity",
    "Waystone Drop Chance",
  ]) {
    expect(texts).toContain(expected);
  }
});

it("granted skills on uniques surface as toggleable stats", async () => {
  // Vendor hides granted-skill filters that aren't max level
  // ("filters.hide_not_max_level"), so "Grants Skill: Level 18 Spirit Vessel"
  // never showed on the panel. Skill level drives unique value — surface it
  // like any other stat (it carries a real trade id, e.g.
  // skill.spirit_vessel_companion).
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/unique-armour.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");

  const skill = stats.find((s: any) => s.tag === "skill");
  expect(skill).toBeDefined();
  expect(skill!.text).toContain("Spirit Vessel");
  expect(skill!.value).toBe(18);
});

it("stat rows carry their source mod's generation (prefix/suffix)", async () => {
  // The trade API has no prefix/suffix dimension, but the advanced-copy parse
  // knows each mod's generation — pass it through so the panel can group
  // filter rows into Prefixes/Suffixes instead of one flat Mods list.
  const { buildQueryAndStats } = await import("../src/price");
  const text = readFileSync(
    new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
    "utf8",
  );
  const { stats } = await buildQueryAndStats(text, "L");

  const gens = stats
    .filter((s: any) => s.tag === "explicit")
    .map((s: any) => s.generation);
  expect(gens).toContain("prefix");
  expect(gens).toContain("suffix");
  expect(gens.every((g: any) => g === "prefix" || g === "suffix")).toBe(true);
});
