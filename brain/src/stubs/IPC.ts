import { AppConfig } from "./Config";

// EE2 routes API calls through Electron main to dodge CORS. Node has no CORS; call directly.
export const Host = {
  proxy: async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const u = String(url);
    const sessionId = AppConfig().sessionId;
    // The API domain is pathofexile.com; the poe2 marketing/SPA domain
    // (pathofexile2.com) has no API and its sessions are foreign — sending
    // a poe2-domain cookie there would be useless; sending any cookie to
    // pathofexile2.com could leak it to a domain that can never use it.
    // Attach POESESSID only when the host contains "pathofexile.com" AND
    // NOT "pathofexile2.com".
    const isApiHost =
      u.includes("pathofexile.com") && !u.includes("pathofexile2.com");
    return fetch(u.startsWith("http") ? u : `https://${u}`, {
      ...init,
      // GGG API etiquette: identify app + contact in UA. Keep email current.
      headers: {
        "user-agent": "waystone/0.1 (contact: github.com/kriskruse)",
        // Authenticated fetch for the real API domain; anonymous for everything else.
        ...(sessionId && isApiHost ? { Cookie: `POESESSID=${sessionId}` } : {}),
        ...(init?.headers ?? {}),
      },
    });
  },
  isElectron: false,
};
