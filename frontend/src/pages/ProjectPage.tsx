import { useEffect } from 'react';
import { Link, useParams } from 'react-router-dom';
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

export function ProjectPage() {
  const { projectId = '' } = useParams();
  const open = useProjects((s) => s.open);
  const current = useProjects((s) => (s.current?.id === projectId ? s.current : null));
  const opening = useProjects((s) => s.opening === projectId);
  const openError = useProjects((s) => s.openError);

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
      chat={<ChatPanel projectId={projectId} readOnly={current.readOnly} />}
    />
  );
}
