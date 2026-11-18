"""`fp_get` on a real api-server (F1 Task 5, spec §7.2)."""

from __future__ import annotations

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types import DocumentType

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.read import fp_get
from copperchat.tools.spec import ToolContext
from tests.kicad.edit.helpers import item_ids, v2

pytestmark = pytest.mark.kicad


def ctx(session: KicadSession) -> ToolContext:
    return ToolContext(project=session.project, session=session, settings=session.settings)


def _open_footprints(h: KicadHandle) -> list[str]:
    try:
        docs = h.kicad.get_open_documents(DocumentType.DOCTYPE_FOOTPRINT)
    except ApiError as exc:
        if "no handler available" in str(exc):  # handler unregistered after the last close
            return []
        raise
    return [d.lib_id.entry_name for d in docs if d.lib_id.entry_name]


async def test_fp_get_boardless_activates(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    assert not session.footprints_active
    r = await fp_get(ctx(session), {"lib_id": SEED_LIB_ID})
    assert r.data is not None
    data = r.data
    assert data["lib_id"] == SEED_LIB_ID and data["scope"] == "global"
    assert len(data["pads"]) == 2 and data["pad_count"] == 2
    assert "shapes" not in data
    assert data["notes"][0]["kind"] == "footprint_libraries_loaded"
    assert data["notes"][0]["seconds"] > 0
    assert data["review"] is None  # only local footprints have a review state
    assert "source" in data and len(data["geometry_hash"]) == 64
    assert session.footprints_active

    again = await fp_get(ctx(session), {"lib_id": SEED_LIB_ID, "detail": "full"})
    assert again.data is not None
    assert "footprint_libraries_loaded" not in {n["kind"] for n in again.data["notes"]}
    assert again.data["shapes"] and again.data["geometry_hash"] == data["geometry_hash"]
    assert await session.call(_open_footprints) == []


async def test_fp_get_with_board_is_lazy(blank_session: KicadSession) -> None:
    session = blank_session
    r = await fp_get(ctx(session), {"lib_id": SEED_LIB_ID})
    assert r.data is not None and len(r.data["pads"]) == 2
    assert "footprint_libraries_loaded" not in {n["kind"] for n in r.data["notes"]}
    assert not session.footprints_active  # board servers read per library (spec §6.1)


async def test_fp_get_not_found_closest(blank_session: KicadSession) -> None:
    with pytest.raises(StructuredToolError) as err:
        await fp_get(ctx(blank_session), {"lib_id": "Inductor_SMD:L_Bourns-SRN4019"})
    assert err.value.code == "not_found", err.value.why
    assert err.value.details is not None
    assert SEED_LIB_ID in err.value.details["closest"]


async def test_coexistence_with_open_commit(blank_nopcb_session: KicadSession) -> None:
    """fp_get (here: activating, i.e. opening and closing a footprint document) inside an
    open schematic commit neither lands in nor breaks that commit.

    Lock order: the nested read takes `_commit_lock` → `_fp_lock`, the reverse of the
    documented order. Safe only because nothing holding `_fp_lock` waits on
    `_commit_lock`; F4a must keep it that way or never read inside a commit (plan
    "As built")."""
    session = blank_nopcb_session
    await session.ensure_symbol_libraries()
    before = await session.call(item_ids)
    async with session.commit("probe", dry_run=True):
        await session.call(
            lambda h: h.schematic().place_symbol_from_library(
                "Device:R", v2(50.8, 50.8), reference="R1"
            )
        )
        r = await fp_get(ctx(session), {"lib_id": SEED_LIB_ID})
        assert r.data is not None and len(r.data["pads"]) == 2
    assert await session.call(item_ids) == before
    assert await session.call(lambda h: h.schematic().is_document_modified()) is False
    assert await session.call(_open_footprints) == []
    assert not session.restart_pending
