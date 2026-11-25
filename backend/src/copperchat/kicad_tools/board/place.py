"""Placement tools (SP4 spec §7.2 "Placement", §9, P4): `pcb_define_blocks` (functional
blocks as KiCad groups `blk:<name>`), `pcb_place_block` (a block clustered and packed into a
region in one commit), `pcb_move` (exact moves) and `pcb_legalize`. All two-phase: prepare
computes everything (the placer runs in the CPU worker, `run_cpu`), apply writes through
the board engine (B-C4).

Routed-part rule (spec §7.2 "Moves on a routed board", call 66): a part that would move
(position, rotation or side differ beyond `placement_hash`'s rounding) with copper attached
is refused (`part_routed`) unless `unroute: true`; then a first stage removes all copper of
its connections, locked items included (`unroute_ops`), and the change set snapshots
"full", so a failure re-creates the removed copper with its `locked` flag."""

from __future__ import annotations

import asyncio
import dataclasses
import difflib
import math
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

from kipy.geometry import Angle, Vector2
from kipy.proto.board import board_commands_pb2
from kipy.proto.board.board_types_pb2 import BoardLayer
from kipy.proto.common.types import KIID

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.classify import classify
from copperchat.board.jobs import legalize_job, place_block_job, score_job, strip_fills
from copperchat.board.model import BoardModel, Part, Rect, Side, placement_hash
from copperchat.board.placer import (
    DoesNotFit,
    Move,
    PlacementScore,
    edge_margin,
    keepout_polys,
)
from copperchat.board.staging import natural_key
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.setup import RULE_NAME, require_board_tools
from copperchat.kicad_tools.board.unroute_ops import attached, select_copper, unroute_stage
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext, ToolResult

BLOCK_PREFIX = "blk:"
MAX_BLOCKS = 32
MAX_BLOCK_REFS = 200
MAX_ITEMS = 200  # pcb_move items, pcb_place_block fixed
MAX_REF_CHARS = 32
MAX_COORD_MM = 1000.0
MAX_REGION_MM = 1000.0
MAX_OFFSET_MM = 500.0
MAX_KEEP_CHANNELS_MM = 5.0
DEFAULT_KEEP_CHANNELS_MM = 0.5
MAX_SHIFT_MM = 50.0
MAX_ROT_DEG = 360.0
ROTATIONS = (0, 90, 180, 270)
EDGES = ("top", "bottom", "left", "right")
SIDES: tuple[Side, ...] = ("top", "bottom")
MAX_LISTED = 50  # moved refs / unplaced blocks in a report
MAX_UNASSIGNED = 200
MOVES_PER_COST = 10  # pcb_move costs ceil(n / 10)
PLACE_TIMEOUT_S = 60.0  # run_cpu budget of one placer / legaliser / score job
POS_TOL_MM = 0.001  # read-back tolerance of positions
ROT_TOL_DEG = 0.01  # read-back tolerance of rotations
NO_BLOCK_CLASSES = frozenset({"mounting_hole", "fiducial"})
NEXT_METRICS = "pcb_metrics, then pcb_render view placement"


# -- arguments -----------------------------------------------------------------------------


def _invalid(why: str, suggestion: str = "See the tool schema.") -> StructuredToolError:
    return StructuredToolError("Invalid argument", why, suggestion, code="invalid_argument")


def _keys(obj: Any, name: str, allowed: set[str]) -> dict[str, Any]:
    if not isinstance(obj, dict):
        raise _invalid(f"{name} must be an object")
    extra = sorted(set(obj) - allowed)
    if extra:
        raise _invalid(f"{name} has unknown keys: {', '.join(map(str, extra[:5]))}")
    return obj


def _num(value: Any, name: str, lo: float, hi: float, *, open_lo: bool = False) -> float:
    ok = isinstance(value, int | float) and not isinstance(value, bool)
    v = float(value) if ok else math.nan
    if not math.isfinite(v) or v > hi or v < lo or (open_lo and v <= lo):
        low = f"> {lo:g}" if open_lo else f">= {lo:g}"
        raise _invalid(f"{name} must be a number {low} and <= {hi:g}")
    return v


def _bool(args: Mapping[str, Any], key: str, default: bool = False) -> bool:
    value = args.get(key, default)
    if not isinstance(value, bool):
        raise _invalid(f"{key} must be true or false")
    return value


def _ref(value: Any, name: str) -> str:
    if not isinstance(value, str) or not 1 <= len(value) <= MAX_REF_CHARS:
        raise _invalid(f"{name} must be a reference of 1-{MAX_REF_CHARS} characters")
    return value


def _list(value: Any, name: str, lo: int, hi: int) -> list[Any]:
    if not isinstance(value, list) or not lo <= len(value) <= hi:
        raise _invalid(f"{name} must be a list of {lo}-{hi} entries")
    return value


def _block_name(value: Any, name: str = "block") -> str:
    if not isinstance(value, str) or not RULE_NAME.fullmatch(value):
        raise _invalid(f"{name} {str(value)[:80]!r} is not a valid block name", RULE_NAME.pattern)
    return value


def _side(value: Any, name: str) -> Side:
    if value not in SIDES:
        raise _invalid(f"{name} must be 'top' or 'bottom'")
    side: Side = value
    return side


