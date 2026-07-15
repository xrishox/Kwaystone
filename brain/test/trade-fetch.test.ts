import { expect, it } from "vitest";
import { __testExports } from "@/web/price-check/trade/pathofexile-trade";

it("parses both string and rich-object trade modifier entries", () => {
  const display = __testExports.parseFetchResult({
    id: "perandus-seal-listing",
    item: {
      w: 1,
      h: 1,
      icon: "https://web.poecdn.com/perandus-seal.png",
      sockets: [],
      name: "Perandus Seal",
      typeLine: "Gold Ring",
      baseType: "Gold Ring",
      rarity: "Unique",
      identified: true,
      implicitMods: ["8% increased [ItemRarity|Rarity of Items] found"],
      explicitMods: [
        {
          description: "+44 to maximum Mana",
          hash: "stat.explicit.stat_1050105434",
          mods: [{ magnitudes: [{ min: "30", max: "50" }] }],
        },
        {
          description: "+7 to all [Attributes|Attributes]",
          hash: "stat.explicit.stat_1379411836",
        },
        // Unknown external entries are ignored rather than failing the lookup.
        { hash: "future.trade2.shape" },
      ],
    },
    listing: {
      indexed: "2026-06-20T00:00:00Z",
      account: { name: "seller", lastCharacterName: "character" },
    },
  } as any);

  expect(display.implicitMods?.map((line) => line.text)).toEqual([
    "8% increased Rarity of Items found",
  ]);
  expect(display.explicitMods?.map((line) => line.text)).toEqual([
    "+44 to maximum Mana",
    "+7 to all Attributes",
  ]);
});
