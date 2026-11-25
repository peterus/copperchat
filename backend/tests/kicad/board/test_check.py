"""`pcb_check` on the real api-server (SP4 P3b Task 2), on `sp4_synth` after sync, outline
and rules (`synth_setup`). Every call goes through the real adapter (`call_tool`)."""

from __future__ import annotations

import asyncio
import dataclasses
import json
from pathlib import Path
from typing import Any

import pytest

from copperchat.agent.broadcaster import Broadcaster
from copperchat.db import Database
from copperchat.db.repos.board import BoardReviewRepo, board_file
from copperchat.design_docs.service import DesignDocs, policy_for
from copperchat.kicad_tools.board import api, check
from copperchat.kicad_tools.board.api import DrcMarker, DrcResult
from copperchat.kicad_tools.cli import CliResult
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext
from tests.kicad.board.conftest import SynthSetup
from tests.support.edit import call_tool

pytestmark = [pytest.mark.kicad, pytest.mark.asyncio(loop_scope="module")]


async def _tool(ctx: ToolContext, name: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, name, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1))
    return out


async def _rows(db: Database, ctx: ToolContext) -> list[Any]:
    from tests.support.conversations import TEST_CID

    return await BoardReviewRepo(db).evidence(ctx.project.id, board_file(ctx.project), TEST_CID)


