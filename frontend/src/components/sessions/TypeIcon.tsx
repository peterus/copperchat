import { CircuitBoard, ClipboardList, Layers, MessagesSquare } from 'lucide-react';
import type { ConversationType } from '@/protocol/frames';

export const TYPE_LABEL: Record<ConversationType, string> = {
  requirements: 'Requirements',
  schematic: 'Schematic',
  free: 'Free',
  board: 'Board',
};

const ICON = { requirements: ClipboardList, schematic: CircuitBoard, free: MessagesSquare, board: Layers } as const;

/** The session type's icon, labelled "<Type> session" for screen readers. */
export function TypeIcon({ type, className = 'h-4 w-4' }: { type: ConversationType; className?: string }) {
  const Icon = ICON[type];
  return (
    <span role="img" aria-label={`${TYPE_LABEL[type]} session`} title={`${TYPE_LABEL[type]} session`} className="inline-flex shrink-0">
      <Icon className={className} />
    </span>
  );
}
