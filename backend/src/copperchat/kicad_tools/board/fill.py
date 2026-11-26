"""`pcb_route_fill` (SP4 spec §7.2, §10.3, §10.4, P6b): Freerouting fills every remaining
connection; what comes back must meet the owner's bar or is ripped and re-routed by the
own router, and a fill that needs too much of that is refused.

Stages (the engine takes the **full** copper snapshot first, B-C4):

1. `clear fills` (commit): every filled copper zone gets its fill cleared (the probe found
   fills clearable through `update_items`); `undo` refills them.
2. `freerouting`: the live board as DSN (a board copy + `kicad-cli pcb export specctra`:
   the API job returns the board of its first run on every later export); the plane
   layers are switched off for the autorouter in the DSN (see `disable_layers`),
   Freerouting runs with the user's
   `timeout_s` and `passes`.
3. `import` (commit): `ImportSpecctraSession` inside the board commit (the probe found it
   staged by the commit: a dropped commit leaves no trace), then the scratch dir goes.
4. `cleanup`: the import's ground copper (KIIDs not on the board before the call) and the
   new copper of nets the old copper still connects completely (Freerouting re-routes
   pad-to-via links it misreads as open) are removed; moved footprints are put back; the
   zones are refilled (own budget).
5. `postprocess`: `router/chamfer.postprocess` on the new tracks in the CPU worker (plus
   width fixes: fill tracks narrower than their net class are widened, `_widen`); more
   than 20 % of the fill's connections ripped → `fill_not_acceptable` (`rip_ratio`), the
   engine restores the pre-fill board. Otherwise the replacements are applied and checked
   by the DRC task; a net with a replacement the DRC rejects gets its originals back and
   its connections through them are ripped.
6. `reroute`: the new copper of every ripped connection is removed and the connection is
   re-routed by the own router (`route_connections`, unlocked) within the post deadline.
7. `relock`: `relock_nets` whose copper passes M4/M7 are locked again.

The round trip's two KiCad I/O calls share `IO_BUDGET_S` (an accumulator of their measured
durations; Freerouting's runtime never counts); post-processing and re-routing share
`POST_DEADLINE_S`; the final metrics have a fixed budget. Worst case of one call:
`timeout_s` + `FILL_FIXED_S`.
"""

from __future__ import annotations

import asyncio
import dataclasses
import itertools
import math
import os
import re
import stat
import time
import uuid
from collections import Counter, defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kipy.board_types import Track as KipyTrack
from kipy.geometry import Angle, Vector2
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.classify import Classification, classify
from copperchat.board.freerouting import (
    FreeroutingRun,
    FreeroutingRuntime,
    find_runtime,
    run_freerouting,
)
from copperchat.board.geom import TrackIndex, analyse_tracks
from copperchat.board.jobs import metrics_job, strip_fills
from copperchat.board.metrics import MetricsReport, connections, outline_polygon
from copperchat.board.model import ArcTrack, BoardModel, Pad, Part, Track, Via, placement_hash
from copperchat.board.router.chamfer import PostResult, _Paths, postprocess
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.analysis import metrics_data, preset_for
from copperchat.kicad_tools.board.api import REFILL_TIMEOUT_S, DrcTimeout, RefillTimeout
from copperchat.kicad_tools.board.checkpoints import require_placement_approved
from copperchat.kicad_tools.board.compensate import CopperSnapshot
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.route import (
    IGNORED_DRC,
    ConnResult,
    _check_layers,
    _check_nets,
    _invalid,
    _joined,
    _keys,
    _kiids,
    _names,
    _unrouted,
    class_params,
    conn_key,
    plan_connections,
    route_connections,
)
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.cli import run_kicad_cli
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.projects import agent_dir
from copperchat.tools.spec import ToolContext, ToolResult
from copperchat.views.board_render import BOARD_CLI_LOCK

__all__ = [
    "FILL_FIXED_S",
    "FILL_WORST_CASE_S",
    "IO_BUDGET_S",
    "POST_DEADLINE_S",
    "FillPlan",
    "apply_fill",
    "default_plane_layers",
    "disable_layers",
    "fill_stages",
    "new_ground_items",
    "plan_fill",
    "prepare_fill",
]

TIMEOUT_MIN_S = 30
TIMEOUT_MAX_S = 600
TIMEOUT_DEFAULT_S = 300
PASSES_MIN = 1
PASSES_MAX = 100
PASSES_DEFAULT = 30
MAX_RELOCK = 50
MAX_PLANE_LAYERS = 4
MAX_NET_CHARS = 256
FILL_COST = 5
IO_BUDGET_S = 60.0  # DSN export + SES import together (an accumulator, not a deadline)
POST_DEADLINE_S = 120.0  # post-processing + rip-and-reroute
POST_JOB_MAX_S = 60.0  # one postprocess run_cpu job
DRC_MAX_S = 60.0
METRICS_TIMEOUT_S = 30.0  # the report's metrics: fixed, independent of the post deadline
REFILL_TOTAL_S = api.refill_call_timeout(REFILL_TIMEOUT_S)  # 105 s, own budget
# Everything but Freerouting's own timeout_s: I/O 60 + refill 105 + post 120 + one DRC
# overrun 60 + final metrics (queue 30 + run 30).
FILL_FIXED_S = int(
    IO_BUDGET_S + REFILL_TOTAL_S + POST_DEADLINE_S + DRC_MAX_S + 2 * METRICS_TIMEOUT_S
)
FILL_WORST_CASE_S = TIMEOUT_MAX_S + FILL_FIXED_S  # 1005 s
RIP_RATIO_MAX = 0.20  # spec §10.4
PLANE_COVER_MIN = 0.80  # a ground zone covering this share of the outline is a plane
CELL_MM = 10.0  # worst-area cells of a rejected fill
MAX_WORST_AREAS = 5
MAX_LISTED = 50
PLANE_DEFAULT = {4: ("In1.Cu",), 6: ("In1.Cu", "In4.Cu")}  # owner D1 / plan call
PLANE_LAYER_MECHANISM = "dsn_autoroute_settings"  # As built (P6b): see `disable_layers`
NEXT_DONE = (
    "pcb_render view top and bottom (markers: true), show the user, then pcb_zones / "
    "pcb_fanout for the planes"
)


