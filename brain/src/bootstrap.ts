// NOTE: importing this module immediately patches globalThis.fetch and sets globalThis.EE2_DATA_BASE.
import { readFile } from "node:fs/promises";
import { fileURLToPath, pathToFileURL } from "node:url";
import path from "node:path";

declare global {
  // eslint-disable-next-line no-var
  var EE2_DATA_BASE: string;
}

const VENDOR_PUBLIC = path.join(path.dirname(fileURLToPath(import.meta.url)), "../vendor/ee2/public/");
// Absolute file:// URL so that:
//  - vendored data/index.ts `fetch(`${EE2_DATA_BASE}data/...`)` is caught by the shim below;
//  - client-string-loader.ts `import(`${EE2_DATA_BASE}data/en/client_strings.js`)` resolves
//    natively as a Node ESM file import (regex-bearing module, not JSON).
globalThis.EE2_DATA_BASE = pathToFileURL(VENDOR_PUBLIC).href;

const FETCH_PATCHED = Symbol.for("poe2.fetchPatched");
if (!(globalThis.fetch as any)?.[FETCH_PATCHED]) {
  const realFetch = globalThis.fetch;
  const shimmedFetch = (async (input: any, init?: any) => {
    const u = String(input);
    if (u.startsWith("file://")) {
      const buf = await readFile(fileURLToPath(u));
      return new Response(buf);
    }
    return realFetch(input, init);
  }) as typeof fetch;
  (shimmedFetch as any)[FETCH_PATCHED] = true;
  globalThis.fetch = shimmedFetch;
}

export async function initBrainData() {
  const { init } = await import("@/assets/data");
  await init("en");
}
