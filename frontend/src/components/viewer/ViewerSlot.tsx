/**
 * Placeholder for the schematic/PCB viewer — STUB OWNED BY PHASE 6B, which replaces the body
 * with `<Viewer projectId={projectId} />` and keeps this signature.
 *
 * Props (deliberately minimal):
 * - `projectId` — 12-hex id of the open project. Everything else the viewer needs comes from
 *   stores: `useProjects().current` (`hasSch`, `hasPcb`, `viewFiles`, `readOnly`),
 *   `useUi().resolvedTheme`, `useComposer().insertText` (tap → @REF) and `view-updated`
 *   frames via `wsClient.onFrame`.
 *
 * It fills its parent (h-full); the layout keeps it mounted across iPhone tab switches.
 */
export function ViewerSlot({ projectId }: { projectId: string }) {
  return (
    <div
      data-testid="viewer-slot"
      data-project-id={projectId}
      className="flex h-full items-center justify-center bg-surface text-sm text-muted"
    >
      Viewer
    </div>
  );
}