# -- planning (pure) -----------------------------------------------------------------------


@dataclass(frozen=True)
class FillArgs:
    timeout_s: int
    passes: int
    relock_nets: tuple[str, ...]
    plane_layers: tuple[str, ...] | None  # None: the default (`default_plane_layers`)


@dataclass(frozen=True)
class FillPlan:
    ground_nets: frozenset[str]
    # unrouted non-ground connections before the fill: (key, net, a, b)
    open: tuple[tuple[str, str, Pad, Pad], ...]
    plane_layers: tuple[str, ...]
    filled_zones: tuple[str, ...]  # copper zones with a fill (stage 1 clears them)


def _area(poly: Sequence[tuple[float, float]]) -> float:
    return (
        abs(sum(a[0] * b[1] - b[0] * a[1] for a, b in itertools.pairwise([*poly, poly[0]]))) / 2
        if len(poly) >= 3
        else 0.0
    )


def _outline_area(model: BoardModel) -> float:
    poly = outline_polygon(model)
    if poly:
        return _area(poly)
    return max(model.outline.w, 0.0) * max(model.outline.h, 0.0)


def default_plane_layers(model: BoardModel, ground_nets: frozenset[str]) -> tuple[str, ...]:
    """The layers the fill keeps free: the layers of existing ground-net copper zones
    that cover at least PLANE_COVER_MIN of the outline, else the owner-D1 default
    (4 layers `In1.Cu`, 6 layers `In1.Cu` + `In4.Cu`, 2 layers none). Copper order."""
    board = _outline_area(model)
    found: set[str] = set()
    if board > 0:
        for z in model.zones:
            if z.rule_area or z.net not in ground_nets:
                continue
            if _area(z.outline) >= PLANE_COVER_MIN * board:
                found.update(n for n in z.layers if n in model.copper_layers)
    if found:
        return tuple(n for n in model.copper_layers if n in found)
    default = PLANE_DEFAULT.get(len(model.copper_layers), ())
    return tuple(n for n in default if n in model.copper_layers)


def ground_nets_of(cls: Classification) -> frozenset[str]:
    return frozenset(n for n, k in cls.net_kind.items() if k == "ground")


def plan_fill(
    model: BoardModel, cls: Classification, plane_layers: tuple[str, ...] | None
) -> FillPlan:
    """Ground nets, unrouted non-ground connections (shortest first per net), the plane
    layers (`default_plane_layers` when None) and the filled copper zones."""
    ground = ground_nets_of(cls)
    nets = sorted({q.net for p in model.parts for q in p.pads if q.net and q.net not in ground})
    open_: list[tuple[str, str, Pad, Pad]] = []
    for net in nets:
        open_ += [(conn_key(net, a, b), net, a, b) for a, b in _unrouted(model, net)]
    layers = default_plane_layers(model, ground) if plane_layers is None else plane_layers
    zones = tuple(
        z.kiid for z in model.zones if not z.rule_area and (z.filled_area_mm2 > 0 or z.filled)
    )
    return FillPlan(ground, tuple(open_), tuple(layers), zones)


def new_ground_items(
    snap: CopperSnapshot | BoardModel, model: BoardModel, ground_nets: frozenset[str]
) -> list[str]:
    """KIIDs of tracks, arcs and vias on ground nets that are on `model` but not in the
    pre-import state `snap` (a snapshot or the pre-call model): locked fanout stubs and
    stitching vias - and any other ground copper from before - are never selected."""
    if isinstance(snap, CopperSnapshot):
        before = snap.track_ids | snap.via_ids
    else:
        before = _copper_ids(snap)
    items = _copper(model)
    return sorted(i.kiid for i in items if i.net in ground_nets and i.kiid not in before)


def _copper(model: BoardModel) -> list[Track | ArcTrack | Via]:
    return [*model.tracks, *model.arcs, *model.vias]


def _copper_ids(model: BoardModel) -> frozenset[str]:
    return frozenset(i.kiid for i in _copper(model))


_LAYER_RE = r"\(layer\s+{name}\s*\n?\s*\(type\s+\w+\)"


def disable_layers(text: str, layers: Sequence[str]) -> str:
    """The exported DSN with `layers` switched off for Freerouting's autorouter: ONE line
    `(autoroute_settings (layer_rule <layer> (active off)) …)` is inserted into the
    `structure` right before its `boundary`; nothing else changes. Owner D4's last resort:
    KiCad's layer type is not settable (P2), Freerouting 2.5.0 has no CLI option for a
    layer (`--router.layers…` paths are rejected), and a `(type power)` layer is still
    routed by 2.5.0 (P6b probes). Refuses a DSN that already has autoroute settings, an
    unknown layer or no structure boundary (`ValueError`)."""
    if not layers:
        return text
    if "(autoroute_settings" in text:
        raise ValueError("the DSN already has autoroute settings")
    for name in layers:
        if not re.search(_LAYER_RE.format(name=re.escape(name)), text):
            raise ValueError(f"layer {name!r} is not in the DSN structure")
    m = re.search(r"\n(\s*)\(boundary", text)
    if m is None:
        raise ValueError("the DSN has no structure boundary")
    rules = " ".join(f"(layer_rule {n} (active off))" for n in layers)
    line = f"\n{m.group(1)}(autoroute_settings {rules})"
    return text[: m.start()] + line + text[m.start() :]


# -- argument parsing --------------------------------------------------------------------------


def _int(args: Mapping[str, Any], key: str, lo: int, hi: int, default: int) -> int:
    v = args.get(key, default)
    if isinstance(v, bool) or not isinstance(v, int | float) or not math.isfinite(float(v)):
        raise _invalid(f"{key} must be an integer {lo}-{hi}")
    if float(v) != int(v) or not lo <= int(v) <= hi:
        raise _invalid(f"{key} must be an integer {lo}-{hi}")
    return int(v)


