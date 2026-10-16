# Phase 6A: Frontend shell + chat

> **Cross-phase corrections apply:** see index § "Cross-phase corrections" (X16); they supersede conflicting code in this file.

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship the CopperChat web client shell: Vite/React/TS scaffold with a CSS-variable theme, login, project picker with lock warning, responsive layouts (desktop/iPad landscape split, iPad portrait sidebar, iPhone tab bar), a schema-validated singleton WebSocket client, and a chat (store + history + composer + queue + Stop + usage + rate-limit banner) that has every Kontor chat-history lesson (spec §5.2) built in and tested.

**Depends on:** Phase 5 (WS protocol, `GET /projects/{id}/messages`, `protocol-fixtures/*.json`), Phase 3 (auth + projects endpoints).

**Parallel with:** 4, 7.

**Read first:** the index `docs/superpowers/plans/2026-10-15-foundation-plan.md` (Global Constraints, Review Focus 1, C7, C8, Phase Conventions); spec §4.2, §5.1, §5.2, §12; Kontor reference (read-only): `~/CloudCLI/kontor/src/stores/chat.ts`, `~/CloudCLI/kontor/src/components/MessageList.tsx`, `~/CloudCLI/kontor/src/lib/ws.ts`, `~/CloudCLI/kontor/docs/superpowers/specs/2026-04-30-chat-pipeline-design.md`.

**PR title:** `foundation phase 6A: frontend shell, chat history and composer`

**Files:**
- Create: `frontend/package.json`, `frontend/package-lock.json` (generated), `frontend/index.html`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, `frontend/eslint.config.js`, `frontend/playwright.config.ts`, `frontend/.gitignore`
- Create: `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/App.test.tsx`
- Create: `frontend/src/theme/theme.css`, `frontend/src/theme/theme.test.ts`
- Create: `frontend/src/api/http.ts`, `frontend/src/api/http.test.ts`, `frontend/src/api/ws.ts`, `frontend/src/api/ws.test.ts`
- Create: `frontend/src/protocol/frames.ts`, `frontend/src/protocol/frames.test.ts`
- Create: `frontend/src/stores/auth.ts`, `frontend/src/stores/projects.ts`, `frontend/src/stores/chat.ts`, `frontend/src/stores/composer.ts`, `frontend/src/stores/ui.ts`
- Create: `frontend/src/stores/chat.ordering.test.ts`, `frontend/src/stores/chat.reconnect.test.ts`, `frontend/src/stores/composer.test.ts`, `frontend/src/stores/ui.test.ts`, `frontend/src/stores/projects.test.tsx`
- Create: `frontend/src/hooks/useChat.ts`, `frontend/src/hooks/useMediaQuery.ts`, `frontend/src/hooks/useKeyboardInset.ts`
- Create: `frontend/src/components/chat/MessageList.tsx`, `MessageList.test.tsx`, `RunControls.tsx`, `RunControls.test.tsx`, `MessageItem.tsx`, `MessageItem.test.tsx`, `Markdown.tsx`, `Lightbox.tsx`, `Composer.tsx`, `Composer.test.tsx`, `composerSlots.ts`, `UsageIndicator.tsx`, `RateLimitBanner.tsx`, `ChatPanel.tsx`, `ChatPanel.test.tsx`, `usage.test.tsx` (all under `frontend/src/components/chat/`)
- Create: `frontend/src/components/PoweredBy.tsx`, `frontend/src/components/icons.tsx`, `frontend/src/components/viewer/ViewerSlot.tsx`
- Create: `frontend/src/layouts/panels.ts`, `frontend/src/layouts/panels.test.tsx`, `frontend/src/layouts/ProjectLayout.tsx`, `frontend/src/layouts/ProjectLayout.test.tsx`, `frontend/src/panels/index.ts`
- Create: `frontend/src/pages/LoginPage.tsx`, `frontend/src/pages/LoginPage.test.tsx`, `frontend/src/pages/RequireAuth.tsx`, `frontend/src/pages/ProjectPickerPage.tsx`, `frontend/src/pages/ProjectPage.tsx`, `frontend/src/pages/SettingsPage.tsx`, `frontend/src/pages/SettingsPage.test.tsx`, `frontend/src/pages/settings/NotificationsSection.tsx`
- Create: `frontend/src/test/setup.ts`, `frontend/src/test/media.ts`, `frontend/src/test/fakeWebSocket.ts`, `frontend/src/test/fakeWsClient.ts`, `frontend/src/test/fakeMessageServer.ts`
- Create: `frontend/e2e/smoke.spec.ts`
- Modify: `.github/workflows/ci.yml` (add `frontend` job)

**Acceptance criteria:**
- `cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build` passes.
- `frames.test.ts` validates every `protocol-fixtures/*.json` file and fails if a C7 frame type has no fixture.
- Chat store tests cover: subscribe only after the HTTP baseline; double mount → one fetch, one subscribe; replay overlap and out-of-order frames → one row per id in id order; optimistic row replaced by server row with the same `clientMessageId` (also on retransmit); older pages via `before=oldestId`; failed older load resets `loadingOlder`; reconnect resets `runActive` and resubscribes from `lastSeenId`; half-streamed rows healed by HTTP merge; unknown-id delta triggers heal; `visibilitychange → visible` heals.
- MessageList tests cover element-based scroll anchoring on prepend, stick-to-bottom within 80 px, "↓ newest" pill, top-sentinel load gated on not-stuck-to-bottom; RunControls test covers "stuck Stop" after a reconnect that swallowed `run-finished`.
- UI never says "Claude Code"; Login and Settings show "Powered by Claude".
- `.github/workflows/ci.yml` has a `frontend` job (Node 20, `npm ci`, typecheck, lint, vitest, build). Playwright is not run in CI.

**Verification commands:**
```bash
cd frontend && npm ci && npm run typecheck && npm run lint && npm test -- --run && npm run build
cd frontend && npx playwright install chromium && npm run e2e   # optional, local only (mocked API)
```

**CI expectations:** existing backend job stays green; new `frontend` job green. No `e2e` job.

---

### Task 6A.1: Scaffold, theme tokens, CI job

**Files:**
- Create: `frontend/package.json`, `frontend/index.html`, `frontend/vite.config.ts`, `frontend/tsconfig.json`, `frontend/tsconfig.node.json`, `frontend/eslint.config.js`, `frontend/.gitignore`, `frontend/src/main.tsx`, `frontend/src/App.tsx`, `frontend/src/theme/theme.css`, `frontend/src/test/setup.ts`, `frontend/src/test/media.ts`
- Test: `frontend/src/theme/theme.test.ts`
- Modify: `.github/workflows/ci.yml`

**Interfaces:**
- Produces: npm scripts `dev`, `build`, `typecheck`, `lint`, `test`, `e2e`; path alias `@/*` → `frontend/src/*`; Tailwind colour utilities `bg-bg`, `bg-surface`, `bg-surface-2`, `text-fg`, `text-muted`, `border-border`, `bg-accent`, `text-accent-fg`, `text-danger`, `bg-danger`, `text-warning`, `bg-warning`, `text-success` backed by `--cc-*` CSS variables on `:root[data-theme]`; test helper `setMedia(matcher: (query: string) => boolean)`.

- [ ] **Step 1: Write the failing test**

`frontend/src/theme/theme.test.ts`:
```ts
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const css = readFileSync(fileURLToPath(new URL('./theme.css', import.meta.url)), 'utf8');

function tokensOf(selector: string): string[] {
  const start = css.indexOf(selector);
  expect(start, `selector ${selector} missing`).toBeGreaterThanOrEqual(0);
  const body = css.slice(css.indexOf('{', start) + 1, css.indexOf('}', start));
  return [...body.matchAll(/(--cc-[a-z0-9-]+)\s*:/g)].map((m) => m[1] ?? '').sort();
}

describe('theme tokens', () => {
  it('light and dark define the same token set', () => {
    const light = tokensOf(':root[data-theme="light"]');
    const dark = tokensOf(':root[data-theme="dark"]');
    expect(light.length).toBeGreaterThanOrEqual(10);
    expect(dark).toEqual(light);
  });

  it('every token is exposed to Tailwind via @theme inline', () => {
    for (const token of tokensOf(':root[data-theme="light"]')) {
      const name = token.replace('--cc-', '');
      expect(css).toContain(`--color-${name}: var(${token})`);
    }
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/theme/theme.test.ts` (after creating `package.json` + `npm install`, see Step 3a)
Expected: FAIL — `ENOENT ... theme.css`.

- [ ] **Step 3: Write minimal implementation**

3a. `frontend/package.json`, then run `cd frontend && npm install` to create `package-lock.json`:
```json
{
  "name": "copperchat-frontend",
  "private": true,
  "version": "0.0.0",
  "type": "module",
  "license": "GPL-3.0-only",
  "engines": { "node": ">=20.19" },
  "scripts": {
    "dev": "vite",
    "build": "vite build",
    "preview": "vite preview",
    "typecheck": "tsc -p tsconfig.json && tsc -p tsconfig.node.json",
    "lint": "eslint .",
    "test": "vitest",
    "e2e": "playwright test"
  },
  "dependencies": {
    "react": "^19.1.0",
    "react-dom": "^19.1.0",
    "react-markdown": "^10.1.0",
    "react-router-dom": "^7.9.0",
    "rehype-sanitize": "^6.0.0",
    "remark-gfm": "^4.0.1",
    "zod": "^4.1.0",
    "zustand": "^5.0.8"
  },
  "devDependencies": {
    "@eslint/js": "^9.35.0",
    "@playwright/test": "^1.55.0",
    "@tailwindcss/vite": "^4.1.0",
    "@testing-library/dom": "^10.4.0",
    "@testing-library/jest-dom": "^6.8.0",
    "@testing-library/react": "^16.3.0",
    "@testing-library/user-event": "^14.6.0",
    "@types/node": "^22.0.0",
    "@types/react": "^19.1.0",
    "@types/react-dom": "^19.1.0",
    "@vitejs/plugin-react": "^5.0.0",
    "eslint": "^9.35.0",
    "eslint-plugin-react-hooks": "^5.2.0",
    "globals": "^16.4.0",
    "jsdom": "^27.0.0",
    "tailwindcss": "^4.1.0",
    "typescript": "~5.9.0",
    "typescript-eslint": "^8.44.0",
    "vite": "^7.1.0",
    "vitest": "^4.0.0"
  }
}
```
All dependencies are MIT except `@playwright/test` and `typescript` (Apache-2.0) — GPL-3.0 compatible.

3b. `frontend/vite.config.ts`:
```ts
/// <reference types="vitest/config" />
import { fileURLToPath, URL } from 'node:url';
import tailwindcss from '@tailwindcss/vite';
import react from '@vitejs/plugin-react';
import { defineConfig } from 'vite';

const backend = process.env.COPPERCHAT_BACKEND ?? 'http://localhost:8080';

export default defineConfig({
  plugins: [react(), tailwindcss()],
  resolve: {
    alias: {
      '@': fileURLToPath(new URL('./src', import.meta.url)),
    },
  },
  server: {
    host: true,
    port: 5173,
    // REST and the /api/ws WebSocket both go to the FastAPI backend (same origin for cookies).
    proxy: { '/api': { target: backend, changeOrigin: true, ws: true } },
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
```

3c. `frontend/tsconfig.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023", "DOM", "DOM.Iterable"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "jsx": "react-jsx",
    "strict": true,
    "noUncheckedIndexedAccess": true,
    "noImplicitOverride": true,
    "noFallthroughCasesInSwitch": true,
    "isolatedModules": true,
    "resolveJsonModule": true,
    "skipLibCheck": true,
    "noEmit": true,
    "types": ["vite/client", "node"],
    "paths": { "@/*": ["./src/*"] }
  },
  "include": ["src"]
}
```
`frontend/tsconfig.node.json`:
```json
{
  "compilerOptions": {
    "target": "ES2022",
    "lib": ["ES2023", "DOM"],
    "module": "ESNext",
    "moduleResolution": "bundler",
    "strict": true,
    "skipLibCheck": true,
    "noEmit": true,
    "types": ["node"]
  },
  "include": ["vite.config.ts", "playwright.config.ts", "e2e"]
}
```

3d. `frontend/eslint.config.js`:
```js
import js from '@eslint/js';
import reactHooks from 'eslint-plugin-react-hooks';
import globals from 'globals';
import tseslint from 'typescript-eslint';

export default tseslint.config(
  { ignores: ['dist', 'vendor', 'playwright-report', 'test-results'] },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ['**/*.{ts,tsx}'],
    languageOptions: { globals: { ...globals.browser, ...globals.node } },
    plugins: { 'react-hooks': reactHooks },
    rules: {
      ...reactHooks.configs.recommended.rules,
      '@typescript-eslint/no-unused-vars': [
        'error',
        { argsIgnorePattern: '^_', varsIgnorePattern: '^_', ignoreRestSiblings: true },
      ],
    },
  },
);
```
`frontend/.gitignore`:
```
node_modules
dist
playwright-report
test-results
```

3e. `frontend/src/theme/theme.css`:
```css
@import 'tailwindcss';

@custom-variant dark (&:where([data-theme='dark'], [data-theme='dark'] *));

:root[data-theme="light"] {
  --cc-bg: #ffffff;
  --cc-surface: #f6f7f9;
  --cc-surface-2: #eceef2;
  --cc-fg: #16181d;
  --cc-muted: #5b6270;
  --cc-border: #d9dde4;
  --cc-accent: #b8641f;
  --cc-accent-fg: #ffffff;
  --cc-danger: #c0392b;
  --cc-warning: #9a6412;
  --cc-success: #2f855a;
  color-scheme: light;
}

:root[data-theme="dark"] {
  --cc-bg: #0f1115;
  --cc-surface: #171a21;
  --cc-surface-2: #20242d;
  --cc-fg: #e6e8ee;
  --cc-muted: #9aa2b1;
  --cc-border: #2c313c;
  --cc-accent: #e08a3c;
  --cc-accent-fg: #14161a;
  --cc-danger: #ef6b5b;
  --cc-warning: #e2b053;
  --cc-success: #5fbf8a;
  color-scheme: dark;
}

@theme inline {
  --color-bg: var(--cc-bg);
  --color-surface: var(--cc-surface);
  --color-surface-2: var(--cc-surface-2);
  --color-fg: var(--cc-fg);
  --color-muted: var(--cc-muted);
  --color-border: var(--cc-border);
  --color-accent: var(--cc-accent);
  --color-accent-fg: var(--cc-accent-fg);
  --color-danger: var(--cc-danger);
  --color-warning: var(--cc-warning);
  --color-success: var(--cc-success);
}

html,
body,
#root {
  height: 100%;
}

body {
  @apply bg-bg text-fg antialiased;
  font-family: ui-sans-serif, system-ui, -apple-system, 'Segoe UI', sans-serif;
}

.cc-markdown p { margin: 0.25rem 0; }
.cc-markdown pre { @apply bg-surface-2 rounded p-2 overflow-x-auto text-sm; }
.cc-markdown code { @apply font-mono text-[0.9em]; }
.cc-markdown ul { @apply list-disc pl-5; }
.cc-markdown ol { @apply list-decimal pl-5; }
.cc-markdown a { @apply text-accent underline; }
.cc-markdown table { @apply border-collapse text-sm; }
.cc-markdown th, .cc-markdown td { @apply border border-border px-2 py-1; }
```

3f. `frontend/index.html` (the inline script prevents a theme flash; Task 6A.12 owns the store that writes the key):
```html
<!doctype html>
<html lang="en" data-theme="light">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover, interactive-widget=resizes-content" />
    <title>CopperChat</title>
    <script>
      try {
        var p = localStorage.getItem('copperchat.theme') || 'system';
        var dark = p === 'dark' || (p === 'system' && window.matchMedia('(prefers-color-scheme: dark)').matches);
        document.documentElement.dataset.theme = dark ? 'dark' : 'light';
      } catch (e) {}
    </script>
  </head>
  <body>
    <div id="root"></div>
    <script type="module" src="/src/main.tsx"></script>
  </body>
</html>
```
`frontend/src/main.tsx`:
```tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@/App';
import '@/theme/theme.css';

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```
`frontend/src/App.tsx` (replaced in Task 6A.13):
```tsx
export function App() {
  return <main className="p-4">CopperChat</main>;
}
```

3g. Test infrastructure. `frontend/src/test/media.ts`:
```ts
type Matcher = (query: string) => boolean;
type Listener = (event: MediaQueryListEvent) => void;

let matcher: Matcher = () => false;
const lists: Array<{ query: string; listeners: Set<Listener> }> = [];

/** Change which media queries match; notifies every live MediaQueryList listener. */
export function setMedia(next: Matcher): void {
  matcher = next;
  for (const entry of lists) {
    const event = { matches: matcher(entry.query), media: entry.query } as MediaQueryListEvent;
    for (const listener of [...entry.listeners]) listener(event);
  }
}

export function resetMedia(): void {
  matcher = () => false;
  lists.length = 0;
}

export function installMatchMedia(): void {
  window.matchMedia = (query: string): MediaQueryList => {
    const entry = { query, listeners: new Set<Listener>() };
    lists.push(entry);
    return {
      get matches() {
        return matcher(query);
      },
      media: query,
      onchange: null,
      addEventListener: (_type: string, listener: Listener) => entry.listeners.add(listener),
      removeEventListener: (_type: string, listener: Listener) => entry.listeners.delete(listener),
      addListener: (listener: Listener) => entry.listeners.add(listener),
      removeListener: (listener: Listener) => entry.listeners.delete(listener),
      dispatchEvent: () => false,
    } as unknown as MediaQueryList;
  };
}
```
`frontend/src/test/setup.ts`:
```ts
import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach } from 'vitest';
import { installMatchMedia, resetMedia } from './media';

installMatchMedia();
// jsdom has no layout: give elements a no-op scrollIntoView that tests can spy on.
Element.prototype.scrollIntoView = function scrollIntoView() {};
if (typeof window.requestAnimationFrame !== 'function') {
  window.requestAnimationFrame = (cb: FrameRequestCallback) => window.setTimeout(() => cb(performance.now()), 0);
  window.cancelAnimationFrame = (id: number) => window.clearTimeout(id);
}

afterEach(() => {
  cleanup();
  resetMedia();
  localStorage.clear();
});
```

3h. CI. Append to `.github/workflows/ci.yml` under `jobs:` (Phase 3 created the file):
```yaml
  frontend:
    runs-on: ubuntu-latest
    defaults:
      run:
        working-directory: frontend
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-node@v4
        with:
          node-version: '20'
          cache: npm
          cache-dependency-path: frontend/package-lock.json
      - run: npm ci
      - run: npm run typecheck
      - run: npm run lint
      - run: npm test -- --run
      - run: npm run build
```
Note: `frames.test.ts` (Task 6A.2) reads `../protocol-fixtures`, which the checkout provides.

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/theme/theme.test.ts && npm run typecheck && npm run lint && npm run build`
Expected: 2 tests pass; typecheck, lint and build succeed.

- [ ] **Step 5: Commit**
```bash
git add frontend .github/workflows/ci.yml
git commit -m "chore: scaffold frontend (Vite, React 19, Tailwind 4 theme tokens) and CI job

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.2: Protocol mirror and singleton WebSocket client

**Files:**
- Create: `frontend/src/protocol/frames.ts`, `frontend/src/api/ws.ts`, `frontend/src/test/fakeWebSocket.ts`
- Test: `frontend/src/protocol/frames.test.ts`, `frontend/src/api/ws.test.ts`

**Interfaces:**
- Consumes: C7 frame table; `protocol-fixtures/<type>.json` (Phase 5; a file holds one frame object or an array of frames of that type).
- Produces (`@/protocol/frames`): zod schemas + same-named types `MessageDTO`, `AttachmentRef`, `Usage`, `ClientFrame`, `ServerFrame`, `MessagesPage`, `DocKind`, `MessageStatus`; `AnyFrame`; `ALL_FRAME_TYPES: readonly string[]`; `MessageDeltaFrame = Extract<ServerFrame, {type: 'message-delta'}>`.
- Produces (`@/api/ws`):
  ```ts
  type ConnectionState = 'open' | 'closed' | 'resumed';
  interface WsClient {
    send(frame: ClientFrame): void;              // validated; queued while not open
    onFrame(listener: (frame: ServerFrame) => void): () => void;
    onConnection(listener: (state: ConnectionState) => void): () => void;
    ensure(): void;                              // connect if not connected
    isOpen(): boolean;
    close(): void;                               // stop + no reconnect until ensure()
  }
  function createWsClient(opts?: WsClientOptions): WsClient;
  function backoffDelay(attempt: number, baseMs?: number, maxMs?: number): number;
  const wsClient: WsClient;                       // app singleton, URL /api/ws
  ```

- [ ] **Step 1: Write the failing test**

`frontend/src/protocol/frames.test.ts`:
```ts
import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { ALL_FRAME_TYPES, AnyFrame, ClientFrame, ServerFrame } from './frames';

const dir = fileURLToPath(new URL('../../../protocol-fixtures/', import.meta.url));
const files = readdirSync(dir).filter((f) => f.endsWith('.json')).sort();

describe('protocol fixtures (shared with backend)', () => {
  it('has a fixture for every C7 frame type', () => {
    const stems = new Set(files.map((f) => path.basename(f, '.json')));
    for (const type of ALL_FRAME_TYPES) expect(stems, `missing protocol-fixtures/${type}.json`).toContain(type);
  });

  it.each(files)('%s validates against the zod mirror', (file) => {
    const raw: unknown = JSON.parse(readFileSync(path.join(dir, file), 'utf8'));
    const frames = Array.isArray(raw) ? raw : [raw];
    expect(frames.length).toBeGreaterThan(0);
    for (const frame of frames) {
      const result = AnyFrame.safeParse(frame);
      expect(result.success, JSON.stringify(result.error?.issues)).toBe(true);
      expect((frame as { type: string }).type).toBe(path.basename(file, '.json'));
    }
  });
});

describe('frame schemas', () => {
  it('rejects a server frame without projectId', () => {
    expect(ServerFrame.safeParse({ type: 'run-started', runId: 'r1' }).success).toBe(false);
  });
  it('rejects unknown frame types', () => {
    expect(AnyFrame.safeParse({ type: 'nope', projectId: 'p' }).success).toBe(false);
  });
  it('ping/pong carry projectId null', () => {
    expect(ClientFrame.safeParse({ type: 'ping', projectId: null }).success).toBe(true);
    expect(ServerFrame.safeParse({ type: 'pong', projectId: null }).success).toBe(true);
  });
});
```

