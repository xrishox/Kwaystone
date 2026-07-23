import { afterEach, beforeAll, beforeEach, expect, it, vi } from "vitest";
import { initBrainData } from "../src/bootstrap";

// Scout feeds mocked; vendored data (names/icons for tradeTags) is REAL —
// initBrainData loads it, so "divine" resolves to Divine Orb + its icon URL.
// A real-shaped poecdn URL whose base64 segment declares w=2 h=1.
// vi.hoisted: vi.mock factories are hoisted above module-level consts.
// `scout` lets tests degrade the mocked market pulls (complete: false).
const { MB_URL, scout } = vi.hoisted(() => {
  const MB_URL =
    "https://web.poecdn.com/gen/image/" +
    Buffer.from('[25,14,{"f":"Mageblood","w":2,"h":1,"scale":1}]').toString("base64") +
    "/abc123/Mageblood.png";
  const uniquesMap = () => new Map([
    ["Mageblood", { price: 67305.6, quantity: 1386, iconUrl: MB_URL, trend: 0.073, priceSource: "current" as const }],
    ["Splinter of Loratta", { price: 12.5, quantity: 50, iconUrl: "https://web.poecdn.com/gen/image/SL.png", trend: null, priceSource: "current" as const }],
  ]);
  const currenciesMap = () => new Map([
    // chronological history 300 -> 333: +11%
    ["divine", { price: 333, quantity: 4444, history: [300, 333], category: "currency" }],
    ["chaos", { price: 0.25, quantity: 9999, history: [0.2, 0.25], category: "currency" }],
    ["atziris-allure", {
      price: 29, quantity: 24, history: [10, 29], category: "lineagesupportgems",
      name: "Live Atziri's Allure",
      iconUrl: "https://web.poecdn.com/gen/image/live-lineage.png",
      quoteAmount: 1.5,
      quoteCurrency: "chaos",
      quoteCurrencyText: "Chaos Orb",
      quoteLiquidity: 200,
      quoteMaxStock: 50,
    }],
    ["swift-alloy", {
      price: 4.5, quantity: 455, history: [3.2, 4.5], category: "verisium",
      name: "Swift Alloy",
      iconUrl: "https://web.poecdn.com/gen/image/swift.png",
    }],
  ]);
  return { MB_URL, scout: { uniquesMap, currenciesMap, complete: true } };
});

vi.mock("../src/poe2scout", () => ({
  divinePrice: vi.fn(async () => 333),
  uniquePriceMap: vi.fn(async () => scout.uniquesMap()),
  uniquePriceMapDetailed: vi.fn(async () => ({
    map: scout.uniquesMap(),
    complete: scout.complete,
  })),
  priceMap: vi.fn(async () => scout.currenciesMap()),
  priceMapDetailed: vi.fn(async () => ({
    map: scout.currenciesMap(),
    complete: scout.complete,
  })),
  refreshPriceMap: vi.fn(async () => scout.currenciesMap()),
  refreshUniquePriceMap: vi.fn(async () => scout.uniquesMap()),
}));

beforeAll(initBrainData);

beforeEach(() => {
  vi.clearAllMocks();
  process.env.POE2_ICON_CACHE = "/tmp/poed-icons-uniques-test";
  scout.complete = true;
  // No network: icon resolution degrades to null, never rejects.
  vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("no net in tests"); }));
});

afterEach(async () => {
  const { _clearCorpusCache } = await import("../src/uniques");
  _clearCorpusCache();
  vi.useRealTimers();
});

it("scanCorpusVersioned reports a stable version until the snapshot rebuilds", async () => {
  const { scanCorpusVersioned, _clearCorpusCache } = await import("../src/uniques");
  const first = await scanCorpusVersioned("L");
  const second = await scanCorpusVersioned("L");
  expect(second.version).toBe(first.version);
  expect(second.rows).toBe(first.rows);

  _clearCorpusCache();
  const rebuilt = await scanCorpusVersioned("L");
  expect(rebuilt.version).toBeGreaterThan(first.version);
});

it("keeps assembled corpus snapshots isolated by league", async () => {
  const { scanCorpusVersioned } = await import("../src/uniques");
  const a = await scanCorpusVersioned("League A");
  await scanCorpusVersioned("League B");
  const aAgain = await scanCorpusVersioned("League A");

  expect(aAgain.version).toBe(a.version);
  expect(aAgain.rows).toBe(a.rows);
});

