"""`fp_create` input and target geometry (spec §7.1, §7.6; F4a Task 2).

`parse_create_args` validates the tool input against the schema limits (author call 18:
coordinates |x|,|y| <= 500 mm, sizes 0 < s <= 200 mm, drills 0 < d <= 50 mm, finite
angles, pad numbers <= 16 chars, layers from a fixed pattern, extra field names after NFKC
+ whitespace collapse) and returns a typed `CreateArgs`. `build_target` turns it into the
`FootprintGeometry` the write engine must produce: pads with default layers by type, the
courtyard, the F.Fab body (pin-1 chamfer), the silkscreen (body outline clipped clear of
copper, pin-1 mark), the Reference/Value/`${REFERENCE}` texts, the fields and the 3D model
link. Pure; no KiCad, no file access.

Units mm, footprint coordinates (origin = anchor, x right, y down), angles in degrees.
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.footprints import geom
from copperchat.kicad_tools.footprints.model import (
    RESERVED_FIELDS,
    REVIEW_FIELD,
    SOURCE_FIELD,
    Drill,
    FootprintGeometry,
    Model3D,
    Pad,
    Shape,
    Text,
)
from copperchat.kicad_tools.footprints.read import (  # one rule (author call 18)
    MAX_COORD_MM,
    MAX_DRILL_MM,
    MAX_PAD_NUMBER_CHARS,
    MAX_SIZE_MM,
    MAX_SYMBOL_CHARS,
)
from copperchat.library.local import NAME_RE, LocalLibrary

Point = tuple[float, float]
Box = tuple[float, float, float, float]
Vec3 = tuple[float, float, float]

# -- input limits (spec §7.6, author call 18) ---------------------------------------------
MAX_NAME_CHARS = 100
MAX_PADS = 512
MAX_SILK_SEGMENTS = 256
MAX_EXTRA_FIELDS = 16
MAX_FIELD_NAME_CHARS = 64
MAX_FIELD_VALUE_CHARS = 1024
MAX_DESCRIPTION_CHARS = 1024
MAX_KEYWORDS_CHARS = 1024
MAX_DOCUMENT_PATH_CHARS = 400  # a repo path (repo_context MAX_PATH_LEN)
MAX_DOCUMENT_URL_CHARS = 2048
MAX_LOCATION_CHARS = 200
MAX_MPN_CHARS = 64
MAX_MODEL_PATH_CHARS = 400
MAX_PAD_LAYERS = 8
MAX_ANGLE_DEG = 360.0
MAX_MARGIN_MM = 10.0
MAX_ROUNDRECT_RATIO = 0.5
MAX_MODEL_SCALE = 100.0

PAD_TYPES = ("smd", "tht", "npth", "connector")
PAD_SHAPES = ("rect", "roundrect", "circle", "oval")
DRILL_SHAPES = ("circle", "oval")
MOUNTINGS = ("smd", "tht")
PAD_LAYER_RE = re.compile(r"^(?:F|B|\*)\.(?:Cu|Mask|Paste)\Z")
FIELD_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}\Z")
# A source document that starts with a URL scheme is a URL (only http(s) is accepted).
URL_SCHEME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
# ${KIPRJMOD}/... or ${KICAD<n>_3DMODEL_DIR}/... with n in 6..11 (spec §7.6)
MODEL_PATH_RE = re.compile(r"^\$\{(KIPRJMOD|KICAD(?:[6-9]|1[01])_3DMODEL_DIR)\}/(.+)\Z", re.S)
MODEL_SUFFIXES = (".step", ".stp", ".wrl", ".wrz")

# -- build rules (spec §7.1, §7.6) -------------------------------------------------------
DEFAULT_LAYERS: dict[str, tuple[str, ...]] = {
    "smd": ("F.Cu", "F.Mask", "F.Paste"),
    "tht": ("*.Cu", "*.Mask"),
    "npth": ("*.Cu", "*.Mask"),
    "connector": ("F.Cu", "F.Mask"),
}
DEFAULT_ROUNDRECT_RATIO = 0.25  # KiCad's default
DEFAULT_MARGIN = 0.25
COURTYARD_GRID = 0.01  # an auto courtyard is rounded outward to this
CRTYD_WIDTH = 0.05
FAB_WIDTH = 0.10
SILK_WIDTH = 0.12
SILK_CLEARANCE = 0.2  # edge of the body-outline silk to copper
SILK_MIN_PIECE = SILK_WIDTH  # shorter clipped outline pieces are dropped
PIN1_CLEARANCE = 0.12  # edge of the pin-1 silk mark to pad 1
CHAMFER_MAX = 1.0
CHAMFER_RATIO = 0.25
TEXT_OFFSET = 1.0  # Reference above / Value below the courtyard
TEXT_SIZE = 1.0
TEXT_THICKNESS = 0.15  # Reference / Value; the ${REFERENCE} text uses 0.15 x its size
USER_TEXT_MIN = 0.25
USER_TEXT_MAX = 1.0
REFERENCE_PLACEHOLDER = "REF**"
USER_TEXT = "${REFERENCE}"
DATASHEET_FIELD = "Datasheet"
_MICRO = 1e-6


def user_text_thickness(size: float) -> float:
    """Stroke thickness of the `${REFERENCE}` text (KiCad library convention)."""
    return round(TEXT_THICKNESS * size, 6)


# -- typed input ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RectSpec:
    size: Point
    center: Point


@dataclass(frozen=True)
class SourceSpec:
    document: str
    location: str
    mpn: str | None
    url: bool  # an http(s) URL (else a repo path, resolved in prepare)


@dataclass(frozen=True)
class ModelSpec:
    path: str
    variable: str  # KIPRJMOD or KICAD<n>_3DMODEL_DIR
    rel: str  # the path below the variable
    offset: Vec3
    rotation: Vec3
    scale: Vec3


@dataclass(frozen=True)
class CreateArgs:
    name: str
    description: str
    keywords: str
    mounting: Literal["smd", "tht"]
    pads: tuple[Pad, ...]
    body: RectSpec
    courtyard: RectSpec | None  # None: `margin` around copper + body
    margin: float
    silk: tuple[tuple[Point, Point], ...] | None  # None: auto
    pin1_marker: bool
    fields: tuple[tuple[str, str], ...]  # extra fields, normalised names, input order
    datasheet: str | None
    model: ModelSpec | None
    source: SourceSpec
    symbol: str | None
    replace: bool


# -- validation helpers ---------------------------------------------------------------------


def invalid(what: str, why: str, suggestion: str = "See the tool schema.") -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code="invalid_argument")


def source_error() -> StructuredToolError:
    """The one error for every bad `source` (spec §7.6): absolute, blocked and missing
    paths look the same, so the check cannot probe for files."""
    return invalid(
        "Invalid source",
        "source.document must be the relative path of an existing regular file in this "
        "repository (not a blocked file) or an http(s) URL, and source.location must say "
        "where the land pattern is (page, figure or table)",
        "e.g. {'document': 'docs/parts/SRN3012BTA/datasheet.pdf', 'location': 'p.2, "
        "Recommended layout'}; list repo files with repo_list.",
    )


def _keys(value: Any, what: str, allowed: set[str], required: set[str]) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise invalid(f"Invalid {what}", f"{what} must be an object")
    unknown = sorted(str(k) for k in set(value) - allowed)
    if unknown:
        raise invalid(f"Invalid {what}", f"unknown key(s) {unknown}", f"allowed: {sorted(allowed)}")
    missing = sorted(required - set(value))
    if missing:
        raise invalid(f"Invalid {what}", f"missing key(s) {missing}")
    return value


def _float(value: Any) -> float:
    """A JSON number as float; NaN for non-numbers, bools and ints too large for a float."""
    if not isinstance(value, int | float) or isinstance(value, bool):
        return math.nan
    try:
        return float(value)
    except OverflowError:
        return math.nan


def _number(value: Any, what: str, lo: float, hi: float, *, open_lo: bool = False) -> float:
    v = _float(value)
    if not (math.isfinite(v) and (v > lo if open_lo else v >= lo) and v <= hi):
        bound = f"{lo} < {what} <= {hi}" if open_lo else f"{lo} <= {what} <= {hi}"
        raise invalid(f"Invalid {what}", f"{what} must be a finite number, {bound}", bound)
    return v + 0.0


def _vec(value: Any, what: str, n: int, lo: float, hi: float, *, open_lo: bool = False) -> Any:
    if not isinstance(value, list | tuple) or len(value) != n:
        raise invalid(f"Invalid {what}", f"{what} must be a list of exactly {n} numbers")
    return tuple(_number(v, what, lo, hi, open_lo=open_lo) for v in value)


def _xy(value: Any, what: str) -> Point:
    return _vec(value, what, 2, -MAX_COORD_MM, MAX_COORD_MM)  # type: ignore[no-any-return]


def _size(value: Any, what: str, hi: float = MAX_SIZE_MM) -> Point:
    return _vec(value, what, 2, 0.0, hi, open_lo=True)  # type: ignore[no-any-return]


def _string(value: Any, what: str, max_chars: int, *, min_chars: int = 0) -> str:
    if not isinstance(value, str) or not min_chars <= len(value) <= max_chars:
        raise invalid(
            f"Invalid {what}", f"{what} must be a string of {min_chars}..{max_chars} characters"
        )
    return value


def _choice(value: Any, what: str, choices: Sequence[str]) -> str:
    if value not in choices:
        raise invalid(f"Invalid {what}", f"{what} must be one of {list(choices)}")
    return str(value)


def _name(value: Any) -> str:
    if not isinstance(value, str) or not NAME_RE.match(value):
        shown = value[:80] if isinstance(value, str) else value
        raise invalid(
            f"Invalid footprint name {shown!r}",
            "footprint names match ^[A-Za-z0-9][A-Za-z0-9_.+-]{0,99}$ (no spaces, no "
            "path separators)",
            "Use KiCad-style names, e.g. L_Bourns_SRN3012BTA.",
        )
    return value


def _pad(value: Any, i: int) -> Pad:
    what = f"pads[{i}]"
    allowed = {"number", "type", "shape", "at", "rotation", "size", "roundrect_ratio", "drill",
               "layers"}  # fmt: skip
    p = _keys(value, what, allowed, {"number", "type", "shape", "at", "size"})
    kind = _choice(p["type"], f"{what}.type", PAD_TYPES)
    shape = _choice(p["shape"], f"{what}.shape", PAD_SHAPES)
    number = _string(p["number"], f"{what}.number", MAX_PAD_NUMBER_CHARS)
    if not number and kind != "npth":
        raise invalid(f"Invalid {what}.number", "only an NPTH pad may have an empty number")
    at = _xy(p["at"], f"{what}.at")
    rotation = 0.0
    if "rotation" in p:
        rotation = _number(p["rotation"], f"{what}.rotation", -MAX_ANGLE_DEG, MAX_ANGLE_DEG)
    size = _size(p["size"], f"{what}.size")
    ratio: float | None = None
    if shape == "roundrect":
        ratio = DEFAULT_ROUNDRECT_RATIO
        if p.get("roundrect_ratio") is not None:
            ratio = _number(p["roundrect_ratio"], f"{what}.roundrect_ratio", 0.0,
                            MAX_ROUNDRECT_RATIO)  # fmt: skip
    elif p.get("roundrect_ratio") is not None:
        raise invalid(f"Invalid {what}.roundrect_ratio", "only a roundrect pad has a ratio")
    drill: Drill | None = None
    if kind in ("tht", "npth"):
        if "drill" not in p:
            raise invalid(f"Invalid {what}", f"a {kind} pad needs a drill")
        d = _keys(p["drill"], f"{what}.drill", {"size", "shape"}, {"size"})
        dshape = _choice(d.get("shape", "circle"), f"{what}.drill.shape", DRILL_SHAPES)
        dsize = _size(d["size"], f"{what}.drill.size", MAX_DRILL_MM)
        if dshape == "circle" and dsize[0] != dsize[1]:
            raise invalid(f"Invalid {what}.drill", "a circular drill has equal sizes [d, d]")
        drill = Drill(dsize, dshape)
    elif "drill" in p:
        raise invalid(f"Invalid {what}.drill", f"a {kind} pad has no drill")
    layers = DEFAULT_LAYERS[kind]
    if "layers" in p:
        raw = p["layers"]
        if (
            not isinstance(raw, list)
            or not 1 <= len(raw) <= MAX_PAD_LAYERS
            or not all(isinstance(x, str) and PAD_LAYER_RE.match(x) for x in raw)
            or len(set(raw)) != len(raw)
        ):
            raise invalid(
                f"Invalid {what}.layers",
                f"layers must be 1..{MAX_PAD_LAYERS} distinct names of F/B/*.Cu, .Mask, .Paste",
                "e.g. ['F.Cu', 'F.Mask', 'F.Paste'], or omit it for the default of the type",
            )
        layers = tuple(raw)
    return Pad(number, kind, shape, at, rotation, size, ratio, drill, layers)


def _rect(value: Any, what: str, *, extra: set[str] | None = None) -> RectSpec:
    r = _keys(value, what, {"rect", "center", *(extra or ())}, {"rect"})
    size = _size(r["rect"], f"{what}.rect")
    center = _xy(r["center"], f"{what}.center") if "center" in r else (0.0, 0.0)
    return RectSpec(size, center)


def _courtyard(value: Any) -> tuple[RectSpec | None, float]:
    if value is None:
        return None, DEFAULT_MARGIN
    c = _keys(value, "courtyard", {"rect", "center", "margin"}, set())
    if "margin" in c:
        if set(c) != {"margin"}:
            raise invalid("Invalid courtyard", "give either rect (+ center) or margin")
        return None, _number(c["margin"], "courtyard.margin", 0.0, MAX_MARGIN_MM)
    if "rect" not in c:
        if c:
            raise invalid("Invalid courtyard", "center needs rect")
        return None, DEFAULT_MARGIN
    return _rect(c, "courtyard"), DEFAULT_MARGIN


def _silk(value: Any) -> tuple[tuple[Point, Point], ...] | None:
    if value is None or value == "auto":
        return None
    s = _keys(value, "silkscreen", {"segments"}, {"segments"})
    raw = s["segments"]
    if not isinstance(raw, list) or len(raw) > MAX_SILK_SEGMENTS:
        raise invalid(
            "Invalid silkscreen",
            f"silkscreen must be 'auto' or {{'segments': [...]}} with at most "
            f"{MAX_SILK_SEGMENTS} segments [[x1, y1], [x2, y2]]",
        )
    out: list[tuple[Point, Point]] = []
    for i, seg in enumerate(raw):
        if not isinstance(seg, list) or len(seg) != 2:
            raise invalid(f"Invalid silkscreen.segments[{i}]", "a segment is [[x1, y1], [x2, y2]]")
        what = f"silkscreen.segments[{i}]"
        out.append((_xy(seg[0], what), _xy(seg[1], what)))
    return tuple(out)


def normalise_field_name(raw: str) -> str:
    """NFKC, then every whitespace run collapsed to one space (ends stripped)."""
    return " ".join(unicodedata.normalize("NFKC", raw).split())


def _fields(value: Any) -> tuple[tuple[tuple[str, str], ...], str | None]:
    """(extra fields, Datasheet) of `fields`; reserved names are refused (casefolded)."""
    if value is None:
        return (), None
    if not isinstance(value, dict) or len(value) > MAX_EXTRA_FIELDS:
        raise invalid(
            "Invalid fields",
            f"fields must be an object of at most {MAX_EXTRA_FIELDS} name -> value strings",
        )
    out: dict[str, str] = {}
    datasheet: str | None = None
    for raw, val in value.items():
        if not isinstance(raw, str) or len(raw) > MAX_FIELD_NAME_CHARS:
            raise invalid(
                "Invalid field name",
                f"field names have at most {MAX_FIELD_NAME_CHARS} characters",
            )
        name = normalise_field_name(raw)
        shown = raw[:64]
        if not FIELD_NAME_RE.match(name):
            raise invalid(
                f"Invalid field name {shown!r}",
                "field names match ^[A-Za-z0-9][A-Za-z0-9 _.-]{0,63}$",
                "e.g. 'Manufacturer' or 'MPN'",
            )
        folded = name.casefold()
        if folded in RESERVED_FIELDS:
            raise invalid(
                f"Field {shown!r} is set by the tool",
                "Reference, Value, Footprint source, needs_user_review and User review are "
                "written by CopperChat only (User review only by fp_approve)",
                "Leave it out; the source goes into `source`.",
            )
        text = _string(val, f"fields[{shown!r}]", MAX_FIELD_VALUE_CHARS)
        if any(unicodedata.category(c) == "Cc" for c in text):
            raise invalid(f"Invalid fields[{shown!r}]", "field values have no control characters")
        if folded == DATASHEET_FIELD.casefold():
            if datasheet is not None:
                raise invalid("Invalid fields", "Datasheet is given twice")
            datasheet = text
            continue
        if any(k.casefold() == folded for k in out):
            raise invalid(f"Invalid field name {shown!r}", "two fields have the same name")
        out[name] = text
    return tuple(out.items()), datasheet


def _source(value: Any) -> SourceSpec:
    if not isinstance(value, dict):
        raise source_error()
    s = _keys(value, "source", {"document", "location", "mpn"}, {"document", "location"})
    document, location = s["document"], s["location"]
    if not isinstance(document, str) or not isinstance(location, str):
        raise source_error()
    document = document.strip()
    url = bool(URL_SCHEME_RE.match(document))
    limit = MAX_DOCUMENT_URL_CHARS if url else MAX_DOCUMENT_PATH_CHARS
    if not document or len(document) > limit:
        raise source_error()
    if not location.strip() or len(location) > MAX_LOCATION_CHARS:
        raise source_error()
    if any(unicodedata.category(c) == "Cc" for c in document + location):
        raise source_error()
    mpn = None
    if s.get("mpn") is not None:
        mpn = _string(s["mpn"], "source.mpn", MAX_MPN_CHARS).strip() or None
    return SourceSpec(document, location.strip(), mpn, url)


def model_path_error(path: str) -> StructuredToolError:
    return invalid(
        f"Invalid model path {path[:80]!r}",
        "model.path must start with ${KIPRJMOD}/ or ${KICAD<n>_3DMODEL_DIR}/ (n = 6..11), "
        "have no '..' or empty segment and end in .step, .stp, .wrl or .wrz",
        "e.g. '${KICAD10_3DMODEL_DIR}/Inductor_SMD.3dshapes/L_Bourns_SRN3012TA.step'",
    )


def _model(value: Any) -> ModelSpec | None:
    if value is None:
        return None
    m = _keys(value, "model", {"path", "offset", "rotation", "scale"}, {"path"})
    path = _string(m["path"], "model.path", MAX_MODEL_PATH_CHARS, min_chars=1)
    match = MODEL_PATH_RE.match(path)
    if match is None:
        raise model_path_error(path)
    variable, rel = match.group(1), match.group(2)
    segments = rel.split("/")
    if (
        any(s in ("", ".", "..") for s in segments)
        or "\\" in rel
        or any(unicodedata.category(c) == "Cc" for c in rel)
        or not rel.casefold().endswith(MODEL_SUFFIXES)
    ):
        raise model_path_error(path)
    offset = (0.0, 0.0, 0.0)
    rotation = (0.0, 0.0, 0.0)
    scale = (1.0, 1.0, 1.0)
    if "offset" in m:
        offset = _vec(m["offset"], "model.offset", 3, -MAX_COORD_MM, MAX_COORD_MM)
    if "rotation" in m:
        rotation = _vec(m["rotation"], "model.rotation", 3, -MAX_ANGLE_DEG, MAX_ANGLE_DEG)
    if "scale" in m:
        scale = _vec(m["scale"], "model.scale", 3, 0.0, MAX_MODEL_SCALE, open_lo=True)
    return ModelSpec(path, variable, rel, offset, rotation, scale)


def _symbol(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not 2 <= len(value.strip()) <= MAX_SYMBOL_CHARS:
        raise invalid(
            "Invalid symbol",
            f"symbol must be a non-blank string of 2..{MAX_SYMBOL_CHARS} characters",
            "e.g. 'Device:L' or 'L201'",
        )
    return value.strip()


ARG_KEYS = {"name", "description", "keywords", "mounting", "pads", "courtyard", "body",
            "silkscreen", "pin1_marker", "fields", "model", "source", "symbol",
            "replace"}  # fmt: skip
REQUIRED_KEYS = {"name", "description", "mounting", "pads", "body", "source"}


def parse_create_args(args: Any) -> CreateArgs:
    """The `fp_create` input, validated against every schema limit (any violation is
    `invalid_argument`; a bad `source` always gives the same `source_error`)."""
    a = _keys(args, "arguments", ARG_KEYS, REQUIRED_KEYS)
    name = _name(a["name"])
    description = _string(a["description"], "description", MAX_DESCRIPTION_CHARS)
    keywords = _string(a.get("keywords", ""), "keywords", MAX_KEYWORDS_CHARS)
    mounting = _choice(a["mounting"], "mounting", MOUNTINGS)
    raw_pads = a["pads"]
    if not isinstance(raw_pads, list) or not 1 <= len(raw_pads) <= MAX_PADS:
        raise invalid("Invalid pads", f"pads must be a list of 1..{MAX_PADS} pads")
    pads = tuple(_pad(p, i) for i, p in enumerate(raw_pads))
    body = _rect(a["body"], "body")
    courtyard, margin = _courtyard(a.get("courtyard"))
    silk = _silk(a.get("silkscreen"))
    marker = _choice(a.get("pin1_marker", "auto"), "pin1_marker", ("auto", "none"))
    fields, datasheet = _fields(a.get("fields"))
    model = _model(a.get("model"))
    source = _source(a["source"])
    symbol = _symbol(a.get("symbol"))
    replace = a.get("replace", False)
    if not isinstance(replace, bool):
        raise invalid("Invalid replace", "replace must be true or false")
    return CreateArgs(
        name=name,
        description=description,
        keywords=keywords,
        mounting=mounting,  # type: ignore[arg-type]
        pads=pads,
        body=body,
        courtyard=courtyard,
        margin=margin,
        silk=silk,
        pin1_marker=marker == "auto",
        fields=fields,
        datasheet=datasheet,
        model=model,
        source=source,
        symbol=symbol,
        replace=replace,
    )


# -- geometry --------------------------------------------------------------------------------


def _q(v: float) -> float:
    """Round to 1 nm (KiCad's internal unit), never -0.0."""
    return round(v, 6) + 0.0


def _qp(p: Point) -> Point:
    return (_q(p[0]), _q(p[1]))


def _box(points: Sequence[Point]) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys))


