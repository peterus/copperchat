import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from '@/App';
import '@/panels';
import { installSessionReset } from '@/stores/session';
import { initTheme } from '@/stores/ui';
import '@/theme/theme.css';

initTheme();
// Logout / 401 / WebSocket 4401 → chat, composer and projects are reset (L9/L11).
installSessionReset();

const root = document.getElementById('root');
if (!root) throw new Error('#root missing');
createRoot(root).render(
  <StrictMode>
    <App />
  </StrictMode>,
);
