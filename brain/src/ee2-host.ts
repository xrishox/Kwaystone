import http, { IncomingMessage, ServerResponse } from "node:http";
import crypto from "node:crypto";
import { createReadStream } from "node:fs";
import { mkdir, readFile, stat, writeFile } from "node:fs/promises";
import net from "node:net";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { WAYSTONE_USER_AGENT, cookieAllowedForHost } from "./session-headers";

type Ee2HostOptions = {
  league?: string;
  accountName?: string;
  sessionId?: string;
};

type Ee2HostState = {
  server: http.Server;
  port: number;
  sockets: Set<net.Socket>;
  configPath: string;
};

let host: Ee2HostState | null = null;
let lastItemTextEvent: unknown | null = null;
let hideRequestSeq = 0;
let hideReason = "";
let defaults: Required<Ee2HostOptions> = {
  league: process.env.POE2_LEAGUE ?? "Standard",
  accountName: process.env.POE2_ACCOUNT ?? "",
  sessionId: process.env.POE2_SESSID ?? "",
};

const MIME: Record<string, string> = {
  ".html": "text/html; charset=utf-8",
  ".js": "text/javascript; charset=utf-8",
  ".mjs": "text/javascript; charset=utf-8",
  ".css": "text/css; charset=utf-8",
  ".json": "application/json; charset=utf-8",
  ".svg": "image/svg+xml",
  ".png": "image/png",
  ".jpg": "image/jpeg",
  ".jpeg": "image/jpeg",
  ".webp": "image/webp",
  ".ico": "image/x-icon",
  ".woff": "font/woff",
  ".woff2": "font/woff2",
  ".ttf": "font/ttf",
  ".bin": "application/octet-stream",
  ".ndjson": "application/x-ndjson",
};

function debug(message: string): void {
  if (process.env.WAYSTONE_DEBUG) console.error(message);
}

function moduleDir(): string {
  return path.dirname(fileURLToPath(import.meta.url));
}

function rendererDist(): string {
  return path.join(moduleDir(), "../vendor/ee2/renderer/dist");
}

function configPath(): string {
  const base =
    process.env.XDG_CONFIG_HOME ??
    path.join(process.env.HOME ?? process.cwd(), ".config");
  return path.join(base, "waystone", "ee2-price-check.json");
}

function defaultConfig() {
  return {
    configVersion: 34,
    leagueId: defaults.league,
    overlayKey: "Alt+Z",
    overlayBackground: "rgba(0, 0, 0, 0)",
    overlayBackgroundClose: true,
    overlayAlwaysClose: false,
    restoreClipboard: false,
    commands: [],
    clientLog: null,
    gameConfig: null,
    windowTitle: "Path of Exile 2",
    logKeys: false,
    accountName: defaults.accountName,
    stashScroll: false,
    language: "en",
    preferredTradeSite: "www",
    realm: "pc-ggg",
    fontSize: 16,
    showAttachNotification: false,
    enableAlphas: false,
    alphas: [],
    tipsFrequency: 3,
    readClientLog: false,
    widgets: [
      {
        wmId: 1,
        wmType: "menu",
        wmTitle: "",
        wmWants: "hide",
        wmZorder: 1,
        wmFlags: ["invisible-on-blur", "menu::skip"],
        anchor: { pos: "tl", x: 5, y: 5 },
        alwaysShow: false,
      },
      {
        wmId: 2,
        wmType: "settings",
        wmTitle: "{icon=fa-cog}",
        wmWants: "hide",
        wmZorder: "exclusive",
        wmFlags: ["invisible-on-blur", "ignore-ui-visibility"],
      },
      {
        wmId: 3,
        wmType: "price-check",
        wmTitle: "",
        wmWants: "hide",
        wmZorder: "exclusive",
        wmFlags: ["hide-on-blur", "menu::skip"],
        showRateLimitState: false,
        apiLatencySeconds: 2,
        collapseListings: "api",
        smartInitialSearch: true,
        lockedInitialSearch: true,
        activateStockFilter: false,
        builtinBrowser: false,
        hotkey: "Z",
        hotkeyHold: "Alt",
        hotkeyLocked: "Alt + Shift + Z",
        showSeller: "account",
        searchStatRange: 10,
        showCursor: true,
        requestPricePrediction: false,
        rememberCurrency: false,
        defaultAllSelected: false,
        itemHoverTooltip: "keybind",
        autoFillEmptyRuneSockets: false,
        alwaysShowTier: false,
        openItemEditorAbove: false,
        coreCurrency: "exalted",
        currencyVolume: "both",
        rememberListingType: false,
        initialDelay: 48,
      },
    ],
  };
}

