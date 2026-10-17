import { Viewer } from './Viewer';

/** The viewer area of the project layout (6A stub replaced by Phase 6B; signature unchanged). */
export function ViewerSlot({ projectId }: { projectId: string }) {
  return (
    <div data-testid="viewer-slot" data-project-id={projectId} className="h-full">
      <Viewer projectId={projectId} />
    </div>
  );
}
