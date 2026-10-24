import { useContext, useEffect, useState } from 'react';
import { ChatStoreContext } from '@/hooks/useChat';
import type { MessageDTO } from '@/protocol/frames';
import { chatKey } from '@/stores/chat';
import { usePlans, usePlansStore } from '@/stores/plans';
import { PlanCard } from './PlanCard';

/**
 * Notices that show the plan card (spec §9.4): the proposal, each revision and the fallback to
 * the previous revision after a rejected one. All are persisted chat rows, so the layout
 * survives a reload.
 */
const CARD_ACTIONS: ReadonlySet<unknown> = new Set(['plan', 'plan-revised', 'plan-restored']);

/** The plan id of a plan-card notice row, else null. */
export function planCardOf(m: MessageDTO): string | null {
  const md = m.metadata;
  if (m.kind !== 'notice' || !CARD_ACTIONS.has(md.action)) return null;
  return typeof md.planId === 'string' && md.planId ? md.planId : null;
}

// Per message list (rows keep their identity, see chat.ts): one pass shared by every card.
const latestCache = new WeakMap<readonly MessageDTO[], Map<string, number>>();

/** planId → id of the plan's last card row in the list (the one that stays interactive). */
export function latestPlanCards(messages: readonly MessageDTO[]): Map<string, number> {
  let map = latestCache.get(messages);
  if (!map) {
    map = new Map();
    for (const m of messages) {
      const planId = planCardOf(m);
      if (planId) map.set(planId, m.id);
    }
    latestCache.set(messages, map);
  }
  return map;
}

/**
 * The note of a card row while it still describes the plan (same revision): the agent's reason
 * of a revision — unless a paused step already shows it — or the fallback after a rejection.
 */
function useNote(m: MessageDTO, planId: string): string | null {
  const plan = usePlans((s) => (s.projectId === m.projectId ? s.byId[planId] : undefined));
  const md = m.metadata;
  if (!plan || md.revision !== plan.revision) return null;
  if (md.action === 'plan-revised') {
    const shownAtStep = plan.status === 'proposed' && plan.steps.some((st) => st.status === 'paused');
    // Agent-authored: rendered as plain text by PlanCard, attributed to the agent.
    return !shownAtStep && typeof md.agentReason === 'string' && md.agentReason ? `Agent's reason: ${md.agentReason}` : null;
  }
  if (md.action === 'plan-restored' && plan.status === 'paused') {
    return `Revision rejected – back to revision ${plan.revision}, paused.`;
  }
  return null;
}

/** An earlier card of a plan: one line, expandable to a read-only view of the plan. */
function SupersededPlan({ projectId, planId }: { projectId: string; planId: string }) {
  const store = usePlansStore();
  const revision = usePlans((s) => (s.projectId === projectId ? s.byId[planId]?.revision : undefined));
  const [open, setOpen] = useState(false);

  useEffect(() => {
    void store.getState().ensure(projectId, planId);
  }, [store, projectId, planId]);

  return (
    <div className="text-center text-xs text-muted">
      <button type="button" aria-expanded={open} onClick={() => setOpen((o) => !o)} className="underline">
        {revision !== undefined ? `Superseded by revision ${revision}` : 'Superseded by a later revision'}
      </button>
      {open && (
        <div className="mt-1">
          {/* Only the plan's current state is kept, not each revision's. */}
          <PlanCard projectId={projectId} planId={planId} readOnly note={revision !== undefined ? `Current state of the plan (revision ${revision})` : null} />
        </div>
      )}
    </div>
  );
}

/**
 * A plan-card notice (spec §9.4, O7): only the plan's latest card row in the conversation is
 * interactive, so a revision awaiting approval sits at the bottom of the chat; earlier rows
 * collapse. Outside a loaded chat (no rows in the store) the card stays interactive.
 */
export function PlanNotice({ message: m, planId }: { message: MessageDTO; planId: string }) {
  const chat = useContext(ChatStoreContext);
  const latest = chat((s) => {
    const c = s.chats[chatKey(m.projectId, m.conversationId)];
    return c ? latestPlanCards(c.messages).get(planId) : undefined;
  });
  const note = useNote(m, planId);
  if (latest !== undefined && latest !== m.id) {
    return <SupersededPlan projectId={m.projectId} planId={planId} />;
  }
  return <PlanCard projectId={m.projectId} planId={planId} note={note} />;
}
