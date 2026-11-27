"""THE one module of `run_cpu` job functions (B-C4, P3a). `run_cpu` accepts only module-level
functions of `copperchat.board.*` whose arguments pickle (frozen dataclasses); every board
computation that runs in the CPU worker is defined here so the worker imports one module.
Later phases append their jobs below (P3a `metrics_job`, P4 `place_block_job` /
`legalize_job`, P7 `fanout_candidates_job` / `stitch_candidates_job`, P5b/P6b as needed).

Also the in-process helper `strip_fills` (not a job: it is cheap and runs before a model is
sent to the worker, so filled-zone polygons are not pickled for computations that ignore
them)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any, Literal

from copperchat.board.boardmd import BoardDoc, Edge
from copperchat.board.classify import BlockProposal, Classification, classify
from copperchat.board.critical import critical_with_reasons
from copperchat.board.metrics import MetricsReport, compute_metrics
from copperchat.board.model import BoardModel, Point, Rect, Side
from copperchat.board.placer import (
    DoesNotFit,
    Move,
    PlacementScore,
    PlaceResult,
    block_extent,
    edge_margin,
    legalize,
    place_block,
    score_placement,
)
from copperchat.board.placer.floorplan import Floorplan, floorplan, place_blocks
from copperchat.board.placer.pack import PLACE_GRID_MM
from copperchat.board.planes import ViaCandidate, fanout_candidates, stitch_candidates
from copperchat.board.presets import FabPreset
from copperchat.board.silk import RefPlacement, plan_refs

if TYPE_CHECKING:
    from copperchat.kicad_tools.board.api import DrcMarker, DrcResult

__all__ = [
    "MAX_BLOCK_REFS",
    "MAX_ENTRIES",
    "MAX_ERROR_CHARS",
    "SCOPE_KEYS",
    "Scope",
    "analyse_job",
    "edge_strip",
    "fanout_candidates_job",
    "legalize_job",
    "metrics_job",
    "place_block_job",
    "score_job",
    "silk_plan_job",
    "stitch_candidates_job",
    "strip_fills",
]

Scope = Literal["parts", "nets", "blocks", "all"]
# Every list / mapping of the `pcb_analyze` result keeps at most this many entries.
MAX_ENTRIES = 500
# Each block lists at most this many refs (+ `refs_omitted`); each board.md error string is
# cut to this many characters (boardmd quotes offending cells): the result stays bounded.
MAX_BLOCK_REFS = 200
MAX_ERROR_CHARS = 200
_ALL_KEYS = (
    "parts",
    "nets",
    "decoupling",
    "switchers",
    "crystals",
    "pairs",
    "blocks",
    "critical_nets",
    "critical_reasons",
    "overrides_applied",
    "board_md_errors",
)
# The result keys of each `pcb_analyze` scope; `board_md_errors` is in every scope.
SCOPE_KEYS: Mapping[str, tuple[str, ...]] = {
    "parts": (
        "parts",
        "decoupling",
        "switchers",
        "crystals",
        "overrides_applied",
        "board_md_errors",
    ),
    "nets": ("nets", "pairs", "critical_nets", "critical_reasons", "board_md_errors"),
    "blocks": ("blocks", "board_md_errors"),
    "all": _ALL_KEYS,
}


def _plain(value: Any) -> Any:
    """A classification record as JSON-ready data (tuples → lists)."""
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {f.name: _plain(getattr(value, f.name)) for f in dataclasses.fields(value)}
    if isinstance(value, tuple | list):
        return [_plain(v) for v in value]
    return value


def _capped(data: dict[str, Any], key: str, value: Mapping[str, str] | Sequence[Any]) -> None:
    """`data[key]` = the first MAX_ENTRIES entries of `value` (order kept), and
    `data[f"{key}_omitted"]` = how many were dropped."""
    if isinstance(value, Mapping):
        items = list(value.items())
        data[key] = dict(items[:MAX_ENTRIES])
        n = len(items)
    else:
        data[key] = [_plain(v) for v in value[:MAX_ENTRIES]]
        n = len(value)
    data[f"{key}_omitted"] = max(0, n - MAX_ENTRIES)


def analyse_job(model: BoardModel, doc: BoardDoc, scope: Scope = "all") -> dict[str, Any]:
    """The `pcb_analyze` result (spec §7.1, B-C6): `classify(model, doc)` as JSON-ready data,
    restricted to the keys of `scope` (`SCOPE_KEYS`). Each key K holds a list (records as
    dicts, tuples as lists) or a mapping (`parts`: ref → class, `nets`: net → kind) of at
    most MAX_ENTRIES entries in classification order, plus `K_omitted` (entries dropped).
    Each block keeps at most MAX_BLOCK_REFS refs plus `refs_omitted`; each board.md error is
    cut to MAX_ERROR_CHARS characters."""
    c = classify(model, doc)
    # R4 (round-2 plan call 8): intra-block and straight-corridor nets join the critical set
    critical: tuple[str, ...] = ()
    reasons: dict[str, str] = {}
    if "critical_nets" in SCOPE_KEYS[scope]:  # the geometry only where it is reported
        critical, reasons = critical_with_reasons(model, c)
    full: dict[str, Mapping[str, str] | Sequence[Any]] = {
        "parts": c.part_class,
        "nets": c.net_kind,
        "decoupling": c.decoupling,
        "switchers": c.switchers,
        "crystals": c.crystals,
        "pairs": c.pairs,
        "blocks": c.blocks,
        "critical_nets": critical,
        "critical_reasons": reasons,
        "overrides_applied": c.overrides_applied,
        "board_md_errors": doc.errors,
    }
    data: dict[str, Any] = {}
    for key in SCOPE_KEYS[scope]:
        _capped(data, key, full[key])
    for block in data.get("blocks", ()):
        refs = block["refs"]
        block["refs"] = refs[:MAX_BLOCK_REFS]
        block["refs_omitted"] = max(0, len(refs) - MAX_BLOCK_REFS)
    data["board_md_errors"] = [
        e if len(e) <= MAX_ERROR_CHARS else e[: MAX_ERROR_CHARS - 3] + "..."
        for e in data["board_md_errors"]
    ]
    return data


def strip_fills(model: BoardModel) -> BoardModel:
    """`model` without filled-zone geometry: every zone with `filled`, `filled_holes` and
    `filled_layers` `()`; everything else (incl. `filled_area_mm2`) kept. In-process."""
    zones = tuple(
        dataclasses.replace(z, filled=(), filled_holes=(), filled_layers=()) for z in model.zones
    )
    return dataclasses.replace(model, zones=zones)


# ---- metrics (P3a, B-C7) -------------------------------------------------------------------


def metrics_job(
    model: BoardModel,
    doc: BoardDoc,
    drc: DrcResult | None,
    preset: FabPreset | None,
    only: frozenset[str] | None,
    parity: tuple[DrcMarker, ...] | None = None,
) -> MetricsReport:
    """Metrics M1-M16 (B-C7) of `model`: classifies it here, in the worker, and passes
    `parity` (full check only, P3b) through. Every metrics computation runs as
    `run_cpu(metrics_job, ...)`; callers strip fills (`strip_fills`) when M11 is not
    requested."""
    return compute_metrics(
        model, classify(model, doc), doc, drc=drc, parity=parity, preset=preset, only=only
    )


# ---- placement (P4, B-C11) -----------------------------------------------------------------


def edge_strip(
    model: BoardModel,
    cls: Classification,
    refs: Sequence[str],
    edge: Edge,
    keep_channels: float,
    offset: float | None,
) -> tuple[Rect, Point]:
    """The region and target of `pcb_place_block`'s edge form: a strip along `edge` of the
    outline's box, as deep as the block's largest layout extent + 2 x `keep_channels` + the
    edge margin + one placement grid step (the block's reference point sits on that grid);
    the target lies on the strip's centre line `offset` mm along the edge from its top /
    left end (default: the middle of the edge). Needs an outline."""
    o = model.outline
    depth = (
        block_extent(model, cls, refs, keep_channels=keep_channels)
        + 2 * keep_channels
        + edge_margin(model)
        + PLACE_GRID_MM
    )
    along = o.h if edge in ("left", "right") else o.w
    depth = min(depth, o.w if edge in ("left", "right") else o.h)
    t = along / 2 if offset is None else offset
    if edge == "left":
        return Rect(o.x, o.y, depth, o.h), (o.x + depth / 2, o.y + t)
    if edge == "right":
        return Rect(o.x + o.w - depth, o.y, depth, o.h), (o.x + o.w - depth / 2, o.y + t)
    if edge == "top":
        return Rect(o.x, o.y, o.w, depth), (o.x + t, o.y + depth / 2)
    return Rect(o.x, o.y + o.h - depth, o.w, depth), (o.x + t, o.y + o.h - depth / 2)


def place_block_job(
    model: BoardModel,
    doc: BoardDoc,
    refs: tuple[str, ...],
    region: Rect | None,
    *,
    side: Side,
    rotation: int | None,
    keep_channels: float,
    fixed: tuple[Move, ...],
    allowed_sides: frozenset[Side],
    edge: Edge | None,
    offset: float | None,
) -> tuple[PlaceResult | DoesNotFit, Rect]:
    """`place_block` of `pcb_place_block` (B-C11), classified here in the worker. Without a
    `region` the block goes into the strip along `edge` (`edge_strip`). Returns the result
    and the region used."""
    cls = classify(model, doc)
    target: Point | None = None
    if region is None:
        if edge is None:
            raise ValueError("place_block_job needs a region or an edge")
        region, target = edge_strip(model, cls, refs, edge, keep_channels, offset)
    result = place_block(
        model,
        cls,
        refs,
        region,
        side=side,
        rotation=rotation,
        keep_channels=keep_channels,
        fixed=fixed,
        allowed_sides=allowed_sides,
        edge=edge,
        target=target,
        doc=doc,
    )
    return result, region


def legalize_job(
    model: BoardModel,
    moves: tuple[Move, ...],
    movable: frozenset[str],
    max_shift: float | None,
    keepouts: tuple[tuple[Point, ...], ...],
    edge_margin: float,
) -> tuple[Move, ...] | DoesNotFit:
    """`legalize` (B-C11) for `pcb_move {legalize: true}` and `pcb_legalize`."""
    return legalize(
        model,
        moves,
        movable=movable,
        max_shift=max_shift,
        keepouts=keepouts,
        edge_margin=edge_margin,
    )


def score_job(model: BoardModel, doc: BoardDoc) -> PlacementScore:
    """The placement score (B-C11) of `model`, classified here: the before / after score of
    the placement tools."""
    return score_placement(model, classify(model, doc), doc)


# ---- copper (P7) ---------------------------------------------------------------------------


def fanout_candidates_job(
    model: BoardModel,
    net: str,
    *,
    via: tuple[float, float],
    width: float,
    clearance: float,
    max_stub: float,
) -> dict[tuple[str, str], tuple[ViaCandidate, ...]]:
    """`planes.fanout_candidates` for `pcb_fanout` (callers pass a fill-stripped model)."""
    return fanout_candidates(
        model, net, via=via, width=width, clearance=clearance, max_stub=max_stub
    )


def stitch_candidates_job(
    model: BoardModel,
    net: str,
    *,
    via: tuple[float, float],
    clearance: float,
    pitch: float,
    edge_ring: bool,
) -> tuple[ViaCandidate, ...]:
    """`planes.stitch_candidates` for `pcb_stitch` (zone outlines, not fills: callers pass
    a fill-stripped model)."""
    return stitch_candidates(
        model, net, via=via, clearance=clearance, pitch=pitch, edge_ring=edge_ring
    )


def silk_plan_job(
    model: BoardModel,
    *,
    height: float,
    line: float,
    skip: frozenset[str] = frozenset(),
    exclude: Mapping[str, frozenset[int]] | None = None,
) -> tuple[tuple[RefPlacement, ...], tuple[str, ...]]:
    """`silk.plan_refs` for `pcb_tidy_silk` (callers pass a fill-stripped model)."""
    return plan_refs(model, height=height, line=line, skip=skip, exclude=exclude)


# ---- floorplan (R2, B-C11 round 2) ----------------------------------------------------------


def floorplan_job(
    model: BoardModel,
    doc: BoardDoc,
    blocks: tuple[BlockProposal, ...],
    *,
    keep_channels: float,
    allowed_sides: frozenset[Side],
) -> Floorplan | DoesNotFit:
    """`floorplan` of `pcb_floorplan`, classified here in the worker."""
    return floorplan(
        model,
        classify(model, doc),
        doc,
        blocks,
        keep_channels=keep_channels,
        allowed_sides=allowed_sides,
    )


def place_blocks_job(
    model: BoardModel,
    doc: BoardDoc,
    blocks: tuple[BlockProposal, ...],
    *,
    keep_channels: float,
    allowed_sides: frozenset[Side],
) -> tuple[PlaceResult, Floorplan] | DoesNotFit:
    """`place_blocks` of `pcb_place_blocks`, classified here in the worker."""
    return place_blocks(
        model,
        classify(model, doc),
        doc,
        blocks,
        keep_channels=keep_channels,
        allowed_sides=allowed_sides,
    )
