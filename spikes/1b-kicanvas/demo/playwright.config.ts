import { defineConfig } from "@playwright/test";

// Runs against the already-running demo server (./serve.sh); no webServer here.
export default defineConfig({
  testDir: "./tests",
  timeout: 400_000,
  workers: 1,
  use: {
    baseURL: process.env.DEMO_URL ?? "http://127.0.0.1:8765",
    viewport: { width: 1400, height: 900 },
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  },
});
