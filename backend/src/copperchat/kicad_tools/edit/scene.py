"""Per-sheet scene model for edit tools (E-C7), read through `for_sheet(instance)`.

Port of spike 2D `geo.py`: symbols with pin ends, one obstacle box per symbol body and
one per visible field (never a union box, 2D failure mode 2), wires, labels,
junctions, no-connects and sheet symbols. Every sub-sheet read goes through
`sch.for_sheet(instance)` (spike 2A Q4: the unbound schematic is not sheet-scoped).
"""

from __future__ import annotations

import difflib
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import Netlist, PinKey
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import (
    Box,
    Direction,
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

MAX_CANDIDATES = 20  # names echoed in an error (closest first where it matters)


def short(value: Any, limit: int = 80) -> str:
    text = repr(value)
    return text if len(text) <= limit else text[: limit - 3] + "..."


def _capped(names: list[str]) -> str:
    text = ", ".join(names[:MAX_CANDIDATES])
    return text + (f" ... ({len(names)} in total)" if len(names) > MAX_CANDIDATES else "")


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
class SheetPinInfo:
    """A pin of a sheet symbol on this sheet. `pin` is a pseudo pin end: key (sheet
    path, sheet symbol uuid, pin name), ref = sheet name, type 'sheet_pin', direction
    outward from the sheet box. Its key is no netlist key: KiCad lists the sheet pin's
    own KIID as a member of the net it joins (on this, the parent, sheet path)."""

    uuid: str  # the sheet pin's KIID
    sheet_uuid: str  # the sheet symbol's KIID
    child: str  # the sub-sheet instance path (this sheet's path + '/' + sheet_uuid)
    pin: PinEnd


SHEET_PIN = "sheet_pin"  # PinEnd.type of a sheet pin
_SIDE_DIR: dict[int, Direction] = {1: "left", 2: "right", 3: "up", 4: "down"}  # SheetSide


@dataclass
class Scene:
    sheet: SheetRef
    symbols: dict[str, SymbolInfo] = field(default_factory=dict)
    wires: dict[str, WireInfo] = field(default_factory=dict)
    labels: dict[str, LabelInfo] = field(default_factory=dict)
    junctions: dict[str, Point] = field(default_factory=dict)
    no_connects: dict[str, Point] = field(default_factory=dict)
    sheets: dict[str, Box] = field(default_factory=dict)  # sheet symbols on this sheet
    sheet_pins: dict[str, SheetPinInfo] = field(default_factory=dict)  # by sheet pin KIID

    def all_pins(self) -> list[PinEnd]:
        return [p for s in self.symbols.values() for p in s.pins]

    def sheet_pin_ends(self) -> list[PinEnd]:
        return [i.pin for i in self.sheet_pins.values()]

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
    listing = _capped([f"{r.human_path} (page {r.page})" for r in refs])
    if not hits:
        raise StructuredToolError(
            f"Sheet {short(sheet)} not found",
            f"Known sheet instances: {listing}",
            "Pass a sheet path like '/amp/' or a page number.",
            code="not_found",
            details={"candidates": [r.human_path for r in refs][:MAX_CANDIDATES]},
        )
    raise StructuredToolError(
        f"Sheet {short(sheet)} is ambiguous",
        f"It matches {len(hits)} sheet instances: "
        + _capped([f"{r.human_path} (page {r.page})" for r in hits]),
        "Pass the full sheet path or the page number instead.",
        code="invalid_argument",
        details={"candidates": [r.human_path for r in hits][:MAX_CANDIDATES]},
    )


# -- symbols and scene ------------------------------------------------------------------


def symbol_info(sym: Any, sheet: SheetRef, *, boxes: bool = True) -> SymbolInfo:
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
        body=body_box(sym, margin=0.0) if boxes else Box(0, 0, 0, 0),
        fields=field_boxes(sym) if boxes else [],
        power=ref.startswith("#"),
        item=sym,
    )


def all_symbols(
    h: KicadHandle, raw: Sequence[Any] | None = None, *, ref: str | None = None
) -> list[SymbolInfo]:
    """Every placed unit on every sheet instance (ref, uuid, sheet path, page, unit ...).
    `raw`: the caller's `get_symbols()` result; `ref`: only units with that reference
    (geometry is computed only for those)."""
    by_path = {r.path: r for r in sheet_refs(h)}
    out: list[SymbolInfo] = []
    for sym in h.schematic().get_symbols() if raw is None else raw:
        if ref is not None and str(sym.reference) != ref:
            continue
        sheet = by_path.get(access.sheet_path_key(sym.path))
        if sheet is not None:
            out.append(symbol_info(sym, sheet))
    return out


def read_scene(h: KicadHandle, sheet: SheetRef, *, boxes: bool = True) -> Scene:
    """`boxes=False`: no body/field boxes (connection points only; cheaper)."""
    from kipy.proto.schematic.schematic_types_pb2 import SchematicLineType

    sch = sheet_api(h, sheet)
    scene = Scene(sheet=sheet)
    for sym in sch.get_symbols():
        info = symbol_info(sym, sheet, boxes=boxes)
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
        box = Box(x, y, x + w, y + hgt)
        uid = access.kiid(s)
        scene.sheets[uid] = box
        name = str(s.name_field.text.value)
        for p in s.pins:
            spin = _sheet_pin(p, uid, name, box, sheet)
            scene.sheet_pins[spin.uuid] = spin
    return scene


