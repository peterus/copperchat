"""Shared helpers of the E5 connection tests (real KiCad, API objects only)."""

from __future__ import annotations

from typing import Any

from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import read_scene, resolve_sheet
from tests.kicad.edit.helpers import item_ids
from tests.support.edit import place_raw


class Counter:
    """on_mutation stand-in: counts turn snapshots."""

    def __init__(self) -> None:
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1


async def place(session: KicadSession, *parts: tuple[str, str, tuple[float, float], int]) -> None:
    """Place (lib_id, ref, at, rotation) parts with E2's engine helper."""
    await session.ensure_symbol_libraries()
    for lib_id, ref, at, rotation in parts:
        await place_raw(session, lib_id, at, ref, rotation=rotation)


async def state(session: KicadSession) -> tuple[set[str], frozenset[Any]]:
    """The document's item set and the netlist partition."""

    def run(h: KicadHandle) -> tuple[set[str], frozenset[Any]]:
        return item_ids(h), access.live_netlist(h).partition()

    return await session.call(run)


async def pin_nets(session: KicadSession) -> dict[str, str]:
    """'REF.PIN' -> net name (live netlist, power symbols included)."""

    def run(h: KicadHandle) -> dict[str, str]:
        refs = {access.kiid(s): str(s.reference) for s in h.schematic().get_symbols()}
        out: dict[str, str] = {}
        for net, keys in access.live_netlist(h).nets.items():
            for _path, uid, number in keys:
                out[f"{refs.get(uid, uid)}.{number}"] = net
        return out

    return await session.call(run)


async def scene(session: KicadSession, sheet: str | None = None) -> Any:
    return await session.call(lambda h: read_scene(h, resolve_sheet(h, sheet), boxes=False))
