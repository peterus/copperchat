"""`sch_duplicate` (E6 Task 3): copy a block of the schematic, `count` times.

The block: the listed symbols (every unit of a reference) plus what connects them inside
the block. Starting at their pins, the wire network of the source sheet is walked with
`delete.WireGraph` (wires split at every attachment point, T-joins included); a piece
that reaches a pin of a part that is not listed (power symbols excepted) or a sheet pin
leads out of the block and is left out, and the stubs that only served such a piece are
cut back like `delete.prune` does. Labels, no-connects and power symbols at the
remaining connection points come along; junctions only where the copy still has three
or more arms. Explicitly listed wires, labels, junctions and no-connects are copied too.

Writes (spike 2A Q10, "method A"; Global Constraints: API only): non-symbol items are
the read protos with fresh UUIDs, shifted, created (`Create`); symbols are placed from
their library (`Place` with the canonical orientation and the unit) and then updated
with the source's fields (positions shifted), attributes, transform (canonical form,
mirror included), body style and pin name/number flags (`Update`, definition cleared by
the engine). `create_items` of a read symbol ("method B") is never used: it gives every
copy a private library cache entry (`:R_1`) and breaks KiCanvas for De Morgan parts.
Stage 1 places the symbols and creates everything else, stage 2 updates the placed
symbols (it reads `inp.placed` in placement order) and adds the copies' pins to the
intent.

New references follow spec §5.5 on the target sheet (one per source reference and
copy, shared by its units; power symbols get new `#PWR`/`#FLG` numbers). Local and
hierarchical labels get `net_suffix` (`{n}` = copy number, 2 for the first copy), global
labels and power symbols keep their names: those nets are shared (spec §12).

Before anything is written every copy is checked against the target sheet (and the
copies before it): bodies and fields as a placement (`place` / `space` obstacles), pins
and points on nothing, every wire like a router route (`router.Obstacles`) that may only
touch the shared global/power net its own connection joins. A refusal raises in
`prepare` (no snapshot, nothing written) with a free offset where one is found cheaply.

Verification: the copies' pins are new, so the intent lists exact merges: per copy, one
group per connected part of the block (a source net whose copied pins the block itself
connects); a group that joins a global/power net by name also holds a pin of that
existing net. Pins of one source net that only the outside connects stay unconnected in
the copy (reported in the notes).
"""

from __future__ import annotations

import dataclasses
import itertools
import re
import time
import uuid as uuidlib
from collections.abc import Callable, Hashable
from dataclasses import dataclass, field
from typing import Any, Literal

from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools import access
from copperchat.kicad_tools.access import PinKey
from copperchat.kicad_tools.edit.annotate import Allocator, Block
from copperchat.kicad_tools.edit.changeset import (
    ChangeSet,
    Create,
    Logical,
    NetIntent,
    Place,
    SheetRef,
    Stage,
    StageInput,
    Update,
)
from copperchat.kicad_tools.edit.delete import NodeKey, WireGraph, node_key
from copperchat.kicad_tools.edit.engine import apply_change_set, copy_item, edit_result
from copperchat.kicad_tools.edit.geometry import (
    NM,
    Point,
    canonical_transform,
    mm_point,
    same_point,
    snap_point,
    symbol_transform,
)
from copperchat.kicad_tools.edit.labels import (
    _SPIN_DIR,
    Clearance,
    World,
    check_net_name,
    label_box,
)
from copperchat.kicad_tools.edit.place import ToolChangeSet, set_transform
from copperchat.kicad_tools.edit.router import Obstacles, problems_summary
from copperchat.kicad_tools.edit.scene import (
    LabelInfo,
    Scene,
    SymbolInfo,
    WireInfo,
    net_representative,
    resolve_sheet,
    sheet_api,
    short,
)
from copperchat.kicad_tools.edit.space import named_obstacles, shift
from copperchat.kicad_tools.edit.specs_core import _coord
from copperchat.kicad_tools.edit.specs_place import _annotation_block
from copperchat.kicad_tools.edit.targets import LABEL_KINDS, Target, bad, resolve
from copperchat.tools.spec import ToolContext, ToolResult, ToolSpec, edit_spec

TOOL = "sch_duplicate"
MAX_COUNT = 8
MAX_SUFFIX = 24
DEFAULT_SUFFIX = "_CH{n}"
DUP_KINDS = ("symbol", "wire", *LABEL_KINDS, "junction", "no_connect")
SUGGEST_BUDGET_S = 3.0  # wall time for searching a free offset after a refusal
SUGGEST_STEPS = 48  # offsets tried per direction (2.54 mm apart)
SUGGEST_STEP_MM = 2.54
_SUFFIXED = ("label", "hierarchical_label")  # labels whose copies get net_suffix

Token = Hashable


class _Union:
    """Union-find over hashable tokens (connection points, label names, pins)."""

    def __init__(self) -> None:
        self.parent: dict[Token, Token] = {}

    def find(self, t: Token) -> Token:
        self.parent.setdefault(t, t)
        while self.parent[t] != t:
            self.parent[t] = self.parent[self.parent[t]]
            t = self.parent[t]
        return t

    def union(self, a: Token, b: Token) -> None:
        ra, rb = self.find(a), self.find(b)
        if ra != rb:
            self.parent[rb] = ra


