"""`fp_approve` on a real api-server (F4b Task 1, spec §7.7, §16.1, §16.2) in the
run-engine rig of `test_turn_end.py` (history hooks, EditingService, PlanService +
PlanGate): turn 1 creates, renders and checks L201; turn 2 is started by a message the
user typed (stored with `metadata.origin = "user"`, as the WebSocket submit stores it)
and approves it. The footprint file is only ever written by KiCad."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
from datetime import UTC, datetime
from typing import Any

import pytest

from copperchat.db.repos.footprints import FootprintReviewRepo
from copperchat.db.repos.messages import MessageRepo
from copperchat.kicad_tools.footprints.read import fp_get
from copperchat.tools import ToolContext
from tests.kicad.footprints.test_fp_create import LIB_ID, NAME, journal, l201_args, lib_dir
from tests.kicad.footprints.test_turn_end import RUN1, RUN2, Rig, _change_log, _step
from tests.kicad.footprints.test_turn_end import rig as rig
from tests.support.conversations import TEST_CID
from tests.support.edit import call_tool

pytestmark = pytest.mark.kicad

RUN3 = "run-fp-3"
OK_TEXT = "Thanks, the render and the check look good - approve L201 please."
OK_QUOTE = "look good - approve L201"


def ctx(rig: Rig, run_id: str) -> ToolContext:
    """The run engine's context of `run_id` in the test conversation."""
    return dataclasses.replace(rig.ctx(run_id), conversation_id=TEST_CID)


