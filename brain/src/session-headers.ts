// Shared identity/session policy for every outbound GGG-facing request —
// used by both the Host.proxy stub (stubs/IPC.ts) and the EE2 host proxy
// (ee2-host.ts). Keep the two call sites behaviorally identical by changing
// this module, not them.

// GGG API etiquette: identify app + contact in UA. Keep contact current.
export const WAYSTONE_USER_AGENT = "waystone/0.1 (contact: github.com/kriskruse)";

/**
 * True when the POESESSID cookie may be attached for `host`. The API domain
 * is pathofexile.com; the poe2 marketing/SPA domain (pathofexile2.com) has no
 * API and its sessions are foreign — sending a poe2-domain cookie there would
 * be useless, and sending any cookie to pathofexile2.com could leak it to a
 * domain that can never use it. Everything else (e.g. poe2scout.com) is
 * always anonymous.
 */
export function cookieAllowedForHost(host: string): boolean {
  return host.includes("pathofexile.com") && !host.includes("pathofexile2.com");
}