def _add(p: Point, d: Point) -> Point:
    return (round(p[0] + d[0], 4), round(p[1] + d[1], 4))


def _fmt(p: Point) -> str:
    return f"[{p[0]:g}, {p[1]:g}]"


def _shift_vec(vec: Any, d: Point) -> None:
    """Shift a kiapi Vector2 proto by `d` mm."""
    vec.x_nm += round(d[0] * NM)
    vec.y_nm += round(d[1] * NM)


# -- the block ----------------------------------------------------------------------------


@dataclass
class _Run:
    """A copied stretch of one source wire (consecutive pieces of it)."""

    item: Any  # the source wire (kipy SchematicLine)
    a: Point
    b: Point
    root: Token  # its connected part of the block


@dataclass
class _Group:
    """Copied pins the block itself connects (one source net), with the existing net a
    copy joins by name (global label, power symbol) and one pin of it."""

    pins: list[PinKey]
    net: str | None  # the source net
    shared: str | None = None
    representative: PinKey | None = None


@dataclass
class DupBlock:
    source: SheetRef
    symbols: list[SymbolInfo]  # listed symbols first, then attached power symbols
    listed: set[str]  # uuids of the listed symbols
    runs: list[_Run]
    labels: list[tuple[Any, LabelInfo]]
    junctions: list[Any]
    no_connects: list[Any]
    groups: list[_Group]
    shared_roots: dict[Token, str]  # connected part -> the shared net it joins
    anchor: Point  # top-left of the symbol bodies (or of the items without symbols)
    wire_groups: int
    notes: list[str] = field(default_factory=list)


def _source_items(world: World, sheet: SheetRef, wanted: set[str]) -> dict[str, Any]:
    """The kipy items (wires, labels, junctions, no-connects) with these UUIDs."""
    sch = sheet_api(world.h, sheet)
    out: dict[str, Any] = {}
    for getter in (sch.get_lines, sch.get_labels, sch.get_junctions, sch.get_no_connects):
        for item in getter():
            uid = access.kiid(item)
            if uid in wanted:
                out[uid] = item
    return out


def _source_sheet(targets: list[Target]) -> SheetRef:
    sheets = {t.sheet.path: t.sheet for t in targets}
    if len(sheets) > 1:
        places = sorted(f"{t.spec} on {t.sheet.human_path}" for t in targets)
        raise bad(
            f"{TOOL}: the items are on several sheets",
            "a block is copied from one sheet: " + ", ".join(places[:8]),
            "Duplicate the parts of each sheet in a separate call (pass UUIDs where a "
            "reference exists on several sheets).",
        )
    return next(iter(sheets.values()))


