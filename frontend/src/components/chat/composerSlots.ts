import { type ComponentType, useSyncExternalStore } from 'react';
import type { AttachmentRef } from '@/protocol/frames';

export interface ComposerAttachmentSlotProps {
  disabled: boolean;
  projectId: string;
  attachments: AttachmentRef[];
  setAttachments(next: AttachmentRef[]): void;
}

export type ComposerAttachmentSlot = ComponentType<ComposerAttachmentSlotProps> | null;

let slot: ComposerAttachmentSlot = null;
const listeners = new Set<() => void>();

/** Extension point: Phase 8 registers the upload button here (null unregisters). */
export const composerAttachmentSlot = {
  register(component: ComposerAttachmentSlot): void {
    slot = component;
    for (const l of [...listeners]) l();
  },
};

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

const getSlot = () => slot;

export function useComposerAttachmentSlot(): ComposerAttachmentSlot {
  return useSyncExternalStore(subscribe, getSlot, getSlot);
}
