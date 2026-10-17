import { defineConfig, devices } from '@playwright/test';

// E2E_BASE_URL points the suite at an already running app (e.g. backend + `npm run dev`);
// otherwise Playwright starts its own Vite dev server on E2E_PORT (the API is mocked per test).
const external = process.env.E2E_BASE_URL;
const port = Number(process.env.E2E_PORT ?? 5199);

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: { baseURL: external ?? `http://localhost:${port}`, trace: 'retain-on-failure' },
  webServer: external
    ? undefined
    : {
        command: `npm run dev -- --host localhost --port ${port} --strictPort`,
        url: `http://localhost:${port}`,
        // never attach to some other dev server that happens to listen on the port
        reuseExistingServer: false,
        timeout: 60_000,
      },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', use: { ...devices['Pixel 7'] } },
  ],
});
