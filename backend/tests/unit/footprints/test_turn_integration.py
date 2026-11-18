"""F4a Task 5: `fp_create.apply` in the turn (spec §9.1, §10, §12) - a library-only
result (`changed_docs` empty, `library_changed` = the nickname), the change-log rows
(`footprint_created` / `footprint_replaced` / `fp_lib_table_row_added`, ref = lib id,
after = the geometry hash), footprint kinds never count as logged schematic items in
the diff, and the adapter's save step does nothing for a library-only result."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from copperchat.agent.sdk_tools import _save_edit
from copperchat.changes.diff import compact_diff
from copperchat.changes.repo import FOOTPRINT_KINDS, ChangeEntry
from copperchat.config import Settings
from copperchat.editing.service import EditingService
from copperchat.kicad_tools.footprints import create as create_mod
from copperchat.kicad_tools.footprints.engine import (
    FootprintApplied,
    FootprintChangeSet,
    FootprintPreImage,
)
from copperchat.kicad_tools.footprints.model import geometry_hash
from copperchat.tools import ToolContext, ToolResult, edit_spec
from tests.unit.footprints.test_engine_unit import PID, _engine_setup, editing  # noqa: F401


def _applied(cs: FootprintChangeSet, *, row_added: bool) -> FootprintApplied:
    assert cs.target is not None and cs.library is not None
    name = cs.target.lib_id.split(":", 1)[1]
    return FootprintApplied(
        lib_ids=[cs.target.lib_id],
        files=[f"footprints/{cs.library.nickname}.pretty/{name}.kicad_mod"],
        row_added=row_added,
        dir_created=row_added,
        readback=cs.target,  # a read-back equal to the target
    )


async def _apply(
    monkeypatch: pytest.MonkeyPatch,
    ctx: ToolContext,
    cs: FootprintChangeSet,
    applied: FootprintApplied,
) -> ToolResult:
    async def engine(_ctx: ToolContext, _cs: Any) -> FootprintApplied:
        return applied

    monkeypatch.setattr(create_mod, "apply_footprint_change_set", engine)
    return await create_mod.apply_footprint_create(ctx, cs)


async def test_apply_is_library_only_and_logs_the_row_and_footprint(
    make_settings: Callable[..., Settings],
    editing: EditingService,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    ctx, _session, cs = _engine_setup(settings.projects_root, settings, editing)
    assert cs.library is not None and cs.target is not None
    editing.turn(PID, "run1").call_seq = 7
    result = await _apply(monkeypatch, ctx, cs, _applied(cs, row_added=True))

    assert result.changed_docs == set()
    assert result.library_changed == {cs.library.nickname}
    rows = await editing.changes.for_run(PID, "run1")
    assert [(r.op, r.kind, r.ref, r.uuid) for r in rows] == [
        ("add", "fp_lib_table_row_added", cs.library.nickname, cs.library.nickname),
        ("add", "footprint_created", cs.target.lib_id, cs.target.lib_id),
    ]
    row, created = rows
    assert row.after == {"uri": cs.library.uri}
    assert created.after == {"geometry_hash": geometry_hash(cs.target)}
    assert created.before is None
    assert {(r.tool, r.call_seq, r.sheet_path, r.side_effect) for r in rows} == {
        ("fp_create", 7, "", False)
    }


async def test_apply_without_a_new_row_logs_only_the_footprint(
    make_settings: Callable[..., Settings],
    editing: EditingService,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    ctx, _session, cs = _engine_setup(settings.projects_root, settings, editing)
    await _apply(monkeypatch, ctx, cs, _applied(cs, row_added=False))
    rows = await editing.changes.for_run(PID, "run1")
    assert [r.kind for r in rows] == ["footprint_created"]


async def test_replace_is_logged_as_footprint_replaced(
    make_settings: Callable[..., Settings],
    editing: EditingService,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`replace` (Task 6): op `modify`, before = the pre-image's geometry hash."""
    settings = make_settings()
    ctx, _session, cs = _engine_setup(settings.projects_root, settings, editing)
    assert cs.target is not None
    cs.pre_image = FootprintPreImage(
        items=[], definition=b"", file_sha256="ab" * 32, geometry_hash="old-hash", fields={}
    )
    await _apply(monkeypatch, ctx, cs, _applied(cs, row_added=False))
    rows = await editing.changes.for_run(PID, "run1")
    assert [(r.op, r.kind, r.ref) for r in rows] == [
        ("modify", "footprint_replaced", cs.target.lib_id)
    ]
    assert rows[0].before == {"geometry_hash": "old-hash"}
    assert rows[0].after == {"geometry_hash": geometry_hash(cs.target)}


async def test_a_failing_change_log_never_fails_the_call(
    make_settings: Callable[..., Settings],
    editing: EditingService,  # noqa: F811
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = make_settings()
    ctx, _session, cs = _engine_setup(settings.projects_root, settings, editing)

    async def broken(_ctx: Any, _entries: Any) -> int:
        raise RuntimeError("db gone")

    monkeypatch.setattr(editing, "record_changes", broken)
    result = await _apply(monkeypatch, ctx, cs, _applied(cs, row_added=True))
    assert result.data is not None and result.data["created"] == [cs.target.lib_id]  # type: ignore[union-attr]


def _entry(kind: str, uuid: str, op: str = "add") -> ChangeEntry:
    return ChangeEntry(
        project_id=PID, run_id="r", plan_step_id=None, call_seq=1, tool="t", op=op,
        kind=kind, uuid=uuid, sheet_path="",
    )  # fmt: skip


def test_footprint_kinds() -> None:
    assert {
        "footprint_created",
        "footprint_replaced",
        "footprint_deleted",
        "footprint_approved",
        "fp_lib_table_row_added",
    } == FOOTPRINT_KINDS


def test_diff_never_counts_footprint_rows_as_logged_items() -> None:
    """A footprint row (uuid = lib id) never marks a schematic diff item as the tool's
    own change; schematic rows still do."""
    raw = {
        "changes": [
            {"id": "/Lib:FP", "kind": "added", "typeName": "Symbol"},
            {"id": "/u-2", "kind": "added", "typeName": "Symbol"},
        ]
    }
    log = [_entry("footprint_created", "Lib:FP"), _entry("symbol", "u-2")]
    out = compact_diff(raw, log)
    flags = {e["uuid"]: e["side_effect"] for es in out["sheets"].values() for e in es}
    assert flags == {"Lib:FP": True, "u-2": False}


async def test_save_step_does_nothing_for_a_library_only_result(
    make_settings: Callable[..., Settings],
    editing: EditingService,  # noqa: F811
) -> None:
    """Footprint stages save inside apply (C9): the adapter's `_save_edit` never calls
    `session.save` for a result without changed_docs, so it cannot fail with
    `save_failed` after a footprint apply (the cleanup notes stay on the result)."""
    settings = make_settings()
    ctx, session, _cs = _engine_setup(settings.projects_root, settings, editing)

    async def save(doc: Any) -> None:
        raise AssertionError(f"saved {doc}")

    session.save = save  # type: ignore[attr-defined]
    notes = [{"kind": "pending_cleaned", "lib_id": "X:Y", "message": "m"}]
    result = ToolResult(data={"notes": notes}, library_changed={"X"})
    spec = edit_spec("fp_create", "d", {"type": "object"}, None, None)  # type: ignore[arg-type]
    await _save_edit(spec, ctx, result)
    assert result.data == {"notes": notes}
    assert not editing.turn(PID, "run1").halted