async function loadConfig(): Promise<string> {
  const p = host?.configPath ?? configPath();
  try {
    return await readFile(p, "utf8");
  } catch {
    return JSON.stringify(defaultConfig());
  }
}

async function saveConfig(contents: string): Promise<void> {
  const p = host?.configPath ?? configPath();
  JSON.parse(contents);
  await mkdir(path.dirname(p), { recursive: true, mode: 0o700 });
  await writeFile(p, contents, { mode: 0o600 });
  updateDefaultsFromConfig(contents);
  broadcast({
    name: "MAIN->CLIENT::config-changed",
    payload: { contents },
  });
}

function updateDefaultsFromConfig(contents: string): void {
  try {
    const cfg = JSON.parse(contents);
    if (typeof cfg.leagueId === "string" && cfg.leagueId.trim()) {
      defaults.league = cfg.leagueId;
    }
    if (typeof cfg.accountName === "string") {
      defaults.accountName = cfg.accountName;
    }
  } catch {
    // Ignore invalid transient state; saveConfig validates before writing.
  }
}

async function handleConfig(res: ServerResponse): Promise<void> {
  const contents = await loadConfig();
  updateDefaultsFromConfig(contents);
  json(res, {
    contents,
    version: "Kwaystone",
    updater: { state: "initial" },
  });
}

async function handleProxy(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const started = Date.now();
  const raw = req.url?.slice("/proxy/".length) ?? "";
  if (!raw) {
    res.writeHead(400).end("missing proxy target");
    return;
  }
  const target = raw.startsWith("http://") || raw.startsWith("https://")
    ? raw
    : `https://${raw}`;
  const requestHeaders = proxyRequestHeaders(req, target);
  const upstream = await fetch(target, {
    method: req.method,
    body: req.method === "GET" || req.method === "HEAD" ? undefined : req,
    headers: requestHeaders,
    duplex: "half",
  } as RequestInit & { duplex?: "half" });
  debug(
    [
      "ee2 proxy",
      req.method ?? "GET",
      proxyLogTarget(target),
      `status=${upstream.status}`,
      `type=${upstream.headers.get("content-type") ?? ""}`,
      `elapsed=${Date.now() - started}ms`,
    ].join(" "),
  );

  const headers: Record<string, string> = {};
  upstream.headers.forEach((value, key) => {
    if (
      key === "content-encoding" ||
      key === "transfer-encoding" ||
      key === "connection"
    ) {
      return;
    }
    headers[key] = value;
  });
  res.writeHead(upstream.status, headers);
  if (!upstream.body) {
    res.end();
    return;
  }
  const reader = upstream.body.getReader();
  try {
    for (;;) {
      const { done, value } = await reader.read();
      if (done) break;
      res.write(Buffer.from(value));
    }
  } finally {
    res.end();
  }
}

const HOP_BY_HOP_REQUEST_HEADERS = new Set([
  "connection",
  "host",
  "keep-alive",
  "proxy-authenticate",
  "proxy-authorization",
  "te",
  "trailer",
  "transfer-encoding",
  "upgrade",
  "content-length",
  "cookie",
  "origin",
  "referer",
  "accept-encoding",
]);

function proxyRequestHeaders(
  req: IncomingMessage,
  target: string,
): Record<string, string> {
  const headers: Record<string, string> = {
    "user-agent": WAYSTONE_USER_AGENT,
    ...cookieHeader(target),
  };
  for (const [name, rawValue] of Object.entries(req.headers)) {
    const key = name.toLowerCase();
    if (HOP_BY_HOP_REQUEST_HEADERS.has(key) || rawValue == null) {
      continue;
    }
    headers[key] = Array.isArray(rawValue) ? rawValue.join(", ") : rawValue;
  }
  return headers;
}

function proxyLogTarget(target: string): string {
  try {
    const url = new URL(target);
    return `${url.hostname}${url.pathname}`;
  } catch {
    return "<invalid-url>";
  }
}

function cookieHeader(target: string): Record<string, string> {
  if (!defaults.sessionId) return {};
  if (cookieAllowedForHost(new URL(target).hostname)) {
    return { cookie: `POESESSID=${defaults.sessionId}` };
  }
  return {};
}

async function handleStatic(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const root = rendererDist();
  const urlPath = decodeURIComponent((req.url ?? "/").split("?")[0] || "/");
  const rel = urlPath === "/" ? "index.html" : urlPath.replace(/^\/+/, "");
  const file = path.normalize(path.join(root, rel));
  if (!file.startsWith(root + path.sep)) {
    res.writeHead(403).end("forbidden");
    return;
  }
  try {
    const s = await stat(file);
    if (!s.isFile()) throw new Error("not a file");
    res.writeHead(200, {
      "content-type": MIME[path.extname(file)] ?? "application/octet-stream",
      "content-length": String(s.size),
    });
    createReadStream(file).pipe(res);
  } catch {
    res.writeHead(404).end("not found");
  }
}