def collect_block(world: World, targets: list[Target]) -> DupBlock:
    source = _source_sheet(targets)
    scene = world.scene(source)
    nets = world.nets(source)
    listed: list[SymbolInfo] = []
    for t in targets:
        if t.kind != "symbol":
            continue
        sym = scene.symbols.get(t.uuid)
        if sym is None:
            raise StructuredToolError(
                f"{TOOL}: {t.spec} not found on {source.human_path}",
                "the symbol is not on the sheet it was resolved on",
                "Re-read the sheet and retry.",
                code="not_found",
            )
        listed.append(sym)
    listed_ids = {s.uuid for s in listed}
    explicit = {t.uuid: t for t in targets if t.kind != "symbol"}
    pin_points = [p.at for s in listed for p in s.pins]
    starts = list(pin_points)
    for uid, t in explicit.items():
        w = scene.wires.get(uid)
        starts += [w.start, w.end] if w is not None else [t.at] if t.at is not None else []
    sheet_pin_points = [i.pin.at for i in scene.sheet_pins.values()]
    world.check_time(TOOL)
    graph = WireGraph(scene, sheet_pin_points, starts)
    listed_nodes = {node_key(p) for p in pin_points}
    power = [s for s in scene.symbols.values() if s.power and s.uuid not in listed_ids]
    foreign = {
        node_key(p.at)
        for s in scene.symbols.values()
        if s.uuid not in listed_ids and not s.power
        for p in s.pins
    }
    foreign = (foreign | {node_key(p) for p in sheet_pin_points}) - listed_nodes
    explicit_edges = {e for uid in explicit for e in graph.pieces.get(uid, [])}

    # Walk the wire network from the listed pins; a piece touching a foreign pin (or a
    # sheet pin) leads out of the block.
    live: set[int] = set(explicit_edges)
    cut_ends: list[NodeKey] = []
    seen: set[NodeKey] = {node_key(p) for p in starts}
    frontier = list(seen)
    while frontier:
        n = frontier.pop()
        for e in graph.at.get(n, []):
            if e in live:
                continue
            edge = graph.edges[e]
            m = edge.b if edge.a == n else edge.a
            if n in foreign or m in foreign:
                cut_ends.append(n)
                continue
            live.add(e)
            if m not in seen:
                seen.add(m)
                frontier.append(m)
    explicit_points = {node_key(t.at) for t in explicit.values() if t.at is not None}
    holders = listed_nodes | explicit_points
    holders |= {node_key(lb.at) for lb in scene.labels.values()}
    holders |= {node_key(p) for p in scene.no_connects.values()}
    holders |= {node_key(p.at) for s in power for p in s.pins}
    # Stubs that only led out of the block are cut back (as `delete.prune`).
    queue = list(cut_ends)
    while queue:
        n = queue.pop()
        if n in holders:
            continue
        at = [e for e in graph.at.get(n, []) if e in live]
        if len(at) != 1 or at[0] in explicit_edges:
            continue
        live.discard(at[0])
        edge = graph.edges[at[0]]
        queue.append(edge.b if edge.a == n else edge.a)

    nodes: set[NodeKey] = set(listed_nodes) | explicit_points
    arms: dict[NodeKey, int] = {}
    for e in live:
        edge = graph.edges[e]
        nodes |= {edge.a, edge.b}
        arms[edge.a] = arms.get(edge.a, 0) + 1
        arms[edge.b] = arms.get(edge.b, 0) + 1
    attached = [s for s in power if any(node_key(p.at) in nodes for p in s.pins)]
    symbols = [*listed, *attached]
    for s in symbols:
        for p in s.pins:
            arms[node_key(p.at)] = arms.get(node_key(p.at), 0) + 1
    label_infos = [
        lb for lb in scene.labels.values() if lb.uuid in explicit or node_key(lb.at) in nodes
    ]
    junction_ids = [
        u
        for u, p in scene.junctions.items()
        if u in explicit or (node_key(p) in nodes and arms.get(node_key(p), 0) >= 3)
    ]
    nc_ids = [u for u, p in scene.no_connects.items() if u in explicit or node_key(p) in nodes]

    # runs: consecutive live pieces of one wire become one copied wire
    run_pieces: list[tuple[str, int, int]] = []
    for uid, pieces in graph.pieces.items():
        for alive, grp in itertools.groupby(pieces, key=lambda e: e in live):
            if alive:
                ids = list(grp)
                run_pieces.append((uid, ids[0], ids[-1]))
    wanted = {u for u, _, _ in run_pieces} | {lb.uuid for lb in label_infos}
    wanted |= set(junction_ids) | set(nc_ids)
    items = _source_items(world, source, wanted)

    # Connected parts of the block: wires, label names, net-naming power symbols, pins.
    uf = _Union()
    wires_only = _Union()
    for e in live:
        edge = graph.edges[e]
        uf.union(("n", edge.a), ("n", edge.b))
        wires_only.union(edge.a, edge.b)
    shared_tokens: list[Token] = []
    for lb in label_infos:
        if lb.kind in _SUFFIXED:
            uf.union(("n", node_key(lb.at)), ("l", lb.text))
        elif lb.kind == "global_label":
            uf.union(("n", node_key(lb.at)), ("g", lb.text))
            shared_tokens.append(("g", lb.text))
    for s in symbols:
        for p in s.pins:
            uf.union(("p", p.key), ("n", node_key(p.at)))
        if s.power and _names_net(s):
            uf.union(("n", node_key(s.pins[0].at)), ("g", s.value))
            shared_tokens.append(("g", s.value))
    root_net: dict[Token, str] = {}
    by_root: dict[Token, list[PinKey]] = {}
    for s in symbols:
        for p in s.pins:
            r = uf.find(("p", p.key))
            by_root.setdefault(r, []).append(p.key)
            net = world.netlist.net_of(p.key)
            if net is not None:
                root_net.setdefault(r, net)
    for lb in label_infos:
        if (net := nets.get(lb.uuid)) is not None:
            root_net.setdefault(uf.find(("n", node_key(lb.at))), net)
    for uid, first, _last in run_pieces:
        if (net := nets.get(uid)) is not None:
            root_net.setdefault(uf.find(("n", graph.edges[first].a)), net)
    shared_roots = {r: root_net[r] for r in {uf.find(t) for t in shared_tokens} if r in root_net}

    notes: list[str] = []
    groups: list[_Group] = []
    roots_of_net: dict[str | None, list[Token]] = {}
    for r, keys in by_root.items():
        src_nets = {x for k in keys if (x := world.netlist.net_of(k)) is not None}
        if len(src_nets) > 1:
            raise bad(
                f"{TOOL}: the block cannot be copied as it is",
                "the copy would connect pins that are on different nets in the source "
                f"({', '.join(sorted(src_nets)[:4])})",
                "Duplicate a smaller block (fewer parts), or list the connecting wires "
                "and labels explicitly.",
            )
        net = next(iter(src_nets), None)
        roots_of_net.setdefault(net, []).append(r)
        shared = shared_roots.get(r)
        rep = None
        if shared is not None:
            rep = net_representative(world.netlist, shared)
        groups.append(_Group(sorted(keys), net, shared, rep))
    labels_of = {(s.uuid, p.number): f"{s.ref}.{p.number}" for s in symbols for p in s.pins}
    for net, roots in roots_of_net.items():
        if net is None or len(roots) < 2 or any(r in shared_roots for r in roots):
            continue
        parts = [", ".join(sorted(labels_of[(k[1], k[2])] for k in by_root[r])[:4]) for r in roots]
        notes.append(
            f"{net}: the pins {' | '.join(parts[:4])} are connected only outside the block; "
            "their copies are not connected to each other"
        )
    runs = [
        _Run(
            items[uid],
            graph.edges[first].pa,
            graph.edges[last].pb,
            uf.find(("n", graph.edges[first].a)),
        )
        for uid, first, last in run_pieces
        if uid in items
    ]
    boxes = [s.body for s in symbols]
    if boxes:
        anchor = (min(b.x0 for b in boxes), min(b.y0 for b in boxes))
    else:
        pts = [p for r in runs for p in (r.a, r.b)] + [lb.at for lb in label_infos]
        pts += [scene.junctions[u] for u in junction_ids] + [scene.no_connects[u] for u in nc_ids]
        anchor = (min(p[0] for p in pts), min(p[1] for p in pts))
    wire_roots = {wires_only.find(graph.edges[e].a) for e in live}
    return DupBlock(
        source=source,
        symbols=symbols,
        listed=listed_ids,
        runs=runs,
        labels=[(items[lb.uuid], lb) for lb in label_infos if lb.uuid in items],
        junctions=[items[u] for u in junction_ids if u in items],
        no_connects=[items[u] for u in nc_ids if u in items],
        groups=groups,
        shared_roots=shared_roots,
        anchor=(round(anchor[0], 4), round(anchor[1], 4)),
        wire_groups=len(wire_roots),
        notes=notes,
    )


