import { beforeAll, afterEach, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";
import { initBrainData } from "../src/bootstrap";
import { bulkSearch } from "../src/bulk";

// Mock the EE2 bulk API module so tests never hit the live exchange API.
// vi.mock is hoisted to the top of the module by vitest. execBulkSearch issues
// ONE network call and returns one entry per `have` (input order, null when
// deferred); the default mock returns a single entry for the single-have tests.
vi.mock("@/web/price-check/trade/pathofexile-bulk", () => ({
  execBulkSearch: vi.fn(async (_item, _filters, have: string[]) =>
    have.map((haveTag) => ({
      queryId: "test-query-id",
      haveTag,
      total: 42,
      listed: [
        {
          id: "uuid-1",
          relativeDate: "5 min. ago",
          exchangeAmount: 180,
          itemAmount: 1,
          stock: 540,
          isMine: false,
          accountName: "Bar#456",
          ign: "OtherChar",
          accountStatus: "online",
        },
      ],
    })),
  ),
}));

// EE2's bulk client (execBulkSearch) hits the live exchange API and has no
// separately-exposed pure body-construction step, so the only thing testable
// offline is the currency-tag -> ParsedItem resolution that precedes the call.
// A bad tag throws before any network access; a good tag would proceed to the
// rate-limited fetch (covered by the manual live check, not here).
beforeAll(initBrainData);

afterEach(() => {
  vi.unstubAllGlobals();
});

it("rejects an unknown currency tag before touching the network", async () => {
  await expect(bulkSearch("exalted", "not-a-currency", "Standard")).rejects.toThrow(
    'unknown currency tag: "not-a-currency"',
  );
});

it("rejects an unknown want tag the same way", async () => {
  // `have` is passed straight through to the query, but `want` is resolved
  // first, so a bad `want` is what we can catch offline.
  await expect(bulkSearch("exalted", "bogus", "Standard")).rejects.toThrow(
    "unknown currency tag",
  );
});

it("bulk result carries have/want icon paths", async () => {
  const dir = mkdtempSync(path.join(tmpdir(), "poed-bulk-icons-"));
  try {
    process.env.POE2_ICON_CACHE = dir;
    // Fail all icon fetches so resolveIcon returns null — tests must not
    // depend on network access; the assertion only checks the properties exist.
    vi.stubGlobal(
      "fetch",
      vi.fn(async () => {
        throw new Error("no network in tests");
      }),
    );
    const result = await bulkSearch("exalted", "divine", "Standard");
    expect(result.haveIconPath).toBeNull();
    expect(result.wantIconPath).toBeNull();
    expect(result.offers[0].exchangeAmount).toBe(180);
  } finally {
    rmSync(dir, { recursive: true, force: true });
    delete process.env.POE2_ICON_CACHE;
  }
});
