import { afterEach, beforeEach, expect, it, vi } from "vitest";

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

beforeEach(() => {
  vi.resetModules();
});

it("Host.proxy sends POESESSID cookie only for pathofexile.com (not pathofexile2.com) hosts", async () => {
  const seen: Record<string, unknown> = {};
  vi.stubGlobal(
    "fetch",
    vi.fn(async (_url: string, init: RequestInit) => {
      Object.assign(seen, init.headers);
      return new Response("{}");
    }),
  );
  const { setBrainConfig } = await import("../src/stubs/Config");
  const { Host } = await import("../src/stubs/IPC");

  // Cookie present for www.pathofexile.com (the real API domain).
  setBrainConfig({ sessionId: "abc123" });
  await Host.proxy("www.pathofexile.com/api/trade2/x", {});
  expect(seen["Cookie"]).toBe("POESESSID=abc123");

  // Cookie must be ABSENT for www.pathofexile2.com — that SPA has no API
  // and its sessions are foreign to pathofexile.com.
  for (const k of Object.keys(seen)) delete seen[k];
  await Host.proxy("www.pathofexile2.com/api/profile", {});
  expect(seen["Cookie"]).toBeUndefined();

  // header absent when not configured
  for (const k of Object.keys(seen)) delete seen[k];
  setBrainConfig({ sessionId: undefined });
  await Host.proxy("www.pathofexile.com/api/x", {});
  expect(seen["Cookie"]).toBeUndefined();
});

it("cookieAllowedForHost rejects lookalike and foreign hosts", async () => {
  const { cookieAllowedForHost } = await import("../src/session-headers");

  // Exact domain and API subdomains only.
  expect(cookieAllowedForHost("pathofexile.com")).toBe(true);
  expect(cookieAllowedForHost("www.pathofexile.com")).toBe(true);
  expect(cookieAllowedForHost("API.PathOfExile.com".toLowerCase())).toBe(true);

  // Lookalike domains must never receive the session cookie.
  expect(cookieAllowedForHost("pathofexile.com.evil.example")).toBe(false);
  expect(cookieAllowedForHost("evil-pathofexile.com")).toBe(false);
  expect(cookieAllowedForHost("pathofexile.com.evil.com")).toBe(false);
  expect(cookieAllowedForHost("notpathofexile.com")).toBe(false);

  // The poe2 SPA domain and unrelated hosts stay anonymous.
  expect(cookieAllowedForHost("pathofexile2.com")).toBe(false);
  expect(cookieAllowedForHost("www.pathofexile2.com")).toBe(false);
  expect(cookieAllowedForHost("poe2scout.com")).toBe(false);
});