async function requestHandler(req: IncomingMessage, res: ServerResponse): Promise<void> {
  try {
    if (req.url === "/config") return await handleConfig(res);
    if (req.url === "/client-error" && req.method === "POST") {
      return await handleClientError(req, res);
    }
    if (req.url?.startsWith("/proxy/")) return await handleProxy(req, res);
    if (req.url?.startsWith("/uploads/")) {
      res.writeHead(404).end("uploads are not supported");
      return;
    }
    return await handleStatic(req, res);
  } catch (e) {
    console.error("ee2 host request failed:", e instanceof Error ? e.stack ?? e.message : e);
    res.writeHead(500).end(String(e instanceof Error ? e.message : e));
  }
}

async function readRequestBody(req: IncomingMessage, maxBytes = 64 * 1024): Promise<string> {
  const chunks: Buffer[] = [];
  let size = 0;
  for await (const chunk of req) {
    const buf = Buffer.isBuffer(chunk) ? chunk : Buffer.from(chunk);
    size += buf.length;
    if (size > maxBytes) throw new Error("request body too large");
    chunks.push(buf);
  }
  return Buffer.concat(chunks).toString("utf8");
}

async function handleClientError(req: IncomingMessage, res: ServerResponse): Promise<void> {
  const body = await readRequestBody(req);
  console.error(`ee2 renderer startup error: ${body.slice(0, 4000)}`);
  res.writeHead(204).end();
}

function json(res: ServerResponse, body: unknown): void {
  const data = JSON.stringify(body);
  res.writeHead(200, {
    "content-type": "application/json; charset=utf-8",
    "content-length": Buffer.byteLength(data),
  });
  res.end(data);
}

function handleUpgrade(req: IncomingMessage, socket: net.Socket): void {
  if (req.url !== "/events") {
    socket.destroy();
    return;
  }
  const key = req.headers["sec-websocket-key"];
  if (typeof key !== "string") {
    socket.destroy();
    return;
  }
  const accept = crypto
    .createHash("sha1")
    .update(`${key}258EAFA5-E914-47DA-95CA-C5AB0DC85B11`)
    .digest("base64");
  socket.write(
    [
      "HTTP/1.1 101 Switching Protocols",
      "Upgrade: websocket",
      "Connection: Upgrade",
      `Sec-WebSocket-Accept: ${accept}`,
      "\r\n",
    ].join("\r\n"),
  );
  host?.sockets.add(socket);
  debug("ee2 websocket connected");
  socket.on("data", (chunk) => {
    for (const message of decodeClientFrames(chunk)) {
      void handleClientEvent(message, socket);
    }
  });
  socket.on("close", () => host?.sockets.delete(socket));
  socket.on("error", () => host?.sockets.delete(socket));
  scheduleLastItemText(socket);
}

async function handleClientEvent(message: string, socket?: net.Socket): Promise<void> {
  let event: any;
  try {
    event = JSON.parse(message);
  } catch {
    return;
  }
  if (event?.name === "CLIENT->MAIN::save-config") {
    const contents = event.payload?.contents;
    if (typeof contents === "string") await saveConfig(contents);
    return;
  }
  if (event?.name === "OVERLAY->MAIN::focus-game") {
    requestNativeOverlayHide("focus-game");
    return;
  }
  if (
    event?.name === "CLIENT->MAIN::user-action" &&
    event.payload?.action === "kwaystone-hide-price-check"
  ) {
    requestNativeOverlayHide("price-check-hidden");
    return;
  }
  if (
    event?.name === "CLIENT->MAIN::user-action" &&
    event.payload?.action === "kwaystone-price-check-ready"
  ) {
    debug("ee2 renderer ready; replaying latest item text");
    if (socket) scheduleLastItemText(socket, [0, 250, 1000]);
    return;
  }
  if (event?.name === "CLIENT->MAIN::user-action" && event.payload?.action === "quit") {
    broadcast({
      name: "MAIN->CLIENT::log-entry",
      payload: { message: "Quit from EE2 UI is disabled inside Kwaystone.\n" },
    });
  }
}

function requestNativeOverlayHide(reason: string): void {
  hideRequestSeq += 1;
  hideReason = reason;
  debug(`ee2 native hide requested reason=${reason} seq=${hideRequestSeq}`);
}