def parse_fill_args(args: Any) -> FillArgs:
    a = _keys(args, {"timeout_s", "passes", "relock_nets", "plane_layers"})
    relock = (
        tuple(_names(a["relock_nets"], "relock_nets", 0, MAX_RELOCK, MAX_NET_CHARS))
        if a.get("relock_nets") is not None
        else ()
    )
    planes = (
        tuple(_names(a["plane_layers"], "plane_layers", 0, MAX_PLANE_LAYERS, 64))
        if a.get("plane_layers") is not None
        else None
    )
    return FillArgs(
        timeout_s=_int(a, "timeout_s", TIMEOUT_MIN_S, TIMEOUT_MAX_S, TIMEOUT_DEFAULT_S),
        passes=_int(a, "passes", PASSES_MIN, PASSES_MAX, PASSES_DEFAULT),
        relock_nets=relock,
        plane_layers=planes,
    )


# -- errors ------------------------------------------------------------------------------------


def _not_acceptable(why: str, reason: str, **details: Any) -> StructuredToolError:
    suggestion = {
        "rip_ratio": "Improve the channels between blocks (pcb_move / pcb_legalize) or the "
        "critical routing (pcb_route), look at the worst areas with pcb_render {region, "
        "markers: true}, then retry.",
        "timeout": "Retry with a larger timeout_s or fewer passes.",
        "freerouting_error": "Read log_tail; retry once, else route the rest with pcb_route.",
        "io_timeout": "KiCad was too slow; retry, or route the rest with pcb_route.",
    }[reason]
    return StructuredToolError(
        "Fill routing not acceptable",
        why,
        f"Nothing was changed. {suggestion}",
        code="fill_not_acceptable",
        details={"reason": reason, **details},
    )


def _unavailable() -> StructuredToolError:
    return StructuredToolError(
        "Freerouting is not available",
        "the Freerouting runtime (JRE + JAR) was not found or failed its check",
        "The app image ships it; set COPPERCHAT_FREEROUTING_HOME. Route the rest with pcb_route.",
        code="freerouting_unavailable",
    )


def _fill_dir(ctx: ToolContext) -> Path:
    """The scratch dir path `.kicad-agent/board/fill-<uuid>` (created by stage 2); a
    symlinked `.kicad-agent` or `board` is refused."""
    agent = agent_dir(ctx.project)
    base = agent / "board"
    for d in (agent, base):
        if d.is_symlink():
            raise StructuredToolError(
                "Cannot run the fill",
                f"{d.relative_to(ctx.project.dir)} is a symlink",
                "Ask the user to replace it with a plain directory, then retry.",
                code="output_path_unsafe",
            )
    return base / f"fill-{uuid.uuid4().hex}"


# -- stage helpers -----------------------------------------------------------------------------


@dataclass
class _FillState:
    args: FillArgs
    plan: FillPlan
    rt: FreeroutingRuntime
    workdir: Path
    pre: BoardModel  # the board before the call
    pre_hash: str
    cls: Classification
    io_spent: float = 0.0
    cleared: list[str] = field(default_factory=list)
    run: FreeroutingRun | None = None
    post_deadline: float = 0.0
    rip: dict[str, str] = field(default_factory=dict)  # key -> reason
    fill_keys: frozenset[str] = frozenset()
    rerouted: list[str] = field(default_factory=list)
    rejected: list[dict[str, Any]] = field(default_factory=list)
    final: BoardModel | None = None


def _io_timeout(st: _FillState) -> StructuredToolError:
    return _not_acceptable(
        f"the DSN export and SES import took {st.io_spent:.1f} s (budget {IO_BUDGET_S:g} s)",
        "io_timeout",
        io_spent_s=round(st.io_spent, 2),
    )


async def _io(sctx: BoardStageContext, st: _FillState, fn: Any) -> Any:
    """One round-trip KiCad call with the remaining I/O budget as its explicit timeout; its
    measured duration adds to `io_spent` (checked right after the call)."""
    return await _timed(st, lambda left: sctx.call(fn, timeout=left))


async def _timed(st: _FillState, make: Any) -> Any:
    """Run `make(remaining budget)` (an awaitable factory); its measured duration adds to
    `io_spent`, a budget used up before or right after it → `io_timeout`."""
    left = IO_BUDGET_S - st.io_spent
    if left <= 0:
        raise _io_timeout(st)
    t0 = time.monotonic()
    try:
        out = await make(left)
    except TimeoutError:  # kicad-cli killed at the remaining budget
        st.io_spent += time.monotonic() - t0
        raise _io_timeout(st) from None
    except BaseException:
        st.io_spent += time.monotonic() - t0
        raise
    st.io_spent += time.monotonic() - t0
    if st.io_spent >= IO_BUDGET_S:
        raise _io_timeout(st)
    return out


SIDE_FILES = ("fp-lib-table", "sym-lib-table")
MAX_SIDE_BYTES = 16 * 1024 * 1024


def _save_board_copy(h: KicadHandle, dest: Path) -> None:
    """The live board written to `dest` (SaveCopyOfDocument, as `KicadSession.save_copy`
    does - which cannot run inside a board edit: it takes the apply lock). KiCad refuses
    when its own `.kicad_dru` copy already exists next to `dest`."""
    dest.with_suffix(".kicad_dru").unlink(missing_ok=True)
    h.board().save_as(str(dest), overwrite=True, include_project=False)


