"""Opt-in live editing eval (E10 Task 4, spec §12; not CI-gating).

Runs each prompt of `editing_scenarios.py` through the real backend (`SdkStreamingBackend`,
the real registry of tools, the built-in skills) against a real headless KiCad with the
blank project, as one chat: a proposed plan is approved and a paused plan resumed as the
user would (at most `MAX_TURNS` turns). Scores golden connectivity by role, ERC errors,
layout, tool calls, tokens and cost, and writes `tests/live/results/<date>.json`.

Needs model credentials **and** KiCad (run inside the kicad-base image):
    uv run pytest -m live_sdk tests/live/test_editing_eval.py [-k s2_ldo]
`LIVE_EVAL_MAX_USD` (default 3) aborts a scenario whose list-price cost passes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from collections.abc import AsyncIterator
from datetime import date
from pathlib import Path
from typing import Any

import pytest
from pydantic import SecretStr

from copperchat.agent.engine import TurnOutcome
from copperchat.agent.events import QueuedItem, ToolResultEvt, ToolUse, TurnFinished, Usage
from copperchat.agent.sdk_backend import SdkStreamingBackend
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.kicad_session import KicadSession
from copperchat.tools import ToolContext, registry
from tests.kicad.edit import golden
from tests.kicad.edit.test_plan_flow import Rig, rig  # noqa: F401  (fixture)
from tests.kicad.edit.test_scenarios import layout_problems, run_script
from tests.support.edit import call_tool

from .conftest import API_KEY, TOKEN, live
from .editing_scenarios import PROMPTS, SCENARIOS, SETUP, check_resize

pytestmark = live

MAX_TURNS = 6
MAX_USD = float(os.environ.get("LIVE_EVAL_MAX_USD", "3"))
RESULTS = Path(__file__).parent / "results"


def current(rig: Rig) -> KicadSession:  # noqa: F811
    session = rig.history.sessions.current()
    assert session is not None
    return session


class Chat:
    """One chat on the rig's project: per-turn run ids, a turn snapshot per run (as the
    run engine takes), event counters."""

    def __init__(self, rig: Rig) -> None:  # noqa: F811
        self.rig = rig
        self.turn = 0
        self.snapshotted: set[str] = set()
        self.tool_calls: list[str] = []
        self.tool_errors: list[str] = []
        self.tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
        self.cost = 0.0
        self.messages: list[str] = []
        self.plans: list[dict[str, Any]] = []  # what the user did with each plan

    @property
    def run_id(self) -> str:
        return f"eval-{self.turn}"

    def ctx(self, run_id: str | None = None) -> ToolContext:
        run_id = run_id or self.run_id
        session = current(self.rig)

        async def on_mutation() -> None:
            if run_id not in self.snapshotted:
                self.snapshotted.add(run_id)
                await self.rig.history.store(session.project).snapshot(run_id, "turn")

        return ToolContext(
            project=session.project,
            session=session,
            settings=session.settings,
            run_id=run_id,
            on_mutation=on_mutation,
            editing=self.rig.editing,
        )

    async def next_message(self) -> str | None:
        """What the user does after a turn: approve a proposed plan, resume a paused one."""
        plans, project = self.rig.plans, current(self.rig).project
        plan = await plans.open_plan(project)
        if plan is not None and plan.status == "running":  # the engine's turn-end rule
            await plans.on_turn_end(
                project, self.run_id, TurnOutcome(reason="completed"), self.rig.runtime.engine
            )
            plan = await plans.open_plan(project)
        if plan is None:
            return None
        sent = len(self.rig.runtime.submitted)
        self.plans.append(
            {"id": plan.id, "status": plan.status, "steps": len(plan.steps),
             "reason": plan.pause_reason}
        )  # fmt: skip
        if plan.status == "proposed":
            await plans.approve(project, plan.id)
        elif plan.status == "paused":
            await plans.resume(project, plan.id)
        else:
            return None
        return (
            self.rig.runtime.submitted[sent][1] if len(self.rig.runtime.submitted) > sent else None
        )

    async def run(self, prompt: str) -> str:
        session = current(self.rig)
        settings = session.settings.model_copy(
            update={
                "claude_code_oauth_token": SecretStr(TOKEN) if TOKEN else None,
                "anthropic_api_key": SecretStr(API_KEY) if API_KEY and not TOKEN else None,
            }
        )
        backend = SdkStreamingBackend(
            settings=settings, specs=registry.all_specs(), ctx_factory=self.ctx,
            db=self.rig.editing.db,
        )  # fmt: skip
        queue: asyncio.Queue[QueuedItem | None] = asyncio.Queue()
        queue.put_nowait(QueuedItem(1, "eval-1", prompt))
        self.messages.append(prompt)

        async def inputs() -> AsyncIterator[QueuedItem]:
            while (item := await queue.get()) is not None:
                yield item

        stop = "completed"
        async for ev in backend.start(session.project, inputs(), None):
            if isinstance(ev, ToolUse) and ev.parent_tool_use_id is None:
                self.tool_calls.append(ev.name.removeprefix("mcp__copperchat__"))
            elif isinstance(ev, ToolResultEvt) and ev.is_error and ev.parent_tool_use_id is None:
                self.tool_errors.append(ev.content[:300])
            elif isinstance(ev, Usage):
                self.tokens["input"] += ev.input_tokens
                self.tokens["output"] += ev.output_tokens
                self.tokens["cache_read"] += ev.cache_read_tokens
                self.tokens["cache_creation"] += ev.cache_creation_tokens
                self.cost += ev.cost_usd or 0.0
            elif isinstance(ev, TurnFinished) and not ev.unsolicited:
                nxt = None
                if ev.reason != "completed":
                    stop = f"turn {ev.reason}: {ev.error}"
                elif self.cost > MAX_USD:
                    stop = f"cost limit US${MAX_USD:g}"
                elif self.turn + 1 >= MAX_TURNS:
                    stop = "turn limit"
                else:
                    nxt = await self.next_message()
                if nxt is None:
                    queue.put_nowait(None)
                    continue
                self.turn += 1
                self.messages.append(nxt)
                queue.put_nowait(QueuedItem(self.turn + 1, f"eval-{self.turn + 1}", nxt))
        return stop


def write_result(name: str, result: dict[str, Any]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{date.today().isoformat()}.json"
    data = json.loads(path.read_text()) if path.exists() else {}
    data[name] = result
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return path


@pytest.mark.timeout(1500)
@pytest.mark.parametrize("name", SCENARIOS)
async def test_editing_eval(rig: Rig, name: str) -> None:  # noqa: F811
    project = current(rig).project
    chat = Chat(rig)
    before: golden.Components = {}
    if name in SETUP:
        await EditingSettingsRepo(rig.editing.db).put(project.id, change_budget=500)
        await run_script(chat.ctx("setup"), SETUP[name])
        await current(rig).save("sch")
        await EditingSettingsRepo(rig.editing.db).put(project.id, change_budget=20)
        before = await current(rig).call(golden.live_components)

    started = time.monotonic()
    stop = await chat.run(PROMPTS[name])
    wall = time.monotonic() - started

    session = current(rig)
    live_comps = await session.call(golden.live_components)
    if name in SETUP:
        score = check_resize(before, live_comps)
    else:
        score = golden.check(name, golden.nets_of(live_comps), live_comps)
    erc, err = await call_tool(chat.ctx(), "run_erc")
    violations = [] if err else erc["violations"]
    result = {
        "model": "cli default",
        "stop": stop,
        "turns": chat.turn + 1,
        "golden": score["golden"],
        "missing": score["missing"],
        "erc_errors": sum(1 for v in violations if v["severity"] == "error"),
        "erc_warnings": sum(1 for v in violations if v["severity"] == "warning"),
        "erc_types": sorted({f"{v['severity']}:{v['type']}" for v in violations}),
        "layout": await session.call(layout_problems),
        "tool_calls": len(chat.tool_calls),
        "tool_errors": len(chat.tool_errors),
        "tools": chat.tool_calls,
        "errors": chat.tool_errors[:20],
        "skills": [t for t in chat.tool_calls if t == "Skill"],
        "tokens": chat.tokens,
        "cost_usd": round(chat.cost, 4),
        "wall_s": round(wall),
        "components": {
            r: {"lib": c["lib"], "value": c["value"], "footprint": c["footprint"]}
            for r, c in sorted(live_comps.items())
        },
        "plans": chat.plans,
        "messages": chat.messages[1:],
    }
    path = write_result(name, result)
    print(
        f"{name}: golden={score['golden']} erc={result['erc_errors']} "
        f"cost={chat.cost:.2f} -> {path}"
    )
    # Not CI-gating: the numbers are the result. Only a broken harness fails the test.
    assert stop in ("completed", "turn limit") or stop.startswith("cost limit"), stop
