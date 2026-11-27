"""Global block floorplan (B-C11 round 2, spec §22.3): every block gets a region of the
whole board before the per-block packing fills it. Pure, deterministic (stable sort keys,
no randomness).

1. Every block is reduced to its minimal box: its `block_layout` (incl. `keep_channels`) +
   `keep_channels` + one placement grid step, in the rotation that suits its region.
2. Usable area = the outline's box inset by the edge margin; parts that stay where they are
   (locked, or not in the given blocks but on the board) and footprint keepouts are
   obstacles. `courtyard_fill` = Σ member courtyard areas / usable area (minus obstacles);
   Σ block boxes > `PACK_MAX` x usable → `DoesNotFit`.
3. Fixed blocks first: a `board.md` Blocks row whose Region parses (`parse_region`) → that
   region; a block with a member in Placement edges → a strip along that edge, as deep as
   the box (its connector's mouth facing the edge), centred on the row's position (a number
   in the cell: mm along the edge from its top / left end) or the edge middle. The rest of
   the usable box, inset by an edge channel (`max(keep_channels, CHANNEL_MIN_MM)`), is
   split into at most `MAX_FREE_RECTS` free rectangles (guillotine, biggest first, a
   channel between them); parts that stay (locked, other blocks) and keepouts come out of
   them too, small fixtures (mounting holes, fiducials, test points) do not.
4. Free blocks are distributed over the free rectangles (the biggest takes all but the
   blocks that belong next to a fixed block in a smaller one), then each rectangle is
   bisected recursively: cut across the longer side, the blocks split into two groups with
   the fewest shared nets across the cut (+ pull toward blocks already elsewhere: terminal
   propagation) and an area balance of 35-65 % (exhaustive up to `EXHAUSTIVE_MAX` blocks,
   greedy moves above; when a deeper cut fails, the next-best partitions are tried within
   a work budget); the cut position follows the group areas, so the slack spreads.
   Every cut reserves a channel `clamp(CHANNEL_PER_NET_MM x crossing nets, CHANNEL_MIN_MM,
   CHANNEL_MAX_MM)` (never below `keep_channels`), half per side.
5. A group that does not fit its side in any rotation: the cut moves once toward its need
   (then the other cut direction); else `DoesNotFit`.

`place_blocks` packs every block into its region (`pack_block`, pulled toward the region
centre with `REGION_PULL_WEIGHT`, fixed blocks and then the largest first, earlier ones as
`fixed`; a block that does not fit its region gets the whole
board once), legalises globally and runs `align_pairs` (rule b): a 2-pad part next to a
connected pad is shifted perpendicular (≤ `REVIEW["M16_align_mm"]`) so a straight corridor
exists, when its legal slack allows."""

from __future__ import annotations

import dataclasses
import itertools
import math
import re
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field

from copperchat.board.boardmd import BoardDoc, Edge
from copperchat.board.classify import BlockProposal, Classification, classify
from copperchat.board.geom import _bbox_of, _pad_rect, polygon_area
from copperchat.board.metrics import REVIEW, alignment_need, alignment_pairs
from copperchat.board.model import BoardModel, Pad, Part, Point, Rect, Side
from copperchat.board.placer.cluster import is_anchor
from copperchat.board.placer.layout import Box, Move, apply_moves, box_of, boxes_hit, moved_part
from copperchat.board.placer.legalize import (
    DoesNotFit,
    Outline,
    _hits_keepout,
    _keepout_box,
    courtyard_clearance,
    edge_margin,
    keepout_polys,
    legalize,
    on_board,
)
from copperchat.board.placer.pack import (
    OCCLUDE_WEIGHT,
    PLACE_GRID_MM,
    PlaceResult,
    _mouth,
    _orient,
    _union_area,
    block_layout,
    pack_block,
)
from copperchat.board.placer.routability import (
    GapRow,
    block_gaps,
    channel_mm,
    shared_weight,
    signal_pitch,
)
from copperchat.board.placer.score import score_placement
from copperchat.board.staging import natural_key

CHANNEL_MIN_MM = 1.0
CHANNEL_MAX_MM = 4.0
CHANNEL_PER_NET_MM = 0.3
PACK_MAX = 0.85  # Σ block boxes may use at most this share of the usable area
SPREAD_SPAN_MIN = 0.75  # "well spread" (plan call 7): reported, not a metric
BALANCE_LO, BALANCE_HI = 0.35, 0.65
EXHAUSTIVE_MAX = 12  # blocks per bisection up to which every partition is tried
GREEDY_PASSES = 50
# backtracking: when a deeper cut fails, the next of the best BACKTRACK_TRIES partitions of
# a level is tried, within BISECT_BUDGET tried partitions per run (deterministic bound)
BACKTRACK_TRIES = 6
BISECT_BUDGET = 400
# pull of a block toward its region centre (per member and mm; P4's packing uses 0.1): the
# block sits in the middle of its region, so the board's slack spreads between the blocks
# instead of every block hugging its neighbours
REGION_PULL_WEIGHT = 2.0
# R2b: a free block is packed on the whole board against the parts already placed, pulled
# toward its floorplan region centre only weakly (per member and mm) - the airwire to its
# placed partners dominates, so connected blocks end next to each other. The greedy order is
# sensitive to it (ODH 80 x 65: airwire 1127 vs 1511 mm), so every weight is tried and the
# placement with the least airwire wins (see SLACK_CREDIT)
TARGET_PULL_WEIGHTS = (0.02, 0.1, 0.2)
# the slack every block gap gets on top of its channel; every step is tried with every pull
# weight and the least airwire x (1 - SLACK_CREDIT x slack) wins (a little more airwire is
# worth more room between the blocks; ties: the first)
SLACK_STEPS_MM = (1.0, 0.5, 0.0)
SLACK_CREDIT = 0.05
# a free block that found no spot moves to the front and the packing restarts, at most this
# many times per layout and slack step (bounds the work on a board that cannot fit)
RETRY_MAX = 8
# `pack_block` calls one `place_blocks` run may spend on the constructive packing (every
# layout, slack, pull and retry; deterministic): a board that cannot fit fails fast with
# `does_not_fit` instead of running into the job timeout
PACK_BUDGET = 600
MAX_FREE_RECTS = 4
# align_pairs' tighten step: two connected pads with a straight corridor end this far apart
# at the least (when the courtyards allow), moved in steps of TIGHT_STEP_MM
TIGHT_GAP_MM = 0.3
TIGHT_STEP_MM = 0.05
_EPS = 1e-6
_FIXTURES = frozenset({"mounting_hole", "fiducial", "test_point"})
PARK_DX_MM = 1000.0  # place_blocks packs with this run's members moved this far right
_NUM = re.compile(r"-?\d+(?:\.\d+)?")
_EDGES: tuple[Edge, ...] = ("top", "bottom", "left", "right")
_NORMAL: Mapping[Edge, Point] = {
    "left": (-1.0, 0.0),
    "right": (1.0, 0.0),
    "top": (0.0, -1.0),
    "bottom": (0.0, 1.0),
}


@dataclass(frozen=True)
class BlockBox:
    name: str
    refs: tuple[str, ...]
    w: float
    h: float
    edge: Edge | None
    region: Rect | None
    # R2 (additive): mm along the edge from its top / left end (board.md position), None =
    # the edge middle; the strip depth when the box must turn to face its edge
    offset: float | None = None
    edge_ref: str | None = None  # the member with the Placement edges row