def _union(boxes: Sequence[Box]) -> Box:
    return (
        min(b[0] for b in boxes),
        min(b[1] for b in boxes),
        max(b[2] for b in boxes),
        max(b[3] for b in boxes),
    )


def _rect_box(r: RectSpec) -> Box:
    (w, h), (cx, cy) = r.size, r.center
    return (_q(cx - w / 2), _q(cy - h / 2), _q(cx + w / 2), _q(cy + h / 2))


def _outward(box: Box, margin: float) -> Box:
    """`box` grown by `margin`, rounded outward to `COURTYARD_GRID`."""
    k = 1 / COURTYARD_GRID
    lo = (math.floor(round((v - margin) * k, 6)) / k for v in box[:2])
    hi = (math.ceil(round((v + margin) * k, 6)) / k for v in box[2:])
    x0, y0 = (_q(v) for v in lo)
    x1, y1 = (_q(v) for v in hi)
    return (x0, y0, x1, y1)


def _rect_shape(layer: str, box: Box, width: float) -> Shape:
    x0, y0, x1, y1 = box
    return Shape(
        "rect",
        layer,
        ((x0, y0), (x1, y1)),
        (_q((x0 + x1) / 2), _q((y0 + y1) / 2)),
        (_q(x1 - x0), _q(y1 - y0)),
        width,
        True,
    )


