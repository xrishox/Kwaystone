import { afterEach, beforeEach, expect, it, vi } from "vitest";
import crypto from "node:crypto";
import net from "node:net";
import os from "node:os";
import path from "node:path";
import { mkdtemp } from "node:fs/promises";

let tmpConfigHome: string;

beforeEach(async () => {
  vi.resetModules();
  tmpConfigHome = await mkdtemp(path.join(os.tmpdir(), "waystone-ee2-test-"));
  vi.stubEnv("XDG_CONFIG_HOME", tmpConfigHome);
  vi.stubEnv("POE2_SESSID", "test-session-id");
});

afterEach(async () => {
  const { stopEe2Host } = await import("../src/ee2-host");
  await stopEe2Host();
  vi.unstubAllEnvs();
  vi.resetModules();
});

async function startHost() {
  const { startEe2Host } = await import("../src/ee2-host");
  const { url } = await startEe2Host({ league: "Standard" });
  const parsed = new URL(url);
  const token = parsed.searchParams.get("k");
  expect(token).toBeTruthy();
  return { url, token: token as string, port: parsed.port };
}

function cookieHeader(token: string) {
  return { cookie: `kwaystone_ee2=${token}` };
}

it("plants the auth cookie via ?k= redirect and gates all routes on it", async () => {
  const { url, token, port } = await startHost();

  // No cookie: every route is forbidden.
  const unauthed = await fetch(`http://127.0.0.1:${port}/config`);
  expect(unauthed.status).toBe(403);

  // Wrong token: forbidden, no cookie planted.
  const wrong = await fetch(`http://127.0.0.1:${port}/?k=nope`, {
    redirect: "manual",
  });
  expect(wrong.status).toBe(403);
  expect(wrong.headers.get("set-cookie")).toBeNull();

  // Right token: redirect + HttpOnly cookie.
  const bootstrap = await fetch(url, { redirect: "manual" });
  expect(bootstrap.status).toBe(302);
  const planted = bootstrap.headers.get("set-cookie") ?? "";
  expect(planted).toContain(`kwaystone_ee2=${token}`);
  expect(planted).toContain("HttpOnly");

  // With the cookie, /config answers.
  const authed = await fetch(`http://127.0.0.1:${port}/config`, {
    headers: cookieHeader(token),
  });
  expect(authed.status).toBe(200);
  const body = await authed.json();
  expect(body.version).toBe("Kwaystone");
});

it("proxy refuses non-HTTPS and non-allowlisted targets", async () => {
  const { token, port } = await startHost();
  const opts = { headers: cookieHeader(token) };

  // Plain HTTP is refused even for the real API host (cookie must never
  // travel in cleartext).
  const http = await fetch(
    `http://127.0.0.1:${port}/proxy/http://www.pathofexile.com/api/trade2/x`,
    opts,
  );
  expect(http.status).toBe(403);

  // Arbitrary hosts are refused (no open proxy / SSRF).
  for (const target of [
    "https://example.com/",
    "https://169.254.169.254/latest/meta-data",
    "https://pathofexile.com.evil.example/x",
    "https://www.pathofexile2.com/",
  ]) {
    const res = await fetch(
      `http://127.0.0.1:${port}/proxy/${target}`,
      opts,
    );
    expect(res.status, target).toBe(403);
  }

  // Unauthenticated callers are refused before any target handling.
  const noAuth = await fetch(
    `http://127.0.0.1:${port}/proxy/https://www.pathofexile.com/api/trade2/x`,
  );
  expect(noAuth.status).toBe(403);
});

function wsHandshake(port: string, headers: Record<string, string>): Promise<{
  socket: net.Socket;
  status: string;
}> {
  return new Promise((resolve, reject) => {
    const socket = net.connect(Number(port), "127.0.0.1");
    let buf = Buffer.alloc(0);
    const onData = (chunk: Buffer) => {
      buf = Buffer.concat([buf, chunk]);
      const text = buf.toString("latin1");
      if (text.includes("\r\n\r\n")) {
        socket.off("data", onData);
        resolve({ socket, status: text.split("\r\n")[0] });
      }
    };
    socket.on("data", onData);
    socket.on("error", reject);
    const key = crypto.randomBytes(16).toString("base64");
    const extra = Object.entries(headers)
      .map(([k, v]) => `${k}: ${v}\r\n`)
      .join("");
    socket.write(
      `GET /events HTTP/1.1\r\nHost: 127.0.0.1:${port}\r\n` +
        `Upgrade: websocket\r\nConnection: Upgrade\r\n` +
        `Sec-WebSocket-Key: ${key}\r\nSec-WebSocket-Version: 13\r\n` +
        `${extra}\r\n`,
    );
    setTimeout(() => {
      // Unauthenticated handshakes are destroyed without a response.
      resolve({ socket, status: "" });
    }, 300);
  });
}

it("websocket upgrade requires the auth cookie and same origin", async () => {
  const { token, port } = await startHost();

  // No cookie: socket destroyed, no 101.
  const noAuth = await wsHandshake(port, {});
  expect(noAuth.status).not.toContain("101");
  noAuth.socket.destroy();

  // Cookie but foreign Origin: rejected.
  const badOrigin = await wsHandshake(port, {
    Cookie: `kwaystone_ee2=${token}`,
    Origin: "http://evil.example",
  });
  expect(badOrigin.status).not.toContain("101");
  badOrigin.socket.destroy();

  // Cookie + same origin: upgraded.
  const good = await wsHandshake(port, {
    Cookie: `kwaystone_ee2=${token}`,
    Origin: `http://127.0.0.1:${port}`,
  });
  expect(good.status).toContain("101");
  good.socket.destroy();
});

function clientFrame(message: string): Buffer {
  const payload = Buffer.from(message, "utf8");
  const mask = crypto.randomBytes(4);
  let header: Buffer;
  if (payload.length < 126) {
    header = Buffer.from([0x81, 0x80 | payload.length]);
  } else {
    header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 0x80 | 126;
    header.writeUInt16BE(payload.length, 2);
  }
  for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
  return Buffer.concat([header, mask, payload]);
}

it("a malformed save-config cannot kill the brain", async () => {
  const { token, port } = await startHost();
  const { socket, status } = await wsHandshake(port, {
    Cookie: `kwaystone_ee2=${token}`,
  });
  expect(status).toContain("101");

  // Invalid JSON contents: saveConfig's JSON.parse must reject — caught and
  // logged, never an unhandled rejection.
  socket.write(
    clientFrame(
      JSON.stringify({
        name: "CLIENT->MAIN::save-config",
        payload: { contents: "{not-json" },
      }),
    ),
  );
  await new Promise((resolve) => setTimeout(resolve, 300));

  // The host is still alive and answers authenticated requests.
  const alive = await fetch(`http://127.0.0.1:${port}/config`, {
    headers: cookieHeader(token),
  });
  expect(alive.status).toBe(200);
  socket.destroy();
});

it("startEe2Host is idempotent and keeps the same token", async () => {
  const first = await startHost();
  const { startEe2Host } = await import("../src/ee2-host");
  const second = await startEe2Host({ league: "Standard" });
  expect(second.url).toBe(first.url);
});
