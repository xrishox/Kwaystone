import { AppConfig } from "./Config";
import { WAYSTONE_USER_AGENT, cookieAllowedForHost } from "../session-headers";

// EE2 routes API calls through Electron main to dodge CORS. Node has no CORS; call directly.
export const Host = {
  proxy: async (url: string | URL, init?: RequestInit): Promise<Response> => {
    const u = String(url);
    const target = u.startsWith("http") ? u : `https://${u}`;
    const sessionId = AppConfig().sessionId;
    return fetch(target, {
      ...init,
      headers: {
        "user-agent": WAYSTONE_USER_AGENT,
        // Authenticated fetch for the real API domain; anonymous for
        // everything else (see session-headers.ts for the cookie policy).
        ...(sessionId && cookieAllowedForHost(new URL(target).hostname)
          ? { Cookie: `POESESSID=${sessionId}` }
          : {}),
        ...(init?.headers ?? {}),
      },
    });
  },
  isElectron: false,
};
