/// <reference types="vitest/config" />
import { readFileSync } from 'node:fs';
import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const backend = process.env.COPPERCHAT_BACKEND ?? 'http://localhost:8080';
// The build version (UpdateBanner): frontend/package.json, since the Docker frontend stage has no
// backend files; scripts/check-docs.sh keeps it equal to the backend version.
const pkg = JSON.parse(readFileSync(new URL('./package.json', import.meta.url), 'utf8')) as { version: string };

export default defineConfig({
  plugins: [react(), tailwindcss()],
  define: { __COPPERCHAT_VERSION__: JSON.stringify(pkg.version) },
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: true,
    port: 5173,
    // REST and the /api/ws WebSocket both go to the FastAPI backend (same origin for cookies).
    // changeOrigin stays false: the backend checks that Origin matches Host on the WebSocket
    // (close code 4403 otherwise), so the proxy must forward the browser's Host unchanged.
    proxy: { '/api': { target: backend, changeOrigin: false, ws: true } },
  },
  build: { outDir: 'dist', sourcemap: true },
  test: {
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    restoreMocks: true,
    unstubGlobals: true,
    css: false,
  },
});
