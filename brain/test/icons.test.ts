import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { mkdtempSync, rmSync, writeFileSync, existsSync, readFileSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

let dir: string;
beforeEach(() => {
  dir = mkdtempSync(path.join(tmpdir(), "poed-icons-"));
  process.env.POE2_ICON_CACHE = dir;
  vi.resetModules();
});
afterEach(() => {
  rmSync(dir, { recursive: true, force: true });
  delete process.env.POE2_ICON_CACHE;
  vi.unstubAllGlobals();
});

const URL1 = "https://web.poecdn.com/gen/image/abc/Foo.png";

describe("resolveIcon", () => {
  it("downloads and caches on miss", async () => {
    const fetchSpy = vi.fn(async () => new Response(Buffer.from("png-bytes")));
    vi.stubGlobal("fetch", fetchSpy);
    const { resolveIcon } = await import("../src/icons");
    const p = await resolveIcon(URL1);
    expect(p).not.toBeNull();
    expect(existsSync(p!)).toBe(true);
    expect(readFileSync(p!, "utf8")).toBe("png-bytes");
    expect(fetchSpy).toHaveBeenCalledTimes(1);
    expect(fetchSpy).toHaveBeenCalledWith(URL1);
  });

  it("returns cached path without fetching", async () => {
    const { resolveIcon, iconCachePath } = await import("../src/icons");
    writeFileSync(iconCachePath(URL1), "already-here");
    const fetchSpy = vi.fn();
    vi.stubGlobal("fetch", fetchSpy);
    const p = await resolveIcon(URL1);
    expect(p).toBe(iconCachePath(URL1));
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it("returns null on fetch failure", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => { throw new Error("offline"); }));
    const { resolveIcon } = await import("../src/icons");
    expect(await resolveIcon(URL1)).toBeNull();
  });

  it("returns null when slower than the timeout", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {}))); // never resolves
    const { resolveIcon } = await import("../src/icons");
    expect(await resolveIcon(URL1, 50)).toBeNull();
  });

  it("returns null for undefined url", async () => {
    const { resolveIcon } = await import("../src/icons");
    expect(await resolveIcon(undefined)).toBeNull();
  });

  it("dedups concurrent requests for the same url", async () => {
    const fetchSpy = vi.fn(
      async () => new Response(Buffer.from("png-bytes")),
    );
    vi.stubGlobal("fetch", fetchSpy);
    const { resolveIcon } = await import("../src/icons");
    const [a, b] = await Promise.all([resolveIcon(URL1), resolveIcon(URL1)]);
    expect(a).toBe(b);
    expect(fetchSpy).toHaveBeenCalledTimes(1);
  });
});
