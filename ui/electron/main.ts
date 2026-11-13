import { spawn } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { pathToFileURL } from "node:url";
import {
  app,
  BrowserWindow,
  dialog,
  globalShortcut,
  ipcMain,
  Menu,
  nativeImage,
  net,
  protocol,
  screen,
  session,
  Tray,
  type WebContents,
} from "electron";
import {
  buildLoginItemConfig,
  readDaemonConnection,
  readDesktopConfig,
  resolveDaemonLauncher,
  resolveRendererAssetPath,
  shouldStartHidden,
} from "./paths";
import { createTrayIconPngBuffer } from "./tray-icon";

const APP_ORIGIN = "clea-app://app";
const DEV_RENDERER_URL = process.env.ELECTRON_RENDERER_URL;
const RENDERER_DIR = path.resolve(import.meta.dirname, "../renderer");
const PRELOAD_PATH = path.resolve(import.meta.dirname, "../preload/preload.cjs");

// Must run before app.whenReady(). Allows Fetch/WebSocket from clea-app://app.
protocol.registerSchemesAsPrivileged([
  {
    scheme: "clea-app",
    privileges: {
      standard: true,
      secure: true,
      supportFetchAPI: true,
      corsEnabled: false,
    },
  },
]);

let mainWindow: BrowserWindow | null = null;
let quickAskWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
let isQuitting = false;

function allowedAppOrigin(): string {
  if (DEV_RENDERER_URL) {
    return new URL(DEV_RENDERER_URL).origin;
  }
  return APP_ORIGIN;
}

function buildRendererUrl(query = ""): string {
  const base = DEV_RENDERER_URL || `${APP_ORIGIN}/index.html`;
  if (!query) return base;
  const separator = base.includes("?") ? "&" : "?";
  return `${base}${separator}${query}`;
}

function hardenWebContents(contents: WebContents): void {
  contents.setWindowOpenHandler(() => ({ action: "deny" }));
  contents.on("will-navigate", (event, targetUrl) => {
    try {
      const origin = new URL(targetUrl).origin;
      if (origin !== allowedAppOrigin()) {
        event.preventDefault();
      }
    } catch {
      event.preventDefault();
    }
  });
}

const PROD_CSP = [
  `default-src 'self' ${APP_ORIGIN}`,
  `script-src 'self' ${APP_ORIGIN}`,
  `style-src 'self' 'unsafe-inline' ${APP_ORIGIN}`,
  "connect-src ws://127.0.0.1:* ws://localhost:*",
  `img-src 'self' data: ${APP_ORIGIN}`,
  "object-src 'none'",
  "base-uri 'none'",
  "frame-ancestors 'none'",
].join("; ");

function registerCsp(): void {
  const csp = DEV_RENDERER_URL
    ? [
        "default-src 'self' http://127.0.0.1:5173",
        "script-src 'self' 'unsafe-inline' http://127.0.0.1:5173",
        "style-src 'self' 'unsafe-inline' http://127.0.0.1:5173",
        "connect-src 'self' http://127.0.0.1:5173 ws://127.0.0.1:* ws://localhost:*",
        "img-src 'self' data: http://127.0.0.1:5173",
        "object-src 'none'",
        "base-uri 'none'",
        "frame-ancestors 'none'",
      ].join("; ")
    : PROD_CSP;

  session.defaultSession.webRequest.onHeadersReceived((details, callback) => {
    const headers = { ...(details.responseHeaders ?? {}) };
    headers["Content-Security-Policy"] = [csp];
    callback({ responseHeaders: headers });
  });
}

function registerAppProtocol(): void {
  protocol.handle("clea-app", async (request) => {
    const resolved = resolveRendererAssetPath(RENDERER_DIR, request.url);
    if (!resolved) {
      return new Response("Forbidden", { status: 403 });
    }
    try {
      if (!fs.statSync(resolved).isFile()) {
        return new Response("Not Found", { status: 404 });
      }
    } catch {
      return new Response("Not Found", { status: 404 });
    }
    const upstream = await net.fetch(pathToFileURL(resolved).toString());
    const headers = new Headers(upstream.headers);
    headers.set("Content-Security-Policy", PROD_CSP);
    return new Response(upstream.body, {
      status: upstream.status,
      statusText: upstream.statusText,
      headers,
    });
  });
}

function createMainWindow(options: { startHidden?: boolean } = {}): BrowserWindow {
  const win = new BrowserWindow({
    width: 1080,
    height: 740,
    minWidth: 760,
    minHeight: 520,
    title: "Clea",
    backgroundColor: "#0b0d10",
    show: false,
    webPreferences: {
      preload: PRELOAD_PATH,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  hardenWebContents(win.webContents);

  win.once("ready-to-show", () => {
    if (!options.startHidden) {
      win.show();
    }
  });

  win.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      win.hide();
    }
  });

  void win.loadURL(buildRendererUrl());
  return win;
}

function createQuickAskWindow(): BrowserWindow {
  const display = screen.getPrimaryDisplay();
  const width = 580;
  const height = 360;
  const x = Math.round(display.workArea.x + (display.workArea.width - width) / 2);
  const y = Math.round(display.workArea.y + Math.min(140, display.workArea.height * 0.16));

  const win = new BrowserWindow({
    width,
    height,
    x,
    y,
    frame: false,
    transparent: true,
    alwaysOnTop: true,
    skipTaskbar: true,
    resizable: false,
    show: false,
    webPreferences: {
      preload: PRELOAD_PATH,
      contextIsolation: true,
      nodeIntegration: false,
      sandbox: true,
      webSecurity: true,
    },
  });

  hardenWebContents(win.webContents);

  win.on("blur", () => {
    win.hide();
  });

  win.on("close", (event) => {
    if (!isQuitting) {
      event.preventDefault();
      win.hide();
    }
  });

  void win.loadURL(buildRendererUrl("window=quick-ask"));
  return win;
}

