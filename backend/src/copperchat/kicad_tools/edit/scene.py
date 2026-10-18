"""Per-sheet scene model for edit tools (E-C7), read through `for_sheet(instance)`.

Port of spike 2D `geo.py`: symbols with pin ends, one obstacle box per symbol body and
one per visible field (never a union box, 2D failure mode 2), wires, labels,
junctions, no-connects and sheet symbols. Every sub-sheet read goes through
`sch.for_sheet(instance)` (spike 2A Q4: the unbound schematic is not sheet-scoped).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import (
    Box,
    PinEnd,
    Point,
    body_box,
    field_boxes,
    in_segment_interior,
    mm_point,
    on_segment,
    pin_ends,
    same_point,
    symbol_transform,
)
from copperchat.kicad_tools.filtering import natural_key

_LABEL_KINDS = {
    "LocalLabel": "label",
    "GlobalLabel": "global_label",
    "HierarchicalLabel": "hierarchical_label",
    "DirectiveLabel": "directive_label",
}


@dataclass
class SymbolInfo:
    uuid: str
    ref: str
    lib_id: str
    value: str
    unit: int
    sheet: SheetRef
    at: Point
    rotation: int
    mirror: str
    pins: list[PinEnd]
    body: Box  # body + pins, no margin
    fields: list[Box]
    power: bool
    item: Any = field(default=None, repr=False, compare=False)

    def as_dict(self) -> dict[str, Any]:
        return {
            "uuid": self.uuid,
            "ref": self.ref,
            "lib_id": self.lib_id,
            "value": self.value,
            "unit": self.unit,
            "sheet": self.sheet.human_path,
            "page": self.sheet.page,
            "at_mm": list(self.at),
        }


@dataclass
class WireInfo:
    uuid: str
    start: Point
    end: Point


@dataclass
class LabelInfo:
    uuid: str
    text: str
    at: Point
    kind: str  # label | global_label | hierarchical_label | directive_label
    spin: int = 0


@dataclass
class Scene:
    sheet: SheetRef
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    wires: dict[str, WireInfo] = field(default_factory=dict)
    labels: dict[str, LabelInfo] = field(default_factory=dict)
    junctions: dict[str, Point] = field(default_factory=dict)
    no_connects: dict[str, Point] = field(default_factory=dict)
    sheets: dict[str, Box] = field(default_factory=dict)  # sheet symbols on this sheet

    def all_pins(self) -> list[PinEnd]:
        return [p for s in self.symbols.values() for p in s.pins]

    def obstacles(self, exclude: set[str] | frozenset[str] = frozenset()) -> list[Box]:
        """Body box (+ 2 mm margin) and field boxes per symbol, plus sheet symbols."""
        out: list[Box] = []
        for s in self.symbols.values():
            if s.uuid in exclude:
                continue
            out.append(s.body.grow(2.0))
            out.extend(s.fields)
        out.extend(b for u, b in self.sheets.items() if u not in exclude)
        return out

    def foreign_pin_ends(self, allowed_nets: set[str], netlist: Netlist) -> list[PinEnd]:
        """Pin ends that are not on one of `allowed_nets` (a wire over them would connect
        them, spike 2A Q3)."""
        return [p for p in self.all_pins() if netlist.net_of(p.key) not in allowed_nets]

    def items_at(self, pt: Point) -> list[dict[str, str]]:
        """Connection items at `pt`: pins, wire ends and interiors, labels, junctions,
        no-connects ({kind, uuid[, pin, ref]})."""
        out: list[dict[str, str]] = []
        for s in self.symbols.values():
            for p in s.pins:
                if same_point(p.at, pt):
                    out.append({"kind": "pin", "uuid": s.uuid, "ref": s.ref, "pin": p.number})
        for w in self.wires.values():
            if same_point(w.start, pt) or same_point(w.end, pt):
                out.append({"kind": "wire", "uuid": w.uuid})
            elif in_segment_interior(pt, w.start, w.end):
                out.append({"kind": "wire", "uuid": w.uuid, "where": "interior"})
        for lb in self.labels.values():
            if same_point(lb.at, pt):
                out.append({"kind": lb.kind, "uuid": lb.uuid})
        for uid, j in self.junctions.items():
            if same_point(j, pt):
                out.append({"kind": "junction", "uuid": uid})
        for uid, n in self.no_connects.items():
            if same_point(n, pt):
                out.append({"kind": "no_connect", "uuid": uid})
        return out

    def wires_through(self, pt: Point) -> list[WireInfo]:
        return [w for w in self.wires.values() if on_segment(pt, w.start, w.end)]


# -- sheets ---------------------------------------------------------------------------


def _iter_sheets(h: KicadHandle) -> Iterator[tuple[Any, SheetRef]]:
    """Every sheet instance, depth-first from the root, with its SheetRef."""
    for inst in h.schematic().get_hierarchy():
        yield inst, sheet_ref(inst)


def sheet_ref(inst: Any) -> SheetRef:
    return SheetRef(
        path=access.sheet_path_key(inst.path),
        page=str(inst.page_number),
        file=str(inst.filename),
        name=str(inst.name),
        human_path=str(inst.path.path_human_readable) or "/",
    )


def sheet_refs(h: KicadHandle) -> list[SheetRef]:
    return [ref for _inst, ref in _iter_sheets(h)]


def sheet_api(h: KicadHandle, sheet: SheetRef) -> Any:
    """The schematic bound to `sheet` (`for_sheet`); every sheet write/read goes here."""
    for inst, ref in _iter_sheets(h):
        if ref.path == sheet.path:
            return h.schematic().for_sheet(inst)
    raise StructuredToolError(
        f"Sheet {sheet.human_path} not found",
        "The sheet instance no longer exists in the schematic",
        "List the sheets with project_info and retry with an existing sheet.",
        code="not_found",
    )


def resolve_sheet(h: KicadHandle, sheet: str | None) -> SheetRef:
    """A sheet instance by name, human path (`/amp/`), KIID path or page number; the
    root sheet when `sheet` is empty."""
    refs = sheet_refs(h)
    if not sheet:
        return refs[0]
    wanted = sheet.strip()
    human = wanted if wanted.endswith("/") else wanted + "/"
    hits = [
        r for r in refs if wanted in (r.path, r.page, r.name) or r.human_path in (wanted, human)
    ]
    if len(hits) == 1:
        return hits[0]
    listing = ", ".join(f"{r.human_path} (page {r.page})" for r in refs)
    if not hits:
        raise StructuredToolError(
            f"Sheet {sheet!r} not found",
            f"Known sheet instances: {listing}",
            "Pass a sheet path like '/amp/' or a page number.",
            code="not_found",
            details={"candidates": [r.human_path for r in refs]},
        )
    raise StructuredToolError(
        f"Sheet {sheet!r} is ambiguous",
        f"It matches {len(hits)} sheet instances: "
        + ", ".join(f"{r.human_path} (page {r.page})" for r in hits),
        "Pass the full sheet path or the page number instead.",
        code="invalid_argument",
        details={"candidates": [r.human_path for r in hits]},
    )


# -- symbols and scene ------------------------------------------------------------------


def symbol_info(sym: Any, sheet: SheetRef) -> SymbolInfo:
    rotation, mirror = symbol_transform(sym)
    ref = str(sym.reference)
    return SymbolInfo(
        uuid=access.kiid(sym),
        ref=ref,
        lib_id=str(sym.definition.id),
        value=str(sym.value),
        unit=int(sym.unit),
        sheet=sheet,
        at=mm_point(sym.position),
        rotation=rotation,
        mirror=mirror,
        pins=pin_ends(sym),
        body=body_box(sym, margin=0.0),
        fields=field_boxes(sym),
        power=ref.startswith("#"),
        item=sym,
    )


def all_symbols(h: KicadHandle) -> list[SymbolInfo]:
    """Every placed unit on every sheet instance (ref, uuid, sheet path, page, unit ...)."""
    by_path = {ref.path: ref for ref in sheet_refs(h)}
    out: list[SymbolInfo] = []
    for sym in h.schematic().get_symbols():
        sheet = by_path.get(access.sheet_path_key(sym.path))
        if sheet is not None:
            out.append(symbol_info(sym, sheet))
    return out


def read_scene(h: KicadHandle, sheet: SheetRef) -> Scene:
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType

    sch = sheet_api(h, sheet)
    scene = Scene(sheet=sheet)
    for sym in sch.get_symbols():
        info = symbol_info(sym, sheet)
        scene.symbols[info.uuid] = info
    for line in sch.get_lines():
        if int(line.type) == SchematicLineType.SLT_WIRE:
            uid = access.kiid(line)
            scene.wires[uid] = WireInfo(uid, mm_point(line.start), mm_point(line.end))
    for lb in sch.get_labels():
        kind = _LABEL_KINDS.get(type(lb).__name__)
        if kind is None:
            continue
        uid = access.kiid(lb)
        scene.labels[uid] = LabelInfo(
            uid, str(lb.text.value), mm_point(lb.position), kind, int(lb.spin_style)
        )
    for j in sch.get_junctions():
        scene.junctions[access.kiid(j)] = mm_point(j.position)
    for n in sch.get_no_connects():
        scene.no_connects[access.kiid(n)] = mm_point(n.position)
    for s in sch.get_sheet_symbols():
        x, y = mm_point(s.position)
        w, hgt = mm_point(s.size)
        scene.sheets[access.kiid(s)] = Box(x, y, x + w, y + hgt)
    return scene


def item_nets(h: KicadHandle) -> dict[tuple[str, str], str]:
    """(sheet_path, item uuid) -> net name for every connectable member of the live
    netlist (pins, wires, junctions, labels)."""
    out: dict[tuple[str, str], str] = {}
    for net in h.schematic().get_netlist():
        name = str(net.name)
        for sheet in net.sheets:
            path = access.sheet_path_key(sheet.path)
            for item in sheet.items:
                out[(path, str(item.value))] = name
    return out


# -- pins -------------------------------------------------------------------------------


def _pin_error(
    what: str, why: str, suggestion: str, code: str, candidates: list[str]
) -> StructuredToolError:
    return StructuredToolError(what, why, suggestion, code=code, details={"candidates": candidates})


def resolve_pin(h: KicadHandle, spec: str, symbols: list[SymbolInfo] | None = None) -> PinEnd:
    """`"U1.3"` (number) or `"U1.VIN"` (unique pin name) over all sheet instances."""
    ref, sep, pin = spec.strip().partition(".")
    if not sep or not ref or not pin:
        raise StructuredToolError(
            f"Invalid pin {spec!r}",
            "A pin is written as REF.PIN, e.g. 'U1.3' or 'U1.VIN'",
            "Pass the reference and the pin number or name separated by a dot.",
            code="invalid_argument",
        )
    syms = all_symbols(h) if symbols is None else symbols
    units = [s for s in syms if s.ref == ref]
    if not units:
        refs = sorted({s.ref for s in syms if not s.power}, key=natural_key)
        raise _pin_error(
            f"Symbol {ref} not found",
            "No placed symbol has that reference",
            "List the symbols with sch_list_symbols and use an existing reference.",
            "not_found",
            refs,
        )
    pins = [p for s in units for p in s.pins]
    hits = [p for p in pins if p.number == pin]
    if not hits:
        hits = [p for p in pins if p.name == pin]
    if not hits:
        hits = [p for p in pins if p.name.lower() == pin.lower()]
    candidates = sorted({f"{p.number} ({p.name})" if p.name else p.number for p in pins},
                        key=natural_key)  # fmt: skip
    if not hits:
        raise _pin_error(
            f"Pin {spec} not found",
            f"{ref} has the pins {', '.join(candidates)}",
            "Use one of the listed pin numbers.",
            "not_found",
            candidates,
        )
    if len(hits) > 1:
        names = sorted({f"{p.ref}.{p.number}" for p in hits}, key=natural_key)
        raise _pin_error(
            f"Pin {spec} is ambiguous",
            f"{len(hits)} pins match: {', '.join(names)}"
            + (" (in several units)" if len(names) == 1 else ""),
            "Use the pin number instead of the name.",
            "ambiguous_pin",
            names,
        )
    return hits[0]


def pin_key_label(key: PinKey, symbols: list[SymbolInfo]) -> str:
    for s in symbols:
        if s.uuid == key[1] and s.sheet.path == key[0]:
            return f"{s.ref}.{key[2]}"
    return f"{key[1]}.{key[2]}"
