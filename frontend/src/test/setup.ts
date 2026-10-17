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
