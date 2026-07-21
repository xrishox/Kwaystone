import { afterEach, beforeEach, expect, it, vi } from "vitest";

// Mock the shared fetch proxy so tests never touch the live poe2scout API.
const proxy = vi.fn();
vi.mock("../src/stubs/IPC", () => ({
  Host: { proxy: (...a: any[]) => proxy(...a), isElectron: false },
}));

import { leagueList, _clearCache } from "../src/poe2scout";

const LEAGUES = [
  { Value: "Dawn of the Hunt", IsCurrent: false, DivinePrice: 1828 },
  { Value: "Standard", IsCurrent: false, DivinePrice: 238 },
  { Value: "Hardcore", IsCurrent: false, DivinePrice: 82 },
  { Value: "Fate of the Vaal", IsCurrent: false, DivinePrice: 187 },
  { Value: "Runes of Aldur", IsCurrent: true, DivinePrice: 428 },
  { Value: "HC Runes of Aldur", IsCurrent: true, DivinePrice: 398 },
];

function json(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

beforeEach(() => {
  _clearCache();
  proxy.mockReset();
  proxy.mockImplementation(async () => json(LEAGUES));
});

afterEach(() => {
  _clearCache();
  vi.useRealTimers();
});

it("returns permanent + current leagues only, HC variants included", async () => {
  const { leagues } = await leagueList();

  const names = leagues.map((l) => l.name);
  expect(names).toEqual([
    "Standard",
    "Hardcore",
    "Runes of Aldur",
    "HC Runes of Aldur",
  ]);
  // Dead leagues are never listed.
  expect(names).not.toContain("Dawn of the Hunt");
  expect(names).not.toContain("Fate of the Vaal");
  expect(leagues.find((l) => l.name === "Standard")?.permanent).toBe(true);
  expect(leagues.find((l) => l.name === "Runes of Aldur")?.current).toBe(true);
});

it("serves the cached list within the TTL without refetching", async () => {
  await leagueList();
  await leagueList();
  expect(proxy).toHaveBeenCalledTimes(1);
});

it("force refresh bypasses the cache TTL", async () => {
  await leagueList();
  await leagueList({ force: true });
  expect(proxy).toHaveBeenCalledTimes(2);
});

it("a failed refresh serves the last-good list instead of throwing", async () => {
  const first = await leagueList();
  proxy.mockImplementation(async () => {
    throw new Error("offline");
  });

  const second = await leagueList({ force: true });
  expect(second).toEqual(first);
});

it("a failed first fetch has no stale list to serve and throws", async () => {
  proxy.mockImplementation(async () => {
    throw new Error("offline");
  });
  await expect(leagueList()).rejects.toThrow("offline");
});
