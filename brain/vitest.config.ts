import { defineConfig } from "vitest/config";
import path from "node:path";
import { fileURLToPath } from "node:url";

const r = (p: string) =>
  path.resolve(path.dirname(fileURLToPath(import.meta.url)), p);

export default defineConfig({
  test: {
    include: ["test/**/*.test.ts"],
  },
  resolve: {
    alias: [
      { find: "@/web/Config", replacement: r("src/stubs/Config.ts") },
      { find: "@/web/background/IPC", replacement: r("src/stubs/IPC.ts") },
      { find: "@/web/overlay/widgets", replacement: r("src/stubs/widgets.ts") },
      { find: "@/web/overlay/interfaces", replacement: r("src/stubs/widgets.ts") },
      { find: /^@\/(.*)/, replacement: r("vendor/ee2/src") + "/$1" },
    ],
  },
});
