import { defineConfig } from "@playwright/test";

// Serves the worktree root so the page can load the vendored KiCanvas bundle
// (frontend/public/vendor/kicanvas) and the spike's saved schematics (results/files).
export default defineConfig({
  testDir: ".",
  testMatch: "*.spec.ts",
  timeout: 120_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8766",
    viewport: { width: 1400, height: 900 },
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  },
  webServer: {
    command: "python3 -m http.server 8766 --bind 127.0.0.1 --directory ../../..",
    url: "http://127.0.0.1:8766/spikes/2a-sch-write/kicanvas/index.html",
    reuseExistingServer: false,
  },
});
