"""Prototype of the data-access layer the implementation phase needs (spike-local).

Everything comes from the KiCad API: listing = GetLibraryItems (names only), details =
GetItemsFromLibrary in batches. No library file parsing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from kipy.proto.board import board_types_pb2
from kipy.proto.common.commands import editor_commands_pb2, library_commands_pb2
from kipy.proto.common.types import LibraryType
from kipy.proto.schematic import schematic_types_pb2
from kipy.util import unpack_any

BATCH = 500


@dataclass
class Pin:
    number: str
    name: str
    etype: int
    x_nm: int
    y_nm: int
    unit: int  # 0 = common to all units
    body_style: int  # 0 = common to all body styles


@dataclass
class SymbolRec:
    lib_id: str
    description: str
    keywords: str
    footprint_filters: list[str]
    footprint: str  # default footprint field
    datasheet: str
    unit_count: int
    body_styles: list[str]
    power: bool
    pins: list[Pin] | None  # None = definition came back without drawing items (derived symbol)

    @property
    def pin_count(self) -> int | None:
        return None if self.pins is None else len({p.number for p in self.pins})


@dataclass
class FootprintRec:
    lib_id: str
    description: str
    keywords: str
    pad_count: int  # pads with a number
    smd: bool
    tht: bool
    extra: dict = field(default_factory=dict)


def _batches(k, ltype, ids, size=BATCH):
    for i in range(0, len(ids), size):
        c = library_commands_pb2.GetItemsFromLibrary()
        c.type = ltype
        for x in ids[i : i + size]:
            c.item_ids.add().CopyFrom(x.proto if hasattr(x, "proto") else x)
        yield k._client.send(c, editor_commands_pb2.GetItemsResponse)


def symbol_from_proto(sy) -> SymbolRec:
    pins: list[Pin] | None = None
    if len(sy.items):
        pins = []
        for ch in sy.items:
            inner = unpack_any(ch.item)
            if isinstance(inner, schematic_types_pb2.SchematicPin):
                pins.append(
                    Pin(
                        inner.number, inner.name, inner.electrical_type,
                        inner.position.x_nm, inner.position.y_nm,
                        ch.unit.unit if ch.HasField("unit") else 0,
                        ch.body_style.style if ch.HasField("body_style") else 0,
                    )
                )
    return SymbolRec(
        lib_id=f"{sy.id.library_nickname}:{sy.id.entry_name}",
        description=sy.description_field.text.text,
        keywords=sy.keywords,
        footprint_filters=list(sy.footprint_filters),
        footprint=sy.footprint_field.text.text,
        datasheet=sy.datasheet_field.text.text,
        unit_count=sy.unit_count,
        body_styles=[b.name for b in sy.body_style],
        power=sy.type != schematic_types_pb2.SchematicSymbolType.SST_NORMAL,
        pins=pins,
    )


def fetch_symbols(k, ids) -> dict[str, SymbolRec]:
    out: dict[str, SymbolRec] = {}
    for r in _batches(k, LibraryType.LT_SYMBOL, ids):
        for a in r.items:
            sy = schematic_types_pb2.SchematicSymbol()
            a.Unpack(sy)
            rec = symbol_from_proto(sy)
            out[rec.lib_id] = rec
    return out


def footprint_from_proto(fp) -> FootprintRec:
    numbers: set[str] = set()
    smd = tht = False
    for it in fp.items:
        if it.type_url.endswith("board.types.Pad"):
            pad = board_types_pb2.Pad()
            it.Unpack(pad)
            if pad.number:
                numbers.add(pad.number)
            if pad.type == board_types_pb2.PadType.PT_SMD:
                smd = True
            elif pad.type == board_types_pb2.PadType.PT_PTH:
                tht = True
    return FootprintRec(
        lib_id=f"{fp.id.library_nickname}:{fp.id.entry_name}",
        description=fp.attributes.description,
        keywords=fp.attributes.keywords,
        pad_count=len(numbers),
        smd=smd,
        tht=tht,
    )


def fetch_footprints(k, ids) -> dict[str, FootprintRec]:
    out: dict[str, FootprintRec] = {}
    for r in _batches(k, LibraryType.LT_FOOTPRINT, ids):
        for a in r.items:
            fp = board_types_pb2.Footprint()
            a.Unpack(fp)
            rec = footprint_from_proto(fp)
            out[rec.lib_id] = rec
    return out


# --- package derivation -------------------------------------------------------------------

# imperial code -> metric code (IPC / KiCad naming: "<imperial>_<metric>Metric")
IMPERIAL_TO_METRIC = {
    "01005": "0402", "0201": "0603", "0402": "1005", "0603": "1608", "0805": "2012",
    "1206": "3216", "1210": "3225", "1812": "4532", "2010": "5025", "2512": "6332",
}
_PKG_RE = re.compile(r"(?:^|[_\-])(\d{4,5})_(\d{4})Metric(?:$|[_\-])")


def package_of(footprint_name: str) -> str | None:
    """Imperial chip-size code derived from a KiCad footprint *name*, or None.

    Only accepted when the imperial code and the metric suffix agree (e.g. 0805 + 2012),
    which excludes the metric-only trap "C_01005_0402Metric" being read as 0402.
    """
    m = _PKG_RE.search(footprint_name)
    if not m:
        return None
    imp, met = m.groups()
    return imp if IMPERIAL_TO_METRIC.get(imp) == met else None
