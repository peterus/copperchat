import { defineConfig } from "@playwright/test";

export default defineConfig({
  testDir: "./tests",
  timeout: 180_000,
  workers: 1,
  use: {
    baseURL: "http://127.0.0.1:8766",
    viewport: { width: 1400, height: 900 },
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader"] },
  },
  webServer: {
    command: "python3 -m http.server 8766 --bind 127.0.0.1",
    url: "http://127.0.0.1:8766/viewer/index.html",
    reuseExistingServer: false,
  },
});