`frontend/src/test/fakeWebSocket.ts`:
```ts
/** Minimal browser-WebSocket double. Install with vi.stubGlobal('WebSocket', FakeWebSocket). */
export class FakeWebSocket {
  static readonly CONNECTING = 0;
  static readonly OPEN = 1;
  static readonly CLOSING = 2;
  static readonly CLOSED = 3;
  static instances: FakeWebSocket[] = [];

  readyState = FakeWebSocket.CONNECTING;
  sent: string[] = [];
  onopen: ((ev: Event) => void) | null = null;
  onmessage: ((ev: MessageEvent) => void) | null = null;
  onclose: ((ev: Event) => void) | null = null;
  onerror: ((ev: Event) => void) | null = null;

  constructor(public url: string) {
    FakeWebSocket.instances.push(this);
  }

  static latest(): FakeWebSocket {
    const ws = FakeWebSocket.instances.at(-1);
    if (!ws) throw new Error('no FakeWebSocket created');
    return ws;
  }

  send(data: string): void {
    if (this.readyState !== FakeWebSocket.OPEN) throw new Error('send on non-open socket');
    this.sent.push(data);
  }

  close(): void {
    if (this.readyState === FakeWebSocket.CLOSED) return;
    this.readyState = FakeWebSocket.CLOSED;
    this.onclose?.(new Event('close'));
  }

  sentFrames(): Array<Record<string, unknown>> {
    return this.sent.map((s) => JSON.parse(s) as Record<string, unknown>);
  }

  serverOpen(): void {
    this.readyState = FakeWebSocket.OPEN;
    this.onopen?.(new Event('open'));
  }

  serverSend(frame: unknown): void {
    const data = typeof frame === 'string' ? frame : JSON.stringify(frame);
    this.onmessage?.(new MessageEvent('message', { data }));
  }
}
```

`frontend/src/api/ws.test.ts`:
```ts
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { FakeWebSocket } from '@/test/fakeWebSocket';
import { backoffDelay, createWsClient, type ConnectionState } from './ws';

const runStarted = { type: 'run-started', projectId: 'p1', runId: 'r1' };

function setVisibility(state: 'visible' | 'hidden') {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}

describe('ws client', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakeWebSocket.instances = [];
    vi.stubGlobal('WebSocket', FakeWebSocket);
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  it('backoff is exponential from 0.5 s and capped at 15 s', () => {
    expect([0, 1, 2, 3, 4, 5, 6, 10].map((a) => backoffDelay(a))).toEqual([
      500, 1000, 2000, 4000, 8000, 15000, 15000, 15000,
    ]);
  });

  it('reconnects with backoff and resets the attempt counter after a successful open', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.ensure();
    expect(FakeWebSocket.instances).toHaveLength(1);
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(499);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(999);
    expect(FakeWebSocket.instances).toHaveLength(2);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(3);
    FakeWebSocket.latest().serverOpen();
    FakeWebSocket.latest().close();
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(4);
    client.close();
  });

  it('queues outgoing frames until open and flushes after notifying open listeners', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.onConnection((state) => {
      if (state === 'open') client.send({ type: 'subscribe', projectId: 'p1', sinceMessageId: 7 });
    });
    client.send({ type: 'interrupt', projectId: 'p1', runId: null });
    const ws = FakeWebSocket.latest();
    expect(ws.sent).toEqual([]);
    ws.serverOpen();
    expect(ws.sentFrames().map((f) => f.type)).toEqual(['subscribe', 'interrupt']);
    client.close();
  });

  it('rejects invalid outgoing frames', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    expect(() => client.send({ type: 'subscribe', projectId: '', sinceMessageId: 0 })).toThrow();
    client.close();
  });

  it('validates incoming frames: invalid ones are dropped with console.warn', () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    const got: unknown[] = [];
    client.onFrame((f) => got.push(f));
    client.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    ws.serverSend('not json');
    ws.serverSend({ type: 'run-started', runId: 'r1' }); // projectId missing
    ws.serverSend(runStarted);
    expect(got).toEqual([runStarted]);
    expect(warn).toHaveBeenCalledTimes(2);
    client.close();
  });

  it('sends heartbeat pings and closes a socket that stops answering', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws', heartbeatIntervalMs: 25_000, pongTimeoutMs: 10_000 });
    client.ensure();
    const ws = FakeWebSocket.latest();
    ws.serverOpen();
    vi.advanceTimersByTime(25_000);
    expect(ws.sentFrames()).toContainEqual({ type: 'ping', projectId: null });
    ws.serverSend({ type: 'pong', projectId: null });
    vi.advanceTimersByTime(10_000);
    expect(ws.readyState).toBe(FakeWebSocket.OPEN);
    vi.advanceTimersByTime(15_000 + 10_000); // next ping, no pong
    expect(ws.readyState).toBe(FakeWebSocket.CLOSED);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(2);
    client.close();
  });

  it('visibilitychange → visible reconnects immediately when closed and emits resumed when open', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    const states: ConnectionState[] = [];
    client.onConnection((s) => states.push(s));
    client.ensure();
    FakeWebSocket.latest().serverOpen();
    setVisibility('hidden');
    setVisibility('visible');
    expect(states).toEqual(['open', 'resumed']);
    FakeWebSocket.latest().close();
    for (let i = 0; i < 4; i++) {
      vi.advanceTimersByTime(backoffDelay(i));
      FakeWebSocket.latest().close();
    }
    const before = FakeWebSocket.instances.length;
    setVisibility('visible');
    expect(FakeWebSocket.instances.length).toBe(before + 1);
    client.close();
  });

  it('close() stops reconnecting', () => {
    const client = createWsClient({ url: () => 'ws://test/api/ws' });
    client.ensure();
    client.close();
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/protocol src/api/ws.test.ts`
Expected: FAIL — `Failed to resolve import "./frames"` / `"./ws"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/protocol/frames.ts`:
```ts
import { z } from 'zod';

/** Zod mirror of the backend protocol (index C7). Keep in sync with copperchat.agent.protocol. */
export const MessageRole = z.enum(['user', 'assistant', 'system']);
export const MessageKind = z.enum(['text', 'thinking', 'tool_use', 'tool_result', 'image', 'error', 'notice']);
export const MessageStatus = z.enum(['queued', 'streaming', 'done', 'cancelled', 'error']);
export type MessageStatus = z.infer<typeof MessageStatus>;
export const RunFinishReason = z.enum(['completed', 'interrupted', 'error', 'rate_limited']);
export const DocKind = z.enum(['sch', 'pcb']);
export type DocKind = z.infer<typeof DocKind>;

const ProjectId = z.string().min(1);

export const AttachmentRef = z.object({
  id: z.string().min(1),
  kind: z.enum(['image', 'pdf']),
  name: z.string(),
});
export type AttachmentRef = z.infer<typeof AttachmentRef>;

export const MessageDTO = z.object({
  id: z.number().int(),
  projectId: ProjectId,
  clientMessageId: z.string().nullable(),
  runId: z.string().nullable(),
  role: MessageRole,
  kind: MessageKind,
  content: z.string(),
  status: MessageStatus,
  metadata: z.record(z.string(), z.unknown()),
  createdAt: z.string(),
});
export type MessageDTO = z.infer<typeof MessageDTO>;

export const Usage = z.object({
  inputTokens: z.number(),
  outputTokens: z.number(),
  cacheReadTokens: z.number(),
  cacheCreationTokens: z.number(),
  costUsd: z.number().optional(),
  rateLimit: z.object({ status: z.string(), resetsAt: z.string().optional() }).optional(),
});
export type Usage = z.infer<typeof Usage>;

export const ClientFrame = z.discriminatedUnion('type', [
  z.object({ type: z.literal('subscribe'), projectId: ProjectId, sinceMessageId: z.number().int().nonnegative() }),
  z.object({
    type: z.literal('send'),
    projectId: ProjectId,
    clientMessageId: z.string().min(1),
    text: z.string(),
    attachments: z.array(AttachmentRef),
  }),
  z.object({ type: z.literal('interrupt'), projectId: ProjectId, runId: z.string().nullable() }),
  z.object({ type: z.literal('cancel-queued'), projectId: ProjectId, messageId: z.number().int() }),
  z.object({ type: z.literal('ping'), projectId: z.null() }),
]);
export type ClientFrame = z.infer<typeof ClientFrame>;

export const ServerFrame = z.discriminatedUnion('type', [
  z.object({ type: z.literal('message'), projectId: ProjectId, message: MessageDTO }),
  z.object({
    type: z.literal('message-delta'),
    projectId: ProjectId,
    messageId: z.number().int(),
    contentDelta: z.string().optional(),
    statusPatch: MessageStatus.optional(),
    metadataPatch: z.record(z.string(), z.unknown()).optional(),
  }),
  z.object({ type: z.literal('run-started'), projectId: ProjectId, runId: z.string() }),
  z.object({ type: z.literal('run-finished'), projectId: ProjectId, runId: z.string(), reason: RunFinishReason }),
  z.object({ type: z.literal('view-updated'), projectId: ProjectId, docs: z.array(DocKind), version: z.number().int() }),
  z.object({ type: z.literal('usage'), projectId: ProjectId, usage: Usage }),
  z.object({ type: z.literal('rate-limited'), projectId: ProjectId, resetsAt: z.string() }),
  z.object({
    type: z.literal('error'),
    // Errors about unparseable client frames cannot name a project; the server sends null then.
    projectId: ProjectId.nullable(),
    code: z.string(),
    message: z.string(),
    clientMessageId: z.string().nullish(),
  }),
  z.object({ type: z.literal('pong'), projectId: z.null() }),
  // Server heartbeat (Phase 5); the client ignores it — liveness is tracked via its own ping/pong.
  z.object({ type: z.literal('ping'), projectId: z.null() }),
]);
export type ServerFrame = z.infer<typeof ServerFrame>;
export type MessageDeltaFrame = Extract<ServerFrame, { type: 'message-delta' }>;

export const AnyFrame = z.union([ClientFrame, ServerFrame]);

export const ALL_FRAME_TYPES = [
  'subscribe', 'send', 'interrupt', 'cancel-queued', 'ping',
  'message', 'message-delta', 'run-started', 'run-finished', 'view-updated',
  'usage', 'rate-limited', 'error', 'pong',
] as const;

export const MessagesPage = z.object({ messages: z.array(MessageDTO), hasMore: z.boolean() });
export type MessagesPage = z.infer<typeof MessagesPage>;
```

`frontend/src/api/ws.ts`:
```ts
import { ClientFrame, ServerFrame } from '@/protocol/frames';

export type ConnectionState = 'open' | 'closed' | 'resumed';

export interface WsClient {
  send(frame: ClientFrame): void;
  onFrame(listener: (frame: ServerFrame) => void): () => void;
  onConnection(listener: (state: ConnectionState) => void): () => void;
  ensure(): void;
  isOpen(): boolean;
  close(): void;
}

export interface WsClientOptions {
  url?: () => string;
  heartbeatIntervalMs?: number;
  pongTimeoutMs?: number;
  baseDelayMs?: number;
  maxDelayMs?: number;
}

const CONNECTING = 0;
const OPEN = 1;

export function backoffDelay(attempt: number, baseMs = 500, maxMs = 15_000): number {
  return Math.min(baseMs * 2 ** attempt, maxMs);
}

function defaultUrl(): string {
  const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${window.location.host}/api/ws`;
}

export function createWsClient(opts: WsClientOptions = {}): WsClient {
  const url = opts.url ?? defaultUrl;
  const heartbeatMs = opts.heartbeatIntervalMs ?? 25_000;
  const pongMs = opts.pongTimeoutMs ?? 10_000;
  const frameListeners = new Set<(frame: ServerFrame) => void>();
  const connListeners = new Set<(state: ConnectionState) => void>();
  const queue: ClientFrame[] = [];
  let socket: WebSocket | null = null;
  let attempt = 0;
  let enabled = false;
  let wasOpen = false;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let heartbeatTimer: ReturnType<typeof setInterval> | null = null;
  let pongTimer: ReturnType<typeof setTimeout> | null = null;
  let visibilityInstalled = false;

  const emitConnection = (state: ConnectionState) => {
    for (const l of [...connListeners]) l(state);
  };

  function clearHeartbeat() {
    if (heartbeatTimer) clearInterval(heartbeatTimer);
    if (pongTimer) clearTimeout(pongTimer);
    heartbeatTimer = null;
    pongTimer = null;
  }

  function probe(ws: WebSocket) {
    if (ws.readyState !== OPEN || pongTimer) return;
    ws.send(JSON.stringify({ type: 'ping', projectId: null }));
    pongTimer = setTimeout(() => {
      pongTimer = null;
      ws.close(); // dead connection: onclose schedules the reconnect
    }, pongMs);
  }

  function markAlive() {
    if (pongTimer) clearTimeout(pongTimer);
    pongTimer = null;
  }

  function scheduleReconnect() {
    if (!enabled || reconnectTimer) return;
    const delay = backoffDelay(attempt, opts.baseDelayMs, opts.maxDelayMs);
    attempt += 1;
    reconnectTimer = setTimeout(() => {
      reconnectTimer = null;
      connect();
    }, delay);
  }

  function connect() {
    if (!enabled) return;
    if (socket && (socket.readyState === OPEN || socket.readyState === CONNECTING)) return;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    const ws = new WebSocket(url());
    socket = ws;
    ws.onopen = () => {
      if (socket !== ws) return;
      attempt = 0;
      wasOpen = true;
      heartbeatTimer = setInterval(() => probe(ws), heartbeatMs);
      emitConnection('open'); // listeners resubscribe first …
      while (queue.length > 0 && ws.readyState === OPEN) {
        ws.send(JSON.stringify(queue.shift())); // … then queued frames go out
      }
    };
    ws.onmessage = (event: MessageEvent) => {
      if (socket !== ws) return;
      markAlive();
      let raw: unknown;
      try {
        raw = JSON.parse(String(event.data));
      } catch {
        console.warn('[ws] dropped non-JSON frame');
        return;
      }
      const parsed = ServerFrame.safeParse(raw);
      if (!parsed.success) {
        console.warn('[ws] dropped invalid frame', parsed.error.issues, raw);
        return;
      }
      if (parsed.data.type === 'pong') return;
      for (const l of [...frameListeners]) l(parsed.data);
    };
    ws.onclose = () => {
      if (socket !== ws) return;
      socket = null;
      clearHeartbeat();
      if (wasOpen) {
        wasOpen = false;
        emitConnection('closed');
      }
      scheduleReconnect();
    };
    ws.onerror = () => {
      // onclose follows and schedules the reconnect.
    };
  }

  function onVisibility() {
    if (document.visibilityState !== 'visible' || !enabled) return;
    if (socket && socket.readyState === OPEN) {
      // iOS keeps zombie sockets after backgrounding: heal state and probe liveness.
      emitConnection('resumed');
      probe(socket);
      return;
    }
    attempt = 0;
    if (reconnectTimer) clearTimeout(reconnectTimer);
    reconnectTimer = null;
    connect();
  }

  return {
    send(frame) {
      const valid = ClientFrame.parse(frame);
      if (socket && socket.readyState === OPEN) {
        socket.send(JSON.stringify(valid));
        return;
      }
      queue.push(valid);
      this.ensure();
    },
    onFrame(listener) {
      frameListeners.add(listener);
      return () => frameListeners.delete(listener);
    },
    onConnection(listener) {
      connListeners.add(listener);
      return () => connListeners.delete(listener);
    },
    ensure() {
      enabled = true;
      if (!visibilityInstalled) {
        document.addEventListener('visibilitychange', onVisibility);
        visibilityInstalled = true;
      }
      connect();
    },
    isOpen() {
      return socket !== null && socket.readyState === OPEN;
    },
    close() {
      enabled = false;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      reconnectTimer = null;
      clearHeartbeat();
      queue.length = 0;
      const s = socket;
      socket = null;
      wasOpen = false;
      s?.close();
      if (visibilityInstalled) {
        document.removeEventListener('visibilitychange', onVisibility);
        visibilityInstalled = false;
      }
    },
  };
}

export const wsClient: WsClient = createWsClient();
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/protocol src/api/ws.test.ts && npm run typecheck && npm run lint`
Expected: all pass (fixture tests need Phase 5's `protocol-fixtures/` on the branch).

- [ ] **Step 5: Commit**
```bash
git add frontend/src/protocol frontend/src/api/ws.ts frontend/src/api/ws.test.ts frontend/src/test/fakeWebSocket.ts
git commit -m "feat: zod protocol mirror and singleton WebSocket client with backoff, heartbeat and visibility heal

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.3: HTTP client, auth store, login page

**Files:**
- Create: `frontend/src/api/http.ts`, `frontend/src/stores/auth.ts`, `frontend/src/pages/LoginPage.tsx`, `frontend/src/pages/RequireAuth.tsx`, `frontend/src/components/PoweredBy.tsx`
- Test: `frontend/src/api/http.test.ts`, `frontend/src/pages/LoginPage.test.tsx`

**Interfaces:**
- Consumes: C8 `POST /api/auth/login {username,password}` → 204, `POST /api/auth/logout`, `GET /api/auth/me` → `{username}` (Phase 3).
- Produces (`@/api/http`):
  ```ts
  class ApiError extends Error { status: number; detail: unknown }
  function apiUrl(path: string): string                 // '/projects' → '/api/projects'
  function apiGet<T>(path: string): Promise<T>
  function apiPost<T>(path: string, body: unknown): Promise<T>
  function apiPut<T>(path: string, body: unknown): Promise<T>
  function apiDelete(path: string): Promise<void>
  function setUnauthorizedHandler(handler: (() => void) | null): void
  ```
- Produces (`@/stores/auth`): `useAuth` with `{status: 'unknown'|'authenticated'|'anonymous', username, error, check(), login(u, p): Promise<boolean>, logout(), handleUnauthorized()}`; `<RequireAuth/>` route guard; `<PoweredBy/>`.

- [ ] **Step 1: Write the failing test**

`frontend/src/api/http.test.ts`:
```ts
import { describe, expect, it, vi } from 'vitest';
import { ApiError, apiDelete, apiGet, apiPost, apiPut, setUnauthorizedHandler } from './http';

function respond(status: number, body?: unknown) {
  const init = { status, headers: { 'content-type': 'application/json' } };
  return new Response(body === undefined ? null : JSON.stringify(body), init);
}

describe('http client', () => {
  it('prefixes /api, sends JSON and parses JSON', async () => {
    const fetchMock = vi.fn(async () => respond(200, { ok: true }));
    vi.stubGlobal('fetch', fetchMock);
    await expect(apiPost<{ ok: boolean }>('/projects/x/open', { a: 1 })).resolves.toEqual({ ok: true });
    const [url, init] = fetchMock.mock.calls[0] as unknown as [string, RequestInit];
    expect(url).toBe('/api/projects/x/open');
    expect(init.method).toBe('POST');
    expect(init.body).toBe('{"a":1}');
    expect((init.headers as Record<string, string>)['Content-Type']).toBe('application/json');
    expect(init.credentials).toBe('same-origin');
  });

  it('returns undefined for 204 and supports PUT/DELETE', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    await expect(apiPut('/x', {})).resolves.toBeUndefined();
    await expect(apiDelete('/x')).resolves.toBeUndefined();
  });

  it('throws ApiError with FastAPI detail text', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => respond(400, { detail: 'invalid project id' })));
    const err = await apiGet('/projects/..').catch((e: unknown) => e);
    expect(err).toBeInstanceOf(ApiError);
    expect((err as ApiError).status).toBe(400);
    expect((err as ApiError).message).toBe('invalid project id');
  });

  it('calls the unauthorized handler on 401, except for the login call itself', async () => {
    const handler = vi.fn();
    setUnauthorizedHandler(handler);
    vi.stubGlobal('fetch', vi.fn(async () => respond(401, { detail: 'not authenticated' })));
    await expect(apiGet('/projects')).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    await expect(apiPost('/auth/login', { username: 'a', password: 'b' })).rejects.toBeInstanceOf(ApiError);
    expect(handler).toHaveBeenCalledTimes(1);
    setUnauthorizedHandler(null);
  });
});
```

`frontend/src/pages/LoginPage.test.tsx`:
```tsx
import { render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { useAuth } from '@/stores/auth';
import { LoginPage } from './LoginPage';
import { RequireAuth } from './RequireAuth';

function renderAt(path: string) {
  return render(
    <MemoryRouter initialEntries={[path]}>
      <Routes>
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route path="/projects" element={<p>projects page</p>} />
        </Route>
      </Routes>
    </MemoryRouter>,
  );
}

describe('login', () => {
  beforeEach(() => useAuth.setState({ status: 'unknown', username: null, error: null }));

  it('redirects anonymous users to /login and signs in', async () => {
    let authed = false;
    vi.stubGlobal('fetch', vi.fn(async (url: string) => {
      if (url === '/api/auth/me') {
        return authed
          ? new Response(JSON.stringify({ username: 'admin' }), { status: 200, headers: { 'content-type': 'application/json' } })
          : new Response(JSON.stringify({ detail: 'no' }), { status: 401, headers: { 'content-type': 'application/json' } });
      }
      if (url === '/api/auth/login') {
        authed = true;
        return new Response(null, { status: 204 });
      }
      return new Response(null, { status: 404 });
    }));
    renderAt('/projects');
    await userEvent.type(await screen.findByLabelText('Username'), 'admin');
    await userEvent.type(screen.getByLabelText('Password'), 'secret');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByText('projects page')).toBeInTheDocument();
    expect(useAuth.getState().username).toBe('admin');
  });

  it('shows an error on wrong credentials', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'bad' }), { status: 401, headers: { 'content-type': 'application/json' } })));
    renderAt('/login');
    await userEvent.type(screen.getByLabelText('Username'), 'admin');
    await userEvent.type(screen.getByLabelText('Password'), 'nope');
    await userEvent.click(screen.getByRole('button', { name: 'Sign in' }));
    expect(await screen.findByRole('alert')).toHaveTextContent('Wrong username or password.');
    expect(screen.getByText(/Powered by Claude/)).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/api/http.test.ts src/pages/LoginPage.test.tsx`
