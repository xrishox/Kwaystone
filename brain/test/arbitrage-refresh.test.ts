import { beforeEach, expect, it, vi } from "vitest";

const scout = vi.hoisted(() => ({
  epoch: vi.fn<() => Promise<string | null>>(),
  snapshot: vi.fn<() => Promise<{
    pairs: unknown[];
    epoch: string;
    snapshotId?: number;
    fetchedAt: number;
  }>>(),
}));

vi.mock("../src/poe2scout", () => ({
  exchangeSnapshotEpoch: scout.epoch,
  exchangePairSnapshot: scout.snapshot,
}));

import {
  _clearArbCache,
  arbAnalyze,
  arbPair,
} from "../src/arbitrage";

const PAIRS = [
  {
    Volume: 100,
    CurrencyOne: { ApiId: "chaos", Text: "Chaos Orb", CategoryApiId: "currency" },
    CurrencyTwo: { ApiId: "exalted", Text: "Exalted Orb", CategoryApiId: "currency" },
    CurrencyOneData: { VolumeTraded: 15 },
    CurrencyTwoData: { VolumeTraded: 1 },
  },
  {
    Volume: 10,
    CurrencyOne: { ApiId: "omen", Text: "Omen", CategoryApiId: "omens" },
    CurrencyTwo: { ApiId: "chaos", Text: "Chaos Orb", CategoryApiId: "currency" },
    CurrencyOneData: { VolumeTraded: 1 },
    CurrencyTwoData: { VolumeTraded: 81 },
  },
  {
    Volume: 100,
    CurrencyOne: { ApiId: "divine", Text: "Divine Orb", CategoryApiId: "currency" },
    CurrencyTwo: { ApiId: "exalted", Text: "Exalted Orb", CategoryApiId: "currency" },
    CurrencyOneData: { VolumeTraded: 1 },
    CurrencyTwoData: { VolumeTraded: 400 },
  },
  {
    Volume: 100,
    CurrencyOne: {
      ApiId: "fracturing-orb",
      Text: "Fracturing Orb",
      CategoryApiId: "currency",
    },
    CurrencyTwo: { ApiId: "divine", Text: "Divine Orb", CategoryApiId: "currency" },
    CurrencyOneData: { VolumeTraded: 2 },
    CurrencyTwoData: { VolumeTraded: 3 },
  },
];

beforeEach(() => {
  _clearArbCache();
  scout.epoch.mockReset();
  scout.snapshot.mockReset();
  scout.epoch.mockResolvedValue("epoch-1");
  scout.snapshot.mockResolvedValue({
    pairs: PAIRS,
    epoch: "epoch-1",
    snapshotId: 101,
    fetchedAt: Date.now(),
  });
});

async function resolvePair(forceRates: boolean) {
  return arbPair({
    league: "Test",
    wantText: "Chaos Orb",
    haveText: "Omen",
    wantAmount: 81,
    haveAmount: 1,
    forceRates,
  });
}

async function analyze(forceRates = false, reuseRates = false) {
  return arbAnalyze({
    league: "Test",
    targetApiId: "omen",
    observations: [],
    forceRates,
    reuseRates,
  });
}

it("Alt+S force refreshes the consistent pair snapshot on every press", async () => {
  await resolvePair(true);
  await resolvePair(true);

  expect(scout.epoch).not.toHaveBeenCalled();
  expect(scout.snapshot).toHaveBeenCalledTimes(2);
  expect(scout.snapshot).toHaveBeenNthCalledWith(1, "Test", { force: true });
  expect(scout.snapshot).toHaveBeenNthCalledWith(2, "Test", { force: true });
});

it("accepts a verified exchange item that is newer than the poe2scout catalog", async () => {
  const result = await arbPair({
    league: "Test",
    wantText: "Divine Orb",
    haveText: "丝Raven's Reflection*",
    wantAmount: 1,
    haveAmount: 2.1,
    forceRates: true,
  });

  expect(result.observation.have).toMatchObject({
    apiId: "observed:raven-s-reflection",
    name: "Raven's Reflection",
    category: "observed-exchange",
    isCurrency: false,
  });
  expect(result.observation.want.apiId).toBe("divine");
});

it("classifies non-core orbs from the catalog as currencies", async () => {
  const result = await arbPair({
    league: "Test",
    wantText: "Divine Orb",
    haveText: "Fracturing Orb",
    wantAmount: 3,
    haveAmount: 2,
    forceRates: true,
  });

  expect(result.observation.have).toMatchObject({
    apiId: "fracturing-orb",
    category: "currency",
    isCurrency: true,
  });
  expect(result.observation.want.isCurrency).toBe(true);
});

it("reuses a session identity for later OCR variants of an observed item", async () => {
  const first = await arbPair({
    league: "Test",
    wantText: "Divine Orb",
    haveText: "丝Raven's Reflection*",
    wantAmount: 1,
    haveAmount: 2.1,
    forceRates: true,
  });
  const result = await arbPair({
    league: "Test",
    wantText: "Chaos Orb",
    haveText: "Raven's Reflectlon",
    wantAmount: 100,
    haveAmount: 1,
    knownItems: [first.observation.have],
  });

  expect(result.observation.have.apiId).toBe("observed:raven-s-reflection");
  expect(result.observation.have.name).toBe("Raven's Reflection");
});

it("does not create an observed identity from vague OCR", async () => {
  await expect(
    arbPair({
      league: "Test",
      wantText: "Divine Orb",
      haveText: "Orb",
      wantAmount: 1,
      haveAmount: 1,
      forceRates: true,
    }),
  ).rejects.toThrow(/ambiguous Currency Exchange item/);
});

it("reuses one bulk snapshot while the exchange epoch is unchanged", async () => {
  await resolvePair(true);
  await analyze();

  expect(scout.epoch).toHaveBeenCalledTimes(1);
  expect(scout.snapshot).toHaveBeenCalledTimes(1);
});

it("refreshes the bulk snapshot when the exchange epoch changes", async () => {
  scout.epoch.mockResolvedValueOnce("epoch-2");
  scout.snapshot
    .mockResolvedValueOnce({ pairs: PAIRS, epoch: "epoch-1", fetchedAt: Date.now() })
    .mockResolvedValueOnce({ pairs: PAIRS, epoch: "epoch-2", fetchedAt: Date.now() });
  await resolvePair(true);
  const result = await analyze();

  expect(scout.snapshot).toHaveBeenCalledTimes(2);
  expect(result.ratesEpoch).toBe("epoch-2");
});

it("marks recent cached data degraded when the epoch probe fails", async () => {
  await resolvePair(true);
  scout.epoch.mockRejectedValueOnce(new Error("epoch unavailable"));
  const result = await analyze();

  expect(scout.snapshot).toHaveBeenCalledTimes(1);
  expect(result.ratesStatus).toBe("degraded");
});

it("a failed forced analysis falls back to the cached book as degraded", async () => {
  await resolvePair(true);
  scout.snapshot.mockRejectedValueOnce(new Error("pairs unavailable"));
  const result = await analyze(true);

  expect(scout.snapshot).toHaveBeenCalledTimes(2);
  expect(result.ratesStatus).toBe("degraded");
});

it("reuses the current rate book for buffer-only UI analysis", async () => {
  await resolvePair(true);
  await analyze(false, true);

  expect(scout.epoch).not.toHaveBeenCalled();
  expect(scout.snapshot).toHaveBeenCalledTimes(1);
});
