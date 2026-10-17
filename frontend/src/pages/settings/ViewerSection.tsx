import { type ViewerLimits, useViewerPrefs } from '@/stores/viewerPrefs';

const FIELDS: Array<{ key: keyof ViewerLimits; label: string }> = [
  { key: 'mobileMb', label: 'Touch devices (MB)' },
  { key: 'desktopMb', label: 'Desktop (MB)' },
];

/** Per-device size limits of the interactive KiCad view (large-board guard). */
export function ViewerSection() {
  const limits = useViewerPrefs((s) => s.limits);
  const setLimit = useViewerPrefs((s) => s.setLimit);
  return (
    <section className="space-y-2">
      <h2 className="font-medium">Viewer</h2>
      <p className="text-sm text-muted">
        Larger schematics or boards open as the agent view first; the interactive view loads on request. Stored on
        this device.
      </p>
      <div className="flex flex-wrap gap-4">
        {FIELDS.map((f) => (
          <label key={f.key} className="flex items-center gap-2 text-sm">
            {f.label}
            <input
              type="number"
              min={1}
              step={1}
              inputMode="numeric"
              className="w-20 rounded border border-border bg-bg px-2 py-1"
              value={limits[f.key]}
              onChange={(e) => setLimit(f.key, Number(e.target.value))}
            />
          </label>
        ))}
      </div>
    </section>
  );
}
