"""KicadSession editing core against a real api-server (phase E1, Review Focus 2/3)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest

from copperchat.kicad_session import KicadHandle, KicadSession
from tests.kicad.edit.helpers import item_ids, local_label, sheet_instance, v2, wire

pytestmark = pytest.mark.kicad


def _create(*items: Any):  # type: ignore[no-untyped-def]
    def run(h: KicadHandle) -> list[str]:
        return [str(i.id.value) for i in h.schematic().create_items(list(items))]

    return run


def _exists(uid: str):  # type: ignore[no-untyped-def]
    def run(h: KicadHandle) -> bool:
        return uid in item_ids(h)

    return run


async def test_commit_pushes_and_marks_stale(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    assert s.jobs_stale is False and s.modified is False
    w = wire((20.32, 20.32), (30.48, 20.32))
    async with s.commit("add wire") as scope:
        assert scope.label == "add wire"
        await s.call(_create(w))
    assert await s.call(_exists(str(w.id.value)))
    assert s.jobs_stale is True and s.modified is True


async def test_commit_drops_on_exception(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    before = await s.call(item_ids)
    with pytest.raises(RuntimeError, match="boom"):
        async with s.commit("t"):
            await s.call(_create(wire((20.32, 20.32), (30.48, 20.32))))
            raise RuntimeError("boom")
    assert await s.call(item_ids) == before
    assert s.jobs_stale is False and s.modified is False


async def test_cancel_inside_commit_leaves_no_stale_commit(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    lost = wire((20.32, 20.32), (30.48, 20.32))
    staged = asyncio.Event()

    async def hang() -> None:
        async with s.commit("cancelled"):
            await s.call(_create(lost))
            staged.set()
            await asyncio.Event().wait()

    task = asyncio.create_task(hang())
    await asyncio.wait_for(staged.wait(), 30)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    kept = wire((20.32, 25.4), (30.48, 25.4))
    async with s.commit("next"):
        await s.call(_create(kept))
    assert await s.call(_exists(str(kept.id.value)))
    assert not await s.call(_exists(str(lost.id.value)))


async def test_dry_run_never_changes_document(blank_session: KicadSession) -> None:
    s = blank_session
    await s.ensure_symbol_libraries()
    before = await s.call(item_ids)
    async with s.commit("probe", dry_run=True):
        await s.call(
            lambda h: h.schematic().place_symbol_from_library(
                "Device:R", v2(50.8, 50.8), reference="R1"
            )
        )
    assert await s.call(item_ids) == before
    assert await s.call(lambda h: h.schematic().is_document_modified()) is False
    assert s.jobs_stale is False


async def test_commit_spans_for_sheet_writes(hier_session: KicadSession) -> None:
    s = hier_session
    label = local_label("E1_PROBE", (20.32, 20.32))

    def create_on_sheet(h: KicadHandle) -> None:
        inst = sheet_instance(h, "ampli_ht.kicad_sch")
        h.schematic().for_sheet(inst).create_items([label])

    def labels_on_sheet(h: KicadHandle) -> list[str]:
        inst = sheet_instance(h, "ampli_ht.kicad_sch")
        return [str(lb.text.value) for lb in h.schematic().for_sheet(inst).get_labels()]

    async with s.commit("sub-sheet label"):
        await s.call(create_on_sheet)
    assert "E1_PROBE" in await s.call(labels_on_sheet)


async def test_save_waits_for_open_commit(ecc83_session: KicadSession) -> None:
    s = ecc83_session
    order: list[str] = []
    opened, release = asyncio.Event(), asyncio.Event()

    async def edit() -> None:
        async with s.commit("held"):
            await s.call(_create(wire((20.32, 20.32), (30.48, 20.32))))
            opened.set()
            await release.wait()
            order.append("commit-exit")

    async def save() -> None:
        await s.save("sch")
        order.append("saved")

    edit_task = asyncio.create_task(edit())
    await asyncio.wait_for(opened.wait(), 30)
    save_task = asyncio.create_task(save())
    await asyncio.sleep(0.5)
    assert not save_task.done()
    release.set()
    await asyncio.wait_for(asyncio.gather(edit_task, save_task), 30)
    assert order == ["commit-exit", "saved"]
    assert s.modified is False


async def test_save_copy_and_refresh_jobs_on_modified_doc_do_not_deadlock(
    ecc83_session: KicadSession, tmp_path: Path
) -> None:
    s = ecc83_session
    async with s.commit("t"):
        await s.call(_create(wire((20.32, 20.32), (30.48, 20.32))))
    dest = tmp_path / "copy" / s.project.sch_file.name  # type: ignore[union-attr]
    await asyncio.wait_for(s.save_copy("sch", dest), 30)
    assert dest.is_file()
    await asyncio.wait_for(s.refresh_jobs(), 30)
    assert s.jobs_stale is False
