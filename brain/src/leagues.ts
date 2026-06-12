import { Host } from "./stubs/IPC";

export interface LeagueEntry {
  id: string;
  text: string;
}

// Official trade2 league roster. One fetch per process lifetime: the roster
// changes only at season boundaries, and the daemon restarts far more often
// than that. Single static-data request, so it skips the search rate limiter.
let cached: LeagueEntry[] | null = null;

export async function leagues(): Promise<LeagueEntry[]> {
  if (cached) return cached;
  const r = await Host.proxy("www.pathofexile.com/api/trade2/data/leagues");
  if (!r.ok) throw new Error(`leagues -> ${r.status}`);
  const json: any = await r.json();
  const out: LeagueEntry[] = (json.result ?? []).map((l: any) => ({
    id: String(l.id),
    text: String(l.text ?? l.id),
  }));
  if (!out.length) throw new Error("leagues: empty result");
  cached = out;
  return out;
}