function showMainWindow(): void {
  if (!mainWindow || mainWindow.isDestroyed()) {
    mainWindow = createMainWindow();
  }
  if (mainWindow.isMinimized()) {
    mainWindow.restore();
  }
  mainWindow.show();
  mainWindow.focus();
}

function toggleQuickAsk(): void {
  if (!quickAskWindow || quickAskWindow.isDestroyed()) {
    quickAskWindow = createQuickAskWindow();
  }
  if (quickAskWindow.isVisible()) {
    quickAskWindow.hide();
    return;
  }
  quickAskWindow.show();
  quickAskWindow.focus();
  const win = quickAskWindow;
  if (win.webContents.isLoading()) {
    win.webContents.once("did-finish-load", () => {
      if (!win.isDestroyed()) {
        win.webContents.send("clea:quick-ask-focus");
      }
    });
  } else {
    win.webContents.send("clea:quick-ask-focus");
  }
}

function createTray(): void {
  const image = nativeImage.createFromBuffer(createTrayIconPngBuffer());
  tray = new Tray(image);
  tray.setToolTip("Clea");
  tray.setContextMenu(
    Menu.buildFromTemplate([
      { label: "Open", click: showMainWindow },
      { label: "Quick Ask", click: toggleQuickAsk },
      { type: "separator" },
      {
        label: "Quit",
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on("click", showMainWindow);
}

function getAutoLaunchEnabled(): boolean {
  const cfg = buildLoginItemConfig(false, app.isPackaged, process.execPath, app.getAppPath());
  return app.getLoginItemSettings({ path: cfg.path, args: cfg.args }).openAtLogin;
}

function setAutoLaunchEnabled(enabled: boolean): boolean {
  const cfg = buildLoginItemConfig(enabled, app.isPackaged, process.execPath, app.getAppPath());
  app.setLoginItemSettings({
    openAtLogin: cfg.openAtLogin,
    path: cfg.path,
    args: cfg.args,
  });
  return getAutoLaunchEnabled();
}

function startBackgroundDaemon(): Promise<{ ok: boolean; error?: string }> {
  const spec = resolveDaemonLauncher(app.getAppPath());
  if (!spec) {
    return Promise.resolve({
      ok: false,
      error: "Could not locate the Clea .venv Python interpreter.",
    });
  }
  return new Promise((resolve) => {
    let settled = false;
    const finish = (result: { ok: boolean; error?: string }) => {
      if (settled) return;
      settled = true;
      resolve(result);
    };
    try {
      const child = spawn(spec.command, spec.args, {
        cwd: spec.cwd,
        detached: true,
        stdio: "ignore",
        windowsHide: true,
        shell: false,
      });
      const timer = setTimeout(() => {
        child.unref();
        finish({ ok: true });
      }, 400);
      child.once("error", (err) => {
        clearTimeout(timer);
        finish({ ok: false, error: err instanceof Error ? err.message : String(err) });
      });
      child.once("exit", (code) => {
        clearTimeout(timer);
        if (code === 0) {
          finish({ ok: true });
        } else {
          finish({
            ok: false,
            error: `Background daemon exited immediately (code ${code ?? "unknown"}). Check daemon.log.`,
          });
        }
      });
    } catch (err) {
      finish({
        ok: false,
        error: err instanceof Error ? err.message : String(err),
      });
    }
  });
}

const gotSingleInstanceLock = app.requestSingleInstanceLock();
if (!gotSingleInstanceLock) {
  app.quit();
} else {
  app.on("second-instance", (_event, argv) => {
    if (!argv.includes("--hidden")) {
      showMainWindow();
    }
  });
}

app.whenReady().then(() => {
  if (!gotSingleInstanceLock) return;

  if (!DEV_RENDERER_URL) {
    registerAppProtocol();
  }
  registerCsp();

  // Read session_token fresh from disk on every connect/reconnect; never cache it.
  ipcMain.handle("clea:get-connection", () => readDaemonConnection());
  ipcMain.handle("clea:get-auto-launch", () => getAutoLaunchEnabled());
  ipcMain.handle("clea:set-auto-launch", (_event, enabled: unknown) =>
    setAutoLaunchEnabled(Boolean(enabled)),
  );
  ipcMain.handle("clea:start-daemon", () => startBackgroundDaemon());
  ipcMain.handle(
    "clea:save-file",
    async (_event, { defaultPath, content }: { defaultPath: string; content: string }) => {
      const win = mainWindow && !mainWindow.isDestroyed() ? mainWindow : undefined;
      const { canceled, filePath } = await dialog.showSaveDialog(win as BrowserWindow, {
        defaultPath,
        filters: [{ name: "Markdown", extensions: ["md"] }],
      });
      if (canceled || !filePath) return false;
      await fs.promises.writeFile(filePath, content, "utf-8");
      return true;
    },
  );
  ipcMain.on("clea:close-quick-ask", () => {
    quickAskWindow?.hide();
  });

  const startHidden = shouldStartHidden(process.argv, app.getLoginItemSettings());
  mainWindow = createMainWindow({ startHidden });
  quickAskWindow = createQuickAskWindow();
  createTray();

  const { quickAskShortcut } = readDesktopConfig();
  if (!globalShortcut.register(quickAskShortcut, toggleQuickAsk)) {
    console.warn(`Could not register global shortcut: ${quickAskShortcut}`);
  }
});

app.on("before-quit", () => {
  isQuitting = true;
});

app.on("will-quit", () => {
  globalShortcut.unregisterAll();
});

app.on("window-all-closed", () => {
  // Stay resident in the system tray on Windows/macOS/Linux until Quit is clicked.
});
