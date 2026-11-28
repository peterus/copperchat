"""Opt-in live board-session eval (SP4 P9b Task 4, spec §18.4; not CI-gating).

Runs a Board session through the real backend (`SdkStreamingBackend`, the real registry,
the built-in skills, the Board profile and prompt) against a real headless KiCad on a copy of
`sp4_synth` (schematic only): turn 1 "Lay out the board for this project.", turn 2 answers
the S0 questions with fixed values, then "ok, continue" until the agent asks "Approve this
placement?" (at most `MAX_TURNS` turns). The placement is never approved, so no routing tool
may succeed. Writes `tests/live/results/<date>-board.json` (per turn: tool calls, errors,
images, final text; tokens, cost, verdicts).

Needs model credentials, KiCad (run inside the kicad-base image) and an explicit opt-in,
because every run costs real money:
    COPPERCHAT_LIVE_EVAL=1 timeout 1800 uv run pytest -m live_sdk tests/live/test_board_eval.py
`LIVE_EVAL_MAX_USD` (default 5): checked between turns; no further turn starts once the
run's list-price cost passes it.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from pydantic import SecretStr

from copperchat.agent.events import (
    QueuedItem,
    TextDelta,
    ToolResultEvt,
    ToolUse,
    TurnFinished,
    Usage,
)
from copperchat.agent.profiles import PCB_WRITES
from copperchat.agent.sdk_backend import SdkStreamingBackend
from copperchat.config import Settings
from copperchat.projects import discover_projects
from copperchat.tools import registry
from tests.kicad_e2e.flow import Rig, open_rig, user_says
from tests.support.conversations import TEST_CID, conversation_row
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import copy_fixture

from .conftest import API_KEY, TOKEN, live

pytestmark = [
    *live,
    pytest.mark.asyncio(loop_scope="module"),
    pytest.mark.timeout(1800),
    pytest.mark.skipif(
        os.environ.get("COPPERCHAT_LIVE_EVAL") != "1",
        reason="paid live eval: set COPPERCHAT_LIVE_EVAL=1 to run it",
    ),
]

MAX_TURNS = 8
MAX_USD = float(os.environ.get("LIVE_EVAL_MAX_USD", "5"))
RESULTS = Path(__file__).parent / "results"
FIRST = "Lay out the board for this project."
# Fixed answers to the S0 questions (size, layers, assembly / sides, edges, holes, keepouts).
ANSWERS = (
    "1. 60 x 45 mm, rectangle, 1 mm corner radius. 2. 4 layers as you propose. "
    "3. JLCPCB, I reflow it myself on a hot plate with a stencil: all SMD parts on top. "
    "4. The USB-C connector on the left edge, centred. 5. Four M2 holes in the corners, "
    "3 mm inset. 6. No keepouts or height limits. Currents: below 500 mA on every rail."
)
CONTINUE = "Ok, continue."
PLACEMENT_QUESTION = "Approve this placement?"
# Tools that need the placement approval (spec §8.3); none may succeed in this run.
ROUTING_TOOLS = frozenset({"pcb_route", "pcb_route_fill", "pcb_zones", "pcb_fanout", "pcb_stitch"})
S0_KEYWORDS = {
    "size": re.compile(r"\bsize|\bmm\b|dimension", re.I),
    "layers": re.compile(r"layer", re.I),
    "assembly": re.compile(r"assembl|reflow|sides?\b", re.I),
    "edges": re.compile(r"\bedge", re.I),
    "holes": re.compile(r"mounting|holes?\b", re.I),
}


@dataclass
class Call:
    name: str
    input: dict[str, Any]
    is_error: bool = False
    result: str = ""
    images: int = 0


@dataclass
class Turn:
    message: str
    calls: list[Call] = field(default_factory=list)
    text: str = ""

    @property
    def final_text(self) -> str:
        """The text after the turn's last tool result (what the user reads at the end)."""
        return self.text.rsplit("\x00", 1)[-1].strip()


@dataclass
class BoardRun:
    turns: list[Turn]
    stop: str
    tokens: dict[str, int]
    cost: float


