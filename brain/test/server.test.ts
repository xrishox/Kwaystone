import { afterAll, beforeAll, expect, it } from "vitest";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { startServer } from "../src/server";

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

it("ping", async () => {
  expect(await rpc({ id: 1, cmd: "ping" })).toEqual({
    id: 1,
    ok: true,
    result: "pong",
  });
});
it("parse magic wand", async () => {
  const { readFileSync } = await import("node:fs");
  const text = readFileSync(
    new URL("./fixtures/magic-wand.txt", import.meta.url),
    "utf8",
  );
  const res = await rpc({ id: 2, cmd: "parse", clipboard: text });
  expect(res.ok).toBe(true);
  expect(res.result.rarity).toBe("Magic");
});
it("unknown cmd errors cleanly", async () => {
  const res = await rpc({ id: 3, cmd: "nope" });
  expect(res).toMatchObject({ id: 3, ok: false });
});
it("requery on a non-item returns an error envelope", async () => {
  const res = await rpc({
    id: 5,
    cmd: "requery",
    clipboard: "garbage clipboard",
    overrides: [],
  });
  expect(res).toMatchObject({ id: 5, ok: false });
  expect(res.error).toMatch(/not an item/);
});
it("bulk missing have/want returns validation error", async () => {
  const res = await rpc({ id: 4, cmd: "bulk" });
  expect(res).toMatchObject({ id: 4, ok: false });
  expect(res.error).toMatch(/requires/);
});
