"""Symbol fields (E4): readable Reference/Value placement and field edits.

Headless placement does no field auto-placement (spike 2A Q2: the library default puts
the value on the body centre), so CopperChat places Reference and Value itself (port of
spike 2D `_place_fields`): stacked on a side of the body without pins, horizontal as
displayed, centred (KiCad transforms a field's justification with the symbol; a centred
field looks the same in every transform). Field edits always go to every unit of a
reference (2A Q2: the API does not sync units).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.edit.geometry import GRID_MM, NM, Box, Point, mm_point

FIELD_SIZE_MM = 1.27  # KiCad's default field text size
CHAR_W = 0.6  # stroke font: ~0.6 em per character (as geometry._field_box)
MAX_FIELDS = 32
MAX_FIELD_NAME = 64
MAX_FIELD_VALUE = 1024
# Field names the agent may use in `fields`, mapped to the KiCad field they set.
STANDARD = {
    "value": "Value",
    "footprint": "Footprint",
    "datasheet": "Datasheet",
    "description": "Description",
}
ALIASES = {"mpn": "MPN", "manufacturer": "Manufacturer"}
RESERVED = {"reference", "ref"}
_NAME_RE = re.compile(r"^[^\x00-\x1f\x7f]+$")


# -- layout -------------------------------------------------------------------------------


@dataclass(frozen=True)
class FieldSpot:
    at: Point  # text centre, absolute mm
    angle: float  # field angle as stored (relative to the symbol: 90 on a rotated part)


def text_width(text: str, size: float = FIELD_SIZE_MM) -> float:
    return CHAR_W * size * max(len(text), 1)


def spot_box(spot: FieldSpot, text: str, size: float = FIELD_SIZE_MM) -> Box:
    """The box of a laid-out field as displayed (always horizontal, centred)."""
    w, h = text_width(text, size), size
    return Box(spot.at[0] - w / 2, spot.at[1] - h / 2, spot.at[0] + w / 2, spot.at[1] + h / 2)


def field_layout(
    body: Box, pin_dirs: Iterable[str], rotation: int, texts: tuple[str, str]
) -> tuple[FieldSpot, FieldSpot]:
    """Reference and Value positions for a body box (no margin) whose pins point in
    `pin_dirs` (outward, as placed): a side without pins, preferring above/below for
    wide parts and right/left for tall ones; a corner above-right when every side has
    pins. Both texts horizontal as displayed, Reference first (above / left column)."""
    used = set(pin_dirs)
    wide = body.width >= body.height
    order = ["up", "down", "right", "left"] if wide else ["right", "left", "up", "down"]
    side = next((d for d in order if d not in used), "corner")
    cx, cy = (body.x0 + body.x1) / 2, (body.y0 + body.y1) / 2
    ref, value = texts
    half = {t: text_width(t) / 2 for t in texts}
    gap = GRID_MM / 2
    line = 2 * GRID_MM  # 2.54 mm between the two lines
    if side == "up":
        spots = [
            (cx, body.y0 - gap - FIELD_SIZE_MM / 2 - line),
            (cx, body.y0 - gap - FIELD_SIZE_MM / 2),
        ]
    elif side == "down":
        spots = [
            (cx, body.y1 + gap + FIELD_SIZE_MM / 2),
            (cx, body.y1 + gap + FIELD_SIZE_MM / 2 + line),
        ]
    elif side == "right":
        spots = [
            (body.x1 + gap + half[ref], cy - line / 2),
            (body.x1 + gap + half[value], cy + line / 2),
        ]
    elif side == "left":
        spots = [
            (body.x0 - gap - half[ref], cy - line / 2),
            (body.x0 - gap - half[value], cy + line / 2),
        ]
    else:
        top = body.y0 - gap - FIELD_SIZE_MM / 2
        spots = [(body.x1 + gap + half[ref], top - line), (body.x1 + gap + half[value], top)]
    angle = 90.0 if rotation % 180 == 90 else 0.0
    r, v = ((round(x, 4), round(y, 4)) for x, y in spots)
    return FieldSpot(r, angle), FieldSpot(v, angle)


def apply_layout(sym: Any, spots: tuple[FieldSpot, FieldSpot]) -> None:
    """Write Reference/Value positions onto a kipy symbol (centred, visible as before)."""
    from kipy.geometry import Vector2
    from kipy.proto.common.types import HorizontalAlignment, VerticalAlignment

    for f, spot in zip((sym.reference_field, sym.value_field), spots, strict=True):
        attrs = f.text.attributes
        attrs.horizontal_alignment = HorizontalAlignment.HA_CENTER
        attrs.vertical_alignment = VerticalAlignment.VA_CENTER
        attrs.angle = spot.angle
        f.text.position = Vector2.from_xy(round(spot.at[0] * NM), round(spot.at[1] * NM))


def laid_out_boxes(sym: Any, spots: tuple[FieldSpot, FieldSpot]) -> list[Box]:
    out = []
    for f, spot in zip((sym.reference_field, sym.value_field), spots, strict=True):
        if f.visible and str(f.text.value):
            out.append(spot_box(spot, str(f.text.value)))
    return out


# -- field edits --------------------------------------------------------------------------


def _bad(what: str, why: str, suggestion: str) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _utf8(text: str) -> bool:
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def check_text(value: Any, name: str, limit: int) -> str:
    """One line of printable text (no control characters), encodable as UTF-8."""
    ok = isinstance(value, str) and len(value) <= limit and not _CONTROL.search(value)
    if ok:
        try:
            value.encode("utf-8")
        except UnicodeEncodeError:
            ok = False
    if not ok:
        raise _bad(
            f"Invalid {name}",
            f"{name} must be one line of at most {limit} printable characters",
            "Pass plain text without line breaks or control characters.",
        )
    return str(value)


def normalize_fields(raw: Any, *, arg: str = "fields") -> dict[str, str]:
    """`{name: value}` from the agent -> KiCad field names (`value` -> Value, `mpn` ->
    MPN, ...); custom names kept as given. Reference cannot be set here."""
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise _bad(
            f"{arg} must be an object",
            f"{arg} was {type(raw).__name__}",
            'Pass {"value": "10k", "footprint": "Resistor_SMD:R_0805_2012Metric"}.',
        )
    if len(raw) > MAX_FIELDS:
        raise _bad(
            f"Too many fields ({len(raw)})",
            f"at most {MAX_FIELDS} per call",
            "Split the edit into several calls.",
        )
    out: dict[str, str] = {}
    for name, value in raw.items():
        key = str(name).strip()
        if not key or len(key) > MAX_FIELD_NAME or not _NAME_RE.match(key) or not _utf8(key):
            raise _bad(
                f"Invalid field name {key[:40]!r}",
                f"field names are 1..{MAX_FIELD_NAME} printable characters",
                "Use a plain name like 'MPN' or 'Tolerance'.",
            )
        if key.lower() in RESERVED:
            raise _bad(
                "The reference cannot be set as a field",
                "references are assigned by the annotation",
                "Use sch_annotate to renumber references.",
            )
        if not isinstance(value, str):
            raise _bad(
                f"Invalid value for field {key}",
                "field values are text",
                "Pass a string (an empty string clears the field's text).",
            )
        text = check_text(value, f"field {key}", MAX_FIELD_VALUE)
        canon = STANDARD.get(key.lower()) or ALIASES.get(key.lower()) or key
        if canon in out:
            raise _bad(
                f"Field {canon} given twice",
                f"{key!r} names the same field",
                "Pass each field once.",
            )
        out[canon] = text
    return out


def _std_field(sym: Any, name: str) -> Any:
    return {
        "Value": sym.value_field,
        "Footprint": sym.footprint_field,
        "Datasheet": sym.datasheet_field,
        "Description": sym.description_field,
    }[name]


def field_values(sym: Any) -> dict[str, str]:
    """Every field of a placed symbol except Reference: {name: text}."""
    out = {name: str(_std_field(sym, name).text.value) for name in STANDARD.values()}
    for f in sym.user_fields:
        out[str(f.name)] = str(f.text.value)
    return out


def _find_user(user: list[Any], name: str) -> Any | None:
    hit = next((f for f in user if str(f.name) == name), None)
    if hit is None:
        hit = next((f for f in user if str(f.name).lower() == name.lower()), None)
    return hit


def apply_fields(
    sym: Any,
    fields: Mapping[str, str],
    remove: Iterable[str] = (),
    *,
    template: Mapping[str, tuple[Point, bool]] | None = None,
) -> None:
    """Set field texts on a kipy symbol; a new custom field is hidden at the symbol's
    position (or at `template[name]` = (offset from the symbol, visible), used to copy a
    sibling unit's layout). Existing fields keep their position and visibility."""
    from kipy.geometry import Vector2
    from kipy.schematic_types import SchematicField

    user = [SchematicField(proto=f.proto) for f in sym.user_fields]  # detached copies
    for name in remove:
        hit = _find_user(user, name)
        if hit is not None:
            user.remove(hit)
    px, py = mm_point(sym.position)
    for name, text in fields.items():
        if name in STANDARD.values():
            _std_field(sym, name).text.value = text
            continue
        hit = _find_user(user, name)
        if hit is not None:
            hit.text.value = text
            continue
        f = SchematicField()
        f.name = name
        f.text.value = text
        offset, visible = (template or {}).get(name, ((0.0, 0.0), False))
        f.text.position = Vector2.from_xy(
            round((px + offset[0]) * NM), round((py + offset[1]) * NM)
        )
        size = round(FIELD_SIZE_MM * NM)
        f.text.attributes.size = Vector2.from_xy(size, size)
        f.visible = visible
        user.append(f)
    sym.user_fields = user


def field_template(sym: Any) -> dict[str, tuple[Point, bool]]:
    """Custom fields of a symbol as (offset from the symbol, visible), to copy a layout."""
    px, py = mm_point(sym.position)
    out: dict[str, tuple[Point, bool]] = {}
    for f in sym.user_fields:
        x, y = mm_point(f.text.position)
        out[str(f.name)] = ((round(x - px, 4), round(y - py, 4)), bool(f.visible))
    return out


FLAGS = {
    "dnp": "do_not_populate",
    "exclude_from_bom": "exclude_from_bill_of_materials",
    "exclude_from_board": "exclude_from_board",
}


def flag_values(sym: Any) -> dict[str, bool]:
    attrs = sym.attributes
    return {name: bool(getattr(attrs, attr)) for name, attr in FLAGS.items()}


def apply_flags(sym: Any, flags: Mapping[str, bool]) -> None:
    attrs = sym.attributes
    for name, value in flags.items():
        setattr(attrs, FLAGS[name], bool(value))
