"""`sch_shift_sheet`: rigid translation of everything on one sheet (by an offset, or
centred in the page frame), connectivity unchanged.

Every item KiCad's API returns for the sheet (`GetItems` with every top-level schematic
type) is translated by one grid-snapped offset and written back in a single `Update`
stage (one commit). The translation is generic over the item protos: every absolute
`Vector2` is shifted (positions, wire ends, field anchors, sheet pins, shape points, text
box corners, table cells); extents (`size`, an image's `transform_origin_offset`) and a
symbol's library `definition` (relative to the symbol origin) are left alone. So no
item kind is left behind: groups (a list of member ids) have no coordinates, and an item
the walker finds no point in is refused before writing (`unmovable`).

Centring uses the default drawing sheet KiCad ships (10 mm frame margins, the title
block 110 x 34 mm in the bottom-right corner of the frame): the content box (bodies,
visible field and label texts, wires, sheet boxes, shapes, texts) is centred in the
frame, moved clear of the title block when it fits beside or above it. The result
must stay on the paper. Verification: the netlist partition stays identical
(`keep_partition`), as for `sch_move`.
"""

from __future__ import annotations

import io
import math
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    SheetRef,
    Stage,
    StageInput,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import copy_item, item_kind
from copperchat.kicad_tools.edit.geometry import (
    GRID_MM,
    NM,
    Box,
    Point,
    body_box,
    field_boxes,
    mm_point,
    text_box,
)
from copperchat.kicad_tools.edit.labels import label_box
from copperchat.kicad_tools.edit.orient import SPIN_DIR, label_fields
from copperchat.kicad_tools.edit.targets import bad

TOOL = "sch_shift_sheet"

# -- the translation --------------------------------------------------------------------

VECTOR2 = "kiapi.common.types.Vector2"
# Vector2 fields that are extents, not points (Box2 / BusEntry / SheetSymbol size, text
# size in TextAttributes, an image's offset of its transform origin, a custom page size).
EXTENT_FIELDS = frozenset({"size", "transform_origin_offset", "user_page_size"})
# Sub-messages in their own frame: a symbol's library definition (pins and graphics
# relative to the symbol origin).
RELATIVE_FIELDS = frozenset({"definition"})


def _vectors(msg: Any) -> Iterator[Any]:
    """Every absolute Vector2 sub-message of proto `msg`, depth-first."""
    for fd, value in msg.ListFields():
        if fd.message_type is None or fd.name in RELATIVE_FIELDS:
            continue
        if fd.message_type.GetOptions().map_entry:
            continue  # no map of positions exists in the schematic protos
        values = value if fd.label == fd.LABEL_REPEATED else [value]
        if fd.message_type.full_name == VECTOR2:
            if fd.name not in EXTENT_FIELDS:
                yield from values
            continue
        for v in values:
            yield from _vectors(v)


def translate_proto(msg: Any, dx_nm: int, dy_nm: int) -> int:
    """Shift every absolute Vector2 in proto `msg` (in place); returns how many."""
    count = 0
    for v in _vectors(msg):
        v.x_nm += dx_nm
        v.y_nm += dy_nm
        count += 1
    return count


def snap_offset(d: Sequence[float]) -> Point:
    """`d` rounded to the 1.27 mm connection grid (so on-grid pins and wires stay on it)."""
    return (_snap(d[0]), _snap(d[1]))


def _snap(v: float) -> float:
    return round(round(v / GRID_MM) * GRID_MM, 4) + 0.0  # + 0.0: no -0.0


def to_nm(d: Point) -> tuple[int, int]:
    return (round(d[0] * NM), round(d[1] * NM))


# -- the page ---------------------------------------------------------------------------

FRAME_MARGIN_MM = 10.0  # the default drawing sheet's left/right/top/bottom margins
TITLE_BLOCK_MM = (110.0, 34.0)  # its title block, in the bottom-right corner of the frame

_PAGE_MM: dict[str, tuple[float, float]] = {  # landscape width x height (page_info.cpp)
    "PS_A5": (210.0, 148.0),
    "PS_A4": (297.0, 210.0),
    "PS_A3": (420.0, 297.0),
    "PS_A2": (594.0, 420.0),
    "PS_A1": (841.0, 594.0),
    "PS_A0": (1189.0, 841.0),
    "PS_ANSI_A": (279.4, 215.9),
    "PS_ANSI_B": (431.8, 279.4),
    "PS_ANSI_C": (558.8, 431.8),
    "PS_ANSI_D": (863.6, 558.8),
    "PS_ANSI_E": (1117.6, 863.6),
    "PS_US_LETTER": (279.4, 215.9),
    "PS_US_LEGAL": (355.6, 215.9),
    "PS_US_LEDGER": (431.8, 279.4),
    "PS_GERBER": (812.8, 812.8),
}


