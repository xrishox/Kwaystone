import { uniquePriceMap, priceMap } from "./poe2scout";
import { resolveIcon } from "./icons";

export interface ScanCorpusRow {
  price: number;
  quantity: number;
  iconPath: string | null;
  kind: "unique" | "tagged";
  w: number; // inventory slots wide — poed normalizes template density with it
  h: number;
  trend: number | null; // fractional price change over the snapshot window
}

// poecdn gen/image URLs carry their item's slot size in a base64 JSON
// segment: /gen/image/<b64([25,14,{"f":...,"w":2,"h":1,...}])>/<sig>/x.png
function slotsFromUrl(url: string): { w: number; h: number } {
  try {
    const seg = url.split("/gen/image/")[1]?.split("/")[0] ?? "";
    const arr = JSON.parse(Buffer.from(seg, "base64").toString("utf8"));
    const o = arr?.[2];
    return { w: o?.w || 1, h: o?.h || 1 };
  } catch {
    return { w: 1, h: 1 };
  }
}

// Cold icon cache means ~1100 downloads on the first call; bound the fan-out
// so we don't open a thousand sockets at once. Warm calls are disk hits.
const ICON_CONCURRENCY = 16;

async function mapLimit<T, R>(
  items: T[],
  limit: number,
  fn: (item: T) => Promise<R>,
): Promise<R[]> {
  const out: R[] = new Array(items.length);
  let next = 0;
  await Promise.all(
    Array.from({ length: Math.min(limit, items.length) }, async () => {
      while (next < items.length) {
        const i = next++;
        out[i] = await fn(items[i]);
      }
    }),
  );
  return out;
}

/**
 * The unique-scan corpus: display name -> {price, quantity, iconPath, kind}.
 *
 * Two price sources merged (2026-06-11 spec + scope add):
 *  - uniques from poe2scout Uniques/ByCategory, keyed by Name;
 *  - tradeTagged items (currency/omens/gems — Ritual rewards) from the
 *    existing currency priceMap, names + icon URLs from the vendored data.
 *    Tags without a scout price are omitted (nothing to triage against).
 *
 * Icons are resolved to brain-cached local files (poed never fetches remote
 * URLs). Any icon failure degrades that row's iconPath to null — prices still
 * flow. Shared arts (e.g. uncut-gem levels) intentionally keep one row per
 * NAME here; poed's matcher dedupes by icon file and groups the labels.
 *
 * PRECONDITION: initBrainData() must have run (server.ts awaits it) — the
 * vendored tag->name lookups are empty until then.
 */
export async function scanCorpus(
  league: string,
): Promise<Record<string, ScanCorpusRow>> {
  const { TRADE_TAG_TO_REF, ITEM_BY_REF } = await import("@/assets/data");
  const [uniques, currencies] = await Promise.all([
    uniquePriceMap(league),
    priceMap(league),
  ]);

  const rows: Array<{ name: string; iconUrl: string; row: Omit<ScanCorpusRow, "iconPath"> }> = [];

  for (const [name, u] of uniques) {
    rows.push({
      name,
      iconUrl: u.iconUrl,
      row: {
        price: u.price, quantity: u.quantity, kind: "unique",
        trend: u.trend, ...slotsFromUrl(u.iconUrl),
      },
    });
  }

  for (const [tag, ref] of TRADE_TAG_TO_REF) {
    const scout = currencies.get(tag);
    if (!scout) continue;
    const icon = ITEM_BY_REF("ITEM", ref)?.[0]?.icon;
    if (!icon || !icon.startsWith("https://")) continue;
    // Trend from the currency snapshot's chronological history.
    const h = scout.history;
    const trend =
      h.length >= 2 && h[0] > 0 ? (h[h.length - 1] - h[0]) / h[0] : null;
    rows.push({
      name: ref,
      iconUrl: icon,
      row: {
        price: scout.price, quantity: scout.quantity, kind: "tagged",
        trend, ...slotsFromUrl(icon),
      },
    });
  }

  const resolved = await mapLimit(rows, ICON_CONCURRENCY, async (r) => {
    const iconPath = r.iconUrl
      ? await resolveIcon(r.iconUrl).catch(() => null)
      : null;
    return [r.name, { ...r.row, iconPath }] as const;
  });

  const out: Record<string, ScanCorpusRow> = {};
  for (const [name, row] of resolved) {
    if (!(name in out)) out[name] = row;
  }
  return out;
}
