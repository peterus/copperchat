import { PCB_LAYERS, type PcbLayer } from '@/lib/viewUrls';

export function LayerToggles({ layers, onToggle }: { layers: readonly PcbLayer[]; onToggle: (l: PcbLayer) => void }) {
  return (
    <fieldset className="flex flex-wrap gap-x-3 gap-y-1 text-xs">
      <legend className="sr-only">Layers</legend>
      {PCB_LAYERS.map((l) => (
        <label key={l} className="flex min-h-7 items-center gap-1">
          <input type="checkbox" checked={layers.includes(l)} onChange={() => onToggle(l)} />
          {l}
        </label>
      ))}
    </fieldset>
  );
}