@dataclass(frozen=True)
class Page:
    name: str
    paper: Box  # the whole sheet of paper
    frame: Box  # inside the frame margins: the drawing area
    title_block: Box
    custom_sheet: bool = False  # a non-default drawing sheet (its frame is assumed)

    def as_dict(self) -> dict[str, Any]:
        return {
            "size": self.name,
            "paper_mm": [round(self.paper.width, 2), round(self.paper.height, 2)],
            "frame_mm": _box_list(self.frame),
            "title_block_mm": _box_list(self.title_block),
        }


def page_of(size_name: str, w: float, h: float, *, custom_sheet: bool = False) -> Page:
    m = FRAME_MARGIN_MM
    frame = Box(m, m, w - m, h - m)
    tw, th = TITLE_BLOCK_MM
    title = Box(frame.x1 - tw, frame.y1 - th, frame.x1, frame.y1)
    return Page(size_name, Box(0.0, 0.0, w, h), frame, title, custom_sheet)


def read_page(settings: Any) -> Page:
    """`Page` of kipy `PageSettings` (portrait swaps the standard sizes)."""
    from kipy.proto.common.types import base_types_pb2 as bt

    name = bt.PageSize.Name(int(settings.page_size))
    custom = bool(str(settings.drawing_sheet))
    if name == "PS_USER":
        w, h = mm_point(settings.user_page_size)
    else:
        w, h = _PAGE_MM.get(name, _PAGE_MM["PS_A4"])
        if int(settings.orientation) == bt.PageOrientation.Value("PO_PORTRAIT"):
            w, h = h, w
    label = name.removeprefix("PS_") if name != "PS_USER" else "User"
    return page_of(label, w, h, custom_sheet=custom)


# -- the content box --------------------------------------------------------------------


def _box_list(b: Box) -> list[float]:
    return [round(b.x0, 2), round(b.y0, 2), round(b.x1, 2), round(b.y1, 2)]


def _pt_box(p: Point, r: float = 0.0) -> Box:
    return Box(p[0] - r, p[1] - r, p[0] + r, p[1] + r)


def _text_boxes(fields: Iterable[Any]) -> list[Box]:
    out = []
    for f in fields:
        if getattr(f, "visible", True):
            b = text_box(f.text)
            if b is not None:
                out.append(b)
    return out


def _shape_box(shape: Any) -> Box | None:
    from kipy.common_types import to_concrete_shape

    if shape is None:
        return None
    geometry = shape.proto.WhichOneof("geometry")
    if geometry in ("ellipse", "ellipse_arc"):  # kipy has no box for these
        e = getattr(shape.proto, geometry)
        c = (e.center.x_nm / NM, e.center.y_nm / NM)
        r = max(e.major_radius.value_nm, e.minor_radius.value_nm) / NM
        return _pt_box(c, r)  # contains the ellipse at any rotation
    concrete = to_concrete_shape(shape) or shape
    try:
        b = concrete.bounding_box()
    except Exception:  # a shape kind without a box: its points still count (below)
        return None
    # KiCad does not normalise a shape's corners: a rectangle drawn right-to-left has a
    # negative size here.
    return Box.around([(b.pos.x / NM, b.pos.y / NM),
                       ((b.pos.x + b.size.x) / NM, (b.pos.y + b.size.y) / NM)])  # fmt: skip


IMAGE_PPI = 300  # KiCad's default for a bitmap without resolution metadata
# What KiCad embeds as a schematic bitmap (wxImage handlers); only these parsers run.
IMAGE_FORMATS = ("PNG", "JPEG", "BMP", "GIF", "TIFF", "WEBP")


def _image_box(item: Any) -> Box | None:
    """The bitmap's extent (its position is its centre); None when it cannot be read."""
    from PIL import Image

    at = mm_point(item.position)
    try:
        with Image.open(io.BytesIO(item.image_data), formats=IMAGE_FORMATS) as img:
            px_w, px_h = img.size
            dpi = img.info.get("dpi", (IMAGE_PPI, IMAGE_PPI))
        ppi_x, ppi_y = float(dpi[0]), float(dpi[1])
    except (OSError, ValueError, TypeError, IndexError, Image.DecompressionBombError):
        return None
    scale = float(item.image_scale)
    ppi_x = ppi_x if ppi_x > 0 else IMAGE_PPI
    ppi_y = ppi_y if ppi_y > 0 else IMAGE_PPI
    scale = scale if scale > 0 else 1.0
    w = px_w / ppi_x * 25.4 * scale
    h = px_h / ppi_y * 25.4 * scale
    return Box(at[0] - w / 2, at[1] - h / 2, at[0] + w / 2, at[1] + h / 2)


