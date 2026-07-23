import { expect, it } from "vitest";
import { Http429, retryAfterMs, Scheduler } from "../src/scheduler";

function tick(): Scheduler {
  // 1ms lane intervals: ordering is deterministic, wall-clock stays fast.
  return new Scheduler(() => Date.now(), { ggg: 1, scout: 1 });
}

async function eventually<T>(fn: () => T, timeoutMs = 3000): Promise<T> {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    try {
      return fn();
    } catch (e) {
      if (Date.now() > deadline) throw e;
      await new Promise((resolve) => setTimeout(resolve, 5));
    }
  }
}

it("dedupes identical keys into one underlying run", async () => {
  const sched = tick();
  let runs = 0;
  const run = async () => {
    runs += 1;
    return 42;
  };
  const [a, b] = await Promise.all([
    sched.schedule("k", 1, "ggg", run),
    sched.schedule("k", 1, "ggg", run),
  ]);
  expect(a).toBe(42);
  expect(b).toBe(42);
  expect(runs).toBe(1);
});

it("runs tasks in priority order, not arrival order", async () => {
  const sched = tick();
  const order: string[] = [];
  const make = (name: string) => async () => {
    order.push(name);
    await new Promise((resolve) => setTimeout(resolve, 10));
  };
  const slow = sched.schedule("slow", 1, "ggg", make("slow-first-priority"));
  const a = sched.schedule("a", 5, "ggg", make("late-low-priority"));
  const b = sched.schedule("b", 2, "ggg", make("mid-high-priority"));
  await Promise.all([slow, a, b]);
  // The first task starts immediately; the rest follow priority (2 before 5).
  expect(order).toEqual(["slow-first-priority", "mid-high-priority", "late-low-priority"]);
});

it("promotes a deduped queued task when an interactive caller joins", async () => {
  const sched = tick();
  const order: string[] = [];
  let release!: () => void;
  const blocker = sched.schedule("blocker", 0, "scout", async () => {
    order.push("blocker");
    await new Promise<void>((resolve) => { release = resolve; });
  });
  const background = sched.schedule("same", 50, "scout", async () => {
    order.push("promoted");
    return 1;
  });
  const middle = sched.schedule("middle", 10, "scout", async () => {
    order.push("middle");
  });
  const interactive = sched.schedule("same", 0, "scout", async () => 2);
  expect(interactive).toBe(background);
  release();

  await Promise.all([blocker, background, middle]);
  expect(order).toEqual(["blocker", "promoted", "middle"]);
});

it("parses both numeric and HTTP-date Retry-After values", () => {
  expect(retryAfterMs(new Headers({ "retry-after": "2.5" }))).toBe(2_500);
  const now = Date.now();
  const at = new Date(now + 5_000).toUTCString();
  const delay = retryAfterMs(new Headers({ "retry-after": at }));
  expect(delay).not.toBeNull();
  expect(delay!).toBeGreaterThanOrEqual(3_900);
  expect(delay!).toBeLessThanOrEqual(5_000);
});

it("429 pauses the lane and requeues the task at the front", async () => {
  const sched = tick();
  let attempts = 0;
  const order: string[] = [];
  const flaky = sched.schedule("flaky", 1, "ggg", async () => {
    attempts += 1;
    order.push(`flaky-${attempts}`);
    if (attempts === 1) throw new Http429(20);
    return "recovered";
  });
  const other = sched.schedule("other", 2, "ggg", async () => {
    order.push("other");
  });
  const [recovered] = await Promise.all([flaky, other]);
  expect(recovered).toBe("recovered");
  expect(attempts).toBe(2);
  // The requeued 429 task ran before the queued lower-priority one.
  expect(order).toEqual(["flaky-1", "flaky-2", "other"]);
});

it("a task error rejects its promise but the lane continues", async () => {
  const sched = tick();
  const bad = sched.schedule("bad", 1, "ggg", async () => {
    throw new Error("boom");
  });
  const good = sched.schedule("good", 2, "ggg", async () => "fine");
  await expect(bad).rejects.toThrow("boom");
  await expect(good).resolves.toBe("fine");
  await eventually(() => {
    if (sched.depth.ggg !== 0) throw new Error("not drained");
    return true;
  });
});

it("lanes are independent: scout runs while ggg is busy", async () => {
  const sched = tick();
  const order: string[] = [];
  const gggTask = sched.schedule("g", 1, "ggg", async () => {
    order.push("ggg-start");
    await new Promise((resolve) => setTimeout(resolve, 30));
    order.push("ggg-end");
  });
  const scoutTask = sched.schedule("s", 1, "scout", async () => {
    order.push("scout");
  });
  await Promise.all([gggTask, scoutTask]);
  expect(order.indexOf("scout")).toBeLessThan(order.indexOf("ggg-end"));
});