def _sheet_pin(p: Any, sheet_uuid: str, sheet_name: str, box: Box, sheet: SheetRef) -> SheetPinInfo:
    at = mm_point(p.position)
    gaps: dict[Direction, float] = {
        "left": abs(at[0] - box.x0),
        "right": abs(at[0] - box.x1),
        "up": abs(at[1] - box.y0),
        "down": abs(at[1] - box.y1),
    }  # an unknown side: the box edge the pin sits on
    side = _SIDE_DIR.get(int(p.side))
    direction: Direction = side if side is not None else min(gaps, key=gaps.__getitem__)
    name = str(p.text.value)
    key = (sheet.path, sheet_uuid, name)
    pin = PinEnd(key, sheet_name, name, name, SHEET_PIN, at, direction)
    return SheetPinInfo(access.kiid(p), sheet_uuid, f"{sheet.path}/{sheet_uuid}", pin)


def sheet_pin_net(
    h: KicadHandle, info: SheetPinInfo, nets: dict[tuple[str, str], str]
) -> str | None:
    """The net a sheet pin joins: its own KIID's net in the live netlist (KiCad lists it
    on the parent sheet path), else the net of a hierarchical label of the pin's name in
    the sub-sheet instance (`nets`: `netlists()[1]`)."""
    parent = info.pin.key[0]
    net = nets.get((parent, info.uuid))
    if net is not None:
        return net
    child = next((r for r in sheet_refs(h) if r.path == info.child), None)
    if child is None:
        return None
    for lb in sheet_api(h, child).get_labels():
        if (
            type(lb).__name__ == "HierarchicalLabel"
            and str(lb.text.value) == info.pin.name
            and (net := nets.get((child.path, access.kiid(lb)))) is not None
        ):
            return net
    return None


def net_representative(netlist: Netlist, net: str | None) -> PinKey | None:
    """One symbol pin of `net` (sorted first), None when the net has no symbol pin."""
    members = sorted(netlist.nets.get(net, ())) if net else []
    return members[0] if members else None


def netlists(
    h: KicadHandle, symbols: Sequence[Any] | None = None
) -> tuple[Netlist, dict[tuple[str, str], str]]:
    """One `GetSchematicNetlist`: the pin netlist (as `access.live_netlist`) and
    (sheet_path, item uuid) -> net for every member (pins, wires, junctions, labels)."""
    sch = h.schematic()
    owner: dict[tuple[str, str], PinKey] = {}
    for sym in sch.get_symbols() if symbols is None else symbols:
        path = access.sheet_path_key(sym.path)
        uuid = access.kiid(sym)
        for pin in sym.definition.pins:
            owner[(path, access.kiid(pin))] = (path, uuid, str(pin.number))
    members: dict[str, set[PinKey]] = {}
    items: dict[tuple[str, str], str] = {}
    for net in sch.get_netlist():
        name = str(net.name)
        keys = members.setdefault(name, set())
        for sheet in net.sheets:
            path = access.sheet_path_key(sheet.path)
            for item in sheet.items:
                uid = str(item.value)
                items[(path, uid)] = name
                key = owner.get((path, uid))
                if key is not None:
                    keys.add(key)
    pins = Netlist({name: frozenset(keys) for name, keys in members.items() if keys})
    return pins, items


def item_nets(h: KicadHandle) -> dict[tuple[str, str], str]:
    """(sheet_path, item uuid) -> net name for every member of the live netlist."""
    return netlists(h)[1]


# -- pins -------------------------------------------------------------------------------


def _pin_error(
    what: str, why: str, suggestion: str, code: str, candidates: list[str]
) -> StructuredToolError:
    details = {"candidates": candidates[:MAX_CANDIDATES], "total": len(candidates)}
    return StructuredToolError(what, why, suggestion, code=code, details=details)


def resolve_pin(
    h: KicadHandle,
    spec: str,
    symbols: list[SymbolInfo] | None = None,
    raw: Sequence[Any] | None = None,
) -> PinEnd:
    """`"U1.3"` (number) or `"U1.VIN"` (unique pin name) over all sheet instances.
    `symbols`: precomputed SymbolInfos; else `raw` (`get_symbols()`) is filtered by ref."""
    ref, sep, pin = spec.strip().partition(".")
    if not sep or not ref or not pin:
        raise StructuredToolError(
            f"Invalid pin {short(spec)}",
            "A pin is written as REF.PIN, e.g. 'U1.3' or 'U1.VIN'",
            "Pass the reference and the pin number or name separated by a dot.",
            code="invalid_argument",
        )
    if symbols is None:
        raw = h.schematic().get_symbols() if raw is None else raw
        units = all_symbols(h, raw, ref=ref)
        all_refs = {str(s.reference) for s in raw}
    else:
        units = [s for s in symbols if s.ref == ref]
        all_refs = {s.ref for s in symbols}
    if not units:
        refs = sorted((r for r in all_refs if not r.startswith("#")), key=natural_key)
        close = difflib.get_close_matches(ref, refs, n=MAX_CANDIDATES, cutoff=0.0)
        refs = close + [r for r in refs if r not in close]
        raise _pin_error(
            f"Symbol {short(ref, 40)} not found",
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
            f"{ref} has the pins {_capped(candidates)}",
            "Use one of the listed pin numbers.",
            "not_found",
            candidates,
        )
    if len(hits) > 1:
        names = sorted({f"{p.ref}.{p.number}" for p in hits}, key=natural_key)
        raise _pin_error(
            f"Pin {short(spec)} is ambiguous",
            f"{len(hits)} pins match: {_capped(names)}"
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