def _closest(name: str, pool: Sequence[str]) -> list[str]:
    return difflib.get_close_matches(name, sorted(set(pool)), n=5, cutoff=0.4)


# -- board lookups -------------------------------------------------------------------------


def _parts_by_ref(model: BoardModel, refs: Sequence[str]) -> dict[str, Part]:
    """`refs` → their parts; `not_found` (details.refs, details.closest: ref → closest
    refs) for unknown refs, `invalid_argument` for a ref two footprints share."""
    count: dict[str, int] = {}
    for p in model.parts:
        count[p.ref] = count.get(p.ref, 0) + 1
    unknown = sorted({r for r in refs if r not in count}, key=natural_key)
    if unknown:
        pool = list(count)
        closest = {r: _closest(r, pool) for r in unknown[:20]}
        first = closest[unknown[0]]
        raise StructuredToolError(
            f"Part {unknown[0]!r} not found"
            if len(unknown) == 1
            else f"{len(unknown)} parts not found",
            f"the board has no footprint with the reference(s) {', '.join(unknown[:20])}",
            f"Closest: {', '.join(first)}" if first else "Check the refs with pcb_analyze.",
            code="not_found",
            details={"refs": unknown[:20], "closest": closest},
        )
    shared = sorted({r for r in refs if count[r] > 1}, key=natural_key)
    if shared:
        raise _invalid(
            f"the reference {shared[0]!r} belongs to {count[shared[0]]} footprints",
            "Annotate the schematic and sync the board (pcb_sync_from_schematic) first.",
        )
    by_ref = {p.ref: p for p in model.parts}
    return {r: by_ref[r] for r in refs}


def _blocks(model: BoardModel) -> dict[str, tuple[str, ...]]:
    """Block name (without `blk:`) → member refs, from the board's groups."""
    out: dict[str, tuple[str, ...]] = {}
    for name, refs in model.groups:
        if name.startswith(BLOCK_PREFIX):
            out.setdefault(name[len(BLOCK_PREFIX) :], refs)
    return out


def _block_refs(model: BoardModel, block: str) -> tuple[str, ...]:
    blocks = _blocks(model)
    if block not in blocks:
        closest = _closest(block, list(blocks))
        raise StructuredToolError(
            f"Block {block!r} not found",
            f"the board has no group {BLOCK_PREFIX}{block}"
            + (f"; blocks: {', '.join(sorted(blocks)[:20])}" if blocks else ""),
            f"Closest: {', '.join(closest)}" if closest else "Define it with pcb_define_blocks.",
            code="unknown_block",
            details={"closest": closest},
        )
    return blocks[block]


def _fp_side(fp: Any) -> Side:
    return "bottom" if fp.layer == BoardLayer.BL_B_Cu else "top"


def _kiids(ids: Sequence[str]) -> list[KIID]:
    out = []
    for value in ids:
        k = KIID()
        k.value = value
        out.append(k)
    return out


# -- pcb_define_blocks ---------------------------------------------------------------------


@dataclass(frozen=True)
class BlockArgs:
    name: str
    refs: tuple[str, ...]
    anchor: str | None


def parse_define_args(args: Any) -> tuple[tuple[BlockArgs, ...], bool]:
    args = _keys(args, "arguments", {"blocks", "replace"})
    if "blocks" not in args:
        raise _invalid("blocks is required")
    blocks: list[BlockArgs] = []
    seen_names: set[str] = set()
    owner: dict[str, str] = {}
    for i, raw in enumerate(_list(args["blocks"], "blocks", 1, MAX_BLOCKS)):
        b = _keys(raw, f"blocks[{i}]", {"name", "refs", "anchor"})
        if "name" not in b or "refs" not in b:
            raise _invalid(f"blocks[{i}] needs name and refs")
        name = _block_name(b["name"], f"blocks[{i}].name")
        if name in seen_names:
            raise _invalid(f"two blocks are named {name!r}", "Give every block its own name.")
        seen_names.add(name)
        refs = [_ref(r, f"blocks[{i}].refs") for r in _list(b["refs"], "refs", 1, MAX_BLOCK_REFS)]
        for r in refs:
            if r in owner:
                where = "twice in" if owner[r] == name else f"in {owner[r]!r} and"
                raise _invalid(
                    f"{r} is listed {where} {name!r}",
                    "A part belongs to one block (KiCad: one group per item).",
                )
            owner[r] = name
        anchor = None
        if "anchor" in b:
            anchor = _ref(b["anchor"], f"blocks[{i}].anchor")
            if anchor not in refs:
                raise _invalid(f"anchor {anchor} of {name!r} is not one of its refs")
        blocks.append(BlockArgs(name, tuple(refs), anchor))
    return tuple(blocks), _bool(args, "replace")


@dataclass(frozen=True)
class _GroupRow:
    kiid: str
    name: str
    items: tuple[str, ...]  # member KIIDs in KiCad's order


def _read_groups(h: KicadHandle) -> tuple[_GroupRow, ...]:
    return tuple(
        sorted(
            (
                _GroupRow(str(g.id.value), str(g.name), tuple(str(k.value) for k in g.item_ids))
                for g in h.board().get_groups()
            ),
            key=lambda g: g.kiid,
        )
    )


