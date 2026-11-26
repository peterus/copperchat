"""`pcb_tidy_silk` (SP4 spec §7.2 "Copper", P7 Task 5): every reference designator next to
its part, off pads and other silk, upright, on the part's side, in the fab preset's
minimum size (JLC: 1.0 mm height, 0.15 mm line - index call 8, not the spec's 0.8 mm).

Placement is pure (`cc/board/silk.py`). Three stages, each with an `undo` that writes the
stored pre-call reference fields back (position, angle, size, thickness, layer,
visibility: text attributes are no items, the KIID diff cannot restore them): (1) the
plan applied, unresolved refs hidden (one commit); (2) the DRC task - a reference named by
a silk marker is re-planned without that spot (one commit); (3) still named → hidden
(`visible = false`) with a report line (one commit). No approval needed - but the change
makes a board approval stale (`board_hash`)."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from kipy.board_types import Field as KipyField
from kipy.geometry import Vector2
from kipy.proto.common.types import enums_pb2
from kipy.util.board_layer import layer_from_canonical_name

from copperchat.board.boardmd import read_board_doc
from copperchat.board.jobs import silk_plan_job, strip_fills
from copperchat.board.model import BoardModel, Part, TextItem, board_hash
from copperchat.board.silk import RefPlacement
from copperchat.board.worker import run_cpu
from copperchat.kicad_session import KicadHandle
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.analysis import preset_for
from copperchat.kicad_tools.board.api import DrcResult
from copperchat.kicad_tools.board.engine import (
    BoardChangeSet,
    BoardStage,
    BoardStageContext,
    apply_board_change_set,
)
from copperchat.kicad_tools.board.read import BOARD_READ_TIMEOUT_S, board_model, require_board
from copperchat.kicad_tools.board.setup import require_board_tools
from copperchat.kicad_tools.board.zones import keys
from copperchat.kicad_tools.edit.changeset import ChangeSet, Logical
from copperchat.kicad_tools.units import mm_to_nm
from copperchat.tools.spec import ToolContext, ToolResult

__all__ = ["SILK_TYPES", "apply_tidy_silk", "prepare_tidy_silk"]

# KiCad API DRC types (lower case, DRCET_ stripped) that concern a silk text; the kicad-cli
# name of `silk_clearance` is `silk_overlap`.
SILK_TYPES = frozenset(
    {
        "silk_clearance",
        "silk_edge_clearance",
        "silk_mask_clearance",
        "text_height",
        "text_thickness",
        "mirrored_text_on_front_layer",
        "nonmirrored_text_on_back_layer",
    }
)
DRC_TIMEOUT_S = 60.0
PLAN_TIMEOUT_S = 30.0  # run_cpu budget of one reference plan
DEFAULT_HEIGHT_MM, DEFAULT_LINE_MM = 1.0, 0.15  # JLC preset values (no preset known)
MAX_LISTED = 50


def _ref_kiids(model: BoardModel) -> dict[str, str]:
    """reference text KIID -> ref."""
    return {k: p.ref for p in model.parts for k, role in p.item_kiids if role == "reference"}


def _silk_markers(drc: DrcResult) -> list[api.DrcMarker]:
    return [m for m in drc.markers if m.type in SILK_TYPES and m.severity != "exclusion"]


def _named_refs(drc: DrcResult, by_kiid: Mapping[str, str]) -> dict[str, str]:
    """ref -> the first silk marker type naming its reference text."""
    out: dict[str, str] = {}
    for m in _silk_markers(drc):
        for k in m.kiids:
            if k in by_kiid:
                out.setdefault(by_kiid[k], m.type)
    return out


# -- KiCad writes --------------------------------------------------------------------------


def _set_ref(f: KipyField, pl: RefPlacement, height: float, line: float) -> None:
    t = f.text
    t.position = Vector2.from_xy(mm_to_nm(pl.at[0]), mm_to_nm(pl.at[1]))
    a = t.attributes
    a.angle = pl.rot
    a.size = Vector2.from_xy(mm_to_nm(height), mm_to_nm(height))
    a.stroke_width = mm_to_nm(line)
    a.mirrored = pl.layer == "B.SilkS"
    a.horizontal_alignment = enums_pb2.HA_CENTER
    a.vertical_alignment = enums_pb2.VA_CENTER
    layer = layer_from_canonical_name(pl.layer)
    f.layer = layer
    t.layer = layer
    f.visible = True


def _write(
    h: KicadHandle,
    fp_of: Mapping[str, str],
    placements: Sequence[RefPlacement],
    hide: Sequence[str],
    height: float,
    line: float,
    saved: dict[str, bytes],
) -> None:
    """One commit's writes: the placements, then `hide`; stores every touched footprint's
    pre-call reference field in `saved` (first time only)."""
    board = h.board()
    want = {fp_of[pl.ref] for pl in placements} | {fp_of[r] for r in hide}
    fps = {str(fp.id.value): fp for fp in board.get_footprints() if str(fp.id.value) in want}
    for k, fp in fps.items():
        saved.setdefault(k, fp.proto.reference_field.SerializeToString())
    for pl in placements:
        _set_ref(fps[fp_of[pl.ref]].reference_field, pl, height, line)
    for r in hide:
        fps[fp_of[r]].reference_field.visible = False
    if fps:
        board.update_items(list(fps.values()))


def _restore(h: KicadHandle, saved: Mapping[str, bytes]) -> None:
    board = h.board()
    fps = [fp for fp in board.get_footprints() if str(fp.id.value) in saved]
    for fp in fps:
        fp.proto.reference_field.ParseFromString(saved[str(fp.id.value)])
    if fps:
        board.update_items(fps)


# -- the tool ------------------------------------------------------------------------------


@dataclass
class _Silk:
    height: float
    line: float
    plan: tuple[RefPlacement, ...]
    unresolved: tuple[str, ...]
    by_kiid: dict[str, str]
    fp_of: dict[str, str]
    current: dict[str, RefPlacement] = field(default_factory=dict)
    hidden: dict[str, str] = field(default_factory=dict)  # ref -> reason
    replanned: list[str] = field(default_factory=list)
    saved: dict[str, bytes] = field(default_factory=dict)
    markers_before: int = 0
    markers_after: int | None = None
    resized: int = 0


def _with_refs(model: BoardModel, st: _Silk) -> BoardModel:
    """`model` with the references at their stage-1 placements and the hidden ones
    invisible (the re-planning obstacles)."""

    def moved(p: Part) -> Part:
        if p.ref in st.hidden:  # hidden in stage 1: no obstacle any more
            texts = tuple(
                dataclasses.replace(t, visible=False) if t.kind == "reference" else t
                for t in p.texts
            )
            return dataclasses.replace(p, texts=texts)
        pl = st.current.get(p.ref)
        if pl is None:
            return p
        texts = tuple(
            dataclasses.replace(
                t,
                at=pl.at,
                angle=pl.rot,
                layer=pl.layer,
                size=(st.height, st.height),
                thickness=st.line,
                visible=True,
            )
            if t.kind == "reference"
            else t
            for t in p.texts
        )
        return dataclasses.replace(p, texts=texts)

    return dataclasses.replace(model, parts=tuple(moved(p) for p in model.parts))


def _stages(st: _Silk, model: BoardModel) -> list[BoardStage]:
    async def undo(sctx: BoardStageContext) -> None:
        if not st.saved:
            return
        async with sctx.ctx.session.commit("copperchat: pcb_tidy_silk: undo", doc="pcb"):
            await sctx.call(lambda h: _restore(h, st.saved), timeout=BOARD_READ_TIMEOUT_S)

    async def apply_plan(sctx: BoardStageContext) -> None:
        hide = list(st.unresolved)
        await sctx.call(
            lambda h: _write(h, st.fp_of, st.plan, hide, st.height, st.line, st.saved),
            timeout=BOARD_READ_TIMEOUT_S,
        )
        st.current = {pl.ref: pl for pl in st.plan}
        for r in hide:
            st.hidden[r] = "no free spot next to the part"

    async def recheck(sctx: BoardStageContext) -> None:
        drc = await sctx.drc(timeout_s=DRC_TIMEOUT_S)
        named = {r: t for r, t in _named_refs(drc, st.by_kiid).items() if r in st.current}
        if not named:
            return
        exclude = {r: frozenset({st.current[r].candidate}) for r in named}
        keep = frozenset(st.current) - frozenset(named)
        base = _with_refs(model, st)
        plan, unresolved = await run_cpu(
            silk_plan_job,
            strip_fills(base),
            height=st.height,
            line=st.line,
            skip=keep | frozenset(st.hidden),
            exclude=exclude,
            timeout_s=PLAN_TIMEOUT_S,
        )
        plan = tuple(pl for pl in plan if pl.ref in named)
        hide = [r for r in unresolved if r in named]
        async with sctx.ctx.session.commit("copperchat: pcb_tidy_silk", doc="pcb"):
            await sctx.call(
                lambda h: _write(h, st.fp_of, plan, hide, st.height, st.line, st.saved),
                timeout=BOARD_READ_TIMEOUT_S,
            )
        for pl in plan:
            st.current[pl.ref] = pl
        for r in hide:
            st.current.pop(r, None)
            st.hidden[r] = f"{named[r]}: no other free spot"
        st.replanned = sorted(named)

    async def hide_failing(sctx: BoardStageContext) -> None:
        drc = await sctx.drc(timeout_s=DRC_TIMEOUT_S)
        named = {r: t for r, t in _named_refs(drc, st.by_kiid).items() if r in st.current}
        if named:
            async with sctx.ctx.session.commit("copperchat: pcb_tidy_silk", doc="pcb"):
                await sctx.call(
                    lambda h: _write(h, st.fp_of, (), list(named), st.height, st.line, st.saved),
                    timeout=BOARD_READ_TIMEOUT_S,
                )
            for r, t in named.items():
                st.current.pop(r, None)
                st.hidden[r] = f"{t} after re-planning"
            drc = await sctx.drc(timeout_s=DRC_TIMEOUT_S)
        st.markers_after = len(_silk_markers(drc))

    return [
        BoardStage("place references", apply_plan, commit=True, undo=undo),
        # stage 1's undo restores every reference any stage touched (`saved` grows)
        BoardStage("re-plan DRC-flagged", recheck, commit=False),
        BoardStage("hide still failing", hide_failing, commit=False),
    ]


@dataclass(kw_only=True)
class SilkChangeSet(BoardChangeSet):
    state: _Silk
    before_hash: str = ""


def _resized(model: BoardModel, refs: Sequence[str], height: float, line: float) -> int:
    n = 0
    for r in refs:
        t = next((t for t in model.part(r).texts if t.kind == "reference"), None)
        if t is not None and (
            abs(t.size[1] - height) > 1e-6
            or abs(t.size[0] - height) > 1e-6
            or abs(t.thickness - line) > 1e-6
        ):
            n += 1
    return n


def _moved(before: BoardModel, after: BoardModel) -> int:
    def ref_text(m: BoardModel) -> dict[str, TextItem]:
        return {p.ref: t for p in m.parts for t in p.texts if t.kind == "reference"}

    a, b = ref_text(before), ref_text(after)
    return sum(1 for r, t in b.items() if r in a and (t.at != a[r].at or t.angle != a[r].angle))


async def prepare_tidy_silk(ctx: ToolContext, args: dict[str, Any]) -> ChangeSet:
    """Plan `pcb_tidy_silk` (module docstring)."""
    require_board_tools(ctx)
    keys(args, set())
    require_board(ctx)
    model = await board_model(ctx)
    doc = await read_board_doc(ctx)
    preset = preset_for(model, doc)
    height = preset.silk_min_height if preset is not None else DEFAULT_HEIGHT_MM
    line = preset.silk_min_line if preset is not None else DEFAULT_LINE_MM
    plan, unresolved = await run_cpu(
        silk_plan_job, strip_fills(model), height=height, line=line, timeout_s=PLAN_TIMEOUT_S
    )
    drc = await ctx.session.call(
        lambda h: api.run_drc_task(h, timeout_s=DRC_TIMEOUT_S),
        timeout=api.drc_call_timeout(DRC_TIMEOUT_S),
    )
    st = _Silk(
        height,
        line,
        plan,
        unresolved,
        _ref_kiids(model),
        {p.ref: p.kiid for p in model.parts},
    )
    st.markers_before = len(_silk_markers(drc))
    st.resized = _resized(model, [pl.ref for pl in plan], height, line)
    stages = _stages(st, model) if (plan or unresolved) else []

    def verify(m: BoardModel, rep: dict[str, Any]) -> list[str]:
        problems: list[str] = []
        for p in m.parts:
            t = next((t for t in p.texts if t.kind == "reference"), None)
            if t is None or p.ref not in st.current:
                continue
            pl = st.current[p.ref]
            if abs(t.at[0] - pl.at[0]) > 1e-3 or abs(t.at[1] - pl.at[1]) > 1e-3:
                problems.append(f"{p.ref}'s reference is at {t.at}, planned {pl.at}")
            if abs((t.angle - pl.rot + 180) % 360 - 180) > 0.1:
                problems.append(f"{p.ref}'s reference is at {t.angle:g}°, planned {pl.rot:g}°")
            if not t.visible or t.layer != pl.layer:
                problems.append(f"{p.ref}'s reference is on {t.layer} (visible={t.visible})")
        for r in st.hidden:
            t = next((t for t in m.part(r).texts if t.kind == "reference"), None)
            if t is not None and t.visible:
                problems.append(f"{r}'s reference is still visible")
        hidden = [{"ref": r, "reason": why} for r, why in sorted(st.hidden.items())]
        after_hash = board_hash(m)
        rep.update(
            {
                "moved": _moved(model, m),
                "resized": st.resized,
                "height_mm": st.height,
                "line_mm": st.line,
                "replanned": st.replanned[:MAX_LISTED],
                "hidden": hidden[:MAX_LISTED],
                "hidden_omitted": max(0, len(hidden) - MAX_LISTED),
                "silk_markers_before": st.markers_before,
                "silk_markers_after": st.markers_after,
                "board_hash": {
                    "before": cs.before_hash,
                    "after": after_hash,
                    "changed": after_hash != cs.before_hash,
                },
            }
        )
        return problems

    cs = SilkChangeSet(
        tool="pcb_tidy_silk",
        logical=Logical(1 if stages else 0),
        kind="board_tidy_silk",
        board_stages=stages,
        verify=verify,
        report={"logical_changes": 1 if stages else 0, "budget": None},
        state=st,
        before_hash=board_hash(model),
    )
    return cs


async def apply_tidy_silk(ctx: ToolContext, cs: ChangeSet) -> ToolResult:
    assert isinstance(cs, SilkChangeSet)
    if not cs.board_stages:
        return ToolResult(data=dict(cs.report) | {"moved": 0, "hidden": [], "next": "pcb_check"})
    result = await apply_board_change_set(ctx, cs)
    data = result.data if isinstance(result.data, dict) else {}
    data["next"] = "pcb_check level=full"
    return result
