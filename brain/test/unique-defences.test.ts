import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

const text = readFileSync(
  new URL("./fixtures/unique-armour.txt", import.meta.url),
  "utf8",
);

// Issue #3: price-checking a unique armour showed a bare card — no Evasion
// Rating / Energy Shield props, no defence stat filter — while rares got the
// full treatment. Root cause: vendor parseArmour wipes armourAR/EV/ES/RW for
// uniques, so both the card and the item-property stat filters (which read
// those fields) come up empty. brain re-populates them from rawText.

describe("unique armour card", () => {
  it("shows Evasion Rating and Energy Shield props (parity with rares)", async () => {
    const { buildCard } = await import("../src/price");
    const card = await buildCard(text);

    expect(card.name).toBe("Forgotten Warden");
    expect(card.rarity).toBe("Unique");
    expect(card.props).toContainEqual({ text: "Evasion Rating", value: "1187" });
    expect(card.props).toContainEqual({ text: "Energy Shield", value: "363" });
    expect(card.props).toContainEqual({ text: "Quality", value: "+20%" });
  });

  it("keeps rune-granted stats grouped under rune, not as armour props", async () => {
    const { buildCard } = await import("../src/price");
    const card = await buildCard(text);
    const runeTexts = card.mods.rune.map((m) => m.text);
    expect(runeTexts.some((t) => t.includes("Rarity of Items found"))).toBe(true);
    expect(runeTexts.some((t) => t.includes("Deflection Rating"))).toBe(true);
  });
});

describe("unique armour stat filters", () => {
  it("emits toggleable defence property filters (Evasion / Energy Shield)", async () => {
    const { buildQueryAndStats } = await import("../src/price");
    const { stats } = await buildQueryAndStats(text, "Standard");
    const propStats = stats.filter((s: any) => s.tag === "property");
    const propTexts = propStats.map((s: any) => s.text);
    expect(propTexts.some((t: string) => t.includes("Evasion Rating"))).toBe(true);
    expect(propTexts.some((t: string) => t.includes("Energy Shield"))).toBe(true);
    // property filters are toggleable but start disabled (affix-mods-only default)
    expect(propStats.every((s: any) => s.enabled === false)).toBe(true);
  });

  it("emits toggleable explicit stat filters for the variable unique mods", async () => {
    const { buildQueryAndStats } = await import("../src/price");
    const { stats } = await buildQueryAndStats(text, "Standard");
    const explicitTexts = stats
      .filter((s: any) => s.tag === "explicit")
      .map((s: any) => s.text);
    // the variable-roll unique mods should be present and toggleable.
    // ("#% increased Evasion and Energy Shield" is intentionally absorbed into
    // the Evasion/Energy Shield property filters — same as rares — so it is NOT
    // a separate explicit row.)
    expect(explicitTexts.some((t: string) => t.includes("to Dexterity"))).toBe(true);
    expect(
      explicitTexts.some((t: string) =>
        t.includes("Deflection Rating per 50 missing Energy Shield"),
      ),
    ).toBe(true);
    expect(
      explicitTexts.some((t: string) =>
        t.includes("Companions have") && t.includes("increased maximum Life"),
      ),
    ).toBe(true);
    // explicit affix mods default to enabled
    const enabled = stats.filter(
      (s: any) => s.tag === "explicit" && s.enabled,
    );
    expect(enabled.length).toBeGreaterThan(0);
  });
});
