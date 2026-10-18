"""`sch_changes` scopes (E8a Task 4); the real-KiCad turn listing is in
tests/kicad/edit/test_checks_real.py."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest

from copperchat.changes import specs
from copperchat.changes.repo import ChangeEntry
from copperchat.db import Database, utcnow_iso
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.errors import StructuredToolError


class P:
    id = "p1"


class Ctx:
    def __init__(self, editing: EditingService | None, run_id: str | None = "run-1") -> None:
        self.project = P()
        self.editing = editing
        self.run_id = run_id


@pytest.fixture
async def editing(tmp_path: Path) -> AsyncIterator[EditingService]:
    db = await Database.open(tmp_path / "db.sqlite")
    yield EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    await db.close()


def entry(run_id: str, uuid: str, step: str | None = None, **kw: Any) -> ChangeEntry:
    return ChangeEntry("p1", run_id, step, 1, "sch_add_wire", "add", "wire", uuid, "/r", **kw)


async def call(ctx: Ctx, **args: Any) -> dict[str, Any]:
    res = await specs._sch_changes(ctx, args)  # type: ignore[arg-type]
    assert res.data is not None
    return res.data


async def _open_plan(db: Database, steps: list[str], status: str = "running") -> None:
    now = utcnow_iso()
    await db.execute(
        "INSERT INTO plans(id, project_id, title, summary, status, revision, created_run_id,"
        " created_at, updated_at) VALUES ('pl', 'p1', 't', 's', ?, 1, 'run-1', ?, ?)",
        (status, now, now),
    )
    for i, sid in enumerate(steps):
        await db.execute(
            "INSERT INTO plan_steps(id, plan_id, idx, title, intent, expected_changes, touches,"
            " verification, status) VALUES (?, 'pl', ?, 't', 'i', 1, '{}', 'v', 'pending')",
            (sid, i),
        )


async def test_turn_scope_lists_this_run_only(editing: EditingService) -> None:
    await editing.changes.add_many(
        [
            entry("run-1", "u1", ref="R1"),
            entry("run-1", "u2", side_effect=True),
            entry("run-2", "x"),
        ]
    )
    data = await call(Ctx(editing))
    assert data["scope"] == "turn" and data["count"] == 2 and data["side_effects"] == 1
    first, second = data["changes"]
    assert first == {
        "op": "add",
        "kind": "wire",
        "uuid": "u1",
        "sheet_path": "/r",
        "tool": "sch_add_wire",
        "call_seq": 1,
        "ref": "R1",
    }
    assert second["side_effect"] is True and "truncated" not in data


async def test_step_scope_needs_active_step(editing: EditingService) -> None:
    await editing.changes.add_many([entry("run-1", "a", "s1"), entry("run-1", "b")])
    with pytest.raises(StructuredToolError) as err:
        await call(Ctx(editing), scope="step")
    assert err.value.code == "not_found"
    editing.turn("p1", "run-1").active_step_id = "s1"
    data = await call(Ctx(editing), scope="step")
    assert [c["uuid"] for c in data["changes"]] == ["a"]


async def test_plan_scope_lists_every_step_of_the_open_plan(editing: EditingService) -> None:
    with pytest.raises(StructuredToolError):
        await call(Ctx(editing), scope="plan")
    await _open_plan(editing.db, ["s1", "s2"])
    await editing.changes.add_many(
        [entry("run-1", "a", "s1"), entry("run-2", "b", "s2"), entry("run-2", "c", "other")]
    )
    data = await call(Ctx(editing, "run-2"), scope="plan")
    assert [c["uuid"] for c in data["changes"]] == ["a", "b"]


async def test_closed_plan_is_not_listed(editing: EditingService) -> None:
    await _open_plan(editing.db, ["s1"], status="done")
    with pytest.raises(StructuredToolError):
        await call(Ctx(editing), scope="plan")


async def test_bad_scope_and_no_turn(editing: EditingService) -> None:
    with pytest.raises(StructuredToolError) as err:
        await call(Ctx(editing), scope="everything")
    assert err.value.code == "invalid_argument"
    with pytest.raises(StructuredToolError):
        await call(Ctx(None))
    with pytest.raises(StructuredToolError):
        await call(Ctx(editing, None))


async def test_long_logs_are_truncated_to_the_newest(
    editing: EditingService, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(specs, "MAX_ENTRIES", 2)
    await editing.changes.add_many([entry("run-1", f"u{i}") for i in range(3)])
    data = await call(Ctx(editing))
    assert data["count"] == 3 and data["truncated"] is True
    assert [c["uuid"] for c in data["changes"]] == ["u1", "u2"]


def test_registered_as_read_only_tool() -> None:
    from copperchat.tools import registry

    spec = next(s for s in registry.all_specs() if s.name == "sch_changes")
    assert not spec.mutating and not spec.changes_documents and spec.prepare is None
