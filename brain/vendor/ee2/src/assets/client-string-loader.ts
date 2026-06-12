import { TranslationDict } from "./data/interfaces";

export async function loadClientStrings(
  lang: string,
): Promise<TranslationDict> {
  return (
    await import(
      /* @vite-ignore */ `${globalThis.EE2_DATA_BASE}data/${lang}/client_strings.js`
    )
  ).default;
}