def _names_net(sym: SymbolInfo) -> bool:
    """A power symbol that names its net (not a PWR_FLAG)."""
    return not sym.ref.startswith("#FLG") and bool(sym.value) and sym.value != "PWR_FLAG"


# -- copies -------------------------------------------------------------------------------


@dataclass
class Copy:
    n: int  # copy number in net_suffix (2 for the first)
    offset: Point  # displacement from the source
    refs: dict[str, str]  # source reference (uuid for unannotated parts) -> new reference
    names: dict[str, str]  # local/hierarchical label text -> suffixed text
    uuids: dict[str, str] = field(default_factory=dict)  # source symbol -> copy (stage 2)


def ref_key(sym: SymbolInfo) -> str:
    """Units of one part share a reference; unannotated parts (`R?`) are each their own."""
    return sym.uuid if sym.ref.endswith("?") else sym.ref


def _prefix(ref: str) -> str:
    prefix = re.sub(r"[0-9?]+$", "", ref).strip()
    return prefix if re.fullmatch(r"[A-Za-z][A-Za-z_]{0,7}", prefix) else "U"


def plan_copies(
    world: World,
    block: DupBlock,
    target: SheetRef,
    offsets: list[Point],
    suffix: str,
    annotation: Block,
) -> list[Copy]:
    alloc = Allocator.from_design(world.h, annotation)
    taken = {lb.text for lb in world.scene(target).labels.values() if lb.kind in _SUFFIXED}
    texts = sorted({lb.text for _item, lb in block.labels if lb.kind in _SUFFIXED})
    copies: list[Copy] = []
    for i, d in enumerate(offsets):
        n = i + 2
        refs: dict[str, str] = {}
        for s in block.symbols:
            key = ref_key(s)
            if key in refs:
                continue
            if s.power:
                prefix: Literal["#PWR", "#FLG"] = "#FLG" if s.ref.startswith("#FLG") else "#PWR"
                refs[key] = alloc.next_power(prefix)
            else:
                refs[key] = alloc.next_ref(_prefix(s.ref), target.page)
        names: dict[str, str] = {}
        for text in texts:
            new = text + suffix.replace("{n}", str(n))
            new = check_net_name(new, TOOL)
            if new in taken:
                raise bad(
                    f"{TOOL}: the label name {new} already exists on {target.human_path}",
                    f"the copy's label {text} would become {new} and join that net",
                    "Pass another net_suffix (it must contain {n} for count > 1), e.g. '_B{n}'.",
                    name=new,
                )
            taken.add(new)
            names[text] = new
        copies.append(Copy(n, d, refs, names))
    return copies


def _copy_label(item: Any, d: Point, names: dict[str, str]) -> Any:
    c = copy_item(item)
    c.proto.id.value = str(uuidlib.uuid4())
    p = c.proto
    _shift_vec(p.position, d)
    _shift_vec(p.text.position, d)
    for f in p.fields:
        _shift_vec(f.text.position, d)
    if type(item).__name__ == "GlobalLabel" and p.HasField("intersheet_refs_field"):
        _shift_vec(p.intersheet_refs_field.text.position, d)
    if p.text.text in names and type(item).__name__ in ("LocalLabel", "HierarchicalLabel"):
        p.text.text = names[p.text.text]
    return c


def _copy_point_item(item: Any, d: Point) -> Any:
    c = copy_item(item)
    c.proto.id.value = str(uuidlib.uuid4())
    _shift_vec(c.proto.position, d)
    return c


def _copy_run(run: _Run, d: Point) -> Any:
    from kipy.geometry import Vector2

    c = copy_item(run.item)
    c.proto.id.value = str(uuidlib.uuid4())
    a, b = _add(run.a, d), _add(run.b, d)
    c.start = Vector2.from_xy(round(a[0] * NM), round(a[1] * NM))
    c.end = Vector2.from_xy(round(b[0] * NM), round(b[1] * NM))
    return c


