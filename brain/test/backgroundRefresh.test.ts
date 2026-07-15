import { afterEach, beforeEach, expect, it, vi } from "vitest";

// backgroundRefresh only needs SCOUT_TTL_MS from poe2scout (to derive its
// default interval) and refreshScanCorpus from uniques (the work it drives).
const { SCOUT_TTL_MS } = vi.hoisted(() => ({ SCOUT_TTL_MS: 30 * 60 * 1000 }));

vi.mock("../src/poe2scout", () => ({ SCOUT_TTL_MS }));

vi.mock("../src/uniques", () => ({
  refreshScanCorpus: vi.fn(async () => {}),
}));

async function refreshMock() {
  const { refreshScanCorpus } = await import("../src/uniques");
  return vi.mocked(refreshScanCorpus);
}

beforeEach(() => {
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.clearAllMocks();
  delete process.env.WAYSTONE_BRAIN_REFRESH_MS;
});

it("refreshes immediately on start with the current league", async () => {
  const { startBackgroundRefresh } = await import("../src/backgroundRefresh");
  const refresh = await refreshMock();

  const stop = startBackgroundRefresh(() => "Standard");
  await vi.advanceTimersByTimeAsync(0);

  expect(refresh).toHaveBeenCalledTimes(1);
  expect(refresh).toHaveBeenCalledWith("Standard");
  stop();
});

it("schedules repeated refreshes on its interval until stopped", async () => {
  const { startBackgroundRefresh } = await import("../src/backgroundRefresh");
  const refresh = await refreshMock();

  const stop = startBackgroundRefresh(() => "L", { intervalMs: 1_000 });
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenCalledTimes(1);

  await vi.advanceTimersByTimeAsync(999);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(2);
  await vi.advanceTimersByTimeAsync(1_000);
  expect(refresh).toHaveBeenCalledTimes(3);

  // League is re-read on every tick, not captured at start.
  const stop2 = startBackgroundRefresh(() => "Later", { intervalMs: 1_000 });
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenLastCalledWith("Later");
  stop2();

  stop();
  await vi.advanceTimersByTimeAsync(10_000);
  expect(refresh).toHaveBeenCalledTimes(4);
});

it("defaults its interval to 80% of the scout TTL", async () => {
  const { DEFAULT_BACKGROUND_REFRESH_MS, startBackgroundRefresh } =
    await import("../src/backgroundRefresh");
  const refresh = await refreshMock();

  expect(DEFAULT_BACKGROUND_REFRESH_MS).toBe(Math.floor(SCOUT_TTL_MS * 0.8));

  const stop = startBackgroundRefresh(() => "L");
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(DEFAULT_BACKGROUND_REFRESH_MS - 1);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(2);
  stop();
});

it("survives a rejected refresh and keeps scheduling", async () => {
  const { startBackgroundRefresh } = await import("../src/backgroundRefresh");
  const refresh = await refreshMock();
  const errorSpy = vi.spyOn(console, "error").mockImplementation(() => {});
  refresh.mockRejectedValueOnce(new Error("scout down"));

  const stop = startBackgroundRefresh(() => "L", { intervalMs: 1_000 });
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenCalledTimes(1);
  expect(errorSpy).toHaveBeenCalledWith(
    "background refresh failed:",
    "scout down",
  );

  // The rejection did not kill the loop: the next tick still refreshes.
  await vi.advanceTimersByTimeAsync(1_000);
  expect(refresh).toHaveBeenCalledTimes(2);

  stop();
  errorSpy.mockRestore();
});

it("honors the WAYSTONE_BRAIN_REFRESH_MS env override", async () => {
  const { startBackgroundRefresh } = await import("../src/backgroundRefresh");
  const refresh = await refreshMock();

  process.env.WAYSTONE_BRAIN_REFRESH_MS = "120000";
  const stop = startBackgroundRefresh(() => "L");
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(119_999);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(2);
  stop();
});

it("clamps a too-small env override up to one minute", async () => {
  const { startBackgroundRefresh } = await import("../src/backgroundRefresh");
  const refresh = await refreshMock();

  process.env.WAYSTONE_BRAIN_REFRESH_MS = "5";
  const stop = startBackgroundRefresh(() => "L");
  await vi.advanceTimersByTimeAsync(0);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(59_999);
  expect(refresh).toHaveBeenCalledTimes(1);
  await vi.advanceTimersByTimeAsync(1);
  expect(refresh).toHaveBeenCalledTimes(2);
  stop();
});
