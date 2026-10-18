import { cpSync, mkdirSync, rmSync } from "node:fs";
import { defineConfig } from "@playwright/test";

// Stages only what the page needs into www/ (gitignored) and serves that directory:
// the vendored KiCanvas bundle (frontend/public/vendor/kicanvas) and the spike's saved
// schematics (results/files) - nothing else of the checkout is reachable.
const here = new URL(".", import.meta.url).pathname;
const www = `${here}www`;
rmSync(www, { recursive: true, force: true });
mkdirSync(`${www}/vendor`, { recursive: true });
cpSync(`${here}index.html`, `${www}/index.html`);
cpSync(`${here}../../../frontend/public/vendor/kicanvas/kicanvas.js`, `${www}/vendor/kicanvas.js`);
cpSync(`${here}../results/files`, `${www}/files`, { recursive: true });

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
    command: "python3 -m http.server 8766 --bind 127.0.0.1 --directory www",
    url: "http://127.0.0.1:8766/index.html",
    reuseExistingServer: false,
  },
});
