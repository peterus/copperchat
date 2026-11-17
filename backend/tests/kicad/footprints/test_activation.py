"""Footprint activation on a real api-server (F1)."""

from __future__ import annotations

from collections.abc import Callable

import pytest
from kipy.errors import ApiError
from kipy.proto.common.types.library_types_pb2 import LibraryTableScope, LibraryType

from copperchat.kicad_session import KicadHandle, KicadSession
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
