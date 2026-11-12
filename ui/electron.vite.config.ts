import path from "node:path";
import tailwindcss from "@tailwindcss/vite";
import react from "@vitejs/plugin-react";
import { defineConfig } from "electron-vite";
import { cleaDevTokenPlugin } from "./dev-token-plugin";

export default defineConfig({
  main: {
    build: {
      outDir: "out/main",
      rollupOptions: {
        input: path.resolve(import.meta.dirname, "electron/main.ts"),
      },
    },
  },
  preload: {
    build: {
      outDir: "out/preload",
      rollupOptions: {
        input: path.resolve(import.meta.dirname, "electron/preload.ts"),
        output: {
          format: "cjs",
          entryFileNames: "[name].cjs",
        },
      },
    },
  },
  renderer: {
    root: import.meta.dirname,
    plugins: [react(), tailwindcss(), cleaDevTokenPlugin()],
    resolve: {
      alias: {
        "@": path.resolve(import.meta.dirname, "src"),
      },
    },
    build: {
      outDir: "out/renderer",
      rollupOptions: {
        input: path.resolve(import.meta.dirname, "index.html"),
      },
    },
    server: {
      host: "127.0.0.1",
      port: 5173,
      strictPort: true,
    },
  },
});