@dataclass(frozen=True)
class Floorplan:
    regions: Mapping[str, Rect]
    channels: tuple[tuple[Point, Point, float], ...]  # cut line + width
    courtyard_fill: float
    usable_mm2: float
    courtyard_mm2: float
    # R2 (additive): blocks `place_blocks` had to place on the whole board (their region
    # was too tight for the packing)
    fallback: tuple[str, ...] = ()
    fixed: tuple[str, ...] = ()  # blocks with a Region row or an edge strip
    # R2b (additive, `place_blocks` only): the gap rows of the placed blocks, the slack every
    # block gap got on top of its channel, the order the blocks were packed in, and whether
    # the relaxed layout was used (False: only the compact P4 layout fitted)
    gaps: tuple[GapRow, ...] = ()
    slack_mm: float = 0.0
    order: tuple[str, ...] = ()
    relaxed: bool = False  # the free blocks' layout; fixed ones may be in `compact_fixed`
    compact_fixed: tuple[str, ...] = ()  # fixed blocks in P4's compact layout (their strip)


class _NoFit(Exception):
    def __init__(self, refs: Sequence[str]) -> None:
        super().__init__("does not fit")
        self.refs = tuple(refs)


# ---- small geometry --------------------------------------------------------------------------


def _area(b: Box) -> float:
    return max(b[2] - b[0], 0.0) * max(b[3] - b[1], 0.0)


def _rect(b: Box) -> Rect:
    return Rect(round(b[0], 6), round(b[1], 6), round(b[2] - b[0], 6), round(b[3] - b[1], 6))


def _box(r: Rect) -> Box:
    return (r.x, r.y, r.x + r.w, r.y + r.h)


def _centre(b: Box) -> Point:
    return ((b[0] + b[2]) / 2, (b[1] + b[3]) / 2)


def _overlap(a: Box, b: Box) -> bool:
    return a[0] < b[2] - _EPS and b[0] < a[2] - _EPS and a[1] < b[3] - _EPS and b[1] < a[3] - _EPS


def _fits(w: float, h: float, b: Box) -> bool:
    """A w x h box fits `b` in one of its two orientations."""
    bw, bh = b[2] - b[0], b[3] - b[1]
    return (w <= bw + _EPS and h <= bh + _EPS) or (h <= bw + _EPS and w <= bh + _EPS)


def _minus(r: Box, f: Box, gap: float = 0.0) -> list[Box]:
    """`r` without `f` as at most four rectangles (guillotine; of the two split orders the
    one whose biggest piece is bigger). The two smaller pieces keep `gap` (a routing
    channel) from the two full bands they touch."""
    if not _overlap(r, f):
        return [r]
    x0, y0, x1, y1 = max(r[0], f[0]), max(r[1], f[1]), min(r[2], f[2]), min(r[3], f[3])
    top, bottom = y0 - r[1] > _EPS, r[3] - y1 > _EPS
    left, right = x0 - r[0] > _EPS, r[2] - x1 > _EPS
    my0, my1 = y0 + (gap if top else 0.0), y1 - (gap if bottom else 0.0)
    mx0, mx1 = x0 + (gap if left else 0.0), x1 - (gap if right else 0.0)
    rows = [  # full-width bands first
        (r[0], r[1], r[2], y0),
        (r[0], y1, r[2], r[3]),
        (r[0], my0, x0, my1),
        (x1, my0, r[2], my1),
    ]
    cols = [  # full-height bands first
        (r[0], r[1], x0, r[3]),
        (x1, r[1], r[2], r[3]),
        (mx0, r[1], mx1, y0),
        (mx0, y1, mx1, r[3]),
    ]
    best = max((rows, cols), key=lambda ps: (max(_area(p) for p in ps), ps is rows))
    return [p for p in best if p[2] - p[0] > _EPS and p[3] - p[1] > _EPS]


# ---- board.md ----------------------------------------------------------------------------------


def parse_region(text: str, usable: Box) -> Rect | None:
    """A `board.md` Blocks Region cell as a rectangle: four numbers = x, y, w, h (mm, board
    coordinates); `left` / `right` / `top` / `bottom` (optionally "half", "side") = that
    half of the usable area; two words like `top-left` = that quarter. Anything else (free
    text such as "near J1") → None: the block is placed freely."""
    t = text.casefold()
    nums = _NUM.findall(t)
    if len(nums) == 4:
        x, y, w, h = (float(n) for n in nums)
        return Rect(x, y, w, h) if w > 0 and h > 0 else None
    if nums:
        return None
    words = set(re.findall(r"[a-z]+", t))
    vert = [e for e in ("top", "bottom") if e in words]
    horiz = [e for e in ("left", "right") if e in words]
    if len(vert) > 1 or len(horiz) > 1 or not (vert or horiz):
        return None
    if words - {
        "top",
        "bottom",
        "left",
        "right",
        "half",
        "side",
        "quarter",
        "corner",
        "of",
        "the",
        "board",
    }:
        return None
    x0, y0, x1, y1 = usable
    mx, my = (x0 + x1) / 2, (y0 + y1) / 2
    if horiz == ["left"]:
        x1 = mx
    elif horiz == ["right"]:
        x0 = mx
    if vert == ["top"]:
        y1 = my
    elif vert == ["bottom"]:
        y0 = my
    return _rect((x0, y0, x1, y1))


# `<edge>[,:;@ ]<n>[ mm][ from <top|left>][, <comment>]` - nothing else is a position
_EDGE_POS = re.compile(
    r"^\s*(top|bottom|left|right)\b[\s,:;@]*(\d+(?:\.\d+)?)\s*(?:mm\b)?"
    r"(?:\s*from\s+(?:the\s+)?(\w+)(?:\s+(?:end|edge|corner))?)?\s*(?:[,;(].*)?$"
)
# the end of the edge an offset is measured from (the outline's top / left end)
_FROM = {"top": "left", "bottom": "left", "left": "top", "right": "top"}


def _edge_offset(position: str) -> float | None:
    """mm along the edge from the outline's top / left end (as `pcb_place_block`'s offset)
    in `<edge>, <n> mm`, optionally `from top` (left/right edges) or `from left` (top/bottom
    edges) and a trailing `, comment`; anything else → None (the edge middle): a reference
    from the other end, a negative number, or a number that is no position ("USB 2.0",
    "top, 2 x USB")."""
    m = _EDGE_POS.match(position.casefold())
    if m is None:
        return None
    edge, value, ref = m.group(1), m.group(2), m.group(3)
    if ref is not None and ref != _FROM[edge]:
        return None
    return float(value)


# ---- the floorplan -----------------------------------------------------------------------------


@dataclass
class _Ctx:
    boxes: dict[str, BlockBox]
    weight: dict[tuple[str, str], int]  # unordered block pair (sorted) -> shared nets
    nets: dict[str, frozenset[str]]  # block -> its non-ground nets
    where: dict[str, Point]  # current centre of every block's region
    keep: float
    channels: list[tuple[Point, Point, float]] = field(default_factory=list)
    leaves: dict[str, Rect] = field(default_factory=dict)
    budget: int = 0  # backtracking steps left (BISECT_BUDGET per bisection run)

    def w(self, a: str, b: str) -> int:
        return self.weight.get((a, b) if a < b else (b, a), 0)

    def channel(self, a: Sequence[str], b: Sequence[str]) -> float:
        na = frozenset().union(*(self.nets[x] for x in a))
        nb = frozenset().union(*(self.nets[x] for x in b))
        n = len(na & nb)
        c = min(max(CHANNEL_PER_NET_MM * n, CHANNEL_MIN_MM), CHANNEL_MAX_MM)
        return round(max(c, self.keep), 6)


