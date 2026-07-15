import { createHash } from "node:crypto";
import { access, mkdir, rename, unlink, writeFile } from "node:fs/promises";
import { homedir } from "node:os";
import path from "node:path";

const cacheDir = () =>
  process.env.POE2_ICON_CACHE ??
  path.join(
    process.env.XDG_CACHE_HOME ?? path.join(homedir(), ".cache"),
    "waystone",
    "icons",
  );

const inflight = new Map<string, Promise<string | null>>();
let warnedOnce = false;

export function iconCachePath(url: string): string {
  const hash = createHash("sha1").update(url).digest("hex");
  return path.join(cacheDir(), `${hash}.png`);
}

/**
 * poecdn URL -> local cached file path, or null. Never delays the caller
 * past timeoutMs: on timeout we answer null while the download keeps
 * filling the cache for the next lookup.
 */
export async function resolveIcon(
  url: string | undefined,
  timeoutMs = 3000,
): Promise<string | null> {
  if (!url) return null;
  const file = iconCachePath(url);
  try {
    await access(file);
    return file;
  } catch {
    /* cache miss */
  }
  let dl = inflight.get(url);
  if (!dl) {
    dl = download(url, file).finally(() => inflight.delete(url));
    inflight.set(url, dl);
  }
  return Promise.race([
    dl,
    new Promise<null>((resolve) => {
      const t = setTimeout(resolve, timeoutMs, null);
      t.unref?.();
    }),
  ]);
}

const MAX_ICON_BYTES = 1_000_000; // poecdn icons are tens of KB; cap hostile/broken responses

async function download(url: string, file: string): Promise<string | null> {
  let bytes: Buffer;
  try {
    const r = await fetch(url);
    if (!r.ok) {
      await r.body?.cancel(); // release the socket
      return null;
    }
    bytes = Buffer.from(await r.arrayBuffer());
    if (bytes.byteLength > MAX_ICON_BYTES) return null;
  } catch {
    return null; // network errors are routine; no warning
  }
  // tmp + rename: a crash mid-write must not leave a truncated file at the
  // cache path, or it would be served as a valid hit forever.
  const tmp = `${file}.tmp-${process.pid}`;
  try {
    await mkdir(cacheDir(), { recursive: true });
    await writeFile(tmp, bytes);
    await rename(tmp, file);
    return file;
  } catch (e) {
    await unlink(tmp).catch(() => {}); // don't litter the cache dir
    if (!warnedOnce) {
      warnedOnce = true;
      console.error(`icon cache disabled: ${e}`);
    }
    return null;
  }
}
