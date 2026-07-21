import { afterAll, beforeAll, expect, it, vi } from "vitest";
import net from "node:net";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { startServer } from "../src/server";

// The uniqueprices handler and the background refresher both import
// ./uniques dynamically; mock it so the handshake test needs no network
// (same pattern as test/uniques.test.ts mocking ../src/poe2scout).
vi.mock("../src/uniques", () => ({
  scanCorpusVersioned: vi.fn(async () => ({
    version: 7,
    rows: { Mageblood: { price: 67305.6 } },
  })),
  refreshScanCorpus: vi.fn(async () => {}),
}));

const SOCK = path.join(os.tmpdir(), `brain-test-${process.pid}.sock`);
let close: () => Promise<void>;
beforeAll(async () => {
  close = await startServer(SOCK);
});
afterAll(() => close());

function rpc(msg: object): Promise<any> {
  return new Promise((resolve, reject) => {
    const c = net.connect(SOCK, () => c.write(JSON.stringify(msg) + "\n"));
    let buf = "";
    c.on("data", (d) => {
      buf += d;
      const nl = buf.indexOf("\n");
      if (nl >= 0) {
        c.end();
        resolve(JSON.parse(buf.slice(0, nl)));
      }
    });
    c.on("error", reject);
  });
}

function post(url: URL, body: string, headers: Record<string, string>) {
  return new Promise<{ status: number; headers: http.IncomingHttpHeaders; body: string }>(
    (resolve, reject) => {
      const req = http.request(
        url,
        { method: "POST", headers },
        (res) => {
          let responseBody = "";
          res.setEncoding("utf8");
          res.on("data", (chunk) => {
            responseBody += chunk;
          });
          res.on("end", () => {
            resolve({
              status: res.statusCode ?? 0,
              headers: res.headers,
              body: responseBody,
            });
          });
        },
      );
      req.on("error", reject);
      req.end(body);
    },
  );
}

async function consumeBody(body: unknown): Promise<string> {
  if (!body || typeof (body as any)[Symbol.asyncIterator] !== "function") {
    return "";
  }
  const chunks: Buffer[] = [];
  for await (const chunk of body as AsyncIterable<Buffer | Uint8Array | string>) {
    chunks.push(Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk));
  }
  return Buffer.concat(chunks).toString("utf8");
}

it("ping", async () => {
  expect(await rpc({ id: 1, cmd: "ping" })).toEqual({
    id: 1,
    ok: true,
    result: "pong",
  });
});
it("uniqueprices ifVersion handshake: rows first, then unchanged", async () => {
  const first = await rpc({ id: 2, cmd: "uniqueprices", league: "L", ifVersion: 0 });
  expect(first).toEqual({
    id: 2,
    ok: true,
    result: { version: 7, rows: { Mageblood: { price: 67305.6 } } },
  });

  const again = await rpc({
    id: 8,
    cmd: "uniqueprices",
    league: "L",
    ifVersion: first.result.version,
  });
  expect(again).toEqual({
    id: 8,
    ok: true,
    result: { version: 7, unchanged: true },
  });
});
it("unknown cmd errors cleanly", async () => {
  const res = await rpc({ id: 3, cmd: "nope" });
  expect(res).toMatchObject({ id: 3, ok: false });
});
it("starts EE2 host and reports selected config", async () => {
  const host = await rpc({
    id: 4,
    cmd: "ee2host",
    league: "Runes of Aldur",
    accountName: "acct",
  });
  expect(host).toMatchObject({ id: 4, ok: true });
  expect(host.result.url).toMatch(/^http:\/\/127\.0\.0\.1:\d+\/\?k=.+$/);

  const config = await rpc({ id: 5, cmd: "ee2config" });
  expect(config).toMatchObject({
    id: 5,
    ok: true,
    result: { league: "Runes of Aldur", accountName: "acct" },
  });

  const state = await rpc({ id: 6, cmd: "ee2state" });
  expect(state).toMatchObject({
    id: 6,
    ok: true,
    result: { hideRequestSeq: expect.any(Number) },
  });
});

it("EE2 proxy forwards JSON request headers", async () => {
  const oldFetch = globalThis.fetch;
  let seenUrl = "";
  let seenMethod = "";
  let seenHeaders = new Headers();
  let seenBody = "";
  globalThis.fetch = (async (input, init) => {
    seenUrl = String(input);
    seenMethod = String(init?.method ?? "GET");
    seenHeaders = new Headers(init?.headers);
    seenBody = await consumeBody(init?.body);
    return new Response(JSON.stringify({ ok: true }), {
      status: 200,
      headers: { "content-type": "application/json" },
    });
  }) as typeof fetch;

  try {
    const host = await rpc({
      id: 7,
      cmd: "ee2host",
      league: "Runes of Aldur",
      accountName: "acct",
    });
    // The panel URL carries the auth token; every route requires the cookie.
    const token = new URL(host.result.url).searchParams.get("k");
    expect(token).toBeTruthy();
    const res = await post(
      new URL(
        "proxy/www.pathofexile.com/api/trade2/search/Runes%20of%20Aldur",
        host.result.url,
      ),
      JSON.stringify({ query: {}, sort: { price: "asc" } }),
      {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Cookie": `kwaystone_ee2=${token}; local-cookie-must-not-leak=yes`,
      },
    );

    expect(res.status).toBe(200);
    expect(JSON.parse(res.body)).toEqual({ ok: true });
    expect(seenUrl).toBe(
      "https://www.pathofexile.com/api/trade2/search/Runes%20of%20Aldur",
    );
    expect(seenMethod).toBe("POST");
    expect(seenHeaders.get("accept")).toBe("application/json");
    expect(seenHeaders.get("content-type")).toBe("application/json");
    expect(seenHeaders.get("cookie")).toBeNull();
    expect(seenBody).toBe(JSON.stringify({ query: {}, sort: { price: "asc" } }));
  } finally {
    globalThis.fetch = oldFetch;
  }
});
