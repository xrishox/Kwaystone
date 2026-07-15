import { createApp, watch } from "vue";
import App from "./web/App.vue";
import * as I18n from "./web/i18n";
import * as Data from "./assets/data";
import { initConfig, AppConfig } from "./web/Config";
import { Host } from "./web/background/IPC";

function errorText(error: unknown): string {
  if (error instanceof Error) return error.stack ?? error.message;
  return String(error);
}

function reportStartupError(error: unknown): void {
  const message = errorText(error);
  console.error("EE2 renderer startup failed", error);
  const box = document.createElement("pre");
  box.textContent = `Kwaystone price-check UI failed to start:\n\n${message}`;
  box.style.cssText = [
    "position:fixed",
    "top:1rem",
    "left:1rem",
    "max-width:min(46rem,calc(100vw - 2rem))",
    "max-height:calc(100vh - 2rem)",
    "overflow:auto",
    "z-index:999999",
    "padding:0.75rem 1rem",
    "border:1px solid #f56565",
    "border-radius:0.375rem",
    "background:rgba(26,32,44,0.96)",
    "color:#fed7d7",
    "white-space:pre-wrap",
  ].join(";");
  document.body.appendChild(box);
  void fetch("/client-error", {
    method: "POST",
    headers: { "content-type": "text/plain; charset=utf-8" },
    body: message,
  }).catch(() => undefined);
}

(async function () {
  try {
    await initConfig();
    const i18nPlugin = await I18n.init(AppConfig().language);
    await Data.init(AppConfig().language);
    await Host.init();

    watch(
      () => AppConfig().language,
      async () => {
        await Data.loadForLang(AppConfig().language);
        await I18n.loadLang(AppConfig().language);
      },
    );

    const app = createApp(App);
    app.use(i18nPlugin);
    app.mount("#app");
    if (import.meta.env.DEV) {
      app.config.performance = true;
      console.error("DEV MODE");
    }
  } catch (error) {
    reportStartupError(error);
  }
})();
