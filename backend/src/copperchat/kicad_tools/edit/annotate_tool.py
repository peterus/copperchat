"""`sch_annotate` planning (E4 Task 4): which references change, and to what.

Components are the units sharing one reference on one sheet instance with one lib_id
(a multi-unit part keeps one shared reference). `keep` gives a new reference only to
unannotated components (`R?`) and to duplicates (every holder of a reference but the
first in page/position order); `renumber` renumbers every component in scope. New
references come from the `Allocator` (E-C8) over the references that stay; renumbering
goes by sheet page, then position (top first, then left).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.edit.annotate import Allocator, Block
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Logical,
    NetIntent,
    SheetRef,
    Update,
    static_stage,
)
from copperchat.kicad_tools.edit.engine import copy_item
from copperchat.kicad_tools.edit.scene import (
    SymbolInfo,
    resolve_sheet,
    sheet_ref,
    sheet_refs,
    symbol_info,
)

Scope = Literal["new", "sheet", "all"]
Mode = Literal["keep", "renumber"]
_NUM = re.compile(r"^(\D*?)(\d+)$")


def unannotated(ref: str) -> bool:
    return ref.endswith("?") or _NUM.match(ref) is None


def prefix_of(ref: str) -> str:
    return re.sub(r"[0-9?]+$", "", ref) or "U"


def _page_no(page: str) -> int:
    return int(page) if page.strip().isdigit() else 1_000_000


@dataclass
class Component:
    ref: str
    units: list[SymbolInfo] = field(default_factory=list)

    @property
    def sheet(self) -> SheetRef:
        return self.units[0].sheet

    def order(self) -> tuple[int, str, float, float]:
        first = min(self.units, key=lambda u: (u.at[1], u.at[0]))
        return (_page_no(self.sheet.page), self.sheet.path, first.at[1], first.at[0])


def read_units(h: KicadHandle) -> list[SymbolInfo]:
    """Every placed unit per sheet instance, read through `for_sheet` (each instance of
    a shared sheet has its own references, spike 2A Q7); one hierarchy walk."""
    out: list[SymbolInfo] = []
    sch = h.schematic()
    for inst in sch.get_hierarchy():
        sheet = sheet_ref(inst)
        for sym in sch.for_sheet(inst).get_symbols():
            out.append(symbol_info(sym, sheet, boxes=False))
    return out


def components(units: list[SymbolInfo]) -> tuple[list[Component], set[int]]:
    """Components in page/position order and the indexes of duplicates (a reference
    already held by an earlier component). Unannotated units are one component each."""
    comps: list[Component] = []
    by_key: dict[tuple[str, str, str], Component] = {}
    for u in units:
        if unannotated(u.ref):
            comps.append(Component(u.ref, [u]))
            continue
        key = (u.sheet.path, u.ref, u.lib_id)
        comp = by_key.get(key)
        if comp is None or any(x.unit == u.unit for x in comp.units):
            comp = Component(u.ref, [])
            by_key[key] = comp
            comps.append(comp)
        comp.units.append(u)
    comps.sort(key=Component.order)
    seen: set[str] = set()
    dupes: set[int] = set()
    for i, c in enumerate(comps):
        if unannotated(c.ref):
            continue
        if c.ref in seen:
            dupes.add(i)
        seen.add(c.ref)
    return comps, dupes


def detect_scheme(units: list[SymbolInfo], pages: list[str]) -> Literal["per_sheet", "flat"]:
    """Per-sheet when most annotated non-power references sit in their page's block (100
    or 1000) - a few strays (swapped or hand-set references) are what renumbering fixes;
    flat when most do not. Single-sheet designs are flat."""
    if len(pages) < 2:
        return "flat"
    fits = misfits = 0
    for u in units:
        m = _NUM.match(u.ref)
        page = u.sheet.page.strip()
        if u.ref.startswith("#") or m is None or not page.isdigit() or int(page) < 1:
            continue
        num, p = int(m.group(2)), int(page)
        if any(num // b == p and num % b for b in (100, 1000)):
            fits += 1
        else:
            misfits += 1
    return "per_sheet" if fits >= misfits else "flat"


@dataclass
class Renamed:
    component: Component
    new: str


def plan_renames(
    units: list[SymbolInfo],
    pages: list[str],
    *,
    scope: Scope,
    mode: Mode,
    sheet: SheetRef | None,
    placed: set[str],
    block: Block,
) -> list[Renamed]:
    comps, dupes = components(units)

    def in_scope(c: Component) -> bool:
        if scope == "all":
            return True
        if scope == "sheet":
            return sheet is not None and c.sheet.path == sheet.path
        return unannotated(c.ref) or any(u.uuid in placed for u in c.units)

    if mode == "renumber":
        todo = [i for i, c in enumerate(comps) if in_scope(c)]
    else:
        todo = [
            i for i, c in enumerate(comps) if in_scope(c) and (unannotated(c.ref) or i in dupes)
        ]
    moving = set(todo)
    fixed = [(u.ref, u.sheet.page) for i, c in enumerate(comps) if i not in moving for u in c.units]
    alloc = Allocator(fixed, pages, block)
    # The design's numbering scheme comes from all its references, not just the ones that
    # stay (renumbering everything must not turn a flat design into per-sheet blocks).
    alloc.scheme = detect_scheme(units, pages)
    out: list[Renamed] = []
    for i in todo:
        c = comps[i]
        prefix = prefix_of(c.ref)
        if prefix == "#PWR":
            new = alloc.next_power("#PWR")
        elif prefix == "#FLG":
            new = alloc.next_power("#FLG")
        else:
            new = alloc.next_ref(prefix, c.sheet.page)
        if new != c.ref:
            out.append(Renamed(c, new))
    return out


def plan_annotation(
    h: KicadHandle,
    *,
    scope: Scope,
    mode: Mode,
    sheet_arg: str | None,
    placed: set[str],
    block: Block,
    tool: str = "sch_annotate",
) -> ChangeSet:
    refs = sheet_refs(h)
    if scope == "sheet" and not sheet_arg:
        raise StructuredToolError(
            "sch_annotate scope=sheet needs a sheet",
            "no sheet was given",
            "Pass sheet='/amp/' (or a page number), or use scope='all'.",
            code="invalid_argument",
        )
    sheet = resolve_sheet(h, sheet_arg) if sheet_arg else None
    units = read_units(h)
    renames = plan_renames(
        units,
        [r.page for r in refs],
        scope=scope,
        mode=mode,
        sheet=sheet if scope == "sheet" else None,
        placed=placed,
        block=block,
    )
    per_sheet: dict[str, tuple[SheetRef, list[Any]]] = {}
    pre: dict[str, Any] = {}
    renamed: list[dict[str, str]] = []
    for r in renames:
        for u in r.component.units:
            item = copy_item(u.item)
            item.reference = r.new
            per_sheet.setdefault(u.sheet.path, (u.sheet, []))[1].append(item)
            pre.setdefault(u.uuid, copy_item(u.item))
            renamed.append(
                {"uuid": u.uuid, "old": u.ref, "new": r.new, "sheet": u.sheet.human_path}
            )
    ops = [Update(items, sheet=s) for s, items in per_sheet.values()]
    root = refs[0]
    return ChangeSet(
        tool=tool,
        sheet=sheet or root,
        stages=[static_stage("annotate", *ops)] if ops else [],
        pre_images=pre,
        intent=NetIntent(keep_partition=True),
        logical=Logical(len(renames)),
        summary={"renamed": renamed, "scope": scope, "mode": mode},
    )
