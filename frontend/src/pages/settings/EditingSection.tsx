import { useEffect, useId, useRef, useState } from 'react';
import {
  CHANGE_BUDGET_MAX,
  CHANGE_BUDGET_MIN,
  type EditingSettings,
  fetchEditingSettings,
  putEditingSettings,
} from '@/api/editingSettings';
import { errorText } from '@/components/history/restore';
import { useProjects } from '@/stores/projects';

const POLICIES: Array<{ value: EditingSettings['planPolicy']; label: string }> = [
  { value: 'always', label: 'Always' },
  { value: 'large', label: 'For large changes' },
  { value: 'never', label: 'Never' },
];
const BLOCKS: Array<{ value: EditingSettings['annotationBlock']; label: string }> = [
  { value: 100, label: '×100' },
  { value: 1000, label: '×1000' },
];

/**
 * Settings › Editing for the current project (the settings page has no project route, index
 * "calls made by the plan author"). Every change PUTs only its own field.
 */
export function EditingSection() {
  const project = useProjects((s) => s.current);
  const projectId = project?.id ?? null;
  const [settings, setSettings] = useState<EditingSettings | null>(null);
  const [budget, setBudget] = useState('');
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);
  /**
   * The newest settings the server confirmed (by request order: load or PUT response) and the
   * saves still in flight. Optimistic values show until the last save settles; then the
   * confirmed copy is shown, whatever order the responses (or failures) arrived in.
   */
  const confirmed = useRef<{ seq: number; settings: EditingSettings } | null>(null);
  const inflight = useRef(0);
  const budgetId = useId();

  useEffect(() => {
    setSettings(null);
    setError(null);
    confirmed.current = null;
    inflight.current = 0;
    if (!projectId) return;
    const mine = ++seq.current;
    fetchEditingSettings(projectId).then(
      (s) => {
        if (mine !== seq.current) return;
        confirmed.current = { seq: mine, settings: s };
        setSettings(s);
        setBudget(String(s.changeBudget));
      },
      (e: unknown) => mine === seq.current && setError(`Could not load the editing settings: ${errorText(e)}`),
    );
  }, [projectId]);

  if (!projectId) {
    return (
      <section className="space-y-2">
        <h2 className="font-medium">Editing</h2>
        <p className="text-sm text-muted">Open a project to change its editing settings.</p>
      </section>
    );
  }

  const save = async (patch: Partial<EditingSettings>) => {
    if (!settings) return;
    const mine = ++seq.current;
    const pid = projectId;
    inflight.current += 1;
    setError(null);
    setSettings({ ...settings, ...patch }); // optimistic until every save has settled
    let failure: string | null = null;
    try {
      const next = await putEditingSettings(pid, patch);
      if (useProjects.getState().current?.id !== pid) return;
      if (!confirmed.current || mine > confirmed.current.seq) confirmed.current = { seq: mine, settings: next };
    } catch (e) {
      failure = errorText(e);
    }
    if (useProjects.getState().current?.id !== pid) return;
    if (failure) setError(failure); // never dropped, also when a newer save superseded this one
    inflight.current -= 1;
    if (inflight.current > 0 || !confirmed.current) return;
    setSettings(confirmed.current.settings);
    setBudget(String(confirmed.current.settings.changeBudget));
  };

  const commitBudget = () => {
    if (!settings) return;
    const n = Number(budget);
    if (!Number.isInteger(n) || n < CHANGE_BUDGET_MIN || n > CHANGE_BUDGET_MAX) {
      setError(`The change budget must be a whole number between ${CHANGE_BUDGET_MIN} and ${CHANGE_BUDGET_MAX}.`);
      return;
    }
    if (n !== settings.changeBudget) void save({ changeBudget: n });
    else setError(null);
  };

  return (
    <section className="space-y-3">
      <div>
        <h2 className="font-medium">Editing</h2>
        <p className="text-sm text-muted">For the open project {project?.name}.</p>
      </div>
      {error && (
        <p role="alert" className="rounded bg-danger/10 p-2 text-sm text-danger">
          {error}
        </p>
      )}
      {!settings && !error && <p className="text-sm text-muted">Loading…</p>}
      {settings && (
        <>
          <div className="space-y-1">
            <h3 className="text-sm font-medium">Plan before editing</h3>
            <div role="radiogroup" aria-label="Plan before editing" className="flex flex-wrap gap-x-4 gap-y-1">
              {POLICIES.map((p) => (
                <label key={p.value} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    name="planPolicy"
                    value={p.value}
                    checked={settings.planPolicy === p.value}
                    onChange={() => void save({ planPolicy: p.value })}
                  />
                  {p.label}
                </label>
              ))}
            </div>
          </div>
          <div className="space-y-1">
            <label htmlFor={budgetId} className="block text-sm font-medium">
              Change budget per turn
            </label>
            <input
              id={budgetId}
              type="number"
              inputMode="numeric"
              min={CHANGE_BUDGET_MIN}
              max={CHANGE_BUDGET_MAX}
              step={1}
              value={budget}
              onChange={(e) => setBudget(e.target.value)}
              onBlur={commitBudget}
              onKeyDown={(e) => {
                if (e.key === 'Enter') commitBudget();
              }}
              className="w-24 rounded border border-border bg-surface px-2 py-1 text-sm"
            />
            <p className="text-xs text-muted">Changes the agent may make in one turn without a plan.</p>
          </div>
          <div className="space-y-1">
            <h3 className="text-sm font-medium">Annotation block</h3>
            <div role="radiogroup" aria-label="Annotation block" className="flex gap-4">
              {BLOCKS.map((b) => (
                <label key={b.value} className="flex items-center gap-1.5 text-sm">
                  <input
                    type="radio"
                    name="annotationBlock"
                    value={b.value}
                    checked={settings.annotationBlock === b.value}
                    onChange={() => void save({ annotationBlock: b.value })}
                  />
                  {b.label}
                </label>
              ))}
            </div>
            <p className="text-xs text-muted">New references on sheet n are numbered from n × this block (×100: R101…R199 on sheet 1).</p>
          </div>
          <label className="flex items-center gap-2 text-sm">
            <input
              type="checkbox"
              checked={settings.showAgentView}
              onChange={(e) => void save({ showAgentView: e.target.checked })}
            />
            Show agent view after each step
          </label>
        </>
      )}
    </section>
  );
}
