import type { ParsedItem } from "@/parser";
import { ItemRarity } from "@/parser";

// Vendor parseArmour (vendor/ee2/src/parser/Parser.ts:813-820) deliberately
// wipes armourAR/EV/ES/RW/BLOCK to undefined for Unique items. Both the hover
// card (item-card.ts reads item.armour*) and the item-property stat filters
// (vendor pseudo/item-property.ts armourProps, gated on `if (item.armourEV)`
// etc.) depend on those fields, so a unique armour rendered a bare card with no
// defence rows and no toggleable defence filter — issue #3.
//
// We don't want to edit pristine vendor (CLAUDE.md: keep vendor mods minimal),
// so we recover the values the same way parseArmour did before discarding them:
// slice the displayed numbers off the clipboard's defence lines. Labels come
// from the same CLIENT_STRINGS table the vendor parser uses, so this stays in
// lock-step with vendor without hardcoding English.
//
// The recovered numbers are the as-displayed totals (quality + mods already
// folded in), which is exactly what armourProps/the card want — for uniques we
// price on the shown defences, not a recomputed base.

type ArmourField = "armourAR" | "armourEV" | "armourES" | "armourRW" | "armourBLOCK";

const DEFENCE_LABELS: ReadonlyArray<readonly [keyof typeof CLIENT_KEYS, ArmourField]> = [
  ["ARMOUR", "armourAR"],
  ["EVASION", "armourEV"],
  ["ENERGY_SHIELD", "armourES"],
  ["RUNIC_WARD", "armourRW"],
  ["BLOCK_CHANCE", "armourBLOCK"],
];

// Only the CLIENT_STRINGS keys we touch; typed as a record so the import shape
// stays narrow and we don't depend on the full TranslationDict surface.
const CLIENT_KEYS = {
  ARMOUR: "",
  EVASION: "",
  ENERGY_SHIELD: "",
  RUNIC_WARD: "",
  BLOCK_CHANCE: "",
};

/**
 * Re-populate the defence fields vendor parseArmour clears for uniques, by
 * re-reading them from the item's rawText. No-op for non-uniques (vendor
 * already parsed them) and for items lacking rawText or a defence section.
 * Mutates and returns the item.
 */
export async function restoreUniqueDefences<T extends ParsedItem>(item: T): Promise<T> {
  if (item.rarity !== ItemRarity.Unique) return item;

  const st = item as ParsedItem & { rawText?: string };
  const rawText = st.rawText;
  if (!rawText) return item;

  const { CLIENT_STRINGS } = (await import("@/assets/data")) as {
    CLIENT_STRINGS: Record<keyof typeof CLIENT_KEYS, string>;
  };

  const lines = rawText.split("\n");
  for (const [key, field] of DEFENCE_LABELS) {
    if (item[field] != null) continue; // don't clobber anything already set
    const label = CLIENT_STRINGS[key];
    if (!label) continue;
    for (const raw of lines) {
      const line = raw.trim();
      if (!line.startsWith(label)) continue;
      // Mirrors parseArmour: parseInt off the slice after the label. Tolerates
      // the trailing "(augmented)" suffix (parseInt stops at the space).
      const n = parseInt(line.slice(label.length), 10);
      if (!Number.isNaN(n)) (item as Record<ArmourField, number | undefined>)[field] = n;
      break;
    }
  }

  return item;
}