def _members(model: BoardModel, b: BlockProposal) -> list[str]:
    known = {p.ref: p for p in model.parts}
    return sorted({r for r in b.refs if r in known and not known[r].locked}, key=natural_key)


def _block_box(
    model: BoardModel, cls: Classification, refs: Sequence[str], keep: float, relax: bool = False
) -> tuple[float, float, list[Move]]:
    local = list(block_layout(model, cls, refs, keep_channels=keep, escape_room=True, relax=relax))
    boxes = [box_of(moved_part(model.part(m.ref), m)) for m in local]
    x0 = min(b[0] for b in boxes)
    y0 = min(b[1] for b in boxes)
    x1 = max(b[2] for b in boxes)
    y1 = max(b[3] for b in boxes)
    pad = keep + PLACE_GRID_MM
    return round(x1 - x0 + pad, 6), round(y1 - y0 + pad, 6), local


def _facing_dims(
    model: BoardModel, local: Sequence[Move], ref: str, edge: Edge, w: float, h: float
) -> list[tuple[float, float]]:
    """(along, depth) of the block box for a strip on `edge`, best first: the rotation that
    turns the connector's mouth toward the edge first (P4's packing makes it face), the
    other orientation as the fallback."""
    n = _NORMAL[edge]
    along_x = edge in ("top", "bottom")
    plain = (w, h) if along_x else (h, w)  # rotation 0 / 180
    turned = (h, w) if along_x else (w, h)  # rotation 90 / 270
    best: tuple[float, int] | None = None
    for theta in (0, 90, 180, 270):
        mv = _orient(local, theta, "top")
        p = next((moved_part(model.part(m.ref), m) for m in mv if m.ref == ref), None)
        mouth = _mouth(p) if p is not None else None
        if mouth is None:
            return sorted({plain, turned}, key=lambda d: (d[1], d[0]))
        dot = mouth[0] * n[0] + mouth[1] * n[1]
        if best is None or dot > best[0] + 1e-9:
            best = (dot, theta)
    assert best is not None
    first = plain if best[1] in (0, 180) else turned
    return [first] + ([turned if first == plain else plain] if plain != turned else [])


def _strip(
    usable: Box, edge: Edge, along: float, depth: float, offset: float | None, origin: Point
) -> Box | None:
    """The strip along `edge`, centred `offset` mm from the outline's top / left end
    (`origin` = the outline's corner), else on the edge middle; clamped into `usable`."""
    x0, y0, x1, y1 = usable
    length = (x1 - x0) if edge in ("top", "bottom") else (y1 - y0)
    if along > length + _EPS or depth > ((y1 - y0) if edge in ("top", "bottom") else (x1 - x0)):
        return None
    start = x0 if edge in ("top", "bottom") else y0
    base = origin[0] if edge in ("top", "bottom") else origin[1]
    c = start + length / 2 if offset is None else base + offset
    lo = min(max(c - along / 2, start), start + length - along)
    hi = lo + along
    if edge == "top":
        return (lo, y0, hi, y0 + depth)
    if edge == "bottom":
        return (lo, y1 - depth, hi, y1)
    if edge == "left":
        return (x0, lo, x0 + depth, hi)
    return (x1 - depth, lo, x1, hi)


def _slide(usable: Box, edge: Edge, b: Box, taken: Sequence[Box]) -> Box | None:
    """`b` moved along its edge to the nearest spot (0.5 mm steps) overlapping none of
    `taken`; None when there is none."""
    horizontal = edge in ("top", "bottom")
    lo, hi = (usable[0], usable[2]) if horizontal else (usable[1], usable[3])
    size = (b[2] - b[0]) if horizontal else (b[3] - b[1])
    here = b[0] if horizontal else b[1]
    steps = int((hi - lo) / PLACE_GRID_MM) + 1
    for k in sorted(range(-steps, steps + 1), key=lambda k: (abs(k), k)):
        s = here + k * PLACE_GRID_MM
        if s < lo - _EPS or s + size > hi + _EPS:
            continue
        cand = (s, b[1], s + size, b[3]) if horizontal else (b[0], s, b[2], s + size)
        if not any(_overlap(cand, t) for t in taken):
            return cand
    return None


def _split_cost(
    ctx: _Ctx, a: Sequence[str], b: Sequence[str], outside: Sequence[str], cut: int, mid: float
) -> int:
    """Shared nets across the cut + pull: a block's weight to an outside block whose region
    centre lies on the other side of the rectangle's midline."""
    c = sum(ctx.w(x, y) for x in a for y in b)
    for o in outside:
        at = ctx.where.get(o)
        if at is None:  # not assigned anywhere yet (a trial bisection in `_assign`)
            continue
        side = at[cut]
        if abs(side - mid) < _EPS:
            continue
        other = b if side < mid else a  # a is the low side
        c += sum(ctx.w(x, o) for x in other)
    return c


def _need(ctx: _Ctx, names: Sequence[str], cross: float) -> float:
    """The least extent along the cut axis that holds the largest block of `names` (its
    other side fitting the rectangle's cross extent `cross`)."""
    need = 0.0
    for n in names:
        bb = ctx.boxes[n]
        opts = [d for d, o in ((bb.w, bb.h), (bb.h, bb.w)) if o <= cross + _EPS]
        if not opts:
            return math.inf
        need = max(need, min(opts))
    return need


def _halves(r: Box, cut: int, pos: float, ch: float) -> tuple[Box, Box]:
    if cut == 0:
        return (r[0], r[1], pos - ch / 2, r[3]), (pos + ch / 2, r[1], r[2], r[3])
    return (r[0], r[1], r[2], pos - ch / 2), (r[0], pos + ch / 2, r[2], r[3])


def _holds(ctx: _Ctx, names: Sequence[str], r: Box) -> bool:
    return _area(r) + _EPS >= sum(ctx.boxes[n].w * ctx.boxes[n].h for n in names) and all(
        _fits(ctx.boxes[n].w, ctx.boxes[n].h, r) for n in names
    )


def _partitions(names: Sequence[str]) -> Iterable[tuple[tuple[str, ...], tuple[str, ...]]]:
    n = len(names)
    for mask in range(1, (1 << n) - 1):
        a = tuple(names[i] for i in range(n) if mask >> i & 1)
        b = tuple(names[i] for i in range(n) if not mask >> i & 1)
        yield a, b


_Key = Callable[[tuple[str, ...], tuple[str, ...]], tuple[object, ...]]


