"""SP4 P1a Task 5 on the real api-server: undo of a board the agent created.

Snapshot a schematic-only project, let KiCad create a board (CreateDocument +
SaveDocument) and write a custom rule (SetCustomDesignRules, KiCad writes the
`.kicad_dru`), save, restore: both files are gone and the reopened session is a
schematic-only one again. Every KiCad file here is written by KiCad itself.

The board is created through the raw API in a local helper (not through
`create_board_document`) so this test does not depend on the board API module.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path

import pytest

from copperchat.config import Settings
from copperchat.db import Database
from copperchat.history.restore import restore_snapshot
from copperchat.history.service import HistoryService
from copperchat.kicad_session import KicadHandle, KicadSessionManager
from copperchat.kicad_tools import project as project_tools
from copperchat.projects import Project, discover_projects, get_project
from copperchat.tools.spec import ToolContext
from tests.support.conversations import TEST_CID, ensure_test_conversation
from tests.support.history import FakeEngine, FakeRuntime, RecordingBroadcaster
from tests.support.kicad_project import copy_fixture, real_settings

pytestmark = pytest.mark.kicad
MM = 1_000_000


@dataclass
class Rig:
    svc: HistoryService
    sessions: KicadSessionManager
    engine: FakeEngine
    settings: Settings
    project: Project


@pytest.fixture
async def rig(make_settings: Callable[..., Settings], tmp_path: Path) -> AsyncIterator[Rig]:
    """The blank fixture without its board (like `blank_nopcb_session`), opened through
    a manager the HistoryService can reopen with."""
    settings = real_settings(make_settings)
    dest = copy_fixture("blank", settings.projects_root)
    for pcb in dest.glob("*.kicad_pcb"):
        pcb.unlink()
    project = next(p for p in discover_projects(settings.projects_root) if p.name == "blank")
    assert project.pcb_file is None
    sessions = KicadSessionManager(settings)
    db = await Database.open(tmp_path / "db.sqlite")
    await ensure_test_conversation(db)
    engine = FakeEngine()
    svc = HistoryService(
        db=db, sessions=sessions, broadcaster=RecordingBroadcaster(), runtime=FakeRuntime(engine)
    )
    svc.projects_root = settings.projects_root
    try:
        await sessions.open(project)
        yield Rig(svc, sessions, engine, settings, project)
    finally:
        await sessions.close_all()
        await db.close()


def _create_board(h: KicadHandle, path: Path) -> None:
    """KiCad creates and writes an empty board (spike 4a `boards.py`)."""
    from kipy.proto.common.types import DocumentType

    h.kicad.create_document(str(path), DocumentType.DOCTYPE_PCB)
    h.kicad.get_board().save()


def _set_one_custom_rule(h: KicadHandle) -> None:
    from kipy.proto.board import board_commands_pb2, board_rules_pb2

    board = h.board()
    rule = board_rules_pb2.CustomRule(name="cc_task5_width", condition="A.NetClass == 'Default'")
    con = rule.constraints.add()
    con.type = board_rules_pb2.CustomRuleConstraintType.CRCT_TRACK_WIDTH
    con.numeric.min = int(0.3 * MM)
    resp = board.client.send(
        board_commands_pb2.SetCustomDesignRules(board=board.document, rules=[rule]),
        board_commands_pb2.CustomRulesResponse,
    )
    status = board_commands_pb2.CustomRulesStatus.Name(resp.status)
    assert status == "CRS_VALID", (status, resp.error_text)


async def test_restore_removes_created_board_and_rules(rig: Rig) -> None:
    project = rig.project
    pcb = project.pro_file.with_suffix(".kicad_pcb")
    dru = project.pro_file.with_suffix(".kicad_dru")
    snap = await rig.svc.store(project).snapshot("run-1", "turn")

    session = rig.sessions.current()
    assert session is not None
    await session.call(lambda h: _create_board(h, pcb))
    assert pcb.is_file()
    with_board = get_project(rig.settings.projects_root, project.id)
    assert with_board.pcb_file == pcb
    session = await rig.sessions.reopen(with_board)  # the caller reopens (B-C contract)
    await session.call(_set_one_custom_rule)
    await session.save("pcb")
    assert dru.is_file() and "cc_task5_width" in dru.read_text()

    await restore_snapshot(rig.svc, with_board, snap.id, conversation_id=TEST_CID)

    assert not pcb.exists() and not dru.exists()
    fresh = rig.sessions.current()
    assert fresh is not None and not fresh.closed
    assert fresh.project.pcb_file is None
    assert rig.svc.docs_of(fresh.project) == {"sch"}
    assert rig.engine.refreshed and rig.engine.refreshed[-1].pcb_file is None
    info = await project_tools.project_info(
        ToolContext(project=fresh.project, session=fresh, settings=fresh.settings), {}
    )
    assert info.data is not None
    assert info.data["files"]["board"] is None
    assert info.data["files"]["schematic"] == "blank.kicad_sch"
