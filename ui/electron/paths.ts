import fs from "node:fs";
import os from "node:os";
import path from "node:path";

export interface DaemonConnection {
  token: string;
  wsUrl: string;
}

export interface DesktopConfig {
  quickAskShortcut: string;
}

export const DEFAULT_QUICK_ASK_SHORTCUT = "CommandOrControl+Alt+Space";

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

export function readDaemonConnection(
  env: NodeJS.ProcessEnv = process.env,
): DaemonConnection | null {
  const tokenFile = path.join(resolveCleaDataDir(env), "session_token");
  const token = (() => {
    try {
      return fs.readFileSync(tokenFile, "utf-8").trim();
    } catch {
      return "";
    }
  })();
  if (!token) return null;

  const host = env.CLEA_HOST || "127.0.0.1";
  const parsedPort = Number(env.CLEA_PORT || 8765);
  const port =
    Number.isInteger(parsedPort) && parsedPort > 0 && parsedPort <= 65535 ? parsedPort : 8765;
  return { token, wsUrl: `ws://${host}:${port}` };
}

export function readDesktopConfig(env: NodeJS.ProcessEnv = process.env): DesktopConfig {
  const configPath = path.join(resolveCleaDataDir(env), "desktop_config.json");
  try {
    const raw = JSON.parse(fs.readFileSync(configPath, "utf-8")) as Partial<DesktopConfig>;
    if (typeof raw.quickAskShortcut === "string" && raw.quickAskShortcut.trim()) {
      return { quickAskShortcut: raw.quickAskShortcut.trim() };
    }
  } catch {
    // Missing or malformed config falls back to default shortcut
  }
  return { quickAskShortcut: DEFAULT_QUICK_ASK_SHORTCUT };
}

/**
 * Resolve a `clea-app://app/...` request strictly inside `rendererDir`.
 * Returns `null` if the host is wrong or the path escapes `rendererDir`.
 */
export function resolveRendererAssetPath(rendererDir: string, requestUrl: string): string | null {
  let parsed: URL;
  try {
    parsed = new URL(requestUrl);
  } catch {
    return null;
  }
  if (parsed.protocol !== "clea-app:" || parsed.hostname !== "app") {
    return null;
  }
  if (requestUrl.includes("..") || requestUrl.toLowerCase().includes("%2e%2e")) {
    return null;
  }

  let pathname = decodeURIComponent(parsed.pathname);
  if (pathname === "/" || pathname === "") {
    pathname = "/index.html";
  }
  if (pathname.includes("..") || pathname.includes("\\")) {
    return null;
  }
  const relative = pathname.replace(/^\/+/, "");
  const root = path.resolve(rendererDir);
  const candidate = path.resolve(root, relative);

  if (candidate !== root && !candidate.startsWith(root + path.sep)) {
    return null;
  }
  return candidate;
}
