import { setBrainConfig, AppConfig } from "./stubs/Config";
import { Host } from "./stubs/IPC";

export async function login(sessionId: string) {
  if (!sessionId) throw new Error("no session id");
  setBrainConfig({ sessionId });
  const r = await Host.proxy("www.pathofexile.com/api/profile", {});
  if (!r.ok) {
    setBrainConfig({ sessionId: undefined });
    throw new Error("invalid or expired session");
  }
  const profile = (await r.json()) as { name?: string };
  return { name: profile.name ?? "" };
}

export function logout() {
  setBrainConfig({ sessionId: undefined });
  return { loggedOut: true };
}
