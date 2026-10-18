"""`sch_changes`: the change log of the current turn, plan step or plan (E8a Task 4)."""

from __future__ import annotations

from typing import Any

from copperchat.changes.repo import ChangeEntry
from copperchat.errors import StructuredToolError
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec

MAX_ENTRIES = 200
SCOPES = ("turn", "step", "plan")
OPEN_PLAN_STATUSES = ("proposed", "approved", "running", "paused")


def entry_dict(e: ChangeEntry) -> dict[str, Any]:
    """One change row for the agent (snake_case, like every tool result)."""
    out: dict[str, Any] = {
        "op": e.op,
        "kind": e.kind,
        "uuid": e.uuid,
        "sheet_path": e.sheet_path,
        "tool": e.tool,
        "call_seq": e.call_seq,
    }
    for key in ("ref", "net", "plan_step_id", "before", "after"):
        value = getattr(e, key)
        if value is not None:
            out[key] = value
    if e.side_effect:
        out["side_effect"] = True
    return out


async def _open_plan_steps(ctx: ToolContext) -> list[str]:
    assert ctx.editing is not None
    marks = ", ".join("?" for _ in OPEN_PLAN_STATUSES)
    rows = await ctx.editing.db.fetchall(
        "SELECT s.id FROM plan_steps s JOIN plans p ON p.id = s.plan_id"
        f" WHERE p.project_id = ? AND p.status IN ({marks}) ORDER BY s.idx",
        (ctx.project.id, *OPEN_PLAN_STATUSES),
    )
    return [str(r["id"]) for r in rows]


def _unavailable(why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError("No change log for this scope", why, suggestion, code="not_found")


async def _sch_changes(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    scope = args.get("scope", "turn")
    if scope not in SCOPES:
        raise StructuredToolError(
            "Invalid scope",
            f"scope must be one of {', '.join(SCOPES)}",
            "Use scope='turn' for this turn's changes",
            code="invalid_argument",
        )
    if ctx.editing is None or ctx.run_id is None:
        raise _unavailable("there is no agent turn", "Call it during an agent turn")
    repo = ctx.editing.changes
    if scope == "turn":
        entries = await repo.for_run(ctx.project.id, ctx.run_id)
    elif scope == "step":
        step = ctx.editing.turn(ctx.project.id, ctx.run_id).active_step_id
        if step is None:
            raise _unavailable("no plan step is active", "Use scope='turn' outside a plan step")
        # Every run of the step: a paused step resumes in a new run (spec §6).
        entries = await repo.for_steps(ctx.project.id, [step])
    else:
        steps = await _open_plan_steps(ctx)
        if not steps:
            raise _unavailable("no plan is open", "Use scope='turn' outside a plan")
        entries = await repo.for_steps(ctx.project.id, steps)
    shown = entries[-MAX_ENTRIES:]
    data: dict[str, Any] = {
        "scope": scope,
        "count": len(entries),
        "side_effects": sum(1 for e in entries if e.side_effect),
        "changes": [entry_dict(e) for e in shown],
    }
    if len(shown) < len(entries):
        data["truncated"] = True  # the newest MAX_ENTRIES rows are listed
    return ToolResult(data=data)


TOOL_SPECS = [
    ToolSpec(
        "sch_changes",
        "List what the editing tools changed in the schematic (the change log): per item "
        "op (add/modify/delete), kind, uuid, ref, net, sheet and before/after values, "
        "incl. KiCad's own cleanup (side_effect: true). scope: 'turn' (this turn, "
        "default), 'step' (the active plan step, all its runs) or 'plan' (every step of "
        "the open plan).",
        {
            "type": "object",
            "properties": {"scope": {"type": "string", "enum": list(SCOPES)}},
            "required": [],
            "additionalProperties": False,
        },
        _sch_changes,
    ),
]
