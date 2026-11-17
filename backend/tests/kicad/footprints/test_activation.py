"""Footprint activation on a real api-server (F1)."""

from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types import DocumentType
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle, KicadSession, footprint_doc
from copperchat.kicad_session.footprint_doc import SEED_LIB_ID
from copperchat.library.service import LibraryService

pytestmark = pytest.mark.kicad


def _global_rows(h: KicadHandle) -> set[str] | str:
    try:
        rows = h.kicad.get_library_table(
            LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_GLOBAL
        ).rows
    except ApiError as exc:
        return f"ApiError: {exc}"
    return {r.nickname for r in rows}


async def test_global_table_before_activation(
    blank_nopcb_session: KicadSession, record_property: Callable[[str, object], None]
) -> None:
    """Probe (author call 9): the global footprint table answers on a boardless api-server
    before any footprint document was opened, and global_footprint_nicknames uses it."""
    session = blank_nopcb_session
    assert not getattr(session, "footprints_active", False)

    api_rows = await session.call(_global_rows)
    assert not isinstance(api_rows, str), (
        f"the global footprint table does not answer before activation ({api_rows}); "
        "implement the stock-dir fallback of author call 9 in global_footprint_nicknames"
    )
    record_property("global_table_probe", "api-table")
    assert "Inductor_SMD" in api_rows

    nicknames = await LibraryService(session).global_footprint_nicknames()
    assert nicknames == frozenset(api_rows)


def _seed_file(h: KicadHandle) -> Path:
    nickname, name = SEED_LIB_ID.split(":")
    rows = h.kicad.get_library_table(
        LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_GLOBAL, substituted=True
    ).rows
    uri = next(r.uri for r in rows if r.nickname == nickname)
    return Path(uri) / f"{name}.kicad_mod"


def _open_footprint_entries(h: KicadHandle) -> list[str]:
    try:
        docs = h.kicad.get_open_documents(DocumentType.DOCTYPE_FOOTPRINT)
    except ApiError as exc:
        # Closing the last footprint document unregisters the footprint handler again
        if "no handler available" in str(exc):
            return []
        raise
    return [d.lib_id.entry_name for d in docs if d.lib_id.entry_name]


async def test_activate_once(
    blank_nopcb_session: KicadSession,
    caplog: pytest.LogCaptureFixture,
    record_property: Callable[[str, object], None],
) -> None:
    session = blank_nopcb_session
    assert not session.footprints_active
    with caplog.at_level(logging.INFO, logger="copperchat.kicad_session"):
        seconds = await session.activate_footprints()
    assert isinstance(seconds, float) and seconds > 0
    assert session.footprints_active
    assert "footprint libraries loaded" in caplog.text
    record_property("activation_seconds", seconds)
    record_property("activation_log", caplog.text)
    assert await session.activate_footprints() is None
    assert await session.call(_open_footprint_entries) == []


async def test_flag_resets_on_restart(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    await session.activate_footprints()
    assert session.footprints_active
    await session.restart()
    assert not session.footprints_active


async def test_document_always_closed(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    with pytest.raises(RuntimeError, match="inside"):
        async with session.footprint_document(SEED_LIB_ID) as doc:
            assert await doc.items()
            raise RuntimeError("inside the block")
    assert await session.call(_open_footprint_entries) == []
    assert not session.restart_pending
    await session.call(lambda h: h.schematic().get_symbols())


async def test_read_items_of_seed(blank_nopcb_session: KicadSession) -> None:
    session = blank_nopcb_session
    seed = await session.call(_seed_file)
    before = seed.stat().st_mtime_ns
    async with session.footprint_document(SEED_LIB_ID) as doc:
        items = await doc.items()
        footprint = await doc.footprint()
        fields = await doc.fields()
    pads = [i for i in items if i.DESCRIPTOR.name == "Pad"]
    assert len(pads) == 2
    assert "SRN4018" in footprint.definition.attributes.description
    assert {f.name for f in fields} >= {"Reference", "Value"}
    assert seed.stat().st_mtime_ns == before
    assert session.footprints_active


async def test_seed_missing(
    blank_nopcb_session: KicadSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(footprint_doc, "SEED_LIB_ID", "Inductor_SMD:Nope")
    with pytest.raises(StructuredToolError) as err:
        await blank_nopcb_session.activate_footprints()
    assert err.value.code == "seed_missing"
    assert "Inductor_SMD:Nope" in str(err.value)
    assert not blank_nopcb_session.footprints_active