@dataclass(frozen=True)
class _BlocksPlan:
    """What the stages write: `leaves` (pre-existing groups losing requested members:
    kiid → remaining member ids), `joins` (reused same-named blocks: kiid → new member
    ids), `creates` (group name → member ids), `deletes` (group kiids, last stage) and the
    pre-call rows of every group it touches (the stage undo)."""

    leaves: tuple[tuple[str, tuple[str, ...]], ...]
    joins: tuple[tuple[str, tuple[str, ...]], ...]
    creates: tuple[tuple[str, tuple[str, ...]], ...]
    deletes: tuple[str, ...]
    before: tuple[_GroupRow, ...]


def plan_blocks(
    groups: Sequence[_GroupRow],
    blocks: Sequence[BlockArgs],
    kiid_of: Mapping[str, str],
    footprint_ids: frozenset[str],
    *,
    replace: bool,
) -> _BlocksPlan:
    """Pure planning of `pcb_define_blocks` over the live groups: a same-named `blk:` group
    is reused (its non-footprint items kept; further groups of that name are deleted),
    others are created; every other group loses the requested members; with `replace`
    every other `blk:` group is deleted, else only the `blk:` groups this call empties."""
    wanted = {BLOCK_PREFIX + b.name: tuple(kiid_of[r] for r in b.refs) for b in blocks}
    members = {k for ids in wanted.values() for k in ids}
    reuse: dict[str, _GroupRow] = {}
    deletes: list[str] = []
    for g in groups:
        if g.name in wanted:
            if g.name in reuse:
                deletes.append(g.kiid)
            else:
                reuse[g.name] = g
    reused = {g.kiid for g in reuse.values()}
    leaves: list[tuple[str, tuple[str, ...]]] = []
    before: list[_GroupRow] = []
    for g in groups:
        if g.kiid in reused or g.kiid in deletes:
            continue
        is_block = g.name.startswith(BLOCK_PREFIX)
        if is_block and replace:
            deletes.append(g.kiid)
            continue
        left = tuple(k for k in g.items if k not in members)
        if left == g.items:
            continue
        if is_block and not left:
            deletes.append(g.kiid)  # this call takes every member: the block is gone
            continue
        leaves.append((g.kiid, left))
        before.append(g)
    joins: list[tuple[str, tuple[str, ...]]] = []
    for name, g in sorted(reuse.items()):
        keep = tuple(k for k in g.items if k not in footprint_ids)
        joins.append((g.kiid, wanted[name] + keep))
        before.append(g)
    creates = tuple((name, ids) for name, ids in wanted.items() if name not in reuse)
    # KiCad also takes members from a group silently (one group per item): the undo
    # restores every group this call touches, the ones deleted last included
    gone = set(deletes)
    before += [g for g in groups if g.kiid in gone]
    return _BlocksPlan(tuple(leaves), tuple(joins), creates, tuple(deletes), tuple(before))


def _update_groups(h: KicadHandle, updates: Sequence[tuple[str, tuple[str, ...]]]) -> None:
    if not updates:
        return
    board = h.board()
    live = {str(g.id.value): g for g in board.get_groups()}
    missing = [k for k, _ in updates if k not in live]
    if missing:
        raise RuntimeError(f"group {missing[0]} disappeared since the plan; retry")
    changed = []
    for kiid, ids in updates:
        g = live[kiid]
        g.item_ids = _kiids(ids)
        changed.append(g)
    board.update_items(changed)


def _write_blocks(h: KicadHandle, plan: _BlocksPlan) -> list[str]:
    """Stage 1 (one commit): members leave their old groups, reused blocks get their
    members, new groups are created. Returns the created group KIIDs."""
    from kipy.board_types import Group

    _update_groups(h, plan.leaves)
    _update_groups(h, plan.joins)
    made: list[Group] = []
    for name, ids in plan.creates:
        g = Group()
        g.item_ids = _kiids(ids)
        api.set_group_name(g.proto, name)
        made.append(g)
    if not made:
        return []
    created = h.board().create_items(made)
    if len(created) != len(made):
        raise RuntimeError(f"KiCad created {len(created)} of {len(made)} groups")
    return [str(c.id.value) for c in created]


def _restore_groups(h: KicadHandle, rows: Sequence[_GroupRow]) -> None:
    """Undo of stage 1: the pre-call name and members of every group it updated."""
    board = h.board()
    live = {str(g.id.value): g for g in board.get_groups()}
    changed = []
    for row in rows:
        g = live.get(row.kiid)
        if g is None:
            continue
        g.item_ids = _kiids(row.items)
        api.set_group_name(g.proto, row.name)
        changed.append(g)
    if changed:
        board.update_items(changed)


def _remove_groups(h: KicadHandle, kiids: Sequence[str]) -> None:
    board = h.board()
    live = {str(g.id.value) for g in board.get_groups()}
    gone = [k for k in kiids if k in live]
    if gone:
        board.remove_items_by_id(_kiids(gone))