def _segment(layer: str, a: Point, b: Point, width: float) -> Shape:
    return Shape("segment", layer, (_qp(a), _qp(b)), None, None, width, False)


def _pad_one(pads: Sequence[Pad]) -> int | None:
    return next((i for i, p in enumerate(pads) if p.number == "1"), None)


def _fab(body: Box, pad1: Point | None) -> Shape:
    """The F.Fab body outline; with `pad1`, its corner nearest pad 1 chamfered by
    `min(1.0, 0.25 * min(w, h))` (ties: top left, top right, bottom right, bottom left)."""
    if pad1 is None:
        return _rect_shape("F.Fab", body, FAB_WIDTH)
    x0, y0, x1, y1 = body
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]  # clockwise on screen (y down)
    k = min(range(4), key=lambda i: math.hypot(corners[i][0] - pad1[0], corners[i][1] - pad1[1]))
    c = min(CHAMFER_MAX, CHAMFER_RATIO * min(x1 - x0, y1 - y0))
    points: list[Point] = []
    for i, corner in enumerate(corners):
        if i != k:
            points.append(corner)
            continue
        for other in (corners[i - 1], corners[(i + 1) % 4]):
            dx, dy = other[0] - corner[0], other[1] - corner[1]
            n = math.hypot(dx, dy)
            points.append(_qp((corner[0] + c * dx / n, corner[1] + c * dy / n)))
    return Shape("polygon", "F.Fab", tuple(points), None, None, FAB_WIDTH, True)


