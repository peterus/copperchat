import { useEffect, useState } from 'react';
import { Link, Navigate, useParams } from 'react-router-dom';
import { ChatPanel } from '@/components/chat/ChatPanel';
import { NewSessionDialog } from '@/components/sessions/NewSessionDialog';
import { SessionList, sessionPath } from '@/components/sessions/SessionList';
import { ViewerSlot } from '@/components/viewer/ViewerSlot';
import { ProjectLayout } from '@/layouts/ProjectLayout';
import { useConversation, useConversations, useConversationsApi } from '@/stores/conversations';
import { usePlansStore } from '@/stores/plans';
import { useProjects } from '@/stores/projects';

/** Conversation ids are 32 lowercase hex characters (backend ConversationId). */
const CONVERSATION_ID = /^[0-9a-f]{32}$/;

/** No open session: "Start a session" (spec §12.1), plus the earlier ones when there are any. */
function NoSession({ projectId, hasEarlier }: { projectId: string; hasEarlier: boolean }) {
  const [dialog, setDialog] = useState(false);
  const [list, setList] = useState(false);
  return (
    <div className="relative flex h-full flex-col items-center justify-center gap-3 p-6 text-center">
      <p className="text-muted">No open session in this project.</p>
      <button type="button" onClick={() => setDialog(true)} className="rounded bg-accent px-4 py-2 font-medium text-accent-fg">
        Start a session
      </button>
      {hasEarlier && (
        <button type="button" aria-expanded={list} onClick={() => setList((o) => !o)} className="text-sm text-accent">
          Earlier sessions
        </button>
      )}
      {list && <SessionList projectId={projectId} currentId={null} onClose={() => setList(false)} />}
      {dialog && <NewSessionDialog projectId={projectId} onClose={() => setDialog(false)} />}
    </div>
  );
}

/**
 * The chat column: `/p/:projectId` redirects to the newest open session (else "Start a
 * session"); `/p/:projectId/s/:cid` shows that session, or "Session not found" for an id that
 * is not one of the project's sessions.
 */
function SessionArea({ projectId, cid, readOnly }: { projectId: string; cid: string | undefined; readOnly: boolean }) {
  const store = useConversationsApi();
  const status = useConversations((s) => s.status[projectId]);
  const error = useConversations((s) => s.errors[projectId] ?? null);
  const list = useConversations((s) => s.byProject[projectId]);
  const conversation = useConversation(projectId, cid ?? null);
  const valid = cid !== undefined && CONVERSATION_ID.test(cid);
  /** The cid whose archived lookup has finished (an archived session is not in the first list). */
  const [checked, setChecked] = useState<string | null>(null);

  useEffect(() => {
    void store.getState().load(projectId);
  }, [store, projectId]);

  useEffect(() => {
    store.getState().view(projectId, valid ? cid : null);
    return () => store.getState().view(projectId, null);
  }, [store, projectId, cid, valid]);

  const loaded = list !== undefined && status !== 'loading';
  useEffect(() => {
    if (!loaded || !valid || conversation || checked === cid) return;
    let live = true;
    void store
      .getState()
      .loadArchived(projectId)
      .finally(() => live && setChecked(cid));
    return () => {
      live = false;
    };
  }, [store, loaded, valid, conversation, checked, cid, projectId]);

  if (status === 'error' && list === undefined) {
    return (
      <div role="alert" className="flex flex-col items-start gap-2 p-4 text-sm text-danger">
        <p>Could not load the sessions: {error}</p>
        <button type="button" className="rounded border border-border px-3 py-1.5 text-fg" onClick={() => void store.getState().load(projectId)}>
          Retry
        </button>
      </div>
    );
  }
  if (cid === undefined) {
    if (!loaded) return <Loading />;
    const open = list.find((c) => !c.archived && c.status !== 'closed');
    if (open) return <Navigate to={sessionPath(projectId, open.id)} replace />;
    return <NoSession projectId={projectId} hasEarlier={list.length > 0} />;
  }
  if (conversation) return <ChatPanel projectId={projectId} conversationId={conversation.id} readOnly={readOnly} />;
  if (!valid || (loaded && checked === cid)) {
    return (
      <div className="flex flex-col items-start gap-2 p-4 text-sm">
        <p className="font-medium">Session not found</p>
        <p className="text-muted">It does not belong to this project, or it no longer exists.</p>
        <Link to={`/p/${projectId}`} className="text-accent">
          Back to the sessions
        </Link>
      </div>
    );
  }
  return <Loading />;
}

function Loading() {
  return (
    <p role="status" className="p-4 text-muted">
      Loading the session…
    </p>
  );
}

export function ProjectPage() {
  const { projectId = '', cid } = useParams();
  const open = useProjects((s) => s.open);
  const current = useProjects((s) => (s.current?.id === projectId ? s.current : null));
  const opening = useProjects((s) => s.opening === projectId);
  const openError = useProjects((s) => s.openError);
  const plans = usePlansStore();

  useEffect(() => {
    void open(projectId);
  }, [projectId, open]);

  // The open plan (its banner shows in the other sessions) and its cards.
  useEffect(() => {
    void plans.getState().load(projectId);
  }, [plans, projectId]);

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
      chat={<SessionArea projectId={projectId} cid={cid} readOnly={current.readOnly} />}
    />
  );
}
