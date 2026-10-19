import { useCallback, useEffect, useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { createConversation, listConversations } from '@/api/conversations';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { ViewerSlot } from '@/components/viewer/ViewerSlot';
import { ProjectLayout } from '@/layouts/ProjectLayout';
import { useComposer } from '@/stores/composer';
import { useProjects } from '@/stores/projects';

/** The composer is global (one draft); a draft belongs to the project it was typed for. */
let composerProjectId: string | null = null;

export function clearComposerOnProjectSwitch(projectId: string): void {
  if (composerProjectId !== null && composerProjectId !== projectId) useComposer.getState().clear();
  composerProjectId = projectId;
}

/** One in-flight resolution per project: a double mount must not create two sessions. */
const resolving = new Map<string, Promise<string>>();

/**
 * Interim default-session shim (C1; the session UI comes in C2): the newest open conversation
 * (the list is newest activity first), else a new `free` one.
 */
export function resolveDefaultConversation(projectId: string): Promise<string> {
  const inflight = resolving.get(projectId);
  if (inflight) return inflight;
  const p = (async () => {
    const list = await listConversations(projectId);
    const open = list.find((c) => c.status === 'open');
    return open ? open.id : (await createConversation(projectId, { type: 'free' })).id;
  })().finally(() => resolving.delete(projectId));
  resolving.set(projectId, p);
  return p;
}

type SessionState = { projectId: string; conversationId: string | null; error: string | null };

function useDefaultConversation(projectId: string) {
  const [state, setState] = useState<SessionState>({ projectId, conversationId: null, error: null });
  const [attempt, setAttempt] = useState(0);
  useEffect(() => {
    let live = true;
    resolveDefaultConversation(projectId).then(
      (conversationId) => live && setState({ projectId, conversationId, error: null }),
      (e: unknown) => live && setState({ projectId, conversationId: null, error: e instanceof Error ? e.message : String(e) }),
    );
    return () => {
      live = false;
    };
  }, [projectId, attempt]);
  const retry = useCallback(() => {
    setState({ projectId, conversationId: null, error: null });
    setAttempt((a) => a + 1);
  }, [projectId]);
  // A state of the previous project (before the effect ran for the new one) does not count.
  const own = state.projectId === projectId ? state : { projectId, conversationId: null, error: null };
  return { ...own, retry };
}

export function ProjectPage() {
  const { projectId = '' } = useParams();
  const open = useProjects((s) => s.open);
  const current = useProjects((s) => (s.current?.id === projectId ? s.current : null));
  const opening = useProjects((s) => s.opening === projectId);
  const openError = useProjects((s) => s.openError);
  const session = useDefaultConversation(projectId);

  useEffect(() => {
    clearComposerOnProjectSwitch(projectId);
    void open(projectId);
  }, [projectId, open]);

  if (!current) {
    // Opening starts KiCad and can take a while; a failure (409/500, KiCad unavailable) is shown,
    // never an endless spinner.
    return (
      <main className="mx-auto flex min-h-full max-w-xl flex-col justify-center gap-4 p-6">
        {openError && !opening ? (
          <>
            <p role="alert" className="text-danger">
              Could not open the project: {openError}
            </p>
            <div className="flex gap-3">
              <button
                type="button"
                className="rounded border border-border px-3 py-1.5 text-sm"
                onClick={() => void open(projectId)}
              >
                Retry
              </button>
              <Link to="/projects" className="px-3 py-1.5 text-sm text-accent">
                Back to projects
              </Link>
            </div>
          </>
        ) : (
          <p role="status" className="text-muted">
            Opening project…
          </p>
        )}
      </main>
    );
  }

  return (
    <ProjectLayout
      projectId={projectId}
      title={current.name}
      viewer={<ViewerSlot projectId={projectId} />}
      chat={
        session.conversationId !== null ? (
          <ChatPanel projectId={projectId} conversationId={session.conversationId} readOnly={current.readOnly} />
        ) : session.error !== null ? (
          <div role="alert" className="flex flex-col items-start gap-2 p-4 text-sm text-danger">
            <p>Could not load the session: {session.error}</p>
            <button type="button" className="rounded border border-border px-3 py-1.5 text-fg" onClick={session.retry}>
              Retry
            </button>
          </div>
        ) : (
          <p role="status" className="p-4 text-muted">
            Loading the session…
          </p>
        )
      }
    />
  );
}
