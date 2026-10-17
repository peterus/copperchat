import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@/App';
import { wsClient } from '@/api/ws';
import '@/panels';
import { registerServiceWorker } from '@/push/push';
import { startVisibilityReporter } from '@/push/visibility';
import { useProjects } from '@/stores/projects';
import { installSessionReset } from '@/stores/session';
import { initTheme } from '@/stores/ui';
import '@/theme/theme.css';

initTheme();
// Logout / 401 / WebSocket 4401 → chat, composer and projects are reset (L9/L11).
installSessionReset();
// PWA: push-only service worker; tell the server whether this page is visible (push gating).
void registerServiceWorker();
const visibility = startVisibilityReporter(() => useProjects.getState().current?.id ?? null, wsClient);
useProjects.subscribe((state, prev) => {
  if (state.current?.id !== prev.current?.id) visibility.notify();
});

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
