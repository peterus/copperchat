"""Opt-in live eval of sessions and design docs (spec §14.4, C6 Task 2; not CI-gating).

The real app (SDK backend, built-in skills, real headless KiCad, blank project):
1. a **Requirements** session gets the brief and writes requirements.md, parts.md and
   decisions.md (it may research the key parts); it must make no ``sch_*`` call;
2. the user closes it (handover) and approves requirements.md via REST;
3. a **Schematic** session gets "Draw the schematic from the design docs." A proposed plan
   is approved and a paused one resumed via REST, as the user would. Its netlist is scored
   with ``golden.check("sessions_attiny85_usbc", …)``, plus ERC;
4. the **baseline**: the same task in one Free session of a fresh project.

Tokens and cost per session go to ``tests/live/results/<date>.json`` (``sessions_eval``).
Needs credentials, KiCad 11 (run it in the kicad-base image) and the explicit opt-in:
    COPPERCHAT_LIVE_EVAL=1 uv run pytest -m live_sdk tests/live/test_sessions_live.py
"""

from __future__ import annotations

import os
import re
import shutil
import time
from pathlib import Path
from typing import Any

import pytest

from copperchat.tools import ToolContext
from tests.kicad.edit import golden
from tests.support.edit import call_tool
from tests.support.kicad_env import KICAD_CLI
from tests.support.kicad_project import FIXTURES

from .conftest import live
from .sessions_app import LiveApp, app_settings, live_app
from .test_editing_eval import write_result

pytestmark = [
    *live,
    pytest.mark.kicad,
    pytest.mark.skipif(
        os.environ.get("COPPERCHAT_LIVE_EVAL") != "1",
        reason="paid live eval: set COPPERCHAT_LIVE_EVAL=1 to run it",
    ),
]

BRIEF = "a USB-C (5 V) powered board with an ATtiny85 blinking one LED, ISP header"
NO_QUESTIONS = (
    "I have no further constraints: decide everything else yourself with sensible defaults "
    "and record them as assumptions."
)
REQ_START = (
    f"New board: {BRIEF}. Work out the requirements. {NO_QUESTIONS} Research the key parts "
    "and write requirements.md, parts.md and decisions.md now, without asking me first."
)
REQ_GO_ON = (
    "Go on without my answers: use sensible defaults and finish requirements.md, parts.md "
    "and decisions.md now."
)
SCH_START = "Draw the schematic from the design docs."
SCH_GO_ON = "Yes, go ahead and finish the schematic. Decide open details yourself."
BASE_START = f"Draw the schematic of {BRIEF}. {NO_QUESTIONS} Do not ask me, just build it."
REQ_TURNS, SCH_TURNS = 3, 6
SCENARIO = "sessions_attiny85_usbc"
REQ_SECTIONS = ("## Function", "## Interfaces", "## Power", "## Open points")


def _settings(tmp_path: Path) -> Any:
    settings = app_settings(
        tmp_path, kicad_cli=KICAD_CLI, kicad_api_timeout_s=30.0, kicad_start_timeout_s=60.0
    )
    shutil.copytree(FIXTURES / "blank", settings.projects_root / "blank")
    return settings


def _doc(app: LiveApp, name: str) -> str:
    path = app.design / name
    return path.read_text() if path.exists() else ""


def _requirements_done(app: LiveApp) -> bool:
    req, parts = _doc(app, "requirements.md"), _doc(app, "parts.md")
    return all(s in req for s in REQ_SECTIONS) and "attiny85" in parts.lower()


async def _score(app: LiveApp) -> dict[str, Any]:
    session = app.app.state.sessions.current()
    if session is None:
        return {"golden": False, "missing": ["no KiCad session"], "erc_errors": None}
    comps = await session.call(golden.live_components)
    score = golden.check(SCENARIO, golden.nets_of(comps), comps)
    ctx = ToolContext(project=session.project, session=session, settings=session.settings)
    erc, err = await call_tool(ctx, "run_erc")
    violations = [] if err else erc["violations"]
    return {
        "golden": score["golden"],
        "missing": score["missing"],
        "erc_failed": erc if err else None,
        "erc_errors": None if err else sum(1 for v in violations if v["severity"] == "error"),
        "erc_warnings": sum(1 for v in violations if v["severity"] == "warning"),
        "erc_types": sorted({f"{v['severity']}:{v['type']}" for v in violations}),
        "components": {
            r: {"lib": c["lib"], "value": c["value"], "footprint": c["footprint"]}
            for r, c in sorted(comps.items())
        },
    }