Expected: FAIL — modules `./http`, `./LoginPage`, `./RequireAuth`, `@/stores/auth` not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/api/http.ts`:
```ts
export class ApiError extends Error {
  readonly status: number;
  readonly detail: unknown;
  constructor(status: number, detail: unknown, message: string) {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

let unauthorizedHandler: (() => void) | null = null;

/** The auth store registers itself here; avoids an import cycle http ↔ auth. */
export function setUnauthorizedHandler(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

export function apiUrl(path: string): string {
  return `/api${path.startsWith('/') ? path : `/${path}`}`;
}

async function readBody(res: Response): Promise<unknown> {
  const text = await res.text();
  if (!text) return null;
  if ((res.headers.get('content-type') ?? '').includes('application/json')) {
    try {
      return JSON.parse(text) as unknown;
    } catch {
      return text;
    }
  }
  return text;
}

function errorMessage(status: number, body: unknown): string {
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === 'string') return detail;
  }
  if (typeof body === 'string' && body.length > 0) return body;
  return `HTTP ${status}`;
}

async function request<T>(method: string, path: string, body?: unknown): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  const init: RequestInit = { method, credentials: 'same-origin', headers };
  if (body !== undefined) {
    headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const res = await fetch(apiUrl(path), init);
  if (res.status === 401 && !path.startsWith('/auth/login')) unauthorizedHandler?.();
  if (!res.ok) {
    const parsed = await readBody(res);
    throw new ApiError(res.status, parsed, errorMessage(res.status, parsed));
  }
  if (res.status === 204) return undefined as T;
  return (await readBody(res)) as T;
}

export const apiGet = <T>(path: string): Promise<T> => request<T>('GET', path);
export const apiPost = <T>(path: string, body: unknown): Promise<T> => request<T>('POST', path, body);
export const apiPut = <T>(path: string, body: unknown): Promise<T> => request<T>('PUT', path, body);
export const apiDelete = (path: string): Promise<void> => request<void>('DELETE', path);
```

`frontend/src/stores/auth.ts`:
```ts
import { z } from 'zod';
import { create } from 'zustand';
import { ApiError, apiGet, apiPost, setUnauthorizedHandler } from '@/api/http';
import { wsClient } from '@/api/ws';

const Me = z.object({ username: z.string() });

export type AuthStatus = 'unknown' | 'authenticated' | 'anonymous';

interface AuthState {
  status: AuthStatus;
  username: string | null;
  error: string | null;
  check: () => Promise<void>;
  login: (username: string, password: string) => Promise<boolean>;
  logout: () => Promise<void>;
  handleUnauthorized: () => void;
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const useAuth = create<AuthState>()((set) => ({
  status: 'unknown',
  username: null,
  error: null,
  check: async () => {
    try {
      const me = Me.parse(await apiGet<unknown>('/auth/me'));
      set({ status: 'authenticated', username: me.username, error: null });
    } catch (e) {
      set({ status: 'anonymous', username: null, error: e instanceof ApiError && e.status === 401 ? null : messageOf(e) });
    }
  },
  login: async (username, password) => {
    try {
      await apiPost<void>('/auth/login', { username, password });
      set({ status: 'authenticated', username, error: null });
      return true;
    } catch (e) {
      set({
        status: 'anonymous',
        error: e instanceof ApiError && e.status === 401 ? 'Wrong username or password.' : messageOf(e),
      });
      return false;
    }
  },
  logout: async () => {
    try {
      await apiPost<void>('/auth/logout', {});
    } catch {
      // The cookie may already be invalid; local logout proceeds regardless.
    }
    wsClient.close();
    set({ status: 'anonymous', username: null, error: null });
  },
  handleUnauthorized: () => {
    wsClient.close();
    set({ status: 'anonymous', username: null });
  },
}));

setUnauthorizedHandler(() => useAuth.getState().handleUnauthorized());
```

`frontend/src/components/PoweredBy.tsx`:
```tsx
export function PoweredBy() {
  return <p className="text-center text-xs text-muted">CopperChat · Powered by Claude</p>;
}
```

`frontend/src/pages/RequireAuth.tsx`:
```tsx
import { useEffect } from 'react';
import { Navigate, Outlet, useLocation } from 'react-router-dom';
import { useAuth } from '@/stores/auth';

export function RequireAuth() {
  const status = useAuth((s) => s.status);
  const check = useAuth((s) => s.check);
  const location = useLocation();
  useEffect(() => {
    if (status === 'unknown') void check();
  }, [status, check]);
  if (status === 'unknown') return <p className="p-6 text-muted">Loading…</p>;
  if (status === 'anonymous') return <Navigate to="/login" replace state={{ from: location.pathname }} />;
  return <Outlet />;
}
```

`frontend/src/pages/LoginPage.tsx`:
```tsx
import { type FormEvent, useState } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useAuth } from '@/stores/auth';

export function LoginPage() {
  const login = useAuth((s) => s.login);
  const error = useAuth((s) => s.error);
  const navigate = useNavigate();
  const location = useLocation();
  const [username, setUsername] = useState('');
  const [password, setPassword] = useState('');
  const [busy, setBusy] = useState(false);

  async function onSubmit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    const ok = await login(username, password);
    setBusy(false);
    const from = (location.state as { from?: string } | null)?.from;
    if (ok) navigate(from && from !== '/login' ? from : '/projects', { replace: true });
  }

  return (
    <main className="flex min-h-full items-center justify-center p-6">
      <form onSubmit={onSubmit} className="w-full max-w-sm space-y-4 rounded-xl border border-border bg-surface p-6">
        <h1 className="text-xl font-semibold">CopperChat</h1>
        <label className="block text-sm">
          Username
          <input className="mt-1 w-full rounded border border-border bg-bg px-3 py-2" autoComplete="username"
            value={username} onChange={(e) => setUsername(e.target.value)} required />
        </label>
        <label className="block text-sm">
          Password
          <input type="password" className="mt-1 w-full rounded border border-border bg-bg px-3 py-2"
            autoComplete="current-password" value={password} onChange={(e) => setPassword(e.target.value)} required />
        </label>
        {error && <p role="alert" className="text-sm text-danger">{error}</p>}
        <button type="submit" disabled={busy} className="w-full rounded bg-accent px-3 py-2 font-medium text-accent-fg disabled:opacity-60">
          Sign in
        </button>
        <PoweredBy />
      </form>
    </main>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/api/http.test.ts src/pages/LoginPage.test.tsx && npm run typecheck && npm run lint`
Expected: 6 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/api/http.ts frontend/src/api/http.test.ts frontend/src/stores/auth.ts frontend/src/pages frontend/src/components/PoweredBy.tsx
git commit -m "feat: HTTP client with 401 handling, auth store and login page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.4: Chat store — baseline, paging, ordering, duplicates (Review Focus 1)

**Files:**
- Create: `frontend/src/stores/chat.ts`, `frontend/src/test/fakeWsClient.ts`, `frontend/src/test/fakeMessageServer.ts`
- Test: `frontend/src/stores/chat.ordering.test.ts`

**Interfaces:**
- Consumes: `WsClient` (6A.2), `apiGet` (6A.3), C8 `GET /api/projects/{id}/messages?limit=50&before=<id>` → `{messages, hasMore}` (row order not assumed).
- Produces (`@/stores/chat`):
  ```ts
  const PAGE_SIZE = 50;
  interface ProjectChat { messages: MessageDTO[]; lastSeenId: number; oldestId: number | null; hasMoreOlder: boolean;
    loadingOlder: boolean; loaded: boolean; runActive: boolean; currentRunId: string | null; usage: Usage | null;
    rateLimitedUntil: string | null; lastError: string | null; suspectIds: number[] }
  interface ChatDeps { ws: WsClient; fetchPage(projectId: string, opts: { limit: number; before?: number }): Promise<MessagesPage>; newClientMessageId?: () => string }
  interface ChatState { chats: Readonly<Record<string, ProjectChat>>; open(projectId): Promise<void>; loadOlder(projectId): Promise<void>;
    send(projectId, text, attachments?): void; cancelQueued(projectId, messageId: number): void; stop(projectId): void;
    heal(projectId): Promise<void> /* 6A.5 */; dismissError(projectId): void }
  function createChatStore(deps: ChatDeps): ChatStore;  type ChatStore = ReturnType<typeof createChatStore>;
  const useChatStore: ChatStore;  function emptyChat(): ProjectChat;  function fetchMessagesPage(...): Promise<MessagesPage>;
  function compareMessages(a, b): number;  function mergeRows(list, rows): MessageDTO[];
  ```
  Ordering invariant: server rows ascending by id, then optimistic rows (negative ids) in creation order.

- [ ] **Step 1: Write the failing test**

`frontend/src/test/fakeWsClient.ts`:
```ts
import type { ConnectionState, WsClient } from '@/api/ws';
import { ClientFrame, ServerFrame } from '@/protocol/frames';

export class FakeWsClient implements WsClient {
  sent: ClientFrame[] = [];
  open = false;
  ensureCalls = 0;
  private frameListeners = new Set<(f: ServerFrame) => void>();
  private connListeners = new Set<(s: ConnectionState) => void>();

  send(frame: ClientFrame): void {
    this.sent.push(ClientFrame.parse(frame));
  }
  onFrame(listener: (f: ServerFrame) => void): () => void {
    this.frameListeners.add(listener);
    return () => this.frameListeners.delete(listener);
  }
  onConnection(listener: (s: ConnectionState) => void): () => void {
    this.connListeners.add(listener);
    return () => this.connListeners.delete(listener);
  }
  ensure(): void {
    this.ensureCalls += 1;
  }
  isOpen(): boolean {
    return this.open;
  }
  close(): void {
    this.open = false;
  }

  // --- test controls ---
  connect(): void {
    this.open = true;
    for (const l of [...this.connListeners]) l('open');
  }
  drop(): void {
    this.open = false;
    for (const l of [...this.connListeners]) l('closed');
  }
  resume(): void {
    for (const l of [...this.connListeners]) l('resumed');
  }
  emit(frame: unknown): void {
    const valid = ServerFrame.parse(frame);
    for (const l of [...this.frameListeners]) l(valid);
  }
  framesOfType<T extends ClientFrame['type']>(type: T): Array<Extract<ClientFrame, { type: T }>> {
    return this.sent.filter((f): f is Extract<ClientFrame, { type: T }> => f.type === type);
  }
}
```

`frontend/src/test/fakeMessageServer.ts`:
```ts
import { vi } from 'vitest';
import type { MessageDTO } from '@/protocol/frames';

export function row(id: number, overrides: Partial<MessageDTO> = {}): MessageDTO {
  return {
    id,
    projectId: 'p1',
    clientMessageId: null,
    runId: null,
    role: 'assistant',
    kind: 'text',
    content: `m${id}`,
    status: 'done',
    metadata: {},
    createdAt: '2026-10-15T10:00:00Z',
    ...overrides,
  };
}

/** Fake backend for GET /api/projects/{id}/messages, installed as global fetch. */
export class FakeMessageServer {
  rows: MessageDTO[] = [];
  calls: string[] = [];
  failNext = 0;
  private held: Array<() => void> | null = null;

  constructor(rows: MessageDTO[] = []) {
    this.rows = rows;
  }
  install(): this {
    vi.stubGlobal('fetch', vi.fn((input: string | URL) => this.handle(input)));
    return this;
  }
  hold(): void {
    this.held = [];
  }
  release(): void {
    const waiting = this.held ?? [];
    this.held = null;
    for (const resume of waiting) resume();
  }
  messageCalls(): string[] {
    return this.calls.filter((c) => c.includes('/messages'));
  }

  private async handle(input: string | URL): Promise<Response> {
    const url = new URL(String(input), 'http://test');
    this.calls.push(url.pathname + url.search);
    const held = this.held;
    if (held) await new Promise<void>((resume) => held.push(resume));
    if (this.failNext > 0) {
      this.failNext -= 1;
      return new Response(JSON.stringify({ detail: 'boom' }), { status: 500, headers: { 'content-type': 'application/json' } });
    }
    const match = url.pathname.match(/^\/api\/projects\/([^/]+)\/messages$/);
    if (!match) return new Response(null, { status: 404 });
    const limit = Number(url.searchParams.get('limit') ?? '50');
    const before = url.searchParams.get('before');
    const eligible = this.rows
      .filter((r) => r.projectId === match[1] && (before === null || r.id < Number(before)))
      .sort((a, b) => a.id - b.id);
    const body = { messages: eligible.slice(-limit), hasMore: eligible.length > limit };
    return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
  }
}
```

`frontend/src/stores/chat.ordering.test.ts`:
```ts
import { beforeEach, describe, expect, it } from 'vitest';
import { FakeMessageServer, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createChatStore, fetchMessagesPage, type ChatStore } from './chat';

const range = (from: number, to: number) => Array.from({ length: to - from + 1 }, (_, i) => from + i);
let ws: FakeWsClient;
let server: FakeMessageServer;
let store: ChatStore;
let cid = 0;

function ids() {
  return store.getState().chats.p1?.messages.map((m) => m.id) ?? [];
}

beforeEach(() => {
  cid = 0;
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer(range(1, 120).map((id) => row(id))).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => `c${++cid}` });
});

describe('chat store: baseline then subscribe', () => {
  it('subscribes only after the HTTP baseline has set lastSeenId', async () => {
    server.hold();
    const opening = store.getState().open('p1');
    await Promise.resolve();
    expect(ws.framesOfType('subscribe')).toEqual([]);
    server.release();
    await opening;
    expect(ws.framesOfType('subscribe')).toEqual([{ type: 'subscribe', projectId: 'p1', sinceMessageId: 120 }]);
    expect(ids()).toEqual(range(71, 120));
    expect(store.getState().chats.p1?.hasMoreOlder).toBe(true);
  });

  it('double mount shares one in-flight load and subscribes once', async () => {
    const a = store.getState().open('p1');
    const b = store.getState().open('p1');
    expect(b).toBe(a);
    await Promise.all([a, b]);
    await store.getState().open('p1');
    expect(server.messageCalls()).toEqual(['/api/projects/p1/messages?limit=50']);
    expect(ws.framesOfType('subscribe')).toHaveLength(1);
  });

  it('defers subscribe to the connection handler when the socket is not open yet', async () => {
    ws.open = false;
    await store.getState().open('p1');
    expect(ws.framesOfType('subscribe')).toEqual([]);
    ws.connect();
    expect(ws.framesOfType('subscribe')).toEqual([{ type: 'subscribe', projectId: 'p1', sinceMessageId: 120 }]);
  });
});

describe('chat store: ordering and duplicates', () => {
  beforeEach(async () => {
    await store.getState().open('p1');
  });

  it('replay overlapping the baseline yields exactly one row per id in id order', () => {
    for (const id of [118, 119, 120, 121, 122]) ws.emit({ type: 'message', projectId: 'p1', message: row(id) });
    ws.emit({ type: 'message', projectId: 'p1', message: row(121) });
    expect(ids()).toEqual(range(71, 122));
    expect(store.getState().chats.p1?.lastSeenId).toBe(122);
  });

  it('out-of-order live frames are inserted in id order', () => {
    ws.emit({ type: 'message', projectId: 'p1', message: row(125) });
    ws.emit({ type: 'message', projectId: 'p1', message: row(123) });
    ws.emit({ type: 'message', projectId: 'p1', message: row(124) });
    expect(ids().slice(-3)).toEqual([123, 124, 125]);
  });

  it('replaces the optimistic row by the server row with the same clientMessageId, also on retransmit echo', () => {
    store.getState().send('p1', 'hello');
    store.getState().send('p1', 'second');
    expect(ids().slice(-2)).toEqual([-1, -2]);
    expect(ws.framesOfType('send')[0]).toMatchObject({ clientMessageId: 'c1', text: 'hello', attachments: [] });
    const echo = row(121, { role: 'user', clientMessageId: 'c1', content: 'hello', status: 'queued' });
    ws.emit({ type: 'message', projectId: 'p1', message: echo });
    ws.emit({ type: 'message', projectId: 'p1', message: echo });
    expect(ids().slice(-2)).toEqual([121, -2]);
    expect(store.getState().chats.p1?.messages.filter((m) => m.clientMessageId === 'c1')).toHaveLength(1);
  });

  it('retransmits pending optimistic sends with the same clientMessageId after reconnect', () => {
    store.getState().send('p1', 'hello');
    ws.drop();
    ws.connect();
    expect(ws.framesOfType('send').map((f) => f.clientMessageId)).toEqual(['c1', 'c1']);
  });

  it('updates are immutable', () => {
    ws.emit({ type: 'message', projectId: 'p1', message: row(121, { status: 'streaming', content: 'Hel' }) });
    const before = store.getState().chats.p1!;
    const rowBefore = before.messages.at(-1)!;
    ws.emit({ type: 'message-delta', projectId: 'p1', messageId: 121, contentDelta: 'lo' });
    const after = store.getState().chats.p1!;
    expect(after).not.toBe(before);
    expect(after.messages).not.toBe(before.messages);
    expect(rowBefore.content).toBe('Hel');
    expect(after.messages.at(-1)!.content).toBe('Hello');
  });

  it('a late run-finished for an older run does not clear the current run', () => {
    ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r2' });
    ws.emit({ type: 'run-finished', projectId: 'p1', runId: 'r1', reason: 'completed' });
    expect(store.getState().chats.p1).toMatchObject({ runActive: true, currentRunId: 'r2' });
    ws.emit({ type: 'run-finished', projectId: 'p1', runId: 'r2', reason: 'completed' });
    expect(store.getState().chats.p1).toMatchObject({ runActive: false, currentRunId: null });
  });

  it('error frame with clientMessageId marks the optimistic row as failed', () => {
    store.getState().send('p1', 'x');
    ws.emit({ type: 'error', projectId: 'p1', code: 'unknown_attachment', message: 'Unknown attachment', clientMessageId: 'c1' });
    const last = store.getState().chats.p1!.messages.at(-1)!;
    expect(last.status).toBe('error');
    expect(store.getState().chats.p1!.lastError).toBe('Unknown attachment');
  });

  it('stop sends interrupt with the current run id; cancelQueued sends cancel-queued', () => {
    ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r9' });
    store.getState().stop('p1');
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: 'p1', runId: 'r9' }]);
    ws.emit({ type: 'message', projectId: 'p1', message: row(130, { role: 'user', status: 'queued' }) });
    store.getState().cancelQueued('p1', 130);
    expect(ws.framesOfType('cancel-queued')).toEqual([{ type: 'cancel-queued', projectId: 'p1', messageId: 130 }]);
    expect(store.getState().chats.p1!.messages.at(-1)!.status).toBe('cancelled');
  });
});