def copy_symbol_onto(src: Any, dst: Any, d: Point, reference: str) -> None:
    """Method A (spike 2A Q10): everything but identity from the source onto its freshly
    placed copy, field positions shifted by `d`."""
    for name in ("value_field", "footprint_field", "datasheet_field", "description_field"):
        f = getattr(dst, name)
        f.proto.CopyFrom(getattr(src, name).proto)
        _shift_vec(f.proto.text.position, d)
    dst.reference_field.proto.CopyFrom(src.reference_field.proto)
    _shift_vec(dst.reference_field.proto.text.position, d)
    dst.reference = reference
    dst.proto.ClearField("user_fields")
    for uf in src.user_fields:
        n = dst.proto.user_fields.add()
        n.CopyFrom(uf.proto)
        _shift_vec(n.text.position, d)
    dst.attributes = src.attributes
    rotation, mirror = symbol_transform(src)
    set_transform(dst, rotation, mirror)
    if src.body_style is not None:  # unset = the placement's default body
        dst.body_style = src.body_style
    dst.show_pin_numbers = src.show_pin_numbers
    dst.show_pin_names = src.show_pin_names
    dst.position = src.position
    _shift_vec(dst.proto.position, d)


# -- checks -------------------------------------------------------------------------------


def _clone(scene: Scene) -> Scene:
    return Scene(
        sheet=scene.sheet,
        symbols=dict(scene.symbols),
        wires=dict(scene.wires),
        labels=dict(scene.labels),
        junctions=dict(scene.junctions),
        no_connects=dict(scene.no_connects),
        sheets=dict(scene.sheets),
        sheet_pins=dict(scene.sheet_pins),
    )


def _shifted_symbol(s: SymbolInfo, d: Point, uid: str, path: str) -> SymbolInfo:
    pins = [dataclasses.replace(p, key=(path, uid, p.number), at=_add(p.at, d)) for p in s.pins]
    return dataclasses.replace(
        s,
        uuid=uid,
        at=_add(s.at, d),
        pins=pins,
        body=shift(s.body, *d),
        fields=[shift(b, *d) for b in s.fields],
        item=None,
    )


def _label_dir(lb: LabelInfo) -> tuple[int, int]:
    return _SPIN_DIR.get(lb.spin, (1, 0))


def copy_problems(
    world: World, scene: Scene, nets: dict[str, str], block: DupBlock, copy: Copy
) -> list[str]:
    """Why one copy cannot go where it is (against `scene`: the target sheet plus the
    copies before it)."""
    d = copy.offset
    out: list[str] = []
    obstacles = named_obstacles(scene)
    for s in block.symbols:
        body = shift(s.body, *d)
        mine = [body, *(shift(b, *d) for b in s.fields)]
        hits = sorted({name for name, box in obstacles if any(b.overlaps(box) for b in mine)})
        out += [f"{s.ref} would overlap {h}" for h in hits[:3]]
        for p in s.pins:
            at = _add(p.at, d)
            here = scene.items_at(at)
            if here:
                kinds = ", ".join(sorted({i["kind"] for i in here}))
                out.append(f"pin {s.ref}.{p.number} would land on a {kinds} at {_fmt(at)}")
    checks: dict[frozenset[str], Obstacles] = {}
    for run in block.runs:
        world.check_time(TOOL)
        shared = block.shared_roots.get(run.root)
        allowed = frozenset({shared} if shared is not None else ())
        if allowed not in checks:
            checks[allowed] = Obstacles(scene, world.netlist, set(allowed), nets)
        a, b = _add(run.a, d), _add(run.b, d)
        problems = checks[allowed].problems([a, b], first_only=True)
        if problems:
            out.append(f"the wire {_fmt(a)}-{_fmt(b)} {problems_summary(problems, 2)}")
    room = Clearance(scene)
    for _item, lb in block.labels:
        at = _add(lb.at, d)
        text = copy.names.get(lb.text, lb.text)
        if scene.items_at(at):
            out.append(f"label {text} at {_fmt(at)} would touch an existing item")
        elif not room.clear(label_box(at, text, _label_dir(lb), lb.kind), ignore=[at]):
            out.append(f"label {text} at {_fmt(at)} would overlap other items")
    for kind, items in (("junction", block.junctions), ("no-connect", block.no_connects)):
        for item in items:
            at = _add(mm_point(item.position), d)
            if scene.items_at(at):
                out.append(f"a {kind} at {_fmt(at)} would touch an existing item")
    return out


def _add_copy(scene: Scene, block: DupBlock, copy: Copy) -> None:
    """The copy becomes part of the scene the next copies are checked against (its
    items count as another net: fake UUIDs are on no net)."""
    d = copy.offset
    tag = f"copy{copy.n}"
    for s in block.symbols:
        uid = f"{tag}:{s.uuid}"
        scene.symbols[uid] = _shifted_symbol(s, d, uid, scene.sheet.path)
    for i, run in enumerate(block.runs):
        uid = f"{tag}:wire{i}"
        scene.wires[uid] = WireInfo(uid, _add(run.a, d), _add(run.b, d))
    for _item, lb in block.labels:
        uid = f"{tag}:{lb.uuid}"
        text = copy.names.get(lb.text, lb.text)
        scene.labels[uid] = LabelInfo(uid, text, _add(lb.at, d), lb.kind, lb.spin)
    for item in block.junctions:
        scene.junctions[f"{tag}:{access.kiid(item)}"] = _add(mm_point(item.position), d)
    for item in block.no_connects:
        scene.no_connects[f"{tag}:{access.kiid(item)}"] = _add(mm_point(item.position), d)


