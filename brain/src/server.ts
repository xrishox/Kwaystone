import net from "node:net";
import { unlink } from "node:fs/promises";
import { initBrainData } from "./bootstrap";

type Emit = (stage: string) => void;
type Handler = (req: any, emit: Emit) => Promise<unknown>;
const handlers: Record<string, Handler> = {
  ping: async () => "pong",
  parse: async (req) => {
    const { parseClipboard } = await import("@/parser");
    const r = parseClipboard(req.clipboard ?? "");
    // r.error is a plain string code from EE2 (e.g. "item.parse_error",
    // "item.unknown"). It surfaces verbatim in a Python toast, so keep it
    // as the readable suffix.
    if (r.isErr()) throw new Error(`not an item: ${r.error}`);
    return r.value;
  },
  price: async (req, emit) => {
    const { priceCheck } = await import("./price");
    return priceCheck(
      req.clipboard ?? "",
      req.league ?? process.env.POE2_LEAGUE ?? "Standard",
      [],
      emit,
    );
  },
  requery: async (req, emit) => {
    const { priceCheck } = await import("./price");
    return priceCheck(
      req.clipboard ?? "",
      req.league ?? process.env.POE2_LEAGUE ?? "Standard",
      Array.isArray(req.overrides) ? req.overrides : [],
      emit,
    );
  },
  bulk: async (req) => {
    if (!req.have || !req.want) {
      throw new Error("bulk requires 'have' and 'want' currency tags");
    }
    const { bulkSearch } = await import("./bulk");
    return bulkSearch(
      req.have,
      req.want,
      req.league ?? process.env.POE2_LEAGUE ?? "Standard",
    );
  },
  uniqueprices: async (req) => {
    const { scanCorpus } = await import("./uniques");
    return scanCorpus(req.league ?? process.env.POE2_LEAGUE ?? "Standard");
  },
  leagues: async () => (await import("./leagues")).leagues(),
  login: async (req) => (await import("./session")).login(String(req.sessionId ?? "")),
  logout: async () => (await import("./session")).logout(),
};

async function handleLine(conn: net.Socket, line: string): Promise<void> {
  let id: unknown = null;
  let cmd = "?";
  try {
    const req = JSON.parse(line);
    id = req.id;
    cmd = String(req.cmd);
    if (process.env.WAYSTONE_DEBUG) console.error(`cmd=${cmd} id=${id} start`);
    const h = handlers[req.cmd];
    if (!h) throw new Error(`unknown cmd: ${req.cmd}`);
    // Progress lines stream before the final response, tagged with the same
    // id. They double as keepalive inside poed's 30s inactivity timeout
    // during slow exchange probes. Write errors are swallowed by the
    // per-connection error handler.
    const emit: Emit = (stage) => {
      conn.write(JSON.stringify({ id, progress: stage }) + "\n");
    };
    conn.write(
      JSON.stringify({ id, ok: true, result: await h(req, emit) }) + "\n",
    );
  } catch (e) {
    // Full stack to stderr — poed pumps it into the shared waystone log.
    // The wire error stays message-only (it surfaces in the panel).
    console.error(
      `cmd=${cmd} failed:`,
      e instanceof Error ? (e.stack ?? e.message) : e,
    );
    conn.write(
      JSON.stringify({
        id,
        ok: false,
        error: String(e instanceof Error ? e.message : e),
      }) + "\n",
    );
  }
}

export async function startServer(
  sockPath: string,
): Promise<() => Promise<void>> {
  await initBrainData();
  await unlink(sockPath).catch(() => {});
  const connections = new Set<net.Socket>();
  const server = net.createServer((conn) => {
    connections.add(conn);
    conn.on("close", () => connections.delete(conn));
    // Swallow socket errors (e.g. EPIPE on client disconnect mid-write) so
    // they don't surface as uncaught exceptions and kill the process.
    conn.on("error", () => {});
    let buf = "";
    // Per-connection FIFO chain. The "data" event fires synchronously, so we
    // first drain ALL complete lines out of the shared buffer into the closure
    // immediately (no await touches `buf`), then chain their async handling
    // onto a per-connection promise. This keeps `buf` from being sliced across
    // an await (the template's race, which could drop/duplicate lines) and
    // preserves request order on a single connection.
    let chain: Promise<void> = Promise.resolve();
    conn.on("data", (d) => {
      buf += d;
      const lines: string[] = [];
      let nl: number;
      while ((nl = buf.indexOf("\n")) >= 0) {
        lines.push(buf.slice(0, nl));
        buf = buf.slice(nl + 1);
      }
      for (const line of lines) {
        chain = chain.then(() => handleLine(conn, line));
      }
    });
  });
  await new Promise<void>((res) => server.listen(sockPath, res));
  // Warm the poe2scout price snapshot in the background once the server is up.
  // The overlay launches with the game, so there are minutes of lead time
  // before the first currency lookup; this just front-loads the bulk pull.
  // Fire-and-forget, errors swallowed — a failed warm leaves an empty/stale map
  // and lookups degrade gracefully (currencyCheck falls back to the exchange).
  // League mirrors the per-request default (POE2_LEAGUE / "Standard").
  void (async () => {
    const league = process.env.POE2_LEAGUE ?? "Standard";
    const { priceMap } = await import("./poe2scout");
    await priceMap(league);
    // Unique-scan corpus warm: pulls the uniques snapshot AND resolves all
    // ~1000 icons to disk, so the first Alt+X pays neither.
    const { scanCorpus } = await import("./uniques");
    await scanCorpus(league);
  })().catch(() => {});
  return () =>
    new Promise((res) => {
      server.close(() => res());
      for (const conn of connections) conn.destroy();
    });
}

// server.ts when run via tsx (dev), server.mjs as the esbuild bundle (release).
if (process.argv[1]?.endsWith("server.ts") || process.argv[1]?.endsWith("server.mjs")) {
  const sock =
    process.env.BRAIN_SOCKET ??
    `${process.env.XDG_RUNTIME_DIR ?? "/tmp"}/waystone-brain.sock`;
  startServer(sock)
    .then(() => console.error(`brain listening on ${sock}`))
    .catch((e) => { console.error(e); process.exit(1); });
}
