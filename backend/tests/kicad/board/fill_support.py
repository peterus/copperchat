"""Helpers of the `pcb_route_fill` kicad tests (SP4 P6b): a fill with real work (some
critical nets unrouted first), the KIID-free copper fingerprint, a fake clock for the
deadline/budget tests and a `session.call` spy."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import json
import time
import types
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from kipy.board_types import Track as KipyTrack
from kipy.board_types import Via as KipyVia
from kipy.board_types import Zone
from kipy.geometry import PolygonWithHoles, PolyLine, PolyLineNode, Vector2
from kipy.proto.board.board_types_pb2 import BoardLayer

from copperchat.board.model import BoardModel, board_hash
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.read import board_model
from copperchat.projects import agent_dir
from tests.kicad.board.conftest import PlacedSynth, setup_call
from tests.support.edit import call_tool

# Short critical signal nets of the placed `sp4_synth` that a test unroutes first, so the
# fill has real connections to route (the routed rig leaves only 2 open).
FILL_NETS = ("/SWDIO", "/SWCLK", "/NRST", "/Power/FB")
FILL_TIMEOUT_S = 120
MM = 1_000_000

Copper = tuple[tuple[Any, ...], ...]


async def model_of(rig: PlacedSynth, run: str) -> BoardModel:
    return await board_model(rig.turn(run))


def copper(m: BoardModel) -> Copper:
    """Sorted KIID-free tuples of every track `(net, layer, start, end, width, locked)` and
    via `(net, at, diameter, drill, locked)`, rounded to 0.001 mm."""

    def r(p: tuple[float, float]) -> tuple[float, float]:
        return (round(p[0], 3), round(p[1], 3))

    tracks = [
        ("t", t.net, t.layer, *sorted((r(t.start), r(t.end))), round(t.width, 3), t.locked)
        for t in m.tracks
    ]
    vias = [
        ("v", v.net, r(v.at), round(v.diameter, 3), round(v.drill, 3), v.locked) for v in m.vias
    ]
    return tuple(sorted(tracks + vias, key=repr))


async def unroute(rig: PlacedSynth, run: str, nets: tuple[str, ...] = FILL_NETS) -> None:
    await setup_call(rig.turn(run), "pcb_unroute", nets=list(nets), include_locked=True)


async def fill(rig: PlacedSynth, run: str, **args: Any) -> tuple[dict[str, Any], bool]:
    """One `pcb_route_fill` call through the adapter (turn snapshot first)."""
    ctx = rig.turn(run)
    if ctx.on_mutation is not None:
        await ctx.on_mutation()
    args.setdefault("timeout_s", FILL_TIMEOUT_S)
    return await call_tool(ctx, "pcb_route_fill", **args)


async def fill_ok(rig: PlacedSynth, run: str, **args: Any) -> dict[str, Any]:
    out, err = await fill(rig, run, **args)
    if err:
        pytest.fail(json.dumps(out, indent=1)[:4000])
    return out


def fill_dirs(rig: PlacedSynth) -> list[Path]:
    return sorted((agent_dir(rig.session.project) / "board").glob("fill-*"))


async def state(rig: PlacedSynth, run: str) -> tuple[Copper, str, BoardModel]:
    m = await model_of(rig, run)
    return copper(m), board_hash(m), m


def _poly(m: BoardModel) -> PolygonWithHoles:
    o = m.outline
    line = PolyLine()
    for x, y in ((o.x, o.y), (o.x + o.w, o.y), (o.x + o.w, o.y + o.h), (o.x, o.y + o.h)):
        line.append(PolyLineNode.from_xy(round(x * MM), round(y * MM)))
    line.closed = True
    poly = PolygonWithHoles()
    poly.outline = line
    return poly


async def add_gnd_plane(rig: PlacedSynth, run: str, layer: int = BoardLayer.BL_In1_Cu) -> str:
    """A GND zone over the outline on `layer` (priority 10), refilled; returns its KIID."""
    m = await model_of(rig, run)
    s = rig.session

    def add(h: KicadHandle) -> str:
        board = h.board()
        (gnd,) = [n for n in board.get_nets() if n.name == "GND"]
        z = Zone()
        z.layers = [layer]
        z.net = gnd
        z.priority = 10
        z.outline = _poly(m)
        (made,) = board.create_items([z])
        return str(made.id.value)

    async with s.commit("test: gnd plane", doc="pcb"):
        kiid = await s.call(add)
    await s.call(lambda h: api.refill(h, timeout_s=60), timeout=api.refill_call_timeout(60))
    return kiid


async def remove_items(rig: PlacedSynth, ids: list[str]) -> None:
    from copperchat.kicad_tools.board.route import _kiids

    s = rig.session
    async with s.commit("test: remove", doc="pcb"):
        await s.call(lambda h: h.board().remove_items_by_id(_kiids(ids)))


async def add_unlocked_track(rig: PlacedSynth, run: str, net: str = "/SWDIO") -> str:
    """An unlocked 1 mm track of `net` on B.Cu starting at its first pad."""
    m = await model_of(rig, run)
    pad = next(q for p in m.parts for q in p.pads if q.net == net)
    s = rig.session

    def add(h: KicadHandle) -> str:
        board = h.board()
        (n,) = [x for x in board.get_nets() if x.name == net]
        t = KipyTrack()
        t.layer = BoardLayer.BL_B_Cu
        t.net = n
        t.start = Vector2.from_xy(round(pad.x * MM), round(pad.y * MM))
        t.end = Vector2.from_xy(round(pad.x * MM), round((pad.y + 1.0) * MM))
        t.width = round(0.2 * MM)
        t.locked = False
        (made,) = board.create_items([t])
        return str(made.id.value)

    async with s.commit("test: unlocked track", doc="pcb"):
        return await s.call(add)


async def locked_gnd_fanout(rig: PlacedSynth, run: str, n: int = 4) -> tuple[list[str], str]:
    """What P7's `pcb_fanout` will create (no P7 import): for `n` SMD GND pads a 0.6 mm
    track stub at 45° from the pad centre and a 0.6/0.3 mm via at its end, both locked;
    plus one GND zone on In1.Cu over the outline (priority 10), refilled. Returns the
    stub/via KIIDs and the zone KIID."""
    m = await model_of(rig, run)
    pads = sorted(
        (q for p in m.parts for q in p.pads if q.net == "GND" and q.smd),
        key=lambda q: (q.ref, q.number),
    )
    chosen = pads[:: max(1, len(pads) // n)][:n]
    s = rig.session

    def add(h: KicadHandle) -> list[str]:
        board = h.board()
        (gnd,) = [x for x in board.get_nets() if x.name == "GND"]
        made = []
        for q in chosen:
            d = 0.8 / 2**0.5
            sx, sy = round(q.x * MM), round(q.y * MM)
            ex, ey = round((q.x + d) * MM), round((q.y + d) * MM)
            t = KipyTrack()
            t.layer = BoardLayer.BL_F_Cu
            t.net = gnd
            t.start, t.end = Vector2.from_xy(sx, sy), Vector2.from_xy(ex, ey)
            t.width = round(0.6 * MM)
            t.locked = True
            v = KipyVia()
            v.position = Vector2.from_xy(ex, ey)
            v.net = gnd
            if not len(v.padstack.proto.copper_layers):
                v.padstack.proto.copper_layers.add().layer = BoardLayer.BL_F_Cu
            v.diameter = round(0.6 * MM)
            v.drill_diameter = round(0.3 * MM)
            v.locked = True
            made += [t, v]
        return [str(c.id.value) for c in board.create_items(made)]

    async with s.commit("test: locked fanout", doc="pcb"):
        ids = await s.call(add)
    zone = await add_gnd_plane(rig, run)
    return ids, zone


class FakeClock:
    """`time.monotonic()` + an offset tests advance; installed as the `time` module of the
    fill and route modules only (asyncio keeps the real clock)."""

    def __init__(self) -> None:
        self.offset = 0.0

    def monotonic(self) -> float:
        return time.monotonic() + self.offset

    def module(self) -> types.SimpleNamespace:
        return types.SimpleNamespace(monotonic=self.monotonic, time=time.time, sleep=time.sleep)


def install_clock(monkeypatch: pytest.MonkeyPatch, *modules: Any) -> FakeClock:
    clock = FakeClock()
    for mod in modules:
        monkeypatch.setattr(mod, "time", clock.module())
    return clock


@dataclasses.dataclass
class CallSpy:
    """Every `session.call` timeout, labelled by the api function the callable ran."""

    calls: list[tuple[str, float | None]] = dataclasses.field(default_factory=list)
    inside: list[str] = dataclasses.field(default_factory=list)


@contextlib.contextmanager
def spy_calls(
    monkeypatch: pytest.MonkeyPatch, rig: PlacedSynth, targets: tuple[tuple[Any, str], ...]
) -> Iterator[CallSpy]:
    """Wrap `rig.session.call` and the functions `targets` ((module, name) pairs): each
    call that ran one of them is recorded as (name, timeout)."""
    spy = CallSpy()
    session = rig.session
    real_call = session.call

    async def call(fn: Callable[[KicadHandle], Any], *, timeout: float | None = None) -> Any:
        before = len(spy.inside)
        out = await real_call(fn, timeout=timeout)
        for name in spy.inside[before:]:
            spy.calls.append((name, timeout))
        return out

    for mod, name in targets:
        real = getattr(mod, name)

        def wrap(*a: Any, _real: Any = real, _name: str = name, **kw: Any) -> Any:
            spy.inside.append(_name)
            return _real(*a, **kw)

        monkeypatch.setattr(mod, name, wrap)
    monkeypatch.setattr(session, "call", call)
    yield spy


async def export_live_dsn(rig: PlacedSynth, work: Path) -> Path:
    """The live board as DSN the way `pcb_route_fill` exports it (board copy + project
    file + `kicad-cli pcb export specctra`), outside any edit."""
    from copperchat.kicad_tools.board.fill import _copy_side_files
    from copperchat.kicad_tools.cli import run_kicad_cli

    ctx = rig.turn("dsn")
    work.mkdir(parents=True, exist_ok=True)
    copy = await rig.session.save_copy("pcb", work / f"{ctx.project.pro_file.stem}.kicad_pcb")
    _copy_side_files(ctx, work)
    dsn = work / "live.dsn"
    result = await run_kicad_cli(
        ctx.settings, ["pcb", "export", "specctra", "-o", str(dsn), str(copy)], timeout=60, cwd=work
    )
    assert result.returncode == 0 and dsn.is_file(), result.stderr
    return dsn


async def gather_quiet(*aws: Any) -> list[Any]:
    return list(await asyncio.gather(*aws, return_exceptions=True))
