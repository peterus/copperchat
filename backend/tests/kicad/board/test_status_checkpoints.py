"""`pcb_status` checkpoint and last-check fields (SP4 P3b Task 4) on `synth_setup`."""

from __future__ import annotations

import dataclasses
import json
from typing import Any

import pytest

from copperchat.db import Database
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup
from tests.kicad.board.test_approve import nudge
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]


async def _tool(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def test_status_no_approvals(synth_setup: SynthSetup, setup_db: Database) -> None:
    out = await _tool(synth_setup.turn(setup_db, "run-s1"), "pcb_status")
    for stage in ("placement", "board"):
        assert out["checkpoints"][stage] == {
            "approved": False,
            "stale": False,
            "hash": None,
            "approved_at": None,
            "conversation_id": None,
        }
    assert out["last_check"] is None


async def test_status_without_db(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = dataclasses.replace(synth_setup.turn(setup_db, "run-s0"), editing=None)
    out = await _tool(ctx, "pcb_status")
    assert out["checkpoints"] is None and out["last_check"] is None


async def test_status_last_check(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-s2")
    check = await _tool(ctx, "pcb_check", level="quick")
    out = await _tool(ctx, "pcb_status")
    last = out["last_check"]
    assert last["level"] == "quick" and last["for_current"] is True
    assert last["hard_failures"] == check["hard_failures"]
    assert last["m1_pass"] is False and last["created_at"]
    await nudge(ctx, "R3", 0.5)
    out = await _tool(ctx, "pcb_status")
    assert out["last_check"]["for_current"] is False


async def test_status_stale_after_move(synth_setup: SynthSetup, setup_db: Database) -> None:
    from copperchat.db.repos.board import BoardCheckpointRepo, board_file
    from tests.support.conversations import TEST_CID

    ctx = synth_setup.turn(setup_db, "run-s3")
    status = await _tool(ctx, "pcb_status")
    repo = BoardCheckpointRepo(setup_db)
    rel = board_file(ctx.project)
    await repo.approve(ctx.project.id, rel, "placement", status["placement_hash"], TEST_CID,
                       "run-s3", "looks good")  # fmt: skip
    await repo.approve(ctx.project.id, rel, "board", status["board_hash"], TEST_CID,
                       "run-s3", "looks good")  # fmt: skip
    out = await _tool(ctx, "pcb_status")
    assert out["checkpoints"]["placement"]["approved"] is True
    assert out["checkpoints"]["placement"]["stale"] is False
    assert out["checkpoints"]["board"]["stale"] is False
    await nudge(ctx, "R3", -0.5)
    out = await _tool(ctx, "pcb_status")
    assert out["checkpoints"]["placement"]["stale"] is True
    assert out["checkpoints"]["board"]["stale"] is True
    assert out["checkpoints"]["placement"]["hash"] == status["placement_hash"]