def all_problems(world: World, target: SheetRef, block: DupBlock, copies: list[Copy]) -> list[str]:
    scene = _clone(world.scene(target))
    nets = world.nets(target)
    for copy in copies:
        problems = copy_problems(world, scene, nets, block, copy)
        if problems:
            return [f"copy {copy.n}: {p}" for p in problems]
        _add_copy(scene, block, copy)
    return []


def _steps(base: Point, step: Point | None, count: int) -> list[Point]:
    """Displacement of each copy: base + k * step."""
    s = step or (0.0, 0.0)
    return [snap_point((base[0] + k * s[0], base[1] + k * s[1])) for k in range(count)]


def suggest(
    world: World,
    target: SheetRef,
    block: DupBlock,
    copies: list[Copy],
    vary: Point,
    offsets_for: Callable[[Point], list[Point]],
    moving: Literal["offset", "at"],
) -> Point | None:
    """A free value for the varied vector (the offset, or the first copy's shift for
    `at`), nearest first: an offset grows along its main axis, an `at` shift moves in
    the four directions. A short search; None when nothing fits within it."""
    deadline = min(time.monotonic() + SUGGEST_BUDGET_S, world.deadline)
    if moving == "offset" and (vary[0] or vary[1]):
        main = 0 if abs(vary[0]) >= abs(vary[1]) else 1
        sign = 1.0 if vary[main] > 0 else -1.0
        dirs = [(sign, 0.0) if main == 0 else (0.0, sign)]
    else:
        dirs = [(1.0, 0.0), (0.0, 1.0), (-1.0, 0.0), (0.0, -1.0)]
    for j in range(1, SUGGEST_STEPS + 1):
        for u in dirs:
            if time.monotonic() > deadline:
                return None
            cand = snap_point(
                (vary[0] + u[0] * j * SUGGEST_STEP_MM, vary[1] + u[1] * j * SUGGEST_STEP_MM)
            )
            trial = [
                dataclasses.replace(c, offset=o)
                for c, o in zip(copies, offsets_for(cand), strict=True)
            ]
            try:
                if not all_problems(world, target, block, trial):
                    return cand
            except StructuredToolError:  # the planning budget is spent
                return None
    return None


# -- the change set -----------------------------------------------------------------------


@dataclass
class DuplicatePlan:
    block: DupBlock
    target: SheetRef
    copies: list[Copy]


def _logical_per_copy(block: DupBlock) -> int:
    refs = {ref_key(s) for s in block.symbols if not s.power}
    power = sum(1 for s in block.symbols if s.power)
    return len(refs) + power + block.wire_groups + len(block.labels) + len(block.no_connects)


def build_change_set(plan: DuplicatePlan, summary: dict[str, Any]) -> ToolChangeSet:
    block, target = plan.block, plan.target
    places: list[tuple[Copy, SymbolInfo]] = []
    stage1: list[Any] = []
    for copy in plan.copies:
        for s in block.symbols:
            rotation, mirror = symbol_transform(s.item)
            deg = canonical_transform(rotation, mirror)[0]
            ref = copy.refs[ref_key(s)]
            stage1.append(Place(s.lib_id, _add(s.at, copy.offset), deg, s.unit, ref, sheet=target))
            places.append((copy, s))
    created: list[Any] = []
    for copy in plan.copies:
        d = copy.offset
        created += [_copy_run(r, d) for r in block.runs]
        created += [_copy_label(item, d, copy.names) for item, _lb in block.labels]
        created += [_copy_point_item(i, d) for i in [*block.junctions, *block.no_connects]]
    if created:
        stage1.append(Create(created, sheet=target))
    intent = NetIntent()

    def finish(inp: StageInput) -> list[Any]:
        placed = list(inp.placed)
        if len(placed) != len(places):
            raise StructuredToolError(
                f"{TOOL}: KiCad placed {len(placed)} of {len(places)} symbols",
                "the read-back after the placement is incomplete",
                "Nothing was kept; retry.",
                code="verification_failed",
            )
        updates = []
        for (copy, src), new in zip(places, placed, strict=True):
            sym = copy_item(new)
            copy_symbol_onto(src.item, sym, copy.offset, copy.refs[ref_key(src)])
            copy.uuids[src.uuid] = access.kiid(sym)
            updates.append(sym)
        merges: list[frozenset[PinKey]] = []
        for copy in plan.copies:
            for g in block.groups:
                keys = {(target.path, copy.uuids[u], num) for _p, u, num in g.pins}
                if g.representative is not None:
                    keys.add(g.representative)
                if len(keys) > 1:
                    merges.append(frozenset(keys))
        intent.merge = merges  # verification runs after the last stage
        return [Update(updates, sheet=target)]

    stages = [Stage("place and create", lambda _inp: list(stage1))]
    if places:
        stages.append(Stage("symbol fields", finish))
    return ToolChangeSet(
        tool=TOOL,
        sheet=target,
        stages=stages,
        pre_images={},
        intent=intent,
        logical=Logical(_logical_per_copy(block) * len(plan.copies)),
        summary=summary,
        notes=list(block.notes),
        plan=plan,
    )


