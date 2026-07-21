/**
 * Rate-limit-aware request scheduler for the arbitrage feature.
 *
 * Every outbound request to a shared source (GGG trade API, poe2scout) goes
 * through a per-source lane with a minimum interval between starts, so a burst
 * of Alt+S presses coalesces in the queue instead of slamming the API. A 429
 * (or Retry-After) pauses the lane and requeues the task — staying out of the
 * ban path by construction. Tasks dedupe by key: a pending identical request
 * shares one promise.
 */

export type SourceLane = "ggg" | "scout";

type Task = {
  key: string;
  priority: number;
  lane: SourceLane;
  run: () => Promise<unknown>;
  resolve: (value: unknown) => void;
  reject: (error: unknown) => void;
};

/** Minimum spacing between request starts per lane (sustained rate). */
const LANE_INTERVAL_MS: Record<SourceLane, number> = {
  // GGG trade endpoints are the strict ones (search/fetch/exchange); ~30/min
  // sustained with natural coalescing keeps us far from the documented limits.
  ggg: 2_000,
  // poe2scout is an aggregator, not GGG — still kept polite.
  scout: 3_000,
};
const DEFAULT_RETRY_PAUSE_MS = 30_000;

export class Http429 extends Error {
  retryAfterMs: number;
  constructor(retryAfterMs: number) {
    super("rate limited (429)");
    this.retryAfterMs = retryAfterMs;
  }
}

export function retryAfterMs(headers: Headers): number | null {
  const raw = headers.get("retry-after");
  if (!raw) return null;
  const seconds = Number(raw);
  return Number.isFinite(seconds) ? Math.max(1_000, seconds * 1000) : null;
}

export class Scheduler {
  private queues: Record<SourceLane, Task[]> = { ggg: [], scout: [] };
  private pendingKeys = new Map<string, Promise<unknown>>();
  private laneIdleAt: Record<SourceLane, number> = { ggg: 0, scout: 0 };
  private lanePausedUntil: Record<SourceLane, number> = { ggg: 0, scout: 0 };
  private laneRunning: Record<SourceLane, boolean> = { ggg: false, scout: false };
  private intervals: Record<SourceLane, number>;

  constructor(
    private readonly now: () => number = Date.now,
    intervals: Partial<Record<SourceLane, number>> = {},
  ) {
    this.intervals = { ...LANE_INTERVAL_MS, ...intervals };
  }

  /** Queue depth per lane (test/introspection seam). */
  get depth(): Record<SourceLane, number> {
    return { ggg: this.queues.ggg.length, scout: this.queues.scout.length };
  }

  schedule<T>(
    key: string,
    priority: number,
    lane: SourceLane,
    run: () => Promise<T>,
  ): Promise<T> {
    const pending = this.pendingKeys.get(key);
    if (pending) return pending as Promise<T>;
    let resolveRef!: (value: unknown) => void;
    let rejectRef!: (error: unknown) => void;
    const promise = new Promise<T>((resolve, reject) => {
      resolveRef = resolve as (value: unknown) => void;
      rejectRef = reject;
    });
    this.pendingKeys.set(key, promise);
    const task: Task = {
      key,
      priority,
      lane,
      run,
      resolve: resolveRef,
      reject: rejectRef,
    };
    const queue = this.queues[lane];
    const insertAt = queue.findIndex((t) => t.priority > priority);
    if (insertAt < 0) queue.push(task);
    else queue.splice(insertAt, 0, task);
    void this.pump(lane);
    return promise;
  }

  private async pump(lane: SourceLane): Promise<void> {
    if (this.laneRunning[lane]) return;
    this.laneRunning[lane] = true;
    try {
      for (;;) {
        const queue = this.queues[lane];
        const task = queue.shift();
        if (!task) return;
        const startAt = Math.max(
          this.laneIdleAt[lane],
          this.lanePausedUntil[lane],
        );
        const wait = startAt - this.now();
        if (wait > 0) {
          queue.unshift(task);
          await sleep(wait);
          continue;
        }
        this.laneIdleAt[lane] = this.now() + this.intervals[lane];
        try {
          const value = await task.run();
          this.pendingKeys.delete(task.key);
          task.resolve(value);
        } catch (error) {
          if (error instanceof Http429) {
            // Pause the lane and requeue at the front: the limiter said stop,
            // so nothing else goes out until it says go.
            this.lanePausedUntil[lane] = this.now() + error.retryAfterMs;
            queue.unshift(task);
            continue;
          }
          this.pendingKeys.delete(task.key);
          task.reject(error);
        }
      }
    } finally {
      this.laneRunning[lane] = false;
    }
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}
