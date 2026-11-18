"""LibraryService footprint access on a real api-server (F1 Task 4): the local library
row, a local footprint read through the footprint document, and a stock footprint read
through GetItemsFromLibrary. The local footprint is created only through the KiCad API
(project row + SaveCopyOfDocument of the seed)."""

from __future__ import annotations

from typing import Any

import pytest
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.kicad_tools.footprints.model import geometry_from_proto, geometry_hash
from copperchat.library.service import LibraryService

pytestmark = pytest.mark.kicad

NAME = "L_CopperChat_Test"


async def test_local_footprint_read_through_document(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    svc = LibraryService(session)
    assert not svc.footprint_libraries_ready()

    info = await svc.local_library()
    assert info is not None  # tmp project without git: root = project dir, not transient
    lib = info.lib
    assert lib.uri == f"${{KIPRJMOD}}/footprints/{lib.nickname}.pretty"
    assert (info.row_present, info.row_conflict) == (False, None)
    assert svc.local_names_on_disk(lib) == []

    # a stock footprint: library read, activates the boardless server, flagged incomplete
    notes: list[dict[str, Any]] = []
    stock = await svc.footprint_geometry(SEED_LIB_ID, notes)
    assert stock is not None and stock.scope == "global" and stock.mounting == "unspecified"
    assert [n["kind"] for n in notes] == [
        "footprint_libraries_loaded",
        "footprint_attributes_unavailable",
    ]
    assert svc.footprint_libraries_ready()
    assert await svc.footprint_geometry("Inductor_SMD:L_Bourns-SRN4019") is None

    # the local library row and one footprint, both written by KiCad
    def add_row(h: KicadHandle) -> None:
        h.kicad.add_library_table_entry(
            LibraryType.LT_FOOTPRINT,
            LibraryTableScope.LTS_PROJECT,
            lib.nickname,
            lib.uri,
            description="CopperChat local footprints",
            create_library=True,
        )

    await session.call(add_row)
    async with session.footprint_document(SEED_LIB_ID) as seed:
        seed_instance = await seed.footprint()
        await seed.save_copy(f"{lib.nickname}:{NAME}")
    svc.invalidate_footprints(lib.nickname)

    info = await svc.local_library(for_write=True)
    assert info is not None and (info.row_present, info.row_conflict) == (True, None)
    assert svc.local_names_on_disk(lib) == [NAME]

    notes = []
    local = await svc.footprint_geometry(f"{lib.nickname}:{NAME}", notes)
    assert local is not None and local.scope == "local"
    assert local.mounting == "smd"  # complete: read through the footprint document
    assert notes == []  # already active, and nothing is missing
    assert {t.kind for t in local.texts} >= {"reference", "value"}
    seed_geometry = geometry_from_proto(
        seed_instance, seed_instance.definition.items, SEED_LIB_ID, "global"
    )
    assert geometry_hash(local) == geometry_hash(seed_geometry)
    assert await svc.footprint_geometry(f"{lib.nickname}:Missing") is None
