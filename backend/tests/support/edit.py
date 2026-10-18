"""Helpers for real-KiCad edit tests (API objects only, never file edits)."""

from __future__ import annotations

from typing import Any

from kipy.proto.common.types import KIID

from copperchat.db import Database
from copperchat.db.repos.editing_settings import EditingSettingsRepo
from copperchat.editing.service import EditingService
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import DEG_SSO
from copperchat.kicad_tools.edit.scene import resolve_sheet, sheet_api
from copperchat.tools.spec import ToolContext
from tests.kicad.edit.helpers import v2

RUN_ID = "run-test"


def edit_ctx(session: KicadSession, db: Database | None = None, **kw: Any) -> ToolContext:
    """ToolContext for edit tests; with `db` it carries an EditingService (turn state,
    budget gate, change log) and a run id, as the run engine's contexts do."""
    editing = None
    if db is not None:
        editing = EditingService(db=db, broadcaster=None, settings_repo=EditingSettingsRepo(db))
    fields: dict[str, Any] = {"run_id": RUN_ID if db is not None else None, "editing": editing}
    fields.update(kw)
    return ToolContext(
        project=session.project, session=session, settings=session.settings, **fields
    )


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


def free_area(
    h: KicadHandle, w: float, hgt: float, sheet: str | None = None
) -> tuple[float, float]:
    """Top-left grid point of a w x hgt mm area on `sheet` with no symbol, field, sheet,
    wire, label, junction or no-connect in it (plus 2.54 mm around)."""
    from copperchat.kicad_tools.edit.geometry import Box
    from copperchat.kicad_tools.edit.scene import read_scene

    scene = read_scene(h, resolve_sheet(h, sheet))
    blocked = scene.obstacles()
    blocked += [Box.around([w_.start, w_.end]) for w_ in scene.wires.values()]
    blocked += [Box.around([p, p]) for p in scene.junctions.values()]
    blocked += [Box.around([lb.at, lb.at]).grow(5.0) for lb in scene.labels.values()]
    blocked += [Box.around([p, p]) for p in scene.no_connects.values()]
    for y in range(2, 30):
        for x in range(2, 44):
            cand = Box(x * 6.35, y * 6.35, x * 6.35 + w, y * 6.35 + hgt)
            if not any(cand.overlaps(b, 2.54) for b in blocked):
                return (cand.x0, cand.y0)
    raise AssertionError("no free area on the sheet")


async def place_raw(
    session: KicadSession,
    lib_id: str,
    at: tuple[float, float],
    ref: str,
    *,
    sheet: str | None = None,
    rotation: int = 0,
) -> str:
    """Place a symbol through the engine (one apply_change_set: a Place stage, then an
    Update stage re-sending the placed symbol, as E4's placement does); returns its UUID.
    For E5/E7/E8a tests that must not depend on E4's sch_place_symbol."""
    from copperchat.kicad_tools.edit.changeset import (
        ChangeSet,
        Logical,
        NetIntent,
        Place,
        Stage,
        StageInput,
        Update,
        static_stage,
    )
    from copperchat.kicad_tools.edit.engine import apply_change_set

    target = await session.call(lambda h: resolve_sheet(h, sheet))

    def resend(inp: StageInput) -> list[Any]:
        return [Update(list(inp.placed), sheet=target)]

    cs = ChangeSet(
        tool="place_raw",
        sheet=target,
        stages=[
            static_stage("place", Place(lib_id, at, rotation, 1, ref, sheet=target)),
            Stage("fields", resend),
        ],
        pre_images={},
        intent=NetIntent(),
        logical=Logical(1),
    )
    applied = await apply_change_set(edit_ctx(session), cs)
    return applied.placed[0]


async def call_tool(ctx: ToolContext, name: str, /, **args: Any) -> tuple[dict[str, Any], bool]:
    """Run a registered tool through the real adapter (agent.sdk_tools); returns the JSON
    payload and whether it is an error."""
    import json

    from copperchat.agent.sdk_tools import build_sdk_tool_list
    from copperchat.tools import registry

    spec = next(s for s in registry.all_specs() if s.name == name)
    tool = build_sdk_tool_list([spec], lambda: ctx)[0]
    raw = await tool.handler(args)
    payload: dict[str, Any] = json.loads(raw["content"][0]["text"])
    return payload, bool(raw.get("is_error"))


async def two_resistors(session: KicadSession) -> dict[str, Any]:
    """R901 and R902 side by side in a free area of the root sheet; returns their UUIDs
    and pin ends ("a1", "a2", "b1", "b2")."""
    from copperchat.kicad_tools.edit.geometry import pin_ends

    await session.ensure_symbol_libraries()
    x, y = await session.call(lambda h: free_area(h, 20.32, 15.24))
    a = await place_raw(session, "Device:R", (x + 2.54, y + 5.08), "R901")
    b = await place_raw(session, "Device:R", (x + 12.7, y + 5.08), "R902")

    def pins(h: KicadHandle) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, uid in (("a", a), ("b", b)):
            for p in pin_ends(get_item(h, uid)):
                out[f"{name}{p.number}"] = p
        return out

    return {"uuids": (a, b), **(await session.call(pins))}