# -- the tool -----------------------------------------------------------------------------


def _xy(args: dict[str, Any], key: str) -> Point | None:
    raw = args.get(key)
    if raw is None:
        return None
    if not isinstance(raw, (list, tuple)) or len(raw) != 2 or not all(_coord(c) for c in raw):
        raise bad(f"{TOOL}: invalid {key}", f"{key} was {short(raw)}", f"Pass {key}=[x, y] in mm.")
    return (float(raw[0]), float(raw[1]))


def _count(args: dict[str, Any]) -> int:
    count = args.get("count", 1)
    if count is None:
        return 1
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= MAX_COUNT:
        raise bad(
            f"{TOOL}: invalid count",
            f"count was {short(count)}",
            f"Pass count from 1 to {MAX_COUNT}.",
        )
    return count


def _suffix(args: dict[str, Any], count: int) -> str:
    raw = args.get("net_suffix", DEFAULT_SUFFIX)
    if raw is None:
        raw = DEFAULT_SUFFIX
    sample = raw.replace("{n}", "2") if isinstance(raw, str) else None
    if (
        sample is None
        or len(raw) > MAX_SUFFIX
        or not re.fullmatch(r"[A-Za-z0-9_+\-.~#()]*", sample)
    ):
        raise bad(
            f"{TOOL}: invalid net_suffix",
            f"net_suffix was {short(raw)}; it may hold letters, digits, _ + - . ~ # ( ) "
            "and {n} (the copy number)",
            "Pass e.g. '_CH{n}'.",
        )
    if count > 1 and "{n}" not in raw:
        raise bad(
            f"{TOOL}: net_suffix needs {{n}} for count > 1",
            f"every copy would get the same label names ({raw!r}) and the copies would "
            "be connected to each other",
            "Pass e.g. '_CH{n}'.",
        )
    return str(raw)


def _sheet(args: dict[str, Any]) -> str | None:
    sheet = args.get("sheet")
    if sheet is None or sheet == "":
        return None
    if not isinstance(sheet, str) or len(sheet) > 256:
        raise bad(f"{TOOL}: invalid sheet", f"sheet was {short(sheet)}",
                  "Pass a sheet path like '/amp/' or a page number.")  # fmt: skip
    return sheet


def _target(world: World, raw: str | None, source: SheetRef) -> SheetRef:
    target = source if raw is None else resolve_sheet(world.h, raw)
    users = world.instances(target)
    if len(users) > 1:
        raise bad(
            f"{TOOL}: {target.file} is used by {len(users)} sheet instances",
            "a copy on a shared sheet file would show in every instance, and their "
            "per-instance references are not handled by sch_duplicate ("
            + ", ".join(r.human_path for r in users[:6])
            + ")",
            "Copy onto a sheet used once (sheet=...), or duplicate in KiCad.",
        )
    return target


def plan_duplicate(
    world: World,
    args: dict[str, Any],
    offset: Point | None,
    at: Point | None,
    count: int,
    suffix: str,
    annotation: Block,
    notes: list[str],
) -> ToolChangeSet:
    targets = resolve(world, args.get("items"), TOOL, allow=DUP_KINDS)
    block = collect_block(world, targets)
    if not block.symbols and not (
        block.runs or block.labels or block.junctions or block.no_connects
    ):
        raise bad(f"{TOOL}: nothing to copy", "the items hold nothing copyable",
                  "List symbols (references) or item UUIDs.")  # fmt: skip
    target = _target(world, _sheet(args), block.source)
    if at is not None:
        base = snap_point((at[0] - block.anchor[0], at[1] - block.anchor[1]))

        def offsets_for(v: Point) -> list[Point]:
            return _steps(base, v, count) if offset is not None else [v]

        vary = offset if offset is not None else base
        moving: Literal["offset", "at"] = "offset" if offset is not None else "at"
    else:
        assert offset is not None

        def offsets_for(v: Point) -> list[Point]:
            return [snap_point((v[0] * (k + 1), v[1] * (k + 1))) for k in range(count)]

        vary, moving = offset, "offset"
    copies = plan_copies(world, block, target, offsets_for(vary), suffix, annotation)
    problems = all_problems(world, target, block, copies)
    if problems:
        found = suggest(world, target, block, copies, vary, offsets_for, moving)
        suggested: list[float] | None = None
        if found is None:
            hint = "Pass a larger offset (or another at / sheet)."
        elif moving == "offset":
            suggested = list(found)
            hint = f"Nearest free offset: {_fmt(found)}."
        else:
            assert at is not None
            suggested = list(snap_point((at[0] + found[0] - vary[0], at[1] + found[1] - vary[1])))
            hint = f"Nearest free position: at={_fmt((suggested[0], suggested[1]))}."
        shown = problems[:6] + ([f"... {len(problems) - 6} more"] if len(problems) > 6 else [])
        raise StructuredToolError(
            f"{TOOL}: the copy would overlap or touch existing items",
            "; ".join(shown),
            hint,
            code="overlap",
            details={
                "problems": problems[:20],
                f"suggested_{moving}_mm": suggested,
            },
        )
    summary = {
        "source_sheet": block.source.human_path,
        "sheet": target.human_path,
        "anchor_mm": list(block.anchor),
        "block": {
            "symbols": sorted({s.ref for s in block.symbols}),
            "wires": len(block.runs),
            "labels": len(block.labels),
            "junctions": len(block.junctions),
            "no_connects": len(block.no_connects),
        },
    }
    cs = build_change_set(DuplicatePlan(block, target, copies), summary)
    cs.notes = [*notes, *cs.notes]
    if any(lb.kind == "hierarchical_label" for _i, lb in block.labels):
        cs.notes.append(
            "hierarchical labels of the copies carry the suffix: connect them with matching "
            "sheet pins (sch_add_sheet_pin) where needed"
        )
    return cs


