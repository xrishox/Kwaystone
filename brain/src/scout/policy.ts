export const MAJOR_CURRENCIES = [
  "exalted",
  "chaos",
  "divine",
  "annul",
  "greater-chaos-orb",
] as const;

export const MAJOR_CURRENCY_SET = new Set<string>(MAJOR_CURRENCIES);

export const SCOUT_MAX_AGE_MS = 4 * 60 * 60 * 1000;
export const SCOUT_RELIABLE_MIN_SIDE_VOLUME = 100;
export const SCOUT_RELIABLE_MIN_LIQUIDITY_EX = 10_000;

export type ScoutConfidence = "reliable" | "thin";

export function scoutConfidence(
  fromVolume: number,
  toVolume: number,
  liquidityExalted: number,
): ScoutConfidence {
  return fromVolume >= SCOUT_RELIABLE_MIN_SIDE_VOLUME &&
    toVolume >= SCOUT_RELIABLE_MIN_SIDE_VOLUME &&
    liquidityExalted >= SCOUT_RELIABLE_MIN_LIQUIDITY_EX
    ? "reliable"
    : "thin";
}