def _verify_blocks(
    blocks: Sequence[BlockArgs], replace: bool, no_block: frozenset[str]
) -> Callable[[BoardModel, dict[str, Any]], list[str]]:
    def verify(m: BoardModel, report: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        names = [n for n, _ in m.groups]
        for b in blocks:
            name = BLOCK_PREFIX + b.name
            got = [set(refs) for n, refs in m.groups if n == name]
            if len(got) != 1:
                problems.append(f"{len(got)} groups named {name}")
            elif got[0] != set(b.refs):
                problems.append(f"{name} holds {sorted(got[0], key=natural_key)}")
        if replace:
            wanted = {BLOCK_PREFIX + b.name for b in blocks}
            extra = sorted(n for n in names if n.startswith(BLOCK_PREFIX) and n not in wanted)
            problems += [f"{n} still on the board" for n in extra[:5]]
        in_block = {r for n, refs in m.groups if n.startswith(BLOCK_PREFIX) for r in refs}
        free = sorted(
            {p.ref for p in m.parts if p.ref not in in_block and p.ref not in no_block},
            key=natural_key,
        )
        report["blocks"] = [
            {"name": n[len(BLOCK_PREFIX) :], "count": len(refs)}
            for n, refs in sorted(m.groups)
            if n.startswith(BLOCK_PREFIX)
        ]
        report["not_in_block"] = free[:MAX_UNASSIGNED]
        report["not_in_block_omitted"] = max(0, len(free) - MAX_UNASSIGNED)
        report["next"] = "pcb_place_block"
        return problems

    return verify


async def prepare_define_blocks(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_define_blocks`: unknown refs → `not_found` (+ closest), a ref in two
    blocks, an anchor outside its refs or a mounting hole / fiducial in a block →
    `invalid_argument`; then one commit of group updates and creations, and (only when
    groups go away) a last stage deleting them — compensation cannot re-create a deleted
    group, so every deletion comes after everything else (like `pcb_set_outline`)."""
    require_board_tools(ctx)
    require_board(ctx)
    blocks, replace = parse_define_args(args)
    model = await board_model(ctx)
    doc = await read_board_doc(ctx)
    refs = [r for b in blocks for r in b.refs]
    parts = _parts_by_ref(model, refs)
    cls = await asyncio.to_thread(classify, model, doc)
    banned = sorted((r for r in refs if cls.part_class.get(r) in NO_BLOCK_CLASSES), key=natural_key)
    if banned:
        raise _invalid(
            f"{', '.join(banned[:10])} cannot be in a block "
            f"({cls.part_class[banned[0]].replace('_', ' ')})",
            "Mounting holes and fiducials are placed with pcb_set_outline / pcb_move.",
        )
    no_block = frozenset(r for r, c in cls.part_class.items() if c in NO_BLOCK_CLASSES)
    groups = await ctx.session.call(_read_groups, timeout=BOARD_READ_TIMEOUT_S)
    plan = plan_blocks(
        groups,
        blocks,
        {r: p.kiid for r, p in parts.items()},
        frozenset(p.kiid for p in model.parts),
        replace=replace,
    )
    by_kiid = {g.kiid: g.name for g in groups}
    report: dict[str, Any] = {
        "defined": [{"name": b.name, "count": len(b.refs), "anchor": b.anchor} for b in blocks],
        "removed": sorted({by_kiid[k][len(BLOCK_PREFIX) :] for k in plan.deletes}),
        "logical_changes": 1,
        "budget": None,  # filled by the adapter
    }

    async def define(sctx: BoardStageContext) -> None:
        made = await sctx.call(lambda h: _write_blocks(h, plan), timeout=BOARD_READ_TIMEOUT_S)
        sctx.created.update(made)

    async def undo(sctx: BoardStageContext) -> None:
        async with sctx.ctx.session.commit("copperchat: pcb_define_blocks: undo", doc="pcb"):
            await sctx.call(lambda h: _restore_groups(h, plan.before), timeout=BOARD_READ_TIMEOUT_S)

    async def remove(sctx: BoardStageContext) -> None:
        await sctx.call(lambda h: _remove_groups(h, plan.deletes), timeout=BOARD_READ_TIMEOUT_S)

    stages = [BoardStage("define blocks", define, undo=undo if plan.before else None)]
    if plan.deletes:
        stages.append(BoardStage("remove replaced blocks", remove))
    return BoardChangeSet(
        tool="pcb_define_blocks",
        logical=Logical(1),
        kind="board_define_blocks",
        board_stages=stages,
        verify=_verify_blocks(blocks, replace, no_block),
        report=report,
    )


async def apply_define_blocks(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, BoardChangeSet)
    return await apply_board_change_set(ctx, cs)


# -- moves (pcb_place_block, pcb_move, pcb_legalize) ---------------------------------------


@dataclass(kw_only=True)
class PlacementChangeSet(BoardChangeSet):
    """A placement write: `doc` for the after-score, `read_back` the verified model."""

    doc: BoardDoc
    read_back: list[BoardModel] = field(default_factory=list)


@dataclass(frozen=True)
class _Write:
    kiid: str
    move: Move
    locked: bool | None  # None: unchanged


def _hash_r(v: float, nd: int) -> float:
    return round(v, nd) + 0.0


def _hash_rot(v: float) -> float:
    return round(v, 1) % 360 + 0.0


def moves_part(p: Part, m: Move) -> bool:
    """The part moves: position, rotation or side differ beyond `placement_hash`'s rounding
    (0.01 mm, 0.1°)."""
    return (
        _hash_r(p.x, 2) != _hash_r(m.x, 2)
        or _hash_r(p.y, 2) != _hash_r(m.y, 2)
        or _hash_rot(p.rot) != _hash_rot(m.rot)
        or p.side != m.side
    )


def _rot_diff(a: float, b: float) -> float:
    return abs(((a - b + 180.0) % 360.0) - 180.0)


def _differs(p: Part, m: Move) -> bool:
    """Anything KiCad would store differently (1 nm, 1e-6°)."""
    return (
        mm_to_nm(p.x) != mm_to_nm(m.x)
        or mm_to_nm(p.y) != mm_to_nm(m.y)
        or _rot_diff(p.rot, m.rot) > 1e-6
        or p.side != m.side
    )


def _write_moves(h: KicadHandle, writes: Sequence[_Write]) -> None:
    """One commit: side changes by KiCad's left/right flip (kipy refreshes the passed
    item), then position, orientation and lock on the same items."""
    board = h.board()
    want = {w.kiid: w for w in writes}
    fps = [fp for fp in board.get_footprints() if str(fp.id.value) in want]
    if len(fps) != len(want):
        raise RuntimeError("a footprint to move is no longer on the board; retry")
    flip = [fp for fp in fps if _fp_side(fp) != want[str(fp.id.value)].move.side]
    if flip:
        done = board.flip_items(flip, board_commands_pb2.BoardFlipDirection.BFD_LEFT_RIGHT)
        if len(done) != len(flip):
            raise RuntimeError(f"KiCad flipped {len(done)} of {len(flip)} footprints")
    for fp in fps:
        w = want[str(fp.id.value)]
        fp.position = Vector2.from_xy(mm_to_nm(w.move.x), mm_to_nm(w.move.y))
        fp.orientation = Angle.from_degrees(w.move.rot % 360)
        if w.locked is not None:
            fp.locked = w.locked
    board.update_items(fps)


def _score_dict(s: PlacementScore | None) -> dict[str, Any] | None:
    return None if s is None else dataclasses.asdict(s)


def _unplaced_blocks(m: BoardModel) -> list[str]:
    by_ref = {p.ref: p for p in m.parts}
    return sorted(
        name
        for name, refs in _blocks(m).items()
        if any(r in by_ref and not m.placed(by_ref[r]) for r in refs)
    )


def _verify_moves(
    writes: Sequence[_Write], removed: frozenset[str], before_hash: str
) -> Callable[[BoardModel, dict[str, Any]], list[str]]:
    def verify(m: BoardModel, report: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        by_kiid = {p.kiid: p for p in m.parts}
        for w in writes:
            p = by_kiid.get(w.kiid)
            mv = w.move
            if p is None:
                problems.append(f"{mv.ref} is gone")
                continue
            if (
                abs(p.x - mv.x) > POS_TOL_MM
                or abs(p.y - mv.y) > POS_TOL_MM
                or _rot_diff(p.rot, mv.rot) > ROT_TOL_DEG
                or p.side != mv.side
            ):
                problems.append(
                    f"{mv.ref} reads back at ({p.x:g}, {p.y:g}) {p.rot:g}° {p.side}, "
                    f"expected ({mv.x:g}, {mv.y:g}) {mv.rot:g}° {mv.side}"
                )
            if w.locked is not None and p.locked != w.locked:
                problems.append(f"{mv.ref} locked is {p.locked}")
        left = removed & (
            {t.kiid for t in m.tracks} | {a.kiid for a in m.arcs} | {v.kiid for v in m.vias}
        )
        if left:
            problems.append(f"{len(left)} unrouted tracks/vias still on the board")
        after = placement_hash(m)
        report["placement_hash"] = {
            "before": before_hash,
            "after": after,
            "changed": after != before_hash,
        }
        unplaced = _unplaced_blocks(m)
        report["unplaced_blocks"] = unplaced[:MAX_LISTED]
        report["next"] = "pcb_place_block" if unplaced else NEXT_METRICS
        return problems

    return verify


async def _score(model: BoardModel, doc: BoardDoc) -> PlacementScore:
    s: PlacementScore = await run_cpu(score_job, strip_fills(model), doc, timeout_s=PLACE_TIMEOUT_S)
    return s


def _routed_error(att: Mapping[str, tuple[str, ...]]) -> StructuredToolError:
    refs = sorted(att, key=natural_key)
    nets = sorted({n for ns in att.values() for n in ns}, key=natural_key)
    return StructuredToolError(
        f"{', '.join(refs[:10])} {'has' if len(refs) == 1 else 'have'} copper attached",
        f"moving would leave tracks/vias of {', '.join(nets[:10])} behind",
        "Pass unroute: true to remove all copper of these connections (locked routes, "
        "fanout and stitching vias included) and route them again afterwards, or leave "
        "the parts where they are.",
        code="part_routed",
        details={"refs": refs, "nets": nets},
    )


async def plan_moves(
    *,
    tool: str,
    kind: str,
    cost: int,
    model: BoardModel,
    doc: BoardDoc,
    moves: Sequence[Move],
    locks: Mapping[str, bool] | None = None,
    unroute: bool,
    report: dict[str, Any],
) -> PlacementChangeSet:
    """The change set of a placement write: the routed-part rule, an unroute stage when
    asked, then all moves in one commit; read-back verify. Nothing to write → a stage-less
    change set (cost 0, nothing applied)."""
    locks = locks or {}
    by_ref = {p.ref: p for p in model.parts}
    final: dict[str, Move] = {m.ref: m for m in moves}
    for ref in locks:
        p = by_ref[ref]
        final.setdefault(ref, Move(ref, p.x, p.y, p.rot, p.side))
    writes = [
        _Write(by_ref[r].kiid, mv, locks.get(r))
        for r, mv in sorted(final.items(), key=lambda kv: natural_key(kv[0]))
        if _differs(by_ref[r], mv) or (r in locks and by_ref[r].locked != locks[r])
    ]
    moving = [w.move.ref for w in writes if moves_part(by_ref[w.move.ref], w.move)]
    shifted = [w.move.ref for w in writes if _differs(by_ref[w.move.ref], w.move)]
    att = attached(model, moving)
    if att and not unroute:
        raise _routed_error(att)
    before_hash = await asyncio.to_thread(placement_hash, model)
    before = await _score(model, doc)
    report.update(
        {
            "moved": shifted[:MAX_LISTED],
            "moved_omitted": max(0, len(shifted) - MAX_LISTED),
            "unrouted_nets": [],
            "score": {"before": _score_dict(before), "after": _score_dict(before)},
            "logical_changes": cost if writes else 0,
            "budget": None,  # filled by the adapter
        }
    )
    if locks:
        report["locked_changed"] = [
            r for r in sorted(locks, key=natural_key) if by_ref[r].locked != locks[r]
        ]
    cs = PlacementChangeSet(
        tool=tool,
        logical=Logical(cost if writes else 0),
        kind=kind,
        board_stages=[],
        report=report,
        doc=doc,
    )
    if not writes:
        report["placement_hash"] = {"before": before_hash, "after": before_hash, "changed": False}
        unplaced = _unplaced_blocks(model)
        report["unplaced_blocks"] = unplaced[:MAX_LISTED]
        report["next"] = "pcb_place_block" if unplaced else NEXT_METRICS
        return cs
    removed: frozenset[str] = frozenset()
    if att:
        sel = select_copper(model, pads_of=moving, include_locked=True)
        removed = sel.track_kiids | sel.via_kiids
        cs.board_stages.append(unroute_stage(sel))
        cs.unrouted_by_call = list(sel.nets)
        cs.snapshot = "full"  # compensation re-creates the removed (locked) copper
        report["unrouted_nets"] = list(sel.nets)

    async def move(sctx: BoardStageContext) -> None:
        await sctx.call(lambda h: _write_moves(h, writes), timeout=BOARD_READ_TIMEOUT_S)

    cs.board_stages.append(BoardStage("move", move))
    inner = _verify_moves(writes, removed, before_hash)

    def verify(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        cs.read_back[:] = [m]
        return inner(m, rep)

    cs.verify = verify
    return cs


async def apply_placement(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a placement change set, then score the read-back model (`score.after`; null
    when the computation timed out — the write itself succeeded)."""
    assert isinstance(cs, PlacementChangeSet)
    if not cs.board_stages:
        return ToolResult(data=dict(cs.report))  # nothing to move: nothing written, cost 0
    result = await apply_board_change_set(ctx, cs)
    if result.data is not None and cs.read_back:
        try:
            after = await _score(cs.read_back[0], cs.doc)
            result.data["score"]["after"] = _score_dict(after)
        except StructuredToolError as exc:
            if exc.code != "board_compute_timeout":
                raise
            result.data["score"]["after"] = None
    return result


def _does_not_fit(d: DoesNotFit, what: str) -> StructuredToolError:
    return StructuredToolError(
        f"{what} does not fit",
        f"needs about {d.needed_mm2:g} mm² but only {d.available_mm2:g} mm² are free "
        f"({', '.join(d.refs[:10])})",
        "Ask the user: a bigger region or board, the bottom side, smaller parts, or move "
        "other blocks first. Nothing was changed.",
        code="does_not_fit",
        details={
            "needed_mm2": d.needed_mm2,
            "available_mm2": d.available_mm2,
            "refs": list(d.refs),
        },
    )


def _check_sides(doc: BoardDoc, parts: Sequence[tuple[Part, Side]]) -> None:
    """Side policy (spec §9.2): SMD parts only on `board.md`'s allowed sides."""
    bad = sorted(
        (
            p.ref
            for p, side in parts
            if side not in doc.allowed_sides and any(q.smd for q in p.pads)
        ),
        key=natural_key,
    )
    if bad:
        sides = "+".join(s for s in SIDES if s in doc.allowed_sides)
        raise _invalid(
            f"SMD parts on a side board.md does not allow ({', '.join(bad[:10])}; "
            f"allowed: {sides})",
            "Keep them on an allowed side, or ask the user to allow the bottom side "
            "(board.md Summary 'Assembly: ...; sides: top+bottom').",
        )


async def _read(ctx: ToolContext) -> tuple[BoardModel, BoardDoc]:
    return await board_model(ctx), await read_board_doc(ctx)


# -- pcb_place_block -----------------------------------------------------------------------

PlaceRegion = tuple[Rect | None, Literal["top", "bottom", "left", "right"] | None, float | None]


def _parse_region(value: Any) -> PlaceRegion:
    r = _keys(value, "region", {"x", "y", "w", "h", "edge", "offset"})
    if "edge" in r:
        if set(r) - {"edge", "offset"}:
            raise _invalid("region takes either {x, y, w, h} or {edge, offset?}")
        edge = r["edge"]
        if edge not in EDGES:
            raise _invalid(f"region.edge must be one of {', '.join(EDGES)}")
        offset = _num(r["offset"], "region.offset", 0.0, MAX_OFFSET_MM) if "offset" in r else None
        return None, edge, offset
    if set(r) != {"x", "y", "w", "h"}:
        raise _invalid("region takes either {x, y, w, h} or {edge, offset?}")
    rect = Rect(
        _num(r["x"], "region.x", -MAX_COORD_MM, MAX_COORD_MM),
        _num(r["y"], "region.y", -MAX_COORD_MM, MAX_COORD_MM),
        _num(r["w"], "region.w", 0.0, MAX_REGION_MM, open_lo=True),
        _num(r["h"], "region.h", 0.0, MAX_REGION_MM, open_lo=True),
    )
    return rect, None, None


def _parse_fixed(value: Any) -> list[dict[str, Any]]:
    out = []
    for i, raw in enumerate(_list(value, "fixed", 0, MAX_ITEMS)):
        f = _keys(raw, f"fixed[{i}]", {"ref", "x", "y", "rot", "side"})
        if set(f) != {"ref", "x", "y", "rot", "side"}:
            raise _invalid(f"fixed[{i}] needs ref, x, y, rot and side")
        out.append(
            {
                "ref": _ref(f["ref"], f"fixed[{i}].ref"),
                "x": _num(f["x"], f"fixed[{i}].x", -MAX_COORD_MM, MAX_COORD_MM),
                "y": _num(f["y"], f"fixed[{i}].y", -MAX_COORD_MM, MAX_COORD_MM),
                "rot": _num(f["rot"], f"fixed[{i}].rot", -MAX_ROT_DEG, MAX_ROT_DEG) % 360,
                "side": _side(f["side"], f"fixed[{i}].side"),
            }
        )
    refs = [f["ref"] for f in out]
    if len(set(refs)) != len(refs):
        raise _invalid("a ref is listed twice in fixed")
    return out


async def prepare_place_block(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_place_block`: the block's group, the side policy, the placer in the CPU
    worker (`place_block_job`); `does_not_fit` writes nothing."""
    require_board_tools(ctx)
    require_board(ctx)
    a = _keys(
        args,
        "arguments",
        {"block", "region", "side", "rotation", "keep_channels", "fixed", "unroute"},
    )
    if "block" not in a or "region" not in a:
        raise _invalid("block and region are required")
    block = _block_name(a["block"])
    region, edge, offset = _parse_region(a["region"])
    side = _side(a.get("side", "top"), "side")
    rotation = a.get("rotation")
    if rotation is not None and (isinstance(rotation, bool) or rotation not in ROTATIONS):
        raise _invalid("rotation must be 0, 90, 180 or 270")
    kc = _num(
        a.get("keep_channels", DEFAULT_KEEP_CHANNELS_MM),
        "keep_channels",
        0.0,
        MAX_KEEP_CHANNELS_MM,
    )
    fixed_args = _parse_fixed(a.get("fixed", []))
    unroute = _bool(a, "unroute")
    model, doc = await _read(ctx)
    refs = _block_refs(model, block)
    if not refs:
        raise _invalid(f"block {block!r} has no parts", "Define it with pcb_define_blocks.")
    parts = _parts_by_ref(model, list(refs) + [f["ref"] for f in fixed_args])
    fixed = tuple(Move(f["ref"], f["x"], f["y"], f["rot"], f["side"]) for f in fixed_args)
    fixed_refs = {m.ref for m in fixed}
    _check_sides(
        doc,
        [(parts[r], side) for r in refs if r not in fixed_refs and not parts[r].locked]
        + [(parts[m.ref], m.side) for m in fixed],
    )
    o = model.outline
    if edge is not None:
        if o.kind == "none":
            raise _invalid("the edge form needs a board outline", "Run pcb_set_outline first.")
        length = o.h if edge in ("left", "right") else o.w
        if offset is not None and offset > length:
            raise _invalid(f"offset {offset:g} mm is past the {edge} edge ({length:g} mm long)")
    try:
        result, used = await run_cpu(
            place_block_job,
            strip_fills(model),
            doc,
            tuple(refs),
            region,
            side=side,
            rotation=rotation,
            keep_channels=kc,
            fixed=fixed,
            allowed_sides=doc.allowed_sides,
            edge=edge,
            offset=offset,
            timeout_s=PLACE_TIMEOUT_S,
        )
    except ValueError as exc:  # the placer's own side-policy check
        raise _invalid(str(exc)) from exc
    if isinstance(result, DoesNotFit):
        raise _does_not_fit(result, f"Block {block!r}")
    report: dict[str, Any] = {
        "block": block,
        "region": dataclasses.asdict(used),
        "edge": edge,
        "side": side,
    }
    return await plan_moves(
        tool="pcb_place_block",
        kind="board_place_block",
        cost=1,
        model=model,
        doc=doc,
        moves=result.moves,
        unroute=unroute,
        report=report,
    )


# -- pcb_move ------------------------------------------------------------------------------

_MOVE_FIELDS = ("x", "y", "rot", "side", "locked")


async def prepare_move(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_move`: exact moves (unset fields keep the part's value), optionally
    legalised (`legalize_job`, movable = the moved parts)."""
    require_board_tools(ctx)
    require_board(ctx)
    a = _keys(args, "arguments", {"items", "legalize", "unroute"})
    if "items" not in a:
        raise _invalid("items is required")
    items: list[dict[str, Any]] = []
    for i, raw in enumerate(_list(a["items"], "items", 1, MAX_ITEMS)):
        it = _keys(raw, f"items[{i}]", {"ref", *_MOVE_FIELDS})
        if "ref" not in it or not set(it) & set(_MOVE_FIELDS):
            raise _invalid(f"items[{i}] needs ref and at least one of {', '.join(_MOVE_FIELDS)}")
        row: dict[str, Any] = {"ref": _ref(it["ref"], f"items[{i}].ref")}
        for k in ("x", "y"):
            if k in it:
                row[k] = _num(it[k], f"items[{i}].{k}", -MAX_COORD_MM, MAX_COORD_MM)
        if "rot" in it:
            row["rot"] = _num(it["rot"], f"items[{i}].rot", -MAX_ROT_DEG, MAX_ROT_DEG) % 360
        if "side" in it:
            row["side"] = _side(it["side"], f"items[{i}].side")
        if "locked" in it:
            if not isinstance(it["locked"], bool):
                raise _invalid(f"items[{i}].locked must be true or false")
            row["locked"] = it["locked"]
        items.append(row)
    refs = [r["ref"] for r in items]
    if len(set(refs)) != len(refs):
        raise _invalid("a ref is listed twice in items", "Give each part one item.")
    do_legalize = _bool(a, "legalize")
    unroute = _bool(a, "unroute")
    model, doc = await _read(ctx)
    parts = _parts_by_ref(model, refs)
    moves: list[Move] = []
    locks: dict[str, bool] = {}
    for r in items:
        p = parts[r["ref"]]
        if set(r) & {"x", "y", "rot", "side"}:
            moves.append(
                Move(
                    p.ref,
                    r.get("x", p.x),
                    r.get("y", p.y),
                    r.get("rot", p.rot),
                    r.get("side", p.side),
                )
            )
        if "locked" in r:
            locks[p.ref] = r["locked"]
    _check_sides(doc, [(parts[m.ref], m.side) for m in moves if m.side != parts[m.ref].side])
    if do_legalize and moves:
        locked_model = dataclasses.replace(
            model,
            parts=tuple(
                dataclasses.replace(p, locked=locks[p.ref]) if p.ref in locks else p
                for p in model.parts
            ),
        )
        result = await run_cpu(
            legalize_job,
            strip_fills(locked_model),
            tuple(moves),
            frozenset(m.ref for m in moves),
            None,
            tuple(keepout_polys(model)),
            edge_margin(model),
            timeout_s=PLACE_TIMEOUT_S,
        )
        if isinstance(result, DoesNotFit):
            raise _does_not_fit(result, "The moved parts")
        legal = {m.ref: m for m in result}
        moves = [legal.get(m.ref, m) for m in moves]
    return await plan_moves(
        tool="pcb_move",
        kind="board_move",
        cost=math.ceil(len(items) / MOVES_PER_COST),
        model=model,
        doc=doc,
        moves=moves,
        locks=locks,
        unroute=unroute,
        report={"legalized": do_legalize},
    )


# -- pcb_legalize --------------------------------------------------------------------------


async def prepare_legalize(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_legalize`: movable = the unlocked parts in scope (on the board)."""
    require_board_tools(ctx)
    require_board(ctx)
    a = _keys(args, "arguments", {"scope", "block", "max_shift", "unroute"})
    scope = a.get("scope")
    if scope not in ("all", "block"):
        raise _invalid("scope must be 'all' or 'block'")
    if scope == "block" and "block" not in a:
        raise _invalid("scope 'block' needs block")
    if scope == "all" and "block" in a:
        raise _invalid("block is only used with scope 'block'")
    max_shift = (
        _num(a["max_shift"], "max_shift", 0.0, MAX_SHIFT_MM, open_lo=True)
        if "max_shift" in a
        else None
    )
    unroute = _bool(a, "unroute")
    model, doc = await _read(ctx)
    block = None
    if scope == "block":
        block = _block_name(a["block"])
        refs = set(_block_refs(model, block))
        movable = frozenset(p.ref for p in model.parts if p.ref in refs and not p.locked)
    else:
        movable = frozenset(p.ref for p in model.parts if not p.locked)
    result = await run_cpu(
        legalize_job,
        strip_fills(model),
        (),
        movable,
        max_shift,
        tuple(keepout_polys(model)),
        edge_margin(model),
        timeout_s=PLACE_TIMEOUT_S,
    )
    if isinstance(result, DoesNotFit):
        what = f"Block {block!r}" if block is not None else "The placement"
        raise _does_not_fit(result, what)
    return await plan_moves(
        tool="pcb_legalize",
        kind="board_legalize",
        cost=1,
        model=model,
        doc=doc,
        moves=result,
        unroute=unroute,
        report={"scope": scope, "block": block, "max_shift": max_shift},
    )
