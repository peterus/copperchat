"""The adapter's generic `library_row` pre-stage (F4b Task 3, spec §7.9): after the turn
snapshot and inside the library lock the project's local row is added, verified and
journalled (`row_add`), then `spec.apply` runs; a failing `apply` or a footprint KiCad
does not list removes the row again. The row costs no budget. Fake KiCad of
`test_engine_unit.py` (it only records table rows; no KiCad file is written)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest

from copperchat.agent.sdk_tools import _run_two_phase
from copperchat.changes.repo import ChangeRepo
from copperchat.config import Settings
from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.db.repos.footprints import FootprintWriteRepo
from copperchat.editing.service import EditingService
from copperchat.history.library_lock import assert_library_locked
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical, NetIntent, SheetRef
from copperchat.kicad_tools.footprints.engine import LibraryRowStage
from copperchat.library import service as service_mod
from copperchat.tools import StructuredToolError, ToolContext, ToolResult, edit_spec
from tests.unit.footprints.test_engine_unit import PID, _engine_setup

SHEET = SheetRef(path="/root", page="1", file="io.kicad_sch", name="io")
NAME = "L_Bourns_SRN3012BTA"


@pytest.fixture
async def editing(tmp_path: Any) -> AsyncIterator[EditingService]:
    db = await Database.open(tmp_path / "db.sqlite")
    yield EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    await db.close()


class Rig:
    def __init__(self, settings: Settings, editing: EditingService, listed: bool) -> None:
        ctx, session, fp_cs = _engine_setup(settings.projects_root, settings, editing)
        self.ctx, self.session, self.editing = ctx, session, editing
        assert fp_cs.library is not None
        self.lib = fp_cs.library
        self.lib.dir.mkdir(parents=True)  # the library exists (another project made it)
        self.lib_id = f"{self.lib.nickname}:{NAME}"
        self.listed = listed
        self.applied = 0
        self.saved: list[str] = []

        async def save(doc: str) -> None:
            self.saved.append(doc)

        session.save = save  # type: ignore[attr-defined]
        session.read_only = False  # type: ignore[attr-defined]

    def spec(self, apply_error: BaseException | None = None) -> Any:
        rig = self

        async def prepare(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
            cs = ChangeSet(
                tool="sch_set_fields",
                sheet=SHEET,
                stages=[],
                pre_images={},
                intent=NetIntent(),
                logical=Logical(1),
            )
            cs.library_row = LibraryRowStage(
                library=rig.lib, create_library=False, footprint=rig.lib_id
            )
            return cs

        async def apply(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
            assert_library_locked(rig.lib.dir)  # apply runs inside the row's lock
            assert rig.session.kicad.rows == {rig.lib.nickname: rig.lib.uri}  # row first
            rig.applied += 1
            if apply_error is not None:
                raise apply_error
            data = {"modified": [], "side_effects": [], "budget": None, "notes": []}
            return ToolResult(data=data, changed_docs={"sch"})

        return edit_spec("sch_set_fields", "d", {"type": "object"}, prepare, apply)

    async def journal(self) -> list[tuple[str, str, str, str]]:
        rows = await FootprintWriteRepo(self.editing.db).after_id(PID, 0)
        return [(r.op, r.state, r.lib_id, r.file_rel) for r in rows]


@pytest.fixture
def make_rig(
    make_settings: Callable[..., Settings],
    editing: EditingService,
    monkeypatch: pytest.MonkeyPatch,
) -> Callable[..., Rig]:
    def make(*, listed: bool = True) -> Rig:
        rig = Rig(make_settings(), editing, listed)

        async def has_footprint(self: Any, lib_id: str) -> bool:
            return rig.listed and lib_id == rig.lib_id

        monkeypatch.setattr(service_mod.LibraryService, "has_footprint", has_footprint)
        return rig

    return make


async def test_row_costs_zero_budget(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    result = await _run_two_phase(rig.spec(), rig.ctx, {})
    assert rig.applied == 1 and rig.saved == ["sch"]
    assert result.data is not None
    assert result.data["budget"] == {"used": 1, "limit": 20}  # the edit only
    assert [e["kind"] for e in result.data["side_effects"]] == ["fp_lib_table_row_added"]
    assert result.library_changed == {rig.lib.nickname}
    assert rig.session.kicad.rows == {rig.lib.nickname: rig.lib.uri}
    rel = f"{rig.lib.table.relative_to(rig.lib.repo_root).as_posix()}"
    assert await rig.journal() == [("row_add", "done", rig.lib.nickname, rel)]
    assert rig.lib.table in rig.ctx.library_touched
    log = await ChangeRepo(rig.editing.db).for_run(PID, "run1")
    assert [(c.op, c.kind, c.ref, c.after) for c in log] == [
        ("add", "fp_lib_table_row_added", rig.lib.nickname, {"uri": rig.lib.uri})
    ]


async def test_row_compensated_when_apply_fails(make_rig: Callable[..., Rig]) -> None:
    rig = make_rig()
    boom = StructuredToolError("set fields failed", "why", "fix", code="verification_failed")
    with pytest.raises(StructuredToolError) as err:
        await _run_two_phase(rig.spec(boom), rig.ctx, {})
    assert err.value is boom  # the original error
    assert rig.applied == 1
    assert rig.session.kicad.calls == ["add_row", "delete_row"]
    assert rig.session.kicad.rows == {}
    assert [(op, state) for op, state, *_ in await rig.journal()] == [("row_add", "compensated")]
    assert rig.lib.table in rig.ctx.library_touched
    assert await ChangeRepo(rig.editing.db).for_run(PID, "run1") == []


async def test_row_mismatch_raises_not_found_and_removes_row(
    make_rig: Callable[..., Rig],
) -> None:
    rig = make_rig(listed=False)
    with pytest.raises(StructuredToolError) as err:
        await _run_two_phase(rig.spec(), rig.ctx, {})
    assert err.value.code == "not_found"
    assert rig.applied == 0  # never reached spec.apply
    assert rig.session.kicad.calls == ["add_row", "delete_row"]
    assert rig.session.kicad.rows == {}
    assert [(op, state) for op, state, *_ in await rig.journal()] == [("row_add", "compensated")]


async def test_row_there_already_adds_nothing(make_rig: Callable[..., Rig]) -> None:
    """The row appeared between prepare and apply (same URI): nothing added, nothing to
    undo, no side effect; the journal row is `compensated`."""
    rig = make_rig()
    rig.session.kicad.rows[rig.lib.nickname] = rig.lib.uri
    real_add = rig.session.kicad.add_library_table_entry

    def exists(*args: Any, **kw: Any) -> None:
        from kipy.errors import LibraryCommandError
        from kipy.proto.common.types.library_types_pb2 import LibraryCommandStatus

        real_add(*args, **kw)
        raise LibraryCommandError("exists", code=LibraryCommandStatus.Code.LCS_ALREADY_EXISTS)

    rig.session.kicad.add_library_table_entry = exists  # type: ignore[method-assign]
    result = await _run_two_phase(rig.spec(), rig.ctx, {})
    assert result.data is not None and result.data["side_effects"] == []
    assert result.library_changed == set()
    assert [(op, state) for op, state, *_ in await rig.journal()] == [("row_add", "compensated")]