async def test_quick_records_row(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-q1")
    out = await _tool(ctx, "pcb_check", level="quick")
    status = await _tool(ctx, "pcb_status")
    assert out["level"] == "quick" and out["stale"] is False
    assert out["parity"] is None and "cli_crosscheck" not in out
    assert out["verdict"] in ("pass", "review", "fail")
    m14 = next(m for m in out["metrics"] if m["id"] == "M14")
    assert m14["verdict"] == "n/a"  # quick: no preset
    rows = await _rows(setup_db, ctx)
    assert len(rows) == 1
    row = rows[0]
    assert row.kind == "check" and row.check_level == "quick" and row.stage == "placement"
    assert row.m1_pass == 0  # parts are still in the staging area
    assert row.hard_failures == out["hard_failures"]
    assert row.placement_hash == out["placement_hash"] == status["placement_hash"]
    assert row.board_hash == out["board_hash"] == status["board_hash"]
    assert row.run_id == "run-q1"


async def test_full_runs_parity_and_m14(
    synth_setup: SynthSetup, setup_db: Database, record_property: Any
) -> None:
    ctx = synth_setup.turn(setup_db, "run-f1")
    quick = await _tool(ctx, "pcb_check", level="quick")
    out = await _tool(ctx, "pcb_check", level="full")
    assert out["level"] == "full" and out["stale"] is False
    assert isinstance(out["parity"], list) and out["parity_omitted"] >= 0
    assert set(out["cli_crosscheck"]) == {"only_cli", "only_api"}
    m14 = next(m for m in out["metrics"] if m["id"] == "M14")
    assert m14["verdict"] != "n/a"
    rows = await _rows(setup_db, ctx)
    assert [(r.check_level, r.stage) for r in rows] == [("quick", "placement"), ("full", "board")]
    record_property("pcb_check_quick_s", quick["seconds"])
    record_property("pcb_check_full_s", out["seconds"])
    print(f"pcb_check quick {quick['seconds']} s, full {out['seconds']} s")


async def test_full_hard_failures_recorded(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-f2")
    out = await _tool(ctx, "pcb_check", level="full")
    failing = sum(1 for m in out["metrics"] if m["verdict"] == "fail")
    assert out["hard_failures"] == failing > 0  # unplaced, unrouted
    assert out["verdict"] == "fail"
    rows = await _rows(setup_db, ctx)
    assert rows[-1].hard_failures == out["hard_failures"]


def _with_docs(ctx: ToolContext, db: Database) -> ToolContext:
    return dataclasses.replace(ctx, docs=DesignDocs(db, ctx.settings, Broadcaster()))


async def test_accepted_warning_marked(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A warning a board.md Checks row covers is `accepted` and leaves M3. sp4_synth's own
    warnings name pads and fields, whose KIIDs map to no part (P3a's M3 rule), so the real
    DRC result gets one part-level warning (U1's KIID) added."""
    from copperchat.kicad_tools.board.read import board_model

    ctx = _with_docs(synth_setup.turn(setup_db, "run-w1"), setup_db)
    u1 = (await board_model(ctx)).part("U1")
    real = api.run_drc_task

    def with_warning(h: Any, *, timeout_s: float = 60.0) -> DrcResult:
        drc = real(h, timeout_s=timeout_s)
        extra = DrcMarker("silk_overlap", "warning", "test overlap", (u1.kiid,), (u1.x, u1.y))
        return dataclasses.replace(drc, markers=(extra, *drc.markers))

    monkeypatch.setattr(api, "run_drc_task", with_warning)
    before = await _tool(ctx, "pcb_check", level="quick")
    warning = next(w for w in before["warnings"] if w["text"] == "test overlap")
    assert warning["refs"] == ["U1"] and warning["accepted"] is False
    m3_before = next(m for m in before["metrics"] if m["id"] == "M3")["value"]
    body = (
        "| DRC type | Items/refs | Reason | Accepted by |\n"
        "|---|---|---|---|\n"
        "| silk_overlap | U1 | test | user |\n"
    )
    assert ctx.docs is not None
    await ctx.docs.patch_section(
        ctx.project, "board.md", "Checks", body, None, create=True, run_id=ctx.run_id,
        policy=policy_for("board"), summary="accept the test warning",
    )  # fmt: skip
    after = await _tool(ctx, "pcb_check", level="quick")
    marked = next(w for w in after["warnings"] if w["text"] == "test overlap")
    assert marked["accepted"] is True
    others = [w for w in after["warnings"] if w["text"] != "test overlap"]
    assert not any(w["accepted"] for w in others)
    m3_after = next(m for m in after["metrics"] if m["id"] == "M3")["value"]
    assert m3_after == m3_before - 1


async def test_full_check_m3_sources(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """M3 = API errors + unaccepted API warnings + parity; the CLI's own violations go to
    cli_crosscheck only (no double count)."""
    ctx = synth_setup.turn(setup_db, "run-m3")
    api_error = DrcMarker("clearance", "error", "api clearance", ("api-kiid",), (1.0, 2.0))

    def fake_drc(h: Any, *, timeout_s: float = 60.0) -> DrcResult:
        return DrcResult((api_error,), 0, 0.01)

    report = {
        "violations": [
            {"type": "clearance", "severity": "error", "description": "cli 1",
             "items": [{"uuid": "cli-1"}]},
            {"type": "track_width", "severity": "error", "description": "cli 2",
             "items": [{"uuid": "cli-2"}]},
        ],
        "unconnected_items": [],
        "schematic_parity": [
            {"type": "missing_footprint", "severity": "warning", "description": "parity",
             "items": [{"uuid": "sym-1"}]},
        ],
    }  # fmt: skip

    async def fake_cli(settings: Any, args: list[str], *, timeout: float, cwd: Path) -> CliResult:
        out = Path(args[args.index("-o") + 1])
        out.write_text(json.dumps(report))
        return CliResult(0, "", "")

    monkeypatch.setattr(api, "run_drc_task", fake_drc)
    monkeypatch.setattr(check, "run_kicad_cli", fake_cli)
    out = await _tool(ctx, "pcb_check", level="full")
    m3 = next(m for m in out["metrics"] if m["id"] == "M3")
    assert m3["value"] == 2  # 1 API error + 1 parity marker
    assert [e["text"] for e in out["errors"]] == ["api clearance"]
    assert [p["text"] for p in out["parity"]] == ["parity"]
    assert out["cli_crosscheck"] == {"only_cli": 1, "only_api": 0}  # track_width


async def test_render_records_evidence(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-r1")
    out = await _tool(ctx, "pcb_render", view="placement")
    await _tool(ctx, "pcb_render", view="top", region={"x": 100, "y": 100, "w": 10, "h": 10})
    rows = await _rows(setup_db, ctx)
    assert [(r.kind, r.view) for r in rows] == [("render", "placement")]  # zoom: no evidence
    assert rows[0].placement_hash == out["placement_hash"]
    assert rows[0].board_hash == out["board_hash"]
    assert rows[0].check_level is None and rows[0].run_id == "run-r1"


async def test_no_conversation_no_rows(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = dataclasses.replace(synth_setup.turn(setup_db, "run-n1"), conversation_id=None)
    await _tool(ctx, "pcb_check", level="quick")
    await _tool(ctx, "pcb_render", view="placement")
    row = await setup_db.fetchone("SELECT COUNT(*) FROM board_reviews")
    assert row is not None and row[0] == 0


def _check_dirs(ctx: ToolContext) -> list[Path]:
    root = agent_dir(ctx.project) / "board" / "check"
    return list(root.iterdir()) if root.exists() else []


async def test_copy_dir_removed(synth_setup: SynthSetup, setup_db: Database) -> None:
    ctx = synth_setup.turn(setup_db, "run-c1")
    seen: list[Path] = []
    real = check.run_kicad_cli

    async def spy(settings: Any, args: list[str], *, timeout: float, cwd: Path) -> CliResult:
        seen.append(cwd)
        assert {p.suffix for p in cwd.iterdir()} >= {".kicad_pcb", ".kicad_sch", ".kicad_pro"}
        return await real(settings, args, timeout=timeout, cwd=cwd)

    check.run_kicad_cli = spy  # type: ignore[assignment]
    try:
        await _tool(ctx, "pcb_check", level="full")
    finally:
        check.run_kicad_cli = real  # type: ignore[assignment]
    assert len(seen) == 1 and not seen[0].exists()
    assert _check_dirs(ctx) == []


async def test_full_parity_uses_board_cli_lock(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    from copperchat.views.board_render import BOARD_CLI_LOCK

    ctx = synth_setup.turn(setup_db, "run-l1")
    calls: list[tuple[bool, float]] = []

    async def slow(settings: Any, args: list[str], *, timeout: float, cwd: Path) -> CliResult:
        calls.append((BOARD_CLI_LOCK.locked(), timeout))
        raise TimeoutError

    monkeypatch.setattr(check, "run_kicad_cli", slow)
    out, err = await call_tool(ctx, "pcb_check", level="full")
    assert err and out["error"]["code"] == "drc_rejected", out
    assert "120 s" in out["error"]["why"]
    assert calls == [(True, 120.0)]
    assert not BOARD_CLI_LOCK.locked()
    assert _check_dirs(ctx) == []
    rows = await _rows(setup_db, ctx)
    assert rows == []


async def test_check_stale_records_nothing(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A board write logged while the check ran → stale, no row."""
    from copperchat.kicad_tools.board import checkpoints

    ctx = synth_setup.turn(setup_db, "run-s1")
    monkeypatch.setattr(
        checkpoints, "board_written_since", lambda db, pid, since: asyncio.sleep(0, True)
    )
    out = await _tool(ctx, "pcb_check", level="quick")
    assert out["stale"] is True
    assert await _rows(setup_db, ctx) == []


async def test_check_hash_change_records_nothing(
    synth_setup: SynthSetup, setup_db: Database, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The board read after the DRC differs (a change no tool logged) → stale, no row."""
    from copperchat.kicad_tools.board.read import board_model

    ctx = synth_setup.turn(setup_db, "run-s2")
    reads: list[int] = []

    async def changing(c: ToolContext) -> Any:
        m = await board_model(c)
        reads.append(1)
        if len(reads) == 1:
            return m
        moved = dataclasses.replace(m.parts[0], x=m.parts[0].x + 1.0)
        return dataclasses.replace(m, parts=(moved, *m.parts[1:]))

    monkeypatch.setattr(check, "board_model", changing)
    out = await _tool(ctx, "pcb_check", level="quick")
    assert len(reads) == 2 and out["stale"] is True
    assert await _rows(setup_db, ctx) == []


@pytest.mark.parametrize(
    ("report", "code", "why"),
    [
        (None, 1, "wrote no report"),
        ("not json", 0, "not valid JSON"),
        ("[1, 2]", 0, "not a JSON object"),
    ],
)
async def test_full_bad_cli_output_rejected(
    synth_setup: SynthSetup,
    setup_db: Database,
    monkeypatch: pytest.MonkeyPatch,
    report: str | None,
    code: int,
    why: str,
) -> None:
    ctx = synth_setup.turn(setup_db, "run-b1")

    async def fake_cli(settings: Any, args: list[str], *, timeout: float, cwd: Path) -> CliResult:
        if report is not None:
            Path(args[args.index("-o") + 1]).write_text(report)
        return CliResult(code, "", "Error: could not load board")

    monkeypatch.setattr(check, "run_kicad_cli", fake_cli)
    out, err = await call_tool(ctx, "pcb_check", level="full")
    assert err and out["error"]["code"] == "drc_rejected", out
    assert why in out["error"]["why"]
    assert _check_dirs(ctx) == [] and await _rows(setup_db, ctx) == []


@pytest.mark.asyncio(loop_scope="function")
async def test_check_boardless(synth_session: Any) -> None:
    from tests.support.edit import edit_ctx

    out, err = await call_tool(edit_ctx(synth_session), "pcb_check", level="quick")
    assert err and out["error"]["code"] == "no_board", out
