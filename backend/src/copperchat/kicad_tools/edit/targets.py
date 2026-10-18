"""Item targets of `sch_move` / `sch_delete` / `sch_duplicate` (E6): a reference (every
unit), a power reference (`#PWR03`), any item UUID, or `sheet:<name>`, resolved over
every sheet instance from one prepare-time `World` read.

An item of a sheet file used by several instances is one item (one UUID) shown in
each instance: it is resolved once, through its first instance, and the target lists
the other instances (`also_in`) so results can say the change shows there too.
"""

from __future__ import annotations

import difflib
import re
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_tools import access
from copperchat.kicad_tools.edit.changeset import SheetRef
from copperchat.kicad_tools.edit.geometry import Box, Point
from copperchat.kicad_tools.edit.labels import World
from copperchat.kicad_tools.edit.scene import (
    MAX_CANDIDATES,
    SheetPinInfo,
    SymbolInfo,
    sheet_api,
    short,
)
from copperchat.kicad_tools.filtering import natural_key

UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
MAX_ITEMS = 64

Kind = Literal[
    "symbol",
    "wire",
    "label",
    "global_label",
    "hierarchical_label",
    "directive_label",
    "junction",
    "no_connect",
    "sheet",
]
LABEL_KINDS = ("label", "global_label", "hierarchical_label", "directive_label")


@dataclass
class Target:
    kind: Kind
    uuid: str
    sheet: SheetRef  # the (first) instance it is read and written through
    spec: str  # what the agent passed
    ref: str | None = None
    symbol: SymbolInfo | None = None
    at: Point | None = None
    also_in: list[SheetRef] = field(default_factory=list)  # other instances of the file
    child: SheetRef | None = None  # kind == "sheet": the sub-sheet instance

    @property
    def power(self) -> bool:
        return self.symbol is not None and self.symbol.power


def bad(what: str, why: str, suggestion: str, **details: Any) -> StructuredToolError:
    return StructuredToolError(
        what, why, suggestion, code="invalid_argument", details=details or None
    )


def sheet_pins(world: World, sheet: SheetRef) -> list[SheetPinInfo]:
    """Pins of every sheet symbol on `sheet`."""
    return list(world.scene(sheet).sheet_pins.values())


def sheet_symbols(world: World, sheet: SheetRef) -> dict[str, Any]:
    """Sheet symbols on `sheet` by uuid (cached on the world)."""
    cache: dict[str, dict[str, Any]] = world.__dict__.setdefault("_e6_sheet_symbols", {})
    if sheet.path not in cache:
        cache[sheet.path] = {
            access.kiid(s): s for s in sheet_api(world.h, sheet).get_sheet_symbols()
        }
    return cache[sheet.path]


def first_instances(world: World) -> list[SheetRef]:
    """One instance per sheet file (hierarchy order)."""
    seen: set[str] = set()
    out = []
    for r in world.sheets.values():
        if r.file not in seen:
            seen.add(r.file)
            out.append(r)
    return out


def others(world: World, sheet: SheetRef) -> list[SheetRef]:
    return [r for r in world.instances(sheet) if r.path != sheet.path]


def _not_found(spec: str, world: World) -> StructuredToolError:
    refs = sorted({s.ref for s in world.symbols}, key=natural_key)
    close = difflib.get_close_matches(spec, refs, n=MAX_CANDIDATES, cutoff=0.5)
    sheets = sorted({f"sheet:{r.name}" for r in world.sheets.values() if r.name})
    return StructuredToolError(
        f"{short(spec, 60)} not found",
        "no symbol has that reference and no item that UUID"
        + (f"; close references: {', '.join(close)}" if close else ""),
        "Pass a reference ('R5', '#PWR03'), an item UUID (wires, labels, junctions, "
        "no-connects) or 'sheet:<name>' "
        + (f"({', '.join(sheets[:6])})" if sheets else "")
        + "; list symbols with sch_list_symbols.",
        code="not_found",
        details={"candidates": close},
    )


def _symbol_target(world: World, spec: str, units: list[SymbolInfo]) -> list[Target]:
    out: list[Target] = []
    seen: set[tuple[str, str]] = set()
    for u in sorted(units, key=lambda s: (natural_key(s.sheet.page), s.unit)):
        key = (u.sheet.file, u.uuid)
        if key in seen:
            continue
        seen.add(key)
        out.append(Target("symbol", u.uuid, u.sheet, spec, u.ref, u, u.at, others(world, u.sheet)))
    return out


_SUPPORTED = {
    "SchematicSymbolInstance",
    "SchematicLine",
    "LocalLabel",
    "GlobalLabel",
    "HierarchicalLabel",
    "DirectiveLabel",
    "Junction",
    "NoConnectMarker",
    "SheetSymbol",
}


