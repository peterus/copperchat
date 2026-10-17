import { forgetPushSubscription } from '@/push/push';
import { useAuth } from './auth';
import { useChatStore } from './chat';
import { useComposer } from './composer';
import { useProjects } from './projects';

/** Forget everything that belongs to the signed-in session (L9/L11). */
export function resetSessionState(): void {
  useChatStore.getState().reset();
  useComposer.getState().clear();
  useProjects.getState().reset();
  // A signed-out device gets no notifications (they name projects and quote the agent).
  void forgetPushSubscription();
}

/**
 * Resets chat, composer and projects whenever the auth store goes anonymous — logout, an HTTP
 * 401, or the WebSocket's 4401 close — so the next user never sees the previous session's data.
 * Called once from main.tsx; returns the unsubscribe.
 */
export function installSessionReset(): () => void {
  return useAuth.subscribe((state, prev) => {
    if (state.status === 'anonymous' && prev.status !== 'anonymous') resetSessionState();
  });
}
