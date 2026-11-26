"""Board analysis read tools (SP4 spec §7.1, P3a Task 5): `pcb_analyze` (part/net
classification, B-C6), `pcb_metrics` (M1-M16, B-C7) and `preset_for` (B-C6 fab preset
rule). Read tools: one board-model
read, the `board.md` tables, the computation in the CPU worker (`run_cpu`, job functions in
`copperchat.board.jobs`); never the edit lock, nothing written."""

from __future__ import annotations

import asyncio
import time
from typing import Any

from copperchat.board.boardmd import BoardDoc, read_board_doc
from copperchat.board.jobs import SCOPE_KEYS, Scope, analyse_job, metrics_job, strip_fills
from copperchat.board.metrics import METRIC_IDS, Metric, MetricsReport, Offender
from copperchat.board.model import BoardModel, board_hash, placement_hash
from copperchat.board.presets import PRESETS, FabPreset
from copperchat.board.worker import run_cpu
from copperchat.errors import StructuredToolError
from copperchat.kicad_tools.board import api
from copperchat.kicad_tools.board.read import board_model, require_board
from copperchat.tools.spec import ToolContext, ToolResult

# run_cpu budget of one analysis job (plan Task 5)
ANALYSIS_TIMEOUT_S = 30.0
# the DRC task behind M2/M3 (B-C4: an explicit, longer session.call timeout)
METRICS_DRC_TIMEOUT_S = 60.0


def preset_for(model: BoardModel, doc: BoardDoc) -> FabPreset | None:
    """The fab preset of a board (B-C6): the one `board.md` names when it is known, else the
    JLCPCB preset of the copper layer count (None for counts without a preset)."""
    if doc.preset is not None and doc.preset in PRESETS:
        return PRESETS[doc.preset]
    return PRESETS.get(f"jlcpcb-{len(model.copper_layers)}l")


def _invalid(what: str, why: str) -> StructuredToolError:
    return StructuredToolError(what, why, "see the tool schema", code="invalid_argument")


def _scope(args: dict[str, Any]) -> Scope:
    scope = args.get("scope", "all")
    if not isinstance(scope, str) or scope not in SCOPE_KEYS:
        raise _invalid("Invalid scope", f"scope must be one of {', '.join(SCOPE_KEYS)}")
    return scope  # type: ignore[return-value]


def _only(args: dict[str, Any]) -> frozenset[str] | None:
    only = args.get("only")
    if only is None:
        return None
    if (
        not isinstance(only, list)
        or not only
        or not all(isinstance(m, str) and m in METRIC_IDS for m in only)
        or len(set(only)) != len(only)
    ):
        raise _invalid(
            "Invalid only",
            f"only must be a non-empty list of distinct metric ids ({METRIC_IDS[0]}"
            f"-{METRIC_IDS[-1]})",
        )
    return frozenset(only)


async def pcb_analyze(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Classification of the live board (`analyse_job`) for `args["scope"]` (default all);
    `invalid_argument` for an unknown scope, `no_board` without a board."""
    scope = _scope(args)
    require_board(ctx)
    model = await board_model(ctx)
    doc = await read_board_doc(ctx)
    data = await run_cpu(analyse_job, model, doc, scope, timeout_s=ANALYSIS_TIMEOUT_S)
    return ToolResult(data=data)


def _offender(o: Offender) -> dict[str, Any]:
    return {
        "what": o.what,
        "refs": list(o.refs),
        "nets": list(o.nets),
        "at": None if o.at is None else [round(o.at[0], 4), round(o.at[1], 4)],
        "value": o.value,
    }


def _metric(m: Metric) -> dict[str, Any]:
    return {
        "id": m.id,
        "value": m.value,
        "verdict": m.verdict,
        "hard": m.hard,
        "review": m.review,
        "offenders": [_offender(o) for o in m.offenders],
        "omitted": m.omitted,
    }


def metrics_data(report: MetricsReport, model: BoardModel) -> dict[str, Any]:
    """The `pcb_metrics` result of a report (pure), without `seconds`."""
    return {
        "stage": report.stage,
        "metrics": [_metric(m) for m in report.metrics],
        "hard_failures": report.hard_failures,
        "reviews": report.reviews,
        "placement_hash": placement_hash(model),
        "board_hash": board_hash(model),
    }


async def pcb_metrics(ctx: ToolContext, args: dict[str, Any]) -> ToolResult:
    """Metrics M1-M16 of the live board (`metrics_job` in the CPU worker); the DRC task runs
    only when M2 or M3 is requested; parity markers belong to the full check (P3b)."""
    start = time.monotonic()
    only = _only(args)
    require_board(ctx)
    model = await board_model(ctx)
    doc = await read_board_doc(ctx)
    drc = None
    if only is None or only & {"M2", "M3"}:
        drc = await ctx.session.call(
            lambda h: api.run_drc_task(h, timeout_s=METRICS_DRC_TIMEOUT_S),
            timeout=api.drc_call_timeout(METRICS_DRC_TIMEOUT_S),
        )
    job_model = model if only is None or "M11" in only else strip_fills(model)
    report = await run_cpu(
        metrics_job,
        job_model,
        doc,
        drc,
        preset_for(model, doc),
        only,
        None,
        timeout_s=ANALYSIS_TIMEOUT_S,
    )
    # the hashes serialise the whole board: in a thread, not on the event loop
    data = await asyncio.to_thread(metrics_data, report, model)
    data["seconds"] = round(time.monotonic() - start, 3)
    return ToolResult(data=data)
