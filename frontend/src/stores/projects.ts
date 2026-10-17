import { z } from 'zod';
import { create } from 'zustand';
import { apiGet, apiPost } from '@/api/http';

/** GET /api/projects (backend web/routes_projects.py). */
export const ProjectSummary = z.object({
  id: z.string(),
  name: z.string(),
  relPath: z.string(),
  hasSch: z.boolean(),
  hasPcb: z.boolean(),
  locked: z.boolean(),
});
export type ProjectSummary = z.infer<typeof ProjectSummary>;

/** POST /api/projects/{id}/open. `viewFiles` (index X7) names the snapshot files the viewer loads. */
export const OpenResult = z.object({
  id: z.string(),
  readOnly: z.boolean(),
  lockFiles: z.array(z.string()),
  kicadVersion: z.string().nullable(),
  viewFiles: z.object({ sch: z.array(z.string()), pcb: z.array(z.string()) }),
});
export type OpenResult = z.infer<typeof OpenResult>;

export type OpenProject = ProjectSummary & OpenResult;

interface ProjectsState {
  projects: ProjectSummary[];
  loading: boolean;
  /** Error of the last list load. */
  error: string | null;
  current: OpenProject | null;
  /** Id of the project whose open request is in flight. */
  opening: string | null;
  /** Error of the last open request (e.g. 409/500, KiCad unavailable). */
  openError: string | null;
  load: () => Promise<void>;
  /** Resolves null on failure (see `openError`) or when a newer open() superseded this one. */
  open: (id: string) => Promise<OpenProject | null>;
  /** Back to the initial state (logout / 4401); in-flight results are discarded. */
  reset: () => void;
}

const messageOf = (e: unknown) => (e instanceof Error ? e.message : String(e));

const initial = {
  projects: [] as ProjectSummary[],
  loading: false,
  error: null,
  current: null,
  opening: null,
  openError: null,
};

export const useProjects = create<ProjectsState>()((set, get) => {
  // Bumped by every open() and reset(): only the latest request may write its result.
  let openSeq = 0;
  let loadSeq = 0;

  return {
    ...initial,

    load: async () => {
      const seq = ++loadSeq;
      set({ loading: true, error: null });
      try {
        const projects = z.array(ProjectSummary).parse(await apiGet<unknown>('/projects'));
        if (seq === loadSeq) set({ projects, loading: false });
      } catch (e) {
        if (seq === loadSeq) set({ loading: false, error: messageOf(e) });
      }
    },

    open: async (id) => {
      const seq = ++openSeq;
      set({ opening: id, openError: null, current: get().current?.id === id ? get().current : null });
      try {
        if (!get().projects.some((p) => p.id === id)) await get().load();
        const result = OpenResult.parse(await apiPost<unknown>(`/projects/${encodeURIComponent(id)}/open`, {}));
        if (seq !== openSeq) return null;
        const summary = get().projects.find((p) => p.id === id);
        if (!summary) throw new Error('Project not found');
        const current: OpenProject = { ...summary, ...result };
        set({ current, opening: null, openError: null });
        return current;
      } catch (e) {
        if (seq === openSeq) set({ current: null, opening: null, openError: messageOf(e) });
        return null;
      }
    },

    reset: () => {
      openSeq += 1;
      loadSeq += 1;
      set({ ...initial });
    },
  };
});
