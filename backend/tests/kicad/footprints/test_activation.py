"""Footprint activation on a real api-server (F1)."""

from __future__ import annotations

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.kicad_session import KicadSession
from copperchat.library.service import LibraryService

pytestmark = pytest.mark.kicad


async def test_global_table_before_activation(
    blank_nopcb_session: KicadSession, record_property: pytest.RecordProperty
) -> None:
    """Probe (author call 9): does the global footprint table answer before any footprint
    document was opened? Whichever branch it takes, global_footprint_nicknames agrees."""
    session = blank_nopcb_session
    assert not getattr(session, "footprints_active", False)

    def probe(h):  # type: ignore[no-untyped-def]
        try:
            rows = h.kicad.get_library_table(
                LibraryType.LT_FOOTPRINT, LibraryTableScope.LTS_GLOBAL
            ).rows
        except ApiError:
            return None
        return {r.nickname for r in rows}

    api_rows = await session.call(probe)
    branch = "api-table" if api_rows else "stock-dir"
    record_property("global_table_probe", branch)
    print(f"PROBE_BRANCH={branch} rows={None if api_rows is None else len(api_rows)}")
    if api_rows:
        assert "Inductor_SMD" in api_rows

    nicknames = await LibraryService(session).global_footprint_nicknames()
    assert "Inductor_SMD" in nicknames
    if api_rows:
        assert nicknames == frozenset(api_rows)