def _min_on(f: Any, lo: float, hi: float) -> float:
    """Argmin of the convex `f` on [lo, hi] (golden-section search)."""
    g = (math.sqrt(5) - 1) / 2
    a, b = lo, hi
    c, d = b - g * (b - a), a + g * (b - a)
    fc, fd = f(c), f(d)
    for _ in range(80):
        if b - a < 1e-9:
            break
        if fc < fd:
            b, d, fd = d, c, fc
            c = b - g * (b - a)
            fc = f(c)
        else:
            a, c, fc = c, d, fd
            d = a + g * (b - a)
            fd = f(d)
    return (a + b) / 2


def _root(f: Any, inside: float, outside: float) -> float:
    """The boundary between `inside` (f < 0) and `outside` (f >= 0), by bisection,
    returned on the outside (f >= 0) side."""
    for _ in range(64):
        mid = (inside + outside) / 2
        if mid in (inside, outside):
            break
        if f(mid) < 0:
            inside = mid
        else:
            outside = mid
    return outside


def _blocked(
    a: Point, b: Point, part: geom.Geometry, clearance: float
) -> tuple[float, float] | None:
    """The open interval of arc length along a->b whose points are nearer than
    `clearance` to the convex `part`, or None."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length

    def f(t: float) -> float:
        p = (a[0] + ux * t, a[1] + uy * t)
        return geom.distance(geom.Seg(p, p), part) - clearance

    t_min = _min_on(f, 0.0, length)
    if f(t_min) >= 0:
        return None
    lo = 0.0 if f(0.0) < 0 else _root(f, t_min, 0.0)
    hi = length if f(length) < 0 else _root(f, t_min, length)
    return lo, hi


def clip_segment(
    a: Point, b: Point, pads: Sequence[Pad], clearance: float
) -> list[tuple[Point, Point]]:
    """The pieces of the centre line a->b at least `clearance` from every pad's copper
    (pieces shorter than `SILK_MIN_PIECE` dropped); ends rounded inward to 1 um."""
    length = math.hypot(b[0] - a[0], b[1] - a[1])
    if length <= 0:
        return []
    seg_box = (min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1]))
    cuts: list[tuple[float, float]] = []
    for pad in pads:
        parts = geom.pad_distance_parts(pad)
        pbox = _box([q for q in geom.pad_polygon(pad)])
        if (
            pbox[0] - clearance > seg_box[2]
            or pbox[2] + clearance < seg_box[0]
            or pbox[1] - clearance > seg_box[3]
            or pbox[3] + clearance < seg_box[1]
        ):
            continue
        for part in parts:
            cut = _blocked(a, b, part, clearance)
            if cut is not None:
                cuts.append(cut)
    cuts.sort()
    kept: list[tuple[float, float]] = []
    start = 0.0
    for lo, hi in cuts:
        if lo > start:
            kept.append((start, lo))
        start = max(start, hi)
    if start < length:
        kept.append((start, length))
    ux, uy = (b[0] - a[0]) / length, (b[1] - a[1]) / length
    out: list[tuple[Point, Point]] = []
    for t0, t1 in kept:
        t0 = math.ceil(round(t0 * 1000, 6)) / 1000 if t0 > 0 else 0.0
        t1 = math.floor(round(t1 * 1000, 6)) / 1000 if t1 < length else length
        if t1 - t0 < SILK_MIN_PIECE - _MICRO:
            continue
        out.append(((a[0] + ux * t0, a[1] + uy * t0), (a[0] + ux * t1, a[1] + uy * t1)))
    return out


def _outline_silk(body: Box, pads: Sequence[Pad]) -> list[Shape]:
    x0, y0, x1, y1 = body
    edges = [((x0, y0), (x1, y0)), ((x1, y0), (x1, y1)), ((x0, y1), (x1, y1)), ((x0, y0), (x0, y1))]
    clearance = SILK_CLEARANCE + SILK_WIDTH / 2
    return [
        _segment("F.SilkS", p, q, SILK_WIDTH)
        for a, b in edges
        for p, q in clip_segment(a, b, pads, clearance)
    ]


def _pin1_mark(pad: Pad, centre: Point) -> Shape:
    """A silk segment along pad 1's outer side (the side facing away from `centre`), its
    edge `PIN1_CLEARANCE` from the pad's bounding box, half the pad's extent long and
    centred on it."""
    px0, py0, px1, py1 = _box(geom.pad_polygon(pad))
    cx, cy = (px0 + px1) / 2, (py0 + py1) / 2
    dx, dy = cx - centre[0], cy - centre[1]
    off = PIN1_CLEARANCE + SILK_WIDTH / 2
    if abs(dx) >= abs(dy):
        x = px0 - off if dx <= 0 else px1 + off
        half = (py1 - py0) / 4
        return _segment("F.SilkS", (x, cy - half), (x, cy + half), SILK_WIDTH)
    y = py0 - off if dy < 0 else py1 + off
    half = (px1 - px0) / 4
    return _segment("F.SilkS", (cx - half, y), (cx + half, y), SILK_WIDTH)


def source_text(source: SourceSpec) -> str:
    """The `Footprint source` field: "<document>, <location>[, MPN <mpn>]"."""
    text = f"{source.document}, {source.location}"
    return f"{text}, MPN {source.mpn}" if source.mpn else text


def build_target(args: CreateArgs, lib: LocalLibrary) -> FootprintGeometry:
    """The footprint `fp_create` writes (spec §7.1, §7.6), in the local library `lib`."""
    lib_id = f"{lib.nickname}:{args.name}"
    pads = args.pads
    pad_boxes = [_box(geom.pad_polygon(p)) for p in pads]
    body = _rect_box(args.body)
    extent = _union([*pad_boxes, body])
    if args.courtyard is not None:
        courtyard = _rect_box(args.courtyard)
    else:
        courtyard = _outward(extent, args.margin)
    one = _pad_one(pads) if args.pin1_marker else None
    pad1 = None if one is None else pads[one]
    shapes: list[Shape] = [
        _rect_shape("F.CrtYd", courtyard, CRTYD_WIDTH),
        _fab(body, None if pad1 is None else pad1.at),
    ]
    if args.silk is None:
        shapes += _outline_silk(body, pads)
    else:
        shapes += [_segment("F.SilkS", a, b, SILK_WIDTH) for a, b in args.silk]
    if pad1 is not None:
        centre = ((extent[0] + extent[2]) / 2, (extent[1] + extent[3]) / 2)
        shapes.append(_pin1_mark(pad1, centre))

    cx = _q((courtyard[0] + courtyard[2]) / 2)
    bw, bh = args.body.size
    user = _q(min(max(min(bw, bh) / 3, USER_TEXT_MIN), USER_TEXT_MAX))
    texts = (
        Text("reference", "F.SilkS", (cx, _q(courtyard[1] - TEXT_OFFSET)),
             (TEXT_SIZE, TEXT_SIZE), 0.0, REFERENCE_PLACEHOLDER),
        Text("value", "F.Fab", (cx, _q(courtyard[3] + TEXT_OFFSET)),
             (TEXT_SIZE, TEXT_SIZE), 0.0, args.name),
        Text("user", "F.Fab", _qp(args.body.center), (user, user), 0.0, USER_TEXT),
    )  # fmt: skip

    datasheet = args.datasheet
    if datasheet is None:
        datasheet = args.source.document if args.source.url else ""
    fields: dict[str, str] = {
        "Reference": REFERENCE_PLACEHOLDER,
        "Value": args.name,
        DATASHEET_FIELD: datasheet,
    }
    fields.update(args.fields)
    fields[SOURCE_FIELD] = source_text(args.source)  # the tool-set fields last
    fields[REVIEW_FIELD] = "yes"
    models: tuple[Model3D, ...] = ()
    if args.model is not None:
        m = args.model
        models = (Model3D(m.path, m.offset, m.rotation, m.scale),)
    return FootprintGeometry(
        lib_id=lib_id,
        scope="local",
        description=args.description,
        keywords=args.keywords,
        mounting=args.mounting,
        exclude_from_bom=False,
        exclude_from_pos=False,
        dnp=False,
        fields=fields,
        pads=pads,
        shapes=tuple(shapes),
        texts=texts,
        models=models,
        overrides=(),
    )
