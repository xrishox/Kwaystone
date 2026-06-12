import { beforeAll, beforeEach, expect, it, vi } from "vitest";
import { initBrainData } from "../src/bootstrap";

// Scout feeds mocked; vendored data (names/icons for tradeTags) is REAL —
// initBrainData loads it, so "divine" resolves to Divine Orb + its icon URL.
// A real-shaped poecdn URL whose base64 segment declares w=2 h=1.
// vi.hoisted: vi.mock factories are hoisted above module-level consts.
const { MB_URL } = vi.hoisted(() => ({
  MB_URL:
    "https://web.poecdn.com/gen/image/" +
    Buffer.from('[25,14,{"f":"Mageblood","w":2,"h":1,"scale":1}]').toString("base64") +
    "/abc123/Mageblood.png",
}));

vi.mock("../src/poe2scout", () => ({
  uniquePriceMap: vi.fn(async () => new Map([
    ["Mageblood", { price: 67305.6, quantity: 1386, iconUrl: MB_URL, trend: 0.073 }],
    ["Splinter of Loratta", { price: 12.5, quantity: 50, iconUrl: "https://web.poecdn.com/gen/image/SL.png", trend: null }],
  ])),
  priceMap: vi.fn(async () => new Map([
    // chronological history 300 -> 333: +11%
    ["divine", { price: 333, quantity: 4444, history: [300, 333] }],
  ])),
}));

beforeAll(initBrainData);

beforeEach(() => {
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-uniques-test";
  // No network: icon resolution degrades to null, never rejects.
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no net in tests"); }));
});

it("scanCorpus merges uniques and scout-priced tagged items", async () => {
  const { scanCorpus } = await import("../src/uniques");
  const out = await scanCorpus("L");

  expect(out["Mageblood"]).toMatchObject({
    price: 67305.6, quantity: 1386, kind: "unique",
    w: 2, h: 1, // parsed from the poecdn URL's base64 segment
    trend: 0.073,
  });
  // tagged trend derives from scout history (300 -> 333 = +11%)
  expect(out["Divine Orb"].trend).toBeCloseTo(0.11, 2);
  expect(out["Mageblood"].iconPath).toBeNull(); // offline test env
  // No base64 metadata in the URL -> 1x1 fallback.
  expect(out["Splinter of Loratta"]).toMatchObject({ w: 1, h: 1 });

  // "divine" tradeTag -> Divine Orb via REAL vendored data, scout-priced.
  expect(out["Divine Orb"]).toMatchObject({
    price: 333, quantity: 4444, kind: "tagged",
  });

  // Tagged items without a scout price are not in the corpus.
  const tagged = Object.values(out).filter((r) => r.kind === "tagged");
  expect(tagged).toHaveLength(1);
  expect(Object.keys(out)).toHaveLength(3);
});
