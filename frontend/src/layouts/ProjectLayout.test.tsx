import { act, fireEvent, render, screen } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { createViewStore, ViewStoreContext } from '@/stores/view';
import { FakeWsClient } from '@/test/fakeWsClient';
import { setMedia } from '@/test/media';
import { registerPanel } from './panels';
import { CHAT_WIDTH_KEY, PHONE_QUERY, PORTRAIT_TABLET_QUERY, ProjectLayout } from './ProjectLayout';

const Icon = () => <svg />;
let off: Array<() => void> = [];
afterEach(() => {
  off.forEach((f) => f());
  off = [];
});

function renderLayout() {
  return render(
    <MemoryRouter>
      <ProjectLayout projectId="a1b2c3d4e5f6" title="demo" viewer={<div>VIEWER</div>} chat={<div>CHAT</div>} />
    </MemoryRouter>,
  );
}

describe('ProjectLayout', () => {
  it('desktop: viewer left, chat right, keyboard-resizable divider persisted to localStorage', () => {
    localStorage.setItem(CHAT_WIDTH_KEY, '400');
    renderLayout();
    expect(screen.getByText('VIEWER')).toBeInTheDocument();
    expect(screen.getByText('CHAT')).toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'demo' })).toBeInTheDocument();
    const divider = screen.getByRole('separator', { name: 'Resize chat' });
    expect(divider).toHaveAttribute('aria-valuenow', '400');
    fireEvent.keyDown(divider, { key: 'ArrowLeft' });
    expect(divider).toHaveAttribute('aria-valuenow', '432');
    expect(localStorage.getItem(CHAT_WIDTH_KEY)).toBe('432');
    fireEvent.keyDown(divider, { key: 'ArrowRight' });
    expect(localStorage.getItem(CHAT_WIDTH_KEY)).toBe('400');
  });

  it('desktop: unavailable localStorage falls back to the default width and does not throw', () => {
    const get = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    const set = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new DOMException('denied', 'SecurityError');
    });
    renderLayout();
    const divider = screen.getByRole('separator', { name: 'Resize chat' });
    expect(divider).toHaveAttribute('aria-valuenow', '420');
    fireEvent.keyDown(divider, { key: 'ArrowLeft' });
    expect(divider).toHaveAttribute('aria-valuenow', '452');
    get.mockRestore();
    set.mockRestore();
  });

  it('desktop: registered panels open as a sheet with {projectId}', () => {
    act(() => {
      off.push(
        registerPanel({
          id: 'git',
          title: 'Git',
          icon: Icon,
          component: ({ projectId }) => <p>GIT PANEL {projectId}</p>,
        }),
      );
    });
    renderLayout();
    fireEvent.click(screen.getByRole('button', { name: 'Git' }));
    expect(screen.getByRole('dialog', { name: 'Git' })).toHaveTextContent('GIT PANEL a1b2c3d4e5f6');
    fireEvent.click(screen.getByRole('button', { name: 'Close panel' }));
    expect(screen.queryByRole('dialog')).toBeNull();
  });

  it('iPad portrait: chat is a collapsible sidebar', () => {
    setMedia((q) => q === PORTRAIT_TABLET_QUERY);
    renderLayout();
    expect(screen.getByText('CHAT')).toBeInTheDocument();
    expect(screen.queryByRole('separator')).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Hide chat' }));
    // stays mounted (scroll position, live state) but hidden
    expect(screen.getByText('CHAT')).not.toBeVisible();
    fireEvent.click(screen.getByRole('button', { name: 'Show chat' }));
    expect(screen.getByText('CHAT')).toBeVisible();
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
    expect(screen.getByRole('tab', { name: 'Chat' })).toHaveAttribute('aria-selected', 'true');
    expect(screen.getByText('CHAT')).toBeVisible();
    expect(screen.getByText('VIEWER')).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'View' }));
    // chat and viewer stay mounted across tab switches (no viewer reload, scroll kept)
    expect(screen.getByText('VIEWER')).toBeVisible();
    expect(screen.getByText('CHAT')).not.toBeVisible();
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    expect(screen.getByText('HISTORY PANEL')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('tab', { name: 'More' }));
    expect(screen.queryByRole('button', { name: 'History' })).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: 'Skills' }));
    expect(screen.getByText('SKILLS PANEL')).toBeInTheDocument();
  });

  it('iPhone: History tab without a registered history panel says so', () => {
    setMedia((q) => q === PHONE_QUERY);
    renderLayout();
    fireEvent.click(screen.getByRole('tab', { name: 'History' }));
    expect(screen.getByText('History is not available yet.')).toBeInTheDocument();
  });

  it('iPhone: a highlight request (e.g. from the Changes panel) switches to the View tab', () => {
    setMedia((q) => q === PHONE_QUERY);
    const view = createViewStore(new FakeWsClient());
    render(
      <MemoryRouter>
        <ViewStoreContext.Provider value={view}>
          <ProjectLayout projectId="a1b2c3d4e5f6" title="demo" viewer={<div>VIEWER</div>} chat={<div>CHAT</div>} />
        </ViewStoreContext.Provider>
      </MemoryRouter>,
    );
    expect(screen.getByRole('tab', { name: 'Chat' })).toHaveAttribute('aria-selected', 'true');
    act(() => view.getState().setHighlight({ refs: ['R1'], boxes: [], sheetPath: null }));
    expect(screen.getByRole('tab', { name: 'View' })).toHaveAttribute('aria-selected', 'true');
  });
});