def item_boxes(item: Any, unknown: list[str] | None = None) -> list[Box]:
    """What `item` covers on the page (bodies and visible texts as the scene estimates
    them; points for the rest). An item whose extent cannot be read (an undecodable
    image) counts as its anchor and is named in `unknown`."""
    kind = item_kind(item)
    if kind == "symbol":
        return [body_box(item, margin=0.0), *field_boxes(item)]
    if kind in ("wire", "bus", "line"):
        return [Box.around([mm_point(item.start), mm_point(item.end)])]
    if kind == "junction":
        return [_pt_box(mm_point(item.position), 0.5)]
    if kind == "no_connect":
        return [_pt_box(mm_point(item.position), 0.7)]
    if kind == "bus_entry":
        a = mm_point(item.position)
        s = mm_point(item.size)
        return [Box.around([a, (a[0] + s[0], a[1] + s[1])])]
    if kind.endswith("label"):
        at = mm_point(item.position)
        d = SPIN_DIR.get(int(item.spin_style), (1, 0)) if hasattr(item, "spin_style") else (1, 0)
        return [label_box(at, str(item.text.value), d, kind), *_text_boxes(label_fields(item))]
    if kind == "sheet":
        x, y = mm_point(item.position)
        w, h = mm_point(item.size)
        fields = [item.name_field, item.filename_field, *getattr(item, "user_fields", [])]
        return [Box(x, y, x + w, y + h).grow(1.27 if item.pins else 0.0), *_text_boxes(fields)]
    if kind == "text":
        b = text_box(item.text)
        return [b] if b is not None else [_pt_box(mm_point(item.position))]
    if kind == "text_box":
        return [Box.around([mm_point(item.top_left), mm_point(item.bottom_right)])]
    if kind in ("SchematicGraphicShape", "SchematicRuleArea"):
        b = _shape_box(item.shape)
        return [b] if b is not None else _point_boxes(item.proto)
    if kind == "SchematicImage":
        box = _image_box(item)
        if box is None:
            if unknown is not None:
                unknown.append(f"image {access.kiid(item)}")
            return [_pt_box(mm_point(item.position))]
        return [box]
    return _point_boxes(item.proto)  # tables and anything else: its points


def item_points(msg: Any) -> list[Point]:
    """Every absolute point in proto `msg` (what `translate_proto` shifts), in mm."""
    return [(v.x_nm / NM, v.y_nm / NM) for v in _vectors(msg)]


def _point_boxes(msg: Any) -> list[Box]:
    pts = item_points(msg)
    return [Box.around(pts)] if pts else []


def union(boxes: Iterable[Box]) -> Box | None:
    out: Box | None = None
    for raw in boxes:
        b = Box.around([(raw.x0, raw.y0), (raw.x1, raw.y1)])  # tolerate inverted boxes
        out = (
            b
            if out is None
            else Box(min(out.x0, b.x0), min(out.y0, b.y0), max(out.x1, b.x1), max(out.y1, b.y1))
        )
    return out


def shifted(b: Box, d: Point) -> Box:
    return Box(b.x0 + d[0], b.y0 + d[1], b.x1 + d[0], b.y1 + d[1])


def inside(b: Box, outer: Box, eps: float = 1e-6) -> bool:
    return (
        b.x0 >= outer.x0 - eps
        and b.y0 >= outer.y0 - eps
        and b.x1 <= outer.x1 + eps
        and b.y1 <= outer.y1 + eps
    )


def overlaps(a: Box, b: Box) -> bool:
    return a.x0 < b.x1 and b.x0 < a.x1 and a.y0 < b.y1 and b.y0 < a.y1


# -- centring ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Centring:
    offset: Point  # snapped
    clear_of_title_block: bool
    fits_frame: bool
    area: str  # "frame", "above the title block" or "left of the title block"


def _centre_in(content: Box, area: Box) -> Point:
    cx, cy = (content.x0 + content.x1) / 2, (content.y0 + content.y1) / 2
    ax, ay = (area.x0 + area.x1) / 2, (area.y0 + area.y1) / 2
    return snap_offset((ax - cx, ay - cy))


def _fit_axis(lo: float, hi: float, a0: float, a1: float, d: float) -> float:
    """`d` nudged by whole grid steps so [lo, hi] + d lies in [a0, a1] (when it fits)."""
    while lo + d < a0 - 1e-6 and hi + d + GRID_MM <= a1 + 1e-6:
        d = _snap(d + GRID_MM)
    while hi + d > a1 + 1e-6 and lo + d - GRID_MM >= a0 - 1e-6:
        d = _snap(d - GRID_MM)
    return d