async def _build(app: LiveApp, cid: str, first: str) -> dict[str, Any]:
    """Send ``first``, then act like the user until the schematic is golden or the turn
    limit: approve a proposed plan, resume a paused one, otherwise say go on."""
    started = time.monotonic()
    plans: list[dict[str, Any]] = []
    await app.send(cid, first)
    turns = 1
    while turns < SCH_TURNS:
        plan = await app.open_plan()
        if plan is not None and plan["status"] in ("proposed", "paused"):
            plans.append({"status": plan["status"], "steps": len(plan.get("steps", []))})
            event = "approve" if plan["status"] == "proposed" else "resume"
            r = await app.client.post(app.url(f"/plans/{plan['id']}/{event}"))
            assert r.status_code == 200, r.text
            await app.idle()
        elif plan is not None or not (await _score(app))["golden"]:
            # an approved or running plan that stopped, or no plan and not done yet
            await app.send(cid, SCH_GO_ON)
        else:
            break
        turns += 1
    turn = await app.turn(cid)
    return {
        "turns": turns,
        "plans": plans,
        "wall_s": round(time.monotonic() - started),
        "tools": turn.main_tools(),
        "tool_errors": turn.errors()[:20],
        "usage": await app.usage(cid),
        **await _score(app),
    }


@pytest.mark.timeout(5400)
async def test_sessions_eval(tmp_path: Path) -> None:
    result: dict[str, Any] = {"model": "cli default", "brief": BRIEF}
    async with live_app(_settings(tmp_path / "split")) as app:
        await app.open_project()
        # 1. requirements session
        started = time.monotonic()
        s1 = await app.create("requirements")
        await app.send(s1, REQ_START)
        turns = 1
        while turns < REQ_TURNS and not _requirements_done(app):
            await app.send(s1, REQ_GO_ON)
            turns += 1
        # 2. close (handover), then approve as the user
        r = await app.client.post(app.url(f"/conversations/{s1}/close"))
        assert r.status_code == 202, r.text
        await app.idle()
        req_turn = await app.turn(s1)
        approved = await app.approve("requirements.md")
        req, parts, decisions = (
            _doc(app, n) for n in ("requirements.md", "parts.md", "decisions.md")
        )
        sch_calls = req_turn.succeeded("sch_")
        result["requirements"] = {
            "turns": turns + 1,  # + the close turn
            "wall_s": round(time.monotonic() - started),
            "sections": {s: s in req for s in REQ_SECTIONS},
            "parts_attiny85": "attiny85" in parts.lower(),
            "parts_usb_c": bool(re.search(r"usb[- _]?c", parts, re.I)),
            "researched": req_turn.main_tools().count("research_part"),
            "decisions": decisions.count("\n## "),
            "sch_calls": sch_calls,
            "approved_version": approved.get("version"),
            "tools": req_turn.main_tools(),
            "tool_errors": req_turn.errors()[:20],
            "usage": await app.usage(s1),
        }
        # 3. schematic session from the docs
        s2 = await app.create("schematic")
        result["schematic"] = await _build(app, s2, SCH_START)

    # 4. baseline: the same task in one Free session
    async with live_app(_settings(tmp_path / "single")) as app:
        await app.open_project()
        s0 = await app.create("free")
        result["baseline"] = await _build(app, s0, BASE_START)

    split = [result["requirements"]["usage"], result["schematic"]["usage"]]
    result["totals"] = {
        "split_cost_usd": round(sum(u.get("costUsd", 0) for u in split), 4),
        "split_output_tokens": sum(u.get("outputTokens", 0) for u in split),
        "baseline_cost_usd": round(result["baseline"]["usage"].get("costUsd", 0), 4),
        "baseline_output_tokens": result["baseline"]["usage"].get("outputTokens", 0),
    }
    path = write_result("sessions_eval", result)
    print(f"sessions eval -> {path}: {result['totals']}")

    req_result, sch = result["requirements"], result["schematic"]
    assert all(req_result["sections"].values()), req_result["sections"]
    assert req_result["parts_attiny85"] and req_result["parts_usb_c"]
    assert req_result["researched"] >= 1 and req_result["decisions"] >= 1
    assert sch_calls == [], "a Requirements session changed the schematic"
    assert sch["golden"], sch["missing"]
    assert sch["erc_failed"] is None, sch["erc_failed"]
    assert sch["erc_errors"] == 0, sch["erc_types"]
