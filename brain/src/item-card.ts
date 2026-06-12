import type { ParsedItem } from "@/parser/ParsedItem";
import type { ParsedStat } from "@/parser/stat-translations";
import { ModifierType } from "@/parser/modifiers";

export interface CardLine {
  text: string;
  tier?: number;
}

export interface ItemCard {
  name: string;
  baseType?: string;
  rarity?: string;
  iconUrl?: string;
  props: Array<{ text: string; value: string }>;
  mods: Record<"rune" | "implicit" | "prefix" | "suffix" | "explicit", CardLine[]>;
}

const trim = (n: number) =>
  Number.isInteger(n) ? String(n) : String(+n.toFixed(2));

// Strip the advanced-copy bound annotations a roll carries, e.g.
// "Adds 1(1-4) to 50(46-66) Lightning Damage" -> "Adds 1 to 50 Lightning Damage".
const stripBounds = (line: string) => line.replace(/\((?:[^()]*)\)/g, "");

// The parser flattens multi-placeholder rolls: for an "Adds # to #" stat,
// getRollOrMinmaxAvg (vendor/ee2/src/parser/stat-translations.ts:312-324)
// AVERAGES the two values into roll.value (and roll.min/roll.max are averaged
// bounds, not the per-placeholder values), so "Adds 1 to 50" collapses to 25.5
// and the originals are unrecoverable from ParsedStat.roll alone. The verbatim
// per-placeholder numbers only survive on item.rawText, so for templates with
// 2+ '#' we re-extract them from the matching clipboard line.
function placeholdersFromRawText(
  template: string,
  rawText: string,
): string[] | undefined {
  const holes = (template.match(/#/g) ?? []).length;
  if (holes < 2) return undefined;
  // Build a regex: escape the literal template, turn each '#' into a number
  // capture (optional sign / decimals), allow flexible inner whitespace.
  const escaped = template.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const pattern = escaped.replace(/#/g, "([+-]?\\d+(?:\\.\\d+)?)");
  const re = new RegExp(`^${pattern}$`);
  for (const raw of rawText.split("\n")) {
    const m = stripBounds(raw).trim().match(re);
    if (m) return m.slice(1, holes + 1);
  }
  return undefined;
}

// translation.string is a template ("#% to Cold Resistance"); the verbatim
// clipboard line is not stored on the stat, so re-render it from the roll.
function renderStat(s: ParsedStat, rawText: string): string {
  const template = s.translation.string;
  const perValue = placeholdersFromRawText(template, rawText);
  if (perValue) {
    // Fill placeholders in order with the real rolls from the clipboard.
    let i = 0;
    return template.replace(/#/g, () => perValue[i++]);
  }
  // Single-# (or no raw match): the flattened roll.value is exact.
  const v = s.roll ? trim(s.roll.value) : "";
  return template.replace(/#/g, v);
}

export function buildItemCard(item: ParsedItem): ItemCard {
  // parseClipboard's runtime object is a ParserState carrying name/baseType
  // (Parser.ts:52-56); the static type hides them.
  const st = item as ParsedItem & {
    name?: string;
    baseType?: string;
    rawText?: string;
  };
  const rawText = st.rawText ?? "";

  const props: ItemCard["props"] = [];
  if (item.quality != null) props.push({ text: "Quality", value: `+${item.quality}%` });
  if (item.armourAR != null) props.push({ text: "Armour", value: trim(item.armourAR) });
  if (item.armourEV != null) props.push({ text: "Evasion Rating", value: trim(item.armourEV) });
  if (item.armourES != null) props.push({ text: "Energy Shield", value: trim(item.armourES) });
  if (item.armourRW != null) props.push({ text: "Runic Ward", value: trim(item.armourRW) });
  if (item.itemLevel != null) props.push({ text: "Item Level", value: String(item.itemLevel) });

  const mods: ItemCard["mods"] = { rune: [], implicit: [], prefix: [], suffix: [], explicit: [] };
  for (const m of item.newMods) {
    const g = m.info.generation;
    const key =
      g === "prefix" || g === "suffix"
        ? g
        : m.info.type === ModifierType.Implicit
          ? "implicit"
          : // AddedAugment ("added-rune", vendor/ee2/src/parser/modifiers.ts:179)
            // is a rune-granted stat too; route it to "rune" not "explicit".
            m.info.type === ModifierType.Augment ||
              m.info.type === ModifierType.AddedAugment
            ? "rune"
            : "explicit";
    for (const s of m.stats) {
      mods[key].push({ text: renderStat(s, rawText), tier: m.info.tier });
    }
  }

  return {
    name: st.name ?? item.info.name,
    baseType: st.baseType,
    rarity: item.rarity,
    iconUrl: item.info.icon,
    props,
    mods,
  };
}
