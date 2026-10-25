"""sch_move(expect_net=...) (v0.5.0 #4, spec §7): a declared new connection passes the
verification; anything else is refused and rolled back with the actual result named.
Real KiCad."""

from __future__ import annotations

from typing import Any

import pytest

from copperchat.db import Database
from copperchat.kicad_session import KicadHandle, KicadSession
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.scene import netlists, resolve_sheet, sheet_api
from tests.kicad.edit.connect_support import pin_nets, state
from tests.kicad.edit.helpers import local_label, wire
from tests.support.edit import call_tool, edit_ctx, place_raw

pytestmark = pytest.mark.kicad


async def _ok(ctx: Any, tool: str, **args: Any) -> dict[str, Any]:
    out, err = await call_tool(ctx, tool, **args)
    assert not err, __import__("json").dumps(out, indent=1)
    return out


async def _commit(s: KicadSession, *items: Any) -> None:
    def run(h: KicadHandle) -> None:
        sheet_api(h, resolve_sheet(h, "/")).create_items(list(items))

    async with s.commit("fixture"):
        await s.call(run)


async def _net_of(s: KicadSession, uid: str) -> str | None:
    def run(h: KicadHandle) -> str | None:
        return netlists(h)[1].get((resolve_sheet(h, "/").path, uid))

    return await s.call(run)


async def _label_at(s: KicadSession, uid: str) -> tuple[float, float]:
    def run(h: KicadHandle) -> tuple[float, float]:
        lb = next(
            x for x in sheet_api(h, resolve_sheet(h, "/")).get_labels() if access.kiid(x) == uid
        )
        return (lb.position.x / 1e6, lb.position.y / 1e6)

    return await s.call(run)


async def _two_nets(s: KicadSession, ctx: Any) -> dict[str, Any]:
    """Wire A (63.5..88.9, y 63.5) labelled NA, wire B (63.5..88.9, y 88.9) labelled NB,
    and a floating label SIG at (76.2, 76.2) — none of them on a pin."""
    wa, wb = wire((63.5, 63.5), (88.9, 63.5)), wire((63.5, 88.9), (88.9, 88.9))
    na, nb = local_label("NA", (63.5, 63.5)), local_label("NB", (63.5, 88.9))
    sig = local_label("SIG", (76.2, 76.2))
    for lb in (na, nb, sig):
        lb.spin_style = 1  # LEFT: the text runs away from the wires
    await _commit(s, wa, wb, na, nb, sig)
    return {
        "wa": access.kiid(wa),
        "wb": access.kiid(wb),
        "sig": access.kiid(sig),
        "a": await _net_of(s, access.kiid(wa)),
        "b": await _net_of(s, access.kiid(wb)),
    }