async def tool(rig: Rig, run_id: str, name: str, /, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx(rig, run_id), name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def refused(rig: Rig, run_id: str, name: str, /, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx(rig, run_id), name, **args)
    assert err, out
    error: dict[str, Any] = out["error"]
    return error


async def user_says(rig: Rig, run_id: str, text: str = OK_TEXT) -> None:
    """The message that starts turn `run_id`, typed by the user (later than every render
    and check before it: the clock is read with millisecond resolution)."""
    await asyncio.sleep(0.01)
    await MessageRepo(rig.db).insert(
        rig.session.project.id,
        TEST_CID,
        role="user",
        kind="text",
        content=text,
        run_id=run_id,
        metadata={"origin": "user"},
    )
    await rig.hooks.on_turn_start(rig.turn(run_id))


async def created_and_shown(rig: Rig, *, check: bool = True) -> str:
    """Turn 1: fp_create, fp_render (and fp_check); returns the rendered geometry hash."""
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    created = await tool(rig, RUN1, "fp_create", **l201_args())
    assert created["created"] == [LIB_ID]
    render = await tool(rig, RUN1, "fp_render", lib_id=LIB_ID)
    digest: str = render["geometry_hash"]
    if check:
        checked = await tool(rig, RUN1, "fp_check", lib_id=LIB_ID)
        assert checked["passed"] and checked["geometry_hash"] == digest
    await rig.end_turn(RUN1)
    return digest


def sha(path: Any) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def test_happy_path_effect(rig: Rig) -> None:
    """Fields, journal, change log, budget 1; the geometry hash is unchanged across the
    approval (spec §16.1) and the next turn start reports no external change."""
    digest = await created_and_shown(rig)
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    before = sha(file)
    await user_says(rig, RUN2)
    out = await tool(
        rig, RUN2, "fp_approve", lib_id=LIB_ID, geometry_hash=digest, user_quote=OK_QUOTE
    )
    today = datetime.now(UTC).date().isoformat()
    value = f"approved {today}, conversation {TEST_CID[:8]}, geometry {digest[:8]}"
    assert out["approved"] == LIB_ID and out["review"] == "approved"
    assert out["geometry_hash"] == digest and out["user_review"] == value
    assert out["logical_changes"] == 1 and out["budget"] == {"used": 1, "limit": 20}

    got = await fp_get(ctx(rig, RUN2), {"lib_id": LIB_ID})
    assert got.data is not None
    assert got.data["review"] == "approved" and got.data["geometry_hash"] == digest
    assert got.data["fields"]["needs_user_review"] == "no"
    assert got.data["fields"]["User review"] == value
    assert OK_QUOTE not in file.read_text()  # the quote never goes into the KiCad file
    checked = await tool(rig, RUN2, "fp_check", lib_id=LIB_ID)
    assert checked["passed"] and checked["counts"]["error"] == 0
    assert not [f for f in checked["findings"] if f["rule"] == "approved_geometry_changed"]

    rows = await journal(rig.db, rig.session)
    assert [(r.op, r.state) for r in rows] == [
        ("row_add", "done"),
        ("create", "done"),
        ("approve", "done"),
    ]
    approve = rows[-1]
    assert approve.lib_id == LIB_ID and approve.geometry_hash == digest
    assert approve.pre_sha256 == before and approve.post_sha256 == sha(file) != before

    log = await _change_log(rig, RUN2)
    assert [(c["op"], c["kind"], c["ref"]) for c in log] == [
        ("modify", "footprint_approved", LIB_ID)
    ]
    assert log[0]["after"]["user_quote"] == OK_QUOTE
    assert log[0]["after"]["geometry_hash"] == digest

    await rig.end_turn(RUN2)
    await rig.hooks.on_turn_start(rig.turn(RUN3))
    assert rig.reported() == [], rig.reported()


async def test_approve_without_fp_check(rig: Rig) -> None:
    """fp_create's read-back check counts as the check (guard 3): create + render + a
    human message + approve, no fp_check call."""
    digest = await created_and_shown(rig, check=False)
    review = await FootprintReviewRepo(rig.db).get(TEST_CID, LIB_ID, digest)
    assert review is not None and review.checked_at is not None
    assert review.rendered_at is not None and review.check_errors == 0
    await user_says(rig, RUN2)
    out = await tool(
        rig, RUN2, "fp_approve", lib_id=LIB_ID, geometry_hash=digest, user_quote=OK_QUOTE
    )
    assert out["review"] == "approved"


async def test_same_turn_refused(rig: Rig) -> None:
    """Approving in the turn that rendered it: the turn's message predates the render."""
    await MessageRepo(rig.db).insert(
        rig.session.project.id,
        TEST_CID,
        role="user",
        kind="text",
        content=OK_TEXT,
        run_id=RUN1,
        metadata={"origin": "user"},
    )
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    await tool(rig, RUN1, "fp_create", **l201_args())
    digest = (await tool(rig, RUN1, "fp_render", lib_id=LIB_ID))["geometry_hash"]
    await tool(rig, RUN1, "fp_check", lib_id=LIB_ID)
    error = await refused(
        rig, RUN1, "fp_approve", lib_id=LIB_ID, geometry_hash=digest, user_quote=OK_QUOTE
    )
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 4


async def test_plan_running(rig: Rig) -> None:
    """Guard 6: a running plan of this session refuses the approval (the gates let it
    through, author call 13)."""
    digest = await created_and_shown(rig)
    await user_says(rig, RUN2)
    out = await tool(rig, RUN2, "plan_propose", title="L202", summary="fp", steps=[_step(5)])
    await rig.plans.approve(rig.session.project, out["plan_id"])
    await tool(rig, RUN2, "plan_step_begin", step=1)
    error = await refused(
        rig, RUN2, "fp_approve", lib_id=LIB_ID, geometry_hash=digest, user_quote=OK_QUOTE
    )
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 6


async def test_not_local(rig: Rig) -> None:
    """Guard 1: a stock footprint, rendered and checked, cannot be approved."""
    await rig.hooks.on_turn_start(rig.turn(RUN1))
    stock = "Inductor_SMD:L_Bourns-SRN4018"
    digest = (await tool(rig, RUN1, "fp_render", lib_id=stock))["geometry_hash"]
    await tool(rig, RUN1, "fp_check", lib_id=stock)
    await rig.end_turn(RUN1)
    await user_says(rig, RUN2)
    error = await refused(
        rig, RUN2, "fp_approve", lib_id=stock, geometry_hash=digest, user_quote=OK_QUOTE
    )
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 1


async def test_already_approved(rig: Rig) -> None:
    """Guard 1: a second approval of the same geometry is refused; nothing is written."""
    digest = await created_and_shown(rig)
    await user_says(rig, RUN2)
    args = {"lib_id": LIB_ID, "geometry_hash": digest, "user_quote": OK_QUOTE}
    await tool(rig, RUN2, "fp_approve", **args)
    await rig.end_turn(RUN2)
    file = lib_dir(rig.root) / f"{NAME}.kicad_mod"
    before, rows = sha(file), await journal(rig.db, rig.session)
    await user_says(rig, RUN3)
    error = await refused(rig, RUN3, "fp_approve", **args)
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 1
    assert sha(file) == before and await journal(rig.db, rig.session) == rows


async def test_refused_after_replace(rig: Rig) -> None:
    """Guard 2: `replace` changed the geometry after the user saw it."""
    digest = await created_and_shown(rig)
    await user_says(rig, RUN2)
    pads = [
        {"number": "1", "type": "smd", "shape": "rect", "at": [-1.15, 0], "size": [1.3, 3.5]},
        {"number": "2", "type": "smd", "shape": "rect", "at": [1.15, 0], "size": [1.3, 3.5]},
    ]
    replaced = await tool(rig, RUN2, "fp_create", **l201_args(replace=True, pads=pads))
    assert replaced["created"] == [LIB_ID]
    error = await refused(
        rig, RUN2, "fp_approve", lib_id=LIB_ID, geometry_hash=digest, user_quote=OK_QUOTE
    )
    assert error["code"] == "approval_not_allowed" and error["details"]["guard"] == 2
    got = await fp_get(ctx(rig, RUN2), {"lib_id": LIB_ID})
    assert got.data is not None and got.data["review"] == "needs_user_review"