describe('chat store: older pages', () => {
  beforeEach(async () => {
    await store.getState().open('p1');
  });

  it('loads older pages with before=oldestId and prepends without duplicates', async () => {
    await store.getState().loadOlder('p1');
    expect(server.messageCalls().at(-1)).toBe('/api/projects/p1/messages?limit=50&before=71');
    expect(ids()).toEqual(range(21, 120));
    await store.getState().loadOlder('p1');
    expect(ids()).toEqual(range(1, 120));
    expect(store.getState().chats.p1?.hasMoreOlder).toBe(false);
    await store.getState().loadOlder('p1');
    expect(server.messageCalls()).toHaveLength(3);
  });

  it('a failed older-page load resets loadingOlder so it can be retried', async () => {
    server.failNext = 1;
    await store.getState().loadOlder('p1');
    expect(store.getState().chats.p1).toMatchObject({ loadingOlder: false, hasMoreOlder: true });
    expect(store.getState().chats.p1?.lastError).toMatch(/older messages/);
    await store.getState().loadOlder('p1');
    expect(ids()[0]).toBe(21);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/stores/chat.ordering.test.ts`
Expected: FAIL — `Failed to resolve import "./chat"`.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/stores/chat.ts`:
```ts
import { z } from 'zod';
import { create } from 'zustand';
import { apiGet } from '@/api/http';
import { wsClient, type ConnectionState, type WsClient } from '@/api/ws';
import {
  AttachmentRef,
  MessagesPage,
  type MessageDeltaFrame,
  type MessageDTO,
  type ServerFrame,
  type Usage,
} from '@/protocol/frames';

export const PAGE_SIZE = 50;
const TERMINAL_STATUSES: ReadonlySet<string> = new Set(['done', 'cancelled', 'error']);

export interface ProjectChat {
  messages: MessageDTO[];
  lastSeenId: number;
  oldestId: number | null;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  loaded: boolean;
  runActive: boolean;
  currentRunId: string | null;
  usage: Usage | null;
  rateLimitedUntil: string | null;
  lastError: string | null;
  /** Rows that may have missed deltas (reconnect / unknown-id delta); re-healed when they settle. */
  suspectIds: number[];
}

export function emptyChat(): ProjectChat {
  return {
    messages: [],
    lastSeenId: 0,
    oldestId: null,
    hasMoreOlder: false,
    loadingOlder: false,
    loaded: false,
    runActive: false,
    currentRunId: null,
    usage: null,
    rateLimitedUntil: null,
    lastError: null,
    suspectIds: [],
  };
}

export interface ChatDeps {
  ws: WsClient;
  fetchPage: (projectId: string, opts: { limit: number; before?: number }) => Promise<MessagesPage>;
  newClientMessageId?: () => string;
}

export interface ChatState {
  chats: Readonly<Record<string, ProjectChat>>;
  open: (projectId: string) => Promise<void>;
  loadOlder: (projectId: string) => Promise<void>;
  send: (projectId: string, text: string, attachments?: AttachmentRef[]) => void;
  cancelQueued: (projectId: string, messageId: number) => void;
  stop: (projectId: string) => void;
  heal: (projectId: string) => Promise<void>;
  dismissError: (projectId: string) => void;
}

export async function fetchMessagesPage(
  projectId: string,
  opts: { limit: number; before?: number },
): Promise<MessagesPage> {
  const q = new URLSearchParams({ limit: String(opts.limit) });
  if (opts.before !== undefined) q.set('before', String(opts.before));
  const raw = await apiGet<unknown>(`/projects/${encodeURIComponent(projectId)}/messages?${q.toString()}`);
  return MessagesPage.parse(raw);
}

/** Server rows ascending by id, then optimistic rows (negative ids) in creation order. */
export function compareMessages(a: MessageDTO, b: MessageDTO): number {
  const aOpt = a.id < 0;
  const bOpt = b.id < 0;
  if (aOpt !== bOpt) return aOpt ? 1 : -1;
  return aOpt ? b.id - a.id : a.id - b.id;
}

/** Server snapshot vs local copy of the same row (DB content lags the live stream). */
export function mergeRow(existing: MessageDTO | undefined, incoming: MessageDTO): MessageDTO {
  if (!existing) return incoming;
  if (incoming.status === 'streaming' && existing.content.startsWith(incoming.content)) {
    if (TERMINAL_STATUSES.has(existing.status)) return existing; // stale snapshot of a finished row
    if (existing.content.length > incoming.content.length) return { ...incoming, content: existing.content };
  }
  return incoming;
}

export function mergeRows(list: MessageDTO[], rows: MessageDTO[]): MessageDTO[] {
  const byId = new Map(list.map((m) => [m.id, m] as const));
  for (const r of rows) {
    if (r.clientMessageId !== null) {
      for (const [id, m] of byId) {
        if (id < 0 && m.clientMessageId === r.clientMessageId) byId.delete(id);
      }
    }
    byId.set(r.id, mergeRow(byId.get(r.id), r));
  }
  return [...byId.values()].sort(compareMessages);
}

function maxServerId(rows: MessageDTO[]): number {
  return rows.reduce((max, r) => (r.id > max ? r.id : max), 0);
}

function minServerId(rows: MessageDTO[]): number | null {
  let min: number | null = null;
  for (const r of rows) if (r.id > 0 && (min === null || r.id < min)) min = r.id;
  return min;
}

function applyDelta(m: MessageDTO, f: MessageDeltaFrame): MessageDTO {
  return {
    ...m,
    content: f.contentDelta !== undefined ? m.content + f.contentDelta : m.content,
    status: f.statusPatch ?? m.status,
    metadata: f.metadataPatch ? { ...m.metadata, ...f.metadataPatch } : m.metadata,
  };
}

function attachmentsOf(m: MessageDTO): AttachmentRef[] {
  const parsed = z.array(AttachmentRef).safeParse(m.metadata.attachments);
  return parsed.success ? parsed.data : [];
}

const errorText = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function createChatStore(deps: ChatDeps) {
  const opening = new Map<string, Promise<void>>();
  const healing = new Map<string, { running: Promise<void>; again: boolean }>();
  let optimisticSeq = 0;
  const newClientMessageId = deps.newClientMessageId ?? (() => crypto.randomUUID());

  return create<ChatState>()((set, get) => {
    function patch(projectId: string, fn: (c: ProjectChat) => ProjectChat): void {
      const current = get().chats[projectId];
      if (!current) return;
      set({ chats: { ...get().chats, [projectId]: fn(current) } });
    }

    function withRows(c: ProjectChat, rows: MessageDTO[]): ProjectChat {
      return { ...c, messages: mergeRows(c.messages, rows), lastSeenId: Math.max(c.lastSeenId, maxServerId(rows)) };
    }

    /** Only with an open socket; otherwise the connection handler subscribes on open. */
    function subscribe(projectId: string): void {
      const c = get().chats[projectId];
      if (!c?.loaded || !deps.ws.isOpen()) return;
      deps.ws.send({ type: 'subscribe', projectId, sinceMessageId: c.lastSeenId });
    }

    /** Server dedupes on clientMessageId, so retransmitting is always safe. */
    function resendPending(projectId: string): void {
      const c = get().chats[projectId];
      if (!c) return;
      for (const m of c.messages) {
        if (m.id >= 0 || m.status === 'error' || m.clientMessageId === null) continue;
        deps.ws.send({ type: 'send', projectId, clientMessageId: m.clientMessageId, text: m.content, attachments: attachmentsOf(m) });
      }
    }

    function onDelta(projectId: string, f: MessageDeltaFrame): void {
      const c = get().chats[projectId];
      if (!c) return;
      const existing = c.messages.find((m) => m.id === f.messageId);
      if (!existing) return; // Task 6A.5 turns this into a heal
      const updated = applyDelta(existing, f);
      patch(projectId, (cur) => ({ ...cur, messages: cur.messages.map((m) => (m.id === updated.id ? updated : m)) }));
    }

    function onConnection(state: ConnectionState): void {
      if (state === 'closed') return;
      for (const [projectId, chat] of Object.entries(get().chats)) {
        if (!chat.loaded) continue; // an in-flight initial load subscribes itself with the right lastSeenId
        subscribe(projectId);
        resendPending(projectId);
      }
    }

    function onFrame(f: ServerFrame): void {
      if (f.type === 'error' && f.projectId === null) {
        for (const projectId of Object.keys(get().chats)) patch(projectId, (c) => ({ ...c, lastError: f.message }));
        return;
      }
      if (f.projectId === null || !get().chats[f.projectId]) return;
      const projectId = f.projectId;
      switch (f.type) {
        case 'message':
          patch(projectId, (c) => withRows(c, [f.message]));
          return;
        case 'message-delta':
          onDelta(projectId, f);
          return;
        case 'run-started':
          patch(projectId, (c) => ({ ...c, runActive: true, currentRunId: f.runId, rateLimitedUntil: null, lastError: null }));
          return;
        case 'run-finished':
          patch(projectId, (c) => {
            if (c.currentRunId !== null && c.currentRunId !== f.runId) return c; // late finish of an older run
            return {
              ...c,
              runActive: false,
              currentRunId: null,
              lastError: f.reason === 'error' ? (c.lastError ?? 'The agent run ended with an error.') : c.lastError,
            };
          });
          return;
        case 'usage':
          patch(projectId, (c) => ({ ...c, usage: f.usage }));
          return;
        case 'rate-limited':
          patch(projectId, (c) => ({ ...c, rateLimitedUntil: f.resetsAt }));
          return;
        case 'error':
          patch(projectId, (c) => ({
            ...c,
            lastError: f.message,
            messages: f.clientMessageId
              ? c.messages.map((m) =>
                  m.id < 0 && m.clientMessageId === f.clientMessageId
                    ? { ...m, status: 'error' as const, metadata: { ...m.metadata, error: f.message } }
                    : m,
                )
              : c.messages,
          }));
          return;
        case 'view-updated':
        case 'pong':
          return;
      }
    }

    async function load(projectId: string): Promise<void> {
      if (!get().chats[projectId]) set({ chats: { ...get().chats, [projectId]: emptyChat() } });
      deps.ws.ensure();
      try {
        const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE });
        patch(projectId, (c) => {
          const next = withRows(c, page.messages);
          return { ...next, loaded: true, hasMoreOlder: page.hasMore, oldestId: minServerId(next.messages), lastError: null };
        });
      } catch (e) {
        patch(projectId, (c) => ({ ...c, lastError: `Could not load messages: ${errorText(e)}` }));
        return;
      }
      subscribe(projectId);
    }

    async function healOnce(projectId: string): Promise<void> {
      try {
        const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE });
        patch(projectId, (c) => {
          const next = withRows(c, page.messages);
          const settled = new Set(next.messages.filter((m) => TERMINAL_STATUSES.has(m.status)).map((m) => m.id));
          return {
            ...next,
            oldestId: minServerId(next.messages),
            hasMoreOlder: c.oldestId === null ? page.hasMore : c.hasMoreOlder,
            suspectIds: next.suspectIds.filter((id) => !settled.has(id)),
          };
        });
      } catch (e) {
        console.warn('[chat] heal failed', e);
      }
    }

    deps.ws.onFrame(onFrame);
    deps.ws.onConnection(onConnection);

    return {
      chats: {},

      open: (projectId) => {
        if (get().chats[projectId]?.loaded) return Promise.resolve();
        const inflight = opening.get(projectId);
        if (inflight) return inflight;
        const p = load(projectId).finally(() => opening.delete(projectId));
        opening.set(projectId, p);
        return p;
      },

      loadOlder: async (projectId) => {
        const c = get().chats[projectId];
        if (!c?.loaded || c.loadingOlder || !c.hasMoreOlder || c.oldestId === null) return;
        const before = c.oldestId;
        patch(projectId, (cur) => ({ ...cur, loadingOlder: true }));
        try {
          const page = await deps.fetchPage(projectId, { limit: PAGE_SIZE, before });
          patch(projectId, (cur) => {
            const messages = mergeRows(cur.messages, page.messages);
            return { ...cur, messages, oldestId: minServerId(messages), hasMoreOlder: page.hasMore, loadingOlder: false };
          });
        } catch (e) {
          patch(projectId, (cur) => ({ ...cur, loadingOlder: false, lastError: `Could not load older messages: ${errorText(e)}` }));
        }
      },

      send: (projectId, text, attachments = []) => {
        if (!get().chats[projectId] || (text.trim() === '' && attachments.length === 0)) return;
        const clientMessageId = newClientMessageId();
        optimisticSeq += 1;
        const optimistic: MessageDTO = {
          id: -optimisticSeq,
          projectId,
          clientMessageId,
          runId: null,
          role: 'user',
          kind: 'text',
          content: text,
          status: 'queued',
          metadata: { pending: true, attachments },
          createdAt: new Date().toISOString(),
        };
        patch(projectId, (c) => ({ ...c, messages: [...c.messages, optimistic].sort(compareMessages) }));
        if (deps.ws.isOpen()) deps.ws.send({ type: 'send', projectId, clientMessageId, text, attachments });
      },

      cancelQueued: (projectId, messageId) => {
        if (messageId <= 0) return;
        deps.ws.send({ type: 'cancel-queued', projectId, messageId });
        patch(projectId, (c) => ({
          ...c,
          messages: c.messages.map((m) => (m.id === messageId ? { ...m, status: 'cancelled' as const } : m)),
        }));
      },

      stop: (projectId) => {
        const c = get().chats[projectId];
        if (!c) return;
        deps.ws.send({ type: 'interrupt', projectId, runId: c.currentRunId });
      },

      heal: (projectId) => {
        const state = healing.get(projectId);
        if (state) {
          state.again = true; // a newer request arrived mid-fetch: fetch once more afterwards
          return state.running;
        }
        const entry = { running: Promise.resolve(), again: false };
        entry.running = (async () => {
          try {
            do {
              entry.again = false;
              await healOnce(projectId);
            } while (entry.again);
          } finally {
            healing.delete(projectId);
          }
        })();
        healing.set(projectId, entry);
        return entry.running;
      },

      dismissError: (projectId) => patch(projectId, (c) => ({ ...c, lastError: null })),
    };
  });
}

export type ChatStore = ReturnType<typeof createChatStore>;

export const useChatStore: ChatStore = createChatStore({ ws: wsClient, fetchPage: fetchMessagesPage });
```
(`heal` is implemented here so the store type is complete; Task 6A.5 wires it into reconnect and deltas and tests it.)

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/stores/chat.ordering.test.ts && npm run typecheck && npm run lint`
Expected: 13 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/chat.ts frontend/src/stores/chat.ordering.test.ts frontend/src/test/fakeWsClient.ts frontend/src/test/fakeMessageServer.ts
git commit -m "feat: chat store with baseline-then-subscribe, id-ordered merge and optimistic reconciliation

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.5: Chat store — reconnect, stuck Stop, half-streamed and unknown-delta heal (Review Focus 1)

**Files:**
- Modify: `frontend/src/stores/chat.ts`
- Test: `frontend/src/stores/chat.reconnect.test.ts`

**Interfaces:**
- Consumes: `createChatStore`, `FakeWsClient`, `FakeMessageServer` (6A.4).
- Produces: reconnect semantics — on `open`/`resumed`: `runActive=false`, `currentRunId=null`, streaming rows become suspects, `subscribe{sinceMessageId: lastSeenId}`, pending sends retransmitted, HTTP heal (last 50, merge by id). Delta for unknown id → suspect + heal. Suspect reaching a terminal status → heal.

- [ ] **Step 1: Write the failing test**

`frontend/src/stores/chat.reconnect.test.ts`:
```ts
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { FakeMessageServer, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { createChatStore, fetchMessagesPage, type ChatStore } from './chat';

let ws: FakeWsClient;
let server: FakeMessageServer;
let store: ChatStore;
const chat = () => store.getState().chats.p1!;
const msg = (id: number) => chat().messages.find((m) => m.id === id);

beforeEach(async () => {
  ws = new FakeWsClient();
  ws.open = true;
  server = new FakeMessageServer([row(10, { role: 'user' }), row(11, { status: 'streaming', content: 'Hel' })]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage });
  await store.getState().open('p1');
  ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r1' });
});

describe('reconnect', () => {
  it('resets runActive (no stuck Stop) and resubscribes from lastSeenId', () => {
    expect(chat().runActive).toBe(true);
    ws.drop();
    ws.connect();
    expect(chat()).toMatchObject({ runActive: false, currentRunId: null });
    expect(ws.framesOfType('subscribe').at(-1)).toEqual({ type: 'subscribe', projectId: 'p1', sinceMessageId: 11 });
  });

  it('a run-started re-sent by the server after resubscribe restores runActive', () => {
    ws.drop();
    ws.connect();
    ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r1' });
    expect(chat()).toMatchObject({ runActive: true, currentRunId: 'r1' });
  });

  it('heals a half-streamed row via HTTP merge after reconnect', async () => {
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello world' })];
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(msg(11)).toMatchObject({ content: 'Hello world', status: 'done' }));
    expect(chat().messages.map((m) => m.id)).toEqual([10, 11]);
    expect(chat().suspectIds).toEqual([]);
  });

  it('keeps longer live content when the DB snapshot lags behind the stream', async () => {
    ws.emit({ type: 'message-delta', projectId: 'p1', messageId: 11, contentDelta: 'lo wor' });
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'streaming', content: 'Hello' })];
    ws.resume();
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(2));
    await vi.waitFor(() => expect(msg(11)?.content).toBe('Hello wor'));
    expect(chat().suspectIds).toEqual([11]);
  });

  it('re-heals a suspect row once it reaches a terminal status', async () => {
    ws.drop();
    ws.connect();
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(2));
    expect(chat().suspectIds).toEqual([11]);
    server.rows = [row(10, { role: 'user' }), row(11, { status: 'done', content: 'Hello, complete' })];
    ws.emit({ type: 'message-delta', projectId: 'p1', messageId: 11, contentDelta: 'p', statusPatch: 'done' });
    await vi.waitFor(() => expect(msg(11)?.content).toBe('Hello, complete'));
    expect(chat().suspectIds).toEqual([]);
  });

  it('a delta for an unknown message id triggers a heal instead of being dropped', async () => {
    server.rows = [...server.rows, row(12, { status: 'streaming', content: 'New row' })];
    ws.emit({ type: 'message-delta', projectId: 'p1', messageId: 12, contentDelta: ' row' });
    await vi.waitFor(() => expect(msg(12)?.content).toBe('New row'));
    expect(chat().lastSeenId).toBe(12);
  });

  it('visibility resume (socket still open) runs the same heal path', async () => {
    ws.resume();
    expect(chat().runActive).toBe(false);
    expect(ws.framesOfType('subscribe')).toHaveLength(2);
    await vi.waitFor(() => expect(server.messageCalls()).toHaveLength(2));
  });

  it('a failing heal keeps state and logs a warning', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {});
    server.failNext = 1;
    ws.resume();
    await vi.waitFor(() => expect(warn).toHaveBeenCalled());
    expect(chat().messages.map((m) => m.id)).toEqual([10, 11]);
  });

  it('concurrent heal requests coalesce into at most one extra fetch', async () => {
    server.hold();
    const a = store.getState().heal('p1');
    const b = store.getState().heal('p1');
    const c = store.getState().heal('p1');
    server.release();
    await Promise.all([a, b, c]);
    server.release();
    await vi.waitFor(() => expect(server.messageCalls().length).toBeLessThanOrEqual(3));
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/stores/chat.reconnect.test.ts`
Expected: FAIL — e.g. "resets runActive" expects `false`, receives `true`; "unknown message id" times out (delta dropped).

- [ ] **Step 3: Write minimal implementation**

In `frontend/src/stores/chat.ts`, replace the whole `function onDelta(...) { ... }` with:
```ts
    function onDelta(projectId: string, f: MessageDeltaFrame): void {
      const c = get().chats[projectId];
      if (!c) return;
      const existing = c.messages.find((m) => m.id === f.messageId);
      if (!existing) {
        // Its `message` frame was lost (e.g. mid-reconnect): heal instead of dropping.
        patch(projectId, (cur) => ({
          ...cur,
          suspectIds: cur.suspectIds.includes(f.messageId) ? cur.suspectIds : [...cur.suspectIds, f.messageId],
        }));
        void get().heal(projectId);
        return;
      }
      const updated = applyDelta(existing, f);
      const settledSuspect =
        c.suspectIds.includes(f.messageId) && f.statusPatch !== undefined && TERMINAL_STATUSES.has(f.statusPatch);
      patch(projectId, (cur) => ({
        ...cur,
        messages: cur.messages.map((m) => (m.id === updated.id ? updated : m)),
        suspectIds: settledSuspect ? cur.suspectIds.filter((id) => id !== f.messageId) : cur.suspectIds,
      }));
      // A suspect may have lost deltas; once final, the DB row is authoritative.
      if (settledSuspect) void get().heal(projectId);
    }
```
and replace the whole `function onConnection(...) { ... }` with:
```ts
    function onConnection(state: ConnectionState): void {
      if (state === 'closed') return;
      for (const [projectId, chat] of Object.entries(get().chats)) {
        if (!chat.loaded) continue; // an in-flight initial load subscribes itself with the right lastSeenId
        // run-finished may have been lost with the old socket; the server re-sends
        // run-started on subscribe when the run is still active (no stuck Stop).
        patch(projectId, (c) => ({
          ...c,
          runActive: false,
          currentRunId: null,
          suspectIds: [
            ...new Set([...c.suspectIds, ...c.messages.filter((m) => m.id > 0 && m.status === 'streaming').map((m) => m.id)]),
          ],
        }));
        subscribe(projectId);
        resendPending(projectId);
        void get().heal(projectId); // half-streamed rows: subscribe(since) never re-sends rows ≤ lastSeenId
      }
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/stores && npm run typecheck && npm run lint`
Expected: all chat store tests pass (ordering + reconnect).

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/chat.ts frontend/src/stores/chat.reconnect.test.ts
git commit -m "feat: chat reconnect heal — reset run state, resubscribe, merge last page, heal unknown deltas

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.6: useChat hook, MessageList scroll behaviour, Stop control (Review Focus 1)

**Files:**
- Create: `frontend/src/hooks/useChat.ts`, `frontend/src/components/chat/MessageList.tsx`, `frontend/src/components/chat/RunControls.tsx`
- Test: `frontend/src/components/chat/MessageList.test.tsx`, `frontend/src/components/chat/RunControls.test.tsx`

**Interfaces:**
- Produces:
  ```ts
  // @/hooks/useChat
  const ChatStoreContext: React.Context<ChatStore>;   // default: useChatStore singleton; tests inject a fake-backed store
  function useChat(projectId: string): {
    messages: MessageDTO[]; runActive: boolean; currentRunId: string | null; queued: MessageDTO[];
    send(text: string, attachments?: AttachmentRef[]): void; cancelQueued(id: number): void; stop(): void;
    loadOlder(): Promise<void>; hasMoreOlder: boolean; loadingOlder: boolean; loaded: boolean;
    usage: Usage | null; rateLimitedUntil: string | null; lastError: string | null; dismissError(): void;
  }
  // components
  function MessageList(props: { messages: MessageDTO[]; hasMoreOlder: boolean; loadingOlder: boolean;
    onLoadOlder: () => void; renderItem: (m: MessageDTO) => ReactNode }): JSX.Element;
  const STICK_THRESHOLD_PX = 80;
  function RunControls(props: { runActive: boolean; onStop: () => void }): JSX.Element | null;
  ```
  `queued` = server rows (`id > 0`) with `role==='user' && status==='queued'`.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/chat/MessageList.test.tsx`:
```tsx
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { MessageDTO } from '@/protocol/frames';
import { row } from '@/test/fakeMessageServer';
import { MessageList } from './MessageList';

const ROW = 100;
const VIEWPORT = 500;
let scrollTop = 0;

class FakeIO {
  static instances: FakeIO[] = [];
  constructor(public cb: IntersectionObserverCallback) {
    FakeIO.instances.push(this);
  }
  observe() {}
  unobserve() {}
  disconnect() {}
  takeRecords() {
    return [];
  }
  fire(isIntersecting: boolean) {
    this.cb([{ isIntersecting } as IntersectionObserverEntry], this as unknown as IntersectionObserver);
  }
}

function rect(top: number, height: number): DOMRect {
  return { top, bottom: top + height, left: 0, right: 300, width: 300, height, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

function rows(from: number, to: number): MessageDTO[] {
  return Array.from({ length: to - from + 1 }, (_, i) => row(from + i));
}

function renderList(messages: MessageDTO[], onLoadOlder = vi.fn()) {
  const view = render(
    <MessageList messages={messages} hasMoreOlder loadingOlder={false} onLoadOlder={onLoadOlder}
      renderItem={(m) => <span>{m.content}</span>} />,
  );
  const scroller = screen.getByTestId('chat-scroller');
  Object.defineProperty(scroller, 'scrollTop', { configurable: true, get: () => scrollTop, set: (v: number) => { scrollTop = v; } });
  Object.defineProperty(scroller, 'clientHeight', { configurable: true, get: () => VIEWPORT });
  Object.defineProperty(scroller, 'scrollHeight', {
    configurable: true,
    get: () => scroller.querySelectorAll('[data-msg-id]').length * ROW,
  });
  return { ...view, scroller, onLoadOlder };
}

function scrollTo(scroller: HTMLElement, top: number) {
  scrollTop = top;
  fireEvent.scroll(scroller);
}

beforeEach(() => {
  scrollTop = 0;
  FakeIO.instances = [];
  vi.stubGlobal('IntersectionObserver', FakeIO);
  vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
    if (this.dataset.testid === 'chat-scroller') return rect(0, VIEWPORT);
    if (this.dataset.msgId !== undefined) {
      const all = Array.from(document.querySelectorAll('[data-msg-id]'));
      return rect(all.indexOf(this) * ROW - scrollTop, ROW);
    }
    return rect(0, 0);
  });
});
afterEach(() => vi.useRealTimers());

describe('MessageList', () => {
  it('keeps the topmost visible message in place when older messages are prepended', () => {
    const { scroller, rerender } = renderList(rows(51, 60));
    scrollTo(scroller, 250); // row index 2 (id 53) is topmost visible, offset -50
    rerender(
      <MessageList messages={rows(1, 60)} hasMoreOlder loadingOlder={false} onLoadOlder={vi.fn()}
        renderItem={(m) => <span>{m.content}</span>} />,
    );
    // id 53 moved from index 2 to index 52: +5000 px
    expect(scrollTop).toBe(5250);
  });

  it('sticks to the bottom within 80 px and shows the newest pill otherwise', async () => {
    const spy = vi.spyOn(Element.prototype, 'scrollIntoView');
    const { scroller } = renderList(rows(1, 10));
    await act(async () => new Promise((r) => requestAnimationFrame(() => r(undefined))));
    expect(spy).toHaveBeenCalled();
    scrollTo(scroller, 1000 - VIEWPORT - 79);
    expect(screen.queryByRole('button', { name: '↓ newest' })).toBeNull();
    scrollTo(scroller, 100);
    const pill = screen.getByRole('button', { name: '↓ newest' });
    spy.mockClear();
    fireEvent.click(pill);
    expect(spy).toHaveBeenCalledWith({ block: 'end' });
    expect(screen.queryByRole('button', { name: '↓ newest' })).toBeNull();
  });

  it('loads older pages from the top sentinel only when not stuck to the bottom', () => {
    const { scroller, onLoadOlder } = renderList(rows(1, 10));
    expect(FakeIO.instances).toHaveLength(0);
    scrollTo(scroller, 0);
    FakeIO.instances.at(-1)!.fire(true);
    expect(onLoadOlder).toHaveBeenCalledTimes(1);
  });

  it('shows the start marker when there is nothing older', () => {
    render(<MessageList messages={rows(1, 2)} hasMoreOlder={false} loadingOlder={false} onLoadOlder={vi.fn()}
      renderItem={(m) => <span>{m.content}</span>} />);
    expect(screen.getByText('Start of conversation')).toBeInTheDocument();
  });
});
```

`frontend/src/components/chat/RunControls.test.tsx`:
```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { act } from 'react';
import { beforeEach, describe, expect, it } from 'vitest';
import { ChatStoreContext, useChat } from '@/hooks/useChat';
import { createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { FakeMessageServer } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { RunControls } from './RunControls';

let ws: FakeWsClient;
let store: ChatStore;

function Harness() {
  const chat = useChat('p1');
  return <RunControls runActive={chat.runActive} onStop={chat.stop} />;
}

beforeEach(() => {
  ws = new FakeWsClient();
  ws.open = true;
  new FakeMessageServer([]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage });
});

describe('Stop button', () => {
  it('appears for an active run, interrupts it, and never sticks after a reconnect that lost run-finished', async () => {
    render(<ChatStoreContext.Provider value={store}><Harness /></ChatStoreContext.Provider>);
    await waitFor(() => expect(store.getState().chats.p1?.loaded).toBe(true));
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    act(() => ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r1' }));
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(ws.framesOfType('interrupt')).toEqual([{ type: 'interrupt', projectId: 'p1', runId: 'r1' }]);
    act(() => {
      ws.drop();
      ws.connect();
    });
    expect(screen.queryByRole('button', { name: 'Stop' })).toBeNull();
    act(() => ws.emit({ type: 'run-started', projectId: 'p1', runId: 'r1' }));
    expect(screen.getByRole('button', { name: 'Stop' })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat`
Expected: FAIL — `./MessageList`, `./RunControls`, `@/hooks/useChat` not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/hooks/useChat.ts`:
```ts
import { createContext, useCallback, useContext, useEffect, useMemo } from 'react';
import type { AttachmentRef, MessageDTO, Usage } from '@/protocol/frames';
import { emptyChat, useChatStore, type ChatStore } from '@/stores/chat';

export const ChatStoreContext = createContext<ChatStore>(useChatStore);

const EMPTY = emptyChat();

export interface UseChatResult {
  messages: MessageDTO[];
  runActive: boolean;
  currentRunId: string | null;
  queued: MessageDTO[];
  send: (text: string, attachments?: AttachmentRef[]) => void;
  cancelQueued: (id: number) => void;
  stop: () => void;
  loadOlder: () => Promise<void>;
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  loaded: boolean;
  usage: Usage | null;
  rateLimitedUntil: string | null;
  lastError: string | null;
  dismissError: () => void;
}

export function useChat(projectId: string): UseChatResult {
  const useStore = useContext(ChatStoreContext);
  const chat = useStore((s) => s.chats[projectId]) ?? EMPTY;

  useEffect(() => {
    void useStore.getState().open(projectId);
  }, [useStore, projectId]);

  const queued = useMemo(
    () => chat.messages.filter((m) => m.id > 0 && m.role === 'user' && m.status === 'queued'),
    [chat.messages],
  );
  const send = useCallback(
    (text: string, attachments?: AttachmentRef[]) => useStore.getState().send(projectId, text, attachments),
    [useStore, projectId],
  );
  const cancelQueued = useCallback((id: number) => useStore.getState().cancelQueued(projectId, id), [useStore, projectId]);
  const stop = useCallback(() => useStore.getState().stop(projectId), [useStore, projectId]);
  const loadOlder = useCallback(() => useStore.getState().loadOlder(projectId), [useStore, projectId]);
  const dismissError = useCallback(() => useStore.getState().dismissError(projectId), [useStore, projectId]);

  return {
    messages: chat.messages,
    runActive: chat.runActive,
    currentRunId: chat.currentRunId,
    queued,
    send,
    cancelQueued,
    stop,
    loadOlder,
    hasMoreOlder: chat.hasMoreOlder,
    loadingOlder: chat.loadingOlder,
    loaded: chat.loaded,
    usage: chat.usage,
    rateLimitedUntil: chat.rateLimitedUntil,
    lastError: chat.lastError,
    dismissError,
  };
}
```

`frontend/src/components/chat/RunControls.tsx`:
```tsx
export function RunControls({ runActive, onStop }: { runActive: boolean; onStop: () => void }) {
  if (!runActive) return null;
  return (
    <button type="button" onClick={onStop}
      className="shrink-0 rounded-lg border border-danger px-3 py-2 text-sm font-medium text-danger">
      Stop
    </button>
  );
}
```

`frontend/src/components/chat/MessageList.tsx`:
```tsx
import { type ReactNode, useCallback, useEffect, useLayoutEffect, useRef, useState } from 'react';
import type { MessageDTO } from '@/protocol/frames';

export const STICK_THRESHOLD_PX = 80;

export interface MessageListProps {
  messages: MessageDTO[];
  hasMoreOlder: boolean;
  loadingOlder: boolean;
  onLoadOlder: () => void;
  renderItem: (m: MessageDTO) => ReactNode;
}

export function MessageList({ messages, hasMoreOlder, loadingOlder, onLoadOlder, renderItem }: MessageListProps) {
  const scrollerRef = useRef<HTMLDivElement>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const topSentinelRef = useRef<HTMLDivElement>(null);
  const anchorRef = useRef<{ id: number; offset: number } | null>(null);
  const prevFirstIdRef = useRef<number | null>(null);
  const [stuck, setStuck] = useState(true);
  const firstId = messages[0]?.id ?? null;

  // Anchor = topmost (partially) visible message and its offset from the scroller top.
  const captureAnchor = useCallback(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const top = el.getBoundingClientRect().top;
    for (const node of el.querySelectorAll<HTMLElement>('[data-msg-id]')) {
      const r = node.getBoundingClientRect();
      if (r.bottom > top) {
        anchorRef.current = { id: Number(node.dataset.msgId), offset: r.top - top };
        return;
      }
    }
    anchorRef.current = null;
  }, []);

  useEffect(() => {
    const el = scrollerRef.current;
    if (!el) return;
    const onScroll = () => {
      setStuck(el.scrollHeight - el.scrollTop - el.clientHeight < STICK_THRESHOLD_PX);
      captureAnchor();
    };
    el.addEventListener('scroll', onScroll, { passive: true });
    captureAnchor();
    return () => el.removeEventListener('scroll', onScroll);
  }, [captureAnchor]);

  // Lazy older load. Gated on "not stuck to bottom": on first mount the sentinel is briefly
  // visible before the auto-scroll lands; loading then would pin the user to old history.
  // Re-created on stuck/firstId change so a still-visible sentinel fires again.
  useEffect(() => {
    const el = topSentinelRef.current;
    if (!el || stuck) return;
    const io = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) onLoadOlder();
      },
      { root: scrollerRef.current, rootMargin: '120px 0px 0px 0px' },
    );
    io.observe(el);
    return () => io.disconnect();
  }, [onLoadOlder, stuck, hasMoreOlder, firstId]);

  // Element-based anchoring after a prepend (not height deltas: those drift by spinner height).
  useLayoutEffect(() => {
    const prev = prevFirstIdRef.current;
    prevFirstIdRef.current = firstId;
    if (prev === null || firstId === null || prev <= 0 || firstId <= 0 || firstId >= prev) return;
    const el = scrollerRef.current;
    const anchor = anchorRef.current;
    if (!el || !anchor) return;
    const node = el.querySelector<HTMLElement>(`[data-msg-id="${anchor.id}"]`);
    if (!node) return;
    const delta = node.getBoundingClientRect().top - el.getBoundingClientRect().top - anchor.offset;
    if (delta !== 0) el.scrollTop += delta;
    captureAnchor();
  }, [firstId, captureAnchor]);

  useEffect(() => {
    if (!stuck) return;
    const id = requestAnimationFrame(() => bottomRef.current?.scrollIntoView({ block: 'end' }));
    return () => cancelAnimationFrame(id);
  }, [messages, stuck]);

  return (
    <div className="relative min-h-0 flex-1">
      <div ref={scrollerRef} data-testid="chat-scroller" role="log" aria-live="polite"
        className="absolute inset-0 overflow-y-auto overscroll-contain px-3 py-3">
        {hasMoreOlder ? (
          <div ref={topSentinelRef} aria-hidden className="h-px" />
        ) : (
          messages.length > 0 && <p className="py-2 text-center text-xs text-muted">Start of conversation</p>
        )}
        {loadingOlder && <p className="py-2 text-center text-xs text-muted">Loading older messages…</p>}
        <ul className="space-y-3">
          {messages.map((m) => (
            <li key={m.id} data-msg-id={m.id}>
              {renderItem(m)}
            </li>
          ))}
        </ul>
        <div ref={bottomRef} aria-hidden />
      </div>
      {!stuck && (
        <button type="button"
          onClick={() => {
            setStuck(true);
            bottomRef.current?.scrollIntoView({ block: 'end' });
          }}
          className="absolute bottom-3 right-3 rounded-full bg-fg/85 px-3 py-1.5 text-xs text-bg shadow-lg">
          ↓ newest
        </button>
      )}
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/chat src/stores && npm run typecheck && npm run lint`
Expected: all pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/hooks/useChat.ts frontend/src/components/chat
git commit -m "feat: useChat hook, message list with element anchoring and stick-to-bottom, Stop control

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.7: Message rendering per kind (markdown, collapsibles, thumbnails, lightbox)

**Files:**
- Create: `frontend/src/components/chat/MessageItem.tsx`, `frontend/src/components/chat/Markdown.tsx`, `frontend/src/components/chat/Lightbox.tsx`
- Test: `frontend/src/components/chat/MessageItem.test.tsx`

**Interfaces:**
- Consumes: `MessageDTO`. Metadata conventions read (Phase 5 writes them): `tool_use` → `metadata.name` (tool name) and `metadata.input` (object); `tool_result` → `metadata.isError?: boolean`, `metadata.images?: Array<{url: string} | string>`; `image` → `metadata.url?: string` and/or `metadata.images`; user rows → `metadata.pending?: boolean`, `metadata.error?: string`.
- Produces: `MessageItem({message}: {message: MessageDTO})`, `imageUrls(m: MessageDTO): string[]` (only same-origin `/api/` or `blob:` URLs), `Markdown({text})`, `Lightbox({src, alt, onClose})`.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/chat/MessageItem.test.tsx`:
```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { row } from '@/test/fakeMessageServer';
import { MessageItem, imageUrls } from './MessageItem';

describe('MessageItem', () => {
  it('renders assistant markdown and does not render raw HTML or javascript: links', () => {
    const { container } = render(
      <MessageItem message={row(1, { content: '**bold** <img src=x onerror=alert(1)> [x](javascript:alert(1))' })} />,
    );
    expect(screen.getByText('bold').tagName).toBe('STRONG');
    expect(container.querySelector('img')).toBeNull();
    const link = screen.getByText('x').closest('a');
    expect(link?.getAttribute('href') ?? '').not.toMatch(/^javascript:/);
  });

  it('renders user text verbatim with a sending hint while optimistic', () => {
    render(<MessageItem message={row(-1, { role: 'user', content: '**not md**', status: 'queued', metadata: { pending: true } })} />);
    expect(screen.getByText('**not md**')).toBeInTheDocument();
    expect(screen.getByText('sending…')).toBeInTheDocument();
  });

  it('thinking is collapsed by default', () => {
    render(<MessageItem message={row(2, { kind: 'thinking', content: 'pondering' })} />);
    const details = screen.getByText('Thinking').closest('details');
    expect(details).not.toHaveAttribute('open');
  });

  it('tool_use shows the tool name and reveals input on expand', () => {
    render(<MessageItem message={row(3, { kind: 'tool_use', content: '', metadata: { name: 'pcb_get_footprint', input: { ref: 'R12' } } })} />);
    const summary = screen.getByText(/pcb_get_footprint/);
    expect(summary.closest('details')).not.toHaveAttribute('open');
    fireEvent.click(summary);
    expect(screen.getByText(/"ref": "R12"/)).toBeInTheDocument();
  });

  it('tool_result error is flagged; image thumbnails open a lightbox that Escape closes', () => {
    render(<MessageItem message={row(4, { kind: 'tool_result', content: 'rendered', metadata: { isError: false, images: [{ url: '/api/projects/p1/renders/a.png' }, 'https://evil.example/x.png'] } })} />);
    const thumbs = screen.getAllByRole('button', { name: /Open image/ });
    expect(thumbs).toHaveLength(1);
    fireEvent.click(thumbs[0]!);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('renders error and notice kinds', () => {
    render(<>
      <MessageItem message={row(5, { kind: 'error', content: 'KiCad API server restarted' })} />
      <MessageItem message={row(6, { kind: 'notice', role: 'system', content: 'Files changed outside CopperChat' })} />
    </>);
    expect(screen.getByRole('alert')).toHaveTextContent('KiCad API server restarted');
    expect(screen.getByText('Files changed outside CopperChat')).toBeInTheDocument();
  });

  it('imageUrls collects url and images and filters foreign origins', () => {
    expect(imageUrls(row(7, { kind: 'image', metadata: { url: '/api/a.png', images: ['/api/b.png', { url: 'http://x/c.png' }] } })))
      .toEqual(['/api/a.png', '/api/b.png']);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/MessageItem.test.tsx`
Expected: FAIL — `./MessageItem` not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/components/chat/Markdown.tsx`:
```tsx
import ReactMarkdown from 'react-markdown';
import rehypeSanitize from 'rehype-sanitize';
import remarkGfm from 'remark-gfm';

/** Raw HTML is never rendered (react-markdown default); rehype-sanitize is defence in depth. */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="cc-markdown break-words">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeSanitize]}
        components={{ a: ({ node, ...props }) => <a {...props} target="_blank" rel="noopener noreferrer" /> }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
```

`frontend/src/components/chat/Lightbox.tsx`:
```tsx
import { useEffect } from 'react';

export function Lightbox({ src, alt, onClose }: { src: string; alt: string; onClose: () => void }) {
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div role="dialog" aria-modal="true" aria-label={alt}
      className="fixed inset-0 z-50 flex items-center justify-center bg-black/85 p-2" onClick={onClose}>
      <img src={src} alt={alt} className="max-h-full max-w-full object-contain" style={{ touchAction: 'pinch-zoom' }}
        onClick={(e) => e.stopPropagation()} />
      <button type="button" onClick={onClose} aria-label="Close image"
        className="absolute right-3 top-3 rounded-full bg-white/15 px-3 py-1 text-white">✕</button>
    </div>
  );
}
```

`frontend/src/components/chat/MessageItem.tsx`:
```tsx
import { useState } from 'react';
import type { MessageDTO } from '@/protocol/frames';
import { Lightbox } from './Lightbox';
import { Markdown } from './Markdown';

