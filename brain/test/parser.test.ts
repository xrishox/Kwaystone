import { beforeAll, describe, expect, it } from "vitest";
import { readFileSync, readdirSync } from "node:fs";
import { initBrainData } from "../src/bootstrap";

beforeAll(initBrainData);

describe("parseClipboard golden fixtures", () => {
  for (const f of readdirSync(new URL("./fixtures", import.meta.url))) {
    it(`parses ${f}`, async () => {
      const { parseClipboard } = await import("@/parser");
      const text = readFileSync(
        new URL(`./fixtures/${f}`, import.meta.url),
        "utf8",
      );
      const r = parseClipboard(text);
      expect(r.isOk(), r.isErr() ? String(r.error) : "").toBe(true);
      expect(r._unsafeUnwrap()).toMatchSnapshot();
    });
  }
});