def _copy_side_files(ctx: ToolContext, work: Path) -> None:
    """Byte copies of the project file (net classes and rules for the DSN) and the library
    tables next to the board copy: regular files only, never through a symlink, bounded."""
    project = ctx.project
    pairs = [(project.pro_file, work / f"{project.pro_file.stem}.kicad_pro")]
    pairs += [(project.dir / n, work / n) for n in SIDE_FILES]
    for src, dest in pairs:
        try:
            st = os.lstat(src)
        except FileNotFoundError:
            continue
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_SIDE_BYTES:
            continue
        fd = os.open(src, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(fd, "rb") as fh:
            dest.write_bytes(fh.read(MAX_SIDE_BYTES + 1)[:MAX_SIDE_BYTES])


async def _export_dsn(sctx: BoardStageContext, st: _FillState, dsn: Path) -> None:
    """The live board as Specctra DSN: a board copy in the scratch dir (+ the project file)
    and `kicad-cli pcb export specctra` on it. Not the API job: it keeps the board it
    exported first and returns that stale copy on every later export, also after a save
    (P6b probe). Both steps share the round-trip I/O budget."""
    ctx = sctx.ctx
    copy = st.workdir / f"{ctx.project.pro_file.stem}.kicad_pcb"
    await _io(sctx, st, lambda h: _save_board_copy(h, copy))

    async def cli(left: float) -> Any:
        await asyncio.to_thread(_copy_side_files, ctx, st.workdir)
        args = ["pcb", "export", "specctra", "-o", str(dsn), str(copy)]
        async with BOARD_CLI_LOCK:
            return await run_kicad_cli(ctx.settings, args, timeout=left, cwd=st.workdir)

    result = await _timed(st, cli)
    if result.returncode != 0 or not dsn.is_file():
        tail = (result.stderr or result.stdout or "").strip().splitlines()[-1:] or ["no output"]
        raise StructuredToolError(
            "Specctra DSN export failed",
            f"kicad-cli pcb export specctra exited with {result.returncode}: {tail[0][:200]}",
            "check the board outline and footprints (pcb_check), then retry",
            code="export_failed",
        )


def _clear_fills(h: KicadHandle, kiids: Sequence[str]) -> list[str]:
    board = h.board()
    zones = list(board.get_items_by_id(_kiids(kiids)))
    for z in zones:
        z.proto.filled = False
        del z.proto.filled_polygons[:]
    if zones:
        board.update_items(zones)
    return [str(z.id.value) for z in zones]


async def _refill(sctx: BoardStageContext) -> None:
    try:
        await sctx.refill(timeout_s=REFILL_TIMEOUT_S)
    except RefillTimeout as exc:
        raise _not_acceptable(
            f"the zone refill after the import did not finish ({exc.why})", "io_timeout"
        ) from exc


def _restore_positions(h: KicadHandle, parts: Sequence[Part]) -> int:
    """Footprints moved since `parts` (the pre-call model) are put back (position,
    rotation, locked; a side change is not expected from an import and left)."""
    board = h.board()
    want = {p.kiid: p for p in parts}
    moved = []
    for fp in board.get_footprints():
        p = want.get(str(fp.id.value))
        if p is None:
            continue
        x, y = mm_to_nm(p.x), mm_to_nm(p.y)
        rot = abs(((fp.orientation.degrees - p.rot + 180) % 360) - 180)
        if fp.position.x != x or fp.position.y != y or rot > 1e-6 or fp.locked != p.locked:
            fp.position = Vector2.from_xy(x, y)
            fp.orientation = Angle.from_degrees(p.rot)
            fp.locked = p.locked
            moved.append(fp)
    if moved:
        board.update_items(moved)
    return len(moved)


def _remove(h: KicadHandle, ids: Sequence[str]) -> None:
    if ids:
        h.board().remove_items_by_id(_kiids(ids))


def _redundant(model: BoardModel, pre_ids: frozenset[str], ground: frozenset[str]) -> list[str]:
    """New copper (not in `pre_ids`) of non-ground nets whose connections the old copper
    alone still connects: Freerouting re-routes pad-to-via links of locked work it reads
    as open, which would leave a second path."""
    by_net: dict[str, list[str]] = defaultdict(list)
    for i in _copper(model):
        if i.kiid not in pre_ids and i.net and i.net not in ground:
            by_net[i.net].append(i.kiid)
    out: list[str] = []
    for net, ids in sorted(by_net.items()):
        new = set(ids)
        old = dataclasses.replace(
            model,
            tracks=tuple(t for t in model.tracks if t.kiid not in new),
            arcs=tuple(a for a in model.arcs if a.kiid not in new),
            vias=tuple(v for v in model.vias if v.kiid not in new),
        )
        if connections(old, net) and not _unrouted(old, net):
            out += sorted(new)
    return out


def _routed_keys(model: BoardModel, nets: set[str]) -> set[str]:
    out: set[str] = set()
    for net in nets:
        open_ = {conn_key(net, a, b) for a, b in _unrouted(model, net)}
        out |= {conn_key(net, a, b) for a, b in connections(model, net)} - open_
    return out


def _worst_areas(keys: Sequence[str], pads: Mapping[str, tuple[Pad, Pad]]) -> list[dict[str, Any]]:
    """The CELL_MM cells with the most rip defects (each ripped connection counts at the
    midpoint of its pads)."""
    cells: Counter[tuple[int, int]] = Counter()
    for k in keys:
        ab = pads.get(k)
        if ab is None:
            continue
        a, b = ab
        mx, my = (a.x + b.x) / 2, (a.y + b.y) / 2
        cells[(math.floor(mx / CELL_MM), math.floor(my / CELL_MM))] += 1
    return [
        {"x": cx * CELL_MM, "y": cy * CELL_MM, "w": CELL_MM, "h": CELL_MM, "count": n}
        for (cx, cy), n in sorted(cells.items(), key=lambda kv: (-kv[1], kv[0]))[:MAX_WORST_AREAS]
    ]


def _pads_by_key(model: BoardModel, nets: set[str]) -> dict[str, tuple[Pad, Pad]]:
    out: dict[str, tuple[Pad, Pad]] = {}
    for net in sorted(nets):
        for a, b in connections(model, net):
            out.setdefault(conn_key(net, a, b), (a, b))
    return out


async def _run_postprocess(
    model: BoardModel, kiids: frozenset[str], *, timeout_s: float, wait_s: float
) -> PostResult:
    """The post-processor in the CPU worker (a seam for tests)."""
    result: PostResult = await run_cpu(
        postprocess, strip_fills(model), kiids, timeout_s=timeout_s, wait_s=wait_s
    )
    return result


def _create_tracks(h: KicadHandle, tracks: Sequence[Track]) -> list[str]:
    board = h.board()
    nets = {n.name: n for n in board.get_nets()}
    made = []
    for t in tracks:
        k = KipyTrack()
        k.layer = layer_from_canonical_name(t.layer)
        k.net = nets[t.net]
        k.start = Vector2.from_xy(mm_to_nm(t.start[0]), mm_to_nm(t.start[1]))
        k.end = Vector2.from_xy(mm_to_nm(t.end[0]), mm_to_nm(t.end[1]))
        k.width = mm_to_nm(t.width)
        k.locked = t.locked
        made.append(k)
    created = board.create_items(made) if made else []
    return [str(c.id.value) for c in created]


def _apply_replace(
    h: KicadHandle, replace: Sequence[tuple[str, tuple[Track, ...]]]
) -> list[list[str]]:
    """Remove every replaced track and create its replacements (one batch each); returns
    the created KIIDs per replacement, in order."""
    _remove(h, [old for old, _ in replace])
    flat = [t for _, new in replace for t in new]
    made = iter(_create_tracks(h, flat))
    return [[next(made) for _ in new] for _, new in replace]


def _conns_using(model: BoardModel, net: str, kiids: set[str]) -> list[str]:
    """Keys of `net`'s connections whose shortest copper path uses one of `kiids`."""
    tracks = [(i, t) for i, t in enumerate(model.tracks) if t.net == net]
    pads = sorted(
        (q for p in model.parts for q in p.pads if q.net == net),
        key=lambda q: (q.ref, q.number),
    )
    paths = _Paths(model, tracks, pads)
    out = []
    for a, b in connections(model, net):
        hit = paths.path(a, b)
        if hit is not None and any(model.tracks[i].kiid in kiids for i in hit[1]):
            out.append(conn_key(net, a, b))
    return out


def _rip_copper(model: BoardModel, keys: Sequence[str], pre_ids: frozenset[str]) -> list[str]:
    """The fill copper of the ripped connections: every unlocked new track on the
    connection's shortest path, plus the unlocked new vias of those nets no remaining
    track end reaches afterwards."""
    by_net: dict[str, set[str]] = defaultdict(set)
    for k in keys:
        by_net[k.split(":", 1)[0]].add(k)
    gone: set[str] = set()
    for net, want in sorted(by_net.items()):
        tracks = [(i, t) for i, t in enumerate(model.tracks) if t.net == net]
        pads = sorted(
            (q for p in model.parts for q in p.pads if q.net == net),
            key=lambda q: (q.ref, q.number),
        )
        paths = _Paths(model, tracks, pads)
        for a, b in connections(model, net):
            if conn_key(net, a, b) not in want:
                continue
            hit = paths.path(a, b)
            if hit is None:
                continue
            for i in hit[1]:
                t = model.tracks[i]
                if not t.locked and t.kiid not in pre_ids:
                    gone.add(t.kiid)
    if not gone:
        return []
    left = [t for t in model.tracks if t.kiid not in gone]
    for v in model.vias:
        if v.locked or v.kiid in pre_ids or v.net not in by_net:
            continue
        r = v.diameter / 2 + 1e-3
        if not any(
            t.net == v.net and min(math.dist(t.start, v.at), math.dist(t.end, v.at)) <= r
            for t in left
        ):
            gone.add(v.kiid)
    return sorted(gone)


def _bends_of(model: BoardModel, net: str) -> int:
    """M4 + M7 failures over `net`'s tracks (B-C5 `analyse_tracks`, the whole board's pads
    and vias)."""
    sub = dataclasses.replace(model, tracks=tuple(t for t in model.tracks if t.net == net), arcs=())
    rep = analyse_tracks(sub, index=TrackIndex(model))
    return (
        len(rep.non_octilinear)
        + len(rep.ninety)
        + len(rep.acute)
        + len(rep.off_angle)
        + len(rep.t_junctions)
        + len(rep.micro_jogs)
        + len(rep.short_chamfers)
    )


def _widen(
    model: BoardModel,
    replace: Sequence[tuple[str, tuple[Track, ...]]],
    new_tracks: frozenset[str],
) -> tuple[list[tuple[str, tuple[Track, ...]]], int]:
    """The post-processor's replacements plus width fixes: every new track (and every
    replacement track) narrower than its net's class width (`route.class_params`, never
    below the board minimum) gets that width - Freerouting necks traces down at pins
    (0.15 mm on a 0.2 mm class on `sp4_synth`). Returns (replacements, widened count); the
    widened copper is DRC-checked with the rest (rejected → originals back, ripped)."""
    widths: dict[str, float] = {}

    def width(net: str) -> float:
        if net not in widths:
            widths[net] = class_params(model, net).width
        return widths[net]

    count = 0

    def wide(t: Track) -> Track:
        nonlocal count
        w = width(t.net)
        if t.width < w - 1e-9:
            count += 1
            return dataclasses.replace(t, width=w)
        return t

    out = [(old, tuple(wide(t) for t in tracks)) for old, tracks in replace]
    done = {old for old, _ in replace}
    for t in sorted(model.tracks, key=lambda t: t.kiid):
        if t.kiid in new_tracks and t.kiid not in done and t.width < width(t.net) - 1e-9:
            count += 1
            out.append((t.kiid, (dataclasses.replace(t, kiid="", width=width(t.net)),)))
    return out, count


def _set_locked(h: KicadHandle, ids: Sequence[str]) -> None:
    board = h.board()
    items = list(board.get_items_by_id(_kiids(ids)))
    for it in items:
        it.locked = True  # type: ignore[attr-defined]
    if items:
        board.update_items(items)


def _remaining(deadline: float) -> float:
    return deadline - time.monotonic()


# -- stages ------------------------------------------------------------------------------------


def fill_stages(st: _FillState) -> list[BoardStage]:
    """The seven stages of one `pcb_route_fill` call (module docstring)."""
    pre_ids = _copper_ids(st.pre)

    async def clear_fills(sctx: BoardStageContext) -> None:
        if st.plan.filled_zones:
            ids = list(st.plan.filled_zones)
            st.cleared = await sctx.call(
                lambda h: _clear_fills(h, ids), timeout=BOARD_READ_TIMEOUT_S
            )
        sctx.report["fills_cleared"] = len(st.cleared)

    async def refill_cleared(sctx: BoardStageContext) -> None:
        if st.cleared:
            await sctx.refill(timeout_s=REFILL_TIMEOUT_S)

    async def freerouting(sctx: BoardStageContext) -> None:
        await asyncio.to_thread(st.workdir.mkdir, parents=True, exist_ok=False)
        dsn, ses = st.workdir / "board.dsn", st.workdir / "board.ses"
        await _export_dsn(sctx, st, dsn)
        layers = st.plan.plane_layers
        if layers:

            def rewrite() -> None:
                dsn.write_text(disable_layers(dsn.read_text(encoding="utf-8"), layers), "utf-8")

            await asyncio.to_thread(rewrite)
        run = await run_freerouting(
            st.rt,
            dsn,
            ses,
            timeout_s=st.args.timeout_s,
            passes=st.args.passes,
            workdir=st.workdir,
        )
        st.run = run
        sctx.report["freerouting"] = {
            "seconds": round(run.seconds, 2),
            "passes": st.args.passes,
            "timed_out": run.timed_out,
        }
        tail = run.log_tail[-2000:]
        if run.timed_out:
            raise _not_acceptable(
                f"Freerouting did not finish within {st.args.timeout_s} s", "timeout", log_tail=tail
            )
        if run.returncode != 0 or not ses.is_file():
            raise _not_acceptable(
                f"Freerouting failed (exit code {run.returncode})",
                "freerouting_error",
                log_tail=tail,
            )

    async def import_ses(sctx: BoardStageContext) -> None:
        ses = st.workdir / "board.ses"
        await _io(sctx, st, lambda h: api.import_specctra_session(h, ses))
        sctx.report["io_seconds"] = round(st.io_spent, 2)

    async def cleanup(sctx: BoardStageContext) -> None:
        import shutil

        await asyncio.to_thread(shutil.rmtree, st.workdir, True)
        m = await sctx.model()
        ground = new_ground_items(st.pre, m, st.plan.ground_nets)
        redundant = _redundant(m, pre_ids, st.plan.ground_nets)
        moved = placement_hash(m) != st.pre_hash
        restored = 0
        if ground or redundant or moved:
            gone = [*ground, *redundant]
            async with sctx.ctx.session.commit("copperchat: pcb_route_fill", doc="pcb"):

                def write(h: KicadHandle) -> int:
                    _remove(h, gone)
                    return _restore_positions(h, st.pre.parts) if moved else 0

                restored = await sctx.call(write, timeout=BOARD_READ_TIMEOUT_S)
        sctx.report["ground_items_deleted"] = len(ground)
        sctx.report["redundant_items_deleted"] = len(redundant)
        sctx.report["positions_restored"] = restored
        if moved:
            m = await sctx.model()
            if placement_hash(m) != st.pre_hash:
                raise StructuredToolError(
                    "pcb_route_fill moved parts",
                    "the placement differs after the SES import and could not be restored",
                    "Nothing was changed. Re-read the board (pcb_status) and retry.",
                    code="verification_failed",
                )
        if any(not z.rule_area for z in m.zones):
            await _refill(sctx)

    async def post(sctx: BoardStageContext) -> None:
        st.post_deadline = time.monotonic() + POST_DEADLINE_S
        m = await sctx.model()
        new_tracks = frozenset(
            t.kiid for t in m.tracks if t.kiid not in pre_ids and t.net not in st.plan.ground_nets
        )
        before_open = {k for k, *_ in st.plan.open}
        nets = {net for _, net, _, _ in st.plan.open}
        st.fill_keys = frozenset(before_open & _routed_keys(m, nets))
        rem = max(_remaining(st.post_deadline), 0.0)
        res = await _run_postprocess(m, new_tracks, timeout_s=min(POST_JOB_MAX_S, rem), wait_s=rem)
        rips = {k: r for k, r in res.reasons}
        for k in res.rip:
            rips.setdefault(k, "rip")
        rips = {k: r for k, r in rips.items() if k.split(":", 1)[0] not in st.plan.ground_nets}
        fill_rip = sorted(set(rips) & st.fill_keys)
        n_fill = len(st.fill_keys)
        ratio = len(fill_rip) / n_fill if n_fill else 0.0
        sctx.report["chamfered"] = res.chamfered
        sctx.report["merged"] = res.merged
        if ratio > RIP_RATIO_MAX:
            reasons = Counter(rips[k] for k in fill_rip)
            raise _not_acceptable(
                f"{len(fill_rip)} of {n_fill} fill connections ({ratio:.0%}) would need a "
                f"rip-and-reroute (more than {RIP_RATIO_MAX:.0%})",
                "rip_ratio",
                fill_connections=n_fill,
                rip=len(fill_rip),
                ratio=round(ratio, 3),
                reasons=dict(sorted(reasons.items())),
                connections=[{"connection": k, "reason": rips[k]} for k in fill_rip[:MAX_LISTED]],
                worst_areas=_worst_areas(fill_rip, _pads_by_key(m, nets)),
            )
        st.rip = dict(rips)
        replace, widened = _widen(m, res.replace, new_tracks)
        sctx.report["widened"] = widened
        drc_skipped = False
        if replace:
            async with sctx.ctx.session.commit("copperchat: pcb_route_fill: chamfer", doc="pcb"):
                made = await sctx.call(
                    lambda h: _apply_replace(h, replace), timeout=BOARD_READ_TIMEOUT_S
                )
            sctx.created.update(k for ks in made for k in ks)
            rem = _remaining(st.post_deadline)
            try:
                if rem <= 0:
                    raise DrcTimeout(0.0, "the post-processing deadline passed")
                drc = await sctx.drc(timeout_s=min(DRC_MAX_S, rem))
            except DrcTimeout:
                drc_skipped = True
            else:
                await _revert_rejected(sctx, st, m, replace, made, drc)
        sctx.report["drc_skipped"] = drc_skipped

    async def reroute(sctx: BoardStageContext) -> None:
        keys = sorted(st.rip)
        sctx.report["ripped"] = len(keys)
        sctx.report["ripped_connections"] = [
            {"connection": k, "reason": st.rip[k]} for k in keys[:MAX_LISTED]
        ]
        if not keys:
            sctx.report["rerouted"] = 0
            return
        m = await sctx.model()
        gone = _rip_copper(m, keys, pre_ids)
        if gone:
            async with sctx.ctx.session.commit("copperchat: pcb_route_fill: rip", doc="pcb"):
                await sctx.call(lambda h: _remove(h, gone), timeout=BOARD_READ_TIMEOUT_S)
        m = await sctx.model()
        nets = sorted({k.split(":", 1)[0] for k in keys})
        layers = tuple(n for n in m.copper_layers if n not in st.plan.plane_layers)
        plans = await asyncio.to_thread(
            plan_connections, m, st.cls, nets, layers=layers, allow_vias=True
        )
        out = await route_connections(sctx, plans, lock=False, deadline=st.post_deadline)
        by_key: dict[str, ConnResult] = {}
        for r in out.results:
            by_key.setdefault(r.key.split("#", 1)[0], r)
        for k in keys:
            got = by_key.get(k)
            if got is not None and got.status == "routed":
                st.rerouted.append(k)
            elif got is None:
                # no plan: the connection is routed again by other copper (or joined)
                st.rerouted.append(k)
            else:
                st.rejected.append(
                    {"connection": k, "reason": got.reason or "no_path", **_where(m, k)}
                )
        sctx.report["rerouted"] = len(st.rerouted)

    async def relock(sctx: BoardStageContext) -> None:
        relocked: list[str] = []
        failed: list[str] = []
        if st.args.relock_nets:
            m = await sctx.model()
            ids: list[str] = []
            for net in st.args.relock_nets:
                items = [i for i in _copper(m) if i.net == net and not isinstance(i, ArcTrack)]
                if not items or await asyncio.to_thread(_bends_of, m, net) > 0:
                    failed.append(net)
                    continue
                relocked.append(net)
                ids += [i.kiid for i in items if not i.locked]
            if ids:
                await sctx.call(lambda h: _set_locked(h, ids), timeout=BOARD_READ_TIMEOUT_S)
        sctx.report["relocked"] = relocked
        sctx.report["relock_failed"] = failed

    return [
        BoardStage("clear fills", clear_fills, undo=refill_cleared),
        BoardStage("freerouting", freerouting, commit=False),
        BoardStage("import", import_ses),
        BoardStage("cleanup", cleanup, commit=False),
        BoardStage("postprocess", post, commit=False),
        BoardStage("reroute", reroute, commit=False),
        BoardStage("relock", relock),
    ]


def _where(model: BoardModel, key: str) -> dict[str, Any]:
    net = key.split(":", 1)[0]
    for a, b in connections(model, net):
        if conn_key(net, a, b) == key:
            return {"x": round((a.x + b.x) / 2, 3), "y": round((a.y + b.y) / 2, 3)}
    return {}


async def _revert_rejected(
    sctx: BoardStageContext,
    st: _FillState,
    model: BoardModel,
    replace: Sequence[tuple[str, tuple[Track, ...]]],
    made: Sequence[Sequence[str]],
    drc: api.DrcResult,
) -> None:
    """Nets with a replacement the DRC rejects get every original of the net back (their
    replacements removed); their connections through those originals are ripped."""
    bad: set[str] = set()
    for m in drc.markers:
        if m.type in IGNORED_DRC or m.severity == "exclusion":
            continue
        bad.update(m.kiids)
    if not bad:
        return
    old_track = {t.kiid: t for t in model.tracks}
    nets = {
        old_track[old].net
        for (old, _), ks in zip(replace, made, strict=True)
        if bad.intersection(ks) and old in old_track
    }
    if not nets:
        return
    undo = [
        (old, ks) for (old, _), ks in zip(replace, made, strict=True) if old_track[old].net in nets
    ]
    drop = [k for _, ks in undo for k in ks]
    back = [old_track[old] for old, _ in undo]
    async with sctx.ctx.session.commit("copperchat: pcb_route_fill: revert", doc="pcb"):

        def write(h: KicadHandle) -> list[str]:
            _remove(h, drop)
            return _create_tracks(h, back)

        again = await sctx.call(write, timeout=BOARD_READ_TIMEOUT_S)
    sctx.created.difference_update(drop)
    sctx.created.update(again)
    originals = {t.kiid for t in back}
    for net in sorted(nets):
        for k in _conns_using(model, net, originals):
            st.rip.setdefault(k, "drc_rejected")
    sctx.report["drc_reverted"] = len(back)


# -- tool ---------------------------------------------------------------------------------------


@dataclass(kw_only=True)
class FillChangeSet(BoardChangeSet):
    state: _FillState | None = None
    doc: BoardDoc | None = None
    read_back: list[BoardModel] = field(default_factory=list)


def _check_plane_layers(model: BoardModel, layers: Sequence[str]) -> None:
    _check_layers(model, layers)
    outer = {"F.Cu", "B.Cu"} & set(layers)
    if outer:
        raise _invalid(
            f"{', '.join(sorted(outer))} cannot be a plane layer of the fill",
            "Plane layers are inner layers (spec §11); pass inner copper layers or none.",
        )


async def prepare_fill(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_route_fill` (module docstring). Refusals, in order: flag off, no board,
    no Freerouting runtime, placement not approved, unknown relock nets / plane layers
    (`not_found` + closest), a ground net in `relock_nets`, nothing to fill."""
    require_board_tools(ctx)
    fargs = parse_fill_args(args)
    require_board(ctx)
    rt = await asyncio.to_thread(find_runtime, ctx.settings)
    if rt is None:
        raise _unavailable()
    model = await board_model(ctx)
    await require_placement_approved(ctx, model)
    if fargs.relock_nets:
        _check_nets(model, fargs.relock_nets)
    if fargs.plane_layers:
        _check_plane_layers(model, fargs.plane_layers)
    doc = await read_board_doc(ctx)
    cls = await asyncio.to_thread(classify, model, doc)
    ground = ground_nets_of(cls)
    bad = [n for n in fargs.relock_nets if n in ground]
    if bad:
        raise _invalid(
            f"{', '.join(bad[:10])} {'is a ground net' if len(bad) == 1 else 'are ground nets'}",
            "Ground/plane nets are never relocked (their fanout stays locked); pass signal nets.",
        )
    plan = await asyncio.to_thread(plan_fill, model, cls, fargs.plane_layers)
    if not plan.open and not fargs.relock_nets:
        raise _invalid(
            "nothing to fill: every non-ground connection is routed",
            "Continue with pcb_zones / pcb_fanout (ground connects through planes).",
        )
    st = _FillState(
        args=fargs,
        plan=plan,
        rt=rt,
        workdir=_fill_dir(ctx),
        pre=model,
        pre_hash=await asyncio.to_thread(placement_hash, model),
        cls=cls,
    )
    report: dict[str, Any] = {
        "logical_changes": FILL_COST,
        "budget": None,  # filled by the adapter
        "plane_layers": list(plan.plane_layers),
        "plane_layer_mechanism": PLANE_LAYER_MECHANISM if plan.plane_layers else None,
        "open_before": len(plan.open),
    }

    def finish(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        cs.read_back[:] = [m]
        return []

    cs = FillChangeSet(
        tool="pcb_route_fill",
        logical=Logical(FILL_COST),
        kind="board_route_fill",
        board_stages=fill_stages(st),
        snapshot="full",
        verify=finish,
        report=report,
        temp_dirs=[st.workdir],
        state=st,
        doc=doc,
    )
    return cs


def _completion(
    model: BoardModel, ground: frozenset[str]
) -> tuple[float, list[dict[str, Any]], int, int]:
    nets = sorted({q.net for p in model.parts for q in p.pads if q.net and q.net not in ground})
    total = 0
    open_: list[dict[str, Any]] = []
    for net in nets:
        conns = [(a, b) for a, b in connections(model, net) if not _joined(a, b)]
        total += len(conns)
        for a, b in _unrouted(model, net):
            open_.append(
                {
                    "connection": conn_key(net, a, b),
                    "net": net,
                    "a": f"{a.ref}.{a.number}",
                    "b": f"{b.ref}.{b.number}",
                    "x": round((a.x + b.x) / 2, 3),
                    "y": round((a.y + b.y) / 2, 3),
                }
            )
    done = total - len(open_)
    return (done / total if total else 1.0), open_, done, total


def _next(open_: Sequence[Mapping[str, Any]], st: _FillState) -> str:
    """The ladder (spec §10.3): never 'done' with open connections."""
    if not open_:
        return NEXT_DONE
    nets = sorted({o["net"] for o in open_})[:10]
    return (
        f"{len(open_)} connection(s) still open ({', '.join(nets)}). Ladder: (a) pcb_route_fill "
        f"again with more passes (now {st.args.passes}); (b) pcb_unroute {{nets: [the critical "
        "signal nets next to the open connections], unlock_only: true}, then pcb_route_fill "
        "{relock_nets: [those nets]}; (c) pcb_route {nets: [the open nets]}; (d) show the user "
        "the open connections (pcb_render with markers) and ask: placement change, more "
        "layers or a larger board. Never call the board done with open connections."
    )


async def _final_metrics(model: BoardModel, doc: BoardDoc) -> MetricsReport:
    """The report's M4/M5/M7/M12 in the CPU worker, fixed budget (a seam for tests)."""
    report: MetricsReport = await run_cpu(
        metrics_job,
        strip_fills(model),
        doc,
        None,
        preset_for(model, doc),
        frozenset({"M4", "M5", "M7", "M12"}),
        timeout_s=METRICS_TIMEOUT_S,
        wait_s=METRICS_TIMEOUT_S,
    )
    return report


async def apply_fill(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    """Apply a `pcb_route_fill` change set; the report gets completion, fill counts, the
    open connections (≤ 50 with positions), the ladder's `next` and the metrics."""
    assert isinstance(cs, FillChangeSet) and cs.state is not None and cs.doc is not None
    st = cs.state
    t0 = time.monotonic()
    result = await apply_board_change_set(ctx, cs)
    data = result.data if isinstance(result.data, dict) else {}
    m = cs.read_back[0] if cs.read_back else await board_model(ctx)
    completion, open_, done, total = await asyncio.to_thread(_completion, m, st.plan.ground_nets)
    before = {k for k, *_ in st.plan.open}
    data["completion"] = round(completion, 4)
    data["connections"] = {"routed": done, "total": total}
    data["fill_routed"] = len(st.fill_keys)
    data["fill_connections_before"] = len(before)
    data["rerouted"] = len(st.rerouted)
    data["rejected"] = st.rejected[:MAX_LISTED]
    data["open"] = open_[:MAX_LISTED]
    if len(open_) > MAX_LISTED:
        data["open_omitted"] = len(open_) - MAX_LISTED
    data.setdefault("relocked", [])
    data.setdefault("relock_failed", [])
    notes: list[str] = list(data.get("notes", []))
    try:
        report = await _final_metrics(m, cs.doc)
    except StructuredToolError as exc:
        if exc.code != "board_compute_timeout":
            raise
        data["metrics"] = None
        notes.append("metrics timed out; run pcb_metrics or pcb_check")
    else:
        full = await asyncio.to_thread(metrics_data, report, m)
        data["metrics"] = {
            "hard_failures": full["hard_failures"],
            "metrics": [{k: mt[k] for k in ("id", "value", "verdict")} for mt in full["metrics"]],
        }
    if notes:
        data["notes"] = notes
    data["next"] = _next(open_, st)
    data["seconds"] = round(time.monotonic() - t0, 2)
    return result