export function imageUrls(m: MessageDTO): string[] {
  const out: string[] = [];
  const md = m.metadata;
  if (typeof md.url === 'string') out.push(md.url);
  if (Array.isArray(md.images)) {
    for (const img of md.images as unknown[]) {
      if (typeof img === 'string') out.push(img);
      else if (img && typeof img === 'object' && typeof (img as { url?: unknown }).url === 'string') {
        out.push((img as { url: string }).url);
      }
    }
  }
  return out.filter((u) => u.startsWith('/api/') || u.startsWith('blob:'));
}

function Thumbnails({ urls }: { urls: string[] }) {
  const [open, setOpen] = useState<string | null>(null);
  if (urls.length === 0) return null;
  return (
    <div className="mt-2 flex flex-wrap gap-2">
      {urls.map((u, i) => (
        <button key={u} type="button" aria-label={`Open image ${i + 1}`} onClick={() => setOpen(u)}
          className="overflow-hidden rounded border border-border">
          <img src={u} alt="" loading="lazy" className="h-24 w-auto max-w-48 object-contain" />
        </button>
      ))}
      {open && <Lightbox src={open} alt="Agent view" onClose={() => setOpen(null)} />}
    </div>
  );
}

function toolName(m: MessageDTO): string {
  const n = m.metadata.name ?? m.metadata.toolName;
  return typeof n === 'string' ? n : 'tool';
}

function oneLine(input: unknown): string {
  const s = JSON.stringify(input ?? {});
  return s.length > 80 ? `${s.slice(0, 77)}…` : s;
}

