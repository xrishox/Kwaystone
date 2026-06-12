import type { PriceCheckWidget } from "./widgets";
import { PRICE_CHECK_DEFAULTS } from "./widgets";

export interface BrainConfig {
  league: string;
  leagueId?: string;
  accountName: string;
  // Widened to the upstream EE2 union so vendored code can compare against
  // other locales (e.g. magic-name.ts checks `=== "cmn-Hant"`). Runtime is
  // always "en" — non-English clients are out of scope for this overlay.
  language: "en" | "ru" | "cmn-Hant" | "ko" | "ja" | "de" | "es" | "pt" | "fr";
  realm: "pc-ggg";
  preferredTradeSite: "www";
  // POESESSID browser cookie. When set, attached as a Cookie header on trade
  // API calls (logged-in fetch). Empty/undefined = anonymous. Never logged.
  sessionId?: string;
}

let current: BrainConfig = {
  league: process.env.POE2_LEAGUE ?? "Standard",
  leagueId: process.env.POE2_LEAGUE ?? "Standard",
  accountName: process.env.POE2_ACCOUNT ?? "",
  language: "en", realm: "pc-ggg", preferredTradeSite: "www",
  sessionId: process.env.POE2_SESSID || undefined,
};

// In-place mutation so vendored code that holds a live reference to AppConfig() (e.g. Leagues.ts) sees updates.
export function setBrainConfig(c: Partial<BrainConfig>) { Object.assign(current, c); }

export function AppConfig(): BrainConfig;
export function AppConfig<T>(type: string): T | undefined;
export function AppConfig(type?: string) {
  if (type === "price-check") return PRICE_CHECK_DEFAULTS as unknown as PriceCheckWidget;
  if (type !== undefined) return undefined;
  return current;
}

export function poeWebApi() { return "www.pathofexile.com"; }
