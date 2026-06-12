import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

const text = readFileSync(
  new URL("./fixtures/rare-gloves-advanced.txt", import.meta.url),
  "utf8",
);

describe("buildItemCard", () => {
  it("builds grouped card from advanced-copy rare", async () => {
    const { parseClipboard } = await import("@/parser");
    const { buildItemCard } = await import("../src/item-card");
    const item = parseClipboard(text)._unsafeUnwrap();
    const card = buildItemCard(item);

    expect(card.name).toBe("Storm Caress");
    expect(card.baseType).toBe("Runeforged Stalking Bracers");
    expect(card.rarity).toBe("Rare");
    expect(card.iconUrl).toMatch(/^https:\/\/web\.poecdn\.com\//);

    expect(card.props).toContainEqual({ text: "Quality", value: "+20%" });
    expect(card.props).toContainEqual({ text: "Evasion Rating", value: "769" });
    expect(card.props).toContainEqual({ text: "Item Level", value: "80" });

    expect(card.mods.prefix).toHaveLength(4); // 3 mods, one hybrid (2 stat lines)
    expect(card.mods.suffix).toHaveLength(3);
    expect(card.mods.rune).toHaveLength(1);
    expect(card.mods.rune[0].text).toContain("Chaos Resistance");

    const dex = card.mods.suffix.find((l) => l.text.includes("Dexterity"));
    expect(dex).toBeDefined();
    expect(dex!.text).toContain("30");
    expect(dex!.tier).toBe(3);
  });

  it("renders both numbers of a multi-placeholder stat", async () => {
    const { parseClipboard } = await import("@/parser");
    const { buildItemCard } = await import("../src/item-card");
    // magic-mace has "Adds 1(1-4) to 50(46-66) Lightning Damage": two distinct
    // placeholders that the parser flattens to a single averaged roll (25.5).
    const mace = readFileSync(
      new URL("./fixtures/magic-mace.txt", import.meta.url),
      "utf8",
    );
    const item = parseClipboard(mace)._unsafeUnwrap();
    const card = buildItemCard(item);

    const adds = card.mods.prefix.find((l) => l.text.includes("Lightning Damage"));
    expect(adds).toBeDefined();
    // Must show the real per-placeholder rolls, not the average (23/23 or 25.5).
    expect(adds!.text).toBe("Adds 1 to 50 Lightning Damage");
    expect(adds!.text).not.toContain("25.5");
  });

  it("handles plain copy without advanced blocks", async () => {
    const { parseClipboard } = await import("@/parser");
    const { buildItemCard } = await import("../src/item-card");
    const plain = readFileSync(
      new URL("./fixtures/magic-wand.txt", import.meta.url),
      "utf8",
    );
    const item = parseClipboard(plain)._unsafeUnwrap();
    const card = buildItemCard(item);
    expect(card.mods).toBeDefined(); // grouping must not throw on untyped mods
  });
});

it("granted skills get their own card bucket, not explicit mods", async () => {
  const { parseClipboard } = await import("@/parser");
  const { buildItemCard } = await import("../src/item-card");
  const text = readFileSync(
    new URL("./fixtures/unique-armour.txt", import.meta.url),
    "utf8",
  );
  const item = parseClipboard(text)._unsafeUnwrap();
  const card = buildItemCard(item);

  expect(card.mods.skill.length).toBe(1);
  expect(card.mods.skill[0].text).toContain("Spirit Vessel");
  expect(card.mods.explicit.some((l) => l.text.includes("Spirit Vessel"))).toBe(false);
});
