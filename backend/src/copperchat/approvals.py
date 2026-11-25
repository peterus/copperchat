"""Guards shared by the approval tools (`fp_approve`, footprints spec §7.7; `pcb_approve`,
board spec §8.3): the user's quote, the messages the user typed in the current run, and
the running-plan check. Moved from `kicad_tools/footprints/approve.py` (behaviour
unchanged; that module re-exports `normalise` and `quote_in_message`)."""

from __future__ import annotations

from typing import TYPE_CHECKING

from copperchat.db.repos.messages import USER_ORIGIN, MessageRepo, MessageRow
from copperchat.plans.repo import PlanRepo

if TYPE_CHECKING:
    from copperchat.tools.spec import ToolContext

MIN_QUOTE_WORDS = 2  # footprints index author call 15
MIN_QUOTE_CHARS = 8  # non-space characters


def normalise(text: str) -> str:
    """Whitespace runs -> one space, trimmed (case and punctuation kept)."""
    return " ".join(text.split())


def _word(ch: str) -> bool:
    return ch.isalnum() or ch == "_"


def quote_in_message(quote: str, message: str) -> bool:
    """After whitespace normalisation the quote equals the whole message, or it holds at
    least 2 words and 8 non-space characters and occurs in the message starting and
    ending on word boundaries (case-sensitive). So "ok" in a longer message and "ok go"
    inside "look good" do not count, while a whole message "ok!" quoted as "ok!" does."""
    q, m = normalise(quote), normalise(message)
    if not q:
        return False
    if q == m:
        return True
    if len(q.split()) < MIN_QUOTE_WORDS or len(q.replace(" ", "")) < MIN_QUOTE_CHARS:
        return False
    start = m.find(q)
    while start >= 0:
        end = start + len(q)
        before_ok = start == 0 or not (_word(m[start - 1]) and _word(q[0]))
        after_ok = end == len(m) or not (_word(m[end]) and _word(q[-1]))
        if before_ok and after_ok:
            return True
        start = m.find(q, start + 1)
    return False


async def human_messages_after(ctx: ToolContext, since_iso: str | None) -> list[MessageRow]:
    """The messages the user typed (`metadata.origin == "user"`, stamped only by the
    WebSocket submit) that the current run processed, created after `since_iso` when
    given; empty without a run, a conversation or a database."""
    if ctx.run_id is None or ctx.editing is None or ctx.conversation_id is None:
        return []
    rows = await MessageRepo(ctx.editing.db).run_user_messages(ctx.conversation_id, ctx.run_id)
    return [
        m
        for m in rows
        if m.metadata.get("origin") == USER_ORIGIN
        and (since_iso is None or m.created_at > since_iso)
    ]


async def plan_running(ctx: ToolContext) -> bool:
    """Whether a plan of this conversation is running (False without a conversation)."""
    if ctx.editing is None or ctx.conversation_id is None:
        return False
    return await PlanRepo(ctx.editing.db).running_in_conversation(ctx.conversation_id)
