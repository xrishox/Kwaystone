import { SCOUT_TTL_MS } from "./poe2scout";

export const DEFAULT_BACKGROUND_REFRESH_MS = SCOUT_TTL_MS;

function refreshIntervalMs(): number {
  const configured = Number(process.env.WAYSTONE_BRAIN_REFRESH_MS);
  if (Number.isFinite(configured) && configured > 0) {
    return Math.max(60_000, Math.floor(configured));
  }
  return DEFAULT_BACKGROUND_REFRESH_MS;
}

export function startBackgroundRefresh(
  getLeague: () => string,
  options: { intervalMs?: number } = {},
): () => void {
  const intervalMs = options.intervalMs ?? refreshIntervalMs();
  let stopped = false;
  let timer: NodeJS.Timeout | null = null;
  let running: Promise<void> | null = null;

  const schedule = () => {
    if (stopped) return;
    timer = setTimeout(() => {
      void run();
    }, intervalMs);
    timer.unref?.();
  };

  const run = async () => {
    if (running) return running;
    const league = getLeague();
    running = (async () => {
      const started = Date.now();
      try {
        const { refreshScanCorpus } = await import("./uniques");
        await refreshScanCorpus(league);
        if (process.env.WAYSTONE_DEBUG) {
          console.error(
            `background refresh complete league=${league} elapsed=${Date.now() - started}ms`,
          );
        }
      } catch (e) {
        console.error(
          "background refresh failed:",
          e instanceof Error ? e.message : e,
        );
      } finally {
        running = null;
        schedule();
      }
    })();
    return running;
  };

  void run();

  return () => {
    stopped = true;
    if (timer) {
      clearTimeout(timer);
      timer = null;
    }
  };
}