def _greedy(names: Sequence[str], key: _Key) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Kernighan-Lin-style: an area-alternating start, then single moves and pair swaps
    while the key improves (bounded passes)."""
    a = list(names[0::2])
    b = list(names[1::2])
    best = key(tuple(a), tuple(b))
    for _ in range(GREEDY_PASSES):
        improved = False
        cands: list[tuple[list[str], list[str]]] = []
        for x in a:
            if len(a) > 1:
                cands.append(([y for y in a if y != x], [*b, x]))
        for x in b:
            if len(b) > 1:
                cands.append(([*a, x], [y for y in b if y != x]))
        for x in a:
            for y in b:
                cands.append(([*(z for z in a if z != x), y], [*(z for z in b if z != y), x]))
        for ca, cb in cands:
            ta = tuple(sorted(ca, key=natural_key))
            tb = tuple(sorted(cb, key=natural_key))
            k = key(ta, tb)
            if k < best:
                best, a, b, improved = k, list(ta), list(tb), True
                break
        if not improved:
            break
    return tuple(sorted(a, key=natural_key)), tuple(sorted(b, key=natural_key))


def _bisect(ctx: _Ctx, r: Box, names: Sequence[str], outside: Sequence[str]) -> None:
    if not names:
        return
    if len(names) == 1:
        n = names[0]
        if not _fits(ctx.boxes[n].w, ctx.boxes[n].h, r):
            raise _NoFit(ctx.boxes[n].refs)
        ctx.leaves[n] = _rect(r)
        ctx.where[n] = _centre(r)
        return
    total = sum(ctx.boxes[n].w * ctx.boxes[n].h for n in names)
    rw, rh = r[2] - r[0], r[3] - r[1]
    first = 0 if rw >= rh else 1  # 0: a vertical cut line (x = pos), across the longer side
    for cut in (first, 1 - first):
        lo, hi = (r[0], r[2]) if cut == 0 else (r[1], r[3])
        cross = rh if cut == 0 else rw
        mid = (lo + hi) / 2

        def layout(
            a: Sequence[str],
            b: Sequence[str],
            *,
            cut: int = cut,
            lo: float = lo,
            hi: float = hi,
            cross: float = cross,
        ) -> tuple[float, float, Box, Box, bool, bool]:
            area_a = sum(ctx.boxes[n].w * ctx.boxes[n].h for n in a)
            ch = ctx.channel(a, b)
            pos = lo + (hi - lo) * area_a / total
            ra, rb = _halves(r, cut, pos, ch)
            ok = _holds(ctx, a, ra) and _holds(ctx, b, rb)
            moved = False
            if not ok:  # rebalance once: move the cut toward the side that needs room
                na, nb = _need(ctx, a, cross), _need(ctx, b, cross)
                if na + nb + ch <= hi - lo + _EPS:
                    pos = min(max(pos, lo + na + ch / 2), hi - nb - ch / 2)
                    ra, rb = _halves(r, cut, pos, ch)
                    ok = _holds(ctx, a, ra) and _holds(ctx, b, rb)
                    moved = True
            return pos, ch, ra, rb, ok, moved

        def key(
            a: tuple[str, ...], b: tuple[str, ...], *, mid: float = mid, cut: int = cut
        ) -> tuple[object, ...]:
            frac = sum(ctx.boxes[n].w * ctx.boxes[n].h for n in a) / total
            balanced = BALANCE_LO - _EPS <= frac <= BALANCE_HI + _EPS
            _, _, _, _, ok, _ = layout(a, b)
            cost = _split_cost(ctx, a, b, outside, cut, mid)
            return (not ok, not balanced, cost, round(abs(frac - 0.5), 9), a)

        ordered = sorted(names, key=natural_key)
        if len(ordered) <= EXHAUSTIVE_MAX:
            ranked = sorted(((key(*ab), ab) for ab in _partitions(ordered)), key=lambda t: t[0])
            cands = [ab for k, ab in ranked if not k[0]][:BACKTRACK_TRIES]
        else:
            cands = [_greedy(ordered, key)]
        for a, b in cands:
            pos, ch, ra, rb, ok, _moved = layout(a, b)
            if not ok:
                continue
            if ctx.budget <= 0 and (a, b) != cands[0]:
                break  # out of work budget: only the best partition is tried
            ctx.budget -= 1
            saved = (dict(ctx.leaves), dict(ctx.where), len(ctx.channels))
            line = ((pos, r[1]), (pos, r[3])) if cut == 0 else ((r[0], pos), (r[2], pos))
            ctx.channels.append((line[0], line[1], ch))
            ctx.where.update({n: _centre(ra) for n in a})
            ctx.where.update({n: _centre(rb) for n in b})
            try:
                _bisect(ctx, ra, a, [*outside, *b])
                _bisect(ctx, rb, b, [*outside, *a])
            except _NoFit:  # a deeper cut failed: the next partition (backtracking)
                ctx.leaves, ctx.where = saved[0], saved[1]
                del ctx.channels[saved[2] :]
                continue
            return
    raise _NoFit([ref for n in sorted(names, key=natural_key) for ref in ctx.boxes[n].refs])


def floorplan(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    *,
    keep_channels: float,
    allowed_sides: frozenset[Side],
    relax: bool = False,
) -> Floorplan | DoesNotFit:
    """Regions for `blocks` on the whole board (module docstring). `DoesNotFit` carries
    `courtyard_fill`. Raises `ValueError` without an outline. `relax` (R2b): the block
    boxes of the relaxed layout."""
    del allowed_sides  # the blocks go on the top side; the packer checks the side policy
    o = model.outline
    if o.kind == "none":
        raise ValueError("the floorplan needs a board outline")
    kc = keep_channels
    m = edge_margin(model)
    usable: Box = (o.x + m, o.y + m, o.x + o.w - m, o.y + o.h - m)
    blocks = sorted(blocks, key=lambda b: natural_key(b.name))
    members = {b.name: _members(model, b) for b in blocks}
    moving = {r for refs in members.values() for r in refs}
    obstacles = [
        box_of(p)
        for p in model.parts
        if p.ref not in moving
        and p.side == "top"
        and len(p.courtyard) + len(p.pads) > 0
        and on_board(model, p)
    ]
    # parts that stay and are not small fixtures (mounting holes, fiducials, test points:
    # the packing steps around those) also come out of the free rectangles below
    staying = [
        box_of(p)
        for p in model.parts
        if p.ref not in moving
        and p.side == "top"
        and len(p.courtyard) + len(p.pads) > 0
        and on_board(model, p)
        and cls.part_class.get(p.ref) not in _FIXTURES
    ]
    obstacles += [_keepout_box(k) for k in keepout_polys(model)]
    staying += [_keepout_box(k) for k in keepout_polys(model)]
    usable_mm2 = round(max(_area(usable) - _union_area(obstacles, usable), 0.0), 3)
    court = 0.0
    for refs in members.values():
        for r in refs:
            p = model.part(r)
            court += polygon_area(p.courtyard) if len(p.courtyard) >= 3 else _area(box_of(p))
    court = round(court, 3)
    fill = round(court / usable_mm2, 4) if usable_mm2 > 0 else math.inf
    all_refs = tuple(sorted(moving, key=natural_key))

    def no_fit(
        refs: Sequence[str] = all_refs, needed: float | None = None, available: float | None = None
    ) -> DoesNotFit:
        return DoesNotFit(
            needed_mm2=round(needed if needed is not None else court, 3),
            available_mm2=round(available if available is not None else usable_mm2, 3),
            refs=tuple(refs),
            courtyard_fill=fill,
        )

    edge_rows = {row.ref: row for row in doc.edges if row.edge is not None}
    region_rows = {row.name: row for row in doc.blocks}
    boxes: dict[str, BlockBox] = {}
    locals_: dict[str, list[Move]] = {}
    for b in blocks:
        refs = members[b.name]
        if not refs:
            continue
        w, h, local = _block_box(model, cls, refs, kc, relax)
        locals_[b.name] = local
        edge_ref = next(
            (r for r in ([b.anchor] if b.anchor else []) + refs if r in edge_rows and r in refs),
            None,
        )
        row = region_rows.get(b.name)
        region = parse_region(row.region, usable) if row is not None else None
        boxes[b.name] = BlockBox(
            name=b.name,
            refs=tuple(refs),
            w=w,
            h=h,
            edge=edge_rows[edge_ref].edge if edge_ref is not None and region is None else None,
            region=region,
            offset=_edge_offset(edge_rows[edge_ref].position) if edge_ref is not None else None,
            edge_ref=edge_ref,
        )
    if not boxes:
        return Floorplan({}, (), fill if usable_mm2 > 0 else 0.0, usable_mm2, court)
    needed = sum(bb.w * bb.h for bb in boxes.values())
    if needed > PACK_MAX * usable_mm2 + _EPS:
        # what the blocks may use (PACK_MAX of the usable area): reported as an area refusal
        return no_fit(needed=needed, available=PACK_MAX * usable_mm2)

    nets: dict[str, frozenset[str]] = {}
    for name, bb in boxes.items():
        nets[name] = frozenset(
            q.net
            for r in bb.refs
            for q in model.part(r).pads
            if q.net and cls.net_kind.get(q.net) != "ground"
        )
    weight: dict[tuple[str, str], int] = {}
    for x, y in itertools.combinations(sorted(boxes), 2):
        k = len(nets[x] & nets[y])
        if k:
            weight[(x, y)] = k
    ctx = _Ctx(boxes=boxes, weight=weight, nets=nets, where={}, keep=kc)

    # fixed blocks: Region rows, then edge strips (explicit positions before slid ones)
    fixed: dict[str, Box] = {}
    for name, bb in sorted(boxes.items(), key=lambda kv: natural_key(kv[0])):
        if bb.region is None:
            continue
        rb = _box(bb.region)
        rb = (
            max(rb[0], usable[0]),
            max(rb[1], usable[1]),
            min(rb[2], usable[2]),
            min(rb[3], usable[3]),
        )
        if not _fits(bb.w, bb.h, rb) or any(_overlap(rb, f) for f in fixed.values()):
            return no_fit(bb.refs)
        fixed[name] = rb
    edged = [n for n, bb in boxes.items() if bb.edge is not None]
    edged.sort(key=lambda n: (boxes[n].offset is None, natural_key(n)))
    for name in edged:
        bb = boxes[name]
        assert bb.edge is not None
        strip: Box | None = None
        assert bb.edge_ref is not None
        for along, depth in _facing_dims(model, locals_[name], bb.edge_ref, bb.edge, bb.w, bb.h):
            strip = _strip(usable, bb.edge, along, depth, bb.offset, (o.x, o.y))
            if strip is not None:
                break
        if strip is None:
            return no_fit(bb.refs)
        taken = list(fixed.values())
        if any(_overlap(strip, t) for t in taken):
            slid = _slide(usable, bb.edge, strip, taken) if bb.offset is None else None
            if slid is None:
                clash = [n for n, f in fixed.items() if _overlap(strip, f)]
                return no_fit([*bb.refs, *(ref for n in clash for ref in boxes[n].refs)])
            strip = slid
        fixed[name] = strip
    for name, f in fixed.items():
        ctx.leaves[name] = _rect(f)
        ctx.where[name] = _centre(f)

    # the free rectangles: the usable box, inset by an edge channel (routing room between
    # a free block and the board edge; edge blocks sit on their edge), minus every fixed
    # region grown by its channel
    ec = max(kc, CHANNEL_MIN_MM)
    free: list[Box] = [(usable[0] + ec, usable[1] + ec, usable[2] - ec, usable[3] - ec)]
    for name, f in sorted(fixed.items(), key=lambda kv: natural_key(kv[0])):
        rest = [n for n in boxes if n != name]
        ch = ctx.channel([name], rest) if rest else ctx.keep
        g = (f[0] - ch, f[1] - ch, f[2] + ch, f[3] + ch)
        free = [piece for fr in free for piece in _minus(fr, g, ch)]
        sides = {
            "left": f[0] > usable[0] + _EPS,
            "right": f[2] < usable[2] - _EPS,
            "top": f[1] > usable[1] + _EPS,
            "bottom": f[3] < usable[3] - _EPS,
        }
        for s, open_ in sides.items():
            if not open_:
                continue
            if s in ("left", "right"):
                lx = f[0] - ch / 2 if s == "left" else f[2] + ch / 2
                ctx.channels.append(((lx, f[1]), (lx, f[3]), ch))
            else:
                ly = f[1] - ch / 2 if s == "top" else f[3] + ch / 2
                ctx.channels.append(((f[0], ly), (f[2], ly), ch))
    for ob in sorted(staying):
        g = (ob[0] - ec, ob[1] - ec, ob[2] + ec, ob[3] + ec)
        free = [piece for fr in free for piece in _minus(fr, g, ec)]
    free_names = sorted((n for n in boxes if n not in fixed), key=natural_key)
    if free_names:
        smallest = min(min(boxes[n].w, boxes[n].h) for n in free_names)
        free = [fr for fr in free if min(fr[2] - fr[0], fr[3] - fr[1]) + _EPS >= smallest]
        free.sort(key=lambda fr: (-_area(fr), fr[1], fr[0]))
        free = free[:MAX_FREE_RECTS]
        if not free:
            return no_fit()
        groups = _assign(ctx, free, free_names, fixed)
        for fr, names in zip(free, groups, strict=True):
            for n in names:
                ctx.where[n] = _centre(fr)
        try:
            for fr, names in zip(free, groups, strict=True):
                outside = [n for n in boxes if n not in names]
                ctx.budget = BISECT_BUDGET
                _bisect(ctx, fr, names, outside)
        except _NoFit as exc:
            return no_fit(exc.refs)
    regions = {n: ctx.leaves[n] for n in sorted(ctx.leaves, key=natural_key)}
    return Floorplan(
        regions=regions,
        channels=tuple(
            (
                (round(a[0], 4), round(a[1], 4)),
                (round(b[0], 4), round(b[1], 4)),
                w,
            )
            for a, b, w in ctx.channels
        ),
        courtyard_fill=fill,
        usable_mm2=usable_mm2,
        courtyard_mm2=court,
        fixed=tuple(sorted(fixed, key=natural_key)),
    )


def _assign(
    ctx: _Ctx, free: Sequence[Box], names: Sequence[str], fixed: Mapping[str, Box]
) -> list[list[str]]:
    """Free blocks per free rectangle: all to the biggest, except blocks sharing nets with a
    fixed block that a smaller rectangle touches (they go there while it holds them), and
    blocks the biggest cannot hold (they go where they fit)."""
    groups: list[list[str]] = [list(names)] + [[] for _ in free[1:]]
    if len(free) == 1:
        return groups

    def used(i: int) -> float:
        return sum(ctx.boxes[n].w * ctx.boxes[n].h for n in groups[i])

    def touches(r: Box, f: Box) -> bool:
        g = CHANNEL_MAX_MM + _EPS
        return r[0] <= f[2] + g and f[0] <= r[2] + g and r[1] <= f[3] + g and f[1] <= r[3] + g

    for i, r in enumerate(free[1:], start=1):
        near = [fn for fn, f in fixed.items() if touches(r, f)]
        cands = sorted(
            groups[0],
            key=lambda n: (
                -sum(ctx.w(n, fn) for fn in near),
                -(ctx.boxes[n].w * ctx.boxes[n].h),
                natural_key(n),
            ),
        )
        for n in cands:
            pull = sum(ctx.w(n, fn) for fn in near)
            over = used(0) > PACK_MAX * _area(free[0])
            if pull <= 0 and not over:
                continue
            bb = ctx.boxes[n]
            if not _fits(bb.w, bb.h, r) or used(i) + bb.w * bb.h > PACK_MAX * _area(r) + _EPS:
                continue
            if len(groups[0]) == 1:
                break
            if not _tiles(ctx, r, [*groups[i], n]):  # the shapes must tile the piece too
                continue
            groups[0].remove(n)
            groups[i].append(n)
    return [sorted(g, key=natural_key) for g in groups]


def _tiles(ctx: _Ctx, r: Box, names: Sequence[str]) -> bool:
    """A trial bisection of `names` in `r` succeeds (on a scratch copy of `ctx`)."""
    trial = dataclasses.replace(
        ctx, where=dict(ctx.where), channels=[], leaves={}, budget=BISECT_BUDGET
    )
    try:
        _bisect(trial, r, sorted(names, key=natural_key), [n for n in ctx.boxes if n not in names])
    except _NoFit:
        return False
    return True


# ---- placing over the floorplan ------------------------------------------------------------


def place_blocks(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    *,
    keep_channels: float,
    allowed_sides: frozenset[Side],
) -> tuple[PlaceResult, Floorplan] | DoesNotFit:
    """R2b: compact, relaxed, connectivity-ordered (owner 2026-11-27). The topology: the
    relaxed `floorplan`, else the compact one, else (the shapes do not tile, the area
    does) one of the fixed blocks alone - its regions place the fixed blocks, its region
    centres pull the free blocks (none: the usable centre). Then per layout (relaxed first,
    then P4's compact one) and slack step (`SLACK_STEPS_MM`) `_pack_ordered`; the first that
    places every block wins. One global legalise, `align_pairs`, the score (incl. `spread`)
    and the gap rows. Nothing fits: R2's region packing (`_pack_regions`), else the compact
    floorplan's `DoesNotFit` (with `courtyard_fill`)."""

    def plan(bs: Sequence[BlockProposal], relax: bool) -> Floorplan | DoesNotFit:
        return floorplan(
            model, cls, doc, bs, keep_channels=keep_channels, allowed_sides=allowed_sides,
            relax=relax,
        )  # fmt: skip

    compact = plan(blocks, False)
    topo: Floorplan | DoesNotFit = plan(blocks, True)
    if isinstance(topo, DoesNotFit):
        topo = compact
    if isinstance(topo, DoesNotFit):
        # no box tiling (a shape or an area refusal of the boxes): the constructive packing
        # interlocks the blocks' parts and may still fit; the fixed blocks' regions alone
        # (on the model with every member parked: the free blocks' stale positions are no
        # obstacles; without fixed blocks this is just the usable area)
        fixed_only = [b for b in blocks if _fixed_block(model, doc, b)]
        topo = floorplan(
            _parked(model, blocks), cls, doc, fixed_only, keep_channels=keep_channels,
            allowed_sides=allowed_sides,
        )  # fmt: skip
    work = _Work(PACK_BUDGET)
    if not isinstance(topo, DoesNotFit):
        for relax in (True, False):
            best: tuple[float, tuple[PlaceResult, Floorplan]] | None = None
            for slack, pull in itertools.product(SLACK_STEPS_MM, TARGET_PULL_WEIGHTS):
                got = _pack_retry(
                    model, cls, doc, blocks, topo, keep=keep_channels,
                    sides=allowed_sides, relax=relax, slack=slack, pull=pull, work=work,
                )  # fmt: skip
                if got is None:
                    continue
                moves = got.moves
                fp = dataclasses.replace(
                    topo, slack_mm=slack, order=got.order, relaxed=relax,
                    fallback=got.fallback, compact_fixed=got.compact,
                )  # fmt: skip
                if isinstance(compact, DoesNotFit):  # the fill of every block
                    court = _courtyards(model, blocks)
                    fp = dataclasses.replace(
                        fp,
                        courtyard_fill=compact.courtyard_fill or 0.0,
                        courtyard_mm2=court,
                    )
                else:
                    fp = dataclasses.replace(
                        fp,
                        courtyard_fill=compact.courtyard_fill,
                        courtyard_mm2=compact.courtyard_mm2,
                        usable_mm2=compact.usable_mm2,
                    )
                result = _finish(model, cls, doc, blocks, fp, moves, keep_channels)
                if isinstance(result, DoesNotFit):
                    continue
                cost = result[0].score.airwire_mm * (1.0 - SLACK_CREDIT * slack)
                if best is None or cost < best[0] - _EPS:
                    best = (cost, result)
            if best is not None:
                return best[1]
    if isinstance(compact, DoesNotFit):
        return compact
    got_r = _pack_regions(model, cls, doc, blocks, compact, keep_channels, allowed_sides)
    if isinstance(got_r, DoesNotFit):
        return got_r
    fp, moves = got_r
    return _finish(model, cls, doc, blocks, fp, moves, keep_channels)


def _courtyards(model: BoardModel, blocks: Sequence[BlockProposal]) -> float:
    """Σ courtyard areas of the blocks' members (the bbox for a part without one)."""
    total = 0.0
    for b in blocks:
        for r in _members(model, b):
            p = model.part(r)
            total += polygon_area(p.courtyard) if len(p.courtyard) >= 3 else _area(box_of(p))
    return round(total, 3)


def _fixed_block(model: BoardModel, doc: BoardDoc, b: BlockProposal) -> bool:
    """A block with a `board.md` Blocks row naming a region, or a member with a Placement
    edges row (the blocks `floorplan` places first)."""
    o = model.outline
    usable: Box = (o.x, o.y, o.x + o.w, o.y + o.h)
    row = next((r for r in doc.blocks if r.name == b.name), None)
    if row is not None and parse_region(row.region, usable) is not None:
        return True
    edged = {r.ref for r in doc.edges if r.edge is not None}
    return any(r in edged for r in _members(model, b))


def _parked(model: BoardModel, blocks: Sequence[BlockProposal]) -> BoardModel:
    """`model` with every member of this run parked `PARK_DX_MM` right of the board: a
    block is packed around the blocks placed before it, never around stale positions of
    members still to come (R2, review round 1)."""
    o = model.outline
    everyone = sorted({r for b in blocks for r in _members(model, b)}, key=natural_key)
    park = [
        Move(r, model.part(r).x + o.w + PARK_DX_MM, model.part(r).y, model.part(r).rot,
             model.part(r).side)
        for r in everyone
    ]  # fmt: skip
    return apply_moves(model, park)


def _block_nets(model: BoardModel, refs: Sequence[str]) -> frozenset[str]:
    return frozenset(q.net for r in refs for q in model.part(r).pads if q.net)


@dataclass(frozen=True)
class _Packed:
    order: tuple[str, ...]
    moves: tuple[Move, ...]
    fallback: tuple[str, ...]  # fixed blocks packed on the whole board
    compact: tuple[str, ...]  # fixed blocks packed in P4's compact layout in their strip


@dataclass
class _Work:
    """`pack_block` calls left for this `place_blocks` run (`PACK_BUDGET`)."""

    left: int


def _pack_ordered(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    fp: Floorplan,
    *,
    keep: float,
    sides: frozenset[Side],
    relax: bool,
    slack: float,
    pull: float,
    first: Sequence[str] = (),
    work: _Work | None = None,
) -> _Packed | str:
    """The blocks packed one by one (R2b): the fixed ones first (largest first, each in its
    region), then always the block with the most weighted shared nets to the placed ones
    (ties: larger courtyard area, name), on the whole usable box against the parts already
    placed, pulled `pull` (per member and mm) toward its floorplan region centre; every placed
    part keeps `channel_mm(shared nets of the two blocks) + slack` (its own block's parts
    are not obstacles yet). The blocks in `first` go right after the fixed ones, in that
    order. Returns the order, the moves, the fixed blocks packed on the whole board and the
    fixed blocks packed in P4's compact layout (their strip was sized for it); a block that
    finds no spot (or `work` is used up): its name."""
    o = model.outline
    m = edge_margin(model)
    whole = Rect(o.x + m, o.y + m, o.w - 2 * m, o.h - 2 * m)
    refs = {b.name: _members(model, b) for b in blocks if _members(model, b)}
    nets = {n: _block_nets(model, r) for n, r in refs.items()}
    area = {n: sum(_area(box_of(model.part(x))) for x in r) for n, r in refs.items()}
    pitch = signal_pitch(model)
    fixed_set = frozenset(fp.fixed)
    base = _parked(model, blocks)
    placed: list[str] = []
    fallback: list[str] = []
    compact_fixed: list[str] = []
    owner: dict[str, str] = {}
    moves: tuple[Move, ...] = ()
    todo = sorted(refs, key=natural_key)
    centre = (whole.x + whole.w / 2, whole.y + whole.h / 2)
    while todo:
        fixed_left = [n for n in todo if n in fixed_set]
        promoted = [n for n in first if n in todo]
        if fixed_left:
            n = min(fixed_left, key=lambda x: (-area[x], natural_key(x)))
        elif promoted:
            n = promoted[0]
        else:
            n = min(
                todo,
                key=lambda x: (
                    -sum(shared_weight(nets[x], nets[p], cls) for p in placed),
                    -area[x],
                    natural_key(x),
                ),
            )
        todo.remove(n)
        region = fp.regions.get(n)
        target = centre if region is None else (region.x + region.w / 2, region.y + region.h / 2)
        need = {
            p: channel_mm(shared_weight(nets[n], nets[p], cls), pitch, keep) + slack for p in placed
        }
        gaps = {r: need[owner[r]] for r in owner}
        # a fixed block in its region (relaxed, else in P4's compact layout when the strip
        # was sized for that), else on the whole board (its connector's edge cost keeps it
        # on the edge); a free block on the whole board
        where: list[tuple[Rect, bool]] = [(whole, relax)]
        if n in fixed_set and region is not None:
            where = [(region, relax), *([(region, False)] if relax else []), (whole, relax)]
        res: tuple[Move, ...] | DoesNotFit = DoesNotFit(0.0, 0.0, ())
        for reg, lay in where:
            if work is not None:
                if work.left <= 0:
                    return n
                work.left -= 1
            res = pack_block(
                base,
                cls,
                refs[n],
                reg,
                side="top",
                rotation=None,
                keep_channels=keep,
                fixed=moves,
                allowed_sides=sides,
                edge=None,
                target=target,
                doc=doc,
                pull_weight=REGION_PULL_WEIGHT if n in fixed_set else pull,
                occlude_weight=OCCLUDE_WEIGHT,
                escape_room=True,
                relax=lay,
                gaps=gaps,
            )
            if not isinstance(res, DoesNotFit):
                break
        if isinstance(res, DoesNotFit):
            return n
        if reg is whole and n in fixed_set:
            fallback.append(n)
        if relax and not lay:
            compact_fixed.append(n)
        moves = res
        placed.append(n)
        owner.update({r: n for r in refs[n]})
    return _Packed(
        tuple(placed),
        moves,
        tuple(sorted(fallback, key=natural_key)),
        tuple(sorted(compact_fixed, key=natural_key)),
    )


def _pack_retry(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    fp: Floorplan,
    *,
    keep: float,
    sides: frozenset[Side],
    relax: bool,
    slack: float,
    pull: float,
    work: _Work | None = None,
) -> _Packed | None:
    """`_pack_ordered`; a free block that finds no spot is moved to the front (after the
    fixed blocks and the blocks moved before it) and the packing starts again - at most
    once per block and `RETRY_MAX` times (the earlier blocks had boxed it out). None: no
    order fits."""
    first: list[str] = []
    for _ in range(min(len(blocks), RETRY_MAX) + 1):
        got = _pack_ordered(
            model, cls, doc, blocks, fp, keep=keep, sides=sides, relax=relax, slack=slack,
            pull=pull, first=first, work=work,
        )  # fmt: skip
        if not isinstance(got, str):
            return got
        if got in first or got in fp.fixed or (work is not None and work.left <= 0):
            return None
        first.append(got)
    return None


def _pack_regions(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    fp: Floorplan,
    keep_channels: float,
    allowed_sides: frozenset[Side],
) -> tuple[Floorplan, tuple[Move, ...]] | DoesNotFit:
    """R2's packing: every block into its region (fixed blocks first, then the largest;
    earlier ones as `fixed`), pulled `REGION_PULL_WEIGHT` toward the region centre; a block
    that does not fit its region gets the whole board once (`Floorplan.fallback`)."""
    o = model.outline
    m = edge_margin(model)
    whole = Rect(o.x + m, o.y + m, o.w - 2 * m, o.h - 2 * m)
    by_name = {b.name: b for b in blocks}

    def size(n: str) -> float:
        return sum(_area(box_of(model.part(x))) for x in _members(model, by_name[n]))

    base = _parked(model, blocks)
    fixed_set = frozenset(fp.fixed)
    order = sorted(fp.regions, key=lambda n: (n not in fixed_set, -size(n), natural_key(n)))
    moves: tuple[Move, ...] = ()
    fallback: list[str] = []
    for n in order:
        refs = _members(model, by_name[n])
        region = fp.regions[n]
        target = (region.x + region.w / 2, region.y + region.h / 2)
        res: tuple[Move, ...] | DoesNotFit = DoesNotFit(0.0, 0.0, ())
        for reg in (region, whole):
            res = pack_block(
                base,
                cls,
                refs,
                reg,
                side="top",
                rotation=None,
                keep_channels=keep_channels,
                fixed=moves,
                allowed_sides=allowed_sides,
                edge=None,
                target=target,
                doc=doc,
                pull_weight=REGION_PULL_WEIGHT,
                occlude_weight=OCCLUDE_WEIGHT,
                escape_room=True,
            )
            if not isinstance(res, DoesNotFit):
                break
            fallback.append(n)
        if isinstance(res, DoesNotFit):
            return dataclasses.replace(res, courtyard_fill=fp.courtyard_fill)
        moves = res
    fp = dataclasses.replace(fp, order=tuple(order))
    if fallback:
        fp = dataclasses.replace(fp, fallback=tuple(sorted(set(fallback), key=natural_key)))
    return fp, moves


def _finish(
    model: BoardModel,
    cls: Classification,
    doc: BoardDoc,
    blocks: Sequence[BlockProposal],
    fp: Floorplan,
    moves: tuple[Move, ...],
    keep: float,
) -> tuple[PlaceResult, Floorplan] | DoesNotFit:
    """One global legalise of this run's members, `align_pairs`, the score and gap rows."""
    members = frozenset(r for b in blocks for r in _members(model, b))
    base = _parked(model, blocks)
    legal = legalize(
        base,
        moves,
        movable=members,
        max_shift=None,
        keepouts=keepout_polys(model),
        edge_margin=edge_margin(model),
    )
    if isinstance(legal, DoesNotFit):
        return dataclasses.replace(legal, courtyard_fill=fp.courtyard_fill)
    merged = {mv.ref: mv for mv in moves}
    merged.update({mv.ref: mv for mv in legal})
    placed = tuple(merged[r] for r in sorted(merged, key=natural_key))
    after = apply_moves(model, placed)
    aligned = align_pairs(after, classify(after, doc), placed, movable=members)
    final = apply_moves(model, aligned)
    fcls = classify(final, doc)
    score = score_placement(final, fcls, doc)
    fp = dataclasses.replace(fp, gaps=block_gaps(final, fcls, blocks, keep=keep))
    return PlaceResult(aligned, score), fp


# ---- rule (b): pair alignment ----------------------------------------------------------------


def align_pairs(
    model: BoardModel, cls: Classification, moves: Sequence[Move], *, movable: frozenset[str]
) -> tuple[Move, ...]:
    """Rule (b) after the legalise, over the M16 alignment pairs (`metrics.alignment_pairs`),
    natural ref order; only a movable 2-pad part moves (never an anchor or a locked part):

    1. align: a pair without a straight corridor whose missing overlap is ≤
       `REVIEW["M16_align_mm"]` - one part shifts across the corridor axis, centre-aligned
       when that shift is small enough, else by the missing overlap (one shift per part);
    2. tighten: a pair with a corridor whose pads are more than `TIGHT_GAP_MM` apart - one
       part moves along the corridor toward the other, as far as the courtyards allow (the
       straight link gets short and no other track squeezes between the two pads); never a
       pair with a crystal (its `CRYSTAL_STANDOFF_MM` escape room stays).

    Every move keeps the courtyard inside the outline margin and off every other courtyard
    and keepout, keeps the pair's corridor and every corridor the part already has.
    `model` already holds `moves`; returns `moves` with the shifts applied."""
    parts: dict[str, Part] = {p.ref: p for p in model.parts}
    out = {mv.ref: mv for mv in moves}
    for r in movable:
        p = parts.get(r)
        if p is not None and r not in out:
            out[r] = Move(r, p.x, p.y, p.rot, p.side)
    pairs = alignment_pairs(model, cls)
    layers = model.copper_layers
    sep = courtyard_clearance(model)
    outline = Outline(model, edge_margin(model))
    keepouts = keepout_polys(model)
    pairs_of: dict[str, list[_PairKey]] = defaultdict(list)
    keyed: list[_PairKey] = []
    for _net, a, b, w in pairs:
        k = (a.ref, a.number, b.ref, b.number, w)
        pairs_of[a.ref].append(k)
        pairs_of[b.ref].append(k)
        keyed.append(k)
    keyed.sort(key=lambda t: (natural_key(t[0]), natural_key(t[2]), t[1], t[3], t[4]))

    def need(k: _PairKey) -> float | None:
        return alignment_need(_pad_of(parts, k[0], k[1]), _pad_of(parts, k[2], k[3]), k[4], layers)

    def movers(k: _PairKey, done: set[str]) -> list[tuple[Part, Pad, Pad]]:
        a, b = _pad_of(parts, k[0], k[1]), _pad_of(parts, k[2], k[3])
        order = ((b, a), (a, b)) if natural_key(b.ref) > natural_key(a.ref) else ((a, b), (b, a))
        out_: list[tuple[Part, Pad, Pad]] = []
        for mover, other in order:
            p = parts[mover.ref]
            if (
                mover.ref in movable
                and mover.ref not in done
                and not p.locked
                and len(p.pads) == 2
                and not is_anchor(p, cls)
            ):
                out_.append((p, mover, other))
        return out_

    def apply(p: Part, k: _PairKey, shifts: Sequence[Point]) -> bool:
        """The first shift that is legal and keeps the corridors (the pair's own included)."""
        had = [q for q in pairs_of[p.ref] if q != k and need(q) == 0.0]
        for dx, dy in shifts:
            cand = moved_part(p, Move(p.ref, p.x + dx, p.y + dy, p.rot, p.side))
            if not _legal_shift(cand, parts, sep, outline, keepouts):
                continue
            trial = {**parts, p.ref: cand}
            if any(_need_in(trial, q, layers) != 0.0 for q in (k, *had)):
                continue
            parts[p.ref] = cand
            out[p.ref] = Move(p.ref, round(cand.x, 6), round(cand.y, 6), p.rot, p.side)
            return True
        return False

    shifted: set[str] = set()
    for k in keyed:
        if not need(k):
            continue
        for p, mover, other in movers(k, shifted):
            if apply(p, k, _shifts(mover, other, k[4], REVIEW["M16_align_mm"])):
                shifted.add(p.ref)
                break
    tightened: set[str] = set()
    for k in keyed:
        if need(k) != 0.0:
            continue
        if _crystal_pair(k, cls):  # the crystal keeps its standoff (P9a: flush = boxed in)
            continue
        for p, mover, other in movers(k, tightened):
            if apply(p, k, _closer(mover, other)):
                tightened.add(p.ref)
                break
    return tuple(out[r] for r in sorted(out, key=natural_key))


def _crystal_pair(k: _PairKey, cls: Classification) -> bool:
    xs = {x.ref for x in cls.crystals}
    return k[0] in xs or k[2] in xs


def _need_in(parts: Mapping[str, Part], k: _PairKey, layers: Sequence[str]) -> float | None:
    return alignment_need(_pad_of(parts, k[0], k[1]), _pad_of(parts, k[2], k[3]), k[4], layers)


_PairKey = tuple[str, str, str, str, float]  # ref a, pad a, ref b, pad b, corridor width


def _pad_of(parts: Mapping[str, Part], ref: str, number: str) -> Pad:
    return next(q for q in parts[ref].pads if q.number == number)


def _shifts(mover: Pad, other: Pad, width: float, limit: float) -> list[Point]:
    """Candidate shifts of `mover` across the corridor axis: centre alignment first, then
    the least shift giving an overlap of `width` (both ≤ `limit`)."""
    bm, bo = _bbox_of(_pad_rect(mover)), _bbox_of(_pad_rect(other))
    out: list[Point] = []
    for ax in (0, 1):  # the corridor runs along ax; shift across o
        o = 1 - ax
        gap = max(bo[ax] - bm[ax + 2], bm[ax] - bo[ax + 2])
        if gap <= 0.0:
            continue
        centre = (bo[o] + bo[o + 2]) / 2 - (bm[o] + bm[o + 2]) / 2
        overlap = min(bm[o + 2], bo[o + 2]) - max(bm[o], bo[o])
        least = math.copysign(max(width - overlap, 0.0) + 1e-4, centre)
        for d in (centre, least):
            if abs(d) <= limit + 1e-9 and abs(d) > 1e-9:
                v = (d, 0.0) if o == 0 else (0.0, d)
                if v not in out:
                    out.append(v)
    return out


def _closer(mover: Pad, other: Pad) -> list[Point]:
    """Moves of `mover` along the corridor axis toward `other` that leave a pad gap of
    `TIGHT_GAP_MM` or more, largest first in `TIGHT_STEP_MM` steps (nothing when the pads
    are no more than `TIGHT_GAP_MM` apart)."""
    bm, bo = _bbox_of(_pad_rect(mover)), _bbox_of(_pad_rect(other))
    for ax in (0, 1):
        if bo[ax] - bm[ax + 2] > _EPS:
            gap, sign = bo[ax] - bm[ax + 2], 1.0
        elif bm[ax] - bo[ax + 2] > _EPS:
            gap, sign = bm[ax] - bo[ax + 2], -1.0
        else:
            continue
        n = math.floor((gap - TIGHT_GAP_MM) / TIGHT_STEP_MM + 1e-9)
        steps = [sign * TIGHT_STEP_MM * i for i in range(n, 0, -1)]
        return [(d, 0.0) if ax == 0 else (0.0, d) for d in steps]
    return []


def _legal_shift(
    cand: Part,
    parts: Mapping[str, Part],
    sep: float,
    outline: Outline,
    keepouts: Sequence[tuple[Point, ...]],
) -> bool:
    b = box_of(cand)
    if not outline.contains(b) or _hits_keepout(b, keepouts) is not None:
        return False
    for r, q in parts.items():
        if r == cand.ref or q.side != cand.side or len(q.courtyard) + len(q.pads) == 0:
            continue
        if boxes_hit(b, box_of(q), sep):
            return False
    return True
