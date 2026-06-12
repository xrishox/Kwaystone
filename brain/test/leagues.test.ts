import { beforeEach, expect, it, vi } from "vitest";

const proxy = vi.fn();
vi.mock("../src/stubs/IPC", () => ({ Host: { proxy: (...a: any[]) => proxy(...a) } }));

beforeEach(() => {
  vi.resetModules();
  proxy.mockReset();
});

function ok(body: unknown): Response {
  return new Response(JSON.stringify(body), { status: 200 });
}

it("maps the trade2 roster to id/text entries", async () => {
  proxy.mockResolvedValueOnce(
    ok({ result: [{ id: "Standard" }, { id: "Runes of Aldur", text: "Runes of Aldur" }] }),
  );
  const { leagues } = await import("../src/leagues");

  expect(await leagues()).toEqual([
    { id: "Standard", text: "Standard" },
    { id: "Runes of Aldur", text: "Runes of Aldur" },
  ]);
  expect(proxy).toHaveBeenCalledWith("www.pathofexile.com/api/trade2/data/leagues");
});

it("caches the roster for the process lifetime", async () => {
  proxy.mockResolvedValueOnce(ok({ result: [{ id: "Standard" }] }));
  const { leagues } = await import("../src/leagues");

  await leagues();
  await leagues();

  expect(proxy).toHaveBeenCalledTimes(1);
});

it("throws on http error and on empty roster", async () => {
  proxy.mockResolvedValueOnce(new Response("nope", { status: 503 }));
  const { leagues } = await import("../src/leagues");
  await expect(leagues()).rejects.toThrow("503");

  proxy.mockResolvedValueOnce(ok({ result: [] }));
  await expect(leagues()).rejects.toThrow("empty");
});