/** Test seam. */
export function decodeClientFrames(chunk: Buffer): string[] {
  const out: string[] = [];
  let offset = 0;
  while (offset + 2 <= chunk.length) {
    const b0 = chunk[offset++];
    const b1 = chunk[offset++];
    const opcode = b0 & 0x0f;
    let len = b1 & 0x7f;
    if (len === 126) {
      if (offset + 2 > chunk.length) break;
      len = chunk.readUInt16BE(offset);
      offset += 2;
    } else if (len === 127) {
      if (offset + 8 > chunk.length) break;
      const big = chunk.readBigUInt64BE(offset);
      offset += 8;
      if (big > BigInt(Number.MAX_SAFE_INTEGER)) break;
      len = Number(big);
    }
    const masked = (b1 & 0x80) !== 0;
    if (!masked || offset + 4 + len > chunk.length) break;
    const mask = chunk.subarray(offset, offset + 4);
    offset += 4;
    const payload = Buffer.from(chunk.subarray(offset, offset + len));
    offset += len;
    for (let i = 0; i < payload.length; i++) payload[i] ^= mask[i % 4];
    if (opcode === 1) out.push(payload.toString("utf8"));
    if (opcode === 8) break;
  }
  return out;
}

/** Test seam. */
export function encodeServerFrame(message: string): Buffer {
  const payload = Buffer.from(message, "utf8");
  if (payload.length < 126) {
    return Buffer.concat([Buffer.from([0x81, payload.length]), payload]);
  }
  if (payload.length < 65536) {
    const header = Buffer.alloc(4);
    header[0] = 0x81;
    header[1] = 126;
    header.writeUInt16BE(payload.length, 2);
    return Buffer.concat([header, payload]);
  }
  const header = Buffer.alloc(10);
  header[0] = 0x81;
  header[1] = 127;
  header.writeBigUInt64BE(BigInt(payload.length), 2);
  return Buffer.concat([header, payload]);
}

function broadcast(event: unknown): void {
  const data = encodeServerFrame(JSON.stringify(event));
  for (const socket of host?.sockets ?? []) {
    if (!socket.destroyed) socket.write(data);
  }
}

function scheduleLastItemText(socket: net.Socket, delays = [100, 500, 1500]): void {
  if (!lastItemTextEvent) return;
  for (const delay of delays) {
    const send = () => {
      if (!lastItemTextEvent || socket.destroyed) return;
      socket.write(encodeServerFrame(JSON.stringify(lastItemTextEvent)));
    };
    if (delay <= 0) {
      send();
    } else {
      setTimeout(send, delay);
    }
  }
}

export async function startEe2Host(options: Ee2HostOptions = {}): Promise<{ url: string }> {
  defaults = {
    ...defaults,
    league: options.league || defaults.league,
    accountName: options.accountName ?? defaults.accountName,
    sessionId: options.sessionId ?? defaults.sessionId,
  };
  if (host) return { url: `http://127.0.0.1:${host.port}/` };

  const server = http.createServer((req, res) => {
    void requestHandler(req, res);
  });
  server.on("upgrade", handleUpgrade);
  await new Promise<void>((resolve) => server.listen(0, "127.0.0.1", resolve));
  const address = server.address();
  if (!address || typeof address === "string") {
    throw new Error("could not start EE2 host");
  }
  host = {
    server,
    port: address.port,
    sockets: new Set(),
    configPath: configPath(),
  };
  console.error(`ee2 host listening on http://127.0.0.1:${address.port}/`);
  return { url: `http://127.0.0.1:${address.port}/` };
}

export function sendEe2ItemText(
  clipboard: string,
  position: { x: number; y: number } = { x: 1, y: 1 },
  focusOverlay = false,
): void {
  debug(`ee2 item text queued chars=${clipboard.length}`);
  lastItemTextEvent = {
    name: "MAIN->CLIENT::item-text",
    payload: {
      target: "price-check",
      clipboard,
      position,
      focusOverlay,
    },
  };
  broadcast(lastItemTextEvent);
}

export async function ee2ConfigSummary(): Promise<{ league: string; accountName: string }> {
  const contents = await loadConfig();
  updateDefaultsFromConfig(contents);
  return { league: defaults.league, accountName: defaults.accountName };
}

export function ee2RuntimeState(): { hideRequestSeq: number; hideReason: string } {
  return { hideRequestSeq, hideReason };
}

export async function stopEe2Host(): Promise<void> {
  if (!host) return;
  const current = host;
  host = null;
  for (const socket of current.sockets) socket.destroy();
  await new Promise<void>((resolve) => current.server.close(() => resolve()));
}
