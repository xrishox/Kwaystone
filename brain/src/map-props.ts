import type { ParsedItem } from "@/parser";

// Vendor parseWaystone (vendor/ee2/src/parser/Parser.ts:311) only parses the
// property section when its first line is "Waystone Tier: ". PoE2 clipboards
// carry the tier inside the base-type name ("Waystone (Tier 15)") and open the
// section with "Revives Available:", so the whole block is SECTION_SKIPPED —
// item.mapTier/mapRevives/mapPackSize/... stay unset, and vendor mapProps()
// (filters/pseudo/item-property.ts) emits no property filters for them.
//
// Same recovery pattern as unique-defences.ts: re-read the lines from rawText
// using the vendor's own CLIENT_STRINGS labels, vendor file untouched.

type MapField =
  | "mapTier"
  | "mapRevives"
  | "mapPackSize"
  | "mapMagicMonsters"
  | "mapRareMonsters"
  | "mapDropChance"
  | "mapItemRarity";

const LABEL_KEYS: ReadonlyArray<readonly [string, MapField]> = [
  ["WAYSTONE_REVIVES", "mapRevives"],
  ["WAYSTONE_PACK_SIZE", "mapPackSize"],
  ["WAYSTONE_MAGIC_MONSTERS", "mapMagicMonsters"],
  ["WAYSTONE_RARE_MONSTERS", "mapRareMonsters"],
  ["WAYSTONE_DROP_CHANCE", "mapDropChance"],
  ["WAYSTONE_RARITY", "mapItemRarity"],
];

const TIER_IN_BASE = /\(Tier (\d+)\)/;

/**
 * Re-populate the map property fields vendor parseWaystone missed, from the
 * item's rawText. No-op unless the item is map-category with unset fields.
 * Mutates and returns the item.
 */
export async function restoreMapProps<T extends ParsedItem>(item: T): Promise<T> {
  const { ItemCategory } = await import("@/parser/meta");
  if (item.category !== ItemCategory.Map) return item;

  const st = item as ParsedItem & { rawText?: string };
  const rawText = st.rawText;
  if (!rawText) return item;

  if (item.mapTier == null) {
    const m = TIER_IN_BASE.exec(item.info.refName ?? "");
    if (m) item.mapTier = Number(m[1]);
  }

  const { CLIENT_STRINGS } = (await import("@/assets/data")) as unknown as {
    CLIENT_STRINGS: Record<string, string>;
  };
  const lines = rawText.split("\n");
  for (const [key, field] of LABEL_KEYS) {
    if (item[field] != null) continue; // don't clobber anything vendor parsed
    const label = CLIENT_STRINGS[key];
    if (!label) continue;
    for (const raw of lines) {
      const line = raw.trim();
      if (!line.startsWith(label)) continue;
      // parseInt tolerates "+27% (augmented)" — stops at '%'.
      const n = parseInt(line.slice(label.length), 10);
      if (!Number.isNaN(n)) {
        (item as Record<MapField, number | undefined>)[field] = n;
      }
      break;
    }
  }

  return item;
}
