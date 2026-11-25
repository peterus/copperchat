"""Circuit-level builders on top of `builders` (P3a classification and metrics tests).

Pad positions are explicit and predictable:

* `ic(ref, pins, at)` — the pins, in natural order of their numbers, form one row along +x:
  the k-th pin (0-based) sits at `(at[0] + k * pitch, at[1])` (default pitch 1 mm). The part
  origin is `at`; the courtyard is the pad row's bounding box grown by 1 mm.
* two-pad parts (`cap`, `res`, `ind`, `xtal`) — pad "1" at `(at[0] - 0.5, at[1])` on `net_a`,
  pad "2" at `(at[0] + 0.5, at[1])` on `net_b`; courtyard = 2 x 1 mm box around `at`.
* `conn(ref, pins, at)` — same row layout as `ic`.

Every builder accepts `sheet=` and any other `Part` field as a keyword override."""

from __future__ import annotations

from typing import Any

from copperchat.board.geom import rect_poly
from copperchat.board.model import BoardModel, Pad, Part, Point
from copperchat.kicad_tools.filtering import natural_key

from .builders import board, part


def pad(ref: str, number: str, net: str, at: Point, *, w: float = 0.6, h: float = 0.6) -> Pad:
    return Pad(
        ref=ref,
        number=number,
        net=net,
        x=at[0],
        y=at[1],
        w=w,
        h=h,
        rot=0.0,
        shape="rect",
        layers=frozenset({"F.Cu"}),
        smd=True,
        drill=None,
    )


def _bbox_courtyard(pads: tuple[Pad, ...], at: Point, grow: float) -> tuple[Point, ...]:
    xs = [p.x for p in pads] or [at[0]]
    ys = [p.y for p in pads] or [at[1]]
    x0, y0 = min(xs) - grow, min(ys) - grow
    return tuple(rect_poly(x0, y0, max(xs) - min(xs) + 2 * grow, max(ys) - min(ys) + 2 * grow))


def _row_part(
    ref: str, pins: dict[str, str], at: Point, lib_id: str, pitch: float, **kw: Any
) -> Part:
    numbers = sorted(pins, key=natural_key)
    pads = tuple(pad(ref, n, pins[n], (at[0] + k * pitch, at[1])) for k, n in enumerate(numbers))
    fields: dict[str, Any] = {
        "lib_id": lib_id,
        "pads": pads,
        "courtyard": _bbox_courtyard(pads, at, 1.0),
    }
    fields.update(kw)
    return part(ref, at[0], at[1], **fields)


def ic(
    ref: str, pins: dict[str, str], at: Point = (50.0, 40.0), *, pitch: float = 1.0, **kw: Any
) -> Part:
    """An IC: pins (pad number → net) in one row along +x, `pitch` mm apart, from `at`."""
    return _row_part(ref, pins, at, kw.pop("lib_id", "Package_SO:SOIC-8"), pitch, **kw)


def conn(
    ref: str, pins: dict[str, str], at: Point = (10.0, 40.0), *, pitch: float = 1.0, **kw: Any
) -> Part:
    """A connector, same row layout as `ic`."""
    return _row_part(ref, pins, at, kw.pop("lib_id", "Connector_Generic:Conn"), pitch, **kw)


def two_pad(ref: str, net_a: str, net_b: str, at: Point, lib_id: str, **kw: Any) -> Part:
    pads = (
        pad(ref, "1", net_a, (at[0] - 0.5, at[1])),
        pad(ref, "2", net_b, (at[0] + 0.5, at[1])),
    )
    fields: dict[str, Any] = {
        "lib_id": lib_id,
        "pads": pads,
        "courtyard": tuple(rect_poly(at[0] - 1.0, at[1] - 0.5, 2.0, 1.0)),
    }
    fields.update(kw)
    return part(ref, at[0], at[1], **fields)


def cap(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:C"), **kw)


def res(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:R"), **kw)


def ind(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Device:L"), **kw)


def xtal(ref: str, net_a: str, net_b: str, at: Point = (50.0, 50.0), **kw: Any) -> Part:
    return two_pad(ref, net_a, net_b, at, kw.pop("lib_id", "Crystal:Crystal_SMD"), **kw)


def circuit(*parts: Part, **kw: Any) -> BoardModel:
    """A board made of exactly `parts` (other fields from `builders.board`)."""
    return board(parts=tuple(parts), **kw)