def _centred(content: Box, area: Box) -> Point:
    """Centred in `area`, snapped, then nudged back inside where snapping pushed it out."""
    dx, dy = _centre_in(content, area)
    return (
        _fit_axis(content.x0, content.x1, area.x0, area.x1, dx),
        _fit_axis(content.y0, content.y1, area.y0, area.y1, dy),
    )


def plan_centring(content: Box, page: Page) -> Centring:
    """The snapped offset that centres `content` in the frame — clear of the title block
    when it fits there (the candidate closest to the frame centre), else centred over it."""
    frame, tb = page.frame, page.title_block
    d = _centred(content, frame)
    fits = content.width <= frame.width + 1e-6 and content.height <= frame.height + 1e-6
    if not fits:
        return Centring(d, not overlaps(shifted(content, d), tb), False, "frame")
    if not overlaps(shifted(content, d), tb):
        return Centring(d, True, True, "frame")
    candidates: list[tuple[str, Box]] = [
        ("above the title block", Box(frame.x0, frame.y0, frame.x1, tb.y0)),
        ("left of the title block", Box(frame.x0, frame.y0, tb.x0, frame.y1)),
    ]
    best: tuple[float, str, Point] | None = None
    for name, area in candidates:
        if content.width > area.width + 1e-6 or content.height > area.height + 1e-6:
            continue
        # Each area spans the frame on one axis: centred there as in the frame.
        cand = _centred(content, area)
        moved = shifted(content, cand)
        if overlaps(moved, tb) or not inside(moved, frame):
            continue
        dist = math.hypot(cand[0] - d[0], cand[1] - d[1])
        if best is None or dist < best[0]:
            best = (dist, name, cand)
    if best is None:
        return Centring(d, False, True, "frame")
    return Centring(best[2], True, True, best[1])


def overflow(b: Box, paper: Box) -> tuple[float, float, float, float]:
    """How far `b` sticks out of `paper` on the left, top, right and bottom (>= 0)."""
    return (
        max(0.0, paper.x0 - b.x0),
        max(0.0, paper.y0 - b.y0),
        max(0.0, b.x1 - paper.x1),
        max(0.0, b.y1 - paper.y1),
    )


def leaves_paper(before: Box, after: Box, paper: Box, eps: float = 1e-6) -> bool:
    """The shift pushes content (further) off the paper on some side. Content that was
    already off the paper may be shifted back towards it."""
    return any(a > b + eps for a, b in zip(overflow(after, paper), overflow(before, paper),
                                            strict=True))  # fmt: skip


def placement_notes(after: Box, page: Page, centring: Centring | None) -> list[str]:
    """What the result says about where the content ended up (`centring` None: by mode)."""
    notes: list[str] = []
    if centring is not None:
        if not centring.fits_frame:
            notes.append("the content is larger than the drawing frame: centred on it, it "
                         "reaches into the frame margins")  # fmt: skip
        elif not centring.clear_of_title_block:
            notes.append("the content cannot be centred clear of the title block: centred "
                         "in the frame, it overlaps the title block")  # fmt: skip
        elif centring.area != "frame":
            notes.append(f"centred {centring.area} (centred in the frame it would overlap "
                         "the title block)")  # fmt: skip
    else:
        if not inside(after, page.paper):
            notes.append("the content still reaches off the paper (less than before)")
        elif not inside(after, page.frame):
            notes.append("the shifted content reaches into the frame margins")
        if overlaps(after, page.title_block):
            notes.append("the shifted content overlaps the title block")
    if page.custom_sheet:
        notes.append("the page uses a custom drawing sheet: its frame and title block were "
                     "assumed to be KiCad's default ones")  # fmt: skip
    return notes


# -- the change set ---------------------------------------------------------------------


# Every top-level type GetItems accepts on a schematic sheet (api_handler_sch.cpp); sheet
# pins, fields and table cells travel inside their parents.
def sheet_item_types() -> list[int]:
    from kipy.proto.common.types.enums_pb2 import KiCadObjectType as T

    return [
        T.KOT_SCH_JUNCTION,
        T.KOT_SCH_NO_CONNECT,
        T.KOT_SCH_BUS_WIRE_ENTRY,
        T.KOT_SCH_BUS_BUS_ENTRY,
        T.KOT_SCH_LINE,
        T.KOT_SCH_SHAPE,
        T.KOT_SCH_RULE_AREA,
        T.KOT_SCH_BITMAP,
        T.KOT_SCH_TEXTBOX,
        T.KOT_SCH_TEXT,
        T.KOT_SCH_TABLE,
        T.KOT_SCH_LABEL,
        T.KOT_SCH_GLOBAL_LABEL,
        T.KOT_SCH_GROUP,
        T.KOT_SCH_HIER_LABEL,
        T.KOT_SCH_DIRECTIVE_LABEL,
        T.KOT_SCH_SYMBOL,
        T.KOT_SCH_SHEET,
    ]