def _by_uuid(world: World, spec: str, found: dict[str, Any]) -> Target | None:
    """`found`: one batched GetItemsById of every UUID the call names."""
    item = found.get(spec)
    if item is None:
        return None
    if type(item).__name__ not in _SUPPORTED:
        raise bad(
            f"Item {spec} cannot be handled here",
            f"it is a {type(item).__name__} (text, bus, field or sheet pin)",
            "Pass symbols, wires, labels, junctions, no-connects or sheet symbols; a sheet "
            "pin moves with its sheet symbol.",
        )
    for sym in world.symbols:
        if sym.uuid == spec:
            units = [s for s in world.symbols if s.uuid == spec and s.sheet.file == sym.sheet.file]
            return _symbol_target(world, spec, units)[0]
    for sheet in first_instances(world):
        world.check_time("item lookup")
        scene = world.scene(sheet)
        also = others(world, sheet)
        if spec in scene.wires:
            w = scene.wires[spec]
            return Target("wire", spec, sheet, spec, at=w.start, also_in=also)
        if spec in scene.labels:
            lb = scene.labels[spec]
            return Target(lb.kind, spec, sheet, spec, at=lb.at, also_in=also)  # type: ignore[arg-type]
        if spec in scene.junctions:
            return Target("junction", spec, sheet, spec, at=scene.junctions[spec], also_in=also)
        if spec in scene.no_connects:
            return Target("no_connect", spec, sheet, spec, at=scene.no_connects[spec], also_in=also)
        if spec in scene.sheets:
            child = next(
                (r for r in world.sheets.values() if r.path == f"{sheet.path}/{spec}"), None
            )
            box = scene.sheets[spec]
            return Target(
                "sheet", spec, sheet, spec, at=(box.x0, box.y0), also_in=also, child=child
            )
    return None


def _by_sheet_name(world: World, spec: str, name: str) -> Target:
    hits = [r for r in world.sheets.values() if r.name == name and "/" in r.path.strip("/")]
    if not hits:
        names = sorted({r.name for r in world.sheets.values() if r.name})
        raise StructuredToolError(
            f"Sheet {short(name, 60)} not found",
            f"known sheet names: {', '.join(names[:MAX_CANDIDATES]) or 'none'}",
            "Pass 'sheet:<name>' with the name shown in the hierarchy.",
            code="not_found",
            details={"candidates": names[:MAX_CANDIDATES]},
        )
    parents = {r.path.rpartition("/")[0] for r in hits}
    if len(parents) > 1:
        raise bad(
            f"Sheet name {short(name, 60)} is ambiguous",
            f"{len(hits)} sheet symbols carry it: "
            + ", ".join(sorted(r.human_path for r in hits)[:8]),
            "Pass the sheet symbol's UUID instead.",
        )
    child = hits[0]
    parent_path, _, uid = child.path.rpartition("/")
    parent = world.sheets[parent_path]
    box: Box | None = world.scene(parent).sheets.get(uid)
    at = (box.x0, box.y0) if box is not None else None
    return Target("sheet", uid, parent, spec, at=at, also_in=others(world, parent), child=child)


def resolve(world: World, raw: Any, tool: str, *, allow: tuple[str, ...] = ()) -> list[Target]:
    """Targets of `items` (deduplicated; a reference yields every unit). `allow`: the
    kinds this tool accepts (empty = all)."""
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw or len(raw) > MAX_ITEMS:
        raise bad(
            f"{tool}: items needs 1 to {MAX_ITEMS} entries",
            f"items was {short(raw)}",
            "Pass items=['R5', ...] (references, item UUIDs or 'sheet:<name>').",
        )
    out: list[Target] = []
    seen: set[tuple[str, str]] = set()
    from copperchat.kicad_tools.edit.engine import by_id

    uuids = [s.strip().lower() for s in raw if isinstance(s, str) and UUID_RE.fullmatch(s.strip())]
    by_uuid = by_id(world.h.schematic(), uuids) if uuids else {}
    for spec in raw:
        if not isinstance(spec, str) or not spec.strip() or len(spec) > 128:
            raise bad(
                f"{tool}: invalid item {short(spec)}",
                "an item is a reference, a UUID or 'sheet:<name>'",
                "Pass items=['R5', '#PWR03', '<uuid>', 'sheet:power'].",
            )
        spec = spec.strip()
        found: list[Target]
        if spec.lower().startswith("sheet:"):
            found = [_by_sheet_name(world, spec, spec[6:].strip())]
        elif UUID_RE.fullmatch(spec):
            hit = _by_uuid(world, spec.lower(), by_uuid)
            if hit is None:
                raise _not_found(spec, world)
            found = [hit]
        else:
            units = [s for s in world.symbols if s.ref == spec]
            if not units:
                raise _not_found(spec, world)
            found = _symbol_target(world, spec, units)
        for t in found:
            if allow and t.kind not in allow:
                raise bad(
                    f"{tool} cannot handle {t.kind.replace('_', ' ')} {short(spec, 60)}",
                    f"{tool} works on: {', '.join(k.replace('_', ' ') for k in allow)}",
                    "Pass only items of those kinds.",
                )
            key = (t.sheet.file, t.uuid)
            if key not in seen:
                seen.add(key)
                out.append(t)
    return out
