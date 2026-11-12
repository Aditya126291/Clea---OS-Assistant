import fs from "node:fs";
import type { IncomingMessage } from "node:http";
import os from "node:os";
import path from "node:path";
import type { Plugin } from "vite";

export function resolveCleaDataDir(env: NodeJS.ProcessEnv = process.env): string {
  const override = env.CLEA_DATA_DIR;
  if (override && override.trim()) {
    return path.resolve(override.trim());
  }
  if (process.platform === "win32") {
    const localAppData = env.LOCALAPPDATA || path.join(os.homedir(), "AppData", "Local");
    return path.join(localAppData, "clea");
  }
  if (process.platform === "darwin") {
    return path.join(os.homedir(), "Library", "Application Support", "clea");
  }
  const xdgData = env.XDG_DATA_HOME || path.join(os.homedir(), ".local", "share");
  return path.join(xdgData, "clea");
}

function isLocalHostname(hostname: string | null | undefined): boolean {
  return hostname === "127.0.0.1" || hostname === "localhost" || hostname === "::1";
}

export function isValidLocalHostHeader(hostHeader: string | undefined): boolean {
  if (!hostHeader || hostHeader.includes("@") || /\s/.test(hostHeader)) return false;
  try {
    const parsed = new URL(`http://${hostHeader}`);
    return isLocalHostname(parsed.hostname.replace(/^\[|\]$/g, ""));
  } catch {
    return false;
  }
}

export function isValidLocalOrigin(originHeader: string | undefined): boolean {
  if (!originHeader) return true;
  try {
    const parsed = new URL(originHeader);
    return (
      (parsed.protocol === "http:" || parsed.protocol === "https:") &&
      isLocalHostname(parsed.hostname.replace(/^\[|\]$/g, ""))
    );
  } catch {
    return false;
  }
}

export interface DevTokenResponse {
  statusCode: number;
  setHeader: (name: string, value: string) => unknown;
  removeHeader?: (name: string) => unknown;
  end: (chunk?: string) => unknown;
}

const CORS_HEADERS = [
  "access-control-allow-origin",
  "access-control-allow-credentials",
  "access-control-allow-headers",
  "access-control-allow-methods",
  "access-control-expose-headers",
  "access-control-max-age",
];

export function handleDevTokenRequest(
  req: Pick<IncomingMessage, "url" | "method" | "headers">,
  res: DevTokenResponse,
  env: NodeJS.ProcessEnv = process.env,
): boolean {
  if (req.url && req.url !== "/" && req.url !== "") {
    return false;
  }
  for (const header of CORS_HEADERS) {
    res.removeHeader?.(header);
  }
  if (req.method !== "GET") {
    res.statusCode = 405;
    res.end("Method Not Allowed");
    return true;
  }
  const host = typeof req.headers.host === "string" ? req.headers.host : undefined;
  const origin = typeof req.headers.origin === "string" ? req.headers.origin : undefined;
  if (!isValidLocalHostHeader(host) || !isValidLocalOrigin(origin)) {
    res.statusCode = 403;
    res.end("Forbidden");
    return true;
  }

  const tokenFile = path.join(resolveCleaDataDir(env), "session_token");
  const token = (() => {
    try {
      return fs.readFileSync(tokenFile, "utf-8").trim();
    } catch {
      return "";
    }
  })();

  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store");

  if (!token) {
    res.statusCode = 404;
    res.end(JSON.stringify({ error: "daemon_not_running" }));
    return true;
  }

  const wsHost = env.CLEA_HOST || "127.0.0.1";
  const parsedPort = Number(env.CLEA_PORT || 8765);
  const wsPort =
    Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65535 ? parsedPort : 8765;
  res.statusCode = 200;
  res.end(JSON.stringify({ token, wsUrl: `ws://${wsHost}:${wsPort}` }));
  return true;
}

/**
 * Dev-only plugin serving the daemon session token from the OS user data directory
 * at `/__clea/token`. Never bundled into production builds (`apply: "serve"`),
 * emits zero CORS headers, and restricts Host and Origin strictly to localhost.
 */
export function cleaDevTokenPlugin(): Plugin {
  return {
    name: "clea-dev-token",
    apply: "serve",
    configureServer(server) {
      server.middlewares.use("/__clea/token", (req, res, next) => {
        if (!handleDevTokenRequest(req, res)) {
          next();
        }
      });
    },
  };
}
