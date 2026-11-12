import type { CleaConnectionInfo } from "./types";

export type TokenLookupResult =
  { ok: true; info: CleaConnectionInfo } | { ok: false; reason: "daemon_offline" | "unavailable" };

export async function resolveConnectionInfo(): Promise<TokenLookupResult> {
  if (typeof window !== "undefined" && window.cleaDesktop) {
    try {
      const info = await window.cleaDesktop.getConnection();
      if (!info || !info.token) {
        return { ok: false, reason: "daemon_offline" };
      }
      return { ok: true, info };
    } catch {
      return { ok: false, reason: "daemon_offline" };
    }
  }

  // Browser dev mode ONLY: compiled away in production (`import.meta.env.DEV === false`).
  if (import.meta.env.DEV) {
    try {
      const resp = await fetch("/__clea/token", { cache: "no-store" });
      if (!resp.ok) {
        return { ok: false, reason: "daemon_offline" };
      }
      const data = (await resp.json()) as Partial<CleaConnectionInfo>;
      if (!data.token || !data.wsUrl) {
        return { ok: false, reason: "daemon_offline" };
      }
      return { ok: true, info: { token: data.token, wsUrl: data.wsUrl } };
    } catch {
      return { ok: false, reason: "daemon_offline" };
    }
  }

  return { ok: false, reason: "unavailable" };
}
