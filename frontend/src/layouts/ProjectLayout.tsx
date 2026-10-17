import { type KeyboardEvent, type PointerEvent, type ReactNode, useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { ChatIcon, CloseIcon, HistoryIcon, MoreIcon, ViewIcon } from '@/components/icons';
import { useMediaQuery } from '@/hooks/useMediaQuery';
import { type PanelDefinition, usePanels } from './panels';

export type LayoutMode = 'split' | 'sidebar' | 'phone';
export const PHONE_QUERY = '(max-width: 767px)';
export const PORTRAIT_TABLET_QUERY = '(min-width: 768px) and (max-width: 1199px) and (orientation: portrait)';
export const CHAT_WIDTH_KEY = 'copperchat.chatWidthPx';
const MIN_CHAT = 320;
const DEFAULT_CHAT = 420;
const STEP = 32;

/** split = desktop / iPad landscape, sidebar = iPad portrait, phone = iPhone. */
export function useLayoutMode(): LayoutMode {
  const phone = useMediaQuery(PHONE_QUERY);
  const portraitTablet = useMediaQuery(PORTRAIT_TABLET_QUERY);
  return phone ? 'phone' : portraitTablet ? 'sidebar' : 'split';
}

function clampWidth(px: number): number {
  const max = Math.max(MIN_CHAT, Math.round(window.innerWidth * 0.7));
  return Math.min(max, Math.max(MIN_CHAT, Math.round(px)));
}

/** Storage may be unavailable (private mode, blocked cookies): fall back like stores/ui.ts. */
function readWidth(): number {
  let stored = NaN;
  try {
    stored = Number(localStorage.getItem(CHAT_WIDTH_KEY));
  } catch {
    // keep the default
  }
  return Number.isFinite(stored) && stored > 0 ? clampWidth(stored) : DEFAULT_CHAT;
}

function storeWidth(px: number): void {
  try {
    localStorage.setItem(CHAT_WIDTH_KEY, String(px));
  } catch {
    // the width still applies for this session
  }
}

export interface ProjectLayoutProps {
  projectId: string;
  title: string;
  viewer: ReactNode;
  chat: ReactNode;
}

function PanelSheet({ panel, projectId, onClose }: { panel: PanelDefinition; projectId: string; onClose: () => void }) {
  const Body = panel.component;
  useEffect(() => {
    const onKey = (e: globalThis.KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', onKey);
    return () => window.removeEventListener('keydown', onKey);
  }, [onClose]);
  return (
    <div
      role="dialog"
      aria-label={panel.title}
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-md flex-col border-l border-border bg-bg shadow-xl"
    >
      <header className="flex items-center justify-between border-b border-border px-3 py-2">
        <h2 className="font-medium">{panel.title}</h2>
        <button type="button" aria-label="Close panel" onClick={onClose}>
          <CloseIcon className="h-4 w-4" aria-hidden />
        </button>
      </header>
      <div className="min-h-0 flex-1 overflow-auto">
        <Body projectId={projectId} />
      </div>
    </div>
  );
}

function Header({
  title,
  panels,
  onPanel,
}: {
  title: string;
  panels: PanelDefinition[];
  onPanel?: (p: PanelDefinition) => void;
}) {
  return (
    <header className="flex items-center gap-3 border-b border-border bg-surface px-3 py-2 pt-[max(0.5rem,env(safe-area-inset-top))]">
      <Link to="/projects" className="text-sm text-accent">
        Projects
      </Link>
      <h1 className="min-w-0 flex-1 truncate font-medium">{title}</h1>
      {onPanel &&
        panels.map((p) => (
          <button key={p.id} type="button" onClick={() => onPanel(p)} className="flex items-center gap-1 text-sm">
            <p.icon className="h-4 w-4" />
            {p.title}
          </button>
        ))}
      {onPanel && (
        <Link to="/settings" className="text-sm">
          Settings
        </Link>
      )}
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
    storeWidth(w);
  };
  const onKeyDown = (e: KeyboardEvent) => {
    // The chat is on the right: ArrowLeft moves the divider left = wider chat.
    if (e.key === 'ArrowLeft') commit(width + STEP);
    if (e.key === 'ArrowRight') commit(width - STEP);
  };
  const onPointerDown = (e: PointerEvent<HTMLDivElement>) => {
    dragging.current = true;
    e.currentTarget.setPointerCapture?.(e.pointerId);
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
      <div
        role="separator"
        aria-label="Resize chat"
        aria-orientation="vertical"
        tabIndex={0}
        aria-valuenow={width}
        aria-valuemin={MIN_CHAT}
        onKeyDown={onKeyDown}
        onPointerDown={onPointerDown}
        onPointerMove={onPointerMove}
        onPointerUp={onPointerUp}
        onPointerCancel={onPointerUp}
        className="w-1.5 shrink-0 cursor-col-resize bg-border hover:bg-accent focus-visible:bg-accent"
        style={{ touchAction: 'none' }}
      />
      <div style={{ width }} className="min-h-0 shrink-0">
        {chat}
      </div>
    </div>
  );
}

function SidebarView({ viewer, chat }: { viewer: ReactNode; chat: ReactNode }) {
  const [open, setOpen] = useState(true);
  return (
    <div className="flex min-h-0 flex-1">
      <div className="relative min-w-0 flex-1">
        {viewer}
        <button
          type="button"
          aria-expanded={open}
          onClick={() => setOpen((o) => !o)}
          className="absolute right-2 top-2 z-10 rounded bg-surface-2 px-2 py-1 text-xs shadow"
        >
          {open ? 'Hide chat' : 'Show chat'}
        </button>
      </div>
      {/* Hidden, not unmounted: keeps scroll position and composer focus state. */}
      <div hidden={!open} className="min-h-0 w-[380px] shrink-0 border-l border-border">
        {chat}
      </div>
    </div>
  );
}

type PhoneTab = 'chat' | 'view' | 'history' | 'more';

const PHONE_TABS: Array<{ id: PhoneTab; label: string; Icon: typeof ChatIcon }> = [
  { id: 'chat', label: 'Chat', Icon: ChatIcon },
  { id: 'view', label: 'View', Icon: ViewIcon },
  { id: 'history', label: 'History', Icon: HistoryIcon },
  { id: 'more', label: 'More', Icon: MoreIcon },
];

function PhoneView({
  projectId,
  viewer,
  chat,
  panels,
}: {
  projectId: string;
  viewer: ReactNode;
  chat: ReactNode;
  panels: PanelDefinition[];
}) {
  const [tab, setTab] = useState<PhoneTab>('chat');
  const [panelId, setPanelId] = useState<string | null>(null);
  const history = panels.find((p) => p.id === 'history');
  const others = panels.filter((p) => p.id !== 'history');
  const active = others.find((p) => p.id === panelId);

  let extra: ReactNode = null;
  if (tab === 'history') {
    extra = history ? (
      <history.component projectId={projectId} />
    ) : (
      <p className="p-4 text-muted">History is not available yet.</p>
    );
  } else if (tab === 'more' && active) {
    extra = (
      <div className="flex h-full flex-col">
        <button type="button" className="px-3 py-2 text-left text-sm text-accent" onClick={() => setPanelId(null)}>
          ← More
        </button>
        <div className="min-h-0 flex-1 overflow-auto">
          <active.component projectId={projectId} />
        </div>
      </div>
    );
  } else if (tab === 'more') {
    extra = (
      <ul className="divide-y divide-border">
        {others.map((p) => (
          <li key={p.id}>
            <button
              type="button"
              onClick={() => setPanelId(p.id)}
              className="flex w-full items-center gap-2 px-4 py-3 text-left"
            >
              <p.icon className="h-5 w-5" />
              {p.title}
            </button>
          </li>
        ))}
        <li>
          <Link to="/settings" className="block px-4 py-3">
            Settings
          </Link>
        </li>
      </ul>
    );
  }

  return (
    <div className="flex min-h-0 flex-1 flex-col">
      {/* Chat and viewer stay mounted across tabs: no viewer reload, chat scroll kept. */}
      <div hidden={tab !== 'chat'} className="min-h-0 flex-1">
        {chat}
      </div>
      <div hidden={tab !== 'view'} className="min-h-0 flex-1">
        {viewer}
      </div>
      {extra !== null && <div className="min-h-0 flex-1 overflow-auto">{extra}</div>}
      <nav
        role="tablist"
        aria-label="Sections"
        className="flex border-t border-border bg-surface pb-[env(safe-area-inset-bottom)]"
      >
        {PHONE_TABS.map(({ id, label, Icon }) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            onClick={() => setTab(id)}
            className={`flex flex-1 flex-col items-center py-1.5 text-xs ${tab === id ? 'text-accent' : 'text-muted'}`}
          >
            <Icon className="h-5 w-5" aria-hidden />
            {label}
          </button>
        ))}
      </nav>
    </div>
  );
}

export function ProjectLayout({ projectId, title, viewer, chat }: ProjectLayoutProps) {
  const mode = useLayoutMode();
  const panels = usePanels();
  const [sheetId, setSheetId] = useState<string | null>(null);
  const sheet = panels.find((p) => p.id === sheetId) ?? null;
  return (
    <div className="flex h-full flex-col">
      {mode === 'phone' ? (
        <Header title={title} panels={panels} />
      ) : (
        <Header title={title} panels={panels} onPanel={(p) => setSheetId(p.id)} />
      )}
      {mode === 'split' && <SplitView viewer={viewer} chat={chat} />}
      {mode === 'sidebar' && <SidebarView viewer={viewer} chat={chat} />}
      {mode === 'phone' && <PhoneView projectId={projectId} viewer={viewer} chat={chat} panels={panels} />}
      {sheet && mode !== 'phone' && <PanelSheet panel={sheet} projectId={projectId} onClose={() => setSheetId(null)} />}
    </div>
  );
}
