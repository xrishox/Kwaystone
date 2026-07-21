import net from "node:net";
import { mkdir, unlink } from "node:fs/promises";
import { tmpdir } from "node:os";
import path from "node:path";
import { startBackgroundRefresh } from "./backgroundRefresh";
import { initBrainData } from "./bootstrap";
import {
  ee2ConfigSummary,
  ee2RuntimeState,
  sendEe2ItemText,
  startEe2Host,
  stopEe2Host,
} from "./ee2-host";

type Emit = (stage: string) => void;
type Handler = (req: any, emit: Emit) => Promise<unknown>;

// A local daemon must not die from a stray rejected promise or thrown
// listener: log and keep serving. Callers own their expected errors.
process.on("unhandledRejection", (reason) => {
  console.error(
    "unhandled rejection:",
    reason instanceof Error ? reason.stack ?? reason.message : reason,
  );
});
process.on("uncaughtException", (e) => {
  console.error("uncaught exception:", e.stack ?? e.message);
});

let activeLeague = process.env.POE2_LEAGUE ?? "Standard";

function requestLeague(req: any): string {
  const league = String(req.league ?? activeLeague ?? "Standard");
  activeLeague = league;
  return league;
}

const handlers: Record<string, Handler> = {
  ping: async () => "pong",
  uniqueprices: async (req, emit) => {
    // A cold-cache corpus build (~1100 icon fetches) can exceed poed's 30s
    // inactivity timeout; heartbeat progress lines keep the request alive.
    const heartbeat = setInterval(() => emit("building scan corpus"), 10_000);
    try {
      if (req.ifVersion !== undefined) {
        // Versioned handshake: an unchanged snapshot skips serializing and
        // shipping the multi-megabyte row dict to the client.
        const { scanCorpusVersioned } = await import("./uniques");
        const { version, rows } = await scanCorpusVersioned(requestLeague(req));
        if (req.ifVersion === version) {
          return { version, unchanged: true };
        }
        return { version, rows };
      }
      const { scanCorpus } = await import("./uniques");
      return scanCorpus(requestLeague(req));
    } finally {
      clearInterval(heartbeat);
    }
  },
  ee2host: async (req) => startEe2Host({
    league: requestLeague(req),
    accountName: String(req.accountName ?? process.env.POE2_ACCOUNT ?? ""),
    sessionId: String(req.sessionId ?? process.env.POE2_SESSID ?? ""),
  }),
  ee2itemtext: async (req) => {
    sendEe2ItemText(String(req.clipboard ?? ""), {
      x: Number(req.x ?? 1),
      y: Number(req.y ?? 1),
    }, Boolean(req.focusOverlay));
    return { ok: true };
  },
  ee2config: async () => ee2ConfigSummary(),
  ee2state: async () => ee2RuntimeState(),
  leagues: async (req) => {
    const { leagueList } = await import("./poe2scout");
    return leagueList({ force: req.force === true });
  },
  arbquote: async (req) => {
    const { arbQuote } = await import("./arbitrage");
    return arbQuote({
      clipboard: String(req.clipboard ?? ""),
      league: requestLeague(req),
      accountName: String(req.accountName ?? process.env.POE2_ACCOUNT ?? ""),
      sessionId: String(req.sessionId ?? process.env.POE2_SESSID ?? ""),
    });
  },
  arbstate: async (req) => {
    const { arbState } = await import("./arbitrage");
    const refreshId = Number(req.refreshId ?? 0);
    return (
      arbState(refreshId) ?? {
        refreshId,
        done: true,
        matrix: [],
        itemRows: [],
        missing: true,
      }
    );
  },
};

async function handleLine(conn: net.Socket, line: string): Promise<void> {
  let id: unknown = null;
  let cmd = "?";
  try {
    const req = JSON.parse(line);
    id = req.id;
    cmd = String(req.cmd);
    if (process.env.WAYSTONE_DEBUG && cmd !== "ee2state") {
      console.error(`cmd=${cmd} id=${id} start`);
    }
    // hasOwn: a plain-object lookup would resolve prototype members like
    // "constructor" as handlers.
    const h = Object.hasOwn(handlers, req.cmd) ? handlers[req.cmd] : undefined;
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
      // Unbounded line buffering lets a broken/malicious local client grow
      // our memory without limit; real requests are small JSON lines.
      if (buf.length > 4 * 1024 * 1024) {
        console.error("line buffer overflow, dropping connection");
        conn.destroy();
        return;
      }
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
  const stopBackgroundRefresh = startBackgroundRefresh(() => activeLeague);
  return () =>
    new Promise((res) => {
      stopBackgroundRefresh();
      void (async () => {
        await stopEe2Host();
        server.close(() => res());
        for (const conn of connections) conn.destroy();
      })();
    });
}

// server.ts when run via tsx (dev), server.mjs as the esbuild bundle (release).
if (process.argv[1]?.endsWith("server.ts") || process.argv[1]?.endsWith("server.mjs")) {
  const runtimeDir =
    process.env.XDG_RUNTIME_DIR ??
    path.join(tmpdir(), `waystone-${process.getuid?.() ?? "user"}`);
  const sock = process.env.BRAIN_SOCKET ?? path.join(runtimeDir, "brain.sock");
  await mkdir(path.dirname(sock), { recursive: true, mode: 0o700 });
  startServer(sock)
    .then(() => console.error(`brain listening on ${sock}`))
    .catch((e) => { console.error(e); process.exit(1); });
}