async def test_expect_net_floating_label_onto_wire_passes(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    f = await _two_nets(s, ctx)
    out = await _ok(ctx, "sch_move", items=[f["sig"]], to=[76.2, 63.5], expect_net=f["a"])
    assert out["connectivity_verified"] is True
    assert await _net_of(s, f["sig"]) == await _net_of(s, f["wa"])
    assert await _net_of(s, f["wb"]) == f["b"]


async def test_expect_net_wrong_net_refused_and_rolled_back(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Onto wire A while declaring B: refused, the result named, everything as before
    (item set, partition, the label's place)."""
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    f = await _two_nets(s, ctx)
    before = await state(s)
    out, err = await call_tool(
        ctx, "sch_move", items=[f["sig"]], to=[76.2, 63.5], expect_net=f["b"]
    )
    assert err and out["error"]["code"] == "verification_failed", out
    why = out["error"]["why"]
    assert f"would not connect to {f['b']!r}" in why, why
    assert await state(s) == before
    assert await _label_at(s, f["sig"]) == (76.2, 76.2)
    assert await _net_of(s, f["wa"]) == f["a"]


async def test_expect_net_no_connection_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    f = await _two_nets(s, ctx)
    before = await state(s)
    out, err = await call_tool(
        ctx, "sch_move", items=[f["sig"]], to=[76.2, 78.74], expect_net=f["a"]
    )
    assert err and out["error"]["code"] == "verification_failed", out
    assert "would not connect" in out["error"]["why"]
    assert await state(s) == before
    assert await _label_at(s, f["sig"]) == (76.2, 76.2)


async def test_expect_net_unknown_name_invalid_argument(
    blank_session: KicadSession, edit_db: Database
) -> None:
    s = blank_session
    ctx = edit_ctx(s, edit_db)
    f = await _two_nets(s, ctx)
    before = await state(s)
    out, err = await call_tool(
        ctx, "sch_move", items=[f["sig"]], to=[76.2, 63.5], expect_net="NOPE"
    )
    assert err and out["error"]["code"] == "invalid_argument", out
    assert await state(s) == before


async def test_move_without_expect_net_unchanged(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Without expect_net the partition must stay: a label NB dropped onto wire A joins A
    and B (pins of both) -> refused as before; with expect_net=A it is the declared join."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    f = await _two_nets(s, ctx)
    # a part on each wire's right end, so the two nets have pins
    await place_raw(s, "Device:R", (88.9, 59.69), "R1")  # pin 2 at (88.9, 63.5)
    await place_raw(s, "Device:R", (88.9, 92.71), "R2")  # pin 1 at (88.9, 88.9)
    nb2 = local_label("NB", (101.6, 76.2))
    nb2.spin_style = 3
    await _commit(s, nb2)
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[access.kiid(nb2)], to=[80.01, 63.5])
    assert err and out["error"]["code"] == "verification_failed", out
    assert await state(s) == before
    a = await _net_of(s, f["wa"])
    out = await _ok(ctx, "sch_move", items=[access.kiid(nb2)], to=[80.01, 63.5], expect_net=a)
    assert out["connectivity_verified"] is True


async def test_expect_net_two_pins_onto_target_refused(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """Review Focus 1: a resistor whose two (unconnected, so two different) pin nets
    would both land on the target net's U-shaped wire: that shorts them -> refused as a
    verification failure and rolled back."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    out = await _ok(ctx, "sch_place_symbol", lib_id="Device:R", at=[101.6, 101.6], rotation=90)
    ref = out["placed"]["ref"]
    pins = [(await _ok(ctx, "sch_get_pin", pin=f"{ref}.{n}"))["at_mm"] for n in ("1", "2")]
    (x1, y), (x2, _y) = sorted(pins)
    top = y - 25.4 - 15.24  # the label TN well clear of the part
    u = [
        wire((x1, y - 25.4), (x1, top)),
        wire((x1, top), (x2, top)),
        wire((x2, top), (x2, y - 25.4)),
    ]
    tn = local_label("TN", (x1, top))
    tn.spin_style = 1
    await _commit(s, *u, tn)
    target = await _net_of(s, access.kiid(u[1]))
    before = await state(s)
    out, err = await call_tool(ctx, "sch_move", items=[ref], by=[0, -25.4], expect_net=target)
    assert err and out["error"]["code"] == "verification_failed", out
    assert "not one" in out["error"]["why"], out
    assert await state(s) == before


async def test_expect_net_pin_onto_wire_end_passes(
    blank_session: KicadSession, edit_db: Database
) -> None:
    """A part's pin moved onto the end of the declared net's wire: the intended join,
    not a placement clash."""
    s = blank_session
    await s.ensure_symbol_libraries()
    ctx = edit_ctx(s, edit_db)
    await place_raw(s, "Device:R", (127.0, 63.5), "R1")  # pins (127, 59.69) / (127, 67.31)
    w = wire((127.0, 44.45), (127.0, 54.61))
    tgt = local_label("TGT", (127.0, 44.45))
    tgt.spin_style = 2
    await _commit(s, w, tgt)
    target = await _net_of(s, access.kiid(w))
    assert target
    out = await _ok(ctx, "sch_move", items=["R1"], by=[0, -5.08], expect_net=target)
    assert out["connectivity_verified"] is True
    nets = await pin_nets(s)
    assert nets["R1.1"] == await _net_of(s, access.kiid(w))