async def prepare_duplicate(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    notes: list[str] = []
    offset, at = _xy(args, "offset"), _xy(args, "at")
    count = _count(args)
    suffix = _suffix(args, count)
    if offset is None and at is None:
        raise bad(f"{TOOL} needs offset or at", "neither was given",
                  "Pass offset=[dx, dy] (mm) or at=[x, y] for the block's top-left.")  # fmt: skip
    if at is not None and offset is None and count > 1:
        raise bad(
            f"{TOOL}: count > 1 needs offset",
            "at places the first copy; the step between copies is missing",
            "Pass offset=[dx, dy] too (copy k goes to at + (k - 1) * offset).",
        )
    if offset is not None:
        snapped = snap_point(offset)
        if not same_point(snapped, offset, 1e-4):
            notes.append(f"offset snapped to the 1.27 mm grid: {list(offset)} -> {list(snapped)}")
        offset = snapped
    annotation = await _annotation_block(ctx)
    await ctx.session.ensure_symbol_libraries()

    def run(h: KicadHandle) -> ToolChangeSet:
        return plan_duplicate(World(h), args, offset, at, count, suffix, annotation, notes)

    return await ctx.session.call(run)


async def apply_duplicate(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, ToolChangeSet) and isinstance(cs.plan, DuplicatePlan)
    plan = cs.plan
    applied = await apply_change_set(ctx, cs)
    after = applied.netlist
    copies = []
    for copy in plan.copies:
        nets: dict[str, str] = {}
        for g in plan.block.groups:
            if g.net is None or g.net in nets or not g.pins:
                continue
            _p, u, num = g.pins[0]
            new = after.net_of((plan.target.path, copy.uuids.get(u, ""), num))
            if new is not None:
                nets[g.net] = new
        copies.append(
            {
                "n": copy.n,
                "offset_mm": list(copy.offset),
                "refs": {_shown_ref(plan.block, k): v for k, v in copy.refs.items()},
                "nets": nets,
            }
        )
    return edit_result(cs, applied, copies=copies, notes=[*cs.notes, *applied.notes])


def _shown_ref(block: DupBlock, key: str) -> str:
    return next((s.ref for s in block.symbols if s.uuid == key), key)


_ITEM = {"type": "string", "maxLength": 128}
_XY = {"type": "array", "items": {"type": "number"}, "minItems": 2, "maxItems": 2}

DUPLICATE_SPECS: list[ToolSpec] = [
    edit_spec(
        TOOL,
        "Copy a block: the listed symbols (every unit of a reference; '#PWR03' or UUIDs "
        "too), the wires between their pins (pieces leading to parts that are not listed "
        "are left out), and the labels, junctions, no-connects and power symbols on them; "
        "listed wire/label/junction/no-connect UUIDs are copied as well. offset=[dx, dy] "
        "mm: copy k is shifted by k * offset; at=[x, y]: where the top-left of the block's "
        "symbol bodies goes (with count > 1 also pass offset = the step between copies). "
        "count 1-8; sheet = target sheet instance (default the source's; not a sheet file "
        "used by several instances). Copies get new references per sheet block; local and "
        "hierarchical labels get net_suffix ('_CH{n}', n = 2 for the first copy) so the "
        "copies stay separate nets, global labels and power symbols keep their names "
        "(shared nets). Refused before writing when a copy would overlap or touch anything "
        "(a free offset is suggested). Verified: each copy's connectivity mirrors the "
        "source. Returns copies: [{n, refs, nets}]. Logical changes: the copied items "
        "(part, connection, label, power symbol, no-connect) times count.",
        {
            "type": "object",
            "properties": {
                "items": {
                    "anyOf": [
                        {"type": "array", "items": _ITEM, "minItems": 1, "maxItems": 64},
                        _ITEM,
                    ],
                    "description": "['R1', 'R2', 'C1', '<wire/label uuid>']",
                },
                "offset": {**_XY, "description": "[dx, dy] in mm (snapped to 1.27 mm)."},
                "at": {**_XY, "description": "Top-left of the first copy's symbols [x, y] mm."},
                "sheet": {
                    "type": "string",
                    "maxLength": 256,
                    "description": "Target sheet path like '/amp/' or page; default the "
                    "source's sheet.",
                },
                "count": {"type": "integer", "minimum": 1, "maximum": MAX_COUNT, "default": 1},
                "net_suffix": {
                    "type": "string",
                    "maxLength": MAX_SUFFIX,
                    "default": DEFAULT_SUFFIX,
                    "description": "Appended to local label names; {n} = copy number (2..).",
                },
            },
            "required": ["items"],
            "additionalProperties": False,
        },
        prepare_duplicate,
        apply_duplicate,
    ),
]