export function MessageItem({ message: m }: { message: MessageDTO }) {
  const streaming = m.status === 'streaming';
  switch (m.kind) {
    case 'text':
      if (m.role === 'user') {
        return (
          <div className="flex justify-end" data-role="user">
            <div className={`max-w-[85%] rounded-2xl bg-accent px-3 py-2 text-accent-fg ${m.status === 'cancelled' ? 'opacity-50 line-through' : ''}`}>
              <p className="whitespace-pre-wrap break-words">{m.content}</p>
              {m.metadata.pending === true && m.status !== 'error' && <p className="mt-1 text-xs opacity-75">sending…</p>}
              {m.status === 'error' && <p className="mt-1 text-xs">not sent: {String(m.metadata.error ?? 'error')}</p>}
            </div>
          </div>
        );
      }
      return (
        <div data-role="assistant" className="max-w-full">
          <Markdown text={m.content} />
          {streaming && <span aria-label="streaming" className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-fg/60 align-middle" />}
        </div>
      );
    case 'thinking':
      return (
        <details className="rounded border border-border bg-surface px-3 py-2 text-sm text-muted" data-role="assistant">
          <summary className="cursor-pointer select-none">Thinking</summary>
          <p className="mt-2 whitespace-pre-wrap">{m.content}</p>
        </details>
      );
    case 'tool_use':
      return (
        <details className="rounded border border-border bg-surface px-3 py-2 text-sm" data-role="assistant">
          <summary className="cursor-pointer select-none font-mono">
            {toolName(m)} <span className="text-muted">{oneLine(m.metadata.input)}</span>
          </summary>
          <pre className="mt-2 overflow-x-auto text-xs">{JSON.stringify(m.metadata.input ?? {}, null, 2)}</pre>
        </details>
      );
    case 'tool_result': {
      const isError = m.metadata.isError === true;
      return (
        <div data-role="assistant">
          <details className={`rounded border px-3 py-2 text-sm ${isError ? 'border-danger' : 'border-border'} bg-surface`}>
            <summary className="cursor-pointer select-none">{isError ? 'Tool error' : 'Tool result'}</summary>
            <pre className="mt-2 max-h-80 overflow-auto whitespace-pre-wrap text-xs">{m.content}</pre>
          </details>
          <Thumbnails urls={imageUrls(m)} />
        </div>
      );
    }
    case 'image':
      return (
        <div data-role={m.role}>
          {m.content && <p className="text-sm text-muted">{m.content}</p>}
          <Thumbnails urls={imageUrls(m)} />
        </div>
      );
    case 'error':
      return (
        <div role="alert" className="rounded border border-danger bg-danger/10 px-3 py-2 text-sm text-danger">
          {m.content}
        </div>
      );
    case 'notice':
      return <p className="text-center text-xs text-muted">{m.content}</p>;
  }
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/chat/MessageItem.test.tsx && npm run typecheck && npm run lint`
Expected: 7 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/chat/MessageItem.tsx frontend/src/components/chat/Markdown.tsx frontend/src/components/chat/Lightbox.tsx frontend/src/components/chat/MessageItem.test.tsx
git commit -m "feat: message rendering per kind with sanitized markdown, collapsibles and image lightbox

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.8: Composer, composer store, attachment slot, keyboard pinning

**Files:**
- Create: `frontend/src/stores/composer.ts`, `frontend/src/components/chat/Composer.tsx`, `frontend/src/components/chat/composerSlots.ts`, `frontend/src/hooks/useKeyboardInset.ts`, `frontend/src/hooks/useMediaQuery.ts`
- Test: `frontend/src/stores/composer.test.ts`, `frontend/src/components/chat/Composer.test.tsx`

**Interfaces:**
- Produces (`@/stores/composer`, consumed by Phase 6B for `@REF` mentions and Phase 8 for attachments):
  ```ts
  const useComposer: UseBoundStore<{
    text: string; selection: { start: number; end: number } | null; attachments: AttachmentRef[]; focusToken: number;
    setText(text: string): void; setSelection(start: number, end: number): void;
    insertText(text: string): void;          // inserts at the caret with surrounding spaces, focuses the textarea
    addAttachment(a: AttachmentRef): void; removeAttachment(id: string): void; clear(): void;
  }>;
  function insertAt(text: string, insert: string, selection: { start: number; end: number } | null): { text: string; caret: number };
  ```
- Produces (`@/components/chat/composerSlots`, Phase 8 registers the upload button):
  ```ts
  interface ComposerAttachmentSlotProps { disabled: boolean; projectId: string; attachments: AttachmentRef[]; setAttachments(next: AttachmentRef[]): void }
  const composerAttachmentSlot: { register(component: ComponentType<ComposerAttachmentSlotProps> | null): void };
  function useComposerAttachmentSlot(): ComponentType<ComposerAttachmentSlotProps> | null;
  ```
- Produces: `useMediaQuery(query: string): boolean`, `useKeyboardInset(): number`, `computeKeyboardInset(innerHeight: number, vv: {height: number; offsetTop: number} | null): number`.
- Produces: `Composer(props: { projectId?: string; runActive: boolean; queued: MessageDTO[]; disabled?: boolean; onSend(text: string, attachments: AttachmentRef[]): void; onStop(): void; onCancelQueued(id: number): void })`.

- [ ] **Step 1: Write the failing test**

`frontend/src/stores/composer.test.ts`:
```ts
import { beforeEach, describe, expect, it } from 'vitest';
import { insertAt, useComposer } from './composer';

describe('insertAt', () => {
  it('appends with a separating space', () => {
    expect(insertAt('check', '@R12', null)).toEqual({ text: 'check @R12 ', caret: 11 });
  });
  it('inserts at the caret without doubling spaces', () => {
    expect(insertAt('is  ok', '@C3', { start: 3, end: 3 })).toEqual({ text: 'is @C3 ok', caret: 7 });
  });
  it('replaces a selection', () => {
    expect(insertAt('what is X?', '@U1', { start: 8, end: 9 })).toEqual({ text: 'what is @U1 ?', caret: 12 });
  });
  it('into an empty prompt', () => {
    expect(insertAt('', '@R1', null)).toEqual({ text: '@R1 ', caret: 4 });
  });
});

describe('useComposer', () => {
  beforeEach(() => useComposer.getState().clear());
  it('insertText updates text, caret and focus token', () => {
    useComposer.getState().setText('why is  hot');
    useComposer.getState().setSelection(7, 7);
    useComposer.getState().insertText('@Q2');
    const s = useComposer.getState();
    expect(s.text).toBe('why is @Q2 hot');
    expect(s.selection).toEqual({ start: 11, end: 11 });
    expect(s.focusToken).toBe(1);
  });
  it('manages attachments', () => {
    useComposer.getState().addAttachment({ id: 'a1', kind: 'pdf', name: 'ds.pdf' });
    useComposer.getState().addAttachment({ id: 'a1', kind: 'pdf', name: 'ds.pdf' });
    expect(useComposer.getState().attachments).toHaveLength(1);
    useComposer.getState().removeAttachment('a1');
    expect(useComposer.getState().attachments).toEqual([]);
  });
});
```

`frontend/src/components/chat/Composer.test.tsx`:
```tsx
import { act, fireEvent, render, screen } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { computeKeyboardInset } from '@/hooks/useKeyboardInset';
import { useComposer } from '@/stores/composer';
import { row } from '@/test/fakeMessageServer';
import { setMedia } from '@/test/media';
import { Composer } from './Composer';
import { composerAttachmentSlot } from './composerSlots';

function setup(props: Partial<Parameters<typeof Composer>[0]> = {}) {
  const onSend = vi.fn();
  const onStop = vi.fn();
  const onCancelQueued = vi.fn();
  render(<Composer runActive={false} queued={[]} onSend={onSend} onStop={onStop} onCancelQueued={onCancelQueued} {...props} />);
  return { onSend, onStop, onCancelQueued, box: screen.getByRole('textbox', { name: 'Message' }) };
}

beforeEach(() => {
  useComposer.getState().clear();
  composerAttachmentSlot.register(null);
});

describe('Composer', () => {
  it('Enter sends on desktop and clears; Shift+Enter does not send', () => {
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: 'hello' } });
    fireEvent.keyDown(box, { key: 'Enter', shiftKey: true });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).toHaveBeenCalledWith('hello', []);
    expect(useComposer.getState().text).toBe('');
  });

  it('Enter inserts a newline on touch devices (send via button)', () => {
    setMedia((q) => q === '(pointer: coarse)');
    const { onSend, box } = setup();
    fireEvent.change(box, { target: { value: 'hi' } });
    fireEvent.keyDown(box, { key: 'Enter' });
    expect(onSend).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(onSend).toHaveBeenCalledWith('hi', []);
  });

  it('shows Stop only while a run is active and keeps Send available for queueing', () => {
    const { onStop } = setup({ runActive: true });
    fireEvent.click(screen.getByRole('button', { name: 'Stop' }));
    expect(onStop).toHaveBeenCalled();
    expect(screen.getByRole('button', { name: 'Send' })).toBeInTheDocument();
  });

  it('lists queued messages with cancel', () => {
    const { onCancelQueued } = setup({ queued: [row(42, { role: 'user', status: 'queued', content: 'next question' })] });
    expect(screen.getByRole('list', { name: 'Queued messages' })).toHaveTextContent('next question');
    fireEvent.click(screen.getByRole('button', { name: 'Cancel queued message: next question' }));
    expect(onCancelQueued).toHaveBeenCalledWith(42);
  });

  it('renders no attachment control until a slot is registered', () => {
    setup();
    expect(screen.queryByTestId('attach')).toBeNull();
    act(() => composerAttachmentSlot.register(({ disabled }) => <button data-testid="attach" disabled={disabled}>Attach</button>));
    expect(screen.getByTestId('attach')).toBeInTheDocument();
  });

  it('insertText from outside (viewer mention) lands in the textarea', () => {
    const { box } = setup();
    act(() => useComposer.getState().insertText('@R12'));
    expect(box).toHaveValue('@R12 ');
    expect(document.activeElement).toBe(box);
  });

  it('keyboard inset = innerHeight − visual viewport bottom', () => {
    expect(computeKeyboardInset(800, { height: 450, offsetTop: 0 })).toBe(350);
    expect(computeKeyboardInset(800, { height: 800, offsetTop: 0 })).toBe(0);
    expect(computeKeyboardInset(800, null)).toBe(0);
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/stores/composer.test.ts src/components/chat/Composer.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/stores/composer.ts`:
```ts
import { create } from 'zustand';
import type { AttachmentRef } from '@/protocol/frames';

type Selection = { start: number; end: number };

export function insertAt(text: string, insert: string, selection: Selection | null): { text: string; caret: number } {
  const start = selection?.start ?? text.length;
  const end = selection?.end ?? start;
  const before = text.slice(0, start);
  const after = text.slice(end);
  const lead = before.length > 0 && !/\s$/.test(before) ? ' ' : '';
  const trail = after.length > 0 && /^\s/.test(after) ? '' : ' ';
  const head = before + lead + insert + trail;
  // When the following text already starts with a space, the caret lands after that space.
  const caret = head.length + (trail === '' && after.startsWith(' ') ? 1 : 0);
  return { text: head + after, caret };
}

interface ComposerState {
  text: string;
  selection: Selection | null;
  attachments: AttachmentRef[];
  focusToken: number;
  setText: (text: string) => void;
  setSelection: (start: number, end: number) => void;
  insertText: (text: string) => void;
  addAttachment: (a: AttachmentRef) => void;
  removeAttachment: (id: string) => void;
  clear: () => void;
}

export const useComposer = create<ComposerState>()((set, get) => ({
  text: '',
  selection: null,
  attachments: [],
  focusToken: 0,
  setText: (text) => set({ text }),
  setSelection: (start, end) => set({ selection: { start, end } }),
  insertText: (insert) => {
    const { text, caret } = insertAt(get().text, insert, get().selection);
    set({ text, selection: { start: caret, end: caret }, focusToken: get().focusToken + 1 });
  },
  addAttachment: (a) => {
    if (get().attachments.some((x) => x.id === a.id)) return;
    set({ attachments: [...get().attachments, a] });
  },
  removeAttachment: (id) => set({ attachments: get().attachments.filter((a) => a.id !== id) }),
  clear: () => set({ text: '', selection: null, attachments: [], focusToken: 0 }),
}));
```

`frontend/src/components/chat/composerSlots.ts`:
```ts
import { type ComponentType, useSyncExternalStore } from 'react';
import type { AttachmentRef } from '@/protocol/frames';

export interface ComposerAttachmentSlotProps {
  disabled: boolean;
  projectId: string;
  attachments: AttachmentRef[];
  setAttachments(next: AttachmentRef[]): void;
}

type Slot = ComponentType<ComposerAttachmentSlotProps> | null;

let slot: Slot = null;
const listeners = new Set<() => void>();

/** Extension point: Phase 8 registers the upload button here. */
export const composerAttachmentSlot = {
  register(component: Slot): void {
    slot = component;
    for (const l of [...listeners]) l();
  },
};

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function useComposerAttachmentSlot(): Slot {
  return useSyncExternalStore(subscribe, () => slot, () => slot);
}
```

`frontend/src/hooks/useMediaQuery.ts`:
```ts
import { useCallback, useSyncExternalStore } from 'react';

export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener('change', onChange);
      return () => mql.removeEventListener('change', onChange);
    },
    [query],
  );
  return useSyncExternalStore(subscribe, () => window.matchMedia(query).matches, () => false);
}
```

`frontend/src/hooks/useKeyboardInset.ts`:
```ts
import { useEffect, useState } from 'react';

export function computeKeyboardInset(innerHeight: number, vv: { height: number; offsetTop: number } | null): number {
  if (!vv) return 0;
  return Math.max(0, Math.round(innerHeight - vv.height - vv.offsetTop));
}

/** Height covered by the on-screen keyboard (iOS Safari does not resize the layout viewport). */
export function useKeyboardInset(): number {
  const [inset, setInset] = useState(0);
  useEffect(() => {
    const vv = window.visualViewport;
    if (!vv) return;
    const update = () => setInset(computeKeyboardInset(window.innerHeight, vv));
    update();
    vv.addEventListener('resize', update);
    vv.addEventListener('scroll', update);
    return () => {
      vv.removeEventListener('resize', update);
      vv.removeEventListener('scroll', update);
    };
  }, []);
  return inset;
}
```

`frontend/src/components/chat/Composer.tsx`:
```tsx
import { type FormEvent, type KeyboardEvent, useEffect, useRef } from 'react';
import { useKeyboardInset } from '@/hooks/useKeyboardInset';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import type { AttachmentRef, MessageDTO } from '@/protocol/frames';
import { useComposer } from '@/stores/composer';
import { useComposerAttachmentSlot } from './composerSlots';
import { RunControls } from './RunControls';

export interface ComposerProps {
  projectId?: string;
  runActive: boolean;
  queued: MessageDTO[];
  disabled?: boolean;
  onSend: (text: string, attachments: AttachmentRef[]) => void;
  onStop: () => void;
  onCancelQueued: (id: number) => void;
}

