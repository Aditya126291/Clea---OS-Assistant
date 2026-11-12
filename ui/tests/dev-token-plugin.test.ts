import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { afterEach, describe, expect, it } from "vitest";
import { handleDevTokenRequest } from "../dev-token-plugin";

function makeRes(initialHeaders: Record<string, string> = {}) {
  const headers: Record<string, string> = { ...initialHeaders };
  let body = "";
  const res = {
    statusCode: 200,
    setHeader(name: string, value: string | number | readonly string[]) {
      headers[name.toLowerCase()] = String(value);
      return res;
    },
    removeHeader(name: string) {
      delete headers[name.toLowerCase()];
    },
    end(chunk?: unknown) {
      body = chunk ? String(chunk) : "";
      return res;
    },
  };
  return { res, headers, getBody: () => body };
}

describe("cleaDevTokenPlugin", () => {
  const tmpDirs: string[] = [];

  afterEach(() => {
    for (const dir of tmpDirs.splice(0)) {
      fs.rmSync(dir, { recursive: true, force: true });
    }
  });

  it("serves token only to localhost Host/Origin with no CORS headers", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-dev-tok-"));
    tmpDirs.push(tmpDir);
    fs.writeFileSync(path.join(tmpDir, "session_token"), "tok-abc\n", "utf-8");

    const ok = makeRes({ "access-control-allow-origin": "http://localhost:5173" });
    handleDevTokenRequest(
      {
        url: "/",
        method: "GET",
        headers: { host: "127.0.0.1:5173", origin: "http://localhost:5173" },
      },
      ok.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(ok.res.statusCode).toBe(200);
    expect(JSON.parse(ok.getBody())).toEqual({ token: "tok-abc", wsUrl: "ws://127.0.0.1:8765" });
    expect(ok.headers["access-control-allow-origin"]).toBeUndefined();
    expect(ok.headers["cache-control"]).toBe("no-store");

    const badHost = makeRes();
    handleDevTokenRequest(
      { url: "/", method: "GET", headers: { host: "evil.example:5173" } },
      badHost.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(badHost.res.statusCode).toBe(403);

    const badOrigin = makeRes();
    handleDevTokenRequest(
      {
        url: "/",
        method: "GET",
        headers: { host: "127.0.0.1:5173", origin: "https://evil.example" },
      },
      badOrigin.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(badOrigin.res.statusCode).toBe(403);

    const nullOrigin = makeRes();
    handleDevTokenRequest(
      { url: "/", method: "GET", headers: { host: "127.0.0.1:5173", origin: "null" } },
      nullOrigin.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(nullOrigin.res.statusCode).toBe(403);

    const userinfoHost = makeRes();
    handleDevTokenRequest(
      { url: "/", method: "GET", headers: { host: "user@127.0.0.1:5173" } },
      userinfoHost.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(userinfoHost.res.statusCode).toBe(403);
  });

  it("returns 404 when daemon token file is absent", () => {
    const tmpDir = fs.mkdtempSync(path.join(os.tmpdir(), "clea-dev-empty-"));
    tmpDirs.push(tmpDir);
    const missing = makeRes();
    handleDevTokenRequest(
      { url: "/", method: "GET", headers: { host: "localhost:5173" } },
      missing.res,
      { CLEA_DATA_DIR: tmpDir },
    );
    expect(missing.res.statusCode).toBe(404);
  });
});
