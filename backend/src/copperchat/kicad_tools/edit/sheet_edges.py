"""One sheet-edge rule for every tool that writes wires (#9, spec §8).

No wire segment may cross a sheet symbol, lie on its edge or run parallel to an edge
closer than SHEET_EDGE_CLEARANCE_MM. The only contact allowed: a segment that ends on a
sheet pin, coming in perpendicular to the pin's edge from outside. sch_add_wire,
sch_reroute and sch_connect's router (via `router.Obstacles`) all ask this module, so
what one tool draws the others accept. Existing wires that break the rule are only
reported (`edge_warnings`), never refused.
"""

from __future__ import annotations

import itertools
from collections.abc import Sequence
from dataclasses import dataclass

from copperchat.kicad_tools.edit.geometry import EPS_MM, Box, Point, on_segment, same_point
from copperchat.kicad_tools.edit.scene import Scene

SHEET_EDGE_CLEARANCE_MM = 2.54


@dataclass(frozen=True)
class SheetBox:
    uuid: str
    name: str  # '' when unknown (a sheet without pins carries no name in the scene)
    box: Box

    def label(self) -> str:
        if self.name:
            return f"sheet {self.name}"
        return f"the sheet symbol at ({self.box.x0:g}, {self.box.y0:g})"


def sheet_boxes(scene: Scene) -> list[SheetBox]:
    names = {i.sheet_uuid: i.pin.ref for i in scene.sheet_pins.values()}
    return [SheetBox(uid, names.get(uid, ""), box) for uid, box in scene.sheets.items()]


def sheet_pin_points(scene: Scene) -> list[Point]:
    return [i.pin.at for i in scene.sheet_pins.values()]


def _fmt(p: Point) -> str:
    return f"({p[0]:g}, {p[1]:g})"


def _one(a: Point, b: Point, s: SheetBox, pins: Sequence[Point]) -> list[str]:
    box, what = s.box, s.label()
    horizontal = abs(a[1] - b[1]) < EPS_MM
    vertical = abs(a[0] - b[0]) < EPS_MM
    if not (horizontal or vertical):  # diagonal: refused elsewhere; keep it off the band
        if box.grow(SHEET_EDGE_CLEARANCE_MM - EPS_MM).hit_by_segment(a, b, eps=0.0):
            return [f"segment {_fmt(a)}-{_fmt(b)} comes too close to {what}"]
        return []
    if horizontal:
        fixed, lo, hi = a[1], *sorted((a[0], b[0]))
        edges = (("top", box.y0), ("bottom", box.y1))
        span = (box.x0, box.x1)
        cross = (("left", box.x0), ("right", box.x1))
        cspan = (box.y0, box.y1)
    else:
        fixed, lo, hi = a[0], *sorted((a[1], b[1]))
        edges = (("left", box.x0), ("right", box.x1))
        span = (box.y0, box.y1)
        cross = (("top", box.y0), ("bottom", box.y1))
        cspan = (box.x0, box.x1)
    out: list[str] = []
    seg = f"segment {_fmt(a)}-{_fmt(b)}"
    inside = cspan[0] + EPS_MM < fixed < cspan[1] - EPS_MM
    if inside and lo < span[1] - EPS_MM and hi > span[0] + EPS_MM:
        out.append(f"{seg} crosses {what}")
    for side, e in edges:
        gap = abs(fixed - e)
        if gap < SHEET_EDGE_CLEARANCE_MM - EPS_MM and min(hi, span[1]) - max(lo, span[0]) > EPS_MM:
            if gap < EPS_MM:
                out.append(f"{seg} runs on the {side} edge of {what}")
            else:
                out.append(
                    f"{seg} runs {gap:g} mm from the {side} edge of {what} "
                    f"(keep at least {SHEET_EDGE_CLEARANCE_MM:g} mm)"
                )
    if cspan[0] - EPS_MM <= fixed <= cspan[1] + EPS_MM:
        for side, e in cross:
            if not lo - EPS_MM <= e <= hi + EPS_MM:
                continue
            at: Point = (e, fixed) if horizontal else (fixed, e)
            if any(same_point(at, p) for p in pins):
                continue  # perpendicular onto a sheet pin; running on through it 'crosses'
            out.append(f"{seg} touches the {side} edge of {what} at {_fmt(at)} (no sheet pin)")
    return out


def sheet_edge_problems(
    segment: tuple[Point, Point], sheets: Sequence[SheetBox], *, sheet_pins: Sequence[Point]
) -> list[str]:
    """Why `segment` breaks the sheet-edge rule (empty = it keeps at least
    SHEET_EDGE_CLEARANCE_MM from every sheet edge, or touches an edge only at a sheet pin,
    perpendicular to it)."""
    a, b = segment
    out: list[str] = []
    for s in sheets:
        out += _one(a, b, s, sheet_pins)
    return out


def path_edge_problems(
    points: Sequence[Point], sheets: Sequence[SheetBox], *, sheet_pins: Sequence[Point]
) -> list[str]:
    out: list[str] = []
    for seg in itertools.pairwise(points):
        out += sheet_edge_problems(seg, sheets, sheet_pins=sheet_pins)
    return out


def edge_warnings(scene: Scene, points: Sequence[Point]) -> list[str]:
    """Existing wires of `scene` that break the rule and touch the new path at a point:
    they stay as they are, the result only names them (pass a scene without wires the
    edit replaces)."""
    sheets = sheet_boxes(scene)
    if not sheets:
        return []
    pins = sheet_pin_points(scene)
    segs = list(itertools.pairwise(points))
    out: list[str] = []
    for w in scene.wires.values():
        touches = any(on_segment(p, a, b) for p in (w.start, w.end) for a, b in segs) or any(
            on_segment(p, w.start, w.end) for p in points
        )
        if not touches:
            continue
        problems = sheet_edge_problems((w.start, w.end), sheets, sheet_pins=pins)
        if problems:
            out.append(
                f"existing wire {w.uuid} breaks the sheet-edge rule: {problems[0]}; "
                "consider sch_reroute"
            )
    return out