export function Composer({ projectId = '', runActive, queued, disabled = false, onSend, onStop, onCancelQueued }: ComposerProps) {
  const text = useComposer((s) => s.text);
  const attachments = useComposer((s) => s.attachments);
  const focusToken = useComposer((s) => s.focusToken);
  const setText = useComposer((s) => s.setText);
  const setSelection = useComposer((s) => s.setSelection);
  const removeAttachment = useComposer((s) => s.removeAttachment);
  const clear = useComposer((s) => s.clear);
  const ref = useRef<HTMLTextAreaElement>(null);
  const coarse = useMediaQuery('(pointer: coarse)');
  const keyboardInset = useKeyboardInset();
  const Slot = useComposerAttachmentSlot();

  useEffect(() => {
    const el = ref.current;
    if (!el || focusToken === 0) return;
    el.focus();
    const sel = useComposer.getState().selection;
    if (sel) el.setSelectionRange(sel.start, sel.end);
  }, [focusToken]);

  const canSend = !disabled && (text.trim() !== '' || attachments.length > 0);

  function submit() {
    if (!canSend) return;
    onSend(text.trim(), attachments);
    clear();
  }

  function onKeyDown(e: KeyboardEvent<HTMLTextAreaElement>) {
    if (e.key === 'Enter' && !e.shiftKey && !coarse && !e.nativeEvent.isComposing) {
      e.preventDefault();
      submit();
    }
  }

  return (
    <div data-testid="composer" className="border-t border-border bg-bg px-3 pt-2"
      style={{ transform: keyboardInset ? `translateY(-${keyboardInset}px)` : undefined, paddingBottom: 'max(0.5rem, env(safe-area-inset-bottom))' }}>
      {queued.length > 0 && (
        <ul aria-label="Queued messages" className="mb-2 space-y-1">
          {queued.map((q) => (
            <li key={q.id} className="flex items-center gap-2 rounded bg-surface px-2 py-1 text-sm">
              <span className="text-xs text-muted">queued</span>
              <span className="min-w-0 flex-1 truncate">{q.content}</span>
              <button type="button" aria-label={`Cancel queued message: ${q.content}`} onClick={() => onCancelQueued(q.id)}
                className="text-xs text-danger">Cancel</button>
            </li>
          ))}
        </ul>
      )}
      {attachments.length > 0 && (
        <ul aria-label="Attachments" className="mb-2 flex flex-wrap gap-1">
          {attachments.map((a) => (
            <li key={a.id} className="flex items-center gap-1 rounded bg-surface-2 px-2 py-0.5 text-xs">
              {a.name}
              <button type="button" aria-label={`Remove ${a.name}`} onClick={() => removeAttachment(a.id)}>✕</button>
            </li>
          ))}
        </ul>
      )}
      <form className="flex items-end gap-2" onSubmit={(e: FormEvent) => { e.preventDefault(); submit(); }}>
        {Slot && (
          <Slot disabled={disabled} projectId={projectId} attachments={attachments}
            setAttachments={(next) => useComposer.setState({ attachments: next })} />
        )}
        <textarea ref={ref} aria-label="Message" rows={1} value={text} disabled={disabled}
          placeholder="Ask about this design…"
          onChange={(e) => setText(e.target.value)}
          onSelect={(e) => setSelection(e.currentTarget.selectionStart, e.currentTarget.selectionEnd)}
          onKeyDown={onKeyDown}
          className="max-h-40 min-h-10 flex-1 resize-none rounded-lg border border-border bg-surface px-3 py-2 field-sizing-content" />
        <RunControls runActive={runActive} onStop={onStop} />
        <button type="submit" disabled={!canSend}
          className="shrink-0 rounded-lg bg-accent px-3 py-2 text-sm font-medium text-accent-fg disabled:opacity-50">
          Send
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/stores/composer.test.ts src/components/chat/Composer.test.tsx && npm run typecheck && npm run lint`
Expected: 13 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/composer.ts frontend/src/stores/composer.test.ts frontend/src/components/chat/Composer.tsx frontend/src/components/chat/Composer.test.tsx frontend/src/components/chat/composerSlots.ts frontend/src/hooks
git commit -m "feat: composer with queue list, Stop, mention insertion API, attachment slot and keyboard pinning

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.9: ChatPanel, usage indicator, rate-limit banner

**Files:**
- Create: `frontend/src/components/chat/UsageIndicator.tsx`, `frontend/src/components/chat/RateLimitBanner.tsx`, `frontend/src/components/chat/ChatPanel.tsx`
- Test: `frontend/src/components/chat/usage.test.tsx`, `frontend/src/components/chat/ChatPanel.test.tsx`

**Interfaces:**
- Produces: `UsageIndicator({usage}: {usage: Usage | null})`, `formatTokens(n: number): string`, `RateLimitBanner({until}: {until: string | null})`, `ChatPanel({projectId, readOnly?}: {projectId: string; readOnly?: boolean})`.

- [ ] **Step 1: Write the failing test**

`frontend/src/components/chat/usage.test.tsx`:
```tsx
import { render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { RateLimitBanner } from './RateLimitBanner';
import { UsageIndicator, formatTokens } from './UsageIndicator';

afterEach(() => vi.useRealTimers());

describe('usage', () => {
  it('formats token counts', () => {
    expect([999, 1234, 15_000, 2_500_000].map(formatTokens)).toEqual(['999', '1.2k', '15k', '2.5M']);
  });

  it('shows input incl. cache, output and cost', () => {
    render(<UsageIndicator usage={{ inputTokens: 1000, outputTokens: 250, cacheReadTokens: 9000, cacheCreationTokens: 200, costUsd: 0.1234 }} />);
    expect(screen.getByLabelText('Token usage')).toHaveTextContent('in 10k · out 250 · $0.12');
  });

  it('renders nothing without usage', () => {
    const { container } = render(<UsageIndicator usage={null} />);
    expect(container).toBeEmptyDOMElement();
  });

  it('rate-limit banner shows until the reset time passes', () => {
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-10-15T10:00:00Z'));
    const { rerender } = render(<RateLimitBanner until="2026-10-15T10:30:00Z" />);
    expect(screen.getByRole('status')).toHaveTextContent(/Rate limit reached.*resume automatically/);
    vi.setSystemTime(new Date('2026-10-15T10:31:00Z'));
    rerender(<RateLimitBanner until="2026-10-15T10:30:00Z" />);
    expect(screen.queryByRole('status')).toBeNull();
  });
});
```

`frontend/src/components/chat/ChatPanel.test.tsx`:
```tsx
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ChatStoreContext } from '@/hooks/useChat';
import { createChatStore, fetchMessagesPage, type ChatStore } from '@/stores/chat';
import { useComposer } from '@/stores/composer';
import { FakeMessageServer, row } from '@/test/fakeMessageServer';
import { FakeWsClient } from '@/test/fakeWsClient';
import { ChatPanel } from './ChatPanel';

let ws: FakeWsClient;
let store: ChatStore;

beforeEach(() => {
  vi.stubGlobal('IntersectionObserver', class { observe() {} disconnect() {} unobserve() {} takeRecords() { return []; } });
  useComposer.getState().clear();
  ws = new FakeWsClient();
  ws.open = true;
  new FakeMessageServer([row(1, { content: 'Welcome to the board' }), row(2, { role: 'user', status: 'queued', content: 'later' })]).install();
  store = createChatStore({ ws, fetchPage: fetchMessagesPage, newClientMessageId: () => 'c1' });
});

function renderPanel() {
  return render(<ChatStoreContext.Provider value={store}><ChatPanel projectId="p1" /></ChatStoreContext.Provider>);
}

describe('ChatPanel', () => {
  it('renders history, keeps queued rows in the composer queue, sends and shows usage/rate limit', async () => {
    renderPanel();
    expect(await screen.findByText('Welcome to the board')).toBeInTheDocument();
    expect(screen.getByRole('list', { name: 'Queued messages' })).toHaveTextContent('later');
    expect(screen.getByRole('log')).not.toHaveTextContent('later');

    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), { target: { value: 'What is R12?' } });
    fireEvent.click(screen.getByRole('button', { name: 'Send' }));
    expect(ws.framesOfType('send')).toEqual([{ type: 'send', projectId: 'p1', clientMessageId: 'c1', text: 'What is R12?', attachments: [] }]);
    expect(screen.getByText('What is R12?')).toBeInTheDocument();

    act(() => ws.emit({ type: 'usage', projectId: 'p1', usage: { inputTokens: 1500, outputTokens: 20, cacheReadTokens: 0, cacheCreationTokens: 0 } }));
    expect(screen.getByLabelText('Token usage')).toHaveTextContent('in 1.5k');
    act(() => ws.emit({ type: 'rate-limited', projectId: 'p1', resetsAt: new Date(Date.now() + 3_600_000).toISOString() }));
    expect(screen.getByRole('status')).toHaveTextContent('Rate limit reached');
  });

  it('shows and dismisses errors', async () => {
    renderPanel();
    await waitFor(() => expect(store.getState().chats.p1?.loaded).toBe(true));
    act(() => ws.emit({ type: 'error', projectId: 'p1', code: 'kicad_unavailable', message: 'KiCad API server restarted' }));
    expect(screen.getByRole('alert')).toHaveTextContent('KiCad API server restarted');
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss error' }));
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/components/chat/usage.test.tsx src/components/chat/ChatPanel.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/components/chat/UsageIndicator.tsx`:
```tsx
import type { Usage } from '@/protocol/frames';

export function formatTokens(n: number): string {
  if (n >= 1_000_000) return `${+(n / 1_000_000).toFixed(1)}M`;
  if (n >= 10_000) return `${Math.round(n / 1000)}k`;
  if (n >= 1000) return `${+(n / 1000).toFixed(1)}k`;
  return String(n);
}

const time = (iso: string) => new Date(iso).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });

export function UsageIndicator({ usage }: { usage: Usage | null }) {
  if (!usage) return null;
  const input = usage.inputTokens + usage.cacheReadTokens + usage.cacheCreationTokens;
  const parts = [`in ${formatTokens(input)}`, `out ${formatTokens(usage.outputTokens)}`];
  if (usage.costUsd !== undefined) parts.push(`$${usage.costUsd.toFixed(2)}`);
  if (usage.rateLimit?.resetsAt) parts.push(`limit ${usage.rateLimit.status}, resets ${time(usage.rateLimit.resetsAt)}`);
  return (
    <p aria-label="Token usage" className="px-3 pb-1 text-right text-[11px] text-muted">
      {parts.join(' · ')}
    </p>
  );
}
```

`frontend/src/components/chat/RateLimitBanner.tsx`:
```tsx
import { useEffect, useState } from 'react';

export function RateLimitBanner({ until }: { until: string | null }) {
  const [, tick] = useState(0);
  const resetsAt = until ? Date.parse(until) : NaN;
  const active = Number.isFinite(resetsAt) && resetsAt > Date.now();
  useEffect(() => {
    if (!active) return;
    const id = setInterval(() => tick((n) => n + 1), 30_000);
    return () => clearInterval(id);
  }, [active]);
  if (!active) return null;
  const at = new Date(resetsAt).toLocaleTimeString(undefined, { hour: '2-digit', minute: '2-digit' });
  return (
    <div role="status" className="border-b border-warning bg-warning/15 px-3 py-2 text-sm text-warning">
      Rate limit reached — queued messages resume automatically at {at}.
    </div>
  );
}
```

`frontend/src/components/chat/ChatPanel.tsx`:
```tsx
import { useCallback, useMemo } from 'react';
import { useChat } from '@/hooks/useChat';
import type { MessageDTO } from '@/protocol/frames';
import { Composer } from './Composer';
import { MessageItem } from './MessageItem';
import { MessageList } from './MessageList';
import { RateLimitBanner } from './RateLimitBanner';
import { UsageIndicator } from './UsageIndicator';

export function ChatPanel({ projectId, readOnly = false }: { projectId: string; readOnly?: boolean }) {
  const chat = useChat(projectId);
  // Server-side queued user rows live in the composer queue, not in the history.
  const visible = useMemo(
    () => chat.messages.filter((m) => !(m.id > 0 && m.role === 'user' && m.status === 'queued')),
    [chat.messages],
  );
  const renderItem = useCallback((m: MessageDTO) => <MessageItem message={m} />, []);
  const { loadOlder } = chat;
  const onLoadOlder = useCallback(() => void loadOlder(), [loadOlder]);

  return (
    <section aria-label="Chat" className="flex h-full min-h-0 flex-col bg-bg">
      <RateLimitBanner until={chat.rateLimitedUntil} />
      {chat.lastError && (
        <div role="alert" className="flex items-start gap-2 border-b border-danger bg-danger/10 px-3 py-2 text-sm text-danger">
          <span className="flex-1">{chat.lastError}</span>
          <button type="button" aria-label="Dismiss error" onClick={chat.dismissError}>✕</button>
        </div>
      )}
      {readOnly && (
        <p className="border-b border-border bg-surface px-3 py-1 text-xs text-muted">
          Read-only: this project is open in KiCad — close it there to let CopperChat save.
        </p>
      )}
      <MessageList messages={visible} hasMoreOlder={chat.hasMoreOlder} loadingOlder={chat.loadingOlder}
        onLoadOlder={onLoadOlder} renderItem={renderItem} />
      <UsageIndicator usage={chat.usage} />
      <Composer projectId={projectId} runActive={chat.runActive} queued={chat.queued} disabled={!chat.loaded}
        onSend={chat.send} onStop={chat.stop} onCancelQueued={chat.cancelQueued} />
    </section>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/components/chat && npm run typecheck && npm run lint`
Expected: all chat component tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/components/chat
git commit -m "feat: chat panel with usage indicator, rate-limit banner and error banner

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.10: Projects store and project picker (lock warning)

**Files:**
- Create: `frontend/src/stores/projects.ts`, `frontend/src/pages/ProjectPickerPage.tsx`
- Test: `frontend/src/stores/projects.test.tsx`

**Interfaces:**
- Consumes: C8 `GET /api/projects` → `[{id,name,relPath,hasSch,hasPcb,locked}]`; `POST /api/projects/{id}/open` → `{id, readOnly, lockFiles, kicadVersion}`.
- Produces (`@/stores/projects`, consumed by 6B for `name`, `hasSch`, `hasPcb`):
  ```ts
  type ProjectSummary = { id: string; name: string; relPath: string; hasSch: boolean; hasPcb: boolean; locked: boolean };
  type OpenProject = ProjectSummary & { readOnly: boolean; lockFiles: string[]; kicadVersion: string | null };
  const useProjects: UseBoundStore<{ projects: ProjectSummary[]; loading: boolean; error: string | null;
    current: OpenProject | null; load(): Promise<void>; open(id: string): Promise<OpenProject | null> }>;
  ```

- [ ] **Step 1: Write the failing test**

`frontend/src/stores/projects.test.tsx`:
```tsx
import { fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { ProjectPickerPage } from '@/pages/ProjectPickerPage';
import { useProjects } from './projects';

const list = [
  { id: 'a1b2c3d4e5f6', name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: true, locked: false },
  { id: 'ffffffffffff', name: 'busy', relPath: 'lab/busy.kicad_pro', hasSch: true, hasPcb: false, locked: true },
];

function json(body: unknown) {
  return new Response(JSON.stringify(body), { status: 200, headers: { 'content-type': 'application/json' } });
}

beforeEach(() => {
  useProjects.setState({ projects: [], loading: false, error: null, current: null });
  vi.stubGlobal('fetch', vi.fn(async (url: string) => {
    if (url === '/api/projects') return json(list);
    if (url === '/api/projects/ffffffffffff/open') return json({ id: 'ffffffffffff', readOnly: true, lockFiles: ['lab/~busy.kicad_sch.lck'], kicadVersion: '10.99.0' });
    return new Response(null, { status: 404 });
  }));
});

describe('projects', () => {
  it('picker lists projects, warns about locks and navigates on click', async () => {
    render(
      <MemoryRouter initialEntries={['/projects']}>
        <Routes>
          <Route path="/projects" element={<ProjectPickerPage />} />
          <Route path="/p/:projectId" element={<p>project page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    expect(await screen.findByText('demo')).toBeInTheDocument();
    expect(screen.getByText('lab/busy.kicad_pro')).toBeInTheDocument();
    expect(screen.getByText(/Open in KiCad — close it there/)).toBeInTheDocument();
    fireEvent.change(screen.getByRole('searchbox', { name: 'Filter projects' }), { target: { value: 'bus' } });
    expect(screen.queryByText('demo')).toBeNull();
    fireEvent.click(screen.getByRole('link', { name: /busy/ }));
    expect(await screen.findByText('project page')).toBeInTheDocument();
  });

  it('open() merges the summary with the open result (loading the list if needed)', async () => {
    const opened = await useProjects.getState().open('ffffffffffff');
    expect(opened).toMatchObject({ name: 'busy', hasPcb: false, readOnly: true, lockFiles: ['lab/~busy.kicad_sch.lck'] });
    expect(useProjects.getState().current?.id).toBe('ffffffffffff');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/stores/projects.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/stores/projects.ts`:
```ts
import { z } from 'zod';
import { create } from 'zustand';
import { apiGet, apiPost } from '@/api/http';

export const ProjectSummary = z.object({
  id: z.string(),
  name: z.string(),
  relPath: z.string(),
  hasSch: z.boolean(),
  hasPcb: z.boolean(),
  locked: z.boolean(),
});
export type ProjectSummary = z.infer<typeof ProjectSummary>;

const OpenResult = z.object({
  id: z.string(),
  readOnly: z.boolean(),
  lockFiles: z.array(z.string()),
  kicadVersion: z.string().nullable(),
});

export type OpenProject = ProjectSummary & z.infer<typeof OpenResult>;

interface ProjectsState {
  projects: ProjectSummary[];
  loading: boolean;
  error: string | null;
  current: OpenProject | null;
  load: () => Promise<void>;
  open: (id: string) => Promise<OpenProject | null>;
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

export const useProjects = create<ProjectsState>()((set, get) => ({
  projects: [],
  loading: false,
  error: null,
  current: null,
  load: async () => {
    set({ loading: true, error: null });
    try {
      const projects = z.array(ProjectSummary).parse(await apiGet<unknown>('/projects'));
      set({ projects, loading: false });
    } catch (e) {
      set({ loading: false, error: messageOf(e) });
    }
  },
  open: async (id) => {
    try {
      if (!get().projects.some((p) => p.id === id)) await get().load();
      const result = OpenResult.parse(await apiPost<unknown>(`/projects/${encodeURIComponent(id)}/open`, {}));
      const summary = get().projects.find((p) => p.id === id);
      if (!summary) throw new Error('Project not found');
      const current: OpenProject = { ...summary, ...result };
      set({ current, error: null });
      return current;
    } catch (e) {
      set({ current: null, error: messageOf(e) });
      return null;
    }
  },
}));
```

`frontend/src/pages/ProjectPickerPage.tsx`:
```tsx
import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useProjects } from '@/stores/projects';

export function ProjectPickerPage() {
  const projects = useProjects((s) => s.projects);
  const loading = useProjects((s) => s.loading);
  const error = useProjects((s) => s.error);
  const load = useProjects((s) => s.load);
  const [filter, setFilter] = useState('');

  useEffect(() => {
    void load();
  }, [load]);

  const shown = useMemo(() => {
    const f = filter.trim().toLowerCase();
    return f ? projects.filter((p) => `${p.name} ${p.relPath}`.toLowerCase().includes(f)) : projects;
  }, [projects, filter]);

  return (
    <main className="mx-auto flex min-h-full max-w-3xl flex-col gap-4 p-4">
      <header className="flex items-center justify-between">
        <h1 className="text-xl font-semibold">Projects</h1>
        <Link to="/settings" className="text-sm text-accent">Settings</Link>
      </header>
      <input type="search" aria-label="Filter projects" placeholder="Filter…" value={filter}
        onChange={(e) => setFilter(e.target.value)} className="rounded border border-border bg-surface px-3 py-2" />
      {loading && <p className="text-muted">Loading…</p>}
      {error && <p role="alert" className="text-danger">{error}</p>}
      {!loading && !error && projects.length === 0 && (
        <p className="text-muted">No KiCad projects found under the projects directory.</p>
      )}
      <ul className="space-y-2">
        {shown.map((p) => (
          <li key={p.id}>
            <Link to={`/p/${p.id}`} className="block rounded-lg border border-border bg-surface px-4 py-3 hover:border-accent">
              <span className="font-medium">{p.name}</span>
              <span className="block text-xs text-muted">{p.relPath}</span>
              <span className="mt-1 flex gap-2 text-xs">
                {p.hasSch && <span className="rounded bg-surface-2 px-1.5">Schematic</span>}
                {p.hasPcb && <span className="rounded bg-surface-2 px-1.5">PCB</span>}
              </span>
              {p.locked && (
                <span className="mt-2 block text-xs text-warning">
                  Open in KiCad — close it there. CopperChat will open it read-only.
                </span>
              )}
            </Link>
          </li>
        ))}
      </ul>
      <footer className="mt-auto"><PoweredBy /></footer>
    </main>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/stores/projects.test.tsx && npm run typecheck && npm run lint`
Expected: 2 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/projects.ts frontend/src/stores/projects.test.tsx frontend/src/pages/ProjectPickerPage.tsx
git commit -m "feat: projects store and project picker with lock warning

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.11: Responsive layouts, panel registry, viewer slot, project page

**Files:**
- Create: `frontend/src/layouts/panels.ts`, `frontend/src/layouts/ProjectLayout.tsx`, `frontend/src/panels/index.ts`, `frontend/src/components/icons.tsx`, `frontend/src/components/viewer/ViewerSlot.tsx`, `frontend/src/pages/ProjectPage.tsx`
- Test: `frontend/src/layouts/panels.test.tsx`, `frontend/src/layouts/ProjectLayout.test.tsx`

**Interfaces:**
- Produces (`@/layouts/panels`, Phases 7/8 call `registerPanel` from modules imported by `src/panels/index.ts`):
  ```ts
  interface PanelProps { projectId: string }
  interface PanelDefinition { id: string; title: string; icon: ComponentType<{ className?: string }>; component: ComponentType<PanelProps>; order?: number }
  function registerPanel(def: PanelDefinition): () => void;   // same id replaces; returns unregister
  function usePanels(): PanelDefinition[];                     // sorted by order, then title
  ```
  Reserved panel ids: `history` (Phase 7, also the iPhone "History" tab), `git` (Phase 7), `skills`, `parts` (Phase 8).
- Produces: `type LayoutMode = 'split' | 'sidebar' | 'phone'`; `useLayoutMode(): LayoutMode`; `PHONE_QUERY = '(max-width: 767px)'`; `PORTRAIT_TABLET_QUERY = '(min-width: 768px) and (max-width: 1199px) and (orientation: portrait)'`; `CHAT_WIDTH_KEY = 'copperchat.chatWidthPx'`; `ProjectLayout({projectId, title, viewer, chat})`.
- Produces: `ViewerSlot({projectId}: {projectId: string})` — **stub owned by Phase 6B**, which replaces its body with the real viewer.

- [ ] **Step 1: Write the failing test**

`frontend/src/layouts/panels.test.tsx`:
```tsx
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { registerPanel, usePanels } from './panels';

const Icon = () => <svg />;

function List() {
  return <ul>{usePanels().map((p) => <li key={p.id}>{p.title}</li>)}</ul>;
}

describe('panel registry', () => {
  it('registers, orders, replaces and unregisters panels reactively', () => {
    render(<List />);
    let offHistory: () => void = () => {};
    act(() => {
      registerPanel({ id: 'b', title: 'Parts', icon: Icon, component: () => null, order: 20 });
      offHistory = registerPanel({ id: 'a', title: 'History', icon: Icon, component: () => null, order: 10 });
    });
    expect(screen.getAllByRole('listitem').map((li) => li.textContent)).toEqual(['History', 'Parts']);
    act(() => registerPanel({ id: 'b', title: 'Parts v2', icon: Icon, component: () => null, order: 20 }));
    expect(screen.getByText('Parts v2')).toBeInTheDocument();
    act(() => offHistory());
    expect(screen.queryByText('History')).toBeNull();
  });
});
```

`frontend/src/layouts/ProjectLayout.test.tsx`:
```tsx
import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it } from 'vitest';
import { setMedia } from '@/test/media';
import { registerPanel } from './panels';
import { CHAT_WIDTH_KEY, PHONE_QUERY, PORTRAIT_TABLET_QUERY, ProjectLayout } from './ProjectLayout';

const Icon = () => <svg />;
let off: Array<() => void> = [];
afterEach(() => { off.forEach((f) => f()); off = []; });

function renderLayout() {
  return render(
    <MemoryRouter>
      <ProjectLayout projectId="p1" title="demo" viewer={<div>VIEWER</div>} chat={<div>CHAT</div>} />
    </MemoryRouter>,
  );
}

describe('ProjectLayout', () => {
  it('desktop: viewer left, chat right, keyboard-resizable divider persisted to localStorage', () => {
    localStorage.setItem(CHAT_WIDTH_KEY, '400');
    renderLayout();
    expect(screen.getByText('VIEWER')).toBeInTheDocument();
    expect(screen.getByText('CHAT')).toBeInTheDocument();
    const divider = screen.getByRole('separator', { name: 'Resize chat' });
    expect(divider).toHaveAttribute('aria-valuenow', '400');
    fireEvent.keyDown(divider, { key: 'ArrowLeft' });
    expect(divider).toHaveAttribute('aria-valuenow', '432');
    expect(localStorage.getItem(CHAT_WIDTH_KEY)).toBe('432');
  });

  it('iPad portrait: chat is a collapsible sidebar', () => {
    setMedia((q) => q === PORTRAIT_TABLET_QUERY);
    renderLayout();
    expect(screen.getByText('CHAT')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Hide chat' }));
    expect(screen.queryByText('CHAT')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Show chat' }));
    expect(screen.getByText('CHAT')).toBeInTheDocument();
  });

  it('iPhone: bottom tabs Chat/View/History/More; More lists registered panels', () => {
    setMedia((q) => q === PHONE_QUERY);
    act(() => {
      off.push(registerPanel({ id: 'history', title: 'History', icon: Icon, component: () => <p>HISTORY PANEL</p> }));
      off.push(registerPanel({ id: 'skills', title: 'Skills', icon: Icon, component: () => <p>SKILLS PANEL</p> }));
    });
    renderLayout();
    const tabs = screen.getByRole('tablist', { name: 'Sections' });
    expect(tabs).toHaveTextContent('ChatViewHistoryMore');
    expect(screen.getByText('CHAT')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'View' }));
    expect(screen.getByText('VIEWER')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    expect(screen.getByText('HISTORY PANEL')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'More' }));
    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));
    expect(screen.getByText('SKILLS PANEL')).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/layouts`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/layouts/panels.ts`:
```ts
import { type ComponentType, useSyncExternalStore } from 'react';

export interface PanelProps {
  projectId: string;
}

export interface PanelDefinition {
  id: string;
  title: string;
  icon: ComponentType<{ className?: string }>;
  component: ComponentType<PanelProps>;
  order?: number;
}

const registry = new Map<string, PanelDefinition>();
let snapshot: PanelDefinition[] = [];
const listeners = new Set<() => void>();

function publish() {
  snapshot = [...registry.values()].sort((a, b) => (a.order ?? 100) - (b.order ?? 100) || a.title.localeCompare(b.title));
  for (const l of [...listeners]) l();
}

export function registerPanel(def: PanelDefinition): () => void {
  registry.set(def.id, def);
  publish();
  return () => {
    if (registry.get(def.id) === def) {
      registry.delete(def.id);
      publish();
    }
  };
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

export function usePanels(): PanelDefinition[] {
  return useSyncExternalStore(subscribe, () => snapshot, () => snapshot);
}
```

`frontend/src/panels/index.ts`:
```ts
/**
 * Side-effect imports of panel modules that call registerPanel().
 * Phase 7 adds `import './history';` and `import './git';`,
 * Phase 8 adds `import './skills';` and `import './parts';`.
 */
export {};
```

`frontend/src/components/icons.tsx`:
```tsx
type IconProps = { className?: string };
const base = { width: 20, height: 20, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2 } as const;

export const ChatIcon = ({ className }: IconProps) => (
  <svg {...base} className={className} aria-hidden><path d="M4 5h16v10H8l-4 4z" /></svg>
);
export const ViewIcon = ({ className }: IconProps) => (
  <svg {...base} className={className} aria-hidden><rect x="3" y="4" width="18" height="16" rx="2" /><path d="M7 15l3-4 3 3 4-5" /></svg>
);
export const HistoryIcon = ({ className }: IconProps) => (
  <svg {...base} className={className} aria-hidden><path d="M3 12a9 9 0 1 0 3-6.7M3 4v4h4M12 7v5l3 2" /></svg>
);
export const MoreIcon = ({ className }: IconProps) => (
  <svg {...base} className={className} aria-hidden><circle cx="5" cy="12" r="1" /><circle cx="12" cy="12" r="1" /><circle cx="19" cy="12" r="1" /></svg>
);
```

`frontend/src/components/viewer/ViewerSlot.tsx` (stub owned by Phase 6B):
```tsx
export function ViewerSlot({ projectId }: { projectId: string }) {
  return (
    <div data-testid="viewer-slot" data-project-id={projectId}
      className="flex h-full items-center justify-center bg-surface text-sm text-muted">
      Viewer
    </div>
  );
}
```

`frontend/src/layouts/ProjectLayout.tsx`:
```tsx
import { type KeyboardEvent, type PointerEvent, type ReactNode, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChatIcon, HistoryIcon, MoreIcon, ViewIcon } from '@/components/icons';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { type PanelDefinition, usePanels } from './panels';

export type LayoutMode = 'split' | 'sidebar' | 'phone';
export const PHONE_QUERY = '(max-width: 767px)';
export const PORTRAIT_TABLET_QUERY = '(min-width: 768px) and (max-width: 1199px) and (orientation: portrait)';
export const CHAT_WIDTH_KEY = 'copperchat.chatWidthPx';
const MIN_CHAT = 320;
const DEFAULT_CHAT = 420;
const STEP = 32;

export function useLayoutMode(): LayoutMode {
  const phone = useMediaQuery(PHONE_QUERY);
  const portraitTablet = useMediaQuery(PORTRAIT_TABLET_QUERY);
  return phone ? 'phone' : portraitTablet ? 'sidebar' : 'split';
}

function clampWidth(px: number): number {
  const max = Math.max(MIN_CHAT, Math.round(window.innerWidth * 0.7));
  return Math.min(max, Math.max(MIN_CHAT, Math.round(px)));
}

function readWidth(): number {
  const stored = Number(localStorage.getItem(CHAT_WIDTH_KEY));
  return Number.isFinite(stored) && stored > 0 ? clampWidth(stored) : DEFAULT_CHAT;
}

interface Props {
  projectId: string;
  title: string;
  viewer: ReactNode;
  chat: ReactNode;
}

function PanelSheet({ panel, projectId, onClose }: { panel: PanelDefinition; projectId: string; onClose: () => void }) {
  const Body = panel.component;
  return (
    <div role="dialog" aria-label={panel.title} className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-border bg-bg shadow-xl">
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="font-medium">{panel.title}</h2>
        <button type="button" aria-label="Close panel" onClick={onClose}>✕</button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto"><Body projectId={projectId} /></div>
    </div>
  );
}

function Header({ title, panels, onPanel }: { title: string; panels: PanelDefinition[]; onPanel: (p: PanelDefinition) => void }) {
  return (
    <header className="flex items-center gap-2 border-b border-border bg-surface px-3 py-2">
      <Link to="/projects" className="text-sm text-accent">Projects</Link>
      <h1 className="min-w-0 flex-1 truncate font-medium">{title}</h1>
      {panels.map((p) => (
        <button key={p.id} type="button" onClick={() => onPanel(p)} className="flex items-center gap-1 text-sm">
          <p.icon className="h-4 w-4" />{p.title}
        </button>
      ))}
      <Link to="/settings" className="text-sm">Settings</Link>
    </header>
  );
}

function SplitView({ viewer, chat }: { viewer: ReactNode; chat: ReactNode }) {
  const [width, setWidth] = useState(readWidth);
  const containerRef = useRef<HTMLDivElement>(null);
  const dragging = useRef(false);
  const commit = (px: number) => {
    const w = clampWidth(px);
    setWidth(w);
    localStorage.setItem(CHAT_WIDTH_KEY, String(w));
  };
  const onKeyDown = (e: KeyboardEvent) => {
    if (e.key === 'ArrowLeft') commit(width + STEP);
    if (e.key === 'ArrowRight') commit(width - STEP);
  };
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture(e.pointerId);
  };
  const onPointerMove = (e: PointerEvent<HTMLDivElement>) => {
    const box = containerRef.current?.getBoundingClientRect();
    if (dragging.current && box) setWidth(clampWidth(box.right - e.clientX));
  };
  const onPointerUp = () => {
    if (!dragging.current) return;
    dragging.current = false;
    commit(width);
  };
  return (
    <div ref={containerRef} className="flex min-h-0 flex-1">
      <div className="min-w-0 flex-1">{viewer}</div>
      <div role="separator" aria-label="Resize chat" aria-orientation="vertical" tabIndex={0}
        aria-valuenow={width} aria-valuemin={MIN_CHAT}
        onKeyDown={onKeyDown} onPointerDown={onPointerDown} onPointerMove={onPointerMove} onPointerUp={onPointerUp}
        className="w-1.5 cursor-col-resize bg-border hover:bg-accent" style={{ touchAction: 'none' }} />
      <div style={{ width }} className="min-h-0 shrink-0">{chat}</div>
    </div>
  );
}

function SidebarView({ viewer, chat }: { viewer: ReactNode; chat: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="relative flex min-h-0 flex-1">
      <div className="min-w-0 flex-1">{viewer}</div>
      {open && <div className="w-[380px] min-h-0 shrink-0 border-l border-border">{chat}</div>}
      <button type="button" onClick={() => setOpen((o) => !o)}
        className="absolute right-2 top-2 z-10 rounded bg-surface-2 px-2 py-1 text-xs shadow">
        {open ? 'Hide chat' : 'Show chat'}
      </button>
    </div>
  );
}

type PhoneTab = 'chat' | 'view' | 'history' | 'more';

function PhoneView({ projectId, viewer, chat, panels }: { projectId: string; viewer: ReactNode; chat: ReactNode; panels: PanelDefinition[] }) {
  const [tab, setTab] = useState<PhoneTab>('chat');
  const [panelId, setPanelId] = useState<string | null>(null);
  const history = panels.find((p) => p.id === 'history');
  const others = panels.filter((p) => p.id !== 'history');
  const active = others.find((p) => p.id === panelId);
  const tabs: Array<{ id: PhoneTab; label: string; Icon: typeof ChatIcon }> = [
    { id: 'chat', label: 'Chat', Icon: ChatIcon },
    { id: 'view', label: 'View', Icon: ViewIcon },
    { id: 'history', label: 'History', Icon: HistoryIcon },
    { id: 'more', label: 'More', Icon: MoreIcon },
  ];
  let body: ReactNode;
  if (tab === 'chat') body = chat;
  else if (tab === 'view') body = viewer;
  else if (tab === 'history') body = history ? <history.component projectId={projectId} /> : <p className="p-4 text-muted">History is not available.</p>;
  else if (active) body = (
    <div className="flex h-full flex-col">
      <button type="button" className="px-3 py-2 text-left text-sm text-accent" onClick={() => setPanelId(null)}>← More</button>
      <div className="min-h-0 flex-1 overflow-auto"><active.component projectId={projectId} /></div>
    </div>
  );
  else body = (
    <ul className="divide-y divide-border">
      {others.map((p) => (
        <li key={p.id}>
          <button type="button" onClick={() => setPanelId(p.id)} className="flex w-full items-center gap-2 px-4 py-3 text-left">
            <p.icon className="h-5 w-5" />{p.title}
          </button>
        </li>
      ))}
      <li><Link to="/settings" className="block px-4 py-3">Settings</Link></li>
    </ul>
  );
  return (
    <div className="flex min-h-0 flex-1 flex-col">
      <div className="min-h-0 flex-1">{body}</div>
      <nav role="tablist" aria-label="Sections" className="flex border-t border-border bg-surface pb-[env(safe-area-inset-bottom)]">
        {tabs.map(({ id, label, Icon }) => (
          <button key={id} type="button" role="tab" aria-selected={tab === id} onClick={() => setTab(id)}
            className={`flex flex-1 flex-col items-center py-1.5 text-xs ${tab === id ? 'text-accent' : 'text-muted'}`}>
            <Icon className="h-5 w-5" />{label}
          </button>
        ))}
      </nav>
    </div>
  );
}

export function ProjectLayout({ projectId, title, viewer, chat }: Props) {
  const mode = useLayoutMode();
  const panels = usePanels();
  const [sheet, setSheet] = useState<PanelDefinition | null>(null);
  return (
    <div className="flex h-full flex-col">
      {mode !== 'phone' && <Header title={title} panels={panels} onPanel={setSheet} />}
      {mode === 'split' && <SplitView viewer={viewer} chat={chat} />}
      {mode === 'sidebar' && <SidebarView viewer={viewer} chat={chat} />}
      {mode === 'phone' && <PhoneView projectId={projectId} viewer={viewer} chat={chat} panels={panels} />}
      {sheet && mode !== 'phone' && <PanelSheet panel={sheet} projectId={projectId} onClose={() => setSheet(null)} />}
    </div>
  );
}
```

`frontend/src/pages/ProjectPage.tsx`:
```tsx
import { useEffect } from 'react';
import { useParams } from 'react-router-dom';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { ViewerSlot } from '@/components/viewer/ViewerSlot';
import { ProjectLayout } from '@/layouts/ProjectLayout';
import { useComposer } from '@/stores/composer';
import { useProjects } from '@/stores/projects';

export function ProjectPage() {
  const { projectId = '' } = useParams();
  const open = useProjects((s) => s.open);
  const current = useProjects((s) => (s.current?.id === projectId ? s.current : null));
  const error = useProjects((s) => s.error);

  useEffect(() => {
    useComposer.getState().clear();
    void open(projectId);
  }, [projectId, open]);

  if (error && !current) return <p role="alert" className="p-6 text-danger">{error}</p>;
  return (
    <ProjectLayout projectId={projectId} title={current?.name ?? '…'}
      viewer={<ViewerSlot projectId={projectId} />}
      chat={<ChatPanel projectId={projectId} readOnly={current?.readOnly ?? false} />} />
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/layouts && npm run typecheck && npm run lint`
Expected: 4 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/layouts frontend/src/panels frontend/src/components/icons.tsx frontend/src/components/viewer/ViewerSlot.tsx frontend/src/pages/ProjectPage.tsx
git commit -m "feat: responsive project layouts (split, sidebar, phone tabs) and panel registry

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.12: Themes and Settings page

**Files:**
- Create: `frontend/src/stores/ui.ts`, `frontend/src/pages/SettingsPage.tsx`, `frontend/src/pages/settings/NotificationsSection.tsx`
- Test: `frontend/src/stores/ui.test.ts`, `frontend/src/pages/SettingsPage.test.tsx`

**Interfaces:**
- Produces (`@/stores/ui`, consumed by 6B for the agent-view `theme=` parameter):
  ```ts
  type ThemePref = 'light' | 'dark' | 'system';  type ResolvedTheme = 'light' | 'dark';
  const THEME_KEY = 'copperchat.theme';
  function resolveTheme(pref: ThemePref, prefersDark: boolean): ResolvedTheme;
  const useUi: UseBoundStore<{ theme: ThemePref; resolvedTheme: ResolvedTheme; setTheme(t: ThemePref): void; syncSystem(): void }>;
  function initTheme(): () => void;   // applies data-theme now and follows prefers-color-scheme
  ```
- Produces: `NotificationsSection` — **stub owned by Phase 9**, which replaces its body.

- [ ] **Step 1: Write the failing test**

`frontend/src/stores/ui.test.ts`:
```ts
import { describe, expect, it } from 'vitest';
import { setMedia } from '@/test/media';
import { THEME_KEY, initTheme, resolveTheme, useUi } from './ui';

describe('theme', () => {
  it('resolves system preference', () => {
    expect(resolveTheme('system', true)).toBe('dark');
    expect(resolveTheme('system', false)).toBe('light');
    expect(resolveTheme('light', true)).toBe('light');
  });

  it('persists the preference and applies data-theme on :root, following the OS in system mode', () => {
    const stop = initTheme();
    useUi.getState().setTheme('dark');
    expect(localStorage.getItem(THEME_KEY)).toBe('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
    useUi.getState().setTheme('system');
    expect(document.documentElement.dataset.theme).toBe('light');
    setMedia((q) => q === '(prefers-color-scheme: dark)');
    expect(useUi.getState().resolvedTheme).toBe('dark');
    expect(document.documentElement.dataset.theme).toBe('dark');
    stop();
  });
});
```

`frontend/src/pages/SettingsPage.test.tsx`:
```tsx
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { MemoryRouter, Route, Routes } from 'react-router-dom';
import { describe, expect, it, vi } from 'vitest';
import { useAuth } from '@/stores/auth';
import { useUi } from '@/stores/ui';
import { SettingsPage } from './SettingsPage';

describe('SettingsPage', () => {
  it('switches theme, signs out, and is branded correctly', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    useAuth.setState({ status: 'authenticated', username: 'admin', error: null });
    render(
      <MemoryRouter initialEntries={['/settings']}>
        <Routes>
          <Route path="/settings" element={<SettingsPage />} />
          <Route path="/login" element={<p>login page</p>} />
        </Routes>
      </MemoryRouter>,
    );
    fireEvent.click(screen.getByRole('radio', { name: 'Dark' }));
    expect(useUi.getState().theme).toBe('dark');
    expect(screen.getByText('Signed in as admin')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Notifications' })).toBeInTheDocument();
    expect(screen.getByText(/Powered by Claude/)).toBeInTheDocument();
    expect(document.body.textContent).not.toMatch(/Claude Code/);
    fireEvent.click(screen.getByRole('button', { name: 'Sign out' }));
    await waitFor(() => expect(screen.getByText('login page')).toBeInTheDocument());
    expect(useAuth.getState().status).toBe('anonymous');
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/stores/ui.test.ts src/pages/SettingsPage.test.tsx`
Expected: FAIL — modules not found.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/stores/ui.ts`:
```ts
import { create } from 'zustand';

export type ThemePref = 'light' | 'dark' | 'system';
export type ResolvedTheme = 'light' | 'dark';
export const THEME_KEY = 'copperchat.theme';
const DARK_QUERY = '(prefers-color-scheme: dark)';

export function resolveTheme(pref: ThemePref, prefersDark: boolean): ResolvedTheme {
  if (pref === 'system') return prefersDark ? 'dark' : 'light';
  return pref;
}

function readPref(): ThemePref {
  const v = typeof localStorage !== 'undefined' ? localStorage.getItem(THEME_KEY) : null;
  return v === 'light' || v === 'dark' || v === 'system' ? v : 'system';
}

const prefersDark = () => window.matchMedia(DARK_QUERY).matches;

function apply(resolved: ResolvedTheme) {
  document.documentElement.dataset.theme = resolved;
}

interface UiState {
  theme: ThemePref;
  resolvedTheme: ResolvedTheme;
  setTheme: (t: ThemePref) => void;
  syncSystem: () => void;
}

export const useUi = create<UiState>()((set, get) => ({
  theme: readPref(),
  resolvedTheme: resolveTheme(readPref(), prefersDark()),
  setTheme: (theme) => {
    localStorage.setItem(THEME_KEY, theme);
    const resolvedTheme = resolveTheme(theme, prefersDark());
    apply(resolvedTheme);
    set({ theme, resolvedTheme });
  },
  syncSystem: () => {
    const resolvedTheme = resolveTheme(get().theme, prefersDark());
    apply(resolvedTheme);
    set({ resolvedTheme });
  },
}));

export function initTheme(): () => void {
  useUi.getState().syncSystem();
  const mql = window.matchMedia(DARK_QUERY);
  const onChange = () => useUi.getState().syncSystem();
  mql.addEventListener('change', onChange);
  return () => mql.removeEventListener('change', onChange);
}
```

`frontend/src/pages/settings/NotificationsSection.tsx` (stub owned by Phase 9):
```tsx
export function NotificationsSection() {
  return (
    <section className="space-y-2">
      <h2 className="font-medium">Notifications</h2>
      <p className="text-sm text-muted">Push notifications are not available in this build.</p>
    </section>
  );
}
```

`frontend/src/pages/SettingsPage.tsx`:
```tsx
import { Link, useNavigate } from 'react-router-dom';
import { PoweredBy } from '@/components/PoweredBy';
import { useAuth } from '@/stores/auth';
import { type ThemePref, useUi } from '@/stores/ui';
import { NotificationsSection } from './settings/NotificationsSection';

const THEMES: Array<{ value: ThemePref; label: string }> = [
  { value: 'light', label: 'Light' },
  { value: 'dark', label: 'Dark' },
  { value: 'system', label: 'System' },
];

export function SettingsPage() {
  const theme = useUi((s) => s.theme);
  const setTheme = useUi((s) => s.setTheme);
  const username = useAuth((s) => s.username);
  const logout = useAuth((s) => s.logout);
  const navigate = useNavigate();

  return (
    <main className="mx-auto flex min-h-full max-w-xl flex-col gap-6 p-4">
      <header className="flex items-center gap-3">
        <Link to="/projects" className="text-sm text-accent">← Projects</Link>
        <h1 className="text-xl font-semibold">Settings</h1>
      </header>
      <section className="space-y-2">
        <h2 className="font-medium">Theme</h2>
        <div role="radiogroup" aria-label="Theme" className="flex gap-4">
          {THEMES.map((t) => (
            <label key={t.value} className="flex items-center gap-1.5 text-sm">
              <input type="radio" name="theme" value={t.value} checked={theme === t.value} onChange={() => setTheme(t.value)} />
              {t.label}
            </label>
          ))}
        </div>
      </section>
      <NotificationsSection />
      <section className="space-y-2">
        <h2 className="font-medium">Account</h2>
        <p className="text-sm">Signed in as {username}</p>
        <button type="button" className="rounded border border-border px-3 py-1.5 text-sm"
          onClick={async () => { await logout(); navigate('/login', { replace: true }); }}>
          Sign out
        </button>
      </section>
      <footer className="mt-auto"><PoweredBy /></footer>
    </main>
  );
}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npx vitest run src/stores/ui.test.ts src/pages/SettingsPage.test.tsx && npm run typecheck && npm run lint`
Expected: 3 tests pass.

- [ ] **Step 5: Commit**
```bash
git add frontend/src/stores/ui.ts frontend/src/stores/ui.test.ts frontend/src/pages/SettingsPage.tsx frontend/src/pages/SettingsPage.test.tsx frontend/src/pages/settings
git commit -m "feat: light/dark/system theme via CSS variables and Settings page

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

### Task 6A.13: App routes, bootstrap, Playwright smoke

**Files:**
- Modify: `frontend/src/App.tsx`, `frontend/src/main.tsx`
- Create: `frontend/playwright.config.ts`, `frontend/e2e/smoke.spec.ts`
- Test: `frontend/src/App.test.tsx`

**Interfaces:**
- Produces: `AppRoutes()` (router-less, for tests), `App()` (BrowserRouter). Routes: `/login`, `/projects`, `/p/:projectId`, `/settings`, `*` → `/projects`.

- [ ] **Step 1: Write the failing test**

`frontend/src/App.test.tsx`:
```tsx
import { render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { AppRoutes } from './App';
import { useAuth } from './stores/auth';

beforeEach(() => useAuth.setState({ status: 'unknown', username: null, error: null }));

describe('App routes', () => {
  it('unknown paths go to /projects which requires login', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(JSON.stringify({ detail: 'no' }), { status: 401, headers: { 'content-type': 'application/json' } })));
    render(<MemoryRouter initialEntries={['/nowhere']}><AppRoutes /></MemoryRouter>);
    expect(await screen.findByRole('button', { name: 'Sign in' })).toBeInTheDocument();
  });

  it('authenticated users see the project picker', async () => {
    vi.stubGlobal('fetch', vi.fn(async (url: string) =>
      new Response(JSON.stringify(url === '/api/auth/me' ? { username: 'admin' } : []), { status: 200, headers: { 'content-type': 'application/json' } })));
    render(<MemoryRouter initialEntries={['/projects']}><AppRoutes /></MemoryRouter>);
    expect(await screen.findByRole('heading', { name: 'Projects' })).toBeInTheDocument();
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd frontend && npx vitest run src/App.test.tsx`
Expected: FAIL — `AppRoutes` is not exported.

- [ ] **Step 3: Write minimal implementation**

`frontend/src/App.tsx`:
```tsx
import { BrowserRouter, Navigate, Route, Routes } from 'react-router-dom';
import { LoginPage } from '@/pages/LoginPage';
import { ProjectPage } from '@/pages/ProjectPage';
import { ProjectPickerPage } from '@/pages/ProjectPickerPage';
import { RequireAuth } from '@/pages/RequireAuth';
import { SettingsPage } from '@/pages/SettingsPage';

export function AppRoutes() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route element={<RequireAuth />}>
        <Route path="/projects" element={<ProjectPickerPage />} />
        <Route path="/p/:projectId" element={<ProjectPage />} />
        <Route path="/settings" element={<SettingsPage />} />
      </Route>
      <Route path="*" element={<Navigate to="/projects" replace />} />
    </Routes>
  );
}

export function App() {
  return (
    <BrowserRouter>
      <AppRoutes />
    </BrowserRouter>
  );
}
```

`frontend/src/main.tsx`:
```tsx
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@/App';
import '@/panels';
import { initTheme } from '@/stores/ui';
import '@/theme/theme.css';

initTheme();

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
```

`frontend/playwright.config.ts`:
```ts
import { defineConfig, devices } from '@playwright/test';

const external = process.env.E2E_BASE_URL;

export default defineConfig({
  testDir: './e2e',
  timeout: 30_000,
  use: { baseURL: external ?? 'http://localhost:5173', trace: 'retain-on-failure' },
  webServer: external
    ? undefined
    : { command: 'npm run dev -- --port 5173 --strictPort', url: 'http://localhost:5173', reuseExistingServer: true },
  projects: [
    { name: 'desktop', use: { ...devices['Desktop Chrome'] } },
    { name: 'phone', use: { ...devices['Pixel 7'] } },
  ],
});
```

`frontend/e2e/smoke.spec.ts`:
```ts
import { expect, test } from '@playwright/test';

const PID = 'a1b2c3d4e5f6';
const msg = (id: number, role: string, content: string, extra: Record<string, unknown> = {}) => ({
  id, projectId: PID, clientMessageId: null, runId: null, role, kind: 'text', content,
  status: 'done', metadata: {}, createdAt: '2026-10-15T10:00:00Z', ...extra,
});

test('mocked API: login → pick project → chat round-trip', async ({ page }) => {
  let authed = false;
  await page.route('**/api/auth/me', (r) =>
    authed ? r.fulfill({ json: { username: 'admin' } }) : r.fulfill({ status: 401, json: { detail: 'not authenticated' } }));
  await page.route('**/api/auth/login', async (r) => { authed = true; await r.fulfill({ status: 204 }); });
  await page.route('**/api/projects', (r) => r.fulfill({ json: [
    { id: PID, name: 'demo', relPath: 'demo/demo.kicad_pro', hasSch: true, hasPcb: true, locked: false },
    { id: 'ffffffffffff', name: 'busy', relPath: 'lab/busy.kicad_pro', hasSch: true, hasPcb: false, locked: true },
  ] }));
  await page.route(`**/api/projects/${PID}/open`, (r) =>
    r.fulfill({ json: { id: PID, readOnly: false, lockFiles: [], kicadVersion: '10.99.0' } }));
  await page.route(`**/api/projects/${PID}/messages**`, (r) =>
    r.fulfill({ json: { messages: [msg(1, 'assistant', 'Hello from the fixture')], hasMore: false } }));
  await page.routeWebSocket('**/api/ws', (ws) => {
    ws.onMessage((raw) => {
      const f = JSON.parse(String(raw)) as { type: string; clientMessageId?: string; text?: string };
      if (f.type === 'ping') ws.send(JSON.stringify({ type: 'pong', projectId: null }));
      if (f.type === 'send') {
        ws.send(JSON.stringify({ type: 'message', projectId: PID, message: msg(2, 'user', f.text ?? '', { clientMessageId: f.clientMessageId }) }));
        ws.send(JSON.stringify({ type: 'run-started', projectId: PID, runId: 'r1' }));
        ws.send(JSON.stringify({ type: 'message', projectId: PID, message: msg(3, 'assistant', `Echo: ${f.text}`, { runId: 'r1' }) }));
        ws.send(JSON.stringify({ type: 'run-finished', projectId: PID, runId: 'r1', reason: 'completed' }));
      }
    });
  });

  await page.goto('/projects');
  await expect(page).toHaveURL(/\/login$/);
  await page.getByLabel('Username').fill('admin');
  await page.getByLabel('Password').fill('secret');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await expect(page.getByText(/Open in KiCad — close it there/)).toBeVisible();
  await page.getByRole('link', { name: /demo/ }).click();
  await expect(page.getByText('Hello from the fixture')).toBeVisible();
  await page.getByRole('textbox', { name: 'Message' }).fill('ping the board');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.getByText('Echo: ping the board')).toBeVisible();
});

test('real backend with dev_fake_agent (opt-in)', async ({ page }) => {
  test.skip(!process.env.E2E_BASE_URL || !process.env.E2E_PASSWORD, 'set E2E_BASE_URL, E2E_USERNAME, E2E_PASSWORD');
  await page.goto('/login');
  await page.getByLabel('Username').fill(process.env.E2E_USERNAME ?? 'admin');
  await page.getByLabel('Password').fill(process.env.E2E_PASSWORD ?? '');
  await page.getByRole('button', { name: 'Sign in' }).click();
  await page.getByRole('link').filter({ hasText: '.kicad_pro' }).first().click();
  await page.getByRole('textbox', { name: 'Message' }).fill('hello');
  await page.getByRole('button', { name: 'Send' }).click();
  await expect(page.locator('[data-role="assistant"]').last()).toBeVisible({ timeout: 20_000 });
});
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd frontend && npm test -- --run && npm run typecheck && npm run lint && npm run build`
Expected: full unit suite green; build succeeds.
Optional (local): `cd frontend && npx playwright install chromium && npm run e2e` → mocked smoke passes on `desktop` and `phone`; the real-backend test is skipped unless `E2E_BASE_URL`/`E2E_PASSWORD` are set (backend started with `DEV_FAKE_AGENT=true`).

- [ ] **Step 5: Commit**
```bash
git add frontend/src/App.tsx frontend/src/App.test.tsx frontend/src/main.tsx frontend/playwright.config.ts frontend/e2e
git commit -m "feat: app routes, theme bootstrap and Playwright smoke test (mocked API, opt-in real backend)

Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>"
```

---

## Phase self-review

**Spec coverage:**
- §4.2 Frontend: project picker with lock warning (6A.10); chat with lazy loading, streaming, collapsible tool calls, inline thumbnails → fullscreen (6A.6, 6A.7); queued messages with cancel, Stop, usage indicator (6A.8, 6A.9); Settings with theme/notifications(stub)/account (6A.12); layouts desktop/iPad landscape split with persisted resizable divider, iPad portrait collapsible sidebar, iPhone bottom tabs Chat/View/History/More, composer pinned above the keyboard (6A.8, 6A.11); CSS-variable theming (6A.1, 6A.12). Viewer, attachments, history/skills/parts/git panels are extension points filled by 6B/7/8 (`ViewerSlot`, `composerAttachmentSlot`, `registerPanel`).
- §5.1: every frame carries `projectId`, zod validation on both directions (6A.2).
- §5.2: every bullet has a dedicated test — baseline-then-subscribe + in-flight guard (6A.4), older pages with `before` + failed-load reset (6A.4), element anchoring + top sentinel gated on not-stuck + 80 px stick + pill (6A.6), keyed by id / negative optimistic ids / immutable updates / id-order insert (6A.4), reconnect + visibility heal + unknown-delta heal (6A.5), singleton socket with 0.5–15 s backoff, queued outgoing frames, heartbeat (6A.2).
- §11 chat-pipeline bug classes: page/replay ordering, duplicate sends (retransmit), reconnect mid-run, stuck Stop, half-streamed heal, scroll anchoring — Review Focus 1 tests live in 6A.4–6A.6.
- §12 branding: "Powered by Claude" on Login/Settings/picker; Settings test asserts no "Claude Code".

**Deviations / additions:**
- Retransmission: pending optimistic `send` frames are not queued in the socket; they are (re)sent by the chat store on every `open`/`resumed`, relying on the server's `clientMessageId` dedupe (spec §5 step 1). Avoids a double send from queue flush + resend.
- Suspect-row heal (`suspectIds`): a streaming row that lived through a reconnect, or showed up via an unknown-id delta, is healed again once it reaches a terminal status, because the DB copy of a streaming row lags the live stream (spec §5 step 4 persists periodically).
- `error` frames may carry `projectId: null` (unparseable client frame); the zod mirror allows that and the store shows them on every loaded chat. Phase 5's pydantic model must allow the same.
- Assumed shapes not pinned by C8: `GET /api/auth/me` → `{username}`; message metadata keys listed in Task 6A.7.
- CI uses Node 20 as specified (Vite 7 needs ≥ 20.19, which `setup-node` "20" provides).