NO_COORDS = frozenset({"Group"})  # kinds that carry no position of their own

_KIND_NAMES = {
    "SchematicGraphicShape": "shape",
    "SchematicRuleArea": "rule_area",
    "SchematicImage": "image",
    "SchematicTable": "table",
    "Group": "group",
}


def kind_name(item: Any) -> str:
    kind = item_kind(item)
    if kind == "symbol" and str(item.reference).startswith("#"):
        return "power_symbol"
    return _KIND_NAMES.get(kind, kind)


@dataclass
class ShiftPlan:
    items: list[Any]  # the shifted copies (to write)
    counts: dict[str, int]
    skipped: dict[str, int]  # kinds without coordinates (groups)


def plan_shift(items: Sequence[Any], d: Point) -> ShiftPlan:
    """Translated copies of `items`; refuses before writing when an item has no
    coordinate the translation could move (it would be left behind)."""
    dx, dy = to_nm(d)
    out: list[Any] = []
    counts: Counter[str] = Counter()
    skipped: Counter[str] = Counter()
    unmovable: list[str] = []
    for item in items:
        name = kind_name(item)
        if type(item).__name__ in NO_COORDS:
            skipped[name] += 1
            continue
        moved = copy_item(item)
        if translate_proto(moved.proto, dx, dy) == 0:
            unmovable.append(f"{name} {access.kiid(item)}")
            continue
        out.append(moved)
        counts[name] += 1
    if unmovable:
        raise bad(
            f"{TOOL}: some items cannot be shifted",
            f"no position found on {len(unmovable)} item(s): " + ", ".join(unmovable[:12]),
            "Move or delete these items by hand in KiCad, then retry.",
            unmovable=unmovable[:64],
        )
    return ShiftPlan(out, dict(sorted(counts.items())), dict(sorted(skipped.items())))


def shift_change_set(
    sheet: SheetRef,
    items: Sequence[Any],
    plan: ShiftPlan,
    summary: dict[str, Any],
    notes: list[str],
) -> ChangeSet:
    return ChangeSet(
        tool=TOOL,
        sheet=sheet,
        stages=[
            static_stage("shift sheet", Update(list(plan.items), sheet=sheet)),
            check_stage(plan.items),
        ],
        pre_images={access.kiid(i): copy_item(i) for i in items},
        intent=NetIntent(keep_partition=True),
        logical=Logical(1),
        summary=summary,
        notes=notes,
    )


_LINE_KINDS = frozenset({"wire", "bus", "line"})


def _nm(msg: Any) -> list[tuple[int, int]]:
    return [(v.x_nm, v.y_nm) for v in _vectors(msg)]


def shift_problems(sent: Sequence[Any], read_back: Sequence[Any]) -> list[str]:
    """Items KiCad did not take as sent: it marks a refused item in a batch update and
    carries on (kipy drops the per-item status), so the read-back must show every
    translated point. A line may have been merged with a collinear one by KiCad's cleanup:
    one of its ends is enough. Items missing from the read-back are the engine's
    (`modified item disappeared`)."""
    got = {access.kiid(i): i for i in read_back}
    out: list[str] = []
    for item in sent:
        uid = access.kiid(item)
        now = got.get(uid)
        if now is None:
            continue
        want, have = _nm(item.proto), _nm(now.proto)
        ok = bool(set(want) & set(have)) if item_kind(item) in _LINE_KINDS else want == have
        if not ok:
            out.append(f"{kind_name(item)} {uid}")
    return out


def check_stage(sent: Sequence[Any]) -> Stage:
    """Writes nothing: refuses (and so undoes the shift) when an item did not move."""

    def build(inp: StageInput) -> list[Any]:
        problems = shift_problems(sent, inp.modified)
        if problems:
            raise StructuredToolError(
                f"{TOOL}: KiCad did not move every item",
                f"{len(problems)} item(s) are not at their shifted position: "
                + ", ".join(problems[:12]),
                "The shift was undone; move these items by hand in KiCad, then retry.",
                code="verification_failed",
                details={"items": problems[:64]},
            )
        return []

    return Stage("check shift", build)