it("queues a forced market refresh behind an ordinary in-flight build", async () => {
  const { refreshPriceMap, refreshUniquePriceMap } = await import("../src/poe2scout");
  const { refreshScanCorpus, scanCorpusVersioned } = await import("../src/uniques");
  const ordinary = scanCorpusVersioned("L");
  const forced = refreshScanCorpus("L");
  await Promise.all([ordinary, forced]);

  expect(refreshPriceMap).toHaveBeenCalledTimes(1);
  expect(refreshUniquePriceMap).toHaveBeenCalledTimes(1);
});

it("corpus versions never collide across brain restarts", async () => {
  const { scanCorpusVersioned } = await import("../src/uniques");
  const first = await scanCorpusVersioned("L");

  // Simulate a brain process restart: a fresh module instance restarts the
  // version counter. Versions are seeded per process (time + random component)
  // so an old poed cache can never see its cached version echoed back by a
  // new process and keep pricing from a dead snapshot.
  vi.resetModules();
  const { initBrainData: reinitBrainData } = await import("../src/bootstrap");
  await reinitBrainData();
  const { scanCorpusVersioned: restartedScanCorpusVersioned } = await import(
    "../src/uniques"
  );
  const second = await restartedScanCorpusVersioned("L");

  expect(second.version).not.toBe(first.version);
});

it("a corpus built from degraded market data expires on the short retry TTL", async () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  const { scanCorpusVersioned } = await import("../src/uniques");

  scout.complete = false;
  const degraded = await scanCorpusVersioned("L");
  // Degraded market data still prices rows (stale beats "no market price").
  expect(degraded.rows["Mageblood"].priceAvailable).toBe(true);

  // Within the retry window: cached.
  vi.setSystemTime(Date.now() + 30_000);
  expect((await scanCorpusVersioned("L")).version).toBe(degraded.version);

  // Once the market recovers, the corpus rebuilds long before the normal
  // hourly TTL would have let it.
  scout.complete = true;
  vi.setSystemTime(Date.now() + 45_000);
  const rebuilt = await scanCorpusVersioned("L");
  expect(rebuilt.version).toBeGreaterThan(degraded.version);

  // And a complete corpus stays cached past the retry window.
  vi.setSystemTime(Date.now() + 75_000);
  expect((await scanCorpusVersioned("L")).version).toBe(rebuilt.version);
});

it("scanCorpus merges uniques and scout-priced tagged items", async () => {
  const { scanCorpus } = await import("../src/uniques");
  const out = await scanCorpus("L");

  expect(out["Mageblood"]).toMatchObject({
    price: 67305.6, quantity: 1386, kind: "unique",
    w: 2, h: 1, // parsed from the poecdn URL's base64 segment
    trend: 0.073,
    exaltedPerChaos: 0.25,
    exaltedPerDivine: 333,
  });
  // tagged trend derives from scout history (300 -> 333 = +11%)
  expect(out["Divine Orb"].trend).toBeCloseTo(0.11, 2);
  expect(out["Mageblood"].iconPath).toBeNull(); // offline test env
  // No base64 metadata in the URL -> 1x1 fallback.
  expect(out["Splinter of Loratta"]).toMatchObject({ w: 1, h: 1 });

  // "divine" tradeTag -> Divine Orb via REAL vendored data, scout-priced.
  expect(out["Divine Orb"]).toMatchObject({
    price: 333, quantity: 4444, kind: "tagged",
    sourceTag: "divine",
    sourceCategory: "currency",
  });

  // Live names and icons take precedence over stale vendored metadata.
  expect(out["Live Atziri's Allure"]).toMatchObject({
    price: 29,
    quantity: 24,
    kind: "tagged",
    exaltedPerChaos: 0.25,
    exaltedPerDivine: 333,
    quoteAmount: 1.5,
    quoteCurrency: "chaos",
    quoteCurrencyText: "Chaos Orb",
    quoteLiquidity: 200,
    quoteMaxStock: 50,
    sourceTag: "atziris-allure",
    sourceCategory: "lineagesupportgems",
  });
  expect(out["Atziri's Allure"]).toBeUndefined();

  // Live metadata covers newly added items absent from vendored EE2 data.
  expect(out["Swift Alloy"]).toMatchObject({
    price: 4.5, quantity: 455, kind: "tagged",
    sourceTag: "swift-alloy",
    sourceCategory: "verisium",
  });

  expect(out["Bitter Dead"]).toMatchObject({
    price: 0,
    quantity: 0,
    kind: "catalog",
    priceAvailable: false,
    iconPath: null,
  });

  const tagged = Object.values(out).filter((r) => r.kind === "tagged");
  expect(tagged).toHaveLength(4);
  expect(Object.keys(out).length).toBeGreaterThan(5);
});
