import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import {
  DEFAULT_QUICK_ASK_SHORTCUT,
  readDaemonConnection,
  readDesktopConfig,
  resolveRendererAssetPath,
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
});
