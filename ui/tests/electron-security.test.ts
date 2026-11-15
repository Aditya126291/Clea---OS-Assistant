import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  buildLoginItemConfig,
  DEFAULT_QUICK_ASK_SHORTCUT,
  readDaemonConnection,
  readDesktopConfig,
  resolveActiveRendererDir,
  resolveDaemonLauncher,
  resolveRendererAssetPath,
  shouldStartHidden,
} from "../electron/paths";
import { createTrayIconPngBuffer } from "../electron/tray-icon";

describe("Electron main helpers", () => {
  const tmpDirs: string[] = [];

  afterEach(() => {
    for (const dir of tmpDirs.splice(0)) {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("scopes clea-app:// requests strictly to rendererDir", () => {
    const rendererDir = path.resolve(os.tmpdir(), "clea-renderer");
    expect(resolveRendererAssetPath(rendererDir, "clea-app://app/")).toBe(
      path.join(rendererDir, "index.html"),
    );
    expect(resolveRendererAssetPath(rendererDir, "clea-app://app/assets/main.js")).toBe(
      path.join(rendererDir, "assets", "main.js"),
    );
    expect(
      resolveRendererAssetPath(rendererDir, "clea-app://app/%2e%2e/%2e%2e/etc/passwd"),
    ).toBeNull();
    expect(
      resolveRendererAssetPath(rendererDir, "clea-app://app/..%5c..%5cWindows/win.ini"),
    ).toBeNull();
    expect(resolveRendererAssetPath(rendererDir, "clea-app://evil/index.html")).toBeNull();
    expect(resolveRendererAssetPath(rendererDir, "https://example.com/index.html")).toBeNull();
  });

  it("reads daemon connection and desktop shortcut config from data dir", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-electron-"));
    tmpDirs.push(tmpDir);

    expect(readDaemonConnection({ CLEA_DATA_DIR: tmpDir })).toBeNull();
    expect(readDesktopConfig({ CLEA_DATA_DIR: tmpDir })).toEqual({
      quickAskShortcut: DEFAULT_QUICK_ASK_SHORTCUT,
    });

    fs.writeFileSync(path.join(tmpDir, "session_token"), "tok-123\n", "utf-8");
    fs.writeFileSync(
      path.join(tmpDir, "desktop_config.json"),
      JSON.stringify({ quickAskShortcut: "Alt+Space" }),
      "utf-8",
    );

    expect(readDaemonConnection({ CLEA_DATA_DIR: tmpDir })).toEqual({
      token: "tok-123",
      wsUrl: "ws://127.0.0.1:8765",
    });
    expect(readDesktopConfig({ CLEA_DATA_DIR: tmpDir })).toEqual({
      quickAskShortcut: "Alt+Space",
    });
  });

  it("generates a valid PNG tray icon buffer", () => {
    const png = createTrayIconPngBuffer();
    expect(png.subarray(0, 8)).toEqual(Buffer.from([137, 80, 78, 71, 13, 10, 26, 10]));
    expect(png.length).toBeGreaterThan(50);
  });

  it("re-reads session_token fresh from disk after a late daemon start or restart", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-token-refresh-"));
    tmpDirs.push(tmpDir);
    const tokenPath = path.join(tmpDir, "session_token");

    expect(readDaemonConnection({ CLEA_DATA_DIR: tmpDir })).toBeNull();

    fs.writeFileSync(tokenPath, "tok-first", "utf-8");
    expect(readDaemonConnection({ CLEA_DATA_DIR: tmpDir })).toEqual({
      token: "tok-first",
      wsUrl: "ws://127.0.0.1:8765",
    });

    fs.writeFileSync(tokenPath, "tok-restarted", "utf-8");
    expect(readDaemonConnection({ CLEA_DATA_DIR: tmpDir })).toEqual({
      token: "tok-restarted",
      wsUrl: "ws://127.0.0.1:8765",
    });
  });

  it("builds login item options and detects hidden startup flags", () => {
    expect(shouldStartHidden(["clea.exe"])).toBe(false);
    expect(shouldStartHidden(["clea.exe", "--hidden"])).toBe(true);
    expect(shouldStartHidden(["clea.exe"], { wasOpenedAtLogin: true })).toBe(true);

    expect(buildLoginItemConfig(true, false, "electron.exe", "C:\\Clea\\ui")).toEqual({
      openAtLogin: true,
      path: "electron.exe",
      args: ["C:\\Clea\\ui", "--hidden"],
    });
    expect(buildLoginItemConfig(false, true, "Clea.exe", "C:\\Clea\\ui")).toEqual({
      openAtLogin: false,
      path: "Clea.exe",
      args: ["--hidden"],
    });
  });

  it("resolves the project venv pythonw background launcher when present", () => {
    const repoDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-repo-"));
    tmpDirs.push(repoDir);
    const uiDir = path.join(repoDir, "ui");
    fs.mkdirSync(uiDir);
    fs.writeFileSync(path.join(repoDir, "pyproject.toml"), "[project]\nname='clea'\n", "utf-8");

    expect(resolveDaemonLauncher(uiDir, "win32")).toBeNull();

    const pythonw = path.join(repoDir, ".venv", "Scripts", "pythonw.exe");
    fs.mkdirSync(path.dirname(pythonw), { recursive: true });
    fs.writeFileSync(pythonw, "", "utf-8");

    expect(resolveDaemonLauncher(uiDir, "win32")).toEqual({
      command: pythonw,
      args: ["-m", "clea", "daemon", "--background"],
      cwd: repoDir,
    });
  });

  it("selects the fresher renderer bundle between out/renderer and dist", () => {
    const baseDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-renderer-sync-"));
    tmpDirs.push(baseDir);
    const outRenderer = path.join(baseDir, "out", "renderer");
    const distDir = path.join(baseDir, "dist");
    fs.mkdirSync(outRenderer, { recursive: true });
    fs.mkdirSync(distDir, { recursive: true });

    const outIndex = path.join(outRenderer, "index.html");
    const distIndex = path.join(distDir, "index.html");
    fs.writeFileSync(outIndex, "<html>out</html>", "utf-8");
    fs.writeFileSync(distIndex, "<html>dist</html>", "utf-8");

    const now = new Date();
    const older = new Date(now.getTime() - 10_000);
    fs.utimesSync(outIndex, older, older);
    fs.utimesSync(distIndex, now, now);

    expect(resolveActiveRendererDir(outRenderer, distDir)).toBe(distDir);

    const newer = new Date(now.getTime() + 10_000);
    fs.utimesSync(outIndex, newer, newer);
    expect(resolveActiveRendererDir(outRenderer, distDir)).toBe(outRenderer);
  });
});
