"""Plain-data builders for BoardModel tests (shared by every board phase)."""

from __future__ import annotations

from dataclasses import replace
from typing import Any

from copperchat.board.geom import rect_poly
from copperchat.board.model import (
    BoardModel,
    NetClassInfo,
    Outline,
    Part,
    Point,
    TextItem,
    Track,
    Via,
    Zone,
)


def outline(x: float = 0.0, y: float = 0.0, w: float = 100.0, h: float = 80.0) -> Outline:
    return Outline(
        kind="rect",
        x=x,
        y=y,
        w=w,
        h=h,
        corner_radius=0.0,
        polygon=tuple(rect_poly(x, y, w, h)),
    )


def part(
    ref: str,
    x: float = 50.0,
    y: float = 40.0,
    *,
    rot: float = 0.0,
    side: str = "top",
    locked: bool = False,
    half: float = 2.0,
    kiid: str | None = None,
    **overrides: Any,
) -> Part:
    """A part whose courtyard is a (2*half)-mm square around (x, y)."""
    fields: dict[str, Any] = {
        "ref": ref,
        "kiid": kiid if kiid is not None else f"kiid-{ref}",
        "lib_id": "Lib:FP",
        "value": "1k",
        "x": x,
        "y": y,
        "rot": rot,
        "side": side,
        "locked": locked,
        "courtyard": tuple(rect_poly(x - half, y - half, 2 * half, 2 * half)),
        "pads": (),
        "sheet": "/",
        "symbol_path": f"/sym-{ref}",
        "texts": (
            TextItem(
                kind="reference",
                text=ref,
                layer="F.SilkS",
                at=(x, y - half - 1.0),
                size=(1.0, 1.0),
                thickness=0.15,
                visible=True,
            ),
        ),
        "group": None,
        "rule_areas": (),
    }
    fields.update(overrides)
    return Part(**fields)


def track(
    net: str = "N1",
    layer: str = "F.Cu",
    start: Point = (10.0, 10.0),
    end: Point = (20.0, 10.0),
    width: float = 0.2,
    *,
    locked: bool = False,
    kiid: str = "kiid-track",
) -> Track:
    return Track(kiid=kiid, net=net, layer=layer, start=start, end=end, width=width, locked=locked)


def via(
    net: str = "N1",
    at: Point = (15.0, 15.0),
    diameter: float = 0.6,
    drill: float = 0.3,
    *,
    locked: bool = False,
    kiid: str = "kiid-via",
) -> Via:
    return Via(kiid=kiid, net=net, at=at, diameter=diameter, drill=drill, locked=locked)


def zone(
    name: str = "Z",
    net: str = "GND",
    layers: tuple[str, ...] = ("F.Cu",),
    poly: tuple[Point, ...] | None = None,
    *,
    priority: int = 0,
    rule_area: bool = False,
    keepout: frozenset[str] = frozenset(),
    settings: tuple[tuple[str, str], ...] = (("clearance", "0.2"),),
    filled_area_mm2: float = 0.0,
    kiid: str = "kiid-zone",
) -> Zone:
    return Zone(
        kiid=kiid,
        name=name,
        net=net,
        layers=layers,
        outline=poly if poly is not None else tuple(rect_poly(5, 5, 20, 20)),
        priority=priority,
        rule_area=rule_area,
        keepout=keepout,
        settings=settings,
        filled_area_mm2=filled_area_mm2,
    )


def net_class(
    name: str = "Default",
    track_width: float = 0.2,
    clearance: float = 0.2,
    patterns: tuple[str, ...] = (),
) -> NetClassInfo:
    return NetClassInfo(
        name=name,
        track_width=track_width,
        clearance=clearance,
        via_diameter=0.6,
        via_drill=0.3,
        diff_pair_width=None,
        diff_pair_gap=None,
        patterns=patterns,
    )


def board(**overrides: Any) -> BoardModel:
    fields: dict[str, Any] = {
        "parts": (part("R1", 30, 30), part("R2", 60, 50)),
        "tracks": (),
        "arcs": (),
        "vias": (),
        "zones": (),
        "graphics": (),
        "outline": outline(),
        "copper_layers": ("F.Cu", "B.Cu"),
        "layer_types": (("F.Cu", "signal"), ("B.Cu", "signal")),
        "rules": (("min_clearance", 0.127), ("min_track_width", 0.127)),
        "net_classes": (net_class(),),
        "net_class_of": (("N1", "Default"),),
        "groups": (),
        "dru_text": "",
        "stackup": (),
    }
    fields.update(overrides)
    return BoardModel(**fields)


def with_part(m: BoardModel, ref: str, **changes: Any) -> BoardModel:
    """`m` with part `ref` replaced by `replace(part, **changes)`."""
    return replace(m, parts=tuple(replace(p, **changes) if p.ref == ref else p for p in m.parts))