async def _chat(rig: Rig) -> BoardRun:
    settings = rig.settings.model_copy(
        update={
            "claude_code_oauth_token": SecretStr(TOKEN) if TOKEN else None,
            "anthropic_api_key": SecretStr(API_KEY) if API_KEY and not TOKEN else None,
        }
    )
    turns = [Turn(FIRST)]
    run_id = "eval-1"
    backend = SdkStreamingBackend(
        settings=settings,
        specs=registry.all_specs(),
        ctx_factory=lambda: rig.ctx(run_id),
        db=rig.db,
    )
    queue: asyncio.Queue[QueuedItem | None] = asyncio.Queue()
    await user_says(rig, run_id, FIRST)
    queue.put_nowait(QueuedItem(1, run_id, TEST_CID, FIRST))

    async def inputs() -> AsyncIterator[QueuedItem]:
        while (item := await queue.get()) is not None:
            yield item

    tokens = {"input": 0, "output": 0, "cache_read": 0, "cache_creation": 0}
    cost = 0.0
    stop = "completed"
    pending: dict[str, Call] = {}
    conversation = conversation_row(rig.project.id, type="board")
    async for ev in backend.start(rig.project, conversation, inputs(), None):
        turn = turns[-1]
        if isinstance(ev, TextDelta):
            turn.text += ev.text
        elif isinstance(ev, ToolUse) and ev.parent_tool_use_id is None:
            call = Call(ev.name.removeprefix("mcp__copperchat__"), dict(ev.input))
            pending[ev.id] = call
            turn.calls.append(call)
        elif isinstance(ev, ToolResultEvt) and ev.parent_tool_use_id is None:
            if ev.id in pending:
                call = pending.pop(ev.id)
                call.is_error, call.result, call.images = ev.is_error, ev.content, len(ev.images)
            turn.text += "\x00"  # text after this belongs to the next answer
        elif isinstance(ev, Usage):
            tokens["input"] += ev.input_tokens
            tokens["output"] += ev.output_tokens
            tokens["cache_read"] += ev.cache_read_tokens
            tokens["cache_creation"] += ev.cache_creation_tokens
            cost += ev.cost_usd or 0.0
        elif isinstance(ev, TurnFinished) and not ev.unsolicited:
            await rig.end_turn(run_id)
            nxt: str | None = None
            if ev.reason != "completed":
                stop = f"turn {ev.reason}: {ev.error}"
            elif PLACEMENT_QUESTION in turn.final_text:
                stop = "placement question"
            elif cost > MAX_USD:
                stop = f"cost limit US${MAX_USD:g}"
            elif len(turns) >= MAX_TURNS:
                stop = "turn limit"
            else:
                nxt = ANSWERS if len(turns) == 1 else CONTINUE
            if nxt is None:
                queue.put_nowait(None)
                continue
            run_id = f"eval-{len(turns) + 1}"
            turns.append(Turn(nxt))
            await user_says(rig, run_id, nxt)
            queue.put_nowait(QueuedItem(len(turns), run_id, TEST_CID, nxt))
    return BoardRun(turns, stop, tokens, cost)


def _write_result(run: BoardRun, verdicts: dict[str, bool]) -> Path:
    RESULTS.mkdir(exist_ok=True)
    path = RESULTS / f"{date.today().isoformat()}-board.json"
    data = {
        "stop": run.stop,
        "tokens": run.tokens,
        "cost_usd": round(run.cost, 4),
        "verdicts": verdicts,
        "turns": [
            {
                "message": t.message,
                "calls": [
                    {
                        "name": c.name,
                        "error": c.is_error,
                        "images": c.images,
                        "result": c.result[:300],
                    }
                    for c in t.calls
                ],
                "final_text": t.final_text[:4000],
            }
            for t in run.turns
        ],
    }
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False) + "\n")
    return path


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def board_run(tmp_path_factory: pytest.TempPathFactory) -> AsyncIterator[BoardRun]:
    tmp: Path = tmp_path_factory.mktemp("board-eval")
    settings = Settings(  # type: ignore[call-arg]
        _env_file=None,
        projects_root=tmp / "projects",
        data_dir=tmp / "data",
        admin_password=SecretStr("test-password"),
        cookie_secure=False,
        frontend_dist=tmp / "no-frontend",
        kicad_cli=KICAD_CLI,
        kicad_api_timeout_s=30.0,
        kicad_start_timeout_s=60.0,
        board_tools=True,
        claude_code_oauth_token=SecretStr(TOKEN) if TOKEN else None,
        anthropic_api_key=SecretStr(API_KEY) if API_KEY and not TOKEN else None,
    )
    settings.projects_root.mkdir(parents=True)
    copy_fixture("sp4_synth", settings.projects_root)
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "sp4_synth")
    async with open_rig(settings, project, tmp / "eval.db") as rig:
        await rig.start_turn("eval-1")
        run = await _chat(rig)
        yield run
    _write_result(run, _verdicts(run))


def _asks_s0(run: BoardRun) -> bool:
    first = run.turns[0]
    asked = all(p.search(first.final_text) for p in S0_KEYWORDS.values())
    wrote = any(c.name in PCB_WRITES and not c.is_error for c in first.calls)
    return asked and not wrote


def _placement_turn(run: BoardRun) -> Turn | None:
    return next((t for t in run.turns if PLACEMENT_QUESTION in t.final_text), None)


def _stops_after_placement(run: BoardRun) -> bool:
    turn = _placement_turn(run)
    return (
        turn is not None
        and turn.final_text.rstrip().endswith("?")
        and not any(c.name in ROUTING_TOOLS for c in turn.calls)
    )


def _never_routes(run: BoardRun) -> bool:
    calls = [c for t in run.turns for c in t.calls if c.name in ROUTING_TOOLS]
    return all(c.is_error and "placement_not_approved" in c.result for c in calls)


def _images_ok(run: BoardRun) -> bool:
    return all(sum(c.images for c in t.calls) <= 3 for t in run.turns)


def _verdicts(run: BoardRun) -> dict[str, bool]:
    return {
        "asks_s0_questions": _asks_s0(run),
        "stops_after_placement": _stops_after_placement(run),
        "never_routes_before_approval": _never_routes(run),
        "step_reports_at_most_3_images": _images_ok(run),
    }


async def test_asks_s0_questions(board_run: BoardRun) -> None:
    """Turn 1 asks about size, layer count, assembly / sides, edges and mounting holes and
    writes nothing to the board before the user answered."""
    assert _asks_s0(board_run), board_run.turns[0].final_text


async def test_stops_after_placement(board_run: BoardRun) -> None:
    """The turn that shows the placement ends with the approval question and routes nothing."""
    assert _stops_after_placement(board_run), board_run.stop


async def test_never_routes_before_approval(board_run: BoardRun) -> None:
    """No routing or copper tool succeeds in the whole run (the server guard refuses)."""
    assert _never_routes(board_run)


async def test_step_reports_at_most_3_images(board_run: BoardRun) -> None:
    assert _images_ok(board_run)
