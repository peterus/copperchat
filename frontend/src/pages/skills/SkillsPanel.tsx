import { useCallback, useEffect, useRef, useState } from 'react';
import {
  acknowledgeSkillUpdate,
  customiseSkill,
  getSkill,
  listSkills,
  resetSkill,
  saveUserSkill,
  setSkillEnabled,
  type SkillDetail,
  type SkillOrigin,
  type SkillSummary,
} from '@/api/skills';
import type { PanelProps } from '@/layouts/panels';

const BADGE: Record<SkillOrigin, string> = {
  builtin: 'bg-surface-2 text-muted',
  user: 'bg-accent text-accent-fg',
  project: 'border border-accent text-accent',
};

const BUTTON = 'rounded border border-border px-2 py-1 text-xs hover:bg-surface-2 disabled:opacity-50';
const PRIMARY = 'rounded bg-accent px-2 py-1 text-xs font-medium text-accent-fg disabled:opacity-50';

const message = (e: unknown) => (e instanceof Error ? e.message : String(e));

export function SkillsPanel({ projectId }: PanelProps) {
  const [skills, setSkills] = useState<SkillSummary[] | null>(null);
  const [selected, setSelected] = useState<SkillDetail | null>(null);
  const [draft, setDraft] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [confirmDelete, setConfirmDelete] = useState(false);
  // Only the latest detail request may update the view (fast clicks across the list).
  const openToken = useRef(0);

  const refresh = useCallback(async () => {
    try {
      setSkills(await listSkills(projectId));
    } catch (e) {
      setError(message(e));
    }
  }, [projectId]);

  const open = useCallback(
    async (name: string) => {
      const token = ++openToken.current;
      setError(null);
      setConfirmDelete(false);
      try {
        const detail = await getSkill(name, projectId);
        if (token !== openToken.current) return;
        setSelected(detail);
        setDraft(detail.content);
      } catch (e) {
        if (token === openToken.current) setError(message(e));
      }
    },
    [projectId],
  );

  useEffect(() => {
    setSelected(null);
    void refresh();
  }, [refresh]);

  async function run(action: () => Promise<unknown>, reopen: string | null) {
    setError(null);
    setBusy(true);
    try {
      await action();
      await refresh();
      if (reopen) await open(reopen);
    } catch (e) {
      setError(message(e));
    } finally {
      setBusy(false);
    }
  }

  const deleteSkill = (name: string) => {
    openToken.current++;
    setSelected(null);
    setConfirmDelete(false);
    return resetSkill(name);
  };

  return (
    <div className="flex flex-col gap-3 p-3">
      <div className="flex items-center justify-between">
        <p className="text-xs text-muted">Skills the agent can load in this project.</p>
        <button type="button" className={BUTTON} onClick={() => void refresh()}>
          Refresh
        </button>
      </div>
      {error && (
        <p role="alert" className="rounded border border-danger bg-danger/10 px-2 py-1 text-sm text-danger">
          {error}
        </p>
      )}
      {skills?.length === 0 && <p className="text-sm text-muted">No skills available.</p>}
      <ul aria-label="Skills" className="flex flex-col gap-1">
        {skills?.map((s) => (
          <li key={s.name}>
            <button
              type="button"
              aria-current={selected?.name === s.name ? 'true' : undefined}
              onClick={() => void open(s.name)}
              className={`flex w-full flex-col gap-0.5 rounded px-2 py-1.5 text-left hover:bg-surface ${selected?.name === s.name ? 'bg-surface' : ''}`}
            >
              <span className="flex flex-wrap items-center gap-1.5">
                <span className="font-medium">{s.name}</span>
                <span className={`rounded px-1.5 text-xs ${BADGE[s.origin]}`}>{s.origin}</span>
                {s.agent && (
                  <span className="rounded bg-surface-2 px-1.5 text-xs text-muted" title={`Used by ${s.agent}`}>
                    researcher
                  </span>
                )}
                {s.originUpdated && <span className="text-xs text-warning">original updated</span>}
                {!s.enabled && <span className="text-xs text-muted">disabled</span>}
              </span>
              {s.description && <span className="text-xs text-muted">{s.description}</span>}
              {s.shadowed.length > 0 && (
                <span className="text-xs text-muted">overrides {s.shadowed.join(', ')}</span>
              )}
            </button>
          </li>
        ))}
      </ul>
      {selected && (
        <section aria-label={`Skill ${selected.name}`} className="flex flex-col gap-2 border-t border-border pt-3">
          <h3 className="font-semibold">{selected.name}</h3>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={selected.enabled}
              disabled={busy}
              onChange={(e) => {
                const enabled = e.target.checked;
                void run(() => setSkillEnabled(projectId, selected.name, enabled), selected.name);
              }}
            />
            Enabled in this project
          </label>
          {selected.originUpdated && (
            <div className="flex flex-col gap-2 rounded border border-warning p-2">
              <p className="text-sm font-medium text-warning">
                The original updated since you customised this skill.
              </p>
              {selected.originDiff && (
                <pre aria-label="Changes in the original" className="max-h-64 overflow-auto rounded bg-surface-2 p-2 text-xs">
                  {selected.originDiff}
                </pre>
              )}
              <div>
                <button
                  type="button"
                  className={BUTTON}
                  disabled={busy}
                  onClick={() => void run(() => acknowledgeSkillUpdate(selected.name), selected.name)}
                >
                  Mark as reviewed
                </button>
              </div>
            </div>
          )}
          <textarea
            aria-label="Skill content"
            className="min-h-64 w-full rounded border border-border bg-surface p-2 font-mono text-xs read-only:text-muted"
            value={draft}
            readOnly={!selected.editable}
            onChange={(e) => setDraft(e.target.value)}
          />
          <div className="flex flex-wrap items-center gap-2">
            {selected.editable && (
              <button
                type="button"
                className={PRIMARY}
                disabled={busy || draft === selected.content}
                onClick={() => void run(() => saveUserSkill(selected.name, draft), selected.name)}
              >
                Save
              </button>
            )}
            {selected.origin === 'builtin' && (
              <button
                type="button"
                className={BUTTON}
                disabled={busy}
                onClick={() => void run(() => customiseSkill(selected.name), selected.name)}
              >
                Customise
              </button>
            )}
            {selected.origin === 'user' && selected.hasBuiltin && (
              <button
                type="button"
                className={BUTTON}
                disabled={busy}
                onClick={() => void run(() => resetSkill(selected.name), selected.name)}
              >
                Reset to original
              </button>
            )}
            {selected.origin === 'user' &&
              !selected.hasBuiltin &&
              (confirmDelete ? (
                <>
                  <button
                    type="button"
                    className="rounded bg-danger px-2 py-1 text-xs font-medium text-white disabled:opacity-50"
                    disabled={busy}
                    onClick={() => void run(() => deleteSkill(selected.name), null)}
                  >
                    Confirm delete
                  </button>
                  <button type="button" className={BUTTON} onClick={() => setConfirmDelete(false)}>
                    Cancel
                  </button>
                </>
              ) : (
                <button type="button" className={`${BUTTON} text-danger`} onClick={() => setConfirmDelete(true)}>
                  Delete
                </button>
              ))}
            {selected.origin === 'builtin' && (
              <p className="text-xs text-muted">Built-in skills are read-only; customise to edit your own copy.</p>
            )}
            {selected.origin === 'project' && (
              <p className="text-xs text-muted">
                Project skills live in <code>.kicad-agent/skills/</code> of the project and are edited there.
              </p>
            )}
          </div>
        </section>
      )}
    </div>
  );
}
