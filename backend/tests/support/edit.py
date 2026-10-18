"""Helpers for real-KiCad edit tests (API objects only, never file edits)."""

from __future__ import annotations

from typing import Any

from kipy.proto.common.types import KIID

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import DEG_SSO
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from tests.kicad.edit.helpers import v2


def kiids(*uuids: str) -> list[Any]:
    out = []
    for u in uuids:
        k = KIID()
        k.value = u
        out.append(k)
    return out


def get_item(h: KicadHandle, uuid: str, sheet: SheetRef | None = None) -> Any | None:
    sch = sheet_api(h, sheet) if sheet is not None else h.schematic()
    try:
        items = sch.get_items_by_id(kiids(uuid))
    except Exception:
        return None
    return items[0] if items else None


async def place_direct(
    session: KicadSession,
    lib_id: str,
    at: tuple[float, float],
    ref: str,
    *,
    unit: int = 1,
    transform: tuple[int, bool, bool] = (0, False, False),
    sheet: str | None = None,
) -> str:
    """Place a symbol with plain commits (place, then transform): for geometry tests
    that must not depend on the engine."""

    def place(h: KicadHandle) -> str:
        target = resolve_sheet(h, sheet)
        sym = sheet_api(h, target).place_symbol_from_library(
            lib_id, v2(*at), unit=unit, reference=ref
        )
        return access.kiid(sym)

    async with session.commit(f"place {ref}"):
        uid = await session.call(place)
    if transform != (0, False, False):

        def update(h: KicadHandle) -> None:
            target = resolve_sheet(h, sheet)
            sch = sheet_api(h, target)
            sym = sch.get_items_by_id(kiids(uid))[0]
            sym.transform.orientation = DEG_SSO[transform[0]]
            sym.transform.mirror_x = transform[1]
            sym.transform.mirror_y = transform[2]
            sym.proto.ClearField("definition")
            sch.update_items([sym])

        async with session.commit(f"transform {ref}"):
            await session.call(update)
    return uid
