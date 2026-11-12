import { execSync } from "node:child_process";
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";

const UI_ROOT = path.resolve(import.meta.dirname, "..");
const DIST_DIR = path.join(UI_ROOT, "dist");

function readAllBuiltText(dir: string): string {
  const entries = fs.readdirSync(dir, { withFileTypes: true });
  return entries
    .map((entry) => {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) return readAllBuiltText(full);
      if (entry.name.endsWith(".js") || entry.name.endsWith(".html")) {
        return fs.readFileSync(full, "utf-8");
      }
      return "";
    })
    .join("\n");
}

describe("Production bundle hygiene", () => {
  it("excludes dev-only mocks and the /__clea/token endpoint from vite build output even if VITE_CLEA_MOCKS=true", () => {
    const cleanEnv: NodeJS.ProcessEnv = {
      ...process.env,
      NODE_ENV: "production",
      VITE_CLEA_MOCKS: "true",
    };
    delete cleanEnv.VITEST;

    execSync("pnpm build", { cwd: UI_ROOT, stdio: "pipe", env: cleanEnv });
    expect(fs.existsSync(DIST_DIR)).toBe(true);

    const bundleText = readAllBuiltText(DIST_DIR);
    expect(bundleText).not.toContain("__clea/token");
    expect(bundleText).not.toContain("dev-voice-mock-panel");
    expect(bundleText).not.toContain("Dev Mock");
  }, 30_000);
});
